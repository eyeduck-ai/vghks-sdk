# SDK 優化方向

P1／P2／P3 的本機優化已完成實作，保留既有 Service、CLI 與請求行為。院內證據與待驗證範圍仍以 [VALIDATION](VALIDATION.md) 為準。

## 已有基礎

- Service／Adapter／Parser／Model 分層、Protocol 注入與共用 Runtime 已建立。
- 已有操作鎖、有限恢復、TLS 政策、參照綁定及合成／localhost 測試。
- 已有附件去重、原子寫入、純證據契約及離線完整性核對，可直接沿用。

## 優先順序

以下記錄完成內容；後續擴充沿用相同驗收條件。

| 優先 | 現況與位置 | 建議修改 | 驗收重點 |
| --- | --- | --- | --- |
| P1 已完成：純解析 | [parsing/prq_extensions.py](../src/vghks_sdk/parsing/prq_extensions.py)、[parsing/oppl.py](../src/vghks_sdk/parsing/oppl.py) | 病人旗標、歷年清單、上傳類型與手術回覆判定歸位；Adapter／offline 共用，舊匯入相容 | 同一合成 ZIP 禁止 socket 重解析，完整 JSON／Markdown 報告、重測設定及原 ZIP hash 一致 |
| P1 已完成：介面 | [services/protocols.py](../src/vghks_sdk/services/protocols.py)、[queries.py](../src/vghks_sdk/queries.py) | 補密碼狀態、眼科掃描及排程 filters；分開 Service／Adapter 的 ref／reference；API generator 核對輸入綁定 | 全目錄合成 discovery／replay 覆蓋；保留位置參數別名，未知 discovery scope 明確報錯 |
| P2 已完成：模組責任 | [offline/analyze.py](../src/vghks_sdk/offline/analyze.py)、[live/profile.py](../src/vghks_sdk/live/profile.py)、[adapters/prq.py](../src/vghks_sdk/adapters/prq.py) 保留原入口 | 拆出證據／呈現、共用步驟／病人流程及 PRQ context／就診／醫囑／附件 | API、操作鎖、HTTP 次序、密碼／審查 POST 次數及重測範圍不變 |
| P2 已完成：登入拆分 | [adapters/auth.py](../src/vghks_sdk/adapters/auth.py) 保留入口、共享狀態與挑戰分類 | Portal、應用 SSO、WebMAAS 及共用傳輸／來源驗證分工；位置見 [ARCHITECTURE](ARCHITECTURE.md) | 原 33 個方法邏輯不變；帳號隔離、有限恢復及不重送密碼的回歸保留 |
| P2 已完成：回傳型別 | [models/json_responses.py](../src/vghks_sdk/models/json_responses.py)、[responses.py](../src/vghks_sdk/responses.py) | PRQ／OPPL／審查 JSON 提供可選 TypedDict 與純轉換函式；既有 Service 仍回傳 dict | 沿用 Parser 驗證，保留未知欄位與原值；未確認型別用 object，不猜來源代碼 |
| P3 已完成：本機效能 | [offline/cache.py](../src/vghks_sdk/offline/cache.py)、[offline/requests.py](../src/vghks_sdk/offline/requests.py)、[benchmark_offline.py](../tools/benchmark_offline.py) | 單次 ZIP 共用有界 JSON／請求形狀快取；預建唯讀簽章，避免重複解碼及 DOM | 公開 Reader 仍回傳新值；完整報告／重測設定、錯誤與大小限制不變，結束或失敗即清除快取 |

登錄檢查應連接 `core/operations.py`、`queries.py`、live discovery、offline replay 與 API generator；HTTP contract 與公開結果仍維持各自職責，一份結果可包含多個請求。既有重複 key、相依循環、異動排除及 HAR contract 檢查繼續保留。

效能工作以本機解析及存檔為起點。維持 SDK 預設節流、Session 操作鎖及按帳號隔離；快取限單次分析，不跨病人／Session 保存查詢內容。

## 本機效能量測

`python tools/benchmark_offline.py` 預設產生 600 筆合成回應，對照有／無快取各五輪；輸出只有計數、耗時、摘要 hash。可用 `--input` 指定本機 ZIP。下表另記錄本輪以同一 ZIP 比較上一版／新版，兩版均啟用 JSON 快取：

| 指標 | 上一版 | 新版 |
| --- | ---: | ---: |
| JSON 解碼次數 | 1,203 | 1,203 |
| 請求參數解析次數 | 1,800 | 1 |
| 分析期間簽章重建次數 | 68,400 | 0 |
| 峰值追蹤記憶體 | 5.13 MiB | 7.64 MiB |
| 耗時中位數 | 0.366 秒 | 0.054 秒 |

