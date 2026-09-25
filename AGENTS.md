# Agent 開發指南

本 repo 是可組合的 Python SDK；應用程式與院內測試器是其使用者。先讀 README、docs/ARCHITECTURE.md、docs/API_REFERENCE.md，再依任務閱讀領域文件。使用者在目前對話的明確需求優先於本文。

## 開始工作

1. 檢查 `git status --short`；保留既有未提交修改，不以 reset／clean 還原使用者工作。
2. 使用 `rg` 搜尋相關 Service、Adapter、Parser、模型與測試；不要先掃描 HAR／returns 的全部個資。
3. 版本唯一來源為 `src/vghks_sdk/_version.py`。`operation_models.py` 是相容匯入層；新程式從 models 匯入。
4. 正常 import 不連線。合成測試不應要求密碼、HAR、returns 或內網。
5. `data/`、`private/`、`output/`、`dist/` 是本機資料，不屬於公開原始碼。若本機不存在，使用合成案例，清楚保留內網未驗證狀態。

## 分層與修改順序

| 層 | 責任 | 改動時看哪裡 |
| --- | --- | --- |
| models | 型別、欄位驗證、參照物件 | models/<domain>.py；models/__init__.py；頂層 __init__.py |
| parsing | HTML／JSON／二進位純解析 | parsing/<domain>.py；不能發 HTTP 或 eval JS |
| adapters | 對應 HAR 的 HTTP 流程 | adapters/<domain>.py、相關 Protocol |
| runtime/core | Session、SSO、操作鎖、retry、節流、診斷 | runtime.py；core/；adapters/auth.py |
| services | 穩定公開 API | services/<domain>.py；sdk.py 注入 |
| queries | 可發現的唯讀操作、輸入、發現相依 | queries.py |
| workflows | 多原子操作組合、去重、階段存檔、錯誤續跑 | workflows/ |
| live | EXE 參數、讀取測試、coverage、結果 ZIP | live/、live_test_app.py |
| contracts/offline | 只讀 HAR／ZIP 的結構驗證與重解析 | contracts/、offline/ |

新增原子操作以「一份有意義結果」為單位；SSO、病人 context 及必要分頁可以包含多次 HTTP。不要把 HTTP 細節交給 workflow，也不要讓 Parser 自動選下一名病人。

## 重要行為不變量

