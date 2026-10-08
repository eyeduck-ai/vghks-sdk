"""Verify saved order attachments without extracting a ZIP or interpreting PHI."""

from __future__ import annotations

import re
from typing import Any

from ..contracts.order_assets import ORDER_ASSET_FORMATS, ORDER_ASSET_STAGE
from ..contracts.order_assets import ORDER_WORKFLOW_PATH as ORDER_WORKFLOW
from ..core.errors import ConfigurationError, ParseError
from ..parsing.assets import parse_binary_asset
from .bundle import BundleReader

_ASSETS = ORDER_WORKFLOW + ORDER_ASSET_STAGE + "/"


def verify_order_assets(reader: BundleReader, steps: list[dict] | None = None) -> dict[str, Any]:
    """Check each successful saved query once, including pre-hash audit formats.

    Outcome paths must identify the sibling PDF/JPG and metadata JSON. Only
    counts and fixed issue codes leave this helper; references and content do
    not enter the analysis report. Service/live statuses remain original.
    """
    result: dict[str, Any] = {
        "status": "NO_ASSETS", "checked_files": 0, "pdf_files": 0, "jpg_files": 0,
        "total_bytes": 0, "issues": [],
        "scope": "Saved files match download metadata and PDF/JPEG boundary validation; no OCR or clinical interpretation.",
    }
    expected = {}
    for name in reader.names:
        for operation, (_, suffix) in ORDER_ASSET_FORMATS.items():
            if (name.startswith(_ASSETS)
                    and name.endswith("-" + operation.removeprefix("prq.") + suffix)):
                expected[name.rsplit(".", 1)[0] + ".outcome.json"] = operation
    for step in steps or []:
        operation, output = step.get("operation"), step.get("output")
        if (step.get("status") == "OK" and isinstance(operation, str) and operation in ORDER_ASSET_FORMATS
                and isinstance(output, str) and output.startswith(_ASSETS)
                and output.endswith("-" + operation.removeprefix("prq.") + ".json")):
            expected[output.removesuffix(".json") + ".outcome.json"] = operation
    for name in sorted(expected.keys() - reader.names):
        result["checked_files"] += 1
        result["issues"].append({"operation": expected[name], "code": "ORDER_ASSET_OUTCOME_MISSING"})
    for name in sorted(reader.names):
        if not name.startswith(_ASSETS) or not name.endswith(".outcome.json"):
            continue
        try:
            audit = reader.json(name)
        except ConfigurationError:
            audit = None
        if not isinstance(audit, dict):
            result["issues"].append({"operation": "", "code": "ORDER_ASSET_OUTCOME_INVALID"})
            continue
        operation = audit.get("operation")
        if audit.get("status") != "OK":
            continue
        if not isinstance(operation, str) or operation not in ORDER_ASSET_FORMATS:
            result["issues"].append({"operation": "", "code": "ORDER_ASSET_OUTCOME_INVALID"})
            continue
        result["checked_files"] += 1
        code = _verify_asset(reader, name, audit, operation)
        if code:
            result["issues"].append({"operation": operation, "code": code})
        else:
            result["pdf_files" if operation == "prq.pdf_attachment" else "jpg_files"] += 1
            result["total_bytes"] += audit["byte_count"]
    result["status"] = "ERROR" if result["issues"] else "OK" if result["checked_files"] else "NO_ASSETS"
    return result


def _verify_asset(reader: BundleReader, name: str, audit: dict, operation: str) -> str:
    media_type, suffix = ORDER_ASSET_FORMATS[operation]
    expected_asset = name.removesuffix(".outcome.json") + suffix
    relative = expected_asset.removeprefix(ORDER_WORKFLOW)
    if audit.get("asset") != relative:
        return "ORDER_ASSET_PATH_INVALID"
    if expected_asset not in reader.names:
        return "ORDER_ASSET_MISSING"
    metadata_path = name.removesuffix(".outcome.json") + ".json"
    if metadata_path not in reader.names:
        return "ORDER_ASSET_METADATA_MISSING"
    try:
        metadata = reader.json(metadata_path)
    except ConfigurationError:
        return "ORDER_ASSET_METADATA_INVALID"
    if not isinstance(metadata, dict):
        return "ORDER_ASSET_METADATA_INVALID"
    size, digest = metadata.get("size"), metadata.get("sha256")
    if (type(size) is not int or size < 1 or metadata.get("media_type") != media_type
            or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", digest)
            or type(audit.get("byte_count")) is not int or audit["byte_count"] != size
            or ("sha256" in audit and (not isinstance(audit["sha256"], str)
                                       or audit["sha256"].casefold() != digest.casefold()))):
        return "ORDER_ASSET_METADATA_INVALID"
    try:
        content = reader.read(expected_asset)
    except ConfigurationError:
        return "ORDER_ASSET_READ_FAILED"
    if len(content) != size:
        return "ORDER_ASSET_SIZE_MISMATCH"
    try:
        asset = parse_binary_asset(content, media_type=media_type)
    except ParseError:
        return "ORDER_ASSET_FORMAT_INVALID"
    if asset.sha256 != digest.casefold():
        return "ORDER_ASSET_HASH_MISMATCH"
    return ""
