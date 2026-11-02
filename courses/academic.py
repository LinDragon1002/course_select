"""學籍相關的共用查詢：這個學生是否已經完成過首次學籍設定。

放在 academic.py 是因為 middleware、view、adapter 都要用同一個判準，
避免各自寫一套而漏掉其中一邊。
"""

from .models import StudentAcademicProfile, Teacher


def has_academic_profile(user):
    """這個登入者是否已經完成過學籍設定（已綁定班級）。"""
    if not user or not user.is_authenticated:
        return False
    return StudentAcademicProfile.objects.filter(user=user).exists()


def is_google_student(user):
    """是不是用 Google 帳號登入的學生。

    學制設定只針對學生；老師與管理員走 Django 自己的帳密流程，不受這個限制。
    """
    from allauth.socialaccount.models import SocialAccount

    if not user or not user.is_authenticated:
        return False
    return SocialAccount.objects.filter(user=user, provider="google").exists()


def is_teacher(user):
    """是不是老師帳號（Teacher 有綁 user）。"""
    if not user or not user.is_authenticated:
        return False
    return Teacher.objects.filter(user=user).exists()


def needs_academic_profile(user):
    """是否需要先做首次學籍設定。

    判斷「學生」而不是「任何登入者」：只有 Google 登入的學生需要先設定學籍，
    老師、staff、以及本機建立的測試／示範帳號都不受影響。
    """
    if not user or not user.is_authenticated:
        return False
    if user.is_staff or is_teacher(user):
        return False
    if not is_google_student(user):
        return False
    return not has_academic_profile(user)


def google_identity(user):
    """回傳 (Google 姓名, Google Email)。

    姓名優先取 SocialAccount 額外資料裡的 name（那是 Google 回傳、已驗證的名字），
    沒有再退回帳號本身的 first_name / last_name。
    兩者都是唯讀顯示，頁面上不提供修改欄位。
    """
    from allauth.socialaccount.models import SocialAccount

    name = ""
    email = ""
    account = SocialAccount.objects.filter(user=user, provider="google").first()
    if account is not None:
        extra = account.extra_data or {}
        name = (extra.get("name") or "").strip()
        email = (extra.get("email") or "").strip()
    if not name:
        name = user.get_full_name().strip() or user.username
    if not email:
        email = (user.email or "").strip()
    return name, email
