"""學生進入系統前需先登入，登入後未完成學籍設定者先完成 onboarding。

學籍導向只針對「用 Google 登入的學生」：
- 老師、staff、以及本機帳號（本機建立的 demo／測試帳號）不受影響。

允許匿名或未設定學籍者存取的路徑（避免 Redirect Loop 並保留登入流程）：
- /onboarding/        首次學籍設定頁本身
- /me/profile/        個人資料頁（設定完成後才有內容，但導過去也安全）
- /accounts/...       allauth 的 OAuth 登入／回呼／登出
- /admin/...          Django Admin
- /logout/            登出
- 靜態資源與登入頁
"""

from django.shortcuts import redirect
from django.urls import reverse

from .academic import needs_academic_profile

# 這些路徑不導向 onboarding（保持登入、回呼、登出、Admin、靜態資源正常）
ALLOWED_PREFIXES = (
    "/onboarding/",
    "/me/profile/",
    "/accounts/",
    "/admin/",
    "/logout/",
    "/static/",
    "/media/",
    "/login/",
    "/favicon.ico",
    "/health",
)


class AcademicProfileRequiredMiddleware:
    """先要求登入，再要求新 Google 學生完成首次學籍設定。"""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        path = request.path
        if path.startswith(ALLOWED_PREFIXES):
            return self.get_response(request)

        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            from django.contrib.auth.views import redirect_to_login
            return redirect_to_login(request.get_full_path(), login_url=reverse("login"))

        if needs_academic_profile(user):
            return redirect(reverse("onboarding"))
        return self.get_response(request)
