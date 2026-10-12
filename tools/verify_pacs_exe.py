"""Verify both PACS protocols using source/frozen CLI and localhost HTTPS only."""

from __future__ import annotations

import argparse
import html
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import zipfile
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests"), str(ROOT / "tools")]

from test_binary_responses import JPEG, LOGIN  # noqa: E402
from test_pacs_img_url import image_url  # noqa: E402
from test_report_data import NARRATIVE  # noqa: E402
from test_tls import LegacyAesLoopbackTests  # noqa: E402
from verify_ophthalmology_exe import EyeIntranet  # noqa: E402

from vghks_sdk import __version__  # noqa: E402
from vghks_sdk.live.config import LIVE_CONFIG_SCHEMA_VERSION  # noqa: E402
from vghks_sdk.live.defaults import SYNTHETIC_MRN  # noqa: E402

MODES = ("both", "uid_only", "imgurl_only", "no_images", "bad_imgurl", "bad_uid",
         "unsafe_imgurl", "html_image", "missing_mime", "history_error", "legacy_narrative", "dynamic_narrative",
         "dual", "dual_union_failure", "dual_vghks_failure", "dual_no_uid",
         "dual_union_login_error", "dual_vghks_login_error", "dual_missing_second")
ERRORS = {"bad_imgurl", "bad_uid", "unsafe_imgurl", "html_image", "history_error"}
DUAL_MRNS = {"union": "0000001", "vghks": "0000002"}


