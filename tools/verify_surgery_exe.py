"""Verify the frozen surgery test with synthetic localhost HTTPS only."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import replace
from pathlib import Path
from urllib.parse import parse_qsl, quote, unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests"), str(ROOT / "tools")]

import test_patient_surgery_pdf as history_fixture  # noqa: E402
from test_surgery_cases import PATH, PDF, REF, row  # noqa: E402
from test_tls import LegacyAesLoopbackTests  # noqa: E402
from verify_live_test_exe import SyntheticIntranet  # noqa: E402

from vghks_sdk.offline.analyze import inspect_bundle  # noqa: E402
from vghks_sdk.offline.bundle import BundleReader  # noqa: E402


class SurgeryIntranet(SyntheticIntranet):
    def dispatch(self):
        address = urlsplit(self.path)
        if address.path in {"/PRQWeb/QueryOpNote.do", "/PRQWeb/Page/JSP/showPDF.jsp"}:
            return self.patient_history(address)
        if address.path not in {
            "/OPPLWeb/qlogAction.do",
            "/OPPLWeb/qdataAction.do",
            "/OPPLWeb/jsp/pc/page/show/showPDF.jsp",
        }:
            return super().dispatch()
        params = dict(parse_qsl(address.query, keep_blank_values=True))
        if self.command == "POST":
            params.update(
                parse_qsl(
                    self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode(),
                    keep_blank_values=True,
                )
            )
            assert params["hid"] == "SYNTHETIC-HID"
            assert self.headers.get("X-Requested-With") == "XMLHttpRequest"
            assert "qlogAction.do" in self.headers.get("Referer", "")
        state = self.server.test_state
        if params.get("method") == "getDeptList":
            return self.reply(json.dumps({"deptlist": ["OPH"]}))
        if params.get("method") == "getQlog":
            assert params["doctVId"] == "SYNTHETIC" and params["opCode"] == "80416"
            assert params["opDept"] == "ALL"
            assert all(params[f"assDoct{i}"] == "" for i in range(1, 5))
            state["periods"].append(params["opdate"])
            selected = [0, 1, 2] if params["opdate"] == "24M" else [2, 3, 4]
            cases = [
                row(
                    replace(REF, request_no=f"TEST-{i}"),
                    day="2026-06-01" if i < 3 else "2020-06-01",
                )
                for i in selected
            ]
            return self.reply(json.dumps({"oprmlist": cases}))
        if params.get("method") == "getOpnotePDF":
            request = params["reqno"]
            assert params["hhisnum"] == REF.mrn and params["seqno"] == "0"
            state["notes"].append(request)
            if request == "TEST-0":
                return self.reply('{"rtnYN":"N"}')
            if request == "TEST-1":
                return self.reply("synthetic one-note failure", 500)
            return self.reply(
                json.dumps({"rtnYN": "Y", "path": PATH.replace(REF.request_no, request)})
            )
        if address.path.endswith("showPDF.jsp"):
            assert self.command == "GET"
            path = params["file"]
            state["pdf_attempts"].append(path)
            if "TEST-2" in path:
                return self.reply("<html>synthetic unexpected viewer</html>")
            return self.reply(PDF.decode("ascii"))
        return self.reply("synthetic route missing", 404)

    def patient_history(self, address):
        params = dict(parse_qsl(address.query))
        if self.command == "POST":
            params.update(
                parse_qsl(self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode())
            )
        assert params["hid"] == "SYNTHETIC-HID"
        state = self.server.test_state
        if address.path.endswith("QueryOpNote.do"):
            state.setdefault("patient_history_queries", []).append(params["date"])
            path = history_fixture.PATH.replace("SYNTHETIC", params["hhisnum"])
            return self.reply(
                history_fixture.page(
                    history_fixture.row(
                        history_fixture.button(path)
                        + history_fixture.button(path.replace("TEST-REQUEST", "TEST-SECOND"))
                    ),
                    history_fixture.row('<input type="button" onclick="unknownReport()">'),
                )
            )
        assert self.command == "GET"
        path = unquote(params["fileName"])
        assert params["fileName"] == quote(path, safe="")
        assert "\\" + params["hhisnum"] + "\\" in path
        state.setdefault("patient_pdf_attempts", []).append(path)
        if "TEST-REQUEST" in path:
            return self.reply("<html>synthetic one-file error</html>")
        return self.reply(PDF.decode("ascii"))


def verify_patient_history(executable, folder, base_config, env, state):
    directory = folder / "patient-history"
    directory.mkdir()
    executable = Path(shutil.copy2(executable, directory))
    config = {
        **base_config,
        **json.loads((ROOT / "configs/patient-surgery-records.example.json").read_text()),
    }
    (directory / "live-test-config.json").write_text(json.dumps(config), encoding="utf-8")
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
    (ROOT / "output/exe-patient-surgery.log").write_text(
        process.stdout + process.stderr, encoding="utf-8"
    )
    assert process.returncode == 1
    archives = list(directory.glob("*.zip"))
    assert len(archives) == 1 and not list(directory.glob("*.sha256"))
    assert state["patient_history_queries"] == ["20000", "365"]
    assert len(state["patient_pdf_attempts"]) == 2
    with zipfile.ZipFile(archives[0]) as archive:
        summary = json.loads(archive.read("run_summary.json"))
        assert summary["status"] == "COMPLETED_WITH_ERRORS"
        history = json.loads(archive.read("parsed/atomic/prq.surgery_history/0001.json"))
        assert len(history[0]["surgery_record_refs"]) == 2
        assert history[1]["surgery_record_issues"] == ["SURGERY_PDF_BUTTON_UNSUPPORTED"]
        steps = [s for s in summary["steps"] if s["operation"] == "prq.surgery_history"]
        assert all(s["issue"]["code"] == "SURGERY_PDF_LINKS_INCOMPLETE" for s in steps)
        pdf_steps = [s for s in summary["steps"] if s["operation"] == "prq.pdf_attachment"]
        assert [s["status"] for s in pdf_steps] == ["ERROR", "OK"]
        assert archive.read("parsed/atomic/prq.pdf_attachment/0002.pdf") == PDF
        assert all(not info.flag_bits & 1 for info in archive.infolist())
    return {
        "history_queries": 2,
        "pdf_attempts": 2,
        "pdf_saved": 1,
        "unknown_button_reported": True,
        "continuation_verified": True,
    }


def main():
    output = ROOT / "output"
    if output.is_symlink() or output.resolve().parent != ROOT:
        raise SystemExit("Output escaped workspace")
    helper = LegacyAesLoopbackTests()
    helper.setUp()
    helper.server.RequestHandlerClass = SurgeryIntranet
    origin = f"https://localhost:{helper.server.server_port}"
    state = {
        "origin": origin,
        "reject_login": False,
        "login_posts": 0,
        "oppl_sso_posts": 0,
        "periods": [],
        "notes": [],
        "pdf_attempts": [],
        "queried_mrns": set(),
    }
    helper.server.test_state = state
    try:
        with tempfile.TemporaryDirectory(prefix="surgery-exe-", dir=output) as temporary:
            folder = Path(temporary).resolve()
            if not folder.is_relative_to(output.resolve()):
                raise SystemExit("Temporary path escaped workspace")
            executable = Path(shutil.copy2(ROOT / "dist/vghks-live-test.exe", folder))
            config = json.loads(
                (ROOT / "configs/surgery-records.example.json").read_text(encoding="utf-8")
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
            (folder / "live-test-config.json").write_text(json.dumps(config), encoding="utf-8")
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
            process = subprocess.run(
                [str(executable), "--config", "live-test-config.json", "--non-interactive"],
                cwd=folder,
                env=env,
                input="\n",
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=90,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            (output / "exe-surgery.log").write_text(
                process.stdout + process.stderr, encoding="utf-8"
            )
            assert process.returncode == 1, process.returncode
            archives = list(folder.glob("*.zip"))
            assert len(archives) == 1 and not list(folder.glob("*.sha256"))
            assert state["periods"] == ["24M", "2YB"]
            assert len(state["notes"]) == len(set(state["notes"])) == 5
            assert len(state["pdf_attempts"]) == 3
            with zipfile.ZipFile(archives[0]) as archive:

                def read(name):
                    return json.loads(archive.read(name))

                summary = read("run_summary.json")
                assert summary["status"] == "COMPLETED_WITH_ERRORS"
                assert len(read("parsed/atomic/oppl_records.cases/0001.json")) == 3
                assert len(read("parsed/atomic/oppl_records.cases/0002.json")) == 3
                assert set(read("parsed/network/selected_profiles.json")) == {
                    "portal",
                    "oppl_records",
                }
                note_steps = [s for s in summary["steps"] if s["operation"] == "oppl_records.note"]
                assert sorted(s["status"] for s in note_steps) == [
                    "EMPTY",
                    "ERROR",
                    "OK",
                    "OK",
                    "OK",
                ]
                pdf_steps = [s for s in summary["steps"] if s["operation"] == "oppl_records.pdf"]
                assert sorted(s["status"] for s in pdf_steps) == ["ERROR", "OK", "OK"]
                assert (
                    len(
                        [
                            n
                            for n in archive.namelist()
                            if n.startswith("parsed/atomic/oppl_records.pdf/")
                            and n.endswith(".pdf")
                        ]
                    )
                    == 2
                )
                assert all(not e.flag_bits & 1 for e in archive.infolist())
            with BundleReader(archives[0]) as reader:
                _, recipe = inspect_bundle(reader)
                assert recipe["surgery_query"] == config["surgery_query"]
            patient_result = verify_patient_history(executable, folder, config, env, state)
            result = {
                "build": json.loads((output / "build-info.json").read_text()),
                "status": summary["status"],
                "intentional_failures_verified": True,
                "periods": state["periods"],
                "unique_notes_attempted": 5,
                "pdf_attempts": 3,
                "pdf_saved": 2,
                "plain_zip": True,
                "patient_history": patient_result,
            }
            (output / "exe-surgery-verification.json").write_text(
                json.dumps(result, indent=2), encoding="utf-8"
            )
    finally:
        helper.doCleanups()
    print(
        "Frozen EXE: OPPL surgery records and PRQ patient-history PDF buttons, encoding, partial errors and continuation verified."
    )


if __name__ == "__main__":
    main()
