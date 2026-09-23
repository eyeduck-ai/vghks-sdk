# 內網測試 EXE

雙擊 `dist/vghks-live-test.exe` 使用建置時選定的計畫。只需搬一個 EXE，不讀旁邊過時的設定檔。建置工具預設 comprehensive，可用 `--default-profile login` 或 `visits` 選擇專項版本；先用 `--plan` 檢視範圍。

目前 SDK 原始碼為 0.19.4；最近有內網回傳的 EXE 為 0.19.3、login 計畫。0.19.4 新增的單位標籤對照尚未打包成下一版 EXE，不能把原始碼測試視為該 EXE 已通過。驗證範圍集中於 [VALIDATION](VALIDATION.md)。

## 本次：登入及人事測試

只輸入正常的 Portal 帳號與密碼，之後自動執行，完成再按 Enter 關閉。**不需要病歷號、身分證、薪資密碼或設定檔**。只帶回 EXE 同目錄、檔名含時間的 ZIP。

1. 先跑 20 個離線模擬情境，使用 SDK 真正的登入／Requests 流程及記憶體回應，不建立網路連線：正常、空白帳密、未知帳號、錯誤密碼表單／文字錯誤頁、401／403、空白／未知頁、回到登入頁、302／307／308、逾時、一次／反覆過期及重新登入遭拒；包含 302 轉向 HTTP 入口與入口自動跳轉頁。
2. 使用輸入的正常帳密登入，逐一檢查九個登入目標、登入重用，以及 PRQ 文件類型查詢。子系統失敗不阻止獨立檢查。
3. 查詢人事選項與登入帳號的精確員工資料，再測員工編號、姓名、職稱、單位與含下層單位的條件。職稱／單位必須能與當次表單選項唯一對應，且與本人員工編號合併查詢；缺對應保留 NO_SAMPLE，不猜代碼或下載整份人事名冊。
4. 清除本機 Cookie，再做 PRQ 查詢，記錄是否重新登入及恢復。若伺服器仍接受原有 SSO token，記 NO_SAMPLE，表示未觸發過期；此測試不等待或證明伺服器自然逾時。
5. 最後使用同一帳號與自動產生的錯誤密碼，最多兩次，分別驗證 `auth.login()` 與查詢時的按需登入。每次獨立 Session、最多一個實際密碼 POST，轉址及意外重試同樣受限。第一筆正常登入失敗則全部略過；第一筆負向測試若逾時、回應不明或意外成功，停止第二筆負向測試。

一般 SDK 不會主動做錯誤密碼測試，僅 `login` 計畫有此行為。一般帳號每輪最多兩次是本次使用者授權的上限；重跑 EXE 會開始新一輪。開發時可用 `--login-negative-attempts 0` 關閉，或設 `1`，不允許超過 `2`。

`parsed/login/` 保存逐項結果、模擬請求次序、人事條件與結果、Cookie 清除前後的登入世代、每次負向測試的密碼 POST 計數。`RESULTS.txt`、`run_summary.json` 將 SIMULATED 與 LIVE 分開。預期的 `PORTAL_LOGIN_REJECTED` 是負向測試通過，不代表登入成功；未知頁面、額外重送或 HTTP 錯誤仍列 ERROR。

離線分析僅在完整步驟確認一次密碼 POST、預期拒絕且 capture 範圍相符時，將該拒絕列為 EXPECTED_NEGATIVE，原始回應不改寫。受控 Cookie 清除後恢復成功的原始 401／403 另列 recovered，後續解析錯誤不會被掩蓋。

登入、PRQ 恢復、人事主要查詢及兩次預期拒絕已有 0.19.3 內網證據。補測單位篩選時不必再做錯誤密碼，可用 `--login-negative-attempts 0`；自然 TTL 仍未量測。

```sh
vghks-live-test --plan --profile login
vghks-live-test --profile login
vghks-live-test --profile login --login-negative-attempts 0
```

## 就診搜尋增量測試

0.17.2／0.18.0 已取得內網證據：身分證與病歷號清單一致，身分證清單的門診 SOAP／醫囑串接成功。原 EXE 將醫師卡號列為 NO_SAMPLE；0.18.1 修正分支解析後，已從同份回應離線取回住院／急診卡號並驗證篩選。門診來源卡號仍空白。此計畫可用於後續不同樣本或新版導覽驗證；不需為已可離線解析的資料重跑完整功能。

