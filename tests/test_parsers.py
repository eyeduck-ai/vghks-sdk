import unittest
from datetime import date

from vghks_sdk.core.errors import NotFoundError
from vghks_sdk.models import VisitCase, VisitFilter, to_jsonable
from vghks_sdk.parsing.audit import parse_unsigned_records
from vghks_sdk.parsing.oppl import parse_surgery_records
from vghks_sdk.parsing.prq import (
    parse_numeric_report,
    parse_opd_patients,
    parse_soap,
    parse_visit_cases,
)
from vghks_sdk.parsing.webmaas import parse_patient_demographics


def _case() -> VisitCase:
    return VisitCase(
        mrn="00000000",
        visit_date=date(2026, 1, 2),
        case_type="O",
        case_no="70000102",
        section_code="70",
        section_name="眼科上午",
        index=0,
    )


class ParserTests(unittest.TestCase):
    def test_patient_demographic_json_mislabeled_as_html(self) -> None:
        payload = [
            {
                "patno": "00000000",
                "patname": "測試病人",
                "hphonno": "0900000000",
                "homephon": "070000000",
                "birthday": "2000-01-01",
                "hsex": "F",
                "address": "測試地址",
                "patid": "SHOULD_NOT_BE_COPIED_TO_EXTRA",
            }
        ]
        result = parse_patient_demographics(payload, "00000000")
        self.assertEqual(result.mrn, "00000000")
        self.assertEqual(result.preferred_phone, "0900000000")
        self.assertTrue(not result.extra or "patid" not in result.extra)

    def test_patient_demographic_rejects_a_missing_patient(self) -> None:
        with self.assertRaises(NotFoundError):
            parse_patient_demographics([], "00000000")

    def test_opd_javascript_is_parsed_without_execution_and_deduplicated(self) -> None:
        html = """
        <script>
          var aryOpdSec = new Array(); var aryOpdRoom = new Array(); var aryOpdDoc = new Array();
          aryOpdSec[0] = '70'; aryOpdRoom[0] = '01'; aryOpdDoc[0] = 'D001';
        </script>
        <div id="room0"><table><tr><td>
          <script>
            var labStr = '<span>001</span>';
            var hnamecStr = '測試病人';
            aryCase[0] = new KSCase('', labStr+'003', '00000000', hnamecStr, 'F', '26', 'link');
            aryCase[0] = new KSCase('', labStr+'003', '00000000', hnamecStr, 'F', '26', 'alternate');
          </script>
        </td></tr></table></div>
        """
        result = parse_opd_patients(html, visit_date=date(2026, 1, 2), doctor_card="D001")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].mrn, "00000000")
        self.assertEqual(result[0].name, "測試病人")
        self.assertEqual(result[0].section_code, "70")

    def test_opd_registration_sequence_preserves_zeros_and_distinct_bookings(self) -> None:
        html = """
        <script>aryOpdSec[0]='70';aryOpdRoom[0]='01';aryOpdDoc[0]='D001';</script>
        <script>
          var labStr = '<span data-mrn="99999999"><img src="icon2.gif"></span>';
          if (aryOpdSec[0] == '70') {
            new KSCase('', labStr+'003 複診', 'TEST001', 'Patient', 'F', '30');
            new KSCase('', '003 複診', 'TEST001', 'Patient', 'F', '30');
            new KSCase('', '004', 'TEST001', 'Patient', 'F', '30');
            new KSCase('', '待確認', 'TEST002', 'Patient', 'M', '40');
          }
        </script>
        """
        result = parse_opd_patients(html, visit_date=date(2026, 1, 2), doctor_card="D001")
        self.assertEqual([(row.mrn, row.sequence_no) for row in result], [
            ("TEST001", "003"), ("TEST001", "004"), ("TEST002", ""),
        ])
        self.assertEqual(to_jsonable(result)[0]["sequence_no"], "003")

    def test_visit_case_links_are_deduplicated_and_filterable(self) -> None:
        eye = (
            "/PRQWeb/QueryCaseDetail.do?hid=1A0&index=0&hhisnum=00000000&"
            "caseType=O&caseNo=70000102&caseSec=70&caseDT=2026-01-02&caseSectC=眼科上午"
        )
        duplicate = eye + "&hidno=REDACTED"
        other = (
            "/PRQWeb/QueryCaseDetail.do?hid=1A0&index=1&hhisnum=00000000&"
            "caseType=O&caseNo=60000101&caseSec=60&caseDT=2026-01-01&caseSectC=家醫科"
        )
        html = f"<script>var a='{eye}'; var b='{duplicate}'; var c='{other}';</script>"
        result = parse_visit_cases(html, "00000000")
        self.assertEqual(len(result), 2)
        self.assertTrue(VisitFilter(section_name_contains=("眼科",)).matches(result[0]))
        self.assertIsNotNone(result[0].detail_params)
        self.assertNotIn("hidno", result[0].detail_params or {})

    def test_soap_search_scope_and_numeric_tables(self) -> None:
        case = _case()
        soap_html = """
        <html><script>var hidden='APPLY';</script><div id="data">
          <div class="soap"><pre>S: stable</pre></div>
          <div class="soap"><pre>P: APPLY treatment</pre></div>
        </div></html>
        """
        soap = parse_soap(soap_html, case)
        self.assertEqual(len(soap.blocks), 2)
        self.assertEqual(soap.full_text.count("APPLY"), 1)

        exact = parse_soap(
            '<div id="data"><div class="soap"><pre>\r\n  ＡＰＰＬＹ  \r\n</pre></div></div>',
            case,
        )
        self.assertEqual(exact.blocks, ("\n  ＡＰＰＬＹ  \n",))

        numeric_html = """
        <table class="eTable">
          <tr><th>日期</th><th>IOP</th><th>OD</th><th>OS</th></tr>
          <tr><td>2026-01-02</td><td></td><td>15</td><td>16</td></tr>
        </table>
        """
        report = parse_numeric_report(numeric_html, case)
        self.assertEqual(report.tables[0].title, "IOP")
        self.assertEqual(report.tables[0].rows, (("2026-01-02", "", "15", "16"),))

    def test_surgery_and_unsigned_record_shapes(self) -> None:
        surgery = parse_surgery_records(
            {
                "surgs": [
                    {
                        "orhisnum": "00000000",
                        "orcaseno": "C1",
                        "ordate": "2026-01-02",
                        "oroproom": "R1",
                        "oropnm1": "TEST PROCEDURE",
                        "unknown": "kept",
                    }
                ]
            }
        )
        self.assertEqual(surgery[0].patient_mrn, "00000000")
        self.assertEqual(surgery[0].extra, {"unknown": "kept"})

        unsigned_html = """
        <table id="pgnTbl"><tr><th>病歷號</th><th>狀態</th></tr>
          <tr><td>00000000</td><td>未完成</td></tr></table>
        """
        unsigned = parse_unsigned_records(unsigned_html)
        self.assertEqual(len(unsigned), 1)
        self.assertEqual(unsigned[0].category, "pgnTbl")


if __name__ == "__main__":
    unittest.main()
