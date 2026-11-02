"""課程維護員角色與「跨班修課政策」的驗收測試。

涵蓋的驗收條件：

1. 同一個 Section 對不同班級有不同修課屬性。
2. 兩個政策不產生重複 Course / Section。
3. 一般學生不能建立或發布政策。
4. 課程維護員只能管理獲授權系所。
5. 維護員不得自行提升權限。
6. DRAFT 不可視為正式開放加選。
7. course_name_only 不會自動取得開放權限。
8. 既有 OAuth、學生學籍與課表功能不受影響（由既有測試與本檔的檢查共同覆蓋）。
9. 既有測試全部保留。
"""

import json
from io import StringIO

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from ..maintainer import (MAINTAINER_GROUP_NAME, grant_maintainer_access, is_maintainer,
                          published_enrollment_policy)
from ..models import (AcademicCatalog, AcademicClassGroup, AcademicDepartment,
                      AcademicProgramType, Course, CourseMaintainerScope, Enrollment,
                      NtubCurriculumRecommendation, NtubOfferingAmbiguity,
                      NtubOfferingSource, Section, SectionClassPolicy, Term, Teacher)
from .session_helpers import force_admin_login


class MaintainerTestBase(TestCase):
    """測試資料：115-1 網站應用程式設計，實際開課班級 40620A。

    系所 A（資管）＝維護員負責的範圍；系所 B（另一個系所）＝未授權範圍。
    """

    def setUp(self):
        User = get_user_model()
        self.term = Term.objects.create(code="115-1")
        self.teacher = Teacher.objects.create(name="林俊杰")
        self.course = Course.objects.create(term=self.term, code="40627960",
                                            name="網站應用程式設計", teacher=self.teacher)
        self.section = Section.objects.create(course=self.course, label="19", day=4,
                                              start_period=2, end_period=4, room="TA202")

        self.catalog = AcademicCatalog.objects.create(version="test-catalog")
        self.program_type = AcademicProgramType.objects.create(
            catalog=self.catalog, code="4", name_zh="四技日間部")
        self.dept_a = AcademicDepartment.objects.create(
            catalog=self.catalog, program_type=self.program_type, code="406", name_zh="資訊管理系")
        self.dept_b = AcademicDepartment.objects.create(
            catalog=self.catalog, program_type=self.program_type, code="500", name_zh="另一個系所")
        self.class_20a = AcademicClassGroup.objects.create(
            catalog=self.catalog, department=self.dept_a, code="40620A",
            name_zh="資管二年甲班", grade_no=2, is_registration_selectable=True)
        self.class_30b = AcademicClassGroup.objects.create(
            catalog=self.catalog, department=self.dept_a, code="40630B",
            name_zh="資管三年乙班", grade_no=3, is_registration_selectable=True)
        self.class_other = AcademicClassGroup.objects.create(
            catalog=self.catalog, department=self.dept_b, code="50020A",
            name_zh="他系二年甲班", grade_no=2)

        # 官方開課來源：這門課是針對 40620A 開的（唯讀，維護員不能改）
        self.offering = NtubOfferingSource.objects.create(
            source_key="115|1|40620A|19|40627960", academic_year="115", semester="1",
            class_code="40620A", official_serial="19", course_code="40627960",
            term=self.term, class_group=self.class_20a, course=self.course,
            section=self.section, teacher=self.teacher, course_name="網站應用程式設計")

        self.superuser = User.objects.create_superuser("root", email="root@example.com",
                                                       password="pw")
        self.maintainer = User.objects.create_user("maintainer", email="maint@ntub.edu.tw",
                                                   password="pw")
        self.student = User.objects.create_user("student", email="student@ntub.edu.tw",
                                                password="pw")
        # superuser 在 Admin 指派權限範圍（等同 CourseMaintainerScopeAdmin.save_model）
        self.scope = CourseMaintainerScope.objects.create(user=self.maintainer,
                                                          department=self.dept_a)
        grant_maintainer_access(self.maintainer)

    def policy_data(self, class_group, category="OTHER", can_enroll=False, status="DRAFT"):
        return {
            "section": self.section.pk,
            "class_group": class_group.pk,
            "course_category": category,
            "can_enroll": "on" if can_enroll else "",
            "status": status,
            "notes": "",
            "_save": "Save",
        }


