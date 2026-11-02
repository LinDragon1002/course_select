from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from ..models import Course, Enrollment, Term
from .base import CourseTestCase


class EnrollViewTests(CourseTestCase):
    def setUp(self):
        super().setUp()
        self.a = self.section("A", "A", 1, 2, capacity=1)
        self.d = self.section("D", "A", 4, 5)
        self.client.force_login(self.u)

    def post(self, section, name="enroll"):
        return self.client.post(reverse(name, args=[section.pk]), follow=True)

    def test_enroll_and_drop(self):
        self.post(self.a)
        self.assertEqual(self.mine(), 1)
        self.post(self.a, name="drop")
        self.assertEqual(self.mine(), 0)
        self.post(self.a)
        self.assertEqual(self.mine(), 1)

    def test_next_cannot_redirect_offsite(self):
        r = self.client.post(reverse("enroll", args=[self.a.pk]), {"next": "https://evil.example/x"})
        self.assertEqual(r["Location"], reverse("course_detail", args=[self.a.course_id]))
        r = self.client.post(reverse("enroll", args=[self.d.pk]), {"next": "/my/"})
        self.assertEqual(r["Location"], "/my/")

    def test_list_shows_sections_of_current_term_only(self):
        term2 = Term.objects.create(code="114-2")
        self.section("OLD", "A", 1, 2, term=term2)
        rows = self.client.get(reverse("course_list")).context["sections"]
        codes = [s.course.code for s in rows]
        self.assertIn("A", codes)
        self.assertNotIn("OLD", codes)

    def test_pages_render(self):
        for name, args in [("course_list", []), ("course_detail", [self.a.course_id]), ("my_schedule", [])]:
            self.assertEqual(self.client.get(reverse(name, args=args)).status_code, 200)

    def test_detail_breadcrumb_preserves_known_list_filters_only(self):
        response = self.client.get(
            reverse("course_detail", args=[self.a.course_id]),
            {"q": "A", "day": "2", "slot": "morning",
             "next": "https://evil.example/"},
        )
        self.assertContains(response, "選課")
        self.assertNotContains(response, "返回選課清單")
        self.assertContains(response, 'href="/?q=A&amp;day=2&amp;slot=morning"')
        self.assertNotContains(response, "evil.example")

    def test_preview_markup_does_not_create_enrollment(self):
        before = Enrollment.objects.filter(user=self.u).count()
        response = self.client.get(reverse("course_detail", args=[self.a.course_id]))
        self.assertContains(response, "預覽加入課表")
        self.assertContains(response, 'data-start-period="1"')
        self.assertContains(response, 'data-end-period="2"')
        self.assertEqual(Enrollment.objects.filter(user=self.u).count(), before)


class TeacherRoleTests(CourseTestCase):
    def setUp(self):
        super().setUp()
        self.a = self.section("A", "A", 1, 2)
        self.student = get_user_model().objects.create_user("s", first_name="王小明", email="s@example.com")
        Enrollment.objects.create(user=self.student, section=self.a)
        self.teacher.user = self.u
        self.teacher.save()

    def test_teaching_lists_only_own_current_term_courses(self):
        self.section("OLD", "A", 1, 2, term=Term.objects.create(code="114-2"))
        self.client.force_login(self.u)
        r = self.client.get(reverse("teaching"))
        self.assertEqual(r.status_code, 200)
        self.assertEqual([c.pk for c in r.context["courses"]], [self.a.course_id])

    def test_unbound_user_sees_empty_teaching_page(self):
        self.client.force_login(self.student)
        r = self.client.get(reverse("teaching"))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(list(r.context["courses"]), [])
        self.assertContains(r, "這學期沒有你的課程")

    def test_roster_shows_student_name(self):
        self.client.force_login(self.u)
        r = self.client.get(reverse("course_roster", args=[self.a.course_id]))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "王小明")
        self.assertContains(r, "s@example.com")

    def test_other_user_gets_404_on_roster(self):
        self.client.force_login(self.student)
        self.assertEqual(self.client.get(reverse("course_roster", args=[self.a.course_id])).status_code, 404)

    def test_anonymous_is_redirected_to_login(self):
        for name, args in [
            ("course_list", []),
            ("course_detail", [self.a.course_id]),
            ("my_schedule", []),
            ("teaching", []),
            ("course_roster", [self.a.course_id]),
        ]:
            r = self.client.get(reverse(name, args=args))
            self.assertEqual(r.status_code, 302)
            self.assertIn(reverse("login"), r["Location"])
            self.assertIn("next=", r["Location"])

    def test_roster_link_only_for_teacher(self):
        detail = reverse("course_detail", args=[self.a.course_id])
        roster = reverse("course_roster", args=[self.a.course_id])
        self.client.force_login(self.student)
        self.assertNotContains(self.client.get(detail), roster)
        self.client.force_login(self.u)
        self.assertContains(self.client.get(detail), roster)

    def test_deleting_user_keeps_teacher(self):
        self.u.delete()
        self.teacher.refresh_from_db()
        self.assertIsNone(self.teacher.user)
        self.assertTrue(Course.objects.filter(pk=self.a.pk).exists())


