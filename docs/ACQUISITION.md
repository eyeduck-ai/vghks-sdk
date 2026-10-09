# 資料獲取與失敗分類

0.21.0 提供可選用的統一結果介面，原有 Service 與 `sdk.queries.run` 的型別及例外行為不變。SDK 辨識來源及安全重試條件，整合系統決定任務排程、提示使用者、是否接受缺資料，以及部分成功如何存檔。

```python
from vghks_sdk import acquire

result = acquire(lambda: sdk.records.get_visit_cases(mrn))
# 動態呼叫亦可用 sdk.queries.run_result("prq.visit_cases", mrn=mrn)
if result.status == "ERROR":
    issue = result.error
    cause = issue.root_cause
    record_failure(issue)  # 儲存結構化資訊，避免靠錯誤訊息文字判斷
    if issue.retry_recommended:
        schedule_later()  # 仍需由應用設定次數、等待及任務期限
elif result.status == "EMPTY":
    record_absence(result.data.availability)
else:
    use_visits(result.value)
    if result.status == "PARTIAL":
        record_parsing_issues(result.data.issues)
```

`acquire` 呼叫傳入的唯讀操作一次；該次呼叫內仍由 Runtime／Transport 執行既有恢復與有限重試。它不新增重試、查詢或登入，不捕捉 KeyboardInterrupt、SystemExit 或非 SDK 的程式錯誤。手動捕捉例外時可用公開的 `error_info(exc)` 取得安全分類；未知例外為 INTERNAL／UNEXPECTED_ERROR，不當成資料缺失。

## 結果契約

| status | 語意 |
| --- | --- |
| OK | 操作正常回傳；仍須讀 data.availability，UNKNOWN 不代表已確認有目標內容 |
| EMPTY | 合法空結果、無參照或 NotFoundError；後者的錯誤資訊仍在 error |
| PARTIAL | 有回傳值，但已知解析問題／rendering notes 需要檢查；原始模型保留在 value |
| ERROR | SDK 操作失敗；value 通常為 None，error 保留完整安全原因鏈 |

`result.ok` 對 OK／EMPTY／PARTIAL 為 True，表示這次讀取有可辨識結果；不代表資料非空或解析完整。登入檢查回傳的非 OK AuthCheckReport 會保留報告並標 ERROR，不誤認為成功。

`DataAssessment.availability` 區分 AVAILABLE、EMPTY、NOT_FOUND、NOT_EXECUTED、ATTACHMENT_ONLY、METADATA_ONLY、BINARY_AVAILABLE、UNKNOWN。`item_count` 是該結果的項目數；非空 BinaryAsset 算一份檔案，位元組數仍由 value.size 取得。`complete` 只表示目前支援解析的完整性：True、False 或未知 None，不表示涵蓋全部病歷或臨床資料。`issues` 保留安全的問題碼，不加入病人值或原始文字。

0.22.1 起另有 `warnings`：目前僅限兩層日期／OD／OS 表頭、每列三欄及 `column_paths` 完全對齊時的 `NUMERIC_HEADER_SPAN_MISMATCH`。此警示不使 `complete=False`；原 `NumericTable.parsing_issues` 與原值仍保留。沒有逐欄對齊證據或同時出現其他問題時，`issues` 與 PARTIAL 判定照常保留。整合系統可分別記錄 `data.issues` 與 `data.warnings`。

空清單及合法 None 只說明這次條件下的結果；不能推論全院沒有資料。未執行醫囑、PDF／JPG 參照、已下載 bytes、報告正文分開表示。未知的 dict／HTML 語意保持 UNKNOWN，不靠 truthiness 判空。`assess_data(value)` 可對已取得的支援模型做相同辨識，不會發 HTTP。

`AttendanceHistory` 依 `records` 筆數評估 AVAILABLE／EMPTY，原模型、查詢模式及來源資訊留在 `value`。Parser 必須核對表頭、逐列時間與頁尾總數，未知頁面是 ERROR；`complete=True` 只表示這份支援表格完整解析，不能推論整段期間的出勤結算或原始／處理模式已獲伺服器確認。使用方式見 [ATTENDANCE](ATTENDANCE.md)。

## 錯誤與重試

`SDKError.info` 保留原 code/category/operation/app/endpoint_path/http_status/attempt/cause_type，另有：