此案例的請求形狀相同，故只解析一次；不同形狀仍分開。簽章初始化時預建 114 個；完整 JSON／Markdown 報告、重測設定及原 ZIP hash 一致。速度改善伴隨此量測的峰值增加，不宣稱所有格式都有相同比例。

快取共用 32 MiB 總預算：JSON 單項最多 8 MiB／原檔 4 MiB、最多 64 項；請求形狀最多 4 MiB／4,096 項，只保留解析所需欄位，不保留額外 headers／Cookie。目錄、自訂 Reader、二進位及跨次分析不共用。

驗證時另修正 Windows 原子換檔偶發拒絕：`local_io.py` 對錯誤碼 5／32／33 最多換檔四次，總等待最多 140 ms；永久失敗仍保留原錯誤碼。重試限於同一份已寫好的本機暫存檔。

## 後續架構改進

目前保留既有分層，優先針對重複工作局部改寫。模組拆分改善維護；總耗時是否下降仍須量測。線上每次請求另有必要的 0.8–1.8 秒節流，目前沒有院內分段耗時足以支持全面重寫。

維持局部改寫，已完成下列工作：

| 項目 | 實作 | 保留條件 |
| --- | --- | --- |
| 操作識別 | 依 method／path 分組預建簽章；當次 ZIP 共用相同請求的解析結果 | 最具體匹配、同分次序、分頁特例及原匯入入口 |
| 回應檢查 | 純文字無密碼線索時免建 DOM；導覽與登入／schema 檢查重用既有 DOM | HTML／entity／script／密碼線索仍完整解析，不只信 Content-Type；強制變更及 timeout 判斷保留 |
| 步驟存檔 | 共用 writer 只做一次序列化；500 步合成存檔由 6.47 降至 4.99 秒 | 每步仍寫完整原子快照；中斷、失敗及原錯誤碼保留。journal／checkpoint 留待更大批次需求確認 |

CPU profile 含量測開銷，上述呼叫計數用於定位重複工作；不視為院內效能或預期加速比例。後續改動仍需完整報告／重測設定一致及來源 hash 不變的驗收。

共用 Session／病人 context 的完整操作鎖維持；同帳號改用 async 或並行 HTTP 仍需遵守狀態與次序，不能據此增加請求量。離線純解析可獨立優化，跨病人／Session 的醫療結果快取仍不採用。

## 測試加速

`python run_tests.py` 預設完整循序執行；`--jobs 2 --durations 10` 以隔離程序按模組執行並顯示慢項目。相同 822 項的案例 ID／結果完全一致：單程序 155.8 秒、兩程序 85.6 秒，均略過 13 項私有條件測試。

日常修改可先用重複的 `--pattern "test_auth*.py"` 選相關模組，交付前仍跑完整 suite。`--list` 可核對範圍；`--timings output/test-timings.json` 保存名稱／耗時。空選取、匯入錯誤、失敗及案例數不符均以非零碼結束；不刪減 TLS／恢復或其他案例。

CI 的 Windows 3.10／Ubuntu 3.12 同樣採兩程序完整測試；修正 Windows 暫存目錄別名的路徑斷言，正式 writer 的回傳契約維持。

## 驗證工作

優先在下一輪經授權的院內測試補齊已知缺口：

- 批次掛號：實際分頁、合法空結果及恢復情境。
- 簽到退：SDK 實際院內登入與唯讀查詢；送出異動另依明確授權。
- 登入／WebMAAS：自然 TTL、強制改密碼與缺表單備援，僅在實際出現時記錄。
- 掃描與調閱：完整分類流程，以及不同入口獨立觸發調閱審查。

Cookie 遺失、合成回應與 localhost 成功各保留自己的證據層級。NO_SAMPLE 保留缺樣本；後續成功不覆寫原 ERROR／PARTIAL。

## 每批變更的完成條件

1. 一批只處理一個責任邊界，保留既有 Service、型別與 CLI 相容入口。
2. 先跑相關 unittest，再跑完整 suite、Ruff、API 同步與公開內容檢查；指令見 [DEVELOPMENT](DEVELOPMENT.md)。
3. 離線重構以同一 ZIP 禁止 socket 比較完整報告（排除 analyzer build）及重測設定；沒有私有證據時使用合成案例並保留未驗證範圍。
4. 涉及 EXE 時補對應 source／frozen localhost 驗證；涉及封裝時檢查 wheel／sdist 及獨立安裝。

[架構與責任邊界](ARCHITECTURE.md) · [新增 HAR 功能](HAR_RECORDING.md)
