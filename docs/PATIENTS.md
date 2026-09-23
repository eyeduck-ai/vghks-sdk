# 病人與掛號查詢

| 方法 | 回傳 | 來源 |
| --- | --- | --- |
| `sdk.patients.get_demographics(mrn)` | `PatientDemographics` | AJAX CHECK_PAT，精簡身分與聯絡資料 |
| `sdk.patients.get_basic_info(mrn)` | `PatientBasicInfo` | QUY15W001，完整基本資料及頁面顯示的就醫 context |
| `sdk.patients.get_registration_history(mrn)` | `list[RegistrationRecord]` | RSV11W001，掛號清單與後續分頁 |

三者能獨立使用，不必先呼叫另一個公開方法。`mrn` 是病歷號；登入帳號／醫師卡號不作為病人識別碼。

以病人身分證找就診清單請用 `sdk.records.get_visit_cases(national_id=...)`；每筆 VisitCase 的 `mrn` 是已核對的病歷號，可以再交給上述病人方法。身分證不能直接填進這些方法的 `mrn` 參數。就診欄位與篩選方式見 [VISITS](VISITS.md)。

```python
from vghks_sdk.models import to_jsonable

basic = sdk.patients.get_basic_info(mrn)
registrations = sdk.patients.get_registration_history(mrn)
payload = to_jsonable(basic)

for row in registrations:
    print(row.visit_date, row.section_code, row.section_name, row.room, row.sequence_no)

# 目錄入口呼叫相同原子操作。
basic = sdk.queries.run("webmaas.basic_info", mrn=mrn)
```

## 基本資料

提供 `mrn`、`name`、`national_id`、`birthday`、`sex`、`age`、`blood_type`、`height`、`weight`、`nationality`、`phone`、`address`、`contact`，以及 `ward_bed`、`section`、`insurance`、`admitted_at`、`discharge_notified_at`、`discharged_at`、`case_no`、`admission_diagnosis`。

`fields` 保留 33 個原始標籤欄位，包括尚未映射為屬性的內容；`notices` 保留「此住院病人已出院」等提示；`raw_html` 保留原頁面供 debug。生日轉為 `date`，未知單位或格式的量測與時間欄位保留原字串。

新 HAR 選擇 **type=A（住院）**。`get_basic_info` 重現這條路徑，附帶資料可能是已出院的紀錄，不能據此推定病人目前住院。急診選項尚未錄製完整查詢，SDK 不宣稱已支援其格式。

基本資料頁 URL 與 page id 是 `QUY15`，但 HAR 的 SSO `externalRoles` 實際為 **maas_QRY15**；掛號是 **maas_RSV11**。SDK 以當次登入取得 bridge keys 並切換角色，重新登入後也重新取得頁面 token。

## 掛號結果

每筆 `RegistrationRecord` 提供：

- `mrn`、`name`：回傳病人識別。
- `visit_date`、`section_code`、`section_name`、`room`、`sequence_no`：日期、科別、診別與序號，保留序號前導零。
- `expected_time`、`status`：預計看診時間與原始狀態。
- `registered_at`、`registered_by`、`cancelled_at`、`cancelled_by`、`notes`：掛號／取消紀錄。
- `columns`：完整原始欄位，保留未來新增欄位。

`visit_date` 支援西元與民國年格式。空白狀態保持空白；掛號與實際有無看診是不同資訊。判斷實際就診應使用 `sdk.records.get_visit_cases(mrn)`，需要時再取該次 SOAP。

本次未加入未錄製的伺服器日期區間參數。方法循下一頁讀完並排除完全相同的重複列；應用可依 `visit_date` 篩選。要增加其他伺服器篩選條件，再錄對應 HAR。

## 空結果與錯誤

正常查無掛號回傳 `[]`。未知版型、缺 token、病人不符、分頁迴圈或超過 50 頁會丟出帶穩定錯誤碼的 `SDKError` 子類別。分頁連結只能指向同一服務、同一掛號端點與同一病人。

基本資料明確查無病人回報 `WEBMAAS_PATIENT_NOT_FOUND`；缺 DETAIL 或識別欄位屬解析錯誤。HTTP 401／403 或登入過期會由 Runtime 至多重新登入一次，重新建立 SSO、token 與病人 context。

## 驗證

2026-09-20 的內網回傳已確認三項病人查詢可取得資料；合成測試另涵蓋空值、分頁、身分不符與登入過期。實際欄位會依病人與就醫狀態不同，不能把錄製樣本的筆數寫成程式假設。

comprehensive 計畫包含病人查詢；雙擊採用建置時的 profile，登入專項不查病人。開發時只測此模組，可執行 `vghks-live-test.exe --config configs/patient-queries.example.json`。ZIP 的 `parsed/atomic/` 下分別有 `webmaas.basic_info/`、`webmaas.demographics/`、`webmaas.registration_query/`，每項獨立留下 HTTP capture 與結果。病歷號由 `--test-mrn`、設定、環境變數或自用 EXE 的內嵌預設提供；公開版沒有真實預設值，互動執行時會提示輸入。
