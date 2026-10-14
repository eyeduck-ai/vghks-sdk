# 病人與掛號查詢

| 方法 | 回傳 | 來源 |
| --- | --- | --- |
| `sdk.patients.get_demographics(mrn)` | `PatientDemographics` | AJAX CHECK_PAT，精簡身分與聯絡資料 |
| `sdk.patients.get_basic_info(mrn)` | `PatientBasicInfo` | QUY15W001，完整基本資料及頁面顯示的就醫 context |
| `sdk.patients.get_registration_history(mrn)` | `list[RegistrationRecord]` | RSV11W001，掛號清單與後續分頁 |

三者能獨立使用，不必先呼叫另一個公開方法。`mrn` 是病歷號；登入帳號／醫師卡號不作為病人識別碼。

2026-09-28 聯合醫院帳號錄製的掛號流程，WebMAAS SSO 轉址直接回傳帶有 `RSV11WForm` 與 token 的查詢頁。SDK 首次掛號查詢會一次性使用這份頁面；後續查詢才重新 GET 表單，避免多餘的第二次 GET。HAR 在首次表單與掛號 POST 之間執行 `CHECK_PAT`，因此獨立呼叫 `get_demographics` 後也保留這份尚未提交的表單，供緊接的掛號查詢使用。這是根據 HAR 與應用錯誤碼 `WEBMAAS_QUERY_FORM_MISSING` 所做的修正；舊錯誤 debug 未包含失敗頁原始回應，因此無法回溯證實當時失敗原因。單靠 HAR 的 12 筆離線解析也不能當成 SDK 內網成功；後續實測結果見下文。

高榮帳號先前能取得掛號，但沒有與聯合醫院同輪錄製的成功頁，兩種帳號是否回傳不同 SSO 頁尚不能確定。0.20.10 起先解析 SSO 頁；若該頁沒有表單或 token，才向同一固定掛號頁 GET 一次。兩條路徑均保持病人 AJAX 核對、token 及掛號結果解析，掛號 POST 不因表單問題重送。單一 `regression` EXE 可用兩組獨立帳號比較直接查詢與先查 `CHECK_PAT` 的流程，詳見 [LIVE_TEST](LIVE_TEST.md)。

0.20.10 與 0.20.11 的院內 `regression` ZIP 均證實兩組帳號可各自登入並取得掛號清單。最新 0.20.11 回傳中，高榮帳號直接查詢與先查 `CHECK_PAT` 後查詢各得 1 筆；聯合醫院帳號兩條流程各得 17 筆。同帳號的兩份完整結果相同，且後一流程沒有額外取得掛號表單。兩組帳號查詢的病歷號不同，筆數差異不能解讀為帳號權限差異。這些成功回傳不證明舊應用失敗時的頁面內容，也未自然觸發缺表單備援。

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
- `no_show: bool | None`：已錄製的「未到診」圖示為 `True`，沒有可辨識圖示為 `None`（未知）；解析器不回傳 `False`。
- `columns`：原始欄位的文字值，保留未來新增文字欄位；圖示屬性另由 `no_show` 表達。

`visit_date` 支援西元與民國年格式。空白狀態保持空白；掛號與實際有無看診是不同資訊。判斷實際就診應使用 `sdk.records.get_visit_cases(mrn)`，需要時再取該次 SOAP。

`no_show` 只辨識該列「註記」欄已錄製的 `<label title="未到診"><img src="../images/icon/Rsdefyrc.png"/></label>`。只有文字、只有圖片檔名、未知圖示、其他欄位、script／template 或明示隱藏的標記都不構成此證據。沒有圖示不表示已到診；不由掛號來源、日期、取消欄位或空白狀態推論。原有 `status`、`notes` 與 `columns` 文字保持原值，圖示不改寫成狀態文字。JSON 序列化的 `no_show` 為 `true` 或 `null`。

`registered_by` 保留「掛號人員」的原始帳號／系統代碼，例如 `MWEB` 或 `OPD…`；尚無院方代碼對照，不分類成病人自行掛號或醫師代掛。

```python
for row in registrations:
    if row.no_show is True:
        print(row.visit_date, "來源標示未到診", row.registered_by)
    else:
        print(row.visit_date, "到診狀態未知", row.registered_by)
```

