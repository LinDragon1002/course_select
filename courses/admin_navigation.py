"""後台側邊欄（AdminLTE 4 `ADMINLTE["admin_menu"]`）的中文分類與權限過濾。

這個模組提供 `ADMIN_MENU`：`config/settings.py` 把它掛到 AdminLTE 官方的
`admin_menu` 設定，後台的側邊欄就會改用這份五個中文分類，而不是套件自動
產生的「app → model」清單。

權限過濾一律走 AdminLTE 官方的 `can` 鍵（`GateFilter`），判斷邏輯刻意**不
只看模型的 view 權限**，而是回頭問「這個模型在 Django Admin 裡實際註冊的
ModelAdmin 允不允許看」：

* `courses/admin.py` 裡的 `SuperuserOnlyAdmin`（選課、候補、學生學籍、使用者
  權限、OAuth 設定、站台設定）因此自動被尊重：就算有人手動把
  `view_enrollment` 之類的權限加給維護員群組，側邊欄也不會多出敏感入口。
* `CourseMaintainerScopeAdmin`（維護員授權範圍）本來就只給 superuser，
  維護員看不到「維護員授權」入口。
* 受「維護員授權範圍」限制的模型（課程、班別、系所、班級、官方資料…），
  維護員若沒有任何有效授權系所，連入口都不會出現。

模組層級刻意不 import 任何 Django 模組（只有標準函式庫），因為
`config/settings.py` 會在 Django 啟動前 import 這個模組，那時 app registry
還沒準備好。所有需要 Django 的部分都延後到 request 時間才 import。
"""

from __future__ import annotations

# --- 權限判定 ---------------------------------------------------------------

#: 受「維護員授權範圍」（`CourseMaintainerScope`）限制的模型。
#:
#: 這些模型的資料只跟授權系所相關，維護員若沒有任何有效授權系所，
#: 看到入口也只會是空清單，所以直接不顯示。
SCOPE_LIMITED_LABELS = frozenset({
    "courses.academicclassgroup",
    "courses.academicdepartment",
    "courses.course",
    "courses.ntubcurriculumrecommendation",
    "courses.ntubofferingambiguity",
    "courses.ntubofferingsource",
    "courses.ntubtimetableentry",
    "courses.ntubtimetablesnapshot",
    "courses.officialcoursecredit",
    "courses.ntubrecommendationcandidate",
    "courses.section",
    # SectionClassPolicyAdmin 的 queryset 一樣只撈授權系所底下的政策。
    "courses.sectionclasspolicy",
})


def _registry_admin(label):
    """從 Django Admin 的註冊表找出這個模型的 ModelAdmin（找不到回 None）。

    以 `app_label.model_name` 當鍵，避免在模組層級 import 模型（settings 會
    在 app registry 就緒前 import 這個模組）。
    """
    from django.contrib import admin

    for model, model_admin in admin.site._registry.items():
        if model._meta.label_lower == label:
            return model_admin
    return None


def _has_active_scope(user):
    """這個使用者是否還有有效的維護員授權範圍。"""
    from .maintainer import scoped_departments

    return scoped_departments(user).exists()


def can_view_model(label, request):
    """這個使用者能不能在側邊欄看到某個模型的入口。

    判斷順序：

    1. 模型必須真的註冊在 Django Admin（沒註冊就不該出現入口）。
    2. 以實際註冊的 ModelAdmin 的 `has_view_permission()` 為準——這會把
       `SuperuserOnlyAdmin` 的限制一起帶進來，不會只靠模型 view 權限。
    3. 非 superuser 的維護員，對受授權範圍限制的模型還必須有有效範圍。
    """
    model_admin = _registry_admin(label)
    if model_admin is None:
        return False
    if not model_admin.has_view_permission(request):
        return False

    user = request.user
    if user.is_superuser:
        return True

    from .maintainer import is_maintainer

    if (is_maintainer(user) and label in SCOPE_LIMITED_LABELS
            and not _has_active_scope(user)):
        return False
    return True


