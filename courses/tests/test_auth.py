"""Google OAuth 登入測試。

全部在本地假造 Google 的回應，不打真實的 Google 網路：
- provider 的換 token 步驟（get_access_token_data）被 mock 掉
- complete_login 被 mock 成用假資料組 SocialLogin
"""

from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from allauth.socialaccount.models import SocialAccount
from allauth.socialaccount.providers.google.views import GoogleOAuth2Adapter
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from ..models import Term
from ..teacher_access import SYLLABUS_TEACHER_GROUP_NAME

# 測試用假憑證：不需要真的 .env 金鑰
FAKE_GOOGLE_SETTINGS = {
    "google": {
        "APP": {"client_id": "test-client-id", "secret": "test-client-secret", "key": ""},
        "SCOPE": ["profile", "email"],
    },
}


def google_data(email, verified=True, uid="google-uid-1"):
    """假造 Google 回傳的 id_token／userinfo 內容。"""
    return {
        "sub": uid,
        "email": email,
        "email_verified": verified,
        "given_name": "小明",
        "family_name": "王",
        "name": "王小明",
    }


class GoogleLoginTestCase(TestCase):
    """走一次假的 Google 登入流程（不連網）。"""

    def setUp(self):
        # 登入成功後會導到課程列表，列表需要當學期
        Term.objects.create(code="114-1", is_current=True)

    def google_login(self, email, verified=True, uid="google-uid-1", follow=False):
        data = google_data(email, verified=verified, uid=uid)

        def fake_complete_login(adapter, request, app, token, **kwargs):
            provider = adapter.get_provider()
            return provider.sociallogin_from_response(request, data)

        with override_settings(SOCIALACCOUNT_PROVIDERS=FAKE_GOOGLE_SETTINGS):
            # 1. 按登入 → 導去 Google（同時把 state 存進 session）
            start = self.client.get("/accounts/google/login/")
            self.assertEqual(start.status_code, 302)
            state = parse_qs(urlparse(start["Location"]).query)["state"][0]
            # 2. Google callback（換 token 與解析 id_token 都 mock 掉）
            with patch.object(GoogleOAuth2Adapter, "get_access_token_data",
                              return_value={"access_token": "fake-token", "token_type": "Bearer"}), \
                 patch.object(GoogleOAuth2Adapter, "complete_login",
                              autospec=True, side_effect=fake_complete_login):
                return self.client.get(f"/accounts/google/login/callback/?code=fake-code&state={state}",
                                       follow=follow)


