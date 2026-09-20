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
