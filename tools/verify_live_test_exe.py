"""Exercise explicit frozen EXE configurations against a synthetic localhost intranet.

All configured origins are loopback. No production credentials, HAR tokens or
patient records are used. The temporary synthetic bundles are removed after
verification; output/exe-verification.json retains the results.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from datetime import date
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

from test_tls import LegacyAesLoopbackTests  # noqa: E402

DAY = date(2026, 9, 19)


class SyntheticIntranet(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.dispatch()

    def do_POST(self):
        self.dispatch()

    def reply(self, body, status=200, cookie=False, location=None):
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        if cookie:
            self.send_header("Set-Cookie", "JSESSIONID=synthetic-only; Path=/; Secure")
        if location:
            self.send_header("Location", location)
        self.end_headers()
        self.wfile.write(data)

    def dispatch(self):
        address = urlsplit(self.path)
        params = dict(parse_qsl(address.query))
        if self.command == "POST":
            params.update(
                dict(
                    parse_qsl(
                        self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode()
                    )
                )
            )
        path = address.path
        state = self.server.test_state
        if path == "/index.do":
            return self.reply(
                '<form action="login.do"><input name="mpassword" type="password"><input type="hidden" name="ssoId2" value="synthetic"></form>',
                cookie=True,
            )
        if path == "/login.do":
            state["login_posts"] += 1
            if state["reject_login"]:
                return self.reply("synthetic login failure", 501)
            return self.reply(
                "<script>var targetUrl='myPortal.do?thetime=1';</script>", cookie=True
            )
        if path == "/ssoFromDn.do":
            dn = params.get("apDn", "")
            if "05070601_0" in dn:
                state["mis_program"] = "PMO003R1" if "05070601_01" in dn else "PMO004R1"
                base = state["origin"]
                fields = {
                    "HID": "SYNTHETIC-MIS-HID",
                    "ssID": "test",
                    "keyOne": "1",
                    "keyTwo": "2",
                    "keyThree": "3",
                    "USR_ID": "SYNTHETIC",
                    "targetURL": base + "/VGHK/MIS_Por/MADSINGLE.ASP",
                }
                return self.reply(
                    f'<form action="{base}/VGHK/WPSAutoLogon.asp">'
                    + "".join(
                        f'<input name="{key}" value="{value}">' for key, value in fields.items()
                    )
                    + "</form>"
                )
            prefix = (
                "/OPPLWeb"
                if "0108_04" in dn or "010801_04" in dn
                else "/SectOrdWeb"
                if "011911_06" in dn
                else "/PRQWeb"
            )
            base = state["origin"] + prefix
            fields = {
                "HID": "SYNTHETIC-HID",
                "ssID": "test",
                "keyOne": "1",
                "keyTwo": "2",
                "keyThree": "3",
                "uid": "SYNTHETIC",
                "targetURL": "qlogAction.do?method=qLog"
                if "010801_04" in dn
                else "surgAction.do?method=surg"
                if prefix == "/OPPLWeb"
                else base + "/landing.do",
            }
            body = (
                f'<form action="{base}/WPSAutoLogon">'
                + "".join(f'<input name="{key}" value="{value}">' for key, value in fields.items())
                + "</form>"
            )
            return self.reply(body)
        if path == "/VGHK/WPSAutoLogon.asp":
            assert self.command == "GET" and params.get("USR_ID") == "SYNTHETIC"
            return self.reply(
                '<form action="/VGHK/MIS_Por/MADSINGLE.ASP"><input name="USR_ID" value="SYNTHETIC"></form>'
            )
        if path == "/VGHK/MIS_Por/MADSINGLE.ASP":
            return self.reply('<frameset><frame src="/ibi_apps/WFServlet?stage=login"></frameset>')
        if path in {"/VGHK/PA_PMO003M.asp", "/VGHK/PA_PMO004M.asp"}:
            return self.reply("", 302, location="/VGHK/Pswdchk.asp")
        if path == "/VGHK/Pswdchk.asp":
            return self.reply(
                '<form action="/VGHK/PAswd2db.asp"><input name="sUSR_ID" value="SYNTHETIC"><input name="txtUsrId"><input name="txtPAPSWD" type="password"><input name="B1" type="submit" value="Submit"></form>'
            )
        if path == "/VGHK/PAswd2db.asp":
            assert params.get("sUSR_ID") == "SYNTHETIC"
            assert params.get("txtUsrId") == "SYNTHETIC-NATIONAL-ID"
            assert params.get("txtPAPSWD") == "SYNTHETIC-SECONDARY"
            state["mis_password_posts"] += 1
            return self.reply(
                '<form action="/VGHK/MAD/MADMAIN.ASP"><input name="USR_ID" value="SYNTHETIC"></form>'
            )
        if path == "/VGHK/MAD/MADMAIN.ASP":
            return self.reply(
                '<frameset><frame src="/ibi_apps/WFServlet?stage=selector"></frameset>'
            )
        if path == "/ibi_apps/WFServlet":
            program = state["mis_program"]
            if params.get("stage") == "login":
                entry = "PA_PMO003M.asp" if program == "PMO003R1" else "PA_PMO004M.asp"
                return self.reply(
                    f'<form action="/VGHK/{entry}"><input name="USR_ID" value="SYNTHETIC"></form>'
                )
            if params.get("stage") == "selector":
                return self.reply(
                    f'<form action="/ibi_apps/WFServlet"><input name="IBIF_ex" value="{program}"><select name="BEGYM"><option value="202609" selected>September</option></select></form>'
                )
            assert params.get("IBIF_ex") == program and params.get("BEGYM") == "202609"
            state["mis_reports"].append(program)
            return self.reply(
                f"<html><body>{program}<table><tr><td>Synthetic report only</td></tr></table></body></html>"
            )
        if path in {"/SectOrdWeb/so.do", "/PRQWeb/GenerateKeyAction.do"}:
            return self.reply("ssID=s&keyOne=1&keyTwo=2&keyThree=3")
        if path == "/OPPLWeb/WPSAutoLogon":
            assert params.get("targetURL") in {
                "surgAction.do?method=surg",
                "qlogAction.do?method=qLog",
            }
            state["oppl_sso_posts"] += 1
            return self.reply("<html><body>synthetic OPPL landing</body></html>")
        if path.startswith("/OPPLWeb/") and params.get("method") in {
            "doSave",
            "doEdit",
            "doCancel",
        }:
            state["mutation_attempts"] += 1
            return self.reply("write attempted during read-only test", 500)
        if path == "/OPPLWeb/surgConsentController.do":
            actions = {
                "getOprpformName": {"forms": {"OPH": ["SYNTHETIC"]}, "status": "Y"},
                "loadform": {"diseasename": [], "opname1": [], "status": "Y"},
                "searchDr": {"upuser": {}, "status": "Y"},
            }
            return self.reply(json.dumps(actions.get(params.get("method"), {})))
        if path == "/OPPLWeb/surgAction.do":
            if self.headers.get("X-Requested-With") != "XMLHttpRequest":
                return self.reply("synthetic missing AJAX header", 404)
            state["oppl_ajax_requests"] += 1
            actions = {
                "getPatInfo": {
                    "patient": {"hhisnum": params.get("hhisnum")},
                    "surgs": [],
                    "consents": [],
                },
                "getReqnos": {"reqs": []},
                "listAnticoagulant": {"status": "Y"},
                "listSGLT2": {"status": "Y"},
                "getPfiles": {"pfiles": [], "chargenos": []},
                "getHolidays": {"holidays": "", "maxDate": "2026-12-31"},
            }
            if params.get("method") in actions:
                return self.reply(json.dumps(actions[params["method"]]))
            return self.reply(
                "Synthetic Doctor" if params.get("method") == "searchDr" else '{"surgs":[]}'
            )
        if path == "/PRQWeb/QueryMrData.do":
            actions = {
                "getUdhcdsps": {"allergy": "", "allergyMsg": ""},
                "getAD": {"adSign": "", "adSignMsg": ""},
                "getIrb": {"irbtype": ""},
                "getNextBed": {"frontBed": "", "nextBed": ""},
            }
            return self.reply(
                json.dumps(
                    {"hhisnum": params.get("hhisnum"), **actions.get(params.get("reqCode"), {})}
                )
            )
        if path == "/PRQWeb/QueryNISAction.do":
            return self.reply('{"status":"Y","caseList":[]}')
        if path == "/PRQWeb/QueryResTextCenter.do":
            return self.reply("<html><script>var height_bd=1;</script></html>")
        if path == "/PRQWeb/QueryUploadMR.do":
            return self.reply('[{"maintp":"OPD","mainnm":"OPD"}]')
        if path == "/PRQWeb/MRUploadFile.do":
            return self.reply('<table id="tbObj"></table>')
        if path == "/PRQWeb/QueryOPDPatList.do":
            form = '<form><input name="docCode"><input name="opdDate"></form>'
            if params.get("opdDate") == "2026-09-15":
                return self.reply("synthetic one-day failure", 500)
            if params.get("opdDate") != DAY.isoformat():
                return self.reply(form)
            body = (
                form
                + "<script>aryOpdSec[0]='V1';aryOpdRoom[0]='02';aryOpdDoc[0]='SYNTHETICF';aryOpdSec[1]='70';aryOpdRoom[1]='01';aryOpdDoc[1]='';</script>"
            )
            for index, mrn in enumerate(("WL1001", "WL1002", "WL1003", "WL1004", "", "SHARED01")):
                room = 1 if mrn == "SHARED01" else 0
                section = "70" if room == 1 else "V1"
                body += f"<script>if(aryOpdSec[{room}]=='{section}'){{new KSCase('','{index}','{mrn}','Synthetic','F','60');}}</script>"
            return self.reply(body)
        if path == "/PRQWeb/QueryPatientRecord.do":
            state["mrn"] = params.get("queryPtID") or params.get("id") or ""
            state["queried_mrns"].add(state["mrn"])
            return self.reply("patient context")
        if path == "/PRQWeb/QueryCaseList.do":
            mrn = state.get("mrn", "")
            body = '<div id="typeO"></div>'
            if mrn.startswith("WL"):
                params = {
                    "hhisnum": mrn,
                    "caseType": "O",
                    "caseNo": "SYN001",
                    "caseSec": "V1",
                    "caseSectC": "OPH",
                    "caseDT": "2026-09-18" if mrn == "WL1002" else DAY.isoformat(),
                    "index": "0",
                }
                body += '<a href="/PRQWeb/QueryCaseDetail.do?' + urlencode(params) + '">case</a>'
            return self.reply(body)
        if path == "/PRQWeb/QueryCaseDetail.do":
            return self.reply('<div id="tabs"><ul id="tab_ul"><li id="soap">SOAP</li></ul></div>')
        if path == "/PRQWeb/QueryBillingSOAP.do":
            mrn = params.get("hhisnum")
            if mrn != state.get("mrn"):
                return self.reply("synthetic patient context mismatch", 500)
            state["soap_mrns"].append(mrn)
            if mrn == "WL1003":
                return self.reply("synthetic one-patient SOAP failure", 500)
            return self.reply(
                '<div id="data"><div class="soap"><pre>P: ARRANGE cata</pre></div></div>'
            )
        return self.reply("synthetic application")


def main():
    output = ROOT / "output"
    if output.is_symlink() or output.resolve().parent != ROOT:
        raise SystemExit("Output directory escapes the workspace")
    executable = ROOT / "dist/vghks-live-test.exe"
    helper = LegacyAesLoopbackTests()
    helper.setUp()
    origin = f"https://localhost:{helper.server.server_port}"
    helper.server.RequestHandlerClass = SyntheticIntranet
    results = {}
    try:
        with tempfile.TemporaryDirectory(prefix="exe-verification-", dir=output) as temporary:
            workspace = Path(temporary).resolve()
            if not workspace.is_relative_to(output.resolve()):
                raise SystemExit("Temporary path escapes output")
            for mode in ("failed_login", "weekly_workflow"):
                directory = workspace / mode
                directory.mkdir()
                copied = Path(shutil.copy2(executable, directory / executable.name))
                helper.server.test_state = state = {
                    "origin": origin,
                    "reject_login": mode == "failed_login",
                    "login_posts": 0,
                    "oppl_ajax_requests": 0,
                    "oppl_sso_posts": 0,
                    "mutation_attempts": 0,
                    "mis_password_posts": 0,
                    "mis_reports": [],
                    "queried_mrns": set(),
                    "soap_mrns": [],
                }
                endpoints = {
                    key + "_base_url": origin + path
                    for key, path in {
                        "portal": "",
                        "prq": "/PRQWeb",
                        "sectord": "/SectOrdWeb",
                        "webmaas": "/webmaas",
                        "oppl": "/OPPLWeb",
                        "audit": "/PRQWeb",
                        "mis": "",
                        "review": "/Pck",
                    }.items()
                }
                config = {
                    "schema_version": 5,
                    "profile": "comprehensive",
                    "include_earnings": mode == "weekly_workflow",
                    "weekly_opd_end": DAY.isoformat(),
                    "ca_bundle": helper.ca,
                    "endpoint_overrides": endpoints,
                    "request_policy": {
                        "min_delay_seconds": 0,
                        "max_delay_seconds": 0,
                        "max_attempts": 1,
                        "connect_timeout_seconds": 0.3,
                        "read_timeout_seconds": 2,
                    },
                }
                (directory / "live-test-config.json").write_text(
                    json.dumps(config), encoding="utf-8"
                )
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
                    VGHKS_TEST_MRN="0000000",
                    NO_PROXY="localhost,127.0.0.1",
                )
                if mode == "weekly_workflow":
                    environment.update(
                        VGHKS_EARNINGS_NATIONAL_ID="SYNTHETIC-NATIONAL-ID",
                        VGHKS_EARNINGS_PASSWORD="SYNTHETIC-SECONDARY",
                    )
                process = subprocess.run(
                    [str(copied), "--config", "live-test-config.json", "--non-interactive"],
                    cwd=directory,
                    env=environment,
                    input="\n",
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=90,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                (output / f"exe-{mode}.log").write_text(
                    process.stdout + process.stderr, encoding="utf-8"
                )
                archives = list(directory.glob("*.zip"))
                assert process.returncode == 1, (mode, process.returncode)
                assert len(archives) == 1 and not list(directory.glob("*.sha256"))
                with zipfile.ZipFile(archives[0]) as archive:
                    assert all(not item.flag_bits & 1 for item in archive.infolist())

                    def read(name):
                        return json.loads(archive.read(name))

                    summary = read("run_summary.json")
                    modes = read("parsed/network/selected_profiles.json")
                    assert len(modes) == (9 if mode == "weekly_workflow" else 8) and all(
                        value["certificate_verification"] and value["applied"]
                        for value in modes.values()
                    )
                    workflow = read("parsed/workflows/opd_soap_week/manifest.json")
                    assert workflow["source"]["card_no"] == "SYNTHETIC"
                    assert state["login_posts"] == 1
                    if mode == "failed_login":
                        assert (
                            summary["status"] == "HTTP_FAILED" and workflow["status"] == "BLOCKED"
                        )
                    else:
                        counts = workflow["counts"]
                        assert workflow["status"] == "INCOMPLETE", workflow
                        expected = {
                            "days_total": 7,
                            "days_completed": 6,
                            "days_failed": 1,
                            "dedicated_registrations": 5,
                            "shared_registrations": 1,
                            "unqueryable_registrations": 1,
                            "patients_checked": 4,
                            "registrations_with_visit": 3,
                            "registrations_without_visit": 1,
                            "soap_checked": 3,
                            "soap_errors": 1,
                            "matches": 2,
                        }
                        assert all(counts[key] == value for key, value in expected.items()), counts
                        assert "SHARED01" not in state["queried_mrns"]
                        assert state["soap_mrns"] == ["WL1001", "WL1003", "WL1004"]
                        assert state["oppl_ajax_requests"] >= 1
                        assert state["oppl_sso_posts"] >= 1
                        assert state["mutation_attempts"] == 0
                        assert state["mis_password_posts"] == 2
                        assert state["mis_reports"] == ["PMO003R1", "PMO004R1"]
                        coverage = read("coverage.json")
                        assert len(coverage["operations"]) == 55
                        assert len(coverage["excluded_write_operations"]) == 4
                        assert len(coverage["earnings_reports"]) == 4 and all(
                            row["status"] == "OK" for row in coverage["earnings_reports"]
                        )
                        for kind in ("performance", "payroll"):
                            assert read(f"parsed/earnings/{kind}-report.json")["tables"]
                        for stage in ("stage1_opd", "stage2_visits", "stage3_soap"):
                            assert any(
                                name.startswith(f"parsed/workflows/opd_soap_week/{stage}/")
                                and name.endswith(".returned.json")
                                for name in archive.namelist()
                            )
                        captures = [
                            json.loads(line)
                            for line in archive.read("capture_manifest.jsonl").splitlines()
                        ]
                        assert any(
                            row.get("live_test_step", "").startswith("weekly_opd.stage3_soap.")
                            for row in captures
                        )
                        assert all(
                            urlsplit(row.get("request", {}).get("url", "")).hostname
                            in {None, "localhost"}
                            for row in captures
                        )
                        assert len(read("parsed/workflows/opd_soap_week/matches.json")) == 2
                    results[mode] = {
                        "status": summary["status"],
                        "workflow_status": workflow["status"],
                        "counts": workflow["counts"],
                        "one_plain_zip_beside_exe": True,
                        "verified_tls_compat": True,
                        "login_posts": state["login_posts"],
                        "oppl_sso_posts": state["oppl_sso_posts"],
                        "clinical_write_attempts": state["mutation_attempts"],
                        "mis_password_posts": state["mis_password_posts"],
                        "mis_reports": state["mis_reports"],
                    }
    finally:
        helper.doCleanups()
    test_log = (output / "tests.log").read_text(encoding="utf-8", errors="replace")
    results["unit_tests_passed"] = (
        int(re.search(r"Ran (\d+) tests", test_log).group(1))
        if re.search(r"\nOK\s*$", test_log)
        else None
    )
    results["build"] = json.loads((output / "build-info.json").read_text())
    (output / "exe-verification.json").write_text(
        json.dumps(results, indent=2) + "\n", encoding="utf-8"
    )
    print(
        "Frozen EXE: packaging, three-stage workflow, OPPL SSO and optional MIS reports verified on localhost."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
