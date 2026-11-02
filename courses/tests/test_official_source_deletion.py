from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied
from django.test import Client, RequestFactory, TestCase
from django.urls import reverse

from ..maintainer import (
    grant_maintainer_access,
)
from ..models import (
    AcademicCatalog,
    AcademicClassGroup,
    AcademicDepartment,
    AcademicProgramType,
    Course,
    CourseMaintainerScope,
    CourseSyllabusSubmission,
    CourseTeachingSyllabus,
    Enrollment,
    NtubCurriculumRecommendation,
    NtubOfferingAmbiguity,
    NtubOfferingSource,
    NtubRecommendationCandidate,
    NtubTimetableEntry,
    NtubTimetableSnapshot,
    Section,
    Teacher,
    Term,
    Waitlist,
)
from ..teacher_access import SYLLABUS_TEACHER_GROUP_NAME
from .session_helpers import force_admin_login


class OfficialOfferingSourceDeletionTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.root = User.objects.create_superuser(
            "source-cleanup-root", email="root@example.test", password="root-pass",
        )
        self.teacher = Teacher.objects.create(name="歷史授課教師")
        self.term = Term.objects.create(code="116-1")
        catalog = AcademicCatalog.objects.create(version="source-cleanup-test")
        program = AcademicProgramType.objects.create(
            catalog=catalog, code="4", name_zh="四技日間部",
        )
        department = AcademicDepartment.objects.create(
            catalog=catalog, program_type=program, code="406",
            name_zh="資訊管理系",
        )
        self.class_group = AcademicClassGroup.objects.create(
            catalog=catalog, department=department, code="40630B",
            name_zh="資管三年乙班", grade_no=3,
        )
        self.course = Course.objects.create(
            term=self.term, owning_department=department, code="SRC001",
            name="來源清理測試課程", teacher=self.teacher, credits=3,
        )
        self.section = Section.objects.create(
            course=self.course, label="A", day=2, start_period=3,
            end_period=4, capacity=30,
        )
        self.source = self.create_source()
        self.student = User.objects.create_user("source-cleanup-student")
        self.enrollment = Enrollment.objects.create(
            user=self.student, section=self.section,
        )
        self.waitlist = Waitlist.objects.create(
            user=User.objects.create_user("source-cleanup-waiting"),
            section=self.section,
        )
        self.snapshot = NtubTimetableSnapshot.objects.create(
            class_group=self.class_group, term=self.term,
            class_name=self.class_group.name_zh,
            source_kind=NtubTimetableSnapshot.SourceKind.MAINTAINED,
            status=NtubTimetableSnapshot.Status.PUBLISHED,
        )
        self.fixed_entry = NtubTimetableEntry.objects.create(
            snapshot=self.snapshot, course=self.course, teacher=self.teacher,
            entry_type="course", course_code=self.course.code,
            course_name=self.course.name, teacher_name=self.teacher.name,
            weekday=2, start_period=3, end_period=4, periods=[3, 4],
            classroom="B202",
        )
        self.published_syllabus = CourseTeachingSyllabus.objects.create(
            course=self.course,
            teaching_target="已發布版本",
            status=CourseTeachingSyllabus.Status.PUBLISHED,
        )
        self.submission = CourseSyllabusSubmission.objects.create(
            course=self.course, teacher=self.teacher,
            teaching_target="教師提交版本",
            status=CourseSyllabusSubmission.Status.PENDING,
        )
        self.recommendation = NtubCurriculumRecommendation.objects.create(
            admission_year="116", class_code=self.class_group.code,
            course_name=self.course.name, match_status="matched",
            offering=self.source,
        )
        self.candidate = NtubRecommendationCandidate.objects.create(
            recommendation=self.recommendation, offering=self.source,
            offering_key=self.source.source_key, course_code=self.course.code,
        )
        self.ambiguity = NtubOfferingAmbiguity.objects.create(
            kind="offering_without_recommendation",
            message="歷史來源疑義", offering=self.source,
        )
        self.source_admin = admin.site._registry[NtubOfferingSource]

    def create_source(self, key="116|1|40630B|1|SRC001"):
        return NtubOfferingSource.objects.create(
            source_key=key, academic_year="116", semester="1",
            class_code=self.class_group.code, official_serial=key.split("|")[3],
            course_code=self.course.code, term=self.term,
            class_group=self.class_group, course=self.course,
            section=self.section, teacher=self.teacher,
            course_name=self.course.name,
        )

    @staticmethod
    def csrf_token(response, client):
        if "csrftoken" in response.cookies:
            return response.cookies["csrftoken"].value
        return client.cookies["csrftoken"].value

    def assert_source_dependents_preserved(self):
        self.assertTrue(Course.objects.filter(pk=self.course.pk).exists())
        self.assertTrue(Teacher.objects.filter(pk=self.teacher.pk).exists())
        self.assertTrue(Section.objects.filter(pk=self.section.pk).exists())
        self.assertTrue(Enrollment.objects.filter(pk=self.enrollment.pk).exists())
        self.assertTrue(Waitlist.objects.filter(pk=self.waitlist.pk).exists())
        self.assertTrue(NtubTimetableEntry.objects.filter(
            pk=self.fixed_entry.pk,
            snapshot__source_kind=NtubTimetableSnapshot.SourceKind.MAINTAINED,
        ).exists())
        self.assertTrue(CourseTeachingSyllabus.objects.filter(
            pk=self.published_syllabus.pk,
            status=CourseTeachingSyllabus.Status.PUBLISHED,
        ).exists())
        self.assertTrue(CourseSyllabusSubmission.objects.filter(
            pk=self.submission.pk,
        ).exists())
        for related in (self.recommendation, self.candidate, self.ambiguity):
            related.refresh_from_db()
            self.assertIsNone(related.offering_id)

    def test_superuser_can_delete_one_source_after_impact_confirmation(self):
        client = Client(enforce_csrf_checks=True)
        force_admin_login(client, self.root)
        url = reverse(
            "admin:courses_ntubofferingsource_delete", args=[self.source.pk],
        )
        confirmation = client.get(url)
        self.assertEqual(confirmation.status_code, 200)
        self.assertContains(confirmation, "即將刪除 1 筆歷史官方開課來源")
        self.assertContains(confirmation, "1 筆 Enrollment")
        self.assertContains(confirmation, "1 筆 Waitlist")
        self.assertContains(confirmation, "1 份已發布教學大綱")
        self.assertContains(confirmation, "1 筆教師課綱提交")
        self.assertContains(confirmation, "課綱推薦、1 筆候選、1 筆來源疑義")
        self.assertNotContains(confirmation, "PROTECT 關聯會阻擋")

        rejected = client.post(url, {"post": "yes"})
        self.assertEqual(rejected.status_code, 403)
        self.assertTrue(NtubOfferingSource.objects.filter(pk=self.source.pk).exists())

        token = self.csrf_token(confirmation, client)
        deleted = client.post(
            url, {"post": "yes", "csrfmiddlewaretoken": token},
        )
        self.assertEqual(deleted.status_code, 302)
        self.assertFalse(NtubOfferingSource.objects.filter(pk=self.source.pk).exists())
        self.assert_source_dependents_preserved()

    def test_superuser_can_batch_delete_sources_with_impact_confirmation(self):
        second = self.create_source("116|1|40630B|2|SRC001")
        client = Client(enforce_csrf_checks=True)
        force_admin_login(client, self.root)
        changelist = reverse("admin:courses_ntubofferingsource_changelist")
        listing = client.get(changelist)
        token = self.csrf_token(listing, client)
        request = client.post(changelist, {
            "action": "delete_selected",
            "_selected_action": [self.source.pk, second.pk],
            "index": "0",
            "csrfmiddlewaretoken": token,
        })
        self.assertEqual(request.status_code, 200)
        self.assertContains(request, "即將刪除 2 筆歷史官方開課來源")
        self.assertContains(request, "1 門 Course")
        self.assertContains(request, "1 筆 Enrollment")
        self.assertContains(request, 'name="post" value="yes"')

        confirmed = client.post(changelist, {
            "action": "delete_selected",
            "_selected_action": [self.source.pk, second.pk],
            "post": "yes",
            "csrfmiddlewaretoken": token,
        })
        self.assertEqual(confirmed.status_code, 302)
        self.assertFalse(NtubOfferingSource.objects.filter(
            pk__in=[self.source.pk, second.pk],
        ).exists())
        self.assert_source_dependents_preserved()

    def test_non_superusers_cannot_delete_or_use_batch_cleanup(self):
        User = get_user_model()
        maintainer = User.objects.create_user(
            "source-cleanup-maintainer", is_staff=True,
        )
        grant_maintainer_access(maintainer)
        CourseMaintainerScope.objects.create(
            user=maintainer, department=self.class_group.department,
        )
        teacher_user = User.objects.create_user(
            "source-cleanup-teacher", is_staff=True,
        )
        teacher_user.groups.add(
            Group.objects.create(name=SYLLABUS_TEACHER_GROUP_NAME),
        )

        for user in (maintainer, teacher_user):
            request = RequestFactory().get("/admin/courses/ntubofferingsource/")
            request.user = user
            self.assertFalse(self.source_admin.has_delete_permission(
                request, self.source,
            ))
            self.assertNotIn("delete_selected", self.source_admin.get_actions(request))
            with self.assertRaises(PermissionDenied):
                self.source_admin.delete_queryset(
                    request, NtubOfferingSource.objects.filter(pk=self.source.pk),
                )
        self.assertTrue(NtubOfferingSource.objects.filter(pk=self.source.pk).exists())

        force_admin_login(self.client, maintainer)
        response = self.client.post(
            reverse("admin:courses_ntubofferingsource_delete", args=[self.source.pk]),
            {"post": "yes"},
        )
        self.assertIn(response.status_code, (403, 404))
        self.assertTrue(NtubOfferingSource.objects.filter(pk=self.source.pk).exists())
        student_client = Client()
        response = student_client.get(
            reverse("admin:courses_ntubofferingsource_changelist"),
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(NtubOfferingSource.objects.filter(pk=self.source.pk).exists())

    def test_source_admin_shows_history_tools_and_superuser_delete_action(self):
        client = Client()
        force_admin_login(client, self.root)
        response = client.get(reverse("admin:courses_ntubofferingsource_changelist"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "歷史匯入工具：批次補抓官方教學大綱")
        self.assertContains(response, "目前日常班級固定課表")
        request = RequestFactory().get("/admin/courses/ntubofferingsource/")
        request.user = self.root
        self.assertIn("delete_selected", self.source_admin.get_actions(request))