1. 輸入 Portal 帳號／密碼。
2. 測試病歷號直接 Enter 沿用內嵌值，也可輸入另一個已授權病歷號。
3. 病人身分證直接 Enter，程式會從該病歷號的基本資料取得；也可手動輸入同一病人的身分證。這不是登入者或 MIS 的身分證。
4. 執行完成後，只帶回 EXE 同目錄下的時間命名 ZIP。

只執行必要登入／連線、病歷號與身分證兩條就診查詢、病人核對與清單比對、到院日／O-A-E 類別／科別／醫師的本機篩選。選定門診後以最多三筆樣本驗證 SOAP／醫囑串接，各項取得非空資料就停止抽樣；眼科優先。身分證查詢失敗時仍以病歷號結果完成可獨立執行的檢查，輸出會註明 fallback，不能視為身分證已驗證成功。

預設不執行審查、手術、薪資或附件下載。基本資料僅在需要自動取得病人身分證時查一次；手動輸入身分證時只需要 Portal／PRQ。沒有住院、急診或可串接門診的樣本記 NO_SAMPLE，保留驗證缺口，不判成程式錯誤。沒有已錄製的住院／急診 SOAP 端點，測試器不嘗試這些路徑。

`parsed/visits/` 包含兩份完整清單、各自輸入、身分核對、逐筆差異、欄位涵蓋率、各篩選結果及抽樣 SOAP／醫囑。`RESULTS.txt` 列出每一項增量檢查；HTTP 原始本文與例外也保留。

一般原始碼或通用 EXE 可明確指定 `--profile visits`。可選 `--patient-national-id`／`VGHKS_PATIENT_NATIONAL_ID`；不把此值放入一般設定檔。`--plan --profile visits` 可離線查看範圍。

## 人事單項測試

`vghks-live-test --profile atomic --only personnel.options --only personnel.search` 只測人事選項及登入帳號的人事清單，原始回應與查詢條件照常保留。新的 SDK 不再要求就診清單有可用醫師卡號，卡號應先透過人事取得姓名。0.19.2 登入專項 EXE 已包含這些查詢及多條件檢查。

## 原有完整測試

comprehensive 計畫包含 57 個唯讀查詢（含人事選項／清單）、登入醫師的審查清單與最多 8 案詳情、手術碼 80416 的近兩年／兩年以上案例與最多 8 份紀錄、病人歷史手術及附件、績點與專勤工作獎金。單次就診抽樣最多 6 筆。啟用 MIS 時另詢問本人身分證字號與薪資系統密碼；所有異動功能排除。

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

登入專項直接使用 SDK 共用連線與自動恢復政策，實際成功模式記於 `selected_profiles.json`。其他計畫還有以下獨立預檢。

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
| run_summary.json／step_results.json | 每步狀態、capture 範圍、耗時 |
| parsed/inputs/ | 每個查詢真正使用的條件 |
| parsed/atomic/ | 原子回傳及 PDF／JPG |
| parsed/visits/ | 身分證增量測試的清單、比對、篩選與門診串接 |
| parsed/login/ | 登入、Session、SIMULATED 情境、人事查詢、負向密碼 POST 計數 |
| parsed/earnings/ | MIS 月份選項、原始 HTML、文字與表格 |
| parsed/workflows/ | 組合流程各階段資料 |
| capture_manifest.jsonl／responses/ | HTTP 原始證據 |
| parsed/network/selected_profiles.json | 實際 TLS／憑證驗證模式 |

ZIP 可能含帳密、Session、病人與薪資內容，與所有衍生資料都只留本機。原件放 data/returns，再執行：

```sh
python run_sdk.py analyze-bundle --input data/returns/return.zip --output output/latest-analysis
```

Parser 修改後可反覆離線重解析；原始 recorded 狀態不會被覆寫。新請求流程、缺少 body 或沒有樣本的路徑，才需要另做內網實測。

本機分析器會以 `no_sample_steps`／`no_sample_operations` 列出缺口；只有缺樣本時分析狀態為 COMPLETED_WITH_GAPS、exit code 為 0，不會產生錯誤根因或要求對同一份缺樣本資料反覆重測。
