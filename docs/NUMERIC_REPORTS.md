# 數值類報告與眼科表格

`sdk.records.get_numeric_report(case)` 查單次就診；`sdk.records.get_numeric_history(mrn, filter)` 查指定期間。兩者都回傳 `NumericTable`，不必由應用程式重做 HTML 或 `document.write` 的解析。醫囑附件 PDF／JPG 是不同入口，這裡不做 OCR。

每張表提供：

| 欄位 | 用途 |
| --- | --- |
| `title` | 表格標題或檢查名稱。 |
| `rows` | 依畫面順序保留的資料列；日期、數值、單位及文字均為原始字串，不自行推論臨床意義。 |
| `header_rows` | 依原始表頭的列順序保留，如 `(("日期", "Va"), ("OD", "OS"))`。 |
| `column_paths` | 能安全對齊時，每個資料欄的完整表頭路徑，如 `(("日期",), ("Va", "OD"), ("Va", "OS"))`。第 N 個路徑對應每筆 `rows` 的第 N 個值。 |
| `headers` | 舊版相容的攤平表頭；可能比資料欄數多，**不可**直接與 `rows` 做 `zip`。 |
| `parsing_issues` | 來源表頭或資料列不一致的問題碼；原始資料列仍保留。已能明確對齊的眼科合併欄差異屬可恢復警示，其他無法對齊的問題仍需人工檢查。 |

```python
report = sdk.records.get_numeric_report(case)
for table in report.tables:
    if not table.column_paths:
        # 保留 table.header_rows、table.rows 和 parsing_issues 供人工檢查。
        continue
    for row in table.rows:
        values = list(zip(table.column_paths, row, strict=True))
        # 例如 (("Va", "OD"), "0.8")；使用 list 可保留重複欄名。
```

眼科表格可有兩層表頭：`日期` 跨兩列，`IOP-pneumo`、`Va`、`驗光-散瞳前` 等檢查名稱跨 OD／OS 兩欄。Parser 依 HTML 儲存格**位置**建立 `column_paths`，不靠數值大小、單位或是否可轉為數字判斷欄位；來源眼別順序若是 OS／OD，路徑也照該順序。手動輸入的 `error`、括號、補充文字與空白儲存格，都以原始字串留在對應欄位，不會使右邊的值前移。完全空白且沒有日期的資料列不產生報告值。

部分原始 HTML 的 `colspan` 甚至大於實際資料欄數；Parser 依資料列與下層表頭的形狀收斂欄位，同時回報 `NUMERIC_HEADER_SPAN_MISMATCH`。0.20.4 測試器只有在兩層表頭明確為「日期」加 OD／OS、每列恰有三個儲存格，且 `column_paths` 完整對齊時，才將此問題計為可恢復警示而非解析失敗；問題碼仍留在模型供檢閱。檢驗表另有第一列列出所有檢查項目、第二列「單位」略去尾端空白 `<th>` 的格式；只有第一列已覆蓋所有資料欄、第二列以「單位」起頭且兩列均無合併儲存格時，Parser 才將缺少的尾端單位視為空白。`header_rows` 仍保留原始短列。若整張表缺少可對齊表頭，`column_paths` 為空，並回報 `NUMERIC_HEADER_UNALIGNED`；不猜測多出的數值屬於哪個檢查。資料列寬度不一致時會回報 `NUMERIC_ROW_WIDTH_MISMATCH`。

私有 HAR 的只讀重解析確認單次眼科表格保有 IOP、Va、驗光／散瞳前的資料列，且可產生逐欄路徑。長期間回應原有 3 張檢驗表被誤判為欄位不足：原始 JavaScript 的異常值分支使用 `else if`，舊靜態解析器把已執行分支與互斥分支的同一數值都列入，造成欄位多出重複值。修正分支邊界後，該 HAR 的 13 張數值表均可對齊；CRP、血球計數和白血球分類的每個解析值也與來源 `rptVal` 逐一核對一致。0.20.1 院內測試另實際取回 23 張跨期間數值表、131 列；4 張只有表頭對齊問題，資料列未遺失。0.20.2 用同份 ZIP 原始 HTML 離線重解析後，23 張均產生逐欄路徑，所有原始標題、表頭與資料列維持一致。0.20.3 院內回傳的跨期間報告再次取得 23 張、131 列，沒有解析問題；單次眼科報告取得 IOP、Va、驗光／散瞳前三張表共六列，逐欄路徑均可與日期／OD／OS 資料對齊。其中 IOP 與驗光表來源 HTML 將跨兩個眼別欄的 `colspan` 寫成 3 與 7，舊 EXE 因來源問題碼將該步驟標成 ERROR。0.20.4 的新院內回傳為 OK：同一病人的三張表、六列原值、手填文字與逐欄路徑和舊 ZIP 完全一致，兩個來源問題碼保留為可恢復警示；跨期間 23 張表、131 列也沒有解析問題。表格值不轉換成 float，也不由文字推斷眼別、屈光度種類或醫療判讀。
