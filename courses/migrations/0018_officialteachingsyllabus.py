import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("courses", "0017_course_owning_department"),
    ]

    operations = [
        migrations.CreateModel(
            name="OfficialTeachingSyllabus",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True,
                                           serialize=False, verbose_name="ID")),
                ("academic_year", models.CharField(max_length=20, verbose_name="學年度")),
                ("semester", models.CharField(max_length=20, verbose_name="學期")),
                ("class_code", models.CharField(max_length=20, verbose_name="班級代碼")),
                ("official_serial", models.CharField(max_length=20, verbose_name="官方序號")),
                ("course_code", models.CharField(max_length=20, verbose_name="課號")),
                ("course_name", models.CharField(blank=True, max_length=100, verbose_name="中文課名")),
                ("course_name_en", models.CharField(blank=True, max_length=200, verbose_name="英文課名")),
                ("credits", models.DecimalField(blank=True, decimal_places=2, max_digits=6,
                                                null=True, verbose_name="官方學分")),
                ("weekly_hours", models.DecimalField(blank=True, decimal_places=2, max_digits=6,
                                                     null=True, verbose_name="每週時數")),
                ("teacher_name", models.CharField(blank=True, max_length=100, verbose_name="授課教師")),
                ("teaching_target", models.TextField(blank=True, verbose_name="教學目標")),
                ("teaching_matter", models.TextField(blank=True, verbose_name="教學內容")),
                ("weekly_progress", models.JSONField(blank=True, default=dict, verbose_name="18週進度")),
                ("assessment", models.TextField(blank=True, verbose_name="成績評量")),
                ("references", models.TextField(blank=True, verbose_name="參考資料")),
                ("source_url", models.URLField(blank=True, max_length=500, verbose_name="官方來源網址")),
                ("fetched_at", models.DateTimeField(blank=True, null=True, verbose_name="同步時間")),
                ("status", models.CharField(choices=[("pending", "待同步"), ("success", "已同步"),
                                                     ("failed", "同步失敗"), ("conflict", "資料不一致")],
                                            default="pending", max_length=20, verbose_name="結果狀態")),
                ("error_message", models.TextField(blank=True, verbose_name="錯誤原因")),
                ("raw_payload", models.JSONField(blank=True, default=dict, verbose_name="原始識別資料")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="建立時間")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="更新時間")),
            ],
            options={
                "verbose_name": "官方教學大綱",
                "verbose_name_plural": "官方教學大綱",
                "ordering": ["academic_year", "semester", "class_code", "course_code"],
            },
        ),
        migrations.AddConstraint(
            model_name="officialteachingsyllabus",
            constraint=models.UniqueConstraint(
                fields=("academic_year", "semester", "class_code",
                        "official_serial", "course_code"),
                name="official_syllabus_identity_unique",
            ),
        ),
    ]
