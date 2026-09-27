# 門診掃描病歷

PRQ 有兩條已錄得的讀取路徑：單次門診 SOAP 頁內的掃描 PDF 參照，以及歷年資訊中的「掃描病歷」頁。歷年頁同時列出眼科及其他類別；SDK 先保存完整清單，再依畫面顯示的「病歷類別」篩選。`RECORD`、`OPG` 是 PDF 腳本的 subtype，不能用來判斷科別。SDK 回傳參照與原始 PDF bytes，不做 OCR，也不宣稱已解析 PDF 病歷內容。

## 歷年清單、類別與逐筆 PDF

```python
history = sdk.records.get_upload_history(mrn)  # 預設 days="*"

# 列出所有可解析的歷年掃描項目；不預先排除其他科別。
for record in history.scanned_records:
    print(record.section_label, record.category_label, record.record_date)

# 病歷類別依頁面順序去重；以畫面上的完整名稱精確篩選。
for category in history.scanned_categories:
    records = history.select_scanned_records(category, section_label="病歷類別")
    for record in records:
        pdf = sdk.orders.download_pdf(record.pdf_ref)
        # pdf.content 是原始 PDF bytes，保存與檢視由應用程式決定。

eye_records = sdk.records.get_ophthalmology_scan_history(mrn)
```

每筆 `ScannedRecord` 保留 `section_label`（例如「病歷類別」或「病歷類別(E化表單)」）、`category_label`、畫面顯示的 `record_date`、來源 `record_type` 與受病人約束的 `pdf_ref`。來源未提供 subtype 時，`record_type` 為 `None`，類別與 PDF 仍保留。日期取自顯示文字，不從檔案路徑猜測。`UploadHistory.scanned_records` 包含頁面上可解析的所有掃描 PDF；`select_scanned_records()` 不給篩選值就回全部，傳入 `category_label` 或 `section_label` 則精確匹配。若新類別出現，仍會留在完整清單，由呼叫端選取。`pdf_refs` 是頁面所有可解析 PDF 參照的相容欄位。

目前 `get_ophthalmology_scan_history` 只選「病歷類別」表格中以「門診-記錄-眼科紀錄」開頭的分類；它是一個便利篩選，不代表醫學內容判讀。對其他眼科類別或文件，應先檢視完整類別目錄，再用原始類別名稱篩選。若表格內已有 PDF 腳本但缺病歷類別，解析器會明確報錯；無法對應表格的 PDF 則保留為類別未知。

## 單次就診與跨來源對照

```python
from vghks_sdk.workflows import collect_ophthalmology_scans

# eye_case 是呼叫端已從就診清單核對的門診 VisitCase。
for record in sdk.records.get_case_scanned_records(eye_case):
    pdf = sdk.orders.download_pdf(record.pdf_ref)

result = collect_ophthalmology_scans(sdk, mrn)
for record in result.scans:
    pdf = sdk.orders.download_pdf(record.pdf_ref)
```

`get_case_scanned_records` 從該次就診的 SOAP 頁讀取嵌入的掃描 PDF 參照；同一參照也在 `SoapRecord.scanned_pdf_refs`，SOAP 來源沒有病歷類別，故其 `record_type` 為 `None`。歷年清單只需一次 `prq.upload_history` 查詢，不依賴逐次 SOAP。便利方法沒有新增重複的原子查詢 ID。

`collect_ophthalmology_scans` 額外查全數眼科門診，將 SOAP 參照與歷年清單交叉核對並去重。`result.history_records` 保留完整歷年清單，`scans` 包含眼科類別及未歸入其他已知類別的眼科 SOAP 參照，`other_history` 保留其他已知類別，`unclassified_history` 保留無表格類別又無眼科 SOAP 對應的項目。`case_links` 保留就診來源。`complete` 表示歷年頁與所有眼科就診均查詢成功；若有 `case_issues` 或設定 `max_cases` 而未涵蓋全部就診，則為 `False`。此流程不自動下載 PDF。

下載仍使用 `sdk.orders.download_pdf(record.pdf_ref)`。參照只能指向允許的 PRQ 來源與路徑，且 PDF 路徑病歷號需與查詢病歷號一致。空 tuple 表示該頁或期間沒有符合的參照；未知頁面結構、不安全路徑和無法對齊的類別會明確報錯。

## 證據與測試範圍

2026-09-27 的兩份有內容 HAR 只讀重解析，單次 SOAP 有 1 筆參照，歷年頁有 9 筆不同參照。重新核對歷年頁的可見表格後，8 筆 `RECORD` 均屬「門診-記錄-眼科紀錄-空白紀錄單」；另 1 筆 `OPG` 屬「同意書-手術/麻醉-術前標示(OPH)」。這兩個 subtype 不能取代病歷類別。兩份 PDF 專項 HAR 各無 network entry，不能單靠 HAR 驗證 PDF bytes。

同日的 0.20.5 院內 EXE ZIP 記錄 `OK`：48 筆就診中有 8 筆眼科門診，抽樣的 6 筆 SOAP 各有 1 個掃描參照且皆對應歷年眼科類別中的 `RECORD`；4 份抽樣 PDF 通過檔頭與結尾檢查，其中 1 份是術前標示 `OPG`。另 2 筆眼科就診未查 SOAP，但其歷年 `RECORD` 可由病歷類別辨識。原 ZIP 與 PDF 僅留本機。0.20.7 的分類與組合流程通過合成及原始 ZIP 的只讀重解析；新版 EXE 的 localhost 驗證不能當作內網實測。

院內測試只需一個 `dist/vghks-live-test.exe`；`scans` 計畫啟動後輸入授權病歷號及 Portal 帳密，可選填單次門診日期。它保存完整歷年清單，預設抽最多六次眼科門診，並按病歷類別優先抽眼科 PDF 與單次 SOAP PDF，最多下載四份。EXE 是抽樣驗證工具；應用程式可直接遍歷 `history.scanned_records`，自行逐筆選擇要下載的 PDF。
