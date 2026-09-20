"""Read return bundles in place, without extracting ZIP members."""

from __future__ import annotations

import json
import stat
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

from ..core.errors import ConfigurationError

MAX_FILE_BYTES = 128 * 1024 * 1024
MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024


def _invalid(code: str) -> ConfigurationError:
    return ConfigurationError("return bundle could not be read", code=code, app="local")


def safe_member(value: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or ":" in value or "\x00" in value:
        raise _invalid("BUNDLE_PATH_INVALID")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in value.split("/")):
        raise _invalid("BUNDLE_PATH_INVALID")
    return path.as_posix()


class BundleReader:
    """Bounded ZIP/directory reader; file checksums are neither needed nor read."""

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()
        self.archive: zipfile.ZipFile | None = None
        self.names: set[str] = set()
        try:
            if self.path.is_dir():
                self._scan_directory()
            elif self.path.is_file() and self.path.suffix.lower() == ".zip":
                self.archive = zipfile.ZipFile(self.path)
                seen: set[str] = set()
                total = 0
                for info in self.archive.infolist():
                    name = safe_member(info.filename.rstrip("/"))
                    if name.casefold() in seen:
                        raise _invalid("BUNDLE_DUPLICATE_PATH")
                    seen.add(name.casefold())
                    if stat.S_ISLNK(info.external_attr >> 16):
                        raise _invalid("BUNDLE_SYMLINK")
                    if info.is_dir():
                        continue
                    if info.file_size > MAX_FILE_BYTES:
                        raise _invalid("BUNDLE_FILE_TOO_LARGE")
                    total += info.file_size
                    self.names.add(name)
                if total > MAX_TOTAL_BYTES or len(seen) > 50000:
                    raise _invalid("BUNDLE_TOO_LARGE")
            else:
                raise _invalid("BUNDLE_INPUT_INVALID")
            self._validate_required_files()
        except BaseException as exc:
            self.close()
            if isinstance(exc, (OSError, ValueError, IndexError, zipfile.BadZipFile)):
                raise _invalid("BUNDLE_INPUT_INVALID") from exc
            raise

    def _scan_directory(self) -> None:
        total = 0
        seen: set[str] = set()
        for path in self.path.rglob("*"):
            if path.is_symlink() or not path.resolve().is_relative_to(self.path):
                raise _invalid("BUNDLE_SYMLINK")
            if not path.is_file():
                continue
            name = safe_member(path.relative_to(self.path).as_posix())
            if name.casefold() in seen:
                raise _invalid("BUNDLE_DUPLICATE_PATH")
            seen.add(name.casefold())
            size = path.stat().st_size
            if size > MAX_FILE_BYTES:
                raise _invalid("BUNDLE_FILE_TOO_LARGE")
            total += size
            self.names.add(name)
        if total > MAX_TOTAL_BYTES or len(seen) > 50000:
            raise _invalid("BUNDLE_TOO_LARGE")

    def open(self, name: str) -> BinaryIO:
        name = safe_member(name)
        if name not in self.names:
            raise _invalid("BUNDLE_FILE_MISSING")
        if self.archive is not None:
            return self.archive.open(name)  # type: ignore[return-value]
        path = self.path / name
        if path.is_symlink() or not path.resolve().is_relative_to(self.path):
            raise _invalid("BUNDLE_PATH_INVALID")
        return path.open("rb")

    def read(self, name: str) -> bytes:
        with self.open(name) as handle:
            value = handle.read(MAX_FILE_BYTES + 1)
        if len(value) > MAX_FILE_BYTES:
            raise _invalid("BUNDLE_FILE_TOO_LARGE")
        return value

    def json(self, name: str) -> Any:
        try:
            return json.loads(self.read(name).decode("utf-8-sig"))
        except (ValueError, UnicodeError) as exc:
            raise _invalid("BUNDLE_JSON_INVALID") from exc

    def jsonl(self, name: str) -> list[dict[str, Any]]:
        if name not in self.names:
            return []
        try:
            rows = [
                json.loads(line)
                for line in self.read(name).decode("utf-8-sig").splitlines()
                if line.strip()
            ]
            if not all(isinstance(row, dict) for row in rows):
                raise ValueError
            return rows
        except (ValueError, UnicodeError) as exc:
            raise _invalid("BUNDLE_JSONL_INVALID") from exc

    def _validate_required_files(self) -> None:
        for required in ("run_summary.json", "capture_manifest.jsonl"):
            if required not in self.names:
                raise _invalid("BUNDLE_FILE_MISSING")
        summary = self.json("run_summary.json")
        if not isinstance(summary, dict) or summary.get("schema_version") not in {3, 4, 5, 6}:
            raise _invalid("BUNDLE_SUMMARY_UNSUPPORTED")

    def close(self) -> None:
        if self.archive is not None:
            self.archive.close()

    def __enter__(self) -> BundleReader:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
