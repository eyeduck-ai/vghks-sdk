# 簽到退與打卡紀錄

`sdk.attendance` 操作目前 Portal 登入者自己的 PSPDPortal 簽到退功能。狀態、日期查詢及明確送出簽到退各是一個 Service 操作，必要 SSO 與表單核對由 Adapter 管理。

## 使用方式

以下沿用已建立的 `sdk`，日期使用 Python `date`：

```python
from datetime import date

state = sdk.attendance.get_status()
history = sdk.attendance.get_records(
    date(2026, 1, 1), date(2026, 1, 31), mode="processed"
)
raw_history = sdk.attendance.get_records(
    date(2026, 1, 1), date(2026, 1, 31), mode="raw"
)
for record in history.records:
    print(record.occurred_at, record.description, record.location_code)
```

應用程式在使用者明確觸發簽到退時，才呼叫：

```python
receipt = sdk.attendance.punch()
print(receipt.status, receipt.record.occurred_at)
```

`punch()` 不接受日期、時間、地點、員工編號或簽到／簽退方向。錄製表單只有 `reqCode=setPCClockInLog`，來源也是「簽到退」共用按鈕。SDK 保存來源事件，不以奇偶筆數、時間或列順序推論上班／下班、工時或出勤核准。

## 電腦位置的來源

第一份 HAR 的簽到退 POST 只送 `reqCode=setPCClockInLog`；URL、表單及錄製的自訂標頭沒有位置、電腦序號或本機 IP 欄位。成功回應才提供 `地點:<代碼>`，紀錄表提供 `電腦:<代碼>`。送出前的狀態頁已提供上次事件地點與電腦序號。

因此，這份證據確認瀏覽器沒有在打卡表單中直接指定位置，位置由院方系統回傳。伺服器是否依連線來源 IP、工作站對照或 Session 中既有資訊判定，HAR 無法證明；HAR 的 `serverIPAddress` 是接收端伺服器地址，也不是用戶端地址。SDK 將來源位置保存為 `location_code`。

## 回傳模型

| 型別／欄位 | 語義 |
| --- | --- |
| `AttendanceState.employee_id`／`employee_name`／`unit_label` | 簽到退狀態頁明示的登入者與單位；Adapter 核對帳號符合 Portal 帳號 |
| `AttendanceState.computer_serial` | 狀態頁的「電腦序號」原值；未證實等同硬體序號或 MAC 地址 |
| `AttendanceState.last_punch_at`／`last_location_code` | 上次簽到退的來源時間與地點；地點不是本次送出位置的預測 |
| `AttendanceRecord.occurred_at` | 原表「日期時間」，精確至分鐘，`datetime` 未附時區 |
| `AttendanceRecord.description`／`location_code` | 完整「說明」；只有完整符合 `電腦:<代碼>` 時解析地點，其他說明保留且地點為 `None` |
| `AttendanceHistory.records`／`total_count` | 原始事件列及頁尾總筆數，兩者必須相符；不因重複 HTML row ID 刪掉事件 |
| `AttendanceHistory.query` | SDK 送出的 `AttendanceQuery`，包含日期與 requested mode |
| `AttendanceHistory.start`／`end` | 回傳查詢表單的日期；Adapter 必須核對與送出日期相符 |
| `AttendanceHistory.reported_mode` | 回傳 radio 明確 checked 的模式；未標示為 `None` |
| `AttendanceHistory.account_context` | Adapter 以同一操作內核對的狀態及共享 Session 綁定帳號；查詢頁只提供姓名，這不是查詢頁回傳的員工編號 |
| `AttendancePunchReceipt` | 明確成功通知與同一回應中的對應事件；`status="ACKNOWLEDGED"` 不表示人事核准 |

`employee_name` 等欄位與查詢結果仍可能包含員工個資。結果、raw capture、HAR 及衍生報告只留本機；部分欄位從 repr 隱藏並不等於匿名化。

## 日期及模式

`AttendanceQuery` 只接受 `date`，排除 `datetime`、字串及逆向日期。錄製頁的前端驗證限制 `(end - start).days <= 180`；SDK 採相同限制，包含端點時最多 181 個日曆日，並非已證實的伺服器硬性限制。

