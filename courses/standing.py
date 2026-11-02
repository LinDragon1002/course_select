"""使用者在一個學期的 standing：已選班別、等待順位、總學分、已修課號、
學生班級、修課政策與官方班級課表。

加選規則（services）與頁面顯示（views）共用的單一來源。
sections、waiting、credits 以傳入的 term 為範圍；passed 刻意跨學期
（「已修」的定義就是過去學期有同課號紀錄）。
waiting 的順位由 waitlist 模組計算（waitlist.positions）。

standing 也帶著加選判定需要的另外三樣東西：

* `class_group`：學生的班級（沒有學籍就是 None）。
* `policies`：這個班級在該學期的所有修課政策（含草稿），
  `{section_id: SectionClassPolicy}`——用來判斷官方開課能不能自行加選。
* `official_entries`：這個班級在該學期的官方課表「課程」時段，
  用來判斷加選的班別是否和班級官方課表衝堂。
"""
from dataclasses import dataclass, field

from .credits import summarize_credits
from .models import (Enrollment, NtubTimetableEntry, Section,
                     SectionClassPolicy, StudentAcademicProfile)
from .waitlist import positions


def total_credits(sections):
    """總學分：同一門課只算一次（防禦 admin 手動造成同課兩班的狀態）。"""
    return sum({s.course_id: s.course.credits for s in sections}.values())


def completed_codes(user):
    """過去學期有過加選紀錄的課號（＝「已修」）；本學期加選不算。"""
    return frozenset(Enrollment.objects.filter(
        user=user, section__course__term__is_current=False)
        .values_list("section__course__code", flat=True))


def enrolled_sections(user, term):
    """該使用者在 term 已加的班別；未登入或沒有學期回空 tuple。"""
    if not user.is_authenticated or term is None:
        return ()
    return tuple(Section.objects.filter(enrollments__user=user, course__term=term)
                 .select_related("course__teacher"))


def student_class_group(user):
    """學生的班級（`StudentAcademicProfile` 綁的那一個）；沒有學籍回 None。"""
    if not user or not user.is_authenticated:
        return None
    profile = (StudentAcademicProfile.objects.filter(user=user)
               .select_related("class_group").first())
    return profile.class_group if profile is not None else None


def class_policies(class_group, term):
    """這個班級在該學期的修課政策（含草稿）：{section_id: policy}。

    同一個班別對同一個班級只會有一條政策（DB constraint），所以不會重複。
    """
    if class_group is None or term is None:
        return {}
    return {policy.section_id: policy
            for policy in SectionClassPolicy.objects.filter(
                class_group=class_group, section__course__term=term)}


def official_entries(class_group, term):
    """這個班級在該學期的官方課表「課程」時段（活動不阻擋，所以不取）。

    這些時段是班級的時間基線：加選的班別撞到其中一門「不同課號」的課，
    就是衝堂。
    """
    if class_group is None or term is None:
        return ()
    return tuple(NtubTimetableEntry.objects.filter(
        snapshot__class_group=class_group, snapshot__term=term,
        entry_type="course").order_by("weekday", "start_period", "id"))


@dataclass(frozen=True)
class Standing:
    sections: tuple  # 本學期已加的班別（Section）
    waiting: dict    # 本學期還在排隊的班別：{section_id: 順位}
    credits: int     # 本學期總學分（按課去重）
    passed: frozenset  # 過去學期已修的課號（刻意跨學期）
    class_group: object = None  # 學生的班級（沒有學籍就是 None）
    policies: dict = field(default_factory=dict)  # {section_id: SectionClassPolicy}
    official_entries: tuple = ()  # 官方班級課表的課程時段
    credit_summary: object = None

    @property
    def section_ids(self):
        return frozenset(s.pk for s in self.sections)


def get_standing(user, term, *, include_waiting=True):
    """使用者在 term 的 standing；未登入或沒有學期回空 standing。

    `include_waiting=False` 給候補遞補用：遞補不需要順位資訊，省下查詢。
    """
    if not user.is_authenticated or term is None:
        summary = summarize_credits(None, None)
        return Standing(sections=(), waiting={}, credits=summary.total,
                        passed=frozenset(), credit_summary=summary)
    sections = enrolled_sections(user, term)
    class_group = student_class_group(user)
    entries = official_entries(class_group, term)
    credit_summary = summarize_credits(class_group, term, sections, entries)
    return Standing(sections=sections,
                    waiting=positions(user, term) if include_waiting else {},
                    credits=credit_summary.total,
                    passed=completed_codes(user),
                    class_group=class_group,
                    policies=class_policies(class_group, term),
                    official_entries=entries,
                    credit_summary=credit_summary)
