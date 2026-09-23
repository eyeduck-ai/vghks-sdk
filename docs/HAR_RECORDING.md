# 用新 HAR 擴充 SDK

HAR 是請求／回應證據，不是要直接重送的腳本。先辨識操作目的與動態相依，再實作可重用 Service。完整開發規則見 [AGENTS](../AGENTS.md)。

## 錄製重點

1. 使用獲准查詢的帳號與資料。每份檔案聚焦一種用途，例如「查詢清單」「開啟明細」「下載附件」，另記錄點擊順序、預期結果與日期條件。
2. 操作前開 DevTools → Network，開啟 Preserve log、Disable cache，保留 All 類型；不要只錄 Fetch/XHR。先清空，再從功能入口完成一次查詢。
3. 同一功能盡量錄到有資料、無資料、多筆／下一頁、不同呈現型態。醫囑報告要分別包含文字、附件 PDF、JPG 有圖、JPG 查無資料、未執行狀態。
4. 檢查 Response 是否有正文；只記錄 URL、status 或 favicon 不能還原資料解析。二進位 response 若未進 HAR，可保留同輪 EXE 的原始 bytes 供本機驗證。
5. 匯出 HAR with content。Chrome sanitized HAR 會去除部分敏感標頭，但 response body 仍可能有病人、薪資及表單資料，因此也不能公開。只有診斷登入流程確實需要時才保留敏感登入標頭，仍只留本機。[Chrome Network 官方說明](https://developer.chrome.com/docs/devtools/network/reference#save-as-har)

## 新視窗／分頁

父頁的 Network 不會自動包含另一分頁的全部請求。報告開到新分頁時，要在該分頁開 DevTools 並另存 HAR。

若第一次開啟來不及錄，可保持報告視窗與 DevTools 開啟，回原頁再點同一或另一份報告，確認命名視窗被重用並有新請求。純讀取頁也可重新載入來觀察正文；新增、取消或提交表單不能為錄製而重送。

父頁／子頁檔案成對命名，記下哪次點擊對應哪個報告。DBR 的不同錄製可能是「建立視窗」與「在既有視窗切換報告」，不要僅以檔名判斷 body 缺失。需要資料時先找正文 API／HTML；有可用文字不必強求 PDF 或導入瀏覽器模擬。

## 本機分析

原件放 `data/recordings/YYYY-MM-DD/`，回傳放 `data/returns/`，皆受 Git 忽略。保留原件，不在上面直接刪改內容。

```sh
python tools/analyze_recordings.py --input data/recordings/new-batch --output output/har-analysis
python run_sdk.py replay-har --input data/recordings/new-batch --output output/har-replay.json
```

分析工具只讀檔，不重播 HTTP。先檢查 missing_response_bodies、未知操作與 parser 結果。登入前 302、查無資料、viewer HTML、PDF bytes 是不同回應。

為每個新功能記錄：method/path、參數名稱、固定 operation 值、日期格式、來源 SSO、前置 context／token、分頁規則、輸出型態、正常空結果、可能異動。不要把 token、HID、Cookie、病歷號或帳密複製成程式常數。

## 從證據到原子操作

1. **模型**：新增回傳型別及下一步所需 Ref，先規範空結果與缺欄位。
2. **純解析**：HTML／JSON → models；僅解析允許的靜態 JS 字面值，不 eval 來源腳本。
3. **傳輸**：在 core/operations.py 定義 endpoint contract，Adapter 透過 Runtime 處理 SSO、context、分頁與輸入驗證。
4. **公開介面**：Service 提供有意義的方法，更新 Protocol、sdk.py 與必要的公開型別匯出。
5. **測試與回放**：加入合成成功／空／錯誤案例及 mock request 次序；offline/replay.py 使用同一 Parser。
6. **EXE**：唯讀功能加入 queries.py 和 live/atomic.py 的輸入發現；缺候選保留 NO_SAMPLE，不冒充成功。
7. **文件**：新增 API generator 的用途說明與組合範例，更新驗證層級。使用新 Session 的內網回傳確認實際可用。

不要將真實 HAR 作為 Git fixture。公開案例應重建最小合成 HTML／JSON，使用合成識別值與內容，同時保留真正造成問題的結構，例如巢狀表格、重複表單欄位或兩個 SSO 模式。

## 判定完成

原子功能應有可識別的成功資料、合理空結果、未知格式錯誤、來源／病人綁定、文件及組合入口。僅 HAR 解析成功只能標記離線驗證；只有 EXE 回傳成功且資料完整，才標記該路徑有內網成功證據。
