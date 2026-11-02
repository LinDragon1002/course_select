from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone


class Teacher(models.Model):
    name = models.CharField("姓名", max_length=50)
    email = models.EmailField("信箱", blank=True)
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                related_name="teacher_profile", verbose_name="登入帳號")

    def __str__(self):
        return self.name


class Term(models.Model):
    code = models.CharField("學期", max_length=20, unique=True)  # 例如 114-1
    is_current = models.BooleanField("當前學期", default=False)
    enroll_open = models.DateTimeField("選課開放時間", null=True, blank=True)  # null = 不限
    enroll_close = models.DateTimeField("選課截止時間", null=True, blank=True)

    class Meta:
        ordering = ["-code"]

    def __str__(self):
        return self.code

    @classmethod
    def current(cls):
        """選課與課表都以當前學期為準；沒有標記時取最新的。"""
        return cls.objects.filter(is_current=True).first() or cls.objects.first()

    @property
    def has_window(self):
        return bool(self.enroll_open or self.enroll_close)


class Course(models.Model):
    term = models.ForeignKey(Term, on_delete=models.PROTECT, related_name="courses", verbose_name="學期")
    owning_department = models.ForeignKey(
        "AcademicDepartment", on_delete=models.PROTECT, null=True, blank=True,
        related_name="owned_courses", verbose_name="管理系所",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="created_courses", verbose_name="建立者",
    )
    code = models.CharField("課號", max_length=20)
    name = models.CharField("課名", max_length=100)
    teacher = models.ForeignKey(Teacher, on_delete=models.PROTECT, verbose_name="老師")
    credits = models.PositiveSmallIntegerField("學分", default=3)
    syllabus = models.TextField("課程大綱", blank=True)
    prerequisites = models.ManyToManyField(
        "self", symmetrical=False, related_name="dependent_courses",
        blank=True, verbose_name="先修課程")  # 1–2 門，admin 表單 clean_prerequisites() 把關

    class Meta:
        ordering = ["code"]
        constraints = [
            models.UniqueConstraint(fields=["term", "code"], name="course_code_per_term"),
        ]

    def __str__(self):
        return f"{self.code} {self.name}"


class CourseTeachingSyllabus(models.Model):
    """助教維護的結構化課綱，與官方同步快取分開保存。"""

    class Status(models.TextChoices):
        DRAFT = "DRAFT", "草稿"
        PUBLISHED = "PUBLISHED", "已發布"

    course = models.OneToOneField(
        Course, on_delete=models.CASCADE, related_name="teaching_syllabus",
        verbose_name="課程",
    )
    teaching_target = models.TextField("教學目標", blank=True)
    teaching_matter = models.TextField("教學內容", blank=True)
    weekly_progress = models.JSONField("18週進度", default=dict, blank=True)
    assessment = models.TextField("成績評量", blank=True)
    references = models.TextField("指定教科書及參考書籍", blank=True)
    status = models.CharField(
        "狀態", max_length=12, choices=Status.choices, default=Status.DRAFT,
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="updated_course_syllabi", verbose_name="更新者",
    )
    updated_at = models.DateTimeField("更新時間", auto_now=True)

    class Meta:
        verbose_name = "人工教學大綱"
        verbose_name_plural = "人工教學大綱"

    def __str__(self):
        return f"{self.course}（{self.get_status_display()}）"


class CourseSyllabusSubmission(models.Model):
    """Teacher-submitted syllabus versions awaiting maintainer review."""

    class Status(models.TextChoices):
        DRAFT = "DRAFT", "草稿"
        PENDING = "PENDING", "待助教審核"
        RETURNED = "RETURNED", "退回修改"
        PUBLISHED = "PUBLISHED", "已發布"

    course = models.ForeignKey(
        Course, on_delete=models.CASCADE, related_name="syllabus_submissions",
        verbose_name="課程",
    )
    supersedes = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="resubmissions", verbose_name="前一提交版本",
    )
    teacher = models.ForeignKey(
        Teacher, on_delete=models.PROTECT, related_name="syllabus_submissions",
        verbose_name="提交教師",
    )
    teaching_target = models.TextField("教學目標", blank=True)
    teaching_matter = models.TextField("教學內容", blank=True)
    weekly_progress = models.JSONField("18週進度", default=dict, blank=True)
    assessment = models.TextField("成績評量", blank=True)
    references = models.TextField("指定教科書及參考書籍", blank=True)
    status = models.CharField(
        "審核狀態", max_length=16, choices=Status.choices, default=Status.DRAFT,
    )
    submitted_at = models.DateTimeField("提交時間", null=True, blank=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="reviewed_course_syllabus_submissions", verbose_name="審核人",
    )
    reviewed_at = models.DateTimeField("審核時間", null=True, blank=True)
    return_reason = models.TextField("退回原因", blank=True)
    created_at = models.DateTimeField("建立時間", auto_now_add=True)
    updated_at = models.DateTimeField("更新時間", auto_now=True)

    class Meta:
        ordering = ["-created_at", "-pk"]
        verbose_name = "教師教學大綱提交"
        verbose_name_plural = "教師教學大綱提交"
        indexes = [
            models.Index(fields=["status", "submitted_at"],
                         name="course_syllabus_review_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["course"],
                condition=models.Q(status="PENDING"),
                name="course_one_pending_syllabus",
            ),
        ]

    def __str__(self):
        return f"{self.course}（{self.get_status_display()}）"


