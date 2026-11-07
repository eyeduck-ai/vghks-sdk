# PACS 原生檢閱與 JPEG API

兩部分都包含在 `vghks-sdk` 套件中。`sdk.orders.prepare_pacs_launch()` 取得院方當次啟動資訊；明確匯入的 `vghks_sdk.integrations.smartiris.SmartIrisClient` 負責使用者工作站的 DownloadApp HTTP 交接。其他桌面應用可直接共用。

## 入口與相容性

| 用途 | 推薦 API | 相容入口 |
| --- | --- | --- |
| 列出一份檢查的 JPEG 圖片參照 | `orders.get_pacs_jpeg_study(ref)` | `get_pacs_study(ref)` |
| 下載一張 JPEG | `orders.download_pacs_jpeg(ref)` | `download_pacs_image(ref)` |
| 取得原生 PACS 啟動資訊 | `orders.prepare_pacs_launch(ref)` | 新功能，需 `PacsLaunchRef` |
| 檢查工作站的 DownloadApp 檔案、程序與連接埠持有者 | `SmartIrisClient.check_availability()` | 明確呼叫的唯讀檢查；不送 HTTP 或啟動程序 |
| 向本機 DownloadApp 交接 | `SmartIrisClient.open_study(context)` | 新功能，明確呼叫才執行 |

原有 `PacsStudyRef`、`PacsImageRef`、`PacsStudy` 型別、回傳值、例外與 `prq.pacs_study`／`prq.pacs_image` 查詢 ID 保留。新名稱委派到相同 Adapter，不增加另一組自動採樣。

`ClinicalOrder.jpeg_ref` 是原 `pacs_ref` 的唯讀別名；`OrderDetail`／`OrderReport.jpeg_refs` 是原 `pacs_refs` 的唯讀別名。這些 property 不增加重複 JSON 欄位。原生參照獨立使用 `pacs_launch_ref`／`pacs_launch_refs`，只從已辨識的原生 PACS 按鈕取得。JPEG 參照不自動轉成原生參照。若操作者已明確選定院方檢查，也可以建立 `PacsLaunchRef(mrn, request_no)`；準備階段仍核對回應的病人、檢查與帳號。

## 桌面應用組合

```python
from vghks_sdk.integrations.smartiris import SmartIrisClient

# detail 來自 sdk.orders.get_order_detail(...)，先確認有參照並由使用者選定。
ref = detail.pacs_launch_refs[0]

# 此呼叫須在裝有 DownloadApp／SmartIris 的使用者工作站執行。
with SmartIrisClient() as viewer:
    availability = viewer.check_availability()
    if availability.status != "AVAILABLE":
        raise RuntimeError(f"DownloadApp availability: {availability.status}")
    context = sdk.orders.prepare_pacs_launch(ref)
    result = viewer.open_study(context)

# HANDOFF_SENT 只表示已完成 HTTP 交接，不表示指定影像已載入。
print(result.status)
```

一般 SDK import、建構與原有 JPEG 下載都不會呼叫 DownloadApp。伺服器上的 `127.0.0.1` 是伺服器本身；有後端與桌面端的整合平台，應由使用者工作站的受信任桌面程式執行本機交接，不將本機 HTTP 服務開放到區網。

目前院內證據是同一工作站完成準備與交接。若平台把準備移至另一台後端，需另行驗證院方回傳的 client IP 與工作站關係；不能任意改寫 `PacsLaunchContext` 的病人、帳號、IP 或 Study 欄位來套用其他機器。最直接的整合方式是讓桌面程式引用同一套 SDK，依使用者選擇組合這兩個 API。

## 準備階段

1. 沿用 SDK 的 PRQ 認證及共享 Session；整段持有 operation lock。
2. GET 固定 `qryExternalAP.jsp`，傳入當次登入帳號、病歷號、檢查單號與錄製的 `LoginLevel=RAD`。
3. 靜態解析並核對回應的帳號、病人及檢查，取得單一 Study UID、伺服器代碼與 client IP。
4. GET 固定 `vwrIris.jsp`，核對登入資訊及已錄製 SK WADO 設定，回傳 `PacsLaunchContext`。

這一階段不執行 JavaScript，不建立瀏覽器視窗，不下載 DICOM，也不呼叫 localhost。來源與路徑限於錄製契約；`SDKSettings.smartwonder_base_url` 的另外一種允許值是具有同一路徑的明確 loopback 模擬器，供離線驗證使用。

目前只支援已錄製的 SK／STUDY／單一 UID／一般檢閱模式。缺 UID、多 UID、其他模式、未知頁面或身分不符均回明確錯誤，不猜測第一筆、不啟動其他分支。準備操作的 GET 不自動重送；SmartWonder 的拒絕也不觸發 Portal 密碼重送。

原 HAR 的 SmartWonder 請求具有既存瀏覽器 Cookie。0.24.0 的院內 SDK 回傳已確認：以自身正常 PRQ Session 取得兩份啟動頁、交接一筆指定檢查，操作者確認影像相符；沒有匯入其他瀏覽器 Cookie。此證據僅涵蓋當次帳號／工作站／單一檢查。其他額外認證或未知頁仍須保存原頁並停止。

