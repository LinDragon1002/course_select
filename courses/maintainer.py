"""課程維護員（Course Maintainer）的權限判斷共用邏輯。

權限分兩層，兩層都在伺服器端檢查：

1. **能不能進 /admin/**：Django 內建的 Group + Model Permission。
   群組名稱固定為 `Course Maintainer`，權限由 `setup_course_maintainer_group`
   指令建立（可重複執行）。這裡不動 `is_superuser`。
2. **進去之後能動到哪些資料**：`CourseMaintainerScope` 指定的系所。
   所有檢查都走這裡的函式，Admin 不自己重寫一套。

維護員帳號多半是既有 Google 登入的學生帳號：信箱網域本身不帶任何權限，
一定要 superuser 明確加進群組並指定負責系所才會有管理能力。
"""

from django.contrib.auth.models import Group, Permission
from django.db.models import Q, QuerySet

from .models import (AcademicClassGroup, AcademicDepartment, Course, CourseMaintainerScope,
                     Section, SectionClassPolicy)

# 群組名稱：superuser 在 Django Admin 指派維護員時看到的名稱
MAINTAINER_GROUP_NAME = "Course Maintainer"

# 維護員在後台需要的基本模型權限（只看 + 政策管理）。
# 刻意不包含 auth.* 、socialaccount.* 、courses.enrollment 、courses.waitlist：
# 維護員不該改 OAuth、User 權限、選課與候補紀錄。
MAINTAINER_PERMISSION_CODENAMES = [
    "courses.view_academicclassgroup",
    "courses.view_academicdepartment",
    "courses.view_academicprogramtype",
    "courses.view_course",
    "courses.add_course",
    "courses.change_course",
    "courses.delete_course",
    "courses.view_ntubcurriculumrecommendation",
    "courses.view_ntubofferingambiguity",
    "courses.view_ntubofferingsource",
    "courses.view_ntubrecommendationcandidate",
    "courses.view_ntubtimetableentry",
    "courses.view_ntubtimetablesnapshot",
    "courses.delete_ntubtimetablesnapshot",
    "courses.delete_ntubtimetableentry",
    "courses.view_officialteachingsyllabus",
    "courses.add_officialteachingsyllabus",
    "courses.change_officialteachingsyllabus",
    "courses.view_officialcoursecredit",
    "courses.add_officialcoursecredit",
    "courses.change_officialcoursecredit",
    "courses.delete_officialcoursecredit",
    "courses.view_section",
    "courses.add_section",
    "courses.change_section",
    "courses.delete_section",
    "courses.add_sectionclasspolicy",
    "courses.change_sectionclasspolicy",
    "courses.delete_sectionclasspolicy",
    "courses.view_sectionclasspolicy",
]


def is_superuser(user):
    """superuser 才能管理維護員權限範圍。"""
    return bool(user and user.is_authenticated and user.is_superuser)


def is_maintainer(user):
    """是不是課程維護員（群組成員且帳號有效）。"""
    if not user or not user.is_authenticated or not user.is_active:
        return False
    if user.is_superuser:
        return True
    return user.groups.filter(name=MAINTAINER_GROUP_NAME).exists()


def maintainer_group():
    """取得（必要時建立）課程維護員群組。"""
    group, _ = Group.objects.get_or_create(name=MAINTAINER_GROUP_NAME)
    return group


def ensure_maintainer_group(apply=True):
    """建立群組並把權限**對齊**到清單。

    可重複執行：權限是對齊而不是累加，多跑幾次結果一樣。
    刻意不把任何學生加入群組——那是 superuser 在 Admin 指派權限範圍時的事。

    `apply=False`（dry-run）時完全不寫入資料。
    回傳 dict：group / created / added / removed / missing。
    """
    wanted = []
    missing = []
    for codename in MAINTAINER_PERMISSION_CODENAMES:
        app_label, _, name = codename.partition(".")
        perm = Permission.objects.filter(
            content_type__app_label=app_label, codename=name).first()
        if perm is None:
            missing.append(codename)
        else:
            wanted.append(perm)

    group = Group.objects.filter(name=MAINTAINER_GROUP_NAME).first()
    created = group is None
    if created:
        to_add = list(wanted)
        to_remove = []
        if apply:
            group = maintainer_group()
            if to_add:
                group.permissions.add(*to_add)
    else:
        current = set(group.permissions.values_list("id", flat=True))
        to_add = [p for p in wanted if p.id not in current]
        to_remove = [group.permissions.get(pk=pid) for pid in current - {p.id for p in wanted}]
        if apply:
            if to_add:
                group.permissions.add(*to_add)
            if to_remove:
                group.permissions.remove(*to_remove)
    return {"group": group, "created": created, "added": to_add,
            "removed": to_remove, "missing": missing}


