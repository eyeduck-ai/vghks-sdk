# 手術案例與手術紀錄

0.14.0 新增「手術紀錄查詢」功能，使用獨立的 SSO 入口，與 `get_schedule` 的手術排程分開驗證。兩者共用 SDK 的 Requests、節流及錯誤處理。

## 原子功能

| 方法 | 結果 |
| --- | --- |
| `sdk.surgery.get_case_departments()` | 此查詢頁提供的科別代碼 |
| `sdk.surgery.get_cases(filter)` | 一個伺服器時間條件下的全部案例 |
| `sdk.surgery.get_record_ref(case.reference)` | `SurgeryNoteRef`；明確查無紀錄時為 `None` |
| `sdk.surgery.download_record(note)` | 通過 PDF 格式檢查的 `BinaryAsset` |

```python
from vghks_sdk import SurgeryCaseFilter

selector = SurgeryCaseFilter(
    surgeon_card=doctor_card,
    procedure_code="80416",
    department="ALL",
    period="24M",
)
cases = sdk.surgery.get_cases(selector)
for case in cases:
    note = sdk.surgery.get_record_ref(case.reference)
    if note is not None:
        pdf = sdk.surgery.download_record(note)
        # pdf.content 是原始 PDF bytes，由應用決定儲存方式。
```

案例包含日期、病人、就診識別、手術名稱、四個手術碼、科別、主刀、指導、主治及四位助手。`fields` 保留後端原始欄位及未知欄位，移除 session HID。`record_status` 保留原始值，不據此推定有無 PDF。

每個案例以 `(病歷號, 申請序號, 手術序號)` 識別。手術序號 `0` 有效；同一病人的不同手術會分別保留。

## 篩選條件

`SurgeryCaseFilter` 支援 `surgeon_card`（主刀）、`supervising_card`（指導）、`assistant_cards`（最多四個位置）、`department`、`procedure_code`、`period`。至少指定一個醫師卡號。只指定第二助手時使用 `assistant_cards=("", "卡號")`，空位置會保留。多條件比對方式由內網後端決定。

表單提供 `3`、`7`、`14`、`1M`、`4M`、`6M`、`12M`、`24M`、`2YB`，分別代表三天、一週、二週、一個月、四個月、半年、一年、兩年及兩年前。本次 HAR 實際提交並回傳的條件是 `24M` 與 `2YB`，手術碼為 `80416`。

API 沒有錄到任意起訖日期欄位。指定年月日的查詢由組合流程先取得涵蓋區間，再依 `surgery_date` 篩選。畫面 DataTables 的 10／25／50 筆是瀏覽器分頁，SDK 保留整份 JSON。

## 全歷史收集

```python
from pathlib import Path
from vghks_sdk.workflows import collect_surgery_records

result = collect_surgery_records(
    sdk,
    selector,
    periods=("24M", "2YB"),
    output_dir=Path("output/surgery-new-run"),
    # start_date=date(2020, 1, 1), end_date=date(2026, 9, 20),
)
print(result.status, len(result.cases), result.manifest_path)
```

流程合併兩批全部回傳、按完整識別去重，再逐筆查詢／下載，不抽樣。日期篩選含首尾兩日，於本機套用。每輪使用新的空目錄：

- `stage1_cases/`：各時間條件的輸入與完整回傳，或錯誤。
- `cases.json`：去重及日期篩選後的案例。
- `stage2_notes/`：每筆紀錄的連結、明確查無資料或錯誤。
- `stage3_pdf/`：通過驗證的 PDF，以及各次下載結果。
- `manifest.json`：持續更新的進度、計數及完整性狀態。

單筆失敗仍繼續後續案例。某個時間區間失敗仍處理另一區間，但結果標為 `INCOMPLETE`；相同案例出現不同內容也會保留來源並標示衝突。`NOT_FOUND` 只表示後端明確回覆沒有紀錄，HTTP 失敗或未知格式記為錯誤。