class Section(models.Model):
    """課程的一個班別：有自己的時間、教室、名額。學生加選的對象是班別，不是課程。"""

    DAYS = [(1, "週一"), (2, "週二"), (3, "週三"), (4, "週四"), (5, "週五")]
    # 課程列表的時段篩選：以班別 start_period 為準
    PERIOD_SLOTS = [("am", "上午(1-3)"), ("midday", "中午(4-6)"), ("pm", "下午(7-9)"), ("evening", "晚上(10+)")]
    PERIOD_SLOT_RANGES = {"am": (1, 3), "midday": (4, 6), "pm": (7, 9), "evening": (10, None)}

    course = models.ForeignKey(Course, on_delete=models.CASCADE, related_name="sections", verbose_name="課程")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="created_sections", verbose_name="建立者",
    )
    label = models.CharField("班別", max_length=10)  # 例如 A / B / C
    day = models.PositiveSmallIntegerField("星期", choices=DAYS)
    start_period = models.PositiveSmallIntegerField("起始節次")
    end_period = models.PositiveSmallIntegerField("結束節次")
    room = models.CharField("教室", max_length=30, blank=True)
    capacity = models.PositiveSmallIntegerField("名額", default=40)

    class Meta:
        ordering = ["course__code", "label"]
        constraints = [
            models.UniqueConstraint(fields=["course", "label"], name="section_label_per_course"),
            models.CheckConstraint(
                condition=models.Q(start_period__gte=1) & models.Q(end_period__gte=models.F("start_period")),
                name="section_periods_valid",
            ),
        ]

    def clean(self):
        if self.start_period and self.end_period and self.end_period < self.start_period:
            raise ValidationError({"end_period": "結束節次不可早於起始節次。"})

    def __str__(self):
        return f"{self.course} {self.label}班"

    @property
    def time_display(self):
        return f"{self.get_day_display()} 第{self.start_period}-{self.end_period}節"

    @property
    def periods(self):
        return self.end_period - self.start_period + 1

    @property
    def label_display(self):
        return f"{self.label} 班"

    @property
    def enrolled_count(self):
        annotated = getattr(self, "enrolled", None)  # 有 annotate 就用現成的，沒有才查
        return self.enrollments.count() if annotated is None else annotated

    @property
    def is_full(self):
        return self.enrolled_count >= self.capacity

    def conflicts_with(self, other):
        """兩個班別的時段是否重疊（同一天的節次範圍相交）。"""
        return (self.day == other.day and self.start_period <= other.end_period
                and other.start_period <= self.end_period)


class Enrollment(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    section = models.ForeignKey(Section, on_delete=models.CASCADE, related_name="enrollments", verbose_name="班別")
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        # 同一班別只選一次；一門課可以有幾個班別，但加選規則只允許進一班（見 services.register）
        constraints = [models.UniqueConstraint(fields=["user", "section"], name="one_per_section")]

    def __str__(self):
        return f"{self.user} - {self.section}"


class Waitlist(models.Model):
    """額滿班別的等待名單：依 queued_at 順序，退選釋出名額時自動遞補。"""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="waitlists")
    section = models.ForeignKey(Section, on_delete=models.CASCADE, related_name="waitlists", verbose_name="班別")
    queued_at = models.DateTimeField("排隊時間", auto_now_add=True)
    promoted_at = models.DateTimeField("遞補時間", null=True, blank=True)
    promoted_seen_at = models.DateTimeField("遞補通知已讀", null=True, blank=True)

    class Meta:
        ordering = ["queued_at"]
        verbose_name = "等待名單"
        constraints = [models.UniqueConstraint(fields=["user", "section"], name="waitlist_one_per_section")]

    def __str__(self):
        return f"{self.user} 等 {self.section}"


# --- NTUB 學籍目錄 ---------------------------------------------------------------
# 來源：ntubook_academic_missing_rows_restore.sql（NTUB 校方下發的學籍班級清單）
# 三層結構：學制（AcademicProgramType）→ 系所（AcademicDepartment）→ 班級（AcademicClassGroup），
# 全部掛在一次匯入批次（AcademicCatalog）底下，讓每筆資料都能回溯來源檔名與雜湊。


