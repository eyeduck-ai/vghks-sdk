import unittest

from vghks_sdk.core.jsliteral import (
    decode_js_string,
    evaluate_expression,
    iter_constructor_calls,
    string_assignments,
)


class JsLiteralTests(unittest.TestCase):
    def test_decode_and_evaluate_safe_string_subset(self) -> None:
        self.assertEqual(decode_js_string(r"'眼科\nA\u0050PLY'"), "眼科\nAPPLY")
        self.assertEqual(
            evaluate_expression("prefix + '-' + '003'", {"prefix": "ABC"}),
            "ABC-003",
        )
        self.assertIsNone(evaluate_expression("danger()", {}))

    def test_constructor_parser_respects_strings_and_nested_parentheses(self) -> None:
        source = "aryCase[0] = new KSCase('', 'a,b', name, wrap('x,y'), 'F', '30');"
        calls = list(iter_constructor_calls(source, "KSCase"))
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            calls[0].arguments,
            ("''", "'a,b'", "name", "wrap('x,y')", "'F'", "'30'"),
        )

    def test_string_assignments_only_reads_allowed_literal_assignments(self) -> None:
        source = "var name='測試'; ignored='secret'; name = name + 'x';"
        self.assertEqual(string_assignments(source, {"name"}), {"name": "測試"})


if __name__ == "__main__":
    unittest.main()
