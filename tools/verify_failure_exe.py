"""Validate acquisition scenarios in the source or frozen EXE on localhost HTTPS only."""

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
from urllib.parse import parse_qsl, urlencode, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests"), str(ROOT / "tools")]

from test_tls import LegacyAesLoopbackTests  # noqa: E402
from verify_visit_exe import MRN, VisitIntranet, test_state  # noqa: E402

from vghks_sdk._version import __version__  # noqa: E402
from vghks_sdk.offline.analyze import inspect_bundle  # noqa: E402
from vghks_sdk.offline.bundle import BundleReader  # noqa: E402


class FailureIntranet(VisitIntranet):
    def dispatch(self):
        path = urlsplit(self.path).path
        state = self.server.test_state
        mode = state["mode"]
        handled = {
            "/login.do",
            "/PRQWeb/QueryUploadMR.do",
            "/PRQWeb/QueryResNumCenter.do",
            "/PRQWeb/QueryOrderResult.do",
            "/PRQWeb/QueryOrderDetail.do",
            "/PRQWeb/QueryReportByOrder.do",
            "/PRQWeb/Adm_QueryPACS.do",
        }
        if path == "/myPortal.do" and mode == "password_required_landing":
            return self.reply("<h3>必須先修改密碼才能登入</h3>")
        if path == "/PRQWeb/QueryBillingSOAP.do" and mode == "soap_parse":
            return self.reply("<html>synthetic unknown SOAP layout</html>")
        if path not in handled:
            return super().dispatch()
        params = dict(parse_qsl(urlsplit(self.path).query, keep_blank_values=True))
        if self.command == "POST":
            params.update(
                parse_qsl(
                    self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode(),
                    keep_blank_values=True,
                )
            )
        if path == "/login.do":
            assert params["muid"] == "SYNTHETIC"
            state["login_posts"] += 1
            wrong = params["mpassword"] != "SYNTHETIC-ONLY"
            state["credential_order"].append("wrong" if wrong else "correct")
            if wrong:
                state["wrong_posts"] += 1
                if mode == "negative_unknown":
                    return self.reply("<p>synthetic unknown login response</p>")
                if mode == "negative_http_denied":
                    return self.reply("synthetic forbidden", 403)
                if mode == "negative_password_required":
                    return self.reply("<p>密碼已過期，必須先修改密碼</p>")
                if mode != "negative_accepted":
                    return self.reply("<h3>登入失敗</h3><p>帳號或密碼錯誤</p>")
                return self.reply('<script>targetUrl="myPortal.do";</script>', cookie=True)
            state["correct_posts"] += 1
            if mode == "login_denied":
                return self.reply("<h3>登入失敗</h3><p>帳號或密碼錯誤</p>")
            if mode == "relogin_denied" and state["correct_posts"] > 1:
                return self.reply("synthetic login HTTP denial", 403)
            if mode == "password_required" or (
                mode == "relogin_password_required" and state["correct_posts"] > 1
            ):
                return self.reply("<p>密碼已到期，必須先變更密碼才能登入</p>")
            if mode == "password_required_form":
                return self.reply('<form><input type="password" name="oldPassword"><input type="password" name="newPassword"></form>')
            if mode == "password_required_redirect":
                return self.reply("", 302, location=state["origin"] + "/changePassword.do")
            if mode == "password_warning":
                return self.reply('<p>密碼剩餘3天到期</p><form><input type="password" name="oldPassword"><input type="password" name="newPassword"></form><script>targetUrl="myPortal.do";</script>', cookie=True)
            if mode == "password_warning_script":
                return self.reply('<script>alert("密碼剩餘3天到期");targetUrl="myPortal.do";</script>', cookie=True)
            if mode == "password_warning_bracketed":
                return self.reply('<script>alert("密碼將於【4】日後到期，請盡快修改。");var targetUrl="myPortal.do";if(window.name=="example"){location.href=targetUrl;}</script>', cookie=True)
            return self.reply(
                "<script>var targetUrl='myPortal.do?thetime=1';</script>", cookie=True
            )
        if path == "/PRQWeb/QueryUploadMR.do":
            state["catalog_requests"] += 1
            if not self.headers.get("Cookie"):
                if state["correct_posts"] == 0:
                    assert not self.headers.get("Authorization"), "anonymous query carried credentials"
                    if mode == "anonymous_redirect":
                        return self.reply("", 302, location=state["origin"] + "/index.do")
                    if mode == "anonymous_form":
                        return self.reply('<form><input name="muid"><input name="mpassword"></form>')
                    if mode == "anonymous_403":
                        return self.reply("synthetic forbidden", 403)
                    if mode == "anonymous_unknown":
                        return self.reply("synthetic unknown catalog page")
                return self.reply("synthetic cookie loss", 401)
            return self.reply('[{"maintp":"OPD","mainnm":"Synthetic"}]')
        if path == "/PRQWeb/QueryResNumCenter.do":
            if mode == "http_503":
                return self.reply("synthetic unavailable", 503)
            if mode == "password_warning_bracketed":
                return self.reply(
                    '<div id="data"><table id="resnumTable0">'
                    '<tr><th rowspan="2">日期</th><th colspan="7">Synthetic</th></tr>'
                    '<tr><th>OD</th><th>OS</th></tr>'
                    '<tr><td>2026-01-02</td><td>error</td><td></td></tr></table></div>'
                )
            return self.reply(
                '<div id="data"><table id="resnumTable0">'
                "<tr><th>日期</th><th>OD</th><th>OS</th></tr>"
                "<tr><td>2026-01-02</td><td>error</td><td></td></tr></table></div>"
            )
        if path == "/PRQWeb/QueryOrderResult.do":
            if mode == "http_403":
                return self.reply("synthetic access denial", 403)
            assert params["hhisnum"] == MRN
            body = '<div id="orderList"></div>'
            for sequence, status in (("U1", "未執行"), ("D1", "完成")):
                query = urlencode(
                    {"hhisnum": MRN, "caseType": "O", "caseNo": params["caseNo"], "seqNo": sequence}
                )
                body += (
                    "<script>var orderStr='<a href=\"/PRQWeb/QueryOrderDetail.do?"
                    + query
                    + "\">Synthetic examination</a>';"
                    + f"new KSCase('',orderStr,'2026-01-02','','','{status}','');</script>"
                )
            return self.reply(body)
        if path == "/PRQWeb/QueryOrderDetail.do":
            assert params["seqNo"] == "D1", "unexecuted orders must not be fetched"
            query = urlencode(
                {
                    "hhisnum": MRN,
                    "caseType": "O",
                    "caseNo": params["caseNo"],
                    "seqNo": params["seqNo"],
                }
            )
            return self.reply(
                "<table><tr><th>醫囑名稱</th><td>Synthetic examination</td></tr></table>"
                f'<a href="/PRQWeb/QueryReportByOrder.do?{query}">Report</a>'
            )
        if path == "/PRQWeb/QueryReportByOrder.do":
            if mode == "password_warning_bracketed":
                return self.reply(
                    '<table><tr><th>報告內容 JPG</th><td><script>'
                    f'var urlStr=encodeURIComponent(encodeURIComponent("//nfs01p/EMRU/{MRN}/report.pdf"));'
                    'document.write("<a href=\\\"/PRQWeb/Page/JSP/showPDF.jsp?url="+urlStr+"\\\">PDF</a>");'
                    f'</script><span reqno="SYNTHETIC" hhisnum="{MRN}">JPG</span></td></tr></table>'
                )
            return self.reply(
                "<table><tr><th>報告內容 JPG</th><td>詳見附件"
                f'<script>var p="//nfs01p/EMRU/{MRN}/report.pdf";</script>'
                f'<span reqno="SYNTHETIC" hhisnum="{MRN}">JPG</span></td></tr></table>'
            )
        return self.reply('<div id="pacsContent">查無資料!</div>')


