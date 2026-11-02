"""加選資格判定：「一個使用者對一個班別能做什麼」的規則單一來源。

register()（POST 加選的判定與 commit）與班別列按鈕（列表／詳情顯示）
都跨同一條 seam 讀 evaluate()，按鈕顯示什麼，POST 就回什麼。
POST 規則順序與 docs/business-rules.md §5 一致：
選課時間窗 → 同課不重複 → 先修已修 → 不衝堂 → 額滿轉等待 → 不超學分。
按鈕例外：「已加選這班／已排隊（班別仍額滿）」先於時間窗
（退選、取消等待不受時間窗影響，docs §7）。

官方開課（NtubOfferingSource）另有一關：**班別修課政策**。政策必須已發布
且可自行加選才開放；政策判定在 `enrollment_policy.policy_decision`，
官方班級課表的衝堂判定在 `enrollment_policy.official_conflicting_entry`。
政策在「已加選／已排隊」之後才檢查，所以政策被撤回時，已加選者仍可退選、
已排隊者仍可取消等待。

新規則只加這裡一個分支。
"""
from dataclasses import dataclass

from django.conf import settings

from .enrollment_policy import official_conflicting_entry, policy_decision


def missing_prereqs(course, passed):
    """使用者還沒修的先修課。「已修」＝課號在 passed（過去學期有同課號加選紀錄）。"""
    return [c for c in course.prerequisites.all() if c.code not in passed]


def same_course_section(sections, course):
    """sections 中同課程的已選班別；沒有回 None。（加選／遞補共用的資格規則）"""
    return next((s for s in sections if s.course_id == course.pk), None)


def conflicting_section(sections, section):
    """sections 中與 section 衝堂的已選班別；沒有回 None。（加選／遞補共用的資格規則）"""
    return next((s for s in sections if s.conflicts_with(section)), None)


@dataclass(frozen=True)
class Verdict:
    """一個使用者對一個班別的資格判定。

    code：現在 POST 加選會得到 register() 回傳的結果
        （enrolled / already / prereq / conflict / waitlisted /
          already_waitlisted / over_credits / not_open / closed /
          policy_missing / policy_closed）
    action：班別列按鈕要顯示的動作
        （drop / cancel_wait / enroll / waitlist / blocked_*）
    severity：register 回傳時 django messages 的等級
        （success / info / warning / error）
    position：等待名單順位（action=cancel_wait 時）
    """
    code: str
    message: str
    action: str
    severity: str
    position: int | None = None


def evaluate(standing, section, window):
    """使用者加選一個班別的資格判定。

    不主動查 DB，但會觸碰 section.course、course.prerequisites.all()、
    section.is_full 這些 lazy 關聯：呼叫端在迴圈裡用它時必須
    select_related／prefetch_related／annotate，否則每列多幾筆查詢（N+1）。

    standing 是該使用者在本學期的 Standing（standing.get_standing）；
    window 是 window_gate 的傳回值 (state, message)。
    三態之外的 window state 丟 ValueError（vocabulary 只由 window_gate 產生）。

    standing 另帶著班別修課政策（standing.policies）與班級官方課表
    （standing.official_entries），官方開課的加選判定會用到它們。

    注意：waitlisted 的 message 是預佔的；register commit 出等待列後
    才補上真實的「第 N 位」。
    """
    state, window_message = window
    if state not in ("not_open", "open", "closed"):
        raise ValueError(f"未知的選課時間窗狀態：{state!r}")
    course = section.course
    sections = standing.sections

    # 退選／取消等待不受時間窗影響（docs §7）：「已加選這班／已排隊」
    # 先於窗口判定；code 仍照 register() 的 window-first 回時間窗結果
    if (current := same_course_section(sections, course)):
        if state != "open":
            action = "drop" if current.pk == section.pk else f"blocked_{state}"
            return Verdict(state, window_message, action, "warning")
        # 已選的就是這個班別 → 動作是退選；選了同課另一班 → 置灰，要換班先退
        action = "drop" if current.pk == section.pk else "blocked_same_course"
        return Verdict("already", f"你已經加選這門課（{current.label_display}）了。", action, "info")
    # 等待列還在、但班別已不額滿（例如 admin 調高名額）：落回正常規則，
    # POST 直接加選並清掉等待列（main 的自動收斂語意）；額滿才停在「取消等待」
    if section.pk in standing.waiting and section.is_full:
        position = standing.waiting[section.pk]
        if state != "open":
            return Verdict(state, window_message, "cancel_wait", "warning", position=position)
        return Verdict("already_waitlisted",
                       f"{section.label_display}已額滿，你已在等待名單（第 {position} 位）。",
                       "cancel_wait", "info", position=position)
    if state != "open":
        return Verdict(state, window_message, f"blocked_{state}", "warning")
    # 官方開課的班別修課政策：已發布且可自行加選才開放。
    # 放在「已加選／已排隊／時間窗」之後，所以退選與取消等待不受政策影響。
    decision = policy_decision(section, standing.class_group, standing.policies)
    if not decision.allowed:
        return Verdict(decision.code, decision.message, decision.action, "warning")
    if missing := missing_prereqs(course, standing.passed):
        names = "、".join(f"{c.code} {c.name}" for c in missing)
        return Verdict("prereq", f"還缺先修課：{names}。", "blocked_prereq", "error")
    if (clash := conflicting_section(sections, section)):
        return Verdict("conflict",
                       f"衝堂：和「{clash.course.name} {clash.label_display}」時間重疊。",
                       "blocked_conflict", "error")
    # 班級官方課表也是時間基線：撞到「不同課號」的官方課程就算衝堂；
    # 官方課表本來就有同一門課時不算（見 enrollment_policy）。
    if (entry := official_conflicting_entry(section, standing.official_entries)):
        return Verdict("conflict",
                       f"衝堂：和班級官方課表「{entry.course_name}」時間重疊。",
                       "blocked_conflict", "error")
    if section.is_full:
        return Verdict("waitlisted", f"{section.label_display}已額滿。", "waitlist", "info")
    maximum = (standing.credit_summary.limits.maximum
               if standing.credit_summary and standing.credit_summary.limits
               else settings.MAX_CREDITS)
    if standing.credits + course.credits > maximum:
        return Verdict("over_credits", f"超過學分上限 {maximum}。", "blocked_credits", "error")
    return Verdict("enrolled", f"已加選「{course.name} {section.label_display}」。", "enroll", "success")