- PortalCredentials 與 EarningsCredentials 分開；不能把密碼存進 config、repr、一般診斷或公開範例。Raw capture 刻意保留完整內容，因此其輸出只能留本機。
- 同一 SDK 共用 Session 與病人／模式 context；保持 operation_lock 跨整個相關操作。多帳號用不同 SDK。
- 讀取重試與登入恢復由 Runtime 控制；密碼 POST 及異動不能因一般 retry 自動補送。
- `LoginRejectedError` 與 `AuthExpiredError` 必須分開；前者不能觸發重新登入。Portal 登入建立階段的 401／403、空回應、錯誤頁不代表既有 Session 過期；審查 OAuth 送出密碼後也不能因 401／403 重跑整段流程。Runtime 恢復失敗時保留原本 AuthenticationError 及具體錯誤碼；未知原因不猜密碼錯誤。主系統登入只允許有限次同來源 GET 轉址，307／308 不重送密碼。
- Portal 文字拒絕頁不一定有登入表單；不可把「重新登入」按鈕的 onclick 當成自動導覽。只解析 script 頂層 literal 指定，忽略註解／字串／callback。JSON 查詢的過期 302 可指向舊 HTTP 入口，應在跟隨前辨識並由 Runtime 回到原 HTTPS 登入，不將入口 HTML 交給 JSON parser；明確自行處理轉址的 Adapter 保留其責任。
- 保留預設 0.8–1.8 秒隨機節流。用 local mock 測 request sequencing，不對內網做負載測試。
- 共用 core/connections.py 負責 TLS；PRQ／SectOrd／WebMAAS 優先 TLS12_COMPAT，相同 HTTPS 主機／埠共用狀態。明確憑證錯誤可依 allow_unverified_tls（預設 True，使用者已授權內網備援）只對該來源略過驗證；不要全域 verify=False 或自行改 HTTP。嚴格模式 False 必須維持有效。
- 初次密碼／異動 POST 前可做獨立匿名探測，不帶 Cookie／Authorization／body，不跟隨轉址。POST 本身不能因 TLS 政策重送；HTTP 回應只證明連線，不證明登入或資料。連線狀態不落地、不在 import／建構 SDK 時發網路。
- VisitCase 與各 Ref 綁定病人／就診。下載僅接受允許來源、路徑與正確病人，不擴成任意 URL 下載器。
- 病人身分證使用 records.get_visit_cases(national_id=...) 的 type=2 路徑；先核對病人標頭並解析實際病歷號，不將身分證當 mrn。0.17.2／0.18.0 內網已驗證兩份清單一致及以 national_id 來源串接門診 SOAP／醫囑。0.18.1 離線修正分支後可取得住院／急診卡號；目前門診來源仍空白，詳見 docs/VISITS.md。
- 就診清單可能含同病人的舊病歷號。PRQ Adapter 必須先確認 QueryPatientRecord 的病人 context；只有啟用的 KSCase 明細列可作為歷史號碼，未歸屬清單的連結、格式不合法號碼與明示衝突的身分證仍拒絕。VisitCase.mrn 保留明細來源病歷號供 SOAP／醫囑查詢，lookup_mrn／patient_mrn 保留此次清單的查詢病歷號供跨次就診及門診掛號組合。純 parse_visit_cases 預設仍嚴格。0.20.1 原始 ZIP 的已確認舊號案例可只讀重解析為 38 筆；此事不等於新版已在內網完成舊號 SOAP 查詢。
- 就診 KSCase 的互斥分支先以 iter_active_constructor_calls 靜態選擇，才解析及去重。所有建構式（含未啟用分支）均須從 legacy-link fallback 遮蔽；不可直接合併重複列的醫師資料或選第一個分支。未知條件報錯，不 eval JavaScript。
- 就診醫師姓名取自清單 KSCase 的醫師欄；卡號僅保留實際回傳 vsNo。VisitFilter 可選 O／A／E，預設 O；住院／急診清單不代表其 SOAP 或醫囑端點已支援。
- 歷次就診以醫師姓名篩選；personnel.get_by_card 先精確核對員工編號，再由呼叫端將 name 交給 VisitFilter。醫師章號與員工編號不可混用；四碼帳號 + F 是已知別名，其他後綴不截短。同名仍不能僅靠就診姓名確定身分。
- DDPortal 表單為 Big5，結果可為 UTF-8；personnel.search 僅送 showAllDoctors，不提交回傳頁的簡訊表單、不執行 JS。職稱／單位由當次表單讀取；323 筆清單已 HAR 離線驗證，0.19.3 內網已驗證選項、卡號、姓名、員工編號與職稱組合查詢。明細未錄製，見 docs/PERSONNEL.md。
- DDPortal 的查詢容器可用 frame 或 iframe；兩者都需比對允許來源與完整 DRQuerySql.jsp 路徑，0.19.3 已有內網成功證據。表單標籤可含「代碼 - 名稱」，測試器只剝除與該 option.value 相同的前綴且須唯一匹配；模型保留原標籤。單位及下層單位仍未內網實測，不把本機修正當成已送出查詢。
- 空結果與未知 schema 不同。未執行醫囑、查無 JPG、只有 PDF 參照各自保留狀態。不得以 HTTP 200 判定登入或正文成功。
- SOAP 使用 parsing/soap.py 依明確標籤及同表 rowspan 分段，A+P 不硬拆 A/P；blocks／full_text 保持相容。diagnoses 只取 ICD 區，不從自由文字推論診斷或主次。SoapOrder／SoapMedication 是頁面摘要，無執行狀態或附件參照；不可冒充 ClinicalOrder／MedicationOrder。藥囑表可能在連續處方說明後的第二行，需保留前置說明、從完整欄位標題解析；明示的「服藥期限」回 SoapChronicPrescriptionPeriod，原文仍保留。未知列保留原文及 parsing_issues，None 表示未辨識段落，空字串表示有標籤但無內容。0.20.0 第二份 SOAP 專項內網回傳驗證四筆 SOAP 與一筆慢性處方期限；當時兩份就診清單因異號阻擋。0.20.3 院內回傳另驗證一名已確認同病人之舊病歷號門診 SOAP，其他人的舊號關係尚待確認。詳見 docs/SOAP.md 與 docs/VALIDATION.md。
- NumericTable.headers 是相容的攤平表頭，不可直接 zip(rows)；新功能使用 header_rows 與逐欄 column_paths。眼科表格可有兩層表頭、rowspan 及不合實際欄數的 colspan；能對齊才產生 column_paths，無法對齊保留原始 rows 並填 parsing_issues。手填 `error`、空白格、帶括號或說明的值依 HTML 儲存格位置保留，不靠數字猜左右眼。0.20.4 只將三欄日期／OD／OS 完全對齊的過大 colspan 計為測試警示，來源問題碼仍保留；其他不確定欄位仍是 ERROR。0.20.4 院內 EXE 回傳 `OK`，舊、新 ZIP 的就診、SOAP 及數值解析內容完全一致，警示數 2、解析錯誤數 0。長期間檢驗表的 else-if 異常值分支必須只輸出一個資料格，不能因靜態解析重複值；不執行任意 JS，數值、單位及左右眼不做臨床推論。詳見 docs/NUMERIC_REPORTS.md。
- 手術排程 status 優先讀 ornstats，備援 orstatus；來源值可為未解碼的代碼，不能宣稱已完成手術。網頁顯示房間使用 oproom，oroproom 為另存的來源代碼；optime 的 TF／TF 加數字是未定時間，不能把 orbgntm 的 23:59:00 或月曆定位 08:30 當成已確定時間。SurgeryRecord.source_fields 保留完整原始列（含巢狀病人個資及院內識別資訊），extra 保持舊版未選欄位語義；兩者不可公開，詳見 docs/SURGERY_SCHEDULE.md。
- 門診歸屬依回應醫師欄與已確認的 F 後綴規則；70／71／V1 只是科別。掛號與當日實際就診要分開。
- Review VerifyCode 是審查結果；ApplyStatus、ApplyFinishFlag 不是核准狀態。
- MIS HTML 可能含巢狀導覽表，必須保留主資料表自身的列；不要只保留最內層 table。
- PDF／JPG 下載成功不等於已解析醫療數據；沒有 OCR 時要明說。
- 異動必須經 SurgeryCommand，測試 EXE 排除所有異動。未知異動結果先讀回，不重送。

