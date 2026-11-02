from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from ..maintainer import grant_maintainer_access
from ..models import (
    AcademicCatalog, AcademicDepartment, AcademicProgramType, Course,
    CourseMaintainerScope, CourseTeachingSyllabus, Teacher, Term,
)
from .session_helpers import force_admin_login


WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
DOCX_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument."
    "wordprocessingml.document.main+xml"
)


def make_docx(*paragraphs):
    body = "".join(
        f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>"
        for text in paragraphs
    )
    document = (
        f'<w:document xmlns:w="{WORD_NS}"><w:body>{body}</w:body></w:document>'
    )
    content_types = (
        f'<Types><Override ContentType="{DOCX_CONTENT_TYPE}"/></Types>'
    )
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as package:
        package.writestr("[Content_Types].xml", content_types)
        package.writestr("word/document.xml", document)
    return output.getvalue()


class CourseTeachingSyllabusTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.term = Term.objects.create(code="115-1", is_current=True)
        self.teacher = Teacher.objects.create(name="測試教師")
        self.catalog = AcademicCatalog.objects.create(version="manual-syllabus")
        self.program = AcademicProgramType.objects.create(
            catalog=self.catalog, code="4", name_zh="四技日間部",
        )
        self.department = AcademicDepartment.objects.create(
            catalog=self.catalog, program_type=self.program,
            code="406", name_zh="資訊管理系",
        )
        self.other_department = AcademicDepartment.objects.create(
            catalog=self.catalog, program_type=self.program,
            code="500", name_zh="其他系所",
        )
        self.course = self.make_course("CS001", self.department)
        self.admin_user = user_model.objects.create_superuser(
            "syllabus-admin", "syllabus-admin@example.com", "password",
        )
        self.student = user_model.objects.create_user("syllabus-student")

    def make_course(self, code, department, *, syllabus=""):
        return Course.objects.create(
            term=self.term, owning_department=department, code=code,
            name="測試課程", teacher=self.teacher, credits=3, syllabus=syllabus,
        )

    def admin_add_data(self, code, department, *, syllabus_values=None):
        data = {
            "term": self.term.pk,
            "owning_department": department.pk,
            "code": code,
            "name": "測試課程",
            "teacher": self.teacher.pk,
            "credits": 3,
            "syllabus": "",
            "prerequisites": [],
            "_save": "Save",
            "teaching_syllabus-TOTAL_FORMS": "1",
            "teaching_syllabus-INITIAL_FORMS": "0",
            "teaching_syllabus-MIN_NUM_FORMS": "0",
            "teaching_syllabus-MAX_NUM_FORMS": "1",
            "teaching_syllabus-0-id": "",
        }
        for field in (
            "teaching_target", "teaching_matter", "assessment", "references",
            *(f"week_{week:02d}" for week in range(1, 19)),
        ):
            data[f"teaching_syllabus-0-{field}"] = (
                (syllabus_values or {}).get(field, "")
            )
        return data

    def make_maintainer(self):
        user = get_user_model().objects.create_user(
            "course-maintainer", "maintainer@example.com",
        )
        CourseMaintainerScope.objects.create(user=user, department=self.department)
        grant_maintainer_access(user)
        return user

    def syllabus_change_data(self, syllabus, action):
        data = {
            "term": self.term.pk,
            "owning_department": self.department.pk,
            "code": self.course.code,
            "name": self.course.name,
            "teacher": self.teacher.pk,
            "credits": self.course.credits,
            "syllabus": "",
            "prerequisites": [],
            action: "1",
            "teaching_syllabus-TOTAL_FORMS": "1",
            "teaching_syllabus-INITIAL_FORMS": "1",
            "teaching_syllabus-MIN_NUM_FORMS": "0",
            "teaching_syllabus-MAX_NUM_FORMS": "1",
            "teaching_syllabus-0-id": str(syllabus.pk),
            "teaching_syllabus-0-teaching_target": syllabus.teaching_target,
            "teaching_syllabus-0-teaching_matter": syllabus.teaching_matter,
            "teaching_syllabus-0-assessment": syllabus.assessment,
            "teaching_syllabus-0-references": syllabus.references,
        }
        data.update({
            f"teaching_syllabus-0-week_{week:02d}":
                syllabus.weekly_progress.get(f"week_{week:02d}", "")
            for week in range(1, 19)
        })
        return data

    def test_admin_can_create_course_with_draft_and_all_week_fields(self):
        maintainer = self.make_maintainer()
        force_admin_login(self.client, maintainer)
        data = self.admin_add_data("CS002", self.department, syllabus_values={
            "teaching_target": "理解資料分析",
            "week_01": "課程介紹",
            "week_18": "成果發表",
        })
        response = self.client.post(reverse("admin:courses_course_add"), data)
        self.assertEqual(response.status_code, 302)
        course = Course.objects.get(code="CS002")
        syllabus = course.teaching_syllabus
        self.assertEqual(syllabus.status, CourseTeachingSyllabus.Status.DRAFT)
        self.assertEqual(syllabus.teaching_target, "理解資料分析")
        self.assertEqual(syllabus.weekly_progress["week_01"], "課程介紹")
        self.assertEqual(syllabus.weekly_progress["week_18"], "成果發表")
        self.assertEqual(syllabus.updated_by, maintainer)
        self.assertEqual(course.created_by, maintainer)
        edit_response = self.client.get(
            reverse("admin:courses_course_change", args=[course.pk]),
        )
        self.assertEqual(edit_response.status_code, 200)
        inline_form = edit_response.context["inline_admin_formsets"][0].formset.forms[0]
        self.assertEqual(inline_form["week_01"].value(), "課程介紹")
        self.assertEqual(inline_form["week_18"].value(), "成果發表")

    def test_admin_has_explicit_publish_action(self):
        maintainer = self.make_maintainer()
        force_admin_login(self.client, maintainer)
        syllabus = CourseTeachingSyllabus.objects.create(
            course=self.course, teaching_target="已完成內容",
        )
        response = self.client.get(reverse(
            "admin:courses_course_change", args=[self.course.pk],
        ))
        self.assertContains(response, "確認並發布課綱")

        response = self.client.post(
            reverse("admin:courses_course_change", args=[self.course.pk]),
            self.syllabus_change_data(syllabus, "_publish_syllabus"),
        )
        self.assertEqual(response.status_code, 302)
        syllabus.refresh_from_db()
        self.assertEqual(syllabus.status, CourseTeachingSyllabus.Status.PUBLISHED)
        self.assertEqual(syllabus.updated_by, maintainer)

    def test_published_syllabus_requires_explicit_publish_to_change(self):
        maintainer = self.make_maintainer()
        force_admin_login(self.client, maintainer)
        syllabus = CourseTeachingSyllabus.objects.create(
            course=self.course, teaching_target="原發布內容",
            status=CourseTeachingSyllabus.Status.PUBLISHED,
        )
        data = self.syllabus_change_data(syllabus, "_save")
        data["teaching_syllabus-0-teaching_target"] = "未明確發布的修改"
        response = self.client.post(
            reverse("admin:courses_course_change", args=[self.course.pk]), data,
        )
        self.assertEqual(response.status_code, 302)
        syllabus.refresh_from_db()
        self.assertEqual(syllabus.teaching_target, "原發布內容")
        self.assertEqual(syllabus.status, CourseTeachingSyllabus.Status.PUBLISHED)
        self.assertIn("已發布課綱未被修改", str(list(response.wsgi_request._messages)))

    def test_docx_preview_requires_confirmation_and_saves_draft(self):
        force_admin_login(self.client, self.admin_user)
        url = reverse(
            "admin:courses_course_import_syllabus", args=[self.course.pk],
        )
        upload = SimpleUploadedFile(
            "syllabus.docx",
            make_docx(
                "教學目標：理解資料分析",
                "教學內容：資料整理與分析",
                "第1週：課程介紹",
                "成績評量：期末報告",
                "指定教科書及參考書籍：未指定",
            ),
            content_type="application/vnd.openxmlformats-officedocument."
                         "wordprocessingml.document",
        )
        preview = self.client.post(
            url, {"import_action": "preview", "file": upload},
        )
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview.context["phase"], "preview")
        self.assertContains(preview, "第十八週")
        self.assertIn("week_18", preview.context["unresolved_fields"])
        self.assertFalse(CourseTeachingSyllabus.objects.filter(
            course=self.course,
        ).exists())

        form = preview.context["syllabus_form"]
        confirm_data = {"import_action": "confirm"}
        confirm_data.update({
            field_name: form.initial.get(field_name, "")
            for field_name in form.fields
        })
        saved = self.client.post(url, confirm_data)
        self.assertEqual(saved.status_code, 302)
        syllabus = self.course.teaching_syllabus
        self.assertEqual(syllabus.status, CourseTeachingSyllabus.Status.DRAFT)
        self.assertEqual(syllabus.weekly_progress["week_01"], "課程介紹")
        self.assertEqual(syllabus.assessment, "期末報告")

    def test_invalid_upload_is_reported_without_creating_syllabus(self):
        force_admin_login(self.client, self.admin_user)
        url = reverse("admin:courses_course_import_syllabus", args=[self.course.pk])
        response = self.client.post(
            url,
            {
                "import_action": "preview",
                "file": SimpleUploadedFile("bad.docx", b"not a zip"),
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "無法讀取此 DOCX")
        self.assertFalse(CourseTeachingSyllabus.objects.filter(
            course=self.course,
        ).exists())
        pdf_response = self.client.post(
            url,
            {
                "import_action": "preview",
                "file": SimpleUploadedFile("syllabus.pdf", b"not a PDF"),
            },
        )
        self.assertContains(pdf_response, "僅接受 .docx")
        self.assertFalse(CourseTeachingSyllabus.objects.filter(
            course=self.course,
        ).exists())

    def test_published_syllabus_cannot_be_overwritten_by_import(self):
        force_admin_login(self.client, self.admin_user)
        syllabus = CourseTeachingSyllabus.objects.create(
            course=self.course,
            teaching_target="原已發布內容",
            status=CourseTeachingSyllabus.Status.PUBLISHED,
        )
        url = reverse(
            "admin:courses_course_import_syllabus", args=[self.course.pk],
        )
        response = self.client.post(url, {"import_action": "confirm"})
        self.assertEqual(response.status_code, 409)
        syllabus.refresh_from_db()
        self.assertEqual(syllabus.teaching_target, "原已發布內容")

    def test_student_view_only_shows_published_syllabus_and_legacy_fallback(self):
        self.client.force_login(self.student)
        self.course.syllabus = "舊版課綱文字"
        self.course.save(update_fields=["syllabus"])
        draft = CourseTeachingSyllabus.objects.create(
            course=self.course, teaching_target="不可公開的草稿",
        )
        detail_url = reverse("course_detail", args=[self.course.pk])
        draft_response = self.client.get(detail_url)
        self.assertContains(draft_response, "舊版課綱文字")
        self.assertNotContains(draft_response, "不可公開的草稿")
        self.assertEqual(draft_response.context["teaching_syllabus"], None)

        draft.status = CourseTeachingSyllabus.Status.PUBLISHED
        draft.teaching_target = "已發布教學目標"
        draft.weekly_progress = {"week_01": "第一週內容"}
        draft.save()
        published_response = self.client.get(detail_url)
        self.assertContains(published_response, "已發布教學目標")
        self.assertContains(published_response, "第一週內容")
        self.assertEqual(len(published_response.context["teaching_syllabus_weeks"]), 18)
        self.assertContains(published_response, "指定教科書及參考書籍")

    def test_draft_without_legacy_text_shows_not_published_message(self):
        self.client.force_login(self.student)
        CourseTeachingSyllabus.objects.create(
            course=self.course, teaching_target="草稿內容",
        )
        response = self.client.get(reverse("course_detail", args=[self.course.pk]))
        self.assertContains(response, "教學大綱尚未公布")
        self.assertNotContains(response, "草稿內容")

    def test_maintainer_cannot_import_course_outside_scoped_department(self):
        user = get_user_model().objects.create_user(
            "scoped-maintainer", "maintainer@example.com", is_staff=True,
        )
        CourseMaintainerScope.objects.create(
            user=user, department=self.department,
        )
        grant_maintainer_access(user)
        force_admin_login(self.client, user)
        other_course = self.make_course("OTHER001", self.other_department)
        url = reverse(
            "admin:courses_course_import_syllabus", args=[other_course.pk],
        )
        response = self.client.post(
            url, {"import_action": "confirm", "teaching_target": "偽造"},
        )
        self.assertEqual(response.status_code, 404)
        self.assertFalse(CourseTeachingSyllabus.objects.filter(
            course=other_course,
        ).exists())
