"""NTUB 學籍目錄匯入的測試。

來源 SQL 是本專案的還原檔（ntubook_academic_missing_rows_restore.sql），
測試直接讀它來驗證筆數、關聯與可重複執行。
"""

import unittest
from pathlib import Path

from django.core.management import call_command
from django.test import TestCase

from ..management.commands.import_ntub_catalog import (DEFAULT_SOURCE,
                                                       parse_sql_inserts)
from ..models import (AcademicCatalog, AcademicClassGroup, AcademicDepartment,
                       AcademicProgramType)

SOURCE = Path(DEFAULT_SOURCE)

EXPECTED_PROGRAM_TYPES = 11
EXPECTED_DEPARTMENTS = 74
EXPECTED_CLASS_GROUPS = 254
EXPECTED_SELECTABLE = 218


def source_available():
    return SOURCE.exists()


class NtubCatalogImportTests(TestCase):
    """完整匯入來源 SQL，檢查筆數與關聯。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if not source_available():
            raise unittest.SkipTest(f"找不到來源 SQL：{SOURCE}")

    @classmethod
    def setUpTestData(cls):
        call_command("import_ntub_catalog", "--file", str(SOURCE), verbosity=0)

    def test_catalog_batch_created(self):
        catalog = AcademicCatalog.objects.get()
        self.assertEqual(catalog.version, "ntub-20260724-755fe94f")
        self.assertEqual(catalog.source_key, "ntub")
        self.assertEqual(catalog.source_file_name, "ntub_classes_20260724.json")
        self.assertEqual(catalog.class_group_count, EXPECTED_CLASS_GROUPS)
        self.assertEqual(catalog.selectable_class_group_count, EXPECTED_SELECTABLE)

    def test_row_counts(self):
        self.assertEqual(AcademicProgramType.objects.count(), EXPECTED_PROGRAM_TYPES)
        self.assertEqual(AcademicDepartment.objects.count(), EXPECTED_DEPARTMENTS)
        self.assertEqual(AcademicClassGroup.objects.count(), EXPECTED_CLASS_GROUPS)

    def test_selectable_class_groups(self):
        selectable = AcademicClassGroup.objects.filter(is_registration_selectable=True)
        self.assertEqual(selectable.count(), EXPECTED_SELECTABLE)

    def test_program_types(self):
        codes = set(AcademicProgramType.objects.values_list("code", flat=True))
        self.assertEqual(codes, {"1", "2", "3", "4", "7", "E", "F", "G", "O", "R", "S"})
        master = AcademicProgramType.objects.get(code="1")
        self.assertEqual(master.name_zh, "碩士班")

    def test_department_belongs_to_program_type(self):
        department = AcademicDepartment.objects.get(code="406")
        self.assertEqual(department.name_zh, "四技資訊管理系")
        self.assertEqual(department.program_type.code, "4")
        self.assertEqual(department.program_type.name_zh, "四技日間部")
        self.assertEqual(department.unit_type, "ACADEMIC")

    def test_class_groups_link_to_department(self):
        for code in ("40620A", "40630B"):
            group = AcademicClassGroup.objects.get(code=code)
            self.assertEqual(group.department.code, "406")
            self.assertEqual(group.department.program_type.code, "4")

    def test_class_group_fields(self):
        group = AcademicClassGroup.objects.get(code="40620A")
        self.assertEqual(group.name_zh, "四技資管二年甲班")
        self.assertEqual(group.grade_no, 2)
        self.assertEqual(group.section_code, "A")
        self.assertEqual(group.group_type, "COHORT")
        self.assertTrue(group.is_registration_selectable)

        group_b = AcademicClassGroup.objects.get(code="40630B")
        self.assertEqual(group_b.name_zh, "四技資管三年乙班")
        self.assertEqual(group_b.grade_no, 3)
        self.assertEqual(group_b.section_code, "B")

    def test_source_payload_preserved(self):
        group = AcademicClassGroup.objects.get(code="40620A")
        payload = group.source_payload
        self.assertEqual(payload["cls_val"], "40620A")
        self.assertEqual(payload["dept_val"], "406")
        self.assertEqual(payload["edu_val"], "4")
        self.assertEqual(payload["cls_text"], "四技資管二年甲班")

    def test_non_selectable_class_groups(self):
        """通識 / 體育 / 軍訓班不開放選課。"""
        general = AcademicClassGroup.objects.filter(group_type="GENERAL_EDUCATION")
        self.assertTrue(general.exists())
        self.assertFalse(any(g.is_registration_selectable for g in general))
        physical = AcademicClassGroup.objects.filter(group_type="PHYSICAL_EDUCATION")
        self.assertTrue(physical.exists())
        self.assertFalse(any(g.is_registration_selectable for g in physical))


class NtubCatalogIdempotencyTests(TestCase):
    """重複匯入不會新增重複資料。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if not source_available():
            raise unittest.SkipTest(f"找不到來源 SQL：{SOURCE}")

    def test_reimport_creates_no_duplicates(self):
        call_command("import_ntub_catalog", "--file", str(SOURCE), verbosity=0)
        first = (
            AcademicProgramType.objects.count(),
            AcademicDepartment.objects.count(),
            AcademicClassGroup.objects.count(),
            AcademicCatalog.objects.count(),
        )
        call_command("import_ntub_catalog", "--file", str(SOURCE), verbosity=0)
        second = (
            AcademicProgramType.objects.count(),
            AcademicDepartment.objects.count(),
            AcademicClassGroup.objects.count(),
            AcademicCatalog.objects.count(),
        )
        self.assertEqual(first, second)
        self.assertEqual(
            second,
            (EXPECTED_PROGRAM_TYPES, EXPECTED_DEPARTMENTS, EXPECTED_CLASS_GROUPS, 1),
        )

    def test_reimport_updates_in_place(self):
        call_command("import_ntub_catalog", "--file", str(SOURCE), verbosity=0)
        group = AcademicClassGroup.objects.get(code="40620A")
        group.name_zh = "舊名稱"
        group.save(update_fields=["name_zh"])

        call_command("import_ntub_catalog", "--file", str(SOURCE), verbosity=0)

        group.refresh_from_db()
        self.assertEqual(group.name_zh, "四技資管二年甲班")


