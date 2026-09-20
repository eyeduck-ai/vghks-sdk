# 原子功能參考

本文件由 `python tools/generate_api_reference.py` 產生；共 57 個登錄唯讀查詢。
用途文字維護於該工具，簽名與回傳型別取自真正的 Service；`--check` 檢查文件是否落後。

每個原子功能回傳一種可使用的結果，可能需要數個 HTTP 請求完成 SSO、病人 context 或分頁。
優先直接呼叫 `sdk.<service>.<method>(...)`。動態任務可用 `sdk.queries.run(key, **inputs)`；
目錄中的 dependencies 是測試器發現輸入時的相依關係，不表示 Service 每次都會重新執行那些查詢。

原子操作不負責把所有病人／所有附件自動跑完；這由 workflows 或應用程式負責。

## sdk.patients

| 操作 ID | Service 呼叫及回傳 | 用途 |
| --- | --- | --- |
| `webmaas.demographics` | `get_demographics(mrn: str) -> PatientDemographics` | 精簡身分與聯絡資訊；供清單補充欄位。 |
| `webmaas.basic_info` | `get_basic_info(mrn: str) -> PatientBasicInfo` | 完整基本資料、住院／出院提示；保留來源欄位。 |
| `webmaas.registration_query` | `get_registration_history(mrn: str) -> list[RegistrationRecord]` | 掛號紀錄與狀態；有掛號不等於已就診。 |
## sdk.opd

| 操作 ID | Service 呼叫及回傳 | 用途 |
| --- | --- | --- |
| `prq.opd_patients` | `get_doctor_patients(card_no: str, visit_date: date) -> list[OutpatientPatient]` | 指定醫師與日期的門診掛號清單；保留科別與回傳的醫師標示。 |
## sdk.records

| 操作 ID | Service 呼叫及回傳 | 用途 |
| --- | --- | --- |
| `prq.visit_cases` | `get_visit_cases(mrn: str &#124; None = None, *, national_id: str &#124; None = None) -> list[VisitCase]` | 以病歷號或 national_id（二擇一）查就診清單；含到院日、類別、科別、醫師。參照 [VISITS](VISITS.md) 篩選並串接。 |
| `prq.case_detail` | `get_case_detail(case: VisitCase) -> CaseDetail` | 單次就診的頁籤、連結與原始頁面。 |
| `prq.soap` | `get_soap(case: VisitCase) -> SoapRecord` | 該次門診的 SOAP；可交給本地關鍵字／正則搜尋，目前僅支援 O 類別。 |
| `prq.numeric` | `get_numeric_report(case: VisitCase) -> NumericReport` | 該次就診的數值表格；保留原始單位及欄位。 |
| `prq.consults` | `get_consults(case: VisitCase) -> list[ConsultRecord]` | 該次就診的會診紀錄；合法空清單不視為例外。 |
| `prq.treatments` | `get_treatments(case: VisitCase) -> list[TreatmentRecord]` | 該次就診的處置／治療清單。 |
| `prq.numeric_history` | `get_numeric_history(mrn: str, filter: NumericHistoryFilter) -> NumericHistoryReport` | 指定期間的數值結果；與單次就診查詢分開。 |
| `prq.surgery_history` | `get_surgery_history(mrn: str, filter: SurgeryHistoryFilter) -> list[PatientSurgeryRecord]` | 病人的歷史手術清單及手術 PDF 參照／解析問題。 |
| `prq.allergy` | `get_allergy(mrn: str) -> dict[str, Any]` | 病人的過敏旗標／資料，保留來源 JSON。 |
| `prq.advance_directives` | `get_advance_directives(mrn: str) -> dict[str, Any]` | 預立醫療意願相關旗標／資料。 |
| `prq.research_flags` | `get_research_flags(mrn: str) -> dict[str, Any]` | 研究／臨床試驗相關旗標。 |
| `prq.bed_transfers` | `get_bed_transfers(mrn: str) -> dict[str, Any]` | 轉床及床位異動歷史。 |
| `prq.care_cases` | `get_care_cases(mrn: str) -> dict[str, Any]` | 照護個案相關清單。 |
| `prq.upload_history` | `get_upload_history(mrn: str, main_type: str = , days: str = *) -> UploadHistory` | 上傳文件清單與 PDF 參照；可按類型及天數查詢。 |
| `prq.upload_types` | `get_upload_types() -> list[dict[str, str]]` | 上傳文件類型目錄，供 upload_history 使用。 |
| `prq.text_report_history` | `get_text_report_history(mrn: str, department: str, days: int = 3650) -> TextReportHistory` | 各科報告清單；PATH／RAD／CHK 與醫囑路徑分開。 |
| `prq.text_report` | `get_text_report(ref: OrderReportRef) -> OrderReport` | 讀取各科報告的正文與附件參照；眼科完整檢查宜由醫囑路徑發現。 |
## sdk.orders

