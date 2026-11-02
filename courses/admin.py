import json

from django import forms
from django.contrib import admin, messages
from django.contrib.admin.actions import delete_selected as django_delete_selected
from django.contrib.auth.admin import GroupAdmin as DjangoGroupAdmin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.models import Group, User
from django.contrib.sites.models import Site
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Count, Prefetch, Q
from django.http import Http404, HttpResponseRedirect
from django.http import HttpResponseNotAllowed, JsonResponse
from django.shortcuts import get_object_or_404
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils import timezone
from django.utils.html import format_html, format_html_join

from allauth.account.models import EmailAddress
from allauth.socialaccount.models import SocialAccount, SocialApp, SocialToken

from .maintainer import (can_manage_policy, can_manage_section, grant_maintainer_access,
                          MAINTAINER_GROUP_NAME, is_maintainer, is_superuser,
                          revoke_maintainer_access,
                          scoped_class_groups, scoped_courses, scoped_departments,
                          scoped_sections)
from .maintainer_assignment_forms import (BulkClassAssignmentForm,
                                          BulkClassAssignmentSelectForm)
from .credits import effective_entry_credit
from .ntub_official import (OfficialFetchError, OfficialFetchClient,
                            parse_timetable_html, sync_offering_syllabi,
                            sync_syllabus, sync_timetable_syllabi)
from .ntub_timetable_import import import_payload
from .ntub_schedule_grid import (
    KIND_OFFICIAL_ACTIVITY,
    KIND_OFFICIAL_COURSE,
    GridItem,
    base_periods_for,
    build_week_grid,
)
from .syllabus_docx import DocxSyllabusError, parse_docx_syllabus
from .syllabus_forms import (CourseTeachingSyllabusForm, SYLLABUS_FIELDS,
                             WEEK_FIELD_NAMES, WEEK_LABELS,
                             CourseSyllabusSubmissionForm,
                             SyllabusDocxUploadForm)
from .teacher_access import (
    SYLLABUS_TEACHER_DISPLAY_NAME,
    SYLLABUS_TEACHER_GROUP_NAME,
    has_teacher_syllabus_access,
)
from .models import (AcademicCatalog, AcademicClassGroup, AcademicDepartment,
                     AcademicProgramType, Course, CourseMaintainerScope, Enrollment,
                     CourseSyllabusSubmission, CourseTeachingSyllabus,
                     NtubCurriculumRecommendation, NtubOfferingAmbiguity,
                     NtubOfferingSource, NtubRecommendationCandidate,
                     NtubTimetableEntry, NtubTimetableSnapshot, OfficialCourseCredit,
                     OfficialTeachingSyllabus, Section,
                     SectionClassPolicy, StudentAcademicProfile, Teacher, Term, Waitlist)


def teacher_admin_index(request):
    """Route standalone syllabus teachers to their teaching dashboard."""
    user = request.user
    if (
        user.is_authenticated
        and user.is_active
        and user.is_staff
        and not is_superuser(user)
        and not is_maintainer(user)
        and user.groups.filter(name=SYLLABUS_TEACHER_GROUP_NAME).exists()
    ):
        return admin.site._registry[Course].teacher_syllabus_list_view(request)
    return admin.site.index(request)


class TeacherAdminForm(forms.ModelForm):
    class Meta:
        model = Teacher
        fields = "__all__"

    def clean_user(self):
        user = self.cleaned_data.get("user")
        if user is None:
            return user
        if self.instance.pk and user.pk == self.instance.user_id:
            return user
        if Teacher.objects.filter(user=user).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError("此登入帳號已綁定其他教師。")
        if (
            not user.is_active
            or not user.is_staff
            or user.is_superuser
            or not user.groups.filter(name=SYLLABUS_TEACHER_GROUP_NAME).exists()
        ):
            raise forms.ValidationError(
                "請先由 Superuser 建立啟用中的教師後台帳號並加入任課教師群組。"
            )
        return user