def can_bulk_assign(request):
    """能不能用「批次安排課程」頁面。

    與 `SectionClassPolicyAdmin._bulk_assign_permission` 同一套規則：
    必須是課程維護員、必須有新增修課政策的模型權限，而且要有有效的授權
    系所（superuser 不受此限）。
    """
    from .maintainer import is_maintainer, is_superuser, scoped_departments

    user = request.user
    if not is_maintainer(user):
        return False
    if not user.has_perm("courses.add_sectionclasspolicy"):
        return False
    if not is_superuser(user) and not scoped_departments(user).exists():
        return False
    return True


def _can_view(label):
    """產生 AdminLTE `can` 用的過濾函式。"""

    def can(request):
        return can_view_model(label, request)

    return can


def can_dashboard(request):
    if not request.user.is_authenticated or not request.user.is_staff:
        return False
    from .maintainer import is_maintainer, is_superuser
    from .teacher_access import has_teacher_syllabus_access

    if (
        not is_maintainer(request.user)
        and not is_superuser(request.user)
        and has_teacher_syllabus_access(request.user)
    ):
        return False
    return True


def can_teacher_syllabi(request):
    from .maintainer import is_maintainer, is_superuser
    from .models import Teacher
    from .teacher_access import has_teacher_syllabus_access

    if not (
        is_superuser(request.user)
        or is_maintainer(request.user)
        or has_teacher_syllabus_access(request.user)
    ):
        return False
    return Teacher.objects.filter(user=request.user).exists()


def can_review_syllabi(request):
    from .maintainer import is_maintainer, is_superuser, scoped_departments

    user = request.user
    return bool(
        is_superuser(user)
        or (
            user.is_authenticated
            and user.is_staff
            and is_maintainer(user)
            and user.has_perm("courses.change_course")
            and scoped_departments(user).exists()
        )
    )


def can_manage_fixed_schedule(request):
    from .maintainer import is_maintainer, is_superuser, scoped_class_groups

    user = request.user
    return bool(
        user.is_authenticated
        and user.is_active
        and user.is_staff
        and (
            is_superuser(user)
            or (
                is_maintainer(user)
                and scoped_class_groups(user).exists()
            )
        )
    )


# --- 選單結構 ---------------------------------------------------------------

def _item(text, route, icon, can):
    """一個側邊欄項目。

    `route` 是 Django Admin 既有的 URL 名稱（官方 `HrefFilter` 會用 `reverse()`
    解析成實際路徑），`can` 是官方的權限過濾 hook。
    """
    return {"text": text, "route": route, "icon": icon, "can": can}


def _category(text, icon, items):
    """一個分類（樹狀選單的上層節點）。"""
    return {"text": text, "icon": icon, "submenu": items}


