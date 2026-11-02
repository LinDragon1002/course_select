from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Exists, OuterRef, Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST
from urllib.parse import urlencode

from . import waitlist
from .academic import google_identity
from .credits import official_entry_credits
from .forms import AcademicSetupForm, cascade_context, selected_values
from .models import (Course, Enrollment, NtubOfferingSource, NtubTimetableSnapshot,
                    NtubTimetableEntry, OfficialCourseCredit, OfficialTeachingSyllabus,
                    CourseTeachingSyllabus, Section, SectionClassPolicy,
                    StudentAcademicProfile, Term, Waitlist)
from .ntub_schedule_grid import (DAYS, KIND_ENROLLMENT, KIND_OFFICIAL_ACTIVITY,
                                 KIND_OFFICIAL_COURSE, KIND_OFFICIAL_ENROLLMENT,
                                 GridItem, base_periods_for, build_week_grid)
from .services import drop_enrollment, register
from .standing import get_standing
from .verdict import evaluate
from .window import window_gate


def _redirect_next(request, fallback, *args):
    """表單帶的 next 只接受同一站台的網址，避免變成開放轉址。"""
    nxt = request.POST.get("next", "")
    if nxt and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}):
        return redirect(nxt)
    return redirect(fallback, *args)


def _window_context(term):
    """選課時間窗狀態列；兩欄皆空（不限）回 None，頁面不顯示。"""
    if not term.has_window:
        return None
    state, _ = window_gate(term)
    if state == "not_open":
        text = f"選課 {term.enroll_open:%Y/%m/%d %H:%M} 開放"
    elif state == "closed":
        text = f"選課已截止（{term.enroll_close:%Y/%m/%d %H:%M} 截止），已加選者仍可退選"
    else:
        text = "加選中"
    return {"state": state, "text": text}


#: 政策擋下的動作：這些按鈕一律是停用狀態，理由另外顯示。
POLICY_BLOCKED_ACTIONS = ("blocked_policy", "blocked_required")


def _visibility_q(user, class_group, term, *, include_required=False):
    """這個學生看得到的開課（`Q` 條件，套在 Section 查詢上）。

    有學籍的學生（`class_group` 不是 None）：只看得到**自己班級已發布的
    修課政策**所指定的開課，而且政策必須 `can_enroll=True`（可以自行加選）
    ——這是「學生搜尋課程」可見性的唯一依據（不看課名、課號、推薦資料或
    來源開課班別，也不從政策以外的欄位推測資格）。

    另外保留兩條路徑讓學生能收尾：已加選（可退選）、已在等待名單（可取消
    等待）。

    `include_required=True`（課程詳情頁用）：政策只要已發布就算，不限
    `can_enroll`，這樣「本班必修」在詳情頁仍然看得到並標出原因。

    沒有學籍的使用者（未登入或尚未設定學籍）沿用原本的顯示規則：不加條件。

    用子查詢（`id__in`）而不是 join：一個開課只會出現一次，不會因為多筆
    政策而重複。
    """
    if class_group is None:
        return Q()
    policies = SectionClassPolicy.objects.filter(
        class_group=class_group,
        status=SectionClassPolicy.Status.PUBLISHED,
        section__course__term=term)
    if not include_required:
        policies = policies.filter(can_enroll=True)
    scope = Q(id__in=policies.values("section_id"))
    if user is not None and user.is_authenticated:
        scope |= Q(id__in=Enrollment.objects.filter(
            user=user, section__course__term=term).values("section_id"))
        scope |= Q(id__in=Waitlist.objects.filter(
            user=user, promoted_at__isnull=True,
            section__course__term=term).values("section_id"))
    return scope



def _section_verdicts(standing, sections, window, authenticated=True):
    """把每個班別的判定結果掛到班別物件上（列表與詳情共用同一份呈現）。

    policy_reason 是「為什麼不能加選」的理由，只在政策擋下時有值；
    頁面把它顯示出來，讓「看得到但不能加選」的課程有明確說明。
    """
    for s in sections:
        verdict = evaluate(standing, s, window)
        s.action = verdict.action
        s.wait_position = verdict.position
        s.policy_reason = (verdict.message
                           if s.action in POLICY_BLOCKED_ACTIONS and authenticated
                           else "")
    return sections


