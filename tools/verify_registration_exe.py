"""Verify registration batches with source/frozen CLI and localhost HTTPS only."""

from __future__ import annotations

import argparse
import ctypes
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

from test_auth_failures import DENIED_PAGE  # noqa: E402
from test_tls import LegacyAesLoopbackTests  # noqa: E402
from test_webmaas import TOKEN, registration  # noqa: E402
from verify_patient_exe import PatientIntranet  # noqa: E402

from vghks_sdk.offline.analyze import inspect_bundle  # noqa: E402
from vghks_sdk.offline.bundle import BundleReader  # noqa: E402

MRNS = ["TEST001", "TEST002", "TEST003"]
MODES = ("success", "empty", "parse_error", "mismatch", "login_rejected", "login_http_error", "unknown_only",
         "interactive_file", "interactive_paste", "missing_inputs")


class RegistrationIntranet(PatientIntranet):
    def dispatch(self):
        address = urlsplit(self.path)
        state = self.server.test_state
        if address.path == "/login.do" and self.command == "POST" and state["mode"] == "login_rejected":
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            state["login_posts"] += 1
            return self.reply(DENIED_PAGE)
        if address.path != "/webmaas/RSV/RSV11W001.do":
            return super().dispatch()
        params = dict(parse_qsl(address.query, keep_blank_values=True))
        if self.command == "GET" and "d-123-p" in params:
            state["next_pages"] += 1
            return self.reply(registration(params["patno"], registered_by="MWEB"))
        if self.command != "POST":
            return super().dispatch()
        params.update(parse_qsl(self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode(),
                                keep_blank_values=True))
        assert state["role"] == "maas_RSV11"
        assert params[TOKEN] == state["token"] and params["QRY"] == "QRY"
        state["query_posts"] += 1
        mrn = params["patno"]
        state["registration_mrns"].append(mrn)
        if state["mode"] == "parse_error" and mrn == MRNS[1]:
            return self.reply("<html>synthetic unknown layout</html>")
        if state["mode"] == "mismatch" and mrn == MRNS[1]:
            return self.reply(registration("WRONG-PATIENT"))
        empty = state["mode"] == "empty"
        marked = mrn == MRNS[0] and state["mode"] != "unknown_only" and not empty
        return self.reply(registration(
            mrn, empty=empty, registered_by="MWEB" if mrn == MRNS[0] else "OPDTESTF",
            notes_html='<label title="未到診"><img src="../images/icon/Rsdefyrc.png"/></label>' if marked else "",
            next_href=f"?d-123-p=2&patno={mrn}" if marked else "",
        ))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", action="store_true")
    args = parser.parse_args()
    wire_encoding = "utf-8" if args.source or os.name != "nt" else f"cp{ctypes.windll.kernel32.GetACP()}"

    def console_text(data: bytes) -> str:
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return data.decode(wire_encoding)

    output = ROOT / "output"
    if output.is_symlink() or output.resolve().parent != ROOT:
        raise SystemExit("Output escapes workspace")
    output.mkdir(exist_ok=True)
    helper = LegacyAesLoopbackTests()
    helper.setUp()
    helper.server.RequestHandlerClass = RegistrationIntranet
    origin = f"https://localhost:{helper.server.server_port}"
    endpoints = {key + "_base_url": origin + path for key, path in {
        "portal": "", "sectord": "/SectOrdWeb", "webmaas": "/webmaas", "prq": "/PRQWeb",
        "oppl": "/OPPLWeb", "audit": "/PRQWeb", "mis": "", "review": "/Pck",
    }.items()}
    policy = {"min_delay_seconds": 0, "max_delay_seconds": 0, "max_attempts": 1,
              "connect_timeout_seconds": 0.5, "read_timeout_seconds": 3}
    results = []
    try:
        for mode in MODES:
            helper.server.test_state = state = {
                "mode": mode, "origin": origin, "reject_login": mode == "login_http_error",
                "login_posts": 0, "queried_mrns": set(), "keys": 0, "role": "", "roles": [],
                "token_number": 0, "token": "", "query_posts": 0, "next_pages": 0,
                "break_basic": False, "single_rsv_landing": False, "rsv_landing_gets": 0,
                "registration_mrns": [],
            }
            with tempfile.TemporaryDirectory(prefix="registration-exe-", dir=output) as temporary:
                directory = Path(temporary).resolve()
                if not directory.is_relative_to(output.resolve()):
                    raise SystemExit("Temporary directory escapes output")
                executable = None if args.source else Path(shutil.copy2(ROOT / "dist/vghks-live-test.exe", directory))
                config = {"schema_version": 8, "profile": "registrations", "ca_bundle": helper.ca,
                          "endpoint_overrides": endpoints, "request_policy": policy}
                list_path = directory / "病歷號 清單.csv"
                list_path.write_text("mrn\nTEST001\nTEST002\nTEST001\nTEST003\n", encoding="utf-8-sig")
                env = {key: value for key, value in os.environ.items()
                       if not key.upper().startswith("VGHKS_")
                       and key.upper() not in {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
                                               "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"}}
                env.update(VGHKS_USERNAME="SYNTHETIC", VGHKS_PASSWORD="SYNTHETIC-ONLY",
                           NO_PROXY="localhost,127.0.0.1", PYTHONUTF8="1")
                interactive = mode.startswith("interactive_")
                if interactive:
                    env.update({"VGHKS_" + key.removesuffix("_base_url").upper() + "_BASE_URL": value
                                for key, value in endpoints.items()})
                    env.update(VGHKS_CA_BUNDLE=str(helper.ca), VGHKS_DELAY_MIN="0", VGHKS_DELAY_MAX="0",
                               VGHKS_MAX_ATTEMPTS="1", VGHKS_LIVE_PROFILE="pacs")
                    if args.source:
                        code = (
                            "import sys; from unittest.mock import patch; "
                            f"sys.path.insert(0, {str(ROOT / 'src')!r}); "
                            "from vghks_sdk.live_test_app import main; "
                            "patcher=patch('vghks_sdk.live_test_app.build_identity', "
                            "return_value={'default_profile':'registrations'}); patcher.start(); "
                            "raise SystemExit(main([]))"
                        )
                        command = [sys.executable, "-c", code]
                    else:
                        command = [str(executable)]
                    input_text = str(list_path) + "\n\n" if mode == "interactive_file" else "\nTEST001,TEST002\nTEST001 TEST003\n\n\n"
                else:
                    config_path = directory / "batch.json"
                    config_path.write_text(json.dumps(config), encoding="utf-8")
                    command = [sys.executable, str(ROOT / "run_live_test.py")] if args.source else [str(executable)]
                    command += ["--config", str(config_path), "--non-interactive", "--no-pause"]
                    if mode != "missing_inputs":
                        command += ["--mrn-file", str(list_path)]
                    input_text = ""
                process = subprocess.run(command, cwd=directory, env=env,
                                         input=input_text.encode(wire_encoding), capture_output=True, timeout=75)
                stdout, stderr = console_text(process.stdout), console_text(process.stderr)
                expected_exit = 2 if mode == "missing_inputs" else 1 if mode in {"parse_error", "mismatch", "login_rejected", "login_http_error"} else 0
                assert process.returncode == expected_exit, (mode, stdout[-2500:], stderr[-1000:])
                archives = list(directory.glob("*.zip"))
                assert len(archives) == 1, (mode, stdout)
                with zipfile.ZipFile(archives[0]) as archive:
                    run = json.loads(archive.read("run_summary.json"))
                    if mode == "missing_inputs":
                        assert state["login_posts"] == 0 and state["query_posts"] == 0
                        assert run["status"] == "BOOTSTRAP_FAILED"
                        results.append({"mode": mode, "status": run["status"], "password_posts": 0})
                        continue
                    saved = json.loads(archive.read("run_config.json"))
                    assert saved["patient_mrns"] == MRNS and saved["profile"] == "registrations"
                    assert saved["login_negative_attempts"] == 0
                    summary = json.loads(archive.read("parsed/registrations/batch_summary.json"))
                    assert summary["requested_patient_count"] == 3 and summary["pending_patient_count"] == 0
                    assert state["login_posts"] == 1
                    if mode in {"login_rejected", "login_http_error"}:
                        assert not state["registration_mrns"] and summary["blocked_patient_count"] == 3
                        assert run["status"] == ("HTTP_FAILED" if mode == "login_http_error" else "AUTHENTICATION_FAILED"), run["status"]
                    else:
                        assert state["registration_mrns"] == MRNS, (mode, state["registration_mrns"])
                        assert set(state["roles"]) == {"maas_RSV11"}
                        expected_status = "COMPLETED_WITH_ERRORS" if mode in {"parse_error", "mismatch"} else "COMPLETED_WITH_GAPS" if mode in {"empty", "unknown_only"} else "OK"
                        assert run["status"] == expected_status, (mode, run["status"])
                        if mode not in {"empty", "unknown_only"}:
                            rows = json.loads(archive.read("parsed/registrations/patients/000001.json"))
                            assert [row["no_show"] for row in rows] == [True, None]
                            assert all(row["registered_by"] == "MWEB" for row in rows)
                        if mode == "unknown_only":
                            assert summary["no_show_count"] == 0 and summary["attendance_unknown_count"] == 3
                        if mode == "empty":
                            assert summary["patient_status_counts"]["EMPTY"] == 3
                with BundleReader(archives[0]) as reader:
                    analysis, recipe = inspect_bundle(reader)
                    assert recipe["profile"] == "registrations" and recipe["patient_mrns"] == MRNS
                    assert recipe["login_negative_attempts"] == 0
                    assert analysis["bundle_status"] == run["status"]
                    batch = analysis["registration_batch"]
                    assert batch["status"] == "OBSERVED", batch
                    assert batch["requested_patient_count"] == 3 and batch["pending_patient_count"] == 0
                    assert batch["record_count"] == summary["record_count"]
                    assert batch["no_show_count"] == summary["no_show_count"]
                    assert batch["verified_patient_count"] == summary["patient_status_counts"]["OK"] + summary["patient_status_counts"]["EMPTY"]
                if mode == "success":
                    analysis_output = directory / "cli-analysis"
                    analysis_command = ([sys.executable, str(ROOT / "run_live_test.py")] if args.source else [str(executable)])
                    analyzed = subprocess.run([*analysis_command, "--analyze-bundle", str(archives[0]),
                                              "--analysis-output", str(analysis_output), "--no-pause"],
                                              cwd=directory, env=env, capture_output=True, timeout=45)
                    assert analyzed.returncode == 0, (console_text(analyzed.stdout), console_text(analyzed.stderr))
                    offline = json.loads((analysis_output / "analysis.json").read_text(encoding="utf-8"))
                    assert offline["registration_batch"]["verified_patient_count"] == 3
                    assert offline["registration_batch"]["no_show_count"] == summary["no_show_count"]
                results.append({"mode": mode, "status": run["status"], "password_posts": state["login_posts"],
                                "patient_count": 3, "queried_patient_count": len(state["registration_mrns"]),
                                "no_show_count": summary["no_show_count"]})
    finally:
        helper.doCleanups()
    label = "source" if args.source else "frozen"
    (output / f"registration-{label}-verification.json").write_text(
        json.dumps({"runtime": label, "scenarios": results}, indent=2) + "\n", encoding="utf-8",
    )
    print(f"Registration {label}: {len(results)} localhost scenarios passed.")


if __name__ == "__main__":
    main()
