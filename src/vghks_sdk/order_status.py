"""Conservative order execution policy over already parsed status labels."""

import re
import unicodedata


def classify_order_execution(status: str) -> str:
    """Keep unknown states queryable; a button is never proof of report data."""
    value = unicodedata.normalize("NFKC", status).strip()
    if re.match(r"^未執行(?:\s|\(|$)", value):
        return "NOT_EXECUTED"
    if re.match(r"^完成(?:\s|\(|$)", value):
        return "COMPLETED"
    return "UNKNOWN"
