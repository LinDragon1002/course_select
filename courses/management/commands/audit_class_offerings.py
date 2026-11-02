"""稽核某個學期、某個學生班級的「可選課程」到底有哪些。

用法：
    python manage.py audit_class_offerings --term 115-1 --class-code 40630B
    python manage.py audit_class_offerings --term 115-1 --class-code 40630B \
        --expected-codes 40637100,40635420,40627960

這支指令**唯讀**：只查資料庫，不建立、不修改、不刪除任何政策、課程或加選紀錄，
也不會切換目前學期。要修正資料請另外用維護員介面或匯入指令。

答案只來自 `SectionClassPolicy`（開課班別 × 學生班級的修課政策）：

* 已發布（PUBLISHED）且 `can_enroll=True` → 這個班級**可以**自行加選。
* 仍是草稿（DRAFT）→ 還沒正式開放，不列入可選課程。
* 已發布但 `can_enroll=False` → 正式開放了這個班別，但這個班級不能自行加選。

課綱推薦（`NtubCurriculumRecommendation`）與全學期的 `Course` 清單**不是**
學生可選課程的依據：推薦只是課綱層級的疑義，而課程會出現在修課政策裡代表
它對「另一個班級」是開放的。所以這支指令一律以政策為準。

`--expected-codes` 給的是「應該可選的課號」清單；完全一致就 PASS，
缺少或多出都會列出差異，並以非零退出碼結束。
"""

from django.core.management.base import BaseCommand, CommandError

from ...models import AcademicClassGroup, SectionClassPolicy, Term

#: 官方班級代碼 → 星期中文（Section.DAYS 只有週一到週五，官方資料可能到週日）。
WEEKDAY_LABELS = {1: "週一", 2: "週二", 3: "週三", 4: "週四",
                  5: "週五", 6: "週六", 7: "週日"}


def _time_display(section, source=None):
    """上課時間：優先用官方開課來源的實際套用時段，沒有就用班別自己的時段。"""
    day = start = end = None
    if source is not None:
        day = source.applied_day
        start = source.applied_start_period
        end = source.applied_end_period
    if not day:
        day = section.day
    if not start:
        start = section.start_period
    if not end:
        end = section.end_period
    label = WEEKDAY_LABELS.get(day) or section.get_day_display()
    if not start or not end:
        return label
    return f"{label} 第{start}-{end}節"


def _collect_course_entry(entry, section):
    """把一個班別的來源資訊併進該課號的項目（同一門課可能有多個班別）。"""
    course = section.course
    sources = list(section.ntub_offering_sources.all())
    if not sources:
        teacher = (course.teacher.name or "").strip()
        if teacher and teacher not in entry["teachers"]:
            entry["teachers"].append(teacher)
        time_text = _time_display(section)
        if time_text not in entry["times"]:
            entry["times"].append(time_text)
        return
    for source in sources:
        source_class = source.class_group
        label = f"{source_class.code} {source_class.name_zh}".strip()
        if label and label not in entry["source_classes"]:
            entry["source_classes"].append(label)
        teacher = (source.teacher_name or source.teacher.name or "").strip()
        if teacher and teacher not in entry["teachers"]:
            entry["teachers"].append(teacher)
        time_text = _time_display(section, source)
        if time_text not in entry["times"]:
            entry["times"].append(time_text)


