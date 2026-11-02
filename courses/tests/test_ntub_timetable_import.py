"""NTUB 官方班級課表匯入的測試。

測試對象：`courses/ntub_timetable_import.py` 與
`courses/management/commands/import_ntub_timetables.py`。

重點確認：

* 匯入結果的筆數與來源一致（快照、明細、課程、活動）。
* 重複匯入不產生重複資料。
* 來源矛盾時整批拒絕寫入。
* 匯入不會動到 Course / Section / Enrollment，也不會改掉目前學期。
"""

import json
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from ..models import (AcademicCatalog, AcademicClassGroup, AcademicDepartment,
                      AcademicProgramType, Course, Enrollment, NtubTimetableEntry,
                      NtubTimetableSnapshot, Section, Term, Waitlist)
from ..ntub_timetable_import import (TimetableImportError, import_from_file,
                                     import_payload, load_payload, validate_payload)

REPO_ROOT = Path(__file__).resolve().parents[2]
PAYLOAD_PATH = REPO_ROOT / "data" / "ntub_import_ready" / "timetables.json"


def build_payload(class_code="40620A", class_name="四技資管二年甲班"):
    """測試用的最小來源：一個班級、兩筆明細（一課程一活動）。"""
    return {
        "schema_version": "1.0",
        "scope": "class_timetable",
        "produces_enrollments": False,
        "classes": [
            {
                "class_code": class_code,
                "class_name": class_name,
                "academic_year": "115",
                "semester": "1",
                "term_code": "115-1",
                "counts": {"total": 2, "course": 1, "activity": 1},
                "entries": [
                    {
                        "entry_type": "course",
                        "weekday": 1,
                        "start_period": 2,
                        "end_period": 4,
                        "periods": [2, 3, 4],
                        "course": {
                            "code": "40626610",
                            "serial": "10",
                            "name": "資訊網路",
                            "teacher": "蒯思齊",
                            "classroom": "電606(承曦樓)",
                        },
                        "activity": None,
                        "source": {
                            "raw_text": "資訊網路 | 蒯思齊 | 電606(承曦樓)",
                            "href": "javascript:ConnectCos_Short('115','1','40620A','10','40626610');",
                            "notes": [],
                            "course_ref": None,
                        },
                    },
                    {
                        "entry_type": "activity",
                        "weekday": 1,
                        "start_period": 8,
                        "end_period": 9,
                        "periods": [8, 9],
                        "course": None,
                        "activity": {"name": "共同空堂"},
                        "source": {"raw_text": "共同空堂", "href": "", "notes": [],
                                   "course_ref": None},
                    },
                ],
                "source": {
                    "source_url": "https://ntcbadm.ntub.edu.tw/pub/Cur_Class.aspx",
                    "retrieved_at": "2026-10-28T16:15:41.634985+00:00",
                },
            }
        ],
        "totals": {"class_count": 1, "entry_count": 2, "course_count": 1,
                   "activity_count": 1},
    }


class TimetableImportTestCase(TestCase):
    """共用的資料庫前置：建立班級、學期與既有課程資料。"""

    def setUp(self):
        self.catalog = AcademicCatalog.objects.create(version="test-timetable", source_key="ntub")
        self.program = AcademicProgramType.objects.create(
            catalog=self.catalog, code="4", name_zh="四技日間部")
        self.department = AcademicDepartment.objects.create(
            catalog=self.catalog, program_type=self.program, code="406", name_zh="資訊管理系")
        self.class_group = AcademicClassGroup.objects.create(
            catalog=self.catalog, department=self.department, code="40620A",
            name_zh="四技資管二年甲班")
        self.current_term = Term.objects.create(code="114-1", is_current=True)
        self.teacher = None

    def make_class_group(self, code, name_zh):
        return AcademicClassGroup.objects.create(
            catalog=self.catalog, department=self.department, code=code, name_zh=name_zh)


