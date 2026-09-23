# 連線與自動恢復

從 0.18.0 起，一般程式只需建立 VghksSDK 並呼叫 Service；HTTPS 相容處理由 SDK 共用傳輸層完成，不依賴測試 EXE，也不需先呼叫 configure_connection。

## 預設行為

| 情況 | SDK 處理方式 |
| --- | --- |
| PRQ、SectOrd、WebMAAS | 第一個請求就用 TLS12_COMPAT；此設定已有內網成功證據 |
| OPPL／手術紀錄與 SectOrd 共用主機及埠 | 共用同一連線設定，不重複探測 |
| 其他服務 | 先用一般 TLS；遇到協定／握手錯誤再切換候選模式 |
| 伺服器只接受較新 TLS | 可由 TLS12_COMPAT 改回 DEFAULT，以支援 TLS 1.3 |
| 明確憑證錯誤 | 預設允許只對已設定的該 HTTPS 來源略過驗證，繼續登入／查詢 |
| DNS、逾時、連線中斷、429／502／503／504 | 唯讀請求按 RequestPolicy 有限重試及退避；不因這些錯誤關閉憑證驗證 |
| 無法恢復 | 回傳含穩定錯誤碼的 RequestError；不無限重試 |

「來源」是 scheme、hostname、port 完全相同。設定及成功狀態只在目前 SDK 實例記憶，重建 SDK 會重新從預設開始；不新增需要搬移的快取或設定檔。跨來源轉址只有 SDKSettings 已設定的 HTTPS 服務可自動調整；其他來源及 ProxyError 不會觸發 TLS 降級。

所有模式維持 HTTPS；TLS12_COMPAT 不開啟 TLS 1.0／1.1。關閉憑證驗證仍有加密，但不再確認伺服器身分，因此僅在明確驗證失敗時啟用。沒有已錄製的 HTTP／SSO 路徑，SDK 不會自行把 https 改成 http。

## 登入、異動與節流

第一次向尚未確認連線的來源傳送密碼或不可重試的 POST 前，SDK 以獨立 Session 做匿名 GET `/`，先解決 TLS 問題。此探測不攜帶使用者 Cookie、Authorization、參數或 body，不跟隨轉址；401／403／404 也能證明 HTTPS 已連通，但不代表已登入。

密碼及異動 POST 不會被傳輸層自動補送；送出後若結果不明，應先查詢結果。GET 與 Adapter 明確標記為唯讀的 POST 才能重試，預設每個請求最多三次，TLS 切換也計入此上限。匿名探測有自己的同等上限。

保留 Session／Cookie、完整原子操作鎖、瀏覽器格式標頭及每次 0.8–1.8 秒隨機等待。同一 SDK 循序執行；現有 workflow／測試器繼續保留各項獨立失敗與後續可執行步驟，不把錯誤偽裝成空資料。

## Session 過期與登入失敗

從 0.19.1 起，SDK 明確區分「既有登入過期」與「這次登入沒有成功」。一般查詢在下次請求發現已知登入頁或 HTTP 401／403 時，清除舊 Cookie／子系統狀態、用建立 SDK 時的帳密重新登入、取得 SSO，再重做原子查詢一次。必要的病人 context 由 Adapter 重建；不需要應用程式自己操作 Cookie。沒有背景定時續期。

0.19.3 也辨識 JSON 查詢被轉址到已知入口、以及回應中實際的入口導覽腳本。遇到舊 HTTP 入口轉址時直接以設定的 HTTPS 重新登入，不跟隨該 HTTP 轉址；未知的 JSON 轉址回報 `QUERY_REDIRECT_UNRECOGNIZED`，不猜測登入狀態。登入回應的「重新登入」按鈕不是自動導覽，也不能當成成功證據。

每次原子查詢最多恢復一次；第二次仍過期回報 `AUTH_RELOGIN_FAILED`。若重新登入本身失敗，保留其具體 AuthenticationError 型別與 `info.code`，不再以籠統的恢復失敗碼覆蓋。HTTP 401／403 也可能是權限問題；一次恢復機會不表示 SDK 已確認原因就是過期。

| 情況 | 對外結果 | 自動再次提交密碼 |
| --- | --- | --- |
| 帳號／密碼空白 | ConfigurationError；`CREDENTIAL_USERNAME_MISSING`／`CREDENTIAL_PASSWORD_MISSING` | 不會；尚未發出登入請求 |
| Portal 提交後仍是登入表單、退回登入頁或明確顯示帳密錯誤 | LoginRejectedError；`PORTAL_LOGIN_REJECTED` | 不會 |
| 登入建立階段收到 401／403 | AuthenticationError；`PORTAL_LOGIN_HTTP_DENIED`，保留 http_status | 不會；不能直接認定密碼錯誤 |
| 登入回應空白、找不到導覽目標或落地頁空白 | AuthenticationError；`PORTAL_LOGIN_RESPONSE_EMPTY`／`PORTAL_LOGIN_TARGET_MISSING`／`PORTAL_LOGIN_LANDING_EMPTY` | 不會；帳密有效性未明 |
| 登入導覽目標不明或互相矛盾 | AuthenticationError；`PORTAL_LOGIN_TARGET_UNRECOGNIZED`／`PORTAL_LOGIN_TARGET_AMBIGUOUS` | 不會 |
| 登入落到已知系統錯誤頁 | AuthenticationError；`PORTAL_LOGIN_NOT_ESTABLISHED` 或 `AUTH_LANDING_ERROR_PAGE` | 不會 |
| 審查 OAuth 提交密碼後退回登入表單 | LoginRejectedError；`REVIEW_OAUTH_LOGIN_REJECTED` | 不會 |
| 審查 OAuth 提交密碼後收到 401／403，或回應格式不明 | AuthenticationError；`REVIEW_OAUTH_LOGIN_HTTP_DENIED`／`REVIEW_OAUTH_LOGIN_RESPONSE_UNRECOGNIZED` | 不會重新開始整段登入 |
| MIS 提交後仍要求輸入獨立密碼 | LoginRejectedError；`EARNINGS_PASSWORD_REJECTED` | 不會 |