class CrossClassPolicyTests(MaintainerTestBase):
    """驗收 1、2：同一個 Section 對不同班級有不同政策，而且不產生重複課程。"""

    def test_same_section_has_two_policies_for_two_class_groups(self):
        required = SectionClassPolicy.objects.create(
            section=self.section, class_group=self.class_20a,
            course_category=SectionClassPolicy.CourseCategory.REQUIRED, can_enroll=False)
        elective = SectionClassPolicy.objects.create(
            section=self.section, class_group=self.class_30b,
            course_category=SectionClassPolicy.CourseCategory.PROFESSIONAL_ELECTIVE,
            can_enroll=True)

        self.assertEqual(SectionClassPolicy.objects.filter(section=self.section).count(), 2)
        self.assertEqual(required.course_category, "REQUIRED")
        self.assertFalse(required.can_enroll)
        self.assertEqual(elective.course_category, "PROFESSIONAL_ELECTIVE")
        self.assertTrue(elective.can_enroll)
        # 兩條政策指向同一個 Section
        self.assertEqual(required.section_id, elective.section_id)

    def test_policies_do_not_create_duplicate_course_or_section(self):
        SectionClassPolicy.objects.create(
            section=self.section, class_group=self.class_20a,
            course_category="REQUIRED", can_enroll=False)
        SectionClassPolicy.objects.create(
            section=self.section, class_group=self.class_30b,
            course_category="PROFESSIONAL_ELECTIVE", can_enroll=True)
        self.assertEqual(Course.objects.filter(code="40627960").count(), 1)
        self.assertEqual(Section.objects.filter(course=self.course).count(), 1)
        self.assertEqual(Section.objects.count(), 1)

    def test_term_comes_from_section_course(self):
        policy = SectionClassPolicy.objects.create(section=self.section,
                                                   class_group=self.class_30b)
        self.assertEqual(policy.term, self.term)
        self.assertFalse(hasattr(policy, "_term_id"))

    def test_section_class_group_pair_is_unique(self):
        SectionClassPolicy.objects.create(section=self.section, class_group=self.class_30b)
        with self.assertRaises(Exception):
            SectionClassPolicy.objects.create(section=self.section, class_group=self.class_30b)

    def test_policy_creation_creates_no_enrollment(self):
        SectionClassPolicy.objects.create(
            section=self.section, class_group=self.class_30b,
            course_category="PROFESSIONAL_ELECTIVE", can_enroll=True, status="PUBLISHED")
        self.assertEqual(Enrollment.objects.count(), 0)


class StudentCannotManagePolicyTests(MaintainerTestBase):
    """驗收 3：一般學生不能建立或發布政策。"""

    def setUp(self):
        super().setUp()
        force_admin_login(self.client, self.student)

    def test_student_cannot_open_policy_admin(self):
        r = self.client.get(reverse("admin:courses_sectionclasspolicy_add"))
        self.assertIn(r.status_code, (302, 403))
        self.assertEqual(SectionClassPolicy.objects.count(), 0)

    def test_student_cannot_post_policy(self):
        r = self.client.post(reverse("admin:courses_sectionclasspolicy_add"),
                             self.policy_data(self.class_30b, can_enroll=True))
        self.assertIn(r.status_code, (302, 403))
        self.assertEqual(SectionClassPolicy.objects.count(), 0)

    def test_student_cannot_publish_policy(self):
        policy = SectionClassPolicy.objects.create(section=self.section,
                                                   class_group=self.class_30b, can_enroll=True)
        r = self.client.post(reverse("admin:courses_sectionclasspolicy_changelist"),
                             {"action": "publish_selected_policies",
                              "_selected_action": [str(policy.pk)]})
        self.assertIn(r.status_code, (302, 403))
        policy.refresh_from_db()
        self.assertEqual(policy.status, "DRAFT")
        self.assertIsNone(policy.confirmed_by_id)

    def test_student_cannot_open_admin_index(self):
        r = self.client.get(reverse("admin:index"))
        self.assertEqual(r.status_code, 302)