| 欄位 | 語意 |
| --- | --- |
| phase | VALIDATION、CONNECTION_PROBE、REQUEST、RESPONSE、PARSE、AUTHENTICATION、AUTHORIZATION 或 REAUTHENTICATION 等失敗階段；未提供時為空字串 |
| cause | 明確 `raise ... from ...` 的 SDK 原因鏈；不從 Python 隱含 context 猜因果 |
| root_cause | 最深層結構化原因，沒有 cause 時就是自身 |
| retry_safe | True 表示可安全重做該讀取；False 表示不可自動補送；None 表示未知 |
| retry_recommended | SDK 已知暫時性錯誤且安全重試時為 True；只是交給應用的提示，不會自行送請求 |

例如重登入 DNS 失敗時，外層仍為 AUTHENTICATION／AUTH_RELOGIN_FAILED、phase=REAUTHENTICATION；`info.root_cause.code` 為 NETWORK_DNS_FAILED。外層不建議盲目重做整段登入，應用可依底層原因暫停或另安排連線檢查。

HTTP 401／403 以 AUTH_HTTP_DENIED 保留狀態與 HTTP 原因，允許原有一次恢復；若恢復後仍遭拒則 AUTH_RELOGIN_FAILED。這不是已證實的 TTL 過期或權限不足。HTTP_404 保持 HTTP 錯誤，不轉成 NOT_FOUND。LoginRejectedError 只表示這次登入未被接受，不猜測密碼錯誤、鎖定或到期。

0.22.0 起，尚未建立登入的直接查詢挑戰以 `NotAuthenticatedError`／`AUTH_NOT_AUTHENTICATED` 表示；一般 Service 仍會按需登入。來源明確要求變更密碼時使用 `PasswordChangeRequiredError`／`PORTAL_PASSWORD_CHANGE_REQUIRED`，不自動重新登入。`sdk.auth.password_status` 保留已觀察的 EXPIRING／倒數天數；只有收到支援的明確文字、登入變更表單或同來源變更導覽才分類，未知原因不猜測。

| 可辨識情況 | 分類或原因碼 | 整合系統可採取的處置 |
| --- | --- | --- |
| 合法查無資料 | EMPTY／NOT_FOUND | 按查詢條件保存缺資料狀態 |
| 這次登入被拒絕 | AUTHENTICATION／PORTAL_LOGIN_REJECTED | 停止相依工作，請使用者確認帳號或院方登入限制 |
| 尚未建立登入的查詢挑戰 | AUTHENTICATION／AUTH_NOT_AUTHENTICATED | 建立正常 SDK 登入，或保留匿名觀察結果 |
| 明確要求變更密碼 | AUTHENTICATION／PORTAL_PASSWORD_CHANGE_REQUIRED | 停止相依工作；由使用者在院方入口變更後提供新密碼 |
| 密碼到期前倒數 | PasswordStatus.status=EXPIRING／remaining_days | 提醒使用者；成功登入及合法查詢仍可繼續 |
| 需要院方調閱審查決策 | AUTHORIZATION／PRQ_ACCESS_REVIEW_REQUIRED | 顯示具體問題，確認當頁可選原因 |
| DNS／連線／讀取逾時 | NETWORK_DNS_FAILED／NETWORK_CONNECT_TIMEOUT／NETWORK_READ_TIMEOUT | 按外層安全性與任務預算決定稍後重試 |
| Proxy／TLS | NETWORK_PROXY_FAILED／TLS_VERIFY_FAILED／TLS_PROTOCOL_FAILED | 檢查連線設定；不靠重送帳密處理 |
| HTTP 失敗 | HTTP_404／HTTP_429／HTTP_503 等 | 保留狀態；429／暫時性伺服器錯誤僅在安全時提示重試 |
| 格式或解析失敗 | PARSE／RESPONSE_JSON_INVALID／UPLOAD_TYPES_INVALID 等 | 保留原始證據，檢查來源格式或解析器 |
| 簽到退送出結果不明 | MUTATION_OUTCOME_UNKNOWN、retry_safe=False | 先以獨立唯讀查詢確認紀錄，再由使用者決定是否另行送出 |
| 呼叫參數不符 | CONFIGURATION／QUERY_INPUTS_INVALID 等 | 修正輸入，避免相同錯誤反覆排程 |

密碼 POST、異動及調閱審查送出後的整段操作維持 retry_safe=False。retry_recommended=True 也不代表已確認伺服器會恢復，更不代表可以忽略 SDK 既有退避、節流或應用的重試預算。應用應讀外層安全性，不能因 root_cause 是網路錯誤便重送整個操作。

診斷 trace、LiveTestStep 及 ZIP 的 parsed 結果保留新資訊；離線分析會辨識結構化底層原因。`value` 和 raw capture 仍可能包含個資；AcquisitionResult 並不去識別化整份回傳。

內網情境與證據層級見 [LIVE_TEST](LIVE_TEST.md)，驗證範圍見 [VALIDATION](VALIDATION.md)。