`LoginRejectedError` 與 `AuthExpiredError` 都繼承 `AuthenticationError`，可從 `vghks_sdk` 匯入；兩者互不繼承。登入遭拒只代表系統沒有接受這次登入，無足夠證據時不進一步猜測密碼錯誤、帳號鎖定或密碼到期。未知頁面保留錯誤，不當成查詢空清單。

在已建立的 `sdk` 中，應用程式可這樣處理：

```python
from vghks_sdk import AuthenticationError, LoginRejectedError, SDKError

try:
    visits = sdk.records.get_visit_cases(mrn)
except LoginRejectedError as exc:
    print("登入遭拒，請確認帳密或帳號狀態：", exc.info.code)
    raise  # 停止這項任務，交給應用程式取得使用者更正後的帳密
except AuthenticationError as exc:
    print("無法建立或恢復登入：", exc.info.code)
    raise
except SDKError as exc:
    print("查詢失敗：", exc.info.category, exc.info.code)
    raise
```

主系統帳密更正後，以新的 `PortalCredentials` 建立新的 `VghksSDK`；不要直接修改 Adapter 的內部狀態。MIS 帳密另以 `EarningsCredentials` 傳給 `open_performance`／`open_bonus`。SDK 不保存帳密到設定檔；Credentials 的 repr 不含密碼，但啟用完整 raw capture 時輸出仍只適合本機使用。

`sdk.auth.check()` 回傳報告而非一律拋出登入例外：以 `report.ok` 與 `report.targets[i].issue.code` 判斷。Portal 登入遭拒後，其相依子系統標為 BLOCKED，不再多送一次帳密。`report.reauthenticated` 表示曾進入恢復流程，不保證恢復成功。

自動恢復的例外範圍：

- MIS 的二次驗證不自動重播；`EARNINGS_SESSION_EXPIRED` 或 `EARNINGS_CONTEXT_EXPIRED` 需重新開啟報表，取得新的 context。
- 新增、修改、取消等異動不會因 Session 過期自動重送；結果不明時先讀回確認。
- 密碼 POST 不交給 Requests 自動跟隨轉址。Portal 只允許有限次同來源 GET 導覽，307／308 要求重送 POST 時即停止；沒有已知導覽目標／成功落地資訊不能僅以 HTTP 200 宣告登入成功。
- 重試上限以每次 API 呼叫計算；應用程式仍可主動再呼叫，因此不要在外層對登入錯誤套用無限制重試。

## 查看狀態與進階設定

```python
from vghks_sdk import SDKSettings

settings = SDKSettings()  # 自動選擇及恢復；一般使用的預設
strict = SDKSettings(allow_unverified_tls=False)  # 保留自動協定相容，憑證失敗即報錯

# 在現有 sdk 中查詢；不會發出額外 HTTP 或包含帳密／Cookie。
status = sdk.connection_status()
print(status["prq"])
```

每個服務包含 tls_profile、certificate_verification、direct、confirmed 與 source。confirmed 表示此 HTTPS 來源已收到回應，或呼叫者／EXE 明確套用了設定；不代表登入成功或取得病人資料。source 可為 PREFERRED、RECOVERED、CONFIGURED。

原有 `sdk.configure_connection(app, tls_profile=..., verify_certificate=...)` 保留為進階覆寫。`SDKSettings(auto_tls=False)` 關閉自動選擇與匿名探測，交由呼叫者設定；一般使用不需要它。`SDKSettings.from_env()` 支援 VGHKS_AUTO_TLS 與 VGHKS_ALLOW_UNVERIFIED_TLS，接受 true／false、1／0、yes／no、on／off。自行注入 transport 的應用仍由該 transport 管理連線政策。

若啟用 raw capture，匿名探測、失敗及重試都會保留；離線分析將同一請求已取得後續 HTTP 回應的網路失敗列入 recovered_requests。後續 HTTP／解析錯誤仍照常列出，不能被「已恢復連線」掩蓋。測試 EXE 的 selected_profiles.json 記錄預檢選擇，個別 HTTP 的 tls_details 記錄其實際模式。

內網已驗證的相容設定與本機合成的自動切換驗證範圍，分別見 [VALIDATION](VALIDATION.md)。
