"""Verify the login round using synthetic accounts and HTTPS localhost only.

Run with --source while developing, then without arguments against the frozen
dist EXE. The frozen runs use zero arguments, no config and no patient input.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests"), str(ROOT / "tools")]

from test_personnel import EMPLOYEE, options_page  # noqa: E402
from test_tls import LegacyAesLoopbackTests  # noqa: E402
from verify_review_exe import ReviewIntranet  # noqa: E402

from vghks_sdk.live.login_simulation import SCENARIOS  # noqa: E402
from vghks_sdk.offline.analyze import inspect_bundle  # noqa: E402
from vghks_sdk.offline.bundle import BundleReader  # noqa: E402

FORM = '<form><input name="muid"><input type="password" name="mpassword"></form>'
DENIED_PAGE = "<h3>登入失敗</h3><p>帳號或密碼錯誤</p><button onclick=\"location.href='logout.do'\">重新登入</button>"


class LoginIntranet(ReviewIntranet):
    def dispatch(self):
        path = urlsplit(self.path).path
        state = self.server.test_state
        state["paths"].append(path)
        mode = state["mode"]
        if path == "/index.do":
            return self.reply(FORM, cookie=True)
        if path == "/login.do":
            assert self.command == "POST"
            data = parse_qs(self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode())
            state["login_posts"] += 1
            assert data["muid"] == [EMPLOYEE]
            wrong = data.get("mpassword") != ["SYNTHETIC-ONLY"]
            state["login_order"].append("wrong" if wrong else "correct")
            if wrong:
                state["wrong_posts"] += 1
                assert state["wrong_posts"] <= 2, "wrong-password cap exceeded"
                if mode == "unknown_negative":
                    return self.reply("<html>Unexpected response</html>")
                if mode == "negative_redirect":
                    return self.reply("", 302, location="/index.do")
                return self.reply(DENIED_PAGE)
            if mode == "rejected_normal":
                return self.reply(FORM)
            return self.reply('<script>targetUrl="myPortal.do";</script>', cookie=True)
        if path == "/PRQWeb/QueryUploadMR.do":
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            if not self.headers.get("Cookie") and mode != "cookies_accepted":
                state["expired_queries"] += 1
                if mode == "cookie_401":
                    return self.reply("session expired", 401)
                return self.reply(
                    "", 302, location=state["origin"].replace("https:", "http:") + "/"
                )
            return self.reply('[{"maintp":"OPD","mainnm":"Synthetic"}]')
        if path == "/DDPortal/DRQuery.jsp":
            return self.reply(
                '<html><iframe src="DRQuerySql.jsp" /><iframe src="blank.htm" /></html>'
            )
        if path == "/DDPortal/DRQuerySql.jsp":
            return self.reply(
                options_page().replace(">合成單位</option>", ">TEST - 合成單位</option>")
            )
        if path == "/DDPortal/dRDoctor.do" and mode == "directory_error":
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            return self.reply("<html>Unknown directory response</html>")
        return super().dispatch()


def main():
    source = "--source" in sys.argv
    output = ROOT / "output"
    output.mkdir(exist_ok=True)
    if output.is_symlink() or output.resolve().parent != ROOT:
        raise SystemExit("Output escapes workspace")
    helper = LegacyAesLoopbackTests()
    helper.setUp()
    helper.server.RequestHandlerClass = LoginIntranet
    origin = f"https://localhost:{helper.server.server_port}"
    results = []
    try:
        for mode in (
            "normal",
            "cookie_401",
            "cookies_accepted",
            "directory_error",
            "rejected_normal",
            "unknown_negative",
            "negative_redirect",
        ):
            helper.server.test_state = state = {
                "mode": mode,
                "origin": origin,
                "login_posts": 0,
                "wrong_posts": 0,
                "reject_login": False,
                "oppl_sso_posts": 0,
                "oppl_ajax_requests": 0,
                "mutation_attempts": 0,
                "oauth_starts": 0,
                "oauth_posts": [],
                "fallback": False,
                "login_order": [],
                "expired_queries": 0,
                "paths": [],
            }
            with tempfile.TemporaryDirectory(prefix="login-exe-", dir=output) as temporary:
                directory = Path(temporary).resolve()
                if not directory.is_relative_to(output.resolve()):
                    raise SystemExit("Temporary path escapes output")
                if source:
                    command = [
                        sys.executable,
                        str(ROOT / "run_live_test.py"),
                        "--profile",
                        "login",
                        "--non-interactive",
                    ]
                else:
                    executable = Path(shutil.copy2(ROOT / "dist/vghks-live-test.exe", directory))
                    command = [str(executable)]
                # Obsolete sidecars must not change zero-argument startup.
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
                    VGHKS_USERNAME=EMPLOYEE,
                    VGHKS_PASSWORD="SYNTHETIC-ONLY",
                    VGHKS_LIVE_PROFILE="comprehensive",
                    VGHKS_CA_BUNDLE=helper.ca,
                    VGHKS_DELAY_MIN="0",
                    VGHKS_DELAY_MAX="0",
                    VGHKS_MAX_ATTEMPTS="1",
                    VGHKS_CONNECT_TIMEOUT="0.5",
                    VGHKS_READ_TIMEOUT="3",
                    NO_PROXY="localhost,127.0.0.1",
                    PYTHONIOENCODING="utf-8",
                )
                for app, suffix in {
                    "portal": "",
                    "prq": "/PRQWeb",
                    "sectord": "/SectOrdWeb",
                    "webmaas": "/webmaas",
                    "oppl": "/OPPLWeb",
                    "audit": "/PRQWeb",
                    "mis": "",
                    "review": "/Pck",
                    "personnel": "/DDPortal",
                }.items():
                    environment[f"VGHKS_{app.upper()}_BASE_URL"] = origin + suffix
                process = subprocess.run(
                    command,
                    cwd=directory,
                    env=environment,
                    input="\n",
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=90,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
                label = "source" if source else "exe"
                (output / f"login-{label}-{mode}.log").write_text(
                    process.stdout + process.stderr, encoding="utf-8"
                )
                archives = list(directory.glob("*.zip"))
                assert len(archives) == 1 and not list(directory.rglob("*.sha256")), (
                    mode,
                    process.stdout,
                )
                with zipfile.ZipFile(archives[0]) as archive:
                    summary = json.loads(archive.read("run_summary.json"))
                    assert all(not info.flag_bits & 1 for info in archive.infolist())
                    steps = {step["name"]: step for step in summary["steps"]}
                    simulated = [
                        s for s in steps.values() if s["name"].startswith("login.simulated.")
                    ]
                    assert len(simulated) == len(SCENARIOS) and all(
                        s["status"] == "OK" for s in simulated
                    )
                    expected = {
                        "normal": "OK",
                        "cookie_401": "OK",
                        "negative_redirect": "OK",
                        "cookies_accepted": "COMPLETED_WITH_GAPS",
                        "directory_error": "COMPLETED_WITH_ERRORS",
                        "rejected_normal": "AUTHENTICATION_FAILED",
                        "unknown_negative": "COMPLETED_WITH_ERRORS",
                    }[mode]
                    assert summary["status"] == expected, (
                        mode,
                        summary["status"],
                        [s for s in steps.values() if s["status"] == "ERROR"],
                    )
                    assert summary["profile"] == "login"
                    assert process.returncode == (
                        0 if expected in {"OK", "COMPLETED_WITH_GAPS"} else 1
                    )
                    wrong_count = (
                        0 if mode == "rejected_normal" else 1 if mode == "unknown_negative" else 2
                    )
                    assert state["wrong_posts"] == wrong_count, state
                    assert state["mutation_attempts"] == 0
                    if wrong_count:
                        first_wrong = state["login_order"].index("wrong")
                        assert all(value == "wrong" for value in state["login_order"][first_wrong:])
                        assert steps["login.cookie_loss"]["status"] == (
                            "NO_SAMPLE" if mode == "cookies_accepted" else "OK"
                        )
                    else:
                        assert state["login_posts"] == 1
                    if mode == "normal":
                        assert all(
                            steps[f"login.personnel.{kind}"]["status"] == "OK"
                            for kind in ("by_card", "employee", "name", "title", "unit", "subunits")
                        )
                with BundleReader(archives[0]) as reader:
                    report, retest = inspect_bundle(reader)
                expected_analysis = (
                    expected if expected in {"OK", "COMPLETED_WITH_GAPS"} else "NEEDS_ATTENTION"
                )
                assert report["analysis_status"] == expected_analysis, (mode, report["problems"])
                assert retest["profile"] == "login"
                if mode == "normal":
                    assert len(report["expected_login_rejections"]) == 2
                    assert all(
                        row["applied"] and row["certificate_verification"]
                        for row in report["connection_profiles"]
                    )
                results.append(
                    {
                        "scenario": mode,
                        "status": expected,
                        "analysis_status": expected_analysis,
                        "wrong_password_posts": wrong_count,
                        "simulated_passed": len(simulated),
                        "zero_argument_start": not source,
                        "same_directory_plain_zip": not source,
                        "clinical_writes": state["mutation_attempts"],
                    }
                )
                print(f"PASS {mode}: {expected}; wrong password POSTs={wrong_count}", flush=True)
    finally:
        helper.doCleanups()
    (output / f"login-{'source' if source else 'exe'}-verification.json").write_text(
        json.dumps({"network": "HTTPS localhost only", "results": results}, indent=2),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