def _course_page_schedule(user, term, standing):
    """建立選課頁的精簡課表，沿用我的課表相同的來源與衝堂資料。"""
    if not user.is_authenticated or term is None:
        return build_week_grid([], base_periods=0)

    profile = (StudentAcademicProfile.objects
               .select_related("class_group__department__program_type")
               .filter(user=user).first())
    program_code = (
        profile.class_group.department.program_type.code
        if profile is not None else "")
    official_entries = list(standing.official_entries)
    mine = list(standing.sections)
    merged_entry_ids = set()
    for section in mine:
        entry = _match_official_entry(section, official_entries)
        section.official_entry = entry
        if entry is not None:
            merged_entry_ids.add(entry.pk)

    items = []
    for entry in official_entries:
        if entry.pk in merged_entry_ids:
            continue
        items.append(GridItem(
            kind=KIND_OFFICIAL_COURSE,
            day=entry.weekday,
            start_period=entry.start_period,
            end_period=entry.end_period,
            title=entry.course_name,
            subtitle=entry.teacher_name,
            meta=entry.classroom,
            url=reverse("official_course_detail", args=[entry.pk]),
        ))
    for section in mine:
        entry = getattr(section, "official_entry", None)
        if entry is not None:
            items.append(GridItem(
                kind=KIND_OFFICIAL_ENROLLMENT,
                day=section.day,
                start_period=section.start_period,
                end_period=section.end_period,
                title=section.course.name,
                subtitle=section.label_display,
                meta=section.room or "教室未定",
                url=reverse("course_detail", args=[section.course.pk]),
            ))
        else:
            items.append(GridItem(
                kind=KIND_ENROLLMENT,
                day=section.day,
                start_period=section.start_period,
                end_period=section.end_period,
                title=section.course.name,
                subtitle=section.label_display,
                meta=section.room or "教室未定",
                url=reverse("course_detail", args=[section.course.pk]),
            ))
    return build_week_grid(
        items,
        days=DAYS[:5],
        base_periods=base_periods_for(program_code),
    )


def login_page(request):
    """登入頁：只有「使用 NTUB Google 帳號登入」一個入口。

    學生身分由 Google 驗證（伺服器端檢查 @ntub.edu.tw 與 email_verified），
    這頁不提供任何繞過驗證的登入方式。
    """
    if request.user.is_authenticated:
        return redirect("course_list")
    return render(request, "courses/login.html", {"current_term": Term.current()})


