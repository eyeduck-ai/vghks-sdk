"""Verify WebMAAS idle/cookie diagnostics using synthetic localhost HTTPS only."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests"), str(ROOT / "tools")]

from test_tls import LegacyAesLoopbackTests  # noqa: E402
from test_webmaas import landing  # noqa: E402
from test_webmaas_session import TIMEOUT_HTML  # noqa: E402
from verify_patient_exe import PatientIntranet  # noqa: E402
from verify_visit_exe import test_state  # noqa: E402

from vghks_sdk._version import __version__  # noqa: E402

MODES = (
    "cookie_ignored", "form_missing", "token_missing", "persistent_missing",
    "expiry_redirect", "http_denied", "relogin_rejected", "no_cookie", "baseline_broken",
    "timeout", "persistent_timeout", "timeout_post",
)


class SessionIntranet(PatientIntranet):
    def reply(self, body, status=200, cookie=False, location=None):
        state = self.server.test_state
        web_cookie = urlsplit(self.path).path == "/webmaas/WPSAutoLogon" and state["mode"] != "no_cookie"
        if not web_cookie:
            return super().reply(body, status, cookie, location)
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Set-Cookie", "JSESSIONID=webmaas-synthetic; Path=/webmaas; Secure")
        if location:
            self.send_header("Location", location)
        self.end_headers()
        self.wfile.write(data)

    def dispatch(self):
        state = self.server.test_state
        mode = state["mode"]
        path = urlsplit(self.path).path
        if path == "/webmaas/comm/pageTimeOut.do":
            return self.reply(TIMEOUT_HTML)
        if path == "/login.do" and self.command == "GET":
            return self.reply('<form><input name="muid"><input name="mpassword" type="password"></form>')
        if path == "/login.do" and self.command == "POST" and mode == "relogin_rejected" and state["login_posts"]:
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            state["login_posts"] += 1
            return self.reply("<h3>登入失敗</h3><p>帳號或密碼錯誤</p>")
        query_page = path in {"/webmaas/RSV/RSV11W001.do", "/webmaas/QUY/QUY15W001.do"}
        has_web_cookie = any(part.strip() == "JSESSIONID=webmaas-synthetic"
                             for part in self.headers.get("Cookie", "").split(";"))
        if query_page and self.command == "POST" and not has_web_cookie and mode == "timeout_post":
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            state["timeout_post_rejections"] += 1
            return self.reply("", 302, location=state["origin"] + "/webmaas/comm/pageTimeOut.do")
        if query_page and self.command == "GET":
            if not has_web_cookie and mode not in {"no_cookie", "cookie_ignored"}:
                state["challenge_seen"] = True
            broken = state["challenge_seen"] and mode == "persistent_missing"
            if ((not has_web_cookie and mode == "timeout") or
                    (state["challenge_seen"] and mode == "persistent_timeout")):
                return self.reply("", 302, location=state["origin"] + "/webmaas/comm/pageTimeOut.do")
            if mode == "baseline_broken" or broken or (not has_web_cookie and mode == "form_missing"):
                return self.reply("<html>synthetic unknown query page</html>")
            if not has_web_cookie and mode == "token_missing":
                return self.reply(landing("QUY15WForm" if "/QUY/" in path else "RSV11WForm", ""))
            if not has_web_cookie and mode in {"expiry_redirect", "relogin_rejected"}:
                return self.reply("", 302, location=state["origin"] + "/login.do")
            if not has_web_cookie and mode == "http_denied":
                return self.reply("synthetic forbidden", 403)
        return super().dispatch()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", action="store_true")
    parser.add_argument("--exe", type=Path, default=ROOT / "dist/vghks-live-test.exe")
    args = parser.parse_args()
    output = ROOT / "output"
    output.mkdir(exist_ok=True)
    if output.is_symlink() or output.resolve().parent != ROOT:
        raise SystemExit("Output escapes workspace")
    helper = LegacyAesLoopbackTests()
    helper.setUp()
    helper.server.RequestHandlerClass = SessionIntranet
    origin = f"https://localhost:{helper.server.server_port}"
    results = []
    try:
        for mode in MODES:
            state = test_state(mode, origin)
            state["challenge_seen"] = False
            state["timeout_post_rejections"] = 0
            helper.server.test_state = state
            with tempfile.TemporaryDirectory(prefix="session-exe-", dir=output) as temporary:
                directory = Path(temporary)
                config = {
                    "schema_version": 7, "profile": "session", "test_mrn": "0000000",
                    "ca_bundle": helper.ca,
                    "endpoint_overrides": {key + "_base_url": origin + path for key, path in {
                        "portal": "", "sectord": "/SectOrdWeb", "webmaas": "/webmaas",
                    }.items()},
                    "request_policy": {"min_delay_seconds": 0, "max_delay_seconds": 0,
                                       "max_attempts": 1, "connect_timeout_seconds": 1,
                                       "read_timeout_seconds": 3},
                }
                config_path = directory / "config.json"
                config_path.write_text(json.dumps(config), encoding="utf-8")
                env = {key: value for key, value in os.environ.items() if not key.upper().startswith("VGHKS_")
                       and key.upper() not in {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
                                               "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"}}
                env.update(VGHKS_USERNAME="SYNTHETIC", VGHKS_PASSWORD="SYNTHETIC-ONLY",
                           NO_PROXY="localhost,127.0.0.1", PYTHONPATH=str(ROOT / "src"),
                           PYTHONIOENCODING="utf-8")
                if args.source:
                    command = [sys.executable, "-m", "vghks_sdk.live_test_app"]
                else:
                    executable = Path(shutil.copy2(args.exe.resolve(), directory / "vghks-live-test.exe"))
                    command = [str(executable)]
                process = subprocess.run(
                    [*command, "--config", str(config_path), "--non-interactive",
                     "--output", str(directory / "run")],
                    cwd=directory, env=env, capture_output=True, text=True, encoding="utf-8",
                    errors="replace", timeout=60,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
                (output / f"session-{'source' if args.source else 'exe'}-{mode}.log").write_text(
                    process.stdout + process.stderr, encoding="utf-8"
                )
                summaries = list((directory / "run").glob("run_summary.json"))
                assert len(summaries) == 1, process.stdout + process.stderr
                summary = json.loads(summaries[0].read_text(encoding="utf-8"))
                expected = "OK" if mode in {"cookie_ignored", "expiry_redirect", "http_denied", "timeout_post"} else (
                    "COMPLETED_WITH_GAPS" if mode == "no_cookie" else "COMPLETED_WITH_ERRORS"
                )
                assert summary["status"] == expected, (mode, summary["status"])
                assert process.returncode == (0 if expected in {"OK", "COMPLETED_WITH_GAPS"} else 1)
                comparison = summary["session_test"]
                assert comparison["natural_ttl"] == {"status": "NOT_TESTED", "verified": False}
                assert state["login_posts"] == (2 if mode in {"expiry_redirect", "http_denied", "relogin_rejected"} else 1)
                assert state["query_posts"] == (0 if mode == "baseline_broken" else
                                                1 if mode in {"persistent_missing", "persistent_timeout", "relogin_rejected", "no_cookie"} else
                                                3 if mode in {"cookie_ignored", "timeout", "timeout_post"} else 2)
                if mode in {"form_missing", "token_missing"}:
                    observation = comparison["observations"][-1]
                    assert observation["sso_recheck_succeeded"]
                    assert any(step.get("issue", {}).get("category") == "PARSE" for step in summary["steps"] if step.get("issue"))
                if mode == "relogin_rejected":
                    assert any(step.get("issue", {}).get("code") == "PORTAL_LOGIN_REJECTED" for step in summary["steps"] if step.get("issue"))
                if mode == "timeout":
                    assert comparison["observations"][-1]["sso_recheck_succeeded"]
                    assert any(step.get("issue", {}).get("code") == "WEBMAAS_SESSION_TIMEOUT"
                               for step in summary["steps"] if step.get("issue"))
                if mode in {"timeout", "timeout_post"}:
                    assert comparison["direct_read_after_cookie_loss"]["status"] == "OK"
                    assert comparison["direct_read_after_cookie_loss"]["exe_retry_attempts"] == 0
                assert state["timeout_post_rejections"] == (1 if mode == "timeout_post" else 0)
                archives = list(directory.glob("*.zip"))
                assert len(archives) == 1
                with zipfile.ZipFile(archives[0]) as archive:
                    assert "parsed/session/comparison.json" in archive.namelist()
                    response_files = [name for name in archive.namelist() if name.startswith("responses/") and name.endswith(".html")]
                    assert response_files
                    if mode in {"form_missing", "persistent_missing", "baseline_broken"}:
                        assert any(b"synthetic unknown query page" in archive.read(name) for name in response_files)
                    if mode in {"timeout", "persistent_timeout", "timeout_post"}:
                        assert any(b"Page time out" in archive.read(name) for name in response_files)
                    traces = [name for name in archive.namelist() if name.endswith("diagnostics.jsonl")]
                    events = [json.loads(line) for name in traces for line in archive.read(name).decode().splitlines()]
                    recovery_events = [row for row in events if row.get("event") == "application_session_recovery_started"]
                    assert len(recovery_events) == (1 if mode in {"timeout", "timeout_post"} else 0)
                    environment = json.loads(archive.read("environment.json"))
                    assert environment["sdk_version"] == __version__
                # Exercise the analyzer inside the actual source/frozen CLI.
                # An operation OK event alone must not hide an unknown page.
                analysis_directory = directory / "analysis"
                analyzed = subprocess.run(
                    [*command, "--analyze-bundle", str(archives[0]),
                     "--analysis-output", str(analysis_directory)],
                    cwd=directory, env=env, capture_output=True, text=True, encoding="utf-8",
                    errors="replace", timeout=60,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
                (output / f"session-{'source' if args.source else 'exe'}-{mode}-analysis.log").write_text(
                    analyzed.stdout + analyzed.stderr, encoding="utf-8"
                )
                analysis = json.loads((analysis_directory / "analysis.json").read_text(encoding="utf-8"))
                targets = {row["target"]: row["status"] for row in analysis["authentication"]}
                assert targets["portal"] == ("ERROR" if mode == "relogin_rejected" else "OK")
                assert targets["webmaas"] == ("BLOCKED" if mode == "relogin_rejected" else
                                               "ERROR" if mode in {"persistent_missing", "baseline_broken", "persistent_timeout"} else "OK")
                recovery_status = analysis["session_test"]["direct_api_recovery"]["status"]
                assert (recovery_status == "VERIFIED") == (mode in {"timeout", "timeout_post"}), (mode, recovery_status)
                if mode in {"timeout", "timeout_post"}:
                    assert analysis["session_test"]["direct_patient_values_equal"] is True
                    assert len(analysis["application_session_recoveries"]) == 1
                    recovered = analysis["application_session_recoveries"][0]
                    assert recovered["status"] == "RECOVERED"
                    assert recovered["correlation"] == "EXPLICIT_OPERATION_LINK"
                    assert not any(row["capture_id"] == recovered["timeout_capture_id"]
                                   for row in analysis["problems"])
                    assert any(row["recovery_type"] == "WEBMAAS_SSO"
                               for row in analysis["recovered_requests"])
                results.append({"mode": mode, "status": expected, "login_posts": state["login_posts"],
                                "raw_response_captured": True, "natural_ttl_verified": False})
                print(f"PASS {mode}: {expected}", flush=True)
    finally:
        helper.tearDown()
    write_path = output / f"session-{'source' if args.source else 'exe'}-verification.json"
    write_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
