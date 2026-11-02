"""NTUB 學生首次學籍設定與個人資料的測試。

涵蓋：
1. Google 新學生首次登入導向 onboarding
2. 完成設定後可以正常進入選課頁
3. 學制、系所、年級、班級級聯正確
4. 偽造不屬於系所的班級 POST 被拒絕
5. 40620A 與 40630B 可以設定
6. 不同入學年度的學生仍可歸屬指定班級
7. Google 姓名與 Email 不可由表單修改
8. 原有課程、選課、候補不受影響
9. Django Admin 正常
10. 未設定學籍無法直接 POST 加選
"""

from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from allauth.socialaccount.providers.google.views import GoogleOAuth2Adapter
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from ..models import (AcademicCatalog, AcademicClassGroup, AcademicDepartment,
                      AcademicProgramType, Course, Enrollment, Section,
                      StudentAcademicProfile, Teacher, Term)

FAKE_GOOGLE_SETTINGS = {
    "google": {
        "APP": {"client_id": "test-client-id", "secret": "test-client-secret", "key": ""},
        "SCOPE": ["profile", "email"],
    },
}


def google_data(email, verified=True, uid="google-uid-1"):
    return {
        "sub": uid,
        "email": email,
        "email_verified": verified,
        "given_name": "小明",
        "family_name": "王",
        "name": "王小明",
    }


class AcademicTestCase(TestCase):
    """建立一個小的學籍目錄（測試用的假資料），並提供 Google 假登入。"""

    def setUp(self):
        Term.objects.create(code="114-1", is_current=True)
        self.catalog = AcademicCatalog.objects.create(version="test-catalog", source_key="ntub")

        self.program_4 = AcademicProgramType.objects.create(
            catalog=self.catalog, code="4", name_zh="四技日間部",
            is_active=True, is_registration_selectable=True)
        self.program_1 = AcademicProgramType.objects.create(
            catalog=self.catalog, code="1", name_zh="碩士班",
            is_active=True, is_registration_selectable=True)

        self.dept_406 = AcademicDepartment.objects.create(
            catalog=self.catalog, program_type=self.program_4, code="406",
            name_zh="四技資訊管理系", is_active=True, is_registration_selectable=True)
        self.dept_501 = AcademicDepartment.objects.create(
            catalog=self.catalog, program_type=self.program_4, code="501",
            name_zh="四技機械工程系", is_active=True, is_registration_selectable=True)

        self.class_40610A = self._class("40610A", "四技資管一年甲班", self.dept_406, 1)
        self.class_40620A = self._class("40620A", "四技資管二年甲班", self.dept_406, 2)
        self.class_40630B = self._class("40630B", "四技資管三年乙班", self.dept_406, 3)
        self.class_50110A = self._class("50110A", "四技機械一年甲班", self.dept_501, 1)
        # 不可選的班級：即使猜到 id 也不該能選
        self.class_40640X = self._class("40640X", "四技資管四年甲班(停招)", self.dept_406, 4,
                                       selectable=False)

    def _class(self, code, name, department, grade, selectable=True):
        return AcademicClassGroup.objects.create(
            catalog=self.catalog, department=department, code=code, name_zh=name,
            grade_no=grade, is_active=True, is_registration_selectable=selectable)

    # --- 假的 Google 登入（不打真實網路） ---

    def google_login(self, email, uid="google-uid-1"):
        data = google_data(email, uid=uid)

        def fake_complete_login(adapter, request, app, token, **kwargs):
            provider = adapter.get_provider()
            return provider.sociallogin_from_response(request, data)

        with override_settings(SOCIALACCOUNT_PROVIDERS=FAKE_GOOGLE_SETTINGS):
            start = self.client.get("/accounts/google/login/")
            state = parse_qs(urlparse(start["Location"]).query)["state"][0]
            with patch.object(GoogleOAuth2Adapter, "get_access_token_data",
                              return_value={"access_token": "fake-token", "token_type": "Bearer"}), \
                 patch.object(GoogleOAuth2Adapter, "complete_login",
                              autospec=True, side_effect=fake_complete_login):
                return self.client.get(
                    f"/accounts/google/login/callback/?code=fake-code&state={state}")

    def setup_payload(self, class_group=None, program_type=None, department=None, grade=None):
        group = class_group or self.class_40620A
        return {
            "program_type": (program_type or self.program_4).pk,
            "department": (department or self.dept_406).pk,
            "grade": group.grade_no if grade is None else grade,
            "class_group": group.pk,
        }

    def complete_setup(self, class_group=None, **kwargs):
        payload = self.setup_payload(class_group=class_group, **kwargs)
        return self.client.post(reverse("onboarding"), payload)