def course_list(request):
    """課程列表：每行是一個班別。

    官方開課能不能加選由班別修課政策決定（policy_missing / policy_closed），
    但課程本身仍然看得到：看得到卻不能加選時，會在列上標出原因。
    """
    q = request.GET.get("q", "").strip()
    day = request.GET.get("day", "")
    slot = request.GET.get("slot", "")
    term = Term.current()
    standing = get_standing(request.user, term)
    # 有學籍的學生只看得到自己班級已發布且 can_enroll=True 的政策所指派的班別；
    # 已加選／已在等待名單的班別仍看得到（為了退選與取消等待）。
    scope = _visibility_q(request.user, standing.class_group, term)
    sections = (Section.objects.filter(course__term=term)
                .filter(scope)
                .select_related("course__teacher")
                .prefetch_related("course__prerequisites", "ntub_offering_sources")
                .annotate(enrolled=Count("enrollments", distinct=True),
                          waitlist_count=Count("waitlists", distinct=True,
                                                filter=Q(waitlists__promoted_at__isnull=True)))
                .distinct())
    if q:
        sections = sections.filter(Q(course__name__icontains=q) | Q(course__code__icontains=q)
                                   | Q(course__teacher__name__icontains=q))
    if day.isdigit():
        sections = sections.filter(day=int(day))
    if slot in Section.PERIOD_SLOT_RANGES:
        lo, hi = Section.PERIOD_SLOT_RANGES[slot]
        sections = sections.filter(start_period__gte=lo)
        if hi:
            sections = sections.filter(start_period__lte=hi)
    window = window_gate(term)
    authenticated = request.user.is_authenticated
    sections = list(sections)
    _section_verdicts(standing, sections, window, authenticated)
    for s in sections:
        s.prereq_list = [(c, c.code in standing.passed) for c in sorted(s.course.prerequisites.all(), key=lambda c: c.code)]
    full_count = sum(1 for s in sections if s.is_full)
    # 「可加選」的班別數：加選或加入等待都算（政策已發布且對這個學生有效）
    enrollable_count = sum(1 for s in sections if s.action in ("enroll", "waitlist"))
    student_scoped = standing.class_group is not None
    # 沒有可加選的班別時顯示說明：有學籍的學生即使列表是空的也要說明原因。
    show_empty_enroll_note = (
        authenticated and enrollable_count == 0 and (bool(sections) or student_scoped))
    credit_summary = standing.credit_summary
    return render(request, "courses/list.html", {
        "sections": sections, "q": q, "day": day, "slot": slot,
        "days": Section.DAYS, "slots": Section.PERIOD_SLOTS, "full_count": full_count,
        "window": _window_context(term),
        "show_prereq_marks": authenticated,
        "enrollable_count": enrollable_count if authenticated else None,
        "student_scoped": student_scoped,
        "show_empty_enroll_note": show_empty_enroll_note,
        "credit_summary": credit_summary,
        "schedule_grid": _course_page_schedule(request.user, term, standing),
        "course_notices": [],
    })


def course_detail(request, pk):
    course = get_object_or_404(
        Course.objects.select_related(
            "teacher", "term", "owning_department", "teaching_syllabus",
        ),
        pk=pk,
    )
    # evaluate() 每列會碰 section.course 與 course.prerequisites，必須 prefetch，
    # 否則每列多 2 筆查詢（與 course_list 同一支配方）
    standing = get_standing(request.user, course.term)
    # 有學籍的學生在詳情頁只看得到自己班級有已發布政策的班別（政策不一定要
    # can_enroll，讓「本班必修」仍然看得到並標出原因），以及自己已加選／等待的。
    scope = _visibility_q(request.user, standing.class_group, course.term,
                          include_required=True)
    sections = list(Section.objects.filter(course=course)
                    .filter(scope)
                    .select_related("course")
                    .prefetch_related("course__prerequisites", "ntub_offering_sources")
                    .annotate(enrolled=Count("enrollments", distinct=True),
                              waitlist_count=Count("waitlists", distinct=True,
                                                   filter=Q(waitlists__promoted_at__isnull=True)))
                    .distinct())
    window = window_gate(course.term)
    _section_verdicts(standing, sections, window, request.user.is_authenticated)
    my_section = next((s for s in sections if s.action == "drop"), None)
    # 匿名使用者的 id 與 teacher.user_id 都可能是 None，先擋掉未登入
    is_teacher = request.user.is_authenticated and course.teacher.user_id == request.user.id
    related = list(Course.objects.filter(teacher=course.teacher, term=course.term).exclude(pk=pk)
                   .select_related("teacher")[:4])
    prereqs = [(c, c.code in standing.passed) for c in sorted(course.prerequisites.all(), key=lambda c: c.code)]
    syllabus_filter = {
        "academic_year": course.term.code.split("-", 1)[0],
        "semester": course.term.code.split("-", 1)[1],
        "course_code": course.code,
        "status": "success",
    }
    if standing.class_group is not None:
        syllabus_filter["class_code"] = standing.class_group.code
    official_syllabus = OfficialTeachingSyllabus.objects.filter(
        **syllabus_filter).order_by("-updated_at").first()
    official_credit_conflicts = []
    if official_syllabus is not None:
        credit_filter = {"term": course.term, "course_code": course.code}
        if standing.class_group is not None:
            credit_filter["class_group"] = standing.class_group
        official_credit_conflicts = list(
            OfficialCourseCredit.objects.filter(**credit_filter).exclude(
                credits=official_syllabus.credits).values_list(
                    "class_group__code", "credits")
        )
    list_params = {
        key: request.GET.get(key, "").strip()
        for key in ("q", "day", "slot")
        if request.GET.get(key, "").strip()
    }
    course_list_url = reverse("course_list")
    if list_params:
        course_list_url += "?" + urlencode(list_params)
    teaching_syllabus = getattr(course, "teaching_syllabus", None)
    if (teaching_syllabus is not None
            and teaching_syllabus.status != CourseTeachingSyllabus.Status.PUBLISHED):
        teaching_syllabus = None
    teaching_syllabus_weeks = []
    if teaching_syllabus is not None:
        week_labels = (
            "第一週", "第二週", "第三週", "第四週", "第五週", "第六週",
            "第七週", "第八週", "第九週", "第十週", "第十一週", "第十二週",
            "第十三週", "第十四週", "第十五週", "第十六週", "第十七週",
            "第十八週",
        )
        weekly_progress = teaching_syllabus.weekly_progress or {}
        teaching_syllabus_weeks = [
            {"label": label, "content": weekly_progress.get(f"week_{week:02d}")}
            for week, label in enumerate(week_labels, start=1)
        ]
    term_parts = course.term.code.split("-", 1)
    return render(request, "courses/detail.html", {
        "course": course, "sections": sections, "my_section": my_section, "is_teacher": is_teacher, "related": related,
        "window": _window_context(course.term),
        "prereqs": prereqs, "show_prereq_marks": request.user.is_authenticated,
        "official_syllabus": official_syllabus,
        "official_credit_conflicts": official_credit_conflicts,
        "teaching_syllabus": teaching_syllabus,
        "teaching_syllabus_weeks": teaching_syllabus_weeks,
        "course_term_year": term_parts[0],
        "course_term_semester": (
            f"第 {term_parts[1]} 學期" if len(term_parts) == 2 else "未提供"
        ),
        "course_list_url": course_list_url,
    })


