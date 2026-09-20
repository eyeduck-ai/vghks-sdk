# 用原子功能組合任務

應用程式建立 VghksSDK，在 `with` 區塊內完成查詢。Service 回傳模型及下一步參照；workflow 決定篩選、去重、錯誤續跑及存檔。import 不應登入或執行查詢。

## 單次就診與期間查詢

```python
from vghks_sdk import OrderHistoryFilter

cases = sdk.records.get_visit_cases(mrn)
for case in cases:
    orders = sdk.orders.get_case_orders(case)
    soap = sdk.records.get_soap(case)

# 直接向伺服器查跨就診醫囑。
history = sdk.orders.get_order_history(mrn, OrderHistoryFilter(lookback_days=365))
```

case 類操作保留就診日期、類型、科別及 case_no。history 類操作使用各自的 Filter；醫囑、藥囑、數值、手術的條件不同，不共用任意字典。日曆日期與回溯天數限制以模型驗證為準。

## 醫囑 → 報告 → PDF／JPG

```python
from pathlib import Path
from vghks_sdk.workflows import collect_order_reports

result = collect_order_reports(
    sdk,
    order_sources={"history": history},
    output_dir=Path("output/eye-reports"),
    terms=("DBR", "Microsonography"),
)
```

自行組合時可使用 ClinicalOrder 的 `detail_ref`、`report_ref`、`pacs_ref`，依實際非空參照呼叫對應方法。不同醫囑可能提供不同分支。

NOT_EXECUTED 通常沒有報告，組合流程保留原醫囑後跳過。PDF、文字及 JPG 分支彼此獨立。JPG 檢視器的明確「查無資料」會回空圖片清單；未知頁面、登入頁及損壞檔案則保留錯誤。

`records.get_text_report_history` 是各科報告入口。眼科完整項目宜由醫囑清單發現；可以將兩個入口按參照去重，但不能假設涵蓋相同檢查。

## 最近七天門診與 SOAP

```python
from datetime import date, timedelta
from pathlib import Path
from vghks_sdk.search import DoctorOpdPatientSource
from vghks_sdk.workflows import scan_opd_soap

end = date.today()
result = scan_opd_soap(
    sdk,
    source=DoctorOpdPatientSource(login_card, end - timedelta(days=6), end),
    output_dir=Path("output/opd-soap"),
)
```

依回傳醫師欄辨識專屬／共用清單，再比對同一天就診，最後搜尋 SOAP 的 arrange CATA。三階段輸出與錯誤分開保存。有掛號不等於有就診；空醫師欄不能補成查詢帳號。

## 手術與審查

`surgery.get_cases(filter)` → 案例的 `reference` → `get_record_ref(ref)` → `download_record(note_ref)`。使用 `collect_surgery_records` 合併期間、去重並保存各階段。見 [手術範例](../examples/surgery_records.py)。

`reviews.get_cases(filter)` → 案件的 `reference` → `get_case`、`get_orders`、`get_attachments`、`get_pacs`。四項可獨立失敗，應分別存檔。審查同意、送件完成、上傳完成不可混用。見 [審查文件](REVIEWS.md)。

## 穩定組合的約定

- 用回傳的型別參照，保留病人與就診綁定，不自行拼接 URL／檔案路徑。
- 捕捉 SDKError 並保存 `exc.info`；保留空結果、沒有候選、依賴失敗、查詢失敗的差別。
- 同一 Session 有共享 context，Runtime 會鎖定相關操作；不要並行呼叫底層 Requests。
- 異動逾時後先讀回查證，不能直接補送。
- 日期、抽樣上限及輸出目錄由應用提供，病歷號不可寫死於 SDK 或公開範例。

其他 workflows：`export_latest_records`、`export_visit_history`、`export_patient_records`、`scan_soap`，分別處理最新紀錄、就診歷史、病人資料收集、指定來源 SOAP 搜尋。組合邏輯可直接閱讀與擴充。