class LoginRedirectTests(AcademicTestCase):
    """1. Google 新學生首次登入導向 onboarding。"""

    def test_new_student_login_redirects_to_onboarding(self):
        r = self.google_login("s1234567@ntub.edu.tw")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r["Location"], reverse("onboarding"))

    def test_student_with_profile_redirects_to_home(self):
        self.google_login("s1234567@ntub.edu.tw")
        self.complete_setup()
        self.client.logout()
        r = self.google_login("s1234567@ntub.edu.tw", uid="google-uid-1")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r["Location"], reverse("course_list"))

    def test_login_page_still_works(self):
        r = self.client.get(reverse("login"))
        self.assertEqual(r.status_code, 200)


class OnboardingAccessTests(AcademicTestCase):
    """2. 完成設定後可以正常進入選課頁。"""

    def test_onboarding_page_requires_login(self):
        r = self.client.get(reverse("onboarding"))
        self.assertEqual(r.status_code, 302)
        self.assertIn(reverse("login"), r["Location"])

    def test_setup_creates_profile_and_allows_course_list(self):
        self.google_login("s1234567@ntub.edu.tw")
        r = self.complete_setup()
        self.assertEqual(r.status_code, 302)

        profile = StudentAcademicProfile.objects.get(user__email="s1234567@ntub.edu.tw")
        self.assertEqual(profile.class_group, self.class_40620A)
        self.assertIsNotNone(profile.completed_at)

        r = self.client.get(reverse("course_list"))
        self.assertEqual(r.status_code, 200)

    def test_onboarding_redirects_when_already_configured(self):
        self.google_login("s1234567@ntub.edu.tw")
        self.complete_setup()
        r = self.client.get(reverse("onboarding"))
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r["Location"], reverse("course_list"))

    def test_setup_uses_database_options_not_hardcoded_codes(self):
        """頁面上的選項來自資料庫，不寫死 406／40620A。"""
        self.google_login("s1234567@ntub.edu.tw")
        r = self.client.get(reverse("onboarding"))
        self.assertContains(r, "四技資管二年甲班")
        self.assertContains(r, "四技資訊管理系")
        self.assertContains(r, "2 年級")
        # 不可選的班級不出現在選單裡
        self.assertNotContains(r, "停招")


class CascadeTests(AcademicTestCase):
    """3. 學制、系所、年級、班級級聯正確。"""

    def test_profile_records_full_hierarchy(self):
        self.google_login("s1234567@ntub.edu.tw")
        self.complete_setup(self.class_40620A)
        profile = StudentAcademicProfile.objects.get(user__email="s1234567@ntub.edu.tw")
        self.assertEqual(profile.program_type, self.program_4)
        self.assertEqual(profile.department, self.dept_406)
        self.assertEqual(profile.grade_no, 2)

    def test_grade_must_match_class_group(self):
        """送出與班級不符的年級要被拒絕。"""
        self.google_login("s1234567@ntub.edu.tw")
        payload = self.setup_payload(self.class_40620A, grade=3)
        r = self.client.post(reverse("onboarding"), payload)
        self.assertEqual(r.status_code, 200)
        self.assertFalse(StudentAcademicProfile.objects.exists())

    def test_class_group_from_another_department_is_rejected(self):
        """4. 偽造不屬於系所的班級 POST 被拒絕。"""
        self.google_login("s1234567@ntub.edu.tw")
        payload = self.setup_payload(self.class_50110A)  # 班級屬於 501，卻送 406
        r = self.client.post(reverse("onboarding"), payload)
        self.assertEqual(r.status_code, 200)
        self.assertFalse(StudentAcademicProfile.objects.exists())

    def test_class_group_from_another_program_type_is_rejected(self):
        """送碩士班學制 + 四技班級也要被拒絕。"""
        self.google_login("s1234567@ntub.edu.tw")
        payload = self.setup_payload(self.class_40620A, program_type=self.program_1)
        r = self.client.post(reverse("onboarding"), payload)
        self.assertEqual(r.status_code, 200)
        self.assertFalse(StudentAcademicProfile.objects.exists())

    def test_non_selectable_class_group_is_rejected(self):
        """不可選的班級（停招）不能設定。"""
        self.google_login("s1234567@ntub.edu.tw")
        payload = self.setup_payload(self.class_40640X)
        r = self.client.post(reverse("onboarding"), payload)
        self.assertEqual(r.status_code, 200)
        self.assertFalse(StudentAcademicProfile.objects.exists())