class AcademicCatalog(models.Model):
    """一次學籍目錄匯入批次：對應來源 SQL 的 academic_catalogs / academic_catalog_sources。"""

    version = models.CharField("版本", max_length=100, unique=True)  # 例如 ntub-20260724-755fe94f
    source_key = models.CharField("來源代碼", max_length=50, default="ntub")
    source_name = models.CharField("來源名稱", max_length=100, blank=True)
    last_successful_sync_at = models.DateTimeField("最後成功同步時間", null=True, blank=True)
    schema_version = models.PositiveSmallIntegerField("結構版本", default=1)
    source_file_name = models.CharField("來源檔名", max_length=255, blank=True)
    file_sha256 = models.CharField("來源檔雜湊", max_length=64, blank=True)
    academic_year = models.CharField("學年度", max_length=20, null=True, blank=True)
    term = models.CharField("學期", max_length=20, null=True, blank=True)
    status = models.CharField("狀態", max_length=20, default="ACTIVE")
    fetched_at = models.DateTimeField("擷取時間", null=True, blank=True)
    imported_at = models.DateTimeField("匯入時間", null=True, blank=True)
    source_row_count = models.PositiveIntegerField("來源列數", default=0)
    program_count = models.PositiveIntegerField("學制數", default=0)
    department_count = models.PositiveIntegerField("系所數", default=0)
    class_group_count = models.PositiveIntegerField("班級數", default=0)
    selectable_class_group_count = models.PositiveIntegerField("可選班級數", default=0)

    class Meta:
        ordering = ["-version"]
        verbose_name = "學籍目錄批次"

    def __str__(self):
        return self.version


class AcademicProgramType(models.Model):
    """學制：例如 碩士班 / 四技日間部 / 二年制進修部。"""

    catalog = models.ForeignKey(AcademicCatalog, on_delete=models.CASCADE,
                                 related_name="program_types", verbose_name="目錄批次")
    code = models.CharField("學制代碼", max_length=20)  # 例如 1 / 4 / S
    name_zh = models.CharField("學制名稱", max_length=100, blank=True)
    name_en = models.CharField("學制英文名稱", max_length=100, blank=True)
    is_active = models.BooleanField("有效", default=True)
    is_registration_selectable = models.BooleanField("可註冊", default=True)
    sort_order = models.PositiveIntegerField("排序", default=0)
    source_last_seen_at = models.DateTimeField("來源最後出現時間", null=True, blank=True)

    class Meta:
        ordering = ["sort_order", "code"]
        verbose_name = "學制"
        verbose_name_plural = "學制"
        constraints = [
            models.UniqueConstraint(fields=["code"], name="academic_program_type_code_unique"),
        ]

    def __str__(self):
        return f"{self.code} {self.name_zh}".strip()


class AcademicDepartment(models.Model):
    """系所：隸屬於某個學制，例如 資訊管理系（406）隸屬四技日間部。"""

    catalog = models.ForeignKey(AcademicCatalog, on_delete=models.CASCADE,
                                 related_name="departments", verbose_name="目錄批次")
    program_type = models.ForeignKey(AcademicProgramType, on_delete=models.PROTECT,
                                     related_name="departments", verbose_name="學制")
    code = models.CharField("系所代碼", max_length=20)  # 例如 406
    name_zh = models.CharField("系所名稱", max_length=100, blank=True)
    name_en = models.CharField("系所英文名稱", max_length=100, blank=True)
    is_active = models.BooleanField("有效", default=True)
    is_registration_selectable = models.BooleanField("可註冊", default=True)
    sort_order = models.PositiveIntegerField("排序", default=0)
    unit_type = models.CharField("單位類型", max_length=32, blank=True)  # 例如 ACADEMIC
    source_last_seen_at = models.DateTimeField("來源最後出現時間", null=True, blank=True)

    class Meta:
        ordering = ["program_type__sort_order", "code"]
        verbose_name = "系所"
        verbose_name_plural = "系所"
        constraints = [
            models.UniqueConstraint(fields=["code"], name="academic_department_code_unique"),
        ]

    def __str__(self):
        return f"{self.code} {self.name_zh}".strip()


class AcademicClassGroup(models.Model):
    """班級：學校開的班，學生選課時的單位，例如 40620A（四技資管二年甲班）。"""

    GROUP_TYPES = [
        ("COHORT", "一般班級"),
        ("GENERAL_EDUCATION", "通識教育"),
        ("PHYSICAL_EDUCATION", "體育"),
        ("MILITARY_EDUCATION", "軍訓"),
    ]

    catalog = models.ForeignKey(AcademicCatalog, on_delete=models.CASCADE,
                                 related_name="class_groups", verbose_name="目錄批次")
    department = models.ForeignKey(AcademicDepartment, on_delete=models.CASCADE,
                                   related_name="class_groups", verbose_name="系所")
    code = models.CharField("班級代碼", max_length=20)  # 例如 40620A
    name_zh = models.CharField("班級名稱", max_length=100, blank=True)
    grade_no = models.PositiveSmallIntegerField("年級", null=True, blank=True)
    section_code = models.CharField("班級類型", max_length=10, null=True, blank=True)  # A / B / C / D
    group_type = models.CharField("群組類型", max_length=32, choices=GROUP_TYPES, blank=True)
    is_active = models.BooleanField("有效", default=True)
    is_registration_selectable = models.BooleanField("可選", default=False)
    sort_order = models.PositiveIntegerField("排序", default=0)
    source_payload = models.JSONField("來源原始資料", default=dict, blank=True)

    class Meta:
        ordering = ["department__code", "code"]
        verbose_name = "班級"
        verbose_name_plural = "班級"
        constraints = [
            models.UniqueConstraint(fields=["code"], name="academic_class_group_code_unique"),
        ]

    def __str__(self):
        return f"{self.code} {self.name_zh}".strip()


