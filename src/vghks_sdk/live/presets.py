"""Credential-free default for the current combined intranet validation round."""


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