class ImportPayloadTests(TimetableImportTestCase):
    def test_import_creates_snapshot_and_entries(self):
        summary = import_payload(build_payload())

        self.assertEqual(summary.snapshots_created, 1)
        self.assertEqual(summary.entries_created, 2)
        self.assertEqual(summary.course_entries, 1)
        self.assertEqual(summary.activity_entries, 1)
        self.assertEqual(NtubTimetableSnapshot.objects.count(), 1)
        self.assertEqual(NtubTimetableEntry.objects.count(), 2)

    def test_term_is_created_without_becoming_current(self):
        import_payload(build_payload())

        term = Term.objects.get(code="115-1")
        self.assertFalse(term.is_current)
        self.assertTrue(Term.objects.get(code="114-1").is_current)

    def test_existing_term_is_current_flag_untouched(self):
        import_payload(build_payload())
        import_payload(build_payload())

        self.assertTrue(Term.objects.get(code="114-1").is_current)
        self.assertEqual(Term.objects.filter(is_current=True).count(), 1)

    def test_course_entry_fields_are_mapped_from_source(self):
        import_payload(build_payload())

        entry = NtubTimetableEntry.objects.get(entry_type="course")
        self.assertEqual(entry.course_code, "40626610")
        self.assertEqual(entry.official_serial, "10")
        self.assertEqual(entry.course_name, "資訊網路")
        self.assertEqual(entry.teacher_name, "蒯思齊")
        self.assertEqual(entry.classroom, "電606(承曦樓)")
        self.assertEqual(entry.weekday, 1)
        self.assertEqual(entry.start_period, 2)
        self.assertEqual(entry.end_period, 4)
        self.assertEqual(entry.periods, [2, 3, 4])
        self.assertEqual(entry.raw_text, "資訊網路 | 蒯思齊 | 電606(承曦樓)")
        self.assertIn("ConnectCos_Short", entry.course_href)

    def test_activity_entry_has_no_course_fields(self):
        import_payload(build_payload())

        entry = NtubTimetableEntry.objects.get(entry_type="activity")
        self.assertEqual(entry.course_name, "共同空堂")
        self.assertEqual(entry.course_code, "")
        self.assertEqual(entry.official_serial, "")
        self.assertEqual(entry.teacher_name, "")
        self.assertEqual(entry.classroom, "")
        self.assertEqual(entry.raw_text, "共同空堂")

    def test_reimport_does_not_duplicate(self):
        import_payload(build_payload())
        import_payload(build_payload())

        self.assertEqual(NtubTimetableSnapshot.objects.count(), 1)
        self.assertEqual(NtubTimetableEntry.objects.count(), 2)

    def test_reimport_updates_existing_entries(self):
        import_payload(build_payload())
        entry = NtubTimetableEntry.objects.get(entry_type="course")
        entry_id = entry.pk

        payload = build_payload()
        payload["classes"][0]["entries"][0]["course"]["teacher"] = "新老師"
        import_payload(payload)

        entry.refresh_from_db()
        self.assertEqual(entry.pk, entry_id)
        self.assertEqual(entry.teacher_name, "新老師")
        self.assertEqual(NtubTimetableEntry.objects.count(), 2)

    def test_reimport_moves_course_without_deleting_it(self):
        import_payload(build_payload())
        entry = NtubTimetableEntry.objects.get(entry_type="course")
        entry_id = entry.pk

        payload = build_payload()
        payload["classes"][0]["entries"][0].update(
            weekday=3, start_period=5, end_period=7, periods=[5, 6, 7])
        import_payload(payload)

        entry.refresh_from_db()
        self.assertEqual(entry.pk, entry_id)
        self.assertEqual((entry.weekday, entry.start_period, entry.end_period),
                         (3, 5, 7))
        self.assertEqual(entry.periods, [5, 6, 7])

    def test_invalid_position_does_not_overwrite_existing_snapshot(self):
        import_payload(build_payload())
        entry = NtubTimetableEntry.objects.get(entry_type="course")
        original = (entry.weekday, entry.start_period, entry.end_period)

        payload = build_payload()
        payload["classes"][0]["entries"][0]["weekday"] = None
        with self.assertRaises(TimetableImportError):
            import_payload(payload)

        entry.refresh_from_db()
        self.assertEqual(
            (entry.weekday, entry.start_period, entry.end_period), original)

    def test_import_does_not_create_course_section_or_enrollment(self):
        user = get_user_model().objects.create_user("student")

        import_payload(build_payload())

        self.assertEqual(Course.objects.count(), 0)
        self.assertEqual(Section.objects.count(), 0)
        self.assertEqual(Enrollment.objects.count(), 0)
        self.assertEqual(Waitlist.objects.count(), 0)
        self.assertEqual(get_user_model().objects.count(), 1)
        self.assertFalse(Enrollment.objects.filter(user=user).exists())

    def test_missing_class_group_is_rejected(self):
        payload = build_payload(class_code="99999X", class_name="不存在的班")

        with self.assertRaises(TimetableImportError):
            import_payload(payload)

        self.assertEqual(NtubTimetableSnapshot.objects.count(), 0)
        self.assertEqual(NtubTimetableEntry.objects.count(), 0)

    def test_class_name_mismatch_is_rejected(self):
        payload = build_payload(class_name="別的班級名稱")

        with self.assertRaises(TimetableImportError):
            import_payload(payload)

        self.assertEqual(NtubTimetableSnapshot.objects.count(), 0)

    def test_invalid_weekday_is_rejected(self):
        payload = build_payload()
        payload["classes"][0]["entries"][0]["weekday"] = 9

        with self.assertRaises(TimetableImportError):
            import_payload(payload)

    def test_inconsistent_periods_are_rejected(self):
        payload = build_payload()
        payload["classes"][0]["entries"][0]["periods"] = [2, 5]

        with self.assertRaises(TimetableImportError):
            import_payload(payload)

    def test_counts_mismatch_is_rejected(self):
        payload = build_payload()
        payload["classes"][0]["counts"]["total"] = 5

        with self.assertRaises(TimetableImportError):
            import_payload(payload)

    def test_course_entry_without_course_field_is_rejected(self):
        payload = build_payload()
        payload["classes"][0]["entries"][0]["course"] = None

        with self.assertRaises(TimetableImportError):
            import_payload(payload)

    def test_activity_entry_with_course_field_is_rejected(self):
        payload = build_payload()
        payload["classes"][0]["entries"][1]["course"] = {
            "code": "1", "serial": "1", "name": "x", "teacher": "y", "classroom": "z"}

        with self.assertRaises(TimetableImportError):
            import_payload(payload)

    def test_failed_import_rolls_back_everything(self):
        """第二個班級有矛盾時，第一個班級也不能留在資料庫裡。"""
        payload = build_payload()
        bad = build_payload(class_code="40630B", class_name="四技資管三年乙班")
        bad["classes"][0]["entries"][0]["weekday"] = 0
        payload["classes"].append(bad["classes"][0])
        payload["totals"]["class_count"] = 2

        with self.assertRaises(TimetableImportError):
            import_payload(payload)

        self.assertEqual(NtubTimetableSnapshot.objects.count(), 0)
        self.assertEqual(NtubTimetableEntry.objects.count(), 0)
        self.assertFalse(Term.objects.filter(code="115-1").exists())

    def test_military_training_elective_keeps_original_classification(self):
        payload = build_payload()
        payload["classes"][0]["entries"][0]["course"]["name"] = "軍訓(選修)"
        payload["classes"][0]["entries"][0]["course"]["code"] = "90319000"
        payload["classes"][0]["entries"][0]["course"]["serial"] = "23"

        import_payload(payload)

        entry = NtubTimetableEntry.objects.get(entry_type="course")
        self.assertEqual(entry.course_name, "軍訓(選修)")
        self.assertEqual(entry.course_code, "90319000")
        self.assertEqual(entry.official_serial, "23")


