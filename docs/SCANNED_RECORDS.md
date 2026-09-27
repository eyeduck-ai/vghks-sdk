# 門診掃描病歷

PRQ 有兩個已錄得的讀取路徑。單次門診 SOAP 頁嵌有掃描 PDF 參照，但該頁沒有提供掃描類別；歷年資訊的「掃描病歷」清單包含來源類別 `RECORD` 與眼科專用 `OPG`。SDK 回傳 PDF 參照，不執行 OCR，也不將 PDF 內容當成已解析的病歷文字。

## 查詢與下載

```python
from vghks_sdk.workflows import collect_ophthalmology_scans

# eye_case 是呼叫端已從就診清單核對的門診 VisitCase。
for record in sdk.records.get_case_scanned_records(eye_case):
    pdf = sdk.orders.download_pdf(record.pdf_ref)
    # pdf.content 是原始 PDF bytes，由應用程式決定是否保存。

# 只查歷年頁明確標為 OPG 的眼科專用掃描檔。
eye_scans = sdk.records.get_ophthalmology_scan_history(mrn)

# 查全數眼科就診，合併歷年 OPG 與已由眼科 SOAP 證實的 RECORD。
result = collect_ophthalmology_scans(sdk, mrn)
for record in result.scans:
    pdf = sdk.orders.download_pdf(record.pdf_ref)

# 如需檢閱全部原始歷年項目，包含目前無法判定科別的 RECORD。
all_scans = sdk.records.get_upload_history(mrn).scanned_records
```

`get_case_scanned_records` 沿用 `prq.soap` 的病人／就診 context、操作鎖與 SOAP 回應；同一參照也在 `SoapRecord.scanned_pdf_refs`，其 `record_type` 為 `None`。`get_ophthalmology_scan_history` 沿用 `prq.upload_history`，**僅**挑選標為 `OPG` 的項目，不能代表所有眼科掃描。完整歷年結果在 `UploadHistory.scanned_records`，`pdf_refs` 則保留可解析的所有 PDF。兩個便利方法沒有重複登錄新的查詢 ID。

`collect_ophthalmology_scans` 是跨原子操作的組合流程，預設查全部眼科門診，不執行 PDF 下載。`result.scans` 將已由眼科 SOAP 確認的 `RECORD`、明確的 `OPG` 以及只在 SOAP 找到的參照去重；`case_links` 保留就診對應，`unclassified_history` 保留不能判定科別的歷年 `RECORD`。`complete` 只表示歷年頁與全部眼科就診查詢成功；若有 `case_issues` 或限制 `max_cases`，它會是 `False`。流程不猜測未查就診或未分類檔案的科別。

下載使用既有的 `sdk.orders.download_pdf(record.pdf_ref)`；原子測試器也能由 SOAP 或上傳歷史的真實回應發現 PDF 輸入。參照僅接受已錄得的 PRQ 檔案來源與路徑，並要求 PDF 路徑的病歷號與查詢病歷號一致。空 tuple 表示該頁或該期間沒有符合的參照；未知頁面結構與不安全路徑會報錯。

2026-09-27 的兩份有內容 HAR 只讀重解析得到單次 1 筆、歷年 9 筆不同 PDF 參照（8 筆 `RECORD`、1 筆 `OPG`）；單次那筆與歷年一筆 `RECORD` 路徑相同。兩份 PDF 專項 HAR 各無 network entry，因此單靠 HAR 不能驗證 PDF bytes。

同日的 0.20.5 院內 EXE ZIP 記錄 `OK`：四項掃描相關操作均成功，48 筆就診中有 8 筆眼科門診，抽樣的 6 筆 SOAP 各有 1 個掃描參照，全部對應歷年清單的 `RECORD`；歷年另有 1 筆 `OPG`，抽樣下載的 4 份 PDF 都通過檔頭與結尾標記檢查，尚未解析 PDF 內文。尚有 2 筆眼科就診未抽 SOAP，另 2 筆歷年 `RECORD` 未經此輪證實屬眼科。原 ZIP 保持私有且狀態不改寫；新組合流程目前僅有合成測試，不能把上述 0.20.5 結果當成它已在院內執行。

院內測試只需雙擊一個 `dist/vghks-live-test.exe`；`scans` 計畫啟動後輸入授權病歷號及 Portal 帳密，可選填單次門診日期。它保存完整歷年清單，預設抽最多六次眼科門診並下載最多四份 PDF，優先各取一份 `OPG` 與單次就診來源。EXE 是抽樣驗證工具；要逐筆整理全部可確認的眼科掃描，使用上面的組合流程。