| `mode` | POST `value(qryType)` | 原頁選項 |
| --- | --- | --- |
| `processed`（預設） | `qryProcess` | 排班日出勤簽到退查詢 |
| `raw` | `qryFinMachine` | 查詢刷卡機原始資料 |

新 HAR 各錄製一次上述查詢，送出日期區間相同。兩次回應 HTML 完全相同，均有三個事件且兩個 radio 都未 checked。SDK 因此保留 requested mode，`reported_mode=None`；不能把選項名稱當作伺服器已完成該分類的證明，也不能把這三筆當作整段期間的完整出勤結算。

來源未提供頁碼或下一頁操作。Parser 核對表格欄位、每列時間及頁尾筆數，缺列或未知表格報 `ParseError`。有正確表頭及「共有 0 筆」頁尾才接受合法空結果；這個零筆結構目前只有合成測試。

## 請求、SSO 與異動

| 操作 | HTTP | 主要參數 |
| --- | --- | --- |
| 狀態 | GET `/PSPDPortal/oFSchedule.do` | `reqCode=getPCClockInLog` |
| 紀錄 | POST 同一路徑 | `reqCode=getProcessedFingerLog`、`value(begDate)`、`value(endDate)`、`value(qryType)`、`b1` |
| 簽到退 | POST 同一路徑 | 僅 `reqCode=setPCClockInLog` |

查詢表單依錄製來源以 Big5 URL encoding 送出，回應依宣告及既有 Transport 規則解碼。新 SSO 使用當次 Portal 表單中的 `ssID`、三個 key、`USR_ID`、`HID` 等欄位，POST `WPSAutoLogon` 一次；只接受同來源已知等待頁的 302，再以有限 GET 讀取等待頁及固定狀態頁。等待頁的 `window.location.replace` 只接受已知 literal，不執行 JavaScript 或任意 callback。307／308 不重送 SSO POST。

狀態及查詢前核對狀態頁的登入帳號；查詢／成功頁另核對姓名。新 SSO 已取得的狀態頁限一次使用，之後讀取新狀態。整段操作持有同一 Runtime 的 `operation_lock`，使用共用 Session、節流與 TLS 政策；PSPDPortal 與 DDPortal 的相同 HTTPS 主機／埠共用連線狀態。

`punch()` 的 preflight 表單與身分驗證失敗時，不送異動。異動 POST 不跟隨轉址、不做一般 retry 或重新登入重播。已嘗試送出後遇到網路中斷、HTTP 拒絕、未知頁面或缺少明確成功通知，回傳 `RequestError`，code 為 `MUTATION_OUTCOME_UNKNOWN`、`retry_safe=False`，保留明確 SDK 原因鏈。應用先獨立查詢紀錄，再由使用者決定下一步；單純 HTTP 200 不是成功證明。

## 目錄、測試器與離線解析

唯讀目錄登錄 `attendance.status`、`attendance.records`，可經 `sdk.queries.run`／`run_result` 呼叫。`AttendanceHistory` 的零筆結果評估為 `EMPTY`，`run_result` 保留原模型於 `value`。`punch()` 是明確異動，排除唯讀目錄與所有院內測試計畫。

`sdk.auth.check(only=("attendance",))` 核對簽到退狀態頁與帳號，不提交打卡。已有快取仍重新 GET；其他人事公告頁的 HTTP 200 不視為簽到退 readiness。

原子測試器可只查本人狀態與兩種模式，不需要病歷號或醫師卡號：

```sh
vghks-live-test --profile atomic --only attendance.status --only attendance.records --start 2026-01-01 --end 2026-01-31
```

未指定日期時使用當日至往前 30 天。設定／環境可用 `attendance_base_url`／`VGHKS_ATTENDANCE_BASE_URL` 覆寫來源，公開預設仍為錄製的 PSPDPortal；相依與 readonly 測試規則保持一致。

`offline/replay.py` 共用相同純 Parser，只讀 HAR 的狀態、查詢及已錄製成功回覆。`RECORDED_ACK` 表示辨識舊錄製回覆，沒有重新執行打卡。實際證據與 SDK 院內驗證缺口見 [VALIDATION](VALIDATION.md)。