class StudentAcademicProfile(models.Model):
    """學生學籍：綁定一個班級，學制／系所／年級都從 class_group 關聯取得。

    不另外存學號或年級欄位：年級屬於班級，學制屬於系所，
    避免同一份學籍在多處重複而互相矛盾。
    """

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                                related_name="academic_profile", verbose_name="學生")
    class_group = models.ForeignKey(AcademicClassGroup, on_delete=models.PROTECT,
                                   related_name="student_profiles", verbose_name="班級")
    completed_at = models.DateTimeField("完成設定時間", default=timezone.now)

    class Meta:
        verbose_name = "學生學籍"
        verbose_name_plural = "學生學籍"

    def __str__(self):
        return f"{self.user} → {self.class_group}"

    @property
    def program_type(self):
        return self.class_group.department.program_type

    @property
    def department(self):
        return self.class_group.department

    @property
    def grade_no(self):
        return self.class_group.grade_no


class NtubTimetableSnapshot(models.Model):
    """班級固定課表的一個版本；官方匯入版與系所維護版並存追溯。

    快照不是學生的個人選課結果，不會產生 Enrollment。官方匯入快照維持原始
    來源版本；維護員接管後另建人工草稿，發布版優先供學生使用。
    """

    class SourceKind(models.TextChoices):
        IMPORTED = "IMPORTED", "官方匯入"
        MAINTAINED = "MAINTAINED", "系所維護"

    class Status(models.TextChoices):
        DRAFT = "DRAFT", "草稿"
        PUBLISHED = "PUBLISHED", "已發布"
        ARCHIVED = "ARCHIVED", "歷史版本"

    class_group = models.ForeignKey(AcademicClassGroup, on_delete=models.PROTECT,
                                    related_name="timetable_snapshots", verbose_name="班級")
    term = models.ForeignKey(Term, on_delete=models.PROTECT,
                             related_name="timetable_snapshots", verbose_name="學期")
    source_kind = models.CharField(
        "版本來源", max_length=12, choices=SourceKind.choices,
        default=SourceKind.IMPORTED,
    )
    status = models.CharField(
        "版本狀態", max_length=12, choices=Status.choices,
        default=Status.PUBLISHED,
    )
    managed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="maintained_timetable_snapshots", verbose_name="維護者",
    )
    supersedes = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="revisions", verbose_name="前一版本",
    )
    published_at = models.DateTimeField("發布時間", null=True, blank=True)
    class_name = models.CharField("班級名稱", max_length=100, blank=True)
    source_url = models.CharField("來源網址", max_length=500, blank=True)
    retrieved_at = models.DateTimeField("來源擷取時間", null=True, blank=True)
    created_at = models.DateTimeField("建立時間", auto_now_add=True)
    updated_at = models.DateTimeField("更新時間", auto_now=True)

    class Meta:
        ordering = ["term__code", "class_group__code"]
        verbose_name = "班級固定課表"
        verbose_name_plural = "班級固定課表"
        constraints = [
            models.UniqueConstraint(fields=["class_group", "term"],
                                   condition=models.Q(source_kind="IMPORTED"),
                                   name="timetable_imported_snapshot_class_term_unique"),
            models.UniqueConstraint(
                fields=["class_group", "term"],
                condition=models.Q(
                    source_kind="MAINTAINED", status="DRAFT",
                ),
                name="timetable_one_maintained_draft_per_class_term",
            ),
            models.UniqueConstraint(
                fields=["class_group", "term"],
                condition=models.Q(
                    source_kind="MAINTAINED", status="PUBLISHED",
                ),
                name="timetable_one_maintained_published_per_class_term",
            ),
        ]

    def __str__(self):
        return f"{self.term.code} {self.class_group.code} {self.class_name}".strip()


class NtubTimetableEntry(models.Model):
    """官方班級課表裡的一個時段。

    `entry_type` 保留官方的分類：`course` 是真正的課程，`activity` 是共同空堂、
    課外活動這類非課程時段。活動只有名稱，因此活動名稱放在 `course_name`，
    課號／老師／教室留空；課程分類完全照來源原樣保存（例如「軍訓(選修)」不會被
    改成必修）。
    """

    ENTRY_TYPES = [
        ("course", "課程"),
        ("activity", "活動"),
    ]

    snapshot = models.ForeignKey(NtubTimetableSnapshot, on_delete=models.CASCADE,
                                 related_name="entries", verbose_name="課表快照")
    course = models.ForeignKey(
        Course, on_delete=models.PROTECT, null=True, blank=True,
        related_name="fixed_timetable_entries", verbose_name="課程資料",
    )
    entry_type = models.CharField("類型", max_length=20, choices=ENTRY_TYPES)
    course_code = models.CharField("課號", max_length=20, blank=True)
    official_serial = models.CharField("官方序號", max_length=20, blank=True)
    course_name = models.CharField("課名", max_length=100, blank=True)
    teacher = models.ForeignKey(
        Teacher, on_delete=models.PROTECT, null=True, blank=True,
        related_name="fixed_timetable_entries", verbose_name="授課教師",
    )
    teacher_name = models.CharField("老師", max_length=50, blank=True)
    credits = models.PositiveSmallIntegerField("學分", null=True, blank=True)
    weekly_hours = models.DecimalField(
        "每週時數", max_digits=5, decimal_places=2, null=True, blank=True,
    )
    course_category = models.CharField("必修類別", max_length=50, blank=True)
    classroom = models.CharField("教室", max_length=100, blank=True)
    weekday = models.PositiveSmallIntegerField("星期")
    start_period = models.PositiveSmallIntegerField("起始節次")
    end_period = models.PositiveSmallIntegerField("結束節次")
    periods = models.JSONField("節次清單", default=list, blank=True)
    raw_text = models.TextField("原始文字", blank=True)
    course_href = models.CharField("課程連結", max_length=500, blank=True)
    is_active = models.BooleanField("啟用", default=True)

    class Meta:
        ordering = ["weekday", "start_period", "id"]
        verbose_name = "固定課表明細"
        verbose_name_plural = "固定課表明細"

    def __str__(self):
        return f"週{self.weekday} {self.start_period}-{self.end_period} {self.course_name}".strip()


