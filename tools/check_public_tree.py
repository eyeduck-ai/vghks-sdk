"""Check publishable files, the Git index, or wheel/sdist contents without uploading.

The optional denylist stays local. Findings name files/rules, never matched secrets.
This guard complements review; it does not prove arbitrary text is de-identified.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import tarfile
import unicodedata
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_DIRS = {"src", "tests", "tools", "docs", "examples", "configs", ".github"}
PUBLIC_FILES = {
    "README.md",
    "AGENTS.md",
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "LICENSE",
    "pyproject.toml",
    "MANIFEST.in",
    ".gitignore",
    ".gitattributes",
    "run_sdk.py",
    "run_live_test.py",
    "run_tests.py",
}
PRIVATE_PARTS = {
    "data",
    "private",
    "output",
    "tmp",
    "dist",
    "build",
    "newHAR",
    "__pycache__",
    "captures",
    "live-test-results",
    ".venv",
    "venv",
}
BLOCKED_SUFFIXES = {".har", ".zip", ".pdf", ".jpg", ".jpeg", ".png", ".exe", ".whl", ".pyc", ".log"}
TLS_FIXTURES = {"tests/fixtures/tls/localhost-key.pem", "tests/fixtures/tls/localhost-cert.pem"}
PATTERNS = {
    "github_token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b|\bgithub_pat_[A-Za-z0-9_]{40,}\b"),
    "private_key": re.compile(r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----"),
    "personal_path": re.compile(r"[A-Z]:[/\\]+Users[/\\]+[^/\\\s]+", re.I),
    "taiwan_identity": re.compile(r"\b[A-Z][12][0-9]{8}\b"),
}


def check_content(name: str, content: bytes, *, denylist: tuple[str, ...] = ()) -> list[str]:
    path = PurePosixPath(name)
    findings = []
    if ".." in path.parts or path.is_absolute() or set(path.parts) & PRIVATE_PARTS:
        findings.append("private_path")
    if "diagnostics" in path.parts and not (
        path.suffix == ".py" and "vghks_sdk/diagnostics/" in path.as_posix()
    ):
        findings.append("private_path")
    if path.suffix.lower() in BLOCKED_SUFFIXES or path.name in {
        "_live_defaults.json",
        "_build_info.json",
    }:
        findings.append("private_artifact")
    if path.name.startswith((".env", "credentials", "token")) or ".local." in path.name:
        findings.append("private_configuration")
    try:
        text = unicodedata.normalize("NFKC", content.decode("utf-8-sig"))
    except UnicodeDecodeError:
        return [*findings, "unexpected_binary"]
    for rule, pattern in PATTERNS.items():
        if rule == "private_key" and any(name.endswith(p) for p in TLS_FIXTURES):
            continue  # Public, self-signed localhost-only fixture, documented beside it.
        if pattern.search(text):
            findings.append(rule)
    folded = text.casefold()
    if any(
        value and unicodedata.normalize("NFKC", value).casefold() in folded for value in denylist
    ):
        findings.append("local_sensitive_value")
    return findings


def candidates(staged: bool):
    if staged:
        names = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).decode().split("\0")
        for name in filter(None, names):
            path = PurePosixPath(name)
            if path.parts[0] not in PUBLIC_DIRS and name not in PUBLIC_FILES:
                yield name, b"", ["unapproved_root"]
            else:
                yield name, subprocess.check_output(["git", "show", f":{name}"], cwd=ROOT), []
    else:
        for root in sorted(PUBLIC_DIRS | PUBLIC_FILES):
            path = ROOT / root
            paths = path.rglob("*") if path.is_dir() else [path]
            for file in paths:
                if not file.is_file() or "__pycache__" in file.parts:
                    continue
                name = file.relative_to(ROOT).as_posix()
                yield name, file.read_bytes(), ["symlink"] if file.is_symlink() else []


def archive_members(path: Path):
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                if not info.is_dir():
                    yield info.filename, archive.read(info), []
    else:
        with tarfile.open(path) as archive:
            for info in archive.getmembers():
                if info.issym() or info.islnk():
                    yield info.name, b"", ["symlink"]
                elif info.isfile():
                    stream = archive.extractfile(info)
                    assert stream is not None
                    yield info.name, stream.read(), []


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--staged", action="store_true")
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--denylist", type=Path, help="local JSON array of private values")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    private = tuple(json.loads(args.denylist.read_text(encoding="utf-8"))) if args.denylist else ()
    rows, count = [], 0
    members = archive_members(args.archive) if args.archive else candidates(args.staged)
    for name, content, prior in members:
        count += 1
        issues = [*prior, *check_content(name, content, denylist=private)]
        if issues:
            rows.append({"path": name, "rules": sorted(set(issues))})
    report = {"checked_files": count, "status": "FAIL" if rows else "OK", "findings": rows}
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    print(json.dumps(report, ensure_ascii=False))
    return int(bool(rows))


if __name__ == "__main__":
    raise SystemExit(main())