class ClassChoiceTests(AcademicTestCase):
    """5. 40620A 與 40630B 可以設定；6. 不同入學年度也能歸屬指定班級。"""

    def test_40620a_can_be_selected(self):
        self.google_login("s1234567@ntub.edu.tw")
        self.complete_setup(self.class_40620A)
        profile = StudentAcademicProfile.objects.get(user__email="s1234567@ntub.edu.tw")
        self.assertEqual(profile.class_group.code, "40620A")
        self.assertEqual(profile.grade_no, 2)

    def test_40630b_can_be_selected(self):
        self.google_login("s7654321@ntub.edu.tw")
        self.complete_setup(self.class_40630B)
        profile = StudentAcademicProfile.objects.get(user__email="s7654321@ntub.edu.tw")
        self.assertEqual(profile.class_group.code, "40630B")
        self.assertEqual(profile.grade_no, 3)

    def test_older_entry_year_student_can_belong_to_40630b(self):
        """不同入學年度的學姊仍可歸屬四技資管三年乙班（不靠學號前三碼推斷）。"""
        self.google_login("s1080001@ntub.edu.tw")  # 學號看起來像一年級
        self.complete_setup(self.class_40630B)
        profile = StudentAcademicProfile.objects.get(user__email="s1080001@ntub.edu.tw")
        self.assertEqual(profile.class_group.code, "40630B")
        self.assertEqual(profile.grade_no, 3)


class ProfileTests(AcademicTestCase):
    """7. Google 姓名與 Email 不可由表單修改。"""

    def setUp(self):
        super().setUp()
        self.google_login("s1234567@ntub.edu.tw")
        self.complete_setup()

    def test_profile_page_shows_google_identity_and_academic_info(self):
        r = self.client.get(reverse("my_profile"))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "王小明")
        self.assertContains(r, "s1234567@ntub.edu.tw")
        self.assertContains(r, "四技資管二年甲班")
        self.assertContains(r, "四技資訊管理系")

    def test_profile_form_has_no_name_or_email_inputs(self):
        r = self.client.get(reverse("my_profile"))
        self.assertNotContains(r, 'name="first_name"')
        self.assertNotContains(r, 'name="last_name"')
        self.assertNotContains(r, 'name="email"')

    def test_posting_name_and_email_does_not_change_account(self):
        user = get_user_model().objects.get(email="s1234567@ntub.edu.tw")
        r = self.client.post(reverse("my_profile"), {
            "program_type": self.program_4.pk,
            "department": self.dept_406.pk,
            "grade": 3,
            "class_group": self.class_40630B.pk,
            "first_name": "壞掉",
            "last_name": "壞掉",
            "email": "hacker@example.com",
        })
        self.assertEqual(r.status_code, 302)
        user.refresh_from_db()
        self.assertEqual(user.email, "s1234567@ntub.edu.tw")
        self.assertNotEqual(user.email, "hacker@example.com")
        self.assertNotEqual(user.first_name, "壞掉")

    def test_profile_update_requires_valid_class_group(self):
        r = self.client.post(reverse("my_profile"), {
            "program_type": self.program_4.pk,
            "department": self.dept_406.pk,
            "grade": 1,
            "class_group": self.class_50110A.pk,  # 不屬於 406
        })
        self.assertEqual(r.status_code, 200)
        profile = StudentAcademicProfile.objects.get(user__email="s1234567@ntub.edu.tw")
        self.assertEqual(profile.class_group, self.class_40620A)  # 沒被改掉

    def test_profile_update_changes_class_group(self):
        r = self.client.post(reverse("my_profile"), {
            "program_type": self.program_4.pk,
            "department": self.dept_406.pk,
            "grade": 3,
            "class_group": self.class_40630B.pk,
        })
        self.assertEqual(r.status_code, 302)
        profile = StudentAcademicProfile.objects.get(user__email="s1234567@ntub.edu.tw")
        self.assertEqual(profile.class_group, self.class_40630B)