class GoogleLoginSuccessTests(GoogleLoginTestCase):
    def test_frontend_and_admin_sessions_are_independent(self):
        self.client = Client(enforce_csrf_checks=True)
        anonymous_frontend = self.client.get("/login/")
        anonymous_admin = self.client.get(reverse("admin:index"))
        self.assertFalse(anonymous_frontend.context["user"].is_authenticated)
        self.assertEqual(anonymous_admin.status_code, 302)
        self.assertNotIn("sessionid", self.client.cookies)
        self.assertNotIn("ntub_admin_sessionid", self.client.cookies)

        admin_user = get_user_model().objects.create_superuser(
            "isolated-admin", email="isolated-admin@example.com",
            password="admin-password-123",
        )
        self.client.get(reverse("admin:login"))
        student_response = self.google_login("isolated-student@ntub.edu.tw")
        self.assertEqual(student_response.status_code, 302)
        student_cookie = self.client.cookies["sessionid"].value
        self.assertEqual(
            self.client.get(reverse("admin:index")).status_code, 302,
        )

        csrf_token = self.client.cookies["csrftoken"].value
        admin_login = self.client.post(reverse("admin:login"), {
            "username": "isolated-admin",
            "password": "admin-password-123",
            "next": reverse("admin:index"),
            "csrfmiddlewaretoken": csrf_token,
        })
        self.assertEqual(admin_login.status_code, 302)
        admin_cookie = self.client.cookies["ntub_admin_sessionid"]
        self.assertEqual(admin_cookie["path"], "/admin/")
        self.assertTrue(admin_cookie["httponly"])
        self.assertIn("sessionid", self.client.cookies)
        self.assertIn("ntub_admin_sessionid", self.client.cookies)
        self.assertEqual(self.client.cookies["sessionid"]["path"], "/")
        self.assertEqual(self.client.cookies["sessionid"].value, student_cookie)
        self.assertNotEqual(
            self.client.cookies["sessionid"].value, admin_cookie.value,
        )
        self.assertEqual(
            self.client.cookies["ntub_admin_sessionid"].value,
            admin_cookie.value,
        )

        frontend = self.client.get("/onboarding/")
        admin = self.client.get(reverse("admin:index"))
        student = get_user_model().objects.get(
            email="isolated-student@ntub.edu.tw",
        )
        self.assertEqual(frontend.context["user"], student)
        self.assertEqual(admin.context["user"], admin_user)

        csrf_token = self.client.cookies["csrftoken"].value
        frontend_logout = self.client.post(
            reverse("logout"),
            {"csrfmiddlewaretoken": csrf_token},
        )
        self.assertEqual(frontend_logout.status_code, 302)
        self.assertEqual(
            self.client.get(reverse("admin:index")).context["user"],
            admin_user,
        )
        self.assertFalse(
            self.client.get("/login/").context["user"].is_authenticated,
        )

        self.google_login("isolated-student@ntub.edu.tw")
        self.assertEqual(
            self.client.get(reverse("admin:index")).context["user"],
            admin_user,
        )
        self.client.get(reverse("admin:logout"))
        csrf_token = self.client.cookies["csrftoken"].value
        admin_logout = self.client.post(
            reverse("admin:logout"),
            {"csrfmiddlewaretoken": csrf_token},
        )
        self.assertEqual(admin_logout.status_code, 302)
        self.assertEqual(
            self.client.get("/onboarding/").context["user"], student,
        )
        self.assertEqual(
            self.client.get(reverse("admin:index")).status_code, 302,
        )

    def test_ntub_verified_email_creates_user_and_logs_in(self):
        r = self.google_login("s1234567@ntub.edu.tw")
        self.assertEqual(r.status_code, 302)
        user = get_user_model().objects.get(email="s1234567@ntub.edu.tw")
        self.assertTrue(SocialAccount.objects.filter(user=user, provider="google").exists())
        self.assertEqual(int(self.client.session["_auth_user_id"]), user.pk)
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)
        self.assertFalse(user.groups.filter(
            name=SYLLABUS_TEACHER_GROUP_NAME,
        ).exists())
        self.assertEqual(
            self.client.get(reverse("admin:index")).status_code, 302,
        )

    def test_second_login_reuses_same_user(self):
        self.google_login("s1234567@ntub.edu.tw")
        self.client.logout()
        self.google_login("s1234567@ntub.edu.tw")
        self.assertEqual(get_user_model().objects.filter(email="s1234567@ntub.edu.tw").count(), 1)

    def test_login_redirects_to_onboarding_for_new_student(self):
        """Google 新學生第一次登入還沒做過學籍設定，先導到 /onboarding/。"""
        r = self.google_login("s1234567@ntub.edu.tw")
        self.assertEqual(r["Location"], reverse("onboarding"))

    def test_student_account_cannot_use_password_login(self):
        self.google_login("s1234567@ntub.edu.tw")
        user = get_user_model().objects.get(email="s1234567@ntub.edu.tw")
        self.assertFalse(user.has_usable_password())


