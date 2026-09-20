"""Crash-tolerant lifecycle and return packaging for unredacted live tests."""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import stat
import sys
import traceback
import uuid
import zipfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

from ..core.errors import ConfigurationError, SDKError, error_info
from ..local_io import write_json_atomic

OWNER_MARKER = ".vghks-live-test"
INCOMPLETE_MARKER = ".incomplete"
COMPLETE_MARKER = ".complete"


@dataclass(frozen=True, slots=True)
class LiveTestArchive:
    run_id: str
    run_directory: Path
    archive_path: Path


class LiveTestBundleManager:
    """Own one run directory from pre-SDK bootstrap through ZIP creation."""

    def __init__(
        self,
        run_directory: Path,
        *,
        run_id: str | None = None,
        archive_directory: Path | None = None,
        overwrite: bool = False,
    ) -> None:
        self.run_directory = _safe_run_directory(run_directory)
        self.run_id = run_id or _new_run_id()
        self.archive_directory = (
            archive_directory.expanduser().resolve()
            if archive_directory is not None
            else _default_archive_directory(self.run_directory)
        )
        self._console: TextIO | None = None
        self._finalized = False
        self._prepare(overwrite=overwrite)
        self.parsed_directory = self._make_directory("parsed")
        self.diagnostics_directory = self._make_directory("diagnostics")
        self.requests_directory = self._make_directory("requests")
        self.responses_directory = self._make_directory("responses")
        self.console_path = self.run_directory / "console.log"
        self._console = self.console_path.open("a", encoding="utf-8", newline="\n")
        _restrict(self.console_path)

    @classmethod
    def create_auto(
        cls,
        output_root: Path,
        *,
        run_id: str | None = None,
        archive_directory: Path | None = None,
    ) -> LiveTestBundleManager:
        resolved_id = run_id or _new_run_id()
        root = output_root.expanduser().resolve()
        return cls(
            root / "runs" / resolved_id,
            run_id=resolved_id,
            archive_directory=archive_directory
            if archive_directory is not None
            else root / "archives",
        )

    @classmethod
    def attach_incomplete(
        cls, run_directory: Path, *, archive_directory: Path | None = None
    ) -> LiveTestBundleManager:
        """Attach without creating or deleting files; used only for offline recovery."""

        self = cls.__new__(cls)
        self.run_directory = _safe_run_directory(run_directory)
        if not (self.run_directory / OWNER_MARKER).is_file():
            raise ConfigurationError("directory was not created by VGHKS live-test")
        if not (self.run_directory / INCOMPLETE_MARKER).is_file():
            raise ConfigurationError("live-test directory is not marked incomplete")
        marker = _read_json_file(self.run_directory / INCOMPLETE_MARKER)
        self.run_id = str(marker.get("run_id") or self.run_directory.name)
        self.archive_directory = (
            archive_directory.expanduser().resolve()
            if archive_directory is not None
            else _default_archive_directory(self.run_directory)
        )
        self.parsed_directory = self.run_directory / "parsed"
        self.diagnostics_directory = self.run_directory / "diagnostics"
        self.requests_directory = self.run_directory / "requests"
        self.responses_directory = self.run_directory / "responses"
        for directory in (
            self.parsed_directory,
            self.diagnostics_directory,
            self.requests_directory,
            self.responses_directory,
        ):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            _restrict(directory, directory=True)
        capture_manifest = self.run_directory / "capture_manifest.jsonl"
        capture_manifest.touch(exist_ok=True)
        _restrict(capture_manifest)
        errors_path = self.run_directory / "errors.jsonl"
        errors_path.touch(exist_ok=True)
        _restrict(errors_path)
        config_path = self.run_directory / "run_config.json"
        if not config_path.is_file():
            write_json_atomic(
                config_path,
                {"schema_version": 3, "recovered_from_incomplete_run": True},
            )
        diagnostic_summary = self.diagnostics_directory / "summary.json"
        if not diagnostic_summary.is_file():
            write_json_atomic(
                diagnostic_summary,
                {
                    "schema_version": 2,
                    "run_id": self.run_id,
                    "status": "INTERRUPTED",
                    "contains_raw_request_or_response": False,
                    "recovered_from_incomplete_run": True,
                },
            )
        environment_path = self.run_directory / "environment.json"
        if not environment_path.is_file():
            from .environment import environment_report

            write_json_atomic(
                environment_path,
                environment_report(self.run_directory),
            )
        self.console_path = self.run_directory / "console.log"
        self._console = self.console_path.open("a", encoding="utf-8", newline="\n")
        self._finalized = False
        return self

    def log(self, message: str, *, error: bool = False, echo: bool = True) -> None:
        """Write credential-free operator text to both terminal and console.log."""

        line = str(message).replace("\r", "")
        if self._console is not None and not self._console.closed:
            self._console.write(line + "\n")
            self._console.flush()
        if echo:
            print(line, file=sys.stderr if error else sys.stdout)

    def write_config(self, payload: Mapping[str, Any]) -> Path:
        return write_json_atomic(self.run_directory / "run_config.json", payload)

    def write_summary(self, payload: Mapping[str, Any]) -> Path:
        return write_json_atomic(self.run_directory / "run_summary.json", payload)

    def finalize(
        self,
        *,
        status: str,
        summary: Mapping[str, Any] | None = None,
    ) -> LiveTestArchive:
        """Always leave a readable summary and a single return ZIP."""

        if self._finalized:
            raise ConfigurationError("live-test bundle was already finalized")
        self._finalized = True
        normalized_status = _safe_status(status)
        packaging_stage = "output"
        try:
            if summary is not None:
                self.write_summary(summary)
            elif not (self.run_directory / "run_summary.json").is_file():
                self.write_summary(
                    {
                        "schema_version": 6,
                        "run_id": self.run_id,
                        "status": normalized_status,
                        "warning": _sensitive_warning(),
                    }
                )
            self._write_return_readme(normalized_status)
            (self.run_directory / COMPLETE_MARKER).write_text(
                json.dumps(
                    {
                        "run_id": self.run_id,
                        "status": normalized_status,
                        "completed_at": _utc_now(),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            _restrict(self.run_directory / COMPLETE_MARKER)
            (self.run_directory / INCOMPLETE_MARKER).unlink(missing_ok=True)
            self.close_console()
            packaging_stage = "archive"
            archive = self._create_archive(normalized_status)
            _restrict_tree(self.run_directory)
        except Exception as exc:
            packaging_error = _packaging_error(exc, stage=packaging_stage)
            self._preserve_packaging_failure(packaging_error)
            raise packaging_error from exc
        return archive

    def _preserve_packaging_failure(self, error: SDKError) -> None:
        """Best-effort recovery that never hides the original packaging failure."""

        with contextlib.suppress(Exception):
            self.close_console()
        with contextlib.suppress(OSError):
            (self.run_directory / COMPLETE_MARKER).unlink(missing_ok=True)
        with contextlib.suppress(Exception):
            write_json_atomic(
                self.run_directory / INCOMPLETE_MARKER,
                {
                    "schema_version": 1,
                    "run_id": self.run_id,
                    "packaging_failed_at": _utc_now(),
                },
            )
        packaging_error_id = _append_packaging_error(self.run_directory, error)
        with contextlib.suppress(Exception):
            write_json_atomic(
                self.run_directory / "run_summary.json",
                {
                    "schema_version": 6,
                    "run_id": self.run_id,
                    "status": "PACKAGING_FAILED",
                    "exit_code": 2,
                    "issue": error_info(error),
                    "error_id": packaging_error_id,
                    "warning": _sensitive_warning(),
                },
            )

    def close_console(self) -> None:
        if self._console is not None and not self._console.closed:
            self._console.flush()
            self._console.close()

    def _prepare(self, *, overwrite: bool) -> None:
        root = self.run_directory
        if root.exists() and not root.is_dir():
            raise ConfigurationError("live-test output path is not a directory")
        if root.exists() and any(root.iterdir()):
            if not overwrite:
                raise ConfigurationError(
                    "live-test output directory is not empty; use --overwrite to replace it"
                )
            if not (root / OWNER_MARKER).is_file():
                raise ConfigurationError(
                    "refusing to overwrite a directory not created by live-test"
                )
            for child in tuple(root.iterdir()):
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child)
                else:
                    child.unlink()
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        _restrict(root, directory=True)
        (root / OWNER_MARKER).write_text("vghks-sdk live-test bundle v3\n", encoding="utf-8")
        _restrict(root / OWNER_MARKER)
        write_json_atomic(
            root / INCOMPLETE_MARKER,
            {
                "schema_version": 1,
                "run_id": self.run_id,
                "started_at": _utc_now(),
            },
        )

    def _make_directory(self, name: str) -> Path:
        path = self.run_directory / name
        path.mkdir(mode=0o700)
        _restrict(path, directory=True)
        return path

    def _write_return_readme(self, status: str) -> None:
        text = f"""VGHKS LIVE TEST RETURN BUNDLE
================================

Run ID: {self.run_id}
Status: {status}

DEBUG FORMAT
------------
Debug information is stored as ordinary readable files and a password-free
ZIP. No encryption or decryption is required. Original HTTP bodies, session
state, and query data are included. Bring back this ZIP only.
Open RESULTS.txt (when present) for the test coverage and failure list.

REVIEW ORDER
------------
1. run_summary.json       Overall status and step/error map.
2. run_config.json        Credential-free effective settings.
3. diagnostics/           Redacted structural request trace.
4. errors.jsonl           Error-to-operation/exchange map without locals.
5. environment.json       Credential-free runtime and EXE environment.
6. parsed/                Complete parsed clinical results.
7. capture_manifest.jsonl Operation-to-HTTP map.
8. requests/, responses/  Exact unredacted HTTP exchange.

Do not replay requests or session state from this bundle.
"""
        path = self.run_directory / "README_RETURN.txt"
        path.write_text(text, encoding="utf-8", newline="\n")
        _restrict(path)

    def _create_archive(self, status: str) -> LiveTestArchive:
        # An existing output directory may also hold the EXE and other user
        # files. Do not change its permissions as part of ZIP creation.
        existing_directory = self.archive_directory.exists()
        self.archive_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not existing_directory:
            _restrict(self.archive_directory, directory=True)
        # Use export time (not the potentially much earlier run start). A new
        # short ID also distinguishes two archives produced in the same second.
        name = f"vghks-live-test-{_new_run_id()}-{status}.zip"
        archive_path = self.archive_directory / name
        if archive_path.exists():
            raise ConfigurationError(
                "live-test archive already exists",
                code="ARCHIVE_ALREADY_EXISTS",
            )
        temporary = archive_path.with_suffix(".zip.tmp")
        try:
            with zipfile.ZipFile(
                temporary,
                mode="x",
                compression=zipfile.ZIP_DEFLATED,
                compresslevel=6,
            ) as archive:
                for directory_name in (
                    "requests",
                    "responses",
                    "parsed",
                    "diagnostics",
                ):
                    directory = self.run_directory / directory_name
                    directory.mkdir(parents=True, exist_ok=True)
                    archive.write(directory, directory_name + "/")
                for path in _iter_bundle_files(self.run_directory):
                    # The EXE/archive directory can be an ancestor of the run.
                    # Excluding everything under it would silently drop all
                    # captured files. Only exclude an archive subtree inside
                    # the run, plus the archive currently being written.
                    if path in {temporary, archive_path}:
                        continue
                    if (
                        self.archive_directory != self.run_directory
                        and _is_within(self.archive_directory, self.run_directory)
                        and _is_within(path, self.archive_directory)
                    ):
                        continue
                    archive.write(path, path.relative_to(self.run_directory).as_posix())
            os.replace(temporary, archive_path)
        except Exception as exc:
            with contextlib.suppress(OSError):
                temporary.unlink(missing_ok=True)
            with contextlib.suppress(OSError):
                archive_path.unlink(missing_ok=True)
            raise ConfigurationError(
                "unable to create live-test return ZIP",
                code="ARCHIVE_CREATE_FAILED",
                operation="live.package",
                app="local",
                cause_type=exc.__class__.__name__,
            ) from exc
        _restrict(archive_path)
        return LiveTestArchive(
            run_id=self.run_id,
            run_directory=self.run_directory,
            archive_path=archive_path,
        )


def find_incomplete_runs(output_root: Path) -> tuple[Path, ...]:
    runs = output_root.expanduser().resolve() / "runs"
    if not runs.is_dir():
        return ()
    return tuple(
        sorted(
            (
                path
                for path in runs.iterdir()
                if path.is_dir()
                and (path / OWNER_MARKER).is_file()
                and (path / INCOMPLETE_MARKER).is_file()
            ),
            key=lambda item: item.name,
        )
    )


def pack_incomplete_run(
    run_directory: Path, *, archive_directory: Path | None = None
) -> LiveTestArchive:
    """Package an interrupted run without creating an SDK or sending a request."""

    manager = LiveTestBundleManager.attach_incomplete(
        run_directory, archive_directory=archive_directory
    )
    manager.log("Packaging an incomplete run offline; no network request will be sent.")
    summary_path = manager.run_directory / "run_summary.json"
    summary = _read_json_file(summary_path) if summary_path.is_file() else {}
    summary.update(
        {
            "schema_version": 6,
            "run_id": manager.run_id,
            "status": "INTERRUPTED",
            "recovered_at": _utc_now(),
            "warning": _sensitive_warning(),
        }
    )
    return manager.finalize(status="INTERRUPTED", summary=summary)


def _iter_bundle_files(root: Path) -> Iterable[Path]:
    return sorted(
        (path for path in root.rglob("*") if path.is_file()),
        key=lambda item: item.relative_to(root).as_posix(),
    )


def _new_run_id() -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{uuid.uuid4().hex[:8]}"


def _default_archive_directory(run_directory: Path) -> Path:
    if run_directory.parent.name.casefold() == "runs":
        return run_directory.parent.parent / "archives"
    return run_directory.parent / "archives"


def _safe_run_directory(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    forbidden = {Path.cwd().resolve(), Path(resolved.anchor)}
    # Frozen/service environments can legitimately have no discoverable home.
    with contextlib.suppress(RuntimeError):
        forbidden.add(Path.home().resolve())
    if resolved in forbidden:
        raise ConfigurationError("live-test run directory is too broad")
    return resolved


def _safe_status(value: str) -> str:
    allowed = {
        "OK",
        "COMPLETED_WITH_ERRORS",
        "COMPLETED_WITH_GAPS",
        "AUTHENTICATION_FAILED",
        "CONNECTIVITY_FAILED",
        "HTTP_FAILED",
        "INTERRUPTED",
        "BOOTSTRAP_FAILED",
        "PACKAGING_FAILED",
    }
    normalized = str(value).strip().upper()
    return normalized if normalized in allowed else "COMPLETED_WITH_ERRORS"


def _read_json_file(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return dict(value) if isinstance(value, dict) else {}


def _is_within(path: Path, directory: Path) -> bool:
    try:
        path.resolve().relative_to(directory.resolve())
    except ValueError:
        return False
    return True


def _restrict(path: Path, *, directory: bool = False) -> None:
    mode = stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR if directory else stat.S_IRUSR | stat.S_IWUSR
    with contextlib.suppress(OSError):
        os.chmod(path, mode)


def _restrict_tree(root: Path) -> None:
    _restrict(root, directory=True)
    for path in root.rglob("*"):
        _restrict(path, directory=path.is_dir())


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sensitive_warning() -> str:
    return (
        "UNREDACTED: archive may contain a password, replayable session state, "
        "tokens, HTTP bodies, patient data, and SOAP. ZIP is not encrypted."
    )


def _append_packaging_error(root: Path, error: BaseException) -> str:
    """Append a safe offline-packaging error when the raw recorder is closed."""

    path = root / "errors.jsonl"
    existing_count = 0
    try:
        if path.is_file():
            with path.open("r", encoding="utf-8") as handle:
                existing_count = sum(1 for line in handle if line.strip())
    except (OSError, UnicodeError):
        existing_count = 0
    error_id = f"err-{existing_count + 1:06d}"
    info = error_info(error)
    frames = [
        {
            "file": os.path.basename(frame.filename),
            "line": frame.lineno,
            "function": frame.name,
        }
        for frame in traceback.extract_tb(error.__traceback__)
    ]
    row = {
        "schema_version": 1,
        "error_id": error_id,
        "captured_at": _utc_now(),
        "step": "packaging",
        "sdk_operation_id": "",
        "sdk_operation": "live.package",
        "app_key": "local",
        "linked_capture_id": None,
        "issue": {
            "code": info.code,
            "category": info.category,
            "operation": info.operation or "live.package",
            "app": info.app or "local",
            "endpoint_path": info.endpoint_path,
            "http_status": info.http_status,
            "attempt": info.attempt,
            "cause_type": info.cause_type,
        },
        "exception_chain": [error.__class__.__name__],
        "stack_frames": frames,
        "locals_included": False,
    }
    try:
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        _restrict(path)
    except (OSError, UnicodeError):
        return ""
    return error_id


def _packaging_error(exc: Exception, *, stage: str) -> SDKError:
    if isinstance(exc, SDKError):
        return exc.with_context(operation="live.package", app="local")
    code = "ARCHIVE_CREATE_FAILED" if stage == "archive" else "OUTPUT_WRITE_FAILED"
    message = (
        "unable to create the live-test return archive"
        if stage == "archive"
        else "unable to write the live-test return bundle"
    )
    return ConfigurationError(
        message,
        code=code,
        operation="live.package",
        app="local",
        cause_type=exc.__class__.__name__,
    )
