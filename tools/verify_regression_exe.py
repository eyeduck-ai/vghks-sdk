"""Check the EXE against synthetic HTTPS, including a legacy MRN and an unscoped link."""

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
    access_review_form,
    visit_link,
    visit_page,
    visit_row,
)
from verify_visit_exe import MRN, VisitIntranet, test_state  # noqa: E402

LEGACY_MRN = "11111111"

NUMERIC_HTML = """
<div id="data"><table id="resnumTable0">
<tr><th rowspan="2">日期</th><th colspan="7">Va</th></tr>
<tr><th>OD</th><th>OS</th></tr>
<tr><td>2026-01-02</td><td>error</td><td></td></tr>
</table><table id="resnumTable1">
<tr><th>日期</th><th>Test A</th><th>Test B</th><th>Test C</th></tr>
<tr><th>單位</th><th>mg/dL</th><th></th></tr>
<tr><td>2026-01-02</td><td>1</td><td>2</td><td>3</td></tr>
</table></div>
"""


class RegressionIntranet(VisitIntranet):
    def dispatch(self):
        path = urlsplit(self.path).path
        state = self.server.test_state
        if state["mode"] in {"comparison", "comparison_rejected"}:
            if path == "/login.do" and self.command == "POST":
                body = dict(parse_qsl(
                    self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode(),
                    keep_blank_values=True,
                ))
                account = body["muid"]
                assert account in {"SYNTHETIC", "SYNTHETIC-UNION"}
                assert body["mpassword"] == (
                    "SYNTHETIC-ONLY" if account == "SYNTHETIC" else "UNION-ONLY"
                )
                state["login_posts"] += 1
                state["login_accounts"].append(account)
                state["active_account"] = account
                if state["mode"] == "comparison_rejected" and account == "SYNTHETIC-UNION":
                    return self.reply("synthetic second-account login rejection", 501)
                return self.reply(
                    "<script>var targetUrl='myPortal.do?thetime=1';</script>", cookie=True
                )
            if path == "/webmaas/WPSAutoLogon" and self.command == "GET":
                roles = dict(parse_qsl(urlsplit(self.path).query)).get("externalRoles")
                if roles == "maas_RSV11":
                    state["account_rsv_gets"] = 0
            if path == "/webmaas/RSV/RSV11W001.do" and self.command == "GET" and not urlsplit(self.path).query:
                state["account_rsv_gets"] += 1
                if state["active_account"] == "SYNTHETIC" and state["account_rsv_gets"] == 1:
                    return self.reply("<html>synthetic SSO transition without a form</html>")
                if state["active_account"] == "SYNTHETIC-UNION" and state["account_rsv_gets"] > 1:
                    return self.reply("<html>synthetic repeated GET without a form</html>")
        if state["mode"] == "review":
            if path == "/webmaas/WPSAutoLogon" and self.command == "GET":
                roles = dict(parse_qsl(urlsplit(self.path).query)).get("externalRoles")
                if roles == "maas_RSV11":
                    state["rsv_landing_gets"] = 0
            if path == "/webmaas/RSV/RSV11W001.do" and self.command == "GET" and not urlsplit(self.path).query:
                state["rsv_landing_gets"] += 1
                if state["rsv_landing_gets"] > 1:
                    return self.reply("<html>synthetic second GET has no query form</html>")
            if (path == "/PRQWeb/QueryPatientRecord.do"
                    and self.command == "POST" and not state["review_requests"]):
                body = dict(parse_qsl(
                    self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode(),
                    keep_blank_values=True,
                ))
                assert body["queryPtID"] == MRN
                return self.reply(access_review_form(mrn=MRN, hid="SYNTHETIC-HID"))
            if path == "/PRQWeb/EMRProcess.do" and self.command == "POST":
                body = dict(parse_qsl(
                    self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode(),
                    keep_blank_values=True,
                ))
                assert body["reqCode"] == "saveAccessCause"
                assert body["valueA(cause)"] == "1A"
                assert body["value(smr_hhisnum)"] == MRN
                assert body["value(smr_hid)"] == "SYNTHETIC-HID"
                state["review_requests"] += 1
                return self.reply(CONTEXT)
        if path == "/PRQWeb/QueryPatientRecord.do" and self.command == "GET":
            params = dict(parse_qsl(urlsplit(self.path).query, keep_blank_values=True))
            assert params["Use"] == "Dur" and params["id"] == MRN
            state["mrn"] = MRN
            return self.reply("synthetic history context")
        if path == "/PRQWeb/QueryCaseList.do" and state["mode"] == "related":
            return self.reply(
                visit_page(
                    visit_row(caseNo="OWN"),
                    visit_row(hhisnum=LEGACY_MRN, caseNo="LEGACY", caseDT="2009-09-03"),
                )
            )
        if path == "/PRQWeb/QueryCaseList.do" and state["mode"] == "unscoped":
            return self.reply(
                visit_page(visit_row(caseNo="OWN"))
                + f'<a href="{visit_link(hhisnum=LEGACY_MRN, caseNo="UNSCOPED")}">link</a>'
            )
        if state["mode"] == "related" and path in {
            "/PRQWeb/QueryCaseDetail.do", "/PRQWeb/QueryBillingSOAP.do"
        }:
            params = dict(parse_qsl(urlsplit(self.path).query, keep_blank_values=True))
            if params.get("hhisnum") == LEGACY_MRN:
                assert params["caseNo"] == "LEGACY"
                if path.endswith("QueryCaseDetail.do"):
                    return self.reply('<div id="tabs"><ul id="tab_ul"><li id="soap">SOAP</li></ul></div>')
                state["soap_cases"].append("LEGACY")
                return self.reply('<div id="data"><div class="soap"><pre>S: legacy SOAP</pre></div></div>')
        if path == "/PRQWeb/QueryResNumCenter.do":
            if self.command == "POST":
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
            state["numeric_requests"] += 1
            return self.reply(NUMERIC_HTML)
        if path == "/OPPLWeb/surgAction.do":
            params = dict(parse_qsl(urlsplit(self.path).query, keep_blank_values=True))
            if self.command == "POST":
                params.update(
                    parse_qsl(
                        self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode(),
                        keep_blank_values=True,
                    )
                )
            assert self.headers.get("X-Requested-With") == "XMLHttpRequest"
            if params.get("method") == "searchDr":
                return self.reply("Synthetic Doctor")
            if params.get("method") == "doSearchByConition":
                state["schedule_requests"] += 1
                assert params["drno"] == "SYNTHETIC"
                return self.reply(
                    json.dumps(
                        {
                            "surgs": [
                                {
                                    "orhisnum": "SYNTHETIC-SURGERY",
                                    "orcaseno": "CASE1",
                                    "orbgndt": "2026-09-25",
                                    "orbgntm": "23:59:00",
                                    "optime": "TF",
                                    "oproom": "1",
                                    "oroproom": "OR-1",
                                    "oropamed": "GA",
                                    "orfreqnc": "Routine",
                                    "orcatgy": "OPH",
                                    "ordocno": "SYNTHETIC",
                                    "ordocnm": "Synthetic Doctor",
                                    "oropnc1": "80416",
                                    "oropnm1": "Synthetic operation",
                                    "patient": {
                                        "hhisnum": "SYNTHETIC-SURGERY",
                                        "hnamec": "Synthetic patient",
                                        "hsexc": "F",
                                        "hnursta": "OPD",
                                    },
                                    "futureField": "preserve-me",
                                }
                            ]
                        }
                    )
                )
        return super().dispatch()


