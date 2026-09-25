# 結構化 SOAP

`sdk.records.get_soap(case)` 取得一筆門診 SOAP，回傳 `SoapRecord`。0.20.0 起以同一份回應辨識分段、診斷及列印摘要，不需額外查詢醫囑頁面。`sdk.queries.run("prq.soap", case=case)` 亦回傳相同模型。

```python
from vghks_sdk import to_jsonable

soap = sdk.records.get_soap(case)  # case 來自 get_visit_cases／find_visit_cases
s = soap.subjective
o = soap.objective
ap = soap.assessment_plan
diagnoses = [(item.code, item.name) for item in soap.diagnoses]
orders = [(item.name, item.quantity) for item in soap.orders]
medications = soap.medications
periods = soap.chronic_prescription_periods  # 僅在頁面明示慢性處方服藥期限時有值
data = to_jsonable(soap)  # 包含新欄位，可由應用程式存為本機 JSON
```

回傳仍包含病歷內容及病人參照；`to_jsonable` 不會去除個資。保存位置應使用本機 `private/` 或 `output/`。

## 欄位與缺值

| 欄位 | 用途 |
| --- | --- |
| `case` | 該次就診參照，包含病人、日期、類別及科別 |
| `subjective` | S 文字 |
| `objective` | O 文字 |
| `assessment_plan` | 明確標示 A+P／A/P 的合併內容，包含 rowspan 涵蓋的多個區塊 |
| `assessment`／`plan` | 僅在來源分別標示 A／P 時有值；不以語意拆分 A+P，也不自行合成 A+P |
| `diagnoses` | `tuple[SoapDiagnosis, ...]`，每筆包含 `code`、`name`、`coding_system`、`raw_text` |
| `orders` | `tuple[SoapOrder, ...]`，每筆包含 `name`、`quantity`、`raw_text` |
| `medications` | `tuple[SoapMedication, ...]`，每筆包含 `name`、`dose`、`unit`、`route`、`frequency`、`days`、`total_quantity`、`raw_text` |
| `chronic_prescription_periods` | `tuple[SoapChronicPrescriptionPeriod, ...]`；頁面明示「慢性病連續處方箋處方」及「服藥期限」時，取其 `start_date`、`end_date`，並保留 `source_label`、`source_block_index`、`raw_text` |
| `present_sections` | 已辨識的段落／摘要標頭：S、O、AP、A、P、DIAGNOSES、ORDERS、MEDICATIONS |
| `unclassified_blocks` | 未歸入上述欄位的原始文字，例如生命徵象；保留供應用程式或後續解析器使用 |
| `parsing_issues` | 部分格式無法辨識時的穩定問題碼，不含病歷正文 |
| `blocks`／`full_text` | 原有非空文字區塊與合併全文；既有搜尋及 workflow 仍可使用 |

分段文字的 `None` 表示沒有辨識到該段落；`""` 表示有明確標籤但內容空白。清單型欄位為空時，搭配 `present_sections` 與 `parsing_issues` 判斷：有標頭且無問題為空清單；沒有標頭代表未識別該摘要，不能推論病人沒有診斷、用藥或醫囑。

解析器保留重複分段與重複診斷的來源順序，不依位置假設 `blocks[0]` 就是 S。A+P 跨多個文字區塊時以空行串接；原始區塊仍存在 `blocks`。只解析正文，忽略 SOAP 外部的導覽及 script。

若藥囑欄位標題前有連續處方說明，解析器會保留說明於 `unclassified_blocks`，並從明確的「藥名／劑量／單位／途徑／頻次／天數／總發藥量」欄位標題開始解析藥囑。明示的服藥期限另存於 `chronic_prescription_periods`，日期在 Python 為 `date`、JSON 為 `YYYY-MM-DD`；`source_block_index` 是對應 `blocks` 的零起始索引。此區間是頁面標示的服藥期限，不推論每次領藥日或單一藥物的實際用藥日。一般處方沒有該行時回空清單。原始整段仍保留於 `blocks`。無完整標題時不從自由文字推論用藥；辨識到不完整的藥囑標題會回傳解析問題碼。