class LoginPageTests(CourseTestCase):
    """登入頁只有 Google 一個入口，沒有 demo 一鍵登入。"""

    def test_get_shows_google_login_only(self):
        r = self.client.get(reverse("login"))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "使用 NTUB Google 帳號登入")
        self.assertContains(r, "/accounts/google/login/")
        self.assertNotContains(r, "demo")

    def test_get_redirects_when_authenticated(self):
        self.client.force_login(self.u)
        r = self.client.get(reverse("login"))
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r["Location"], "/")

    def test_post_is_not_a_way_in(self):
        """登入頁不是表單：POST 不會登入任何人。"""
        get_user_model().objects.create_user("demo", email="demo@example.com")
        r = self.client.post(reverse("login"), follow=True)
        self.assertFalse(r.context["user"].is_authenticated)


class SlotFilterTests(CourseTestCase):
    def setUp(self):
        super().setUp()
        # 四個時段各一、day 分開
        self.am = self.section("AM1", "A", 2, 3)          # start 2，週一
        self.mid = self.section("MID1", "A", 5, 6)         # start 5，週一
        self.pm = self.section("PM1", "A", 8, 9, day=3)    # start 8，週三
        self.eve = self.section("EVE1", "A", 11, 12, day=3)  # start 11，週三
        self.client.force_login(self.u)

    def codes(self, **params):
        r = self.client.get(reverse("course_list"), params)
        return {s.course.code for s in r.context["sections"]}

    def test_am(self):
        self.assertEqual(self.codes(slot="am"), {"AM1"})

    def test_midday(self):
        self.assertEqual(self.codes(slot="midday"), {"MID1"})

    def test_pm(self):
        self.assertEqual(self.codes(slot="pm"), {"PM1"})

    def test_evening(self):
        self.assertEqual(self.codes(slot="evening"), {"EVE1"})

    def test_slot_combines_with_day(self):
        self.assertEqual(self.codes(day="3", slot="pm"), {"PM1"})
        self.assertEqual(self.codes(day="3", slot="evening"), {"EVE1"})
        self.assertEqual(self.codes(day="3", slot="am"), set())

    def test_form_remembers_selection(self):
        r = self.client.get(reverse("course_list"), {"slot": "pm"})
        self.assertContains(r, 'value="pm" selected')


class EmptyDatabaseTests(TestCase):
    """只跑過 migrate、還沒 seed 的乾淨 DB（沒有 Term）。"""

    def test_my_schedule_renders_without_term(self):
        # mine 裡的班別必定已加選 → drop，時間窗參數沒作用；
        # 不能因 Term.current() 回 None 而 500。
        u = get_user_model().objects.create_user("u")
        self.client.force_login(u)
        self.assertEqual(self.client.get(reverse("my_schedule")).status_code, 200)


class ScheduleTotalTests(CourseTestCase):
    def test_same_course_two_sections_counts_credits_once(self):
        # admin 手動造成的狀態（service 層平常會擋同課兩班）：
        # 總學分按課去重，3 學分只算一次
        a = self.section("A", "A", 1, 2)
        b = self.section("A", "B", 4, 5)
        Enrollment.objects.create(user=self.u, section=a)
        Enrollment.objects.create(user=self.u, section=b)
        self.client.force_login(self.u)
        r = self.client.get(reverse("my_schedule"))
        # 斷言資料而非 markup，避免模板改版時測試假紅
        self.assertEqual(r.context["total"], 3)
