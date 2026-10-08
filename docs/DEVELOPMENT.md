# 開發、建置與發布

Python 3.10+。clone 後 `python -m pip install -e ".[dev]"`；使用 unittest。一般安裝只在 Windows 引入 truststore；dev 額外在其他平台安裝它，讓模擬 Windows 信任庫的測試也能執行。

```sh
python run_tests.py
python -m ruff check .
python tools/generate_api_reference.py --check
python tools/check_public_tree.py
```

指定模組可用 `python -m unittest discover -s tests -p test_review.py -v`（先安裝 editable package）。測試只使用合成回應／localhost；真實 HAR 和 ZIP 並非必要依賴。

## 新增或修改一項功能

1. 在 models 定義輸入、回傳與參照；保留伺服器原始欄位，不猜業務意義。
2. 純 Parser 對已知成功、空結果、登入頁、未知 schema、缺欄位寫必要的合成測試。
3. 在 core/operations.py 登錄 request contract；Adapter 透過 Runtime 發送，不直接另開 requests.Session。
4. Service 與 Protocol 暴露穩定介面；需要新領域時由 sdk.py 組裝並匯出型別。
5. 唯讀結果加入 queries.py；live/atomic.py 添加 discovery input；新 protocol／schema 也接入 offline/replay.py。
6. 更新 `tools/generate_api_reference.py` 用途字典後重新產生 API_REFERENCE；補適當組合範例。
7. 本機測試、私有證據重解析、localhost EXE 檢查通過後，才以內網回傳確認實際可用。

不要為細小改動新增抽象框架；已驗證的分層可直接擴充。CLI／公開 Service 有相容性需求，變更參數、回傳與狀態時同步測試及 CHANGELOG。

## 私有證據

```sh
python run_sdk.py replay-har --input data/recordings --output output/har-replay.json
python run_sdk.py analyze-bundle --input data/returns/return.zip --output output/latest-analysis
```

`replay-har` 用現行 Parser 重讀 bytes；`har-check` 是較早基準 HAR 的嚴格 contract 檢查，新功能以 replay 及專屬 tests 驗證。先看 `python run_sdk.py --help` 與子指令 `--help`。

設定 `VGHKS_RUN_HAR_CONTRACT=1` 後執行 tests，可加入本機可用的 HAR／歷史回傳回歸；沒有檔案的項目會跳過。不得對外回報「跳過」為成功驗證。

離線分析不修改原始測試結果；要同時看 recorded_status、live_status、重解析結果、report_data_status。合法空結果不能當成功取得正文，HTTP 200 也可能只是登入／viewer 頁。

醫囑附件以 `order_assets` 核對 ZIP 內實際存檔與下載 metadata，包含大小、SHA-256 及格式。原始 HTTP replay 與存檔核對是不同證據；存檔問題不能改寫原成功步驟，也不能因原成功便忽略缺檔。組合 workflow 重用 runner 已寫好的結果，自訂 runner 僅回傳值時仍會保存；PDF／JPG 走 `local_io.write_bytes_atomic`，存檔失敗保留 OUTPUT_WRITE_FAILED 並續跑其他附件。

附件目錄或支援格式的調整集中於 `contracts/order_assets.py`，由 live、workflow 與 offline 共用；不要讓離線分析匯入測試執行器以取得路徑。重構後對同一 ZIP 做禁止 socket 的完整分析與重測設定比較，再跑對應 unittest／localhost EXE，確認只改責任分工而未改證據判定。詳細分層見 [ARCHITECTURE](ARCHITECTURE.md)。

## 建置

下列 `VERSION` 為檔名佔位，請替換為 `src/vghks_sdk/_version.py` 的版本；wheel／sdist 使用同一來源，文件不另維護套件版本。

```sh
python -m build --outdir output/package
python tools/check_public_tree.py --archive output/package/vghks_sdk-VERSION-py3-none-any.whl
python tools/check_public_tree.py --archive output/package/vghks_sdk-VERSION.tar.gz
```

若本機尚未安裝 `build`，先安裝 `.[dev]`；建置環境也須有 `wheel`。專案的 PyInstaller 暫存目錄也叫 `build/`，在 Python 將它誤判為模組時，改從 `output/` 目錄執行 `python -m build .. --outdir package`。產生封包後逐一檢查實際內容。