def audit_class_offerings(term_code, class_code):
    """查出某學期、某班級的政策稽核結果（純查詢，不寫入）。

    回傳 dict：

    * term / class_group：學期與班級物件。
    * enrollable：可選課程清單，每筆是
      `{code, name, source_classes, teachers, times, sections}`。
    * draft_count：草稿政策筆數。
    * closed_count：已發布但 `can_enroll=False` 的筆數。
    * has_policy：這個班級在這個學期有沒有任何政策。
    """
    term = Term.objects.filter(code=term_code).first()
    if term is None:
        raise CommandError(f"查無學期「{term_code}」。")

    class_group = AcademicClassGroup.objects.filter(code=class_code).first()
    if class_group is None:
        raise CommandError(f"查無班級代碼「{class_code}」。")

    policies = (SectionClassPolicy.objects
                .filter(class_group=class_group, section__course__term=term)
                .select_related("section__course__teacher", "section__course")
                .prefetch_related("section__ntub_offering_sources"))

    draft_count = policies.filter(status=SectionClassPolicy.Status.DRAFT).count()
    published = policies.filter(status=SectionClassPolicy.Status.PUBLISHED)
    closed_count = published.filter(can_enroll=False).count()
    open_policies = published.filter(can_enroll=True)

    # 同一門課可能有多個班別各自對這個班級開放；可選課程以「課號」為單位，
    # 不因為同一門課查到多筆政策就重複計算。
    courses = {}
    for policy in open_policies:
        section = policy.section
        course = section.course
        entry = courses.setdefault(course.code, {
            "code": course.code,
            "name": course.name,
            "source_classes": [],
            "teachers": [],
            "times": [],
            "sections": [],
        })
        entry["sections"].append(section)
        _collect_course_entry(entry, section)

    enrollable = sorted(courses.values(), key=lambda item: item["code"])
    return {
        "term": term,
        "class_group": class_group,
        "enrollable": enrollable,
        "draft_count": draft_count,
        "closed_count": closed_count,
        "has_policy": policies.exists(),
    }


class Command(BaseCommand):
    help = "稽核某學期、某學生班級的正式修課政策與可選課程（唯讀）"

    def add_arguments(self, parser):
        parser.add_argument("--term", required=True, help="學期代碼，例如 115-1")
        parser.add_argument("--class-code", required=True,
                            help="學生班級代碼，例如 40630B")
        parser.add_argument("--expected-codes", default="",
                            help="逗號分隔的預期可選課號；完全一致才 PASS")

    def handle(self, *args, **opts):
        term_code = opts["term"]
        class_code = opts["class_code"]
        raw_expected = opts.get("expected_codes") or ""
        expected = [code.strip() for code in raw_expected.split(",") if code.strip()]

        result = audit_class_offerings(term_code, class_code)
        term = result["term"]
        class_group = result["class_group"]
        enrollable = result["enrollable"]

        self.stdout.write(f"學期：{term.code}")
        self.stdout.write(f"目標班級：{class_group.code} {class_group.name_zh}".rstrip())
        self.stdout.write(f"已發布且可加選的課程數：{len(enrollable)}")

        if not result["has_policy"]:
            self.stdout.write(self.style.WARNING(
                "（此班級在此學期沒有任何 SectionClassPolicy 紀錄：沒有正式政策，"
                "因此沒有可自行加選的課程。）"))
        elif not enrollable:
            self.stdout.write(self.style.WARNING(
                "（此班級在此學期有政策，但沒有任何「已發布且可自行加選」的課程。）"))

        for item in enrollable:
            sources = "、".join(item["source_classes"]) or "（無官方開課來源）"
            teachers = "、".join(item["teachers"]) or "（未指定教師）"
            times = "、".join(item["times"]) or "（未指定時間）"
            self.stdout.write(
                f"  · {item['code']} {item['name']}"
                f"｜開課來源班級：{sources}｜教師：{teachers}｜上課時間：{times}")

        self.stdout.write(f"草稿政策數（不列入可選課程）：{result['draft_count']}")
        self.stdout.write(f"已發布但 can_enroll=False 的數量：{result['closed_count']}")

        if not expected:
            self.stdout.write(self.style.SUCCESS(
                "稽核完成：未指定 --expected-codes，不做課號比對（本指令未修改任何資料）。"))
            return

        actual_codes = {item["code"] for item in enrollable}
        expected_codes = set(expected)
        missing = sorted(expected_codes - actual_codes)
        extra = sorted(actual_codes - expected_codes)

        if not missing and not extra:
            self.stdout.write(self.style.SUCCESS(
                f"PASS：可選課號與預期完全一致（{len(actual_codes)} 門）"
                "（本指令未修改任何資料）。"))
            return

        self.stdout.write(self.style.ERROR("FAIL：可選課號與預期不一致。"))
        if missing:
            self.stdout.write(
                "  缺少（預期有、實際沒有）：" + "、".join(missing))
        if extra:
            self.stdout.write(
                "  多出（實際有、預期沒有）：" + "、".join(extra))
        raise CommandError(
            f"可選課號比對失敗：缺少 {len(missing)} 門、多出 {len(extra)} 門。"
            "（本指令未修改任何資料，也不會自動修復。）")