@login_required
@require_POST
def enroll(request, pk):
    section = get_object_or_404(Section.objects.select_related("course"), pk=pk)
    result = register(request.user, section)
    # severity 是 Verdict interface 的一部分（success/info/warning/error＝django messages 等級）
    getattr(messages, result.severity)(request, result.message)
    return _redirect_next(request, "course_detail", section.course_id)


@login_required
@require_POST
def drop(request, pk):
    section = get_object_or_404(Section.objects.select_related("course"), pk=pk)
    if drop_enrollment(request.user, section):
        messages.success(request, f"已退選「{section.course.name} {section.label_display}」。")
    else:
        messages.info(request, "你沒有選這個班別。")
    return _redirect_next(request, "my_schedule")


@login_required
@require_POST
def cancel_wait(request, pk):
    section = get_object_or_404(Section.objects.select_related("course"), pk=pk)
    if waitlist.cancel(request.user, section):
        messages.success(request, f"已取消等待「{section.course.name} {section.label_display}」。")
    else:
        messages.info(request, "你沒有在等這個班別。")
    return _redirect_next(request, "my_schedule")


@login_required
@require_POST
def ack_promotion(request):
    """明確確認遞補通知（banner 的「知道了」）；GET 不寫庫。"""
    waitlist.mark_promotions_read(request.user)
    return _redirect_next(request, "course_list")


def _match_official_entry(section, entries):
    """個人加選的班別對應到班級官方課表的哪一筆（同一課號且同一時段）。

    對應到 → 課表上合併成一個時段（官方與本站兩邊的資料都保留）。
    對應不到 → 各自顯示（不同課程本來就該分開看）。
    """
    code = (section.course.code or "").strip()
    if not code:
        return None
    for entry in entries:
        if entry.entry_type != "course":
            continue
        if (entry.course_code or "").strip() != code:
            continue
        if entry.weekday != section.day:
            continue
        if not (section.start_period <= entry.end_period
                and entry.start_period <= section.end_period):
            continue
        return entry
    return None


