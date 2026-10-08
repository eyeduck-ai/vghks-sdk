# 驗證範圍

本文件集中記錄目前功能的證據與限制，不含患者、帳號、金額或原始私有回應。版本變更歷程見 [CHANGELOG](../CHANGELOG.md)，操作方式見 [LIVE_TEST](LIVE_TEST.md)。

## 目前狀態速覽

| 項目 | 已有證據 | 尚未由目前版本院內驗證 |
| --- | --- | --- |
| SDK／單檔 EXE | 0.22.1 院內 `failures` 89 步；0.22.4 `session` 直接 API 自動 SSO 恢復；0.22.8 `dbr` 23 步、兩筆醫囑與四份 PDF | 0.22.9 存檔優化、強制改密碼與自然 TTL |
| DBR PDF 下載 | 0.22.8 院內兩種歷年清單成功，四份 PDF 逐檔與 raw 及 metadata 一致；兩份與原 debug 失敗樣本 SHA-256 相同 | 本輪沒有 JPG、HTML 正文或其他病人樣本；0.22.9 優化尚未院內重跑 |
| WebMAAS 閒置後失敗 | 0.22.3 院內 Cookie 遺失已取得 timeout 原頁；0.22.4 直接 API 自行 SSO 恢復且三份基本資料一致 | 舊平台自然閒置未保存失敗 HTML；自然 TTL 仍未院內證實 |
| 失敗分類與密碼狀態 | 0.22.1 三日倒數、未登入／錯誤密碼拒絕及 Cookie 恢復；0.22.3 兩日倒數與 WebMAAS timeout | 自然 TTL、強制變更、帳號鎖定及未自然出現的網路失敗沒有院內樣本 |
| 高榮與聯合醫院掛號 | 0.20.11 的兩組帳號均完成直接掛號、先查 `CHECK_PAT` 後掛號；同帳號兩次結果一致 | WebMAAS 缺表單備援未自然觸發；舊應用失敗頁缺原始回應，根因仍未證實 |
| PRQ 調閱審查 | 0.20.11 無手動原因設定時，自動送出一次 `1A`，接著取得就診清單及 SOAP | SOAP／明細／歷年掃描頁各自觸發審查、身分證查詢遇審查 |
| 掃描病歷 | 0.20.5 院內抽樣驗證兩條來源及 4 份 PDF；0.20.7 分類邏輯有原始頁只讀重解析與 localhost 測試 | 0.20.7 完整分類及組合流程仍無院內執行結果；0.20.11 本輪沒有掃描 PDF 樣本 |
| 數值與手術排程 | 0.20.11 兩份數值報告各有 3 個已對齊的來源警示、解析錯誤 0；排程清單 48 筆 | 排程不代表已完成手術，PDF 及醫療內容擷取不在此輪範圍 |

## 目前版本與證據層級

