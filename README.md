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
| `surgery` | 手術案例、紀錄 PDF、排程、同意書與明確的異動命令 |
| `reviews` | 審查登入、案件、審查結果、醫囑、附件／影像清單 |
| `audit`／`earnings` | 未簽病歷、績點及專勤工作獎金 |
| `personnel` | 依姓名／員工編號／職稱／單位查人事，供醫師卡號轉姓名及就診篩選組合 |
| `attendance` | 本人簽到退狀態、日期區間打卡紀錄與明確送出一次簽到退 |
| `auth`／`queries` | 登入與連線檢查、唯讀功能目錄與動態呼叫 |
| `vghks_sdk.workflows` | 報告收集、門診 SOAP 篩選、手術紀錄收集 |

## 使用重點

- 查詢按需登入；可恢復的 Session 過期最多自動恢復一次。登入遭拒、強制改密碼與異動不套用一般重播；明確 WebMAAS timeout 只重建該頁 SSO。
- 同一 SDK 共用 Session 與病人 context，操作循序執行，預設每次請求隨機等待 0.8–1.8 秒。不同帳號各用獨立 SDK，並限制合計請求量。
- TLS 自動選擇相容設定；明確憑證錯誤預設可對該來源略過驗證。嚴格模式使用 `SDKSettings(allow_unverified_tls=False)`，詳見 [CONNECTIONS](docs/CONNECTIONS.md)。
- Service 保留原回傳型別；需要統一狀態時用 `acquire(...)` 或 `sdk.queries.run_result(...)`，區分空值、部分解析與失敗。排程及後續處置由應用決定，見 [ACQUISITION](docs/ACQUISITION.md)。
- 遇到已錄製的 PRQ 病歷調閱審查頁，核對後預設提交照護原因 `1A` 一次；送出結果不明時不重送。手術與簽到退異動需明確呼叫。
- 數值、SOAP 及手術欄位保留來源語意；未執行、只有附件與查無圖片分開表示。PDF／JPG 下載不包含 OCR。

## 文件

| 閱讀需求 | 文件 |
| --- | --- |
| 原子功能用途、參數、回傳值 | [API_REFERENCE](docs/API_REFERENCE.md) |
| 自動連線、TLS、重試與狀態 | [CONNECTIONS](docs/CONNECTIONS.md) |
| 資料缺失、部分解析、錯誤原因與重試判斷 | [ACQUISITION](docs/ACQUISITION.md) |
| 組合較複雜的應用 | [COMPOSITION](docs/COMPOSITION.md)、[examples](examples/) |
| 整體分層、資料流與擴充位置 | [ARCHITECTURE](docs/ARCHITECTURE.md) |
| 優化順序與驗收條件 | [OPTIMIZATION](docs/OPTIMIZATION.md) |
| 錄製新 HAR 並新增功能 | [HAR_RECORDING](docs/HAR_RECORDING.md) |
| 測試、建置、發布與離線安裝 | [DEVELOPMENT](docs/DEVELOPMENT.md) |
| 後續 agents 接手 | [AGENTS](AGENTS.md) |
| 內網 EXE 與回傳分析 | [LIVE_TEST](docs/LIVE_TEST.md)、[VALIDATION](docs/VALIDATION.md) |
| 公開資料邊界 | [SECURITY](SECURITY.md) |

領域欄位細節：[簽到退](docs/ATTENDANCE.md)、[人事](docs/PERSONNEL.md)、[就診](docs/VISITS.md)、[病人](docs/PATIENTS.md)、[SOAP](docs/SOAP.md)、[數值](docs/NUMERIC_REPORTS.md)、[掃描病歷](docs/SCANNED_RECORDS.md)、[手術排程](docs/SURGERY_SCHEDULE.md)、[手術案例](docs/SURGERY_CASES.md)、[審查](docs/REVIEWS.md)。

## 開發與測試

clone 後執行 `python -m pip install -e ".[dev]"`，再依修改範圍驗證：

```sh
python run_tests.py
python -m ruff check .
python tools/generate_api_reference.py --check
python tools/check_public_tree.py
python -m build --outdir output/package
```

一般測試使用合成資料與 localhost，不需要內網、HAR 或帳密。合成、HAR 重解析、localhost EXE 與院內回傳是不同證據；目前範圍及缺口集中於 [VALIDATION](docs/VALIDATION.md)。

## 本機資料與測試 EXE

EXE 是使用 SDK 的院內測試工具；批次掛號、PACS、DBR、登入與 Session 等計畫、輸入方式及讀取上限見 [LIVE_TEST](docs/LIVE_TEST.md)。公開工具不內嵌真實病歷號；自用建置參數不得含帳密。

`data/`、`private/`、`dist/`、`output/` 是本機資料，`tmp/`、`build/` 是暫存。**結果 ZIP 未加密；HAR、原始回傳、報告及自用 EXE 只留本機，不進公開 repo、Issue 或 Actions artifact。** 原始證據與可重建檔案的保留方式見 [本機資料整理](docs/DEVELOPMENT.md#本機資料整理)。