@login_required
def my_schedule(request):
    """一張統一課表：班級官方課表 + 個人加選。

    官方課表與個人加選都只取 `Term.current()` 這個學期，兩者不會混在一起：
    官方來源是 `NtubTimetableSnapshot`（班級 × 學期的固定課表），個人來源是
    同一學期的 `Enrollment`（見 `standing.enrolled_sections`）。

    官方課表只是「班級官方課程」，不代表全部都是必修，也不會建立任何
    `Enrollment`；官方活動（共同空堂、課外活動）只是時間框架，不計學分。
    """
    term = Term.current()
    standing = get_standing(request.user, term)
    # mine 裡的班別必定已加選、動作必定是退選（退選不受時間窗影響）
    mine = list(standing.sections)
    for s in mine:
        s.action = "drop"

    profile = (StudentAcademicProfile.objects
               .select_related("class_group__department__program_type")
               .filter(user=request.user).first())

    # 四技日間部的基底是第 1～9 節；其他學制依實際排到的節次決定。
    program_code = (profile.class_group.department.program_type.code
                    if profile is not None else "")
    base_periods = base_periods_for(program_code)

    # 班級人工發布版優先；無人工版本時才顯示保留的官方匯入版。
    snapshot = None
    if profile is not None and term is not None:
        snapshot = (NtubTimetableSnapshot.objects
                    .filter(class_group=profile.class_group, term=term)
                    .filter(status=NtubTimetableSnapshot.Status.PUBLISHED)
                    .order_by("-source_kind", "-published_at", "-pk")
                    .prefetch_related("entries")
                    .first())

    items = []
    official_courses = []
    official_course_rows = []
    weekday_labels = {day_no: label for day_no, label in DAYS}

    # 個人加選與班級官方課表是同一門課時合併顯示：官方那一筆不重複佔一個時段，
    # 但官方資料（教師、教室）與本站資料（班別、教室、老師）都保留。
    snapshot_entries = (
        [entry for entry in snapshot.entries.all() if entry.is_active]
        if snapshot else []
    )
    official_course_entries = [
        entry for entry in snapshot_entries if entry.entry_type == "course"
    ]
    credit_summary = standing.credit_summary
    effective_credits = official_entry_credits(official_course_entries)
    merged_entry_ids = set()
    for s in mine:
        entry = _match_official_entry(s, official_course_entries)
        s.official_entry = entry
        if entry is not None:
            merged_entry_ids.add(entry.pk)

    if snapshot is not None:
        for entry in snapshot_entries:
            if entry.entry_type == "course":
                kind = KIND_OFFICIAL_COURSE
                official_courses.append(entry)
                credit = effective_credits.get(
                    (term.code, entry.snapshot.class_group.code,
                     entry.official_serial, entry.course_code))
                official_course_rows.append({
                    "name": entry.course_name,
                    "code": entry.course_code,
                    "teacher": entry.teacher.name if entry.teacher_id else entry.teacher_name,
                    "classroom": entry.classroom,
                    "weekday_label": weekday_labels.get(entry.weekday, ""),
                    "start_period": entry.start_period,
                    "end_period": entry.end_period,
                    "credits": credit.value if credit else None,
                    "credit_basis": credit.basis if credit else "待同步",
                    "course": Course.objects.filter(
                        term=term, code=entry.course_code).first(),
                    "url": reverse("official_course_detail", args=[entry.pk]),
                })
            else:
                kind = KIND_OFFICIAL_ACTIVITY
            if entry.pk in merged_entry_ids:
                # 已與個人加選合併成一個時段，不重複顯示
                continue
            items.append(GridItem(
                kind=kind,
                day=entry.weekday,
                start_period=entry.start_period,
                end_period=entry.end_period,
                title=entry.course_name,
                subtitle=entry.teacher_name,
                meta=entry.classroom,
                url=(reverse("official_course_detail", args=[entry.pk])
                     if entry.entry_type == "course" else None),
            ))

    for s in mine:
        entry = getattr(s, "official_entry", None)
        if entry is not None:
            items.append(GridItem(
                kind=KIND_OFFICIAL_ENROLLMENT,
                day=s.day,
                start_period=s.start_period,
                end_period=s.end_period,
                title=s.course.name,
                subtitle=f"{s.label_display} · 官方課表＋本站加選",
                meta=(f"官方 {entry.teacher_name or '教師未定'} {entry.classroom or '教室未定'}"
                      f" · 本站 {s.room or '教室未定'} {s.course.teacher}"),
                url=reverse("course_detail", args=[s.course.pk]),
            ))
            continue
        items.append(GridItem(
            kind=KIND_ENROLLMENT,
            day=s.day,
            start_period=s.start_period,
            end_period=s.end_period,
            title=s.course.name,
            subtitle=s.label_display,
            meta=f"{s.room} · {s.course.teacher}" if s.room else str(s.course.teacher),
            url=reverse("course_detail", args=[s.course.pk]),
        ))

    grid = build_week_grid(items, base_periods=base_periods)
    return render(request, "courses/schedule.html", {
        "mine": mine,
        "grid": grid,
        "term": term,
        "profile": profile,
        "snapshot": snapshot,
        "official_courses": official_courses,
        "official_course_rows": official_course_rows,
        "official_courses_count": len(official_courses),
        "credit_summary": credit_summary,
        "total": credit_summary.total,
        "max": credit_summary.limits.maximum if credit_summary.limits else None,
        "min": credit_summary.limits.minimum if credit_summary.limits else None,
        "minimum_gap": (
            max(credit_summary.limits.minimum - credit_summary.total, 0)
            if credit_summary.limits else None),
        "available_credits": (
            max(credit_summary.limits.maximum - credit_summary.total, 0)
            if credit_summary.limits else None),
        "below_minimum": (
            credit_summary.complete and credit_summary.limits is not None
            and credit_summary.total < credit_summary.limits.minimum),
    })