class OfficialTeachingSyllabus(models.Model):
    """官方教師教學大綱快取；以校方五個識別欄位唯一定位。"""

    STATUS_CHOICES = [
        ("pending", "待同步"),
        ("success", "已同步"),
        ("failed", "同步失敗"),
        ("conflict", "資料不一致"),
    ]

    academic_year = models.CharField("學年度", max_length=20)
    semester = models.CharField("學期", max_length=20)
    class_code = models.CharField("班級代碼", max_length=20)
    official_serial = models.CharField("官方序號", max_length=20)
    course_code = models.CharField("課號", max_length=20)
    snapshot = models.ForeignKey(
        NtubTimetableSnapshot, on_delete=models.CASCADE, null=True, blank=True,
        related_name="teaching_syllabi", verbose_name="官方課表快照",
    )
    course_name = models.CharField("中文課名", max_length=100, blank=True)
    course_name_en = models.CharField("英文課名", max_length=200, blank=True)
    credits = models.DecimalField("官方學分", max_digits=6, decimal_places=2,
                                  null=True, blank=True)
    weekly_hours = models.DecimalField("每週時數", max_digits=6, decimal_places=2,
                                       null=True, blank=True)
    teacher_name = models.CharField("授課教師", max_length=100, blank=True)
    teaching_target = models.TextField("教學目標", blank=True)
    teaching_matter = models.TextField("教學內容", blank=True)
    weekly_progress = models.JSONField("18週進度", default=dict, blank=True)
    assessment = models.TextField("成績評量", blank=True)
    references = models.TextField("參考資料", blank=True)
    source_url = models.URLField("官方來源網址", max_length=500, blank=True)
    fetched_at = models.DateTimeField("同步時間", null=True, blank=True)
    status = models.CharField("結果狀態", max_length=20, choices=STATUS_CHOICES,
                              default="pending")
    error_message = models.TextField("錯誤原因", blank=True)
    raw_payload = models.JSONField("原始識別資料", default=dict, blank=True)
    created_at = models.DateTimeField("建立時間", auto_now_add=True)
    updated_at = models.DateTimeField("更新時間", auto_now=True)

    class Meta:
        verbose_name = "官方教學大綱"
        verbose_name_plural = "官方教學大綱"
        ordering = ["academic_year", "semester", "class_code", "course_code"]
        constraints = [
            models.UniqueConstraint(
                fields=["academic_year", "semester", "class_code",
                        "official_serial", "course_code"],
                name="official_syllabus_identity_unique",
            ),
        ]

    def __str__(self):
        return f"{self.academic_year}-{self.semester} {self.class_code} {self.course_code}"


class OfficialCourseCredit(models.Model):
    """管理員核對的班級官方課程學分，不修改匯入的課表資料。"""

    term = models.ForeignKey(Term, on_delete=models.PROTECT,
                             related_name="official_course_credits", verbose_name="學期")
    class_group = models.ForeignKey(AcademicClassGroup, on_delete=models.PROTECT,
                                    related_name="official_course_credits", verbose_name="班級")
    course_code = models.CharField("課號", max_length=20)
    credits = models.PositiveSmallIntegerField("確認學分")
    source_note = models.CharField("學分來源／備註", max_length=255, blank=True)

    class Meta:
        ordering = ["term__code", "class_group__code", "course_code"]
        verbose_name = "官方課程學分核對"
        verbose_name_plural = "官方課程學分核對"
        constraints = [
            models.UniqueConstraint(
                fields=["term", "class_group", "course_code"],
                name="official_course_credit_term_class_code_unique"),
        ]

    def __str__(self):
        return f"{self.term.code} {self.class_group.code} {self.course_code}（{self.credits} 學分）"


# --- NTUB 官方專業選修開課 -------------------------------------------------------
# 來源：data/ntub_import_ready/offerings.json（校方課表查詢系統的開課擷取結果）。
#
# 這些模型只描述「官方開課」這件事本身：官方資料的完整保存、官方開課與本地
# Course / Section / Teacher 的穩定對應、以及課綱推薦（哪些班級被建議修哪些課）。
#
# 刻意不做的事：
# - 不建立任何 Enrollment：官方開課是學校的課表基線，不是任何學生的個人選課結果。
# - 不猜測個人資格：StudentAcademicProfile 沒有入學年度／專班類型，所以推薦
#   資料只到「班級層級」，不會推導成個人可選課的資格。
# - 不覆寫別的來源建立的資料：遇到既有 Course / Teacher / Section 衝突時回報，
#   交由人工確認。