## 本機交接

可選模組以獨立 HTTP Session 預設連到 `http://127.0.0.1:15516`，不沿用 Portal Cookie／密碼或環境 Proxy。若注入 Session，必須是專供本模組使用的實例；client 會接管其設定及關閉，移除所有既有 adapter（包含更長 URL prefix），確保使用零重試傳輸。每次依序送 `/login`（本次協定的 pwd 為空）、`/wonderurl`、`/setanonymousmode`、`/cmove`。每步等 HTTP 回應，整段有執行緒鎖及 Windows 跨程序鎖，讓採用本模組的多個桌面應用協調使用同一服務；院方原網頁不參與此鎖，無法阻止它同時送出請求。

只有固定端點與經模型驗證的資料可用；巢狀 URL 由參數編碼傳遞。無自動重試、轉址、EXE 啟動或下載安裝。首次使用需既有 DownloadApp 常駐；服務不在時回報失敗。逾時可能表示指令已送達，必須保留結果不明，不以自動重送解決。

`HANDOFF_SENT` 表示四個步驟皆得到 HTTP 200；`viewer_loaded` 保持未知。只有院方明確完成訊息或操作者核對指定影像，才可額外確認載入。0.24.0 院內回傳的四個 body 均為小寫 ASCII `ok`（兩 bytes），但這仍只是交接回應，不是影像載入完成通知。不提供尚無協定證據的 `new_window`／`reuse_window`，也不因同醫囑剛開過而跳過交接。

原網頁的空白瀏覽器視窗是承載啟動 JavaScript 的中介頁。SDK 直接取得並解析啟動資訊，再呼叫本機橋接服務，全程不建立瀏覽器視窗；SmartIris 本身的影像視窗仍由檢閱器管理。院內成功的單筆回傳已驗證此路徑。

## DownloadApp 可用性

`check_availability()` 在 Windows 使用唯讀系統資訊檢查設定的程式路徑、程序及 IPv4 監聽埠持有者，不送 `/login`、`/cmove`、HTTP 探測或 TCP 連線，也不開啟 EXE。預設程式位置為 `C:\TEDPCOCX\DownloadApp.exe`；不同安裝位置可由 `SmartIrisClient(download_app_path=...)` 明確設定本機絕對路徑。

| 欄位 | 意義 |
| --- | --- |
| `installation` | 設定位置的檔案存在、缺少或未知；不是全機安裝清單，也不保證 SmartIris 本體齊全 |
| `process` | 設定路徑的程序運行、未運行或未知 |
| `listener`／`owner` | 指定本機埠是否監聽，以及持有者是否符合設定的程式路徑 |
| `status` | `AVAILABLE`、`UNAVAILABLE`、`UNKNOWN` 或非 Windows 的 `UNSUPPORTED` |

只有符合設定路徑的程序持有該埠時，才可回 `AVAILABLE`；權限不足保留 `UNKNOWN`，其他程序占用該埠保留 `OTHER_PROCESS`，不猜成「未安裝」。這是當下的程序／監聽快照，不是廠商簽章、服務協定健康或影像可載入的證明，檢查後程序仍可能退出。

Windows 介面依據：[GetExtendedTcpTable](https://learn.microsoft.com/en-us/windows/win32/api/iphlpapi/nf-iphlpapi-getextendedtcptable)、[QueryFullProcessImageNameW](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-queryfullprocessimagenamew)。

一般 import／建構不進行檢查，`open_study()` 亦維持原有明確交接契約；桌面平台可自行組合預檢與錯誤提示。0.24.1 院內測試器會在選號後、取得啟動資訊前檢查，非 `AVAILABLE` 即保存狀態並停止。0.24.1 的兩份院內回傳已確認此預檢為 AVAILABLE，後續皆完成交接並由操作者確認影像相符；權限不足、其他持有者與不同安裝位置仍須分別看待。

## 資料與驗證邊界

`PacsLaunchContext` 含敏感病人／帳號／檢查／內網資料，只供當次受信任整合使用；repr 及 SDK `to_jsonable()` 對此型別輸出安全摘要，不能用摘要還原啟動指令。這個例外不代表其他 SDK 型別的 JSON 都已去識別。不要將 `dataclasses.asdict(context)`、原始請求或回應送到一般記錄系統。

模組責任與完整資料流見 [ARCHITECTURE](ARCHITECTURE.md#原生-pacs-與-jpeg-的整合邊界)。

原生準備及交接是明確使用的整合入口，不放入一般 `queries`、readiness 或既有 PACS JPEG 專項的自動遍歷。底層院方請求契約仍登錄於 `core/operations.py`；offline 使用相同純 Parser，僅配對同一 capture operation 或同一 HAR page 的準備回應，缺少配對時保留缺口。

院內驗證程式、結果保存及 source／frozen 驗證方式見 [PACS_LAUNCH_TEST](PACS_LAUNCH_TEST.md)。合成測試、私有 HAR 重解析、localhost 模擬及真正 SDK 院內執行分開記錄於 [VALIDATION](VALIDATION.md)。
