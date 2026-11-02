"""NTUB 官方專業選修開課與課綱推薦匯入的測試。

測試對象：`courses/ntub_offerings_import.py` 與
`courses/management/commands/import_ntub_offerings.py`。

重點確認：

* 六筆官方開課完整保存，來源唯一鍵（學年|學期|班級代碼|序號|課號）正確。
* 六筆課綱推薦完整保存：3 筆 matched、3 筆 course_name_only 保留疑義狀態。
* 「財務管理」與「財務管理(二)(技優領航專班)」維持為不同課程。
* 重複匯入不會產生重複資料，也不會覆寫人工調整過的班別。
* 匯入不建立任何 Enrollment，也不改動目前學期與既有 Demo 資料。
* 來源矛盾時整批拒絕寫入（transaction.atomic 全回滾）。
"""

import json
import tempfile
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from ..models import (AcademicCatalog, AcademicClassGroup, AcademicDepartment,
                      AcademicProgramType, Course, Enrollment,
                      NtubCurriculumRecommendation, NtubOfferingAmbiguity,
                      NtubOfferingSource, NtubRecommendationCandidate,
                      Section, Term, Teacher)
from ..ntub_offerings_import import (OfferingImportError, import_from_file,
                                     import_payload, load_payload,
                                     validate_payload)

REPO_ROOT = Path(__file__).resolve().parents[2]
PAYLOAD_PATH = REPO_ROOT / "data" / "ntub_import_ready" / "offerings.json"

# 來源裡的 6 筆官方開課（學年|學期|班級代碼|序號|課號）
SOURCE_KEYS = [
    "115|1|40620A|15|40627440",
    "115|1|40620A|19|40627960",
    "115|1|40620A|21|40624660",
    "115|1|40620B|13|40624670",
    "115|1|40630A|20|40637100",
    "115|1|40630A|21|40635420",
]


def build_offering(key="115|1|40620A|15|40627440",
                   academic_year="115", semester="1", term_code="115-1",
                   class_code="40620A", class_name="四技資管二年甲班",
                   serial="15", course_code="40627440", course_name="數位內容概論",
                   teacher_id="1142", teacher_name="蕭培元",
                   weekday=5, start_period=5, end_period=7,
                   classroom_code="A1401", classroom_name="A1401 資401",
                   credits=3.0, capacity=48, enrolled=37):
    """測試用的最小官方開課來源。"""
    return {
        "key": key,
        "academic_year": academic_year,
        "semester": semester,
        "term_code": term_code,
        "class_code": class_code,
        "class_name": class_name,
        "serial": serial,
        "course_code": course_code,
        "course_name": course_name,
        "teacher": {"id": teacher_id, "name": teacher_name, "raw": f"{teacher_id} {teacher_name}"},
        "schedule": {
            "weekday": weekday,
            "weekday_label": "週五",
            "periods": list(range(start_period, end_period + 1)),
            "start_period": start_period,
            "end_period": end_period,
            "raw": "",
        },
        "classroom": {"code": classroom_code, "name": classroom_name, "raw": classroom_code},
        "credits": credits,
        "credits_raw": str(credits),
        "category": "專選",
        "elective_category": "專選",
        "official_capacity": capacity,
        "official_enrolled": enrolled,
        "official_capacity_raw": f"{capacity}/{enrolled}",
        "notes": [],
        "source": {
            "file": "115-1_數位內容概論.json",
            "keyword": course_name,
            "source_url": "https://ntcbadm.ntub.edu.tw/pub/TchSchedule_Search.aspx",
            "retrieved_at": "2026-10-29T02:34:20.518776+00:00",
            "exported_at": "2026-10-29T02:34:20.518776+00:00",
        },
    }


def build_payload(offerings=None, recommendations=None, ambiguities=None):
    offerings = offerings if offerings is not None else [build_offering()]
    return {
        "schema_version": "1.0",
        "offerings": offerings,
        "curriculum_recommendations": recommendations or [],
        "ambiguities": ambiguities or [],
    }


