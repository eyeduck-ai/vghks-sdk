# 內網測試 EXE

雙擊 `dist/vghks-live-test.exe` 使用建置時選定的計畫。只需搬一個 EXE，不讀旁邊過時的設定檔。建置工具預設 comprehensive，可用 `--default-profile password`、`session`、`failures`、`scans`、`regression`、`login`、`visits` 或 `soap` 選擇專項版本；先用 `--plan` 檢視範圍。

目前 SDK 原始碼與本機單檔 EXE 為 0.22.7，EXE 內建 `password` 計畫，以一次舊密碼登入收集目前強制改密碼的院內回應。這是新增的專項測試；強制變更尚待實際 ZIP 驗證。先前 0.22.4 `session` 院內回傳已確認隔離 Cookie 遺失後直接 API 自行恢復 SSO 及三份結構化資料一致，自然 TTL 仍未測。驗證範圍見 [VALIDATION](VALIDATION.md)。

| profile | 用途 | 啟動輸入 |
| --- | --- | --- |
| `password` | 一次舊帳密登入、強制變更頁面及限定唯讀對照 | 舊 Portal 帳密，可選一名授權病人的病歷號 |
| `session` | WebMAAS 表單驗證、基本資料、Cookie 遺失或手動閒置對照 | 一名授權病人的病歷號、Portal 帳密 |
| `failures` | 資料狀態、登入拒絕／通知及 Cookie 恢復 | 授權病歷號、Portal 帳密 |
| `regression` | 病人／SOAP／數值回歸、調閱審查、兩院掛號比較 | 授權病歷號、Portal 帳密，第二組帳密可略過 |
| `scans` | 單次及歷年眼科掃描與 PDF 抽樣 | 授權病歷號、Portal 帳密 |
| `soap` | 登入醫師指定日的多病人 SOAP 抽樣 | Portal 帳密 |
| `login` | 登入、人事及正常查詢後的有限負向測試 | Portal 帳密 |
| `visits` | 病歷號／身分證查詢、比對及篩選 | Portal 帳密、授權病歷號，病人身分證可自動取得 |
| `atomic`／`comprehensive` | 指定唯讀操作／完整涵蓋計畫 | 依計畫要求輸入，MIS 使用獨立帳密 |

