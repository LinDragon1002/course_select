"""批次安排班級課程（維護員一次指派多門可選課程）的驗收測試。

涵蓋的驗收條件：

1. 單班三課批次建立三筆獨立政策，狀態一律 DRAFT。
2. 重複指派不產生重複資料，並回報已存在筆數。
3. 無權限者（未登入、學生、無授權範圍的維護員）GET / POST 都不行。
4. 偽造 Section ID：不會建立任何政策。
5. 偽造班級 ID：不會建立任何政策。
6. DRAFT 預設：不會自動發布。
7. 既有已發布政策不被覆寫（分類、狀態、確認者都不動）。
8. 不修改 Course / Section。
9. 其他系所未授權維護員不得把課程分配給 40630B。
"""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from ..maintainer import grant_maintainer_access
from ..models import (AcademicCatalog, AcademicClassGroup, AcademicDepartment,
                      AcademicProgramType, Course, CourseMaintainerScope, Section,
                      SectionClassPolicy, Term, Teacher)
from .session_helpers import force_admin_login

BULK_URL = "admin:courses_sectionclasspolicy_bulk_assign"


class BulkAssignmentTestBase(TestCase):
    """測試資料：115-1，三門課各自有開在 40620A 的開課班別。

    系所 A（資管）＝維護員負責的範圍；系所 B（另一個系所）＝未授權範圍。
    目標學生班級固定是 40630B，來源開課班別開在 40620A（跨班指派）。
    """

    def setUp(self):
        User = get_user_model()
        self.term = Term.objects.create(code="115-1")
        self.teacher = Teacher.objects.create(name="林俊杰")

        self.catalog = AcademicCatalog.objects.create(version="test-catalog")
        self.program_type = AcademicProgramType.objects.create(
            catalog=self.catalog, code="4", name_zh="四技日間部")
        self.dept_a = AcademicDepartment.objects.create(
            catalog=self.catalog, program_type=self.program_type, code="406",
            name_zh="資訊管理系")
        self.dept_b = AcademicDepartment.objects.create(
            catalog=self.catalog, program_type=self.program_type, code="500",
            name_zh="另一個系所")
        self.class_20a = AcademicClassGroup.objects.create(
            catalog=self.catalog, department=self.dept_a, code="40620A",
            name_zh="資管二年甲班", grade_no=2, is_registration_selectable=True)
        self.class_30b = AcademicClassGroup.objects.create(
            catalog=self.catalog, department=self.dept_a, code="40630B",
            name_zh="資管三年乙班", grade_no=3, is_registration_selectable=True)
        self.class_other = AcademicClassGroup.objects.create(
            catalog=self.catalog, department=self.dept_b, code="50020A",
            name_zh="他系二年甲班", grade_no=2)

        # 三門課各有一個開在 40620A 的開課班別（跨班指派的來源）。
        self.sections = []
        self.courses = []
        for index, (code, name) in enumerate([
            ("40637100", "伺服器架設與規劃"),
            ("40635420", "資管個案研討"),
            ("40627960", "網站應用程式設計"),
        ]):
            course = Course.objects.create(term=self.term, code=code, name=name,
                                           teacher=self.teacher)
            section = Section.objects.create(course=course, label=str(index + 1),
                                             day=index + 1, start_period=1, end_period=2,
                                             room="TA101")
            self.courses.append(course)
            self.sections.append(section)
            # 官方開課來源：官方曾針對 40620A 開課 → 這門課跟授權班級有關，
            # 因此維護員「有權管理」這個來源開課班別。
            from ..models import NtubOfferingSource
            NtubOfferingSource.objects.create(
                source_key=f"115|1|40620A|{index + 1}|{code}", academic_year="115",
                semester="1", class_code="40620A", official_serial=str(index + 1),
                course_code=code, term=self.term, class_group=self.class_20a,
                course=course, section=section, teacher=self.teacher, course_name=name)

        self.superuser = User.objects.create_superuser("root", email="root@example.com",
                                                       password="pw")
        self.maintainer = User.objects.create_user("maintainer", email="maint@ntub.edu.tw",
                                                   password="pw")
        self.student = User.objects.create_user("student", email="student@ntub.edu.tw",
                                                password="pw")
        self.other_maintainer = User.objects.create_user(
            "other-maint", email="other@ntub.edu.tw", password="pw")
        CourseMaintainerScope.objects.create(user=self.maintainer, department=self.dept_a)
        CourseMaintainerScope.objects.create(user=self.other_maintainer,
                                             department=self.dept_b)
        grant_maintainer_access(self.maintainer)
        grant_maintainer_access(self.other_maintainer)

    def post_bulk(self, sections, class_group=None, category="PROFESSIONAL_ELECTIVE",
                  can_enroll=True, term=None, follow=False):
        data = {
            "term": (term or self.term).pk,
            "class_group": (class_group or self.class_30b).pk,
            "sections": [s.pk for s in sections],
            "course_category": category,
        }
        if can_enroll:
            data["can_enroll"] = "on"
        return self.client.post(reverse(BULK_URL), data, follow=follow)