class NtubSqlParserTests(TestCase):
    """來源 SQL 的剖析器。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if not source_available():
            raise unittest.SkipTest(f"找不到來源 SQL：{SOURCE}")
        cls.tables = parse_sql_inserts(SOURCE.read_text(encoding="utf-8"))

    def test_table_row_counts(self):
        self.assertEqual(len(self.tables["program_types"]), EXPECTED_PROGRAM_TYPES)
        self.assertEqual(len(self.tables["departments"]), EXPECTED_DEPARTMENTS)
        self.assertEqual(len(self.tables["class_groups"]), EXPECTED_CLASS_GROUPS)

    def test_values_parsed_correctly(self):
        row = next(r for r in self.tables["program_types"] if r["code"] == "1")
        self.assertEqual(row["name_zh"], "碩士班")
        self.assertIsNone(row["name_en"])
        self.assertEqual(row["is_registration_selectable"], 1)

    def test_source_payload_is_json_string(self):
        row = next(r for r in self.tables["class_groups"] if r["code"] == "40620A")
        self.assertIn("cls_val", row["source_payload"])
        self.assertEqual(row["grade_no"], 2)
        self.assertEqual(row["section_code"], "A")


class ExistingModelsUnchangedTests(TestCase):
    """原本的 Course / Section / Enrollment 不受影響。"""

    def test_core_models_still_work(self):
        from django.contrib.auth import get_user_model

        from ..models import Course, Enrollment, Section, Teacher, Term

        term = Term.objects.create(code="114-1", is_current=True)
        teacher = Teacher.objects.create(name="王大明")
        course = Course.objects.create(term=term, code="CS101", name="程式設計", teacher=teacher)
        section = Section.objects.create(course=course, label="A", day=1, start_period=1, end_period=3)
        user = get_user_model().objects.create_user("u")
        Enrollment.objects.create(user=user, section=section)

        self.assertEqual(Course.objects.count(), 1)
        self.assertEqual(Section.objects.count(), 1)
        self.assertEqual(Enrollment.objects.count(), 1)
        self.assertEqual(section.enrolled_count, 1)