class PacsIntranet(EyeIntranet):
    def dispatch(self):
        state = self.server.test_state
        address = urlsplit(self.path)
        path = address.path
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        account = cookie["PACS_ACCOUNT"].value if "PACS_ACCOUNT" in cookie else ""
        dual = state.get("dual", False)
        if dual and path == "/index.do":
            assert not account, "new SDK inherited another account's Cookie"
        if dual and path == "/login.do" and self.command == "POST":
            params = dict(parse_qsl(self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode()))
            account = {"UNION-SYNTHETIC": "union", "VGHKS-SYNTHETIC": "vghks"}[params["muid"]]
            assert "PACS_ACCOUNT" not in cookie, "cross-account Cookie at password POST"
            assert params["mpassword"] == ("UNION-ONLY" if account == "union" else "VGHKS-ONLY")
            state["login_posts"] += 1
            state["login_accounts"].append(account)
            if state["mode"] == f"dual_{account}_login_error":
                return self.reply("synthetic login HTTP failure", 501)
            body = b"<script>var targetUrl='myPortal.do?thetime=1';</script>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Set-Cookie", f"PACS_ACCOUNT={account}; Path=/; Secure; HttpOnly")
            self.end_headers()
            return self.wfile.write(body)
        if dual and path == "/ssoFromDn.do":
            assert account in DUAL_MRNS
            base = state["origin"] + "/PRQWeb"
            fields = {"HID": f"SYNTHETIC-{account.upper()}-HID", "ssID": "test", "keyOne": "1",
                      "keyTwo": "2", "keyThree": "3", "uid": account, "targetURL": base + "/landing.do"}
            return self.reply(f'<form action="{base}/WPSAutoLogon">' + "".join(
                f'<input name="{key}" value="{value}">' for key, value in fields.items()) + "</form>")
        if path == "/login.do" and self.command == "GET":
            return self.binary_reply(LOGIN, "text/html")
        handled = {"/PRQWeb/QueryOrderResult.do", "/PRQWeb/QueryReportByOrder.do",
                   "/PRQWeb/Adm_QueryPACS.do", "/PRQWeb/Page/JSP/showPACSPic.jsp"}
        if dual:
            handled.add("/PRQWeb/QueryPatientRecord.do")
        if path not in handled:
            if path.startswith("/disabled/") or path in {
                "/PRQWeb/QueryCaseList.do", "/PRQWeb/QueryBillingSOAP.do", "/xwado/PushWADO"
            }:
                state["forbidden"].append(path)
                return self.reply("outside PACS test scope", 500)
            return super().dispatch()
        state["paths"].append(path)
        params = dict(parse_qsl(address.query, keep_blank_values=True))
        if self.command == "POST":
            params.update(parse_qsl(self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode()))
        mode = state["mode"]
        mrn = DUAL_MRNS[account] if dual else SYNTHETIC_MRN
        if dual:
            state["account_requests"].append(account)
            assert account in DUAL_MRNS
            if path.endswith("QueryPatientRecord.do"):
                assert (params.get("queryPtID") or params.get("id")) == mrn
                state["queried_mrns"].add(mrn)
                return self.reply('<frameset><frame src="/PRQWeb/Page/JSP/KS_Patient.jsp">'
                                  '<frame src="/PRQWeb/QueryCaseList.do"></frameset>')
            assert params["hhisnum"] == mrn
            assert params["hid"] == f"SYNTHETIC-{account.upper()}-HID"
            mode = "imgurl_only" if account == "union" else "no_images" if mode == "dual_no_uid" else "uid_only"
        if path.endswith("QueryOrderResult.do"):
            state["history_filters"].append({key: params.get(key) for key in ("Use", "date", "ordertype")})
            if mode == "history_error":
                return self.reply("synthetic changed schema")
            body = '<div id="orderList"></div>'
            for sequence, status in (("P1", "完成"), ("P2", "完成"), ("UNEXECUTED", "未執行")):
                query = urlencode({"hhisnum": mrn, "caseType": "O", "caseNo": "EYE",
                                   "seqNo": sequence, "orDept": "CHK"})
                body += f"""<script>var orderStr='<a href="/PRQWeb/QueryOrderDetail.do?{query}">Synthetic CT {sequence}</a>';
                var report='/PRQWeb/QueryReportByOrder.do?{query}';
                new KSCase('',orderStr,'2026-09-21','','','{status}','');</script>"""
            return self.reply(body)
        if path.endswith("QueryReportByOrder.do"):
            sequence = params["seqNo"]
            state["report_sequences"].append(sequence)
            request = "UID" if sequence == "P1" else "IMGURL"
            text = (f'<script>{NARRATIVE}</script>' if mode == "legacy_narrative" else
                    '<script>document.write(fetchSecret());</script>' if mode == "dynamic_narrative" else
                    'Synthetic CT text')
            return self.reply(f'<table><tr><th>報告內容</th><td><pre>{text}</pre>'
                              f'<span reqno="{request}" hhisnum="{mrn}">JPG</span></td></tr></table>')
        if path.endswith("Adm_QueryPACS.do"):
            request = params["reqno"]
            state["studies"].append(request)
            if mode == "no_images" or (mode == "uid_only" and request == "IMGURL") or (
                    mode == "imgurl_only" and request == "UID"):
                return self.reply('<div id="pacsContent"><!-- synthetic branch -->查無資料!!</div>')
            tags = []
            for number in range(59 if request == "IMGURL" else 2):
                fields = {"hhisnum": mrn, "hid": "OLD-HID", "reqno": request}
                if request == "IMGURL":
                    url = image_url(f"1.2.840.10008.{number}")
                    if mode == "unsafe_imgurl":
                        url = url.replace("192.168.20.137:800", "example.invalid")
                    fields["imgUrl"] = url
                else:
                    fields.update(uid=f"UID-{number}", SERIES_UID="SERIES", STUDY_UID="STUDY")
                source = "/PRQWeb/Page/JSP/showPACSPic.jsp?" + urlencode(fields)
                tags.append(f'<img class="pacsJpg" src="{html.escape(source, quote=True)}">')
            return self.reply('<div id="pacsContent">' + "".join(tags) + '</div>')
        assert self.command == "GET"
        assert params["hhisnum"] == mrn
        assert params["hid"] == (f"SYNTHETIC-{account.upper()}-HID" if dual else "SYNTHETIC-HID")
        assert self.headers.get("Accept", "").startswith("image/")
        referer = urlsplit(self.headers.get("Referer", ""))
        assert referer.path == "/PRQWeb/Adm_QueryPACS.do"
        assert dict(parse_qsl(referer.query))["reqno"] == params["reqno"]
        if "imgUrl" in params:
            assert set(params) == {"hhisnum", "hid", "reqno", "imgUrl"}
            number = int(dict(parse_qsl(urlsplit(params["imgUrl"]).query))["objectUID"].rsplit(".", 1)[1])
            kind = "imgUrl"
        else:
            assert set(params) == {"hhisnum", "hid", "reqno", "uid", "SERIES_UID", "STUDY_UID"}
            assert (params["SERIES_UID"], params["STUDY_UID"]) == ("SERIES", "STUDY")
            number, kind = int(params["uid"].split("-")[1]), "uid"
        state["images"].append({"mode": kind, "number": number})
        if number == 0 and ((mode == "bad_imgurl" and kind == "imgUrl") or (mode == "bad_uid" and kind == "uid")):
            return self.binary_reply(b"\xff\xd8incomplete", "image/jpeg")
        if dual and number == 0 and state["mode"] == f"dual_{account}_failure":
            return self.binary_reply(b"\xff\xd8incomplete", "image/jpeg")
        if mode == "html_image" and kind == "imgUrl" and number == 0:
            return self.binary_reply(LOGIN, "image/jpeg")
        return self.binary_reply(JPEG, "" if mode == "missing_mime" else "image/jpeg")


