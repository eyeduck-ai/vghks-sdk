# 資料與安全回報

HAR／return ZIP **不適合公開**。它們可能包含帳密、Cookie、SSO code/token、病人資料、檢查影像及薪資；衍生 JSON、HTML、PDF、JPG、console log 與分析報告也可能含相同內容。sanitized HAR 通常只移除部分標頭，不保證正文去識別化。

公開 repo 僅保留程式、說明及重新製作的合成測試。data、private、output、dist、build、tmp 等資料夾受 Git 忽略；發布工具另外檢查 Git index 與套件內容。範例中的識別值均為合成值或由呼叫者提供。

測試 raw capture 依使用者需求不加密，搬移與保存由使用者管理；不應上傳到 GitHub Issue／Discussion／Release／Actions artifact。一般 SDK 不會因 import 而啟用 raw capture。

tests/fixtures/tls 的私鑰是公開的 localhost 合成測試用金鑰，只能用於 loopback mock server，不能用於任何真實服務。

回報問題時提供版本、操作 ID、ErrorInfo.code、合成最小案例。若問題需要原始敏感資料，先與維護者安排合適的私下方式，不在公開 Issue 張貼。

若曾誤公開真實登入憑證或可重播 Session，應處理憑證／Session 失效及所有傳播位置，再清理 Git 歷史與衍生附件。只刪除最新版本不會移除既有歷史。