@login_required
def official_course_detail(request, entry_id):
    """顯示目前學生所屬班級的官方課表課程與教學大綱。"""
    term = Term.current()
    profile = (StudentAcademicProfile.objects
               .select_related("class_group")
               .filter(user=request.user).first())
    if profile is None or term is None:
        raise Http404
    entry = get_object_or_404(
        NtubTimetableEntry.objects.select_related(
            "snapshot__term", "snapshot__class_group", "teacher", "course",
        ),
        pk=entry_id,
        snapshot__term=term,
        snapshot__class_group=profile.class_group,
        snapshot__status=NtubTimetableSnapshot.Status.PUBLISHED,
        is_active=True,
        entry_type="course",
    )
    course = entry.course or Course.objects.filter(
        term=term, code=entry.course_code,
    ).first()
    teaching_syllabus = None
    if course is not None:
        teaching_syllabus = CourseTeachingSyllabus.objects.filter(
            course=course,
            status=CourseTeachingSyllabus.Status.PUBLISHED,
        ).first()
    syllabus = teaching_syllabus or OfficialTeachingSyllabus.objects.filter(
        academic_year=term.code.split("-", 1)[0],
        semester=term.code.split("-", 1)[1],
        class_code=entry.snapshot.class_group.code,
        official_serial=entry.official_serial,
        course_code=entry.course_code,
        status="success",
    ).first()
    week_labels = (
        "第一週", "第二週", "第三週", "第四週", "第五週", "第六週",
        "第七週", "第八週", "第九週", "第十週", "第十一週", "第十二週",
        "第十三週", "第十四週", "第十五週", "第十六週", "第十七週",
        "第十八週",
    )
    weekly_progress = (syllabus.weekly_progress or {}) if syllabus else {}
    weekly_rows = [
        {"label": label, "content": weekly_progress.get(f"week_{week:02d}")}
        for week, label in enumerate(week_labels, start=1)
    ]
    academic_year, _, semester = entry.snapshot.term.code.partition("-")
    return render(request, "courses/official_course_detail.html", {
        "entry": entry,
        "snapshot": entry.snapshot,
        "syllabus": syllabus,
        "display_credits": (
            entry.credits if entry.credits is not None
            else getattr(syllabus, "credits", None)
        ),
        "display_weekly_hours": (
            entry.weekly_hours if entry.weekly_hours is not None
            else getattr(syllabus, "weekly_hours", None)
        ),
        "display_teacher_name": (
            entry.teacher.name if entry.teacher_id
            else (getattr(syllabus, "teacher_name", "") or entry.teacher_name)
        ),
        "academic_year": getattr(syllabus, "academic_year", academic_year),
        "semester_label": f"第 {semester} 學期" if semester else None,
        "weekly_rows": weekly_rows,
    })


