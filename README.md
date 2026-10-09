# vghks-sdk

以 Python Requests 組合 VGHKS 內網查詢的 SDK，提供病人、門診、SOAP、醫囑、報告、手術、審查及簽到退功能。共用登入、SSO、Session、節流與錯誤處理，不需要 Playwright。Python 3.10+，MIT 授權。

這是獨立開發的 SDK，並非醫院官方產品。使用者仍需具備內網存取與相應資料查詢權限。

## 安裝與第一個查詢

```sh
python -m pip install "vghks-sdk @ git+https://github.com/eyeduck-ai/vghks-sdk.git@main"
```

固定部署可將 `main` 改為已存在的 tag 或 commit。開發時 clone 後執行 `python -m pip install -e ".[dev]"`。目前尚未發布到 PyPI；wheel 與完全離線安裝見 [DEVELOPMENT](docs/DEVELOPMENT.md)。

```python
import os
from getpass import getpass
from vghks_sdk import PortalCredentials, SDKSettings, VghksSDK, VisitFilter

with VghksSDK(
    settings=SDKSettings(),
    credentials=PortalCredentials(os.environ["VGHKS_USERNAME"], getpass("Portal 密碼：")),
) as sdk:
    mrn = os.environ["VGHKS_MRN"]
    patient = sdk.patients.get_basic_info(mrn)
    visits = sdk.records.get_visit_cases(mrn)
    outpatient = VisitFilter(all_sections=True).select(visits)
    if outpatient:
        soap = sdk.records.get_soap(outpatient[0])
```

每個 Service 功能回傳一份有意義的結果；所需 SSO、token、病人 context 與分頁由 Adapter 管理。使用回傳的 VisitCase、OrderReportRef 等物件串接下一步，不需手動組 HTTP 請求。

SDK 自動處理 HTTPS 相容性：PRQ、SectOrd、WebMAAS 優先使用已驗證的 TLS12_COMPAT，同一主機／埠共用成功設定。連線失敗會依原因有限重試；明確的憑證錯誤預設可對該服務略過驗證，HTTPS 加密仍保留。一般使用不需設定 TLS；要求嚴格驗證時設 `SDKSettings(allow_unverified_tls=False)`。完整行為見 [連線與自動恢復](docs/CONNECTIONS.md)。