class BulkAssignmentFlowTests(BulkAssignmentTestBase):
    """驗收 1、6：單班三課批次建立，狀態一律 DRAFT。"""

    def setUp(self):
        super().setUp()
        force_admin_login(self.client, self.maintainer)

    def test_bulk_page_get_shows_selection_form(self):
        r = self.client.get(reverse(BULK_URL))
        self.assertEqual(r.status_code, 200)
        content = r.content.decode()
        self.assertIn("115-1", content)
        self.assertIn("40630B", content)

    def test_select_step_shows_sections_of_chosen_term(self):
        r = self.client.get(reverse(BULK_URL),
                            {"term": self.term.pk, "class_group": self.class_30b.pk})
        self.assertEqual(r.status_code, 200)
        content = r.content.decode()
        for section in self.sections:
            self.assertIn(section.course.code, content)
        self.assertIn("PROFESSIONAL_ELECTIVE", content)

    def test_select_step_cascade_only_exposes_authorized_classes(self):
        r = self.client.get(reverse(BULK_URL))
        self.assertEqual(r.status_code, 200)
        content = r.content.decode()
        self.assertIn("學制", content)
        self.assertIn("系所", content)
        self.assertIn("年級", content)
        self.assertIn("資管三年乙班（40630B）", content)
        self.assertNotIn("他系二年甲班（50020A）", content)

    def test_bulk_category_choices_are_electives_only(self):
        r = self.client.get(reverse(BULK_URL),
                            {"term": self.term.pk, "class_group": self.class_30b.pk})
        content = r.content.decode()
        self.assertIn("DEPARTMENT_ELECTIVE", content)
        self.assertIn("PROFESSIONAL_ELECTIVE", content)
        self.assertIn("GENERAL_ELECTIVE", content)
        self.assertNotIn("本班必修", content)
        self.assertNotIn("其他", content)

    def test_bulk_create_three_policies_for_one_class_group(self):
        r = self.post_bulk(self.sections)
        self.assertEqual(r.status_code, 302)
        policies = SectionClassPolicy.objects.filter(class_group=self.class_30b)
        self.assertEqual(policies.count(), 3)
        self.assertEqual(
            set(policies.values_list("section_id", flat=True)),
            {s.pk for s in self.sections})
        for policy in policies:
            self.assertEqual(policy.status, SectionClassPolicy.Status.DRAFT)
            self.assertEqual(policy.course_category, "PROFESSIONAL_ELECTIVE")
            self.assertTrue(policy.can_enroll)
            # 批次安排不等於發布：確認者與確認時間必須是空的。
            self.assertIsNone(policy.confirmed_by_id)
            self.assertIsNone(policy.confirmed_at)

    def test_each_elective_category_creates_draft_policy(self):
        categories = (
            "DEPARTMENT_ELECTIVE",
            "PROFESSIONAL_ELECTIVE",
            "GENERAL_ELECTIVE",
        )
        for section, category in zip(self.sections, categories):
            r = self.post_bulk([section], category=category)
            self.assertEqual(r.status_code, 302)
        self.assertEqual(
            set(SectionClassPolicy.objects.values_list("course_category", flat=True)),
            set(categories),
        )
        self.assertEqual(
            SectionClassPolicy.objects.filter(
                status=SectionClassPolicy.Status.DRAFT).count(), 3)

    def test_bulk_create_reports_counts(self):
        r = self.post_bulk(self.sections, follow=True)
        self.assertEqual(r.status_code, 200)
        content = r.content.decode()
        self.assertIn("建立 3 筆", content)
        self.assertIn("略過 0 筆", content)

    def test_bulk_page_link_on_changelist(self):
        r = self.client.get(reverse("admin:courses_sectionclasspolicy_changelist"))
        self.assertEqual(r.status_code, 200)
        self.assertIn(reverse(BULK_URL), r.content.decode())


