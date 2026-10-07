"""Validate the password EXE against synthetic HTTPS; never contact the intranet."""

# ruff: noqa: RUF001

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
from urllib.parse import parse_qsl, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests"), str(ROOT / "tools")]

from test_tls import LegacyAesLoopbackTests  # noqa: E402
from verify_visit_exe import MRN, VisitIntranet, test_state  # noqa: E402

MODES = ("normal", "warning", "empty_catalog", "required_text", "required_form", "required_redirect",
         "required_script", "required_landing", "required_catalog_open", "required_catalog_public",
         "unknown_login", "denied_login", "rejected_login", "foreign_redirect", "query_expiry")
CHANGE_PAGE = '<p>密碼已過期，必須先修改密碼才能登入</p><form action="/password-save.do" method="POST"><input type="password" name="oldPassword"><input type="password" name="newPassword"></form>'


class PasswordIntranet(VisitIntranet):
    def dispatch(self):
        path = urlsplit(self.path).path
        state = self.server.test_state
        mode = state["mode"]
        if path not in {"/login.do", "/PRQWeb/QueryUploadMR.do", "/changePassword.do", "/password-save.do"}:
            if path == "/myPortal.do" and mode == "required_landing":
                return self.reply(CHANGE_PAGE)
            return super().dispatch()
        params = dict(parse_qsl(urlsplit(self.path).query))
        if self.command == "POST":
            params.update(parse_qsl(self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode()))
        if path == "/password-save.do" or (path == "/changePassword.do" and self.command == "POST"):
            state["change_posts"] += 1
            return self.reply("unexpected password mutation", 403)
        if path == "/changePassword.do":
            state["change_gets"] += 1
            return self.reply(CHANGE_PAGE)
        if path == "/login.do":
            state["login_posts"] += 1
            assert params["muid"] == "SYNTHETIC" and params["mpassword"] == "SYNTHETIC-ONLY"
            if mode == "denied_login":
                return self.reply("synthetic denied", 403)
            if mode == "rejected_login":
                return self.reply("<h3>登入失敗</h3><p>帳號或密碼錯誤</p>")
            if mode == "unknown_login":
                return self.reply("<p>synthetic unknown password policy</p>")
            if mode == "foreign_redirect":
                return self.reply("", 302, location="https://foreign.invalid/changePassword.do")
            if mode == "required_redirect":
                return self.reply("", 302, location=state["origin"] + "/changePassword.do")
            if mode == "required_script":
                return self.reply('<script>targetUrl="changePassword.do";</script>')
            if mode == "required_form":
                return self.reply('<form action="/password-save.do"><input type="password" name="oldPassword"><input type="password" name="newPassword"></form>')
            if mode in {"required_text", "required_catalog_open", "required_catalog_public"}:
                return self.reply(CHANGE_PAGE)
            if mode == "warning":
                return self.reply('<p>密碼剩餘2天到期</p><script>targetUrl="myPortal.do";</script>', cookie=True)
            return self.reply('<script>targetUrl="myPortal.do";</script>', cookie=True)
        state["catalog_requests"] += 1
        if mode == "required_catalog_public":
            return self.reply('[{"maintp":"OPD","mainnm":"Synthetic"}]')
        if state["login_posts"] == 0:
            assert not self.headers.get("Authorization") and not self.headers.get("Cookie")
            return self.reply("synthetic anonymous", 401)
        if mode == "empty_catalog":
            return self.reply("[]")
        if mode == "required_catalog_open" or mode in {"normal", "warning", "required_landing"}:
            return self.reply('[{"maintp":"OPD","mainnm":"Synthetic"}]')
        return self.reply("synthetic inaccessible", 401)


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
    helper.server.RequestHandlerClass = PasswordIntranet
    origin = f"https://localhost:{helper.server.server_port}"
    results = []
    try:
        for mode in MODES:
            state = test_state(mode, origin)
            state.update(change_posts=0, change_gets=0, catalog_requests=0)
            helper.server.test_state = state
            with tempfile.TemporaryDirectory(prefix="password-exe-", dir=output) as directory:
                root = Path(directory)
                config = {
                    "schema_version": 7, "profile": "password", "test_mrn": MRN, "ca_bundle": helper.ca,
                    "endpoint_overrides": {key + "_base_url": origin + path for key, path in {
                        "portal": "", "prq": "/PRQWeb", "sectord": "/SectOrdWeb", "webmaas": "/webmaas",
                    }.items()},
                    "request_policy": {"min_delay_seconds": 0, "max_delay_seconds": 0,
                                       "max_attempts": 1, "connect_timeout_seconds": 1,
                                       "read_timeout_seconds": 3},
                }
                (root / "config.json").write_text(json.dumps(config), encoding="utf-8")
                env = {k: v for k, v in os.environ.items() if not k.upper().startswith("VGHKS_")
                       and k.upper() not in {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
                                             "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"}}
                env.update(VGHKS_USERNAME="SYNTHETIC", VGHKS_PASSWORD="SYNTHETIC-ONLY",
                           PYTHONPATH=str(ROOT / "src"), PYTHONIOENCODING="utf-8", NO_PROXY="localhost,127.0.0.1")
                if args.source:
                    command = [sys.executable, "-m", "vghks_sdk.live_test_app"]
                else:
                    executable = Path(shutil.copy2(args.exe.resolve(), root / "vghks-live-test.exe"))
                    command = [str(executable)]
                process = subprocess.run(
                    [*command, "--config", str(root / "config.json"), "--non-interactive", "--output", str(root / "run")],
                    env=env, cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
                assert (root / "run/run_summary.json").exists(), process.stdout + process.stderr
                summary = json.loads((root / "run/run_summary.json").read_text(encoding="utf-8"))
                assert process.returncode == (0 if mode in {"normal", "warning", "empty_catalog"} else 1), (mode, process.stdout, process.stderr)
                observation = summary["password_change_test"]
                assert state["login_posts"] == 1, (mode, state)
                assert state["change_posts"] == 0
                assert state["change_gets"] == (1 if mode in {"required_redirect", "required_script"} else 0)
                assert observation["password_post_budget"]["password_posts"] == 1
                assert observation["natural_ttl_verified"] is False
                if mode in {"normal", "warning", "empty_catalog"}:
                    assert observation["login_status"] == "OK", (mode, observation)
                    assert observation["patient_read_status"] == "OK"
                    assert observation["credential_validity"] == "ACCEPTED"
                    assert state["query_posts"] == 1
                    assert summary["status"] == "COMPLETED_WITH_GAPS"
                elif mode == "query_expiry":
                    assert observation["login_status"] == "OK"
                    assert observation["credential_validity"] == "ACCEPTED"
                    assert observation["patient_read_status"] == "BLOCKED"
                    assert observation["sdk_catalog_status"] == "ERROR"
                    assert observation["password_post_budget"]["blocked_posts"] == 1
                    assert summary["status"] == "COMPLETED_WITH_ERRORS"
                    assert state["query_posts"] == 0
                else:
                    assert observation["login_status"] == "ERROR", (mode, observation)
                    assert observation["patient_read_status"] == "BLOCKED"
                    assert state["query_posts"] == 0
                    assert summary["status"] == "COMPLETED_WITH_ERRORS"
                if mode.startswith("required"):
                    assert observation["password_status"]["status"] == "CHANGE_REQUIRED"
                    assert observation["credential_validity"] == "UNKNOWN"
                if mode == "warning":
                    assert observation["password_status"]["status"] == "EXPIRING"
                if mode in {"required_catalog_open", "required_catalog_public", "required_landing"}:
                    assert observation["existing_cookie_catalog"]["query_accepted"] is True
                    assert observation["credential_validity"] == "UNKNOWN"
                if mode == "required_catalog_public":
                    assert observation["anonymous_catalog"]["query_accepted"] is True
                archives = list(root.glob("*.zip"))
                assert len(archives) == 1
                with zipfile.ZipFile(archives[0]) as bundle:
                    assert bundle.testzip() is None
                    if state["change_gets"]:
                        assert any(CHANGE_PAGE.encode() in bundle.read(n) for n in bundle.namelist()
                                   if n.startswith("responses/") and not n.endswith("/")), mode
                analysis_dir = root / "analysis"
                analysis_process = subprocess.run(
                    [*command, "--analyze", str(archives[0]), "--analysis-output", str(analysis_dir)],
                    cwd=root, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
                assert analysis_process.returncode in {0, 1}, analysis_process.stdout + analysis_process.stderr
                report = json.loads((analysis_dir / "analysis.json").read_text(encoding="utf-8"))
                retest = json.loads((analysis_dir / "retest-config.json").read_text(encoding="utf-8"))
                assert retest["profile"] == "password" and retest["login_negative_attempts"] == 0
                assert report["password_change_test"]["credential_validity"] == observation["credential_validity"]
                if mode != "required_catalog_public":
                    assert report["expected_unauthenticated_responses"], (mode, report["problems"])
                if mode == "empty_catalog":
                    catalog = next(row for row in report["operations"] if row["operation"] == "prq.upload_types")
                    assert catalog["live_status"] == "EMPTY", catalog
                    assert catalog["live_step_count"] == 1
                if mode in {"normal", "warning", "empty_catalog"}:
                    assert not report["problems"], (mode, report["problems"])
                results.append({"mode": mode, "status": summary["status"], "password_posts": 1,
                                "change_posts": 0, "change_page_gets": state["change_gets"],
                                "credential_validity": observation["credential_validity"]})
                print(f"PASS {mode}: {summary['status']}", flush=True)
    finally:
        helper.tearDown()
    (output / f"password-{'source' if args.source else 'exe'}-verification.json").write_text(
        json.dumps(results, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
