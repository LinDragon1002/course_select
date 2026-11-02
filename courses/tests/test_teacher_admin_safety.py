from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.contrib.messages.storage.fallback import FallbackStorage
from django.core.exceptions import PermissionDenied
from django.test import RequestFactory, TestCase

from ..models import (
    AcademicCatalog,
    AcademicClassGroup,
    AcademicDepartment,
    AcademicProgramType,
    Course,
    NtubOfferingSource,
    Section,
    Teacher,
    Term,
)
from ..maintainer import MAINTAINER_GROUP_NAME
from ..teacher_access import SYLLABUS_TEACHER_GROUP_NAME
from .session_helpers import force_admin_login


class TeacherAdminSafetyTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.admin_user = User.objects.create_superuser(
            "teacher-admin-root", email="root@example.test", password="test-pass",
        )
        self.term = Term.objects.create(code="116-1")
        self.teacher = Teacher.objects.create(name="待測教師")
        self.request = RequestFactory().post("/admin/")
        self.request.user = self.admin_user
        self.request.session = self.client.session
        self.request._messages = FallbackStorage(self.request)
        self.teacher_admin = admin.site._registry[Teacher]

    def _create_course_history(self, teacher=None):
        teacher = teacher or self.teacher
        course = Course.objects.create(
            term=self.term, code="TST001", name="歷史課程", teacher=teacher,
        )
        section = Section.objects.create(
            course=course, label="A", day=1, start_period=1, end_period=2,
        )
        catalog = AcademicCatalog.objects.create(version="teacher-delete-test")
        program = AcademicProgramType.objects.create(
            catalog=catalog, code="4", name_zh="四技日間部",
        )
        department = AcademicDepartment.objects.create(
            catalog=catalog, program_type=program, code="406",
            name_zh="資訊管理系",
        )
        class_group = AcademicClassGroup.objects.create(
            catalog=catalog, department=department, code="40630B",
            name_zh="資管三年乙班", grade_no=3,
        )
        source = NtubOfferingSource.objects.create(
            source_key="116|1|40630B|1|TST001",
            academic_year="116", semester="1", class_code="40630B",
            official_serial="1", course_code="TST001", term=self.term,
            class_group=class_group, course=course, section=section,
            teacher=teacher, course_name="歷史課程",
        )
        return course, section, source

    def test_superuser_can_delete_teacher_without_related_records(self):
        teacher = Teacher.objects.create(name="無關聯教師")

        self.assertIn("delete_selected", self.teacher_admin.get_actions(self.request))
        self.teacher_admin.delete_model(self.request, teacher)

        self.assertFalse(Teacher.objects.filter(pk=teacher.pk).exists())

    def test_delete_preview_explains_protected_official_sources(self):
        course, section, source = self._create_course_history()

        _, _, perms_needed, protected = self.teacher_admin.get_deleted_objects(
            [self.teacher], self.request,
        )

        self.assertNotIn(NtubOfferingSource._meta.verbose_name, perms_needed)
        self.assertTrue(any("官方開課來源 1 筆" in str(item) for item in protected))
        with self.assertRaisesMessage(PermissionDenied, "正式課程 1 筆"):
            self.teacher_admin.delete_model(self.request, self.teacher)
        self.assertTrue(Teacher.objects.filter(pk=self.teacher.pk).exists())
        self.assertTrue(Course.objects.filter(pk=course.pk).exists())
        self.assertTrue(Section.objects.filter(pk=section.pk).exists())
        self.assertTrue(NtubOfferingSource.objects.filter(pk=source.pk).exists())
        force_admin_login(self.client, self.admin_user)
        response = self.client.get(
            f"/admin/courses/teacher/{self.teacher.pk}/delete/",
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "官方開課來源 1 筆")

    def test_batch_delete_is_atomic_when_a_teacher_has_history(self):
        safe_teacher = Teacher.objects.create(name="可刪除教師")
        course, _, _ = self._create_course_history()

        with self.assertRaisesMessage(PermissionDenied, "批次刪除已取消"):
            self.teacher_admin.delete_queryset(
                self.request,
                Teacher.objects.filter(pk__in=[safe_teacher.pk, self.teacher.pk]),
            )

        self.assertTrue(Teacher.objects.filter(pk=safe_teacher.pk).exists())
        self.assertTrue(Teacher.objects.filter(pk=self.teacher.pk).exists())
        self.assertTrue(Course.objects.filter(pk=course.pk).exists())

    def test_superuser_can_batch_delete_unreferenced_teachers(self):
        teachers = [
            Teacher.objects.create(name="未連結教師甲"),
            Teacher.objects.create(name="未連結教師乙"),
        ]

        self.teacher_admin.delete_queryset(
            self.request, Teacher.objects.filter(pk__in=[item.pk for item in teachers]),
        )

        self.assertFalse(
            Teacher.objects.filter(pk__in=[item.pk for item in teachers]).exists(),
        )

    def test_deactivation_unlinks_account_without_deleting_course_history(self):
        account = get_user_model().objects.create_user(
            "syllabus-teacher-account", is_staff=True,
        )
        account.groups.add(Group.objects.create(name=SYLLABUS_TEACHER_GROUP_NAME))
        self.teacher.user = account
        self.teacher.save(update_fields=["user"])

        with self.assertRaisesMessage(PermissionDenied, "教師後台帳號 1 筆"):
            self.teacher_admin.delete_model(self.request, self.teacher)

        course, _, source = self._create_course_history()
        self.teacher_admin.deactivate_and_unlink_teacher_accounts(
            self.request, Teacher.objects.filter(pk=self.teacher.pk),
        )

        account.refresh_from_db()
        self.teacher.refresh_from_db()
        self.assertFalse(account.is_active)
        self.assertIsNone(self.teacher.user_id)
        self.assertTrue(Course.objects.filter(pk=course.pk).exists())
        self.assertTrue(NtubOfferingSource.objects.filter(pk=source.pk).exists())

    def test_user_admin_displays_real_roles_and_keeps_group_editing(self):
        User = get_user_model()
        maintainer_group = Group.objects.create(name=MAINTAINER_GROUP_NAME)
        teacher_group = Group.objects.create(name=SYLLABUS_TEACHER_GROUP_NAME)
        maintainer = User.objects.create_user(
            "role-maintainer", is_staff=True, password="test-pass",
        )
        maintainer.groups.add(maintainer_group)
        teacher = User.objects.create_user(
            "role-teacher", is_staff=True, password="test-pass",
        )
        teacher.groups.add(teacher_group)
        regular = User.objects.create_user("role-regular", password="test-pass")
        user_admin = admin.site._registry[User]

        self.assertEqual(user_admin.system_role(self.admin_user), "超級管理員")
        self.assertEqual(user_admin.system_role(maintainer), "課程維護員")
        self.assertEqual(user_admin.system_role(teacher), "任課教師")
        self.assertEqual(user_admin.system_role(regular), "一般使用者")

        force_admin_login(self.client, self.admin_user)
        response = self.client.get(f"/admin/auth/user/{teacher.pk}/change/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("groups", response.context["adminform"].form.fields)

    def test_maintainer_and_teacher_cannot_delete_teachers_or_official_sources(self):
        User = get_user_model()
        maintainer = User.objects.create_user(
            "delete-maintainer", is_staff=True, password="test-pass",
        )
        maintainer.groups.add(Group.objects.create(name=MAINTAINER_GROUP_NAME))
        syllabus_teacher = User.objects.create_user(
            "delete-syllabus-teacher", is_staff=True, password="test-pass",
        )
        syllabus_teacher.groups.add(
            Group.objects.create(name=SYLLABUS_TEACHER_GROUP_NAME),
        )
        source_admin = admin.site._registry[NtubOfferingSource]
        request = RequestFactory().post("/admin/")

        for user in (maintainer, syllabus_teacher):
            request.user = user
            self.assertFalse(self.teacher_admin.has_delete_permission(request))
            self.assertFalse(source_admin.has_delete_permission(request))

        request.user = maintainer
        with self.assertRaises(PermissionDenied):
            self.teacher_admin.deactivate_and_unlink_teacher_accounts(
                request, Teacher.objects.filter(pk=self.teacher.pk),
            )
