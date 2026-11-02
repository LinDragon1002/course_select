from django.contrib.auth import get_user_model
from django.test import TestCase

from ..credits import credit_limits, summarize_credits
from ..models import (AcademicCatalog, AcademicClassGroup, AcademicDepartment,
                      AcademicProgramType, Course, Enrollment,
                      NtubTimetableEntry, NtubTimetableSnapshot, Section,
                      Teacher, Term, OfficialTeachingSyllabus)


class CreditSummaryTests(TestCase):
    def setUp(self):
        self.term = Term.objects.create(code="115-1", is_current=True)
        self.teacher = Teacher.objects.create(name="T")
        catalog = AcademicCatalog.objects.create(version="credits")
        program = AcademicProgramType.objects.create(
            catalog=catalog, code="4", name_zh="四技日間部")
        department = AcademicDepartment.objects.create(
            catalog=catalog, program_type=program, code="406", name_zh="資管系")
        self.class_20a = AcademicClassGroup.objects.create(
            catalog=catalog, department=department, code="40620A",
            name_zh="資管二年甲班", grade_no=2)
        self.class_30b = AcademicClassGroup.objects.create(
            catalog=catalog, department=department, code="40630B",
            name_zh="資管三年乙班", grade_no=3)
        self.class_40a = AcademicClassGroup.objects.create(
            catalog=catalog, department=department, code="40640A",
            name_zh="資管四年甲班", grade_no=4)

    def official_entry(self, class_group, code, name=None, day=1, start=1):
        snapshot = NtubTimetableSnapshot.objects.get_or_create(
            class_group=class_group, term=self.term)[0]
        return NtubTimetableEntry.objects.create(
            snapshot=snapshot, entry_type="course", course_code=code,
            course_name=name or code, weekday=day, start_period=start,
            end_period=start + 1)

    def course_section(self, code, credits=3):
        course = Course.objects.create(
            term=self.term, code=code, name=code, teacher=self.teacher,
            credits=credits)
        return Section.objects.create(
            course=course, label="A", day=5, start_period=1, end_period=2)

    def test_four_year_program_limits_by_grade(self):
        self.assertEqual(credit_limits(self.class_30b).minimum, 16)
        self.assertEqual(credit_limits(self.class_30b).maximum, 25)
        self.assertEqual(credit_limits(self.class_40a).minimum, 9)

    def test_unconfigured_program_has_no_default_limits(self):
        self.class_30b.department.program_type.code = "3"
        self.class_30b.department.program_type.save(update_fields=["code"])
        self.assertIsNone(credit_limits(self.class_30b))

    def test_zero_credit_and_duplicate_slots_count_once(self):
        self.official_entry(self.class_30b, "PE301")
        self.official_entry(self.class_30b, "PE301", day=2)
        self.official_entry(self.class_30b, "OS301")
        snapshot = NtubTimetableSnapshot.objects.get(
            class_group=self.class_30b, term=self.term)
        NtubTimetableEntry.objects.create(
            snapshot=snapshot, entry_type="activity", course_name="共同空堂",
            weekday=3, start_period=1, end_period=1)
        for code, credits in (("PE301", 0), ("OS301", 3)):
            OfficialTeachingSyllabus.objects.create(
                academic_year="115", semester="1", class_code="40630B",
                official_serial="", course_code=code,
                snapshot=snapshot, credits=credits, status="success")
        summary = summarize_credits(
            self.class_30b, self.term,
            official_entries=NtubTimetableEntry.objects.filter(
                snapshot__class_group=self.class_30b))
        self.assertEqual(summary.official_confirmed, 3)
        self.assertTrue(summary.complete)

    def test_enrolled_course_overlapping_official_is_not_double_counted(self):
        self.official_entry(self.class_30b, "OS301")
        section = self.course_section("OS301", credits=3)
        user = get_user_model().objects.create_user("student")
        Enrollment.objects.create(user=user, section=section)
        summary = summarize_credits(
            self.class_30b, self.term, [section],
            NtubTimetableEntry.objects.filter(
                snapshot__class_group=self.class_30b))
        self.assertEqual(summary.total, 2)
        self.assertEqual(summary.enrolled, 3)

    def test_official_course_without_syllabus_uses_period_estimate(self):
        self.official_entry(self.class_30b, "OS301")
        summary = summarize_credits(
            self.class_30b, self.term,
            official_entries=NtubTimetableEntry.objects.filter(
                snapshot__class_group=self.class_30b))
        self.assertTrue(summary.complete)
        self.assertEqual(summary.total, 2)
        self.assertEqual(summary.pending_course_codes, ())

    def test_each_class_uses_its_own_confirmations(self):
        self.official_entry(self.class_20a, "A20")
        self.official_entry(self.class_30b, "A30")
        self.assertEqual(summarize_credits(
            self.class_20a, self.term,
            official_entries=NtubTimetableEntry.objects.filter(
                snapshot__class_group=self.class_20a)).total, 2)
        self.assertEqual(summarize_credits(
            self.class_30b, self.term,
            official_entries=NtubTimetableEntry.objects.filter(
                snapshot__class_group=self.class_30b)).total, 2)
