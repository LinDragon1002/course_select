"""NTUB 官方開課標準化 payload 的測試。

測試分成兩類：
* 純邏輯測試：使用暫存目錄做出來的來源檔，不依賴外部爬蟲輸出。
* 整合測試：直接驗證本機來源目錄與已產出的 data/ntub_import_ready/offerings.json。
"""

import json
import shutil
import tempfile
from pathlib import Path

from django.test import SimpleTestCase

from courses.ntub_offerings_payload import (
    DEFAULT_CURRICULUM_FILE,
    DEFAULT_OFFERINGS_DIR,
    DEFAULT_OUTPUT_PATH,
    PayloadError,
    build_payload,
    collect_offerings,
    load_curriculum,
    match_recommendations,
    payload_without_timestamp,
    section_label,
    validate_payload,
    write_payload,
)

SOURCE_URL = "https://ntcbadm.ntub.edu.tw/pub/TchSchedule_Search.aspx"


def offering_doc(keyword, offerings, ambiguities=None, academic_year="115", semester="1"):
    return {
        "source_url": SOURCE_URL,
        "retrieved_at": "2026-10-29T02:00:00+00:00",
        "exported_at": "2026-10-29T02:00:00+00:00",
        "request": {"academic_year": academic_year, "semester": semester, "edu": "4",
                    "dept": "406", "class_value": "-1", "keyword": keyword},
        "echo": {"academic_year": academic_year, "semester": semester},
        "offerings": offerings,
        "stats": {},
        "ambiguities": ambiguities or [],
    }


def raw_offering(class_code="40620A", serial="21", course_code="40624660",
                 course_name="財務管理", class_name="四技資管二年甲班",
                 weekday=1, periods=(5, 6, 7), capacity=46, enrolled=33,
                 category="專選", teacher_id="0249", teacher_name="賴蓉禾"):
    return {
        "class_code": class_code,
        "class_name": class_name,
        "serial": serial,
        "course_code": course_code,
        "course_name": course_name,
        "teacher_raw": "%s %s" % (teacher_id, teacher_name),
        "teacher_id": teacher_id,
        "teacher_name": teacher_name,
        "schedule_raw": "105 106 107/A4607 電607(承曦樓)",
        "weekday": weekday,
        "periods": list(periods),
        "classroom_code": "A4607",
        "classroom": "A4607 電607(承曦樓)",
        "credits_raw": "3.0",
        "credits": 3.0,
        "hours_raw": "3.0",
        "hours": 3.0,
        "capacity_raw": "%s/%s" % (capacity, enrolled),
        "capacity": capacity,
        "enrolled_official": enrolled,
        "category": category,
        "course_href": "javascript:ConnectCos_Short('115','1','%s','%s','%s');"
                       % (class_code, serial, course_code),
        "course_ref": {"academic_year": "115", "semester": "1", "class_code": class_code,
                       "serial": serial, "course_id": course_code},
        "notes": [],
    }


CURRICULUM_114 = {
    "admission_year": "114", "track": "一般四技", "program_code": "4",
    "program_name": "四技日間部", "department_code": "406", "department_name": "資訊管理系",
    "grade": 2, "semester": 1, "class_code": "40620A", "course_name": "財務管理",
}

CURRICULUM_113 = {
    "admission_year": "113", "track": "技優領航專班", "program_code": "4",
    "program_name": "四技日間部", "department_code": "406", "department_name": "資訊管理系",
    "grade": 3, "semester": 1, "class_code": "40630B", "course_name": "資管個案研討",
}