def build_recommendation(class_code="40630B", course_name="網站應用程式設計",
                         offering_key="115|1|40620A|19|40627960",
                         matched_class="40620A", match_status="course_name_only",
                         course_code="40627960", admission_year="113",
                         track="技優領航專班"):
    return {
        "admission_year": admission_year,
        "track": track,
        "program_code": "4",
        "program_name": "四技日間部",
        "department_code": "406",
        "department_name": "資訊管理系",
        "grade": 3,
        "semester": 1,
        "class_code": class_code,
        "course_name": course_name,
        "match_status": match_status,
        "offering_key": offering_key,
        "course_code": course_code,
        "matched_course_code": course_code,
        "matched_offering_class_code": matched_class,
        "candidates": [
            {
                "offering_key": offering_key,
                "course_code": course_code,
                "class_code": matched_class,
                "class_name": "四技資管二年甲班",
                "course_name": course_name,
            }
        ],
    }


class OfferingImportTestCase(TestCase):
    """共用的資料庫前置：建立學籍目錄、班級與既有 Demo 資料。"""

    def setUp(self):
        self.catalog = AcademicCatalog.objects.create(version="test-offerings", source_key="ntub")
        self.program = AcademicProgramType.objects.create(
            catalog=self.catalog, code="4", name_zh="四技日間部")
        self.department = AcademicDepartment.objects.create(
            catalog=self.catalog, program_type=self.program, code="406", name_zh="資訊管理系")
        self.current_term = Term.objects.create(code="114-1", is_current=True)
        self.teacher = Teacher.objects.create(name="Demo 教師")
        self.demo_course = Course.objects.create(
            term=self.current_term, code="D001", name="Demo 課程",
            teacher=self.teacher, credits=3)
        self.demo_section = Section.objects.create(
            course=self.demo_course, label="A", day=1, start_period=1,
            end_period=2, room="D101", capacity=40)
        self.user = get_user_model().objects.create_user("demo")
        Enrollment.objects.create(user=self.user, section=self.demo_section)

    def make_class_group(self, code, name_zh):
        return AcademicClassGroup.objects.create(
            catalog=self.catalog, department=self.department, code=code, name_zh=name_zh)


class ValidationTests(OfferingImportTestCase):
    def test_missing_offerings_is_rejected(self):
        with self.assertRaises(OfferingImportError):
            validate_payload({"offerings": []})

    def test_missing_required_field_is_rejected(self):
        offering = build_offering()
        del offering["course_code"]
        with self.assertRaises(OfferingImportError):
            validate_payload(build_payload([offering]))

    def test_invalid_weekday_is_rejected(self):
        offering = build_offering()
        offering["schedule"]["weekday"] = 9
        with self.assertRaises(OfferingImportError):
            validate_payload(build_payload([offering]))

    def test_duplicate_source_keys_are_rejected(self):
        offering = build_offering()
        with self.assertRaises(OfferingImportError):
            validate_payload(build_payload([offering, dict(offering)]))

    def test_invalid_match_status_is_rejected(self):
        recommendation = build_recommendation()
        recommendation["match_status"] = "whatever"
        with self.assertRaises(OfferingImportError):
            validate_payload(build_payload([build_offering()], [recommendation]))

    def test_stats_mismatch_is_rejected(self):
        payload = build_payload([build_offering()])
        payload["stats"] = {"raw_offerings": 9}
        with self.assertRaises(OfferingImportError):
            validate_payload(payload)


