"""Run the no-argument EXE against synthetic eye reports on loopback HTTPS."""

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

from test_tls import LegacyAesLoopbackTests  # noqa: E402
from verify_live_test_exe import SyntheticIntranet  # noqa: E402

from vghks_sdk.live.profile import LIVE_TEST_MRN  # noqa: E402
from vghks_sdk.offline.analyze import inspect_bundle  # noqa: E402
from vghks_sdk.offline.bundle import BundleReader  # noqa: E402


class EyeIntranet(SyntheticIntranet):
    def binary_reply(self, content, mime):
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def dispatch(self):
        state = self.server.test_state
        path = urlsplit(self.path).path
        state["paths"].append(path)
        if not self.headers.get("User-Agent", "").startswith("Mozilla/5.0"):
            state["bad_headers"].append(path)
        handled = {
            "/PRQWeb/QueryOrderResult.do",
            "/PRQWeb/QueryCaseList.do",
            "/PRQWeb/QueryOrderDetail.do",
            "/PRQWeb/QueryReportByOrder.do",
            "/PRQWeb/Adm_QueryPACS.do",
            "/PRQWeb/Page/JSP/showPDF.jsp",
            "/PRQWeb/Page/JSP/showPACSPic.jsp",
        }
        if path not in handled:
            return super().dispatch()
        params = dict(parse_qsl(urlsplit(self.path).query))
        if self.command == "POST":
            params.update(
                parse_qsl(self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode())
            )
        if path.endswith("QueryCaseList.do"):
            query = urlencode(
                {
                    "hhisnum": LIVE_TEST_MRN,
                    "caseType": "O",
                    "caseNo": "EYE",
                    "caseSec": "70",
                    "caseSectC": "眼科",
                    "caseDT": "2026-09-19",
                    "index": "0",
                }
            )
            return self.reply(
                f'<div id="typeO"><a href="/PRQWeb/QueryCaseDetail.do?{query}">case</a></div>'
            )
        if path.endswith("QueryOrderResult.do"):
            body = '<div id="orderList"></div>'
            for seq, name, status in (
                ("U1", "DBR", "未執行"),
                ("D1", "DBR, free charge", "完成"),
                ("M1", "Microsonography", "完成"),
                ("X1", "Other examination", "完成"),
            ):
                query = urlencode(
                    {
                        "hhisnum": LIVE_TEST_MRN,
                        "caseType": "O",
                        "caseNo": "EYE",
                        "seqNo": seq,
                        "orDept": "CHK",
                    }
                )
                body += f"""<script>var orderStr='<a href="/PRQWeb/QueryOrderDetail.do?{query}">{name}</a>';
                var report='/PRQWeb/QueryReportByOrder.do?{query}';
                new KSCase('',orderStr,'2026-09-19','','','{status}','');</script>"""
            return self.reply(body)
        if path.endswith("QueryOrderDetail.do"):
            state["order_sequences"].append(params.get("seqNo"))
            return self.reply(
                "<table><tr><th>醫囑名稱</th><td>Synthetic eye examination</td></tr></table>"
            )
        if path.endswith("QueryReportByOrder.do"):
            seq = params["seqNo"]
            state["report_sequences"].append(seq)
            if seq == "D1":
                attachments = "".join(
                    f'<script>var f="//nfs01p/EMRU/{LIVE_TEST_MRN}/{name}.pdf";</script>'
                    for name in ("bad", "good")
                )
                return self.reply(
                    f'<table><tr><th>報告內容 JPG</th><td>詳見附件{attachments}<span reqno="DBR" hhisnum="{LIVE_TEST_MRN}">JPG</span></td></tr></table>'
                )
            return self.reply(
                f'<table><tr><th>報告內容 JPG</th><td><pre>Synthetic result 23.5</pre><button reqno="MICRO" hhisnum="{LIVE_TEST_MRN}">JPG</button></td></tr></table>'
            )
        if path.endswith("Adm_QueryPACS.do"):
            if params["reqno"] == "DBR":
                return self.reply('<div id="pacsContent">查無資料!</div>')
            images = "".join(
                f'<img class="pacsJpg" src="/PRQWeb/Page/JSP/showPACSPic.jsp?hhisnum={LIVE_TEST_MRN}&reqno=MICRO&uid={n}&SERIES_UID=&STUDY_UID=">'
                for n in range(3)
            )
            return self.reply(f'<div id="pacsContent">{images}</div>')
        if path.endswith("showPDF.jsp"):
            if "bad" in params["fileName"]:
                return self.reply('<html><embed type="application/pdf"></html>')
            return self.binary_reply(b"%PDF-1.3\nSynthetic only\n%%EOF", "application/pdf")
        if path.endswith("showPACSPic.jsp"):
            assert self.headers.get("Accept", "").startswith("image/")
            assert "/Adm_QueryPACS.do?" in self.headers.get("Referer", "")
            state["image_ids"].append(params["uid"])
            if params["uid"] == "0":
                return self.binary_reply(b"broken image", "image/jpeg")
            return self.binary_reply(b"\xff\xd8synthetic\xff\xd9", "image/jpeg")


