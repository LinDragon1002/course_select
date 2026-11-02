"""把 NTUB 官方專業選修開課匯入 Django ORM。

用法：
    python manage.py import_ntub_offerings
    python manage.py import_ntub_offerings --file data/ntub_import_ready/offerings.json
    python manage.py import_ntub_offerings --dry-run

只寫入官方開課、來源對應與課綱推薦，不動 Enrollment，也不改目前學期設定。
重複執行不會產生重複資料（來源鍵相同即視為同一筆開課）。
"""

from django.core.management.base import BaseCommand, CommandError

from ...ntub_offerings_import import (OfferingImportError, default_payload_path,
                                      import_from_file, load_payload,
                                      validate_payload)


class Command(BaseCommand):
    help = "匯入 NTUB 官方專業選修開課與課綱推薦，可重複執行"

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
        except OfferingImportError as exc:
            raise CommandError(f"來源資料驗證失敗，未寫入任何資料：{exc}")

        offerings = payload.get("offerings") or []
        recommendations = payload.get("curriculum_recommendations") or []
        ambiguities = payload.get("ambiguities") or []
        self.stdout.write(self.style.WARNING(
            f"dry-run：來源 {path} 通過驗證，"
            f"將寫入 {len(offerings)} 筆官方開課、{len(recommendations)} 筆課綱推薦、"
            f"{len(ambiguities)} 筆疑義，未寫入資料庫"))
        for offering in offerings:
            self.stdout.write(
                f"  · {offering.get('key')} {offering.get('course_code')} "
                f"{offering.get('course_name')}（{offering.get('class_code')}）")
        if opts["dry_run"]:
            return

        try:
            summary = import_from_file(path)
        except OfferingImportError as exc:
            raise CommandError(f"匯入失敗，已整批回滾：{exc}")

        self.stdout.write(self.style.SUCCESS(
            "匯入完成：官方開課來源新增 {offerings_created} 筆、更新 {offerings_updated} 筆；"
            "課程新增 {courses_created} 筆、沿用 {courses_reused} 筆；"
            "班別新增 {sections_created} 筆、沿用 {sections_reused} 筆；"
            "教師新增 {teachers_created} 筆、沿用 {teachers_reused} 筆；"
            "課綱推薦新增 {recommendations_created} 筆、更新 {recommendations_updated} 筆；"
            "候選開課關係新增 {candidates_created} 筆、更新 {candidates_updated} 筆；"
            "疑義 {ambiguities_created} 筆；"
            "新增加選紀錄 {enrollments_created} 筆".format(**summary.as_dict())))

        if summary.has_conflicts:
            self.stdout.write(self.style.WARNING(
                f"發現 {len(summary.conflicts)} 筆資料衝突（既有資料未被覆寫）："))
            for conflict in summary.conflicts:
                self.stdout.write(
                    f"  ! [{conflict['kind']}] {conflict['key']}：{conflict['message']}")

        self.stdout.write(self.style.SUCCESS(
            "註：官方已選人數只保留在來源紀錄裡，本地加選人數一律以 Enrollment 統計；"
            "官方開課不會建立任何學生加選紀錄。"))
