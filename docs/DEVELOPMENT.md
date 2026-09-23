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

## 建置

```sh
python -m build --outdir output/package
python tools/check_public_tree.py --archive output/package/vghks_sdk-0.19.4-py3-none-any.whl
python tools/check_public_tree.py --archive output/package/vghks_sdk-0.19.4.tar.gz
```

Windows EXE 使用 Python 3.10 x64、PyInstaller 6.14.2、truststore 0.10.4：

```sh
python -m pip install -e ".[build]"
python tools/build_live_test_exe.py
# 自用 EXE 內嵌本機病歷號；此檔案及此 EXE 不公開。
python tools/build_live_test_exe.py --defaults private/live-test-defaults.json
# 就診搜尋專用版，雙擊即進入 visits 計畫。
python tools/build_live_test_exe.py --defaults private/live-test-defaults.json --default-profile visits
# 登入專項版，不需 private defaults 或病人參數。
python tools/build_live_test_exe.py --default-profile login
```

private defaults 僅接受 `{"test_mrn": "已獲授權的病歷號"}`，不接受帳密。公開版本在啟動時詢問 MRN 或接受 CLI／環境參數。兩種版本都可只搬 EXE。

建置後以 tools/verify_live_test_exe.py、verify_patient_exe.py、verify_ophthalmology_exe.py、verify_surgery_exe.py、verify_review_exe.py 驗證 localhost HTTPS；它們將測試病歷號明確設為合成值。錯誤紀錄在 output，不進 Git。

visits 專用版使用 `python tools/verify_visit_exe.py`，先驗證一般 SDK Service 不手動設定 TLS 也能登入／查詢，再驗證實際 EXE 的相容模式優先、零參數啟動、舊設定檔忽略、自動／手動身分證、部分錯誤續跑、空結果及 ZIP 同目錄輸出。所有請求只發到 localhost；測試器沒有取代尚待取得的內網證據。

`tests/test_auto_tls.py` 以 localhost 真實 TLS 交握驗證舊 AES 相容、TLS 1.3、憑證備援／嚴格模式、匿名探測、Cookie 保留及 POST 不重送。合成伺服器不使用醫院域名或資料；網路切換也須接離線重解析，避免將已恢復的中途失敗誤判為未解錯誤。

登入版以 `python tools/verify_login_exe.py` 驗證當前原始碼建置的 EXE；`--source` 可先驗證原始碼。七種 HTTPS localhost 情境涵蓋過期 302／401、Cookie 清除未觸發過期、人事錯誤續跑、初始登入拒絕、負向未知回應及負向轉址；人事用 iframe 及含代碼前綴的選項，錯誤密碼用文字拒絕頁。核對伺服器實際收到的密碼 POST 次數、20 個模擬案例、零參數啟動及 ZIP／離線分析分類。每項失敗保留於 output/login-*.log。舊 EXE 不會因修改原始碼而更新，驗證結果必須記錄其 build_id。

## 引用方式與離線安裝

| 方式 | 適合用途 | 需求 |
| --- | --- | --- |
| Git 來源／固定 commit | 引用 SDK、共同開發、追蹤版本 | Git、Python、相依套件；安裝時建置 |
| wheel | 固定版本部署、搬入內網 | Python 3.10+ 與依賴；不需 Git |
| EXE | 院內測試及帶回 debug | Windows x64；作為獨立程式執行 |

來源 repo 為 [eyeduck-ai/vghks-sdk](https://github.com/eyeduck-ai/vghks-sdk)，MIT 授權，尚未發布到 PyPI。由 Git 安裝時可用 main，固定部署請改用已存在的 commit 或 tag；不要假設每個 SDK 版本都有對應 tag。

```sh
python -m pip install "vghks-sdk @ git+https://github.com/eyeduck-ai/vghks-sdk.git@main"
python -m pip install output/package/vghks_sdk-0.19.4-py3-none-any.whl
```

SDK wheel 為純 Python `py3-none-any`，仍需 requests、beautifulsoup4，以及 Windows 的 truststore。完全離線部署時，在與目標相符的 Python／OS 環境先準備 wheel 及依賴：

```sh
python -m pip download --only-binary=:all: --dest wheelhouse output/package/vghks_sdk-0.19.4-py3-none-any.whl
python -m pip install --no-index --find-links wheelhouse vghks-sdk==0.19.4
```

## 發布檢查

1. 更新唯一版本來源 `src/vghks_sdk/_version.py`、CHANGELOG 及公開文件；依變更執行測試、ruff 與 API 文件同步檢查。
2. 只 stage 公開來源、文件、合成測試與工具。執行 `python tools/check_public_tree.py --staged` 掃描 Git index；本機有 denylist 時加 `--denylist private/publish-denylist.json`，命中不輸出敏感值。
3. 建置 wheel／sdist，分別用 `--archive` 檢查實際內容，再從獨立暫存工作目錄安裝驗證 import／CLI，避免誤用 src。
4. 核對 remote owner、repo、visibility 及待推送 commit。依使用者授權 commit／push；建立 tag、Release 或上傳套件另依該次發布範圍執行。
5. CI 僅使用合成資料。Windows 與 Linux 的 Python 測試用來檢查可攜性；CI actions 的執行環境不構成 SDK 的 Node.js 或瀏覽器相依。

GitHub main 的原始碼、wheel 與內網使用的 EXE 可以有不同版本；報告要分別記錄 source version 與 EXE build_id。原始碼 push 不會自動更新院內 EXE。

## 本機資料整理

原始 HAR／returns 放 data，私有參數及人工檢閱資料放 private，現行 EXE 放 dist。output 保留最近需要的分析、套件及驗證紀錄即可；舊 wheel、安裝副本、建置目錄與快取可重建後移除。清理前核對完整路徑在 workspace 內；不可為清理而刪除原始 HAR／回傳 ZIP，或仍被公開匯入的相容層。

`.gitignore` 不會移除既有 Git 歷史。若發現敏感資料誤推，依 [SECURITY](../SECURITY.md) 處理；不要只刪最新檔案。完整開發不變量見 [AGENTS](../AGENTS.md)。