- SDK 原始碼：0.22.9，保留 0.22.8 的二進位隔離修正，改善單次 JSON 存檔、附件原子寫入、唯一檔案統計與離線存檔大小／SHA-256／格式核對。HTTP／登入 URL／HTML 挑戰、附件格式及大小檢查保留；強制改密碼與子系統恢復政策不放寬。
- 本機現行 EXE：0.22.9、`dbr` 計畫，build_id=`20261008T115643Z`，Python 3.10.6 x64／PyInstaller 6.14.2／truststore 0.10.4，沒有 private defaults。單一授權病人的全部／各科歷年醫囑，最多八筆 DBR，保存 PDF／JPG；沒有刻意錯誤密碼或 Cookie 挑戰。其他計畫仍可明確選用。
- 0.22.9 完整 suite 663 項：651 項通過、12 項私有條件跳過；相關 unittest 77 項通過，包含一般 SDK import 不載入 live／offline／錄製契約及離線分析不載入測試執行流程。新增共用附件僅下載一次、runner 已存檔不重寫、自訂 runner 的存檔備援、單份附件寫入失敗仍續跑，以及缺檔／缺 outcome／錯誤大小／hash／格式／metadata／路徑回歸。source 與 frozen EXE 各十種 localhost DBR 情境及實際 CLI 離線分析通過；共享眼科流程的既有 frozen 工具亦確認正文／多份 PDF／JPG／附件失敗續跑。現行 EXE 分析同一院內 ZIP 的完整報告（排除 analyzer build）及重測設定與原始碼完全一致，四份 PDF 核對通過，原 ZIP 不變。這些本機存檔證據不替代另列的 0.22.8 院內執行結果。
- 0.22.8 完整 suite 653 項：641 項通過、12 項私有條件測試跳過。`dbr` source 與 frozen EXE 各十種 localhost HTTPS 情境通過，包含真正零參數啟動、舊 sidecar／profile 不擴大計畫、跨年份參照、PDF／JPG 原始位元組、MIME 缺失／錯標、損壞檔案、登入頁／HTTP 拒絕與有限恢復、缺樣本及實際 CLI 離線分析。共享眼科流程亦以 frozen EXE 的既有 localhost 工具確認文字／PDF／JPG 分支；二進位合成回歸保留強制改密碼、登入轉址、截斷及大小限制。這些 localhost 與禁止 socket 重解析屬本機證據；0.22.8 的院內 DBR 下載結果另列於下。
- 0.22.7 完整 suite 643 項：631 項通過、12 項私有條件測試跳過；新增可信導覽／拒絕 callback、單次 GET 不改原重試政策、專項範圍、不可信摘要與原 SDK 通知不被額外頁面覆寫的測試。`password` source 與 frozen EXE 各十五種 HTTPS localhost 情境通過，核對文字／表單／轉址強制變更、倒數、未知／HTTP／密碼拒絕、匿名及既有 Cookie 目錄可讀、查詢過期仍不重送密碼，並以實際 CLI 分析 ZIP。0.22.7 先前建置另以 `session` 的十二種及 `failures` 的 23 種 localhost 情境（每輪 68 個無 socket 模擬）亦通過；Ruff、API 文件、公開內容與實際 wheel／sdist 檢查，以及獨立 wheel 禁止 socket 的 import／建構／CLI 計畫驗證通過。這些是合成證據；本次真實強制頁及舊密碼可否讀取資料仍待院內回傳，不測自然 TTL。
- 0.22.6 完整 suite 637 項：625 項通過、12 項私有條件測試跳過。新增獨立程序驗證離線分析不載入測試執行模組，舊公開匯出保持相容，禁止 import／建構／計畫發 socket。相同 0.22.4 院內 ZIP 的完整分析報告（排除 analyzer build）及重測設定與 0.22.5 完全一致。`session` 原始碼與 frozen EXE 各十二種 HTTPS localhost 情境、`failures` 各 23 種情境及每輪 68 個無 socket 模擬通過；Ruff、API、公開內容、實際 wheel／sdist 及獨立安裝檢查通過。這些架構與封裝回歸不新增院內或自然 TTL 證據。
- 0.22.5 完整 suite 636 項：624 項通過、12 項私有條件測試跳過。新增合成證據核對不同 recorder 計數器、舊版唯一請求匹配、缺少完成／表單／結果、錯誤角色／來源及範圍外成功不能掩蓋 timeout。`session` 原始碼與 frozen EXE 各十二種 HTTPS localhost 情境通過，且實際 CLI 的離線分析驗證直接 API 的 VERIFIED、結構化資料比較及原錯誤保留。0.22.4 晚間原 ZIP 禁止 socket 重解析確認只將 000034 標為恢復，000022 的原 ERROR 保留；不宣稱自然 TTL。
- 0.22.4 完整 suite 628 項：616 項通過、12 項私有條件測試跳過。`session` 原始碼與 frozen EXE 各十二種 HTTPS localhost 情境通過，包括固定 timeout 轉址、GET／唯讀 POST 的 SSO 自動恢復、原角色及新 token、持續 timeout 停止，明確情境只有一次初始密碼 POST。`failures` 原始碼與同一 EXE（明確指定 profile）各 23 種情境及每輪 68 個模擬通過；Ruff、API、公開內容、實際 wheel／sdist 與獨立安裝的禁止 socket 匯入／CLI 檢查通過。合成 TLS 伺服器先讀完 POST 再回 501，避免 Windows 未讀 body 關閉造成連線重設；不修改 SDK 的 TLS 政策。這些本機測試與下列院內回傳分開。
- 0.22.3 完整 suite 617 項：605 項通過、12 項私有條件測試跳過。`session` 原始碼及 frozen EXE 各九種 HTTPS localhost 情境通過，核對表單／token、隔離 Cookie、原失敗 HTML、有限恢復、持續失敗及登入拒絕停止。舊 `failures` 原始碼與同一 frozen EXE（明確指定 profile）各 23 種情境及每輪 68 個模擬亦通過；以下另列其後的院內 Session 回傳，不能混用版本。
- 0.22.2 用原 0.22.1 院內 ZIP 做禁止 socket 的只讀比較：除了 analyzer_build 版本，完整分析報告及 retest 設定完全一致。新增合成檢查確認範圍外的登入錯誤、先前／不同操作／範圍外的成功與後續解析錯誤不會被 Cookie 恢復掩蓋；原始 ZIP 保留不動。
- 0.22.2 完整 suite 602 項：590 項通過、12 項私有條件測試跳過。原始碼與 frozen EXE（build_id=`20261004T091257Z`）各 23 種 HTTPS localhost 情境通過，每輪 68 個模擬通過；沒有將預期拒絕或刻意失敗情境宣稱為真實成功讀取。新版尚無院內執行結果。
- 本機 0.22.0 的既有驗證：完整 suite 595 項，583 項通過、12 項私有條件測試跳過；build_id=`20261004T021459Z` 的 22 種 localhost 情境及每輪 67 個模擬通過。此建置即本次 2026-10-04 院內 ZIP 的來源。
- 0.22.1 的原頁私有回歸全程禁止 socket：SDK 以記憶體 adapter 完成登入並保留 EXPIRING／4，五張數值表的十二列、表頭、逐欄路徑及三個來源問題碼完全不變；兩份報告的原欄位、正文狀態及一／兩個 PDF 參照維持一致，僅移除可靜態辨識按鈕造成的錯誤 extraction note。新版重解析零份 PARTIAL，一份數值回應保留警示；原 ZIP 位元組與執行結果未改寫。
- 0.22.1 完整 suite 執行 600 項：588 項通過、12 項私有條件測試跳過；23 種原始碼與 frozen EXE（build_id=`20261004T031550Z`）HTTPS localhost 情境全部通過，每輪 68 個模擬通過。新增情境核對四日倒數在登入與 readiness 中一致、已對齊數值完整且保留 warnings、正常 PDF 按鈕維持 ATTACHMENT_ONLY 並無解析缺漏；實際 EXE 的結果仍不能取代內網重跑。
- 2026-10-04 下午同一 0.22.1 建置的院內回傳已確認三日倒數與數值警示修正；全輪沒有執行或解析錯誤。PDF 按鈕未出現在本輪樣本，原始 ZIP 與新版重解析結果分開保留，見下節。

## 2026-10-08：0.22.8 DBR 院內回傳與 0.22.9 存檔優化

本輪 `dbr` ZIP 為 OK、exit code 0：23 個步驟中 21 個 OK、2 個 EMPTY，34 個原始 HTTP exchanges，沒有 ERROR／BLOCKED／MISSING／NO_SAMPLE，errors.jsonl 為空。Portal／PRQ readiness 通過；Portal TLSv1.3、PRQ TLSv1.2 相容模式，兩者憑證均有驗證。僅一次正常登入，沒有刻意錯誤密碼、Cookie 挑戰或自動重新登入。

兩種歷年醫囑清單分別 219 列、56 列，共 275 個來源列，找到並處理兩筆 DBR；清單列數不是去重後醫囑數。兩份報告各有兩份 PDF，四份共 11,355,511 bytes，存檔、下載 metadata 與原始 HTTP 回應逐檔大小／SHA-256 相同，格式邊界檢查通過。兩份較小 PDF 正是原整合 debug 的失敗檔案，SHA-256 相同；因此原 SDK 二進位誤送 HTML 密碼解析器的缺陷已有新版院內修正證據。

兩份報告頁均為 ATTACHMENT_ONLY、正文長度零；兩個 PACS 檢視器明示 NO_IMAGES，為有效空結果，沒有 JPG 可下載。PDF 是原始附件，沒有 OCR 或數值擷取。本輪只證明該病人與回傳範圍的 DBR 流程，不推論其他年份、病人或所有報告形式。

0.22.9 在禁止 socket 下重新分析原 ZIP，四份附件存檔核對及現行 Service／replay 均通過，原 ZIP SHA-256 未變。新增的存檔與統計優化只有合成及 localhost 證據，尚未以 0.22.9 EXE 院內重跑。

附件契約共用及查詢／存檔責任拆分前後，同一原 ZIP 的完整離線分析（排除 analyzer build）與重測設定完全一致。契約在 workflow 執行時才載入，維持一般 SDK import 的分層界線；此架構驗證未新增院內 HTTP 請求。

## 2026-10-08：整合專案 DBR debug 與修正

SDK 0.22.6 的整合 debug 保存兩次 `orders.download_pdf` 失敗及其完整原始回應。兩次 `/PRQWeb/Page/JSP/showPDF.jsp` 均收到 HTTP 200，PDF 格式驗證成功；Runtime 在回傳附件前一律解碼並呼叫 `parse_password_status`，在二進位內容觸發 `ParserRejectedMarkup`。0.22.7 原始碼仍可重現，離線 replay 也有同樣問題。

