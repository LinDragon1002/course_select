"""把 NTUB 官方班級課表匯入 Django ORM。

用法：
    python manage.py import_ntub_timetables
    python manage.py import_ntub_timetables --file data/ntub_import_ready/timetables.json
    python manage.py import_ntub_timetables --dry-run

只寫入班級課表快照與明細，不動 Course / Section / Enrollment，也不改目前學期設定。
重複執行不會產生重複資料（同一組班級＋學期會更新既有快照）。
"""

from django.core.management.base import BaseCommand, CommandError

from ...ntub_timetable_import import (TimetableImportError, default_payload_path,
                                      import_from_file, load_payload, validate_payload)


class Command(BaseCommand):
    help = "匯入 NTUB 官方班級課表（班級課表快照與明細），可重複執行"

    def add_arguments(self, parser):
        parser.add_argument(
            "--file",
            default=str(default_payload_path()),
            help="來源 JSON 路徑（預設：%s）" % default_payload_path())
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="只驗證來源資料，不寫入資料庫")

    def handle(self, *args, **opts):
        path = opts["file"]
        try:
            payload = load_payload(path)
            validate_payload(payload)
        except TimetableImportError as exc:
            raise CommandError(f"來源資料驗證失敗，未寫入任何資料：{exc}")

        classes = payload.get("classes") or []
        entries = sum(len(c.get("entries") or []) for c in classes)
        self.stdout.write(self.style.WARNING(
            f"dry-run：來源 {path} 通過驗證，"
            f"將寫入 {len(classes)} 筆快照、{entries} 筆明細，未寫入資料庫"))
        if opts["dry_run"]:
            return

        try:
            summary = import_from_file(path)
        except TimetableImportError as exc:
            raise CommandError(f"匯入失敗，已整批回滾：{exc}")

        self.stdout.write(self.style.SUCCESS(
            "匯入完成：快照新增 {snapshots_created} 筆、更新 {snapshots_updated} 筆；"
            "明細新增 {entries_created} 筆、更新 {entries_updated} 筆、"
            "刪除 {entries_deleted} 筆；課程 {course_entries} 筆、活動 {activity_entries} 筆；"
            "新建學期 {terms_created} 筆".format(**summary.as_dict())))
