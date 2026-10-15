"""Every catalog result has a usable production replay path, with synthetic bodies."""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from vghks_sdk.offline.replay import replay_response
from vghks_sdk.queries import QUERY_SPECS

MRN = "SYN001"
PARAMS = {
    "hhisnum": MRN,
    "patno": MRN,
    "caseDT": "2026-01-02",
    "caseNo": "C1",
    "caseType": "O",
    "caseSec": "70",
    "seqNo": "1",
    "reqno": "R1",
    "seqno": "1",
    "ApplySeq": "1",
    "dept": "CHK",
    "value(qryType)": "qryProcess",
    "value(begDate)": "2026-01-01",
    "value(endDate)": "2026-01-02",
}


def samples():
    numeric = (
        "<table><tr><th>日期</th><th>OD</th><th>OS</th></tr>"
        "<tr><td>2026-01-02</td><td>1</td><td>2</td></tr></table>"
    )
    labelled = "<table><tr><th>項目</th><td>Synthetic</td></tr></table>"
    html = {
        "attendance.status": """<form name="pSPDForm" method="post" action="/PSPDPortal/oFSchedule.do">
            <input name="reqCode" value="setPCClockInLog">
            <fieldset><legend>[999-合成單位　合成員工(TEST)]</legend>
            <input type="submit" value="請按我">
            上次電腦簽到退記錄:2026-01-01 08:00 地點:TESTROOM<br>電腦序號:TESTPC
            </fieldset></form>""",
        "attendance.records": """<form name="pSPDForm" method="post" action="/PSPDPortal/oFSchedule.do">
            <fieldset><legend>[<span id="spanUsrName">合成員工</span>出勤簽到退查詢]</legend>
            <input name="reqCode" value="getProcessedFingerLog">
            <input name="value(begDate)" value="2026-01-01">
            <input name="value(endDate)" value="2026-01-02">
            <input name="b1" value="查詢" type="submit">
            <input type="radio" name="value(qryType)" value="qryProcess">
            <input type="radio" name="value(qryType)" value="qryFinMachine">
            <span id="empname">合成員工</span><table id="drlistTb">
            <tr id="head"><th>日期時間</th><th>說明</th></tr>
            <tr id="tfoot"><td colspan="2">查詢結果：共有~ 0 ~筆資料</td></tr>
            </table></fieldset></form>""",
        "webmaas.basic_info": f"""<div id="DETAIL">
            <div class="form-group"><label>病歷號</label><span class="label label-orange">{MRN}</span></div>
            <div class="form-group"><label>姓名</label><span class="label label-orange">合成病人</span></div></div>""",
        "webmaas.registration_query": '<table id="row"><tr><th>病歷號</th></tr></table>',
        "prq.visit_cases": '<div id="typeO"></div>',
        "prq.case_detail": '<div id="tabs"></div>',
        "prq.soap": '<div id="data"><div class="soap"><pre>S: Synthetic</pre></div></div>',
        "prq.numeric": numeric,
        "prq.case_orders": '<div id="orderList"></div>',
        "prq.order_history": '<div id="orderList"></div>',
        "prq.case_medications": '<div id="medicationList"></div>',
        "prq.medication_history": '<div id="medicationList"></div>',
        "prq.numeric_history": numeric,
        "prq.surgery_history": "<table><tr>" + "<th>Synthetic</th>" * 8 + "</tr></table>",
        "prq.consults": '<div id="clorderList"><div id="orwordsDiv"></div></div>',
        "prq.treatments": '<div id="orderList"><div id="agrfileDiv"></div></div>',
        "prq.order_detail": labelled,
        "prq.order_report": labelled,
        "prq.text_report": labelled,
        "prq.pacs_study": "查無資料!!",
        "prq.opd_patients": "查無門診病患清單",
        "prq.text_report_history": '<div id="height_bd"></div>',
        "prq.upload_history": '<table id="tbObj"></table>',
        "oppl.schedule_form": '<form id="openForm"><input name="orcaseno" value="C1"></form>',
        "audit.unsigned_records": '<table id="dataTbl"><tr><th>Synthetic</th></tr></table>',
        "personnel.options": """<form method="post" action="/DDPortal/dRDoctor.do">
            <input name="reqCode" value="showAllDoctors"><input name="value(source)" value="DR">
            <input name="value(name)"><input name="value(usrId)">
            <input name="value(subOU)" value="Y">
            <select name="value(title)"><option value="1">Synthetic</option></select>
            <select name="value(costId)"><option value="70">Synthetic</option></select></form>""",
        "personnel.search": """<table id="drlistTb"><tr>
            <th>姓名</th><th>醫師章號</th><th>醫師類別</th><th>單位</th>
            <th>院內分機</th><th>電話一</th><th>電話二</th><th>電話三</th></tr>
            <tr id="tfoot"><td colspan="8">查詢結果：共有~ 0 ~筆資料</td></tr></table>""",
    }
    payloads = {
        "webmaas.demographics": {"patno": MRN, "patname": "Synthetic"},
        "prq.allergy": {"hhisnum": MRN, "allergy": False, "allergyMsg": ""},
        "prq.advance_directives": {"hhisnum": MRN, "adSign": "", "adSignMsg": ""},
        "prq.research_flags": {"hhisnum": MRN, "irbtype": ""},
        "prq.bed_transfers": {"hhisnum": MRN, "frontBed": "", "nextBed": ""},
        "prq.care_cases": {"status": "", "caseList": []},
        "prq.upload_types": [],
        "oppl.surgery_schedule": {"surgs": []},
        "oppl.patient_info": {"patient": {"hhisnum": MRN}, "surgs": []},
        "oppl.patient_consents": {"patient": {"hhisnum": MRN}, "surgs": [], "consents": []},
        "oppl.request_numbers": {"reqs": []},
        "oppl.anticoagulants": {"status": ""},
        "oppl.sglt2": {"status": ""},
        "oppl.procedure_catalog": {"pfiles": [], "chargenos": []},
        "oppl.holidays": {"holidays": [], "maxDate": ""},
        "oppl.consent_catalog": {"forms": {}, "status": ""},
        "oppl.consent_doctor": {"upuser": {}, "status": ""},
        "oppl.consent_template": {"status": "", "diseasename": "", "opname1": ""},
        "oppl_records.departments": {"deptlist": []},
        "oppl_records.cases": {"oprmlist": []},
        "oppl_records.note": {"rtnYN": "N"},
        "review.login_info": {"UserNMC": "Synthetic", "LoginDateTime": "", "LastLoginDateTime": ""},
        "review.options": {"InsuSectNo": [], "VSDrID": [], "VerifyCode": []},
        "review.doctors": [],
        "review.cases": {"Data": [], "Total": 0},
        "review.case_detail": {"ApplySeq": "1"},
        "review.orders": {"Data": [], "Total": 0},
        "review.attachments": {"Data": [], "Total": 0},
        "review.pacs": {"Data": [], "Total": 0},
    }
    pdf = b"%PDF-1.4\n1 0 obj <<>> endobj\n%%EOF\n"
    return {
        **{key: text.encode("utf-8") for key, text in html.items()},
        **{key: json.dumps(value).encode("utf-8") for key, value in payloads.items()},
        "prq.pdf_attachment": pdf,
        "oppl_records.pdf": pdf,
        "prq.pacs_image": b"\xff\xd8SYNTHETIC\xff\xd9",
    }


class QueryReplayContractTests(unittest.TestCase):
    def test_every_registered_query_replays_a_synthetic_result_without_sockets(self):
        evidence = samples()
        self.assertEqual(set(evidence), {spec.key for spec in QUERY_SPECS})
        with patch("socket.socket.connect", side_effect=AssertionError("offline only")):
            for key, body in evidence.items():
                with self.subTest(query=key):
                    result = replay_response(
                        key, body, PARAMS, context_mrn=MRN, mime="text/plain; charset=utf-8"
                    )
                    self.assertIn(result["status"], {"PARSED", "EMPTY"}, result)
                    self.assertEqual(result["error_code"], "")
