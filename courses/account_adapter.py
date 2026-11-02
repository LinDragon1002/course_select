"""登入後的預設導向：還沒做過學籍設定的 Google 學生先到 /onboarding/。

只改「沒有指定 next 時的預設落地頁」：
- 已經完成學籍設定 → 原本的首頁（LOGIN_REDIRECT_URL）
- 還沒完成學籍設定 → /onboarding/

allauth 的註冊流程（Google 第一次登入建立新帳號）走 get_signup_redirect_url，
既有帳號登入走 get_login_redirect_url，兩個都要覆蓋才會一致。

老師、staff、非 Google 登入的帳號完全不變（needs_academic_profile 只對
Google 登入的學生為真）。
"""

from allauth.account.adapter import DefaultAccountAdapter
from django.urls import reverse

from .academic import needs_academic_profile


class NtubAccountAdapter(DefaultAccountAdapter):
    """登入導向：未完成學籍設定的學生先到 onboarding。"""

    def _onboarding_if_needed(self, request):
        if needs_academic_profile(getattr(request, "user", None)):
            return reverse("onboarding")
        return None

    def get_login_redirect_url(self, request):
        return self._onboarding_if_needed(request) or super().get_login_redirect_url(request)

    def get_signup_redirect_url(self, request):
        return self._onboarding_if_needed(request) or super().get_signup_redirect_url(request)