## 以 HAR 新增功能

完整流程見 docs/HAR_RECORDING.md。依序檢查錄製範圍／缺少 body、抽出請求形狀與動態相依、設計模型及純 Parser、接 Adapter／Service，再登錄操作與測試。

`core/operations.py` 描述底層 request contract；`queries.py` 描述使用者可呼叫的唯讀結果。兩者不是一對一，也不能只登錄其中一處便宣告功能完成。

同一原子操作的替代輸入用 QuerySpec.alternative_inputs 描述；例如 prq.visit_cases 接受 mrn 或 national_id，兩者不能同時傳入。不為同一份結果重複增加查詢 ID。

`queries.dependencies` 用於測試器發現參照；`live/atomic.py` 的 `_query_inputs` 必須能以真實回應產生輸入。無候選用 NO_SAMPLE，登入／上游失敗用 BLOCKED，缺必要設定用 MISSING。

新增純解析合成測試、mock transport 測試；必要時補 localhost EXE 測試。HAR 可用於本機私有回歸，公開 fixtures 必須重新製作合成資料，不能只刪 Cookie 就提交。

新增解析器同時接 `offline/replay.py`；不要以固定 HAR 的非空資料假設套用到所有 live 回應。重解析成功與原始實測狀態分別保留。

## 驗證指令

```sh
python run_tests.py
python -m ruff check .
python tools/generate_api_reference.py --check
python tools/check_public_tree.py
python -m build --outdir output/package
```

先跑修改涉及的 unittest 模組，再跑完整 suite。API 文件修改用途字典後用 generator 更新。私有回歸需明確設定 `VGHKS_RUN_HAR_CONTRACT=1` 且本機有 data；它只讀檔，不把舊請求重送到醫院。