class CourseAdminForm(forms.ModelForm):
    def __init__(self, *args, request=None, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.instance.pk:
            self.fields["owning_department"].required = True
        if request is not None and not is_superuser(request.user):
            self.fields["owning_department"].queryset = scoped_departments(request.user)

    class Meta:
        model = Course
        fields = "__all__"

    def clean_prerequisites(self):
        # 只能在表單驗證：full_clean() 跑在 save_m2m() 之前，model 層讀不到表單送出的 M2M
        qs = self.cleaned_data["prerequisites"]
        if qs.count() > 2:
            raise forms.ValidationError("先修課程最多設 2 門。")
        return qs


class SectionAdminForm(forms.ModelForm):
    def __init__(self, *args, request=None, **kwargs):
        super().__init__(*args, **kwargs)
        if request is not None and not is_superuser(request.user):
            self.fields["course"].queryset = scoped_courses(request.user)

    class Meta:
        model = Section
        fields = "__all__"


class CourseTeachingSyllabusInline(admin.StackedInline):
    model = CourseTeachingSyllabus
    form = CourseTeachingSyllabusForm
    extra = 1
    max_num = 1
    can_delete = False
    verbose_name = "02 教學大綱管理"
    verbose_name_plural = "02 教學大綱管理"
    description = "儲存後預設為草稿；按「確認並發布課綱」後學生才能查看。"
    fieldsets = (
        (None, {"fields": ("teaching_target", "teaching_matter")}),
        ("18 週進度（展開編輯）", {
            "classes": ("collapse",),
            "fields": WEEK_FIELD_NAMES,
        }),
        (None, {"fields": ("assessment", "references")}),
    )

    def has_add_permission(self, request, obj=None):
        course_admin = admin.site._registry[Course]
        if obj is not None and obj.syllabus_submissions.filter(
            status=CourseSyllabusSubmission.Status.PENDING,
        ).exists():
            return False
        if obj is None:
            return course_admin.has_add_permission(request)
        return course_admin.has_change_permission(request, obj)

    def has_change_permission(self, request, obj=None):
        course_admin = admin.site._registry[Course]
        if obj is not None and obj.syllabus_submissions.filter(
            status=CourseSyllabusSubmission.Status.PENDING,
        ).exists():
            return False
        if obj is None:
            return course_admin.has_change_permission(request)
        return course_admin.has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        return False

class MaintainerScopedAdmin(admin.ModelAdmin):
    """課程維護員在後台只看得到自己負責系所的資料。

    這是伺服器端的第一道過濾：changelist、搜尋、autocomplete、ForeignKey
    下拉選單全部從這裡的 queryset 出去，所以維護員連「猜 id」都猜不到別系的
    資料。各個 ModelAdmin 只需實作 `scope_queryset`。
    """

    def scope_queryset(self, request, queryset):
        """回傳這個使用者看得到的資料子集（superuser 走原 queryset）。"""
        return queryset

    def get_queryset(self, request):
        queryset = super().get_queryset(request)
        if is_superuser(request.user):
            return queryset
        return self.scope_queryset(request, queryset)


class SuperuserOnlyAdmin(admin.ModelAdmin):
    """只有 superuser 能動的介面（選課、候補、學生學籍）。

    維護員群組本來就沒有這些模型權限，這裡再釘死一次：就算日後有人手動加權限，
    維護員也不會碰到學生的選課與學籍資料。
    """

    def has_view_permission(self, request, obj=None):
        return is_superuser(request.user)

    def has_add_permission(self, request):
        return is_superuser(request.user)

    def has_change_permission(self, request, obj=None):
        return is_superuser(request.user)

    def has_delete_permission(self, request, obj=None):
        return is_superuser(request.user)


class SyllabusTeacherUserCreationForm(UserCreationForm):
    groups = forms.ModelMultipleChoiceField(
        queryset=Group.objects.none(),
        required=False,
        label="群組",
    )

    class Meta(UserCreationForm.Meta):
        model = User
        fields = ("username", "email", "is_active", "groups")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["groups"].queryset = Group.objects.order_by("name")
        self.fields["groups"].label_from_instance = self._group_label

    @staticmethod
    def _group_label(group):
        return (
            SYLLABUS_TEACHER_DISPLAY_NAME
            if group.name == SYLLABUS_TEACHER_GROUP_NAME else group.name
        )

    def clean(self):
        cleaned_data = super().clean()
        groups = cleaned_data.get("groups", ())
        if (
            any(group.name == SYLLABUS_TEACHER_GROUP_NAME for group in groups)
            and not cleaned_data.get("is_active")
        ):
            self.add_error("is_active", "新建教師後台帳號必須保持啟用。")
        return cleaned_data


class SyllabusTeacherUserAdmin(DjangoUserAdmin):
    list_display = (
        "username", "email", "first_name", "last_name", "system_role",
        "is_active", "is_staff", "is_superuser",
    )
    add_form = SyllabusTeacherUserCreationForm
    add_fieldsets = (
        (None, {
            "classes": ("wide",),
            "fields": (
                "username", "email", "password1", "password2", "is_active",
                "groups",
            ),
        }),
    )

    def has_module_permission(self, request):
        return is_superuser(request.user)

    def has_view_permission(self, request, obj=None):
        return is_superuser(request.user)

    def has_add_permission(self, request):
        return is_superuser(request.user)

    def has_change_permission(self, request, obj=None):
        return is_superuser(request.user)

    def has_delete_permission(self, request, obj=None):
        return is_superuser(request.user)

    def get_queryset(self, request):
        return super().get_queryset(request).prefetch_related("groups")

    @admin.display(description="系統角色")
    def system_role(self, obj):
        if obj.is_superuser:
            return "超級管理員"
        if not obj.is_active or not obj.is_staff:
            return "一般使用者"
        group_names = {group.name for group in obj.groups.all()}
        if MAINTAINER_GROUP_NAME in group_names:
            return "課程維護員"
        if SYLLABUS_TEACHER_GROUP_NAME in group_names:
            return "任課教師"
        return "一般使用者"

    def formfield_for_manytomany(self, db_field, request, **kwargs):
        field = super().formfield_for_manytomany(db_field, request, **kwargs)
        if db_field.name == "groups" and field is not None:
            field.label_from_instance = SyllabusTeacherUserCreationForm._group_label
        return field

    def save_model(self, request, obj, form, change):
        selected_groups = form.cleaned_data.get("groups", ())
        selects_teacher_group = any(
            group.name == SYLLABUS_TEACHER_GROUP_NAME
            for group in selected_groups
        )
        previously_teacher = (
            change
            and obj.groups.filter(name=SYLLABUS_TEACHER_GROUP_NAME).exists()
        )
        if selects_teacher_group and not obj.is_superuser:
            obj.is_staff = True
        elif (
            previously_teacher
            and not obj.is_superuser
            and not selected_groups
            and not obj.user_permissions.exists()
        ):
            obj.is_staff = False
        super().save_model(request, obj, form, change)


class SyllabusTeacherGroupAdmin(DjangoGroupAdmin):
    list_display = ("display_name", "name", "member_count", "permission_count")

    def has_module_permission(self, request):
        return is_superuser(request.user)

    def has_view_permission(self, request, obj=None):
        return is_superuser(request.user)

    def has_add_permission(self, request):
        return is_superuser(request.user)

    def has_change_permission(self, request, obj=None):
        return is_superuser(request.user)

    def has_delete_permission(self, request, obj=None):
        if not is_superuser(request.user):
            return False
        return not (
            obj is not None and obj.name == SYLLABUS_TEACHER_GROUP_NAME
        )

    def get_readonly_fields(self, request, obj=None):
        if obj is not None and obj.name == SYLLABUS_TEACHER_GROUP_NAME:
            return ("name", "permissions")
        return super().get_readonly_fields(request, obj)

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        if obj.name == SYLLABUS_TEACHER_GROUP_NAME:
            obj.permissions.clear()

    @admin.display(description="群組名稱")
    def display_name(self, obj):
        return (
            SYLLABUS_TEACHER_DISPLAY_NAME
            if obj.name == SYLLABUS_TEACHER_GROUP_NAME else obj.name
        )

    @admin.display(description="成員數")
    def member_count(self, obj):
        return obj.user_set.count()

    @admin.display(description="模型權限數")
    def permission_count(self, obj):
        return obj.permissions.count()


for _auth_model in (User, Group):
    if _auth_model in admin.site._registry:
        admin.site.unregister(_auth_model)
admin.site.register(User, SyllabusTeacherUserAdmin)
admin.site.register(Group, SyllabusTeacherGroupAdmin)


@admin.register(Term)
class TermAdmin(admin.ModelAdmin):
    list_display = ["code", "is_current", "enroll_open", "enroll_close"]
    list_filter = ["is_current"]
    date_hierarchy = "enroll_open"
    fields = ["code", "is_current", "enroll_open", "enroll_close"]


@admin.register(Teacher)
class TeacherAdmin(SuperuserOnlyAdmin):
    form = TeacherAdminForm
    list_display = ["name", "email", "user", "course_count"]
    search_fields = ["name"]
    autocomplete_fields = ["user"]
    actions = ["delete_selected", "deactivate_and_unlink_teacher_accounts"]

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(
            _course_count=Count("course"),
        )

    @admin.display(description="授課課程數")
    def course_count(self, obj):
        return obj._course_count

    @staticmethod
    def _related_record_counts(teacher):
        courses = Course.objects.filter(teacher=teacher)
        sections = Section.objects.filter(course__teacher=teacher)
        return {
            "courses": courses.count(),
            "sections": sections.count(),
            "enrollments": Enrollment.objects.filter(
                section__course__teacher=teacher,
            ).count(),
            "waitlists": Waitlist.objects.filter(
                section__course__teacher=teacher,
            ).count(),
            "official_sources": NtubOfferingSource.objects.filter(
                teacher=teacher,
            ).count(),
            "fixed_schedule_entries": NtubTimetableEntry.objects.filter(
                teacher=teacher,
            ).count(),
            "linked_accounts": int(teacher.user_id is not None),
            "syllabus_submissions": CourseSyllabusSubmission.objects.filter(
                teacher=teacher,
            ).count(),
            "teaching_syllabi": CourseTeachingSyllabus.objects.filter(
                course__teacher=teacher,
            ).count(),
        }

    @staticmethod
    def _related_record_summary(teacher, counts):
        labels = (
            ("courses", "正式課程"),
            ("sections", "開課班別"),
            ("enrollments", "選課紀錄"),
            ("waitlists", "候補紀錄"),
            ("official_sources", "官方開課來源"),
            ("fixed_schedule_entries", "班級固定課表明細"),
            ("linked_accounts", "教師後台帳號"),
            ("syllabus_submissions", "課綱提交歷史"),
            ("teaching_syllabi", "人工教學大綱"),
        )
        details = "、".join(
            f"{label} {counts[key]} 筆"
            for key, label in labels if counts[key]
        )
        return (
            f"教師「{teacher.name}」仍有關聯帳號或資料，為保留帳號綁定及歷史紀錄，"
            f"禁止刪除：{details}。請改用停用教師帳號並解除綁定。"
        )

    def _raise_if_teacher_has_related_records(self, teacher):
        counts = self._related_record_counts(teacher)
        if any(counts.values()):
            raise PermissionDenied(
                self._related_record_summary(teacher, counts),
            )

    def get_deleted_objects(self, objs, request):
        teachers = list(objs)
        deleted_objects, model_count, perms_needed, protected = (
            super().get_deleted_objects(teachers, request)
        )
        for teacher in teachers:
            counts = self._related_record_counts(teacher)
            if not any(counts.values()):
                continue
            if counts["official_sources"]:
                # Read-only source rows are protected by the model's PROTECT
                # relation; their Admin correctly denies deleting the source.
                # They block deleting the Teacher but are not a missing grant.
                perms_needed.discard(NtubOfferingSource._meta.verbose_name)
            protected.append(
                self._related_record_summary(teacher, counts),
            )
        return deleted_objects, model_count, perms_needed, protected

    def delete_model(self, request, obj):
        if not self.has_delete_permission(request, obj):
            raise PermissionDenied("只有超級管理員可以刪除教師。")
        self._raise_if_teacher_has_related_records(obj)
        super().delete_model(request, obj)

    def delete_queryset(self, request, queryset):
        if not self.has_delete_permission(request):
            raise PermissionDenied("只有超級管理員可以刪除教師。")
        teachers = list(queryset)
        blocked = [
            self._related_record_summary(teacher, counts)
            for teacher in teachers
            if any(
                (counts := self._related_record_counts(teacher)).values()
            )
        ]
        if blocked:
            raise PermissionDenied("批次刪除已取消，未刪除任何教師。\n" + "\n".join(blocked))
        with transaction.atomic():
            for teacher in teachers:
                super().delete_model(request, teacher)

    @admin.action(description="停用教師後台帳號並解除 Teacher.user 綁定")
    def deactivate_and_unlink_teacher_accounts(self, request, queryset):
        if not is_superuser(request.user):
            raise PermissionDenied("只有超級管理員可以停用教師帳號並解除綁定。")
        teachers = list(queryset.select_related("user"))
        if any(
            teacher.user_id
            and (is_superuser(teacher.user) or is_maintainer(teacher.user))
            for teacher in teachers
        ):
            raise PermissionDenied(
                "選取項目包含超級管理員或課程維護員帳號；請先由管理者個別確認，"
                "避免中斷其他管理職務。",
            )
        changed = 0
        with transaction.atomic():
            for teacher in teachers:
                user = teacher.user
                if user is not None:
                    user.is_active = False
                    user.save(update_fields=["is_active"])
                    teacher.user = None
                    teacher.save(update_fields=["user"])
                    changed += 1
        self.message_user(
            request,
            f"已停用並解除 {changed} 個教師後台帳號綁定；課程及歷史資料均予保留。",
            messages.SUCCESS,
        )


@admin.register(Course)
class CourseAdmin(MaintainerScopedAdmin):
    form = CourseAdminForm
    inlines = [CourseTeachingSyllabusInline]
    change_form_template = "admin/courses/course_change_form.html"
    readonly_fields = ("syllabus_docx_import",)
    fieldsets = (
        ("01 課程基本資料", {
            "fields": ("term", "owning_department", "code", "name", "teacher",
                       "credits", "prerequisites"),
        }),
        ("舊版課綱文字（相容既有資料）", {
            "classes": ("collapse",),
            "fields": ("syllabus",),
        }),
        ("Word 課綱匯入", {"fields": ("syllabus_docx_import",)}),
    )
    list_display = ["code", "name", "owning_department", "term", "teacher", "credits"]
    list_filter = ["term", "teacher"]
    search_fields = ["code", "name"]

    def scope_queryset(self, request, queryset):
        return queryset.filter(pk__in=scoped_courses(request.user).values("pk"))

    def get_form(self, request, obj=None, **kwargs):
        form_class = super().get_form(request, obj, **kwargs)

        class RequestCourseForm(form_class):
            def __init__(self, *args, **form_kwargs):
                super().__init__(*args, request=request, **form_kwargs)

        return RequestCourseForm

    def get_exclude(self, request, obj=None):
        exclude = list(super().get_exclude(request, obj) or [])
        if "created_by" not in exclude:
            exclude.append("created_by")
        return exclude

    def save_model(self, request, obj, form, change):
        if not change:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)

    @admin.display(description="教學大綱 Word 匯入")
    def syllabus_docx_import(self, obj):
        if obj is None or obj.pk is None:
            return "請先儲存課程，再使用 DOCX 匯入預覽。"
        url = reverse("admin:courses_course_import_syllabus", args=[obj.pk])
        return format_html(
            '<a class="button" href="{}">選擇 DOCX 並預覽匯入</a> '
            '<span class="help">匯入內容需人工確認；不接受 PDF。</span>',
            url,
        )

    def get_urls(self):
        custom_urls = [
            path(
                "teacher-syllabi/stats/",
                self.admin_site.admin_view(self.teacher_dashboard_stats_view),
                name="courses_course_teacher_stats",
            ),
            path(
                "teacher-syllabi/section/<int:section_id>/roster/",
                self.admin_site.admin_view(self.teacher_section_roster_view),
                name="courses_course_teacher_section_roster",
            ),
            path(
                "teacher-syllabi/",
                self.admin_site.admin_view(self.teacher_syllabus_list_view),
                name="courses_course_teacher_syllabi",
            ),
            path(
                "<int:course_id>/teacher-syllabus/",
                self.admin_site.admin_view(self.teacher_syllabus_view),
                name="courses_course_teacher_syllabus",
            ),
            path(
                "syllabus-review/",
                self.admin_site.admin_view(self.syllabus_review_list_view),
                name="courses_course_syllabus_review",
            ),
            path(
                "syllabus-review/<int:submission_id>/",
                self.admin_site.admin_view(self.syllabus_review_view),
                name="courses_course_syllabus_review_detail",
            ),
            path(
                "<path:object_id>/syllabus/import/",
                self.admin_site.admin_view(self.import_syllabus_view),
                name="courses_course_import_syllabus",
            ),
        ]
        return custom_urls + super().get_urls()

    @staticmethod
    def _syllabus_form_context(form):
        return {
            "syllabus_form": form,
            "basic_fields": [form[name] for name in SYLLABUS_FIELDS[:2]],
            "weekly_fields": [form[name] for name in WEEK_FIELD_NAMES],
            "closing_fields": [form[name] for name in SYLLABUS_FIELDS[-2:]],
        }

    @staticmethod
    def _submission_initial(submission):
        return {
            "teaching_target": submission.teaching_target,
            "teaching_matter": submission.teaching_matter,
            **(submission.weekly_progress or {}),
            "assessment": submission.assessment,
            "references": submission.references,
        }

    @staticmethod
    def _published_initial(course):
        syllabus = CourseTeachingSyllabus.objects.filter(
            course=course, status=CourseTeachingSyllabus.Status.PUBLISHED,
        ).first()
        if syllabus is None:
            return {}
        return {
            "teaching_target": syllabus.teaching_target,
            "teaching_matter": syllabus.teaching_matter,
            **(syllabus.weekly_progress or {}),
            "assessment": syllabus.assessment,
            "references": syllabus.references,
        }

    def _teacher_profile(self, request):
        if not has_teacher_syllabus_access(request.user):
            raise PermissionDenied("需要任課教師（課綱提交）群組才能使用此頁面。")
        teacher = Teacher.objects.filter(user=request.user).first()
        if teacher is None:
            raise PermissionDenied("此後台帳號尚未綁定任課教師資料。")
        return teacher

    def _teacher_dashboard_profile(self, request):
        user = request.user
        if (
            not user.is_authenticated
            or not user.is_active
            or not user.is_staff
            or not user.groups.filter(name=SYLLABUS_TEACHER_GROUP_NAME).exists()
        ):
            raise PermissionDenied("只有已授權的任課教師帳號可以使用此頁面。")
        teacher = Teacher.objects.filter(user=user).first()
        if teacher is None:
            raise PermissionDenied("此後台帳號尚未綁定任課教師資料。")
        return teacher

    @staticmethod
    def _teacher_course_queryset(teacher):
        fixed_assignment = Q(
            fixed_timetable_entries__teacher=teacher,
            fixed_timetable_entries__is_active=True,
            fixed_timetable_entries__snapshot__source_kind=(
                NtubTimetableSnapshot.SourceKind.MAINTAINED
            ),
            fixed_timetable_entries__snapshot__status=(
                NtubTimetableSnapshot.Status.PUBLISHED
            ),
        )
        return Course.objects.filter(Q(teacher=teacher) | fixed_assignment).distinct()

    @staticmethod
    def _teacher_term(teacher, requested_term=""):
        current = Term.current()
        terms = Term.objects.filter(
            pk__in=CourseAdmin._teacher_course_queryset(teacher).values("term_id"),
        )
        if current is not None:
            terms = terms | Term.objects.filter(pk=current.pk)
        terms = terms.distinct()
        if requested_term:
            term = terms.filter(pk=requested_term).first()
            if term is None:
                raise Http404("找不到此教師的授課學期。")
            return term

        if current is not None:
            return current
        return terms.first()

    @staticmethod
    def _teacher_dashboard_sections(teacher, term):
        if term is None:
            return []
        return list(
            Section.objects.filter(
                course__teacher=teacher,
                course__term=term,
            ).select_related(
                "course", "course__term",
            ).annotate(
                dashboard_enrolled_count=Count("enrollments", distinct=True),
                dashboard_waiting_count=Count(
                    "waitlists",
                    filter=Q(waitlists__promoted_at__isnull=True),
                    distinct=True,
                ),
            ).order_by("course__code", "label")
        )

    @staticmethod
    def _teacher_section_stats(sections):
        serialized = []
        for section in sections:
            enrolled = section.dashboard_enrolled_count
            capacity = section.capacity
            serialized.append({
                "id": section.pk,
                "enrolled": enrolled,
                "waiting": section.dashboard_waiting_count,
                "capacity": capacity,
                "remaining": max(0, capacity - enrolled),
                "over_capacity": enrolled > capacity,
                "progress": min(100, round(enrolled * 100 / capacity))
                if capacity else 100,
            })
        return serialized

    @staticmethod
    def _teacher_syllabus_statuses(courses):
        course_ids = [course.pk for course in courses]
        latest_submissions = {}
        submissions = CourseSyllabusSubmission.objects.filter(
            course_id__in=course_ids,
        ).order_by("course_id", "-created_at", "-pk")
        for submission in submissions:
            latest_submissions.setdefault(submission.course_id, submission)

        published_ids = set(CourseTeachingSyllabus.objects.filter(
            course_id__in=course_ids,
            status=CourseTeachingSyllabus.Status.PUBLISHED,
        ).values_list("course_id", flat=True))

        for course in courses:
            latest = latest_submissions.get(course.pk)
            if latest is not None:
                course.syllabus_status = latest.get_status_display()
                course.latest_syllabus_submission = latest
            elif course.pk in published_ids:
                course.syllabus_status = CourseSyllabusSubmission.Status.PUBLISHED.label
                course.latest_syllabus_submission = None
            else:
                course.syllabus_status = "尚未提交"
                course.latest_syllabus_submission = None

    def _teacher_courses(self, request):
        teacher = self._teacher_profile(request)
        return self._teacher_course_queryset(teacher).select_related(
            "term", "teacher",
        ).prefetch_related(
            "syllabus_submissions",
            Prefetch(
                "teaching_syllabus",
                queryset=CourseTeachingSyllabus.objects.filter(
                    status=CourseTeachingSyllabus.Status.PUBLISHED,
                ),
                to_attr="published_syllabi",
            ),
        )

    def _teacher_status(self, course):
        latest = next(iter(course.syllabus_submissions.all()), None)
        if latest is not None:
            return latest.get_status_display(), latest
        if getattr(course, "published_syllabi", []):
            return CourseSyllabusSubmission.Status.PUBLISHED.label, None
        return "尚未提交", None

    def teacher_syllabus_list_view(self, request):
        if is_superuser(request.user) or is_maintainer(request.user):
            teacher = self._teacher_profile(request)
            polling_enabled = request.user.groups.filter(
                name=SYLLABUS_TEACHER_GROUP_NAME,
            ).exists()
        else:
            teacher = self._teacher_dashboard_profile(request)
            polling_enabled = True
        current = Term.current()
        terms = Term.objects.filter(
            pk__in=self._teacher_course_queryset(teacher).values("term_id"),
        )
        if current is not None:
            terms = terms | Term.objects.filter(pk=current.pk)
        terms = terms.distinct()
        term = self._teacher_term(teacher, request.GET.get("term", ""))
        courses = list(
            self._teacher_course_queryset(teacher).filter(
                term=term,
            ).select_related("term", "teacher").order_by("code")
        ) if term else []
        self._teacher_syllabus_statuses(courses)
        sections = self._teacher_dashboard_sections(teacher, term)
        sections_by_course = {}
        for section in sections:
            section.remaining = max(
                0, section.capacity - section.dashboard_enrolled_count,
            )
            section.over_capacity = (
                section.dashboard_enrolled_count > section.capacity
            )
            section.progress = min(
                100, round(
                    section.dashboard_enrolled_count * 100 / section.capacity,
                ),
            ) if section.capacity else 100
            sections_by_course.setdefault(section.course_id, []).append(section)
        for course in courses:
            course.section_list = sections_by_course.get(course.pk, [])
        fixed_entries = list(
            NtubTimetableEntry.objects.filter(
                teacher=teacher,
                snapshot__term=term,
                snapshot__status=NtubTimetableSnapshot.Status.PUBLISHED,
                is_active=True,
                entry_type="course",
            ).select_related(
                "course", "snapshot__class_group", "snapshot__term",
            ).order_by(
                "snapshot__class_group__code", "weekday", "start_period",
            ) if term else []
        )
        fixed_by_course = {}
        for entry in fixed_entries:
            fixed_by_course.setdefault(entry.course_id, []).append(entry)
        for course in courses:
            course.fixed_schedule_entries = fixed_by_course.get(course.pk, [])

        summary = {
            "course_count": len(courses),
            "section_count": len(sections),
            "enrolled_count": sum(
                section.dashboard_enrolled_count for section in sections
            ),
            "waiting_count": sum(
                section.dashboard_waiting_count for section in sections
            ),
        }
        return TemplateResponse(request, "admin/courses/teacher_dashboard.html", {
            **self.admin_site.each_context(request),
            "opts": self.model._meta,
            "title": "教師授課管理中心",
            "courses": courses,
            "terms": terms,
            "selected_term": term,
            "summary": summary,
            "stats_url": (
                reverse("admin:courses_course_teacher_stats")
                if polling_enabled else ""
            ),
            "initial_updated_at": timezone.localtime(),
        })

    def teacher_dashboard_stats_view(self, request):
        if request.method != "GET":
            return HttpResponseNotAllowed(["GET"])
        teacher = self._teacher_dashboard_profile(request)
        term = self._teacher_term(teacher, request.GET.get("term", ""))
        courses = list(
            self._teacher_course_queryset(teacher).filter(
                term=term,
            ).order_by("code")
        ) if term else []
        sections = self._teacher_dashboard_sections(teacher, term)
        return JsonResponse({
            "summary": {
                "course_count": len(courses),
                "section_count": len(sections),
                "enrolled_count": sum(
                    section.dashboard_enrolled_count for section in sections
                ),
                "waiting_count": sum(
                    section.dashboard_waiting_count for section in sections
                ),
            },
            "sections": self._teacher_section_stats(sections),
            "updated_at": timezone.localtime().isoformat(),
        }, headers={"Cache-Control": "no-store, private"})

    def teacher_section_roster_view(self, request, section_id):
        if request.method != "GET":
            return HttpResponseNotAllowed(["GET"])
        teacher = self._teacher_dashboard_profile(request)
        section = get_object_or_404(
            Section.objects.select_related("course", "course__term"),
            pk=section_id, course__teacher=teacher,
        )
        enrollments = list(
            Enrollment.objects.filter(section=section).select_related(
                "user", "user__academic_profile__class_group",
            ).order_by("created", "pk")
        )
        roster = []
        for enrollment in enrollments:
            profile = getattr(enrollment.user, "academic_profile", None)
            class_group = profile.class_group if profile is not None else None
            username = enrollment.user.get_username()
            if "@" in username:
                username = username.split("@", 1)[0]
            roster.append({
                "name": enrollment.user.first_name or username,
                "class_name": class_group.name_zh if class_group else "",
                "class_code": class_group.code if class_group else "",
                "created": enrollment.created,
                "status": "已選課",
            })
        return TemplateResponse(
            request, "admin/courses/teacher_section_roster.html", {
                **self.admin_site.each_context(request),
                "opts": self.model._meta,
                "title": f"{section.course.name}－{section.label} 班選課名單",
                "section": section,
                "roster": roster,
            },
        )

    def teacher_syllabus_view(self, request, course_id):
        teacher = self._teacher_profile(request)
        course = get_object_or_404(
            self._teacher_course_queryset(teacher).select_related(
                "term", "teacher",
            ),
            pk=course_id,
        )
        latest = course.syllabus_submissions.order_by(
            "-created_at", "-pk",
        ).first()
        pending = course.syllabus_submissions.filter(
            status=CourseSyllabusSubmission.Status.PENDING,
        ).first()
        context = {
            **self.admin_site.each_context(request),
            "opts": self.model._meta,
            "title": f"教學大綱：{course}",
            "course": course,
            "upload_form": SyllabusDocxUploadForm(),
            "unresolved_labels": [],
            "error_message": "",
            "submission": None,
            "is_read_only": pending is not None,
            "return_reason": "",
        }

        if pending is not None:
            context["submission"] = pending
            context["form_groups"] = self._syllabus_form_context(
                CourseSyllabusSubmissionForm(instance=pending),
            )
            context["status_label"] = pending.get_status_display()
        else:
            editable = (
                latest if latest and latest.status == CourseSyllabusSubmission.Status.DRAFT
                else None
            )
            if editable is not None:
                context["submission"] = editable
                previous = editable.supersedes
                context["return_reason"] = (
                    previous.return_reason if previous else editable.return_reason
                )
                initial = self._submission_initial(editable)
                context["status_label"] = editable.get_status_display()
            else:
                initial = self._published_initial(course)
                context["status_label"] = (
                    latest.get_status_display() if latest else "尚未提交"
                )
                if latest and latest.status == CourseSyllabusSubmission.Status.RETURNED:
                    context["return_reason"] = latest.return_reason
                    context["previous_submission"] = latest
                    initial = self._submission_initial(latest)
                elif latest and latest.status == CourseSyllabusSubmission.Status.PUBLISHED:
                    context["previous_submission"] = latest
            context["form_groups"] = self._syllabus_form_context(
                CourseSyllabusSubmissionForm(
                    instance=editable, initial=initial,
                ),
            )

        if request.method == "POST":
            action = request.POST.get("teacher_action")
            if pending is not None:
                raise PermissionDenied("教學大綱待審核期間不可再修改。")
            if action == "preview":
                upload_form = SyllabusDocxUploadForm(request.POST, request.FILES)
                context["upload_form"] = upload_form
                if upload_form.is_valid():
                    try:
                        initial = parse_docx_syllabus(
                            upload_form.cleaned_data["file"],
                        )
                    except DocxSyllabusError as exc:
                        upload_form.add_error("file", str(exc))
                    else:
                        labels = dict(zip(
                            SYLLABUS_FIELDS,
                            ("教學目標", "教學內容", *WEEK_LABELS,
                             "成績評量", "指定教科書及參考書籍"),
                        ))
                        unresolved = [
                            field for field in SYLLABUS_FIELDS
                            if not initial[field].strip()
                        ]
                        context.update(
                            form_groups=self._syllabus_form_context(
                                CourseSyllabusSubmissionForm(initial=initial),
                            ),
                            unresolved_labels=[
                                labels[field] for field in unresolved
                            ],
                        )
                return TemplateResponse(
                    request, "admin/courses/teacher_syllabus_form.html", context,
                )

            if action not in {"save_draft", "submit"}:
                context["error_message"] = "操作無效，請重新載入頁面。"
                return TemplateResponse(
                    request, "admin/courses/teacher_syllabus_form.html",
                    context, status=400,
                )

            submission = None
            submission_id = request.POST.get("submission_id", "").strip()
            if submission_id:
                submission = get_object_or_404(
                    course.syllabus_submissions.filter(teacher=teacher),
                    pk=submission_id,
                )
                if submission.status not in {
                    CourseSyllabusSubmission.Status.DRAFT,
                }:
                    raise PermissionDenied("此提交版本目前不可修改。")
            else:
                if latest and latest.status == CourseSyllabusSubmission.Status.DRAFT:
                    submission = latest
                else:
                    previous = latest if latest and latest.status in {
                        CourseSyllabusSubmission.Status.RETURNED,
                        CourseSyllabusSubmission.Status.PUBLISHED,
                    } else None
                    submission = CourseSyllabusSubmission(
                        course=course, teacher=teacher, supersedes=previous,
                    )

            form = CourseSyllabusSubmissionForm(
                request.POST, instance=submission,
            )
            context["form_groups"] = self._syllabus_form_context(form)
            if form.is_valid():
                with transaction.atomic():
                    submission = form.save(commit=False)
                    submission.course = course
                    submission.teacher = teacher
                    if action == "submit":
                        submission.status = CourseSyllabusSubmission.Status.PENDING
                        submission.submitted_at = timezone.now()
                        success_message = "教學大綱已提交給課程維護員審核。"
                    else:
                        submission.status = CourseSyllabusSubmission.Status.DRAFT
                        success_message = "教學大綱草稿已儲存。"
                    submission.save()
                self.message_user(request, success_message, messages.SUCCESS)
                return HttpResponseRedirect(reverse(
                    "admin:courses_course_teacher_syllabi",
                ))
            context["submission"] = submission
            context["status_label"] = submission.get_status_display()
            return TemplateResponse(
                request, "admin/courses/teacher_syllabus_form.html",
                context, status=400,
            )

        return TemplateResponse(
            request, "admin/courses/teacher_syllabus_form.html", context,
        )

    def _reviewable_submissions(self, request):
        if not (
            is_superuser(request.user)
            or (
                is_maintainer(request.user)
                and request.user.has_perm("courses.change_course")
            )
        ):
            raise PermissionDenied("只有具備課程維護權限的助教可以審核。")
        return CourseSyllabusSubmission.objects.filter(
            course__in=scoped_courses(request.user),
            status=CourseSyllabusSubmission.Status.PENDING,
        ).select_related(
            "course", "course__term", "course__teacher", "teacher",
        )

    def syllabus_review_list_view(self, request):
        submissions = self._reviewable_submissions(request).order_by(
            "submitted_at", "pk",
        )
        return TemplateResponse(request, "admin/courses/syllabus_review_list.html", {
            **self.admin_site.each_context(request),
            "opts": self.model._meta,
            "title": "待審核教學大綱",
            "submissions": submissions,
        })

    def syllabus_review_view(self, request, submission_id):
        queryset = self._reviewable_submissions(request)
        submission = get_object_or_404(queryset, pk=submission_id)
        course_admin = admin.site._registry[Course]
        if not course_admin.has_change_permission(request, submission.course):
            raise PermissionDenied("你沒有修改此課程教學大綱的權限。")

        error_message = ""
        if request.method == "POST":
            with transaction.atomic():
                submission = get_object_or_404(
                    self._reviewable_submissions(request).select_for_update(),
                    pk=submission_id,
                )
                form = CourseSyllabusSubmissionForm(
                    request.POST, instance=submission,
                )
                action = request.POST.get("review_action")
                if action not in {"publish", "return"}:
                    error_message = "操作無效，請重新載入頁面。"
                elif form.is_valid():
                    return_reason = request.POST.get("return_reason", "").strip()
                    if action == "return" and not return_reason:
                        error_message = "請填寫退回原因。"
                    else:
                        submission = form.save(commit=False)
                        submission.reviewed_by = request.user
                        submission.reviewed_at = timezone.now()
                        if action == "return":
                            submission.status = CourseSyllabusSubmission.Status.RETURNED
                            submission.return_reason = return_reason
                            submission.save()
                            self.message_user(
                                request, "教學大綱已退回教師修改。",
                                messages.SUCCESS,
                            )
                            return HttpResponseRedirect(reverse(
                                "admin:courses_course_syllabus_review",
                            ))

                        syllabus, _ = CourseTeachingSyllabus.objects.get_or_create(
                            course=submission.course,
                        )
                        syllabus.teaching_target = submission.teaching_target
                        syllabus.teaching_matter = submission.teaching_matter
                        syllabus.weekly_progress = submission.weekly_progress
                        syllabus.assessment = submission.assessment
                        syllabus.references = submission.references
                        syllabus.status = CourseTeachingSyllabus.Status.PUBLISHED
                        syllabus.updated_by = request.user
                        syllabus.save()
                        submission.status = CourseSyllabusSubmission.Status.PUBLISHED
                        submission.return_reason = ""
                        submission.save()
                        self.message_user(
                            request, "教學大綱已確認並發布。",
                            messages.SUCCESS,
                        )
                        return HttpResponseRedirect(reverse(
                            "admin:courses_course_syllabus_review",
                        ))
                context_form = form
        else:
            context_form = CourseSyllabusSubmissionForm(instance=submission)

        return TemplateResponse(request, "admin/courses/syllabus_review_form.html", {
            **self.admin_site.each_context(request),
            **self._syllabus_form_context(context_form),
            "opts": self.model._meta,
            "title": f"審核教學大綱：{submission.course}",
            "submission": submission,
            "course": submission.course,
            "error_message": error_message,
            "return_reason": request.POST.get(
                "return_reason", submission.return_reason,
            ),
        }, status=400 if request.method == "POST" and error_message else 200)

    def import_syllabus_view(self, request, object_id):
        course = self.get_object(request, object_id)
        if course is None:
            raise Http404
        if not self.has_change_permission(request, course):
            raise PermissionDenied

        existing = getattr(course, "teaching_syllabus", None)
        context = {
            **self.admin_site.each_context(request),
            "opts": self.model._meta,
            "title": f"匯入教學大綱：{course}",
            "course": course,
            "phase": "upload",
            "upload_form": SyllabusDocxUploadForm(),
            "syllabus_form": None,
            "unresolved": [],
            "error_message": "",
        }
        if existing and existing.status == CourseTeachingSyllabus.Status.PUBLISHED:
            context.update(
                phase="blocked",
                error_message=(
                    "此課程已有已發布的教學大綱。為避免覆蓋學生正在查看的內容，"
                    "請勿重新匯入；請使用課程編輯頁明確修改並重新發布。"
                ),
                existing_syllabus=existing,
            )
            return TemplateResponse(
                request, "admin/courses/course_syllabus_import.html", context,
                status=409 if request.method == "POST" else 200,
            )

        action = request.POST.get("import_action") if request.method == "POST" else ""
        if action == "preview":
            upload_form = SyllabusDocxUploadForm(request.POST, request.FILES)
            context["upload_form"] = upload_form
            if upload_form.is_valid():
                try:
                    initial = parse_docx_syllabus(upload_form.cleaned_data["file"])
                except DocxSyllabusError as exc:
                    upload_form.add_error("file", str(exc))
                else:
                    unresolved_fields = [
                        field_name for field_name in SYLLABUS_FIELDS
                        if not initial[field_name].strip()
                    ]
                    field_labels = dict(zip(
                        SYLLABUS_FIELDS,
                        ("教學目標", "教學內容", *WEEK_LABELS,
                         "成績評量", "指定教科書及參考書籍"),
                    ))
                    context.update(
                        phase="preview",
                        syllabus_form=CourseTeachingSyllabusForm(initial=initial),
                        unresolved_fields=unresolved_fields,
                        unresolved_labels=[
                            field_labels[field_name]
                            for field_name in unresolved_fields
                        ],
                    )
            return TemplateResponse(
                request, "admin/courses/course_syllabus_import.html", context,
            )

        if action == "confirm":
            existing = getattr(course, "teaching_syllabus", None)
            if existing and existing.status == CourseTeachingSyllabus.Status.PUBLISHED:
                context.update(
                    phase="blocked",
                    error_message="課綱已發布，匯入確認遭拒；原有已發布內容未變更。",
                    existing_syllabus=existing,
                )
                return TemplateResponse(
                    request, "admin/courses/course_syllabus_import.html",
                    context, status=409,
                )
            syllabus_form = CourseTeachingSyllabusForm(
                request.POST, instance=existing,
            )
            context.update(phase="preview", syllabus_form=syllabus_form)
            if syllabus_form.is_valid():
                syllabus = syllabus_form.save(commit=False)
                syllabus.course = course
                syllabus.status = CourseTeachingSyllabus.Status.DRAFT
                syllabus.updated_by = request.user
                syllabus.save()
                self.message_user(
                    request,
                    "DOCX 內容已儲存為草稿；請檢查欄位並按「確認並發布課綱」公開。",
                    messages.SUCCESS,
                )
                return HttpResponseRedirect(
                    reverse("admin:courses_course_change", args=[course.pk])
                )
            return TemplateResponse(
                request, "admin/courses/course_syllabus_import.html", context,
                status=400,
            )

        if request.method == "POST":
            context["error_message"] = "匯入步驟無效，請重新選擇 DOCX 檔案。"
        return TemplateResponse(
            request, "admin/courses/course_syllabus_import.html", context,
        )

    def save_formset(self, request, form, formset, change):
        if formset.model is not CourseTeachingSyllabus:
            return super().save_formset(request, form, formset, change)
        instances = formset.save(commit=False)
        published_syllabus_exists = formset.queryset.filter(
            status=CourseTeachingSyllabus.Status.PUBLISHED,
        ).exists()
        if (published_syllabus_exists and formset.has_changed()
                and "_publish_syllabus" not in request.POST):
            self.message_user(
                request,
                "已發布課綱未被修改。若要套用目前編輯內容，請使用「確認並發布課綱」。",
                messages.WARNING,
            )
            return
        for instance in instances:
            instance.updated_by = request.user
            instance.status = (
                CourseTeachingSyllabus.Status.PUBLISHED
                if "_publish_syllabus" in request.POST
                else CourseTeachingSyllabus.Status.DRAFT
            )
            instance.save()
        formset.save_m2m()

    def save_related(self, request, form, formsets, change):
        syllabus_formset_changed = any(
            formset.model is CourseTeachingSyllabus and formset.has_changed()
            for formset in formsets
        )
        super().save_related(request, form, formsets, change)
        syllabus = getattr(form.instance, "teaching_syllabus", None)
        if "_publish_syllabus" in request.POST:
            if syllabus is None:
                self.message_user(
                    request, "尚未建立教學大綱，無法發布。",
                    messages.ERROR,
                )
                return
            syllabus.status = CourseTeachingSyllabus.Status.PUBLISHED
            syllabus.updated_by = request.user
            syllabus.save(update_fields=("status", "updated_by", "updated_at"))
            self.message_user(request, "教學大綱已確認並發布。", messages.SUCCESS)
        elif syllabus is not None and syllabus_formset_changed:
            if syllabus.status == CourseTeachingSyllabus.Status.DRAFT:
                syllabus.updated_by = request.user
                syllabus.save(update_fields=("updated_by", "updated_at"))
                self.message_user(
                    request,
                    "教學大綱已儲存為草稿，學生目前看不到此版本。",
                    messages.INFO,
                )

    def _can_delete_object(self, request, obj):
        if is_superuser(request.user):
            return True
        return (
            obj.created_by_id == request.user.pk
            and obj.owning_department_id is not None
            and scoped_departments(request.user).filter(
                pk=obj.owning_department_id).exists())

    def has_delete_permission(self, request, obj=None):
        if not (is_superuser(request.user)
                or request.user.has_perm("courses.delete_course")):
            return False
        if obj is None:
            return True
        return is_superuser(request.user) or self._can_delete_object(request, obj)

    @staticmethod
    def _check_delete_dependencies(obj):
        sections = obj.sections.all()
        if sections.filter(enrollments__isnull=False).exists():
            raise PermissionDenied("課程仍有加選紀錄，不能刪除。")
        if sections.filter(waitlists__isnull=False).exists():
            raise PermissionDenied("課程仍有候補紀錄，不能刪除。")
        if obj.ntub_offering_sources.exists():
            raise PermissionDenied("課程仍有官方開課來源，不能刪除。")
        if obj.sections.filter(class_policies__isnull=False).exists():
            raise PermissionDenied("課程仍有班別修課政策，不能刪除。")

    def _check_delete_sections(self, request, obj):
        for section in obj.sections.all():
            if not SectionAdmin._can_delete_object(
                    admin.site._registry[Section], request, section):
                raise PermissionDenied(
                    f"課程包含不能由目前帳號刪除的班別：{section}。")

    @admin.action(
        permissions=["delete"],
        description="刪除所選課程",
    )
    def delete_selected(self, request, queryset):
        for obj in queryset.select_related("owning_department"):
            self._validate_delete(request, obj)
        return django_delete_selected(self, request, queryset)

    def _validate_delete(self, request, obj):
        if not self.has_delete_permission(request, obj):
            raise PermissionDenied("你沒有刪除此課程的權限。")
        if not is_superuser(request.user) and not self._can_delete_object(request, obj):
            raise PermissionDenied(
                "維護員只能刪除自己建立且屬於授權系所的課程；舊資料不得推測建立者。")
        self._check_delete_dependencies(obj)
        self._check_delete_sections(request, obj)

    def delete_model(self, request, obj):
        self._validate_delete(request, obj)
        super().delete_model(request, obj)

    def delete_queryset(self, request, queryset):
        with transaction.atomic():
            for obj in queryset.select_related("owning_department"):
                self._validate_delete(request, obj)
            for obj in queryset.select_related("owning_department"):
                super().delete_model(request, obj)