0.22.8 以 PDF／JPEG 檔頭隔離二進位與 HTML 檢查，原始回應在禁止 socket 下透過現行 Service 及 replay 均成功，下載位元組與 SHA-256 一致，原 ZIP 不改寫。這是對原始院內回應的離線修正驗證，不等同新版 EXE 已完成院內查詢。缺少或錯標 MIME 仍需以檔頭及完整性驗證；實際回傳登入／強制改密碼 HTML、HTTP 拒絕及登入目的 URL 仍保留原分類與有限恢復。

`dbr` 專項從兩份歷年清單發現參照並去重，預設最多八筆；沒有 DBR／PDF 樣本、未執行與上游錯誤分開，單筆錯誤仍保存其他報告及附件。原始 PDF／JPG 只保留為檔案，不包含 OCR。此修正後的 0.22.8 院內回傳已另列於上一節。

## 2026-10-05 晚間：0.22.4 院內直接 API 恢復

原 ZIP build_id=`20261005T104253Z`，107 個 ZIP 項目，其中 103 個檔案完整可讀、39 筆 capture；十步中九步 OK、一個 ERROR，exit code 1／COMPLETED_WITH_ERRORS，所有計畫步驟已執行。ERROR 是第一次刻意移除可隔離 WebMAAS JSESSIONID 後的 readiness，SDK 已分類 `WEBMAAS_SESSION_TIMEOUT`／AUTHENTICATION／HTTP 200；Portal、SectOrd 仍正常，獨立 SSO 複查及後續病人查詢成功。`errors.jsonl` 仍為空，原 issue 在 AuthCheckReport 及步驟中保留。

第二次隔離 Cookie 遺失後，EXE 直接呼叫 `sdk.patients.get_basic_info`，沒有 readiness 或 EXE 補重試。相同 SDK 操作的 capture 000033–000039 顯示：固定 GET 302，000034 為明確 timeout；接著 SectOrd 新金鑰橋接、原 `maas_QRY15` 角色 SSO、000037 新表單／token、CHECK_PAT，最後 000039 基本資料成功。診斷只有一次 `application_session_recovery_started`，同一操作以 OK 完成，整輪只在初始登入送出一次 Portal 密碼 POST。

基準、獨立 SSO 恢復後與直接 API 恢復後三份基本資料的全部結構化欄位一致；兩份 CHECK_PAT 資料亦一致。整份 raw HTML 的動態內容排除於比較，不能宣稱 HTML 完全相同。密碼通知為 EXPIRING／2／SCRIPT_LITERAL，提醒未阻止查詢。自然 TTL、強制改密碼、錯誤密碼與附件不在本輪測試。

這輪已確認 **0.22.4 在 Cookie 遺失後由公開基本資料 API 自行恢復一次 WebMAAS SSO**，同一情境不需再帶 EXE 補測。0.22.5 只讀分析修正把 000034 誤列為未解故障的問題，保留其原 timeout 與已恢復狀態，000022 的原 readiness ERROR 不變；`session_test.direct_api_recovery.status` 為 VERIFIED，另列第三份結構化資料比較。不能把此結果當成自然閒置 TTL、所有查詢或未觀察到的失敗形態均已驗證。

## 2026-10-05：0.22.3 院內 Session 回傳

原 ZIP build_id=`20261004T193223Z`，91 個檔案完整可讀，32 筆 capture；八步中七步 OK、一個 ERROR，exit code 1／COMPLETED_WITH_ERRORS。流程已跑完，錯誤為刻意移除一個可隔離 WebMAAS JSESSIONID 後的原 readiness，恢復成功仍保留。`errors.jsonl` 為空，因為這次錯誤存於回傳的 AuthCheckReport 與步驟 issue；不能只用錯誤日誌是否為空判斷測試成功。

捕獲的固定基本資料 GET 先回 302，再導向 `/webmaas/comm/pageTimeOut.do` 的 HTTP 200；原頁無查詢表單，明確顯示 `Page time out` 與重新登入提示。Portal、SectOrd 維持 OK，沒有 HTTP／網路或 TLS 失敗。原 0.22.3 記為 `WEBMAAS_QUERY_FORM_MISSING`／PARSE；0.22.4 的禁止 socket 只讀重解析將 capture 000022 辨識為 `WEBMAAS_SESSION_TIMEOUT`／AUTHENTICATION，原 ZIP 位元組及實測結果不改寫。

下一個獨立 SSO 複查成功，隨後 CHECK_PAT 與完整基本資料成功；恢復前後的全部結構化病人欄位一致，只有 raw HTML 的動態內容不同。整輪只在正常初始登入送出一次密碼 POST；恢復為 SectOrd 金鑰橋接與 WebMAAS SSO，沒有強制 Portal 重登入。原與恢復後登入報告均保存 EXPIRING／2 日，提醒未阻止查詢。沒有錯誤密碼、強制改密碼或附件測試。

這輪證實 **Cookie 遺失、明確 WebMAAS timeout 與獨立 SSO 恢復**，不證實自然閒置 TTL，也不能回溯證明舊 67 分鐘 debug 的失敗原頁相同。0.22.4 增加的是正常唯讀 API 自行有限恢復；其後的晚間院內回傳已另行完成直接 API 複驗，見上節。

## 2026-10-05：整合平台閒置後的 WebMAAS debug

平台 6.6.42／SDK 0.22.2 的 debug 包含 129 筆時序事件、19 個 SDK 操作與 45 組 HTTP；18 個操作 OK，最後 `patient_session_check` 回報 `WEBMAAS_QUERY_FORM_MISSING`。WebMAAS 最後一次成功 CHECK_PAT 到失敗約隔 67 分鐘，期間 PRQ 仍有活動。最後先通過 `auth.check`，接著固定掛號頁 GET 302 轉址，最終 HTTP 200 的 HTML 沒有任何 form，未嘗試重新登入；此時尚未發送下一個病人查詢。

這證明快取 readiness 不足以驗證閒置後的 WebMAAS 頁面。轉址目的部分遮罩，ZIP 沒有失敗 HTML 或重連後成功的原始對照；使用者回報重新連線可以查到資料，但來源 TTL、子系統失效、權限或其他錯誤頁仍無法由這份 ZIP 排除。原始檔留本機，不能將頁面缺表單直接改成 AuthExpiredError 或病人不存在。

0.22.3 與 `session` EXE 補上當次表單驗證、快取失效、有限 SSO 複查及完整原始對照；前述九種 localhost 情境只驗證機制，仍待院內回傳。

## 2026-10-04 下午：0.22.1 失敗分類院內回傳

