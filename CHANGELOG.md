# Changelog

## 0.16.0

- 整理可公開的 SDK、MIT 授權、CI、原子 API 參考、組合開發及 HAR 擴充文件。
- 移除真實測試識別值；LiveTestConfig.test_mrn 可由 CLI／JSON／環境設定，自用 EXE 可內嵌私有預設。
- 修正 MIS 巢狀資料表被忽略，保留主表的數值欄位。
- 修正離線分析對空門診入口及 MIS USR_ID SSO 欄位的誤判。
- 沒有可用參照的測試標 NO_SAMPLE，整體可標 COMPLETED_WITH_GAPS；已恢復的 TLS 探測不當成未解錯誤。
- comprehensive 的原子門診日期涵蓋最近七天；完整 SOAP 組合流程仍由明確設定控制。
- HAR contract 從請求取得病人 context，移除固定病人依賴。

## 0.15.x 以前

本機開發階段建立 Requests 登入與 SSO、病人／掛號／就診、醫囑與報告、手術案例／PDF、審查系統及 MIS 報表。0.16.0 為整理後的首次公開原始碼版本；歷史私有 HAR 與內網回傳不納入 Git。