@admin.register(Section)
class SectionAdmin(MaintainerScopedAdmin):
    form = SectionAdminForm
    list_display = ["label", "course", "day", "start_period", "end_period", "room", "capacity"]
    list_filter = ["course__term", "day"]
    search_fields = ["course__code", "course__name"]

    def scope_queryset(self, request, queryset):
        return queryset.filter(pk__in=scoped_sections(request.user).values("pk"))

    def get_form(self, request, obj=None, **kwargs):
        form_class = super().get_form(request, obj, **kwargs)

        class RequestSectionForm(form_class):
            def __init__(self, *args, **form_kwargs):
                super().__init__(*args, request=request, **form_kwargs)

        return RequestSectionForm

    def get_exclude(self, request, obj=None):
        exclude = list(super().get_exclude(request, obj) or [])
        if "created_by" not in exclude:
            exclude.append("created_by")
        return exclude

    def save_model(self, request, obj, form, change):
        if not change:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)

    def _can_delete_object(self, request, obj):
        if is_superuser(request.user):
            return True
        return (
            obj.created_by_id == request.user.pk
            and obj.course.owning_department_id is not None
            and scoped_departments(request.user).filter(
                pk=obj.course.owning_department_id).exists())

    def has_delete_permission(self, request, obj=None):
        if not (is_superuser(request.user)
                or request.user.has_perm("courses.delete_section")):
            return False
        if obj is None:
            return True
        if is_superuser(request.user):
            return True
        return self._can_delete_object(request, obj)

    @staticmethod
    def _check_delete_dependencies(obj):
        if obj.enrollments.exists():
            raise PermissionDenied("班別仍有加選紀錄，不能刪除。")
        if obj.waitlists.exists():
            raise PermissionDenied("班別仍有候補紀錄，不能刪除。")
        if obj.ntub_offering_sources.exists():
            raise PermissionDenied("班別仍有官方開課來源，不能刪除。")

    @admin.action(
        permissions=["delete"],
        description="刪除所選班別",
    )
    def delete_selected(self, request, queryset):
        for obj in queryset.select_related("course"):
            self._validate_delete(request, obj)
        return django_delete_selected(self, request, queryset)

    def _validate_delete(self, request, obj):
        if not self.has_delete_permission(request, obj):
            raise PermissionDenied("你沒有刪除此班別的權限。")
        if not is_superuser(request.user) and not self._can_delete_object(request, obj):
            raise PermissionDenied(
                "維護員只能刪除自己建立且屬於授權系所的班別；舊資料不得推測建立者。")
        self._check_delete_dependencies(obj)

    def delete_model(self, request, obj):
        self._validate_delete(request, obj)
        super().delete_model(request, obj)

    def delete_queryset(self, request, queryset):
        with transaction.atomic():
            for obj in queryset.select_related("course"):
                self._validate_delete(request, obj)
            for obj in queryset.select_related("course"):
                super().delete_model(request, obj)