#: `ADMINLTE["admin_menu"]` 使用的中文分類。
#:
#: 每一項都對應到目前主要操作流程的管理入口。未列入的舊 ModelAdmin
#: 仍保留註冊、URL 與程式依賴，但不再作為主要側邊欄入口。
ADMIN_MENU = [
    _category("管理總覽", "bi bi-speedometer2", [
        _item("Dashboard", "admin:index", "bi bi-speedometer2", can_dashboard),
    ]),
    _category("課程規劃", "bi bi-calendar-range", [
        _item("我的授課課程", "admin:courses_course_teacher_syllabi",
              "bi bi-person-workspace", can_teacher_syllabi),
        _item("待審核教學大綱", "admin:courses_course_syllabus_review",
              "bi bi-clipboard-check", can_review_syllabi),
        _item("課程資料", "admin:courses_course_changelist",
              "bi bi-book", _can_view("courses.course")),
        _item("開課班別", "admin:courses_section_changelist",
              "bi bi-columns-gap", _can_view("courses.section")),
        _item("批次安排課程", "admin:courses_sectionclasspolicy_bulk_assign",
              "bi bi-list-check", can_bulk_assign),
        _item("班別修課政策", "admin:courses_sectionclasspolicy_changelist",
              "bi bi-journal-check", _can_view("courses.sectionclasspolicy")),
    ]),
    _category("班級固定課表", "bi bi-calendar-week", [
        _item("班級固定課表", "admin:courses_ntubtimetablesnapshot_fixed_schedule",
              "bi bi-calendar3", can_manage_fixed_schedule),
        _item("固定課表明細", "admin:courses_ntubtimetableentry_changelist",
              "bi bi-list-ul", _can_view("courses.ntubtimetableentry")),
        _item("班級課程教學大綱", "admin:courses_course_changelist",
              "bi bi-book-open", _can_view("courses.course")),
    ]),
    _category("歷史匯入與來源追蹤", "bi bi-archive", [
        _item("歷史匯入快照", "admin:courses_ntubtimetablesnapshot_changelist",
              "bi bi-database", _can_view("courses.ntubtimetablesnapshot")),
        _item("官方教學大綱來源", "admin:courses_officialteachingsyllabus_changelist",
              "bi bi-file-earmark-text", _can_view("courses.officialteachingsyllabus")),
        _item("歷史官方開課來源", "admin:courses_ntubofferingsource_changelist",
              "bi bi-link-45deg", _can_view("courses.ntubofferingsource")),
    ]),
    _category("學籍與班級", "bi bi-mortarboard", [
        _item("學制", "admin:courses_academicprogramtype_changelist",
              "bi bi-mortarboard", _can_view("courses.academicprogramtype")),
        _item("系所", "admin:courses_academicdepartment_changelist",
              "bi bi-building", _can_view("courses.academicdepartment")),
        _item("班級", "admin:courses_academicclassgroup_changelist",
              "bi bi-people", _can_view("courses.academicclassgroup")),
        _item("學生學籍", "admin:courses_studentacademicprofile_changelist",
              "bi bi-person-vcard", _can_view("courses.studentacademicprofile")),
    ]),
    _category("選課與候補", "bi bi-person-check", [
        _item("加選紀錄", "admin:courses_enrollment_changelist",
              "bi bi-check2-square", _can_view("courses.enrollment")),
        _item("候補名單", "admin:courses_waitlist_changelist",
              "bi bi-hourglass-split", _can_view("courses.waitlist")),
    ]),
    _category("系統管理", "bi bi-gear", [
        _item("學期", "admin:courses_term_changelist",
              "bi bi-calendar-event", _can_view("courses.term")),
        _item("教師", "admin:courses_teacher_changelist",
              "bi bi-person-badge", _can_view("courses.teacher")),
        _item("學籍目錄", "admin:courses_academiccatalog_changelist",
              "bi bi-archive", _can_view("courses.academiccatalog")),
        _item("維護員授權", "admin:courses_coursemaintainerscope_changelist",
              "bi bi-key", _can_view("courses.coursemaintainerscope")),
        _item("使用者", "admin:auth_user_changelist",
              "bi bi-person", _can_view("auth.user")),
        _item("群組", "admin:auth_group_changelist",
              "bi bi-people-fill", _can_view("auth.group")),
        _item("電子郵件", "admin:account_emailaddress_changelist",
              "bi bi-envelope", _can_view("account.emailaddress")),
        _item("社交登入帳號", "admin:socialaccount_socialaccount_changelist",
              "bi bi-person-lock", _can_view("socialaccount.socialaccount")),
        _item("社交應用程式", "admin:socialaccount_socialapp_changelist",
              "bi bi-plug", _can_view("socialaccount.socialapp")),
        _item("社交權杖", "admin:socialaccount_socialtoken_changelist",
              "bi bi-key-fill", _can_view("socialaccount.socialtoken")),
        _item("站台設定", "admin:sites_site_changelist",
              "bi bi-globe2", _can_view("sites.site")),
    ]),
]
