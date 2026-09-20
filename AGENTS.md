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
- 保留預設 0.8–1.8 秒隨機節流。用 local mock 測 request sequencing，不對內網做負載測試。
- TLS12_COMPAT 是特定服務的相容模式，仍有 HTTPS 與憑證驗證；不要全域設定 verify=False。
- VisitCase 與各 Ref 綁定病人／就診。下載僅接受允許來源、路徑與正確病人，不擴成任意 URL 下載器。
- 空結果與未知 schema 不同。未執行醫囑、查無 JPG、只有 PDF 參照各自保留狀態。不得以 HTTP 200 判定登入或正文成功。
- 門診歸屬依回應醫師欄與已確認的 F 後綴規則；70／71／V1 只是科別。掛號與當日實際就診要分開。
- Review VerifyCode 是審查結果；ApplyStatus、ApplyFinishFlag 不是核准狀態。
- MIS HTML 可能含巢狀導覽表，必須保留主資料表自身的列；不要只保留最內層 table。
- PDF／JPG 下載成功不等於已解析醫療數據；沒有 OCR 時要明說。
- 異動必須經 SurgeryCommand，測試 EXE 排除所有異動。未知異動結果先讀回，不重送。

## 以 HAR 新增功能

完整流程見 docs/HAR_RECORDING.md。依序檢查錄製範圍／缺少 body、抽出請求形狀與動態相依、設計模型及純 Parser、接 Adapter／Service，再登錄操作與測試。

`core/operations.py` 描述底層 request contract；`queries.py` 描述使用者可呼叫的唯讀結果。兩者不是一對一，也不能只登錄其中一處便宣告功能完成。

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

## 文件與清理

- README 保持簡短。完整用途放 API_REFERENCE，架構放 ARCHITECTURE，新增功能流程放 HAR_RECORDING／DEVELOPMENT。
- 更新 VALIDATION 的驗證層級，區分合成、HAR、localhost EXE、內網回傳；不複製個別病人與薪資內容。
- 不為整理而移除仍被公開匯入、CLI 或測試使用的相容層。原始 HAR／return 是不可再生的證據，與可重建 cache／舊 wheel／EXE 分開。
- Windows 移除／搬移前驗證完整路徑在 workspace 內，使用原生 LiteralPath；不可跨 shell 組字串刪除。

## 發布

公開內容僅含來源、文件、範例、合成測試與工具。`check_public_tree.py --staged` 讀取 Git index 的實際內容；若有本機 denylist 可加 `--denylist private/publish-denylist.json`，命中只列檔名／規則，不印值。

建立 wheel／sdist 後用 `--archive` 檢查實際包內容，再從獨立暫存目錄安裝驗證 import／CLI。不要發布自用 EXE、private defaults、HAR、return、衍生報告、帳密或 Cookie。

只有使用者授權的發布動作才推送。已明確授權就完成本機檢查後執行，不需額外推導一輪確認。推送前核對 remote owner／repo／visibility；不得 force push 或把資料先推上去再刪除。
