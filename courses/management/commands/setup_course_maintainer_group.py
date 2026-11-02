"""初始化「Course Maintainer（課程維護員）」群組與必要權限。

用法：
    python manage.py setup_course_maintainer_group
    python manage.py setup_course_maintainer_group --dry-run

這個指令只做兩件事：
1. 建立／對齊 `Course Maintainer` 群組，把維護員需要的模型權限放進去。
2. 印出目前的權限與成員狀態。

它**不會**把任何學生加入群組。維護員的身分一律由 superuser 在 Django Admin
新增 `CourseMaintainerScope`（維護員權限範圍）時指定，並由那裡同步群組與
staff 權限。信箱網域（@ntub.edu.tw）本身不帶任何權限。

可重複執行：權限是「對齊」而不是「累加」，多跑幾次結果一樣。
"""

from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand

from ...maintainer import MAINTAINER_GROUP_NAME, ensure_maintainer_group


class Command(BaseCommand):
    help = "建立／對齊課程維護員群組與權限（不會自動把任何學生加入群組）。"

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true",
            help="只顯示會做什麼，不真的寫入資料。")

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        result = ensure_maintainer_group(apply=not dry_run)
        group = result["group"]
        to_add = result["added"]
        to_remove = result["removed"]
        missing = result["missing"]

        self.stdout.write(f"群組：{MAINTAINER_GROUP_NAME}"
                          + (f"（尚未建立，dry-run 不會建立）" if group is None
                             else f"（id={group.pk}）"))
        if group is not None:
            self.stdout.write(f"  權限總數：{group.permissions.count()} 筆")
        if to_add:
            self.stdout.write(f"  將新增權限 {len(to_add)} 筆：")
            for permission in to_add:
                self.stdout.write(
                    f"    + {permission.content_type.app_label}.{permission.codename}")
        if to_remove:
            self.stdout.write(f"  將移除權限 {len(to_remove)} 筆：")
            for permission in to_remove:
                self.stdout.write(
                    f"    - {permission.content_type.app_label}.{permission.codename}")
        if not to_add and not to_remove:
            self.stdout.write("  權限已經是最新，無須變更。")
        if missing:
            self.stdout.write(self.style.WARNING(
                "  找不到權限（請先跑 makemigrations + migrate）：" + ", ".join(missing)))

        if dry_run:
            self.stdout.write(self.style.WARNING("dry-run：沒有寫入任何資料。"))
        else:
            self.stdout.write(self.style.SUCCESS("群組權限已對齊。"))

        members = Group.objects.get(pk=group.pk).user_set.all() if group is not None else []
        self.stdout.write(f"  目前群組成員：{len(members)} 位"
                          + (f"（{', '.join(str(u) for u in members)}）" if members else ""))
        self.stdout.write(
            "下一步：由 superuser 在 Django Admin 的「維護員權限範圍」新增帳號與負責系所，"
            "帳號才會被加入群組並取得管理能力。")