class ImportPayloadTests(OfferingImportTestCase):
    def test_import_creates_offering_source(self):
        self.make_class_group("40620A", "四技資管二年甲班")
        summary = import_payload(build_payload())

        self.assertEqual(summary.offerings_created, 1)
        self.assertEqual(summary.courses_created, 1)
        self.assertEqual(summary.sections_created, 1)
        self.assertEqual(NtubOfferingSource.objects.count(), 1)

        source = NtubOfferingSource.objects.get()
        self.assertEqual(source.source_key, "115|1|40620A|15|40627440")
        self.assertEqual(source.academic_year, "115")
        self.assertEqual(source.semester, "1")
        self.assertEqual(source.class_code, "40620A")
        self.assertEqual(source.official_serial, "15")
        self.assertEqual(source.course_code, "40627440")
        self.assertEqual(source.course_name, "數位內容概論")
        self.assertEqual(source.teacher_source_id, "1142")
        self.assertEqual(source.teacher_name, "蕭培元")
        self.assertEqual(source.elective_category, "專選")
        self.assertEqual(source.classroom_code, "A1401")
        self.assertEqual(source.classroom_name, "A1401 資401")
        self.assertEqual(source.official_capacity, 48)
        self.assertEqual(source.official_enrolled, 37)
        self.assertEqual(source.source_file, "115-1_數位內容概論.json")
        self.assertIsNotNone(source.retrieved_at)
        self.assertIsNotNone(source.exported_at)

    def test_import_maps_to_existing_orm_objects(self):
        self.make_class_group("40620A", "四技資管二年甲班")
        import_payload(build_payload())

        source = NtubOfferingSource.objects.get()
        self.assertEqual(source.term.code, "115-1")
        self.assertEqual(source.class_group.code, "40620A")
        self.assertEqual(source.course.code, "40627440")
        self.assertEqual(source.course.name, "數位內容概論")
        self.assertEqual(source.course.credits, 3)
        self.assertEqual(source.teacher.name, "蕭培元")
        self.assertEqual(source.section.day, 5)
        self.assertEqual(source.section.start_period, 5)
        self.assertEqual(source.section.end_period, 7)
        self.assertEqual(source.section.room, "A1401")
        self.assertEqual(source.section.capacity, 48)

    def test_missing_class_group_is_reported_and_skipped(self):
        summary = import_payload(build_payload())
        self.assertEqual(summary.offerings_created, 0)
        self.assertTrue(summary.has_conflicts)
        self.assertEqual(summary.conflicts[0]["kind"], "class_group_missing")

    def test_import_does_not_create_enrollments(self):
        self.make_class_group("40620A", "四技資管二年甲班")
        summary = import_payload(build_payload())

        self.assertEqual(summary.enrollments_created, 0)
        self.assertEqual(Enrollment.objects.count(), 1)  # 只有既有的 Demo 加選
        self.assertEqual(NtubOfferingSource.objects.get().section.enrollments.count(), 0)

    def test_import_does_not_change_current_term(self):
        self.make_class_group("40620A", "四技資管二年甲班")
        import_payload(build_payload())

        self.assertTrue(Term.objects.get(code="114-1").is_current)
        self.assertFalse(Term.objects.get(code="115-1").is_current)

    def test_import_does_not_touch_existing_demo_data(self):
        self.make_class_group("40620A", "四技資管二年甲班")
        import_payload(build_payload())

        demo = Course.objects.get(code="D001")
        self.assertEqual(demo.name, "Demo 課程")
        self.assertEqual(demo.term.code, "114-1")
        self.assertEqual(demo.teacher.name, "Demo 教師")
        self.assertEqual(Section.objects.filter(course=demo).count(), 1)
        self.assertEqual(Enrollment.objects.count(), 1)

    def test_existing_course_conflict_is_reported_not_overwritten(self):
        self.make_class_group("40620A", "四技資管二年甲班")
        term = Term.objects.create(code="115-1", is_current=False)
        course = Course.objects.create(
            term=term, code="40627440", name="別人建立的名稱",
            teacher=self.teacher, credits=2)

        summary = import_payload(build_payload())

        course.refresh_from_db()
        self.assertEqual(course.name, "別人建立的名稱")
        self.assertEqual(course.credits, 2)
        self.assertEqual(course.teacher.name, "Demo 教師")
        self.assertTrue(any(c["kind"] == "course_name" for c in summary.conflicts))

    def test_existing_section_with_enrollment_is_not_overwritten(self):
        self.make_class_group("40620A", "四技資管二年甲班")
        term = Term.objects.create(code="115-1", is_current=False)
        course = Course.objects.create(
            term=term, code="40627440", name="數位內容概論",
            teacher=self.teacher, credits=3)
        section = Section.objects.create(
            course=course, label="A", day=5, start_period=5, end_period=7,
            room="A1401", capacity=20)
        user = get_user_model().objects.create_user("student")
        Enrollment.objects.create(user=user, section=section)

        summary = import_payload(build_payload())

        section.refresh_from_db()
        self.assertEqual(section.capacity, 20)
        self.assertEqual(section.enrollments.count(), 1)
        self.assertTrue(any(c["kind"] == "section_has_enrollment"
                            for c in summary.conflicts))

    def test_import_rolls_back_on_failure(self):
        self.make_class_group("40620A", "四技資管二年甲班")
        bad = build_offering(key="115|1|40620A|99|40699999",
                              serial="99", course_code="40699999",
                              course_name="壞掉的課")
        bad["schedule"]["weekday"] = 0

        with self.assertRaises(OfferingImportError):
            import_payload(build_payload([bad]))

        self.assertEqual(Course.objects.count(), 1)  # 只有既有的 Demo 課程
        self.assertEqual(Section.objects.count(), 1)
        self.assertEqual(NtubOfferingSource.objects.count(), 0)