@admin.register(Enrollment)
class EnrollmentAdmin(SuperuserOnlyAdmin):
    list_display = ["user", "section", "created"]
    list_filter = ["section__course__term"]
    list_select_related = ["user", "section", "section__course"]


@admin.register(Waitlist)
class WaitlistAdmin(SuperuserOnlyAdmin):
    list_display = ["user", "section", "queued_at", "promoted_at"]
    list_filter = ["section__course__term"]
    list_select_related = ["user", "section", "section__course"]


@admin.register(AcademicCatalog)
class AcademicCatalogAdmin(admin.ModelAdmin):
    list_display = ["version", "source_key", "status", "source_file_name",
                    "class_group_count", "selectable_class_group_count", "imported_at"]
    list_filter = ["source_key", "status"]
    search_fields = ["version", "source_file_name", "file_sha256"]
    date_hierarchy = "imported_at"
    readonly_fields = ["source_file_name", "file_sha256", "source_row_count",
                       "program_count", "department_count", "class_group_count",
                       "selectable_class_group_count"]


@admin.register(AcademicProgramType)
class AcademicProgramTypeAdmin(admin.ModelAdmin):
    list_display = ["code", "name_zh", "catalog", "is_registration_selectable", "is_active", "sort_order"]
    list_filter = ["catalog", "is_registration_selectable", "is_active"]
    search_fields = ["code", "name_zh"]
    autocomplete_fields = ["catalog"]


@admin.register(AcademicDepartment)
class AcademicDepartmentAdmin(MaintainerScopedAdmin):
    list_display = ["code", "name_zh", "program_type", "catalog", "is_registration_selectable", "is_active"]
    list_filter = ["catalog", "program_type", "is_registration_selectable", "is_active"]
    search_fields = ["code", "name_zh"]
    autocomplete_fields = ["catalog", "program_type"]

    def scope_queryset(self, request, queryset):
        return queryset.filter(pk__in=scoped_departments(request.user).values("pk"))


@admin.register(StudentAcademicProfile)
class StudentAcademicProfileAdmin(SuperuserOnlyAdmin):
    list_display = ["user", "class_group", "department", "program_type", "grade_no", "completed_at"]
    list_filter = ["class_group__department__program_type", "class_group__grade_no"]
    search_fields = ["user__email", "user__username", "class_group__code", "class_group__name_zh"]
    list_select_related = ["class_group__department__program_type", "user"]
    autocomplete_fields = ["user"]

    @admin.display(description="系所", ordering="class_group__department__name_zh")
    def department(self, obj):
        return obj.class_group.department

    @admin.display(description="學制", ordering="class_group__department__program_type__sort_order")
    def program_type(self, obj):
        return obj.class_group.department.program_type

    @admin.display(description="年級", ordering="class_group__grade_no")
    def grade_no(self, obj):
        return obj.class_group.grade_no


@admin.register(AcademicClassGroup)
class AcademicClassGroupAdmin(MaintainerScopedAdmin):
    list_display = ["code", "name_zh", "department", "grade_no", "section_code",
                    "group_type", "is_registration_selectable", "is_active"]
    list_filter = ["catalog", "department__program_type", "group_type",
                   "is_registration_selectable", "is_active", "grade_no"]
    search_fields = ["code", "name_zh", "department__code", "department__name_zh"]
    autocomplete_fields = ["catalog", "department"]

    def scope_queryset(self, request, queryset):
        return queryset.filter(pk__in=scoped_class_groups(request.user).values("pk"))


class ReadOnlyTimetableAdmin(admin.ModelAdmin):
    """官方班級課表只能由匯入指令寫入，管理介面一律唯讀。"""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class ScopedReadOnlyAdmin(ReadOnlyTimetableAdmin, MaintainerScopedAdmin):
    """官方來源資料：唯讀，而且只看得到自己系所的部分。"""

    def scope_queryset(self, request, queryset):
        return queryset


