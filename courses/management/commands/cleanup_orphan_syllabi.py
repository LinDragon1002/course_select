"""預覽或清理沒有官方班級課表快照的課綱。"""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from ...models import (
    NtubOfferingSource,
    NtubTimetableSnapshot,
    OfficialTeachingSyllabus,
)


CONFIRMATION = "DELETE-ORPHAN-SYLLABI"


def _has_valid_timetable(syllabus):
    return NtubTimetableSnapshot.objects.filter(
        term__code=f"{syllabus.academic_year}-{syllabus.semester}",
        class_group__code=syllabus.class_code,
        entries__entry_type="course",
        entries__official_serial=syllabus.official_serial,
        entries__course_code=syllabus.course_code,
    ).exists()


def _offering_exists(syllabus):
    return NtubOfferingSource.objects.filter(
        academic_year=syllabus.academic_year,
        semester=syllabus.semester,
        class_code=syllabus.class_code,
        official_serial=syllabus.official_serial,
        course_code=syllabus.course_code,
    ).exists()


class Command(BaseCommand):
    help = "預覽或清理沒有官方班級課表快照的 OfficialTeachingSyllabus。"

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply", action="store_true",
            help="實際刪除再次確認仍為孤兒的課綱。",
        )
        parser.add_argument(
            "--confirm", default="",
            help=f"正式刪除時必須指定 {CONFIRMATION}。",
        )

    def _candidates(self):
        candidates = []
        for syllabus in OfficialTeachingSyllabus.objects.filter(
                snapshot__isnull=True).order_by("pk"):
            if not _has_valid_timetable(syllabus):
                candidates.append((syllabus, _offering_exists(syllabus)))
        return candidates

    def _print_candidates(self, candidates):
        for syllabus, has_offering in candidates:
            source = "仍有 NtubOfferingSource 對應" if has_offering else "無 NtubOfferingSource 對應"
            self.stdout.write(
                f"PK={syllabus.pk} "
                f"{syllabus.academic_year}-{syllabus.semester} "
                f"{syllabus.class_code} "
                f"序號={syllabus.official_serial} "
                f"課號={syllabus.course_code} "
                f"狀態={syllabus.status} "
                f"[{source}]")

    def handle(self, *args, **options):
        if options["apply"] and options["confirm"] != CONFIRMATION:
            raise CommandError(
                f"--apply 必須同時指定 --confirm {CONFIRMATION}。")

        candidates = self._candidates()
        self.stdout.write(f"預計清理課綱：{len(candidates)} 筆")
        self._print_candidates(candidates)

        if not options["apply"]:
            self.stdout.write("Dry-run：資料庫沒有變動。")
            return

        candidate_ids = {syllabus.pk for syllabus, _ in candidates}
        with transaction.atomic():
            deletable = []
            for syllabus in OfficialTeachingSyllabus.objects.select_for_update().filter(
                    pk__in=candidate_ids, snapshot__isnull=True).order_by("pk"):
                if not _has_valid_timetable(syllabus):
                    deletable.append(syllabus.pk)
            if len(deletable) != len(candidate_ids):
                raise CommandError(
                    "清理前重新核對發現資料已改變，已中止刪除，請重新 dry-run。")
            deleted_count, _ = OfficialTeachingSyllabus.objects.filter(
                pk__in=deletable, snapshot__isnull=True).delete()
        self.stdout.write(f"已清理孤兒課綱：{deleted_count} 筆")
