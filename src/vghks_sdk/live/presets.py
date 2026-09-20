"""Credential-free default for the current combined intranet validation round."""


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