class NtubEmailGrantsNothingTests(TestCase):
    """驗收 5 / 需求一：@ntub.edu.tw 信箱不會自動取得管理權限。"""

    def test_ntub_email_user_has_no_admin_rights(self):
        user = get_user_model().objects.create_user("s2", email="s2@ntub.edu.tw", password="pw")
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)
        self.assertFalse(user.groups.exists())
        force_admin_login(self.client, user)
        r = self.client.get(reverse("admin:index"))
        self.assertEqual(r.status_code, 302)


class MaintainerScopeTests(MaintainerTestBase):
    """驗收 4：維護員只能管理獲授權系所的資料。"""

    def setUp(self):
        super().setUp()
        force_admin_login(self.client, self.maintainer)

    def test_maintainer_can_open_admin_and_policy_list(self):
        self.assertEqual(self.client.get(reverse("admin:index")).status_code, 200)
        self.assertEqual(
            self.client.get(reverse("admin:courses_sectionclasspolicy_changelist")).status_code, 200)

    def test_maintainer_can_create_policy_for_own_department(self):
        r = self.client.post(reverse("admin:courses_sectionclasspolicy_add"),
                             self.policy_data(self.class_30b, category="PROFESSIONAL_ELECTIVE",
                                              can_enroll=True))
        self.assertEqual(r.status_code, 302)
        policy = SectionClassPolicy.objects.get(section=self.section, class_group=self.class_30b)
        self.assertEqual(policy.course_category, "PROFESSIONAL_ELECTIVE")
        self.assertTrue(policy.can_enroll)

    def test_maintainer_cannot_create_policy_for_other_department(self):
        r = self.client.post(reverse("admin:courses_sectionclasspolicy_add"),
                             self.policy_data(self.class_other, can_enroll=True))
        self.assertNotEqual(r.status_code, 302)
        self.assertFalse(SectionClassPolicy.objects.filter(class_group=self.class_other).exists())
        # 表單明確擋在「適用班級」欄位上
        self.assertIn("class_group", r.context["adminform"].form.errors)

    def test_maintainer_cannot_open_other_department_policy(self):
        policy = SectionClassPolicy.objects.create(section=self.section,
                                                   class_group=self.class_other)
        r = self.client.get(reverse("admin:courses_sectionclasspolicy_change", args=[policy.pk]))
        self.assertNotEqual(r.status_code, 200)
        self.assertIn(r.status_code, (302, 403, 404))

    def test_maintainer_cannot_move_policy_to_other_department(self):
        policy = SectionClassPolicy.objects.create(section=self.section,
                                                   class_group=self.class_30b)
        r = self.client.post(reverse("admin:courses_sectionclasspolicy_change", args=[policy.pk]),
                             self.policy_data(self.class_other))
        self.assertNotEqual(r.status_code, 302)
        policy.refresh_from_db()
        self.assertEqual(policy.class_group_id, self.class_30b.pk)

    def test_maintainer_cannot_delete_other_department_policy(self):
        policy = SectionClassPolicy.objects.create(section=self.section,
                                                   class_group=self.class_other)
        r = self.client.post(reverse("admin:courses_sectionclasspolicy_delete", args=[policy.pk]),
                             {"post": "yes"})
        self.assertIn(r.status_code, (302, 403, 404))
        self.assertTrue(SectionClassPolicy.objects.filter(pk=policy.pk).exists())

    def test_class_group_list_only_shows_own_department(self):
        r = self.client.get(reverse("admin:courses_academicclassgroup_changelist"))
        codes = set(r.context["cl"].result_list.values_list("code", flat=True))
        self.assertIn("40620A", codes)
        self.assertIn("40630B", codes)
        self.assertNotIn("50020A", codes)

    def test_class_group_autocomplete_only_returns_own_department(self):
        r = self.client.get(reverse("admin:autocomplete"), {
            "app_label": "courses", "model_name": "sectionclasspolicy",
            "field_name": "class_group", "term": "406"})
        self.assertEqual(r.status_code, 200)
        payload = json.loads(r.content.decode())
        texts = [item["text"] for item in payload["results"]]
        self.assertTrue(any("40630B" in text for text in texts))
        self.assertFalse(any("50020A" in text for text in texts))

    def test_section_autocomplete_only_returns_related_sections(self):
        other_course = Course.objects.create(term=self.term, code="99999999",
                                              name="別系課", teacher=self.teacher)
        other_section = Section.objects.create(course=other_course, label="1", day=1,
                                               start_period=1, end_period=2)
        r = self.client.get(reverse("admin:autocomplete"), {
            "app_label": "courses", "model_name": "sectionclasspolicy",
            "field_name": "section", "term": "40627960"})
        self.assertEqual(r.status_code, 200)
        payload = json.loads(r.content.decode())
        values = {int(item["id"]) for item in payload["results"]}
        self.assertIn(self.section.pk, values)
        self.assertNotIn(other_section.pk, values)

    def test_offering_source_list_is_scoped(self):
        r = self.client.get(reverse("admin:courses_ntubofferingsource_changelist"))
        keys = set(r.context["cl"].result_list.values_list("source_key", flat=True))
        self.assertIn(self.offering.source_key, keys)

    def test_offering_source_cannot_be_modified(self):
        r = self.client.post(reverse("admin:courses_ntubofferingsource_change",
                                     args=[self.offering.pk]),
                             {"source_key": "forged", "course_code": "00000000",
                              "course_name": "改掉", "academic_year": "115", "semester": "1",
                              "class_code": "40620A", "official_serial": "19",
                              "term": self.term.pk, "class_group": self.class_20a.pk,
                              "course": self.course.pk, "section": self.section.pk,
                              "teacher": self.teacher.pk})
        self.assertEqual(r.status_code, 403)
        self.offering.refresh_from_db()
        self.assertEqual(self.offering.source_key, "115|1|40620A|19|40627960")
        self.assertEqual(self.offering.course_code, "40627960")