class RecommendationTests(OfferingImportTestCase):
    def setUp(self):
        super().setUp()
        self.make_class_group("40620A", "四技資管二年甲班")
        self.make_class_group("40630B", "四技資管三年乙班")
        self.offering = build_offering(
            key="115|1|40620A|19|40627960", serial="19",
            course_code="40627960", course_name="網站應用程式設計",
            teacher_id="1683", teacher_name="林俊杰",
            weekday=4, start_period=2, end_period=4,
            classroom_code="A4708", classroom_name="A4708")
        import_payload(build_payload([self.offering]))

    def test_matched_recommendation_is_confirmed(self):
        recommendation = build_recommendation(
            class_code="40620A", course_name="網站應用程式設計",
            offering_key="115|1|40620A|19|40627960", matched_class="40620A",
            match_status="matched", course_code="40627960",
            admission_year="114", track="一般四技")

        import_payload(build_payload([self.offering], [recommendation]))

        rec = NtubCurriculumRecommendation.objects.get()
        self.assertEqual(rec.match_status, "matched")
        self.assertFalse(rec.is_ambiguous)
        self.assertTrue(rec.is_confirmed)
        self.assertEqual(rec.matched_offering_class_code, "40620A")
        self.assertEqual(rec.offering.source_key, "115|1|40620A|19|40627960")
        self.assertEqual(rec.class_group.code, "40620A")

    def test_course_name_only_recommendation_keeps_ambiguity(self):
        recommendation = build_recommendation()

        import_payload(build_payload([self.offering], [recommendation]))

        rec = NtubCurriculumRecommendation.objects.get()
        self.assertEqual(rec.match_status, "course_name_only")
        self.assertTrue(rec.is_ambiguous)
        self.assertFalse(rec.is_confirmed)
        self.assertTrue(rec.ambiguity_message)
        self.assertEqual(rec.class_code, "40630B")
        self.assertEqual(rec.matched_offering_class_code, "40620A")
        # 候選開課仍然保留，但不代表已確認跨班選修
        self.assertEqual(rec.candidates.count(), 1)
        self.assertEqual(rec.offering.source_key, "115|1|40620A|19|40627960")

    def test_recommendation_keeps_academic_fields(self):
        recommendation = build_recommendation()
        import_payload(build_payload([self.offering], [recommendation]))

        rec = NtubCurriculumRecommendation.objects.get()
        self.assertEqual(rec.admission_year, "113")
        self.assertEqual(rec.track, "技優領航專班")
        self.assertEqual(rec.program_code, "4")
        self.assertEqual(rec.program_name, "四技日間部")
        self.assertEqual(rec.department_code, "406")
        self.assertEqual(rec.department_name, "資訊管理系")
        self.assertEqual(rec.grade, 3)
        self.assertEqual(rec.semester, "1")

    def test_candidate_relation_is_saved(self):
        import_payload(build_payload([self.offering], [build_recommendation()]))

        candidate = NtubRecommendationCandidate.objects.get()
        self.assertEqual(candidate.offering_key, "115|1|40620A|19|40627960")
        self.assertEqual(candidate.class_code, "40620A")
        self.assertEqual(candidate.course_code, "40627960")
        self.assertEqual(candidate.offering.source_key, "115|1|40620A|19|40627960")

    def test_ambiguity_is_saved_as_is(self):
        ambiguity = {
            "kind": "recommendation_class_mismatch",
            "message": "113 技優領航專班 班級 40630B 推薦「網站應用程式設計」，"
                       "官方開課屬班級 40620A（課號 40627960）；視為同一門課但班別不同，需人工確認。",
            "admission_year": "113",
            "track": "技優領航專班",
            "class_code": "40630B",
            "course_name": "網站應用程式設計",
            "candidates": [
                {"offering_key": "115|1|40620A|19|40627960", "course_code": "40627960",
                 "class_code": "40620A", "class_name": "四技資管二年甲班",
                 "course_name": "網站應用程式設計"}
            ],
        }
        import_payload(build_payload([self.offering], [build_recommendation()], [ambiguity]))

        stored = NtubOfferingAmbiguity.objects.get()
        self.assertEqual(stored.kind, "recommendation_class_mismatch")
        self.assertEqual(stored.message, ambiguity["message"])
        self.assertIsNotNone(stored.offering)
        self.assertIsNotNone(stored.recommendation)


