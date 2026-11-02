"""加選資格的「班別修課政策」與「官方課表衝堂」判準。

這兩個判準都在回答同一個問題：**這個學生能不能加選這個班別**。所以放在
同一個模組，讓課程列表、課程詳情、`register()`、候補加入與候補遞補讀到
同一份答案（按鈕顯示什麼，POST 就回什麼）。

判準只看可靠的鍵，**不看課名**：

* **政策**：`Section`（開課班別）× `AcademicClassGroup`（學生班級）。
  同一門課的同一個班別可以對不同班級有不同政策，所以政策是「班別 × 班級」
  層級，不是課程層級。政策必須是 `PUBLISHED` 且 `can_enroll=True` 才算開放。
* **官方課表衝堂**：`NtubTimetableEntry`（學生班級在該學期的官方班級課表）
  的課號 + 星期 + 節次。官方課表本來就有這門課（同一課號）時**不算**衝堂；
  不同課程在同一時段重疊才算衝堂。官方活動（共同空堂、課外活動）只是時間
  框架，不阻擋加選。

沒有官方開課來源的班別（例如 114-1 的示範課程）不需要政策，沿用原本的
加選規則；這讓示範資料與既有測試的行為維持相容。
"""

from dataclasses import dataclass

from .models import SectionClassPolicy

#: 政策未開放時的 verdict code。
CODE_POLICY_MISSING = "policy_missing"   # 沒有政策／草稿／沒有學籍
CODE_POLICY_CLOSED = "policy_closed"     # 已發布但不可自行加選

#: 政策未開放時的班別列動作（按鈕一律是停用狀態）。
ACTION_BLOCKED_POLICY = "blocked_policy"
ACTION_BLOCKED_REQUIRED = "blocked_required"

#: 給學生看的封鎖理由（訊息本身就是理由，所以列表與詳情頁直接顯示）。
REASON_NO_PROFILE = "尚未設定學籍，無法確認這個開課是否對你開放。"
REASON_NO_POLICY = "這個開課還沒有對你的班級開放加選。"
REASON_DRAFT = "這個開課的加選政策尚未發布（仍是草稿）。"
REASON_REQUIRED = "本班必修：學校已有固定班級課表，不開放自行加選。"
REASON_NOT_ENROLLABLE = "這個開課已發布，但不開放自行加選。"


@dataclass(frozen=True)
class PolicyDecision:
    """某個學生對某個班別的政策判定結果。

    allowed：能不能自行加選。
    code：verdict 使用的代碼（enrolled / policy_missing / policy_closed）。
    message：封鎖理由（允許時是空字串）。
    action：班別列按鈕的動作。
    category：修課分類的中文名稱（有政策時才有）。
    policy：對應的 SectionClassPolicy（沒有時是 None）。
    """

    allowed: bool
    code: str
    message: str
    action: str
    category: str = ""
    policy: object = None

    @property
    def blocked(self):
        return not self.allowed


def is_official_section(section):
    """這個班別是不是「官方開課」（有 `NtubOfferingSource` 對應）。

    官方開課才需要班別修課政策；其他班別沿用原本的加選規則。
    已經 prefetch 過的班別不會再查資料庫。
    """
    cache = getattr(section, "_prefetched_objects_cache", None) or {}
    if "ntub_offering_sources" in cache:
        return bool(cache["ntub_offering_sources"])
    return section.ntub_offering_sources.exists()


def policy_decision(section, class_group=None, policies=None):
    """這個學生（班級）能不能加選這個班別。

    class_group 是學生的班級（沒有學籍就是 None）；policies 是
    `{section_id: SectionClassPolicy}`（由 standing 預先載入，含草稿）。

    判定順序：

    1. 有政策 → 已發布且 can_enroll 才開放；已發布但不可加選 → 擋下並說明
       修課分類；仍是草稿 → 擋下。
    2. 沒有政策但這個班別是官方開課 → 擋下（沒有政策不能加選）。
    3. 沒有政策也不是官方開課 → 允許（示範課程等，沿用舊規則）。
    """
    policies = policies or {}
    policy = policies.get(section.pk)
    if policy is not None:
        category = policy.get_course_category_display()
        if policy.status == SectionClassPolicy.Status.PUBLISHED:
            if policy.can_enroll:
                return PolicyDecision(True, "enrolled", "", "enroll", category, policy)
            required = policy.course_category == SectionClassPolicy.CourseCategory.REQUIRED
            return PolicyDecision(
                False, CODE_POLICY_CLOSED,
                REASON_REQUIRED if required else REASON_NOT_ENROLLABLE,
                ACTION_BLOCKED_REQUIRED if required else ACTION_BLOCKED_POLICY,
                category, policy)
        return PolicyDecision(False, CODE_POLICY_MISSING, REASON_DRAFT,
                              ACTION_BLOCKED_POLICY, category, policy)
    if not is_official_section(section):
        # 沒有官方開課來源的班別（例如 114-1 示範課程）：不需要政策。
        return PolicyDecision(True, "enrolled", "", "enroll")
    if class_group is None:
        return PolicyDecision(False, CODE_POLICY_MISSING, REASON_NO_PROFILE,
                              ACTION_BLOCKED_POLICY)
    return PolicyDecision(False, CODE_POLICY_MISSING,
                          f"{REASON_NO_POLICY}（你的班級：{class_group.code}）",
                          ACTION_BLOCKED_POLICY)


def _overlaps(section, entry):
    """班別與官方課表時段是否重疊（同一天、節次區間相交）。"""
    if entry.weekday != section.day:
        return False
    return (section.start_period <= entry.end_period
            and entry.start_period <= section.end_period)


def official_conflicting_entry(section, entries):
    """官方班級課表裡與這個班別衝突的課程時段；沒有回 None。

    比對用「課號 + 星期 + 節次」：官方課表本來就有同一課號的課程時不算
    衝堂（那是同一筆官方開課），不同課號重疊才算。官方活動不阻擋。
    """
    if not entries:
        return None
    code = (section.course.code or "").strip()
    for entry in entries:
        if entry.entry_type != "course":
            continue
        if not _overlaps(section, entry):
            continue
        entry_code = (entry.course_code or "").strip()
        if code and entry_code and entry_code == code:
            continue  # 同一門課：官方課表本來就有它，不是衝堂
        return entry
    return None
