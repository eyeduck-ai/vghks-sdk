"""Shared binary validation for live requests and offline replay."""

from ..core.errors import ParseError
from ..models import BinaryAsset


def binary_asset_media_type(content: bytes) -> str:
    """Identify supported binary prefixes, without proving file completeness.

    MIME headers alone cannot distinguish an attachment from a returned login
    page. Truncated files still belong in the binary validator, not an HTML
    parser; callers must retain URL/HTTP authentication checks separately.
    """
    if content.startswith(b"%PDF"):
        return "application/pdf"
    if content.startswith(b"\xff\xd8"):
        return "image/jpeg"
    return ""


def parse_binary_asset(content: bytes, *, media_type: str) -> BinaryAsset:
    if len(content) > 64 * 1024 * 1024:
        raise ParseError("asset exceeded the supported size", code="ASSET_TOO_LARGE")
    if media_type == "image/jpeg":
        if (
            len(content) < 4
            or not content.startswith(b"\xff\xd8")
            or not content.endswith(b"\xff\xd9")
        ):
            raise ParseError("PACS response was not a complete JPEG", code="PACS_JPEG_INVALID")
    elif media_type == "application/pdf":
        # Recorded scanner PDFs append this exact producer marker after EOF.
        # Preserve original bytes; do not accept arbitrary trailing content.
        end = content.rstrip()
        if end.endswith(b"%%EOF\r%Avision"):
            end = end.removesuffix(b"\r%Avision")
        if not content.startswith(b"%PDF") or not end.endswith(b"%%EOF"):
            raise ParseError(
                "PDF response did not contain a complete PDF", code="PDF_BINARY_INVALID"
            )
    else:
        raise ParseError("unsupported asset type", code="ASSET_TYPE_INVALID")
    return BinaryAsset(content=content, media_type=media_type)
