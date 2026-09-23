# 架構

應用程式或 workflow → Service → Adapter → Runtime／Requests；Adapter 呼叫純 Parser 產生 models。57 個唯讀功能由 queries.py 統一登錄，可供自動測試與其他應用發現。

```text
application / workflows
        ↓
services       ← sdk.py 組裝各領域
        ↓
adapters ──────→ parsing ──────→ models
        ↓
runtime / core / authentication
        ↓
Requests Session（鎖、節流、retry、capture）
```

| 模組 | 責任 |
| --- | --- |
| sdk.py | 公開 facade 與依賴注入，提供 context manager |
| services/ | 穩定 API，讓應用不依賴 endpoint 與 HTML |
| adapters/ | 病人、就診、表單、SSO 模式等請求次序 |
| parsing/ | 無網路的 HTML／JSON／binary 解析，可用同一份證據反覆測試 |
| models/ | 不同領域型別、篩選條件與參照；`py.typed` 隨 wheel 發布 |
| runtime.py / core/ | Session、操作鎖、登入復原、傳輸政策、錯誤與診斷 |
| workflows/ | 跨多次原子操作的任務、去重、部分失敗及階段輸出 |
| live/ | 使用 SDK 的內網測試器，與一般 library 使用分離 |
| contracts/ / offline/ | 只讀本機錄製資料，驗證及重解析，不重送 HAR 請求 |

一般 `import vghks_sdk` 不載入 live／offline；直接使用型別模型及 Services。`operation_models.py` 保留舊匯入相容性，新程式使用 models。現有 core/full CLI 是既有使用介面，仍有測試覆蓋，未以清理名義移除。

**兩種目錄的用途不同**：core/operations.py 記錄 HTTP contract（method、path、欄位、是否異動）；queries.py 記錄有意義的公開唯讀結果（Service、輸入、抽樣 scope、發現相依）。一個結果可能需多次 HTTP。

**Session 有狀態**：病人 context 與同主機 SSO 模式可能互相影響；Runtime 在完整操作期間持有 RLock 並管理 cache invalidation。不要在同一 Session 外加 thread pool；獨立任務各用 SDK，並維持整體節流。

**登入失敗不等於 Session 過期**：AuthExpiredError 才代表可嘗試恢復的既有登入；LoginRejectedError 為 AuthenticationError 的另一個子類別，不能進入重登入迴圈。Adapter 區分登入建立階段與查詢階段的回應，Runtime 在重登入遭拒時保留原錯誤。原子查詢最多恢復一次；MIS 二次驗證與異動不套用這個自動重做機制。詳見 [CONNECTIONS](CONNECTIONS.md#session-過期與登入失敗)。

**連線政策由 SDK 共用**：core/connections.py 管理每個 HTTPS 來源的優先 TLS 模式及成功狀態，core/transport.py 在有限重試內切換，並在初次不可重試 POST 前匿名確認連線。live/preflight.py 沿用相同優先設定；一般 Service 不依賴測試器。Session／Cookie 不因換模式重建。預設行為與嚴格模式見 [CONNECTIONS](CONNECTIONS.md)。

**測試參數屬於執行，不屬於 SDK**：LiveTestConfig.test_mrn 沿各 profile 傳遞，不使用真實病歷號全域常數。公開預設只有合成識別值；自用 EXE 的 private defaults 在建置時注入，wheel 不包含它。

**診斷有兩層**：一般 DiagnosticRecorder 記錄有限的結構化錯誤；RawCaptureRecorder 為使用者明確啟用的完整未加密證據。所有 raw／parsed 回傳仍可能含個資，不能公開。

**SOAP 結構化屬於純解析**：`parsing/soap.py` 依標籤、rowspan 及摘要標頭處理同一個 SOAP 回應；`parsing/prq.py` 保留原解析器匯入入口。SoapRecord 保留原有 blocks／full_text，新增分段、SoapDiagnosis／SoapOrder／SoapMedication，以及明示「服藥期限」的 SoapChronicPrescriptionPeriod。醫囑與藥囑是頁面列印摘要，不含報告參照；取詳細醫囑及報告仍由 orders／medications 原子操作負責。未知列保留原文及 parsing_issues，不能猜測醫療含義。

修改順序與檢查表見 [AGENTS](../AGENTS.md)；實際組合見 [COMPOSITION](COMPOSITION.md)。