`failures` ZIP 為 COMPLETED_WITH_GAPS、exit code 0，build_id=`20261004T031550Z`。89 步中 85 個 OK、2 個 EMPTY、2 個 NO_SAMPLE；68 個合成模擬全數通過。76 個原始 capture 經只讀重解析，沒有未恢復的 HTTP／登入／解析錯誤，原錯誤 journal 為空；預期匿名挑戰、一次錯誤密碼拒絕及已恢復的 Cookie 挑戰均依通過步驟及 capture 範圍分開。

密碼通知已直接由 EXE 分類為 EXPIRING／3／SCRIPT_LITERAL，登入、readiness、Cookie 恢復後的觀察與兩份原始登入回應一致。Portal／PRQ／SectOrd／WebMAAS 全數通過；清 Cookie 後回 HTTPS Portal 恢復一次，generation 1→2 並取得六筆目錄。自然 Session TTL 依使用者選擇未等待；本輪沒有強制改密碼頁。

正常讀取取得基本資料、二十筆掛號、二百七十八筆完整就診清單及抽樣兩份 SOAP。兩份數值各有四張表、十三列，逐欄路徑與所有資料列完全對齊；各兩個來源 NUMERIC_HEADER_SPAN_MISMATCH 保留在原表格，DataAssessment 另列 warnings，status=OK、complete=True。沒有 PARTIAL。掛號與就診筆數依樣本及條件而定，不由不同輪次的筆數差異推論功能變化。

兩個 EMPTY 分別為第二次就診的醫囑清單與唯一取回的醫囑報告；報告明示無資料、report_data_status=EMPTY，沒有 PDF／JPG 參照。兩個 NO_SAMPLE 是沒有醫囑明細參照及沒有 JPG／PACS 參照。因此密碼與數值兩項修正已有新版院內證據，PDF 按鈕修正仍僅有先前原頁只讀重解析與 localhost 證據；不將合法空報告當作該按鈕已驗證。未執行、查無病人、自然網路失敗、帳號鎖定與強制變更仍沒有本輪樣本。

## 2026-10-04：0.22.0 失敗分類院內回傳

本輪 `failures` ZIP 為 COMPLETED_WITH_GAPS、exit code 0：90 個步驟中 84 個 OK、4 個 EMPTY、2 個 NO_SAMPLE，沒有 ERROR／BLOCKED／MISSING。67 個模擬全數通過；80 個原始 HTTP exchanges 與實際資料觀察分開保存，離線沒有未恢復的執行錯誤。

匿名 PRQ 目錄請求在沒有登入、零密碼 POST 下收到院方入口轉址，SDK 分類為 AUTH_NOT_AUTHENTICATED。另一次獨立 Session 的錯誤密碼先於正確登入，只送一個密碼 POST；院方回 HTTP 200 文字拒絕頁，包含「登入失敗／帳號或密碼錯誤」，分類 PORTAL_LOGIN_REJECTED。這是該次明確拒絕，不能由此推論未知帳號、鎖定或其他登入限制。隨後 Portal／PRQ／SectOrd／WebMAAS 均通過。最後清 Cookie 的目錄查詢收到入口 302，Runtime 回 HTTPS Portal 重新登入一次，generation 1→2 並取得原目錄。此證據只證實 Cookie 遺失恢復，沒有等待自然 Session TTL。

正常查詢取得基本資料、六筆掛號、二十三筆完整就診清單及抽樣兩次門診 SOAP。第一份數值有五張表、十二列；第二份數值與醫囑合法空值，兩份 JPG 清單也明示無資料。兩份醫囑報告只有附件參照（共三個 PDF），沒有下載或 OCR。醫囑明細沒有候選參照，為 NO_SAMPLE，並非登入或資料讀取失敗。

另一個 NO_SAMPLE 為密碼通知：0.22.0 漏讀成功登入與 Cookie 恢復登入兩份原始回應的頂層「【4】日後到期」alert。0.22.1 已用同份保存頁重解析，兩份均為 EXPIRING／4／SCRIPT_LITERAL；原步驟狀態仍保留 NO_SAMPLE。原三筆 PARTIAL 分別為一份已對齊數值表的來源 colspan 警示、兩份正常 PDF 按鈕的靜態變數解析缺漏，均已由 0.22.1 修正且保留附件／原表格。未執行、查無病人、自然網路失敗、帳號鎖定及強制改密碼均沒有本輪院內樣本；不能拿模擬取代證據。

## 先前本機驗證

- 本機 0.21.0：完整 suite 執行 587 項，575 項通過、12 項私有條件測試跳過；Ruff、API 文件、公開檔案、wheel／sdist 實際封包及獨立目錄 wheel 匯入／CLI 檢查通過。測試確認合法 NotFoundError 與 HTTP 404 不混用、未知 schema 不判空、原資料及部分解析問題保留、重登入 DNS 原因可序列化、明確 SDK 因果不混入 Python 隱含 context，以及整段登入失敗於 GET 時不提示重送密碼。
- 先前 0.21.0 frozen EXE build_id=`20261003T181938Z`：七種 HTTPS localhost 情境、每輪 51 個模擬通過；原範圍沒有實際負向密碼測試。這些只代表當時的合成檢查，不能視為院內或自然 TTL 證據。
- 本機 0.20.11：完整 suite 執行 569 項，557 項通過、12 項私有條件測試跳過；Ruff、API 文件、公開檔案、wheel／sdist 實際封包與獨立目錄 wheel 匯入／CLI 檢查通過。合成測試涵蓋預設 `1A` 自動提交、SOAP／就診明細讀取續跑、歷年掃描與醫囑歷史病人狀態、不符表單及寫入後禁止整段重播。單檔 EXE 的 regression、patient、visits、soap、scans 專項 HTTPS localhost 測試通過；其中 regression 的審查情境未提供原因參數，確認自動送出一次。這些均不能取代院內回傳。
- 本機 0.20.10：完整 suite 執行 563 項，551 項通過、12 項私有條件測試跳過；Ruff、API 文件、公開內容、wheel／sdist 實際封包與獨立目錄 wheel 匯入均通過。單檔 EXE 的 HTTPS localhost 測試涵蓋有效 SSO 表單、SSO 頁缺表單後固定 GET、先 `CHECK_PAT` 再查掛號、雙帳號隔離，以及第二帳號合成登入失敗後阻斷該帳號相依步驟；原有病歷調閱審查與其他回歸情境保留。這些頁面形態是合成案例，高榮實際 SSO 是否缺表單尚無同輪 HAR 證據。
- 本機 0.20.9：完整 suite 執行 559 項，547 項通過、12 項私有條件測試跳過；Ruff、API 文件同步、公開檔案檢查、wheel／sdist 實際封包檢查及獨立目錄 wheel 匯入通過。單檔 EXE 的 HTTPS localhost 合成驗證涵蓋先獨立查 `CHECK_PAT`、再查掛號且第二次 GET 無表單的情境，結果為 `OK`；回歸 EXE 既有四種情境亦通過。院內新版結果仍待確認。
- 本機 0.20.8：完整 suite 執行 558 項，546 項通過、12 項私有條件測試跳過；Ruff、API 文件同步、公開檔案檢查、wheel／sdist 實際封包檢查及獨立目錄 wheel 匯入通過。聯合醫院掛號與調閱審查 HAR 各只讀重解析，不重送請求；`regression` 及病人查詢 EXE 通過 HTTPS localhost 合成驗證。這些均不等於新版已在院內完成查詢。
- 先前 `regression` EXE：0.20.4，查指定病歷號的基本資料、完整就診清單、最多兩份眼科 SOAP／單次數值報告、跨期間數值報告及登入醫師手術排程。有舊號眼科門診時，兩份樣本中抽一筆舊號。HTTPS localhost 已驗證舊號查詢及手填文字／空白格的眼科表格警示情境；2026-09-25 第三份院內回傳為 `OK`，實際範圍見下文。
- 本機 0.20.7：549 項合成／一般測試通過（未啟用私有資料時跳過 12 項）；啟用私有 HAR／回傳只讀回歸後 549 項全數通過。0.20.5 掃描 ZIP 原始頁重新解析為 9 筆、2 類、8 筆眼科紀錄，9 筆均有顯示日期；未重送任何醫院請求。單檔 `scans` EXE 已以 HTTPS localhost 模擬回應驗證完整分類清單、眼科樣本與 PDF 下載。Ruff、API 文件、公開內容、wheel／sdist 封存及獨立安裝後的 import／CLI 檢查通過。新版分類及組合流程的院內操作尚未驗證。
- 先前 0.20.4 的 535 項測試在啟用本機私有 HAR／回傳只讀回歸後全部通過；SOAP 專項曾另啟用私有 HAR 回歸，當時 21 項通過。先前從實際 ZIP 的原始 SOAP 離線重解析確認漏列藥囑補回，並取得一筆明示的慢性處方服藥期限；其他三筆藥囑及各筆 S／O／A+P 不變。
- 0.19.3 登入 EXE 曾通過七個 HTTPS localhost 情境，並有以下內網回傳；不得以原始碼測試宣稱新版 EXE 的所有情境已通過。

