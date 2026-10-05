"""Read-only Session evidence, readiness selection and parsed-value comparisons."""

from __future__ import annotations

from ..contracts.auth_evidence import in_capture_range
from .bundle import BundleReader


def session_test_summary(reader: BundleReader, steps: list[dict], rows: list[dict],
                          application_recoveries: list[dict] | None = None) -> dict:
    """Keep cookie/idle observations separate from retained failures and TTL."""
    path = "parsed/session/comparison.json"
    if path not in reader.names:
        return {"status": "NOT_RUN"}
    source = reader.json(path)
    challenge = source.get("challenge") or {}
    kind = challenge.get("evidence")
    kind = kind if kind in {"LOCAL_COOKIE_LOSS", "MANUAL_IDLE", "NOT_TESTED"} else "UNKNOWN"
    rechecks = []
    for phase in ("after_cookie_loss", "after_idle"):
        step = next((s for s in steps if s.get("name") == f"session.{phase}.sso_recheck"), None)
        if step is not None:
            # A later success outside this exact step is not recovery evidence.
            verified = step.get("status") == "OK" and any(
                row["operation"] in {"webmaas.registration_landing", "webmaas.basic_info_landing"}
                and row["status"] == "CONTRACT_OK" and in_capture_range(row, step) for row in rows
            )
            rechecks.append({"phase": phase, "status": step.get("status") if
                             step.get("status") in {"OK", "ERROR", "BLOCKED"} else "UNKNOWN",
                             "query_form_verified_in_capture_range": verified})
    equality = {}
    for key in ("webmaas.demographics", "webmaas.basic_info"):
        baseline = f"parsed/session/baseline/{key}.json"
        after = f"parsed/session/after_cookie_loss/{key}.json"
        if baseline in reader.names and after in reader.names:
            before_value, after_value = reader.json(baseline), reader.json(after)
            # HTML may change its SSO token even when all parsed values agree.
            equality[key] = ({k: v for k, v in before_value.items() if k != "raw_html"} ==
                             {k: v for k, v in after_value.items() if k != "raw_html"})
    direct = next((s for s in steps if s.get("name") ==
                   "session.direct_after_cookie_loss.webmaas.basic_info"), None)
    direct_equality = None
    baseline_path = "parsed/session/baseline/webmaas.basic_info.json"
    direct_path = "parsed/session/direct_after_cookie_loss/webmaas.basic_info.json"
    if baseline_path in reader.names and direct_path in reader.names:
        before, after = reader.json(baseline_path), reader.json(direct_path)
        direct_equality = ({k: v for k, v in before.items() if k != "raw_html"} ==
                           {k: v for k, v in after.items() if k != "raw_html"})
    verified = next((r for r in application_recoveries or [] if r["status"] == "RECOVERED"
                     and r["operation"] == "webmaas.basic_info" and direct
                     and in_capture_range({"capture_id": r["timeout_capture_id"]}, direct)
                     and in_capture_range({"capture_id": r["result_capture_id"]}, direct)), None)
    direct_status = direct.get("status") if direct else None
    recovery_status = ("NOT_TESTED" if direct is None else
                       "FAILED" if direct_status in {"ERROR", "BLOCKED"} else
                       "VERIFIED" if direct_status == "OK" and verified else
                       "UNVERIFIED" if any(row["error_code"] == "WEBMAAS_SESSION_TIMEOUT"
                                           and in_capture_range(row, direct) for row in rows) else
                       "NOT_OBSERVED")
    if direct is None and (source.get("direct_read_after_cookie_loss") or {}).get("status") == "NO_SAMPLE":
        recovery_status = "NO_SAMPLE"
    return {
        "status": "OBSERVED", "challenge": kind,
        "natural_ttl_verified": False, "original_failures_retained": True,
        "timeout_responses": [{"capture_id": row["capture_id"], "operation": row["operation"]}
                              for row in rows if row["error_code"] == "WEBMAAS_SESSION_TIMEOUT"],
        "sso_rechecks": rechecks, "parsed_patient_values_equal": equality,
        "direct_read_status": direct["status"] if direct and direct.get("status") in
                              {"OK", "ERROR", "BLOCKED"} else "NO_SAMPLE" if
                              recovery_status == "NO_SAMPLE" else "NOT_TESTED",
        "direct_patient_values_equal": direct_equality,
        "direct_api_recovery": {"status": recovery_status,
                                "timeout_capture_id": verified["timeout_capture_id"] if verified else "",
                                "result_capture_id": verified["result_capture_id"] if verified else ""},
    }


def recorded_session_readiness(reader: BundleReader, steps: list[dict]) -> dict | None:
    paths = {f"session.{phase}.{check}": f"parsed/session/{phase}/{check}.json"
             for phase in ("baseline", "after_cookie_loss", "after_idle")
             for check in ("auth_check", "sso_recheck")}
    selected = next((s for s in reversed(steps) if s.get("name") in paths), None)
    if selected is None:
        return None
    path = paths[selected["name"]]
    # Unexecuted/stale files cannot replace the latest recorded check. If it
    # threw before returning a report, earlier readiness is not current proof.
    if selected.get("status") not in {"OK", "ERROR"} or path not in reader.names:
        return {}
    report = reader.json(path)
    return report if isinstance(report, dict) and isinstance(report.get("targets"), list) else {}