@login_required
def teaching_courses(request):
    courses = list(Course.objects.filter(teacher__user=request.user, term=Term.current())
                   .select_related("teacher"))
    by_course = {}
    for s in (Section.objects.filter(course__teacher__user=request.user, course__term=Term.current())
              .select_related("course").annotate(enrolled=Count("enrollments"))):
        by_course.setdefault(s.course_id, []).append(s)
    for c in courses:
        c.section_list = by_course.get(c.pk, [])
    return render(request, "courses/teaching.html", {"courses": courses})


@login_required
def course_roster(request, pk):
    course = get_object_or_404(Course.objects.select_related("teacher"), pk=pk, teacher__user=request.user)
    sections = list(Section.objects.filter(course=course))
    enrollments = (Enrollment.objects.filter(section__course=course)
                   .select_related("user", "section").order_by("created"))
    return render(request, "courses/roster.html", {
        "course": course, "sections": sections, "enrollments": enrollments,
    })


# --- 學籍設定 / 個人資料 --------------------------------------------------------


def _safe_next(request, fallback="course_list"):
    """表單帶的 next 只接受同一站台的網址。"""
    nxt = request.POST.get("next") or request.GET.get("next") or ""
    if nxt and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}):
        return nxt
    return None


def _academic_form_initial(profile):
    """從既有學籍帶出四階選單的初始值（系所、學制、年級都從班級關聯取得）。"""
    if profile is None:
        return {}
    group = profile.class_group
    return {
        "program_type": group.department.program_type_id,
        "department": group.department_id,
        "grade": group.grade_no,
        "class_group": group.pk,
    }


@login_required
def onboarding(request):
    """首次學籍設定：學制 → 系所 → 年級 → 班級。

    設定完成後回到原本想去的頁面；已經設定過的學生不重複設定。
    """
    profile = StudentAcademicProfile.objects.filter(user=request.user).first()
    if profile is not None:
        return redirect(_safe_next(request) or "course_list")

    if request.method == "POST":
        form = AcademicSetupForm(request.POST)
        if form.is_valid():
            StudentAcademicProfile.objects.update_or_create(
                user=request.user,
                defaults={"class_group": form.cleaned_data["class_group"],
                          "completed_at": timezone.now()},
            )
            messages.success(request, "學籍設定完成，開始選課吧！")
            return redirect(_safe_next(request) or "course_list")
    else:
        form = AcademicSetupForm()

    return render(request, "courses/onboarding.html", {
        "form": form,
        "selected": selected_values(form),
        "cascade": cascade_context(),
        "google_name": google_identity(request.user)[0],
        "google_email": google_identity(request.user)[1],
    })


@login_required
def my_profile(request):
    """個人資料：Google 姓名／Email 唯讀顯示，學籍可用四階選單修改。

    學籍的修改走同一份表單與同一套伺服器端驗證（班級必須真的屬於該系所與年級）。
    """
    profile = StudentAcademicProfile.objects.filter(user=request.user).first()
    if profile is None:
        return redirect("onboarding")

    if request.method == "POST":
        form = AcademicSetupForm(request.POST)
        if form.is_valid():
            profile.class_group = form.cleaned_data["class_group"]
            profile.completed_at = timezone.now()
            profile.save(update_fields=["class_group", "completed_at"])
            messages.success(request, "學籍已更新。")
            return redirect("my_profile")
    else:
        form = AcademicSetupForm(initial=_academic_form_initial(profile))

    google_name, google_email = google_identity(request.user)
    group = profile.class_group
    return render(request, "courses/profile.html", {
        "form": form,
        "selected": selected_values(form),
        "cascade": cascade_context(),
        "profile": profile,
        "class_group": group,
        "program_type": group.department.program_type,
        "department": group.department,
        "google_name": google_name,
        "google_email": google_email,
    })
