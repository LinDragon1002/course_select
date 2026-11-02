from django.contrib.auth import get_user_model
from django.test import override_settings
from django.urls import reverse

from ..models import Course, Enrollment, Section, Term, Waitlist
from ..services import register
from .base import CourseTestCase


class RegisterTests(CourseTestCase):
    def test_enrolls(self):
        a = self.section("A", "A", 1, 2)
        self.assertEqual(register(self.u, a).code, "enrolled")
        self.assertEqual(self.mine(), 1)
        self.assertEqual([s.pk for s in self.my_sections()], [a.pk])

    def test_already_enrolled_same_section(self):
        a = self.section("A", "A", 1, 2)
        register(self.u, a)
        self.assertEqual(register(self.u, a).code, "already")
        self.assertEqual(self.mine(), 1)

    def test_other_section_of_same_course_is_rejected(self):
        a = self.section("A", "A", 1, 2)
        b = self.section("A", "B", 4, 5)  # 同課的 B 班，不同時段
        register(self.u, a)
        r = register(self.u, b)
        self.assertEqual(r.code, "already")
        self.assertIn("A 班", r.message)
        self.assertEqual(self.mine(), 1)

    def test_can_switch_section_after_dropping(self):
        a = self.section("A", "A", 1, 2)
        b = self.section("A", "B", 4, 5)
        register(self.u, a)
        Enrollment.objects.filter(user=self.u, section=a).delete()
        self.assertEqual(register(self.u, b).code, "enrolled")
        self.assertEqual(self.mine(), 1)

    def test_overlap_is_rejected(self):
        a = self.section("A", "A", 1, 2)
        b = self.section("B", "A", 2, 3)  # 不同課程，重疊一節
        register(self.u, a)
        r = register(self.u, b)
        self.assertEqual(r.code, "conflict")
        # 衝堂訊息引用的是「已選」的 A 課 A 班，不是被擋的 B
        self.assertIn("A 班", r.message)
        self.assertEqual(self.mine(), 1)

    def test_conflict_checked_per_section_time(self):
        a1 = self.section("A", "A", 1, 2)   # A 課 A 班：週一 1-2
        b1 = self.section("B", "A", 4, 5)   # B 課 A 班：週一 4-5（不衝堂）
        register(self.u, a1)
        self.assertEqual(register(self.u, b1).code, "enrolled")

    def test_full_section_goes_to_waitlist(self):
        a = self.section("A", "A", 1, 2, capacity=1)
        b = self.section("A", "B", 4, 5)
        Enrollment.objects.create(user=get_user_model().objects.create_user("v"), section=a)
        self.assertEqual(register(self.u, a).code, "waitlisted")  # A 班滿 → 進等待名單
        self.assertEqual(register(self.u, b).code, "enrolled")  # B 班不受影響
        self.assertEqual(self.mine(), 1)
        # 選進 B 班後，同課的等待紀錄自動清掉
        self.assertFalse(Waitlist.objects.filter(user=self.u, section=a).exists())

    @override_settings(MAX_CREDITS=3)
    def test_credit_limit_is_per_term(self):
        term2 = Term.objects.create(code="114-2")
        a = self.section("A", "A", 1, 2)
        b = self.section("B", "A", 1, 2, term=term2)  # 別的學期，同時段也不算衝堂、不算學分
        self.assertEqual(register(self.u, a).code, "enrolled")
        self.assertEqual(register(self.u, b).code, "enrolled")

    @override_settings(MAX_CREDITS=3)
    def test_over_credits_is_rejected(self):
        a = self.section("A", "A", 1, 2)
        c = self.section("C", "A", 4, 5)
        self.assertEqual(register(self.u, a).code, "enrolled")
        self.assertEqual(register(self.u, c).code, "over_credits")
        self.assertEqual(self.mine(), 1)


class PrereqTests(CourseTestCase):
    def past_section(self, code, label="A"):
        past = Term.objects.get_or_create(code="113-2", defaults={})[0]
        c, _ = Course.objects.get_or_create(term=past, code=code,
                                            defaults=dict(name=code, teacher=self.teacher))
        return Section.objects.update_or_create(course=c, label=label,
                                                defaults=dict(day=5, start_period=1, end_period=2))[0]

    def with_prereq(self, section, *prereq_codes):
        section.course.prerequisites.set([self.past_section(c).course for c in prereq_codes])

    def test_missing_prereq_blocks(self):
        a = self.section("CS301", "A", 3, 5)
        self.with_prereq(a, "CS201")
        r = register(self.u, a)
        self.assertEqual(r.code, "prereq")
        self.assertIn("CS201", r.message)
        self.assertEqual(self.mine(), 0)

    def test_completed_prereq_allows(self):
        a = self.section("CS301", "A", 3, 5)
        self.with_prereq(a, "CS201")
        Enrollment.objects.create(user=self.u, section=self.past_section("CS201"))
        self.assertEqual(register(self.u, a).code, "enrolled")

    def test_multiple_prereqs_missing_one(self):
        a = self.section("CS301", "A", 3, 5)
        self.with_prereq(a, "CS101", "CS201")
        Enrollment.objects.create(user=self.u, section=self.past_section("CS101"))
        r = register(self.u, a)
        self.assertEqual(r.code, "prereq")
        self.assertIn("CS201", r.message)
        self.assertNotIn("CS101", r.message)

    def test_current_term_enrollment_does_not_count(self):
        # 同一學期的加選不算「修過」，只有過去學期才算
        a = self.section("CS301", "A", 3, 5)
        self.with_prereq(a, "CS201")
        cur = self.section("CS201", "A", 1, 2)
        Enrollment.objects.create(user=self.u, section=cur)  # 本學期選了 CS201（但先修比的是 past course 的 code）
        # 本學期那筆的 course.term 是 is_current=True → 不算已修
        self.assertEqual(register(self.u, a).code, "prereq")

    def test_post_enroll_blocked_via_view(self):
        self.client.force_login(self.u)
        a = self.section("CS301", "A", 3, 5)
        self.with_prereq(a, "CS201")
        r = self.client.post(reverse("enroll", args=[a.pk]), follow=True)
        self.assertContains(r, "先修")
        self.assertEqual(self.mine(), 0)

    def test_guest_redirects_to_login(self):
        a = self.section("CS301", "A", 3, 5)
        self.with_prereq(a, "CS201")

        r = self.client.get(reverse("course_list"))

        self.assertRedirects(
            r,
            f"{reverse('login')}?next={reverse('course_list')}",
            fetch_redirect_response=False,
        )

    def test_logged_in_list_marks_completed_and_missing(self):
        self.client.force_login(self.u)
        a = self.section("CS301", "A", 3, 5)
        self.with_prereq(a, "CS101", "CS201")
        Enrollment.objects.create(user=self.u, section=self.past_section("CS101"))
        r = self.client.get(reverse("course_list"))
        self.assertContains(r, "CS101✓")
        self.assertContains(r, "CS201")
        self.assertContains(r, "未修")

    def test_detail_shows_prereqs(self):
        self.client.force_login(self.u)
        a = self.section("CS301", "A", 3, 5)
        self.with_prereq(a, "CS201")
        r = self.client.get(reverse("course_detail", args=[a.course_id]))
        self.assertContains(r, "先修：CS201")
        self.assertContains(r, "未修")