建置及 localhost 驗證工具見 [DEVELOPMENT](DEVELOPMENT.md#建置)；各計畫的實際順序與限制如下。

## 強制改密碼與舊帳密觀察（password）

目前有強制改密碼頁面時，將新版 `dist/vghks-live-test.exe` 帶入內網並雙擊，輸入院方原帳號與舊密碼；不要填希望改成的新密碼。病歷號可直接 Enter 略過，或提供一名有權讀取的病人。沒有病歷號仍可完成登入及頁面訊號觀察。

1. 獨立 Session 不先登入，直接查一次 PRQ 文件類型目錄，密碼 POST 預算為零。保存匿名登入挑戰或合法 JSON 目錄，作為後續對照。
2. 主 Session 以輸入的舊密碼登入。整輪實際密碼 POST 上限為一次；未知結果、被拒絕、強制變更或之後查詢要求重新登入時，也不會再次送出密碼。此計畫不產生另一組錯誤密碼。
3. 保存登入原始 HTTP／HTML／轉址與 SDK 的 `PasswordStatus`。若回應提供唯一、同來源的 `/changePassword.do`、`/changePwd.do` 或 `/modifyPassword.do` 導覽，再 GET 該頁一次；不跟隨其轉址、不重試此 GET、不執行 JS／callback，也不提交改密碼表單。表單 action 不當成 GET 導覽。沒有這種目的地為 `NO_SAMPLE`，已出現在登入回應的強制頁面仍完整保存在 `responses/`。
4. 登入失敗時，用當次 Cookie 直接再查一次相同 PRQ 目錄，不觸發 SSO 或登入恢復。SDK 相依查詢列為 `BLOCKED`。目錄回應即使合法可讀，也不證明帳密有效或所有病人資料可讀；若匿名對照也可讀，更不能歸因於舊密碼。
5. 登入正常完成時，呼叫 SDK 的目錄 API，再以提供的病歷號查一次 WebMAAS 基本資料；沒有病歷號為 `NO_SAMPLE`。沒有就診／SOAP／附件、調閱審查或異動；維持預設 0.8–1.8 秒節流，不清 Cookie、不等待自然過期。

`parsed/password/summary.json` 分開保存登入原因、`credential_validity`、SDK 原密碼狀態、額外可信頁 GET 與該頁自己的通知、匿名目錄／既有 Cookie 目錄及 SDK 基本資料結果。只有 SDK 正常完成登入為 `ACCEPTED`；明確密碼拒絕為 `REJECTED`；強制變更、HTTP 拒絕及未知頁面維持 `UNKNOWN`。這不是測試帳密永久有效的保證。

強制變更是要收集的情境，但原登入錯誤仍保留，所以完整跑完也可能輸出 `COMPLETED_WITH_ERRORS`／exit code 1。正常登入卻沒有強制頁或沒有病人樣本，為 `COMPLETED_WITH_GAPS`／exit code 0。請帶回新產生的 ZIP，分析 `password_change_test` 與各步結果，不能只看檔名。離線分析不重送請求，重測設定保留 `password` 的一次密碼預算與原範圍。

ZIP 未加密，完整登入請求、Cookie 及病人回應僅留本機，不放公開 Git／Issue。設定範例見 [password-change.example.json](../configs/password-change.example.json)，不含帳密。

```sh
python tools/build_live_test_exe.py --default-profile password
python tools/verify_password_exe.py --source
python tools/verify_password_exe.py
dist/vghks-live-test.exe --plan
```

驗證器以十五種 HTTPS localhost 情境核對文字／表單／轉址強制變更、倒數、未知／HTTP／密碼拒絕、匿名目錄可讀、合法空目錄、強制變更後目錄可讀及查詢過期。核對實際密碼只送一次、改密碼零次、原始 HTML 的 ZIP 保存與實際 CLI 離線分析；這些合成結果不等於院內強制變更已驗證。

## WebMAAS Session 專項（session）

雙擊新版 EXE：輸入一名授權病人的病歷號與正確 Portal 帳密，先驗證目前表單／token，再查 CHECK_PAT 與完整基本資料。正常階段成功後，只清除可隔離至 WebMAAS 的 JSESSIONID，保留共用 Portal Cookie，再檢查同一 SDK 與病人的回應。找不到可隔離 Cookie 為 NO_SAMPLE，不改成清掉所有 Cookie。此計畫沒有 PRQ 調閱審查、錯誤密碼、異動或附件下載。

明確 Portal 挑戰由 Runtime 至多恢復一次。若 Portal 正常、只有 WebMAAS 缺表單／token 或 `WEBMAAS_SESSION_TIMEOUT`，且 Runtime 尚未恢復，EXE 可另外做一次 SSO readiness 複查；SDK 此時已清除 WebMAAS 快取。不主動強制 Portal 登入，複查仍失敗就停止病人查詢。原錯誤即使後來恢復仍保留 ERROR，因此 ZIP 可能是 COMPLETED_WITH_ERRORS；應同時查看 `parsed/session/comparison.json` 的 `sso_recheck_succeeded`，不能只看 ZIP 檔名。

Cookie 對照恢復及病人讀取成功後，再清除一次同樣可隔離的 WebMAAS Cookie，直接呼叫基本資料 API；這次沒有 EXE readiness 或重試，驗證 Runtime 對明確 GET／唯讀 POST timeout 的一次 SSO 恢復。`direct_read_after_cookie_loss.status` 保存 API 結果，原回應及 `application_session_recovery_started`／同一操作完成事件才證明確實發生恢復；成功讀取本身不證明過期。若前一步已經重送 Portal 登入，或使用手動閒置模式，省略第二次挑戰。正常情況最多三份基本資料結果，不擴大病人樣本。

離線分析的 `session_test.direct_api_recovery.status=VERIFIED` 表示 timeout、原角色 SSO、新表單及最終資料都在同一 SDK 操作中核對成功；`direct_patient_values_equal` 另比對第三份資料與基準。UNVERIFIED 表示缺少完整恢復證據，NOT_OBSERVED 表示成功讀取但沒有觀察到 timeout／恢復，NO_SAMPLE 表示沒有可隔離的 Cookie。`recovered_requests` 保存已恢復的原錯誤，原 readiness ERROR 仍在 `problems`，因此 COMPLETED_WITH_ERRORS 與直接 API 恢復成功可以同時成立。

Session 的登入／SSO 表取最後一個實際執行的 readiness 報告，先前問題另留 `problems`。未執行的複查檔及較早的成功不能替代最後一次失敗；若最後一次沒有返回報告，就維持沒有 readiness 證據。

預設不等待自然過期。若要重現本次閒置問題，從命令列啟動：

```powershell
.\vghks-live-test.exe --profile session --session-pause
```

正常階段完成後，視窗停在按 Enter 的提示；保持視窗開啟並閒置到要測試的時間，再按 Enter。等待期間沒有背景請求，也不清 Cookie、不另建 SDK，輸出記錄實際閒置秒數。這是原 Session 的閒置觀察；沒有明確來源回應時仍不宣稱確認自然 TTL。此選項需要互動主控台，不能和 `--non-interactive` 使用。

ZIP 保存完整原始 HTTP、轉址、失敗 HTML、當次安全登入報告與前後基本資料。`parsed/session/comparison.json`（schema 2）連結原失敗、獨立 SSO 複查及直接 API 結果，`step_results.json` 保留各步 capture 範圍。可用 [webmaas-session.example.json](../configs/webmaas-session.example.json) 指定設定；範例只有合成病歷號。建置後使用 `tools/verify_session_exe.py`，其十二種 localhost HTTPS 情境與院內結果分開。

## 資料獲取與失敗分類（failures）

只需搬 `dist/vghks-live-test.exe`，雙擊後輸入一個授權病歷號與正常 Portal 帳密。帳密在執行時輸入，不內嵌、不寫設定檔。測試完成後帶回 EXE 同目錄新產生的時間命名 ZIP；不需 CMD 或額外設定檔。

1. 先執行 68 個無 socket 模擬：20 個既有登入、19 個傳輸／Runtime、12 個資料狀態、17 個未登入／過期／密碼通知案例。包括未登入與既有登入遭遇 401／403、轉址／表單的區分，三天與「【4】日後」提醒、強制變更文字／表單／導覽、未知密碼頁、無效 callback，以及原有 DNS、逾時、TLS、HTTP、解析及資料狀態。使用合成帳密與記憶體 adapter，不向醫院送請求。
2. 全新匿名 Session 直接查 PRQ 文件類型目錄，不先呼叫登入，密碼 POST 預算為 0；保存真實登入挑戰，或在沒有挑戰時記 NO_SAMPLE。此一步沒有病人參數。未知 HTML／JSON 或網路錯誤仍列 ERROR。
3. 另一獨立 Session 使用同帳號、自動產生的不同密碼，最多送一個密碼 POST。只有明確 `PORTAL_LOGIN_REJECTED` 才通過並繼續；逾時、未知回應、HTTP 拒絕、強制變更或意外成功時停止後續登入。可用 `--login-negative-attempts 0` 略過，failures 不接受 2。這項順序依使用者本次授權；舊 login 計畫仍在正常查詢後做負向測試。
4. 以正常帳密檢查 Portal／PRQ／WebMAAS，保存密碼通知；倒數提醒不阻斷成功登入，明確強制變更則停止相依讀取。查基本資料、掛號及完整就診清單，抽最多兩次門診的 SOAP、數值、醫囑，以及兩份明細、報告、JPG 清單。未執行醫囑略過明細，不下載 PDF／JPG、不做 OCR。
5. 正常 PRQ 目錄查詢成功後，只清除本機 Cookie，再查同一目錄觀察 Runtime 恢復，保留 generation 與實際回應；未重登入記 NO_SAMPLE。依使用者選擇不等待自然過期，此步驟與合成過期均不證明 TTL。恢復時遇強制變更，保留具體錯誤並停止。
6. 其他單筆錯誤保留並繼續可執行項目；合法空結果為 EMPTY、缺下游參照為 NO_SAMPLE、來源／登入失敗為 BLOCKED。未知 schema 不轉成空值。

此計畫不送變更密碼表單，也不做手術／同意書異動或薪資查詢。院方調閱審查若自然出現，沿用 SDK 核對後自動提交一次 `1A` 的既有流程；送出結果不明不重送。仍維持預設 0.8–1.8 秒隨機節流。

ZIP 的 `parsed/failures/simulations.json` 明示 SIMULATED；`observations.json` 與 `live/` 保存實際 AcquisitionResult、原模型與結構化錯誤。`classification_coverage.json` 列出各分類是否在實際查詢被觀察到，也計入登入預檢及 Cookie 恢復錯誤；其安全原因保留在 additional_error_steps。未出現的網路／登入等錯誤維持 NO_SAMPLE，不能用模擬通過代替院內證據。`cookie_loss.json` 及 coverage 的 natural_ttl 欄位固定保留自然 TTL 未驗證狀態。完整原始 HTTP 與 parsed 值仍可能含個資，ZIP 未加密，僅留本機。

`live/unauthenticated.json`、`live/negative_before_login.json` 保留觀察及密碼送出計數；`negative-post-counts.json` 即使負向測試失敗也寫入。`password-status-*.json`、`negative-password-status.json`、`readiness.json` 保存安全通知狀態，原文字與表單留在 raw response。`failures.live.password_policy` 列出是否觀察到提醒，NO_NOTICE 對應 NO_SAMPLE。離線分析僅依通過的明確步驟、零／一次密碼計數、capture 範圍及已知錯誤碼分出預期未登入回應與拒絕；其他錯誤仍保留。

```sh
python tools/build_live_test_exe.py --default-profile failures
python tools/verify_failure_exe.py
dist/vghks-live-test.exe --plan
```

`verify_failure_exe.py --source` 可先檢查原始碼；正式驗證以新建置 EXE 的零參數啟動執行 23 種 HTTPS localhost 情境，涵蓋原有資料與失敗、匿名挑戰、三天／方括號提醒、強制變更及負向結果不明時停止。另核對已對齊表頭警示與 PDF 按鈕不誤判 PARTIAL、模擬數量、實際密碼 POST 次序／次數、讀取上限、ZIP 與離線分類。這些結果不代表新版已在內網通過。

離線 `analysis.json` 的 `password_policy` 與 `data_quality` 分開保留原 EXE 的觀察和目前解析器對保存回應的結果。原 NO_SAMPLE 不改寫；`data.warnings` 是可恢復來源問題，`data.issues` 仍表示解析不完整。錯誤密碼拒絕本輪已驗證，需要再觀察倒數或其他資料時可用 `--login-negative-attempts 0`，不必為補樣本重做負向登入。

## 高榮與聯合醫院掛號比較、病歷調閱審查（regression）

以下流程需重新建置 `regression` profile；本機現行 EXE 使用上節的 `password` profile。

只需搬 `dist/vghks-live-test.exe`。雙擊後先輸入高榮帳號可查的授權病歷號、Portal 帳密；接著可輸入聯合醫院 Portal 帳密與其授權病歷號，直接 Enter 可略過第二組。兩組帳號各用自己的 SDK Session，分別查一次掛號，並在另一個全新 Session 先查 `CHECK_PAT` 再查掛號。聯合醫院可使用與高榮相同或不同的病歷號。帳密只在執行時輸入，不寫入設定；測試結果與完整原始回應一起留在同一份 ZIP，`registration_comparison.json` 列出兩組帳號各步狀態與請求形狀。

若院方要求病歷調閱審查，EXE 會核對病歷號、登入 HID、表單位置及當頁選項，再自動送出錄製的 `1A`（「了解病情」）一次；無需另外輸入代碼。當頁未提供該選項或結果無法確認時會報錯，不重送結果不明的審查請求。

此 `regression` 計畫仍以高榮帳號取得病人基本資料、掛號清單、完整就診清單、最多兩份眼科 SOAP 與數值報告，以及數值歷史、登入醫師手術排程。聯合醫院帳號只做掛號比較；若其中一組登入失敗，另一組仍繼續，該帳號不會再重送密碼。逐步結果和原始 HTTP 回應保存在 EXE 同目錄的時間命名 ZIP；ZIP 未加密，僅留本機。請帶回新 ZIP 供分析，毋須 CMD 或設定檔。`--access-review-reason` 僅供明確覆寫預設原因。

```sh
python tools/build_live_test_exe.py --default-profile regression
python tools/verify_regression_exe.py
```

### regression 的其他檢查

`regression` 可用 `--defaults private/regression-test-defaults.json` 建置自用版本，只內嵌授權測試病歷號。雙擊後輸入 Portal 帳號與密碼，毋須搬設定檔；若未內嵌則啟動時輸入病歷號。若需換病人，可從命令列用 `--test-mrn` 覆寫。此計畫不做錯誤密碼、薪資或附件下載；審查表單只有實際出現且核對通過時才會送出。請求仍依 SDK 預設循序及隨機節流。

測試依序取得病人基本資料、完整就診清單，並抽最多兩次眼科門診就診查結構化 SOAP 與單次數值報告；若清單有舊病歷號的眼科門診，保留一筆近期就診，另一筆改抽最近的舊號就診。每筆 `VisitCase.mrn` 和 `lookup_mrn` 一起存入結果，步驟摘要另列不同來源病歷號數及舊號就診筆數。同一病人的跨期間數值報告及登入醫師的手術排程另行執行。排程預設範圍為測試當日前 29 天至後 30 天，只有一次排程清單查詢。數值結果保留雙層表頭、欄位路徑、原始值與解析問題；步驟摘要分列 `numeric_warning_count` 與 `numeric_error_count`，只有無法確定欄位對應的問題使步驟標 `NUMERIC_TABLE_PARSING_ISSUES`。手術排程保留網頁可見欄位、完整來源列與 `TF` 未定時間狀態。

若病人查詢狀態無法確認、異號連結不屬於啟用的正式就診列，或來源身分證明示衝突，仍會阻擋後續就診查詢；SOAP 和單次報告標為 `BLOCKED`，數值歷史與手術排程仍繼續。ZIP 的 `capture_manifest.jsonl` 可找到 `QueryCaseList.do` 對應的 `responses/` 完整原始頁面；`errors.jsonl`、`step_results.json` 與 `parsed/atomic/` 可對照錯誤、查詢條件和已完成的結果。舊號就診的 SOAP／數值步驟會分別保存原始 HTTP 和解析結果，單筆失敗不掩蓋其他步驟。

```sh
python tools/build_live_test_exe.py --default-profile regression --defaults private/regression-test-defaults.json
dist/vghks-live-test.exe --plan
```

測完只需帶回 EXE 同目錄新產生的時間命名 ZIP；ZIP 未加密，含病人資料、登入請求及 Session，僅留本機。`live-test-results` 在 ZIP 成功產生時毋須另外搬回。

## 單次門診與歷年眼科掃描病歷（scans）

以下流程需先重新建置 `scans` profile；本機現行 EXE 使用 `password` profile。

只需帶 `dist/vghks-live-test.exe`。雙擊後輸入授權病歷號、Portal 帳號與密碼；可選填單次門診日期，不填時抽最近最多六次眼科門診。EXE 查完整歷年掃描清單，逐筆保留表格、病歷類別、顯示日期、`RECORD`／`OPG` 來源 subtype 與 PDF 參照。眼科樣本依「門診-記錄-眼科紀錄」病歷類別選取，最多下載四份 PDF，優先交替抽歷年眼科與單次就診參照。這是**抽樣驗證**，不代表已查每次眼科就診或下載歷年全部 PDF；SDK 使用者可用 `get_upload_history(mrn).scanned_records` 遍歷全清單並逐筆下載。沒有符合樣本時記錄 `NO_SAMPLE`，不以 HTTP 成功代替 PDF 成功。

此計畫只執行 PRQ 就診清單、SOAP、歷年掃描清單與 PDF 下載，以及必要的 Portal／PRQ 登入；不做異動、錯誤密碼、薪資或其他報告查詢。結果 ZIP 直接存於 EXE 同目錄，未加密，僅留本機。帶回新產生的時間命名 ZIP 供分析即可，不需要 CMD 或設定檔。

兩條來源已有院內抽樣證據，類別辨識與完整組合流程的驗證層級另見 [SCANNED_RECORDS](SCANNED_RECORDS.md) 及 [VALIDATION](VALIDATION.md)。

```sh
python tools/build_live_test_exe.py --default-profile scans
dist/vghks-live-test.exe --plan
python tools/verify_scan_exe.py
```

## 多病人結構化 SOAP（soap）

建置使用 `soap` 計畫時，雙擊後輸入 Portal 帳號及密碼，即查預設 **2026-09-21** 登入卡號的門診清單，不需手填病歷號或設定檔。程式以回傳的醫師欄判斷專屬清單，接受帳號本身或帳號加 `F`；科別代碼只作就診比對，不用來推斷歸屬。共用及歸屬不明清單照樣保存，但不擅自查詢其病人。

從專屬清單選最多 8 個**不同病歷號**，優先涵蓋不同科別／診間。每人先保存完整就診清單，再以同病歷號、日期、科別及門診類別匹配當日就診；掛號而無實際就診者記 `NO_SAMPLE`。每人最多抽 2 次匹配就診查 SOAP，保留 S、O、A+P、診斷、醫囑與藥囑的完整結構化回傳及解析問題。單一病人或 SOAP 失敗，仍測其餘病人；無法建立登入時停止相依查詢。不進行錯誤密碼、薪資、附件或異動測試。預設請求間隔仍為隨機 0.8–1.8 秒。

`parsed/soap/` 中的 `roster.json`、`classified_roster.json`、`selection.json`、每名病人的 `visit_cases.json`／`matching_visits.json`／`soap_*.json` 及 `field_coverage.json` 分別保存各階段證據；原始 HTTP 本文另在 `responses/`。SOAP JSON 另有慢性處方服藥期限的結構欄位；沒有慢性處方時是空清單，覆蓋率只記錄有此樣本的筆數，不強制視為缺口。只有少於兩名可查病人或缺其他必要欄位樣本時是驗證缺口，不代表資料解析成功，也不推論病人沒有該類臨床資料。帶回 EXE 同目錄下新產生的時間命名 ZIP 即可，毋須帶回 `live-test-results`。

```sh
python tools/build_live_test_exe.py --default-profile soap
dist/vghks-live-test.exe --plan
dist/vghks-live-test.exe --profile soap --soap-date 2026-09-22 --max-cases 12
```

`--soap-date` 只用於另一次指定日期測試；不給參數時該版 SOAP 計畫固定 2026-09-21。`--max-cases` 調整病人上限，`--max-items` 調整每病人的就診上限。這些選項不會改變 SDK 的原子操作。

結構化 SOAP、舊號及缺匹配就診的院內樣本分別記錄於 [SOAP](SOAP.md) 與 [VALIDATION](VALIDATION.md)；重解析成功不改寫原 ZIP 的執行狀態。

## 登入及人事專項計畫（login）

只輸入正常的 Portal 帳號與密碼，之後自動執行，完成再按 Enter 關閉。**不需要病歷號、身分證、薪資密碼或設定檔**。只帶回 EXE 同目錄、檔名含時間的 ZIP。

1. 先跑 20 個離線模擬情境，使用 SDK 真正的登入／Requests 流程及記憶體回應，不建立網路連線：正常、空白帳密、未知帳號、錯誤密碼表單／文字錯誤頁、401／403、空白／未知頁、回到登入頁、302／307／308、逾時、一次／反覆過期及重新登入遭拒；包含 302 轉向 HTTP 入口與入口自動跳轉頁。
2. 使用輸入的正常帳密登入，逐一檢查九個登入目標、登入重用，以及 PRQ 文件類型查詢。子系統失敗不阻止獨立檢查。
3. 查詢人事選項與登入帳號的精確員工資料，再測員工編號、姓名、職稱、單位與含下層單位的條件。職稱／單位必須能與當次表單選項唯一對應，且與本人員工編號合併查詢；缺對應保留 NO_SAMPLE，不猜代碼或下載整份人事名冊。
4. 清除本機 Cookie，再做 PRQ 查詢，記錄是否重新登入及恢復。若伺服器仍接受原有 SSO token，記 NO_SAMPLE，表示未觸發過期；此測試不等待或證明伺服器自然逾時。
5. 最後使用同一帳號與自動產生的錯誤密碼，最多兩次，分別驗證 `auth.login()` 與查詢時的按需登入。每次獨立 Session、最多一個實際密碼 POST，轉址及意外重試同樣受限。第一筆正常登入失敗則全部略過；第一筆負向測試若逾時、回應不明或意外成功，停止第二筆負向測試。

一般 SDK 不會主動做錯誤密碼測試。`login` 在正常查詢後最多兩次，`failures` 在正常登入前最多一次；順序與上限各自固定。重跑 EXE 會開始新一輪。`login` 可用 `--login-negative-attempts 0` 關閉，或設 `1`，不允許超過 `2`。

`parsed/login/` 保存逐項結果、模擬請求次序、人事條件與結果、Cookie 清除前後的登入世代、每次負向測試的密碼 POST 計數。`RESULTS.txt`、`run_summary.json` 將 SIMULATED 與 LIVE 分開。預期的 `PORTAL_LOGIN_REJECTED` 是負向測試通過，不代表登入成功；未知頁面、額外重送或 HTTP 錯誤仍列 ERROR。

離線分析僅在完整步驟確認一次密碼 POST、預期拒絕且 capture 範圍相符時，將該拒絕列為 EXPECTED_NEGATIVE，原始回應不改寫。受控 Cookie 清除後恢復成功的原始 401／403 另列 recovered，後續解析錯誤不會被掩蓋。

登入、PRQ 恢復、人事主要查詢及兩次預期拒絕已有 0.19.3 內網證據。補測單位篩選時不必再做錯誤密碼，可用 `--login-negative-attempts 0`；自然 TTL 仍未量測。

```sh
vghks-live-test --plan --profile login
vghks-live-test --profile login
vghks-live-test --profile login --login-negative-attempts 0
```

## 就診搜尋（visits）

病歷號與身分證兩條路徑已有內網證據；不同輸入、門診卡號缺值及住院／急診的限制見 [VISITS](VISITS.md)。此計畫供不同樣本或新版流程驗證，不需為已可離線解析的欄位重跑完整功能。

1. 輸入 Portal 帳號／密碼。
2. 測試病歷號直接 Enter 沿用內嵌值，也可輸入另一個已授權病歷號。
3. 病人身分證直接 Enter，程式會從該病歷號的基本資料取得；也可手動輸入同一病人的身分證。這不是登入者或 MIS 的身分證。
4. 執行完成後，只帶回 EXE 同目錄下的時間命名 ZIP。

只執行必要登入／連線、病歷號與身分證兩條就診查詢、病人核對與清單比對、到院日／O-A-E 類別／科別／醫師的本機篩選。選定門診後以最多三筆樣本驗證 SOAP／醫囑串接，各項取得非空資料就停止抽樣；眼科優先。身分證查詢失敗時仍以病歷號結果完成可獨立執行的檢查，輸出會註明 fallback，不能視為身分證已驗證成功。

預設不執行手術、薪資或附件下載等業務異動；PRQ 如要求病歷調閱審查會依 SDK 預設提交錄製原因一次。基本資料僅在需要自動取得病人身分證時查一次；手動輸入身分證時只需要 Portal／PRQ。沒有住院、急診或可串接門診的樣本記 NO_SAMPLE，保留驗證缺口，不判成程式錯誤。沒有已錄製的住院／急診 SOAP 端點，測試器不嘗試這些路徑。

`parsed/visits/` 包含兩份完整清單、各自輸入、身分核對、逐筆差異、欄位涵蓋率、各篩選結果及抽樣 SOAP／醫囑。`RESULTS.txt` 列出每一項增量檢查；HTTP 原始本文與例外也保留。

一般原始碼或通用 EXE 可明確指定 `--profile visits`。可選 `--patient-national-id`／`VGHKS_PATIENT_NATIONAL_ID`；不把此值放入一般設定檔。`--plan --profile visits` 可離線查看範圍。

## 人事單項測試

`vghks-live-test --profile atomic --only personnel.options --only personnel.search` 只測人事選項及登入帳號的人事清單，原始回應與查詢條件照常保留。新的 SDK 不再要求就診清單有可用醫師卡號，卡號應先透過人事取得姓名。0.19.2 登入專項 EXE 已包含這些查詢及多條件檢查。

## 完整測試（comprehensive）

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
| parsed/soap/ | 指定日期門診清單、多病人就診與結構化 SOAP |
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
