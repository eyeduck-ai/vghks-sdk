"""Build the patient/order-selection PACS verifier on pinned Windows Python."""

from __future__ import annotations

import json
import os
import platform
import runpy
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NAME = "vghks-pacs-launch-test"


def main() -> int:
    if platform.system() != "Windows" or platform.architecture()[0] != "64bit":
        raise SystemExit("Build requires 64-bit Windows.")
    if sys.version_info[:2] != (3, 10):
        raise SystemExit("Build requires Python 3.10.")
    import PyInstaller
    import truststore
    if PyInstaller.__version__ != "6.14.2" or truststore.__version__ != "0.10.4":
        raise SystemExit("Build requires PyInstaller 6.14.2 and truststore 0.10.4.")
    version = str(runpy.run_path(ROOT / "src/vghks_sdk/_version.py")["__version__"])
    build_root, release, output = ROOT / "build", ROOT / "dist", ROOT / "output"
    for directory in (build_root, release, output):
        if directory.is_symlink() or directory.resolve().parent != ROOT:
            raise SystemExit("Build directory escapes workspace.")
        directory.mkdir(exist_ok=True)
    metadata = {
        "sdk_version": version, "build_id": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        "target": "Windows x64", "python": platform.python_version(),
        "pyinstaller": PyInstaller.__version__, "truststore": truststore.__version__,
        "profile": "pacs_launch",
        "interactive_default_native_handoff": True,
        "noninteractive_default_native_handoff": False,
        "interactive_default_selection": "DISCOVER_AND_SELECT",
        "discovery_lookback_days": 4000,
        "discovery_batch_size": 30,
    }
    with tempfile.TemporaryDirectory(prefix="pacs-launch-", dir=build_root) as temporary:
        workspace = Path(temporary).resolve()
        if not workspace.is_relative_to(build_root.resolve()):
            raise SystemExit("Temporary build directory escapes workspace.")
        info_path = workspace / "_build_info.json"
        info_path.write_text(json.dumps(metadata), encoding="utf-8")
        env = os.environ.copy()
        env["PYINSTALLER_CONFIG_DIR"] = str(workspace / "config")
        subprocess.run([
            sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile",
            "--console", "--noupx", "--hidden-import", "truststore._windows",
            "--add-data", f"{info_path}{os.pathsep}vghks_sdk", "--name", NAME,
            "--paths", str(ROOT / "src"), "--distpath", str(workspace / "staging"),
            "--workpath", str(workspace / "work"), "--specpath", str(workspace / "spec"),
            str(ROOT / "run_pacs_launch_test.py"),
        ], cwd=ROOT, env=env, check=True)
        executable = workspace / "staging" / f"{NAME}.exe"
        checks = (
            (["--self-check"], None, None),
            (["--plan", "--non-interactive", "--request-no", "SYNTHETIC"], 0, "EXPLICIT_REFERENCE"),
            (["--plan", "--non-interactive", "--request-no", "SYNTHETIC", "--open-viewer"],
             1, "EXPLICIT_REFERENCE"),
            (["--plan", "--interactive"], 1, "DISCOVER_AND_SELECT"),
            (["--plan", "--interactive", "--prepare-only"], 0, "DISCOVER_AND_SELECT"),
            (["--plan", "--interactive", "--request-no", "SYNTHETIC"], 0, "EXPLICIT_REFERENCE"),
        )
        for arguments, native_calls, selection in checks:
            checked = subprocess.run([str(executable), *arguments], cwd=ROOT, check=True,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     text=True, encoding="utf-8", stdin=subprocess.DEVNULL,
                                     timeout=30)
            value = json.loads(checked.stdout)
            if value["sdk_version"] != version:
                raise SystemExit("Frozen version mismatch.")
            if native_calls is not None and value["native_handoff_calls"] != native_calls:
                raise SystemExit("Frozen interaction mode has an incorrect native handoff default.")
            if selection is not None and value["order_selection"] != selection:
                raise SystemExit("Frozen plan has an incorrect order selection mode.")
            if selection == "DISCOVER_AND_SELECT" and (
                    value["lookback_days"] != 4000 or value["discovery_batch_size"] != 30):
                raise SystemExit("Frozen discovery limits do not match the interactive workflow.")
        destination = release / executable.name
        candidate = release / f".{NAME}.exe.tmp"
        if destination.is_symlink() or candidate.is_symlink():
            raise SystemExit("Executable destination must not be a symlink.")
        shutil.copy2(executable, candidate)
        os.replace(candidate, destination)
    (output / "pacs-launch-build-info.json").write_text(
        json.dumps(metadata, indent=2) + "\n", encoding="utf-8",
    )
    print(f"Built {version}: {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