建置後端最低 setuptools 77.0.0，以 `license = "MIT"` 及 `license-files` 明確保存授權，移除舊 license table 的棄用警告；格式見 [setuptools 官方設定文件](https://setuptools.pypa.io/en/latest/userguide/pyproject_config.html)。這是建置需求，不增加 SDK 執行時相依。

Windows EXE 使用 Python 3.10 x64、PyInstaller 6.14.2、truststore 0.10.4：

```sh
python -m pip install -e ".[build]"
python tools/build_live_test_exe.py --default-profile failures
python tools/verify_failure_exe.py
# 自用版可內嵌授權病歷號；此檔案與 EXE 不公開。
python tools/build_live_test_exe.py --default-profile failures --defaults private/live-test-defaults.json
```

private defaults 僅接受 `{"test_mrn": "已獲授權的病歷號"}`，不接受帳密。公開版本在啟動時詢問 MRN 或接受 CLI／環境參數。兩種版本都可只搬 EXE。

建置其他計畫時替換 `--default-profile`，依相關改動選擇驗證工具：

| profile | localhost 驗證工具 | 主要檢查 |
| --- | --- | --- |
| `dbr` | `tools/verify_dbr_exe.py` | 十種 HTTPS 情境、歷年 DBR 抽樣、二進位／登入分類、原始 PDF／JPG 保存、有限恢復與 CLI 離線分析 |
| `password` | `tools/verify_password_exe.py` | 十五種 HTTPS 情境、舊密碼只送一次、強制變更原頁保存、匿名／Cookie 目錄對照及離線白名單 |
| `session` | `tools/verify_session_exe.py` | 十二種 HTTPS 情境、表單／token、隔離 Cookie、明確 timeout 的 GET／唯讀 POST 自動 SSO 恢復及持續失效停止 |
| `failures` | `tools/verify_failure_exe.py` | 68 個無 socket 模擬、23 種 HTTPS 情境、未登入與錯誤密碼計數、通知、資料狀態及離線分類 |
| `login` | `tools/verify_login_exe.py` | 登入／SSO、人事條件、Cookie 恢復及有限負向測試 |
| `visits` | `tools/verify_visit_exe.py` | 病歷號／身分證、篩選、fallback 與部分失敗續跑 |
| `soap` | `tools/verify_soap_exe.py` | 多病人抽樣、結構化 SOAP 與慢性處方期限 |
| `scans` | `tools/verify_scan_exe.py` | 單次及歷年掃描、病歷類別、PDF 抽樣 |
| `regression` | `tools/verify_regression_exe.py` | 獨立帳號掛號比較、SSO token、審查送出一次及 SOAP 接續 |
| `comprehensive`／其他領域 | `tools/verify_live_test_exe.py`、`verify_patient_exe.py`、`verify_ophthalmology_exe.py`、`verify_surgery_exe.py`、`verify_review_exe.py` | 完整流程及所修改領域 |

工具只對 localhost 發合成請求，紀錄留在 output；不代表院內通過。`--source` 可預先檢查原始碼，不能取代新建置 frozen EXE。零參數啟動、build_id、實際密碼 POST 次序／次數、讀取上限與 ZIP 離線分析均需核對。failures 的一次錯誤密碼在正常登入前，未知結果停止；一般 SDK 不做負向測試。實際操作與採樣上限見 [LIVE_TEST](LIVE_TEST.md)，院內證據見 [VALIDATION](VALIDATION.md)。

`tests/test_auto_tls.py` 以 localhost 真實 TLS 交握驗證舊 AES 相容、TLS 1.3、憑證備援／嚴格模式、匿名探測、Cookie 保留及 POST 不重送。合成伺服器不使用醫院域名或資料；網路切換也須接離線重解析，避免將已恢復的中途失敗誤判為未解錯誤。

## 引用方式與離線安裝

| 方式 | 適合用途 | 需求 |
| --- | --- | --- |
| Git 來源／固定 commit | 引用 SDK、共同開發、追蹤版本 | Git、Python、相依套件；安裝時建置 |
| wheel | 固定版本部署、搬入內網 | Python 3.10+ 與依賴；不需 Git |
| EXE | 院內測試及帶回 debug | Windows x64；作為獨立程式執行 |

來源 repo 為 [eyeduck-ai/vghks-sdk](https://github.com/eyeduck-ai/vghks-sdk)，MIT 授權，尚未發布到 PyPI。由 Git 安裝時可用 main，固定部署請改用已存在的 commit 或 tag；不要假設每個 SDK 版本都有對應 tag。

```sh
python -m pip install "vghks-sdk @ git+https://github.com/eyeduck-ai/vghks-sdk.git@main"
python -m pip install output/package/vghks_sdk-VERSION-py3-none-any.whl
```

SDK wheel 為純 Python `py3-none-any`，仍需 requests、beautifulsoup4，以及 Windows 的 truststore。完全離線部署時，在與目標相符的 Python／OS 環境先準備 wheel 及依賴：

```sh
python -m pip download --only-binary=:all: --dest wheelhouse output/package/vghks_sdk-VERSION-py3-none-any.whl
python -m pip install --no-index --find-links wheelhouse vghks-sdk==VERSION
```

## 發布檢查

1. 更新唯一版本來源 `src/vghks_sdk/_version.py`、CHANGELOG 及公開文件；依變更執行測試、ruff 與 API 文件同步檢查。
2. 只 stage 公開來源、文件、合成測試與工具。執行 `python tools/check_public_tree.py --staged` 掃描 Git index；本機有 denylist 時加 `--denylist private/publish-denylist.json`，命中不輸出敏感值。
3. 建置 wheel／sdist，分別用 `--archive` 檢查實際內容，再從獨立暫存工作目錄安裝驗證 import／CLI，避免誤用 src。
4. 核對 remote owner、repo、visibility 及待推送 commit。依使用者授權 commit／push；建立 tag、Release 或上傳套件另依該次發布範圍執行。
5. CI 僅使用合成資料。Windows 與 Linux 的 Python 測試用來檢查可攜性；CI actions 的執行環境不構成 SDK 的 Node.js 或瀏覽器相依。

GitHub main 的原始碼、wheel 與內網使用的 EXE 可以有不同版本；報告要分別記錄 source version 與 EXE build_id。原始碼 push 不會自動更新院內 EXE。

## 本機資料整理

| 路徑／類型 | 保留與清理方式 |
| --- | --- |
| `data/`、`dist/` 的原始 HAR／回傳 ZIP | 不可再生的證據，保留原檔；離線分析不得改寫 |
| `private/` | 保留人工檢閱、denylist 及建置參數，不公開 |
| `output/` 的院內分析與私有回歸結果 | 保留仍支撐驗證結論的紀錄；原始回傳已不在本機時，不因舊版本而刪除剩餘證據 |
| `dist/vghks-live-test.exe`、`output/build-info.json` | 保留現行 EXE 與其建置資訊，更新後完成 localhost 驗證 |
| `output/package/`、驗證報告 | 保留目前版本套件及近期檢查結果，舊套件、重複 log 與安裝副本可移除 |
| `build/`、`__pycache__/`、`.ruff_cache/` | 可重建快取；建置相依環境需核對無其他用途後才清除 |

每次整理先記錄清理清單、檔案數與大小，再核對用途。同一版本驗證的逐情境 log 可在總結報告已通過後移除；保留近期完整測試、EXE 驗證總結與建置資訊。暫存安裝副本及舊 wheel／sdist 可重建，原始 HAR／ZIP 和失去原檔後的分析證據不能用版本新舊判斷是否多餘。重構離線分析時，應以同一份 ZIP 禁止 socket 重解析，比較完整報告（排除 analyzer build）及重測設定，避免整理改變結論或範圍。

Windows 清理前解析完整目標路徑，確認仍在 workspace 內；使用 `Remove-Item -LiteralPath`，不跨 shell 組字串刪除。仍被公開匯入、CLI 或測試使用的相容層保留。共用 JSON 原子寫入及本機權限處理由 `local_io.py` 維護。

`.gitignore` 不會移除既有 Git 歷史。若發現敏感資料誤推，依 [SECURITY](../SECURITY.md) 處理；不要只刪最新檔案。完整開發不變量見 [AGENTS](../AGENTS.md)。