class GoogleLoginRejectionTests(GoogleLoginTestCase):
    def test_google_login_cannot_authenticate_teacher_backend_account_by_email(self):
        teacher_group = Group.objects.create(name=SYLLABUS_TEACHER_GROUP_NAME)
        teacher_user = get_user_model().objects.create_user(
            "separate-teacher-account",
            email="teacher@ntub.edu.tw",
            password="separate-password",
            is_staff=True,
        )
        teacher_group.user_set.add(teacher_user)

        response = self.google_login("teacher@ntub.edu.tw")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("login"))
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertFalse(SocialAccount.objects.filter(
            user=teacher_user, provider="google",
        ).exists())
        teacher_user.refresh_from_db()
        self.assertTrue(teacher_user.has_usable_password())
        self.assertFalse(
            get_user_model().objects.filter(
                email="teacher@ntub.edu.tw",
            ).exclude(pk=teacher_user.pk).exists()
        )

    def test_non_ntub_email_is_rejected(self):
        r = self.google_login("someone@gmail.com")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r["Location"], reverse("login"))
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertFalse(get_user_model().objects.filter(email="someone@gmail.com").exists())

    def test_unverified_ntub_email_is_rejected(self):
        r = self.google_login("s1234567@ntub.edu.tw", verified=False)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r["Location"], reverse("login"))
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertFalse(get_user_model().objects.filter(email="s1234567@ntub.edu.tw").exists())

    def test_rejection_shows_message_on_login_page(self):
        r = self.google_login("someone@gmail.com", follow=True)
        self.assertContains(r, "只接受 @ntub.edu.tw")
        self.assertFalse(r.context["user"].is_authenticated)

    def test_rejection_message_for_unverified_email(self):
        r = self.google_login("s1234567@ntub.edu.tw", verified=False, follow=True)
        self.assertContains(r, "尚未驗證")
        self.assertFalse(r.context["user"].is_authenticated)

    def test_lookalike_domain_is_rejected(self):
        """不是 ntub.edu.tw 的網域也要擋（例如 ntub.edu.tw.evil.com）。"""
        r = self.google_login("s@ntub.edu.tw.evil.com")
        self.assertEqual(r["Location"], reverse("login"))
        self.assertNotIn("_auth_user_id", self.client.session)


class LogoutTests(TestCase):
    def setUp(self):
        self.u = get_user_model().objects.create_user("u", email="s@ntub.edu.tw")
        Term.objects.create(code="114-1", is_current=True)

    def test_post_logout_logs_out(self):
        self.client.force_login(self.u)
        r = self.client.post(reverse("logout"), follow=True)
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertFalse(r.context["user"].is_authenticated)


class AdminLoginTests(TestCase):
    """Django Admin 仍可用帳密登入（老師／管理員）。"""

    def setUp(self):
        self.staff = get_user_model().objects.create_superuser(
            "staff", email="staff@example.com", password="staff-pass-123")

    def test_staff_can_log_into_admin_with_password(self):
        r = self.client.post(reverse("admin:login"),
                             {"username": "staff", "password": "staff-pass-123", "next": reverse("admin:index")},
                             follow=True)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context["user"], self.staff)
        self.assertTrue(r.context["user"].is_staff)
        frontend = self.client.get("/login/")
        self.assertFalse(frontend.context["user"].is_authenticated)

    def test_admin_login_page_is_reachable(self):
        r = self.client.get(reverse("admin:login"))
        self.assertEqual(r.status_code, 200)


class PasswordSignupClosedTests(TestCase):
    """學生只能走 Google：allauth 的密碼註冊／登入／改密頁面都不開放。"""

    def test_signup_url_is_gone(self):
        self.assertEqual(self.client.get("/accounts/signup/").status_code, 404)

    def test_password_reset_url_is_gone(self):
        self.assertEqual(self.client.get("/accounts/password/reset/").status_code, 404)

    def test_password_login_page_redirects_to_google_login(self):
        r = self.client.get("/accounts/login/")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r["Location"], reverse("login"))

    def test_student_with_password_cannot_log_in_through_allauth(self):
        """就算有密碼（例如 seed 建立的 demo 帳號），也不能用密碼登入。"""
        get_user_model().objects.create_user("demo", email="demo@example.com", password="demo1234")
        r = self.client.post("/accounts/login/", {"login": "demo", "password": "demo1234"},
                             follow=True)
        self.assertNotIn("_auth_user_id", self.client.session)