@admin.register(NtubTimetableSnapshot)
class NtubTimetableSnapshotAdmin(ScopedReadOnlyAdmin):
    list_display = [
        "term", "class_group", "class_name", "source_label",
        "publication_state", "managed_by", "entry_count",
    ]
    list_filter = ["term", "class_group__department__program_type"]
    search_fields = ["class_group__code", "class_group__name_zh", "class_name"]
    list_select_related = ["class_group", "term"]
    date_hierarchy = "retrieved_at"
    change_list_template = "admin/courses/ntubtimetablesnapshot/change_list.html"
    actions = ["publish_selected", "withdraw_selected", "delete_selected"]

    def scope_queryset(self, request, queryset):
        return queryset.filter(class_group__in=scoped_class_groups(request.user))

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        if is_superuser(request.user):
            return True
        if not (
            request.user.is_authenticated
            and request.user.has_perm("courses.delete_ntubtimetablesnapshot")
        ):
            return False
        if obj is None:
            return True
        return (
            obj.source_kind == NtubTimetableSnapshot.SourceKind.MAINTAINED
            and obj.status == NtubTimetableSnapshot.Status.DRAFT
            and scoped_class_groups(request.user).filter(
                pk=obj.class_group_id,
            ).exists()
        )

    def delete_model(self, request, obj):
        self._validate_snapshot_delete(request, obj)
        with transaction.atomic():
            self._archive_imported_fallback(obj)
            super().delete_model(request, obj)

    def delete_queryset(self, request, queryset):
        snapshot_ids = list(queryset.values_list("pk", flat=True))
        with transaction.atomic():
            snapshots = list(
                self.get_queryset(request).select_for_update().filter(
                    pk__in=snapshot_ids,
                ).select_related("class_group", "term")
            )
            if len(snapshots) != len(snapshot_ids):
                raise PermissionDenied("刪除清單包含無權管理的固定課表。")
            for snapshot in snapshots:
                self._validate_snapshot_delete(request, snapshot)
            for snapshot in snapshots:
                self._archive_imported_fallback(snapshot)
            super().delete_queryset(
                request, self.model.objects.filter(pk__in=snapshot_ids),
            )

    def _validate_snapshot_delete(self, request, snapshot):
        if not self.has_delete_permission(request, snapshot):
            raise PermissionDenied(
                "只有 Superuser 可清理歷史或已發布版本；維護員僅能刪除"
                "自己授權系所中的草稿。"
            )
        if not is_superuser(request.user) and not scoped_class_groups(
            request.user,
        ).filter(pk=snapshot.class_group_id).exists():
            raise PermissionDenied("你沒有管理此班級固定課表的權限。")

    @staticmethod
    def _archive_imported_fallback(snapshot):
        if (
            snapshot.source_kind == NtubTimetableSnapshot.SourceKind.MAINTAINED
            and snapshot.status == NtubTimetableSnapshot.Status.PUBLISHED
        ):
            NtubTimetableSnapshot.objects.filter(
                class_group_id=snapshot.class_group_id,
                term_id=snapshot.term_id,
                source_kind=NtubTimetableSnapshot.SourceKind.IMPORTED,
                status=NtubTimetableSnapshot.Status.PUBLISHED,
            ).update(status=NtubTimetableSnapshot.Status.ARCHIVED)

    @admin.action(description="發布選定的草稿課表")
    def publish_selected(self, request, queryset):
        self._ensure_fixed_schedule_access(request)
        snapshot_ids = list(queryset.values_list("pk", flat=True))
        if not snapshot_ids:
            self.message_user(request, "請先選擇草稿課表。", messages.WARNING)
            return
        with transaction.atomic():
            snapshots = list(
                self.get_queryset(request).select_for_update().filter(
                    pk__in=snapshot_ids,
                ).select_related("class_group", "term")
            )
            if len(snapshots) != len(snapshot_ids):
                raise PermissionDenied("發布清單包含無權管理的固定課表。")
            for snapshot in snapshots:
                if (
                    snapshot.source_kind != NtubTimetableSnapshot.SourceKind.MAINTAINED
                    or snapshot.status != NtubTimetableSnapshot.Status.DRAFT
                ):
                    self.message_user(
                        request, "批次發布只接受人工維護的草稿版本。",
                        messages.ERROR,
                    )
                    return
                if not is_superuser(request.user) and not scoped_class_groups(
                    request.user,
                ).filter(pk=snapshot.class_group_id).exists():
                    raise PermissionDenied("選取的課表包含未授權系所。")
                error = self._fixed_schedule_publish_error(snapshot)
                if error:
                    self.message_user(
                        request,
                        f"{snapshot.class_group.code}／{snapshot.term.code}：{error}",
                        messages.ERROR,
                    )
                    return
            for snapshot in snapshots:
                self._publish_fixed_schedule(snapshot, request.user)
        self.message_user(
            request, f"已發布 {len(snapshots)} 個班級固定課表版本。",
            messages.SUCCESS,
        )

    @admin.action(description="撤回選定的已發布課表")
    def withdraw_selected(self, request, queryset):
        self._ensure_fixed_schedule_access(request)
        snapshot_ids = list(queryset.values_list("pk", flat=True))
        if not snapshot_ids:
            self.message_user(request, "請先選擇已發布的人工課表。", messages.WARNING)
            return
        with transaction.atomic():
            snapshots = list(
                self.get_queryset(request).select_for_update().filter(
                    pk__in=snapshot_ids,
                ).select_related("class_group", "term")
            )
            if len(snapshots) != len(snapshot_ids):
                raise PermissionDenied("撤回清單包含無權管理的固定課表。")
            if any(
                snapshot.source_kind != NtubTimetableSnapshot.SourceKind.MAINTAINED
                or snapshot.status != NtubTimetableSnapshot.Status.PUBLISHED
                for snapshot in snapshots
            ):
                self.message_user(
                    request, "批次撤回只接受已發布的人工維護版本。",
                    messages.ERROR,
                )
                return
            for snapshot in snapshots:
                self._withdraw_fixed_schedule(snapshot)
        self.message_user(
            request,
            f"已撤回 {len(snapshots)} 個版本；學生課表不會自動回退到官方匯入資料。",
            messages.SUCCESS,
        )

    @admin.action(permissions=["delete"], description="依確認頁面清理選定的課表版本")
    def delete_selected(self, request, queryset):
        for snapshot in queryset.select_related("class_group", "term"):
            self._validate_snapshot_delete(request, snapshot)
        return django_delete_selected(self, request, queryset)

    def get_list_display(self, request):
        def operations(snapshot):
            return self.snapshot_operations(request, snapshot)

        operations.short_description = "操作"
        return [*self.list_display, operations]

    def snapshot_operations(self, request, snapshot):
        detail_url = reverse(
            "admin:courses_ntubtimetablesnapshot_fixed_schedule_detail",
            args=[snapshot.class_group_id, snapshot.term_id],
        )
        source_url = reverse(
            "admin:courses_ntubtimetablesnapshot_source_detail",
            args=[snapshot.pk],
        )
        delete_url = reverse(
            "admin:courses_ntubtimetablesnapshot_delete",
            args=[snapshot.pk],
        )
        controls = [format_html('<a href="{}">查看課表</a>', detail_url)]

        def post_button(url, label):
            return format_html(
                '<button class="fixed-schedule-row-action" type="submit" '
                'form="fixed-schedule-row-action" formaction="{}">{}</button>',
                url, label,
            )

        if snapshot.source_kind == NtubTimetableSnapshot.SourceKind.IMPORTED:
            controls = [format_html('<a href="{}">查看來源</a>', source_url)]
            controls.append(post_button(
                reverse(
                    "admin:courses_ntubtimetablesnapshot_source_takeover",
                    args=[snapshot.pk],
                ),
                "接管編輯",
            ))
            if is_superuser(request.user):
                controls.append(format_html(
                    '<a href="{}">確認影響後清理</a>', delete_url,
                ))
        elif snapshot.status == NtubTimetableSnapshot.Status.DRAFT:
            controls.append(post_button(
                reverse(
                    "admin:courses_ntubtimetablesnapshot_fixed_schedule_publish",
                    args=[snapshot.class_group_id, snapshot.term_id],
                ),
                "發布",
            ))
            if self.has_delete_permission(request, snapshot):
                controls.append(format_html(
                    '<a href="{}">刪除草稿</a>', delete_url,
                ))
        elif snapshot.status == NtubTimetableSnapshot.Status.PUBLISHED:
            controls.append(post_button(
                reverse(
                    "admin:courses_ntubtimetablesnapshot_fixed_schedule_takeover",
                    args=[snapshot.class_group_id, snapshot.term_id],
                ),
                "建立修改草稿",
            ))
            controls.append(post_button(
                reverse(
                    "admin:courses_ntubtimetablesnapshot_fixed_schedule_withdraw",
                    args=[snapshot.pk],
                ),
                "撤回發布",
            ))
            if is_superuser(request.user):
                controls.append(format_html(
                    '<a href="{}">安全刪除</a>', delete_url,
                ))
        elif is_superuser(request.user):
            controls.append(format_html(
                '<a href="{}">確認影響後清理</a>', delete_url,
            ))
        return format_html_join(
            " · ", "{}", ((control,) for control in controls),
        )

    @admin.display(description="版本來源")
    def source_label(self, obj):
        return obj.get_source_kind_display()

    @admin.display(description="發布狀態")
    def publication_state(self, obj):
        if obj.status == NtubTimetableSnapshot.Status.ARCHIVED:
            return "已撤回／封存"
        if obj.source_kind == NtubTimetableSnapshot.SourceKind.IMPORTED:
            return "可供人工版本缺席時顯示" if obj.status == (
                NtubTimetableSnapshot.Status.PUBLISHED
            ) else obj.get_status_display()
        return obj.get_status_display()

    delete_confirmation_template = (
        "admin/courses/ntubtimetablesnapshot/delete_confirmation.html"
    )

    def delete_view(self, request, object_id, extra_context=None):
        snapshot = self.get_object(request, object_id)
        context = dict(extra_context or {})
        if snapshot is not None:
            course_ids = snapshot.entries.exclude(
                course_id=None,
            ).values_list("course_id", flat=True)
            section_ids = Section.objects.filter(
                course_id__in=course_ids,
            ).values_list("pk", flat=True)
            context.update({
                "snapshot_entry_count": snapshot.entries.count(),
                "snapshot_syllabus_count": snapshot.teaching_syllabi.count(),
                "snapshot_course_count": snapshot.entries.exclude(
                    course_id=None,
                ).values("course_id").distinct().count(),
                "snapshot_teacher_count": snapshot.entries.exclude(
                    teacher_id=None,
                ).values("teacher_id").distinct().count(),
                "snapshot_section_count": Section.objects.filter(
                    pk__in=section_ids,
                ).count(),
                "snapshot_enrollment_count": Enrollment.objects.filter(
                    section_id__in=section_ids,
                ).count(),
                "snapshot_waitlist_count": Waitlist.objects.filter(
                    section_id__in=section_ids,
                ).count(),
                "snapshot_archives_imported": (
                    snapshot.source_kind
                    == NtubTimetableSnapshot.SourceKind.MAINTAINED
                    and snapshot.status == NtubTimetableSnapshot.Status.PUBLISHED
                ),
            })
        return super().delete_view(request, object_id, extra_context=context)

    def get_deleted_objects(self, objs, request):
        snapshots = list(objs)
        deleted_objects, model_count, perms_needed, protected = super().get_deleted_objects(
            snapshots, request)
        if snapshots and all(
            self.has_delete_permission(request, snapshot)
            for snapshot in snapshots
        ):
            # Entries are cascade-owned by a snapshot; this does not grant their
            # standalone Admin delete permission.
            perms_needed.discard(NtubTimetableEntry._meta.verbose_name)
            if is_superuser(request.user):
                perms_needed.discard(OfficialTeachingSyllabus._meta.verbose_name)
        syllabus_count = sum(
            snapshot.teaching_syllabi.count() for snapshot in snapshots
        )
        if syllabus_count:
            deleted_objects.append(f"官方教學大綱（{syllabus_count} 份）")
        return deleted_objects, model_count, perms_needed, protected

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path(
                "fixed-schedule/",
                self.admin_site.admin_view(self.fixed_schedule_list_view),
                name="courses_ntubtimetablesnapshot_fixed_schedule",
            ),
            path(
                "fixed-schedule/<int:class_group_id>/<int:term_id>/",
                self.admin_site.admin_view(self.fixed_schedule_detail_view),
                name="courses_ntubtimetablesnapshot_fixed_schedule_detail",
            ),
            path(
                "fixed-schedule/snapshot/<int:snapshot_id>/",
                self.admin_site.admin_view(self.fixed_schedule_snapshot_view),
                name="courses_ntubtimetablesnapshot_source_detail",
            ),
            path(
                "fixed-schedule/snapshot/<int:snapshot_id>/takeover/",
                self.admin_site.admin_view(self.fixed_schedule_source_takeover_view),
                name="courses_ntubtimetablesnapshot_source_takeover",
            ),
            path(
                "fixed-schedule/<int:class_group_id>/<int:term_id>/takeover/",
                self.admin_site.admin_view(self.fixed_schedule_takeover_view),
                name="courses_ntubtimetablesnapshot_fixed_schedule_takeover",
            ),
            path(
                "fixed-schedule/<int:class_group_id>/<int:term_id>/publish/",
                self.admin_site.admin_view(self.fixed_schedule_publish_view),
                name="courses_ntubtimetablesnapshot_fixed_schedule_publish",
            ),
            path(
                "fixed-schedule/snapshot/<int:snapshot_id>/withdraw/",
                self.admin_site.admin_view(self.fixed_schedule_withdraw_view),
                name="courses_ntubtimetablesnapshot_fixed_schedule_withdraw",
            ),
            path(
                "fixed-schedule/<int:class_group_id>/<int:term_id>/entry/add/",
                self.admin_site.admin_view(self.fixed_schedule_entry_edit_view),
                name="courses_ntubtimetablesnapshot_fixed_schedule_entry_add",
            ),
            path(
                "fixed-schedule/<int:class_group_id>/<int:term_id>/entry/<int:entry_id>/change/",
                self.admin_site.admin_view(self.fixed_schedule_entry_edit_view),
                name="courses_ntubtimetablesnapshot_fixed_schedule_entry_change",
            ),
            path(
                "fixed-schedule/<int:class_group_id>/<int:term_id>/entry/<int:entry_id>/delete/",
                self.admin_site.admin_view(self.fixed_schedule_entry_delete_view),
                name="courses_ntubtimetablesnapshot_fixed_schedule_entry_delete",
            ),
            path("sync-official/", self.admin_site.admin_view(self.sync_official),
                 name="courses_ntubtimetablesnapshot_sync_official"),
        ]
        return custom + urls

    @staticmethod
    def _ensure_fixed_schedule_access(request):
        user = request.user
        if not (
            user.is_authenticated and user.is_active and user.is_staff
            and (is_superuser(user) or is_maintainer(user))
        ):
            raise PermissionDenied("只有已授權的課程維護員可以管理班級固定課表。")
        if not is_superuser(user) and not scoped_departments(user).exists():
            raise PermissionDenied("目前帳號沒有可管理的授權系所。")

    @staticmethod
    def _fixed_schedule_class_group(user, class_group_id):
        return get_object_or_404(
            scoped_class_groups(user).select_related(
                "department", "department__program_type",
            ),
            pk=class_group_id,
        )

    @staticmethod
    def _fixed_schedule_term(term_id):
        return get_object_or_404(Term, pk=term_id)

    def _fixed_schedule_draft(self, class_group, term):
        return NtubTimetableSnapshot.objects.filter(
            class_group=class_group, term=term,
            source_kind=NtubTimetableSnapshot.SourceKind.MAINTAINED,
            status=NtubTimetableSnapshot.Status.DRAFT,
        ).first()

    def _fixed_schedule_published(self, class_group, term):
        return NtubTimetableSnapshot.objects.filter(
            class_group=class_group, term=term,
            source_kind=NtubTimetableSnapshot.SourceKind.MAINTAINED,
            status=NtubTimetableSnapshot.Status.PUBLISHED,
        ).order_by("-published_at", "-pk").first()

    def _fixed_schedule_imported(self, class_group, term):
        return NtubTimetableSnapshot.objects.filter(
            class_group=class_group, term=term,
            source_kind=NtubTimetableSnapshot.SourceKind.IMPORTED,
            status=NtubTimetableSnapshot.Status.PUBLISHED,
        ).first()

    def fixed_schedule_list_view(self, request):
        self._ensure_fixed_schedule_access(request)
        initial = {}
        current_term = Term.current()
        if current_term is not None:
            initial["term"] = current_term.pk
        form = FixedScheduleSelectionForm(
            request.GET or None, user=request.user, initial=initial,
        )
        if request.method == "GET" and request.GET and form.is_valid():
            return HttpResponseRedirect(reverse(
                "admin:courses_ntubtimetablesnapshot_fixed_schedule_detail",
                args=[
                    form.cleaned_data["class_group"].pk,
                    form.cleaned_data["term"].pk,
                ],
            ))
        return TemplateResponse(
            request, "admin/courses/fixed_schedule_list.html", {
                **self.admin_site.each_context(request),
                "opts": self.model._meta,
                "title": "班級固定課表",
                "form": form,
            },
        )

    def fixed_schedule_detail_view(self, request, class_group_id, term_id):
        self._ensure_fixed_schedule_access(request)
        class_group = self._fixed_schedule_class_group(request.user, class_group_id)
        term = self._fixed_schedule_term(term_id)
        draft = self._fixed_schedule_draft(class_group, term)
        published = self._fixed_schedule_published(class_group, term)
        imported = self._fixed_schedule_imported(class_group, term)
        current = draft or published or imported
        entries = list(
            current.entries.select_related("teacher", "course").order_by(
                "weekday", "start_period", "id",
            ) if current else NtubTimetableEntry.objects.none()
        )
        course_admin = admin.site._registry[Course]
        visible_courses = {
            course.code: course
            for course in course_admin.get_queryset(request).filter(term=term)
        }
        for entry in entries:
            managed_course = entry.course or visible_courses.get(entry.course_code)
            entry.managed_course_url = (
                reverse("admin:courses_course_change", args=[managed_course.pk])
                if managed_course is not None
                and course_admin.has_change_permission(request, managed_course)
                else ""
            )
        weekly_grid = build_week_grid(
            [
                GridItem(
                    kind=(
                        KIND_OFFICIAL_COURSE if entry.entry_type == "course"
                        else KIND_OFFICIAL_ACTIVITY
                    ),
                    day=entry.weekday,
                    start_period=entry.start_period,
                    end_period=entry.end_period,
                    title=entry.course_name,
                    subtitle=(
                        entry.teacher.name if entry.teacher_id
                        else entry.teacher_name
                    ),
                    meta=entry.classroom,
                )
                for entry in entries if entry.is_active
            ],
            base_periods=base_periods_for(
                class_group.department.program_type.code,
            ),
        )
        return TemplateResponse(
            request, "admin/courses/fixed_schedule_detail.html", {
                **self.admin_site.each_context(request),
                "opts": self.model._meta,
                "title": f"{class_group.name_zh or class_group.code} 固定課表",
                "class_group": class_group,
                "term": term,
                "draft": draft,
                "published": published,
                "imported": imported,
                "current": current,
                "entries": entries,
                "weekly_grid": weekly_grid,
                "can_edit": draft is not None,
            },
        )

    def fixed_schedule_snapshot_view(self, request, snapshot_id):
        self._ensure_fixed_schedule_access(request)
        snapshot = get_object_or_404(
            self.get_queryset(request).select_related(
                "class_group__department__program_type", "term", "managed_by",
            ).prefetch_related("entries__teacher", "entries__course"),
            pk=snapshot_id,
        )
        return TemplateResponse(
            request,
            "admin/courses/fixed_schedule_snapshot_source.html",
            {
                **self.admin_site.each_context(request),
                "opts": self.model._meta,
                "title": f"{snapshot.class_group.code} 課表版本來源",
                "snapshot": snapshot,
                "entries": snapshot.entries.all(),
            },
        )

    def _create_fixed_schedule_draft(
        self, request, class_group, term, source_snapshot=None,
    ):
        existing_draft = self._fixed_schedule_draft(class_group, term)
        if existing_draft is not None:
            return existing_draft
        source = source_snapshot or (
            self._fixed_schedule_published(class_group, term)
            or self._fixed_schedule_imported(class_group, term)
        )
        snapshot = NtubTimetableSnapshot.objects.create(
            class_group=class_group,
            term=term,
            class_name=class_group.name_zh,
            source_kind=NtubTimetableSnapshot.SourceKind.MAINTAINED,
            status=NtubTimetableSnapshot.Status.DRAFT,
            managed_by=request.user,
            supersedes=source,
        )
        if source is not None:
            for entry in source.entries.all().order_by("id"):
                course = entry.course
                if course is None and entry.entry_type == "course" and entry.course_code:
                    course = Course.objects.filter(
                        term=term, code=entry.course_code,
                    ).first()
                credits = entry.credits
                if credits is None and entry.entry_type == "course":
                    verified_credit = OfficialCourseCredit.objects.filter(
                        term=term, class_group=class_group,
                        course_code=entry.course_code,
                    ).first()
                    if verified_credit is not None:
                        credits = verified_credit.credits
                    else:
                        official_syllabus = OfficialTeachingSyllabus.objects.filter(
                            academic_year=term.code.split("-", 1)[0],
                            semester=term.code.split("-", 1)[1],
                            class_code=class_group.code,
                            official_serial=entry.official_serial,
                            course_code=entry.course_code,
                            status="success",
                        ).first()
                        if (
                            official_syllabus is not None
                            and official_syllabus.credits is not None
                            and official_syllabus.credits
                            == official_syllabus.credits.to_integral_value()
                        ):
                            credits = int(official_syllabus.credits)
                NtubTimetableEntry.objects.create(
                    snapshot=snapshot,
                    course=course,
                    entry_type=entry.entry_type,
                    course_code=entry.course_code,
                    official_serial=entry.official_serial,
                    course_name=entry.course_name,
                    teacher=entry.teacher or (course.teacher if course else None),
                    teacher_name=entry.teacher_name,
                    credits=credits,
                    classroom=entry.classroom,
                    weekday=entry.weekday,
                    start_period=entry.start_period,
                    end_period=entry.end_period,
                    periods=list(entry.periods),
                    is_active=entry.is_active,
                )
        return snapshot

    def fixed_schedule_source_takeover_view(self, request, snapshot_id):
        self._ensure_fixed_schedule_access(request)
        if request.method != "POST":
            raise PermissionDenied("請以 POST 接管歷史匯入課表。")
        source = get_object_or_404(
            self.get_queryset(request).select_related("class_group", "term"),
            pk=snapshot_id,
            source_kind=NtubTimetableSnapshot.SourceKind.IMPORTED,
        )
        with transaction.atomic():
            draft = self._create_fixed_schedule_draft(
                request, source.class_group, source.term,
                source_snapshot=source,
            )
        self.message_user(
            request,
            "已將所選歷史匯入版本複製到人工草稿；來源與學生目前課表均未被修改。",
            messages.SUCCESS,
        )
        return HttpResponseRedirect(reverse(
            "admin:courses_ntubtimetablesnapshot_fixed_schedule_detail",
            args=[draft.class_group_id, draft.term_id],
        ))

    def fixed_schedule_takeover_view(self, request, class_group_id, term_id):
        self._ensure_fixed_schedule_access(request)
        if request.method != "POST":
            raise PermissionDenied("請以 POST 建立固定課表修訂版。")
        class_group = self._fixed_schedule_class_group(request.user, class_group_id)
        term = self._fixed_schedule_term(term_id)
        with transaction.atomic():
            snapshot = self._create_fixed_schedule_draft(
                request, class_group, term,
            )
        self.message_user(
            request,
            "已建立草稿版本；官方匯入資料仍保留，學生目前仍看到已發布版本。",
            messages.SUCCESS,
        )
        return HttpResponseRedirect(reverse(
            "admin:courses_ntubtimetablesnapshot_fixed_schedule_detail",
            args=[class_group.pk, term.pk],
        ))

    def fixed_schedule_publish_view(self, request, class_group_id, term_id):
        self._ensure_fixed_schedule_access(request)
        if request.method != "POST":
            raise PermissionDenied("請以 POST 發布固定課表。")
        class_group = self._fixed_schedule_class_group(request.user, class_group_id)
        term = self._fixed_schedule_term(term_id)
        with transaction.atomic():
            snapshot = get_object_or_404(
                NtubTimetableSnapshot.objects.select_for_update(),
                class_group=class_group, term=term,
                source_kind=NtubTimetableSnapshot.SourceKind.MAINTAINED,
                status=NtubTimetableSnapshot.Status.DRAFT,
            )
            error = self._fixed_schedule_publish_error(snapshot)
            if error:
                self.message_user(
                    request, error,
                    messages.ERROR,
                )
                return HttpResponseRedirect(reverse(
                    "admin:courses_ntubtimetablesnapshot_fixed_schedule_detail",
                    args=[class_group.pk, term.pk],
                ))
            self._publish_fixed_schedule(snapshot, request.user)
        self.message_user(request, "班級固定課表已發布，學生將看到此版本。", messages.SUCCESS)
        return HttpResponseRedirect(reverse(
            "admin:courses_ntubtimetablesnapshot_fixed_schedule_detail",
            args=[class_group.pk, term.pk],
        ))

    @staticmethod
    def _fixed_schedule_publish_error(snapshot):
        entries = list(snapshot.entries.filter(is_active=True).select_related(
            "course", "teacher",
        ))
        courses = [entry for entry in entries if entry.entry_type == "course"]
        if not courses:
            return "空白課表或只有活動的課表不能發布；至少需要一筆有效課程安排。"
        for entry in entries:
            if (
                not 1 <= entry.weekday <= 7
                or not 1 <= entry.start_period <= entry.end_period <= 18
            ):
                return f"明細「{entry.course_name or entry.course_code}」的星期或節次無效。"
            if entry.entry_type == "course":
                if not entry.course_id or not entry.teacher_id:
                    return (
                        f"課程「{entry.course_name or entry.course_code}」尚未連結課程資料"
                        "或指定授課教師。"
                    )
                if entry.course.term_id != snapshot.term_id:
                    return f"課程「{entry.course_name or entry.course_code}」的學期不符。"
            elif entry.entry_type == "activity" and not entry.course_name.strip():
                return "活動明細缺少名稱，請先完成資料。"
        return ""

    @staticmethod
    def _publish_fixed_schedule(snapshot, user):
        NtubTimetableSnapshot.objects.filter(
            class_group_id=snapshot.class_group_id,
            term_id=snapshot.term_id,
            source_kind=NtubTimetableSnapshot.SourceKind.MAINTAINED,
            status=NtubTimetableSnapshot.Status.PUBLISHED,
        ).exclude(pk=snapshot.pk).update(
            status=NtubTimetableSnapshot.Status.ARCHIVED,
        )
        snapshot.status = NtubTimetableSnapshot.Status.PUBLISHED
        snapshot.published_at = timezone.now()
        snapshot.managed_by = user
        snapshot.save(
            update_fields=["status", "published_at", "managed_by", "updated_at"],
        )

    @staticmethod
    def _withdraw_fixed_schedule(snapshot):
        NtubTimetableSnapshotAdmin._archive_imported_fallback(snapshot)
        snapshot.status = NtubTimetableSnapshot.Status.ARCHIVED
        snapshot.save(update_fields=["status", "updated_at"])

    def fixed_schedule_withdraw_view(self, request, snapshot_id):
        self._ensure_fixed_schedule_access(request)
        if request.method != "POST":
            raise PermissionDenied("請以 POST 撤回固定課表。")
        with transaction.atomic():
            snapshot = get_object_or_404(
                self.get_queryset(request).select_for_update(),
                pk=snapshot_id,
                source_kind=NtubTimetableSnapshot.SourceKind.MAINTAINED,
                status=NtubTimetableSnapshot.Status.PUBLISHED,
            )
            self._withdraw_fixed_schedule(snapshot)
        self.message_user(
            request,
            "已撤回發布。學生不會自動回退到官方匯入版本；再次顯示課表需發布新版本。",
            messages.SUCCESS,
        )
        return HttpResponseRedirect(reverse(
            "admin:courses_ntubtimetablesnapshot_fixed_schedule_detail",
            args=[snapshot.class_group_id, snapshot.term_id],
        ))

    def _fixed_schedule_entry_form_response(
        self, request, class_group, term, snapshot, form, entry=None,
    ):
        return TemplateResponse(
            request, "admin/courses/fixed_schedule_entry_form.html", {
                **self.admin_site.each_context(request),
                "opts": self.model._meta,
                "title": "編輯固定課表明細" if entry else "新增固定課程",
                "class_group": class_group,
                "term": term,
                "snapshot": snapshot,
                "entry": entry,
                "form": form,
            },
            status=400 if request.method == "POST" and not form.is_valid() else 200,
        )

    def fixed_schedule_entry_edit_view(
        self, request, class_group_id, term_id, entry_id=None,
    ):
        self._ensure_fixed_schedule_access(request)
        class_group = self._fixed_schedule_class_group(request.user, class_group_id)
        term = self._fixed_schedule_term(term_id)
        snapshot = get_object_or_404(
            NtubTimetableSnapshot.objects,
            class_group=class_group, term=term,
            source_kind=NtubTimetableSnapshot.SourceKind.MAINTAINED,
            status=NtubTimetableSnapshot.Status.DRAFT,
        )
        entry = None
        if entry_id is not None:
            entry = get_object_or_404(
                snapshot.entries.select_related("teacher", "course"),
                pk=entry_id,
            )
        form = FixedScheduleEntryEditForm(
            request.POST or None, instance=entry, snapshot=snapshot,
        )
        if request.method == "POST" and form.is_valid():
            with transaction.atomic():
                item = form.save(commit=False)
                if item.entry_type == "course":
                    existing_course = Course.objects.filter(
                        term=term, code=item.course_code,
                    ).first()
                    if existing_course is None:
                        item.course = Course.objects.create(
                            term=term,
                            owning_department=class_group.department,
                            created_by=request.user,
                            code=item.course_code,
                            name=item.course_name,
                            teacher=item.teacher,
                            credits=item.credits if item.credits is not None else 3,
                        )
                    else:
                        item.course = existing_course
                else:
                    item.course = None
                item.save()
            self.message_user(request, "固定課表明細已儲存於草稿。", messages.SUCCESS)
            return HttpResponseRedirect(reverse(
                "admin:courses_ntubtimetablesnapshot_fixed_schedule_detail",
                args=[class_group.pk, term.pk],
            ))
        return self._fixed_schedule_entry_form_response(
            request, class_group, term, snapshot, form, entry,
        )

    def fixed_schedule_entry_delete_view(
        self, request, class_group_id, term_id, entry_id,
    ):
        self._ensure_fixed_schedule_access(request)
        if request.method != "POST":
            raise PermissionDenied("請以 POST 移除草稿明細。")
        class_group = self._fixed_schedule_class_group(request.user, class_group_id)
        term = self._fixed_schedule_term(term_id)
        snapshot = get_object_or_404(
            NtubTimetableSnapshot.objects,
            class_group=class_group, term=term,
            source_kind=NtubTimetableSnapshot.SourceKind.MAINTAINED,
            status=NtubTimetableSnapshot.Status.DRAFT,
        )
        entry = get_object_or_404(snapshot.entries, pk=entry_id)
        entry.delete()
        self.message_user(request, "草稿明細已移除；歷史及已發布版本不受影響。", messages.SUCCESS)
        return HttpResponseRedirect(reverse(
            "admin:courses_ntubtimetablesnapshot_fixed_schedule_detail",
            args=[class_group.pk, term.pk],
        ))

    def sync_official(self, request):
        form = OfficialTimetableSyncForm(
            request.POST or None, user=request.user)
        preview = None
        syllabus_records = []
        error = None
        if request.method == "POST" and form.is_valid():
            action = request.POST.get("action")
            identifiers = form.identifiers()
            try:
                if action == "preview":
                    html = OfficialFetchClient().fetch_timetable(identifiers)
                    preview = parse_timetable_html(
                        html, identifiers["academic_year"], identifiers["semester"],
                        identifiers["class_code"], identifiers["class_name"])
                    request.session["ntub_sync_preview"] = preview
                elif action == "syllabus":
                    records = sync_timetable_syllabi(
                        form.cleaned_data["class_group"],
                        Term.objects.get(code=(
                            f"{identifiers['academic_year']}-{identifiers['semester']}")),
                    )
                    syllabus_records = records
                    success_count = sum(r.status == "success" for r in records)
                    self.message_user(
                        request,
                        f"教學大綱同步完成：成功 {success_count} 筆，"
                        f"其餘 {len(records) - success_count} 筆保留狀態。",
                        messages.SUCCESS if success_count == len(records)
                        else messages.WARNING,
                    )
                elif action == "import":
                    preview = request.session.pop("ntub_sync_preview", None)
                    if not preview:
                        raise OfficialFetchError("請先預覽官方查詢結果，再確認匯入。")
                    summary = import_payload(preview)
                    self.message_user(
                        request,
                        f"匯入完成：新增 {summary.entries_created} 筆、"
                        f"更新 {summary.entries_updated} 筆課表明細。",
                        messages.SUCCESS,
                    )
                    return HttpResponseRedirect(
                        reverse("admin:courses_ntubtimetablesnapshot_changelist"))
            except OfficialFetchError as exc:
                error = str(exc)
            except Exception as exc:
                error = f"匯入失敗，未寫入資料：{exc}"
        context = {
            **self.admin_site.each_context(request),
            "title": "歷史官方課表資料匯入／補抓",
            "form": form,
            "preview": preview,
            "syllabus_records": syllabus_records,
            "error": error,
            "opts": self.model._meta,
            "class_options": [
                {
                    "value": group.pk,
                    "label": f"{group.name_zh}（{group.code}）",
                    "program_type_id": group.department.program_type_id,
                    "program_type_label": str(group.department.program_type),
                    "department_id": group.department_id,
                    "department_label": str(group.department),
                    "grade_no": group.grade_no,
                }
                for group in form.fields["class_group"].queryset
            ],
        }
        return TemplateResponse(
            request, "admin/courses/ntubtimetablesnapshot/sync_official.html", context)

    def scope_queryset(self, request, queryset):
        return queryset.filter(class_group__in=scoped_class_groups(request.user))

    @admin.display(description="節數")
    def entry_count(self, obj):
        return obj.entries.count()