| 操作 ID | Service 呼叫及回傳 | 用途 |
| --- | --- | --- |
| `prq.case_orders` | `get_case_orders(case: VisitCase) -> list[ClinicalOrder]` | 該次就診的醫囑；參照物件可串接明細及報告。 |
| `prq.order_history` | `get_order_history(mrn: str, filter: OrderHistoryFilter) -> list[ClinicalOrder]` | 跨次就診醫囑；可依日期、期間及類別選擇。 |
| `prq.order_detail` | `get_order_detail(ref: OrderDetailRef) -> OrderDetail` | 醫囑細部欄位及可繼續查詢的報告參照。 |
| `prq.order_report` | `get_order_report(ref: OrderReportRef) -> OrderReport` | 醫囑路徑的文字、PDF 及 JPG 參照；正文狀態另行判定。 |
| `prq.pacs_study` | `get_pacs_study(ref: PacsStudyRef) -> PacsStudy` | 開啟 JPG 檢視資料，列出圖片；按鈕存在仍可能回傳空清單。 |
| `prq.pacs_image` | `download_pacs_image(ref: PacsImageRef) -> BinaryAsset` | 下載一張 JPG，驗證格式及完整結尾。 |
| `prq.pdf_attachment` | `download_pdf(ref: PdfAttachmentRef) -> BinaryAsset` | 下載一個 PRQ PDF 附件；共用於檢查、上傳文件及病人歷史手術。 |
## sdk.medications

| 操作 ID | Service 呼叫及回傳 | 用途 |
| --- | --- | --- |
| `prq.case_medications` | `get_case_medications(case: VisitCase) -> list[MedicationOrder]` | 該次就診的藥囑與用法資料。 |
| `prq.medication_history` | `get_medication_history(mrn: str, filter: MedicationHistoryFilter) -> list[MedicationOrder]` | 指定期間的藥囑歷史。 |
## sdk.surgery