合成測試、HAR／ZIP 只讀重解析、localhost EXE、真實內網回傳是不同層級。HTTP 成功、解析成功、取得正文、下載二進位及完整擷取醫療數值也必須分開。

## 2026-09-28 掛號與病歷調閱審查修正

### 0.20.11 院內 EXE 回傳

本輪 ZIP 的 build metadata 為 SDK 0.20.11、`regression`；封包 258 個檔案完整可讀。原始執行 `OK`、exit code 0，16 個步驟全數 `OK`，`errors.jsonl` 為空；只讀重解析為 `OK`，110 個 capture 中沒有未解的 HTTP 或解析錯誤。Portal、PRQ、SectOrd、WebMAAS、OPPL 的登入目標均為 OK；其他登入目標未測。沒有套用略過憑證驗證的 TLS 模式。

PRQ 的病歷號就診查詢確實收到調閱審查頁；`run_config.json` 未提供 `access_review_reason`，SDK 在同一操作中只向固定 `EMRProcess.do` POST 一次 `saveAccessCause`／`1A`。送出的病歷號與 HID 均符合前一步病人查詢，HTTP 回應 200 且離線重解析為 `RECORDED_ACK`；之後就診清單與一份 SOAP 均成功。此證據驗證了**病人就診清單遇到審查後自動續跑**。它未自然觸發 SOAP 頁、就診明細頁或歷年掃描頁各自的審查分支，也未驗證身分證查詢遇審查。

兩組帳號的直接掛號、先查 `CHECK_PAT` 後掛號均 OK；高榮帳號各得 1 筆，聯合醫院帳號各得 17 筆。同帳號兩條流程的完整結構化結果相同，後一流程不再額外 GET 掛號表單，且各只送出一次掛號查詢 POST。兩組回應中的病歷號不同，因此筆數不能用來比較帳號權限。就診清單有 1 筆、只含一個來源病歷號；這輪未驗證舊號關係。一份 SOAP 有 S／O／A+P，沒有解析問題及掃描 PDF 參照。單次與跨期間數值報告各 5 張表、5 列，各有 3 個來源 `NUMERIC_HEADER_SPAN_MISMATCH` 警示；受警示的欄位路徑與資料列等寬，解析錯誤為 0。手術排程查詢得 48 筆，不能視為已完成手術數。

0.20.10 的前一份院內 ZIP 也為 `OK`，兩組帳號的直接與先查病人資料後掛號均成功，但沒有觸發 PRQ 調閱審查。兩份 ZIP 查詢樣本不同，不能用筆數變化判斷功能退化。兩輪均未證實舊應用的 `WEBMAAS_QUERY_FORM_MISSING` 根因，也未自然觸發 WebMAAS 缺表單備援。

### 功能設計與 HAR 證據

- 0.20.11 依使用者需求將 HAR 錄製的 `1A` 設為 PRQ 審查頁預設原因；這是應用程式遇到相同頁面的自動流程，不代表所有病歷查詢皆會出現審查。送出前需當頁仍提供 `1A` 且病歷號、HID、表單位置均一致；提交後需取得病人頁框，結果不明則停止。Runtime 在任何已嘗試的寫入後禁止同一操作因認證錯誤整段重播。新版院內 EXE 已驗證一次病歷號就診查詢遇審查後成功續跑；其他觸發位置仍待樣本。

