import re

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied
from django.test import RequestFactory, TestCase
from django.urls import reverse

from ..maintainer import grant_maintainer_access
from ..models import (
    AcademicCatalog,
    AcademicClassGroup,
    AcademicDepartment,
    AcademicProgramType,
    Course,
    CourseMaintainerScope,
    CourseTeachingSyllabus,
    Enrollment,
    NtubTimetableEntry,
    NtubTimetableSnapshot,
    OfficialTeachingSyllabus,
    Section,
    StudentAcademicProfile,
    Teacher,
    Term,
    Waitlist,
)
from ..ntub_timetable_import import import_payload
from ..teacher_access import SYLLABUS_TEACHER_GROUP_NAME
from .session_helpers import force_admin_login


class FixedScheduleManagementTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.admin_user = User.objects.create_superuser(
            "fixed-schedule-root", email="root@example.test", password="test-pass",
        )
        self.maintainer = User.objects.create_user(
            "fixed-schedule-maintainer", password="test-pass",
        )
        grant_maintainer_access(self.maintainer)
        self.term = Term.objects.create(code="116-1", is_current=True)
        self.teacher = Teacher.objects.create(name="王老師")
        catalog = AcademicCatalog.objects.create(version="fixed-schedule-test")
        program = AcademicProgramType.objects.create(
            catalog=catalog, code="4", name_zh="四技日間部",
        )
        self.department = AcademicDepartment.objects.create(
            catalog=catalog, program_type=program, code="406",
            name_zh="資訊管理系",
        )
        self.other_department = AcademicDepartment.objects.create(
            catalog=catalog, program_type=program, code="500",
            name_zh="其他系所",
        )
        self.class_group = AcademicClassGroup.objects.create(
            catalog=catalog, department=self.department, code="40630B",
            name_zh="資管三年乙班", grade_no=3,
        )
        self.other_class_group = AcademicClassGroup.objects.create(
            catalog=catalog, department=self.other_department, code="50030A",
            name_zh="他系三年甲班", grade_no=3,
        )
        CourseMaintainerScope.objects.create(
            user=self.maintainer, department=self.department,
        )
        self.admin_client = self.client_class()
        force_admin_login(self.admin_client, self.maintainer)
        self.student_client = self.client_class()
        self.student = User.objects.create_user("fixed-schedule-student")
        self.student_client.force_login(self.student)
        StudentAcademicProfile.objects.create(
            user=self.student, class_group=self.class_group,
        )
        self.snapshot = NtubTimetableSnapshot.objects.create(
            class_group=self.class_group,
            term=self.term,
            class_name=self.class_group.name_zh,
        )
        self.imported_entry = NtubTimetableEntry.objects.create(
            snapshot=self.snapshot,
            entry_type="course",
            course_code="FIX100",
            official_serial="9",
            course_name="原匯入課程",
            teacher_name="舊資料教師",
            classroom="A101",
            weekday=1,
            start_period=1,
            end_period=2,
            periods=[1, 2],
        )

    def url(self, name, *args):
        return reverse(f"admin:courses_ntubtimetablesnapshot_{name}", args=args)

    def takeover(self, class_group=None):
        group = class_group or self.class_group
        response = self.admin_client.post(
            self.url(
                "fixed_schedule_takeover", group.pk, self.term.pk,
            ),
        )
        self.assertEqual(response.status_code, 302)
        return NtubTimetableSnapshot.objects.get(
            class_group=group,
            term=self.term,
            source_kind=NtubTimetableSnapshot.SourceKind.MAINTAINED,
            status=NtubTimetableSnapshot.Status.DRAFT,
        )

    def add_valid_course_entry(self, snapshot, code="FIX200", name="有效固定課程"):
        course = Course.objects.create(
            term=self.term,
            owning_department=snapshot.class_group.department,
            code=code,
            name=name,
            teacher=self.teacher,
            credits=3,
        )
        return NtubTimetableEntry.objects.create(
            snapshot=snapshot,
            course=course,
            entry_type="course",
            course_code=code,
            course_name=name,
            teacher=self.teacher,
            teacher_name=self.teacher.name,
            weekday=2,
            start_period=3,
            end_period=4,
            periods=[3, 4],
            classroom="B202",
        )

    def create_root_draft(self, class_group):
        return NtubTimetableSnapshot.objects.create(
            class_group=class_group,
            term=self.term,
            class_name=class_group.name_zh,
            source_kind=NtubTimetableSnapshot.SourceKind.MAINTAINED,
            status=NtubTimetableSnapshot.Status.DRAFT,
            managed_by=self.admin_user,
        )

    def test_maintainer_can_take_over_and_publish_without_mutating_import(self):
        draft = self.takeover()
        copied = draft.entries.get(course_code="FIX100")
        self.assertEqual(copied.course_name, "原匯入課程")
        self.assertEqual(self.imported_entry.course_name, "原匯入課程")
        self.assertEqual(self.snapshot.status, NtubTimetableSnapshot.Status.PUBLISHED)

        import_payload({
            "classes": [{
                "academic_year": "116",
                "semester": "1",
                "term_code": self.term.code,
                "class_code": self.class_group.code,
                "class_name": self.class_group.name_zh,
                "counts": {"total": 1, "course": 1, "activity": 0},
                "entries": [{
                    "entry_type": "course",
                    "weekday": 1,
                    "start_period": 1,
                    "end_period": 2,
                    "periods": [1, 2],
                    "course": {
                        "code": "FIX100",
                        "serial": "9",
                        "name": "同步更新課程",
                    },
                    "source": {},
                }],
            }],
        })
        copied.refresh_from_db()
        self.imported_entry.refresh_from_db()
        self.assertEqual(copied.course_name, "原匯入課程")
        self.assertEqual(self.imported_entry.course_name, "同步更新課程")

        edit_url = self.url(
            "fixed_schedule_entry_change", self.class_group.pk, self.term.pk,
            copied.pk,
        )
        response = self.admin_client.post(edit_url, {
            "entry_type": "course",
            "course_code": "FIX100",
            "course_name": "人工維護課程",
            "teacher": self.teacher.pk,
            "teacher_name": self.teacher.name,
            "credits": 3,
            "weekly_hours": "3.00",
            "course_category": "必修",
            "weekday": 2,
            "start_period": 3,
            "end_period": 4,
            "classroom": "B202",
            "is_active": "on",
        })
        self.assertEqual(response.status_code, 302)
        copied.refresh_from_db()
        self.assertEqual(copied.course_name, "人工維護課程")
        self.assertEqual(copied.classroom, "B202")
        self.assertEqual(copied.teacher_id, self.teacher.pk)
        self.assertEqual(copied.course.term_id, self.term.pk)
        self.assertEqual(copied.course.owning_department_id, self.department.pk)

        response = self.admin_client.post(
            self.url("fixed_schedule_publish", self.class_group.pk, self.term.pk),
        )
        self.assertEqual(response.status_code, 302)
        draft.refresh_from_db()
        self.assertEqual(draft.status, NtubTimetableSnapshot.Status.PUBLISHED)
        self.assertTrue(
            NtubTimetableSnapshot.objects.filter(
                pk=self.snapshot.pk,
                source_kind=NtubTimetableSnapshot.SourceKind.IMPORTED,
            ).exists(),
        )

        response = self.student_client.get(reverse("my_schedule"))
        self.assertContains(response, "人工維護課程")
        self.assertContains(response, "B202")
        self.assertNotContains(response, "原匯入課程")
        self.assertEqual(response.context["snapshot"].pk, draft.pk)
        self.assertEqual(Enrollment.objects.count(), 0)
        self.assertEqual(Section.objects.count(), 0)

    def test_published_fixed_course_uses_existing_html_syllabus(self):
        draft = self.takeover()
        entry = draft.entries.get(course_code="FIX100")
        response = self.admin_client.post(
            self.url(
                "fixed_schedule_entry_change", self.class_group.pk, self.term.pk,
                entry.pk,
            ),
            {
                "entry_type": "course",
                "course_code": "FIX100",
                "course_name": "人工維護課程",
                "teacher": self.teacher.pk,
                "teacher_name": self.teacher.name,
                "credits": 3,
                "weekly_hours": "3.00",
                "course_category": "必修",
                "weekday": 2,
                "start_period": 3,
                "end_period": 4,
                "classroom": "B202",
                "is_active": "on",
            },
        )
        self.assertEqual(response.status_code, 302)
        entry.refresh_from_db()
        CourseTeachingSyllabus.objects.create(
            course=entry.course,
            teaching_target="學生理解課程目標",
            teaching_matter="課程內容",
            weekly_progress={"week_01": "第一週課程"},
            assessment="作業與考試",
            references="參考書目",
            status=CourseTeachingSyllabus.Status.PUBLISHED,
        )
        self.admin_client.post(
            self.url("fixed_schedule_publish", self.class_group.pk, self.term.pk),
        )

        response = self.student_client.get(
            reverse("official_course_detail", args=[entry.pk]),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "學生理解課程目標")
        self.assertContains(response, "第一週課程")
        self.assertContains(response, "3 學分")

    def test_maintainer_cannot_guess_other_department_class_or_post(self):
        detail = self.url(
            "fixed_schedule_detail", self.other_class_group.pk, self.term.pk,
        )
        self.assertEqual(self.admin_client.get(detail).status_code, 404)
        response = self.admin_client.post(
            self.url(
                "fixed_schedule_takeover",
                self.other_class_group.pk, self.term.pk,
            ),
        )
        self.assertEqual(response.status_code, 404)
        self.assertFalse(NtubTimetableSnapshot.objects.filter(
            class_group=self.other_class_group,
            source_kind=NtubTimetableSnapshot.SourceKind.MAINTAINED,
        ).exists())

    def test_superuser_can_manage_a_class_outside_maintainer_scope(self):
        root_client = self.client_class()
        force_admin_login(root_client, self.admin_user)
        response = root_client.get(self.url(
            "fixed_schedule_detail", self.other_class_group.pk, self.term.pk,
        ))
        self.assertEqual(response.status_code, 200)
        response = root_client.post(self.url(
            "fixed_schedule_takeover", self.other_class_group.pk, self.term.pk,
        ))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(NtubTimetableSnapshot.objects.filter(
            class_group=self.other_class_group,
            source_kind=NtubTimetableSnapshot.SourceKind.MAINTAINED,
            status=NtubTimetableSnapshot.Status.DRAFT,
        ).exists())

    def test_superuser_can_publish_nonempty_draft_and_list_has_operations(self):
        root_client = self.client_class()
        force_admin_login(root_client, self.admin_user)
        draft = self.create_root_draft(self.other_class_group)
        self.add_valid_course_entry(draft)

        response = root_client.post(
            self.url(
                "fixed_schedule_publish",
                self.other_class_group.pk,
                self.term.pk,
            ),
        )

        self.assertEqual(response.status_code, 302)
        draft.refresh_from_db()
        self.assertEqual(draft.status, NtubTimetableSnapshot.Status.PUBLISHED)
        self.assertIsNotNone(draft.published_at)
        listing = root_client.get(reverse("admin:courses_ntubtimetablesnapshot_changelist"))
        self.assertEqual(listing.status_code, 200)
        self.assertContains(listing, "版本來源")
        self.assertContains(listing, "發布狀態")
        self.assertContains(listing, "撤回發布")
        self.assertContains(listing, "安全刪除")
        self.assertContains(listing, 'id="fixed-schedule-row-action"')
        self.assertContains(listing, 'form="fixed-schedule-row-action"')

    def test_empty_draft_cannot_be_published(self):
        root_client = self.client_class()
        force_admin_login(root_client, self.admin_user)
        draft = self.create_root_draft(self.other_class_group)

        response = root_client.post(
            self.url(
                "fixed_schedule_publish",
                self.other_class_group.pk,
                self.term.pk,
            ),
            follow=True,
        )

        draft.refresh_from_db()
        self.assertEqual(draft.status, NtubTimetableSnapshot.Status.DRAFT)
        self.assertContains(response, "至少需要一筆有效課程安排")

    def test_withdraw_archives_imported_fallback_and_does_not_restore_it(self):
        root_client = self.client_class()
        force_admin_login(root_client, self.admin_user)
        draft = self.takeover()
        draft.entries.filter(entry_type="course").update(is_active=False)
        self.add_valid_course_entry(draft)
        root_client.post(
            self.url("fixed_schedule_publish", self.class_group.pk, self.term.pk),
        )

        response = root_client.post(
            self.url("fixed_schedule_withdraw", draft.pk),
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        draft.refresh_from_db()
        self.snapshot.refresh_from_db()
        self.assertEqual(draft.status, NtubTimetableSnapshot.Status.ARCHIVED)
        self.assertEqual(self.snapshot.status, NtubTimetableSnapshot.Status.ARCHIVED)
        student_schedule = self.student_client.get(reverse("my_schedule"))
        self.assertIsNone(student_schedule.context["snapshot"])
        self.assertContains(response, "不會自動回退")

    def test_revision_keeps_student_version_until_new_revision_is_published(self):
        first_draft = self.takeover()
        first_draft.entries.filter(entry_type="course").update(is_active=False)
        first_entry = self.add_valid_course_entry(
            first_draft, code="FIX200", name="第一版固定課程",
        )
        self.admin_client.post(
            self.url("fixed_schedule_publish", self.class_group.pk, self.term.pk),
        )
        original_student_view = self.student_client.get(reverse("my_schedule"))
        self.assertContains(original_student_view, "第一版固定課程")

        self.admin_client.post(
            self.url("fixed_schedule_takeover", self.class_group.pk, self.term.pk),
        )
        second_draft = NtubTimetableSnapshot.objects.get(
            class_group=self.class_group,
            term=self.term,
            source_kind=NtubTimetableSnapshot.SourceKind.MAINTAINED,
            status=NtubTimetableSnapshot.Status.DRAFT,
        )
        copied_entry = second_draft.entries.get(course_code="FIX200")
        self.assertNotEqual(copied_entry.pk, first_entry.pk)
        changed = self.admin_client.post(
            self.url(
                "fixed_schedule_entry_change", self.class_group.pk, self.term.pk,
                copied_entry.pk,
            ),
            {
                "entry_type": "course",
                "course_code": "FIX200",
                "course_name": "第二版固定課程",
                "teacher": self.teacher.pk,
                "teacher_name": self.teacher.name,
                "credits": 3,
                "weekly_hours": "3.00",
                "course_category": "必修",
                "weekday": 3,
                "start_period": 5,
                "end_period": 6,
                "classroom": "B303",
                "is_active": "on",
            },
        )
        self.assertEqual(changed.status_code, 302)
        still_original = self.student_client.get(reverse("my_schedule"))
        self.assertContains(still_original, "第一版固定課程")
        self.assertNotContains(still_original, "第二版固定課程")

        self.admin_client.post(
            self.url("fixed_schedule_publish", self.class_group.pk, self.term.pk),
        )

        first_draft.refresh_from_db()
        second_draft.refresh_from_db()
        self.assertEqual(first_draft.status, NtubTimetableSnapshot.Status.ARCHIVED)
        self.assertEqual(second_draft.status, NtubTimetableSnapshot.Status.PUBLISHED)
        self.assertEqual(
            NtubTimetableSnapshot.objects.filter(
                class_group=self.class_group,
                term=self.term,
                source_kind=NtubTimetableSnapshot.SourceKind.MAINTAINED,
                status=NtubTimetableSnapshot.Status.PUBLISHED,
            ).count(),
            1,
        )
        latest_student_view = self.student_client.get(reverse("my_schedule"))
        self.assertContains(latest_student_view, "第二版固定課程")
        self.assertNotContains(latest_student_view, "第一版固定課程")

    def test_teacher_and_student_cannot_manage_fixed_schedule(self):
        draft = self.takeover()
        User = get_user_model()
        teacher_user = User.objects.create_user(
            "fixed-schedule-teacher", is_staff=True, password="test-pass",
        )
        teacher_user.groups.add(
            Group.objects.create(name=SYLLABUS_TEACHER_GROUP_NAME),
        )
        self.teacher.user = teacher_user
        self.teacher.save(update_fields=["user"])
        teacher_client = self.client_class()
        force_admin_login(teacher_client, teacher_user)
        detail_url = self.url(
            "fixed_schedule_detail", self.class_group.pk, self.term.pk,
        )
        publish_url = self.url(
            "fixed_schedule_publish", self.class_group.pk, self.term.pk,
        )

        self.assertEqual(teacher_client.get(detail_url).status_code, 403)
        self.assertEqual(teacher_client.post(publish_url).status_code, 403)
        self.assertIn(self.student_client.get(detail_url).status_code, (302, 403))
        draft.refresh_from_db()
        self.assertEqual(draft.status, NtubTimetableSnapshot.Status.DRAFT)

    def test_superuser_delete_of_published_version_prevents_fallback(self):
        root_client = self.client_class()
        force_admin_login(root_client, self.admin_user)
        draft = self.takeover()
        draft.entries.filter(entry_type="course").update(is_active=False)
        self.add_valid_course_entry(draft)
        root_client.post(
            self.url("fixed_schedule_publish", self.class_group.pk, self.term.pk),
        )
        delete_url = reverse(
            "admin:courses_ntubtimetablesnapshot_delete",
            args=[draft.pk],
        )
        confirmation = root_client.get(delete_url)
        csrf_token = re.search(
            rb'name="csrfmiddlewaretoken" value="([^"]+)"',
            confirmation.content,
        ).group(1).decode()

        response = root_client.post(
            delete_url,
            {"post": "yes", "csrfmiddlewaretoken": csrf_token},
            HTTP_REFERER=f"http://testserver{delete_url}",
        )

        self.assertEqual(response.status_code, 302)
        self.snapshot.refresh_from_db()
        self.assertEqual(self.snapshot.status, NtubTimetableSnapshot.Status.ARCHIVED)
        student_schedule = self.student_client.get(reverse("my_schedule"))
        self.assertIsNone(student_schedule.context["snapshot"])

    def test_superuser_delete_confirmation_describes_impact_and_preserves_related_records(self):
        root_client = self.client_class()
        force_admin_login(root_client, self.admin_user)
        course = Course.objects.create(
            term=self.term,
            owning_department=self.department,
            code="KEEP100",
            name="保留的自主課程",
            teacher=self.teacher,
            credits=3,
        )
        section = Section.objects.create(
            course=course,
            label="A",
            day=1,
            start_period=1,
            end_period=2,
            room="A101",
            capacity=30,
        )
        enrollment = Enrollment.objects.create(user=self.student, section=section)
        waitlist = Waitlist.objects.create(
            user=get_user_model().objects.create_user("delete-impact-waitlist"),
            section=section,
        )
        self.imported_entry.course = course
        self.imported_entry.teacher = self.teacher
        self.imported_entry.save(update_fields=["course", "teacher"])
        syllabus = OfficialTeachingSyllabus.objects.create(
            academic_year="116",
            semester="1",
            class_code=self.class_group.code,
            official_serial="9",
            course_code="FIX100",
            snapshot=self.snapshot,
            status="success",
        )
        delete_url = reverse(
            "admin:courses_ntubtimetablesnapshot_delete",
            args=[self.snapshot.pk],
        )

        confirmation = root_client.get(delete_url)

        self.assertEqual(confirmation.status_code, 200)
        self.assertFalse(confirmation.context["perms_lacking"])
        self.assertFalse(confirmation.context["protected"])
        self.assertEqual(confirmation.context["snapshot_syllabus_count"], 1)
        self.assertContains(confirmation, "1 筆課表明細")
        self.assertContains(confirmation, "官方教學大綱來源")
        self.assertContains(confirmation, "不會連帶刪除")
        self.assertContains(confirmation, "Enrollment")
        self.assertContains(confirmation, "學生帳號也會保留")
        csrf_token = re.search(
            rb'name="csrfmiddlewaretoken" value="([^"]+)"',
            confirmation.content,
        ).group(1).decode()
        deleted = root_client.post(
            delete_url,
            {
                "post": "yes",
                "csrfmiddlewaretoken": csrf_token,
            },
            HTTP_REFERER=f"http://testserver{delete_url}",
        )

        self.assertEqual(
            deleted.status_code, 302,
            f"user={deleted.wsgi_request.user!r}; "
            f"csrf_cookie={bool(root_client.cookies.get('csrftoken'))}",
        )
        self.assertFalse(NtubTimetableSnapshot.objects.filter(pk=self.snapshot.pk).exists())
        self.assertFalse(NtubTimetableEntry.objects.filter(pk=self.imported_entry.pk).exists())
        self.assertFalse(OfficialTeachingSyllabus.objects.filter(pk=syllabus.pk).exists())
        self.assertTrue(Course.objects.filter(pk=course.pk).exists())
        self.assertTrue(Teacher.objects.filter(pk=self.teacher.pk).exists())
        self.assertTrue(Section.objects.filter(pk=section.pk).exists())
        self.assertTrue(Enrollment.objects.filter(pk=enrollment.pk).exists())
        self.assertTrue(Waitlist.objects.filter(pk=waitlist.pk).exists())

    def test_maintainer_can_withdraw_own_published_schedule_only(self):
        draft = self.takeover()
        draft.entries.filter(entry_type="course").update(is_active=False)
        self.add_valid_course_entry(draft)
        self.admin_client.post(
            self.url("fixed_schedule_publish", self.class_group.pk, self.term.pk),
        )

        response = self.admin_client.post(
            self.url("fixed_schedule_withdraw", draft.pk),
        )

        self.assertEqual(response.status_code, 302)
        draft.refresh_from_db()
        self.assertEqual(draft.status, NtubTimetableSnapshot.Status.ARCHIVED)
        self.assertEqual(
            self.admin_client.post(
                self.url("fixed_schedule_withdraw", draft.pk),
            ).status_code,
            404,
        )

    def test_assigned_teacher_sees_fixed_course_without_fake_enrollment_counts(self):
        canonical_teacher = Teacher.objects.create(name="原課程教師")
        existing_course = Course.objects.create(
            term=self.term,
            owning_department=self.department,
            code="FIX100",
            name="既有自主選課課程",
            teacher=canonical_teacher,
            credits=3,
        )
        draft = self.takeover()
        entry = draft.entries.get(course_code="FIX100")
        self.assertEqual(entry.course_id, existing_course.pk)

        response = self.admin_client.post(
            self.url(
                "fixed_schedule_entry_change", self.class_group.pk, self.term.pk,
                entry.pk,
            ),
            {
                "entry_type": "course",
                "course_code": "FIX100",
                "course_name": "本班固定課程",
                "teacher": self.teacher.pk,
                "teacher_name": self.teacher.name,
                "credits": 3,
                "weekly_hours": "3.00",
                "course_category": "必修",
                "weekday": 2,
                "start_period": 3,
                "end_period": 4,
                "classroom": "B202",
                "is_active": "on",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.admin_client.post(
            self.url("fixed_schedule_publish", self.class_group.pk, self.term.pk),
        )

        User = get_user_model()
        teacher_user = User.objects.create_user(
            "fixed-course-teacher", is_staff=True, password="test-pass",
        )
        teacher_user.groups.add(
            Group.objects.create(name=SYLLABUS_TEACHER_GROUP_NAME),
        )
        self.teacher.user = teacher_user
        self.teacher.save(update_fields=["user"])
        teacher_client = self.client_class()
        force_admin_login(teacher_client, teacher_user)

        response = teacher_client.get(reverse("admin:index"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "本班固定課程")
        self.assertContains(response, "班級固定課程不建立 Enrollment")
        self.assertEqual(response.context["summary"]["section_count"], 0)
        self.assertEqual(response.context["summary"]["enrolled_count"], 0)
        self.assertEqual(Enrollment.objects.count(), 0)
        existing_course.refresh_from_db()
        self.assertEqual(existing_course.teacher_id, canonical_teacher.pk)

    def test_imported_history_cannot_be_edited_or_deleted_from_admin(self):
        entry_admin = admin.site._registry[NtubTimetableEntry]
        snapshot_admin = admin.site._registry[NtubTimetableSnapshot]
        request = RequestFactory().get("/admin/")
        request.user = self.maintainer
        self.assertFalse(entry_admin.has_change_permission(request, self.imported_entry))
        self.assertFalse(entry_admin.has_delete_permission(request, self.imported_entry))
        self.assertFalse(snapshot_admin.has_delete_permission(request, self.snapshot))
        with self.assertRaises(PermissionDenied):
            snapshot_admin.delete_model(request, self.snapshot)
        self.assertTrue(NtubTimetableSnapshot.objects.filter(pk=self.snapshot.pk).exists())
        self.assertTrue(NtubTimetableEntry.objects.filter(pk=self.imported_entry.pk).exists())
