"""Synthetic numeric-report layouts, including the recorded eye-table shape."""

from __future__ import annotations

import unittest

from vghks_sdk.parsing.clinical import parse_numeric_tables


class NumericTableTests(unittest.TestCase):
    def test_eye_tables_keep_two_level_headers_aligned_with_values(self) -> None:
        html = "<div id='data'>" + "".join(
            f"""
            <table class="eTable">
              <tr><th rowspan="2">日期</th><th colspan="{span}">{title}</th></tr>
              <tr><th>OD</th><th>OS</th></tr>
              <tr><td>2026-01-02</td><td>{right}</td><td>{left}</td></tr>
            </table>
            """
            for title, span, right, left in (
                ("IOP", 2, "15 mmHg", "16 mmHg"),
                ("Va", 2, "0.8", "0.7"),
                ("散瞳-驗光", 8, "-1.25", "-1.50"),
            )
        ) + "</div>"
        tables = parse_numeric_tables(html)
        self.assertEqual(len(tables), 3)
        for table in tables:
            self.assertEqual(table.header_rows, (("日期", table.title), ("OD", "OS")))
            self.assertEqual(
                table.column_paths,
                (("日期",), (table.title, "OD"), (table.title, "OS")),
            )
            self.assertEqual(len(table.rows[0]), len(table.column_paths))
            self.assertEqual(table.headers, ("日期", table.title, "OD", "OS"))
        self.assertEqual(tables[0].parsing_issues, ())
        self.assertEqual(tables[2].parsing_issues, ("NUMERIC_HEADER_SPAN_MISMATCH",))

    def test_script_rendered_header_rows_remain_distinct(self) -> None:
        html = """
        <table id="resnumTable0">
          <tr><script>document.write('<th>日期</th><th>IOP</th>');</script></tr>
          <tr><script>document.write('<th>日期</th><th>mmHg</th>');</script></tr>
          <tr><script>document.write('<td>2026-01-02</td><td>15</td>');</script></tr>
        </table>
        """
        (table,) = parse_numeric_tables(html)
        self.assertEqual(table.header_rows, (("日期", "IOP"), ("日期", "mmHg")))
        self.assertEqual(table.column_paths, (("日期",), ("IOP", "mmHg")))
        self.assertEqual(table.rows, (("2026-01-02", "15"),))
        self.assertEqual(table.parsing_issues, ())

    def test_manual_text_and_blank_cells_follow_the_table_header_order(self) -> None:
        html = """
        <table class="eTable">
          <tr><th rowspan="2">日期</th><th colspan="7">IOP</th></tr>
          <tr><th>OS</th><th>OD</th></tr>
          <tr><td>2026-09-21</td><td>error</td><td></td></tr>
          <tr><td>2026-07-20</td><td></td><td>(13) mmHg</td></tr>
          <tr><td>2026-06-01</td><td></td><td></td></tr>
        </table>
        """
        (table,) = parse_numeric_tables(html)
        self.assertEqual(
            table.column_paths,
            (("日期",), ("IOP", "OS"), ("IOP", "OD")),
        )
        self.assertEqual(
            table.rows,
            (
                ("2026-09-21", "error", ""),
                ("2026-07-20", "", "(13) mmHg"),
                ("2026-06-01", "", ""),
            ),
        )
        self.assertEqual(table.parsing_issues, ("NUMERIC_HEADER_SPAN_MISMATCH",))

    def test_lab_abnormal_branch_does_not_duplicate_result_cell(self) -> None:
        html = """
        <table id="resnumTable0">
          <tr><th>日期</th><th>CRP</th></tr>
          <tr><th>單位</th><th>mg/dL</th></tr>
          <tr><td>2026-03-16</td><script>
            if ('true' != null && 'true' == 'true') {
              document.write('<td class="abnormal">1.67</td>');
            } else if (unknownRange()) {
              document.write('<td class="abnormal">1.67</td>');
            } else {
              document.write('<td>1.67</td>');
            }
          </script></tr>
        </table>
        """
        for abnormal in ("true", "false"):
            with self.subTest(abnormal=abnormal):
                source = html.replace(
                    "if ('true' != null && 'true' == 'true')",
                    f"if ('{abnormal}' != null && '{abnormal}' == 'true')",
                )
                (table,) = parse_numeric_tables(source)
                self.assertEqual(table.rows, (("2026-03-16", "1.67"),))
                self.assertEqual(table.column_paths, (("日期",), ("CRP", "mg/dL")))
                self.assertEqual(table.parsing_issues, ())

    def test_mismatched_columns_retain_raw_data_without_guessing(self) -> None:
        html = """
        <table id="resnumTable0">
          <tr><th>日期</th><th>檢查</th></tr>
          <tr><td>2026-01-02</td><td>A</td><td>B</td></tr>
        </table>
        """
        (table,) = parse_numeric_tables(html)
        self.assertEqual(table.rows, (("2026-01-02", "A", "B"),))
        self.assertEqual(table.column_paths, ())
        self.assertIn("NUMERIC_HEADER_UNALIGNED", table.parsing_issues)

    def test_inconsistent_data_row_width_does_not_claim_alignment(self) -> None:
        html = """
        <table class="eTable">
          <tr><th>日期</th><th>IOP</th></tr>
          <tr><td>2026-01-02</td><td>15</td></tr>
          <tr><td>2026-01-03</td><td>16</td><td>17</td></tr>
        </table>
        """
        (table,) = parse_numeric_tables(html)
        self.assertEqual(len(table.rows), 2)
        self.assertEqual(table.column_paths, ())
        self.assertIn("NUMERIC_ROW_WIDTH_MISMATCH", table.parsing_issues)

    def test_legacy_unit_row_omits_trailing_empty_cells(self) -> None:
        html = """
        <table id="resnumTable0">
          <tr><th>日期</th><th>Test A</th><th>Test B</th><th>Test C</th></tr>
          <tr><th>單位</th><th>mg/dL</th><th></th></tr>
          <tr><td>2026-01-02</td><td>1</td><td>2</td><td>3</td></tr>
        </table>
        """
        (table,) = parse_numeric_tables(html)
        self.assertEqual(table.header_rows, (("日期", "Test A", "Test B", "Test C"), ("單位", "mg/dL", "")))
        self.assertEqual(
            table.column_paths,
            (("日期",), ("Test A", "mg/dL"), ("Test B",), ("Test C",)),
        )
        self.assertEqual(table.parsing_issues, ())

    def test_short_grouped_header_is_not_padded_as_units(self) -> None:
        html = """
        <table id="resnumTable0">
          <tr><th>日期</th><th>Test A</th><th>Test B</th></tr>
          <tr><th>分組</th><th>值</th></tr>
          <tr><td>2026-01-02</td><td>1</td><td>2</td></tr>
        </table>
        """
        (table,) = parse_numeric_tables(html)
        self.assertEqual(table.column_paths, ())
        self.assertIn("NUMERIC_HEADER_UNALIGNED", table.parsing_issues)


if __name__ == "__main__":
    unittest.main()
