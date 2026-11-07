# vghks-sdk

可組合的 VGHKS 內網 Python SDK，提供病人、門診、SOAP、醫囑、報告、手術、審查及簽到退功能。共用登入、SSO、Session 與錯誤處理，以 Requests 運作。Python 3.10+，MIT 授權。

這是獨立開發的 SDK，並非醫院官方產品。使用者仍需具備內網存取與相應資料查詢權限。

## 安裝與第一個查詢

```sh
python -m pip install "vghks-sdk @ git+https://github.com/eyeduck-ai/vghks-sdk.git@main"
```

固定部署請使用已存在的 tag 或 commit；wheel 與離線安裝見 [DEVELOPMENT](docs/DEVELOPMENT.md)。

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

使用回傳的 `VisitCase`、`OrderReportRef` 等型別參照串接下一步。多步驟收集可使用 `vghks_sdk.workflows`，範例見 [COMPOSITION](docs/COMPOSITION.md)。

## 功能入口

| 入口 | 用途 |
| --- | --- |
| `patients`／`opd` | 基本資料、掛號、醫師每日門診清單與掛號序號 |
| `records` | 就診、SOAP、數值、會診、歷史手術、各科報告、病人旗標 |
| `orders`／`medications` | 單次就診與跨期間醫囑／藥囑、報告、PDF／JPG |
| `surgery`／`reviews` | 手術案例、排程、紀錄及同意書；審查案件、結果與附件清單 |
| `audit`／`earnings`／`personnel`／`attendance` | 未簽病歷、績點與獎金、人事、本人簽到退 |
| `auth`／`queries`／`vghks_sdk.workflows` | 連線檢查、唯讀目錄、多步驟查詢與收集 |
| `vghks_sdk.integrations.smartiris` | 可選工作站模組：檢查 DownloadApp 並交接所選原生 PACS 檢查 |

## 使用重點

- 每個帳號各用一個 SDK。查詢按需登入，同一 Session 依序維護病人 context，預設每次請求節流 0.8–1.8 秒。
- 可辨識的唯讀 Session 過期最多自動恢復一次。嚴格憑證驗證使用 `SDKSettings(allow_unverified_tls=False)`。[連線、TLS 與恢復](docs/CONNECTIONS.md)
- 需要區分空值、未執行、只有附件、部分解析與失敗時，用 `acquire(...)` 或 `sdk.queries.run_result(...)` 取得狀態與原回傳值。[結果與錯誤處理](docs/ACQUISITION.md)
- PRQ 調閱審查會核對當頁後預設提交照護原因 `1A` 一次。手術與簽到退異動由呼叫端明確提交。[就診與調閱審查](docs/VISITS.md#院方要求病歷調閱審查時)

## 文件

| 閱讀需求 | 文件 |
| --- | --- |
| 查 API、欄位與組合範例 | [API 參考](docs/API_REFERENCE.md)、[領域文件索引](docs/ARCHITECTURE.md#領域與來源對照)、[組合範例](docs/COMPOSITION.md) |
| 理解設計與效能 | [架構](docs/ARCHITECTURE.md)、[優化重點與量測](docs/OPTIMIZATION.md) |
| 擴充、測試或建置 | [開發指南](docs/DEVELOPMENT.md)、[HAR 錄製](docs/HAR_RECORDING.md)、[AGENTS](AGENTS.md) |
| 執行院內測試或判讀證據 | [EXE 使用方式](docs/LIVE_TEST.md)、[驗證範圍](docs/VALIDATION.md)、[資料安全](SECURITY.md) |
| PACS 原生開啟、JPEG 命名與桌面整合 | [PACS_LAUNCH](docs/PACS_LAUNCH.md) |

## 開發與測試

clone 後執行 `python -m pip install -e ".[dev]"`，再依修改範圍執行下列檢查。

```sh
python run_tests.py --jobs 2
python -m ruff check .
python tools/generate_api_reference.py --check
python tools/check_public_tree.py
python -m build --outdir output/package
```

一般測試使用合成資料與 localhost，不需要內網、HAR 或帳密。合成、HAR 重解析、localhost EXE 與院內回傳是不同證據；目前範圍及缺口集中於 [VALIDATION](docs/VALIDATION.md)。

## 本機資料與測試 EXE

本機建置的 wheel／sdist 放在 `output/package/`，Windows 測試器放在 `dist/vghks-live-test.exe`。雙擊使用建置時選定的測試計畫，可用 `--plan` 核對。[EXE 操作方式](docs/LIVE_TEST.md)

**結果 ZIP 未加密。** HAR、原始回傳、報告及自用 EXE 僅留本機。原始錄製放 `data/recordings/`，院內回傳放 `data/returns/`。[本機資料整理](docs/DEVELOPMENT.md#本機資料整理)
