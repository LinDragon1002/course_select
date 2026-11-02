from decimal import Decimal

from django.test import TestCase

from ..models import (AcademicCatalog, AcademicClassGroup, AcademicDepartment,
                      AcademicProgramType, NtubTimetableEntry,
                      NtubTimetableSnapshot, NtubOfferingSource,
                      OfficialTeachingSyllabus, Teacher, Course, Section, Term)
from ..ntub_official import (_FormFieldParser, OfficialFetchClient, OfficialFetchError,
                              parse_syllabus_html, parse_timetable_html,
                              sync_syllabus)


SYLLABUS_HTML = """
<span id="lblYear">115</span><span id="lblTerm">1</span>
<span id="lblClass">40630B</span><span id="lblC_CourseNo">40635210</span>
<span id="lblC_CName">作業系統</span><span id="lblC_EName">Operating Systems</span>
<span id="lblC_Credit">3.5</span><span id="lblC_Hour">3</span>
<span id="lblC_Teacher">測試教師</span>
<div id="lblC_Target">教學目標</div><div id="lblC_Matter">教學內容</div>
<div id="lblWeek01">第一週進度</div><div id="lblWeek18">第十八週進度</div>
<div id="lblScore">評量方式</div><div id="lblReference">參考資料</div>
"""

REAL_SYLLABUS_HTML = """
<span id="lblYears">115</span><span id="lblTerm">1</span>
<span id="lblOP_Class">40630B</span><span id="lblCos_ID">40037270</span>
<span id="lblCos_Name">生活藝術賞析</span>
<span id="lblCos_Eng">APPRECIATION OF LIFE ART</span>
<span id="lblCos_Credit">2.0</span><span id="lblCos_Hours">2.0</span>
<span id="lblTchName">張婕</span>
<div id="lblC_Target">認識表演藝術的基本概念。<br>培養觀察與欣賞能力。</div>
<div id="lblC_Matter">表演藝術的性別平權</div>
<div id="lblWeek01">表演藝術的性別平權</div>
<div id="lblWeek18">期末考</div>
<div id="lblScore">平時30%<br>期中考30%<br>期末考40%</div>
<div id="lblReference">表演的藝術<br>表演學</div>
"""


