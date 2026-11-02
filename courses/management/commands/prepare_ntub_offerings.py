"""把既有 NTUB 爬蟲輸出整理成可供 Django 匯入的統一 JSON。

用法::

    python manage.py prepare_ntub_offerings
    python manage.py prepare_ntub_offerings --source <來源目錄> --curriculum <課綱檔>
    python manage.py prepare_ntub_offerings --stdout

不會重新爬蟲，只讀取已存在的來源檔。
"""

from django.core.management.base import BaseCommand

from courses.ntub_offerings_payload import (
    DEFAULT_CURRICULUM_FILE,
    DEFAULT_OFFERINGS_DIR,
    DEFAULT_OUTPUT_PATH,
    build_payload,
    write_payload,
)


class Command(BaseCommand):
    help = "讀取既有 NTUB 官方開課 JSON，標準化後輸出 data/ntub_import_ready/offerings.json"

    def add_arguments(self, parser):
        parser.add_argument(
            "--source",
            default=str(DEFAULT_OFFERINGS_DIR),
            help="爬蟲輸出來源目錄（預設：%s）" % DEFAULT_OFFERINGS_DIR,
        )
        parser.add_argument(
            "--curriculum",
            default=str(DEFAULT_CURRICULUM_FILE),
            help="課綱推薦來源檔（預設：%s）" % DEFAULT_CURRICULUM_FILE,
        )
        parser.add_argument(
            "--output",
            default=str(DEFAULT_OUTPUT_PATH),
            help="輸出檔路徑（預設：%s）" % DEFAULT_OUTPUT_PATH,
        )
        parser.add_argument(
            "--stdout",
            action="store_true",
            help="只印出 JSON 到標準輸出，不寫檔。",
        )

    def handle(self, *args, **opts):
        payload = build_payload(source_dir=opts["source"], curriculum_file=opts["curriculum"])
        stats = payload["stats"]

        if opts["stdout"]:
            import json

            self.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2))
            return

        path = write_payload(payload, opts["output"])
        self.stdout.write(
            "來源檔 %s 個、關鍵字 %s 個；官方開課 %s 筆（合併重複 %s 筆）；課綱推薦 %s 筆"
            % (
                stats["source_files"],
                stats["keywords"],
                stats["unique_offerings"],
                stats["duplicates_merged"],
                stats["curriculum_recommendations"],
            )
        )
        self.stdout.write(
            "推薦對應到開課：完全對應 %s 筆、同課名但班別不同 %s 筆、未對應 %s 筆；疑義：%s 筆"
            % (
                stats["recommendations_matched"],
                stats["recommendations_linked_other_class"],
                stats["recommendations_unlinked"],
                stats["ambiguities"],
            )
        )
        self.stdout.write("已輸出：%s" % path)