def verify_mode(command: list[str], helper, origin: str, mode: str, *, source: bool, explicit_profile: bool = False) -> dict:
    state = test_state(mode, origin)
    state["catalog_requests"] = 0
    state.update(wrong_posts=0, correct_posts=0, credential_order=[])
    helper.server.test_state = state
    output = ROOT / "output"
    with tempfile.TemporaryDirectory(prefix="failure-exe-", dir=output) as temporary:
        directory = Path(temporary).resolve()
        assert directory.parent == output.resolve(), "temporary directory escaped output"
        if not source:
            executable = directory / "vghks-live-test.exe"
            shutil.copy2(command[0], executable)
            arguments = [str(executable)]  # Verify the double-click build scope.
            if explicit_profile:
                arguments.extend(["--profile", "failures", "--non-interactive"])
        else:
            arguments = [*command, "--profile", "failures", "--non-interactive"]
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("VGHKS_")
            and key.upper()
            not in {
                "HTTP_PROXY",
                "HTTPS_PROXY",
                "ALL_PROXY",
                "NO_PROXY",
                "REQUESTS_CA_BUNDLE",
                "CURL_CA_BUNDLE",
                "PYTHONPATH",
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
            VGHKS_CONNECT_TIMEOUT="0.5",
            VGHKS_READ_TIMEOUT="2",
            NO_PROXY="localhost,127.0.0.1",
        )
        for app, path in {
            "portal": "",
            "prq": "/PRQWeb",
            "sectord": "/SectOrdWeb",
            "webmaas": "/webmaas",
            "oppl": "/OPPLWeb",
            "audit": "/PRQWeb",
            "review": "/Pck",
            "mis": "",
            "personnel": "/DDPortal",
        }.items():
            environment[f"VGHKS_{app.upper()}_BASE_URL"] = origin + path
        process = subprocess.run(
            arguments,
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
        label = "source" if source else "exe"
        (output / f"{label}-failures-{mode}.log").write_text(
            process.stdout + process.stderr, encoding="utf-8"
        )
        archives = list(directory.glob("vghks-live-test-*.zip"))
        assert len(archives) == 1, (mode, process.returncode, process.stdout[-2000:])
        with zipfile.ZipFile(archives[0]) as archive:
            assert archive.testzip() is None
            summary = json.loads(archive.read("run_summary.json"))
            recorded_build = json.loads(archive.read("environment.json"))["build"]
            assert recorded_build["sdk_version"] == __version__
            if not source:
                expected_build = json.loads((output / "build-info.json").read_text(encoding="utf-8"))
                assert recorded_build["build_id"] == expected_build["build_id"]
            steps = {row["name"]: row for row in summary["steps"]}
            coverage = json.loads(archive.read("coverage.json"))["failure_classification"]
            assert summary["profile"] == "failures"
            assert coverage["simulated_passed"] == coverage["simulated_count"] == 68
            assert coverage["natural_ttl"] == {"status": "NOT_TESTED", "verified": False}
            assert state["wrong_posts"] == 1
            assert state["credential_order"][0] == "wrong"
            positive_modes = {"normal", "empty", "anonymous_redirect", "anonymous_form",
                              "anonymous_403", "password_warning", "password_warning_script",
                              "password_warning_bracketed"}
            if mode in positive_modes:
                assert process.returncode == 0, process.stdout[-1000:]
                assert state["login_posts"] == 3, state["login_posts"]
                assert coverage["cookie_loss"]["recovery_observed"] is True
            else:
                assert process.returncode == 1, (mode, summary["status"])
            if mode.startswith("negative_"):
                assert state["correct_posts"] == 0
                assert steps["auth_check.portal"]["status"] == "BLOCKED"
                assert steps["failures.live.negative_before_login"]["status"] == "ERROR"
            else:
                assert steps["failures.live.negative_before_login"]["status"] == "OK"
            if mode == "anonymous_unknown":
                assert steps["failures.live.unauthenticated"]["status"] == "ERROR"
            else:
                anonymous = steps["failures.live.unauthenticated"]
                assert anonymous["status"] == "OK"
                assert anonymous["details"]["password_posts"] == 0
            if mode.startswith("password_warning"):
                notice = steps["failures.live.password_policy"]["details"]["password_status"]
                expected_days = 4 if mode == "password_warning_bracketed" else 3
                assert notice["status"] == "EXPIRING" and notice["remaining_days"] == expected_days
                readiness = json.loads(archive.read("parsed/readiness.json"))
                assert readiness["password_status"]["remaining_days"] == expected_days
                if mode == "password_warning_bracketed":
                    numeric = steps["failures.live.case.1.numeric"]["details"]
                    assert numeric["acquisition_status"] == "OK" and numeric["complete"] is True
                    assert numeric["parsing_issues"] == []
                    assert numeric["parsing_warnings"] == ["NUMERIC_HEADER_SPAN_MISMATCH"]
                    report = steps["failures.live.report.1"]["details"]
                    assert report["acquisition_status"] == "OK" and report["complete"] is True
                    assert report["availability"] == "ATTACHMENT_ONLY"
                    assert report["report_data_status"] == "ATTACHMENT_ONLY"
            if mode.startswith("password_required"):
                issue = steps["auth_check.portal"]["issue"]
                assert issue["code"] == "PORTAL_PASSWORD_CHANGE_REQUIRED"
                assert issue["retry_safe"] is False
                assert state["correct_posts"] == 1
                assert state["catalog_requests"] == 1
                assert not any("/changePassword.do" in path for path in state["paths"])
            if mode in {"relogin_password_required", "negative_password_required"} or mode.startswith("password_required"):
                assert steps["failures.live.password_policy"]["details"]["password_status"]["status"] == "CHANGE_REQUIRED"
            if mode == "normal":
                assert (
                    steps["failures.live.report.1"]["details"]["availability"] == "ATTACHMENT_ONLY"
                )
                assert steps["failures.live.pacs.1"]["status"] == "EMPTY"
                assert coverage["live_categories"]["NOT_EXECUTED"]["count"] == 2
            if mode == "empty":
                assert steps["failures.live.visits"]["status"] == "EMPTY"
                assert steps["failures.live.soap"]["status"] == "NO_SAMPLE"
            if mode == "soap_parse":
                assert steps["failures.live.case.1.soap"]["issue"]["category"] == "PARSE"
                assert steps["failures.live.case.1.numeric"]["status"] == "OK"
            if mode == "http_503":
                issue = steps["failures.live.case.1.numeric"]["issue"]
                assert issue["code"] == "HTTP_503" and issue["retry_recommended"] is True
            if mode == "login_denied":
                assert state["login_posts"] == 2 and state["catalog_requests"] == 1
                assert steps["failures.live.visits"]["status"] == "BLOCKED"
                assert coverage["live_categories"]["AUTHENTICATION"]["status"] == "OBSERVED"
            if mode in {"relogin_denied", "relogin_password_required"}:
                issue = steps["failures.live.cookie_loss"]["issue"]
                assert issue["code"] == ("PORTAL_LOGIN_HTTP_DENIED" if mode == "relogin_denied"
                                         else "PORTAL_PASSWORD_CHANGE_REQUIRED")
                assert state["login_posts"] == 3 and state["catalog_requests"] == 3
                assert coverage["live_categories"]["AUTHENTICATION"]["status"] == "OBSERVED"
            if mode == "http_403":
                issue = steps["failures.live.case.1.orders"]["issue"]
                assert issue["code"] == "AUTH_RELOGIN_FAILED"
                assert issue["cause"]["code"] == "AUTH_HTTP_DENIED"
                assert issue["cause"]["http_status"] == 403
                assert issue["retry_safe"] is False
                assert state["login_posts"] == 3 and state["catalog_requests"] == 1
        with BundleReader(archives[0]) as reader:
            analysis, _ = inspect_bundle(reader)
        if mode in positive_modes:
            if analysis["root_cause"] is not None or not analysis["expected_unauthenticated_responses"]:
                shutil.copy2(archives[0], output / f"{label}-failures-{mode}-debug.zip")
            assert analysis["root_cause"] is None, analysis["root_cause"]
            assert analysis["expected_login_rejections"], "known bounded rejection was lost"
            assert analysis["expected_unauthenticated_responses"], "known anonymous challenge was lost"
            if mode == "password_warning_bracketed":
                assert all(row["remaining_days"] == 4 for row in analysis["password_policy"]["offline_observations"])
                assert analysis["password_policy"]["offline_observations"]
                assert analysis["data_quality"]["offline_partial_responses"] == []
                assert analysis["data_quality"]["offline_warnings"]
        else:
            assert analysis["root_cause"] is not None, (mode, analysis["analysis_status"])
        return {
            "mode": mode,
            "sdk_version": recorded_build["sdk_version"],
            "build_id": recorded_build.get("build_id"),
            "status": summary["status"],
            "password_posts": state["login_posts"],
            "simulations_passed": coverage["simulated_passed"],
            "zip_verified": True,
            "analysis_status": analysis["analysis_status"],
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", action="store_true")
    parser.add_argument("--explicit-profile", action="store_true",
                        help="verify failures via CLI when the EXE defaults to another profile")
    args = parser.parse_args()
    output = ROOT / "output"
    if output.is_symlink() or output.resolve().parent != ROOT:
        raise SystemExit("Output directory escapes workspace")
    output.mkdir(exist_ok=True)
    command = (
        [sys.executable, str(ROOT / "run_live_test.py")]
        if args.source
        else [str(ROOT / "dist/vghks-live-test.exe")]
    )
    helper = LegacyAesLoopbackTests()
    helper.setUp()
    helper.server.RequestHandlerClass = FailureIntranet
    origin = f"https://localhost:{helper.server.server_port}"
    try:
        results = [
            verify_mode(command, helper, origin, mode, source=args.source, explicit_profile=args.explicit_profile)
            for mode in (
                "normal",
                "empty",
                "soap_parse",
                "http_503",
                "login_denied",
                "relogin_denied",
                "http_403",
                "anonymous_redirect", "anonymous_form", "anonymous_403", "anonymous_unknown",
                "password_warning", "password_warning_script", "password_warning_bracketed",
                "password_required", "password_required_form", "password_required_redirect",
                "password_required_landing", "relogin_password_required",
                "negative_unknown", "negative_http_denied", "negative_accepted",
                "negative_password_required",
            )
        ]
    finally:
        helper.tearDown()
    label = "source" if args.source else "exe"
    write_path = output / f"{label}-failure-verification.json"
    write_path.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