class FinancialManagementDistinctnessTests(OfferingImportTestCase):
    """「財務管理」與「財務管理(二)(技優領航專班)」必須是兩門不同的課。"""

    def setUp(self):
        super().setUp()
        self.make_class_group("40620A", "四技資管二年甲班")
        self.make_class_group("40620B", "四技資管二年乙班")
        self.plain = build_offering(
            key="115|1|40620A|21|40624660", serial="21",
            course_code="40624660", course_name="財務管理",
            teacher_id="0249", teacher_name="賴蓉禾",
            weekday=1, start_period=5, end_period=7,
            classroom_code="A4607", classroom_name="A4607")
        self.special = build_offering(
            key="115|1|40620B|13|40624670", class_code="40620B",
            class_name="四技資管二年乙班", serial="13",
            course_code="40624670", course_name="財務管理(二)(技優領航專班)",
            teacher_id="0161", teacher_name="許慶文",
            weekday=1, start_period=5, end_period=7,
            classroom_code="A6410", classroom_name="A6410")

    def test_two_financial_management_courses_are_distinct(self):
        import_payload(build_payload([self.plain, self.special]))

        plain = Course.objects.get(code="40624660")
        special = Course.objects.get(code="40624670")
        self.assertNotEqual(plain, special)
        self.assertEqual(plain.name, "財務管理")
        self.assertEqual(special.name, "財務管理(二)(技優領航專班)")

        self.assertEqual(NtubOfferingSource.objects.count(), 2)
        plain_source = NtubOfferingSource.objects.get(course_code="40624660")
        special_source = NtubOfferingSource.objects.get(course_code="40624670")
        self.assertEqual(plain_source.class_group.code, "40620A")
        self.assertEqual(special_source.class_group.code, "40620B")
        self.assertNotEqual(plain_source.section, special_source.section)

    def test_partial_keyword_ambiguity_points_to_special_course(self):
        ambiguity = {
            "kind": "source_partial_keyword_match",
            "message": "關鍵字「財務管理」命中「財務管理(二)(技優領航專班)」，屬部分比對，已保留",
            "keyword": "財務管理",
        }
        import_payload(build_payload([self.plain, self.special], [], [ambiguity]))

        stored = NtubOfferingAmbiguity.objects.get(kind="source_partial_keyword_match")
        self.assertIsNotNone(stored.offering)
        self.assertEqual(stored.offering.course_code, "40624670")
        self.assertEqual(stored.offering.course_name, "財務管理(二)(技優領航專班)")


