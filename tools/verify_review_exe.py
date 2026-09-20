"""Verify both review OAuth paths and independent case reads in the frozen EXE.

Synthetic data and HTTPS localhost only. No production account or HAR token is used.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from html import escape
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests"), str(ROOT / "tools")]

from test_review import LOGIN_INFO, REVIEW_KEYS, case_row, grid, options  # noqa: E402
from test_tls import LegacyAesLoopbackTests  # noqa: E402
from verify_live_test_exe import SyntheticIntranet  # noqa: E402
from verify_surgery_exe import SurgeryIntranet  # noqa: E402

from vghks_sdk.offline.analyze import inspect_bundle  # noqa: E402
from vghks_sdk.offline.bundle import BundleReader  # noqa: E402


class ReviewIntranet(SyntheticIntranet):
    def review_reply(self, value, *, status=200, location=None, cookie=False, html=False):
        data = (value if html else json.dumps(value, ensure_ascii=False)).encode("utf-8")
        self.send_response(status)
        self.send_header(
            "Content-Type",
            "text/html; charset=utf-8" if html else "application/json; charset=utf-8",
        )
        self.send_header("Content-Length", str(len(data)))
        if location:
            self.send_header("Location", location)
        if cookie:
            self.send_header("Set-Cookie", "HIS_IPD=synthetic-review; Path=/Pck; Secure; HttpOnly")
        self.end_headers()
        self.wfile.write(data)

    def dispatch(self):
        address = urlsplit(self.path)
        path = address.path
        if not (path.startswith("/Pck/") or path.startswith("/oauth2Server")):
            return super().dispatch()
        params = dict(parse_qsl(address.query))
        if self.command == "POST":
            params.update(
                parse_qsl(self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode())
            )
        state = self.server.test_state
        origin = state["origin"]
        callback = origin.replace("https://", "http://") + "/Pck/HISLogin/SSOLoginCallBack"
        authorization = {
            "response_type": "code",
            "client_id": "pckoauth",
            "redirect_uri": callback,
            "state": f"fresh-state-{state['oauth_starts']}",
        }
        callback_url = (
            callback
            + "?"
            + urlencode(
                {
                    "oauthServer": origin,
                    "code": f"fresh-exchange-{state['oauth_starts']}",
                    "redirect_uri": callback,
                }
            )
        )
        if path == "/Pck/":
            return self.review_reply("synthetic probe")
        if path == "/Pck/HISLogin":
            state["oauth_starts"] += 1
            authorization["state"] = f"fresh-state-{state['oauth_starts']}"
            return self.review_reply(
                "", status=302, location=origin + "/oauth2Server.do?" + urlencode(authorization)
            )
        if path == "/oauth2Server.do":
            assert params == authorization
            if state["fallback"]:
                fields = {
                    **authorization,
                    "oauthServer": origin,
                    "code": f"fresh-form-{state['oauth_starts']}",
                }
                html = (
                    '<form action="/oauth2ServerLogin.do" method="post">'
                    + "".join(
                        f'<input type="hidden" name="{key}" value="{escape(value, quote=True)}">'
                        for key, value in fields.items()
                    )
                    + '<input name="muid"><input type="password" name="mpassword"><button name="submit" value="登入">登入</button></form>'
                )
                return self.review_reply(html, html=True)
            return self.review_reply("", status=302, location=callback_url)
        if path == "/oauth2ServerLogin.do":
            assert self.command == "POST"
            assert params == {
                **authorization,
                "oauthServer": origin,
                "code": f"fresh-form-{state['oauth_starts']}",
                "muid": "SYNTHETIC",
                "mpassword": "SYNTHETIC-ONLY",
                "submit": "登入",
            }
            assert self.headers["Origin"] == origin
            state["oauth_posts"].append(params["code"])
            return self.review_reply("", status=302, location=callback_url)
        if path.endswith("/SSOLoginCallBack"):
            assert params["code"] == f"fresh-exchange-{state['oauth_starts']}"
            return self.review_reply("", status=302, location="/Pck/angular/bulletin", cookie=True)
        if path == "/Pck/angular/bulletin":
            return self.review_reply("<html>synthetic bulletin</html>", html=True)
        assert "HIS_IPD=synthetic-review" in self.headers.get("Cookie", "")
        if path == "/Pck/Menu/GetLoginInfo":
            return self.review_reply(LOGIN_INFO)
        assert self.headers.get("X-Requested-With") == "XMLHttpRequest"
        if path == "/Pck/PCKQ010/Selections":
            return self.review_reply(options())
        if path == "/Pck/PCKQ010/ReadVSDrList":
            assert params == {"InsuSectNo": "OPH"}
            return self.review_reply(options()["VSDrID"])
        if path == "/Pck/PCKQ010/PckQ010Grid_Read":
            assert params == {"InsuSectNo": "OPH", "VSDrID": "SYNTHETIC"}
            return self.review_reply(grid(*(case_row(str(i), str(i)) for i in (1, 2, 3))))
        routes = {
            "/Pck/PCKC010/ReadPckC010": "case_detail",
            "/Pck/PCKC010/PCKAPPLOGrid_Read": "orders",
            "/Pck/PCKC010/PCKAPPLAGrid_Read": "attachments",
            "/Pck/PCKC010/PCKAPPLAPacsGrid_Read": "pacs",
        }
        assert path in routes, "Unexpected review request"
        assert self.command == "POST" and set(params) == {"ApplySeq"}
        key, seq = routes[path], params["ApplySeq"]
        state["reads"].append((key, seq))
        if state["fallback"]:
            if key == "case_detail" and seq == "1":
                return self.review_reply("synthetic detail error", status=500)
            if key == "attachments" and seq == "2":
                return self.review_reply("<html>synthetic unexpected document</html>", html=True)
            if key == "orders" and not state["expired_once"]:
                state["expired_once"] = True
                return self.review_reply("", status=302, location="/Pck/HISLogin")
        if key == "case_detail":
            return self.review_reply(case_row(seq, seq))
        if key == "pacs" and seq == "3":
            return self.review_reply(grid())
        return self.review_reply(
            grid(
                {
                    "ApplySeq": seq,
                    "VerifyCode": seq,
                    "VerifyText": "Synthetic decision",
                    "FutureField": True,
                }
            )
        )


class CombinedIntranet(ReviewIntranet, SurgeryIntranet):
    """Compose review, both surgery paths and MIS in one localhost session."""


def main():
    output = ROOT / "output"
    if output.is_symlink() or output.resolve().parent != ROOT:
        raise SystemExit("Output escaped workspace")
    helper = LegacyAesLoopbackTests()
    helper.setUp()
    helper.server.RequestHandlerClass = ReviewIntranet
    origin = f"https://localhost:{helper.server.server_port}"
    results = []
    try:
        for scenario in ("portal_session", "portal_credentials", "combined_default"):
            fallback = scenario == "portal_credentials"
            combined = scenario == "combined_default"
            helper.server.RequestHandlerClass = CombinedIntranet if combined else ReviewIntranet
            state = {
                "origin": origin,
                "reject_login": False,
                "login_posts": 0,
                "fallback": fallback,
                "oauth_starts": 0,
                "oauth_posts": [],
                "reads": [],
                "expired_once": False,
                "oppl_ajax_requests": 0,
                "oppl_sso_posts": 0,
                "mutation_attempts": 0,
                "queried_mrns": set(),
                "soap_mrns": [],
                "mis_password_posts": 0,
                "mis_reports": [],
                "periods": [],
                "notes": [],
                "pdf_attempts": [],
            }
            helper.server.test_state = state
            with tempfile.TemporaryDirectory(prefix="review-exe-", dir=output) as temporary:
                folder = Path(temporary).resolve()
                assert folder.is_relative_to(output.resolve())
                executable = Path(shutil.copy2(ROOT / "dist/vghks-live-test.exe", folder))
                config = json.loads(
                    (ROOT / "configs/review-system.example.json").read_text(encoding="utf-8")
                )
                config.update(
                    ca_bundle=helper.ca,
                    endpoint_overrides={
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
                    },
                    request_policy={
                        "min_delay_seconds": 0,
                        "max_delay_seconds": 0,
                        "max_attempts": 1,
                        "connect_timeout_seconds": 0.3,
                        "read_timeout_seconds": 2,
                    },
                )
                if not combined:
                    (folder / "live-test-config.json").write_text(
                        json.dumps(config), encoding="utf-8"
                    )
                env = {
                    k: v
                    for k, v in os.environ.items()
                    if not k.upper().startswith("VGHKS_")
                    and k.upper()
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
                if combined:
                    # Exercise the actual double-click defaults with no sidecar;
                    # only route/transport settings use environment overrides.
                    env.update(
                        {
                            "VGHKS_" + key.upper(): value
                            for key, value in config["endpoint_overrides"].items()
                        }
                    )
                    env.update(
                        VGHKS_CA_BUNDLE=helper.ca,
                        VGHKS_DELAY_MIN="0",
                        VGHKS_DELAY_MAX="0",
                        VGHKS_MAX_ATTEMPTS="1",
                        VGHKS_CONNECT_TIMEOUT="0.3",
                        VGHKS_READ_TIMEOUT="2",
                        VGHKS_EARNINGS_PASSWORD="SYNTHETIC-SECONDARY",
                    )
                process = subprocess.run(
                    [str(executable)]
                    if combined
                    else [
                        str(executable),
                        "--config",
                        "live-test-config.json",
                        "--non-interactive",
                    ],
                    cwd=folder,
                    env=env,
                    input="SYNTHETIC-NATIONAL-ID\n\n" if combined else "\n",
                    capture_output=True,
                    text=True,
                    # A frozen Windows app writes the system code page to a
                    # pipe; its real console uses Windows' Unicode handling.
                    encoding="mbcs",
                    errors="replace",
                    timeout=90,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                mode = "portal_credentials" if fallback else "portal_session"
                (output / f"exe-review-{scenario}.log").write_text(
                    process.stdout + process.stderr, encoding="utf-8"
                )
                assert process.returncode == int(fallback or combined), process.returncode
                archives = list(folder.glob("*.zip"))
                assert len(archives) == 1 and not list(folder.glob("*.sha256"))
                assert state["oauth_posts"] == (
                    ["fresh-form-1", "fresh-form-2"] if fallback else []
                )
                with zipfile.ZipFile(archives[0]) as archive:
                    summary = json.loads(archive.read("run_summary.json"))
                    assert summary["status"] == (
                        "COMPLETED_WITH_ERRORS" if fallback or combined else "OK"
                    )
                    if combined:
                        plan = json.loads(archive.read("test_plan.json"))
                        assert len(plan["operations"]) == 55 and len(plan["auth_targets"]) == 8
                        assert plan["weekly_opd_soap"]["enabled"] is False
                        assert plan["review_cases"]["enabled"] is True
                        assert plan["earnings_reports"]["enabled"] is True
                        assert plan["surgery_cases"]["query"]["procedure_code"] == "80416"
                        assert not (folder / "live-test-config.json").exists()
                        assert state["mutation_attempts"] == 0
                        assert "身分證字號" in process.stdout
                        assert state["periods"] == ["24M", "2YB"]
                        assert len(set(state["notes"])) == 5
                        assert len(state["pdf_attempts"]) == 3
                        assert state["patient_history_queries"] == ["20000", "365"]
                        assert len(state["patient_pdf_attempts"]) == 2
                        assert state["mis_password_posts"] == 2
                        assert state["mis_reports"] == ["PMO003R1", "PMO004R1"]
                        for kind in ("performance", "payroll"):
                            assert json.loads(archive.read(f"parsed/earnings/{kind}-report.json"))[
                                "tables"
                            ]
                        config_saved = json.loads(archive.read("run_config.json"))
                        assert "SYNTHETIC-NATIONAL-ID" not in json.dumps(config_saved)
                        assert "SYNTHETIC-SECONDARY" not in json.dumps(config_saved)
                    login = json.loads(archive.read("parsed/atomic/review.login_info/0001.json"))
                    assert login["authentication_mode"] == mode
                    cases = json.loads(archive.read("parsed/atomic/review.cases/0001.json"))
                    assert len(cases) == 3 and [r["approved"] for r in cases] == [True, False, None]
                    for key in ("case_detail", "orders", "attachments", "pacs"):
                        steps = [s for s in summary["steps"] if s["operation"] == "review." + key]
                        assert len(steps) == 3
                        assert {seq for part, seq in state["reads"] if part == key} == {
                            "1",
                            "2",
                            "3",
                        }
                        errors = sum(s["status"] == "ERROR" for s in steps)
                        assert errors == int(fallback and key in {"case_detail", "attachments"})
                    assert all(not e.flag_bits & 1 for e in archive.infolist())
                    captures = [
                        json.loads(line)
                        for line in archive.read("capture_manifest.jsonl").decode().splitlines()
                    ]
                    requests = [
                        row["request"] for row in captures if row.get("kind") == "HTTP_EXCHANGE"
                    ]
                    assert all(
                        urlsplit(r["url"]).hostname == "localhost"
                        and urlsplit(r["url"]).scheme == "https"
                        for r in requests
                    )
                with BundleReader(archives[0]) as reader:
                    report, recipe = inspect_bundle(reader)
                    assert recipe["review_query"] == config["review_query"]
                    review = [
                        row for row in report["operations"] if row["operation"] in REVIEW_KEYS
                    ]
                    assert len(review) == 8
                results.append(
                    {
                        "mode": mode,
                        "scenario": scenario,
                        "status": summary["status"],
                        "cases": 3,
                        "independent_parts": 4,
                        "expiry_recovered": state["expired_once"],
                        "all_requests_https_localhost": True,
                        "plain_zip": True,
                        "all_four_requested_areas_queried": combined,
                    }
                )
    finally:
        helper.doCleanups()
    (output / "exe-review-verification.json").write_text(
        json.dumps(
            {
                "build": json.loads((output / "build-info.json").read_text()),
                "runs": results,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(
        "Frozen EXE: review OAuth reuse, fresh credential fallback, expiry recovery and independent case reads verified."
    )


if __name__ == "__main__":
    main()