def main():
    output = ROOT / "output"
    if output.is_symlink() or output.resolve().parent != ROOT:
        raise SystemExit("Output escapes workspace")
    helper = LegacyAesLoopbackTests()
    helper.setUp()
    helper.server.RequestHandlerClass = EyeIntranet
    origin = f"https://localhost:{helper.server.server_port}"
    helper.server.test_state = state = {
        "origin": origin,
        "reject_login": False,
        "login_posts": 0,
        "queried_mrns": set(),
        "paths": [],
        "bad_headers": [],
        "order_sequences": [],
        "report_sequences": [],
        "image_ids": [],
    }
    try:
        with tempfile.TemporaryDirectory(prefix="eye-exe-check-", dir=output) as temporary:
            directory = Path(temporary).resolve()
            if not directory.is_relative_to(output.resolve()):
                raise SystemExit("Temporary path escapes output")
            executable = Path(shutil.copy2(ROOT / "dist/vghks-live-test.exe", directory))
            # Explicit focused retest; double-click now runs the built-in round.
            # Other routes are disabled so unintended queries cannot pass.
            config = {
                "schema_version": 5,
                "profile": "ophthalmology",
                "ca_bundle": helper.ca,
                "endpoint_overrides": {
                    key + "_base_url": origin + path
                    for key, path in {
                        "portal": "",
                        "prq": "/PRQWeb",
                        "sectord": "/disabled/SectOrdWeb",
                        "webmaas": "/disabled/webmaas",
                        "oppl": "/disabled/OPPLWeb",
                        "audit": "/disabled/Audit",
                        "mis": "/disabled/mis",
                        "review": "/disabled/Pck",
                    }.items()
                },
                "request_policy": {
                    "min_delay_seconds": 0,
                    "max_delay_seconds": 0,
                    "max_attempts": 1,
                    "connect_timeout_seconds": 0.3,
                    "read_timeout_seconds": 2,
                },
            }
            (directory / "live-test-config.json").write_text(json.dumps(config), encoding="utf-8")
            env = {
                key: val
                for key, val in os.environ.items()
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
            env.update(
                VGHKS_USERNAME="SYNTHETIC",
                VGHKS_PASSWORD="SYNTHETIC-ONLY",
                    VGHKS_TEST_MRN="0000000",
                NO_PROXY="localhost,127.0.0.1",
            )
            process = subprocess.run(
                [str(executable), "--config", "live-test-config.json", "--non-interactive"],
                cwd=directory,
                env=env,
                input="\n",
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=90,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            (output / "exe-ophthalmology.log").write_text(
                process.stdout + process.stderr, encoding="utf-8"
            )
            assert process.returncode == 1, process.returncode  # Two intentional asset failures.
            archives = list(directory.glob("*.zip"))
            assert len(archives) == 1 and not list(directory.glob("*.sha256"))
            with zipfile.ZipFile(archives[0]) as archive:

                def read(path):
                    return json.loads(archive.read(path))

                summary = read("run_summary.json")
                manifest = read("parsed/workflows/ophthalmology_orders/manifest.json")
                assert summary["profile"] == "ophthalmology"
                assert summary["status"] == "COMPLETED_WITH_ERRORS"
                assert manifest["counts"]["skipped_not_executed"] == 1
                assert manifest["counts"]["query_errors"] == 2
                assert manifest["counts"]["reports_with_text"] == 1
                assert manifest["counts"]["pdf_files"] == 1
                assert manifest["counts"]["jpg_files"] == 2
                assert manifest["counts"]["empty_studies"] == 1
                assert all(not item.flag_bits & 1 for item in archive.infolist())
                assert sorted(state["report_sequences"]) == ["D1", "M1"]
                assert sorted(state["order_sequences"]) == ["D1", "M1"]
                assert state["image_ids"] == ["0", "1", "2"]
                assert not state["bad_headers"]
                assert not any(path.startswith("/disabled/") for path in state["paths"])
                assert not any(path.endswith("QueryResTextCenter.do") for path in state["paths"])
                assert not any(path.endswith("QueryUploadMR.do") for path in state["paths"])
            with BundleReader(archives[0]) as reader:
                analysis, recipe = inspect_bundle(reader)
                assert analysis["ophthalmology_orders"]["counts"]["jpg_files"] == 2
                assert recipe["profile"] == "ophthalmology"
            results = {
                "build": json.loads((output / "build-info.json").read_text(encoding="utf-8")),
                "default_profile": summary["profile"],
                "counts": manifest["counts"],
                "only_portal_and_prq": True,
                "headers_checked": True,
                "independent_asset_failures_checked": True,
                "no_data_is_empty": True,
                "plain_zip_beside_exe": True,
                "offline_recipe_keeps_eye_profile": True,
            }
            (output / "exe-ophthalmology-verification.json").write_text(
                json.dumps(results, indent=2) + "\n", encoding="utf-8"
            )
    finally:
        helper.doCleanups()
    print(
        "Frozen EXE: eye order discovery, skip/no-data/text/PDF/JPG branches, headers and ZIP verified on localhost."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