class IdempotencyTests(OfferingImportTestCase):
    def setUp(self):
        super().setUp()
        self.make_class_group("40620A", "四技資管二年甲班")
        self.payload = build_payload([build_offering()], [build_recommendation()])

    def test_repeated_import_creates_no_duplicates(self):
        import_payload(self.payload)
        summary = import_payload(self.payload)

        self.assertEqual(summary.offerings_created, 0)
        self.assertEqual(summary.offerings_updated, 1)
        self.assertEqual(summary.courses_created, 0)
        self.assertEqual(summary.sections_created, 0)
        self.assertEqual(summary.recommendations_created, 0)
        self.assertEqual(summary.candidates_created, 0)
        self.assertEqual(summary.candidates_updated, 1)

        self.assertEqual(NtubOfferingSource.objects.count(), 1)
        self.assertEqual(Course.objects.count(), 2)  # 1 筆 Demo + 1 筆匯入
        self.assertEqual(Section.objects.count(), 2)
        self.assertEqual(NtubCurriculumRecommendation.objects.count(), 1)
        self.assertEqual(NtubRecommendationCandidate.objects.count(), 1)
        self.assertEqual(Enrollment.objects.count(), 1)

    def test_repeated_import_keeps_manual_section_adjustment(self):
        import_payload(self.payload)
        source = NtubOfferingSource.objects.get()
        section = source.section
        section.day = 2
        section.start_period = 3
        section.end_period = 4
        section.capacity = 55
        section.save()

        summary = import_payload(self.payload)

        section.refresh_from_db()
        self.assertEqual(section.day, 2)
        self.assertEqual(section.capacity, 55)
        self.assertTrue(any(c["kind"] == "section_adjusted" for c in summary.conflicts))


class CommandTests(OfferingImportTestCase):
    def setUp(self):
        super().setUp()
        self.make_class_group("40620A", "四技資管二年甲班")

    def write_payload(self, payload):
        tmp = tempfile.mkdtemp()
        path = Path(tmp) / "offerings.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    def test_command_imports_payload_file(self):
        path = self.write_payload(build_payload())
        call_command("import_ntub_offerings", "--file", str(path))

        self.assertEqual(NtubOfferingSource.objects.count(), 1)
        self.assertEqual(Course.objects.filter(term__code="115-1").count(), 1)

    def test_command_dry_run_writes_nothing(self):
        path = self.write_payload(build_payload())
        call_command("import_ntub_offerings", "--file", str(path), "--dry-run")

        self.assertEqual(NtubOfferingSource.objects.count(), 0)
        self.assertEqual(Course.objects.filter(term__code="115-1").count(), 0)
        self.assertEqual(Section.objects.filter(course__term__code="115-1").count(), 0)

    def test_command_rejects_invalid_payload(self):
        path = self.write_payload({"offerings": []})
        with self.assertRaises(CommandError):
            call_command("import_ntub_offerings", "--file", str(path))

    def test_command_reports_missing_file(self):
        with self.assertRaises(CommandError):
            call_command("import_ntub_offerings", "--file", "does-not-exist.json")


