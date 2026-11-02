from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path
from django.views.generic.base import RedirectView

from courses.admin import teacher_admin_index

urlpatterns = [
    path("admin/", admin.site.admin_view(teacher_admin_index)),
    path("admin/", admin.site.urls),
    # 學生登入只走 Google：allauth 的密碼登入頁導回 Google 登入頁（放在 allauth.urls 前面才會先被比對到）
    path("accounts/login/", RedirectView.as_view(pattern_name="login")),
    # Google OAuth（allauth）：學生登入與 Google 驗證都在伺服器端處理
    path("accounts/", include("allauth.urls")),
    # 登出改用 Django 內建（POST-only）
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("", include("courses.urls")),
]