class CourseMaintainerScope(models.Model):
    """課程維護員的權限範圍：這個帳號被授權管理哪幾個系所。

    維護員本身是 Django 的 User（多數是 Google 登入的既有帳號），權限分兩層：

    * 「能不能進 /admin/」由 Django 的 Group 與 Model Permission 決定（見
      `courses.maintainer.ensure_maintainer_group` 與 setup_course_maintainer_group 指令）。
    * 「進去之後能動到哪些資料」由這張表決定：只碰得到授權系所底下的班級與政策。

    這裡不放權限旗標，也不放 is_superuser：維護員就是一般的 staff 帳號，
    權限範圍只靠 (user, department) 這一列一列地授權。
    """

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                             related_name="maintainer_scopes", verbose_name="維護員帳號")
    department = models.ForeignKey(AcademicDepartment, on_delete=models.CASCADE,
                                   related_name="maintainer_scopes", verbose_name="負責系所")
    is_active = models.BooleanField("有效", default=True)
    created_at = models.DateTimeField("建立時間", auto_now_add=True)
    updated_at = models.DateTimeField("更新時間", auto_now=True)

    class Meta:
        ordering = ["user__username", "department__code"]
        verbose_name = "維護員權限範圍"
        verbose_name_plural = "維護員權限範圍"
        constraints = [
            models.UniqueConstraint(fields=["user", "department"],
                                   name="course_maintainer_scope_user_department_unique"),
        ]

    def __str__(self):
        return f"{self.user} / {self.department}"


class SectionClassPolicy(models.Model):
    """某個開課班別（Section）對某個學生班級（AcademicClassGroup）的修課政策。

    同一門課的同一個班別，可以對不同班級有不同屬性。實際例子：115-1
    「網站應用程式設計」（課號 40627960）只有一個 Section，但：

    * 對 40620A（資管二甲）＝本班必修（can_enroll=False），學校已有固定班級課表，
      不需要也不允許學生自行加選。
    * 對 40630B（資管三乙）＝跨班專業選修（can_enroll=True），維護員確認發布後
      三乙學生可以自己加選。

    這是「每學期開課的適用班級政策」，不是 Course 的全域必選修分類，所以
    學期直接從 section.course.term 取，不另外存一份。
    """

    class CourseCategory(models.TextChoices):
        """修課分類：同一個開課班別對不同班級可以不同。"""

        REQUIRED = "REQUIRED", "本班必修"
        DEPARTMENT_ELECTIVE = "DEPARTMENT_ELECTIVE", "專業選修"
        PROFESSIONAL_ELECTIVE = "PROFESSIONAL_ELECTIVE", "跨班專業選修"
        GENERAL_ELECTIVE = "GENERAL_ELECTIVE", "通識選修"
        OTHER = "OTHER", "其他"

    class Status(models.TextChoices):
        """政策狀態：DRAFT 不算正式開放。"""

        DRAFT = "DRAFT", "草稿"
        PUBLISHED = "PUBLISHED", "已發布"

    section = models.ForeignKey(Section, on_delete=models.CASCADE,
                                related_name="class_policies", verbose_name="開課班別")
    class_group = models.ForeignKey(AcademicClassGroup, on_delete=models.CASCADE,
                                    related_name="section_policies", verbose_name="適用班級")
    course_category = models.CharField("修課分類", max_length=30, choices=CourseCategory.choices,
                                       default=CourseCategory.OTHER)
    can_enroll = models.BooleanField("可自行加選", default=False)
    status = models.CharField("狀態", max_length=20, choices=Status.choices, default=Status.DRAFT)
    confirmed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                     null=True, blank=True, related_name="confirmed_class_policies",
                                     verbose_name="確認帳號")
    confirmed_at = models.DateTimeField("確認時間", null=True, blank=True)
    notes = models.TextField("備註", blank=True)
    created_at = models.DateTimeField("建立時間", auto_now_add=True)
    updated_at = models.DateTimeField("更新時間", auto_now=True)

    class Meta:
        ordering = ["section__course__code", "section__label", "class_group__code"]
        verbose_name = "班別修課政策"
        verbose_name_plural = "班別修課政策"
        constraints = [
            models.UniqueConstraint(fields=["section", "class_group"],
                                   name="section_class_policy_section_class_group_unique"),
        ]

    def __str__(self):
        return f"{self.section} → {self.class_group}（{self.get_course_category_display()}）"

    @property
    def term(self):
        """學期一律從 section 的課程取得，不重複儲存。"""
        return self.section.course.term

    @property
    def is_published(self):
        """草稿不算正式開放：只有 PUBLISHED 才是有效的修課政策。"""
        return self.status == self.Status.PUBLISHED

    @property
    def allows_enrollment(self):
        """已發布而且可以自行加選，才算真正開放加選。"""
        return self.is_published and self.can_enroll