Windows EXE：Python 3.10 x64、PyInstaller 6.14.2、truststore 0.10.4，使用 `tools/build_live_test_exe.py`。公開版不加 `--defaults`；自用版可用 `--defaults private/live-test-defaults.json`。該 JSON 只接受 test_mrn，不能含帳密。程式啟動時 CLI／JSON／環境參數高於內嵌預設。

EXE 修改後跑 tools/verify_*_exe.py，各工具只對 localhost 發合成請求。不能將這些成功當成內網實測成功。

結構化 SOAP 複驗用 `--default-profile soap` 建置、`tools/verify_soap_exe.py` 驗證；live/soap.py 從指定日期門診清單選不同病歷號，只查回傳醫師匹配登入卡號／加 F 的專屬清單，逐人保存完整就診清單、同日門診比對與 SOAP。預設日期 2026-09-21、最多八人、每人兩筆就診；缺樣本為 NO_SAMPLE，單筆失敗仍續跑，登入失敗停止相依查詢。此計畫不測錯誤密碼或異動，localhost 證據不視為院內資料驗證。

就診搜尋用 `--default-profile visits` 建置、`tools/verify_visit_exe.py` 驗證；build metadata 決定零參數啟動範圍。測試流程在 live/visits.py，保留 MRN／身分證差異、篩選結果、NO_SAMPLE 及 fallback 來源，不能因 MRN 成功便宣稱身分證已成功。

登入專項測試用 `--default-profile login` 建置、`tools/verify_login_exe.py` 驗證，不需 private defaults。live/login.py 是測試 SDK 的應用層，live/login_simulation.py 使用無 socket 的合成 adapter。使用者授權一般帳號每輪最多兩次刻意錯誤密碼，必須在正常登入／查詢後執行；一次獨立 Session 最多一個 password POST，未知結果停止後續負向測試。禁止將模擬或清 Cookie 當成院內自然 TTL 過期證據。預期拒絕只能按通過的明確負向步驟及 capture 範圍從離線錯誤分類中分開，不可忽略所有登入失敗。

0.19.3 內網已驗證明確與按需登入的兩次預期拒絕，以及清 Cookie 後 PRQ 查詢自動重新登入成功。沒有新登入缺陷時，不為補單位篩選而重跑錯誤密碼；需重用 login 計畫時可設 `--login-negative-attempts 0`。

離線分析的 no_sample_steps 保留沒有對應 QuerySpec 的欄位檢查；只有缺樣本時 analysis_status 為 COMPLETED_WITH_GAPS、CLI exit code 0。不能把缺樣本列為 root_cause，也不能用它掩蓋實際 HTTP／解析錯誤。

自動 TLS／網路重試的 capture 以 request_group_id 配對，只有同群組後續收到 HTTP 才將先前網路錯誤標 recovered；後續 HTTP／Parser 錯誤仍列出。connection_probe 匿名回應不交給業務 Parser。test_auto_tls.py 用真實 localhost TLS 握手驗證相容、升級、憑證、Cookie 及 POST 不重送。

## 文件與清理

- README 保持簡短。完整用途放 API_REFERENCE，架構放 ARCHITECTURE，新增功能流程放 HAR_RECORDING／DEVELOPMENT。
- 更新 VALIDATION 的驗證層級，區分合成、HAR、localhost EXE、內網回傳；不複製個別病人與薪資內容。
- 不為整理而移除仍被公開匯入、CLI 或測試使用的相容層。原始 HAR／return 是不可再生的證據，與可重建 cache／舊 wheel／EXE 分開。
- Windows 移除／搬移前驗證完整路徑在 workspace 內，使用原生 LiteralPath；不可跨 shell 組字串刪除。

## 發布

公開內容僅含來源、文件、範例、合成測試與工具。`check_public_tree.py --staged` 讀取 Git index 的實際內容；若有本機 denylist 可加 `--denylist private/publish-denylist.json`，命中只列檔名／規則，不印值。

建立 wheel／sdist 後用 `--archive` 檢查實際包內容，再從獨立暫存目錄安裝驗證 import／CLI。不要發布自用 EXE、private defaults、HAR、return、衍生報告、帳密或 Cookie。

只有使用者授權的發布動作才推送。已明確授權就完成本機檢查後執行，不需額外推導一輪確認。推送前核對 remote owner／repo／visibility；不得 force push 或把資料先推上去再刪除。
