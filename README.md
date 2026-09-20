# vghks-sdk

以 Python Requests 組合 VGHKS 內網查詢的 SDK，提供病人、門診、SOAP、醫囑、報告、手術及審查功能。共用登入、SSO、Session、節流與錯誤處理，不需要 Playwright。Python 3.10+，MIT 授權。

這是獨立開發的 SDK，並非醫院官方產品。使用者仍需具備內網存取與相應資料查詢權限。

## 安裝與第一個查詢

```sh
python -m pip install "vghks-sdk @ git+https://github.com/eyeduck-ai/vghks-sdk.git@v0.18.0"
```

開發時 clone 後執行 `python -m pip install -e ".[dev]"`。尚未發布到 PyPI；目前由 GitHub 來源或自行建置的 wheel 安裝。

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

## 功能入口

| 入口 | 用途 |
| --- | --- |
| `patients`／`opd` | 基本資料、掛號、醫師每日門診清單 |
| `records` | 就診、SOAP、數值、會診、歷史手術、各科報告、病人旗標 |
| `orders`／`medications` | 單次就診與跨期間醫囑／藥囑、報告、PDF／JPG |
| `surgery` | 手術案例、紀錄 PDF、排程、同意書與明確的異動命令 |
| `reviews` | 審查登入、案件、審查結果、醫囑、附件／影像清單 |
| `audit`／`earnings` | 未簽病歷、績點及專勤工作獎金 |
| `auth`／`queries` | 連線檢查、55 項唯讀功能的目錄與動態呼叫 |
| `vghks_sdk.workflows` | 報告收集、門診 SOAP 篩選、手術紀錄收集 |

**支援單次就診與指定期間兩條路徑**：依 VisitCase 查單次資料，或使用 HistoryFilter 向伺服器查指定期間，不必先下載每次就診再自行篩選。醫囑報告與各科報告亦為獨立入口。

就診清單可由病歷號或 `records.get_visit_cases(national_id=病人身分證)` 取得，再依到院日、類別、科別及醫師組合篩選。兩條路徑及由身分證清單串接門診 SOAP／醫囑已有內網樣本驗證；用法與支援範圍見 [VISITS](docs/VISITS.md)。

未執行醫囑、文字正文、只有 PDF 參照、JPG 按鈕卻查無圖片，均分開處理。PDF／JPG 下載不包含 OCR 或數值擷取。門診清單歸屬依回傳「醫師」欄判斷，不能以科別代碼判斷。

## 文件

| 閱讀需求 | 文件 |
| --- | --- |
| 原子功能用途、參數、回傳值 | [API_REFERENCE](docs/API_REFERENCE.md) |
| 自動連線、TLS、重試與狀態 | [CONNECTIONS](docs/CONNECTIONS.md) |
| 組合較複雜的應用 | [COMPOSITION](docs/COMPOSITION.md)、[examples](examples/) |
| 分層與擴充位置 | [ARCHITECTURE](docs/ARCHITECTURE.md) |
| 錄製新 HAR 並新增功能 | [HAR_RECORDING](docs/HAR_RECORDING.md) |
| 測試、建置、發布 | [DEVELOPMENT](docs/DEVELOPMENT.md)、[DISTRIBUTION](docs/DISTRIBUTION.md) |
| 後續 agents 接手 | [AGENTS](AGENTS.md) |
| 內網 EXE 與回傳分析 | [LIVE_TEST](docs/LIVE_TEST.md)、[VALIDATION](docs/VALIDATION.md) |
| 公開資料邊界 | [SECURITY](SECURITY.md) |

領域欄位細節：[就診搜尋與篩選](docs/VISITS.md)、[病人](docs/PATIENTS.md)、[手術](docs/SURGERY_CASES.md)、[審查](docs/REVIEWS.md)。

## 開發與測試

```sh
python run_tests.py
python -m ruff check .
python tools/generate_api_reference.py --check
python tools/check_public_tree.py
python -m build --outdir output/package
```

一般測試僅使用合成資料及 localhost，不需要內網、HAR 或帳密。主要登入、病人、報告附件、手術與審查查詢已有內網成功證據；各功能驗證範圍見 [VALIDATION](docs/VALIDATION.md)。

SDK 預設循序請求，每次隨機等待 0.8–1.8 秒，使用瀏覽器格式標頭。平行任務應各自建立 SDK／Session，並限制所有工作合計的請求量。

## 本機資料與測試 EXE

公開原始碼不含真實測試病歷號。通用工具使用 `--test-mrn`、`VGHKS_TEST_MRN` 或啟動時輸入；自用 EXE 可在建置時內嵌 `private/live-test-defaults.json`，維持只搬一個 EXE。

結果 ZIP 不加密，存於 EXE 同目錄並含輸出時間，無 `.sha256` 搬移機制。**HAR、returns、raw debug、報告、個人設定及自用 EXE 只留本機，不進 public repo、Issue 或 Actions artifact。**

| 路徑 | 性質 |
| --- | --- |
| `src/`、`tests/`、`docs/`、`examples/`、`configs/`、`tools/` | 公開程式、合成測試、文件 |
| `data/har/`、`data/recordings/`、`data/returns/` | 本機原始證據，Git 忽略 |
| `private/` | 本機建置參數及人工檢閱資料，Git 忽略 |
| `dist/` | 本機現行測試 EXE，Git 忽略 |
| `output/`、`tmp/`、`build/` | 可重建產物，Git 忽略 |
