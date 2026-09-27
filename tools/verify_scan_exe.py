"""Run the frozen one-file scan EXE against a synthetic HTTPS intranet."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests"), str(ROOT / "tools")]

from test_tls import LegacyAesLoopbackTests  # noqa: E402
from test_visit_search import MRN, patient_header, visit_page, visit_row  # noqa: E402
from verify_live_test_exe import SyntheticIntranet  # noqa: E402

PDF_ROOT = f"//HFS01_3A0.vghks.gov.tw/EMRU/8/{MRN}"
PDF = "%PDF-1.4\n% synthetic PDF\n%%EOF\n"


def scan_script(name: str, subtype: str) -> str:
    return (
        "<script>var filepath = encodeURIComponent('"
        + f"{PDF_ROOT}/{name}.pdf"
        + "'); var subtype = '"
        + subtype
        + "'; document.write('showPDF.jsp?fileName=' + encodeURIComponent(filepath));</script>"
    )


class ScanIntranet(SyntheticIntranet):
    def dispatch(self):
        address = urlsplit(self.path)
        state = self.server.test_state
        if address.path == "/PRQWeb/Page/JSP/KS_Patient.jsp":
            return self.reply(patient_header())
        if address.path == "/PRQWeb/QueryCaseList.do":
            assert state["mrn"] == MRN
            return self.reply(visit_page(visit_row()))
        if address.path == "/PRQWeb/QueryBillingSOAP.do":
            assert state["mrn"] == MRN
            state["soap_mrns"].append(MRN)
            return self.reply(
                '<div id="data"><div class="soap"><pre>S: synthetic visit</pre></div></div>'
                + scan_script("case", "RECORD")
            )
        if address.path == "/PRQWeb/MRUploadFile.do":
            assert self.command == "POST"
            fields = dict(parse_qsl(self.rfile.read(int(self.headers["Content-Length"])).decode()))
            assert fields["reqCode"] == "getUploadFile" and fields["days"] == "*"
            state["history_requests"] += 1
            return self.reply(
                '<table id="tbObj"><tr><th>Category</th></tr></table>'
                + scan_script("ordinary", "RECORD")
                + scan_script("eye-one", "OPG")
                + scan_script("eye-two", "OPG")
            )
        if address.path == "/PRQWeb/Page/JSP/showPDF.jsp":
            params = dict(parse_qsl(address.query))
            assert params["hhisnum"] == MRN
            path = unquote(params["fileName"])
            assert path.startswith(PDF_ROOT) and path.endswith(".pdf")
            state["pdf_paths"].append(path)
            return self.reply(PDF)
        return super().dispatch()


def main() -> int:
    output = ROOT / "output"
    if output.is_symlink() or output.resolve().parent != ROOT:
        raise SystemExit("Output directory escapes workspace")
    output.mkdir(exist_ok=True)
    executable = ROOT / "dist" / "vghks-live-test.exe"
    helper = LegacyAesLoopbackTests()
    helper.setUp()
    helper.server.RequestHandlerClass = ScanIntranet
    origin = f"https://localhost:{helper.server.server_port}"
    try:
        with tempfile.TemporaryDirectory(prefix="scan-exe-", dir=output) as temporary:
            directory = Path(temporary).resolve()
            if not directory.is_relative_to(output.resolve()):
                raise SystemExit("Temporary directory escapes output")
            copied = Path(shutil.copy2(executable, directory / executable.name))
            environment = {
                key: value
                for key, value in os.environ.items()
                if not key.upper().startswith("VGHKS_")
                and key.upper()
                not in {
                    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
                    "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
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
                VGHKS_PORTAL_BASE_URL=origin,
                VGHKS_PRQ_BASE_URL=origin + "/PRQWeb",
                NO_PROXY="localhost,127.0.0.1",
            )
            plan = subprocess.run(
                [str(copied), "--plan"], cwd=directory, env=environment,
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=30, creationflags=subprocess.CREATE_NO_WINDOW,
            )
            assert plan.returncode == 0, plan.stderr
            planned = json.loads(plan.stdout)
            assert planned["profile"] == "scans"
            assert [row["key"] for row in planned["operations"]] == [
                "prq.visit_cases", "prq.soap", "prq.upload_history", "prq.pdf_attachment"
            ]
            assert planned["max_cases"] == 6 and planned["max_items_per_operation"] == 4
            state = helper.server.test_state = {
                "origin": origin,
                "reject_login": False,
                "login_posts": 0,
                "queried_mrns": set(),
                "soap_mrns": [],
                "history_requests": 0,
                "pdf_paths": [],
            }
            process = subprocess.run(
                [str(copied)], cwd=directory, env=environment,
                input="\n\n", capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=120,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            (output / "exe-scans.log").write_text(
                process.stdout + process.stderr, encoding="utf-8"
            )
            archives = list(directory.glob("vghks-live-test-*.zip"))
            assert process.returncode == 0 and len(archives) == 1, (
                process.returncode, process.stderr[-1000:], archives
            )
            with zipfile.ZipFile(archives[0]) as archive:
                assert archive.testzip() is None
                summary = json.loads(archive.read("run_summary.json"))
                assert summary["profile"] == "scans" and summary["status"] == "OK"
                history = json.loads(archive.read("parsed/atomic/prq.upload_history/0001.json"))
                assert [row["record_type"] for row in history["scanned_records"]] == [
                    "RECORD", "OPG", "OPG"
                ]
                soap = json.loads(archive.read("parsed/atomic/prq.soap/0001.json"))
                assert len(soap["scanned_pdf_refs"]) == 1
                inputs = json.loads(archive.read("parsed/inputs/prq.pdf_attachment.json"))
                assert [row["ref"]["file_path"].rsplit("/", 1)[-1] for row in inputs] == [
                    "eye-one.pdf", "case.pdf", "eye-two.pdf"
                ]
                pdfs = sorted(name for name in archive.namelist() if name.endswith(".pdf"))
                assert len(pdfs) == 3
                assert all(archive.read(name).startswith(b"%PDF") for name in pdfs)
                assert [path.rsplit("/", 1)[-1] for path in state["pdf_paths"]] == [
                    "eye-one.pdf", "case.pdf", "eye-two.pdf"
                ]
                assert state["login_posts"] == 1 and state["history_requests"] == 1
                assert state["soap_mrns"] == [MRN]
                captures = [
                    json.loads(line)
                    for line in archive.read("capture_manifest.jsonl").splitlines()
                ]
                assert all(
                    urlsplit(row.get("request", {}).get("url", "")).hostname in {None, "localhost"}
                    for row in captures
                )
    finally:
        helper.tearDown()
    (output / "exe-scan-verification.json").write_text(
        json.dumps({"profile": "scans", "status": "OK", "pdf_downloads": 3}, indent=2)
        + "\n", encoding="utf-8"
    )
    print("Frozen scan EXE: zero-argument run, case/history references and PDFs verified on localhost.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
