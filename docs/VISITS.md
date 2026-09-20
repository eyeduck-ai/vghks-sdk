# 病人就診搜尋與篩選

`records.get_visit_cases` 先取得伺服器回傳的就診清單；`VisitFilter` 在本機篩選，所選 VisitCase 可直接交給 SOAP、醫囑等單次就診方法。SDK 不會為每個篩選條件重新下載清單，也不會先下載所有 SOAP 再找日期。

## 以病歷號或身分證查詢

```python
# 二擇一；身分證請使用 keyword，第一個 positional 參數仍是病歷號。
cases = sdk.records.get_visit_cases(mrn)
cases_by_id = sdk.records.get_visit_cases(national_id=national_id)
```

病歷號沿用已驗證的查詢路徑。身分證路徑依 HAR 錄到的前端表單使用 `type=2`，自動轉大寫並建立病人 context；接著核對病人標頭的身分證及實際病歷號，再取就診清單。後續 VisitCase.mrn 使用病歷號，不會把身分證當成病歷號。兩個參數同時提供、皆未提供、空字串或不合法字元，會在連線前報錯。

**身分證路徑已完成 0.17.2 與 0.18.0 內網實測**：先從基本資料取得身分證，查詢後核對病人，與病歷號清單及醫師／科別欄位比對一致，並從身分證清單選定門診成功取得 SOAP／醫囑。本次證據是一名病人的非空清單與一筆門診抽樣。0.18.1 另以原回應離線修正分支選擇，取回原先漏掉的住院／急診醫師卡號；門診來源卡號仍空白。無法辨識查詢成功頁或身分不符時仍明確報錯，不會沿用上一位病人的 Session 資料。

## 可用欄位

| 畫面／用途 | VisitCase 屬性 | 說明 |
| --- | --- | --- |
| 到院日 | `visit_date` | `datetime.date`；來源缺失或無法解析為 `None` |
| 類別 | `case_type`／`case_type_label` | `O` 門診、`A` 住院、`E` 急診；其他原始代碼保留 |
| 科別 | `section_code`／`section_name` | 保留代碼及畫面名稱，例如含上午／下午的科別名稱 |
| 醫師 | `doctor_name`／`doctor_card` | 姓名取自清單醫師欄；卡號僅在回傳有 `vsNo` 時存在 |
| 病人／就診識別 | `mrn`／`case_no`／`index` | 後續 Adapter 自動使用，不必自行組 URL |
| 就診導覽參數 | `detail_params` | 保留來源必要參數；呼叫時使用當前登入 HID |

空醫師欄保持空白，不推定為登入醫師。醫師卡號不從姓名反查，也不套每日門診清單的 F 後綴歸屬規則。醫師欄屬於這筆就診，不能由科別代碼推定。

同一頁可能包含同次就診的兩個互斥 JavaScript 建構式。Parser 以既有的靜態條件白名單選擇實際分支後才去重，不合併分支、不執行 JavaScript；無法判定的條件明確報錯。只有選中分支的卡號及導覽參數可以交給下游查詢。

## 四個維度一起篩選

```python
from datetime import date
from vghks_sdk import VisitFilter

selector = VisitFilter(
    start_date=date(2026, 1, 1),
    end_date=date(2026, 1, 31),
    case_types=("O",),
    section_name_contains=("眼科",),
    doctor_names=("測試醫師甲",),  # 替換為回傳清單內的姓名
)

# 同份清單可以反覆套用不同 selector，不增加 HTTP。
selected = selector.select(cases)

for case in selected:
    soap = sdk.records.get_soap(case)
    orders = sdk.orders.get_case_orders(case)
    # orders 的 detail_ref／report_ref／pacs_ref 可再查明細、報告與圖片。
```

也可以合成一次呼叫：

```python
selected = sdk.records.find_visit_cases(mrn, selector)
selected_by_id = sdk.records.find_visit_cases(
    national_id=national_id, visit_filter=selector,
)
```

| 條件 | 參數與規則 |
| --- | --- |
| 到院日 | `start_date`、`end_date` 一起提供，包含兩端；查單一天就設為相同日期；缺日期的紀錄不符合日期條件 |
| 類別 | `case_types=("O", "A", "E")` 選多種類別；預設只有 `("O",)`，保留原有門診 workflow 行為 |
| 科別 | `section_codes` 精確比對，或 `section_name_contains` 子字串比對；同一維度多條件採 OR |
| 所有科別 | 未限制科別時明確設 `all_sections=True`；不能同時指定科別名稱／代碼 |
| 醫師 | `doctor_names` 完整姓名、`doctor_name_contains` 部分姓名、`doctor_cards` 完整卡號；同一維度採 OR，沒有醫師條件則不限制 |

**日期、類別、科別與醫師之間採 AND。** 結果依原有就診識別去重，按到院日由新到舊排列；同日不同就診或不同科別會保留。日期不明的紀錄在無日期條件時可保留並排在最後。同名醫師無法只靠姓名區分；有回傳卡號時可用卡號篩選。

## 回傳範圍與下游支援

- `get_visit_cases` 不套用眼科、最近一週或筆數上限，解析該次伺服器清單中的所有就診；伺服器未回傳的歷史紀錄無法憑空補齊，不能保證等於終身完整病歷。
- `get_soap`、`get_case_orders`、`get_case_medications`、`get_consults`、`get_treatments` 目前使用已錄製的門診路徑。傳住院／急診會回 `CASE_TYPE_UNSUPPORTED`，需要錄製相應路徑才能擴充。
- 單次數值 `get_numeric_report(case)` 和就診頁籤 `get_case_detail(case)` 保留 case_type，但既有內網成功樣本以門診為主。清單支援某類別不等於該類別所有報告都已驗證。
- 跨期間資料仍使用各自的 `get_order_history(mrn, filter)`、`get_numeric_history` 等功能；本頁日期篩選是就診清單的本機篩選，沒有增加未錄製的伺服器查詢參數。
- 已確認病人而沒有就診回傳 `[]`。未知頁面、身分不符或來源欄位無法解析則拋出 `SDKError`，不要把例外改成空清單。

動態入口也支援 `sdk.queries.run("prq.visit_cases", national_id=national_id)`；QuerySpec.alternative_inputs 描述替代輸入，原有 `mrn` 呼叫相容。輸出保存可用 `to_jsonable`，其中病人／醫師及正文仍是私有資料。

日後新增帳號或不同情境時，可對同一病人分別使用病歷號與身分證取清單，比對 VisitCase.identity 及醫師／科別欄位，再選一筆門診查 SOAP／醫囑。只需驗證新情境，已有成功的其他模組不必為此重測。
