from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand

from courses.teacher_access import (
    SYLLABUS_TEACHER_DISPLAY_NAME,
    SYLLABUS_TEACHER_GROUP_NAME,
)


class Command(BaseCommand):
    help = "建立／對齊任課教師（課綱提交）專屬群組，不授予任何模型權限。"

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true",
            help="只顯示目前狀態，不建立或修改群組。",
        )

    def handle(self, *args, **options):
        group = Group.objects.filter(name=SYLLABUS_TEACHER_GROUP_NAME).first()
        dry_run = options["dry_run"]
        if group is None:
            if dry_run:
                self.stdout.write(self.style.WARNING(
                    f"{SYLLABUS_TEACHER_GROUP_NAME} 尚未建立；dry-run 未寫入。"
                ))
                return
            group = Group.objects.create(name=SYLLABUS_TEACHER_GROUP_NAME)
            self.stdout.write(f"已建立群組：{SYLLABUS_TEACHER_DISPLAY_NAME}")

        permissions = list(group.permissions.all())
        if dry_run:
            self.stdout.write(
                f"群組：{SYLLABUS_TEACHER_DISPLAY_NAME}（{group.name}）"
            )
            self.stdout.write(f"目前模型權限：{len(permissions)} 筆")
            return

        if permissions:
            group.permissions.clear()
        self.stdout.write(
            f"群組：{SYLLABUS_TEACHER_DISPLAY_NAME}（{group.name}）"
        )
        self.stdout.write(
            f"已移除模型權限 {len(permissions)} 筆；教師頁面以後端身分檢查授權。"
        )