def grant_maintainer_access(user) -> bool:
    """把帳號放進維護員群組並開啟 staff 權限（不碰 is_superuser）。

    被授權的帳號多半是 Google 登入的既有學生帳號，沒有密碼也沒關係：
    Django Admin 判斷能不能進來看的是 `is_staff`，登入身分則沿用 Google 登入
    的 session。
    """
    if user is None:
        return False
    # 先把群組權限對齊，維護員才真的有東西可用（權限不足時只是看得到、不能改）。
    ensure_maintainer_group()
    group = maintainer_group()
    if not user.groups.filter(pk=group.pk).exists():
        user.groups.add(group)
    if not user.is_staff:
        user.is_staff = True
        user.save(update_fields=["is_staff"])
    return True


def revoke_maintainer_access(user) -> bool:
    """收回維護員權限：移出群組，若已無任何有效範圍就一併關掉 staff。

    只在沒有其他有效 `CourseMaintainerScope` 時才關掉 is_staff，避免把還有
    權限的維護員踢出後台；superuser 本來就有全部權限，不動他的 is_staff。
    """
    if user is None:
        return False
    group = Group.objects.filter(name=MAINTAINER_GROUP_NAME).first()
    still_scoped = CourseMaintainerScope.objects.filter(user=user, is_active=True).exists()
    if still_scoped:
        # 停用其中一筆範圍時，其他有效範圍仍需要完整的維護員存取權。
        grant_maintainer_access(user)
        return True
    if group is not None:
        user.groups.remove(group)
    has_independent_group = (
        group is not None
        and user.groups.exclude(pk=group.pk).exists()
    )
    has_direct_permissions = user.user_permissions.exists()
    if (not user.is_superuser and not has_independent_group
            and not has_direct_permissions and user.is_staff):
        user.is_staff = False
        user.save(update_fields=["is_staff"])
    return True


def scoped_departments(user) -> QuerySet:
    """這個維護員被授權管理的系所。

    superuser 拿到全部；一般維護員只拿到 `CourseMaintainerScope` 指定且有效的系所。
    """
    if is_superuser(user):
        return AcademicDepartment.objects.all()
    if not user or not user.is_authenticated:
        return AcademicDepartment.objects.none()
    return AcademicDepartment.objects.filter(
        maintainer_scopes__user=user,
        maintainer_scopes__is_active=True,
    ).distinct()


def scoped_class_groups(user) -> QuerySet:
    """維護員可管理的學生班級（落在授權系所底下）。"""
    return AcademicClassGroup.objects.filter(department__in=scoped_departments(user))


def scoped_courses(user) -> QuerySet:
    """維護員可管理的課程：與授權系所的班級相關的課程。

    相關性有三個來源：官方開課曾針對該班級開課、該班級已有修課政策、
    官方課綱推薦提到該班級。三者都指向「這門課跟這個班級有關」。
    """
    if is_superuser(user):
        return Course.objects.all()
    if not user or not user.is_authenticated:
        return Course.objects.none()
    group_ids = scoped_class_groups(user).values_list("id", flat=True)
    return Course.objects.filter(
        Q(owning_department__in=scoped_departments(user))
        | Q(sections__ntub_offering_sources__class_group_id__in=group_ids)
        | Q(sections__class_policies__class_group_id__in=group_ids)
        | Q(ntub_recommendations__class_group_id__in=group_ids)
    ).distinct()


def scoped_sections(user) -> QuerySet:
    """維護員可管理的開課班別。

    同樣以「這個班別跟授權班級有關」來判斷：官方曾針對該班級開課、
    該班級已有政策、或該課程的課綱推薦提到該班級。
    """
    if is_superuser(user):
        return Section.objects.all()
    if not user or not user.is_authenticated:
        return Section.objects.none()
    group_ids = scoped_class_groups(user).values_list("id", flat=True)
    return Section.objects.filter(
        Q(course__owning_department__in=scoped_departments(user))
        |
        Q(ntub_offering_sources__class_group_id__in=group_ids)
        | Q(class_policies__class_group_id__in=group_ids)
        | Q(course__ntub_recommendations__class_group_id__in=group_ids)
    ).distinct()


def can_manage_section(user, section) -> bool:
    """維護員是否可以管理這個開課班別。"""
    if is_superuser(user):
        return True
    if not user or not user.is_authenticated or section is None:
        return False
    return scoped_sections(user).filter(pk=section.pk).exists()


def can_manage_policy(user, policy) -> bool:
    """維護員是否可以管理這條修課政策。

    判斷依據是政策的「適用班級」屬於哪個系所：跨班政策是由適用班級（學生）
    那個系所的維護員負責，而不是由開課班別的系所決定。
    """
    if is_superuser(user):
        return True
    if not user or not user.is_authenticated or policy is None:
        return False
    return scoped_departments(user).filter(pk=policy.class_group.department_id).exists()


def published_enrollment_policy(section, class_group):
    """取得某個班別對某個班級「已發布」的政策；沒有就回 None。

    這是下一階段（學生加選 / 候補判定）會用到的查詢入口：
    DRAFT 一律不算正式開放，所以只認 PUBLISHED；至於能不能加選，
    再看 policy.allows_enrollment（已發布 且 can_enroll=True）。
    """
    return SectionClassPolicy.objects.filter(
        section=section,
        class_group=class_group,
        status=SectionClassPolicy.Status.PUBLISHED,
    ).first()