| 操作 ID | Service 呼叫及回傳 | 用途 |
| --- | --- | --- |
| `oppl.surgery_schedule` | `get_schedule(card_no: str, start: date, end: date, **filters: str) -> list[SurgeryRecord]` | 醫師在日期區間內的手術排程，包含狀態與來源欄位。 |
| `oppl.patient_info` | `get_patient_info(mrn: str) -> dict[str, Any]` | 手術系統的病人資料與現有排程，可提供開啟表單所需欄位。 |
| `oppl.patient_consents` | `get_patient_consents(mrn: str) -> dict[str, Any]` | 病人既有同意書清單／狀態。 |
| `oppl.request_numbers` | `get_request_numbers(mrn: str) -> dict[str, Any]` | 病人相關申請單號，供排程／同意書組合使用。 |
| `oppl.anticoagulants` | `get_anticoagulants(mrn: str) -> dict[str, Any]` | 手術頁面的抗凝血用藥查詢；不推論停藥決策。 |
| `oppl.sglt2` | `get_sglt2(mrn: str) -> dict[str, Any]` | 手術頁面的 SGLT2 用藥查詢。 |
| `oppl.procedure_catalog` | `get_procedure_catalog() -> dict[str, Any]` | 手術術式／代碼目錄。 |
| `oppl.holidays` | `get_holidays() -> dict[str, Any]` | 排程使用的假日資料。 |
| `oppl.consent_catalog` | `get_consent_catalog() -> dict[str, Any]` | 依科別分類的同意書範本目錄。 |
| `oppl.consent_doctor` | `resolve_consent_doctor(card_no: str) -> dict[str, Any]` | 將卡號解析成同意書使用的醫師資訊。 |
| `oppl.consent_template` | `get_consent_template(name: str, department: str) -> dict[str, Any]` | 讀取指定科別／名稱的同意書範本資料。 |
| `oppl.schedule_form` | `open_schedule_form(fields: Mapping[str, Any], *, mode: str = edit) -> FormSnapshot` | 取得既有排程表單快照；不儲存排程，不能直接把快照當異動命令。 |
| `oppl_records.departments` | `get_case_departments() -> list[str]` | 已完成手術案例查詢可用的科別。 |
| `oppl_records.cases` | `get_cases(filter: SurgeryCaseFilter) -> list[SurgeryCase]` | 依執刀／指導／助手、術式代碼、科別及期間查詢手術案例。 |
| `oppl_records.note` | `get_record_ref(ref: SurgeryCaseRef) -> SurgeryNoteRef &#124; None` | 由 SurgeryCase.reference 取得該次手術紀錄 PDF 位置；無紀錄可回 None。 |
| `oppl_records.pdf` | `download_record(ref: SurgeryNoteRef) -> BinaryAsset` | 下載手術案例查詢系統的 PDF；與 PRQ 附件為不同入口。 |
## sdk.audit

| 操作 ID | Service 呼叫及回傳 | 用途 |
| --- | --- | --- |
| `audit.unsigned_records` | `get_unsigned_records(doctor: str, start: date, end: date) -> list[UnsignedRecord]` | 醫師指定期間的未完成／未簽病歷。 |
## sdk.reviews

| 操作 ID | Service 呼叫及回傳 | 用途 |
| --- | --- | --- |
| `review.login_info` | `get_login_info() -> ReviewLoginInfo` | 取得審查登入身分及登入方式；可確認是否沿用 Portal Session。 |
| `review.options` | `get_options() -> dict` | 審查狀態、科別、模式等查詢選項。 |
| `review.doctors` | `get_doctors(department: str) -> list[dict]` | 指定科別可選的醫師目錄。 |
| `review.cases` | `get_cases(filter: ReviewCaseFilter) -> list[ReviewCase]` | 依醫師／病人／日期／審查條件查詢全部案件清單。 |
| `review.case_detail` | `get_case(ref: ReviewCaseRef) -> ReviewCase` | 單一案件主資料與審查結果；送件完成和審查同意為不同欄位。 |
| `review.orders` | `get_orders(ref: ReviewCaseRef) -> ReviewCasePart` | 該審查案件的醫囑明細。 |
| `review.attachments` | `get_attachments(ref: ReviewCaseRef) -> ReviewCasePart` | 該審查案件的附件清單／資訊；不下載附件本體。 |
| `review.pacs` | `get_pacs(ref: ReviewCaseRef) -> ReviewCasePart` | 該審查案件的 PACS 清單／資訊；不下載圖片本體。 |
## sdk.personnel

| 操作 ID | Service 呼叫及回傳 | 用途 |
| --- | --- | --- |
| `personnel.options` | `get_options() -> PersonnelOptions` | 人事查詢可用的職稱／單位代碼與名稱，依當次表單讀取。 |
| `personnel.search` | `search(filter: PersonnelFilter) -> list[PersonnelRecord]` | 依姓名、員工編號、職稱、單位及下層單位條件查詢人事清單；保留醫師章號及聯絡欄位。見 [PERSONNEL](PERSONNEL.md)。 |