class MaintainerCannotEscalateTests(MaintainerTestBase):
    """驗收 5：維護員不能自己加權限、不能改自己的權限範圍。"""

    def setUp(self):
        super().setUp()
        force_admin_login(self.client, self.maintainer)

    def test_maintainer_is_not_superuser(self):
        self.assertFalse(self.maintainer.is_superuser)
        self.assertTrue(self.maintainer.is_staff)
        self.assertTrue(is_maintainer(self.maintainer))

    def test_maintainer_cannot_open_scope_admin(self):
        r = self.client.get(reverse("admin:courses_coursemaintainerscope_add"))
        self.assertEqual(r.status_code, 403)
        r = self.client.get(reverse("admin:courses_coursemaintainerscope_changelist"))
        self.assertEqual(r.status_code, 403)

    def test_maintainer_cannot_add_scope(self):
        r = self.client.post(reverse("admin:courses_coursemaintainerscope_add"),
                             {"user": self.maintainer.pk, "department": self.dept_b.pk,
                              "is_active": "on", "_save": "Save"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(CourseMaintainerScope.objects.count(), 1)
        self.assertFalse(CourseMaintainerScope.objects.filter(user=self.maintainer,
                                                              department=self.dept_b).exists())

    def test_maintainer_cannot_edit_user_permissions(self):
        r = self.client.post(reverse("admin:auth_user_change", args=[self.maintainer.pk]),
                             {"username": "maintainer", "is_staff": "on", "is_superuser": "on",
                              "groups": [Group.objects.get_or_create(
                                  name=MAINTAINER_GROUP_NAME)[0].pk]})
        self.assertIn(r.status_code, (403, 302))
        self.maintainer.refresh_from_db()
        self.assertFalse(self.maintainer.is_superuser)

    def test_maintainer_cannot_touch_student_data_models(self):
        for url_name in ("admin:courses_enrollment_changelist",
                         "admin:courses_waitlist_changelist",
                         "admin:courses_studentacademicprofile_changelist",
                         "admin:auth_user_changelist",
                         "admin:auth_group_changelist",
                         "admin:sites_site_changelist",
                         "admin:socialaccount_socialaccount_changelist",
                         "admin:socialaccount_socialapp_changelist",
                         "admin:account_emailaddress_changelist"):
            r = self.client.get(reverse(url_name))
            self.assertEqual(r.status_code, 403, msg=url_name)

    def test_maintainer_group_has_no_sensitive_permissions(self):
        group = Group.objects.get(name=MAINTAINER_GROUP_NAME)
        self.assertFalse(group.permissions.filter(
            content_type__app_label__in=["auth", "socialaccount"]).exists())
        for codename in ("add_enrollment", "change_enrollment", "view_enrollment",
                         "add_waitlist", "change_waitlist", "view_waitlist",
                         "add_coursemaintainerscope", "change_coursemaintainerscope",
                         "delete_coursemaintainerscope"):
            self.assertFalse(group.permissions.filter(
                content_type__app_label="courses", codename=codename).exists(),
                msg=codename)


class DraftIsNotOpenTests(MaintainerTestBase):
    """驗收 6：DRAFT 不算正式開放加選。"""

    def test_draft_policy_is_not_open(self):
        policy = SectionClassPolicy.objects.create(
            section=self.section, class_group=self.class_30b,
            course_category="PROFESSIONAL_ELECTIVE", can_enroll=True, status="DRAFT")
        self.assertFalse(policy.is_published)
        self.assertFalse(policy.allows_enrollment)
        self.assertIsNone(published_enrollment_policy(self.section, self.class_30b))

    def test_published_but_not_can_enroll_is_not_open(self):
        policy = SectionClassPolicy.objects.create(
            section=self.section, class_group=self.class_20a,
            course_category="REQUIRED", can_enroll=False, status="PUBLISHED")
        self.assertTrue(policy.is_published)
        self.assertFalse(policy.allows_enrollment)
        self.assertIsNotNone(published_enrollment_policy(self.section, self.class_20a))

    def test_published_and_can_enroll_is_open(self):
        policy = SectionClassPolicy.objects.create(
            section=self.section, class_group=self.class_30b,
            course_category="PROFESSIONAL_ELECTIVE", can_enroll=True, status="PUBLISHED")
        self.assertTrue(policy.allows_enrollment)
        self.assertIsNotNone(published_enrollment_policy(self.section, self.class_30b))


class CourseNameOnlyRecommendationTests(MaintainerTestBase):
    """驗收 7：course_name_only 疑義不會自動變成開放加選。"""

    def setUp(self):
        super().setUp()
        self.recommendation = NtubCurriculumRecommendation.objects.create(
            admission_year="113", track="技優領航專班", program_code="4",
            program_name="四技日間部", department_code="406", department_name="資訊管理系",
            grade=3, semester="1", class_code="40630B", course_name="網站應用程式設計",
            match_status="course_name_only", matched_offering_class_code="40620A",
            matched_course_code="40627960", offering=self.offering, course=self.course,
            class_group=self.class_30b, is_ambiguous=True,
            ambiguity_message="推薦的班別與官方開課班別不同")
        self.ambiguity = NtubOfferingAmbiguity.objects.create(
            kind="recommendation_class_mismatch",
            message="113 技優領航專班 40630B 推薦「網站應用程式設計」，官方開課屬班級 40620A",
            offering=self.offering, recommendation=self.recommendation)

    def test_course_name_only_is_not_confirmed_and_not_open(self):
        self.assertFalse(self.recommendation.is_confirmed)
        self.assertTrue(self.recommendation.is_ambiguous)
        self.assertIsNone(published_enrollment_policy(self.section, self.class_30b))

    def test_maintainer_confirmation_keeps_source_records_intact(self):
        force_admin_login(self.client, self.maintainer)
        r = self.client.post(reverse("admin:courses_sectionclasspolicy_add"),
                             self.policy_data(self.class_30b, category="PROFESSIONAL_ELECTIVE",
                                              can_enroll=True))
        self.assertEqual(r.status_code, 302)
        policy = SectionClassPolicy.objects.get(section=self.section, class_group=self.class_30b)

        # 發布：記錄維護員帳號與時間，但來源資料完全不动
        r = self.client.post(reverse("admin:courses_sectionclasspolicy_changelist"),
                             {"action": "publish_selected_policies",
                              "_selected_action": [str(policy.pk)]})
        self.assertEqual(r.status_code, 302)
        policy.refresh_from_db()
        self.assertEqual(policy.status, "PUBLISHED")
        self.assertEqual(policy.confirmed_by_id, self.maintainer.pk)
        self.assertIsNotNone(policy.confirmed_at)

        self.recommendation.refresh_from_db()
        self.ambiguity.refresh_from_db()
        self.assertEqual(self.recommendation.match_status, "course_name_only")
        self.assertTrue(self.recommendation.is_ambiguous)
        self.assertFalse(self.recommendation.is_confirmed)
        self.assertEqual(self.ambiguity.kind, "recommendation_class_mismatch")
        self.assertEqual(self.ambiguity.recommendation_id, self.recommendation.pk)

    def test_withdraw_action_closes_policy_again(self):
        policy = SectionClassPolicy.objects.create(
            section=self.section, class_group=self.class_30b,
            course_category="PROFESSIONAL_ELECTIVE", can_enroll=True, status="PUBLISHED")
        force_admin_login(self.client, self.maintainer)
        r = self.client.post(reverse("admin:courses_sectionclasspolicy_changelist"),
                             {"action": "withdraw_selected_policies",
                              "_selected_action": [str(policy.pk)]})
        self.assertEqual(r.status_code, 302)
        policy.refresh_from_db()
        self.assertEqual(policy.status, "DRAFT")
        self.assertIsNone(published_enrollment_policy(self.section, self.class_30b))


class SuperuserAssignsMaintainerTests(TestCase):
    """驗收：superuser 在 Admin 指派維護員，帳號才取得管理能力。"""

    def setUp(self):
        self.superuser = get_user_model().objects.create_superuser(
            "root", email="root@example.com", password="pw")
        self.catalog = AcademicCatalog.objects.create(version="test-catalog")
        self.program_type = AcademicProgramType.objects.create(
            catalog=self.catalog, code="4", name_zh="四技日間部")
        self.department = AcademicDepartment.objects.create(
            catalog=self.catalog, program_type=self.program_type, code="406", name_zh="資訊管理系")
        self.user = get_user_model().objects.create_user("teacher1", email="t1@ntub.edu.tw",
                                                         password="pw")
        force_admin_login(self.client, self.superuser)

    def test_superuser_adds_scope_and_user_becomes_maintainer(self):
        r = self.client.post(reverse("admin:courses_coursemaintainerscope_add"),
                             {"user": self.user.pk, "department": self.department.pk,
                              "is_active": "on", "_save": "Save"})
        self.assertEqual(r.status_code, 302)
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_staff)
        self.assertFalse(self.user.is_superuser)
        self.assertTrue(self.user.groups.filter(name=MAINTAINER_GROUP_NAME).exists())
        self.assertTrue(is_maintainer(self.user))

    def test_superuser_can_revoke_scope(self):
        scope = CourseMaintainerScope.objects.create(user=self.user,
                                                      department=self.department)
        grant_maintainer_access(self.user)
        r = self.client.post(reverse("admin:courses_coursemaintainerscope_changelist"),
                             {"action": "revoke_selected_scopes",
                              "_selected_action": [str(scope.pk)]})
        self.assertEqual(r.status_code, 302)
        scope.refresh_from_db()
        self.assertFalse(scope.is_active)
        self.user.refresh_from_db()
        self.assertFalse(self.user.groups.filter(name=MAINTAINER_GROUP_NAME).exists())
        self.assertFalse(self.user.is_staff)

    def test_superuser_can_see_scope_admin(self):
        CourseMaintainerScope.objects.create(user=self.user, department=self.department)
        r = self.client.get(reverse("admin:courses_coursemaintainerscope_changelist"))
        self.assertEqual(r.status_code, 200)


class MaintainerCourseSectionAdminTests(MaintainerTestBase):
    """維護員可管理明確歸屬於授權系所的新課程與班別。"""

    def setUp(self):
        super().setUp()
        force_admin_login(self.client, self.maintainer)

    def course_data(self, code, department):
        return {
            "term": self.term.pk,
            "owning_department": department.pk,
            "code": code,
            "name": "維護員新增課程",
            "teacher": self.teacher.pk,
            "credits": 3,
            "syllabus": "",
            "prerequisites": [],
            "teaching_syllabus-TOTAL_FORMS": "1",
            "teaching_syllabus-INITIAL_FORMS": "0",
            "teaching_syllabus-MIN_NUM_FORMS": "0",
            "teaching_syllabus-MAX_NUM_FORMS": "1",
            "teaching_syllabus-0-id": "",
        }

    def section_data(self, course):
        return {
            "course": course.pk,
            "label": "A",
            "day": 1,
            "start_period": 1,
            "end_period": 2,
            "room": "",
            "capacity": 40,
        }

    def test_group_sync_includes_course_and_section_write_permissions(self):
        call_command("setup_course_maintainer_group", stdout=StringIO())
        group = Group.objects.get(name=MAINTAINER_GROUP_NAME)
        codenames = set(group.permissions.values_list("codename", flat=True))
        self.assertTrue({"add_course", "change_course", "add_section", "change_section"} <= codenames)
        self.assertTrue({"delete_course", "delete_section",
                         "delete_ntubtimetablesnapshot",
                         "delete_ntubtimetableentry"} <= codenames)

    def test_maintainer_can_create_owned_course_and_section(self):
        course_response = self.client.post(
            reverse("admin:courses_course_add"),
            self.course_data("NEW406", self.dept_a),
        )
        self.assertEqual(course_response.status_code, 302)
        course = Course.objects.get(code="NEW406")
        self.assertEqual(course.owning_department_id, self.dept_a.pk)
        self.assertContains(
            self.client.get(reverse("admin:courses_course_changelist")),
            "NEW406",
        )

        section_response = self.client.post(
            reverse("admin:courses_section_add"),
            self.section_data(course),
        )
        self.assertEqual(section_response.status_code, 302)
        section = Section.objects.get(course=course, label="A")
        self.assertContains(
            self.client.get(reverse("admin:courses_section_changelist")),
            "NEW406",
        )

        section_response = self.client.post(
            reverse("admin:courses_section_change", args=[section.pk]),
            {**self.section_data(course), "room": "A101"},
        )
        self.assertEqual(section_response.status_code, 302)
        section.refresh_from_db()
        self.assertEqual(section.room, "A101")

    def test_maintainer_cannot_create_or_edit_unowned_course(self):
        create_response = self.client.post(
            reverse("admin:courses_course_add"),
            self.course_data("NEW500", self.dept_b),
        )
        self.assertEqual(create_response.status_code, 200)
        self.assertFalse(Course.objects.filter(code="NEW500").exists())

        other = Course.objects.create(
            term=self.term, owning_department=self.dept_b,
            code="OTHER500", name="他系課程", teacher=self.teacher,
        )
        change_response = self.client.post(
            reverse("admin:courses_course_change", args=[other.pk]),
            self.course_data("OTHER500", self.dept_b),
        )
        self.assertEqual(change_response.status_code, 302)
        other.refresh_from_db()
        self.assertEqual(other.name, "他系課程")
        self.assertEqual(
            self.client.get(
                reverse("admin:courses_course_change", args=[other.pk])
            ).status_code,
            302,
        )

    def test_new_course_requires_explicit_department(self):
        data = self.course_data("NO-DEPT", self.dept_a)
        data.pop("owning_department")
        response = self.client.post(reverse("admin:courses_course_add"), data)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Course.objects.filter(code="NO-DEPT").exists())