def verify(mode, args, output):
    dual = mode.startswith("dual")
    helper = LegacyAesLoopbackTests()
    helper.setUp()
    try:
        helper.server.RequestHandlerClass = PacsIntranet
        origin = f"https://localhost:{helper.server.server_port}"
        helper.server.test_state = state = {
            "origin": origin, "mode": mode, "reject_login": False, "login_posts": 0,
            "queried_mrns": set(), "paths": [], "bad_headers": [], "order_sequences": [],
            "report_sequences": [], "image_ids": [], "images": [], "studies": [],
            "forbidden": [], "history_filters": [],
            "dual": dual, "login_accounts": [], "account_requests": [],
        }

        class IPv6Server(type(helper.server)):
            address_family = socket.AF_INET6

        ipv6 = IPv6Server(("::1", helper.server.server_port), PacsIntranet)
        ipv6.test_state = state
        helper.addCleanup(ipv6.server_close)
        helper.addCleanup(ipv6.shutdown)
        threading.Thread(target=ipv6.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        with tempfile.TemporaryDirectory(prefix=f"pacs-{mode}-", dir=output) as temporary:
            root = Path(temporary).resolve()
            if not root.is_relative_to(output.resolve()):
                raise SystemExit("Temporary directory escapes output")
            config = {
                "schema_version": LIVE_CONFIG_SCHEMA_VERSION, "profile": "pacs", "test_mrn": DUAL_MRNS["union"] if dual else SYNTHETIC_MRN,
                "ca_bundle": helper.ca,
                "endpoint_overrides": {key + "_base_url": origin + path for key, path in {
                    "portal": "", "prq": "/PRQWeb", "sectord": "/disabled/SectOrdWeb",
                    "webmaas": "/disabled/webmaas", "oppl": "/disabled/OPPLWeb", "audit": "/disabled/Audit",
                    "mis": "/disabled/mis", "review": "/disabled/Pck", "personnel": "/disabled/DDPortal",
                    "attendance": "/disabled/ATTN",
                }.items()},
                "request_policy": {"min_delay_seconds": 0, "max_delay_seconds": 0,
                                   "max_attempts": 1, "connect_timeout_seconds": 0.3, "read_timeout_seconds": 2},
            }
            config_path = root / "config.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            env = {key: value for key, value in os.environ.items() if not key.upper().startswith("VGHKS_")
                   and key.upper() not in {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
                                         "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"}}
            env.update(VGHKS_USERNAME="SYNTHETIC", VGHKS_PASSWORD="SYNTHETIC-ONLY", PYTHONPATH=str(ROOT / "src"),
                       PYTHONIOENCODING="utf-8", NO_PROXY="localhost,127.0.0.1,::1")
            if dual:
                env.update(VGHKS_USERNAME="UNION-SYNTHETIC", VGHKS_PASSWORD="UNION-ONLY",
                           VGHKS_COMPARISON_USERNAME="VGHKS-SYNTHETIC", VGHKS_COMPARISON_PASSWORD="VGHKS-ONLY",
                           VGHKS_COMPARISON_MRN=DUAL_MRNS["vghks"])
                if mode == "dual_missing_second":
                    del env["VGHKS_COMPARISON_PASSWORD"]
            command = [sys.executable, "-m", "vghks_sdk.live_test_app", "--profile", "pacs"]
            if not args.source:
                executable = Path(shutil.copy2(args.exe.resolve(), root / "vghks-live-test.exe"))
                command = [str(executable)]
            options = {"cwd": root, "env": env, "capture_output": True, "text": True, "encoding": "utf-8",
                       "errors": "replace", "timeout": 120,
                       "creationflags": subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0}
            checked = subprocess.run([*command, "--plan"], **options)
            assert checked.returncode == 0, checked.stdout + checked.stderr
            planned = json.loads(checked.stdout)
            assert planned["profile"] == "pacs"
            assert planned["pacs_comparison"]["enabled"]
            run_root = root / "run"
            launch = [*command, "--config", str(config_path), "--non-interactive", "--output", str(run_root)]
            inputs = "\n"
            if not dual:
                launch.append("--pacs-single-account")
            if not args.source and mode == "dual":
                # No-argument launch must honor the built scope despite old settings.
                env.update(VGHKS_TEST_MRN=DUAL_MRNS["union"], VGHKS_CA_BUNDLE=helper.ca,
                           VGHKS_LIVE_OUTPUT_ROOT=str(root / "runs"), VGHKS_LIVE_PROFILE="full",
                           VGHKS_DELAY_MIN="0", VGHKS_DELAY_MAX="0", VGHKS_MAX_ATTEMPTS="1",
                           VGHKS_CONNECT_TIMEOUT="0.3", VGHKS_READ_TIMEOUT="2")
                env.update({"VGHKS_" + key.upper(): endpoint for key, endpoint in config["endpoint_overrides"].items()})
                (root / "live-test-config.json").write_text(json.dumps({"profile": "full"}), encoding="utf-8")
                # Windows getpass reads its console directly, so a hidden
                # subprocess cannot receive passwords over piped stdin.
                # Unit tests cover six interactive inputs before execution;
                # the real zero-argument EXE receives synthetic inputs via env.
                launch = command
            process = subprocess.run(launch, input=inputs, **options)
            label = f"pacs-{'source' if args.source else 'exe'}-{mode}"
            (output / f"{label}.log").write_text(process.stdout + process.stderr, encoding="utf-8")
            if dual:
                return verify_dual_result(mode, root, output, label, command, options, process, state)
            summary = json.loads((run_root / "run_summary.json").read_text(encoding="utf-8"))
            assert summary["profile"] == "pacs"
            assert process.returncode == (1 if mode in ERRORS else 0), (mode, process.stdout, process.stderr)
            assert state["history_filters"] == [{"Use": "Dur", "date": "4000", "ordertype": "*"},
                                                {"Use": "Dur", "date": "4000", "ordertype": "OR"}]
            assert not state["forbidden"] and not state["bad_headers"]
            assert sorted(state["report_sequences"]) == ([] if mode == "history_error" else ["P1", "P2"])
            assert state["login_posts"] == (2 if mode == "html_image" else 1)
            expected_uid = 0 if mode in {"imgurl_only", "no_images", "history_error"} else 1 if mode == "bad_uid" else 2
            expected_img = 0 if mode in {"uid_only", "no_images", "unsafe_imgurl", "history_error"} else 58 if mode in {"bad_imgurl", "html_image"} else 59
            archives = list(root.glob("*.zip"))
            assert len(archives) == 1
            with zipfile.ZipFile(archives[0]) as bundle:
                assert bundle.testzip() is None
                evidence = json.loads(bundle.read("parsed/workflows/ophthalmology_orders/pacs_verification.json"))
                assert evidence["modes"]["uid"]["downloaded_files"] == expected_uid, (mode, evidence)
                assert evidence["modes"]["imgUrl"]["downloaded_files"] == expected_img, (mode, evidence)
                for kind in ("uid", "imgUrl"):
                    if mode == "history_error" or (mode == "unsafe_imgurl" and kind == "imgUrl"):
                        expected_status = "BLOCKED"
                    elif mode == "no_images" or (mode == "uid_only" and kind == "imgUrl") or (mode == "imgurl_only" and kind == "uid"):
                        expected_status = "NO_SAMPLE"
                    elif (mode in {"bad_imgurl", "html_image"} and kind == "imgUrl") or (mode == "bad_uid" and kind == "uid"):
                        expected_status = "ERROR"
                    else:
                        expected_status = "OK"
                    assert evidence["modes"][kind]["status"] == expected_status, (mode, evidence)
                files = [name for name in bundle.namelist() if name.startswith("parsed/") and name.endswith(".jpg")]
                assert len(files) == expected_uid + expected_img
                assert all(bundle.read(name) == JPEG for name in files)
                if mode in {"legacy_narrative", "dynamic_narrative"}:
                    reports = [json.loads(bundle.read(name)) for name in bundle.namelist()
                               if name.endswith('-order_report.json')]
                    assert len(reports) == 2
                    for value in reports:
                        if mode == "legacy_narrative":
                            assert value["report_text"] == 'First synthetic line\n第二行 & <literal>\nLast line'
                            assert not value["text_extraction_notes"]
                        else:
                            assert not value["report_text"] and value["text_extraction_notes"]
                    assert summary["status"] == ("OK" if mode == "legacy_narrative" else "COMPLETED_WITH_GAPS")
            analyzed = subprocess.run([*command, "--analyze", str(archives[0]), "--analysis-output", str(root / "analysis")], **options)
            assert analyzed.returncode in {0, 1}, analyzed.stdout + analyzed.stderr
            analysis = json.loads((root / "analysis/analysis.json").read_text(encoding="utf-8"))
            recipe = json.loads((root / "analysis/retest-config.json").read_text(encoding="utf-8"))
            assert recipe["profile"] == "pacs" and recipe["login_negative_attempts"] == 0
            assert recipe["pacs_single_account"]
            assert analysis["order_assets"]["jpg_files"] == expected_uid + expected_img
            assert analysis["order_assets"]["status"] == ("NO_ASSETS" if not files else "OK")
            assert bool(analysis["problems"]) == (mode in ERRORS), (mode, analysis["problems"])
            assert len(analysis["data_quality"]["recorded_partial_steps"]) == (2 if mode == "dynamic_narrative" else 0)
            assert analysis["pacs_test"]["modes"]["uid"]["verified_files"] == expected_uid
            assert analysis["pacs_test"]["modes"]["imgUrl"]["verified_files"] == expected_img
            report = {"scenario": mode, "status": summary["status"], "modes": evidence["modes"],
                      "login_posts": state["login_posts"], "only_prq_proxy_downloads": True,
                      "original_bytes_preserved": True, "offline_zip_replay_verified": True}
            (output / f"{label}.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
            return report
    finally:
        helper.doCleanups()


def verify_dual_result(mode, root, output, label, command, options, process, state):
    archives = list(root.glob("*.zip"))
    assert not state["forbidden"] and not state["bad_headers"]
    if mode == "dual_missing_second":
        assert process.returncode == 2, process.stdout + process.stderr
        assert state["login_posts"] == 0 and not state["account_requests"]
        assert len(archives) == 1
        with zipfile.ZipFile(archives[0]) as bundle:
            assert json.loads(bundle.read("run_summary.json"))["status"] == "BOOTSTRAP_FAILED"
        report = {"scenario": mode, "status": "BOOTSTRAP_FAILED", "login_posts": 0,
                  "all_inputs_required_before_network": True}
    else:
        expected_error = mode.endswith("failure") or mode.endswith("login_error")
        assert process.returncode == (1 if expected_error else 0), process.stdout + process.stderr
        assert state["login_accounts"] == ["union", "vghks"]
        assert state["login_posts"] == 2
        assert state["account_requests"] == sorted(state["account_requests"], key=lambda s: s == "vghks")
        assert len(archives) == 2, process.stdout + process.stderr
        summaries = list(root.glob("vghks-pacs-comparison-*.json"))
        assert len(summaries) == 1
        comparison = json.loads(summaries[0].read_text(encoding="utf-8"))
        assert comparison["scope"] == ["union.imgUrl", "vghks.uid"]
        assert comparison["status"] == ("COMPLETED_WITH_ERRORS" if expected_error else
                                         "COMPLETED_WITH_GAPS" if mode == "dual_no_uid" else "OK")
        assert [row["hospital"] for row in comparison["accounts"]] == ["union", "vghks"]
        for archive in archives:
            with zipfile.ZipFile(archive) as bundle:
                assert bundle.testzip() is None
                account = json.loads(bundle.read("pacs_account.json"))["hospital"]
                kind = "imgUrl" if account == "union" else "uid"
                other = "uid" if account == "union" else "imgUrl"
                assert f"-{account}-" in archive.name
                summary = json.loads(bundle.read("run_summary.json"))
                assert summary["test_mrn"] == DUAL_MRNS[account]
                assert summary["pacs_account"]["expected_mode"] == kind
                evidence = json.loads(bundle.read("parsed/workflows/ophthalmology_orders/pacs_verification.json"))
                account_error = mode in {f"dual_{account}_failure", f"dual_{account}_login_error"}
                expected_files = 59 if account == "union" else 2
                expected_status = "OK"
                if mode == f"dual_{account}_failure":
                    expected_files -= 1
                    expected_status = "ERROR"
                elif mode == f"dual_{account}_login_error":
                    expected_files, expected_status = 0, "BLOCKED"
                elif mode == "dual_no_uid" and account == "vghks":
                    expected_files, expected_status = 0, "NO_SAMPLE"
                assert evidence["modes"][kind]["status"] == expected_status, (mode, account, evidence)
                assert evidence["modes"][kind]["downloaded_files"] == expected_files
                assert evidence["modes"][other]["downloaded_files"] == 0
                row = next(row for row in comparison["accounts"] if row["hospital"] == account)
                assert row["expected_mode_status"] == expected_status
                assert row["archive"] == archive.name
                assert (summary["exit_code"] == 1) == account_error
                files = [name for name in bundle.namelist() if name.startswith("parsed/") and name.endswith(".jpg")]
                assert len(files) == expected_files
                assert all(bundle.read(name) == JPEG for name in files)
                requests = b"".join(bundle.read(name) for name in bundle.namelist()
                                    if name.startswith("requests/") and not name.endswith("/"))
                opposite = "vghks" if account == "union" else "union"
                assert DUAL_MRNS[opposite].encode() not in requests
                assert ("VGHKS-ONLY" if account == "union" else "UNION-ONLY").encode() not in requests
                safe = bundle.read("run_config.json") + json.dumps(comparison).encode()
                safe += b"".join(bundle.read(name) for name in bundle.namelist()
                                 if name.startswith("diagnostics/") and not name.endswith("/"))
                assert all(secret not in safe for secret in (b"UNION-ONLY", b"VGHKS-ONLY"))
                if not account_error:
                    empty = [step for step in summary["steps"] if step["operation"] == "prq.pacs_study"
                             and step["status"] == "EMPTY"]
                    assert empty, "double-exclamation empty viewer was not recorded as EMPTY"
            analysis_root = root / f"analysis-{account}"
            analyzed = subprocess.run([*command, "--analyze", str(archive), "--analysis-output", str(analysis_root)], **options)
            assert analyzed.returncode in {0, 1}, analyzed.stdout + analyzed.stderr
            analysis = json.loads((analysis_root / "analysis.json").read_text(encoding="utf-8"))
            recipe = json.loads((analysis_root / "retest-config.json").read_text(encoding="utf-8"))
            assert recipe["profile"] == "pacs" and recipe["pacs_single_account"]
            assert analysis["order_assets"]["jpg_files"] == expected_files
            assert analysis["order_assets"]["status"] == ("OK" if files else "NO_ASSETS")
            assert bool(analysis["problems"]) == account_error, (mode, account, analysis["problems"])
        report = {"scenario": mode, "status": comparison["status"], "accounts": comparison["accounts"],
                  "login_posts": 2, "sessions_and_patient_scopes_isolated": True,
                  "sequence": state["login_accounts"], "offline_zip_replay_verified": True,
                  "interactive_zero_argument_launch": mode == "dual" and len(command) == 1,
                  "synthetic_credentials_source": "environment"}
    (output / f"{label}.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", action="store_true")
    parser.add_argument("--exe", type=Path, default=ROOT / "dist/vghks-live-test.exe")
    args = parser.parse_args()
    output = ROOT / "output"
    if output.is_symlink() or output.resolve().parent != ROOT:
        raise SystemExit("Output escapes workspace")
    output.mkdir(exist_ok=True)
    results = [verify(mode, args, output) for mode in MODES]
    report = {"sdk_version": __version__, "mode": "source" if args.source else "exe",
              "localhost_only": True, "scenarios": results}
    destination = output / f"pacs-{'source' if args.source else 'exe'}-verification.json"
    destination.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"PACS {report['mode']}: {len(results)} localhost HTTPS scenarios and offline ZIP replay passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