- 0.20.10 增加 SSO 頁缺表單或 token 的一次固定 GET 備援，並用獨立 SDK Session 比較高榮與聯合醫院帳號各自的直接掛號及先 `CHECK_PAT` 再掛號。EXE 的 `registration_comparison.json` 僅記錄階段狀態、筆數與請求種類；原始 HTTP 仍保存在同輪私有 ZIP。聯合醫院 HAR 證實有效 SSO 表單路徑，原應用 debug 沒有失敗頁正文；0.20.10 與 0.20.11 的院內 ZIP 均未自然觸發缺表單備援。
- 聯合醫院帳號的應用 debug 僅顯示 `WEBMAAS_QUERY_FORM_MISSING`，沒有失敗頁原始回應。新 HAR 錄到瀏覽器 WebMAAS SSO 轉址後第一頁即有 `RSV11WForm` 與 token；其後 AJAX 病人核對與掛號 POST 均成功，掛號結果離線重解析為 12 筆。SDK 原先在 SSO 已取得該頁後又 GET 一次，現改為首次直接使用 SSO 頁並一次性消耗 token。0.20.10 與 0.20.11 院內回傳證實新版可成功查詢，但「多餘 GET 是舊失敗原因」仍只是流程差異推論，沒有舊失敗頁原始證據。
- PRQ HAR 顯示 `QueryPatientRecord.do` 回傳病歷調閱表單，選擇頁面提供的 `1A` 後，`EMRProcess.do` 回可辨識病人 context，接著取得就診清單與 SOAP。私有 HAR 只讀重解析為 23 筆就診與 8 筆 SOAP 回應；它是錄製結果，並非新版 SDK 的院內請求。0.20.8 原設計要求明確原因；0.20.11 已改為在核對病歷號、HID、表單位置及可選原因後預設自動提交一次，送出後仍需確認病人 context。合成測試覆蓋不符、選項缺少與結果不明不重送。
- 0.20.8 單檔 EXE 的 HTTPS localhost 驗證通過四種回歸情境，包含「第二次 WebMAAS GET 無表單」與「PRQ 審查後繼續 SOAP」同時出現的情境；病人查詢 EXE 的 SSO、token、分頁與錯誤續跑亦通過。所有 localhost 回應與帳號均為合成資料，院內結果仍待測。
- 0.20.9 再核對同份聯合醫院 HAR：首次 SSO 掛號表單後僅呼叫 AJAX `CHECK_PAT`，其後掛號 POST 仍使用首次 token。SDK 因此保留獨立 `get_demographics` 後的首次表單；合成與 frozen EXE 的 HTTPS localhost 測試均確認此順序沒有多餘 GET。HAR 不能證明原失敗頁內容或新版院內查詢結果。

## 2026-09-27 0.20.5 掃描 EXE 院內回傳

原始 ZIP 自我檢查通過，錄製狀態與離線分析均為 `OK`，沒有未解的登入、HTTP、Parser 錯誤或 `NO_SAMPLE`。四個掃描相關唯讀操作均有成功證據：完整就診清單 48 筆，其中 8 筆為眼科門診；EXE 預設上限抽查 6 筆眼科 SOAP，每筆各有 1 個掃描 PDF 參照。歷年掃描頁有 9 筆參照：8 筆 `RECORD` 位於「門診-記錄-眼科紀錄-空白紀錄單」類別，1 筆 `OPG` 位於「同意書-手術/麻醉-術前標示(OPH)」類別。六筆 SOAP 參照都與眼科 `RECORD` 項目相同。四份抽樣 PDF 均有有效的檔頭及結尾標記，其中一份來自術前標示 `OPG`，其餘三份由眼科就診連結發現。

上述結果證實兩條讀取路徑及抽樣 PDF 下載在此輪院內可用。重新只讀解析原始頁的可見病歷類別後，可辨識 8 筆眼科紀錄及 1 筆術前標示；因此 `OPG` 不能當作眼科類別。尚有 2 筆眼科門診未由此 EXE 查 SOAP，不能宣稱已比對每筆就診或下載歷年所有 PDF。0.20.7 的類別解析與組合流程已通過合成及原始頁只讀重解析，但原 ZIP 沒有執行新版邏輯。原始 ZIP、PDF 與分析檔僅留本機。

`OutpatientPatient.sequence_no` 已依既有門診 HAR 的 `KSCase` 顯示欄位做本機只讀核對：兩份回應分別解析出 74 與 60 筆門診列，全部取得三位數序號。合成案例另檢查前導零、畫面標記及同診間不同序號的保留。此新增欄位尚未以新版 EXE 在內網重新執行。

## 指定病歷號 SOAP 異常的診斷範圍

另一個應用回傳的 debug JSON（應用 6.6.1，SDK 0.20.0）含七筆失敗記錄、六個不同病人。每筆均在 `get_visit_cases` 的就診索引階段得到 `PRQ_CASE_PATIENT_MISMATCH`，尚未執行 SOAP 下載或結構化解析。每筆回應的病人標頭與要求病歷號一致，但解析到的連結另外含異病歷號；同一病人的兩次嘗試結果相同。該 JSON 未保存原始 HTTP 頁面，這六名病人的舊號關係並未逐一確認。新版只在可辨識的病人查詢狀態下接受正式啟用的就診列，不能把那些舊失敗當成當時完成的 SOAP。

0.20.1 增量 EXE 已在 localhost 驗證：異號連結當時會阻擋 SOAP／單次數值報告，獨立的數值歷史與手術排程仍完成。2026-09-25 的真實內網 ZIP 證實原始就診頁面有 38 個靜態啟用的 `KSCase`：22 個連結使用指定病歷號，16 個較早期連結使用另一病歷號，且不是註解、字串或未啟用分支。`QueryCaseList.do` HTTP 查詢本身成功；使用者之後確認兩個號碼屬同一人、較早紀錄使用舊號。0.20.3 在先確認 `QueryPatientRecord.do` 建立可辨識 context 後，只接受清單啟用列的歷史號碼；該 ZIP 的離線重解析取得完整 38 筆，原始號碼與就診連結均保留。原 ZIP 未發出 SOAP，因此不能宣稱舊號 SOAP 已在內網成功。

## 2026-09-25 增量 EXE 院內回傳

0.20.1 `regression` 計畫回報 `COMPLETED_WITH_ERRORS`，7 個步驟中登入、病人基本資料和手術排程為 OK；就診清單為 `PRQ_CASE_PATIENT_MISMATCH`，相依的 SOAP 及單次數值報告均標 BLOCKED。跨期間數值報告獨立完成 HTTP 查詢並取回 23 張表、131 列，但其中 4 張因單位表頭比資料欄短而標 `NUMERIC_TABLE_PARSING_ISSUES`。這 4 張原始表格的第一列表頭及資料列欄數一致，只有第二列「單位」省略尾端空白儲存格；0.20.2 的限定格式修正重新解析同一原始 HTML 後，23 張表均有與資料列等寬的 `column_paths`，原本標題、表頭和資料列完全一致。原 ZIP 的結果狀態不因此改寫，0.20.2 尚待院內複驗。

同輪手術排程查詢取得 71 筆，常用欄位及 125 種來源鍵有保留；12 筆麻醉來源空白、50 筆 `TF` 類型為 `UNCONFIRMED` 且開始時間空白，另 21 筆為 `CLOCK_TIME`。網路與 TLS 沒有阻擋這三個獨立查詢；這只證明該範圍的登入、查詢和解析，未驗證每筆畫面或來源代碼含義。

## 2026-09-25 0.20.3 增量 EXE 院內回傳

第二份 `regression` ZIP 為 `COMPLETED_WITH_ERRORS`，但登入、病人基本資料、就診清單、兩筆 SOAP、跨期間數值報告及手術排程均完成。就診清單共 38 筆，其中 22 筆使用查詢號碼、16 筆使用同一病人已確認的舊號。兩筆門診 SOAP 分別使用各自來源病歷號發出請求，均取得正文並解析 S／O／A+P 與診斷，無 SOAP 解析問題；舊號那筆含四筆診斷。這驗證了此人的舊號門診 SOAP 路徑，不代表其他人的多號關係或所有舊號端點皆已驗證。

