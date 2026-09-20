# 內網測試 EXE

雙擊 `dist/vghks-live-test.exe` 使用建置時選定的計畫。只需搬一個 EXE，不讀旁邊過時的設定檔。**0.18.0 自用版預設為 visits，就診搜尋增量測試**；通用建置仍預設 comprehensive，可用 `--default-profile visits` 改成增量版。

## 本次：就診搜尋增量測試

0.17.2 已取得內網證據：身分證與病歷號清單一致，身分證清單的門診 SOAP／醫囑串接成功；唯一缺口為醫師卡號未回傳。此計畫可用於後續不同樣本：病歷號查詢作為比較基準，篩選在本機執行，不新增網路查詢；既有其他模組不納入。

1. 輸入 Portal 帳號／密碼。
2. 測試病歷號直接 Enter 沿用內嵌值，也可輸入另一個已授權病歷號。
3. 病人身分證直接 Enter，程式會從該病歷號的基本資料取得；也可手動輸入同一病人的身分證。這不是登入者或 MIS 的身分證。
4. 執行完成後，只帶回 EXE 同目錄下的時間命名 ZIP。

只執行必要登入／連線、病歷號與身分證兩條就診查詢、病人核對與清單比對、到院日／O-A-E 類別／科別／醫師的本機篩選。選定門診後以最多三筆樣本驗證 SOAP／醫囑串接，各項取得非空資料就停止抽樣；眼科優先。身分證查詢失敗時仍以病歷號結果完成可獨立執行的檢查，輸出會註明 fallback，不能視為身分證已驗證成功。

預設不執行審查、手術、薪資或附件下載。基本資料僅在需要自動取得病人身分證時查一次；手動輸入身分證時只需要 Portal／PRQ。沒有住院、急診、醫師卡號或可串接門診的樣本記 NO_SAMPLE，保留驗證缺口，不判成程式錯誤。沒有已錄製的住院／急診 SOAP 端點，測試器不嘗試這些路徑。

`parsed/visits/` 包含兩份完整清單、各自輸入、身分核對、逐筆差異、欄位涵蓋率、各篩選結果及抽樣 SOAP／醫囑。`RESULTS.txt` 列出每一項增量檢查；HTTP 原始本文與例外也保留。

一般原始碼或通用 EXE 可明確指定 `--profile visits`。可選 `--patient-national-id`／`VGHKS_PATIENT_NATIONAL_ID`；不把此值放入一般設定檔。`--plan --profile visits` 可離線查看範圍。

## 原有完整測試

comprehensive 計畫包含 55 個唯讀查詢、登入醫師的審查清單與最多 8 案詳情、手術碼 80416 的近兩年／兩年以上案例與最多 8 份紀錄、病人歷史手術及附件、績點與專勤工作獎金。單次就診抽樣最多 6 筆。啟用 MIS 時另詢問本人身分證字號與薪資系統密碼；所有異動功能排除。

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

預檢優先採用 SDK 的已知設定：PRQ、SectOrd、WebMAAS 直接用 TLS12_COMPAT，其他服務用 DEFAULT；成功即停止展開該來源的診斷，相同主機／埠共用結果。失敗才測其他協定、必要的 proxy／direct 路由及匿名憑證比對。此順序省去舊版在三個服務重複產生的六次失敗探測。

預設仍先驗證憑證。測試配置可採用 allow_unverified_tls 備援，預檢選擇寫入 selected_profiles.json。一般 SDK 也有獨立的自動恢復，遇到明確憑證錯誤可依此政策僅對該服務略過驗證，維持 HTTPS；不需靠 EXE 才能查詢，詳見 [CONNECTIONS](CONNECTIONS.md)。

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
| parsed/visits/ | 身分證增量測試的清單、比對、篩選與門診串接 |
| parsed/earnings/ | MIS 月份選项、原始 HTML、文字與表格 |
| parsed/workflows/ | 組合流程各階段資料 |
| capture_manifest.jsonl／responses/ | HTTP 原始證據 |
| parsed/network/selected_profiles.json | 實際 TLS／憑證驗證模式 |

ZIP 可能含帳密、Session、病人與薪資內容，與所有衍生資料都只留本機。原件放 data/returns，再執行：

```sh
python run_sdk.py analyze-bundle --input data/returns/return.zip --output output/latest-analysis
```

Parser 修改後可反覆離線重解析；原始 recorded 狀態不會被覆寫。新請求流程、缺少 body 或沒有樣本的路徑，才需要另做內網實測。

本機分析器會以 `no_sample_steps`／`no_sample_operations` 列出缺口；只有缺樣本時分析狀態為 COMPLETED_WITH_GAPS、exit code 為 0，不會產生錯誤根因或要求對同一份缺樣本資料反覆重測。
