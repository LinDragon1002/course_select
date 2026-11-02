"""清除 `seed` 指令建立的示範（Demo）資料。

**這個指令做什麼**

`seed` 指令（`courses/management/commands/seed.py`）會建立一批純屬示範用的假資料：
CS101／CS201／CS301 等假課、示範用學期、以及 `demo` / `demo_fill_*` 測試帳號。
正式的本機開發資料庫不該留著這些東西——學生搜尋課程時會看到不存在的課，
「目前學期」也會被 Demo 的 114-1 佔住。

**辨識依據（多重條件）**

只靠「課號在 CS 開頭」或「學期是 114-1」都不足以判定是 Demo 資料：真實的
NTUB 資料也可能落在 114-1。因此這個指令的每一筆刪除都必須同時滿足：

1. **來源**：該筆資料是 `seed.py` 真的建立出來的（課號、課名、老師、學分、
   學期全部對得上 `seed.DATA`；使用者名稱符合 `demo` / `demo_fill_*`）。
2. **關聯**：刪掉之後不會留下任何其他模型的外鍵指向它（官方開課來源、
   課綱推薦、修課政策、課表快照、選課、候補都檢查過）。

**安全保險**

* 預設就是 dry-run：`--apply` 必須明確指定才會寫入資料庫。
* 絕不碰真實資料：Google 登入帳號、allauth 的 SocialAccount / EmailAddress、
  superuser、真實 StudentAcademicProfile、學籍目錄（AcademicCatalog /
  AcademicProgramType / AcademicDepartment / AcademicClassGroup）、115-1 的
  課程與班別、NTUB 官方來源與課表快照、修課政策與維護員權限。
* 對不上來源、或被其他資料引用的，一律保留並列為「待確認」，不猜測後刪除。
* 可以重複執行：第二次跑不會出錯（該刪的當初就刪了，剩下的是不該刪的）。
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand
from django.db import transaction

from courses.models import (AcademicClassGroup, AcademicDepartment, Course,
                            CourseMaintainerScope, Enrollment,
                            NtubCurriculumRecommendation, NtubOfferingAmbiguity,
                            NtubOfferingSource, NtubRecommendationCandidate,
                            NtubTimetableEntry, NtubTimetableSnapshot, Section,
                            SectionClassPolicy, StudentAcademicProfile, Teacher,
                            Term, Waitlist)

from .seed import DATA

# `seed.py` 建立的示範學期：114-1（當時的「目前學期」）、114-2（未開放的未來學期）、
# 113-2（先修示範用）。
DEMO_TERM_CODES = ("114-1", "114-2", "113-2")

# `seed.py` 建立的使用者名稱。
DEMO_USER_NAMES = ("demo",)
DEMO_USER_PREFIXES = ("demo_fill_",)

# `seed.py` 幫 demo 帳號填的信箱；這是示範用的假信箱，不是真的聯絡信箱。
# 真正有在使用者的信箱（allauth 的 EmailAddress、真實 Google 帳號）不在這裡，
# 會被下面的檢查擋下來。
DEMO_USER_EMAILS = {"demo@example.com"}

# `seed.py` 的 DATA：(課號, 課名, 老師, 學分, 大綱, sections)
DEMO_COURSE_SPECS = {
    code: {"name": name, "teacher": teacher, "credits": credits}
    for code, name, teacher, credits, _syl, _sections in DATA
}


def _demo_user(username: str) -> bool:
    return username in DEMO_USER_NAMES or username.startswith(DEMO_USER_PREFIXES)


class CleanupReport:
    """一次清理的結果：會刪的、會保留的、待確認的。"""

    def __init__(self):
        self.plans: list[dict] = []      # 預計刪除
        self.keep: list[dict] = []       # 判定為非 Demo，保留
        self.needs_review: list[dict] = []  # 來源無法確認或被引用，待人工確認

    def plan(self, model: str, obj, reason: str, pk=None, **extra):
        entry = {"model": model, "obj": obj, "reason": reason, "pk": pk}
        entry.update(extra)
        self.plans.append(entry)

    def keep_it(self, model: str, obj, reason: str):
        self.keep.append({"model": model, "obj": obj, "reason": reason})

    def pending(self, model: str, obj, reason: str):
        self.needs_review.append({"model": model, "obj": obj, "reason": reason})

    @property
    def counts(self) -> dict:
        out: dict = {}
        for entry in self.plans:
            out[entry["model"]] = out.get(entry["model"], 0) + 1
        return out


class Command(BaseCommand):
    help = ("清除 seed 指令建立的示範課程／學期／帳號等假資料。"
            "預設為 dry-run（不寫入資料庫），要真的刪除請加 --apply。")

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply", action="store_true",
            help="真的執行刪除（未指定時只列出預計刪除的資料，完全不寫入資料庫）。")
        parser.add_argument(
            "--dry-run", action="store_true",
            help="只列出預計刪除的資料（預設行為，保留給明確呼叫的用途）。")

    def handle(self, *args, **opts):
        apply = bool(opts.get("apply"))
        report = self.collect()

        self.stdout.write("=" * 72)
        self.stdout.write("Demo 資料清理" + ("（--apply：將寫入資料庫）" if apply else "（dry-run：不寫入）"))
        self.stdout.write("=" * 72)

        self.stdout.write("\n【預計刪除】")
        if not report.plans:
            self.stdout.write("  （沒有任何資料需要刪除）")
        for model in sorted({e["model"] for e in report.plans}):
            entries = [e for e in report.plans if e["model"] == model]
            self.stdout.write(f"  {model}（{len(entries)} 筆）")
            for entry in entries:
                self.stdout.write(f"    - {entry['obj']}  ← {entry['reason']}")

        self.stdout.write("\n【保留：判定為真實資料】")
        if not report.keep:
            self.stdout.write("  （無）")
        for entry in report.keep:
            self.stdout.write(f"  {entry['model']}: {entry['obj']}  ← {entry['reason']}")

        self.stdout.write("\n【保留：不可刪的真實資料（摘要）】")
        for line in self.kept_summary():
            self.stdout.write(f"  {line}")

        self.stdout.write("\n【待確認：來源無法確定或被其他資料引用，已保留】")
        if not report.needs_review:
            self.stdout.write("  （無）")
        for entry in report.needs_review:
            self.stdout.write(f"  {entry['model']}: {entry['obj']}  ← {entry['reason']}")

        total = len(report.plans)
        self.stdout.write(f"\n合計預計刪除 {total} 筆：{report.counts}")

        if not apply:
            self.stdout.write("\nDry-run 結束：資料庫完全沒有變動。要真的刪除請加 --apply。")
            return

        with transaction.atomic():
            self.apply_plan(report)
        self.stdout.write("\n已套用刪除（單一 transaction）。")

    # -- 盤點 ------------------------------------------------------------------

    @staticmethod
    def kept_summary() -> list[str]:
        """列出「絕對不會被這個指令動到」的真實資料，讓 dry-run 報告可被稽核。"""
        from django.contrib.auth.models import Group

        current = Term.current()
        lines = []
        if current is not None:
            lines.append(
                f"目前學期：{current.code}（Course {current.courses.count()} 筆 / "
                f"Section {Section.objects.filter(course__term=current).count()} 筆）")
        for code in ("115-1",):
            term = Term.objects.filter(code=code).first()
            if term is not None:
                lines.append(
                    f"{code}：Course {term.courses.count()} 筆 / "
                    f"Section {Section.objects.filter(course__term=term).count()} 筆")
        lines.append(f"學期總數：{Term.objects.count()}（{[t.code for t in Term.objects.all()]}）")
        lines.append(f"班級（AcademicClassGroup）：{AcademicClassGroup.objects.count()}")
        lines.append(f"系所（AcademicDepartment）：{AcademicDepartment.objects.count()}")
        lines.append(f"官方課表快照：{NtubTimetableSnapshot.objects.count()}"
                     f"／明細：{NtubTimetableEntry.objects.count()}")
        lines.append(f"官方開課來源：{NtubOfferingSource.objects.count()}"
                     f"／課綱推薦：{NtubCurriculumRecommendation.objects.count()}"
                     f"／候選：{NtubRecommendationCandidate.objects.count()}"
                     f"／疑義：{NtubOfferingAmbiguity.objects.count()}")
        lines.append(f"修課政策：{SectionClassPolicy.objects.count()}"
                     f"／維護員權限範圍：{CourseMaintainerScope.objects.count()}")
        lines.append(f"學生學籍：{StudentAcademicProfile.objects.count()}"
                     f"（{[p.class_group.code for p in StudentAcademicProfile.objects.select_related('class_group')]}）")
        superusers = list(User.objects.filter(is_superuser=True).values_list("username", flat=True))
        lines.append(f"Superuser：{superusers}")
        admins = list(User.objects.filter(is_staff=True).values_list("username", flat=True))
        lines.append(f"Staff 帳號：{admins}")
        groups = list(Group.objects.values_list("name", flat=True))
        lines.append(f"群組：{groups}")
        return lines

    def collect(self) -> CleanupReport:
        report = CleanupReport()
        # 先找出「被其他資料引用、不能刪」的 Demo 班別；因為刪 Course 會 cascade
        # 掉底下的 Section，持有受保護班別的 Course 也不能刪，所以要分兩階段判斷。
        self.protected_section_ids = self.find_protected_sections()
        self.collect_sections(report)
        self.collect_courses(report)
        self.collect_users(report)
        self.collect_teachers(report)
        self.collect_terms(report)
        return report

    def find_protected_sections(self) -> set:
        """Demo 班別中，被其他模型引用而不能刪的那些（回傳 pk 集合）。"""
        protected = set()
        for section in Section.objects.select_related("course__term", "course__teacher"):
            if not self.is_demo_course(section.course):
                continue
            if SectionClassPolicy.objects.filter(section=section).exists():
                protected.add(section.pk)
            elif NtubOfferingSource.objects.filter(section=section).exists():
                protected.add(section.pk)
        return protected

    # -- 課程 -----------------------------------------------------------------

    def is_demo_course(self, course) -> bool:
        """多重條件判定：學期 + 課號 + 課名 + 老師 + 學分 全部對得上 seed.DATA。

        只符合其中一兩項不算 Demo——真實的 NTUB 課程也可能落在示範學期裡。
        """
        if course.term.code not in DEMO_TERM_CODES:
            return False
        spec = DEMO_COURSE_SPECS.get(course.code)
        if spec is None:
            return False
        if course.name != spec["name"] or course.teacher.name != spec["teacher"]:
            return False
        return course.credits == spec["credits"]

    def collect_courses(self, report):
        for course in Course.objects.select_related("term", "teacher").order_by("term__code", "code"):
            if not self.is_demo_course(course):
                continue
            if NtubOfferingSource.objects.filter(course=course).exists():
                report.pending("Course", course, "有 NtubOfferingSource 引用")
                continue
            if NtubCurriculumRecommendation.objects.filter(course=course).exists():
                report.pending("Course", course, "有 NtubCurriculumRecommendation 引用")
                continue
            # 底下有受保護的班別就不能刪這門課：刪 Course 會 cascade 掉 Section。
            protected = Section.objects.filter(course=course, pk__in=self.protected_section_ids)
            if protected.exists():
                report.pending("Course", course,
                               f"底下的班別被引用（{[str(s) for s in protected]}），不刪")
                continue
            report.plan("Course", f"{course.term.code} {course.code} {course.name}",
                        "seed.py 建立的示範課程", pk=course.pk)

    # -- 班別 / 選課 / 候補 -----------------------------------------------------

    def collect_sections(self, report):
        for section in Section.objects.select_related("course__term", "course__teacher").order_by(
                "course__term__code", "course__code", "label"):
            course = section.course
            if not self.is_demo_course(course):
                continue
            if section.pk in self.protected_section_ids:
                report.pending("Section", section,
                               "被其他資料引用（SectionClassPolicy 或 NtubOfferingSource）")
                continue
            report.plan("Section", f"{course.term.code} {course.code} {section.label}",
                        "seed.py 建立的示範班別", pk=section.pk)

            for enrollment in Enrollment.objects.filter(section=section).select_related("user"):
                report.plan("Enrollment", f"{enrollment.user.username} → {section}",
                            "指向 Demo 班別的選課紀錄", pk=enrollment.pk)
            for waitlist in Waitlist.objects.filter(section=section).select_related("user"):
                report.plan("Waitlist", f"{waitlist.user.username} → {section}",
                            "指向 Demo 班別的候補紀錄", pk=waitlist.pk)

    # -- 使用者 ---------------------------------------------------------------

    def collect_users(self, report):
        for user in User.objects.all().order_by("pk"):
            if not _demo_user(user.username):
                continue
            # Demo 帳號必須「乾淨」：不能是 superuser／staff、不能綁 Google 登入、
            # 不能有 allauth 的信箱紀錄、不能有真實學籍，也不能在任何非 Demo 班別上。
            if user.is_superuser:
                report.pending("User", user.username, "是 superuser，不刪")
                continue
            if user.is_staff:
                report.pending("User", user.username, "是 staff 帳號，不刪")
                continue
            if self.has_oauth(user):
                report.pending("User", user.username, "有 Google／allauth 登入紀錄，不刪")
                continue
            if StudentAcademicProfile.objects.filter(user=user).exists():
                report.pending("User", user.username, "有 StudentAcademicProfile，不刪")
                continue
            if user.email and user.email not in DEMO_USER_EMAILS:
                report.pending("User", user.username, f"有非示範信箱（{user.email}），不刪")
                continue
            stray = Enrollment.objects.filter(user=user).exclude(
                section__course__term__code__in=DEMO_TERM_CODES).exists()
            if stray:
                report.pending("User", user.username, "有非 Demo 學期的選課紀錄，不刪")
                continue
            stray_wl = Waitlist.objects.filter(user=user).exclude(
                section__course__term__code__in=DEMO_TERM_CODES).exists()
            if stray_wl:
                report.pending("User", user.username, "有非 Demo 學期的候補紀錄，不刪")
                continue
            report.plan("User", user.username,
                        "seed.py 建立的示範帳號（無 OAuth／無學籍／無 email）", pk=user.pk)

    @staticmethod
    def has_oauth(user) -> bool:
        """有沒有 allauth 的社交登入或信箱紀錄。"""
        try:
            from allauth.account.models import EmailAddress
            from allauth.socialaccount.models import SocialAccount
        except ImportError:  # pragma: no cover - allauth 一定存在
            return False
        return (SocialAccount.objects.filter(user=user).exists()
                or EmailAddress.objects.filter(user=user).exists())

    # -- 老師 -----------------------------------------------------------------

    def collect_teachers(self, report):
        demo_teacher_names = {spec["teacher"] for spec in DEMO_COURSE_SPECS.values()}
        # 這位老師的課「全部」都會被刪掉，才能刪老師本人（Course.teacher 是 PROTECT）。
        deletable_course_ids = {e["pk"] for e in report.plans if e["model"] == "Course"}
        for teacher in Teacher.objects.order_by("pk"):
            if teacher.name not in demo_teacher_names:
                continue
            if teacher.user_id:
                report.pending("Teacher", teacher.name, "綁定了登入帳號，不刪")
                continue
            if NtubOfferingSource.objects.filter(teacher=teacher).exists():
                report.pending("Teacher", teacher.name, "被 NtubOfferingSource 引用，不刪")
                continue
            course_ids = set(teacher.course_set.values_list("id", flat=True))
            if not course_ids:
                continue
            if course_ids - deletable_course_ids:
                report.pending("Teacher", teacher.name, "仍被真實課程使用，不刪")
                continue
            report.plan("Teacher", teacher.name,
                        "seed.py 建立、且沒有任何真實課程使用", pk=teacher.pk)

    # -- 學期 -----------------------------------------------------------------

    def collect_terms(self, report):
        # 這個學期的課「全部」都會被刪掉，才能刪學期（Course.term 是 PROTECT）。
        deletable_course_ids = {e["pk"] for e in report.plans if e["model"] == "Course"}
        for term in Term.objects.order_by("code"):
            if term.code not in DEMO_TERM_CODES:
                continue
            course_ids = set(term.courses.values_list("id", flat=True))
            if course_ids - deletable_course_ids:
                report.pending("Term", term.code,
                               "含有不會被刪的課程，不刪")
                continue
            if NtubTimetableSnapshot.objects.filter(term=term).exists():
                report.pending("Term", term.code, "有 NtubTimetableSnapshot 引用")
                continue
            if NtubOfferingSource.objects.filter(term=term).exists():
                report.pending("Term", term.code, "有 NtubOfferingSource 引用")
                continue
            if SectionClassPolicy.objects.filter(section__course__term=term).exists():
                report.pending("Term", term.code, "有 SectionClassPolicy 引用")
                continue
            report.plan("Term", term.code,
                        "seed.py 建立的示範學期，且已無任何真實資料引用", pk=term.pk)

    # -- 套用刪除 -------------------------------------------------------------

    def apply_plan(self, report):
        by_model: dict = {}
        for entry in report.plans:
            by_model.setdefault(entry["model"], []).append(entry)

        # 順序很重要：先選課／候補，再班別，再課程，再老師、帳號，最後學期。
        for model, model_class in (("Enrollment", Enrollment), ("Waitlist", Waitlist)):
            for entry in by_model.get(model, []):
                obj = model_class.objects.filter(pk=entry["pk"]).first()
                if obj is None:
                    continue
                label = entry["obj"]
                obj.delete()
                self.stdout.write(f"  刪除 {model}: {label}")

        for entry in by_model.get("Section", []):
            obj = Section.objects.filter(pk=entry["pk"]).first()
            if obj is None:
                continue
            label = entry["obj"]
            obj.delete()
            self.stdout.write(f"  刪除 Section: {label}")

        for entry in by_model.get("Course", []):
            obj = Course.objects.filter(pk=entry["pk"]).first()
            if obj is None:
                continue
            label = entry["obj"]
            obj.delete()
            self.stdout.write(f"  刪除 Course: {label}")

        for entry in by_model.get("Teacher", []):
            obj = Teacher.objects.filter(pk=entry["pk"]).first()
            if obj is None:
                continue
            label = entry["obj"]
            obj.delete()
            self.stdout.write(f"  刪除 Teacher: {label}")

        for entry in by_model.get("User", []):
            obj = User.objects.filter(pk=entry["pk"]).first()
            if obj is None:
                continue
            label = entry["obj"]
            obj.delete()
            self.stdout.write(f"  刪除 User: {label}")

        for entry in by_model.get("Term", []):
            obj = Term.objects.filter(pk=entry["pk"]).first()
            if obj is None:
                continue
            label = entry["obj"]
            obj.delete()
            self.stdout.write(f"  刪除 Term: {label}")