唯一 ERROR 是目前號碼就診的單次數值報告 `NUMERIC_TABLE_PARSING_ISSUES`。三張眼科表共六列，IOP-pneumo、Va、驗光／散瞳前的 `column_paths` 均為日期、OD、OS 且與資料列等寬；其中兩張來源 HTML 將跨 OD／OS 的 `colspan` 分別標成 3、7，實際只有兩欄，Parser 保留 `NUMERIC_HEADER_SPAN_MISMATCH` 因而讓該步驟標 ERROR。這是來源合併欄宣告與實際欄數不一致，現有證據未顯示數值列遺失或眼別錯位。舊號就診的單次數值頁面未包含資料表，該步驟為 `EMPTY`，不能由此推論所有舊號報告皆無資料。跨期間數值報告另取回 23 張表、131 列，沒有解析問題；手術排程取回 71 筆。本測試啟用的登入／SSO 目標均通過，沒有 TLS 或憑證備援記錄。原始 ZIP 保留，分析檔僅存本機。

0.20.4 對同份原始 HTML 的只讀重解析確認：三張表的六列原始值、日期／OD／OS 順序和兩個來源 `NUMERIC_HEADER_SPAN_MISMATCH` 問題碼均與 0.20.3 儲存的結果相同；其中手填的 `error` 是來源儲存格內容，不由 SDK 產生。兩張眼科表符合明確的日期加 OD／OS 三欄形狀，故新版測試判定為 `numeric_warning_count=2`、`numeric_error_count=0`，不再因來源合併欄標示錯誤而宣稱無法解析。原始 ZIP 仍是當時的 `COMPLETED_WITH_ERRORS`，不能當作 0.20.4 已在院內重跑。

## 2026-09-25 0.20.4 增量 EXE 院內回傳

第三份 `regression` ZIP 為 `OK`、exit code 0，離線分析亦為 `OK`，`errors.jsonl` 為空。啟用的 Portal、PRQ、SectOrd、WebMAAS、OPPL 登入／SSO 目標均為 OK；未啟用的子系統不列為本輪驗證。38 筆就診仍為兩個來源病歷號（22 筆查詢號、16 筆舊號），兩筆門診 SOAP 均 OK。單次眼科報告三張表、六列，來源仍有兩個 `NUMERIC_HEADER_SPAN_MISMATCH`，新版摘要為 `numeric_warning_count=2`、`numeric_error_count=0`，步驟為 OK；舊號那筆單次數值頁面仍是空清單，獨立標 `EMPTY`。跨期間數值報告 23 張表、131 列，沒有解析問題；手術排程 71 筆。沒有 TLS 憑證略過或重試恢復紀錄。

同一測試病人的兩份 ZIP 逐一比對後，儲存的完整就診清單、兩筆 SOAP、兩筆單次數值結果及跨期間數值結果均完全相同；新版本沒有靠刪除手填 `error`、空白或醫療數值來取得 OK。這證明 0.20.4 對該病人及上述操作的實際院內流程，未涵蓋全部 57 個登錄查詢、其他病人的舊號關係或所有眼科表格樣式。原始 ZIP、分析檔及手動報告值僅存本機，不進公開 repo。

## 9/21 多病人 SOAP 第一份內網回傳

2026-09-23 回傳狀態 `COMPLETED_WITH_ERRORS`。9/21 門診清單 106 筆，其中回傳醫師欄可核對為專屬 34 筆、共用 72 筆；測試選 8 個不同病歷號。6 份就診清單完成，2 份因原始清單含異病歷號連結而回 `PRQ_CASE_PATIENT_MISMATCH`，SDK 因此沒有跟隨那些連結。另有 2 名掛號病人無匹配的當日門診就診，保留 `NO_SAMPLE`。4 名病人各取得 1 份 SOAP。

這 4 份 SOAP 都辨識到 S、O、A+P 與診斷，且原始 ZIP 沒有 `parsing_issues`；3 份有檢查醫囑摘要。原 ZIP 僅列 3 份有藥囑摘要，但第 4 份其實在原始回應中有藥囑表，前面多一行連續處方說明，舊解析器只檢查文字區塊第一行而漏列。修正後直接重解析原始 HTTP 回應，該份藥囑由 0 筆補為 1 筆，並取得該行明示的一筆慢性處方服藥期限；四份均辨識到藥囑摘要，S／O／A+P 及其餘三份藥囑未變。**原 ZIP 的 `OK` SOAP 步驟不能單獨證明每個摘要都已完整辨識；新版實測見下節。**

## 9/21 多病人 SOAP 第二份內網回傳

2026-09-23 22:38 的修正後 EXE 回傳 `COMPLETED_WITH_ERRORS`：32 個步驟中 28 個 OK、2 個 ERROR、2 個 NO_SAMPLE。Portal 與 PRQ 登入、TLS 連線均成功，兩個來源均維持 HTTPS 與憑證驗證。門診清單仍為 106 筆，專屬 34 筆、共用 72 筆，抽樣 8 名不同病人。4 份 SOAP 均取得 S、O、A+P、診斷與藥囑，3 份有頁面列印的醫囑摘要；其中 1 份正確取得慢性處方的服藥期限。四份原始 HTTP 回應在本機重解析後與 EXE 保存的結構化 JSON 一致，均無 `parsing_issues`。分開的 A／P 欄位空白是來源使用合併 A+P，並非漏抓；沒有醫囑摘要的那筆也不能推論沒有其他醫囑。

兩個 ERROR 都是 `PRQ_CASE_PATIENT_MISMATCH`：原始就診清單的啟用資料列分別含 2 筆及 1 筆異病歷號連結；當時 SDK 拒絕整份清單，沒有對那些連結繼續查詢。兩個 NO_SAMPLE 是掛號清單與實際同日同科別門診就診不相符：一名當日有其他科別門診，一名沒有當日就診。當時離線僅以同病歷號連結診斷時，其中一個受阻樣本可能有 1 筆符合條件的就診；這不是已完成的 SOAP 查詢。0.20.3 新規則不改寫該輪結果。

## 最近的登入與人事實測

0.19.3 回傳為 COMPLETED_WITH_GAPS：40 個步驟通過（20 個內建模擬、20 個實際步驟），1 個 NO_SAMPLE，沒有未恢復的執行錯誤。

