"""Verify historical DBR downloads and offline replay on synthetic localhost HTTPS."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import zipfile
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests"), str(ROOT / "tools")]

from test_binary_responses import JPEG, LOGIN, PDF  # noqa: E402
from test_tls import LegacyAesLoopbackTests  # noqa: E402
from verify_ophthalmology_exe import EyeIntranet  # noqa: E402

from vghks_sdk import __version__  # noqa: E402
from vghks_sdk.live.config import LIVE_CONFIG_SCHEMA_VERSION  # noqa: E402
from vghks_sdk.live.defaults import SYNTHETIC_MRN  # noqa: E402

MODES = ("normal", "missing_mime", "wrong_mime", "invalid_pdf", "html_as_pdf",
         "expired_once", "expired_twice", "http_denied", "no_dbr", "no_pdf")
ERRORS = {"invalid_pdf": "PDF_BINARY_INVALID", "html_as_pdf": "AUTH_RELOGIN_FAILED",
          "expired_twice": "AUTH_RELOGIN_FAILED", "http_denied": "AUTH_RELOGIN_FAILED"}


class DbrIntranet(EyeIntranet):
    def dispatch(self):
        path = urlsplit(self.path).path
        state = self.server.test_state
        if path == "/login.do" and self.command == "GET":
            return self.binary_reply(LOGIN, "text/html; charset=utf-8")
        if path in {"/PRQWeb/QueryCaseList.do", "/PRQWeb/QueryBillingSOAP.do",
                    "/PRQWeb/MRUploadFile.do", "/PRQWeb/QueryUploadMR.do"}:
            state["forbidden"].append(path)
            return self.reply("unexpected query outside historical DBR scope", 500)
        handled = {"/PRQWeb/QueryOrderResult.do", "/PRQWeb/QueryReportByOrder.do",
                   "/PRQWeb/Page/JSP/showPDF.jsp", "/PRQWeb/Adm_QueryPACS.do",
                   "/PRQWeb/Page/JSP/showPACSPic.jsp"}
        if path not in handled:
            return super().dispatch()
        state["paths"].append(path)
        params = dict(parse_qsl(urlsplit(self.path).query))
        if self.command == "POST":
            params.update(parse_qsl(self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode()))
        mode = state["mode"]
        if path.endswith("QueryOrderResult.do"):
            state["history_filters"].append({k: params.get(k) for k in ("Use", "date", "ordertype")})
            rows = [("D1", "DBR", "2017-09-01", "完成"),
                    ("D2", "DBR, free charge", "2026-09-19", "完成"),
                    ("U1", "DBR", "2026-09-20", "未執行"),
                    ("M1", "Microsonography", "2026-09-21", "完成")]
            if mode == "no_dbr":
                rows = rows[-1:]
            body = '<div id="orderList"></div>'
            for seq, name, day, status in rows:
                query = urlencode({"hhisnum": SYNTHETIC_MRN, "caseType": "O",
                                   "caseNo": "EYE", "seqNo": seq, "orDept": "CHK"})
                body += f"""<script>var orderStr='<a href="/PRQWeb/QueryOrderDetail.do?{query}">{name}</a>';
                var report='/PRQWeb/QueryReportByOrder.do?{query}';
                new KSCase('',orderStr,'{day}','','','{status}','');</script>"""
            return self.reply(body)
        if path.endswith("QueryReportByOrder.do"):
            seq = params["seqNo"]
            state["report_sequences"].append(seq)
            attachment = ("" if mode == "no_pdf" else
                          f'<script>var f="//nfs01p/EMRU/{SYNTHETIC_MRN}/{seq}.pdf";</script>')
            jpg = (f'<span reqno="DBR-JPG" hhisnum="{SYNTHETIC_MRN}">JPG</span>'
                   if seq == "D2" else "")
            return self.reply('<table><tr><th>報告內容</th><td><pre>Synthetic DBR report</pre>'
                              + attachment + jpg + '</td></tr></table>')
        if path.endswith("showPDF.jsp"):
            seq = "D1" if "D1.pdf" in params["fileName"] else "D2"
            state["pdf_attempts"][seq] = state["pdf_attempts"].get(seq, 0) + 1
            if seq == "D1":
                if mode in {"expired_once", "expired_twice"} and (
                        mode == "expired_twice" or state["pdf_attempts"][seq] == 1):
                    return self.reply("", 302, location="/login.do")
                if mode == "http_denied":
                    return self.reply("synthetic denied", 403)
                if mode == "html_as_pdf":
                    return self.binary_reply(LOGIN, "application/pdf")
                if mode == "invalid_pdf":
                    return self.binary_reply(PDF.removesuffix(b"%%EOF\n"), "application/pdf")
            return self.binary_reply(PDF, "" if mode == "missing_mime" else
                                     "text/html" if mode == "wrong_mime" else "application/pdf")
        if path.endswith("Adm_QueryPACS.do"):
            query = urlencode({"hhisnum": SYNTHETIC_MRN, "reqno": "DBR-JPG", "uid": "1",
                               "SERIES_UID": "", "STUDY_UID": ""})
            return self.reply(f'<div id="pacsContent"><img class="pacsJpg" src="/PRQWeb/Page/JSP/showPACSPic.jsp?{query}"></div>')
        if path.endswith("showPACSPic.jsp"):
            assert self.headers.get("Accept", "").startswith("image/")
            assert "/Adm_QueryPACS.do?" in self.headers.get("Referer", "")
            state["image_ids"].append(params["uid"])
            return self.binary_reply(JPEG, "image/jpeg")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", action="store_true")
    parser.add_argument("--exe", type=Path, default=ROOT / "dist/vghks-live-test.exe")
    args = parser.parse_args()
    output = ROOT / "output"
    output.mkdir(exist_ok=True)
    if output.is_symlink() or output.resolve().parent != ROOT:
        raise SystemExit("Output escapes workspace")
    results = []
    for mode in MODES:
        helper = LegacyAesLoopbackTests()
        helper.setUp()
        helper.server.RequestHandlerClass = DbrIntranet
        origin = f"https://localhost:{helper.server.server_port}"
        helper.server.test_state = state = {
            "origin": origin, "mode": mode, "reject_login": False, "login_posts": 0,
            "queried_mrns": set(), "paths": [], "bad_headers": [], "order_sequences": [],
            "report_sequences": [], "image_ids": [], "forbidden": [],
            "history_filters": [], "pdf_attempts": {},
        }
        # localhost may resolve IPv6 first. Serve both loopback addresses so
        # the direct TCP diagnostic and Requests test the same synthetic host.
        class IPv6Server(type(helper.server)):
            address_family = socket.AF_INET6

        ipv6 = IPv6Server(("::1", helper.server.server_port), DbrIntranet)
        ipv6.test_state = state
        helper.addCleanup(ipv6.server_close)
        helper.addCleanup(ipv6.shutdown)
        threading.Thread(target=ipv6.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        try:
            with tempfile.TemporaryDirectory(prefix=f"dbr-{mode}-", dir=output) as temporary:
                root = Path(temporary).resolve()
                if not root.is_relative_to(output.resolve()):
                    raise SystemExit("Temporary path escapes output")
                config = {
                    "schema_version": LIVE_CONFIG_SCHEMA_VERSION, "profile": "dbr", "test_mrn": SYNTHETIC_MRN,
                    "ca_bundle": helper.ca,
                    "endpoint_overrides": {
                        key + "_base_url": origin + path for key, path in {
                            "portal": "", "prq": "/PRQWeb", "sectord": "/disabled/SectOrdWeb",
                            "webmaas": "/disabled/webmaas", "oppl": "/disabled/OPPLWeb",
                            "audit": "/disabled/Audit", "mis": "/disabled/mis",
                            "review": "/disabled/Pck", "personnel": "/disabled/DDPortal",
                        }.items()
                    },
                    "request_policy": {"min_delay_seconds": 0, "max_delay_seconds": 0,
                                       "max_attempts": 1, "connect_timeout_seconds": 0.3,
                                       "read_timeout_seconds": 2},
                }
                config_path = root / "config.json"
                config_path.write_text(json.dumps(config), encoding="utf-8")
                env = {k: v for k, v in os.environ.items() if not k.upper().startswith("VGHKS_")
                       and k.upper() not in {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
                                             "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"}}
                env.update(VGHKS_USERNAME="SYNTHETIC", VGHKS_PASSWORD="SYNTHETIC-ONLY",
                           PYTHONPATH=str(ROOT / "src"), PYTHONIOENCODING="utf-8",
                           NO_PROXY="localhost,127.0.0.1")
                if args.source:
                    command = [sys.executable, "-m", "vghks_sdk.live_test_app", "--profile", "dbr"]
                else:
                    executable = Path(shutil.copy2(args.exe.resolve(), root / "vghks-live-test.exe"))
                    command = [str(executable)]
                    checked = subprocess.run([*command, "--plan"], cwd=root, env=env,
                                             capture_output=True, text=True, encoding="utf-8", timeout=30,
                                             creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                    assert checked.returncode == 0, checked.stdout + checked.stderr
                    assert json.loads(checked.stdout)["profile"] == "dbr"
                run_root = root / "run"
                launch = [*command, "--config", str(config_path), "--non-interactive", "--output", str(run_root)]
                if not args.source and mode == "normal":
                    # Exercise the exact double-click entry with no CLI args.
                    # Endpoint/policy environment values keep it on localhost;
                    # a stale sidecar/profile must not expand the built scope.
                    env.update(VGHKS_TEST_MRN=SYNTHETIC_MRN, VGHKS_CA_BUNDLE=helper.ca,
                               VGHKS_LIVE_OUTPUT_ROOT=str(root / "runs"), VGHKS_LIVE_PROFILE="full",
                               VGHKS_DELAY_MIN="0", VGHKS_DELAY_MAX="0", VGHKS_MAX_ATTEMPTS="1",
                               VGHKS_CONNECT_TIMEOUT="0.3", VGHKS_READ_TIMEOUT="2")
                    for key, endpoint in config["endpoint_overrides"].items():
                        env["VGHKS_" + key.upper()] = endpoint
                    (root / "live-test-config.json").write_text(
                        json.dumps({"profile": "full", "download_assets": False}), encoding="utf-8")
                    launch = command
                process = subprocess.run(
                    launch, input="\n",
                    cwd=root, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
                    timeout=90, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
                (output / f"dbr-{'source' if args.source else 'exe'}-{mode}.log").write_text(
                    process.stdout + process.stderr, encoding="utf-8")
                if not args.source and mode == "normal":
                    summaries = list((root / "runs").glob("runs/*/run_summary.json"))
                    assert len(summaries) == 1, process.stdout + process.stderr
                    run_root = summaries[0].parent
                summary = json.loads((run_root / "run_summary.json").read_text(encoding="utf-8"))
                manifest_path = run_root / "parsed/workflows/ophthalmology_orders/manifest.json"
                if not manifest_path.is_file():
                    # Preserve synthetic bootstrap evidence before temporary
                    # cleanup, rather than obscuring it with a missing file.
                    for archive in root.glob("*.zip"):
                        shutil.copy2(archive, output / f"dbr-{'source' if args.source else 'exe'}-{mode}-bootstrap-failed.zip")
                    raise AssertionError((mode, summary["status"], process.stdout, process.stderr))
                evidence = json.loads(manifest_path.read_text(encoding="utf-8"))
                failed_audits = [json.loads(path.read_text(encoding="utf-8")) for path in
                                (run_root / "parsed/workflows/ophthalmology_orders").rglob("*.outcome.json")]
                failed_audits = [row for row in failed_audits if row.get("status") == "ERROR"]
                (output / f"dbr-{'source' if args.source else 'exe'}-{mode}.json").write_text(
                    json.dumps({"counts": evidence["counts"], "failed_queries": failed_audits,
                                "http": state}, indent=2, default=list), encoding="utf-8")
                assert summary["profile"] == "dbr"
                assert process.returncode == (1 if mode in ERRORS else 0), (mode, failed_audits, process.stdout, process.stderr)
                assert summary["status"] == ("COMPLETED_WITH_ERRORS" if mode in ERRORS else
                                              "COMPLETED_WITH_GAPS" if mode in {"no_dbr", "no_pdf"} else "OK"), (mode, summary)
                assert state["history_filters"] == [{"Use": "Dur", "date": "4000", "ordertype": "*"},
                                                    {"Use": "Dur", "date": "4000", "ordertype": "OR"}]
                assert not state["forbidden"] and not state["bad_headers"]
                assert not any(path.startswith("/disabled/") for path in state["paths"])
                assert sorted(state["report_sequences"]) == ([] if mode == "no_dbr" else ["D1", "D2"])
                assert sorted(state["order_sequences"]) == ([] if mode == "no_dbr" else ["D1", "D2"])
                assert state["login_posts"] == (2 if mode in {"expired_once", "expired_twice", "html_as_pdf", "http_denied"} else 1)
                archives = list(root.glob("*.zip"))
                assert len(archives) == 1 and not list(root.glob("*.sha256"))
                expected_pdfs = 0 if mode in {"no_dbr", "no_pdf"} else 1 if mode in ERRORS else 2
                with zipfile.ZipFile(archives[0]) as bundle:
                    assert bundle.testzip() is None
                    manifest = json.loads(bundle.read("parsed/workflows/ophthalmology_orders/manifest.json"))
                    assert manifest["terms"] == ["DBR"]
                    assert manifest["counts"].get("pdf_files", 0) == expected_pdfs, (mode, manifest)
                    assert manifest["counts"].get("unique_pdf_files", 0) == expected_pdfs
                    assert manifest["counts"].get("jpg_files", 0) == (0 if mode == "no_dbr" else 1)
                    assert manifest["counts"].get("unique_jpg_files", 0) == (0 if mode == "no_dbr" else 1)
                    assert manifest["counts"].get("query_errors", 0) == (1 if mode in ERRORS else 0)
                    assets = [n for n in bundle.namelist() if n.startswith("parsed/") and n.endswith((".pdf", ".jpg"))]
                    for name in assets:
                        assert bundle.read(name) == (PDF if name.endswith(".pdf") else JPEG)
                    if mode in ERRORS:
                        codes = [step.get("issue", {}).get("code") for step in summary["steps"] if step.get("issue")]
                        assert ERRORS[mode] in codes, (mode, codes)
                analyzed = subprocess.run(
                    [*command, "--analyze", str(archives[0]), "--analysis-output", str(root / "analysis")],
                    cwd=root, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
                    timeout=90, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
                assert analyzed.returncode in {0, 1}, (mode, analyzed.stdout, analyzed.stderr)
                analysis = json.loads((root / "analysis/analysis.json").read_text(encoding="utf-8"))
                recipe = json.loads((root / "analysis/retest-config.json").read_text(encoding="utf-8"))
                assert recipe["profile"] == "dbr" and recipe["asset_terms"] == ["DBR"]
                assert recipe["login_negative_attempts"] == 0
                assert analysis["ophthalmology_orders"]["counts"].get("pdf_files", 0) == expected_pdfs
                assert analysis["order_assets"]["status"] == ("NO_ASSETS" if mode == "no_dbr" else "OK")
                assert analysis["order_assets"]["pdf_files"] == expected_pdfs
                assert analysis["order_assets"]["jpg_files"] == (0 if mode == "no_dbr" else 1)
                assert analysis["order_assets"]["total_bytes"] == manifest["counts"].get("downloaded_bytes", 0)
                (output / f"dbr-{'source' if args.source else 'exe'}-{mode}-analysis.json").write_text(
                    json.dumps(analysis, indent=2), encoding="utf-8")
                if mode == "expired_once":
                    # A successful download does not erase the original
                    # redirected/login responses from the offline evidence.
                    assert {row["code"] for row in analysis["problems"]} == {
                        "AUTH_SESSION_REDIRECT", "AUTH_SESSION_LOGIN_PAGE",
                    }, analysis["problems"]
                else:
                    assert (not analysis["problems"] if mode not in ERRORS else analysis["problems"]), (mode, analysis["problems"])
                results.append({"mode": mode, "status": summary["status"], "pdf_files": expected_pdfs,
                                "login_posts": state["login_posts"], "pdf_attempts": state["pdf_attempts"],
                                "only_history_dbr": True, "original_bytes_preserved": True,
                                "frozen_offline_cli_verified": not args.source})
        finally:
            helper.doCleanups()
    report = {"sdk_version": __version__, "mode": "source" if args.source else "exe",
              "localhost_only": True, "scenarios": results}
    (output / f"dbr-{'source' if args.source else 'exe'}-verification.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"DBR {'source' if args.source else 'EXE'}: {len(results)} localhost HTTPS scenarios and offline ZIP replay passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
