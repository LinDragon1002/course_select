from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase
from django.urls import reverse

from ..models import (
    AcademicCatalog,
    AcademicClassGroup,
    AcademicDepartment,
    AcademicProgramType,
    Course,
    CourseSyllabusSubmission,
    CourseTeachingSyllabus,
    Enrollment,
    NtubTimetableEntry,
    NtubTimetableSnapshot,
    Section,
    Teacher,
    Term,
    Waitlist,
)
from ..teacher_access import SYLLABUS_TEACHER_GROUP_NAME
from .session_helpers import force_admin_login


class TeacherDashboardTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.teacher_group = Group.objects.create(
            name=SYLLABUS_TEACHER_GROUP_NAME,
        )
        self.teacher_user = user_model.objects.create_user(
            "dashboard-teacher", is_staff=True,
        )
        self.teacher_group.user_set.add(self.teacher_user)
        self.teacher = Teacher.objects.create(
            name="授課教師", user=self.teacher_user,
        )
        self.other_teacher_user = user_model.objects.create_user(
            "other-dashboard-teacher", is_staff=True,
        )
        self.teacher_group.user_set.add(self.other_teacher_user)
        self.other_teacher = Teacher.objects.create(
            name="其他教師", user=self.other_teacher_user,
        )

        self.current_term = Term.objects.create(code="115-1", is_current=True)
        self.old_term = Term.objects.create(code="114-2")
        self.course = self.make_course(self.teacher, self.current_term, "CS101")
        self.second_course = self.make_course(
            self.teacher, self.current_term, "CS102",
        )
        self.other_course = self.make_course(
            self.other_teacher, self.current_term, "CS201",
        )
        self.old_course = self.make_course(
            self.teacher, self.old_term, "CS001",
        )
        self.section = self.make_section(self.course, "A", capacity=2)
        self.second_section = self.make_section(
            self.course, "B", capacity=5,
        )
        self.other_section = self.make_section(self.other_course, "A")
        self.old_section = self.make_section(self.old_course, "A")

        self.students = [
            user_model.objects.create_user(
                f"dashboard-student-{index}",
                first_name=f"學生{index}",
                email=f"student{index}@example.test",
            )
            for index in range(1, 4)
        ]
        Enrollment.objects.create(user=self.students[0], section=self.section)
        active_waitlist = Waitlist.objects.create(
            user=self.students[1], section=self.section,
        )
        promoted_waitlist = Waitlist.objects.create(
            user=self.students[2], section=self.section,
        )
        promoted_waitlist.promoted_at = promoted_waitlist.queued_at
        promoted_waitlist.save(update_fields=["promoted_at"])
        self.active_waitlist = active_waitlist

    @staticmethod
    def make_course(teacher, term, code):
        return Course.objects.create(
            term=term, code=code, name=f"課程 {code}", teacher=teacher,
            credits=3,
        )

    @staticmethod
    def make_section(course, label, capacity=40):
        return Section.objects.create(
            course=course, label=label, day=1, start_period=1, end_period=2,
            room="行政大樓 101", capacity=capacity,
        )

    def login_teacher(self):
        force_admin_login(self.client, self.teacher_user)

    def test_admin_index_shows_owned_dashboard_and_current_term(self):
        self.login_teacher()

        response = self.client.get(reverse("admin:index"))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "admin/courses/teacher_dashboard.html")
        self.assertContains(response, "setInterval(refresh, 10000)")
        self.assertContains(response, "visibilitychange")
        self.assertEqual(response.context["summary"], {
            "course_count": 2,
            "section_count": 2,
            "enrolled_count": 1,
            "waiting_count": 1,
        })

    def test_dashboard_shows_fixed_class_course_without_counting_enrollment(self):
        catalog = AcademicCatalog.objects.create(version="teacher-fixed-schedule")
        program = AcademicProgramType.objects.create(
            catalog=catalog, code="4", name_zh="四技日間部",
        )
        department = AcademicDepartment.objects.create(
            catalog=catalog, program_type=program, code="406",
            name_zh="資訊管理系",
        )
        class_group = AcademicClassGroup.objects.create(
            catalog=catalog, department=department, code="40630B",
            name_zh="資管三年乙班", grade_no=3,
        )
        snapshot = NtubTimetableSnapshot.objects.create(
            class_group=class_group,
            term=self.current_term,
            source_kind=NtubTimetableSnapshot.SourceKind.MAINTAINED,
            status=NtubTimetableSnapshot.Status.PUBLISHED,
            managed_by=self.teacher_user,
        )
        NtubTimetableEntry.objects.create(
            snapshot=snapshot,
            course=self.course,
            entry_type="course",
            course_code=self.course.code,
            course_name=self.course.name,
            teacher=self.teacher,
            teacher_name=self.teacher.name,
            weekday=3,
            start_period=3,
            end_period=4,
            classroom="A4708",
        )
        self.login_teacher()

        response = self.client.get(reverse("admin:index"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "班級固定課程")
        self.assertContains(response, "資管三年乙班")
        self.assertContains(response, "班級固定課程不建立 Enrollment")
        self.assertEqual(response.context["summary"]["enrolled_count"], 1)
        self.assertEqual(Enrollment.objects.count(), 1)
        self.assertContains(response, self.course.code)
        self.assertContains(response, self.second_course.code)
        self.assertNotContains(response, self.other_course.code)
        self.assertNotContains(response, self.old_course.code)
        self.assertContains(
            response,
            reverse(
                "admin:courses_course_teacher_section_roster",
                args=[self.section.pk],
            ),
        )

    def test_historical_term_filter_is_scoped_to_teacher(self):
        self.login_teacher()

        response = self.client.get(
            reverse("admin:courses_course_teacher_syllabi"),
            {"term": self.old_term.pk},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.old_course.code)
        self.assertNotContains(response, self.course.code)
        self.assertEqual(response.context["summary"]["section_count"], 1)

        unrelated_term = Term.objects.create(code="113-1")
        response = self.client.get(
            reverse("admin:courses_course_teacher_syllabi"),
            {"term": unrelated_term.pk},
        )
        self.assertEqual(response.status_code, 404)

    def test_statistics_are_live_per_section_and_exclude_promoted_waitlist(self):
        self.login_teacher()
        endpoint = reverse("admin:courses_course_teacher_stats")

        first_response = self.client.get(endpoint, {"term": self.current_term.pk})
        self.assertEqual(first_response.status_code, 200)
        self.assertIn("no-store", first_response["Cache-Control"])
        self.assertIn("private", first_response["Cache-Control"])
        data = first_response.json()
        self.assertEqual(data["summary"], {
            "course_count": 2,
            "section_count": 2,
            "enrolled_count": 1,
            "waiting_count": 1,
        })
        first_section = next(
            section for section in data["sections"]
            if section["id"] == self.section.pk
        )
        self.assertEqual(first_section, {
            "id": self.section.pk,
            "enrolled": 1,
            "waiting": 1,
            "capacity": 2,
            "remaining": 1,
            "over_capacity": False,
            "progress": 50,
        })

        Enrollment.objects.create(user=self.students[1], section=self.section)
        self.active_waitlist.delete()
        updated_response = self.client.get(
            endpoint, {"term": self.current_term.pk},
        )
        updated = next(
            section for section in updated_response.json()["sections"]
            if section["id"] == self.section.pk
        )
        self.assertEqual(updated["enrolled"], 2)
        self.assertEqual(updated["waiting"], 0)
        self.assertEqual(updated["remaining"], 0)
        self.assertEqual(updated["progress"], 100)
        Enrollment.objects.filter(
            user=self.students[0], section=self.section,
        ).delete()
        dropped_response = self.client.get(
            endpoint, {"term": self.current_term.pk},
        )
        dropped = next(
            section for section in dropped_response.json()["sections"]
            if section["id"] == self.section.pk
        )
        self.assertEqual(dropped["enrolled"], 1)
        self.assertEqual(dropped["remaining"], 1)
        self.assertEqual(
            self.client.post(endpoint).status_code, 405,
        )

    def test_published_syllabus_remains_live_while_update_is_pending(self):
        CourseTeachingSyllabus.objects.create(
            course=self.course,
            teaching_target="已發布版本",
            status=CourseTeachingSyllabus.Status.PUBLISHED,
        )
        CourseSyllabusSubmission.objects.create(
            course=self.course,
            teacher=self.teacher,
            status=CourseSyllabusSubmission.Status.PENDING,
            teaching_target="待審核新版",
        )
        self.login_teacher()

        response = self.client.get(reverse("admin:index"))

        course = next(
            course for course in response.context["courses"]
            if course.pk == self.course.pk
        )
        self.assertEqual(course.syllabus_status, "待助教審核")
        self.assertEqual(
            CourseTeachingSyllabus.objects.get(course=self.course).teaching_target,
            "已發布版本",
        )

    def test_roster_is_read_only_scoped_and_excludes_unneeded_email(self):
        self.login_teacher()
        url = reverse(
            "admin:courses_course_teacher_section_roster",
            args=[self.section.pk],
        )

        response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.students[0].first_name)
        self.assertNotContains(response, self.students[0].email)
        self.assertContains(response, "已選課")
        self.assertNotContains(response, "候補")
        self.assertEqual(
            self.client.post(url, {"action": "drop"}).status_code, 405,
        )
        guessed_section = reverse(
            "admin:courses_course_teacher_section_roster",
            args=[self.other_section.pk],
        )
        self.assertEqual(self.client.get(guessed_section).status_code, 404)

    def test_oauth_frontend_session_does_not_authorize_admin_endpoint(self):
        self.client.force_login(self.teacher_user)

        response = self.client.get(
            reverse("admin:courses_course_teacher_stats"),
            {"term": self.current_term.pk},
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("admin:login"), response["Location"])

    def test_staff_without_teacher_group_cannot_access_dashboard_or_endpoint(self):
        ungrouped = get_user_model().objects.create_user(
            "unapproved-dashboard", is_staff=True,
        )
        Teacher.objects.create(name="未授權教師", user=ungrouped)
        force_admin_login(self.client, ungrouped)
        self.client.raise_request_exception = False

        self.assertEqual(self.client.get(reverse("admin:index")).status_code, 200)
        stats = self.client.get(
            reverse("admin:courses_course_teacher_stats"),
            {"term": self.current_term.pk},
        )
        self.assertEqual(stats.status_code, 403)
        self.assertNotIn(self.course.code, stats.content.decode())