| 路徑 | 內網證據 | 限制 |
| --- | --- | --- |
| 正常登入／SSO | Portal、PRQ、SectOrd、WebMAAS、OPPL、AUDIT、手術紀錄、審查、人事共九個目標 OK | SSO 成功不等於各服務全部資料查詢通過 |
| Session 重用／恢復 | 重複登入沒有增加請求；清 Cookie 後偵測入口轉址，登入世代由 1 變成 2，原 PRQ 文件目錄查詢再次取得六筆 | 受控 Cookie 遺失已有證據；自然 TTL、其他子系統與病人 context 的恢復未在本輪逐一測試 |
| 錯誤帳密辨識 | 明確登入與按需查詢兩條路徑，各一次錯誤密碼 POST，均回報 PORTAL_LOGIN_REJECTED，無額外密碼重送 | 未取得實際帳號鎖定、密碼到期或其他拒絕頁面的樣本 |
| 人事查詢 | 取得八個職稱、73 個單位；精確卡號、員工編號、姓名及「職稱加員工編號」各取得本人資料 | 全目錄另有 HAR 的 323 筆解析證據；人員明細未錄製 |
| 單位／包含下層單位 | 表單與人事資料均有單位，但 0.19.3 測試器未對應含代碼的標籤，因此沒有送出查詢 | 0.19.4 已離線確認能唯一對應；兩種條件仍未內網驗證，不把舊 NO_SAMPLE 改成實測成功 |
| TLS 自動相容 | 兩次匿名探測 TLS EOF 均恢復；所有套用模式保留 HTTPS 與憑證驗證 | 協定切換、限定來源的憑證備援與嚴格模式另有 localhost 真實 TLS 握手測試 |

原始錯誤回應仍保留：只將已完成且 capture 範圍相符的負向登入列為預期拒絕；網路重試只在同一 request_group_id 有後續 HTTP 時標為恢復。後續 Parser／HTTP 錯誤不能被覆蓋。

## 其他功能既有證據

最近登入專項僅對三個原子查詢取得非空資料，其他 54 個未在該輪重跑；下表保留各自已有的證據。

| 路徑 | 已有證據 | 限制 |
| --- | --- | --- |
| 病人基本資料、掛號、門診、SOAP、醫囑 | 多次內網回傳取得資料 | 空結果不代表已取得非空樣本；掛號不等於當日就診 |
| 結構化 SOAP | 歷史 HAR 可辨識 S／O／跨列 A+P；9/23 第二份修正後 EXE 回傳的 4 筆 SOAP 均有 S／O／A+P、診斷及藥囑，其中 1 筆正確結構化慢性處方期限；0.20.3 另有一筆舊號門診 SOAP 取得 S／O／A+P 與診斷。 | 另兩名病人的異號清單在舊版當時阻擋後續 SOAP，未以新版逐一複驗。摘要不是完整醫囑，ICD 版本與主次不推論，詳見 [SOAP](SOAP.md) |
| 病歷號／病人身分證 → 就診清單 | 0.17.2／0.18.0 兩條輸入清單一致，身分證來源成功串接門診 SOAP／醫囑；0.20.3 院內回傳取得 38 筆同病人新舊號就診並沿舊號取得一筆門診 SOAP | 舊號醫囑及其他端點尚待抽樣；其他病人的舊號關係不由姓名或日期推斷 |
| 就診日期、類別、科別、醫師姓名篩選 | 0.17.1／0.17.2／0.18.0 內網欄位及組合篩選一致 | 就診清單有 O／A／E，不代表住院／急診 SOAP 或醫囑端點已支援 |
| 就診醫師卡號 | 0.18.1 用原回應修正互斥分支，離線取回住院／急診的實際 vsNo | 門診來源仍空白；以人事卡號轉姓名再篩選是另一路徑，同名仍需核對 |
| 眼科醫囑 PDF／JPG | 0.12.0 內網取得 DBR PDF 與 JPG | 未實作 OCR／PDF 數值擷取 |
| 門診掃描病歷 PDF | HAR 離線重解析找到單次 SOAP 與歷年掃描兩條參照；0.20.5 院內 EXE 取得 6 筆眼科 SOAP 掃描連結、9 筆歷年參照及 4 份完整 PDF。新版只讀解析可由頁面病歷類別辨識 8 筆眼科 `RECORD` 與 1 筆術前標示 `OPG` | 兩份 PDF 專項 HAR 無 network entry；EXE 只抽 6／8 筆眼科就診及 4 份 PDF。0.20.7 分類及全數組合流程尚未內網實測，見 [SCANNED_RECORDS](SCANNED_RECORDS.md) |
| 數值表格與眼科檢查 | 0.20.4 院內單次眼科報告取得 IOP、Va、驗光／散瞳前三張表、六列；日期／OD／OS 逐欄路徑均對齊，手填 `error` 仍在原欄。跨期間報告另有 23 張、131 列，無解析問題 | 兩張眼科表的原始 `colspan` 超過實際 OD／OS 欄數，來源問題碼保留並計為可恢復警示；舊號單次報告為空；不轉換為臨床數值型別，見 [NUMERIC_REPORTS](NUMERIC_REPORTS.md) |
| 手術碼案例、手術紀錄 PDF | 0.15.1 內網取得清單、紀錄與 PDF | 特定醫師、代碼與期間的抽樣，不代表全部條件 |
| 病人歷史手術、紀錄 PDF | 0.15.1 內網列出參照並抽測 PDF | 不是已下載所有歷史檔案；未知按鈕保留解析問題 |
| 審查 OAuth、案件、四種明細 | 0.15.1 內網沿用 Portal Session 取得案件與抽樣明細 | 附件／PACS 為清單，未實作本體下載；未逐案抓完全部明細 |
| MIS 績點／專勤工作獎金 | 0.15.1 取得 HTML／文字；0.16.0 以同一回傳修正巢狀主表解析 | 新解析為離線驗證；MIS 獨立密碼不套用主系統自動重登入 |
| 手術排程表單 | 合成／HAR 流程驗證 | 最近測試病人沒有既有排程樣本 |
| 手術排程清單 | 兩份錄製 HAR 共 70 筆資料只讀重解析；0.20.1 院內另取回 71 筆，網頁常用欄位及完整來源列均保留，50 筆 `TF` 未定時間均不回傳假開始時間 | 12 筆麻醉來源空白；來源狀態代碼尚無業務對照；原始列含個資及院內識別資訊，見 [SURGERY_SCHEDULE](SURGERY_SCHEDULE.md) |
| 最近七天門診 → 實際就診 → SOAP | 組合流程與合成測試存在 | 未由最近內網回傳完成整個組合流程 |
| 新增／編輯／取消排程、同意書提交 | HAR 與合成回應識別 | EXE 排除異動，未宣稱正式寫入全面驗證 |

NO_SAMPLE 只表示該項缺少可用測試輸入或樣本，不是錯誤根因。只有缺口時分析器回傳 COMPLETED_WITH_GAPS、exit code 0；實際 HTTP、登入或解析錯誤仍回報錯誤。原件位於本機 data，較早的失敗不會被新解析覆寫。