class PayloadLogicTests(SimpleTestCase):
    """不依賴外部來源的純邏輯測試。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ntub_payload_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def write_source(self, name, doc):
        path = self.tmp / name
        path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        return path

    def write_curriculum(self, entries):
        path = self.tmp / "curriculum.json"
        path.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
        return path

    # -- 欄位保留 ---------------------------------------------------------------
    def test_offering_keeps_all_required_fields(self):
        self.write_source("115-1_財務管理.json", offering_doc("財務管理", [raw_offering()]))
        payload = build_payload(source_dir=self.tmp, curriculum_file=self.write_curriculum([]))

        self.assertEqual(len(payload["offerings"]), 1)
        offering = payload["offerings"][0]
        self.assertEqual(offering["academic_year"], "115")
        self.assertEqual(offering["semester"], "1")
        self.assertEqual(offering["term_code"], "115-1")
        self.assertEqual(offering["class_code"], "40620A")
        self.assertEqual(offering["serial"], "21")
        self.assertEqual(offering["course_code"], "40624660")
        self.assertEqual(offering["course_name"], "財務管理")
        self.assertEqual(offering["teacher"]["name"], "賴蓉禾")
        self.assertEqual(offering["schedule"]["weekday"], 1)
        self.assertEqual(offering["schedule"]["periods"], [5, 6, 7])
        self.assertEqual(offering["classroom"]["code"], "A4607")
        self.assertEqual(offering["credits"], 3.0)
        self.assertEqual(offering["hours"], 3.0)
        self.assertEqual(offering["elective_category"], "專選")
        self.assertEqual(offering["official_capacity"], 46)
        self.assertEqual(offering["official_enrolled"], 33)
        self.assertEqual(offering["source"]["keyword"], "財務管理")
        self.assertEqual(offering["source"]["source_url"], SOURCE_URL)

    def test_official_counts_are_historical_only(self):
        self.write_source("115-1_財務管理.json", offering_doc("財務管理", [raw_offering()]))
        payload = build_payload(source_dir=self.tmp, curriculum_file=self.write_curriculum([]))

        offering = payload["offerings"][0]
        self.assertTrue(offering["capacity_is_historical_only"])
        # 不得出現「本地選課人數」欄位：本地人數由 Enrollment 獨立統計。
        self.assertNotIn("enrolled", offering)
        self.assertIn("official_capacity", offering)
        self.assertIn("official_enrolled", offering)
        self.assertIn("official_capacity", payload["policy"])
        self.assertIn("local_enrollment", payload["policy"])

    # -- 去重 -------------------------------------------------------------------
    def test_same_offering_from_two_keywords_is_merged(self):
        doc_a = offering_doc("財務管理", [raw_offering()])
        doc_b = offering_doc("財務", [raw_offering()])
        self.write_source("a.json", doc_a)
        self.write_source("b.json", doc_b)

        payload = build_payload(source_dir=self.tmp, curriculum_file=self.write_curriculum([]))
        self.assertEqual(len(payload["offerings"]), 1)
        self.assertEqual(payload["stats"]["raw_offerings"], 2)
        self.assertEqual(payload["stats"]["unique_offerings"], 1)
        self.assertEqual(payload["stats"]["duplicates_merged"], 1)
        self.assertEqual(
            sorted(payload["offerings"][0]["matched_keywords"]), ["財務", "財務管理"]
        )

    def test_distinct_offerings_are_not_merged(self):
        doc = offering_doc("財務管理", [
            raw_offering(serial="21", course_code="40624660"),
            raw_offering(class_code="40620B", serial="13", course_code="40624670",
                         course_name="財務管理(二)(技優領航專班)", class_name="四技資管二年乙班",
                         capacity=42, enrolled=22),
        ])
        self.write_source("115-1_財務管理.json", doc)
        payload = build_payload(source_dir=self.tmp, curriculum_file=self.write_curriculum([]))
        self.assertEqual(len(payload["offerings"]), 2)
        self.assertEqual(payload["stats"]["duplicates_merged"], 0)

    # -- 課綱推薦 ---------------------------------------------------------------
    def test_recommendation_tracks_are_kept_separate(self):
        doc = offering_doc("資管個案研討", [
            raw_offering(class_code="40630A", serial="21", course_code="40635420",
                         course_name="資管個案研討", class_name="四技資管三年甲班",
                         weekday=2, periods=(2, 3, 4), capacity=45, enrolled=42),
        ])
        self.write_source("115-1_資管個案研討.json", doc)
        payload = build_payload(
            source_dir=self.tmp,
            curriculum_file=self.write_curriculum([CURRICULUM_113, CURRICULUM_114]),
        )

        recs = payload["curriculum_recommendations"]
        self.assertEqual(len(recs), 2)
        by_track = {r["track"]: r for r in recs}
        self.assertEqual(by_track["技優領航專班"]["admission_year"], "113")
        self.assertEqual(by_track["技優領航專班"]["class_code"], "40630B")
        self.assertEqual(by_track["一般四技"]["admission_year"], "114")
        self.assertEqual(by_track["一般四技"]["class_code"], "40620A")
        # 113 技優班與官方開課班別不同 → 仍是同一門課，但記為疑義。
        self.assertEqual(by_track["技優領航專班"]["match_status"], "course_name_only")
        self.assertEqual(by_track["一般四技"]["match_status"], "no_offering")
        self.assertEqual(payload["stats"]["recommendations_linked_other_class"], 1)
        self.assertEqual(payload["stats"]["recommendations_unlinked"], 1)
        kinds = {a["kind"] for a in payload["ambiguities"]}
        self.assertIn("recommendation_class_mismatch", kinds)
        self.assertIn("recommendation_without_offering", kinds)

    def test_matching_recommendation_links_to_offering(self):
        doc = offering_doc("財務管理", [raw_offering()])
        self.write_source("115-1_財務管理.json", doc)
        payload = build_payload(
            source_dir=self.tmp, curriculum_file=self.write_curriculum([CURRICULUM_114])
        )

        rec = payload["curriculum_recommendations"][0]
        self.assertEqual(rec["match_status"], "matched")
        self.assertEqual(rec["offering_key"], payload["offerings"][0]["key"])
        self.assertEqual(rec["course_code"], "40624660")

    # -- 財務管理 二技優領航專班 不得誤配 ---------------------------------------
    def test_financial_management_advanced_track_not_matched_to_plain(self):
        doc = offering_doc("財務管理", [
            raw_offering(serial="21", course_code="40624660", course_name="財務管理"),
            raw_offering(class_code="40620B", serial="13", course_code="40624670",
                         course_name="財務管理(二)(技優領航專班)",
                         class_name="四技資管二年乙班", capacity=42, enrolled=22),
        ])
        self.write_source("115-1_財務管理.json", doc)
        payload = build_payload(
            source_dir=self.tmp, curriculum_file=self.write_curriculum([CURRICULUM_114])
        )

        rec = payload["curriculum_recommendations"][0]
        self.assertEqual(rec["course_name"], "財務管理")
        self.assertEqual(rec["match_status"], "matched")
        linked = {o["course_code"]: o for o in payload["offerings"]}[rec["course_code"]]
        self.assertEqual(linked["course_code"], "40624660")
        self.assertEqual(linked["class_code"], "40620A")
        self.assertNotEqual(linked["course_name"], "財務管理(二)(技優領航專班)")

        # 技優領航班那筆不應被任何課綱推薦指到。
        advanced = [o for o in payload["offerings"] if o["course_code"] == "40624670"][0]
        linked_keys = {r["offering_key"] for r in payload["curriculum_recommendations"]}
        self.assertNotIn(advanced["key"], linked_keys)
        self.assertIn("offering_without_recommendation",
                      {a["kind"] for a in payload["ambiguities"]})

    def test_validate_rejects_cross_name_matching(self):
        self.write_source("115-1_財務管理.json", offering_doc("財務管理", [raw_offering()]))
        payload = build_payload(
            source_dir=self.tmp, curriculum_file=self.write_curriculum([CURRICULUM_114])
        )
        payload["curriculum_recommendations"][0]["offering_key"] = "bogus"
        with self.assertRaises(PayloadError):
            validate_payload(payload)

    def test_validate_rejects_name_mismatch(self):
        self.write_source("115-1_財務管理.json", offering_doc("財務管理", [raw_offering()]))
        payload = build_payload(
            source_dir=self.tmp, curriculum_file=self.write_curriculum([CURRICULUM_114])
        )
        payload["curriculum_recommendations"][0]["course_name"] = "財務管理(二)(技優領航專班)"
        with self.assertRaises(PayloadError):
            validate_payload(payload)

    # -- 其他 -------------------------------------------------------------------
    def test_section_label_from_class_code(self):
        self.assertEqual(section_label("40620A", "21"), "A")
        self.assertEqual(section_label("40620B", "13"), "B")
        self.assertEqual(section_label("40620", "13"), "13")

    def test_missing_source_directory_raises(self):
        with self.assertRaises(PayloadError):
            build_payload(source_dir=self.tmp / "nope", curriculum_file=self.write_curriculum([]))


class RealSourceIntegrationTests(SimpleTestCase):
    """直接驗證本機爬蟲輸出與已產出的 offerings.json。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.has_source = Path(DEFAULT_OFFERINGS_DIR).is_dir()
        cls.has_curriculum = Path(DEFAULT_CURRICULUM_FILE).is_file()
        cls.has_output = Path(DEFAULT_OUTPUT_PATH).is_file()

    def test_real_source_counts(self):
        if not (self.has_source and self.has_curriculum):
            self.skipTest("本機沒有 NTUB 來源資料")
        payload = build_payload()
        self.assertEqual(payload["stats"]["source_files"], 5)
        self.assertEqual(payload["stats"]["keywords"], 5)
        self.assertEqual(payload["stats"]["unique_offerings"], 6)
        self.assertEqual(payload["stats"]["curriculum_recommendations"], 6)
        self.assertEqual(payload["stats"]["duplicates_merged"], 0)
        self.assertFalse(payload["source"]["rescraped"])

    def test_real_source_offering_fields_present(self):
        if not (self.has_source and self.has_curriculum):
            self.skipTest("本機沒有 NTUB 來源資料")
        payload = build_payload()
        for offering in payload["offerings"]:
            for field in ("academic_year", "semester", "class_code", "serial", "course_code",
                          "course_name", "teacher", "schedule", "classroom", "credits", "hours",
                          "elective_category", "official_capacity", "source"):
                self.assertIn(field, offering)
            self.assertTrue(offering["schedule"]["periods"])
            self.assertGreaterEqual(offering["schedule"]["start_period"], 1)
            self.assertGreaterEqual(offering["schedule"]["end_period"],
                                    offering["schedule"]["start_period"])

    def test_real_source_financial_management_not_cross_matched(self):
        if not (self.has_source and self.has_curriculum):
            self.skipTest("本機沒有 NTUB 來源資料")
        payload = build_payload()
        recs = payload["curriculum_recommendations"]
        for rec in recs:
            if rec["course_name"] != "財務管理":
                continue
            self.assertEqual(rec["match_status"], "matched")
            offering = {o["key"]: o for o in payload["offerings"]}[rec["offering_key"]]
            self.assertEqual(offering["course_name"], "財務管理")
            self.assertEqual(offering["course_code"], "40624660")
            self.assertNotIn("技優", offering["course_name"])

    def test_committed_output_matches_current_source(self):
        if not (self.has_source and self.has_curriculum and self.has_output):
            self.skipTest("本機沒有 NTUB 來源資料或輸出檔")
        committed = json.loads(Path(DEFAULT_OUTPUT_PATH).read_text(encoding="utf-8"))
        rebuilt = build_payload()
        self.assertEqual(
            payload_without_timestamp(committed),
            payload_without_timestamp(rebuilt),
        )

    def test_output_file_is_valid_json(self):
        if not self.has_output:
            self.skipTest("沒有輸出檔")
        data = json.loads(Path(DEFAULT_OUTPUT_PATH).read_text(encoding="utf-8"))
        self.assertEqual(data["schema_version"], "1.0")
        self.assertEqual(len(data["offerings"]), 6)
        self.assertEqual(len(data["curriculum_recommendations"]), 6)
