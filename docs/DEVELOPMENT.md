# 開發與驗證

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
python tools/check_public_tree.py --archive output/package/vghks_sdk-0.18.0-py3-none-any.whl
```

Windows EXE 使用 Python 3.10 x64、PyInstaller 6.14.2、truststore 0.10.4：

```sh
python -m pip install -e ".[build]"
python tools/build_live_test_exe.py
# 自用 EXE 內嵌本機病歷號；此檔案及此 EXE 不公開。
python tools/build_live_test_exe.py --defaults private/live-test-defaults.json
# 本次新增功能專用版，雙擊即進入 visits 計畫。
python tools/build_live_test_exe.py --defaults private/live-test-defaults.json --default-profile visits
```

private defaults 僅接受 `{"test_mrn": "已獲授權的病歷號"}`，不接受帳密。公開版本在啟動時詢問 MRN 或接受 CLI／環境參數。兩種版本都可只搬 EXE。

建置後以 tools/verify_live_test_exe.py、verify_patient_exe.py、verify_ophthalmology_exe.py、verify_surgery_exe.py、verify_review_exe.py 驗證 localhost HTTPS；它們將測試病歷號明確設為合成值。錯誤紀錄在 output，不進 Git。

visits 專用版使用 `python tools/verify_visit_exe.py`，先驗證一般 SDK Service 不手動設定 TLS 也能登入／查詢，再驗證實際 EXE 的相容模式優先、零參數啟動、舊設定檔忽略、自動／手動身分證、部分錯誤續跑、空結果及 ZIP 同目錄輸出。所有請求只發到 localhost；測試器沒有取代尚待取得的內網證據。

`tests/test_auto_tls.py` 以 localhost 真實 TLS 交握驗證舊 AES 相容、TLS 1.3、憑證備援／嚴格模式、匿名探測、Cookie 保留及 POST 不重送。合成伺服器不使用醫院域名或資料；網路切換也須接離線重解析，避免將已恢復的中途失敗誤判為未解錯誤。

發布流程見 [DISTRIBUTION](DISTRIBUTION.md)，AI 接手的完整規則見 [AGENTS](../AGENTS.md)。