## 診斷及醫囑的界線

診斷只從明確的 ICD 摘要取得，不從 S／O／A+P 自由文字推論。來源只寫 `ICD碼` 時，`coding_system` 就是 `ICD`；來源明示 ICD-9／ICD-10／ICD-10-CM 時才保留相應版本。清單順序不代表主次診斷，現有 HAR 沒有明確的主次欄位。

`soap.orders` 與 `soap.medications` 是 SOAP 列印頁面呈現的摘要。數量、劑量、頻次及天數保留字串，不換算單位，不推論藥物或手術代碼。檢查醫囑的雙欄版面依每列由左到右保留；沒有標記的排序不代表臨床優先序。

需要執行狀態、開立日期、報告或 PDF／JPG 參照時，繼續使用原有獨立功能：

```python
case_orders = sdk.orders.get_case_orders(case)          # list[ClinicalOrder]
case_medications = sdk.medications.get_case_medications(case)  # list[MedicationOrder]
```

`get_soap` 不會隱含呼叫這兩項功能。摘要名稱可能經院方截斷，不能只按名稱把摘要與完整醫囑當成同一筆。

## 未知格式與驗證

`SOAP_DIAGNOSIS_ROW_UNRECOGNIZED`、`SOAP_ORDER_ROW_UNRECOGNIZED`、`SOAP_MEDICATION_ROW_UNRECOGNIZED` 表示摘要中有列未完整解析；已辨識的資料仍回傳，原始內容保留於 `blocks`。慢性處方說明存在，但期限格式或日期不合法時回 `SOAP_CHRONIC_PRESCRIPTION_PERIOD_UNRECOGNIZED`，不猜測日期；藥囑表仍獨立解析。摘要標頭改變時分別回 `SOAP_ORDER_HEADER_UNRECOGNIZED` 或 `SOAP_MEDICATION_HEADER_UNRECOGNIZED`。只有舊式未標示文字時回 `SOAP_SECTIONS_UNRECOGNIZED`，仍可使用全文。

沒有資料容器會拋出 `PRQ_SOAP_CONTAINER_MISSING`；有容器但正文格式完全不符會拋出 `PRQ_SOAP_STRUCTURE_UNRECOGNIZED`，避免把未知頁面當作成功空結果。明確空容器及查無資料回傳空紀錄。

已有 8 份歷史 HAR SOAP 回應離線驗證，包含跨列 A+P、診斷、單／雙欄醫囑、存在／缺少藥囑摘要。合成案例另覆蓋缺段、空段、欄位重排、重複、未知列、中文字寬及來源範圍；Adapter mock 驗證請求次序與病人／就診參數維持一致。離線報告僅輸出欄位名稱、筆數及問題碼，不輸出診斷、藥名或病人內容。

本次是原有 HTTP 操作的解析擴充，沒有新增 endpoint 或 QuerySpec。0.20.0 SOAP 專項 EXE 先以包含前置說明藥囑表的 HTTPS localhost 回應通過四病人合成測試。2026-09-23 第一份內網 ZIP 揭露舊解析器會漏掉表格前有連續處方說明的藥囑；對原始 SOAP 離線重解析後可補回。當日第二份修正後 EXE 的內網回傳中，四筆 SOAP 均取得 S／O／A+P、診斷與藥囑，一筆取得慢性處方的日期區間；四份原始 HTTP 回應重新解析後均與 EXE 儲存的結構一致，沒有解析問題。當時兩份病人歷次就診清單因異號連結阻擋；0.20.3 另取得一筆舊病歷號門診 SOAP，來源請求使用該次就診原號碼，S／O／A+P 與四筆診斷均可解析，沒有解析問題。其他病人的舊號關係尚未逐一確認。目前僅支援門診 O；詳見 [VALIDATION](VALIDATION.md)。