class SetupMaintainerGroupCommandTests(TestCase):
    """可重複執行的初始化指令：只建群組與權限，不會把學生加進群組。"""

    def test_command_is_idempotent_and_adds_no_users(self):
        out = StringIO()
        call_command("setup_course_maintainer_group", stdout=out)
        group = Group.objects.get(name=MAINTAINER_GROUP_NAME)
        first = set(group.permissions.values_list("id", flat=True))
        self.assertTrue(first)

        call_command("setup_course_maintainer_group", stdout=StringIO())
        group.refresh_from_db()
        self.assertEqual(set(group.permissions.values_list("id", flat=True)), first)
        self.assertEqual(group.user_set.count(), 0)

    def test_command_grants_policy_permissions_only(self):
        call_command("setup_course_maintainer_group", stdout=StringIO())
        group = Group.objects.get(name=MAINTAINER_GROUP_NAME)
        codenames = set(group.permissions.values_list("codename", flat=True))
        self.assertIn("add_sectionclasspolicy", codenames)
        self.assertIn("change_sectionclasspolicy", codenames)
        self.assertIn("view_sectionclasspolicy", codenames)
        self.assertNotIn("view_enrollment", codenames)
        self.assertNotIn("change_user", codenames)

    def test_dry_run_writes_nothing(self):
        call_command("setup_course_maintainer_group", "--dry-run", stdout=StringIO())
        self.assertFalse(Group.objects.filter(name=MAINTAINER_GROUP_NAME).exists())