## 登入、報表與額外入口

| 入口 | 用途 |
| --- | --- |
| `sdk.auth.login()` | 建立 Portal Session；一般查詢會按需登入。 |
| `sdk.auth.check(only=[...])` | 檢查登入／子系統 SSO；不代表已有查詢資料。 |
| `sdk.connection_status()` | 查看每個服務實際選擇的 TLS／憑證驗證及連線確認狀態；不發出請求，不含帳密。 |
| `sdk.configure_connection(app, tls_profile=...)` | 進階覆寫指定服務的 TLS；一般使用已有自動相容與恢復，見 [CONNECTIONS](CONNECTIONS.md)。 |
| `sdk.earnings.open_performance(credentials)` | 二次身分驗證並取得績點報表月份表單 context。 |
| `sdk.earnings.open_bonus(credentials)` | 取得專勤工作獎金 context；此功能不代表完整薪資系統。 |
| `sdk.earnings.get_report(context, period=None)` | 取得所選月份的 HtmlDocument；表單可選月份是唯一允許值。 |
| `sdk.records.download_surgery_record(ref)` | PRQ 歷史手術 PDF 的語意入口，使用相同 PDF 附件下載。 |
| `sdk.records.find_visit_cases(mrn=None, visit_filter=..., national_id=None)` | 查一位病人的就診清單並套用 VisitFilter；病歷號／身分證二擇一。 |
| `VisitFilter(...).select(cases)` | 對已取得清單依日期、類別、科別、醫師篩選、去重、排序，不發 HTTP。 |
| `sdk.personnel.get_by_card(card_no)` | 用員工帳號或已知四碼帳號 + F 別名查找精確匹配；回 PersonnelRecord 或 None；其 name 可接 VisitFilter.doctor_names，不使用醫師章號作為帳號。 |
| `sdk.surgery.get_supply_model(key, department)` | 依材料／範本鍵查詢供應模型；未列入預設抽樣。 |
| `sdk.surgery.open_consent_form(fields)` | 讀取同意書表單快照；不提交。 |

MIS 使用 `EarningsCredentials(national_id, password)`，與 Portal 帳密分開。
重新登入後舊 EarningsReportContext 失效，需再次開啟報表。

## 明確提交的異動功能

`surgery.prepare_command(action, reviewed_fields)` 只在本機驗證並建立 SurgeryCommand。
`create_schedule`、`edit_schedule`、`cancel_schedule`、`create_consent` 接受該 command 後才會送出異動。
取消必須有原因；動態表單欄位需由呼叫端確認。測試 EXE 不會執行這四項。
收到 ACKNOWLEDGED 代表伺服器回覆已辨識，仍可讀回確認結果。
MUTATION_OUTCOME_UNKNOWN 必須先讀回查證，禁止自動重送；跨程序沒有伺服器冪等保證。

## 回傳值與錯誤

清單空值、參照 None、SDKError 與未知頁面分開處理；不要把例外轉成空清單。
`OrderReport.report_data_status` 區分 TEXT_AVAILABLE、ATTACHMENT_ONLY、METADATA_ONLY、EMPTY。
`HtmlDocument` 保留 html、text、tables、rendering_notes；巢狀表格保留主表自己的列。
PDF/JPG 以 BinaryAsset 回傳；取得二進位不表示已做 OCR 或醫療數值抽取。
`to_jsonable` 只轉成可儲存結構，不會去除個資。
SDKError.info 提供 code/category/operation/app；診斷錯誤欄位與完整 raw capture 用途不同。

[組合方式](COMPOSITION.md) · [架構](ARCHITECTURE.md) · [新增 HAR 功能](HAR_RECORDING.md)