class BulkAssignmentDuplicateTests(BulkAssignmentTestBase):
    """驗收 2、7：重複指派不產生重複資料，既有政策不被覆寫。"""

    def setUp(self):
        super().setUp()
        force_admin_login(self.client, self.maintainer)

    def test_repeat_assignment_does_not_duplicate(self):
        self.post_bulk(self.sections)
        self.assertEqual(SectionClassPolicy.objects.count(), 3)
        r = self.post_bulk(self.sections, follow=True)
        content = r.content.decode()
        self.assertEqual(SectionClassPolicy.objects.count(), 3)
        self.assertIn("略過 3 筆", content)
        self.assertIn("已存在", content)

    def test_existing_published_policy_is_not_overwritten(self):
        published = SectionClassPolicy.objects.create(
            section=self.sections[0], class_group=self.class_30b,
            course_category="REQUIRED", can_enroll=False,
            status=SectionClassPolicy.Status.PUBLISHED, confirmed_by=self.superuser)
        confirmed_at = published.confirmed_at
        self.post_bulk(self.sections, category="GENERAL_ELECTIVE", can_enroll=True)
        published.refresh_from_db()
        self.assertEqual(published.status, SectionClassPolicy.Status.PUBLISHED)
        self.assertEqual(published.course_category, "REQUIRED")
        self.assertFalse(published.can_enroll)
        self.assertEqual(published.confirmed_by_id, self.superuser.pk)
        self.assertEqual(published.confirmed_at, confirmed_at)
        # 其餘兩筆照常建立草稿
        self.assertEqual(SectionClassPolicy.objects.count(), 3)


class BulkAssignmentPermissionTests(BulkAssignmentTestBase):
    """驗收 3、9：權限一律在伺服器端檢查。"""

    def test_anonymous_cannot_get_bulk_page(self):
        r = self.client.get(reverse(BULK_URL))
        self.assertIn(r.status_code, (302, 403))
        self.assertEqual(SectionClassPolicy.objects.count(), 0)

    def test_anonymous_cannot_post_bulk(self):
        r = self.post_bulk(self.sections)
        self.assertIn(r.status_code, (302, 403))
        self.assertEqual(SectionClassPolicy.objects.count(), 0)

    def test_student_cannot_get_bulk_page(self):
        force_admin_login(self.client, self.student)
        r = self.client.get(reverse(BULK_URL))
        self.assertIn(r.status_code, (302, 403))

    def test_student_cannot_post_bulk(self):
        force_admin_login(self.client, self.student)
        r = self.post_bulk(self.sections)
        self.assertIn(r.status_code, (302, 403))
        self.assertEqual(SectionClassPolicy.objects.count(), 0)

    def test_maintainer_without_scope_is_denied(self):
        """沒有任何授權系所的維護員連頁面都開不了。"""
        from ..maintainer import revoke_maintainer_access
        scoped = CourseMaintainerScope.objects.filter(user=self.maintainer).first()
        scoped.is_active = False
        scoped.save(update_fields=["is_active"])
        force_admin_login(self.client, self.maintainer)
        r = self.client.get(reverse(BULK_URL))
        self.assertIn(r.status_code, (302, 403))
        r = self.post_bulk(self.sections)
        self.assertIn(r.status_code, (302, 403))
        self.assertEqual(SectionClassPolicy.objects.count(), 0)
        revoke_maintainer_access(self.maintainer)

    def test_other_department_maintainer_cannot_assign_to_our_class_group(self):
        """其他系所的維護員不得把課程分配給 40630B。"""
        force_admin_login(self.client, self.other_maintainer)
        r = self.post_bulk(self.sections, class_group=self.class_30b)
        self.assertNotEqual(r.status_code, 302)
        self.assertFalse(SectionClassPolicy.objects.filter(
            class_group=self.class_30b).exists())
        self.assertEqual(SectionClassPolicy.objects.count(), 0)


