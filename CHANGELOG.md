# Changelog

## 0.18.0

- SDK 共用傳輸層自動選擇 TLS：PRQ／SectOrd／WebMAAS 第一個請求使用已驗證的 TLS12_COMPAT，同源共用且保留 Session／Cookie；一般使用不需手動 configure_connection。
- 唯讀請求在有限次數內切換協定，明確憑證錯誤可對已設定的來源使用略過驗證的 HTTPS。預設 allow_unverified_tls=True，嚴格模式 False 可關閉該備援；不改 HTTP，不全域關閉驗證。
- 第一次不可重試 POST 前匿名確認連線；登入與異動本身不因 TLS 自動補送。新增 connection_status，記錄每個服務實際選擇。
- 測試 EXE 預檢成功即停止，同源重用結果；raw capture 保留匿名探測及已恢復失敗，離線分析不將已恢復網路失敗當成未解錯誤。
- 離線分析將 NO_SAMPLE 獨立列為驗證缺口，只有缺口時保留 COMPLETED_WITH_GAPS 並回傳 exit code 0；不再將缺欄位誤報為執行錯誤，真正的回應錯誤仍照常報告。

## 0.17.2

- 修正病人標頭將網址分段串接時，`&hidno=...` 未被辨識成 query 而誤報 PRQ_PATIENT_ID_MISMATCH。保留完整身分比對，缺失、空白或衝突的識別值仍拒絕。
- 0.17.2 內網回傳確認身分證分支修正：病歷號／身分證兩份清單一致，四欄篩選及從身分證清單選定門診取得 SOAP／醫囑成功；醫師卡號因來源未提供而保留 NO_SAMPLE。
- 合成病人標頭改用實際遇到的 URL 串接結構，供 Parser、Adapter、離線重解析及 localhost EXE 共用回歸。

## 0.17.1

- 新增 visits 增量測試：比較病歷號／身分證就診清單，保留四欄篩選及最多三筆門診的 SOAP／醫囑串接證據。
- 可從基本資料自動取得病人身分證或啟動時手動輸入；錯誤、無樣本及替代路徑分開記錄，各項失敗不阻止獨立檢查。
- EXE 可用 `--default-profile visits` 建置成雙擊即測新增功能的版本；沿用無設定檔搬移、未加密且含時間的同目錄 ZIP。
- 離線重測建議保留 visits 計畫，避免退回只查病歷號而漏測身分證分支。

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
