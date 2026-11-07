"""Exercise PACS preparation and native handoff on synthetic localhost servers only."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import zipfile
from dataclasses import fields
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests"), str(ROOT / "tools")]

from test_pacs_launch_parsing import bootstrap_html, viewer_html  # noqa: E402
from test_tls import LegacyAesLoopbackTests  # noqa: E402
from verify_live_test_exe import SyntheticIntranet  # noqa: E402

from vghks_sdk import SDKSettings, __version__  # noqa: E402

MODES = ("prepare", "handoff", "login_error", "patient_mismatch", "missing_uid",
         "bootstrap_redirect", "native_redirect", "native_http_error", "native_disconnect",
         "menu_handoff", "menu_empty", "menu_cancel", "menu_invalid", "menu_partial",
         "menu_cross_patient", "menu_prepare", "menu_more", "menu_pending", "missing_reference",
         "bridge_missing", "bridge_wrong_owner")
_MENU_HANDOFFS = {"menu_handoff", "menu_invalid", "menu_partial", "menu_more", "menu_pending"}
_HISTORY_PATHS = {
    "/PRQWeb/QueryPatientRecord.do", "/PRQWeb/Page/JSP/OrderC.jsp",
    "/PRQWeb/Page/JSP/OrderC_Select.jsp", "/PRQWeb/QueryOrderResult.do",
    "/PRQWeb/QueryOrderDetail.do",
}


def history_html(mode: str) -> str:
    if mode == "menu_empty":
        return "<script>var aryCase=[];</script>"
    sequences = ([f"JPG{number:02d}" for number in range(30)] + ["READY"]
                 if mode == "menu_more" else ["READY", "JPG", "UNEXECUTED"])
    if mode == "menu_pending":
        sequences = ["READY", *(f"JPG{number:02d}" for number in range(30))]
    if mode == "menu_partial":
        sequences.insert(0, "BROKEN")
    body = "<script>var aryCase=[];</script>"
    for index, sequence in enumerate(sequences):
        query = urlencode({"hhisnum": "0000000", "caseType": "O", "caseNo": "SYNTHETIC",
                           "seqNo": sequence})
        day = (date(2026, 11, 6) - timedelta(days=index)).isoformat()
        status = "未執行" if sequence == "UNEXECUTED" else "完成"
        body += (f'<script>var orderStr=\'<a href="/PRQWeb/QueryOrderDetail.do?{query}">'
                 f'Synthetic examination {sequence}</a>\';'
                 f"new KSCase('',orderStr,'{day}','','','{status}','');</script>")
    return body


class Portal(SyntheticIntranet):
    def dispatch(self):
        address = urlsplit(self.path)
        path = address.path
        state = self.server.test_state
        if path in _HISTORY_PATHS:
            state["history_paths"].append(path)
            query = parse_qs(address.query, keep_blank_values=True)
            if self.command == "POST":
                query.update(parse_qs(self.rfile.read(
                    int(self.headers.get("Content-Length", "0"))).decode(), keep_blank_values=True))
            if path.endswith("QueryPatientRecord.do"):
                assert query["id"] == ["0000000"] and query["Use"] == ["Dur"]
                return self.reply('<frameset><frame src="/PRQWeb/Page/JSP/KS_Patient.jsp">'
                                  '<frame src="/PRQWeb/QueryCaseList.do"></frameset>')
            assert query["hhisnum"] == ["0000000"]
            if path.endswith("QueryOrderResult.do"):
                assert query["date"] == ["4000"] and query["ordertype"] in (["*"], ["OR"])
                state["history_categories"].append(query["ordertype"][0])
                return self.reply(history_html(state["mode"]))
            if path.endswith("QueryOrderDetail.do"):
                sequence = query["seqNo"][0]
                state["detail_sequences"].append(sequence)
                assert sequence != "UNEXECUTED", "unexecuted order should not be inspected"
                if sequence == "BROKEN":
                    return self.reply("synthetic detail failure", 500)
                body = '<table><tr><th>Order name</th><td>Synthetic examination</td></tr></table>'
                if sequence.startswith("JPG"):
                    return self.reply(body + '<button id="pacsBtn2" hhisnum="0000000" '
                                      'reqno="JPEG-ONLY">JPG</button>')
                mrn = "9999999" if state["mode"] == "menu_cross_patient" else "0000000"
                return self.reply(body + f'<button id="pacsBtn" hhisnum="{mrn}" '
                                  'reqno="SYNTHETIC-ORDER">PACS</button>')
            return self.reply("<html>synthetic order selector</html>")
        allowed = {"/index.do", "/login.do", "/myPortal.do", "/ssoLogAdd.do",
                   "/aptreePath.do", "/sessionCheck.do", "/ssoFromDn.do",
                   "/PRQWeb/WPSAutoLogon", "/PRQWeb/landing.do"}
        if path not in allowed:
            self.server.test_state["unexpected"].append(path)
            if self.command == "POST":
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
            return self.reply("unexpected synthetic request", 500)
        return super().dispatch()


class WonderAndBridge(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, body: str, status: int = 200, location: str = "") -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        if location:
            self.send_header("Location", location)
        self.end_headers()
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            self.wfile.write(data)

    def do_GET(self):
        state = self.server.test_state
        address = urlsplit(self.path)
        query = parse_qs(address.query, keep_blank_values=True)
        path, mode = address.path, state["mode"]
        state["paths"].append(path)
        if path.endswith("/Query/qryExternalAP.jsp"):
            assert query == {"LoginID": ["SYNTHETIC"], "PatID": ["0000000"],
                             "AccNo": ["SYNTHETIC-ORDER"], "LoginLevel": ["RAD"]}
            if mode == "bootstrap_redirect":
                return self.reply("", 302, "/forbidden")
            return self.reply(bootstrap_html(
                mrn="9999999" if mode == "patient_mismatch" else "0000000",
                study_uid="" if mode == "missing_uid" else "1.2.3.4",
            ))
        if path.endswith("/Viewer/vwrIris.jsp"):
            assert query == {"LoginID": ["SYNTHETIC"], "WonderClientIP": ["192.0.2.10"]}
            return self.reply(viewer_html())
        if path in {"/login", "/wonderurl", "/setanonymousmode", "/cmove"}:
            assert not self.headers.get("Cookie"), "portal cookies leaked to native bridge"
            assert not self.headers.get("Authorization"), "authorization leaked to native bridge"
            state["native_paths"].append(path)
            if path == "/login":
                assert query["id"] == ["SYNTHETIC"] and query["pwd"] == [""]
                if mode == "native_redirect":
                    return self.reply("", 302, "/forbidden")
            if path == "/cmove":
                assert query["uid"] == ["1.2.3.4"] and query["command"] == ["1"]
                cmove = parse_qs(urlsplit(query["cmoveurl"][0]).query, keep_blank_values=True)
                assert cmove["PatID"] == ["0000000"]
                assert cmove["AccNo"] == ["SYNTHETIC-ORDER"]
                if mode == "native_http_error":
                    return self.reply("synthetic error", 500)
                if mode == "native_disconnect":
                    self.close_connection = True
                    return
            return self.reply("ok")
        state["unexpected"].append(path)
        return self.reply("unexpected synthetic request", 500)


def verify(mode: str, *, source: bool, executable: Path, output: Path) -> dict:
    helper = LegacyAesLoopbackTests()
    helper.setUp()
    server = ThreadingHTTPServer(("127.0.0.1", 0), WonderAndBridge)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        portal_origin = f"https://localhost:{helper.server.server_port}"
        bridge_origin = f"http://127.0.0.1:{server.server_port}"
        state = {"mode": mode, "origin": portal_origin, "reject_login": mode == "login_error",
                 "login_posts": 0, "paths": [], "native_paths": [], "unexpected": [],
                 "history_paths": [], "history_categories": [], "detail_sequences": []}
        helper.server.RequestHandlerClass = Portal
        helper.server.test_state = server.test_state = state
        with tempfile.TemporaryDirectory(prefix="pacs-launch-verify-", dir=output) as temporary:
            root = Path(temporary).resolve()
            if not root.is_relative_to(output.resolve()):
                raise SystemExit("Verification directory escapes workspace.")
            endpoints = {item.name: portal_origin + "/disabled/" + item.name
                         for item in fields(SDKSettings) if item.name.endswith("_base_url")}
            endpoints.update(portal_base_url=portal_origin, prq_base_url=portal_origin + "/PRQWeb",
                             smartwonder_base_url=bridge_origin + "/SmartWonderRAD")
            download_app = Path(sys.executable).resolve()
            if mode == "bridge_missing":
                download_app = root / "nonexistent" / "DownloadApp.exe"
                assert not download_app.exists()
            elif mode == "bridge_wrong_owner":
                download_app = Path(sys.executable).with_name("pythonw.exe").resolve()
                assert download_app.is_file(), "wrong-owner fixture requires pythonw.exe"
                assert download_app != Path(sys.executable).resolve()
            config = root / "synthetic.json"
            config.write_text(json.dumps({"endpoint_overrides": endpoints,
                                          "bridge_url": bridge_origin, "ca_bundle": helper.ca,
                                          "download_app_path": str(download_app)}),
                              encoding="utf-8")
            env = {key: value for key, value in os.environ.items()
                   if not key.upper().startswith("VGHKS_") and key.upper() not in {
                       "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
                       "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
                   }}
            env.update(VGHKS_USERNAME="SYNTHETIC", VGHKS_PASSWORD="SYNTHETIC-PASSWORD-NOT-SAVED",
                       NO_PROXY="localhost,127.0.0.1", PYTHONPATH=str(ROOT / "src"),
                       PYTHONIOENCODING="utf-8")
            command = ([sys.executable, str(ROOT / "run_pacs_launch_test.py")] if source else
                       [str(executable.resolve())])
            command += ["--loopback-config", str(config), "--output-root", str(root / "results")]
            if mode != "menu_handoff":
                command += ["--mrn", "0000000"]
            menu = mode.startswith("menu_")
            if menu:
                command.append("--interactive")
                if mode == "menu_prepare":
                    command.append("--prepare-only")
            else:
                command.append("--non-interactive")
                if mode != "missing_reference":
                    command += ["--request-no", "SYNTHETIC-ORDER"]
            if not menu and mode not in {"prepare", "missing_reference"}:
                command.append("--open-viewer")
            input_text = {"menu_cancel": "0\n\n", "menu_empty": "\n",
                          "menu_handoff": "0000000\n1\n1\n\n",
                          "menu_cross_patient": "\n", "menu_prepare": "1\n\n",
                          "menu_invalid": "invalid\n999\n1\n1\n\n",
                          "menu_more": "m\n1\n1\n\n"}.get(mode, "1\n1\n\n")
            completed = subprocess.run(command, env=env, cwd=root, capture_output=True,
                                       timeout=90, input=input_text.encode("ascii") if menu else None)
            expected_exit = 0 if mode in {"prepare", "handoff", "menu_empty", "menu_cancel",
                                         "menu_prepare", *_MENU_HANDOFFS - {"menu_partial"}} else 2
            archives = list((root / "results/archives").glob("*.zip"))
            assert len(archives) == 1, (mode, completed.stdout, completed.stderr)
            with zipfile.ZipFile(archives[0]) as archive:
                summary = json.loads(archive.read("run_summary.json"))
                assert completed.returncode == expected_exit, (
                    mode, summary, state["unexpected"], completed.stderr,
                )
                names = archive.namelist()
                if mode == "missing_reference":
                    assert summary["status"] == "BOOTSTRAP_FAILED", summary
                    assert summary["issue"]["code"] == "PACS_SELECTION_REQUIRED", summary
                else:
                    assert summary["sdk_version"] == __version__
                    execution = json.loads(archive.read("parsed/pacs_launch_execution.json"))
                    assert execution["handoff"] == summary["handoff"]
                    assert execution["manual_observation"] == summary["manual_observation"]
                assert summary.get("manual_observation", "NOT_CHECKED") == (
                    "MATCHED" if mode in _MENU_HANDOFFS else "NOT_CHECKED"
                )
                assert "0000000" not in json.dumps(summary)
                assert "SYNTHETIC-ORDER" not in json.dumps(summary)
                for name in names:
                    assert b"SYNTHETIC-PASSWORD-NOT-SAVED" not in archive.read(name), name
                manifest = [json.loads(line) for line in archive.read("capture_manifest.jsonl").splitlines()]
                exchanges = [row for row in manifest if row["kind"] == "HTTP_EXCHANGE"]
                pacs_exchanges = [row for row in exchanges
                                  if urlsplit(row["request"]["url"]).path.startswith("/SmartWonderRAD/")]
                expected_documents = 0 if mode in {
                    "login_error", "menu_empty", "menu_cancel", "menu_cross_patient", "missing_reference",
                    "bridge_missing", "bridge_wrong_owner",
                } else 1 if mode in {
                    "patient_mismatch", "missing_uid", "bootstrap_redirect"
                } else 2
                assert len(pacs_exchanges) == expected_documents, (mode, pacs_exchanges)
                availability = summary.get("bridge_availability")
                if mode in {"prepare", "menu_prepare", "login_error", "menu_empty", "menu_cancel",
                            "menu_cross_patient", "missing_reference"}:
                    assert availability is None, (mode, availability)
                elif mode in {"bridge_missing", "bridge_wrong_owner"}:
                    assert summary["status"] == "BRIDGE_UNAVAILABLE", summary
                    assert summary["prepare"] == "NOT_RUN" and summary["native_calls"] == 0, summary
                    assert availability["status"] == "UNAVAILABLE", availability
                    if mode == "bridge_missing":
                        assert availability["installation"] == "MISSING", availability
                    else:
                        assert availability["installation"] == "PRESENT", availability
                        assert availability["owner"] == "OTHER_PROCESS", availability
                    assert not state["paths"], state["paths"]
                else:
                    assert availability["status"] == "AVAILABLE", (mode, availability)
                native_bodies = [name for name in names if name.startswith("private/native/")
                                 and name.endswith(".body")]
                assert len(native_bodies) == len(state["native_paths"])
                if mode == "handoff" or mode in _MENU_HANDOFFS:
                    assert summary["handoff"] == "HANDOFF_SENT"
                    assert summary["native_result"]["viewer_loaded"] is None
                    assert all(archive.read(name) == b"ok" for name in native_bodies)
                if mode == "menu_partial":
                    assert summary["discovery"]["status"] == "PARTIAL", summary
                    assert summary["status"] == "HANDOFF_SENT_WITH_GAPS", summary
                    assert summary["bundle_status"] == "COMPLETED_WITH_ERRORS", summary
                if mode == "menu_pending":
                    assert summary["discovery"]["pending"] == 1, summary
                    assert summary["bundle_status"] == "COMPLETED_WITH_GAPS", summary
                    assert len(state["detail_sequences"]) == 30, state
                if mode == "menu_empty":
                    assert summary["status"] == "NO_SAMPLE", summary
                if mode == "menu_cross_patient":
                    assert summary["status"] == "DISCOVERY_FAILED", summary
                if mode == "menu_cancel":
                    assert summary["status"] == "CANCELLED", summary
            assert state["login_posts"] == (0 if mode == "missing_reference" else 1), (mode, state)
            assert not state["unexpected"], (mode, state["unexpected"])
            if mode in {"handoff", "native_http_error", "native_disconnect", *_MENU_HANDOFFS}:
                assert state["native_paths"] == ["/login", "/wonderurl", "/setanonymousmode", "/cmove"]
            elif mode == "native_redirect":
                assert state["native_paths"] == ["/login"]
            else:
                assert state["native_paths"] == []
            if menu:
                assert state["history_categories"] == ["*", "OR"], state["history_categories"]
                if mode == "menu_more":
                    assert len(state["detail_sequences"]) == 31, state["detail_sequences"]
                else:
                    assert len(state["detail_sequences"]) == len(set(state["detail_sequences"])), state
            else:
                assert not state["history_paths"]
            return {"mode": mode, "status": "PASS", "native_calls": len(state["native_paths"]),
                    "password_posts": state["login_posts"], "sdk_status": summary["status"],
                    "availability_status": (summary.get("bridge_availability") or {}).get(
                        "status", "NOT_RUN")}
    finally:
        server.shutdown()
        server.server_close()
        helper.doCleanups()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", action="store_true")
    parser.add_argument("--exe", type=Path, default=ROOT / "dist/vghks-pacs-launch-test.exe")
    args = parser.parse_args()
    output = ROOT / "output"
    if output.is_symlink() or output.resolve().parent != ROOT:
        raise SystemExit("Output escapes workspace.")
    output.mkdir(exist_ok=True)
    results = []
    for mode in MODES:
        results.append(verify(mode, source=args.source, executable=args.exe, output=output))
        print(f"PASS {mode}")
    path = output / ("pacs-launch-source-verification.json" if args.source else
                     "pacs-launch-exe-verification.json")
    path.write_text(json.dumps({"sdk_version": __version__, "evidence": "SYNTHETIC_LOCALHOST",
                                "results": results}, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