class ExistingFlowTests(AcademicTestCase):
    """8. 原有課程、選課、候補不受影響。"""

    def setUp(self):
        super().setUp()
        self.teacher = Teacher.objects.create(name="王老師")
        self.course = Course.objects.create(term=Term.current(), code="CS101",
                                            name="程式設計", teacher=self.teacher)
        self.section = Section.objects.create(course=self.course, label="A", day=1,
                                              start_period=1, end_period=2, capacity=40)

    def test_student_with_profile_can_enroll(self):
        self.google_login("s1234567@ntub.edu.tw")
        self.complete_setup()
        r = self.client.post(reverse("enroll", args=[self.section.pk]))
        self.assertEqual(r.status_code, 302)
        self.assertTrue(Enrollment.objects.filter(user__email="s1234567@ntub.edu.tw").exists())

    def test_course_list_and_detail_still_render(self):
        self.google_login("s1234567@ntub.edu.tw")
        self.complete_setup()
        self.assertEqual(self.client.get(reverse("course_list")).status_code, 200)
        self.assertEqual(self.client.get(reverse("course_detail", args=[self.course.pk])).status_code, 200)
        self.assertEqual(self.client.get(reverse("my_schedule")).status_code, 200)


class GuardTests(AcademicTestCase):
    """未完成學籍設定的學生不能直接進入課程相關頁面，也不能再加選。"""

    def setUp(self):
        super().setUp()
        self.teacher = Teacher.objects.create(name="王老師")
        self.course = Course.objects.create(term=Term.current(), code="CS101",
                                            name="程式設計", teacher=self.teacher)
        self.section = Section.objects.create(course=self.course, label="A", day=1,
                                              start_period=1, end_period=2, capacity=40)
        self.google_login("s1234567@ntub.edu.tw")

    def test_course_list_redirects_to_onboarding(self):
        r = self.client.get(reverse("course_list"))
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r["Location"], reverse("onboarding"))

    def test_course_detail_redirects_to_onboarding(self):
        r = self.client.get(reverse("course_detail", args=[self.course.pk]))
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r["Location"], reverse("onboarding"))

    def test_my_schedule_redirects_to_onboarding(self):
        r = self.client.get(reverse("my_schedule"))
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r["Location"], reverse("onboarding"))

    def test_cannot_post_enroll_without_profile(self):
        """10. 未設定學籍無法直接 POST 加選。"""
        r = self.client.post(reverse("enroll", args=[self.section.pk]))
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r["Location"], reverse("onboarding"))
        self.assertFalse(Enrollment.objects.exists())

    def test_logout_still_works(self):
        r = self.client.post(reverse("logout"))
        self.assertEqual(r.status_code, 302)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_teacher_and_staff_are_not_blocked(self):
        """現有教師與 staff 的權限流程不受影響。"""
        self.client.logout()
        teacher = get_user_model().objects.create_user("teacher1", email="t1@ntub.edu.tw")
        Teacher.objects.create(name="王老師", user=teacher)
        self.client.force_login(teacher)
        r = self.client.get(reverse("course_list"))
        self.assertEqual(r.status_code, 200)

        staff = get_user_model().objects.create_superuser("staff", email="s@example.com",
                                                          password="staff-pass-123")
        self.client.force_login(staff)
        r = self.client.get(reverse("course_list"))
        self.assertEqual(r.status_code, 200)

    def test_non_google_user_is_not_blocked(self):
        """本機建立的帳號（例如 demo 帳號）不需要學籍設定。"""
        self.client.logout()
        user = get_user_model().objects.create_user("demo", email="demo@example.com")
        self.client.force_login(user)
        r = self.client.get(reverse("course_list"))
        self.assertEqual(r.status_code, 200)


class AdminTests(AcademicTestCase):
    """9. Django Admin 正常。"""

    def test_student_profile_is_registered_in_admin(self):
        self.assertIn(StudentAcademicProfile, admin.site._registry)

    def test_admin_index_is_reachable_for_staff(self):
        staff = get_user_model().objects.create_superuser("staff", email="s@example.com",
                                                          password="staff-pass-123")
        self.client.force_login(staff)
        r = self.client.get(reverse("admin:index"))
        self.assertEqual(r.status_code, 200)

    def test_admin_changelist_for_student_profiles(self):
        staff = get_user_model().objects.create_superuser("staff", email="s@example.com",
                                                          password="staff-pass-123")
        self.client.force_login(staff)
        r = self.client.get(reverse("admin:courses_studentacademicprofile_changelist"))
        self.assertEqual(r.status_code, 200)
