"""等待名單生命週期：排隊 → 順位 → 遞補 → 已讀 → 取消。

Waitlist 表的單一入口：register／drop 的 commit 路徑各呼叫
join／promote_next 一行的程度，頁面讀 positions／unread_promotions，
遞補的 skip 規則只有這一份。
"""
from django.utils import timezone

from .enrollment_policy import official_conflicting_entry, policy_decision
from .models import Enrollment, Waitlist
from .verdict import conflicting_section, same_course_section


def join(user, section):
    """把使用者加入該班等待名單（在 register 的 transaction 內呼叫）；回傳建好的列。

    「遞補過後來又退選」的歷史列不佔順位、也擋不到新排隊，先清掉再建。
    """
    Waitlist.objects.filter(user=user, section=section,
                            promoted_at__isnull=False).delete()
    return Waitlist.objects.create(user=user, section=section)


def release_course(user, course, keep_pk=None):
    """使用者已加選這門課（加選或遞補），該課其他班的等待紀錄清掉。"""
    qs = Waitlist.objects.filter(user=user, section__course=course)
    if keep_pk is not None:
        qs = qs.exclude(pk=keep_pk)
    qs.delete()


def _position(queued_at, section_times):
    """「第 N 位」的單一定義：1＋同班級中排隊時間更早（還沒遞補）的列數。

    section_times 是該班級所有還沒遞補列的 queued_at。
    排隊時間相同的列會拿到同一順位（只數「更早」，不數「相同」）。
    """
    return 1 + sum(1 for t in section_times if t < queued_at)


def _queued_times_by_section(section_ids):
    """{section_id: 該班級所有還沒遞補列的 queued_at}，固定 1 筆查詢。

    順位規則（_position）的單一查詢點：單列版與批量版共用同一個 filter 形狀。
    """
    times = {}
    for section_id, queued in Waitlist.objects.filter(promoted_at__isnull=True,
                                                      section_id__in=set(section_ids)) \
            .values_list("section_id", "queued_at"):
        times.setdefault(section_id, []).append(queued)
    return times


def position_of(wait):
    """單一等待列的目前順位（1 筆查詢）。"""
    times = _queued_times_by_section([wait.section_id])
    return _position(wait.queued_at, times[wait.section_id])


def positions(user, term):
    """該使用者在 term 還在排隊的班別：{section_id: 順位}（2 筆查詢，非每列一筆 COUNT）。"""
    if not user.is_authenticated or term is None:
        return {}
    rows = list(Waitlist.objects.filter(user=user, promoted_at__isnull=True,
                                        section__course__term=term)
                .values_list("section_id", "queued_at"))
    if not rows:
        return {}
    section_times = _queued_times_by_section({section_id for section_id, _ in rows})
    # rows 的每個 section_id 都進了上面的查詢，直接取；KeyError＝邏輯有錯，要顯性化
    return {section_id: _position(queued, section_times[section_id])
            for section_id, queued in rows}


def _next_waiter(section, exclude=()):
    # 只鎖 waitlist 列本身（of=("self",)），不連帶鎖 User/Course，
    # 避免與 register() 的鎖順序不同造成 deadlock
    # section__course__term 與 ntub_offering_sources 一起帶進來：
    # 遞補前要重新驗證班別政策與官方課表衝堂
    return (Waitlist.objects.filter(section=section, promoted_at__isnull=True)
            .exclude(pk__in=exclude)
            .select_related("section__course__term")
            .prefetch_related("section__ntub_offering_sources")
            .select_for_update(of=("self",)).first())


def promote_next(section):
    """該班等待名單第一位轉為正式加選（FIFO，不越序）。

    遞補前會重新驗證與加選相同的判準：已選同課另一班、與已選班別衝堂、
    與班級官方課表衝堂、班別修課政策已被撤回（撤回就不能自動變成加選）。
    不符合者移出名單、順延下一位。
    遞補不重查學分上限（見 docs 已知限制）。
    """
    # 函式內 import：standing 的 get_standing 讀本模組的 positions，模組層不能互相 import
    from .standing import get_standing
    now = timezone.now()
    seen = []
    while True:
        wait = _next_waiter(section, exclude=seen)
        if wait is None:
            return
        seen.append(wait.pk)
        course = wait.section.course
        # 只取需要的 standing（同課、衝堂、政策、官方課表），不在持鎖的迴圈裡掃描等待順位與已修課號
        standing = get_standing(wait.user, course.term, include_waiting=False)
        sections = standing.sections
        if same_course_section(sections, course) is not None:
            # 排隊後又選了同課另一班：不再符合等待條件，移出名單後看下一位
            wait.delete()
            continue
        # 政策被撤回（或從未發布）就不能自動遞補成加選
        decision = policy_decision(wait.section, standing.class_group, standing.policies)
        if not decision.allowed:
            wait.delete()
            continue
        if conflicting_section(sections, wait.section) is not None:
            # 與已選班別衝堂：同樣移出、順延（維持「不衝堂」不變式）
            wait.delete()
            continue
        if official_conflicting_entry(wait.section, standing.official_entries) is not None:
            # 與班級官方課表衝堂：移出、順延
            wait.delete()
            continue
        if section.is_full:  # 建立前重查容量（正常退選後必有空位，純防禦）
            return
        Enrollment.objects.create(user=wait.user, section=section)
        wait.promoted_at = now
        wait.save(update_fields=["promoted_at"])
        # 人已遞補進這門課，該課其他班的等待紀錄同步清掉
        release_course(wait.user, course, keep_pk=wait.pk)
        return


def cancel(user, section):
    """取消等待；回 True 表示真的有紀錄被取消。"""
    deleted, _ = Waitlist.objects.filter(user=user, section=section,
                                         promoted_at__isnull=True).delete()
    return deleted > 0


def unread_promotions(user):
    """已遞補、還沒標已讀的列：banner 顯示與 ack 讀取共用的單一定義。"""
    return Waitlist.objects.filter(user=user, promoted_at__isnull=False,
                                   promoted_seen_at__isnull=True)


def mark_promotions_read(user):
    """把使用者所有未讀遞補標成已讀（ack_promotion 的 POST）。"""
    return unread_promotions(user).update(promoted_seen_at=timezone.now())
