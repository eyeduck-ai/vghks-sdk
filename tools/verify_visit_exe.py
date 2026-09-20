"""Exercise the incremental frozen EXE against synthetic localhost HTTPS only."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests"), str(ROOT / "tools")]

from test_tls import LegacyAesLoopbackTests  # noqa: E402
from test_visit_search import (  # noqa: E402
    CONTEXT,
    EMPTY_LIST,
    patient_header,
    visit_page,
    visit_row,
)
from verify_patient_exe import PatientIntranet  # noqa: E402

from vghks_sdk import PortalCredentials, RequestPolicy, SDKSettings, VghksSDK  # noqa: E402
from vghks_sdk.offline.analyze import inspect_bundle  # noqa: E402
from vghks_sdk.offline.bundle import BundleReader  # noqa: E402

MRN = "00000000"
IDENTIFIER = "SYNTHETIC-ID"


def test_state(mode, origin):
    return {
        "mode": mode,
        "origin": origin,
        "reject_login": False,
        "login_posts": 0,
        "queried_mrns": set(),
        "keys": 0,
        "role": "",
        "roles": [],
        "token_number": 0,
        "token": "",
        "query_posts": 0,
        "next_pages": 0,
        "break_basic": False,
        "paths": [],
        "lookups": [],
        "soap_cases": [],
        "order_cases": [],
    }


class VisitIntranet(PatientIntranet):
    def dispatch(self):
        path = urlsplit(self.path).path
        state = self.server.test_state
        state["paths"].append(path)
        handled = {
            "/PRQWeb/QueryPatientRecord.do",
            "/PRQWeb/Page/JSP/KS_Patient.jsp",
            "/PRQWeb/QueryCaseList.do",
            "/PRQWeb/QueryCaseDetail.do",
            "/PRQWeb/QueryBillingSOAP.do",
            "/PRQWeb/QueryOrderResult.do",
        }
        if path not in handled:
            return super().dispatch()
        params = dict(parse_qsl(urlsplit(self.path).query, keep_blank_values=True))
        if self.command == "POST":
            params.update(
                parse_qsl(
                    self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode(),
                    keep_blank_values=True,
                )
            )
        if path.endswith("QueryPatientRecord.do"):
            state["lookups"].append(params["type"])
            if params["type"] == "2":
                assert params["id"] == params["queryID"] == IDENTIFIER
                assert params["queryPtID"] == ""
                if state["mode"] == "id_and_soap_failure":
                    return self.reply('<script>alert("synthetic unknown response")</script>')
            else:
                assert params["id"] == params["queryPtID"] == MRN
            state["mrn"] = MRN
            return self.reply(CONTEXT)
        if path.endswith("KS_Patient.jsp"):
            return self.reply(patient_header(MRN, IDENTIFIER))
        if path.endswith("QueryCaseList.do"):
            if state["mode"] == "empty":
                return self.reply(EMPTY_LIST)
            rows = []
            for i, kind in enumerate(("O", "O", "O", "A", "E")):
                fields = {
                    "caseNo": f"C{i}", "caseType": kind,
                    "caseDT": f"2026-01-0{5 - i}", "hidno": IDENTIFIER,
                }
                # Only the active JSP branch carries a card. An EXE with the
                # old first-constructor parser would incorrectly report gaps.
                rows.append(
                    'if ("current" == "legacy") {' + visit_row(**fields)
                    + '} else {' + visit_row(vsNo="D001", **fields) + '}'
                )
            return self.reply(visit_page(*rows))
        assert params["hhisnum"] == MRN, "national ID must never be used as an MRN"
        if path.endswith("QueryCaseDetail.do"):
            assert params["caseType"] == "O"
            assert params["hid"] != "STALE-HID"
            return self.reply('<div id="tabs"><ul id="tab_ul"><li id="soap">SOAP</li></ul></div>')
        if path.endswith("QueryBillingSOAP.do"):
            state["soap_cases"].append(params["caseNo"])
            if state["mode"] == "id_and_soap_failure" and params["caseNo"] == "C0":
                return self.reply("synthetic first SOAP failure", 500)
            return self.reply(
                '<div id="data"><div class="soap"><pre>S: synthetic SOAP</pre></div></div>'
            )
        state["order_cases"].append(params["caseNo"])
        return self.reply(
            "<script>var aryCase=[]; aryCase[0]=new KSCase('', 'Synthetic order', '2026-01-01', '', 'Synthetic doctor', 'Done', '');</script>"
        )


def verify_plain_sdk(helper, origin):
    helper.server.test_state = state = test_state("automatic", origin)
    settings = SDKSettings(
        portal_base_url=origin,
        prq_base_url=origin + "/PRQWeb",
        sectord_base_url=origin + "/SectOrdWeb",
        webmaas_base_url=origin + "/webmaas",
        ca_bundle=helper.ca,
        request_policy=RequestPolicy(min_delay_seconds=0, max_delay_seconds=0),
    )
    with VghksSDK(credentials=PortalCredentials("SYNTHETIC", "SYNTHETIC-ONLY"), settings=settings) as sdk:
        sdk._runtime.transport.session.trust_env = False
        by_mrn = sdk.records.get_visit_cases(MRN)
        basic = sdk.patients.get_basic_info(MRN)
        by_id = sdk.records.get_visit_cases(national_id=basic.national_id)
        assert by_mrn == by_id
        case = next(row for row in by_id if row.case_type == "O")
        assert sdk.records.get_soap(case).blocks
        assert sdk.orders.get_case_orders(case)
        assert state["login_posts"] == 1
        status = sdk.connection_status()
        for app in ("portal", "prq", "sectord", "webmaas"):
            assert status[app]["tls_profile"] == "TLS12_COMPAT"
            assert status[app]["confirmed"] and status[app]["certificate_verification"]
    return {"public_services": "OK", "manual_tls_configuration": False, "login_posts": 1}


def main():
    output = ROOT / "output"
    if output.is_symlink() or output.resolve().parent != ROOT:
        raise SystemExit("Output escapes workspace")
    helper = LegacyAesLoopbackTests()
    helper.setUp()
    helper.server.RequestHandlerClass = VisitIntranet
    origin = f"https://localhost:{helper.server.server_port}"
    results = []
    try:
        sdk_result = verify_plain_sdk(helper, origin)
        for mode in ("automatic", "id_and_soap_failure", "empty"):
            helper.server.test_state = state = test_state(mode, origin)
            with tempfile.TemporaryDirectory(prefix="visit-exe-", dir=output) as temporary:
                directory = Path(temporary).resolve()
                if not directory.is_relative_to(output.resolve()):
                    raise SystemExit("Temporary path escapes output")
                exe = Path(shutil.copy2(ROOT / "dist/vghks-live-test.exe", directory))
                # Zero-argument startup must ignore obsolete sidecars and a
                # conflicting profile environment value.
                (directory / "live-test-config.json").write_text("{obsolete", encoding="utf-8")
                environment = {
                    key: value
                    for key, value in os.environ.items()
                    if not key.upper().startswith("VGHKS_")
                    and key.upper()
                    not in {
                        "HTTP_PROXY",
                        "HTTPS_PROXY",
                        "ALL_PROXY",
                        "NO_PROXY",
                        "REQUESTS_CA_BUNDLE",
                        "CURL_CA_BUNDLE",
                    }
                }
                environment.update(
                    VGHKS_USERNAME="SYNTHETIC",
                    VGHKS_PASSWORD="SYNTHETIC-ONLY",
                    VGHKS_TEST_MRN=MRN,
                    VGHKS_LIVE_PROFILE="comprehensive",
                    VGHKS_CA_BUNDLE=helper.ca,
                    VGHKS_DELAY_MIN="0",
                    VGHKS_DELAY_MAX="0",
                    VGHKS_MAX_ATTEMPTS="1",
                    VGHKS_CONNECT_TIMEOUT="0.3",
                    VGHKS_READ_TIMEOUT="2",
                    NO_PROXY="localhost,127.0.0.1",
                )
                for app, suffix in {
                    "portal": "",
                    "prq": "/PRQWeb",
                    "sectord": "/SectOrdWeb",
                    "webmaas": "/webmaas",
                }.items():
                    environment[f"VGHKS_{app.upper()}_BASE_URL"] = origin + suffix
                manual = IDENTIFIER if mode == "id_and_soap_failure" else ""
                process = subprocess.run(
                    [str(exe)],
                    cwd=directory,
                    env=environment,
                    input=f"\n{manual}\n\n",
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=150,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                (output / f"exe-visits-{mode}.log").write_text(
                    process.stdout + process.stderr, encoding="utf-8"
                )
                assert process.returncode == (1 if mode == "id_and_soap_failure" else 0), (
                    mode,
                    process.returncode,
                )
                archives = list(directory.glob("*.zip"))
                assert len(archives) == 1 and not list(directory.glob("*.sha256"))
                assert state["lookups"] == ["1", "2"], state["lookups"]
                assert not any(
                    path.startswith(("/OPPLWeb", "/Pck", "/VGHK", "/ibi_apps"))
                    for path in state["paths"]
                )
                with zipfile.ZipFile(archives[0]) as archive:
                    summary = json.loads(archive.read("run_summary.json"))
                    expected = {
                        "automatic": "OK",
                        "id_and_soap_failure": "COMPLETED_WITH_ERRORS",
                        "empty": "COMPLETED_WITH_GAPS",
                    }[mode]
                    assert summary["status"] == expected, (mode, summary["status"])
                    assert summary["profile"] == "visits"
                    assert not any(
                        step["status"] == "ERROR"
                        and step["name"].startswith("network.")
                        and ".https" in step["name"]
                        for step in summary["steps"]
                    )
                    selections = json.loads(archive.read("parsed/network/selected_profiles.json"))
                    assert all(
                        item["tls_profile"] == "TLS12_COMPAT" and item["certificate_verification"]
                        for item in selections.values()
                    )
                    assert all(not item.flag_bits & 1 for item in archive.infolist())
                    if mode != "empty":
                        assert "parsed/visits/filters/combined.json" in archive.namelist()
                        assert state["order_cases"] == ["C0"]
                        assert state["soap_cases"] == (["C0", "C1"] if manual else ["C0"])
                    else:
                        assert not state["soap_cases"] and not state["order_cases"]
                with BundleReader(archives[0]) as reader:
                    _, recipe = inspect_bundle(reader)
                    assert recipe["profile"] == "visits"
                results.append(
                    {
                        "scenario": mode,
                        "status": summary["status"],
                        "zero_argument_start": True,
                        "no_sidecar_required": True,
                        "same_directory_plain_zip": True,
                        "compat_first_without_failed_tls_probes": True,
                        "soap_samples": len(state["soap_cases"]),
                        "order_samples": len(state["order_cases"]),
                    }
                )
    finally:
        helper.doCleanups()
    (output / "exe-visits-verification.json").write_text(
        json.dumps(
            {
                "build": json.loads((output / "build-info.json").read_text(encoding="utf-8")),
                "sdk": sdk_result,
                "scenarios": results,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(
        "Frozen EXE verified: focused scope, automatic/manual ID, error continuation, empty data and portable ZIP."
    )


if __name__ == "__main__":
    main()
