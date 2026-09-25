"""Credential-free defaults for bounded intranet validation rounds."""


REGRESSION_QUERIES = (
    "webmaas.basic_info",
    "prq.visit_cases",
    "prq.soap",
    "prq.numeric",
    "prq.numeric_history",
    "oppl.surgery_schedule",
)


def login_test_round() -> dict:
    return {
        "profile": "login",
        "login_negative_attempts": 2,
        "weekly_opd_soap": False,
        "include_earnings": False,
        "download_assets": False,
    }


def visit_search_round() -> dict:
    return {
        "profile": "visits",
        "weekly_opd_soap": False,
        "include_earnings": False,
        "include_surgery": False,
        "include_unsigned": False,
        "download_assets": False,
        "max_cases": 3,
        "visit_filter": {"all_sections": True, "section_name_contains": []},
    }


def soap_test_round() -> dict:
    return {
        "profile": "soap",
        "download_assets": False,
        "include_earnings": False,
        "weekly_opd_soap": False,
        "max_cases": 8,
        "max_items": 2,
    }


def regression_round() -> dict:
    """Focused read-only checks for visit-index, numeric and surgery changes."""
    return {
        "profile": "regression",
        "weekly_opd_soap": False,
        "include_earnings": False,
        "download_assets": False,
        "max_cases": 2,
        "max_items": 2,
        "only_operations": list(REGRESSION_QUERIES),
    }


def combined_round() -> dict:
    return {
        "profile": "comprehensive",
        "weekly_opd_soap": False,
        "include_earnings": True,
        "download_assets": True,
        "max_cases": 6,
        "max_items": 8,
        "surgery_query": {
            "procedure_code": "80416",
            "department": "ALL",
            "periods": ["24M", "2YB"],
        },
        "review_query": {},
    }