## 已錄製的證據

HAR 提供了兩個期間的案例查詢、`getOpnotePDF` 回覆，以及頁面 JavaScript 的 viewer GET 路徑。2026-09-20 內網測試進一步成功查詢案例並下載 OPPL 手術 PDF。

SDK 使用頁面按鈕的 HTTPS viewer；收到 HTML 或錯誤頁時會明確失敗並保存診斷。PDF 下載已實測，但尚未實作手術 PDF 全文／數值擷取。不同查詢醫師及條件的結果筆數不可直接互相比較。

## EXE 測試

comprehensive 計畫內建下列手術碼測試參數；login 計畫只驗證登入，不查手術案例。開發時若只測此模組，可明確執行 `vghks-live-test.exe --config configs/surgery-records.example.json`。

這份設定以登入帳號作為主刀，查手術碼 `80416` 的 `24M` 與 `2YB` 全部案例，再跨年份抽樣最多 8 個案例測試紀錄連結與 PDF。此上限只用於 EXE 首次驗證；SDK 的收集流程逐筆處理全部案例。醫師角色、科別及手術碼可在 `surgery_query` 調整。

上述獨立設定只診斷 Portal 與手術紀錄 SSO；雙擊範圍依建置 profile 而定。結果是不加密 ZIP、無 `.sha256`，存於 EXE 同目錄；`parsed/atomic/oppl_records.*` 與 HTTP capture 可供下一輪調整。

## 病人手術歷史入口

`records.get_surgery_history` 保留「手術記錄」欄位每個按鈕的 PDF 參照。解析依據為錄製頁面的 `window.open`、PRQ viewer 網址、檔案路徑及雙重 URL 編碼；不需要啟動瀏覽器。

```python
from vghks_sdk import SurgeryHistoryFilter

records = sdk.records.get_surgery_history(mrn, SurgeryHistoryFilter())
for record in records:
    # 呼叫端可保存此欄位，辨識未能解析的按鈕。
    print(record.surgery_record_issues)
    for ref in record.surgery_record_refs:
        pdf = sdk.records.download_surgery_record(ref)
        # pdf.content 是原始 PDF bytes。
```

`download_surgery_record` 共用 `orders.download_pdf` 的 PRQ Adapter、Session、節流、目前 HID、雙重編碼及 PDF 格式檢查。它不需要 OPPL SSO，也不需要主刀卡號；與前述 OPPL 入口各自使用其錄製網址。

同一列可有多份 PDF；同日同名的不同手術列都會保留。單一未知／不合法按鈕記入 `surgery_record_issues`，同列其他有效參照仍可使用。只從「手術記錄」欄位擷取，麻醉紀錄、麻醉同意書、術前／術後訪視的下載尚未接上。`surgery_record_available` 仍表示畫面有紀錄標示，不代表 PDF 已下載成功。

comprehensive 計畫包含病人歷史手術查詢。單獨重測可明確執行 `vghks-live-test.exe --config configs/patient-surgery-records.example.json`；這個獨立範例將 PDF 抽樣上限設為 12，comprehensive 內建上限為 8。使用設定的測試病歷號，查全部歷史及近一年；完整清單與按鈕參照存入 `parsed/atomic/prq.surgery_history/`。PDF 使用共用 `prq.pdf_attachment` 測試，因此也會執行其既有醫囑／報告／上傳附件來源查詢。優先放入手術參照，再依當次上限從所有來源抽樣 PDF，存入 `parsed/atomic/prq.pdf_attachment/`。

EXE 將未知按鈕標為 `SURGERY_PDF_LINKS_INCOMPLETE`，仍繼續下載已解析的參照；單份 PDF 錯誤也不阻擋後續檔案。原子 API 自身沒有 12 份上限。兩條手術紀錄入口均已有內網 PDF 成功樣本；若新增樣本顯示不同流程，再補錄新視窗。