class NtubOfferingSource(models.Model):
    """一筆官方開課的來源對應：官方資料本身 + 它對應到的本地物件。

    `source_key` 是來源的唯一鍵：``學年|學期|班級代碼|序號|課號``
    （例如 ``115|1|40620A|15|40627440``）。重複匯入時以這個鍵對上既有的
    Section，不會再建立一份重複的班別。

    官方欄位一律照來源原樣保存（官方序號、來源教師 ID、教室代碼與名稱、
    選修分類、官方名額、官方已選人數、來源檔名與擷取時間）。既有模型放不下的
    欄位整包放進 `source_payload`，不丟失也不猜測。

    `official_enrolled` 只是校方公告的歷史已選人數，**不會**寫入 Enrollment；
    本地加選人數一律以 `Section.enrollments` 統計。
    """

    source_key = models.CharField("來源鍵", max_length=120, unique=True)
    academic_year = models.CharField("學年度", max_length=20)
    semester = models.CharField("學期", max_length=20)
    class_code = models.CharField("班級代碼", max_length=20)
    official_serial = models.CharField("官方序號", max_length=20)
    course_code = models.CharField("課號", max_length=20)

    term = models.ForeignKey(Term, on_delete=models.PROTECT,
                              related_name="ntub_offering_sources", verbose_name="學期")
    class_group = models.ForeignKey(AcademicClassGroup, on_delete=models.PROTECT,
                                    related_name="ntub_offering_sources", verbose_name="開課班級")
    course = models.ForeignKey(Course, on_delete=models.PROTECT,
                               related_name="ntub_offering_sources", verbose_name="課程")
    section = models.ForeignKey(Section, on_delete=models.PROTECT,
                                related_name="ntub_offering_sources", verbose_name="班別")
    teacher = models.ForeignKey(Teacher, on_delete=models.PROTECT,
                                related_name="ntub_offering_sources", verbose_name="教師")

    teacher_source_id = models.CharField("來源教師 ID", max_length=50, blank=True)
    teacher_name = models.CharField("教師姓名", max_length=50, blank=True)
    course_name = models.CharField("課程名稱", max_length=100)
    elective_category = models.CharField("選修分類", max_length=50, blank=True)
    classroom_code = models.CharField("教室代碼", max_length=50, blank=True)
    classroom_name = models.CharField("教室名稱", max_length=100, blank=True)
    official_capacity = models.PositiveIntegerField("官方名額", null=True, blank=True)
    official_enrolled = models.PositiveIntegerField("官方已選人數", null=True, blank=True)

    source_url = models.CharField("來源網址", max_length=500, blank=True)
    source_file = models.CharField("來源檔名", max_length=255, blank=True)
    retrieved_at = models.DateTimeField("來源擷取時間", null=True, blank=True)
    exported_at = models.DateTimeField("來源匯出時間", null=True, blank=True)

    # 匯入當下寫進 Section 的值：重複匯入時用來判斷有沒有人工調整過。
    applied_day = models.PositiveSmallIntegerField("匯入星期", null=True, blank=True)
    applied_start_period = models.PositiveSmallIntegerField("匯入起始節次", null=True, blank=True)
    applied_end_period = models.PositiveSmallIntegerField("匯入結束節次", null=True, blank=True)
    applied_room = models.CharField("匯入教室", max_length=30, blank=True)
    applied_capacity = models.PositiveIntegerField("匯入名額", null=True, blank=True)

    source_payload = models.JSONField("來源原始資料", default=dict, blank=True)
    created_at = models.DateTimeField("建立時間", auto_now_add=True)
    updated_at = models.DateTimeField("更新時間", auto_now=True)

    class Meta:
        ordering = ["academic_year", "semester", "class_code", "official_serial"]
        verbose_name = "官方開課來源"
        verbose_name_plural = "官方開課來源"
        constraints = [
            models.UniqueConstraint(
                fields=["academic_year", "semester", "class_code", "official_serial", "course_code"],
                name="ntub_offering_source_key_unique"),
        ]

    def __str__(self):
        return f"{self.source_key} {self.course_name}".strip()