class RealPayloadTests(OfferingImportTestCase):
    """用真正的來源檔跑一次，確認六筆來源都能正確映射。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if not PAYLOAD_PATH.exists():
            raise cls.skipTest(cls, f"找不到來源檔：{PAYLOAD_PATH}")

    def setUp(self):
        super().setUp()
        for code, name in [("40620A", "四技資管二年甲班"),
                           ("40620B", "四技資管二年乙班"),
                           ("40630A", "四技資管三年甲班"),
                           ("40630B", "四技資管三年乙班")]:
            self.make_class_group(code, name)
        self.payload = load_payload(PAYLOAD_PATH)
        validate_payload(self.payload)

    def test_real_payload_maps_all_six_offerings(self):
        summary = import_payload(self.payload)

        self.assertEqual(summary.offerings_created, 6)
        self.assertEqual(summary.courses_created, 6)
        self.assertEqual(summary.sections_created, 6)
        self.assertEqual(NtubOfferingSource.objects.count(), 6)
        self.assertEqual(
            sorted(NtubOfferingSource.objects.values_list("source_key", flat=True)),
            sorted(SOURCE_KEYS))

    def test_real_payload_all_courses_belong_to_115_1(self):
        import_payload(self.payload)

        terms = set(Course.objects.filter(
            code__in=[o["course_code"] for o in self.payload["offerings"]]
        ).values_list("term__code", flat=True))
        self.assertEqual(terms, {"115-1"})

    def test_real_payload_credits_and_capacity(self):
        import_payload(self.payload)

        for offering in self.payload["offerings"]:
            source = NtubOfferingSource.objects.get(source_key=offering["key"])
            self.assertEqual(source.course.credits, int(round(offering["credits"])))
            self.assertEqual(source.section.capacity, offering["official_capacity"])
            self.assertEqual(source.official_enrolled, offering["official_enrolled"])
            self.assertEqual(source.section.enrollments.count(), 0)

    def test_real_payload_saves_six_recommendations(self):
        import_payload(self.payload)

        self.assertEqual(NtubCurriculumRecommendation.objects.count(), 6)
        matched = NtubCurriculumRecommendation.objects.filter(match_status="matched")
        name_only = NtubCurriculumRecommendation.objects.filter(match_status="course_name_only")
        self.assertEqual(matched.count(), 3)
        self.assertEqual(name_only.count(), 3)
        # course_name_only 一律保留疑義
        self.assertTrue(all(rec.is_ambiguous for rec in name_only))
        self.assertFalse(any(rec.is_ambiguous for rec in matched))
        self.assertTrue(all(rec.is_confirmed for rec in matched))
        self.assertFalse(any(rec.is_confirmed for rec in name_only))

    def test_real_payload_saves_ambiguities(self):
        import_payload(self.payload)

        self.assertEqual(NtubOfferingAmbiguity.objects.count(), 5)
        self.assertEqual(
            NtubOfferingAmbiguity.objects.filter(kind="recommendation_class_mismatch").count(), 3)
        self.assertEqual(
            NtubOfferingAmbiguity.objects.filter(kind="offering_without_recommendation").count(), 1)
        self.assertEqual(
            NtubOfferingAmbiguity.objects.filter(kind="source_partial_keyword_match").count(), 1)

    def test_real_payload_repeated_import_is_idempotent(self):
        import_payload(self.payload)
        summary = import_payload(self.payload)

        self.assertEqual(summary.offerings_created, 0)
        self.assertEqual(summary.courses_created, 0)
        self.assertEqual(summary.sections_created, 0)
        self.assertEqual(NtubOfferingSource.objects.count(), 6)
        self.assertEqual(Course.objects.count(), 7)  # 1 筆 Demo + 6 筆
        self.assertEqual(Section.objects.count(), 7)
        self.assertEqual(NtubCurriculumRecommendation.objects.count(), 6)
        self.assertEqual(Enrollment.objects.count(), 1)

    def test_real_payload_creates_no_enrollments(self):
        import_payload(self.payload)

        self.assertEqual(Enrollment.objects.count(), 1)  # 只有既有的 Demo 加選
        for source in NtubOfferingSource.objects.all():
            self.assertEqual(source.section.enrollments.count(), 0)

    def test_real_payload_keeps_current_term_untouched(self):
        import_payload(self.payload)

        self.assertTrue(Term.objects.get(code="114-1").is_current)
        self.assertFalse(Term.objects.get(code="115-1").is_current)
