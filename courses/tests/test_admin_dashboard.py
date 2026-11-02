from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from .session_helpers import force_admin_login

from ..admin_navigation import ADMIN_MENU
from ..maintainer import (MAINTAINER_GROUP_NAME, grant_maintainer_access,
                          scoped_departments)
from ..models import (AcademicCatalog, AcademicClassGroup, AcademicDepartment,
                      AcademicProgramType, Course, CourseMaintainerScope,
                      Section, SectionClassPolicy, Term, Teacher)


class AdminDashboardTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.term = Term.objects.create(code="115-1", is_current=True)
        teacher = Teacher.objects.create(name="T")
        catalog = AcademicCatalog.objects.create(version="dashboard")
        program = AcademicProgramType.objects.create(
            catalog=catalog, code="4", name_zh="四技日間部")
        dept_a = AcademicDepartment.objects.create(
            catalog=catalog, program_type=program, code="406", name_zh="資管系")
        dept_b = AcademicDepartment.objects.create(
            catalog=catalog, program_type=program, code="500", name_zh="他系")
        other_program = AcademicProgramType.objects.create(
            catalog=catalog, code="M", name_zh="碩士班")
        self.dept_c = AcademicDepartment.objects.create(
            catalog=catalog, program_type=other_program, code="M0L",
            name_zh="資管碩士班")
        self.group_a = AcademicClassGroup.objects.create(
            catalog=catalog, department=dept_a, code="40630B",
            name_zh="資管三年乙班", grade_no=3)
        group_b = AcademicClassGroup.objects.create(
            catalog=catalog, department=dept_b, code="50030A",
            name_zh="他系三年甲班", grade_no=3)
        self.course_a = Course.objects.create(
            term=self.term, code="A100", name="資管課", teacher=teacher)
        course_b = Course.objects.create(
            term=self.term, code="B100", name="他系課", teacher=teacher)
        self.section_a = Section.objects.create(
            course=self.course_a, label="A", day=1, start_period=1, end_period=2)
        section_b = Section.objects.create(
            course=course_b, label="A", day=2, start_period=1, end_period=2)
        SectionClassPolicy.objects.create(
            section=self.section_a, class_group=self.group_a,
            course_category="DEPARTMENT_ELECTIVE")
        SectionClassPolicy.objects.create(
            section=section_b, class_group=group_b,
            course_category="DEPARTMENT_ELECTIVE")
        self.superuser = User.objects.create_superuser(
            "dashboard-root", "root@example.com", "password")
        self.maintainer = User.objects.create_user("dashboard-maintainer")
        CourseMaintainerScope.objects.create(
            user=self.maintainer, department=dept_a)
        grant_maintainer_access(self.maintainer)

    def test_dashboard_shows_database_statistics_and_actions(self):
        force_admin_login(self.client, self.superuser)
        response = self.client.get(reverse("admin:index"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["dashboard_stats"]["course_count"], 2)
        self.assertContains(response, "管理總覽")
        self.assertContains(response, "批次安排課程")
        self.assertContains(response, "資管課")

    def test_maintainer_dashboard_is_scoped(self):
        force_admin_login(self.client, self.maintainer)
        response = self.client.get(reverse("admin:index"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["dashboard_stats"]["course_count"], 1)
        self.assertNotContains(response, "他系課")
        self.assertNotContains(response, "50030A")

    def test_navigation_categories_are_in_required_order(self):
        self.assertEqual(
            [item["text"] for item in ADMIN_MENU],
            ["管理總覽", "課程規劃", "班級固定課表",
             "歷史匯入與來源追蹤", "學籍與班級", "選課與候補", "系統管理"])

    def test_superuser_can_assign_cross_program_departments_in_one_form(self):
        force_admin_login(self.client, self.superuser)
        response = self.client.post(
            reverse("admin:courses_coursemaintainerscope_add"),
            {
                "user": self.maintainer.pk,
                "departments": [self.group_a.department_id, self.dept_c.pk],
                "is_active": "on",
                "_save": "Save",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            CourseMaintainerScope.objects.filter(user=self.maintainer).count(), 2
        )
        self.assertEqual(
            set(scoped_departments(self.maintainer).values_list("code", flat=True)),
            {"406", "M0L"},
        )

    def test_disabling_one_scope_keeps_other_scope_access(self):
        force_admin_login(self.client, self.superuser)
        scope_c = CourseMaintainerScope.objects.create(
            user=self.maintainer, department=self.dept_c)
        grant_maintainer_access(self.maintainer)
        scope_a = CourseMaintainerScope.objects.get(
            user=self.maintainer, department=self.group_a.department)
        response = self.client.post(
            reverse("admin:courses_coursemaintainerscope_changelist"),
            {
                "action": "revoke_selected_scopes",
                "_selected_action": [str(scope_a.pk)],
            },
        )
        self.assertEqual(response.status_code, 302)
        scope_a.refresh_from_db()
        scope_c.refresh_from_db()
        self.maintainer.refresh_from_db()
        self.assertFalse(scope_a.is_active)
        self.assertTrue(scope_c.is_active)
        self.assertTrue(self.maintainer.is_staff)
        self.assertTrue(
            self.maintainer.groups.filter(name=MAINTAINER_GROUP_NAME).exists()
        )
        self.assertEqual(
            list(scoped_departments(self.maintainer).values_list("code", flat=True)),
            ["M0L"],
        )

    def test_scope_form_uses_searchable_four_level_picker(self):
        force_admin_login(self.client, self.superuser)
        response = self.client.get(
            reverse("admin:courses_coursemaintainerscope_add")
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="scope-search"')
        self.assertContains(response, 'id="scope-program"')
        self.assertContains(response, 'id="scope-department"')
        self.assertContains(response, 'id="scope-grade"')
        self.assertContains(response, 'id="scope-class"')
        self.assertContains(response, "實際授權單位仍是整個 AcademicDepartment")

    def test_scope_form_rejects_empty_department_selection(self):
        force_admin_login(self.client, self.superuser)
        response = self.client.post(
            reverse("admin:courses_coursemaintainerscope_add"),
            {"user": self.maintainer.pk, "departments": [], "is_active": "on"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "請至少選取一個授權系所")
        self.assertEqual(
            CourseMaintainerScope.objects.filter(user=self.maintainer).count(), 1
        )
