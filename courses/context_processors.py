"""模板 context processors。"""
from . import waitlist
from .maintainer import scoped_class_groups, scoped_courses, scoped_sections
from .admin_navigation import can_bulk_assign, can_view_model
from .credits import official_entry_credits
from .models import SectionClassPolicy, NtubTimetableEntry, Term
from django.urls import reverse


def ui_shell(request):
    """App shell 需要的顯示資料（學期代碼）。不改業務邏輯。"""
    if request.path.startswith("/admin/"):
        return {}
    term = Term.current()
    term_label = ""
    if term:
        year, separator, semester = term.code.partition("-")
        term_label = f"{year} 學年度 第 {semester} 學期" if separator else term.code
    return {"current_term": term, "current_term_label": term_label}


def waitlist_banner(request):
    """遞補成功通知：只 SELECT、不寫庫（GET 不該有狀態變更）。

    清除「已讀」由明確的 POST ack_promotion 處理，任何模板渲染
    （含 admin、allauth）都不會吃掉通知。
    """
    user = request.user
    if not user.is_authenticated:
        return {}
    items = list(waitlist.unread_promotions(user).select_related("section__course"))
    if not items:
        return {}
    return {"waitlist_banner": [f"{w.section.course.name} {w.section.label_display}" for w in items]}


def admin_dashboard(request):
    """Admin 首頁的真實統計；只在 dashboard 路徑查詢，避免影響其他頁面。"""
    if request.path.rstrip("/") != "/admin" or not request.user.is_authenticated:
        return {}

    term = Term.current()
    courses = scoped_courses(request.user)
    sections = scoped_sections(request.user)
    groups = scoped_class_groups(request.user)
    policies = SectionClassPolicy.objects.filter(class_group__in=groups)
    pending_keys = set(
        NtubTimetableEntry.objects.filter(
            snapshot__class_group__in=groups,
            snapshot__term=term,
            entry_type="course",
        ).exclude(course_code="").values_list(
            "snapshot__class_group_id", "snapshot__term_id", "course_code"
        ).distinct()
    ) if term is not None else set()
    entries = list(NtubTimetableEntry.objects.filter(
        snapshot__class_group__in=groups,
        snapshot__term=term,
        entry_type="course",
    ).select_related("snapshot__term", "snapshot__class_group")) if term else []
    effective = official_entry_credits(entries)
    pending_credits = sum(1 for item in effective.values() if item.value is None)
    action_specs = [
        ("課程資料", "bi bi-1-circle", "admin:courses_course_changelist",
         can_view_model("courses.course", request)),
        ("開課班別", "bi bi-2-circle", "admin:courses_section_changelist",
         can_view_model("courses.section", request)),
        ("批次安排課程", "bi bi-3-circle", "admin:courses_sectionclasspolicy_bulk_assign",
         can_bulk_assign(request)),
        ("管理班別修課政策", "bi bi-4-circle",
         "admin:courses_sectionclasspolicy_changelist",
         can_view_model("courses.sectionclasspolicy", request)),
    ]

    return {
        "dashboard_term": term,
        "dashboard_stats": {
            "course_count": courses.filter(term=term).count() if term else 0,
            "section_count": sections.filter(course__term=term).count() if term else 0,
            "published_policy_count": policies.filter(
                section__course__term=term,
                status=SectionClassPolicy.Status.PUBLISHED,
            ).count() if term else 0,
            "draft_policy_count": policies.filter(
                section__course__term=term,
                status=SectionClassPolicy.Status.DRAFT,
            ).count() if term else 0,
            "pending_credit_count": pending_credits,
        },
        "dashboard_draft_policies": policies.filter(
            section__course__term=term,
            status=SectionClassPolicy.Status.DRAFT,
        ).select_related(
            "section__course", "class_group"
        ).order_by("-updated_at")[:8] if term else [],
        "dashboard_actions": [
            {"label": label, "icon": icon, "url": reverse(route)}
            for label, icon, route, allowed in action_specs if allowed
        ],
    }