class OfficialTimetableSyncForm(forms.Form):
    academic_year = forms.CharField(label="學年度", max_length=20)
    semester = forms.ChoiceField(label="學期", choices=[("1", "第 1 學期"), ("2", "第 2 學期")])
    program_type = forms.ModelChoiceField(label="學制", queryset=AcademicProgramType.objects.none())
    department = forms.ModelChoiceField(label="系所", queryset=AcademicDepartment.objects.none())
    grade_no = forms.ChoiceField(label="年級", choices=(), required=False)
    class_group = forms.ModelChoiceField(label="班級", queryset=AcademicClassGroup.objects.none())

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        groups = scoped_class_groups(user).select_related("department__program_type")
        self.fields["class_group"].queryset = groups
        self.fields["department"].queryset = scoped_departments(user).select_related(
            "program_type")
        self.fields["program_type"].queryset = AcademicProgramType.objects.filter(
            departments__in=self.fields["department"].queryset).distinct()
        grades = set(groups.values_list("grade_no", flat=True))
        self.fields["grade_no"].choices = [
            (str(grade), f"{grade} 年級")
            for grade in sorted((grade for grade in grades if grade is not None),
                                key=int)
        ]
        if None in grades:
            self.fields["grade_no"].choices.append(("__none__", "未設定年級"))

    def clean_academic_year(self):
        value = self.cleaned_data["academic_year"].strip()
        if not value.isdigit():
            raise forms.ValidationError("學年度必須是數字。")
        return value

    def clean_class_group(self):
        group = self.cleaned_data["class_group"]
        if not scoped_class_groups(self.user).filter(pk=group.pk).exists():
            raise forms.ValidationError("你沒有操作此班級的權限。")
        return group

    def clean(self):
        cleaned = super().clean()
        group = cleaned.get("class_group")
        if not group:
            return cleaned
        if cleaned.get("program_type") and group.department.program_type_id != cleaned[
                "program_type"].pk:
            self.add_error("class_group", "班級不屬於所選學制。")
        if cleaned.get("department") and group.department_id != cleaned["department"].pk:
            self.add_error("class_group", "班級不屬於所選系所。")
        selected_grade = cleaned.get("grade_no")
        if selected_grade == "__none__" and group.grade_no is not None:
            self.add_error("class_group", "班級不屬於未設定年級。")
        elif selected_grade and selected_grade != "__none__" and str(
                group.grade_no) != selected_grade:
            self.add_error("class_group", "班級不屬於所選年級。")
        return cleaned

    def identifiers(self):
        group = self.cleaned_data["class_group"]
        return {
            "academic_year": self.cleaned_data["academic_year"],
            "semester": self.cleaned_data["semester"],
            "class_code": group.code,
            "class_name": group.name_zh,
            "program_type_id": group.department.program_type_id,
            "department_id": group.department_id,
            "grade_no": group.grade_no,
            "program_code": group.department.program_type.code,
            "department_code": group.department.code,
        }


class NtubTimetableEntryForm(forms.ModelForm):
    class Meta:
        model = NtubTimetableEntry
        fields = "__all__"


class FixedScheduleSelectionForm(forms.Form):
    term = forms.ModelChoiceField(label="學年度／學期", queryset=Term.objects.none())
    class_group = forms.ModelChoiceField(label="班級", queryset=AcademicClassGroup.objects.none())

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["term"].queryset = Term.objects.all()
        self.fields["class_group"].queryset = scoped_class_groups(
            user,
        ).select_related("department")


class FixedScheduleEntryEditForm(forms.ModelForm):
    class Meta:
        model = NtubTimetableEntry
        fields = (
            "entry_type", "course_code", "course_name", "teacher",
            "teacher_name", "credits", "weekly_hours", "course_category",
            "weekday", "start_period", "end_period", "classroom", "is_active",
        )

    def __init__(self, *args, snapshot, **kwargs):
        super().__init__(*args, **kwargs)
        self.snapshot = snapshot
        self.fields["teacher"].queryset = Teacher.objects.order_by("name")
        self.fields["teacher"].required = False

    def clean(self):
        cleaned = super().clean()
        weekday = cleaned.get("weekday")
        start_period = cleaned.get("start_period")
        end_period = cleaned.get("end_period")
        if weekday is not None and not 1 <= weekday <= 7:
            self.add_error("weekday", "星期必須介於 1 到 7。")
        if start_period is not None and not 1 <= start_period <= 18:
            self.add_error("start_period", "起始節次必須介於 1 到 18。")
        if end_period is not None and not 1 <= end_period <= 18:
            self.add_error("end_period", "結束節次必須介於 1 到 18。")
        if (
            start_period is not None and end_period is not None
            and end_period < start_period
        ):
            self.add_error("end_period", "結束節次不可早於起始節次。")
        if cleaned.get("entry_type") == "course":
            for field in ("course_code", "course_name", "teacher"):
                if not cleaned.get(field):
                    self.add_error(field, "課程項目必須填寫課號、課名並指定授課教師。")
        elif cleaned.get("entry_type") == "activity" and not cleaned.get("course_name"):
            self.add_error("course_name", "活動項目必須填寫活動名稱。")
        return cleaned

    def save(self, commit=True):
        entry = super().save(commit=False)
        entry.snapshot = self.snapshot
        entry.periods = list(range(entry.start_period, entry.end_period + 1))
        if entry.teacher_id:
            entry.teacher_name = entry.teacher.name
        if commit:
            entry.save()
        return entry


@admin.register(NtubTimetableEntry)
class NtubTimetableEntryAdmin(MaintainerScopedAdmin):
    form = NtubTimetableEntryForm
    list_display = ["snapshot", "entry_type", "weekday", "start_period", "end_period",
                    "course_code", "official_serial", "course_name", "teacher_name",
                    "classroom", "course_credit_display", "credit_basis_display"]
    list_filter = ["entry_type", "weekday", "snapshot__term"]
    search_fields = ["course_code", "course_name", "teacher_name", "classroom",
                     "snapshot__class_group__code"]
    list_select_related = ["snapshot", "snapshot__class_group", "snapshot__term"]
    readonly_fields = [
        "snapshot",
        "entry_type", "course_code", "official_serial", "course_name",
        "teacher_name", "classroom", "weekday", "start_period", "end_period",
        "periods", "raw_text", "course_href",
        "course_credit_display", "credit_basis_display",
    ]

    def get_queryset(self, request):
        queryset = super().get_queryset(request)
        return queryset.filter(snapshot__class_group__in=scoped_class_groups(request.user))

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_readonly_fields(self, request, obj=None):
        # 新增時允許填入完整官方明細；既有匯入資料的原始欄位不可覆蓋。
        return self.readonly_fields if obj is not None else []

    @admin.display(description="課程學分")
    def course_credit_display(self, obj):
        return effective_entry_credit(obj).value

    @admin.display(description="學分依據")
    def credit_basis_display(self, obj):
        return effective_entry_credit(obj).basis

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "snapshot":
            kwargs["queryset"] = NtubTimetableSnapshot.objects.filter(
                class_group__in=scoped_class_groups(request.user))
        return super().formfield_for_foreignkey(db_field, request, **kwargs)



@admin.register(OfficialCourseCredit)
class OfficialCourseCreditAdmin(MaintainerScopedAdmin):
    list_display = ["term", "class_group", "course_code", "credits", "source_note"]
    list_filter = ["term", "class_group__department__program_type"]
    search_fields = ["course_code", "class_group__code", "class_group__name_zh", "source_note"]
    list_select_related = ["term", "class_group"]

    def get_queryset(self, request):
        return super().get_queryset(request).filter(
            class_group__in=scoped_class_groups(request.user))

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "class_group":
            kwargs["queryset"] = scoped_class_groups(request.user)
        return super().formfield_for_foreignkey(db_field, request, **kwargs)