class NtubCurriculumRecommendation(models.Model):
    """一筆課綱推薦：某個入學年度／專班／班級被建議修某門課。

    `match_status` 說明這筆推薦跟官方開課對得有多準：

    * `matched`：課名與班級代碼都相同，可以完整對應到某筆官方開課。
    * `course_name_only`：只有課名相同、班級代碼不同（跨班開課）。這是**疑義**，
      代表「同名課、但開課班別不同」，不能當成已確認的開放跨班選修。

    推薦只到班級層級：來源沒有學生的入學年度／專班類型，本系統也不從學號或
    班級代碼去猜，所以不會產生個人化的選課資格。
    """

    MATCH_STATUSES = [
        ("matched", "課名與班級皆相符"),
        ("course_name_only", "僅課名相符（班別待確認）"),
    ]

    admission_year = models.CharField("入學年度", max_length=20)
    track = models.CharField("專班類型", max_length=50, blank=True)
    program_code = models.CharField("學制代碼", max_length=20, blank=True)
    program_name = models.CharField("學制名稱", max_length=100, blank=True)
    department_code = models.CharField("系所代碼", max_length=20, blank=True)
    department_name = models.CharField("系所名稱", max_length=100, blank=True)
    grade = models.PositiveSmallIntegerField("年級", null=True, blank=True)
    semester = models.CharField("學期", max_length=20, blank=True)
    class_code = models.CharField("班級代碼", max_length=20)
    course_name = models.CharField("推薦課程名稱", max_length=100)

    match_status = models.CharField("對應狀態", max_length=30, choices=MATCH_STATUSES)
    matched_offering_class_code = models.CharField("對應開課班級代碼", max_length=20, blank=True)
    matched_course_code = models.CharField("對應課號", max_length=20, blank=True)

    offering = models.ForeignKey(NtubOfferingSource, on_delete=models.SET_NULL, null=True, blank=True,
                                 related_name="recommendations", verbose_name="對應官方開課")
    course = models.ForeignKey(Course, on_delete=models.SET_NULL, null=True, blank=True,
                               related_name="ntub_recommendations", verbose_name="對應課程")
    class_group = models.ForeignKey(AcademicClassGroup, on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name="ntub_recommendations", verbose_name="推薦班級")

    is_ambiguous = models.BooleanField("仍有疑義", default=False)
    ambiguity_message = models.TextField("疑義說明", blank=True)
    source_payload = models.JSONField("來源原始資料", default=dict, blank=True)
    created_at = models.DateTimeField("建立時間", auto_now_add=True)
    updated_at = models.DateTimeField("更新時間", auto_now=True)

    class Meta:
        ordering = ["admission_year", "class_code", "course_name"]
        verbose_name = "課綱推薦"
        verbose_name_plural = "課綱推薦"
        constraints = [
            models.UniqueConstraint(
                fields=["admission_year", "track", "program_code", "department_code",
                        "grade", "semester", "class_code", "course_name"],
                name="ntub_curriculum_recommendation_unique"),
        ]

    def __str__(self):
        return f"{self.admission_year} {self.class_code} {self.course_name}".strip()

    @property
    def is_confirmed(self):
        """只有完全對上的推薦才算已確認；`course_name_only` 一律仍是疑義。"""
        return self.match_status == "matched" and not self.is_ambiguous


class NtubRecommendationCandidate(models.Model):
    """推薦與候選開課之間的關係：一筆推薦可能對應多個候選官方開課。"""

    recommendation = models.ForeignKey(NtubCurriculumRecommendation, on_delete=models.CASCADE,
                                       related_name="candidates", verbose_name="課綱推薦")
    offering = models.ForeignKey(NtubOfferingSource, on_delete=models.SET_NULL, null=True, blank=True,
                                 related_name="recommendation_candidates", verbose_name="候選開課")
    offering_key = models.CharField("候選來源鍵", max_length=120)
    course_code = models.CharField("候選課號", max_length=20, blank=True)
    class_code = models.CharField("候選班級代碼", max_length=20, blank=True)
    class_name = models.CharField("候選班級名稱", max_length=100, blank=True)
    course_name = models.CharField("候選課程名稱", max_length=100, blank=True)

    class Meta:
        ordering = ["recommendation_id", "offering_key"]
        verbose_name = "推薦候選開課"
        verbose_name_plural = "推薦候選開課"
        constraints = [
            models.UniqueConstraint(fields=["recommendation", "offering_key"],
                                   name="ntub_recommendation_candidate_unique"),
        ]

    def __str__(self):
        return f"{self.recommendation_id} → {self.offering_key}".strip()


class NtubOfferingAmbiguity(models.Model):
    """來源裡列出的疑義：原樣保存，不做任何自動判斷。

    來源的三種疑義都收在這裡：

    * `recommendation_class_mismatch`：推薦的班別與官方開課班別不同（跨班選修待確認）。
    * `offering_without_recommendation`：有開課但沒有任何課綱推薦指向它。
    * `source_partial_keyword_match`：關鍵字只部分命中（例如「財務管理」命中
      「財務管理(二)(技優領航專班)」），兩者是不同課程，互不取代。
    """

    KIND_CHOICES = [
        ("recommendation_class_mismatch", "推薦班別與開課班別不同"),
        ("offering_without_recommendation", "開課沒有對應的課綱推薦"),
        ("source_partial_keyword_match", "關鍵字部分比對"),
    ]

    kind = models.CharField("疑義類型", max_length=50, choices=KIND_CHOICES)
    message = models.TextField("疑義說明")
    offering = models.ForeignKey(NtubOfferingSource, on_delete=models.SET_NULL, null=True, blank=True,
                                 related_name="ambiguities", verbose_name="相關開課")
    recommendation = models.ForeignKey(NtubCurriculumRecommendation, on_delete=models.SET_NULL,
                                       null=True, blank=True, related_name="ambiguities",
                                       verbose_name="相關推薦")
    source_payload = models.JSONField("來源原始資料", default=dict, blank=True)
    created_at = models.DateTimeField("建立時間", auto_now_add=True)

    class Meta:
        ordering = ["kind", "id"]
        verbose_name = "來源疑義"
        verbose_name_plural = "來源疑義"

    def __str__(self):
        return f"{self.kind} {self.message}"[:80]
