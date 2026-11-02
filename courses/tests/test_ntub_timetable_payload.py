"""NTUB 官方班級課表標準化的測試。

測試對象是 `courses/ntub_timetable_payload.py`：確認它把來源 JSON 轉成正確的
標準格式，並且不會把官方課表誤當成學生的 Enrollment。

測試使用兩種資料來源：

* 內嵌的最小來源片段 → 測純函式的正規化行為，不依賴外部檔案。
* 真正的來源目錄（存在才跑）→ 驗證實際筆數與預期一致。
"""

import json
import unittest
from pathlib import Path

from courses.ntub_timetable_payload import (
    ENTRY_ACTIVITY,
    ENTRY_COURSE,
    SourceRef,
    academic_year_ad,
    build_payload,
    build_payload_from_dir,
    normalize_class,
    normalize_entry,
    parse_course_href,
    term_code,
    to_orm_records,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE_DIR = Path(
    r"C:\Users\user\OneDrive\Desktop\course_select\coursepuzzle-main\data\ntub_timetables"
)
OUTPUT_PATH = REPO_ROOT / "data" / "ntub_import_ready" / "timetables.json"


def make_raw_entry(**overrides):
    raw = {
        "weekday": 1,
        "weekday_name": "一",
        "start_period": 2,
        "end_period": 4,
        "periods": [2, 3, 4],
        "entry_type": "course",
        "course_name": "程式設計",
        "teacher": "王大明",
        "classroom": "A101",
        "course_href": "javascript:ConnectCos_Short('115','1','40620A','10','40626610');",
        "course_ref": {
            "academic_year": "115",
            "semester": "1",
            "class_code": "40620A",
            "serial": "10",
            "course_id": "40626610",
        },
        "raw_text": "程式設計 | 王大明 | A101",
        "notes": [],
    }
    raw.update(overrides)
    return raw


def make_raw_class(entries=None, class_code="40620A"):
    return {
        "source_url": "https://ntcbadm.ntub.edu.tw/pub/Cur_Class.aspx",
        "retrieved_at": "2026-10-28T16:15:41.634985+00:00",
        "exported_at": "2026-10-28T16:15:41.636004+00:00",
        "request": {
            "academic_year": "115",
            "semester": "1",
            "edu": "4",
            "dept": "406",
            "class_code": class_code,
        },
        "class": {
            "code": class_code,
            "name": "四技資管二年甲班",
            "academic_year": "115",
            "semester": "1",
        },
        "entries": entries if entries is not None else [make_raw_entry()],
        "stats": {},
        "ambiguities": [],
    }


class TermCodeTests(unittest.TestCase):
    def test_term_code_format(self):
        self.assertEqual(term_code("115", "1"), "115-1")
        self.assertEqual(term_code(115, 1), "115-1")

    def test_term_code_rejects_empty(self):
        with self.assertRaises(ValueError):
            term_code("", "1")
        with self.assertRaises(ValueError):
            term_code("115", "")

    def test_academic_year_ad_conversion(self):
        self.assertEqual(academic_year_ad("115"), 2026)
        self.assertEqual(academic_year_ad(114), 2025)


class ParseCourseHrefTests(unittest.TestCase):
    def test_parses_connect_short(self):
        parsed = parse_course_href(
            "javascript:ConnectCos_Short('115','1','40620A','10','40626610');"
        )
        self.assertEqual(parsed["academic_year"], "115")
        self.assertEqual(parsed["semester"], "1")
        self.assertEqual(parsed["class_code"], "40620A")
        self.assertEqual(parsed["serial"], "10")
        self.assertEqual(parsed["course_id"], "40626610")

    def test_returns_empty_for_garbage(self):
        self.assertEqual(parse_course_href(""), {})
        self.assertEqual(parse_course_href("javascript:void(0);"), {})


class NormalizeEntryTests(unittest.TestCase):
    def test_keeps_start_and_end_period(self):
        entry = normalize_entry(make_raw_entry(), class_code="40620A", term="115-1")
        self.assertEqual(entry.start_period, 2)
        self.assertEqual(entry.end_period, 4)
        self.assertEqual(entry.periods, [2, 3, 4])
        self.assertEqual(entry.period_count, 3)
        self.assertTrue(entry.is_consecutive)

    def test_single_period_entry(self):
        entry = normalize_entry(
            make_raw_entry(start_period=7, end_period=7, periods=[7]),
            class_code="40620A",
            term="115-1",
        )
        self.assertEqual(entry.periods, [7])
        self.assertEqual(entry.period_count, 1)
        self.assertTrue(entry.is_consecutive)

    def test_non_consecutive_periods_are_rebuilt_and_flagged(self):
        entry = normalize_entry(
            make_raw_entry(start_period=2, end_period=4, periods=[2, 4]),
            class_code="40620A",
            term="115-1",
        )
        self.assertEqual(entry.periods, [2, 3, 4])
        self.assertTrue(any("不一致" in w for w in entry.warnings))

    def test_swapped_periods_are_corrected(self):
        entry = normalize_entry(
            make_raw_entry(start_period=4, end_period=2, periods=[2, 3, 4]),
            class_code="40620A",
            term="115-1",
        )
        self.assertEqual(entry.start_period, 2)
        self.assertEqual(entry.end_period, 4)

    def test_entry_without_periods_is_dropped(self):
        entry = normalize_entry(
            make_raw_entry(start_period=None, end_period=None, periods=[]),
            class_code="40620A",
            term="115-1",
        )
        self.assertIsNone(entry)

    def test_weekday_label(self):
        entry = normalize_entry(
            make_raw_entry(weekday=4, weekday_name="四"),
            class_code="40620A",
            term="115-1",
        )
        self.assertEqual(entry.weekday, 4)
        self.assertEqual(entry.weekday_label, "週四")

    def test_course_entry_fields(self):
        entry = normalize_entry(make_raw_entry(), class_code="40620A", term="115-1")
        self.assertEqual(entry.entry_type, ENTRY_COURSE)
        self.assertIsNotNone(entry.course)
        self.assertEqual(entry.course["name"], "程式設計")
        self.assertEqual(entry.course["teacher"], "王大明")
        self.assertEqual(entry.course["classroom"], "A101")
        self.assertEqual(entry.course["code"], "40626610")
        self.assertIsNone(entry.activity)

    def test_activity_entry_has_no_course_payload(self):
        entry = normalize_entry(
            make_raw_entry(
                entry_type="activity",
                course_name="",
                teacher="",
                classroom="",
                course_href="",
                course_ref=None,
                raw_text="共同空堂",
            ),
            class_code="40620A",
            term="115-1",
        )
        self.assertEqual(entry.entry_type, ENTRY_ACTIVITY)
        self.assertIsNone(entry.course)
        self.assertEqual(entry.activity["name"], "共同空堂")

    def test_entry_type_inferred_when_missing(self):
        entry = normalize_entry(
            make_raw_entry(entry_type=""), class_code="40620A", term="115-1"
        )
        self.assertEqual(entry.entry_type, ENTRY_COURSE)
        self.assertTrue(any("推斷" in w for w in entry.warnings))

    def test_course_ref_recovered_from_href(self):
        entry = normalize_entry(
            make_raw_entry(course_ref=None), class_code="40620A", term="115-1"
        )
        self.assertEqual(entry.course["code"], "40626610")
        self.assertTrue(any("course_href" in w for w in entry.warnings))

    def test_missing_fields_are_not_fabricated(self):
        entry = normalize_entry(
            make_raw_entry(teacher="", classroom=""), class_code="40620A", term="115-1"
        )
        self.assertEqual(entry.course["teacher"], "")
        self.assertEqual(entry.course["classroom"], "")


class NormalizeClassTests(unittest.TestCase):
    def test_class_metadata(self):
        snapshot = normalize_class(
            make_raw_class(), source=SourceRef(file="115-1_40620A.json")
        )
        self.assertEqual(snapshot.class_code, "40620A")
        self.assertEqual(snapshot.class_name, "四技資管二年甲班")
        self.assertEqual(snapshot.term, "115-1")
        self.assertEqual(snapshot.department, "406")
        self.assertEqual(snapshot.education_level, "4")

    def test_counts_split_course_and_activity(self):
        entries = [
            make_raw_entry(),
            make_raw_entry(
                entry_type="activity",
                course_name="",
                teacher="",
                classroom="",
                course_href="",
                course_ref=None,
                raw_text="共同空堂",
            ),
        ]
        snapshot = normalize_class(
            make_raw_class(entries=entries), source=SourceRef(file="x.json")
        )
        self.assertEqual(snapshot.counts, {"total": 2, "course": 1, "activity": 1})

    def test_missing_class_code_raises(self):
        raw = make_raw_class()
        raw["class"]["code"] = ""
        raw["request"]["class_code"] = ""
        with self.assertRaises(ValueError):
            normalize_class(raw, source=SourceRef(file="x.json"))


class OrmProjectionTests(unittest.TestCase):
    def build(self):
        entries = [
            make_raw_entry(),
            make_raw_entry(
                weekday=3,
                start_period=5,
                end_period=7,
                periods=[5, 6, 7],
                course_name="作業系統",
                teacher="林志宗",
                classroom="電703(承曦樓)",
                course_ref={
                    "academic_year": "115",
                    "semester": "1",
                    "class_code": "40620A",
                    "serial": "1",
                    "course_id": "40635210",
                },
            ),
            make_raw_entry(
                entry_type="activity",
                course_name="",
                teacher="",
                classroom="",
                course_href="",
                course_ref=None,
                raw_text="課外活動",
            ),
        ]
        snapshot = normalize_class(
            make_raw_class(entries=entries), source=SourceRef(file="x.json")
        )
        return to_orm_records([snapshot])

    def test_terms_teachers_courses_sections(self):
        records = self.build()
        self.assertEqual(records["terms"], [{"code": "115-1"}])
        self.assertEqual(
            sorted(t["name"] for t in records["teachers"]), sorted(["王大明", "林志宗"])
        )
        self.assertEqual(len(records["courses"]), 2)
        self.assertEqual(len(records["sections"]), 2)

    def test_activity_does_not_become_a_section(self):
        records = self.build()
        for section in records["sections"]:
            self.assertNotIn("課外活動", section["room"])

    def test_section_carries_period_range(self):
        records = self.build()
        by_code = {s["course"]: s for s in records["sections"]}
        section = by_code["40635210"]
        self.assertEqual(section["day"], 3)
        self.assertEqual(section["start_period"], 5)
        self.assertEqual(section["end_period"], 7)
        self.assertEqual(section["room"], "電703(承曦樓)")
        self.assertEqual(section["label"], "40620A")

    def test_no_enrollment_records_are_produced(self):
        records = self.build()
        self.assertNotIn("enrollments", records)
        self.assertNotIn("users", records)


class PayloadShapeTests(unittest.TestCase):
    def test_payload_marks_class_scope_and_no_enrollment(self):
        snapshot = normalize_class(make_raw_class(), source=SourceRef(file="x.json"))
        payload = build_payload([snapshot], generated_at="2026-10-30T00:00:00+00:00")
        self.assertEqual(payload["scope"], "class_timetable")
        self.assertFalse(payload["produces_enrollments"])
        self.assertNotIn("enrollments", payload)
        self.assertFalse(payload["classes"][0]["is_student_enrollment"])
        self.assertEqual(payload["classes"][0]["scope"], "class")

    def test_payload_totals(self):
        snapshot = normalize_class(make_raw_class(), source=SourceRef(file="x.json"))
        payload = build_payload([snapshot])
        self.assertEqual(payload["totals"]["class_count"], 1)
        self.assertEqual(payload["totals"]["entry_count"], 1)
        self.assertEqual(payload["totals"]["course_count"], 1)
        self.assertEqual(payload["totals"]["activity_count"], 0)


@unittest.skipUnless(SOURCE_DIR.is_dir(), f"來源目錄不存在：{SOURCE_DIR}")
class RealSourceTests(unittest.TestCase):
    """對真正的官方課表 JSON 做端到端檢查。"""

    @classmethod
    def setUpClass(cls):
        cls.payload = build_payload_from_dir(SOURCE_DIR)

    def test_two_class_snapshots(self):
        self.assertEqual(self.payload["totals"]["class_count"], 2)
        codes = [c["class_code"] for c in self.payload["classes"]]
        self.assertEqual(codes, ["40620A", "40630B"])

    def test_expected_course_and_activity_counts(self):
        by_code = {c["class_code"]: c for c in self.payload["classes"]}
        self.assertEqual(by_code["40630B"]["counts"]["course"], 5)
        self.assertEqual(by_code["40630B"]["counts"]["activity"], 2)
        self.assertEqual(by_code["40620A"]["counts"]["course"], 7)
        self.assertEqual(by_code["40620A"]["counts"]["activity"], 2)

    def test_sixteen_entries_total(self):
        self.assertEqual(self.payload["totals"]["entry_count"], 16)
        self.assertEqual(self.payload["totals"]["course_count"], 12)
        self.assertEqual(self.payload["totals"]["activity_count"], 4)

    def test_all_entries_are_115_1(self):
        for snapshot in self.payload["classes"]:
            self.assertEqual(snapshot["term_code"], "115-1")
            self.assertEqual(snapshot["academic_year"], "115")
            self.assertEqual(snapshot["semester"], "1")

    def test_every_entry_keeps_period_range_and_is_consecutive(self):
        for snapshot in self.payload["classes"]:
            for entry in snapshot["entries"]:
                self.assertGreaterEqual(entry["end_period"], entry["start_period"])
                self.assertEqual(
                    entry["periods"],
                    list(range(entry["start_period"], entry["end_period"] + 1)),
                )
                self.assertTrue(entry["is_consecutive"])

    def test_courses_carry_name_teacher_classroom(self):
        seen = 0
        for snapshot in self.payload["classes"]:
            for entry in snapshot["entries"]:
                if entry["entry_type"] != ENTRY_COURSE:
                    continue
                seen += 1
                self.assertTrue(entry["course"]["name"])
                self.assertTrue(entry["course"]["teacher"])
                self.assertTrue(entry["course"]["classroom"])
                self.assertTrue(entry["course"]["code"])
        self.assertEqual(seen, 12)

    def test_activities_have_no_course_payload(self):
        for snapshot in self.payload["classes"]:
            for entry in snapshot["entries"]:
                if entry["entry_type"] != ENTRY_ACTIVITY:
                    continue
                self.assertIsNone(entry["course"])
                self.assertTrue(entry["activity"]["name"])

    def test_orm_records_do_not_include_enrollments(self):
        records = self.payload["orm_records"]
        self.assertNotIn("enrollments", records)
        self.assertEqual(len(records["sections"]), 12)
        self.assertEqual(len(records["courses"]), 12)
        self.assertEqual(len(records["terms"]), 1)

    def test_no_source_data_is_invented(self):
        """每筆課程的資料都必須在原始 JSON 裡找得到。"""
        for snapshot in self.payload["classes"]:
            raw = json.loads(
                (SOURCE_DIR / snapshot["source"]["file"]).read_text(encoding="utf-8")
            )
            raw_courses = {
                (e.get("course_ref") or {}).get("course_id")
                for e in raw["entries"]
                if e["entry_type"] == "course"
            }
            for entry in snapshot["entries"]:
                if entry["entry_type"] != ENTRY_COURSE:
                    continue
                self.assertIn(entry["course"]["code"], raw_courses)


@unittest.skipUnless(OUTPUT_PATH.exists(), f"輸出檔不存在：{OUTPUT_PATH}")
class GeneratedOutputTests(unittest.TestCase):
    """檢查已產生的 data/ntub_import_ready/timetables.json。"""

    @classmethod
    def setUpClass(cls):
        cls.payload = json.loads(OUTPUT_PATH.read_text(encoding="utf-8"))

    def test_counts(self):
        totals = self.payload["totals"]
        self.assertEqual(totals["class_count"], 2)
        self.assertEqual(totals["entry_count"], 16)
        self.assertEqual(totals["course_count"], 12)
        self.assertEqual(totals["activity_count"], 4)

    def test_no_enrollment_key(self):
        self.assertFalse(self.payload["produces_enrollments"])
        self.assertNotIn("enrollments", self.payload)
        self.assertNotIn("enrollments", self.payload["orm_records"])

    def test_is_valid_json_with_chinese_unescaped(self):
        self.assertIn("四技資管", OUTPUT_PATH.read_text(encoding="utf-8"))