@admin.register(OfficialTeachingSyllabus)
class OfficialTeachingSyllabusAdmin(MaintainerScopedAdmin):
    list_display = ["academic_year", "semester", "class_code", "course_code",
                    "course_name", "credits", "status", "fetched_at"]
    list_filter = ["status", "academic_year", "semester"]
    search_fields = ["class_code", "course_code", "course_name", "teacher_name"]
    readonly_fields = ["academic_year", "semester", "class_code", "official_serial",
                       "course_code", "course_name", "course_name_en", "credits",
                       "weekly_hours", "teacher_name", "teaching_target",
                       "teaching_matter", "weekly_progress", "assessment",
                       "references", "source_url", "fetched_at", "status",
                       "error_message", "raw_payload", "created_at", "updated_at"]

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path("retry/<int:object_id>/",
                 self.admin_site.admin_view(self.retry_syllabus),
                 name="courses_officialteachingsyllabus_retry"),
        ]
        return custom + urls

    def retry_syllabus(self, request, object_id):
        if request.method != "POST":
            raise PermissionDenied("請以 POST 重試官方課綱。")
        record = self.get_queryset(request).filter(pk=object_id).first()
        if record is None:
            raise PermissionDenied("你沒有重試此課綱的權限。")
        if record.snapshot_id is None:
            raise PermissionDenied("此課綱沒有對應的官方班級課表快照，不能重試。")
        record = sync_syllabus({
            "academic_year": record.academic_year,
            "semester": record.semester,
            "class_code": record.class_code,
            "official_serial": record.official_serial,
            "course_code": record.course_code,
        }, snapshot=record.snapshot)
        self.message_user(
            request,
            f"{record.course_code}：{record.get_status_display()}",
            messages.SUCCESS if record.status == "success" else messages.WARNING,
        )
        return HttpResponseRedirect(
            reverse("admin:courses_officialteachingsyllabus_changelist"))

    def get_queryset(self, request):
        return super().get_queryset(request).filter(
            class_code__in=scoped_class_groups(request.user).values("code"))

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(NtubOfferingSource)
class NtubOfferingSourceAdmin(ScopedReadOnlyAdmin):
    """歷史官方開課來源：匯入唯讀，僅 Superuser 可安全清理來源列。"""

    list_display = ["source_key", "course_code", "course_name", "class_group",
                    "official_serial", "teacher_name", "day_label", "time_range",
                    "classroom_code", "official_capacity", "official_enrolled",
                    "local_enrolled", "term"]
    list_filter = ["term", "class_group__department__program_type", "elective_category"]
    search_fields = ["source_key", "course_code", "course_name", "teacher_name",
                     "class_group__code", "classroom_code", "classroom_name"]
    list_select_related = ["term", "class_group", "course", "section", "teacher"]
    date_hierarchy = "retrieved_at"
    change_list_template = "admin/courses/ntubofferingsource/change_list.html"
    delete_confirmation_template = (
        "admin/courses/ntubofferingsource/delete_confirmation.html"
    )
    delete_selected_confirmation_template = (
        "admin/courses/ntubofferingsource/delete_selected_confirmation.html"
    )
    actions = ["delete_selected"]

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path("sync-syllabi/", self.admin_site.admin_view(self.sync_syllabi),
                 name="courses_ntubofferingsource_sync_syllabi"),
        ]
        return custom + urls

    def sync_syllabi(self, request):
        if request.method != "POST":
            raise PermissionDenied("請以 POST 執行官方課綱補抓。")
        if not self.has_view_or_change_permission(request):
            raise PermissionDenied("你沒有使用歷史匯入工具的權限。")
        records = sync_offering_syllabi(self.get_queryset(request))
        succeeded = sum(record.status == "success" for record in records)
        failed = len(records) - succeeded
        self.message_user(
            request,
            f"課綱補抓完成：成功 {succeeded} 筆，失敗 {failed} 筆；"
            "失敗資料保留，可稍後重試。",
            messages.SUCCESS if not failed else messages.WARNING,
        )
        return HttpResponseRedirect(
            reverse("admin:courses_ntubofferingsource_changelist"))

    def has_delete_permission(self, request, obj=None):
        return is_superuser(request.user)

    @staticmethod
    def _deletion_impact(queryset):
        source_ids = list(queryset.values_list("pk", flat=True))
        course_ids = list(queryset.values_list("course_id", flat=True))
        section_ids = list(queryset.values_list("section_id", flat=True))
        protected_relations = []
        for relation in NtubOfferingSource._meta.related_objects:
            if getattr(relation.field.remote_field.on_delete, "__name__", "") != "PROTECT":
                continue
            related_count = relation.related_model._default_manager.filter(
                **{f"{relation.field.name}__in": source_ids},
            ).count()
            if related_count:
                protected_relations.append({
                    "model": relation.related_model._meta.verbose_name_plural,
                    "count": related_count,
                    "reason": "此關聯使用 PROTECT，需先由管理員處理。",
                })
        return {
            "source_count": len(source_ids),
            "course_count": queryset.values("course_id").distinct().count(),
            "teacher_count": queryset.values("teacher_id").distinct().count(),
            "section_count": queryset.values("section_id").distinct().count(),
            "enrollment_count": Enrollment.objects.filter(
                section_id__in=section_ids,
            ).count(),
            "waitlist_count": Waitlist.objects.filter(
                section_id__in=section_ids,
            ).count(),
            "fixed_entry_count": NtubTimetableEntry.objects.filter(
                course_id__in=course_ids,
            ).count(),
            "published_syllabus_count": CourseTeachingSyllabus.objects.filter(
                course_id__in=course_ids,
                status=CourseTeachingSyllabus.Status.PUBLISHED,
            ).count(),
            "syllabus_submission_count": CourseSyllabusSubmission.objects.filter(
                course_id__in=course_ids,
            ).count(),
            "recommendation_count": NtubCurriculumRecommendation.objects.filter(
                offering_id__in=source_ids,
            ).count(),
            "candidate_count": NtubRecommendationCandidate.objects.filter(
                offering_id__in=source_ids,
            ).count(),
            "ambiguity_count": NtubOfferingAmbiguity.objects.filter(
                offering_id__in=source_ids,
            ).count(),
            "protected_relations": protected_relations,
        }

    @admin.action(
        permissions=["delete"],
        description="依確認頁面刪除選定的歷史官方來源",
    )
    def delete_selected(self, request, queryset):
        if not is_superuser(request.user):
            raise PermissionDenied("只有 Superuser 可以清理歷史官方開課來源。")
        response = django_delete_selected(self, request, queryset)
        if isinstance(response, TemplateResponse) and response.context_data:
            response.context_data["source_deletion_impact"] = (
                self._deletion_impact(queryset)
            )
        return response

    def delete_view(self, request, object_id, extra_context=None):
        source = self.get_object(request, object_id)
        context = dict(extra_context or {})
        if source is not None:
            context["source_deletion_impact"] = self._deletion_impact(
                self.get_queryset(request).filter(pk=source.pk),
            )
        return super().delete_view(request, object_id, extra_context=context)

    def delete_model(self, request, obj):
        if not self.has_delete_permission(request, obj):
            raise PermissionDenied("只有 Superuser 可以清理歷史官方開課來源。")
        with transaction.atomic():
            super().delete_model(request, obj)

    def delete_queryset(self, request, queryset):
        if not is_superuser(request.user):
            raise PermissionDenied("只有 Superuser 可以清理歷史官方開課來源。")
        source_ids = list(queryset.values_list("pk", flat=True))
        with transaction.atomic():
            sources = list(
                self.get_queryset(request).select_for_update().filter(
                    pk__in=source_ids,
                )
            )
            if len(sources) != len(source_ids):
                raise PermissionDenied("刪除清單包含無權管理的官方來源。")
            super().delete_queryset(request, self.model.objects.filter(
                pk__in=source_ids,
            ))

    def scope_queryset(self, request, queryset):
        return queryset.filter(class_group__in=scoped_class_groups(request.user))

    @admin.display(description="星期", ordering="applied_day")
    def day_label(self, obj):
        labels = {1: "週一", 2: "週二", 3: "週三", 4: "週四",
                  5: "週五", 6: "週六", 7: "週日"}
        return labels.get(obj.applied_day, "")

    @admin.display(description="節次")
    def time_range(self, obj):
        if not obj.applied_start_period:
            return ""
        return f"{obj.applied_start_period}-{obj.applied_end_period}"

    @admin.display(description="本地加選人數")
    def local_enrolled(self, obj):
        return obj.section.enrollments.count()


@admin.register(NtubCurriculumRecommendation)
class NtubCurriculumRecommendationAdmin(ScopedReadOnlyAdmin):
    """課綱推薦：來源資料只能由匯入指令寫入，管理介面一律唯讀。

    `course_name_only` 的推薦在這裡只是「疑義」，不會因為有人在這裡看過
    就變成開放加選；真正開放要走 SectionClassPolicy。
    """

    list_display = ["admission_year", "track", "class_code", "course_name",
                    "match_status", "matched_offering_class_code", "is_ambiguous",
                    "offering"]
    list_filter = ["match_status", "is_ambiguous", "track", "admission_year"]
    search_fields = ["class_code", "course_name", "matched_offering_class_code",
                     "matched_course_code"]
    list_select_related = ["offering", "course", "class_group"]

    def scope_queryset(self, request, queryset):
        return queryset.filter(
            Q(class_group__in=scoped_class_groups(request.user))
            | Q(department_code__in=scoped_departments(request.user).values("code"))
        )

    @admin.display(description="候選開課")
    def candidate_count(self, obj):
        return obj.candidates.count()


@admin.register(NtubRecommendationCandidate)
class NtubRecommendationCandidateAdmin(ScopedReadOnlyAdmin):
    list_display = ["recommendation", "offering_key", "course_code", "class_code",
                    "course_name", "offering"]
    search_fields = ["offering_key", "course_code", "class_code", "course_name"]
    list_select_related = ["recommendation", "offering"]

    def scope_queryset(self, request, queryset):
        return queryset.filter(
            Q(offering__class_group__in=scoped_class_groups(request.user))
            | Q(recommendation__class_group__in=scoped_class_groups(request.user))
        )


@admin.register(NtubOfferingAmbiguity)
class NtubOfferingAmbiguityAdmin(ScopedReadOnlyAdmin):
    list_display = ["kind", "message", "offering", "recommendation"]
    list_filter = ["kind"]
    search_fields = ["message"]
    list_select_related = ["offering", "recommendation"]

    def scope_queryset(self, request, queryset):
        return queryset.filter(
            Q(offering__class_group__in=scoped_class_groups(request.user))
            | Q(recommendation__class_group__in=scoped_class_groups(request.user))
        )


def restrict_to_superuser(*models):
    """把已註冊的 ModelAdmin 限制成只有 superuser 能動（保留原本的欄位設定）。

    用在 OAuth 設定（allauth）、使用者與群組權限（auth）、站台設定（sites）這幾個
    介面上：維護員本來就沒有這些模型權限，這裡再釘死一次，避免日後有人手動加權限
    就讓維護員碰到 OAuth／帳號權限／系統設定。
    """
    for model in models:
        existing = admin.site._registry.get(model)
        if existing is None:
            continue
        admin.site.unregister(model)
        admin.site.register(
            model,
            type(f"SuperuserOnly{type(existing).__name__}",
                 (type(existing), SuperuserOnlyAdmin), {}),
        )


restrict_to_superuser(
    User, Group, EmailAddress, SocialAccount, SocialApp, SocialToken, Site,
)


class PolicyTermFilter(admin.SimpleListFilter):
    """依「學期」篩選修課政策。

    學期是從 section → course → term 推導出來的，模型上沒有可直接篩選的欄位，
    所以自己寫一支 SimpleListFilter。選項只列出 **目前使用者看得到的政策**
    所涉及的學期（走 ModelAdmin.get_queryset()），維護員不會從篩選清單看到
    別系政策的存在。
    """

    title = "學期"
    parameter_name = "policy_term"

    def lookups(self, request, model_admin):
        queryset = model_admin.get_queryset(request).prefetch_related(None)
        codes = (queryset.order_by()
                 .values_list("section__course__term__code", flat=True)
                 .distinct())
        return [(code, code) for code in codes if code]

    def queryset(self, request, queryset):
        value = self.value()
        if not value:
            return queryset
        return queryset.filter(section__course__term__code=value)


class ApplicableClassGroupFilter(admin.SimpleListFilter):
    """依「適用學生班級」篩選修課政策。

    選項只列出 **目前使用者看得到的政策** 所涉及的班級：篩選器本身走的是
    ModelAdmin.get_queryset()，所以 Course Maintainer 只會看到自己授權系所的
    班級，不會因為篩選清單就看到別系的班級。

    篩選用班級代碼（class_group__code）而不是主鍵：代碼是維護員在課表上
    認得的東西，而且同一個班級代碼在不同匯入批次可能對到不同主鍵。
    """

    title = "適用學生班級"
    parameter_name = "policy_class_group"

    def lookups(self, request, model_admin):
        # prefetch_related(None)：values_list 不需要預載，而且 Django 不允許
        # 對 values() 結果做 prefetch。
        queryset = model_admin.get_queryset(request).prefetch_related(None)
        pairs = (queryset.order_by()
                 .values_list("class_group__code", "class_group__name_zh")
                 .distinct())
        return [(code, f"{code} {name}".strip())
                for code, name in pairs if code]

    def queryset(self, request, queryset):
        value = self.value()
        if not value:
            return queryset
        return queryset.filter(class_group__code=value)


