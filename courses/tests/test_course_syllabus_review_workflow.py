from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.core.management import call_command
from django.contrib.auth.signals import user_logged_in
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory, TestCase
from django.urls import reverse

from ..admin import TeacherAdminForm
from ..admin_navigation import can_dashboard
from ..maintainer import grant_maintainer_access
from ..models import (
    AcademicCatalog, AcademicDepartment, AcademicProgramType, Course,
    CourseMaintainerScope, CourseSyllabusSubmission, CourseTeachingSyllabus,
    Teacher, Term,
)
from ..teacher_access import SYLLABUS_TEACHER_GROUP_NAME
from .session_helpers import force_admin_login


WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
DOCX_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument."
    "wordprocessingml.document.main+xml"
)


def make_table_docx():
    rows = [
        ("教學目標", "理解社群資料分析"),
        ("教學內容", "社群資料蒐集與分析"),
    ]
    chinese_weeks = (
        "一", "二", "三", "四", "五", "六", "七", "八", "九", "十",
        "十一", "十二", "十三", "十四", "十五", "十六", "十七", "十八",
    )
    for week in range(1, 19):
        label = f"第{week}週" if week % 2 == 0 else (
            f"第{chinese_weeks[week - 1]}週"
        )
        rows.append((label, f"實作內容 {week}"))
    rows.extend((
        ("成績評量", "期末專題"),
        ("指定教科書及參考書籍", "教師指定資料"),
    ))
    row_xml = []
    for index, (label, content) in enumerate(rows):
        content_paragraphs = (
            f"<w:p><w:r><w:t>{content}</w:t></w:r></w:p>"
            "<w:p><w:r><w:t>補充內容</w:t></w:r></w:p>"
            if index == 2 else
            f"<w:p><w:r><w:t>{content}</w:t></w:r></w:p>"
        )
        second_cell = (
            f"<w:tc><w:tcPr><w:gridSpan w:val=\"2\"/></w:tcPr>"
            f"{content_paragraphs}</w:tc>"
            if index == 1 else
            f"<w:tc>{content_paragraphs}</w:tc>"
        )
        row_xml.append(
            "<w:tr>"
            f"<w:tc><w:p><w:r><w:t>{label}</w:t></w:r></w:p></w:tc>"
            f"{second_cell}</w:tr>"
        )
    document = (
        f'<w:document xmlns:w="{WORD_NS}"><w:body><w:tbl>'
        f'{"".join(row_xml)}</w:tbl></w:body></w:document>'
    )
    content_types = (
        f'<Types><Override ContentType="{DOCX_CONTENT_TYPE}"/></Types>'
    )
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as package:
        package.writestr("[Content_Types].xml", content_types)
        package.writestr("word/document.xml", document)
    return output.getvalue()


class CourseSyllabusReviewWorkflowTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.teacher_group = Group.objects.create(
            name=SYLLABUS_TEACHER_GROUP_NAME,
        )
        self.term = Term.objects.create(code="115-1", is_current=True)
        self.catalog = AcademicCatalog.objects.create(version="syllabus-review")
        self.program = AcademicProgramType.objects.create(
            catalog=self.catalog, code="4", name_zh="四技日間部",
        )
        self.department = AcademicDepartment.objects.create(
            catalog=self.catalog, program_type=self.program, code="406",
            name_zh="資訊管理系",
        )
        self.other_department = AcademicDepartment.objects.create(
            catalog=self.catalog, program_type=self.program, code="500",
            name_zh="其他系所",
        )
        self.teacher_user = user_model.objects.create_user(
            "syllabus-teacher", is_staff=True,
        )
        self.teacher_group.user_set.add(self.teacher_user)
        self.teacher = Teacher.objects.create(
            name="測試教師", user=self.teacher_user,
        )
        self.other_teacher_user = user_model.objects.create_user(
            "other-syllabus-teacher", is_staff=True,
        )
        self.teacher_group.user_set.add(self.other_teacher_user)
        self.other_teacher = Teacher.objects.create(
            name="其他教師", user=self.other_teacher_user,
        )
        self.course = self.make_course("CS001", self.department, self.teacher)
        self.other_course = self.make_course(
            "CS002", self.department, self.other_teacher,
        )
        self.out_of_scope_course = self.make_course(
            "CS003", self.other_department, self.teacher,
        )
        self.student = user_model.objects.create_user("syllabus-student")
        self.maintainer = user_model.objects.create_user(
            "syllabus-maintainer", is_staff=True,
        )
        CourseMaintainerScope.objects.create(
            user=self.maintainer, department=self.department,
        )
        grant_maintainer_access(self.maintainer)

    def make_course(self, code, department, teacher):
        return Course.objects.create(
            term=self.term, owning_department=department, code=code,
            name=f"課程 {code}", teacher=teacher, credits=3,
        )

    def syllabus_values(self, target="教師提交的教學目標"):
        return {
            "teaching_target": target,
            "teaching_matter": "教學內容",
            **{f"week_{week:02d}": f"第 {week} 週"
               for week in range(1, 19)},
            "assessment": "期末專題",
            "references": "參考書籍",
        }

    def submission_data(self, action, values=None, submission=None):
        data = {"teacher_action": action, **(values or self.syllabus_values())}
        if submission is not None:
            data["submission_id"] = str(submission.pk)
        return data

    def submit_for_review(self, course=None, values=None):
        course = course or self.course
        force_admin_login(self.client, self.teacher_user)
        response = self.client.post(
            reverse("admin:courses_course_teacher_syllabus", args=[course.pk]),
            self.submission_data("submit", values),
        )
        self.assertEqual(response.status_code, 302)
        return CourseSyllabusSubmission.objects.get(
            course=course, status=CourseSyllabusSubmission.Status.PENDING,
        )

    def test_teacher_only_sees_owned_courses_and_cannot_edit_course_model(self):
        force_admin_login(self.client, self.teacher_user)
        response = self.client.get(
            reverse("admin:courses_course_teacher_syllabi"),
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.course.code)
        self.assertNotContains(response, self.other_course.code)
        self.assertContains(response, self.out_of_scope_course.code)
        self.assertEqual(
            self.client.get(reverse("admin:courses_course_add")).status_code,
            403,
        )

        guessed_course_url = reverse(
            "admin:courses_course_teacher_syllabus",
            args=[self.other_course.pk],
        )
        self.assertEqual(self.client.get(guessed_course_url).status_code, 404)
        forged_post = self.client.post(
            guessed_course_url, self.submission_data("submit"),
        )
        self.assertEqual(forged_post.status_code, 404)
        self.assertFalse(CourseSyllabusSubmission.objects.exists())

    def test_binding_teacher_does_not_grant_staff_or_group_access(self):
        user_model = get_user_model()
        linked_user = user_model.objects.create_user("new-teacher-link")
        request = RequestFactory().post("/admin/courses/teacher/add/")
        request.user = user_model.objects.create_superuser(
            "teacher-link-admin", "teacher-link-admin@example.com", "password",
        )
        teacher_admin = admin.site._registry[Teacher]
        linked_teacher = Teacher(name="新教師", user=linked_user)
        teacher_admin.save_model(request, linked_teacher, None, False)

        linked_user.refresh_from_db()
        self.assertFalse(linked_user.is_staff)
        self.assertFalse(linked_user.is_superuser)
        self.assertFalse(linked_user.get_all_permissions())
        request.user = linked_user
        self.assertFalse(can_dashboard(request))

        force_admin_login(self.client, linked_user)
        self.assertEqual(
            self.client.get(reverse("admin:courses_course_teacher_syllabi")).status_code,
            302,
        )

        restricted_user = user_model.objects.create_user("teacher-with-permission")
        restricted_user.user_permissions.add(Permission.objects.get(
            content_type__app_label="courses", codename="change_course",
        ))
        form = TeacherAdminForm(
            data={
                "name": "已有權限",
                "email": "",
                "user": restricted_user.pk,
            },
        )
        self.assertFalse(form.is_valid())
        self.assertIn("先由 Superuser", str(form.errors))

    def test_login_does_not_promote_a_user_only_because_teacher_is_linked(self):
        user_model = get_user_model()
        linked_user = user_model.objects.create_user("existing-teacher-login")
        Teacher.objects.create(name="既有教師", user=linked_user)
        user_logged_in.send(
            sender=user_model,
            request=RequestFactory().get("/"),
            user=linked_user,
        )
        linked_user.refresh_from_db()
        self.assertFalse(linked_user.is_staff)
        self.assertFalse(linked_user.is_superuser)
        self.assertFalse(linked_user.get_all_permissions())

    def test_syllabus_teacher_group_command_is_idempotent_and_permissionless(self):
        group, _ = Group.objects.get_or_create(
            name=SYLLABUS_TEACHER_GROUP_NAME,
        )
        group.permissions.add(Permission.objects.get(
            content_type__app_label="courses", codename="change_course",
        ))
        original_members = set(group.user_set.values_list("pk", flat=True))
        call_command("setup_syllabus_teacher_group")
        call_command("setup_syllabus_teacher_group")
        group.refresh_from_db()
        self.assertEqual(
            Group.objects.filter(name=SYLLABUS_TEACHER_GROUP_NAME).count(), 1,
        )
        self.assertEqual(group.permissions.count(), 0)
        self.assertEqual(
            set(group.user_set.values_list("pk", flat=True)), original_members,
        )

    def test_superuser_can_create_teacher_account_and_bind_teacher(self):
        user_model = get_user_model()
        superuser = user_model.objects.create_superuser(
            "teacher-account-admin", "teacher-admin@example.com", "password",
        )
        force_admin_login(self.client, superuser)
        call_command("setup_syllabus_teacher_group")
        group = Group.objects.get(name=SYLLABUS_TEACHER_GROUP_NAME)
        add_url = reverse("admin:auth_user_add")
        add_page = self.client.get(add_url)
        self.assertEqual(add_page.status_code, 200)
        self.assertContains(add_page, "任課教師（課綱提交）")
        created = self.client.post(add_url, {
            "username": "managed-teacher",
            "email": "teacher-login@ntub.edu.tw",
            "password1": "Strong-test-password-123!",
            "password2": "Strong-test-password-123!",
            "is_active": "on",
            "groups": [str(group.pk)],
            "_save": "Save",
        })
        self.assertEqual(created.status_code, 302)
        teacher_user = user_model.objects.get(username="managed-teacher")
        self.assertTrue(teacher_user.is_active)
        self.assertTrue(teacher_user.is_staff)
        self.assertFalse(teacher_user.is_superuser)
        self.assertQuerySetEqual(
            teacher_user.groups.all(), [group], ordered=False,
        )
        self.assertFalse(teacher_user.get_all_permissions())

        teacher_add = self.client.post(reverse("admin:courses_teacher_add"), {
            "name": "正式教師",
            "email": "teacher-login@ntub.edu.tw",
            "user": str(teacher_user.pk),
            "_save": "Save",
        })
        self.assertEqual(teacher_add.status_code, 302)
        bound_teacher = Teacher.objects.get(user=teacher_user)
        self.assertEqual(bound_teacher.course_set.count(), 0)

        duplicate = self.client.post(reverse("admin:courses_teacher_add"), {
            "name": "重複教師",
            "email": "teacher-login@ntub.edu.tw",
            "user": str(teacher_user.pk),
            "_save": "Save",
        })
        self.assertEqual(duplicate.status_code, 200)
        self.assertContains(duplicate, "已綁定其他教師")
        self.assertEqual(Teacher.objects.filter(user=teacher_user).count(), 1)

    def test_superuser_can_open_teacher_admin_list_with_course_counts(self):
        superuser = get_user_model().objects.create_superuser(
            "teacher-list-admin", "teacher-list-admin@example.com", "password",
        )
        force_admin_login(self.client, superuser)

        response = self.client.get(reverse("admin:courses_teacher_changelist"))

        self.assertEqual(response.status_code, 200)
        teachers = {
            teacher.pk: teacher._course_count
            for teacher in response.context["cl"].result_list
        }
        self.assertEqual(teachers[self.teacher.pk], 2)
        self.assertEqual(teachers[self.other_teacher.pk], 1)

    def test_teacher_group_and_user_admin_are_superuser_only(self):
        force_admin_login(self.client, self.maintainer)
        for route in (
            "admin:auth_user_changelist",
            "admin:auth_user_add",
            "admin:auth_group_changelist",
            "admin:auth_group_add",
            "admin:courses_teacher_changelist",
            "admin:courses_teacher_add",
        ):
            self.assertEqual(self.client.get(reverse(route)).status_code, 403)

        force_admin_login(self.client, self.teacher_user)
        self.assertEqual(
            self.client.get(reverse("admin:auth_user_changelist")).status_code,
            403,
        )
        self.assertEqual(
            self.client.get(reverse("admin:auth_group_changelist")).status_code,
            403,
        )

    def test_staff_user_without_teacher_group_cannot_access_teacher_views(self):
        user_model = get_user_model()
        ungrouped_user = user_model.objects.create_user(
            "ungrouped-teacher", is_staff=True,
        )
        Teacher.objects.create(name="無群組教師", user=ungrouped_user)
        force_admin_login(self.client, ungrouped_user)
        self.assertEqual(
            self.client.get(reverse("admin:courses_course_teacher_syllabi")).status_code,
            403,
        )
        self.assertEqual(
            self.client.get(reverse(
                "admin:courses_course_teacher_syllabus", args=[self.course.pk],
            )).status_code,
            403,
        )

    def test_teacher_cannot_access_course_section_or_student_admin_models(self):
        force_admin_login(self.client, self.teacher_user)
        routes = (
            "admin:courses_course_changelist",
            "admin:courses_course_add",
            "admin:courses_section_changelist",
            "admin:courses_section_add",
            "admin:courses_enrollment_changelist",
            "admin:courses_waitlist_changelist",
            "admin:courses_studentacademicprofile_changelist",
            "admin:courses_ntubtimetablesnapshot_changelist",
        )
        for route in routes:
            with self.subTest(route=route):
                self.assertEqual(self.client.get(reverse(route)).status_code, 403)

    def test_teacher_can_save_draft_before_submitting(self):
        force_admin_login(self.client, self.teacher_user)
        url = reverse(
            "admin:courses_course_teacher_syllabus", args=[self.course.pk],
        )
        saved = self.client.post(
            url, self.submission_data("save_draft"),
        )
        self.assertEqual(saved.status_code, 302)
        draft = CourseSyllabusSubmission.objects.get(course=self.course)
        self.assertEqual(draft.status, CourseSyllabusSubmission.Status.DRAFT)
        self.assertIsNone(draft.submitted_at)
        page = self.client.get(url)
        self.assertContains(page, "草稿")
        self.assertContains(page, "教師提交的教學目標")
        self.client.post(
            url,
            self.submission_data(
                "submit", self.syllabus_values("正式提交內容"), draft,
            ),
        )
        draft.refresh_from_db()
        self.assertEqual(draft.status, CourseSyllabusSubmission.Status.PENDING)
        self.assertEqual(draft.teaching_target, "正式提交內容")
        self.assertIsNotNone(draft.submitted_at)

    def test_teacher_docx_table_preview_parses_all_weeks_before_submission(self):
        force_admin_login(self.client, self.teacher_user)
        url = reverse(
            "admin:courses_course_teacher_syllabus", args=[self.course.pk],
        )
        upload = SimpleUploadedFile(
            "syllabus.docx", make_table_docx(),
            content_type="application/vnd.openxmlformats-officedocument."
                         "wordprocessingml.document",
        )
        preview = self.client.post(
            url, {"teacher_action": "preview", "file": upload},
        )
        self.assertEqual(preview.status_code, 200)
        self.assertContains(preview, "第十八週")
        parsed = preview.context["form_groups"]["syllabus_form"].initial
        self.assertEqual(parsed["teaching_target"], "理解社群資料分析")
        self.assertEqual(parsed["week_01"], "實作內容 1\n補充內容")
        self.assertEqual(parsed["week_18"], "實作內容 18")
        self.assertEqual(parsed["references"], "教師指定資料")
        self.assertFalse(CourseSyllabusSubmission.objects.exists())

        submitted = self.client.post(
            url, self.submission_data("submit", parsed),
        )
        self.assertEqual(submitted.status_code, 302)
        record = CourseSyllabusSubmission.objects.get(course=self.course)
        self.assertEqual(record.status, CourseSyllabusSubmission.Status.PENDING)
        self.assertEqual(record.weekly_progress["week_18"], "實作內容 18")
        self.assertIsNotNone(record.submitted_at)

    def test_published_version_remains_visible_until_maintainer_publishes(self):
        CourseTeachingSyllabus.objects.create(
            course=self.course,
            teaching_target="目前已發布版本",
            status=CourseTeachingSyllabus.Status.PUBLISHED,
        )
        submission = self.submit_for_review(values=self.syllabus_values(
            "待審核的新版本",
        ))
        self.client.force_login(self.student)
        student_response = self.client.get(
            reverse("course_detail", args=[self.course.pk]),
        )
        self.assertContains(student_response, "目前已發布版本")
        self.assertNotContains(student_response, "待審核的新版本")

        force_admin_login(self.client, self.maintainer)
        review_list = self.client.get(
            reverse("admin:courses_course_syllabus_review"),
        )
        self.assertContains(review_list, self.course.code)
        review_url = reverse(
            "admin:courses_course_syllabus_review_detail",
            args=[submission.pk],
        )
        publish = {
            **self.syllabus_values("助教修正後的新版本"),
            "review_action": "publish",
        }
        response = self.client.post(review_url, publish)
        self.assertEqual(response.status_code, 302)
        submission.refresh_from_db()
        self.assertEqual(submission.status, CourseSyllabusSubmission.Status.PUBLISHED)
        self.assertEqual(submission.reviewed_by, self.maintainer)
        self.assertIsNotNone(submission.reviewed_at)
        published = CourseTeachingSyllabus.objects.get(course=self.course)
        self.assertEqual(published.status, CourseTeachingSyllabus.Status.PUBLISHED)
        self.assertEqual(published.teaching_target, "助教修正後的新版本")

        self.client.force_login(self.student)
        student_response = self.client.get(
            reverse("course_detail", args=[self.course.pk]),
        )
        self.assertContains(student_response, "助教修正後的新版本")
        self.assertNotContains(student_response, "目前已發布版本")

    def test_maintainer_can_return_and_teacher_can_resubmit(self):
        submission = self.submit_for_review()
        force_admin_login(self.client, self.maintainer)
        review_url = reverse(
            "admin:courses_course_syllabus_review_detail",
            args=[submission.pk],
        )
        returned = self.client.post(review_url, {
            **self.syllabus_values(),
            "review_action": "return",
            "return_reason": "請補上第 18 週內容。",
        })
        self.assertEqual(returned.status_code, 302)
        submission.refresh_from_db()
        self.assertEqual(submission.status, CourseSyllabusSubmission.Status.RETURNED)
        self.assertEqual(submission.return_reason, "請補上第 18 週內容。")
        self.assertEqual(submission.reviewed_by, self.maintainer)

        force_admin_login(self.client, self.teacher_user)
        teacher_page = self.client.get(
            reverse("admin:courses_course_teacher_syllabus", args=[self.course.pk]),
        )
        self.assertContains(teacher_page, "請補上第 18 週內容。")
        resubmitted = self.client.post(
            reverse("admin:courses_course_teacher_syllabus", args=[self.course.pk]),
            self.submission_data("submit", self.syllabus_values("補正後內容")),
        )
        self.assertEqual(resubmitted.status_code, 302)
        submission.refresh_from_db()
        self.assertEqual(submission.status, CourseSyllabusSubmission.Status.RETURNED)
        self.assertEqual(submission.return_reason, "請補上第 18 週內容。")
        self.assertEqual(submission.reviewed_by, self.maintainer)
        next_submission = CourseSyllabusSubmission.objects.exclude(
            pk=submission.pk,
        ).get(course=self.course)
        self.assertEqual(
            next_submission.status, CourseSyllabusSubmission.Status.PENDING,
        )
        self.assertEqual(next_submission.teaching_target, "補正後內容")
        self.assertEqual(next_submission.supersedes, submission)
        self.assertIsNone(next_submission.reviewed_by)

    def test_review_queries_and_posts_are_limited_to_maintainer_scope(self):
        in_scope = self.submit_for_review()
        out_of_scope = self.submit_for_review(
            course=self.out_of_scope_course,
        )
        force_admin_login(self.client, self.maintainer)
        listing = self.client.get(
            reverse("admin:courses_course_syllabus_review"),
        )
        self.assertContains(listing, self.course.code)
        self.assertNotContains(listing, self.out_of_scope_course.code)

        denied_url = reverse(
            "admin:courses_course_syllabus_review_detail",
            args=[out_of_scope.pk],
        )
        self.assertEqual(self.client.get(denied_url).status_code, 404)
        denied_post = self.client.post(denied_url, {
            **self.syllabus_values(),
            "review_action": "publish",
        })
        self.assertEqual(denied_post.status_code, 404)
        out_of_scope.refresh_from_db()
        self.assertEqual(out_of_scope.status, CourseSyllabusSubmission.Status.PENDING)
        self.assertIsNone(out_of_scope.reviewed_by)

        allowed_url = reverse(
            "admin:courses_course_syllabus_review_detail", args=[in_scope.pk],
        )
        self.assertEqual(self.client.get(allowed_url).status_code, 200)

    def test_invalid_docx_is_rejected_without_changing_published_version(self):
        published = CourseTeachingSyllabus.objects.create(
            course=self.course, teaching_target="不得覆寫",
            status=CourseTeachingSyllabus.Status.PUBLISHED,
        )
        force_admin_login(self.client, self.teacher_user)
        response = self.client.post(
            reverse("admin:courses_course_teacher_syllabus", args=[self.course.pk]),
            {
                "teacher_action": "preview",
                "file": SimpleUploadedFile("bad.docx", b"not a zip"),
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "無法讀取此 DOCX")
        published.refresh_from_db()
        self.assertEqual(published.teaching_target, "不得覆寫")
        self.assertFalse(CourseSyllabusSubmission.objects.exists())

    def test_docx_entity_declarations_are_rejected(self):
        source = make_table_docx()
        files = {}
        with ZipFile(BytesIO(source)) as package:
            for name in package.namelist():
                files[name] = package.read(name)
        document = files["word/document.xml"]
        files["word/document.xml"] = document.replace(
            b"<w:document",
            b'<!DOCTYPE w:document [<!ENTITY x "unsafe">]><w:document',
            1,
        )
        output = BytesIO()
        with ZipFile(output, "w", ZIP_DEFLATED) as package:
            for name, content in files.items():
                package.writestr(name, content)

        force_admin_login(self.client, self.teacher_user)
        response = self.client.post(
            reverse("admin:courses_course_teacher_syllabus", args=[self.course.pk]),
            {
                "teacher_action": "preview",
                "file": SimpleUploadedFile("unsafe.docx", output.getvalue()),
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "不安全的 XML 宣告")
        self.assertFalse(CourseSyllabusSubmission.objects.exists())
