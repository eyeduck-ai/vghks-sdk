"""Run the frozen SOAP-profile EXE against a synthetic localhost HTTPS intranet."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests"), str(ROOT / "tools")]

from test_structured_soap import MEDICATION_HEADER, page  # noqa: E402
from test_tls import LegacyAesLoopbackTests  # noqa: E402
from verify_live_test_exe import SyntheticIntranet  # noqa: E402

DAY = "2026-09-21"
CHRONIC_NOTICE = "慢性病連續處方箋處方　服藥期限\uff1a2026/09/21 \u223c 2026/12/14"
PATIENTS = ("SOAP1001", "SOAP1002", "SOAP1003", "SOAP1004")


class SoapIntranet(SyntheticIntranet):
    def dispatch(self):
        path = urlsplit(self.path).path
        state = self.server.test_state
        if path not in {
            "/PRQWeb/QueryOPDPatList.do",
            "/PRQWeb/QueryPatientRecord.do",
            "/PRQWeb/QueryCaseList.do",
            "/PRQWeb/QueryCaseDetail.do",
            "/PRQWeb/QueryBillingSOAP.do",
        }:
            return super().dispatch()
        params = dict(parse_qsl(urlsplit(self.path).query, keep_blank_values=True))
        if self.command == "POST":
            params.update(parse_qsl(
                self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode(),
                keep_blank_values=True,
            ))
        if path.endswith("QueryOPDPatList.do"):
            assert params.get("docCode") == "SYNTHETIC"
            form = '<form><input name="docCode"><input name="opdDate"></form>'
            if self.command == "GET":
                return self.reply(form)
            assert params.get("opdDate") == DAY
            state["roster_posts"] += 1
            body = (
                form
                + "<script>aryOpdSec[0]='70';aryOpdRoom[0]='01';aryOpdDoc[0]='SYNTHETICF';"
                "aryOpdSec[1]='71';aryOpdRoom[1]='02';aryOpdDoc[1]='';"
                "aryOpdSec[2]='V1';aryOpdRoom[2]='03';aryOpdDoc[2]='OTHER';</script>"
            )
            for index, mrn in enumerate(PATIENTS):
                body += (
                    f"<script>if(aryOpdSec[0]=='70'){{new KSCase('','{index}',"
                    f"'{mrn}','Synthetic patient','F','60');}}</script>"
                )
            body += (
                "<script>if(aryOpdSec[1]=='71'){new KSCase('','5','SHARED1',"
                "'Synthetic shared','F','60');}</script>"
            )
            body += (
                "<script>if(aryOpdSec[2]=='V1'){new KSCase('','6','OTHER1',"
                "'Synthetic other','F','60');}</script>"
            )
            return self.reply(body)
        if path.endswith("QueryPatientRecord.do"):
            assert params.get("type") == "1"
            mrn = params.get("queryPtID")
            assert mrn in PATIENTS
            state["current_mrn"] = mrn
            state["queried_mrns"].add(mrn)
            return self.reply(
                '<frameset id="hFrameset"><frame src="/PRQWeb/Page/JSP/KS_Patient.jsp">'
                '<frame src="/PRQWeb/QueryCaseList.do"></frameset>'
            )
        if path.endswith("QueryCaseList.do"):
            mrn = state["current_mrn"]
            visit_day = "2026-09-20" if state["mode"] == "partial" and mrn == "SOAP1002" else DAY
            fields = {
                "hhisnum": mrn, "caseType": "O", "caseNo": "SYN001",
                "caseSec": "70", "caseSectC": "Synthetic eye", "caseDT": visit_day,
                "index": "0", "vsNo": "SYNTHETICF",
            }
            link = "/PRQWeb/QueryCaseDetail.do?" + urlencode(fields)
            return self.reply(f'<div id="typeO"></div><a href="{link}">case</a>')
        if path.endswith("QueryCaseDetail.do"):
            assert params.get("hhisnum") == state["current_mrn"]
            return self.reply('<div id="tabs"><ul id="tab_ul"><li id="soap">SOAP</li></ul></div>')
        mrn = params.get("hhisnum")
        assert mrn == state["current_mrn"]
        state["soap_mrns"].append(mrn)
        if state["mode"] == "partial" and mrn == "SOAP1003":
            return self.reply("synthetic one-patient SOAP failure", 500)
        body = page()
        if mrn == "SOAP1001":
            body = body.replace(
                MEDICATION_HEADER,
                CHRONIC_NOTICE + "\n" + MEDICATION_HEADER,
                1,
            )
        return self.reply(body)


def main() -> int:
    output = ROOT / "output"
    if output.is_symlink() or output.resolve().parent != ROOT:
        raise SystemExit("Output directory escapes workspace")
    output.mkdir(exist_ok=True)
    helper = LegacyAesLoopbackTests()
    helper.setUp()
    helper.server.RequestHandlerClass = SoapIntranet
    origin = f"https://localhost:{helper.server.server_port}"
    results = []
    try:
        for mode in ("complete", "partial"):
            helper.server.test_state = state = {
                "origin": origin, "reject_login": False, "login_posts": 0,
                "roster_posts": 0, "queried_mrns": set(), "soap_mrns": [],
                "mode": mode, "current_mrn": "",
            }
            with tempfile.TemporaryDirectory(prefix="soap-exe-", dir=output) as temporary:
                directory = Path(temporary).resolve()
                if not directory.is_relative_to(output.resolve()):
                    raise SystemExit("Temporary directory escapes output")
                exe = Path(shutil.copy2(ROOT / "dist/vghks-live-test.exe", directory))
                (directory / "live-test-config.json").write_text("{obsolete", encoding="utf-8")
                environment = {
                    key: value for key, value in os.environ.items()
                    if not key.upper().startswith("VGHKS_")
                    and key.upper() not in {
                        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
                        "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
                    }
                }
                environment.update(
                    VGHKS_USERNAME="SYNTHETIC", VGHKS_PASSWORD="SYNTHETIC-ONLY",
                    VGHKS_LIVE_PROFILE="login", VGHKS_CA_BUNDLE=helper.ca,
                    VGHKS_DELAY_MIN="0", VGHKS_DELAY_MAX="0", VGHKS_MAX_ATTEMPTS="1",
                    VGHKS_CONNECT_TIMEOUT="0.3", VGHKS_READ_TIMEOUT="2",
                    VGHKS_PORTAL_BASE_URL=origin,
                    VGHKS_PRQ_BASE_URL=origin + "/PRQWeb",
                    NO_PROXY="localhost,127.0.0.1",
                )
                process = subprocess.run(
                    [str(exe)], cwd=directory, env=environment,
                    input="\n", capture_output=True, text=True, encoding="utf-8",
                    errors="replace", timeout=180,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                (output / f"exe-soap-{mode}.log").write_text(
                    process.stdout + process.stderr, encoding="utf-8"
                )
                expected = "OK" if mode == "complete" else "COMPLETED_WITH_ERRORS"
                archives = list(directory.glob("vghks-live-test-*.zip"))
                assert len(archives) == 1 and not list(directory.glob("*.sha256")), archives
                with zipfile.ZipFile(archives[0]) as archive:
                    assert archive.testzip() is None
                    summary = json.loads(archive.read("run_summary.json"))
                    assert summary["profile"] == "soap" and summary["status"] == expected, (
                        mode, summary["status"], [(row["name"], row["status"]) for row in summary["steps"]]
                    )
                    assert summary["roster_date"] == DAY
                    assert summary["selected_patient_count"] == 4
                    assert summary["soap_response_count"] == (4 if mode == "complete" else 2)
                    selection = json.loads(archive.read("parsed/soap/selection.json"))
                    assert selection["shared_count"] == selection["unclassified_count"] == 1
                    record = json.loads(archive.read("parsed/soap/patients/0001/soap_0001.json"))
                    assert record["subjective"] == "synthetic complaint"
                    assert record["objective"] == "synthetic examination"
                    assert "synthetic assessment" in record["assessment_plan"]
                    assert record["diagnoses"] and record["orders"] and record["medications"]
                    assert any(CHRONIC_NOTICE in block
                               for block in record["unclassified_blocks"])
                    assert record["chronic_prescription_periods"][0]["start_date"] == DAY
                    assert record["chronic_prescription_periods"][0]["end_date"] == "2026-12-14"
                    assert record["chronic_prescription_periods"][0]["source_block_index"] >= 0
                    field_coverage = json.loads(archive.read("parsed/soap/field_coverage.json"))
                    assert field_coverage["field_record_counts"]["chronic_prescription_periods"] == 1
                    assert "parsed/soap/field_coverage.json" in archive.namelist()
                    assert "responses/" in " ".join(archive.namelist())
                assert state["roster_posts"] == 1 and state["login_posts"] == 1
                assert state["queried_mrns"] == set(PATIENTS)
                assert set(state["soap_mrns"]) == (
                    set(PATIENTS) if mode == "complete" else {"SOAP1001", "SOAP1003", "SOAP1004"}
                )
                assert process.returncode == (0 if mode == "complete" else 1)
                results.append({
                    "mode": mode, "status": summary["status"],
                    "selected_patients": summary["selected_patient_count"],
                    "soap_responses": summary["soap_response_count"],
                    "single_login": state["login_posts"] == 1,
                })
    finally:
        helper.tearDown()
    (output / "exe-soap-verification.json").write_text(
        json.dumps(results, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