class NtubOfficialParserTests(TestCase):
    def test_webforms_parser_keeps_named_fields_and_selected_options(self):
        parser = _FormFieldParser()
        parser.feed("""
            <input value="token" name="__VIEWSTATE" type="hidden">
            <input name="txtYear" value="115">
            <select name="ddlEdu"><option value="-1">請選</option>
              <option selected="selected" value="4">四技</option></select>
        """)
        self.assertEqual(parser.fields["__VIEWSTATE"], "token")
        self.assertEqual(parser.fields["txtYear"], "115")
        self.assertEqual(parser.fields["ddlEdu"], "4")

    def test_syllabus_parser_keeps_decimal_credit_and_weeks(self):
        parsed = parse_syllabus_html(SYLLABUS_HTML, {
            "academic_year": "115", "semester": "1",
            "class_code": "40630B", "course_code": "40635210",
        })
        self.assertEqual(parsed["credits"], Decimal("3.5"))
        self.assertEqual(parsed["weekly_progress"]["week_01"], "第一週進度")
        self.assertEqual(parsed["weekly_progress"]["week_18"], "第十八週進度")

    def test_syllabus_parser_matches_real_life_art_fields(self):
        parsed = parse_syllabus_html(REAL_SYLLABUS_HTML, {
            "academic_year": "115", "semester": "1",
            "class_code": "40630B", "course_code": "40037270",
        })
        self.assertEqual(parsed["course_name"], "生活藝術賞析")
        self.assertEqual(parsed["course_name_en"], "APPRECIATION OF LIFE ART")
        self.assertEqual(parsed["teacher_name"], "張婕")
        self.assertEqual(parsed["credits"], Decimal("2.0"))
        self.assertEqual(parsed["weekly_hours"], Decimal("2.0"))
        self.assertEqual(parsed["weekly_progress"]["week_01"], "表演藝術的性別平權")
        self.assertEqual(parsed["weekly_progress"]["week_18"], "期末考")
        self.assertEqual(parsed["assessment"], "平時30%\n期中考30%\n期末考40%")
        self.assertEqual(parsed["references"], "表演的藝術\n表演學")

    def test_syllabus_parser_rejects_wrong_course(self):
        with self.assertRaises(OfficialFetchError):
            parse_syllabus_html(SYLLABUS_HTML, {
                "academic_year": "115", "semester": "1",
                "class_code": "40630B", "course_code": "WRONG",
            })

    def test_timetable_connect_identifiers_are_deduplicated(self):
        html = """
        <table>
          <tr><th>節</th><th>一</th><th>二</th><th>三</th><th>四</th>
              <th>五</th><th>六</th><th>日</th></tr>
          <tr><td>第1節</td><td></td><td></td>
              <td><a href="javascript:ConnectCos_Short('115','1','40630B','1','40635210')">作業系統</a></td>
              <td></td><td></td><td></td><td></td></tr>
          <tr><td>第2節</td><td></td><td></td>
              <td><a href="javascript:ConnectCos_Short('115','1','40630B','1','40635210')">重複時段</a></td>
              <td></td><td></td><td></td><td></td></tr>
        </table>
        """
        payload = parse_timetable_html(html, "115", "1", "40630B", "資管三乙")
        entries = payload["classes"][0]["entries"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["course"]["code"], "40635210")
        self.assertEqual(entries[0]["course"]["name"], "作業系統")
        self.assertEqual(entries[0]["weekday"], 3)
        self.assertEqual(entries[0]["start_period"], 1)
        self.assertEqual(entries[0]["end_period"], 2)

    def test_timetable_without_position_rejects_instead_of_using_fake_1_1_1(self):
        html = """
        <table><tr><th>節</th><th>一</th><th>二</th><th>三</th>
        <th>四</th><th>五</th><th>六</th><th>日</th></tr></table>
        <a href="javascript:ConnectCos_Short('115','1','40630B','1','40635210')">作業系統</a>
        """
        with self.assertRaises(OfficialFetchError):
            parse_timetable_html(html, "115", "1", "40630B", "資管三乙")

    def test_sync_failure_is_retryable_and_does_not_raise(self):
        term = Term.objects.create(code="115-1")
        catalog = AcademicCatalog.objects.create(version="syllabus-snapshot")
        program = AcademicProgramType.objects.create(
            catalog=catalog, code="4", name_zh="四技")
        department = AcademicDepartment.objects.create(
            catalog=catalog, program_type=program, code="406", name_zh="資管")
        class_group = AcademicClassGroup.objects.create(
            catalog=catalog, department=department, code="40630B")
        snapshot = NtubTimetableSnapshot.objects.create(
            term=term, class_group=class_group)
        NtubTimetableEntry.objects.create(
            snapshot=snapshot, entry_type="course", course_code="40635210",
            official_serial="1", course_name="作業系統",
            weekday=3, start_period=1, end_period=2)

        class FailingClient:
            def fetch_syllabus(self, identifiers):
                raise OfficialFetchError("timeout")

        record = sync_syllabus({
            "academic_year": "115", "semester": "1",
            "class_code": "40630B", "official_serial": "1",
            "course_code": "40635210",
        }, FailingClient(), snapshot=snapshot)
        self.assertEqual(record.status, "failed")
        self.assertIn("timeout", record.error_message)
        self.assertEqual(OfficialTeachingSyllabus.objects.count(), 1)
        self.assertEqual(record.snapshot_id, snapshot.pk)

    def test_sync_without_snapshot_rejects_orphan_creation(self):
        with self.assertRaises(OfficialFetchError):
            sync_syllabus({
                "academic_year": "115", "semester": "1",
                "class_code": "40630B", "official_serial": "1",
                "course_code": "40635210",
            })
        self.assertEqual(OfficialTeachingSyllabus.objects.count(), 0)

    def test_offering_sync_skips_source_without_matching_snapshot(self):
        term = Term.objects.create(code="115-1")
        teacher = Teacher.objects.create(name="來源教師")
        catalog = AcademicCatalog.objects.create(version="offering-no-snapshot")
        program = AcademicProgramType.objects.create(
            catalog=catalog, code="4", name_zh="四技")
        department = AcademicDepartment.objects.create(
            catalog=catalog, program_type=program, code="406", name_zh="資管")
        class_group = AcademicClassGroup.objects.create(
            catalog=catalog, department=department, code="40630B")
        course = Course.objects.create(
            term=term, code="OS001", name="課程", teacher=teacher)
        section = Section.objects.create(
            course=course, label="1", day=1, start_period=1, end_period=2)
        NtubOfferingSource.objects.create(
            source_key="115|1|40630B|1|OS001", academic_year="115",
            semester="1", class_code="40630B", official_serial="1",
            course_code="OS001", term=term, class_group=class_group,
            course=course, section=section, teacher=teacher, course_name="課程")
        from ..ntub_official import sync_offering_syllabi
        self.assertEqual(len(sync_offering_syllabi()), 0)
        self.assertEqual(OfficialTeachingSyllabus.objects.count(), 0)
