from django.contrib import admin
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.test import RequestFactory, TestCase

from ..maintainer import grant_maintainer_access
from ..models import (
    AcademicCatalog,
    AcademicClassGroup,
    AcademicDepartment,
    AcademicProgramType,
    Course,
    Enrollment,
    NtubTimetableEntry,
    NtubTimetableSnapshot,
    OfficialCourseCredit,
    OfficialTeachingSyllabus,
    Section,
    Teacher,
    Term,
    Waitlist,
    CourseMaintainerScope,
    NtubOfferingSource,
)
from .session_helpers import force_admin_login


class AdminDeletionScopeTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.term = Term.objects.create(code="115-1")
        self.teacher = Teacher.objects.create(name="測試教師")
        catalog = AcademicCatalog.objects.create(version="admin-deletion")
        program = AcademicProgramType.objects.create(
            catalog=catalog, code="4", name_zh="四技日間部")
        self.department = AcademicDepartment.objects.create(
            catalog=catalog, program_type=program, code="406", name_zh="資訊管理系")
        self.other_department = AcademicDepartment.objects.create(
            catalog=catalog, program_type=program, code="500", name_zh="其他系所")
        self.class_group = AcademicClassGroup.objects.create(
            catalog=catalog, department=self.department, code="40630B",
            name_zh="資管三年乙班", grade_no=3)
        self.other_class_group = AcademicClassGroup.objects.create(
            catalog=catalog, department=self.other_department, code="50030A",
            name_zh="其他三年甲班", grade_no=3)
        self.maintainer = User.objects.create_user(
            "deletion-maintainer", password="password")
        CourseMaintainerScope.objects.create(
            user=self.maintainer, department=self.department)
        grant_maintainer_access(self.maintainer)
        force_admin_login(self.client, self.maintainer)
        self.factory = RequestFactory()
        self.request = self.factory.post("/admin/")
        self.request.user = self.maintainer

    def _course_with_section(self, department=None, code="DEL001"):
        course = Course.objects.create(
            term=self.term, owning_department=department or self.department,
            code=code, name="可刪除課程", teacher=self.teacher,
            created_by=self.maintainer)
        section = Section.objects.create(
            course=course, label="A", day=1, start_period=1, end_period=2,
            created_by=self.maintainer)
        return course, section

    def test_admin_add_records_creator_and_exposes_delete_action(self):
        response = self.client.post(
            "/admin/courses/course/add/",
            {
                "term": self.term.pk,
                "owning_department": self.department.pk,
                "code": "CREATED001",
                "name": "維護員建立課程",
                "teacher": self.teacher.pk,
                "credits": 3,
                "syllabus": "",
                "teaching_syllabus-TOTAL_FORMS": "1",
                "teaching_syllabus-INITIAL_FORMS": "0",
                "teaching_syllabus-MIN_NUM_FORMS": "0",
                "teaching_syllabus-MAX_NUM_FORMS": "1",
                "teaching_syllabus-0-id": "",
                "_save": "儲存",
            })
        self.assertEqual(response.status_code, 302)
        course = Course.objects.get(code="CREATED001")
        self.assertEqual(course.created_by_id, self.maintainer.pk)

        response = self.client.post(
            "/admin/courses/section/add/",
            {
                "course": course.pk,
                "label": "A",
                "day": 1,
                "start_period": 1,
                "end_period": 2,
                "room": "",
                "capacity": 40,
                "_save": "儲存",
            })
        self.assertEqual(response.status_code, 302)
        section = Section.objects.get(course=course)
        self.assertEqual(section.created_by_id, self.maintainer.pk)

        course_admin = admin.site._registry[Course]
        section_admin = admin.site._registry[Section]
        self.assertIn("delete_selected", course_admin.get_actions(self.request))
        self.assertIn("delete_selected", section_admin.get_actions(self.request))

    def test_legacy_creatorless_course_cannot_be_deleted_by_maintainer(self):
        course = Course.objects.create(
            term=self.term, owning_department=self.department,
            code="LEGACY001", name="舊課程", teacher=self.teacher)
        with self.assertRaises(PermissionDenied):
            admin.site._registry[Course].delete_model(self.request, course)
        self.assertTrue(Course.objects.filter(pk=course.pk).exists())

    def test_course_delete_requires_all_sections_to_be_maintainer_created(self):
        course, _ = self._course_with_section(code="DEL007")
        Section.objects.create(
            course=course, label="B", day=2, start_period=1, end_period=2)
        with self.assertRaises(PermissionDenied):
            admin.site._registry[Course].delete_model(self.request, course)
        self.assertTrue(Course.objects.filter(pk=course.pk).exists())

    def test_batch_delete_rejects_mixed_selection_without_partial_delete(self):
        allowed, _ = self._course_with_section(code="DEL008")
        other, _ = self._course_with_section(self.other_department, "DEL009")
        with self.assertRaises(PermissionDenied):
            admin.site._registry[Course].delete_queryset(
                self.request, Course.objects.filter(pk__in=[allowed.pk, other.pk]))
        self.assertTrue(Course.objects.filter(pk=allowed.pk).exists())
        self.assertTrue(Course.objects.filter(pk=other.pk).exists())

    def test_direct_admin_delete_post_cannot_bypass_creator_scope(self):
        other, _ = self._course_with_section(self.other_department, "DEL010")
        response = self.client.post(
            f"/admin/courses/course/{other.pk}/delete/",
            {"post": "yes"},
        )
        self.assertIn(response.status_code, (302, 403, 404))
        self.assertTrue(Course.objects.filter(pk=other.pk).exists())

    def test_batch_action_uses_creator_validation_before_deleting(self):
        allowed, _ = self._course_with_section(code="DEL011")
        legacy = Course.objects.create(
            term=self.term, owning_department=self.department,
            code="DEL012", name="舊課程", teacher=self.teacher)
        course_admin = admin.site._registry[Course]
        request = self.factory.post(
            "/admin/courses/course/",
            {"action": "delete_selected", "post": "1"},
        )
        request.user = self.maintainer
        with self.assertRaises(PermissionDenied):
            course_admin.delete_selected(
                request, Course.objects.filter(pk__in=[allowed.pk, legacy.pk]))
        self.assertTrue(Course.objects.filter(pk=allowed.pk).exists())
        self.assertTrue(Course.objects.filter(pk=legacy.pk).exists())

    def test_maintainer_can_delete_owned_course_and_section(self):
        course, section = self._course_with_section()
        section_admin = admin.site._registry[Section]
        section_admin.delete_model(self.request, section)
        self.assertFalse(Section.objects.filter(pk=section.pk).exists())
        course_admin = admin.site._registry[Course]
        course_admin.delete_model(self.request, course)
        self.assertFalse(Course.objects.filter(pk=course.pk).exists())

    def test_maintainer_cannot_delete_course_in_other_department(self):
        course, _ = self._course_with_section(self.other_department, "DEL002")
        course_admin = admin.site._registry[Course]
        with self.assertRaises(PermissionDenied):
            course_admin.delete_model(self.request, course)
        self.assertTrue(Course.objects.filter(pk=course.pk).exists())


    def test_enrollment_and_waitlist_protect_section_and_course(self):
        course, section = self._course_with_section(code="DEL003")
        enrolled_user = get_user_model().objects.create_user(
            "enrolled-user", password="password")
        Enrollment.objects.create(user=enrolled_user, section=section)
        section_admin = admin.site._registry[Section]
        with self.assertRaises(PermissionDenied):
            section_admin.delete_model(self.request, section)
        self.assertTrue(Section.objects.filter(pk=section.pk).exists())

        section.enrollments.all().delete()
        Waitlist.objects.create(user=enrolled_user, section=section)
        course_admin = admin.site._registry[Course]
        with self.assertRaises(PermissionDenied):
            course_admin.delete_model(self.request, course)
        self.assertTrue(Course.objects.filter(pk=course.pk).exists())

    def test_snapshot_with_history_cannot_be_deleted(self):
        snapshot = NtubTimetableSnapshot.objects.create(
            class_group=self.class_group, term=self.term, class_name="資管三乙")
        entry = NtubTimetableEntry.objects.create(
            snapshot=snapshot, entry_type="course", course_code="DEL004",
            course_name="課程", weekday=2, start_period=3, end_period=4)
        OfficialCourseCredit.objects.create(
            term=self.term, class_group=self.class_group,
            course_code="DEL004", credits=0)
        OfficialTeachingSyllabus.objects.create(
            academic_year="115", semester="1", class_code="40630B",
            official_serial="1", course_code="DEL004", status="success",
            snapshot=snapshot)

        snapshot_admin = admin.site._registry[NtubTimetableSnapshot]
        with self.assertRaises(PermissionDenied):
            snapshot_admin.delete_model(self.request, snapshot)

        self.assertTrue(NtubTimetableSnapshot.objects.filter(pk=snapshot.pk).exists())
        self.assertTrue(NtubTimetableEntry.objects.filter(pk=entry.pk).exists())
        self.assertTrue(OfficialCourseCredit.objects.filter(
            term=self.term, class_group=self.class_group,
            course_code="DEL004").exists())
        self.assertTrue(OfficialTeachingSyllabus.objects.filter(
            class_code="40630B", course_code="DEL004").exists())

    def test_snapshot_delete_keeps_other_class_and_term_syllabi(self):
        other_term = Term.objects.create(code="116-1")
        other_snapshot = NtubTimetableSnapshot.objects.create(
            class_group=self.class_group, term=other_term)
        other_entry = NtubTimetableEntry.objects.create(
            snapshot=other_snapshot, entry_type="course", course_code="DEL005",
            official_serial="2", weekday=3, start_period=1, end_period=2)
        target_snapshot = NtubTimetableSnapshot.objects.create(
            class_group=self.class_group, term=self.term)
        target_entry = NtubTimetableEntry.objects.create(
            snapshot=target_snapshot, entry_type="course", course_code="DEL005",
            official_serial="2", weekday=3, start_period=1, end_period=2)
        other_syllabus = OfficialTeachingSyllabus.objects.create(
            academic_year="116", semester="1", class_code="40630B",
            official_serial="2", course_code="DEL005", snapshot=other_snapshot)
        target_syllabus = OfficialTeachingSyllabus.objects.create(
            academic_year="115", semester="1", class_code="40630B",
            official_serial="2", course_code="DEL005", snapshot=target_snapshot)

        with self.assertRaises(PermissionDenied):
            admin.site._registry[NtubTimetableSnapshot].delete_queryset(
                self.request,
                NtubTimetableSnapshot.objects.filter(pk=target_snapshot.pk),
            )

        self.assertTrue(NtubTimetableEntry.objects.filter(pk=target_entry.pk).exists())
        self.assertTrue(OfficialTeachingSyllabus.objects.filter(
            pk=target_syllabus.pk).exists())
        self.assertTrue(NtubTimetableEntry.objects.filter(pk=other_entry.pk).exists())
        self.assertTrue(OfficialTeachingSyllabus.objects.filter(
            pk=other_syllabus.pk).exists())

    def test_delete_confirmation_lists_cascaded_syllabus_count(self):
        snapshot = NtubTimetableSnapshot.objects.create(
            class_group=self.class_group, term=self.term)
        OfficialTeachingSyllabus.objects.create(
            academic_year="115", semester="1", class_code="40630B",
            official_serial="3", course_code="DEL006", snapshot=snapshot)
        deleted, _, _, _ = admin.site._registry[
            NtubTimetableSnapshot].get_deleted_objects([snapshot], self.request)
        self.assertIn("官方教學大綱（1 份）", deleted)

    def test_maintainer_cannot_delete_unscoped_snapshot(self):
        snapshot = NtubTimetableSnapshot.objects.create(
            class_group=self.other_class_group, term=self.term)
        snapshot_admin = admin.site._registry[NtubTimetableSnapshot]
        with self.assertRaises(PermissionDenied):
            snapshot_admin.delete_model(self.request, snapshot)
        self.assertTrue(NtubTimetableSnapshot.objects.filter(pk=snapshot.pk).exists())
