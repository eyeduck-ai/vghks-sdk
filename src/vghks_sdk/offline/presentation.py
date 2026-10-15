"""Render and compare already assembled offline reports; no bundle access."""

# ruff: noqa: RUF001

from __future__ import annotations

from typing import Any


def compare_reports(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    old = {row["operation"]: row["live_status"] for row in before["operations"]}
    new = {row["operation"]: row["live_status"] for row in after["operations"]}
    return {
        "previous_build": before["recorded_build"],
        "newly_verified": [
            key for key in new if new[key] == "VERIFIED" and old.get(key) != "VERIFIED"
        ],
        "regressions": [
            key
            for key in new
            if old.get(key) == "VERIFIED" and new[key] in {"FAILED", "BLOCKED", "MISSING"}
        ],
        "not_retested": [
            key for key in new if old.get(key) == "VERIFIED" and new[key] == "NOT_TESTED"
        ],
        "note": "Different dates, filters and changing clinical data can affect comparisons.",
    }


def _markdown(report: dict[str, Any]) -> str:
    root = report["root_cause"]
    lines = [
        "# 內網測試回傳分析",
        "",
        f"- 原執行狀態：`{report['bundle_status']}`",
        f"- 分析狀態：`{report['analysis_status']}`",
        f"- 封裝讀取狀態：`{report['bundle']['status']}`",
        f"- 原 EXE／SDK：`{report['recorded_build']['sdk_version']}`",
        f"- 本機分析器：`{report['analyzer_build']['sdk_version']}`",
        f"- 主要問題：`{root['code'] if root else '未發現錯誤'}`",
        "",
        report["next_action"],
        "",
    ]
    session = report.get("session_test", {})
    if session.get("status") == "OBSERVED":
        lines += [
            f"WebMAAS Session 情境：{session['challenge']}；辨識到 {len(session['timeout_responses'])} 份明確 timeout 回應。",
            f"獨立 SSO 複查：{session['sso_rechecks']}；直接 API 讀取：{session['direct_read_status']}。",
            f"直接 API 的 Runtime SSO 恢復證據：{session['direct_api_recovery']['status']}；"
            f"直接查詢與基準的結構化資料一致：{session['direct_patient_values_equal']}。",
            f"前後結構化病人欄位一致：{session['parsed_patient_values_equal']}（排除整份動態 raw HTML）。",
            "已證實恢復的 timeout 另列 recovered_requests；未恢復及原 readiness 錯誤仍保留。",
            "原始失敗仍保留；Cookie 遺失不證明自然閒置 TTL。", "",
        ]
    password = report.get("password_change_test")
    if password:
        lines += [
            f"舊帳密登入：{password['login_status']}；帳密判斷：{password['credential_validity']}；"
            f"原因碼：{password['login_error_code']}。",
            f"額外密碼頁擷取：{password['change_page']['status']}；"
            f"SDK 基本資料讀取：{password['patient_read_status']}。",
            f"匿名目錄可讀：{password['anonymous_catalog_accepted']}；"
            f"既有 Cookie 的目錄可讀：{password['existing_cookie_catalog']['query_accepted']}。",
            "目錄可讀不證明帳密有效或可讀所有病歷；未送出密碼變更、未測自然 TTL，原登入錯誤仍保留。", "",
        ]
    if report["no_sample_steps"]:
        lines += [
            "缺少樣本的檢查（不列為執行錯誤）：",
            "",
            "| 步驟序號 | 檢查／操作 | 原因碼 |",
            "| ---: | --- | --- |",
        ]
        lines.extend(
            f"| {row['step_index']} | {row['step']} | {row['reason_code']} |"
            for row in report["no_sample_steps"]
        )
        lines.append("")
    pacs = report.get("pacs_test", {})
    if pacs.get("status") not in {None, "NOT_RUN"}:
        account = pacs["account"]
        lines += [
            f"PACS 圖片核對：{pacs['status']}；"
            f"本院目標 {account.get('hospital', '未指定')}.{account.get('expected_mode', '未指定')} = {pacs['expected_mode_status']}。",
            "", "| 格式 | 原判定 | 存檔核對 | 參照數 | 通過核對的檔案 | 缺少下載 | 下載錯誤 |",
            "| --- | --- | --- | ---: | ---: | ---: | ---: |",
        ]
        lines.extend(
            f"| {mode} | {row['recorded_status']} | {row['status']} | {row['references']} | "
            f"{row['verified_files']} | {row['missing_downloads']} | {row['download_errors']} |"
            for mode, row in pacs["modes"].items()
        )
        lines += ["", "另一格式缺樣本不代表本院目標下載失敗；目標缺樣本、上游失敗及存檔不完整分別保留。"
                  "核對參照、outcome 與實際檔案，不只依摘要計數；沒有 OCR 或完整圖片解碼。", ""]
    policy = report["password_policy"]
    if policy["offline_observations"]:
        lines += [
            "原始回應的密碼政策重解析（保留原 EXE 觀察，未重送登入）：",
            "",
            "| 回應編號 | 狀態 | 剩餘日數 | 證據 |",
            "| --- | --- | ---: | --- |",
        ]
        lines.extend(
            f"| {row['capture_id']} | {row['status']} | {row['remaining_days']} | {row['evidence']} |"
            for row in policy["offline_observations"]
        )
        lines += ["", "此結果不證實自然 Session 過期或之後強制變更密碼的實際回應。", ""]
    quality = report["data_quality"]
    if quality["recorded_partial_steps"] or quality["offline_partial_responses"] or quality["offline_warnings"]:
        lines += [
            f"原 EXE 標記部分解析 {len(quality['recorded_partial_steps'])} 步；"
            f"新版離線重解析仍不完整 {len(quality['offline_partial_responses'])} 份回應，"
            f"另有 {len(quality['offline_warnings'])} 份回應保留可恢復警示。",
            "原始表頭問題碼仍留在模型；PDF／JPG 參照不表示已取得或解析附件正文。",
            "",
        ]
    lines += ["| 登入／SSO 目標 | 實測狀態 | 錯誤碼 |", "| --- | --- | --- |"]
    lines.extend(
        f"| {row['target']} | {row['status']} | {row['error_code']} |"
        for row in report["authentication"]
    )
    counts = report["query_summary"]
    content = report["report_content"]
    lines += [
        "",
        "報告正文與 PDF／JPG 下載分開驗證：",
        "",
        "| 正文狀態 | 原實測步驟 | 本機重解析回應 |",
        "| --- | ---: | ---: |",
    ]
    lines.extend(
        f"| {status} | {count} | {content['offline_responses'][status]} |"
        for status, count in content["live_steps"].items()
    )
    lines += [
        "",
        "TEXT_AVAILABLE 表示已擷取可供後續使用的報告文字，不要求 PDF 下載成功。ATTACHMENT_ONLY／METADATA_ONLY 仍未取得正文；UNASSESSED 表示該版本未獨立判定。",
        "整體測試仍保留其他操作與附件的錯誤；本機重解析不改寫原始實測結果，也不保證附件中的所有內容均已擷取。",
        "",
    ]
    eye = report.get("ophthalmology_orders", {})
    if eye.get("status") not in {None, "NOT_RUN"}:
        lines += ["", f"眼科醫囑路徑：{eye['status']}。"]
        lines.extend(f"- {term}：{counts}" for term, counts in eye.get("coverage", {}).items())
        lines += [
            "",
            "未執行醫囑只記錄略過；JPG 查無資料是有效空結果。PDF/JPG 成功下載不等於已抽取文字或數值；各筆來源、正文與附件位於 parsed/workflows/ophthalmology_orders/。",
            "",
        ]
        assets = report.get("order_assets", {})
        lines += [
            f"附件存檔核對：{assets.get('status', 'NOT_RUN')}；"
            f"PDF {assets.get('pdf_files', 0)}、JPG {assets.get('jpg_files', 0)}，"
            f"問題 {len(assets.get('issues', []))}。逐檔核對下載 metadata 的大小、SHA-256 及檔案格式；原始實測狀態維持不變。",
            "",
        ]
    batch = report.get("registration_batch", {})
    if batch.get("status") not in {None, "NOT_RUN"}:
        lines += [
            "", f"批次掛號存檔核對：{batch['status']}；原 EXE 狀態 {batch['recorded_status']}。",
            f"輸入 {batch['requested_patient_count']} 人、完成 {batch['completed_patient_count']} 人、"
            f"阻擋 {batch['blocked_patient_count']} 人、待執行 {batch['pending_patient_count']} 人；"
            f"原頁與逐人存檔一致 {batch['verified_patient_count']} 人。",
            f"掛號 {batch['record_count']} 筆，未到診 {batch['no_show_count']} 筆、到診狀態未知 "
            f"{batch['attendance_unknown_count']} 筆；來源代碼 {batch['distinct_registered_by_code_count']} 種，保留原值且不分類。",
            "沒有未到診圖示維持未知；此核對不查實際就診，也不改寫原實測狀態。", "",
        ]
    opd = report["opd_evidence"]
    if opd["recorded_rows_without_section"]:
        lines += [
            "",
            f"門診清單原解析有 {opd['recorded_rows_without_section']} 筆缺少科別；舊結果不能直接用來區分專屬／共用門診。新版依每筆靜態索引重建科別及診間，以下是同一回應的本機重解析：",
            "",
        ]
    for row in opd["offline_responses"]:
        if row["section_counts"]:
            lines += [
                f"- 回應 {row['capture_id']}：科別筆數 {row['section_counts']}；缺病歷號 {row['missing_mrn_count']} 筆（保留清單，不能進行病歷查詢）。"
            ]
    workflow = report["weekly_opd_workflow"]
    physician_review = report["weekly_physician_review"]
    if physician_review["status"] == "RECLASSIFIED":
        lines += [
            "",
            "依新版醫師標示規則重算門診清單（不改寫原實測就診／SOAP 結果）：",
            f"- 清單分類：{physician_review['counts']}",
            f"- 專屬門診科別分布：{physician_review['dedicated_section_counts']}",
            f"- 專屬門診病人 {physician_review['dedicated_patients']} 人；缺病歷號 {physician_review['dedicated_missing_mrn']} 筆。",
            f"- 比原分類新增 {physician_review['additional_registrations']} 筆掛號、{physician_review['additional_patients']} 位病人；新增掛號須補足當日就診／SOAP 驗證。",
            "",
        ]
    lines += ["", f"最近七天門診／當日就診／SOAP 組合測試：{workflow['status']}。", ""]
    if workflow["counts"]:
        lines += [f"- {key}: {value}" for key, value in workflow["counts"].items()]
        lines += [
            "",
            "三階段資料與命中清單在 ZIP 的 parsed/workflows/opd_soap_week/。INCOMPLETE 代表仍有未知結果，不能當成完整陰性結果。",
            "",
        ]
    lines += [
        "",
        f"{len(report['operations'])} 個原子查詢：成功取得非空資料 {counts['VERIFIED']}、空結果 {counts['EMPTY']}、"
        f"執行失敗 {counts['FAILED']}、相依阻擋 {counts['BLOCKED']}、"
        f"缺輸入 {counts['MISSING']}、未測 {counts['NOT_TESTED']}。",
        "",
    ]
    if report["authentication"][0]["status"] == "OK":
        lines += ["入口登入與 Session 已通過；這不代表所有子系統 SSO 或資料查詢都已通過。", ""]
    if report["connection_profiles"]:
        lines += [
            "本輪選用的 HTTPS 模式（連線成功與登入／資料查詢分開判定）：",
            "",
            "| 服務 | 模式 | 實際 TLS | 實際套件 | 憑證驗證 | 已套用 |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        lines.extend(
            f"| {row['target']} | {row['profile']} | {row['protocol']} | {row['cipher']} | "
            f"{row['certificate_verification']} | {row['applied']} |"
            for row in report["connection_profiles"]
        )
        lines += [
            "",
            "HTTPS 與 TLS 加密仍保留。certificate_verification=False 才代表略過憑證驗證；HTTP 404 是收到伺服器回應後的應用程式問題。",
            "",
        ]
    for evidence in report["portal_login_evidence"]:
        lines.extend(
            [
                f"登入證據：HTTP {evidence['http_status']}，回應 {evidence['response_bytes']} bytes，"
                f"僅空白：{evidence['response_blank']}；登入前已取登入頁：{evidence['entry_page_requested_before_login']}；"
                f"Origin：{evidence['origin_header_present']}；Referer 指向登入頁：{evidence['referer_is_entry_page']}。",
                "",
            ]
        )
    if report["preflight_findings"]:
        lines.extend(
            [
                "連線探測紀錄（recovered=True 表示同輪已有可用模式套用或登入／SSO 通過；不代表資料查詢成功）：",
                "",
                "| 探測步驟 | 回應編號 | 依原始證據分類 | recovered |",
                "| --- | --- | --- | --- |",
            ]
        )
        lines.extend(
            f"| `{item.get('step', '')}` | `{item['capture_id']}` | `{item['code']}` | {item['recovered']} |"
            for item in report["preflight_findings"]
        )
        if any(item["code"] == "TLS_EOF" for item in report["preflight_findings"]):
            lines.extend(
                [
                    "",
                    "`TLS_EOF` 表示 TLS 連線提早中斷，單靠此訊息無法證實是憑證或加密套件問題。",
                ]
            )
        lines.append("")
    tls12_failed = [
        row["step"].split(".")[1]
        for row in report["connection_trials"]
        if row["step"].endswith(".https_tls12") and row["status"] == "ERROR"
    ]
    if tls12_failed:
        lines += [
            f"本輪已實測 TLS 1.2 仍失敗（預設套件）：{', '.join(tls12_failed)}；後續相容套件的結果見連線模式表。",
            "",
        ]
    if report["certificate_comparisons"]:
        lines += [
            "憑證驗證開／關對照：仍使用 HTTPS；以下探測不帶帳密。是否套用至後續登入／查詢，依本輪設定與探測結果決定。",
            "",
            "| 對照探測 | 開啟驗證 | 略過驗證 |",
            "| --- | --- | --- |",
        ]
        lines.extend(
            f"| `{row['probe']}` | {row['verified_status']} {row['verified_error']} | "
            f"{row['unverified_status']} {row['unverified_error']} |"
            for row in report["certificate_comparisons"]
        )
        lines += [
            "",
            "若只有略過驗證時收到 HTTP 回應，優先檢查憑證信任鏈、有效期與主機名稱；"
            "若略過後仍 TLS_EOF，關閉憑證驗證並未解決本次連線中斷。單次成功差異仍可能受連線波動影響。",
            "",
        ]
    if report["unverified_tls_services"]:
        lines += [
            "本輪後續登入／查詢已設定略過憑證驗證的服務："
            + ", ".join(report["unverified_tls_services"])
            + "。HTTPS 加密保留；各服務是否實際登入及取得資料，仍以登入／SSO 與操作結果為準。",
            "",
        ]
    lines += [
        "`VERIFIED` 只代表這份回傳包中有成功且非空的 Service 結果。離線解析成功不等同內網驗證；`EMPTY`、`NOT_TESTED`、`MISSING` 均不算取得資料的成功證據。",
        "",
        "| 操作 | 內網結果 | 本機解析成功 | 空結果 | 解析／HTTP 錯誤 |",
        "| --- | --- | ---: | ---: | ---: |",
    ]
    lines.extend(
        f"| `{row['operation']}` | {row['live_status']} | {row['replay_parsed']} | {row['replay_empty']} | {row['replay_errors']} |"
        for row in report["operations"]
    )
    if report["earnings_reports"]:
        lines += [
            "",
            "選用的本人績點／專勤工作獎金查詢：",
            "",
            "| 操作 | 內網結果 | 錯誤碼 |",
            "| --- | --- | --- |",
        ]
        lines.extend(
            f"| `{row['operation']}` | {row['live_status']} | {row['error_code']} |"
            for row in report["earnings_reports"]
        )
    lines += [
        "",
        "重測設定：`retest-config.json`（不含帳密；使用新 Session 重查，不沿用舊 Cookie）。",
        "",
        "本報告不輸出病歷內容、請求值、帳密或 Cookie；原始回傳包仍保留在原處。",
        "",
    ]
    return "\n".join(lines)
