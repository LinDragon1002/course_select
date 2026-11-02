"""加退選的 commit 路徑。資格規則（為什麼能／不能加選）全在 verdict.evaluate，
等待名單生命週期（排隊／順位／遞補／取消）全在 waitlist。"""
from dataclasses import replace

from django.db import transaction

from . import waitlist
from .models import Enrollment, Section
from .standing import get_standing
from .verdict import evaluate
from .window import window_gate


def register(user, section):
    """加選一個班別：在 transaction 內用新查出的 DB 狀態 evaluate，再 commit。

    額滿不再直接拒絕：改為加入該班等待名單（等有人退選自動遞補）。
    成功加選時，該課其他班的等待紀錄自動清掉。
    傳回的 Verdict 與班別列按鈕是同一個判定（verdict.evaluate）。

    standing 含等待順位掃描（多 1–2 筆查詢），舊版刻意省掉；
    現在按鈕與 POST 共用 evaluate，「已排隊第 N 位」的按鈕狀態
    需要它，接受這個成本換兩端語意一致。
    """
    with transaction.atomic():
        # shortcut: sqlite 不支援列鎖，名額在高併發下可能超收；換 PostgreSQL 時 select_for_update 才會生效
        # course__term 一起帶進來，時間窗與 standing 判斷永遠用新查出的 DB 狀態
        # ntub_offering_sources 一起帶進來：官方開課的班別政策判定要用的
        section = (Section.objects.select_for_update()
                   .select_related("course__term")
                   .prefetch_related("ntub_offering_sources")
                   .get(pk=section.pk))
        term = section.course.term
        standing = get_standing(user, term)
        verdict = evaluate(standing, section, window_gate(term))
        if verdict.code == "enrolled":
            Enrollment.objects.create(user=user, section=section)
            # 選進了這門課，該課其他班的等待紀錄自動清掉
            waitlist.release_course(user, section.course)
            return verdict
        if verdict.code == "waitlisted":
            wait = waitlist.join(user, section)
            return replace(verdict,
                           message=f"{section.label_display}已額滿，已加入等待名單"
                                   f"（第 {waitlist.position_of(wait)} 位），有人退選將自動遞補。")
        return verdict


def drop_enrollment(user, section):
    """退選一個班別；釋出的名額立即遞補等待名單第一位（同 transaction）。

    先鎖 Section（與 register() 同一把鎖、同樣先 Section 的順序），
    避免退選＋遞補與並發加選互相穿插造成超收（換 PostgreSQL 後生效）。
    """
    with transaction.atomic():
        Section.objects.select_for_update().get(pk=section.pk)
        deleted, _ = Enrollment.objects.filter(user=user, section=section).delete()
        if not deleted:
            return False
        waitlist.promote_next(section)
        return True