一般查詢的 Session 過期最多自動恢復一次。明確 WebMAAS timeout 只重建該頁 SSO，不強制 Portal 登入；登入遭拒以 `LoginRejectedError` 結束。應用程式可依 `SDKError.info.code` 提示更正帳密或檢查連線；[錯誤處理與例外範圍](docs/CONNECTIONS.md#session-過期與登入失敗) 說明恢復範圍及寫入操作的處理。

整合系統可用 `acquire(lambda: sdk.records.get_visit_cases(mrn))` 或 `sdk.queries.run_result(...)` 取得統一結果，區分正常回傳、合法空值、部分解析與失敗。SDK 提供來源狀態、結構化底層原因與安全重試提示；應用決定排程、提示及如何接受缺資料。用法見 [資料獲取與失敗分類](docs/ACQUISITION.md)。

`sdk.auth.password_status` 可讀取已觀察的密碼倒數與強制變更狀態，不發新請求；`PasswordChangeRequiredError` 需由使用者透過院方入口改密碼，再以新帳密建立 SDK。未辨識到通知不代表密碼永不過期，詳見 [登入與密碼狀態](docs/CONNECTIONS.md#session-過期與登入失敗)。

## 功能入口

| 入口 | 用途 |
| --- | --- |
| `patients`／`opd` | 基本資料、掛號、醫師每日門診清單與掛號序號 |
| `records` | 就診、SOAP、數值、會診、歷史手術、各科報告、病人旗標 |
| `orders`／`medications` | 單次就診與跨期間醫囑／藥囑、報告、PDF／JPG |
| `surgery` | 手術案例、紀錄 PDF、排程、同意書與明確的異動命令 |
| `reviews` | 審查登入、案件、審查結果、醫囑、附件／影像清單 |
| `audit`／`earnings` | 未簽病歷、績點及專勤工作獎金 |
| `personnel` | 依姓名／員工編號／職稱／單位查人事，供醫師卡號轉姓名及就診篩選組合 |
| `attendance` | 本人簽到退狀態、日期區間打卡紀錄與明確送出一次簽到退 |
| `auth`／`queries` | 連線檢查、59 項唯讀功能的目錄與動態呼叫 |
| `vghks_sdk.workflows` | 報告收集、門診 SOAP 篩選、手術紀錄收集 |

查詢支援單次就診與指定期間，也可由病歷號或病人身分證取得就診清單再篩選。數值依表頭與儲存格對齊；SOAP 及手術欄位保留來源，不推論臨床意義。未執行醫囑、正文、只有附件與查無圖片各自保留狀態；PDF／JPG 下載不包含 OCR。

院方病歷調閱審查自然出現時，SDK 核對後可自動提交已錄製的照護原因 `1A` 一次。各領域欄位、查詢限制與組合方式集中於下列文件。

## 文件

| 閱讀需求 | 文件 |
| --- | --- |
| 原子功能用途、參數、回傳值 | [API_REFERENCE](docs/API_REFERENCE.md) |
| 自動連線、TLS、重試與狀態 | [CONNECTIONS](docs/CONNECTIONS.md) |
| 資料缺失、部分解析、錯誤原因與重試判斷 | [ACQUISITION](docs/ACQUISITION.md) |
| 組合較複雜的應用 | [COMPOSITION](docs/COMPOSITION.md)、[examples](examples/) |
| 分層與擴充位置 | [ARCHITECTURE](docs/ARCHITECTURE.md) |
| 錄製新 HAR 並新增功能 | [HAR_RECORDING](docs/HAR_RECORDING.md) |
| 測試、建置、發布與離線安裝 | [DEVELOPMENT](docs/DEVELOPMENT.md) |
| 後續 agents 接手 | [AGENTS](AGENTS.md) |
| 內網 EXE 與回傳分析 | [LIVE_TEST](docs/LIVE_TEST.md)、[VALIDATION](docs/VALIDATION.md) |
| 公開資料邊界 | [SECURITY](SECURITY.md) |

領域欄位細節：[簽到退](docs/ATTENDANCE.md)、[人事](docs/PERSONNEL.md)、[就診](docs/VISITS.md)、[病人](docs/PATIENTS.md)、[SOAP](docs/SOAP.md)、[數值](docs/NUMERIC_REPORTS.md)、[掃描病歷](docs/SCANNED_RECORDS.md)、[手術排程](docs/SURGERY_SCHEDULE.md)、[手術案例](docs/SURGERY_CASES.md)、[審查](docs/REVIEWS.md)。

## 開發與測試

```sh
python run_tests.py
python -m ruff check .
python tools/generate_api_reference.py --check
python tools/check_public_tree.py
python -m build --outdir output/package
```

一般測試使用合成資料及 localhost，不需要內網、HAR 或帳密。0.22.4 院內回傳已確認 WebMAAS Cookie 遺失後直接 API 自動 SSO 恢復；強制改密碼、自然 TTL 及未出現的失敗仍保留驗證缺口。各功能實際證據集中於 [VALIDATION](docs/VALIDATION.md)。

離線分析只讀原始回傳，Session 對照、恢復證據與重測設定分別處理；讀取設定不載入院內測試流程。原始 HAR／ZIP 保留，舊套件與可重建暫存的清理方式見 [DEVELOPMENT](docs/DEVELOPMENT.md#本機資料整理)。

SDK 預設循序請求，每次隨機等待 0.8–1.8 秒，使用瀏覽器格式標頭。平行任務應各自建立 SDK／Session，並限制所有工作合計的請求量。

## 本機資料與測試 EXE

公開原始碼不含真實測試病歷號。通用工具使用 `--test-mrn`、`VGHKS_TEST_MRN` 或啟動時輸入；自用 EXE 可在建置時內嵌 `private/live-test-defaults.json`，維持只搬一個 EXE。

結果 ZIP 不加密，存於 EXE 同目錄並含輸出時間，無 `.sha256` 搬移機制。**HAR、returns、raw debug、報告、個人設定及自用 EXE 只留本機，不進 public repo、Issue 或 Actions artifact。**

SDK 是 Python library；EXE 是使用 SDK 的院內測試工具。建置 profile 決定雙擊範圍，原始碼更新不會自動更新既有 EXE。現行 `dbr` 專項以一名授權病人的歷年醫囑抽樣 DBR，取得報告與 PDF／JPG，預設最多八筆；雙擊後輸入病歷號及 Portal 帳密。`password`、`session` 及 `failures` 等計畫仍可明確選用。帶回同目錄新產生的 ZIP，詳見 [LIVE_TEST](docs/LIVE_TEST.md)。

DBR 專項已有院內兩筆醫囑、四份 PDF 的成功回傳；離線分析另核對附件存檔大小、SHA-256 與格式。報告頁只有附件及 JPG 查無資料仍保留來源狀態，下載不包含 OCR。

| 路徑 | 性質 |
| --- | --- |
| `src/`、`tests/`、`docs/`、`examples/`、`configs/`、`tools/` | 公開程式、合成測試、文件 |
| `data/har/`、`data/recordings/`、`data/returns/` | 本機原始證據，Git 忽略 |
| `private/` | 本機建置參數及人工檢閱資料，Git 忽略 |
| `dist/` | 本機現行測試 EXE、待分析的原始回傳 ZIP，Git 忽略 |
| `output/` | 本機分析與驗證紀錄、可重建套件及暫存環境，Git 忽略 |
| `tmp/`、`build/` | 可重建暫存檔，Git 忽略 |
