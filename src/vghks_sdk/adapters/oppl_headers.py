"""Browser AJAX request context observed in the OPPL HAR."""

from urllib.parse import urlsplit


def oppl_ajax_headers(base_url: str) -> dict[str, str]:
    base = base_url.rstrip("/")
    url = urlsplit(base)
    return {
        "X-Requested-With": "XMLHttpRequest",
        "Origin": f"{url.scheme}://{url.netloc}",
        "Referer": f"{base}/surgAction.do?method=surg",
    }