@admin.register(SectionClassPolicy)
class SectionClassPolicyAdmin(admin.ModelAdmin):
    """跨班修課政策：這個開課班別對哪個學生班級是什麼屬性、能不能自行加選。

    「管理員必須先指定適用班級」：政策一定要綁一個 AcademicClassGroup，
    不存在只靠課名自動開放的路徑。

    權限全部在伺服器端檢查，任何一層單獨失效都不會失守：

    1. changelist / 搜尋 → 只列出授權系所的政策（get_queryset）。
    2. ForeignKey 下拉與 autocomplete → 只給授權系所的班別與相關班別
       （formfield_for_foreignkey，autocomplete 走相關 ModelAdmin 的 get_queryset）。
    3. 物件層級檢查 → 別系的政策連編輯頁都開不了（has_change_permission）。
    4. 儲存時再檢查一次 → 即使偽造 POST 送出別系的班別，也會被擋下來
       （save_model），而且不會寫入任何資料。
    """

    # --- 清單欄位 -----------------------------------------------------------
    # 欄位順序對應維護員的判讀順序：這是誰的課、哪一門課、什麼屬性、有沒有開放。
    list_display = [
        "term_display",
        "applicable_class_group_display",
        "course_code_display",
        "course_name_display",
        "source_class_group_display",
        "course_category_display",
        "can_enroll_display",
        "status_display",
        "confirmed_by_display",
        "confirmed_at_display",
    ]
    # 點課號進到該筆政策的編輯頁（學期欄只是說明用的文字，不做成連結）。
    list_display_links = ("course_code_display",)
    # 篩選：學期 / 適用班級 / 發布狀態，加上維護員原本就在用的分類與加選旗標。
    list_filter = [
        "section__course__term",
        ApplicableClassGroupFilter,
        "status",
        "course_category",
        "can_enroll",
    ]
    # 搜尋：課號、課名、班級代碼。
    search_fields = ["section__course__code", "section__course__name",
                     "class_group__code", "class_group__name_zh"]
    list_select_related = ["section__course__term", "class_group__department", "confirmed_by"]
    list_per_page = 50
    # 排序：最新的學期優先，然後課號、班別、班級代碼。
    ordering = ["-section__course__term__code", "section__course__code",
                "section__label", "class_group__code"]
    autocomplete_fields = ["section", "class_group"]
    fields = ["section", "class_group", "course_category", "can_enroll", "status",
              "notes", "confirmed_by", "confirmed_at"]
    # 確認帳號與時間由系統寫入，表單裡不可編輯（避免偽造稽核紀錄）
    readonly_fields = ["confirmed_by", "confirmed_at"]
    actions = ["publish_selected_policies", "withdraw_selected_policies"]
    # 批次安排入口的連結放在名單頁的 object-tools 區塊（見下方模板）。
    change_list_template = "admin/courses/sectionclasspolicy_change_list.html"

    # --- 清單欄位的顯示邏輯 -------------------------------------------------

    @admin.display(description="學期", ordering="section__course__term__code")
    def term_display(self, obj):
        """學期來自 section → course → term，不另外存一份。"""
        return obj.section.course.term.code

    @admin.display(description="適用學生班級", ordering="class_group__code")
    def applicable_class_group_display(self, obj):
        """適用的學生班級：班級代碼 + 班級名稱。"""
        group = obj.class_group
        name = group.name_zh or ""
        return f"{group.code} {name}".strip()

    @admin.display(description="課號", ordering="section__course__code")
    def course_code_display(self, obj):
        return obj.section.course.code

    @admin.display(description="課名", ordering="section__course__name")
    def course_name_display(self, obj):
        return obj.section.course.name

    @admin.display(description="來源開課班級")
    def source_class_group_display(self, obj):
        """官方來源對應到的開課班級代碼（沒有官方來源時顯示「—」）。

        同一個 Section 可能對應到多筆官方來源（不同來源鍵），所以把代碼
        去重後用「、」串起來；沒有資料時不顯示空白，讓維護員一眼看出
        「這筆政策沒有官方來源」。
        """
        codes = sorted({source.class_group.code
                        for source in obj.section.ntub_offering_sources.all()
                        if source.class_group_id})
        if not codes:
            return "—"
        return "、".join(codes)

    @admin.display(description="修課分類", ordering="course_category")
    def course_category_display(self, obj):
        return obj.get_course_category_display()

    @admin.display(description="可否自行加選", ordering="can_enroll")
    def can_enroll_display(self, obj):
        """以顏色區分是否可自行加選。"""
        if obj.can_enroll:
            return format_html(
                '<span style="color:#1a7f37;font-weight:600;">{}</span>',
                "可自行加選",
            )
        return format_html(
            '<span style="color:#6c757d;font-weight:600;">{}</span>',
            "不可自行加選",
        )

    @admin.display(description="發布狀態", ordering="status")
    def status_display(self, obj):
        """以標籤區分已發布與草稿。"""
        if obj.status == SectionClassPolicy.Status.PUBLISHED:
            return format_html(
                '<span style="color:#1a7f37;font-weight:700;'
                'background:#e3f3e6;padding:1px 8px;border-radius:10px;">'
                '{}</span>',
                "已發布",
            )
        return format_html(
            '<span style="color:#6c757d;font-weight:600;'
            'background:#f1f1f1;padding:1px 8px;border-radius:10px;">'
            '{}</span>',
            "草稿",
        )

    @admin.display(description="確認人", ordering="confirmed_by__username")
    def confirmed_by_display(self, obj):
        """誰確認了這筆政策（沒確認過就是「—」）。"""
        user = obj.confirmed_by
        if user is None:
            return "—"
        return user.email or user.get_username()

    @admin.display(description="確認時間", ordering="confirmed_at")
    def confirmed_at_display(self, obj):
        """確認時間（台北時區；沒有確認過就是「—」）。"""
        if obj.confirmed_at is None:
            return "—"
        return timezone.localtime(obj.confirmed_at).strftime("%Y-%m-%d %H:%M")

    def get_urls(self):
        """額外掛一個「批次安排班級課程」的獨立頁面。

        自訂網址放在 `super().get_urls()` **前面**：Django 內建的
        `<path:object_id>/` 會吃掉任何第一層路徑，必須先比對才不會把
        「bulk-assign/」當成物件 id。

        這條網頁用 `admin_site.admin_view()` 包住：未登入／非 staff 會被導到
        登入頁（沿用 Django Admin 自己的登入、CSRF 與權限機制）。
        """
        custom = [
            path("bulk-assign/",
                 self.admin_site.admin_view(self.bulk_assign_view),
                 name="courses_sectionclasspolicy_bulk_assign"),
        ]
        return custom + super().get_urls()

    # --- 批次安排班級課程 ----------------------------------------------------

    def _bulk_assign_permission(self, request):
        """批次功能的伺服器端權限檢查。

        三層：必須是課程維護員（群組）、必須有有效的授權系所範圍、
        必須有新增修課政策的模型權限。任何一層不過就 403。
        """
        user = request.user
        if not is_maintainer(user):
            raise PermissionDenied("只有課程維護員可以使用批次安排功能。")
        if not user.has_perm("courses.add_sectionclasspolicy"):
            raise PermissionDenied("你沒有新增修課政策的權限。")
        if not is_superuser(user) and not scoped_departments(user).exists():
            raise PermissionDenied("你尚未被授權任何系所，無法批次安排課程。")

    def bulk_assign_view(self, request):
        """批次安排班級課程。

        流程：GET 選學期 + 學生班級 → 勾選多門來源開課 + 選修課分類 →
        POST 後批次建立 DRAFT 政策（已存在的不覆寫，只回報略過筆數）。
        """
        self._bulk_assign_permission(request)
        user = request.user

        context = {
            **self.admin_site.each_context(request),
            "title": "批次安排班級課程",
            "opts": self.model._meta,
            "media": self.media,
            "changelist_url": reverse("admin:courses_sectionclasspolicy_changelist"),
        }

        # 第一步的表單：有帶參數才綁定，避免初次開頁面就顯示「必填」錯誤。
        select_data = request.GET if (request.method == "GET" and request.GET) else None
        select_form = BulkClassAssignmentSelectForm(select_data, user=user)
        bulk_form = None
        selected_term = request.POST.get("term") if request.method == "POST" else None
        selected_class_group = (request.POST.get("class_group")
                                if request.method == "POST" else None)

        if request.method == "POST":
            # 先用 POST 裡的學期收斂來源班別範圍，欄位驗證仍會再檢查一次。
            term = Term.objects.filter(pk=request.POST.get("term")).first()
            bulk_form = BulkClassAssignmentForm(request.POST, user=user, term=term)
            if bulk_form.is_valid():
                created, skipped = self._create_bulk_policies(bulk_form)
                self.message_user(
                    request,
                    f"批次安排完成：建立 {created} 筆草稿政策；已存在而略過 {skipped} 筆。")
                return HttpResponseRedirect(
                    reverse("admin:courses_sectionclasspolicy_changelist"))
        elif select_form.is_valid():
            term = select_form.cleaned_data["term"]
            class_group = select_form.cleaned_data["class_group"]
            selected_term = term.pk
            selected_class_group = class_group.pk
            bulk_form = BulkClassAssignmentForm(
                user=user, term=term,
                initial={"term": term.pk, "class_group": class_group.pk})

        context["select_form"] = select_form
        context["bulk_form"] = bulk_form
        context["selected_term"] = selected_term
        context["selected_class_group"] = selected_class_group
        context["selected_class_group_obj"] = (
            scoped_class_groups(user).select_related(
                "department", "department__program_type"
            ).filter(pk=selected_class_group).first()
            if selected_class_group else None
        )
        return TemplateResponse(request, "admin/courses/bulk_class_assignment.html", context)

    def _create_bulk_policies(self, form):
        """批次建立政策：全部在單一 transaction 裡，已存在的不覆寫。

        已存在的政策（同一 section + class_group）會被安全略過：既有的
        修課分類、發布狀態、確認者都不會被改動，只回報略過筆數。
        """
        class_group = form.cleaned_data["class_group"]
        category = form.cleaned_data["course_category"]
        can_enroll = form.cleaned_data["can_enroll"]
        sections = form.cleaned_data["sections"]

        created = 0
        skipped = 0
        with transaction.atomic():
            for section in sections:
                _, was_created = SectionClassPolicy.objects.get_or_create(
                    section=section,
                    class_group=class_group,
                    defaults={
                        "course_category": category,
                        "can_enroll": can_enroll,
                        "status": SectionClassPolicy.Status.DRAFT,
                        "notes": "批次安排班級課程建立",
                    },
                )
                if was_created:
                    created += 1
                else:
                    skipped += 1
        return created, skipped

    def get_queryset(self, request):
        queryset = super().get_queryset(request)
        if is_superuser(request.user):
            return queryset.prefetch_related(
                Prefetch("section__ntub_offering_sources",
                         queryset=NtubOfferingSource.objects.select_related("class_group")))
        # 政策屬於「適用班級」那個系所：跨班政策由學生端的系所負責維護。
        return queryset.filter(
            class_group__department__in=scoped_departments(request.user)
        ).prefetch_related(
            Prefetch("section__ntub_offering_sources",
                     queryset=NtubOfferingSource.objects.select_related("class_group")))

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        # 下拉選單（與 autocomplete）只給授權系所的資料：偽造 POST 也選不到別系的班級。
        if db_field.name == "class_group":
            kwargs["queryset"] = scoped_class_groups(request.user)
        elif db_field.name == "section":
            kwargs["queryset"] = scoped_sections(request.user)
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def has_view_permission(self, request, obj=None):
        if not super().has_view_permission(request, obj):
            return False
        if obj is None or is_superuser(request.user):
            return True
        return can_manage_policy(request.user, obj)

    def has_change_permission(self, request, obj=None):
        if not super().has_change_permission(request, obj):
            return False
        if obj is None or is_superuser(request.user):
            return True
        return can_manage_policy(request.user, obj)

    def has_delete_permission(self, request, obj=None):
        if not super().has_delete_permission(request, obj):
            return False
        if obj is None or is_superuser(request.user):
            return True
        return can_manage_policy(request.user, obj)

    def save_model(self, request, obj, form, change):
        user = request.user
        if not is_superuser(user):
            # 伺服器端最後一道：適用班級必須在授權系所底下，開課班別必須跟自己的系所相關。
            if not scoped_departments(user).filter(pk=obj.class_group.department_id).exists():
                raise PermissionDenied("你只能管理授權系所的班級修課政策。")
            if not can_manage_section(user, obj.section):
                raise PermissionDenied("你只能管理自己系所相關的開課班別。")
        if obj.status == SectionClassPolicy.Status.PUBLISHED:
            previous = None
            if change:
                previous = (SectionClassPolicy.objects.filter(pk=obj.pk)
                            .values_list("status", flat=True).first())
            if previous != obj.status or obj.confirmed_by_id is None:
                # 發布＝維護員確認：記下是誰、何時確認的。
                obj.confirmed_by = user
                obj.confirmed_at = timezone.now()
        super().save_model(request, obj, form, change)

    @admin.action(description="發布選定的修課政策（記錄確認帳號與時間）")
    def publish_selected_policies(self, request, queryset):
        published = 0
        for policy in queryset:
            if not (is_superuser(request.user) or can_manage_policy(request.user, policy)):
                continue
            policy.status = SectionClassPolicy.Status.PUBLISHED
            policy.confirmed_by = request.user
            policy.confirmed_at = timezone.now()
            policy.save(update_fields=["status", "confirmed_by", "confirmed_at", "updated_at"])
            published += 1
        self.message_user(request, f"已發布 {published} 筆修課政策。")

    @admin.action(description="撤回選定的修課政策（回到草稿，不開放加選）")
    def withdraw_selected_policies(self, request, queryset):
        withdrawn = 0
        for policy in queryset:
            if not (is_superuser(request.user) or can_manage_policy(request.user, policy)):
                continue
            policy.status = SectionClassPolicy.Status.DRAFT
            policy.save(update_fields=["status", "updated_at"])
            withdrawn += 1
        self.message_user(request, f"已撤回 {withdrawn} 筆修課政策。")


class CourseMaintainerScopeAdminForm(forms.ModelForm):
    # 保留舊版單一 department POST 的相容輸入；畫面改用 departments。
    department = forms.ModelChoiceField(
        queryset=AcademicDepartment.objects.all(),
        required=False,
        widget=forms.HiddenInput,
    )
    departments = forms.ModelMultipleChoiceField(
        label="授權系所（可跨學制多選）",
        queryset=AcademicDepartment.objects.select_related("program_type").order_by(
            "program_type__sort_order", "program_type__code", "code"
        ),
        required=False,
        help_text="請選取現有系所；修改既有範圍時，原系所會保留，停用請使用列表操作。",
        widget=forms.SelectMultiple(attrs={"size": 12}),
    )
    department_catalog = forms.CharField(
        required=False,
        label="系所資料",
        widget=forms.HiddenInput,
    )

    class Meta:
        model = CourseMaintainerScope
        fields = ["user", "department", "departments", "department_catalog", "is_active"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        departments = self.fields["departments"].queryset
        class_groups = AcademicClassGroup.objects.filter(
            department__in=departments
        ).values("id", "department_id", "grade_no", "code", "name_zh")
        catalog = [
            {
                "id": department.pk,
                "code": department.code,
                "name": department.name_zh,
                "program_id": department.program_type_id,
                "program_code": department.program_type.code,
                "program_name": department.program_type.name_zh,
                "classes": [
                    {
                        "id": group["id"],
                        "grade": group["grade_no"],
                        "code": group["code"],
                        "name": group["name_zh"],
                    }
                    for group in class_groups
                    if group["department_id"] == department.pk
                ],
            }
            for department in departments
        ]
        self.initial["department_catalog"] = json.dumps(catalog, ensure_ascii=False)
        if self.instance.pk:
            self.fields["departments"].initial = CourseMaintainerScope.objects.filter(
                user=self.instance.user, is_active=True
            ).values_list("department_id", flat=True)

    def clean_departments(self):
        departments = self.cleaned_data["departments"]
        if not departments and self.cleaned_data.get("department") is not None:
            departments = [self.cleaned_data["department"]]
        if not departments:
            raise forms.ValidationError("請至少選取一個授權系所。")
        if self.instance.pk and self.instance.department_id not in {
            department.pk for department in departments
        }:
            raise forms.ValidationError("修改既有授權時不可移除原系所，請使用停用操作。")
        return departments

    def clean_user(self):
        user = self.cleaned_data["user"]
        if self.instance.pk and user.pk != self.instance.user_id:
            raise forms.ValidationError("既有授權不可更換維護員帳號。")
        return user


@admin.register(CourseMaintainerScope)
class CourseMaintainerScopeAdmin(admin.ModelAdmin):
    """維護員權限範圍：只有 superuser 能新增、修改、刪除。

    一般維護員連這個頁面都開不了（不是把欄位藏起來而已），所以維護員
    無法自己擴大權限、也無法把自己升成 superuser。
    """

    list_display = ["user", "department", "is_active", "updated_at"]
    list_filter = ["is_active", "department"]
    search_fields = ["user__email", "user__username", "department__code", "department__name_zh"]
    form = CourseMaintainerScopeAdminForm
    change_form_template = "admin/courses/coursemaintainerscope/change_form.html"
    actions = ["grant_selected_scopes", "revoke_selected_scopes"]

    def has_module_permission(self, request):
        return is_superuser(request.user)

    def has_view_permission(self, request, obj=None):
        return is_superuser(request.user)

    def has_add_permission(self, request):
        return is_superuser(request.user)

    def has_change_permission(self, request, obj=None):
        return is_superuser(request.user)

    def has_delete_permission(self, request, obj=None):
        return is_superuser(request.user)

    def save_model(self, request, obj, form, change):
        departments = list(form.cleaned_data["departments"])
        obj.department = departments[0]
        if not change:
            existing = CourseMaintainerScope.objects.filter(
                user=obj.user, department=obj.department
            ).first()
            if existing is not None:
                obj.pk = existing.pk
                obj._state.adding = False
                obj.created_at = existing.created_at
                obj.updated_at = existing.updated_at
        super().save_model(request, obj, form, change)
        for department in departments[1:]:
            scope, created = CourseMaintainerScope.objects.get_or_create(
                user=obj.user,
                department=department,
                defaults={"is_active": obj.is_active},
            )
            if not created and scope.is_active != obj.is_active:
                scope.is_active = obj.is_active
                scope.save(update_fields=["is_active", "updated_at"])
            if obj.is_active:
                grant_maintainer_access(scope.user)
            else:
                revoke_maintainer_access(scope.user)
        # 授權的當下就把帳號放進維護員群組並開啟 staff，維護員才能進 /admin/。
        if obj.is_active:
            grant_maintainer_access(obj.user)
        else:
            revoke_maintainer_access(obj.user)

    def delete_model(self, request, obj):
        user = obj.user
        super().delete_model(request, obj)
        revoke_maintainer_access(user)

    @admin.action(description="啟用選定的權限範圍（並同步維護員權限）")
    def grant_selected_scopes(self, request, queryset):
        granted = 0
        for scope in queryset:
            scope.is_active = True
            scope.save(update_fields=["is_active", "updated_at"])
            grant_maintainer_access(scope.user)
            granted += 1
        self.message_user(request, f"已啟用 {granted} 筆維護員權限範圍。")

    @admin.action(description="停用選定的權限範圍（並收回維護員權限）")
    def revoke_selected_scopes(self, request, queryset):
        revoked = 0
        for scope in queryset:
            scope.is_active = False
            scope.save(update_fields=["is_active", "updated_at"])
            revoke_maintainer_access(scope.user)
            revoked += 1
        self.message_user(request, f"已停用 {revoked} 筆維護員權限範圍。")
