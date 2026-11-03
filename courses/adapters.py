"""Google 登入的伺服器端把關。

正式環境學生只能用「經 Google 驗證的 @ntub.edu.tw 信箱」登入。
開發 Demo 可在明確設定下允許經 Google 驗證的其他信箱。判斷全部在伺服器端做：
資料來自 Google 回傳的 token／userinfo（不是前端，也不是使用者自己輸入的表單），
所以不能只靠頁面上的提示擋人。
"""

from allauth.core.exceptions import ImmediateHttpResponse
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.shortcuts import redirect
from django.conf import settings

from .teacher_access import SYLLABUS_TEACHER_GROUP_NAME

NTUB_EMAIL_DOMAIN = "ntub.edu.tw"

REJECT_DOMAIN_MESSAGE = "此系統只接受 @ntub.edu.tw 的 NTUB 學生帳號登入。"
REJECT_UNVERIFIED_MESSAGE = "Google 尚未驗證這個信箱，請先在 Google 完成信箱驗證再登入。"
REJECT_TEACHER_ACCOUNT_MESSAGE = "教師後台帳號請使用獨立的後台帳號及密碼登入。"


def is_ntub_email(email):
    """信箱是不是 @ntub.edu.tw（大小寫不拘）。"""
    if not email:
        return False
    return email.strip().lower().endswith("@" + NTUB_EMAIL_DOMAIN)


def social_email(sociallogin):
    """從 Google 回傳的資料取出信箱。"""
    extra_data = getattr(sociallogin.account, "extra_data", None) or {}
    email = extra_data.get("email") or getattr(sociallogin, "email", None)
    return (email or "").strip().lower()


def google_email_verified(sociallogin):
    """Google 有沒有說這個信箱是已驗證的。

    Google 的兩種回應格式欄位名不同：
    - id_token：email_verified
    - userinfo：verified_email
    兩個都沒有 → 視為未驗證（寧可拒絕，不要放行）。
    """
    extra_data = getattr(sociallogin.account, "extra_data", None) or {}
    for key in ("email_verified", "verified_email"):
        if key in extra_data:
            return bool(extra_data.get(key))
    return False


class NtubSocialAccountAdapter(DefaultSocialAccountAdapter):
    """在 allauth 建立／登入帳號之前，先檢查信箱網域與 Google 驗證狀態。"""

    def pre_social_login(self, request, sociallogin):
        email = social_email(sociallogin)
        allow_any_demo_email = (
            settings.APP_ENV == "development"
            and settings.DEBUG
            and settings.ALLOW_ANY_GOOGLE_EMAIL_FOR_DEMO
        )
        if not allow_any_demo_email and not is_ntub_email(email):
            self._reject(request, REJECT_DOMAIN_MESSAGE)
        if not google_email_verified(sociallogin):
            self._reject(request, REJECT_UNVERIFIED_MESSAGE)
        social_user = getattr(sociallogin, "user", None)
        if (
            social_user is not None
            and social_user.pk
            and social_user.groups.filter(name=SYLLABUS_TEACHER_GROUP_NAME).exists()
        ):
            self._reject(request, REJECT_TEACHER_ACCOUNT_MESSAGE)
        if get_user_model().objects.filter(
            email__iexact=social_email(sociallogin),
            groups__name=SYLLABUS_TEACHER_GROUP_NAME,
        ).exists():
            self._reject(request, REJECT_TEACHER_ACCOUNT_MESSAGE)

    def _reject(self, request, message):
        messages.error(request, message)
        raise ImmediateHttpResponse(redirect("login"))
