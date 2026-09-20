"""Build one current Windows EXE; preserve any results beside it."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import platform
import re
import runpy
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

PYINSTALLER_VERSION = "6.14.2"
ROOT = Path(__file__).resolve().parents[1]
SDK_VERSION = str(runpy.run_path(ROOT / "src" / "vghks_sdk" / "_version.py")["__version__"])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--defaults", type=Path, help="private JSON containing only test_mrn")
    args = parser.parse_args()
    defaults = None
    if args.defaults:
        defaults = json.loads(args.defaults.read_text(encoding="utf-8"))
        if (
            not isinstance(defaults, dict)
            or set(defaults) != {"test_mrn"}
            or not isinstance(defaults["test_mrn"], str)
            or not defaults["test_mrn"].strip()
        ):
            raise SystemExit("Private defaults must contain only a nonempty test_mrn string.")
    if platform.system() != "Windows" or platform.architecture()[0] != "64bit":
        raise SystemExit("Build requires 64-bit Windows.")
    if sys.version_info[:2] != (3, 10):
        raise SystemExit("The pinned build requires Python 3.10.")
    import PyInstaller
    import truststore

    if PyInstaller.__version__ != PYINSTALLER_VERSION or truststore.__version__ != "0.10.4":
        raise SystemExit("Build requires PyInstaller 6.14.2 and truststore 0.10.4.")
    if not re.fullmatch(r"\d+\.\d+\.\d+", SDK_VERSION):
        raise SystemExit("Invalid release version.")
    build_parent = ROOT / "build"
    release = ROOT / "dist"
    for directory in (build_parent, release):
        if directory.is_symlink() or directory.resolve().parent != ROOT:
            raise SystemExit("Build/release directory escapes the workspace.")
        directory.mkdir(exist_ok=True)
    output = ROOT / "output"
    output.mkdir(exist_ok=True)
    build = {
        "sdk_version": SDK_VERSION,
        "build_id": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        "target": "Windows x64",
        "python": platform.python_version(),
        "pyinstaller": PyInstaller.__version__,
        "truststore": truststore.__version__,
    }
    # Only this generated temporary directory is cleaned up. Never recursively
    # delete dist: users may have put an actual intranet return ZIP there.
    with tempfile.TemporaryDirectory(prefix="live-test-", dir=build_parent) as temporary:
        workspace = Path(temporary).resolve()
        if not workspace.is_relative_to(build_parent.resolve()):
            raise SystemExit("Temporary build path escapes the build directory.")
        metadata = workspace / "_build_info.json"
        metadata.write_text(json.dumps(build), encoding="utf-8")
        private_args = []
        if defaults is not None:
            private_file = workspace / "_live_defaults.json"
            private_file.write_text(json.dumps(defaults), encoding="utf-8")
            private_args = ["--add-data", f"{private_file}{os.pathsep}vghks_sdk/live"]
        staging = workspace / "staging"
        environment = os.environ.copy()
        environment["PYINSTALLER_CONFIG_DIR"] = str(workspace / "config")
        subprocess.run(
            [
                sys.executable,
                "-m",
                "PyInstaller",
                "--noconfirm",
                "--clean",
                "--onefile",
                "--console",
                "--noupx",
                "--hidden-import",
                "truststore._windows",
                "--add-data",
                f"{metadata}{os.pathsep}vghks_sdk",
                *private_args,
                "--name",
                "vghks-live-test",
                "--paths",
                str(ROOT / "src"),
                "--distpath",
                str(staging),
                "--workpath",
                str(workspace / "work"),
                "--specpath",
                str(workspace / "spec"),
                str(ROOT / "run_live_test.py"),
            ],
            cwd=ROOT,
            env=environment,
            check=True,
        )
        executable = staging / "vghks-live-test.exe"
        # Validate the candidate before replacing the current executable.
        with (output / "build-checks.log").open("w", encoding="utf-8") as log:
            for arguments in (
                ["--self-check"],
                ["--list-operations"],
                ["--plan"],
                ["--plan", "--only", "prq.soap"],
                ["--plan", "--profile", "comprehensive"],
                ["--plan", "--profile", "ophthalmology"],
            ):
                subprocess.run(
                    [str(executable), *arguments],
                    cwd=ROOT,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    check=True,
                )
        destination = release / executable.name
        candidate = release / ".vghks-live-test.exe.tmp"
        if destination.is_symlink() or candidate.is_symlink():
            raise SystemExit("Executable destination must not be a symlink.")
        shutil.copy2(executable, candidate)
        os.replace(candidate, destination)
    with contextlib.suppress(OSError):
        build_parent.rmdir()  # Only succeeds when the directory is empty.
    (output / "build-info.json").write_text(json.dumps(build, indent=2) + "\n", encoding="utf-8")
    print(f"Built {SDK_VERSION}: {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
