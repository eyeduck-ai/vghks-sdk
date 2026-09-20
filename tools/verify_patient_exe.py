"""Check patient queries in the frozen EXE using only synthetic localhost HTTPS."""

from __future__ import annotations

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
from test_webmaas import TOKEN, basic_info, landing, registration  # noqa: E402
from verify_live_test_exe import SyntheticIntranet  # noqa: E402

from vghks_sdk.offline.analyze import inspect_bundle  # noqa: E402
from vghks_sdk.offline.bundle import BundleReader  # noqa: E402


class PatientIntranet(SyntheticIntranet):
    def dispatch(self):
        path = urlsplit(self.path).path
        if not path.startswith("/webmaas/") and path != "/SectOrdWeb/so.do":
            return super().dispatch()
        params = dict(parse_qsl(urlsplit(self.path).query, keep_blank_values=True))
        if self.command == "POST":
            params.update(
                parse_qsl(
                    self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode(),
                    keep_blank_values=True,
                )
            )
        state = self.server.test_state
        if path == "/SectOrdWeb/so.do":
            state["keys"] += 1
            return self.reply(f"ssID=fresh-{state['keys']}&keyOne=1&keyTwo=2&keyThree=3")
        if path == "/webmaas/WPSAutoLogon":
            role = params["externalRoles"]
            assert role in {"maas_RSV11", "maas_QRY15"}
            assert params["ssID"] == f"fresh-{state['keys']}"
            state["role"] = role
            state["roles"].append(role)
            return self.reply("synthetic SSO success", 302, location=params["targetURL"])
        if path == "/webmaas/ajax/AJAXAction.do":
            expected = "maas_QRY15" if params["pageid"] == "QUY15W001" else "maas_RSV11"
            assert state["role"] == expected
            return self.reply(
                json.dumps(
                    [
                        {
                            "patno": params["patno"],
                            "patname": "Synthetic",
                            "birthday": "2000-01-02",
                        }
                    ]
                )
            )
        if path in {"/webmaas/QUY/QUY15W001.do", "/webmaas/RSV/RSV11W001.do"}:
            basic = "/QUY/" in path
            assert state["role"] == ("maas_QRY15" if basic else "maas_RSV11")
            if self.command == "GET" and "d-123-p" not in params:
                state["token_number"] += 1
                state["token"] = f"fresh-token-{state['token_number']}"
                return self.reply(landing("QUY15WForm" if basic else "RSV11WForm", state["token"]))
            if self.command == "POST":
                assert params[TOKEN] == state["token"]
                assert params["QRY"] == "QRY"
                state["query_posts"] += 1
            if basic:
                assert params["type"] == "A" and not params["hcaseno"]
                if state["break_basic"]:
                    return self.reply("<html>synthetic layout change</html>")
                return self.reply(basic_info(params["patno"]))
            if self.command == "POST":
                return self.reply(
                    registration(params["patno"], next_href=f"?d-123-p=2&patno={params['patno']}")
                )
            state["next_pages"] += 1
            return self.reply(registration(params["patno"], day="2026-10-10"))
        return self.reply("unexpected synthetic route", 404)


def main():
    output = ROOT / "output"
    if output.is_symlink() or output.resolve().parent != ROOT:
        raise SystemExit("Output escapes workspace")
    helper = LegacyAesLoopbackTests()
    helper.setUp()
    helper.server.RequestHandlerClass = PatientIntranet
    origin = f"https://localhost:{helper.server.server_port}"
    results = []
    try:
        for broken in (False, True):
            helper.server.test_state = state = {
                "origin": origin,
                "reject_login": False,
                "login_posts": 0,
                "queried_mrns": set(),
                "keys": 0,
                "role": "",
                "roles": [],
                "token_number": 0,
                "token": "",
                "query_posts": 0,
                "next_pages": 0,
                "break_basic": broken,
            }
            with tempfile.TemporaryDirectory(prefix="patient-exe-", dir=output) as temporary:
                directory = Path(temporary).resolve()
                if not directory.is_relative_to(output.resolve()):
                    raise SystemExit("Temporary path escapes output")
                exe = Path(shutil.copy2(ROOT / "dist/vghks-live-test.exe", directory))
                config = json.loads(
                    (ROOT / "configs/patient-queries.example.json").read_text(encoding="utf-8")
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
                (directory / "live-test-config.json").write_text(
                    json.dumps(config), encoding="utf-8"
                )
                env = {
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
                env.update(
                    VGHKS_USERNAME="SYNTHETIC",
                    VGHKS_PASSWORD="SYNTHETIC-ONLY",
                    VGHKS_TEST_MRN="0000000",
                    NO_PROXY="localhost,127.0.0.1",
                )
                process = subprocess.run(
                    [str(exe), "--config", "live-test-config.json", "--non-interactive"],
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
                (output / f"exe-patient-{int(broken)}.log").write_text(
                    process.stdout + process.stderr, encoding="utf-8"
                )
                assert process.returncode == (1 if broken else 0), process.returncode
                archives = list(directory.glob("*.zip"))
                assert len(archives) == 1 and not list(directory.glob("*.sha256"))
                with zipfile.ZipFile(archives[0]) as archive:
                    summary = json.loads(archive.read("run_summary.json"))
                    expected = "COMPLETED_WITH_ERRORS" if broken else "OK"
                    assert summary["status"] == expected, summary["status"]
                    rows = json.loads(
                        archive.read("parsed/atomic/webmaas.registration_query/0001.json")
                    )
                    assert len(rows) == 2 and all(r["section_code"] == "70" for r in rows)
                    if not broken:
                        info = json.loads(
                            archive.read("parsed/atomic/webmaas.basic_info/0001.json")
                        )
                        assert (
                            info["fields"]["未來新欄位"] == "keep-me" and len(info["notices"]) == 1
                        )
                    assert all(not entry.flag_bits & 1 for entry in archive.infolist())
                assert state["roles"] == ["maas_RSV11", "maas_QRY15", "maas_RSV11"], state["roles"]
                assert state["query_posts"] == 2 and state["next_pages"] == 1
                with BundleReader(archives[0]) as reader:
                    analysis, recipe = inspect_bundle(reader)
                    assert any(
                        r["operation"] == "webmaas.basic_info" for r in analysis["operations"]
                    )
                    assert recipe["only_operations"] == config["only_operations"]
                results.append(
                    {
                        "intentional_basic_failure": broken,
                        "status": summary["status"],
                        "role_switches_verified": True,
                        "fresh_tokens_verified": True,
                        "registrations_after_basic_query": len(rows),
                        "plain_zip": True,
                    }
                )
    finally:
        helper.doCleanups()
    (output / "exe-patient-verification.json").write_text(
        json.dumps(
            {
                "build": json.loads((output / "build-info.json").read_text(encoding="utf-8")),
                "scenarios": results,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(
        "Frozen EXE: patient queries, SSO roles, tokens, pagination and error continuation verified."
    )


if __name__ == "__main__":
    main()