class CommandTests(TimetableImportTestCase):
    def test_command_imports_payload_file(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "timetables.json"
            path.write_text(json.dumps(build_payload(), ensure_ascii=False), encoding="utf-8")
            call_command("import_ntub_timetables", "--file", str(path))

        self.assertEqual(NtubTimetableSnapshot.objects.count(), 1)
        self.assertEqual(NtubTimetableEntry.objects.count(), 2)

    def test_command_rejects_invalid_payload(self):
        import tempfile

        payload = build_payload()
        payload["classes"][0]["entries"][0]["weekday"] = 9
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "timetables.json"
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            with self.assertRaises(CommandError):
                call_command("import_ntub_timetables", "--file", str(path))

        self.assertEqual(NtubTimetableSnapshot.objects.count(), 0)

    def test_dry_run_writes_nothing(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "timetables.json"
            path.write_text(json.dumps(build_payload(), ensure_ascii=False), encoding="utf-8")
            call_command("import_ntub_timetables", "--file", str(path), "--dry-run")

        self.assertEqual(NtubTimetableSnapshot.objects.count(), 0)
        self.assertFalse(Term.objects.filter(code="115-1").exists())


class RealPayloadTests(TimetableImportTestCase):
    """用真正的來源檔跑一次，確認筆數與任務規格一致。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if not PAYLOAD_PATH.exists():
            raise cls.skipTest(cls, f"找不到來源檔：{PAYLOAD_PATH}")

    def setUp(self):
        super().setUp()
        self.make_class_group("40630B", "四技資管三年乙班")
        self.payload = load_payload(PAYLOAD_PATH)
        validate_payload(self.payload)

    def test_real_payload_matches_expected_counts(self):
        summary = import_payload(self.payload)

        self.assertEqual(summary.snapshots_created, 2)
        self.assertEqual(summary.entries_created, 16)
        self.assertEqual(summary.course_entries, 12)
        self.assertEqual(summary.activity_entries, 4)
        self.assertEqual(NtubTimetableSnapshot.objects.count(), 2)
        self.assertEqual(NtubTimetableEntry.objects.count(), 16)
        self.assertEqual(
            NtubTimetableEntry.objects.filter(entry_type="course").count(), 12)
        self.assertEqual(
            NtubTimetableEntry.objects.filter(entry_type="activity").count(), 4)

    def test_real_payload_per_class_counts(self):
        import_payload(self.payload)

        first = NtubTimetableSnapshot.objects.get(class_group__code="40620A")
        second = NtubTimetableSnapshot.objects.get(class_group__code="40630B")
        self.assertEqual(first.entries.count(), 9)
        self.assertEqual(second.entries.count(), 7)
        self.assertEqual(first.term.code, "115-1")
        self.assertEqual(second.term.code, "115-1")

    def test_real_payload_reimport_keeps_counts(self):
        import_payload(self.payload)
        import_payload(self.payload)

        self.assertEqual(NtubTimetableSnapshot.objects.count(), 2)
        self.assertEqual(NtubTimetableEntry.objects.count(), 16)

    def test_real_payload_creates_no_enrollment(self):
        import_payload(self.payload)

        self.assertEqual(Enrollment.objects.count(), 0)
        self.assertEqual(Section.objects.count(), 0)
        self.assertEqual(Course.objects.count(), 0)
