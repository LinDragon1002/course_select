import os
from pathlib import Path
from dotenv import load_dotenv

# 後台側邊欄的中文分類（AdminLTE 官方的 admin_menu 設定）。
# 這個模組在 import 時不會碰 Django（app registry 還沒準備好），
# 所有權限判斷都延後到 request 時間才進行。
from courses.admin_navigation import ADMIN_MENU

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")
SECRET_KEY = os.environ.get("SECRET_KEY", "dev-only-change-me")
def env_bool(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


DEBUG = env_bool("DEBUG", default=True)
ALLOWED_HOSTS = ["*"] if DEBUG else os.environ.get("ALLOWED_HOSTS", "").split(",")
APP_ENV = os.environ.get("APP_ENV", "production").strip().lower()
ALLOW_ANY_GOOGLE_EMAIL_FOR_DEMO = env_bool(
    "ALLOW_ANY_GOOGLE_EMAIL_FOR_DEMO",
    default=False,
)

INSTALLED_APPS = [
    # django_components 必須在前面：AdminLTE 4 的元件（component）依賴它。
    "django_components",
    # django_adminlte4 必須排在 django.contrib.admin 前面，套件自帶的 admin/*
    # 模板覆寫才會贏（後台變成 AdminLTE 4 外框）；同理排在 allauth 前面，
    # 套件的 allauth/* 模板會蓋掉 allauth 自己的版本，Google 登入頁走 AdminLTE 樣式。
    "django_adminlte4",
    "django.contrib.admin", "django.contrib.auth", "django.contrib.contenttypes",
    "django.contrib.sessions", "django.contrib.messages", "django.contrib.staticfiles",
    "django.contrib.sites",
    "allauth", "allauth.account", "allauth.socialaccount",
    "allauth.socialaccount.providers.google",
    "courses",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "courses.session_middleware.PathAwareSessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "allauth.account.middleware.AccountMiddleware",
    # 還沒做過首次學籍設定的 Google 學生，一律先導到 /onboarding/
    "courses.middleware.AcademicProfileRequiredMiddleware",
]
ROOT_URLCONF = "config.urls"
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": False,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",

                # AdminLTE 4
                "django_adminlte4.context_processors.adminlte",

                # 原本的課程功能
                "courses.context_processors.waitlist_banner",
                "courses.context_processors.admin_dashboard",

                # 佳欣新增的學生端 UI
                "courses.context_processors.ui_shell",
            ],
            "loaders": [
                (
                    "django.template.loaders.cached.Loader",
                    [
                        "django.template.loaders.filesystem.Loader",
                        "django.template.loaders.app_directories.Loader",
                        "django_components.template_loader.Loader",
                    ],
                ),
            ],
            "builtins": [
                "django_components.templatetags.component_tags",
            ],
        },
    },
]
# AdminLTE 4 的元件（component）住在套件的 components/ 目錄，開啟自動探索即可。
COMPONENTS = {"dirs": [], "app_dirs": ["components"], "autodiscover": True}
# 元件目錄裡的 .css/.js 也要能 collectstatic，否則 {% component_css_dependencies %}
# 產生的 <link> 會 404。
STATICFILES_FINDERS = [
    "django.contrib.staticfiles.finders.FileSystemFinder",
    "django.contrib.staticfiles.finders.AppDirectoriesFinder",
    "django_components.finders.ComponentsFileSystemFinder",
]
# 專案層級的靜態檔（static/css/admin.css 等）也要能被 collectstatic 收進 STATIC_ROOT。
STATICFILES_DIRS = [BASE_DIR / "static"]
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": BASE_DIR / "db.sqlite3"}}
LANGUAGE_CODE = "zh-hant"
TIME_ZONE = "Asia/Taipei"
USE_TZ = True
STATIC_URL = "static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
SESSION_COOKIE_NAME = "sessionid"

# --- 登入 ---
# 學生一律走 Google OAuth（/login/ → Google）；正式環境只允許經 Google
# 驗證的 @ntub.edu.tw 信箱，開發 Demo 可由設定開放其他信箱。
# Django Admin 仍用 Django 自己的帳密登入（ModelBackend）。
AUTHENTICATION_BACKENDS = [
    "django.contrib.auth.backends.ModelBackend",
    "allauth.account.auth_backends.AuthenticationBackend",
]
SITE_ID = 1
LOGIN_URL = "/login/"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/"
# 只留社交登入：關掉 allauth 的密碼註冊／改密／密碼重設等頁面
SOCIALACCOUNT_ONLY = True
SOCIALACCOUNT_LOGIN_ON_GET = True  # 點登入直接跳 Google，不多一頁確認
# 信箱驗證由 Google 端把關（adapter 會檢查 email_verified），不用再寄確認信
ACCOUNT_EMAIL_VERIFICATION = "none"
# 不要用「信箱相同就自動連結既有帳號」，避免非 Google 的帳號被綁架
SOCIALACCOUNT_EMAIL_AUTHENTICATION = False
SOCIALACCOUNT_ADAPTER = "courses.adapters.NtubSocialAccountAdapter"
# 登入後的預設導向（未完成學籍設定的學生 → /onboarding/）
ACCOUNT_ADAPTER = "courses.account_adapter.NtubAccountAdapter"
SOCIALACCOUNT_PROVIDERS = {"google": {
    "APP": {"client_id": os.environ.get("GOOGLE_CLIENT_ID", ""),
            "secret": os.environ.get("GOOGLE_CLIENT_SECRET", ""), "key": ""},
    "SCOPE": ["profile", "email"],
}}

MAX_CREDITS = 25  # 學分上限

# --- AdminLTE 4（後台外框）---
# 這個專案沒有前端建置流程（沒有 npm/vite），所以用套件內附的預編譯 bundle，
# 不需要 Node 就能 collectstatic 並使用。
ADMINLTE = {
    "assets_mode": "static",
    "title": "NTUB 課程管理",
    "logo": "<b>NTUB</b> 課程管理",
    "admin_brand": "NTUB 課程管理",
    "sidebar_theme": "dark",
    # 後台側邊欄改用自訂的五個中文分類（課程規劃／學籍與班級／官方資料／
    # 選課與候補／系統管理），權限過濾走官方的 can 機制。
    "admin_menu": ADMIN_MENU,
}

# --- ngrok demo：只在 USE_NGROK=1 時生效 ---
# 不寫死 *.ngrok-free.app，那是公共命名空間，別人的頁面也會被當成可信任 origin；
# X-Forwarded-Proto 任何客戶端也都能自己帶。上線前不要把這兩行留著生效。
if os.environ.get("USE_NGROK") == "1":
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    CSRF_TRUSTED_ORIGINS = [o.strip() for o in os.environ.get("NGROK_ORIGIN", "").split(",") if o.strip()]