class BulkAssignmentForgedInputTests(BulkAssignmentTestBase):
    """驗收 4、5：偽造 Section ID / 班級 ID 一律無效。"""

    def setUp(self):
        super().setUp()
        force_admin_login(self.client, self.maintainer)

    def test_forged_section_id_creates_nothing(self):
        data = {
            "term": self.term.pk,
            "class_group": self.class_30b.pk,
            "sections": [999999],
            "course_category": "PROFESSIONAL_ELECTIVE",
        }
        r = self.client.post(reverse(BULK_URL), data)
        self.assertNotEqual(r.status_code, 302)
        self.assertEqual(SectionClassPolicy.objects.count(), 0)

    def test_forged_section_id_mixed_with_valid_creates_nothing(self):
        """混在合法班別中間的偽造 id 也不能讓整批通過。"""
        data = {
            "term": self.term.pk,
            "class_group": self.class_30b.pk,
            "sections": [self.sections[0].pk, 999999],
            "course_category": "PROFESSIONAL_ELECTIVE",
        }
        r = self.client.post(reverse(BULK_URL), data)
        self.assertNotEqual(r.status_code, 302)
        self.assertEqual(SectionClassPolicy.objects.count(), 0)

    def test_forged_class_group_id_creates_nothing(self):
        r = self.post_bulk(self.sections, class_group=self.class_other)
        self.assertNotEqual(r.status_code, 302)
        self.assertFalse(SectionClassPolicy.objects.filter(
            class_group=self.class_other).exists())
        self.assertEqual(SectionClassPolicy.objects.count(), 0)

    def test_forbidden_categories_create_nothing(self):
        for category in ("REQUIRED", "OTHER", "UNKNOWN"):
            with self.subTest(category=category):
                r = self.post_bulk(self.sections, category=category)
                self.assertNotEqual(r.status_code, 302)
                self.assertEqual(SectionClassPolicy.objects.count(), 0)


class BulkAssignmentDoesNotTouchSourceTests(BulkAssignmentTestBase):
    """驗收 8：批次安排不會動到 Course / Section（也不會自動發布）。"""

    def setUp(self):
        super().setUp()
        force_admin_login(self.client, self.maintainer)

    def test_course_and_section_are_not_modified(self):
        before_courses = {c.pk: (c.code, c.name, c.term_id, c.teacher_id, c.credits)
                          for c in Course.objects.all()}
        before_sections = {s.pk: (s.label, s.day, s.start_period, s.end_period, s.room,
                                  s.capacity, s.course_id)
                           for s in Section.objects.all()}
        self.post_bulk(self.sections)
        after_courses = {c.pk: (c.code, c.name, c.term_id, c.teacher_id, c.credits)
                         for c in Course.objects.all()}
        after_sections = {s.pk: (s.label, s.day, s.start_period, s.end_period, s.room,
                                 s.capacity, s.course_id)
                          for s in Section.objects.all()}
        self.assertEqual(before_courses, after_courses)
        self.assertEqual(before_sections, after_sections)
        self.assertEqual(Course.objects.count(), 3)
        self.assertEqual(Section.objects.count(), 3)

    def test_bulk_create_does_not_publish(self):
        self.post_bulk(self.sections)
        self.assertFalse(SectionClassPolicy.objects.filter(
            status=SectionClassPolicy.Status.PUBLISHED).exists())
        self.assertTrue(SectionClassPolicy.objects.filter(
            status=SectionClassPolicy.Status.DRAFT).exists())
