"""把 NTUB 官方班級課表 JSON 標準化成可匯入 Django ORM 的 payload。

用法（在 repo 根目錄）::

    python manage.py prepare_ntub_timetables
    python manage.py prepare_ntub_timetables --source-dir <來源目錄> --output <輸出路徑>
    python manage.py prepare_ntub_timetables --check   # 只比對，不寫檔

預設輸出 `data/ntub_import_ready/timetables.json`。
這支命令只產生檔案，不寫入資料庫：官方課表是班級層級的資料，不是某位學生的
選課結果，因此不會（也不應）順手建立 Enrollment。
"""

from __future__ import annotations

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from courses.ntub_timetable_payload import (
    DEFAULT_OUTPUT_RELPATH,
    build_payload_from_dir,
    default_output_path,
    resolve_source_dir,
    write_payload,
)


class Command(BaseCommand):
    help = "把 NTUB 官方班級課表 JSON 標準化，輸出可匯入 Django ORM 的 timetables.json"

    def add_arguments(self, parser):
        parser.add_argument(
            "--source-dir",
            default=None,
            help="官方課表 JSON 所在目錄（預設用 NTUB_TIMETABLE_SOURCE_DIR 或內建常數）。",
        )
        parser.add_argument(
            "--output",
            default=None,
            help=f"輸出路徑（預設 {DEFAULT_OUTPUT_RELPATH}）。",
        )
        parser.add_argument(
            "--check",
            action="store_true",
            help="只比對現有輸出與重新產生的內容是否一致，不寫入檔案。",
        )

    def handle(self, *args, **opts):
        source_dir = resolve_source_dir(opts.get("source_dir"))
        output_path = Path(opts["output"]) if opts.get("output") else default_output_path()

        if not source_dir.is_dir():
            raise CommandError(f"來源目錄不存在：{source_dir}")

        payload = build_payload_from_dir(source_dir)

        if opts.get("check"):
            if not output_path.exists():
                raise CommandError(f"輸出檔不存在，無法比對：{output_path}")
            current = json.loads(output_path.read_text(encoding="utf-8"))
            # generated_at 每次執行都不同，比對時排除這欄。
            current.pop("generated_at", None)
            expected = dict(payload)
            expected.pop("generated_at", None)
            if current == expected:
                self.stdout.write(self.style.SUCCESS(f"內容一致：{output_path}"))
                return
            raise CommandError(f"內容與重新產生的結果不一致：{output_path}")

        write_payload(payload, output_path)

        totals = payload["totals"]
        self.stdout.write(
            self.style.SUCCESS(
                f"已輸出 {output_path}："
                f"{totals['class_count']} 班級快照、"
                f"{totals['entry_count']} 筆明細"
                f"（課程 {totals['course_count']}、活動 {totals['activity_count']}）"
            )
        )
        for snapshot in payload["classes"]:
            counts = snapshot["counts"]
            self.stdout.write(
                f"  {snapshot['class_code']} {snapshot['class_name']} "
                f"({snapshot['term_code']}): "
                f"課程 {counts['course']}、活動 {counts['activity']}"
            )
        self.stdout.write("（未寫入資料庫；官方課表不產生 Enrollment。）")
