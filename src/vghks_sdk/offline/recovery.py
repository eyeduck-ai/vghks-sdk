"""Verify recorded application recovery using one complete SDK operation."""

from __future__ import annotations

import re
from collections import defaultdict
from urllib.parse import urlsplit

from .bundle import BundleReader
from .replay import request_parts

_READS = {
    "get_patient_basic_info": ("webmaas.basic_info", "webmaas.basic_info_landing", "maas_QRY15"),
    "get_patient_demographics": ("webmaas.demographics", "webmaas.registration_landing", "maas_RSV11"),
    "get_registration_history": ("webmaas.registration_query", "webmaas.registration_landing", "maas_RSV11"),
}
_CAPTURE_ID = re.compile(r"\d{6}", re.ASCII)
_OPERATION_ID = re.compile(r"op-\d{6,12}", re.ASCII)


def verify_webmaas_recoveries(reader: BundleReader, rows: list[dict]) -> list[dict]:
    """Mark only the timeout preceding a proven SSO and successful read.

    Counters in the two recorders are independent. New events link the raw
    operation explicitly; older events require an unambiguous match of the
    complete HTTP request sequence. A later success alone is insufficient.
    """
    traces = reader.jsonl("diagnostics/diagnostics.jsonl")
    by_operation = defaultdict(list)
    for index, event in enumerate(traces):
        operation_id = event.get("operation_id")
        if type(operation_id) is int and operation_id > 0:
            by_operation[operation_id].append((index, event))
    captures = defaultdict(list)
    for capture in reader.jsonl("capture_manifest.jsonl"):
        operation_id, capture_id = capture.get("sdk_operation_id"), capture.get("capture_id")
        if (isinstance(operation_id, str) and _OPERATION_ID.fullmatch(operation_id)
                and isinstance(capture_id, str) and _CAPTURE_ID.fullmatch(capture_id)
                and capture.get("kind") in {"HTTP_EXCHANGE", "NETWORK_ERROR"}):
            captures[operation_id].append(capture)
    replay = {row["capture_id"]: row for row in rows}
    results = []
    for operation_id, events in by_operation.items():
        attempts = [(i, e) for i, e in events if e.get("event") == "application_session_recovery_started"
                    and e.get("application") == "webmaas" and e.get("recovery") == "APPLICATION_SSO"
                    and (e.get("issue") or {}).get("code") == "WEBMAAS_SESSION_TIMEOUT"]
        if not attempts:
            continue
        starts = [(i, e) for i, e in events if e.get("event") == "operation_started"]
        finishes = [(i, e) for i, e in events if e.get("event") == "operation_finished"]
        name = starts[0][1].get("operation") if len(starts) == 1 else ""
        if name not in _READS:
            continue
        query, landing, role = _READS[name]
        result = {"status": "UNVERIFIED", "operation": query,
                  "diagnostic_operation_id": operation_id, "capture_operation_id": "",
                  "timeout_capture_id": "", "sso_form_capture_id": "", "result_capture_id": "",
                  "correlation": "UNVERIFIED"}
        results.append(result)
        if len(attempts) != 1 or len(finishes) != 1:
            continue
        start_index, start = starts[0]
        recovery_index, attempt = attempts[0]
        finish_index, finish = finishes[0]
        if not (start_index < recovery_index < finish_index and finish.get("operation") == name
                and start.get("application") == "webmaas"):
            continue
        if finish.get("status") == "ERROR":
            result["status"] = "FAILED"
            continue
        if finish.get("status") != "OK" or any(
            e.get("event") == "reauthentication_started" for _, e in events
        ):
            continue
        requests = [(i, e) for i, e in events if e.get("event") == "http_request"]
        if not requests or any(not start_index < i < finish_index for i, _ in requests):
            continue
        shape = [(e.get("method"), e.get("path")) for _, e in requests]
        before = sum(i < recovery_index for i, _ in requests)
        if not 0 < before < len(requests):
            continue
        explicit = attempt.get("capture_operation_id")
        if explicit is not None and (not isinstance(explicit, str) or not _OPERATION_ID.fullmatch(explicit)):
            continue
        if explicit is not None and finish.get("capture_operation_id") != explicit:
            continue
        candidates = []
        for raw_id, records in captures.items():
            if explicit is not None and raw_id != explicit:
                continue
            if any(c.get("sdk_operation") != name for c in records):
                continue
            # One initial record per transport attempt, with redirect hops
            # retained in the same group. Retry ambiguity stays unverified.
            groups = []
            for record in sorted(records, key=lambda c: c["capture_id"]):
                group = record.get("request_group_id")
                if not isinstance(group, str) or not re.fullmatch(r"req-\d{6,12}", group, re.ASCII):
                    groups = []
                    break
                if not groups or groups[-1][0] != group:
                    groups.append((group, []))
                groups[-1][1].append(record)
            raw_shape = [(g[0].get("request", {}).get("method"),
                          urlsplit(g[0].get("request", {}).get("url", "")).path) for _, g in groups]
            if raw_shape == shape:
                candidates.append((raw_id, records, groups))
        if len(candidates) != 1:
            continue
        raw_id, records, groups = candidates[0]
        result.update(capture_operation_id=raw_id,
                      correlation="EXPLICIT_OPERATION_LINK" if explicit is not None else "LEGACY_REQUEST_SEQUENCE")
        failed_group = groups[before - 1][1]
        timeout = replay.get(failed_group[-1]["capture_id"], {})
        if timeout.get("error_code") != "WEBMAAS_SESSION_TIMEOUT":
            continue
        result["timeout_capture_id"] = timeout["capture_id"]
        subsequent = [c for _, group in groups[before:] for c in group]
        if any(replay.get(c["capture_id"], {}).get("error_code")
               and not replay[c["capture_id"]].get("recovered") for c in subsequent):
            continue
        # Prove the fresh bridge, original role, valid token form and actual
        # query result, in this order and in this same raw operation.
        bridge = [c for c in subsequent if replay.get(c["capture_id"], {}).get("operation") == "sectord.key_bridge"
                  and 200 <= (c.get("response") or {}).get("status_code", 0) < 300]
        sso = [c for c in subsequent if replay.get(c["capture_id"], {}).get("operation") == "webmaas.sso_logon"]
        forms = [c for c in subsequent if replay.get(c["capture_id"], {}).get("operation") == landing
                 and replay[c["capture_id"]].get("status") == "CONTRACT_OK"]
        values = [c for c in subsequent if replay.get(c["capture_id"], {}).get("operation") == query
                  and replay[c["capture_id"]].get("status") in {"PARSED", "EMPTY"}]
        if len(bridge) != 1 or len(sso) != 1 or not forms or not values:
            continue
        _, _, params, _ = request_parts(sso[0].get("request") or {})
        target = urlsplit(params.get("targetURL", ""))
        form_url = urlsplit((forms[0].get("response") or {}).get("url", ""))
        timeout_url = urlsplit((failed_group[-1].get("response") or {}).get("url", ""))
        sso_url = urlsplit((sso[0].get("request") or {}).get("url", ""))
        origin = (timeout_url.scheme.lower(), timeout_url.netloc.lower())
        if (params.get("externalRoles") != role
                or any((u.scheme.lower(), u.netloc.lower()) != origin for u in (sso_url, target, form_url))
                or target.path != form_url.path):
            continue
        ids = [timeout["capture_id"], bridge[0]["capture_id"], sso[0]["capture_id"],
               forms[0]["capture_id"], values[-1]["capture_id"]]
        if ids != sorted(set(ids)) or any(
            replay.get(c["capture_id"], {}).get("operation") == "portal.login" for c in records
        ):
            continue
        result.update(status="RECOVERED", sso_form_capture_id=forms[0]["capture_id"],
                      result_capture_id=values[-1]["capture_id"])
        timeout.update(recovered=True, recovery_type="WEBMAAS_SSO", recovery_operation_id=raw_id)
    return results
