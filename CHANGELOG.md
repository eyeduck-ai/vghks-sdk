# Changelog

## 0.17.0

- `records.get_visit_cases`／`find_visit_cases` 支援 `national_id`，與病歷號二擇一；依錄製的前端 type=2 表單查詢，核對病人標頭身分並解析實際病歷號。此新增連線分支尚待內網實測。
- VisitCase 增加 doctor_name、doctor_card、case_type_label；完整解析 KSCase 的醫師欄與串接 URL，保留就診參照。
- VisitFilter 支援到院日期、O／A／E 類別、科別、醫師姓名／卡號的組合篩選；可重複篩選同份清單而不增加 requests。
- 病人不符明確報錯；身分證查詢逾時重登入會重建及核對病人 context。離線 HAR／ZIP 重解析支援病人標頭。
- SOAP 明確限制已錄製的門診路徑；列出住院／急診不代表已有其 SOAP／醫囑串接。補充使用範例、原子 API 與驗證範圍。

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