def verify_mode(executable: Path, helper: LegacyAesLoopbackTests, origin: str, mode: str) -> dict:
    state = test_state(mode, origin)
    state.update(
        oppl_ajax_requests=0,
        oppl_sso_posts=0,
        mutation_attempts=0,
        numeric_requests=0,
        schedule_requests=0,
        review_requests=0,
        rsv_landing_gets=0,
        active_account="",
        account_rsv_gets=0,
        login_accounts=[],
        single_rsv_landing=False,
    )
    helper.server.test_state = state
    with tempfile.TemporaryDirectory(prefix="regression-exe-", dir=ROOT / "output") as temporary:
        directory = Path(temporary).resolve()
        if not directory.is_relative_to((ROOT / "output").resolve()):
            raise SystemExit("Temporary directory escapes output")
        copy = Path(shutil.copy2(executable, directory))
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
            VGHKS_CA_BUNDLE=helper.ca,
            VGHKS_DELAY_MIN="0",
            VGHKS_DELAY_MAX="0",
            VGHKS_MAX_ATTEMPTS="1",
            VGHKS_CONNECT_TIMEOUT="0.3",
            VGHKS_READ_TIMEOUT="2",
            NO_PROXY="localhost,127.0.0.1",
        )
        if mode in {"comparison", "comparison_rejected"}:
            environment.update(
                VGHKS_COMPARISON_USERNAME="SYNTHETIC-UNION",
                VGHKS_COMPARISON_PASSWORD="UNION-ONLY",
                VGHKS_COMPARISON_MRN=MRN,
            )
        for app, path in {
            "portal": "",
            "prq": "/PRQWeb",
            "sectord": "/SectOrdWeb",
            "webmaas": "/webmaas",
            "oppl": "/OPPLWeb",
        }.items():
            environment[f"VGHKS_{app.upper()}_BASE_URL"] = origin + path
        process = subprocess.run(
            [str(copy), *(["--profile", "regression", "--access-review-reason", "1A", "--non-interactive"]
                          if mode == "review" else ["--profile", "regression", "--non-interactive"]
                          if mode == "comparison" else [])],
            cwd=directory,
            env=environment,
            input="\n\n",
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        (ROOT / "output" / f"exe-regression-{mode}.log").write_text(
            process.stdout + process.stderr, encoding="utf-8"
        )
        archives = list(directory.glob("vghks-live-test-*.zip"))
        assert len(archives) == 1 and not list(directory.glob("*.sha256")), archives
        with zipfile.ZipFile(archives[0]) as archive:
            assert archive.testzip() is None
            summary = json.loads(archive.read("run_summary.json"))
            steps = {step["name"]: step for step in summary["steps"]}
            assert summary["profile"] == "regression", summary["profile"]
            assert state["schedule_requests"] == 1, steps.get("oppl.surgery_schedule.0001")
            assert state["numeric_requests"] >= 1
            schedule = json.loads(archive.read("parsed/atomic/oppl.surgery_schedule/0001.json"))
            assert schedule[0]["schedule_time"] == "TF"
            assert schedule[0]["time_status"] == "UNCONFIRMED"
            assert schedule[0]["start_time"] == ""
            assert schedule[0]["source_fields"]["futureField"] == "preserve-me"
            history = json.loads(archive.read("parsed/atomic/prq.numeric_history/0001.json"))
            assert history["tables"][0]["column_paths"] == [["日期"], ["Va", "OD"], ["Va", "OS"]]
            assert history["tables"][0]["rows"] == [["2026-01-02", "error", ""]]
            assert history["tables"][0]["parsing_issues"] == ["NUMERIC_HEADER_SPAN_MISMATCH"]
            assert history["tables"][1]["column_paths"] == [
                ["日期"], ["Test A", "mg/dL"], ["Test B"], ["Test C"]
            ]
            assert steps["prq.numeric_history.0001"]["status"] == "OK"
            assert steps["prq.numeric_history.0001"]["details"]["numeric_warning_count"] == 1
            assert steps["prq.numeric_history.0001"]["details"]["numeric_error_count"] == 0
            assert steps["oppl.surgery_schedule.0001"]["status"] == "OK"
            if mode == "review":
                assert state["review_requests"] == 1
                assert state["rsv_landing_gets"] == 1
                assert steps["webmaas.registration_query.0001"]["status"] == "OK"
            if mode in {"comparison", "comparison_rejected"}:
                report = json.loads(archive.read("registration_comparison.json"))
                accounts = report["accounts"]
                expected_accounts = [
                    "SYNTHETIC", "SYNTHETIC", "SYNTHETIC", "SYNTHETIC-UNION",
                ]
                if mode == "comparison":
                    expected_accounts.append("SYNTHETIC-UNION")
                assert state["login_accounts"] == expected_accounts
                for label in (("vghks", "union") if mode == "comparison" else ("vghks",)):
                    assert accounts[label]["direct"]["status"] == "OK"
                    assert accounts[label]["after_demographics"]["status"] == "OK"
                    assert accounts[label]["direct"]["record_count"] == 2
                    assert accounts[label]["after_demographics"]["record_count"] == 2
                assert accounts["vghks"]["direct"]["requests"]["form_get"] == 2, accounts
                assert accounts["vghks"]["after_demographics"]["requests"]["form_get"] == 1
                if mode == "comparison":
                    assert accounts["union"]["direct"]["requests"]["form_get"] == 1
                    assert accounts["union"]["after_demographics"]["requests"].get("form_get", 0) == 0
                else:
                    assert summary["status"] == "COMPLETED_WITH_ERRORS"
                    assert accounts["union"]["login"]["status"] == "ERROR"
                    assert accounts["union"]["direct"]["status"] == "BLOCKED"
                    assert accounts["union"]["demographics"]["status"] == "BLOCKED"
                    assert accounts["union"]["after_demographics"]["status"] == "BLOCKED"
            if mode == "unscoped":
                assert summary["status"] == "COMPLETED_WITH_ERRORS"
                assert steps["prq.visit_cases.0001"]["issue"]["code"] == "PRQ_CASE_PATIENT_MISMATCH"
                assert steps["prq.soap"]["status"] == "BLOCKED"
                assert steps["prq.numeric"]["status"] == "BLOCKED"
                manifest = [
                    json.loads(line)
                    for line in archive.read("capture_manifest.jsonl").decode().splitlines()
                ]
                list_rows = [
                    row
                    for row in manifest
                    if row["kind"] == "HTTP_EXCHANGE"
                    and urlsplit(row["request"]["url"]).path.endswith("/QueryCaseList.do")
                ]
                assert list_rows
                assert "UNSCOPED" in archive.read(list_rows[-1]["response_file"]).decode()
                assert not state["soap_cases"]
            elif mode != "comparison_rejected":
                assert summary["status"] == "OK", (
                    summary["status"],
                    [(name, row["status"]) for name, row in steps.items()],
                )
                assert steps["prq.soap.0001"]["status"] == "OK"
                assert steps["prq.numeric.0001"]["status"] == "OK"
                if mode == "related":
                    visits = json.loads(archive.read("parsed/atomic/prq.visit_cases/0001.json"))
                    assert len(visits) == 2
                    assert steps["prq.visit_cases.0001"]["details"]["source_mrn_count"] == 2
                    assert steps["prq.visit_cases.0001"]["details"]["related_mrn_visit_count"] == 1
                    legacy = next(item for item in visits if item["mrn"] == LEGACY_MRN)
                    assert legacy["lookup_mrn"] == MRN
                    assert "LEGACY" in state["soap_cases"]
                    assert steps["prq.soap.0002"]["status"] == "OK"
                    assert steps["prq.numeric.0002"]["status"] == "OK"
        assert process.returncode == (1 if mode in {"unscoped", "comparison_rejected"} else 0), process.returncode
        assert state["mutation_attempts"] == 0
        return {
            "mode": mode,
            "status": summary["status"],
            "raw_case_list_saved": mode == "unscoped",
            "independent_checks_completed": True,
        }


def main() -> int:
    output = ROOT / "output"
    if output.is_symlink() or output.resolve().parent != ROOT:
        raise SystemExit("Output directory escapes workspace")
    output.mkdir(exist_ok=True)
    executable = ROOT / "dist" / "vghks-live-test.exe"
    helper = LegacyAesLoopbackTests()
    helper.setUp()
    helper.server.RequestHandlerClass = RegressionIntranet
    origin = f"https://localhost:{helper.server.server_port}"
    try:
        results = [
            verify_mode(executable, helper, origin, mode)
            for mode in ("related", "unscoped", "valid", "review", "comparison", "comparison_rejected")
        ]
    finally:
        helper.tearDown()
    (output / "exe-regression-verification.json").write_text(
        json.dumps(results, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