本次未加入未錄製的伺服器日期區間參數。方法循下一頁讀完並排除文字及 `no_show` 都相同的重複列；文字相同而未到診標記不同時保留兩列。應用可依 `visit_date` 篩選。要增加其他伺服器篩選條件，再錄對應 HAR。

## 空結果與錯誤

整合系統在閒置後可用 `sdk.auth.check(only=("webmaas",))` 驗證當次 WebMAAS 表單與 token，再按報告決定是否繼續。0.22.3 已避免只靠快取 Session 回報可用；檢查結果及有限 SSO 複查的處理見 [CONNECTIONS](CONNECTIONS.md)。失敗前若尚未送出病歷號，不能由缺表單錯誤判定該病人沒有資料。缺表單／token 會清除該子系統快取，原錯誤仍保留；下一次查詢重新建立 SSO。

正常查無掛號回傳 `[]`。未知版型、缺 token、病人不符、分頁迴圈或超過 50 頁會丟出帶穩定錯誤碼的 `SDKError` 子類別。分頁連結只能指向同一服務、同一掛號端點與同一病人。

基本資料明確查無病人回報 `WEBMAAS_PATIENT_NOT_FOUND`；缺 DETAIL 或識別欄位屬解析錯誤。HTTP 401／403 或登入過期會由 Runtime 至多重新登入一次，重新建立 SSO、token 與病人 context。

## 驗證

2026-09-20 的內網回傳已確認三項病人查詢可取得資料；合成測試另涵蓋空值、分頁、身分不符與登入過期。實際欄位會依病人與就醫狀態不同，不能把錄製樣本的筆數寫成程式假設。

2026-10-13 另以既有三份掛號 HAR 禁止 socket 重解析：共 37 筆，7 筆「未到診」圖示回傳 `no_show=True`、其餘 30 筆為 `None`；掛號來源代碼及原文字欄位維持一致。離線 replay 共用相同 Parser；合成案例涵蓋未知／隱藏圖示及分頁去重。

2026-10-14 的 0.23.4 批次院內回傳確認 13 個病歷號全部成功，91 筆掛號中 9 筆為 `True`、82 筆為 `None`；24 種來源代碼完整保留。逐人結果與原始 HTML 禁止 socket 重解析完全一致，沒有病人混用。0.23.5 的離線 `registration_batch` 另核對完整輸入、存檔及分頁，原 EXE 狀態維持 OK；本輪沒有分頁、空結果或失敗恢復樣本，這些仍依合成測試判讀。

comprehensive 計畫包含病人查詢；雙擊採用建置時的 profile，登入專項不查病人。開發時只測此模組，可執行 `vghks-live-test.exe --config configs/patient-queries.example.json`。ZIP 的 `parsed/atomic/` 下分別有 `webmaas.basic_info/`、`webmaas.demographics/`、`webmaas.registration_query/`，每項獨立留下 HTTP capture 與結果。病歷號由 `--test-mrn`、設定、環境變數或自用 EXE 的內嵌預設提供；公開版沒有真實預設值，互動執行時會提示輸入。

多病歷號的掛號整合測試可使用 `registrations` EXE 計畫，支援 TXT／CSV 或貼上清單；逐一呼叫同一個單病人 Service 並保存全部結果。操作與結果檔案見 [批次掛號測試](LIVE_TEST.md#批次掛號整合測試registrations)。此計畫不查實際就診清單。

## 醫師指定日門診掛號清單

`sdk.opd.get_doctor_patients(card_no, visit_date)` 回傳該醫師指定日期的 `list[OutpatientPatient]`。每筆的 `sequence_no` 是門診頁面顯示的掛號序號，型別為字串，保留前導零；來源沒有可辨識序號時為空字串。同一病人、同日、同科別與診間若有不同序號，會保留為不同掛號列。`section_code`、`room`、`doctor_card` 仍依當次清單回應保留。

```python
from datetime import date

for patient in sdk.opd.get_doctor_patients(card_no, date(2026, 9, 21)):
    print(patient.mrn, patient.sequence_no, patient.section_code, patient.room)
```

掛號序號來自門診清單，與 `VisitCase.case_no` 的就診識別不同。清單有掛號不代表當日實際就診；要確認就診仍需查 `sdk.records.get_visit_cases(patient.mrn)`。
