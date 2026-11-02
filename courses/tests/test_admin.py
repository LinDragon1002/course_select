from django.contrib import admin
from django.contrib.auth import get_user_model
from django.test import RequestFactory
from django.urls import reverse

from ..models import (AcademicCatalog, AcademicClassGroup, AcademicDepartment,
                      AcademicProgramType, Course, NtubTimetableEntry,
                      NtubTimetableSnapshot, OfficialCourseCredit, Teacher, Term)
from .base import CourseTestCase
from .session_helpers import force_admin_login


class PrereqLimitTests(CourseTestCase):
    """先修課至多 2 門；走 admin 的新增與編輯頁面。"""

    def setUp(self):
        super().setUp()
        self.client.force_login(get_user_model().objects.create_superuser("root", password="pw"))

        force_admin_login(
            self.client,
            get_user_model().objects.get(username="root"),
        )

    def course_data(self, code, *prereq_codes):
        catalog, _ = AcademicCatalog.objects.get_or_create(version="prereq-admin")
        program, _ = AcademicProgramType.objects.get_or_create(
            catalog=catalog, code="4",
            defaults={"name_zh": "四技日間部"},
        )
        department, _ = AcademicDepartment.objects.get_or_create(
            catalog=catalog, program_type=program, code="406",
            defaults={"name_zh": "資管系"},
        )
        return {"term": self.term.pk, "owning_department": department.pk,
                "code": code, "name": "測試課", "teacher": self.teacher.pk,
                "credits": 3, "syllabus": "",
                "prerequisites": [self.course(c).pk for c in prereq_codes],
                "teaching_syllabus-TOTAL_FORMS": "1",
                "teaching_syllabus-INITIAL_FORMS": "0",
                "teaching_syllabus-MIN_NUM_FORMS": "0",
                "teaching_syllabus-MAX_NUM_FORMS": "1",
                "teaching_syllabus-0-id": ""}

    def test_add_course_with_two_prereqs_saves(self):
        r = self.client.post(reverse("admin:courses_course_add"), self.course_data("CS999", "CS101", "CS202"))
        self.assertEqual(r.status_code, 302)
        self.assertEqual(Course.objects.get(term=self.term, code="CS999").prerequisites.count(), 2)

    def test_add_course_with_three_prereqs_is_rejected(self):
        r = self.client.post(reverse("admin:courses_course_add"),
                             self.course_data("CS999", "CS101", "CS202", "CS303"))
        self.assertContains(r, "先修課程最多設 2 門。")
        self.assertFalse(Course.objects.filter(term=self.term, code="CS999").exists())

    def test_edit_course_to_three_prereqs_is_rejected(self):
        target = self.course("CS999")
        target.prerequisites.set([self.course("CS101"), self.course("CS202")])
        r = self.client.post(reverse("admin:courses_course_change", args=[target.pk]),
                             self.course_data("CS999", "CS101", "CS202", "CS303"))
        self.assertContains(r, "先修課程最多設 2 門。")
        self.assertEqual(target.prerequisites.count(), 2)


class OfficialCourseCreditAdminTests(CourseTestCase):
    def setUp(self):
        super().setUp()
        catalog = AcademicCatalog.objects.create(version="admin-credit")
        program = AcademicProgramType.objects.create(
            catalog=catalog, code="4", name_zh="四技日間部")
        department = AcademicDepartment.objects.create(
            catalog=catalog, program_type=program, code="406", name_zh="資管系")
        self.class_group = AcademicClassGroup.objects.create(
            catalog=catalog, department=department, code="40630B",
            name_zh="資管三年乙班", grade_no=3)
        self.snapshot = NtubTimetableSnapshot.objects.create(
            class_group=self.class_group, term=self.term)
        self.entry = NtubTimetableEntry.objects.create(
            snapshot=self.snapshot, entry_type="course", course_code="PE301",
            course_name="體育三（上）", weekday=1, start_period=1, end_period=2)
        self.admin_user = get_user_model().objects.create_superuser(
            "credit-admin", "credit@example.com", "password")
        force_admin_login(self.client, self.admin_user)

    def test_course_credit_is_read_only_and_uses_timetable_periods(self):
        model_admin = admin.site._registry[NtubTimetableEntry]
        request = RequestFactory().get("/")
        request.user = self.admin_user
        form_class = model_admin.get_form(request, self.entry)
        form = form_class(instance=self.entry)
        self.assertNotIn("confirmed_credits", form.fields)
        self.assertNotIn("credit_source_note", form.fields)
        self.assertEqual(model_admin.course_credit_display(self.entry), 2)
        self.assertEqual(model_admin.credit_basis_display(self.entry), "節次暫估")

    def test_adding_entry_can_create_credit_in_same_save(self):
        model_admin = admin.site._registry[NtubTimetableEntry]
        request = RequestFactory().get("/")
        request.user = self.admin_user
        form_class = model_admin.get_form(request)
        form = form_class(data={
            "snapshot": self.snapshot.pk,
            "entry_type": "course",
            "course_code": "OS301",
            "official_serial": "",
            "course_name": "作業系統",
            "teacher_name": "",
            "classroom": "",
            "weekday": 1,
            "start_period": 3,
            "end_period": 4,
            "periods": "[]",
            "raw_text": "",
            "course_href": "",
        })
        self.assertTrue(form.is_valid(), form.errors)
        entry = form.save(commit=False)
        model_admin.save_model(request, entry, form, change=False)
        self.assertTrue(NtubTimetableEntry.objects.filter(
            snapshot=self.snapshot, course_code="OS301").exists())

    def test_sync_official_page_handles_class_without_grade(self):
        AcademicClassGroup.objects.create(
            catalog=self.class_group.catalog,
            department=self.class_group.department,
            code="406XX",
            name_zh="未設定年級班級",
            grade_no=None,
        )
        response = self.client.get(
            reverse("admin:courses_ntubtimetablesnapshot_sync_official"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "未設定年級")
