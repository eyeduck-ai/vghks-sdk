# 內網測試 EXE

雙擊 `dist/vghks-live-test.exe` 使用內建 comprehensive 計畫。只需搬一個 EXE，不讀旁邊過時的設定檔。先提供 Portal 帳密；通用版再需要授權測試病歷號，自用建置可已內嵌此值。啟用 MIS 時另詢問本人身分證字號與薪資系統密碼，密碼不回顯。

目前預設：55 個唯讀查詢、登入醫師的審查清單與最多 8 案詳情、手術碼 80416 的近兩年／兩年以上案例與最多 8 份紀錄、病人歷史手術及附件、績點與專勤工作獎金。單次就診抽樣最多 6 筆。異動功能排除。

## 執行與設定

```sh
vghks-live-test --plan
vghks-live-test --test-mrn YOUR_AUTHORIZED_MRN
vghks-live-test --config configs/review-system.example.json
```

內嵌值只作預設；`--test-mrn`、設定檔 `test_mrn`、`VGHKS_TEST_MRN` 可明確覆寫。個人設定放 private 或 *.local.json，不能提交 Git。必要登入資訊使用環境變數 VGHKS_USERNAME／VGHKS_PASSWORD；MIS 使用 VGHKS_EARNINGS_NATIONAL_ID／VGHKS_EARNINGS_PASSWORD，不放設定檔。

未指定醫師時使用登入帳號。comprehensive 原子門診查詢走最近七天（受日期範圍限制），避免只抽到週末。完整「七天門診 → 當日就診 → SOAP」組合流程由 weekly_opd_soap 額外啟用，內建當輪預設關閉，以控制測試量。眼科醫囑專用流程可用 `--profile ophthalmology`。

每項查詢、每份報告的錯誤分別保存，其他可獨立操作的功能會繼續；登入無法建立時，相依功能標 BLOCKED。Requests 預設循序及 0.8–1.8 秒隨機間隔。

## TLS 與狀態

預檢依序測預設 HTTPS、TLS 1.2、相容 TLS 1.2，必要時區分 proxy／direct。已測得舊子系統可用 TLS12_COMPAT，仍有 HTTPS 與憑證驗證。測試配置支援使用者授權的 allow_unverified_tls 備援，真實選擇寫入 selected_profiles.json；一般 SDK 不預設略過憑證。

| 步驟狀態 | 解讀 |
| --- | --- |
| OK | 操作有結果；報告正文與欄位完整性仍須另核對 |
| EMPTY | 確認有效的空回應 |
| NO_SAMPLE | 前置查詢完成，但沒有可供下一步的參照／樣本，未執行該功能 |
| MISSING | 缺必要設定或額外帳密 |
| BLOCKED | 登入或依賴失敗 |
| ERROR | 已嘗試但失敗 |

整體 COMPLETED_WITH_GAPS 表示只有未取得樣本的缺口，exit code 為 0，coverage 保留未驗證項目；COMPLETED_WITH_ERRORS 表示仍有錯誤／阻擋／缺必要輸入。前置 TLS 探測若後續同服務已登入成功，不重複算成致命錯誤。

## 帶回資料

結果 ZIP 直接寫在 EXE 同目錄，檔名含輸出時間與結果；無加密、無 .sha256，帶回 ZIP 即可。成功封裝後不用另外搬 live-test-results；若封裝失敗，依畫面提示保留完整 run 目錄，可用 `--pack-incomplete` 補封裝。

| 檔案／路徑 | 用途 |
| --- | --- |
| RESULTS.txt／coverage.json | 功能結果與未覆蓋情境 |
| run_summary.json／step_results.json | 每步状态、capture 範圍、耗時 |
| parsed/inputs/ | 每個查詢真正使用的條件 |
| parsed/atomic/ | 原子回傳及 PDF／JPG |
| parsed/earnings/ | MIS 月份選项、原始 HTML、文字與表格 |
| parsed/workflows/ | 組合流程各階段資料 |
| capture_manifest.jsonl／responses/ | HTTP 原始證據 |
| parsed/network/selected_profiles.json | 實際 TLS／憑證驗證模式 |

ZIP 可能含帳密、Session、病人與薪資內容，與所有衍生資料都只留本機。原件放 data/returns，再執行：

```sh
python run_sdk.py analyze-bundle --input data/returns/return.zip --output output/latest-analysis
```

Parser 修改後可反覆離線重解析；原始 recorded 狀態不會被覆寫。新請求流程、缺少 body 或沒有樣本的路徑，才需要另做內網實測。
