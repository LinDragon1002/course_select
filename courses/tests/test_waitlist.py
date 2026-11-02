from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from .. import waitlist
from ..models import Enrollment, Waitlist
from ..services import drop_enrollment, register
from .base import CourseTestCase
from .html_helpers import section_actions


class WaitlistTests(CourseTestCase):
    def setUp(self):
        super().setUp()
        self.a = self.section("A", "A", 1, 2, capacity=1)
        self.v = get_user_model().objects.create_user("v")
        Enrollment.objects.create(user=self.v, section=self.a)  # A 班額滿

    def test_waitlisted_with_position(self):
        r = register(self.u, self.a)
        self.assertEqual(r.code, "waitlisted")
        self.assertIn("第 1 位", r.message)

    def test_already_waitlisted_shows_position(self):
        register(self.u, self.a)
        r = register(self.u, self.a)
        self.assertEqual(r.code, "already_waitlisted")
        self.assertIn("第 1 位", r.message)

    def test_waiting_positions_batch_matches_row_positions(self):
        # 批量版（positions）與逐行版（position_of）的順位必須一致——UI 與訊息同一來源
        b = self.section("B", "A", 3, 4, capacity=1)
        w1 = get_user_model().objects.create_user("w1")
        Enrollment.objects.create(user=w1, section=b)  # B 班額滿
        register(self.u, self.a)   # a 班第 1 位
        register(self.u, b)        # b 班第 1 位
        register(w1, self.a)       # a 班第 2 位
        positions = waitlist.positions(self.u, self.term)
        self.assertEqual(positions, {self.a.pk: 1, b.pk: 1})
        for wait in Waitlist.objects.filter(user=self.u, promoted_at__isnull=True):
            self.assertEqual(positions[wait.section_id], waitlist.position_of(wait))

    def test_tied_queued_at_share_position(self):
        # 兩列排隊時間相同：依「只數更早的列」規則，兩列拿到同一順位
        w1 = get_user_model().objects.create_user("w1")
        r1 = Waitlist.objects.create(user=self.u, section=self.a)
        r2 = Waitlist.objects.create(user=w1, section=self.a)
        Waitlist.objects.filter(pk__in=[r1.pk, r2.pk]).update(queued_at=timezone.now())
        self.assertEqual(waitlist.position_of(r1), waitlist.position_of(r2))
        self.assertEqual(waitlist.position_of(r1), 1)

    def test_promotes_on_drop(self):
        register(self.u, self.a)
        drop_enrollment(self.v, self.a)
        self.assertTrue(Enrollment.objects.filter(user=self.u, section=self.a).exists())
        self.assertIsNotNone(Waitlist.objects.get(user=self.u, section=self.a).promoted_at)

    def test_no_skip_order(self):
        first = get_user_model().objects.create_user("w1")
        register(first, self.a)   # 第 1 位
        register(self.u, self.a)  # 第 2 位
        drop_enrollment(self.v, self.a)      # 只開一個位置
        self.assertTrue(Enrollment.objects.filter(user=first, section=self.a).exists())
        self.assertFalse(Enrollment.objects.filter(user=self.u, section=self.a).exists())
        self.assertEqual(waitlist.position_of(Waitlist.objects.get(user=self.u, section=self.a)), 1)

    def test_cancel_wait(self):
        register(self.u, self.a)
        self.assertTrue(waitlist.cancel(self.u, self.a))
        self.assertFalse(waitlist.cancel(self.u, self.a))
        self.assertFalse(Waitlist.objects.filter(user=self.u).exists())

    def test_list_shows_waiting_row_and_cancel_button(self):
        register(self.u, self.a)  # 額滿 → 等待第 1 位
        self.client.force_login(self.u)
        r = self.client.get(reverse("course_list"))
        self.assertContains(r, "等待中（第 1 位）")
        row = section_actions(r.content.decode())[str(self.a.pk)]
        self.assertTrue(any(
            form["action"] == reverse("cancel_wait", args=[self.a.pk])
            and any(button["text"] == "取消等待" for button in form["buttons"])
            for form in row["forms"]
        ))

        response = self.client.post(
            reverse("cancel_wait", args=[self.a.pk]),
            {"next": reverse("course_list")},
        )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Waitlist.objects.filter(user=self.u, section=self.a).exists())

    def test_cancel_wait_via_view(self):
        self.client.force_login(self.u)
        register(self.u, self.a)
        r = self.client.post(reverse("cancel_wait", args=[self.a.pk]), follow=True)
        self.assertContains(r, "已取消等待")
        self.assertFalse(Waitlist.objects.filter(user=self.u).exists())

    def test_enrolling_other_section_clears_waitlist(self):
        b = self.section("A", "B", 4, 5)
        register(self.u, self.a)  # 等 A 班
        self.assertEqual(register(self.u, b).code, "enrolled")
        self.assertFalse(Waitlist.objects.filter(user=self.u).exists())

    def test_cannot_wait_when_enrolled_same_course(self):
        b = self.section("A", "B", 4, 5)
        register(self.u, b)  # 已選 B 班
        r = register(self.u, self.a)  # A 班額滿
        self.assertEqual(r.code, "already")
        self.assertFalse(Waitlist.objects.filter(user=self.u).exists())

    def test_banner_shown_until_acked(self):
        self.client.force_login(self.u)
        register(self.u, self.a)
        drop_enrollment(self.v, self.a)
        # GET 不消耗通知：反覆開頁面 banner 都在
        self.assertContains(self.client.get(reverse("my_schedule")), "你已遞補進")
        self.assertContains(self.client.get(reverse("my_schedule")), "你已遞補進")
        # 明確 POST 之後才消失
        self.client.post(reverse("ack_promotion"), follow=True)
        self.assertNotContains(self.client.get(reverse("my_schedule")), "你已遞補進")

    def test_admin_page_does_not_consume_banner(self):
        self.client.force_login(self.u)
        register(self.u, self.a)
        drop_enrollment(self.v, self.a)
        self.client.get("/admin/")  # 任何不顯示 banner 的渲染都不能吃掉通知
        self.assertContains(self.client.get(reverse("my_schedule")), "你已遞補進")

    def test_cancel_wait_rejects_get_and_anonymous(self):
        self.client.force_login(self.u)
        register(self.u, self.a)
        self.assertEqual(self.client.get(reverse("cancel_wait", args=[self.a.pk])).status_code, 405)
        anon = Client()
        r = anon.post(reverse("cancel_wait", args=[self.a.pk]))
        self.assertIn("/login/", r.headers["Location"])
        self.assertTrue(Waitlist.objects.filter(user=self.u).exists())

    def test_requeue_after_promotion_and_drop(self):
        # 被遞補 → 退選 → 班別再額滿 → 重新排隊不能卡死
        register(self.u, self.a)
        drop_enrollment(self.v, self.a)  # u 被遞補
        self.assertTrue(Enrollment.objects.filter(user=self.u, section=self.a).exists())
        drop_enrollment(self.u, self.a)  # u 又退選
        w = get_user_model().objects.create_user("w")
        self.assertEqual(register(w, self.a).code, "enrolled")  # 再額滿
        r = register(self.u, self.a)
        self.assertEqual(r.code, "waitlisted")  # 不再是 already_waitlisted
        self.assertIn("第 1 位", r.message)
        self.assertTrue(waitlist.cancel(self.u, self.a))  # 取消也要動得了

    def test_register_converges_when_section_no_longer_full(self):
        # 等待列還在但班別已不額滿（admin 調高名額）：POST 直接加選、等待列清掉，
        # 不用先手動取消等待（main 的自動收斂語意）
        self.a.capacity = 2
        self.a.save()
        r = register(self.u, self.a)
        self.assertEqual(r.code, "enrolled")
        self.assertTrue(Enrollment.objects.filter(user=self.u, section=self.a).exists())
        self.assertFalse(Waitlist.objects.filter(user=self.u, section=self.a).exists())

    def test_position_message_after_promotion(self):
        # 發生過遞補後，新加入者的「第 N 位」不能把歷史列算進去
        w1 = get_user_model().objects.create_user("w1")
        register(w1, self.a)
        drop_enrollment(self.v, self.a)  # w1 被遞補，A 班又額滿
        w2 = get_user_model().objects.create_user("w2")
        r = register(w2, self.a)
        self.assertEqual(r.code, "waitlisted")
        self.assertIn("第 1 位", r.message)
        self.assertNotIn("第 2 位", r.message)

    def test_promotion_clears_other_same_course_wait(self):
        # 遞補進 A 班後，同課 B 班的等待紀錄要同步清掉
        b = self.section("A", "B", 4, 5, capacity=1)
        w = get_user_model().objects.create_user("w")
        Enrollment.objects.create(user=w, section=b)  # B 班額滿
        register(self.u, self.a)
        register(self.u, b)
        drop_enrollment(self.v, self.a)  # u 遞補進 A 班
        self.assertTrue(Enrollment.objects.filter(user=self.u, section=self.a).exists())
        self.assertFalse(Waitlist.objects.filter(
            user=self.u, section=b, promoted_at__isnull=True).exists())

    def test_promotion_skips_conflicting_waiter(self):
        # 排隊第一的人選了與本班衝堂的課 → 遞補時移出、順延下一位
        c = self.section("C", "A", 1, 2)  # 與 A 班同時段
        register(self.u, self.a)  # 第 1 位
        self.assertEqual(register(self.u, c).code, "enrolled")
        w2 = get_user_model().objects.create_user("w2")
        register(w2, self.a)  # 第 2 位
        drop_enrollment(self.v, self.a)  # 只開一個位置
        self.assertTrue(Enrollment.objects.filter(user=w2, section=self.a).exists())
        self.assertFalse(Enrollment.objects.filter(user=self.u, section=self.a).exists())
        # u 的等待列已被移出
        self.assertFalse(Waitlist.objects.filter(user=self.u, section=self.a).exists())

    def test_promotion_skips_same_course_waiter(self):
        # 排隊第一的人選了同課另一班 → 遞補時移出、順延下一位
        b = self.section("A", "B", 4, 5)  # 同課另一班，時段不衝
        register(self.u, self.a)  # 第 1 位
        self.assertEqual(register(self.u, b).code, "enrolled")
        w2 = get_user_model().objects.create_user("w2")
        register(w2, self.a)  # 第 2 位
        drop_enrollment(self.v, self.a)  # 只開一個位置
        self.assertTrue(Enrollment.objects.filter(user=w2, section=self.a).exists())
        self.assertFalse(Enrollment.objects.filter(user=self.u, section=self.a).exists())
        # u 的等待列已被移出
        self.assertFalse(Waitlist.objects.filter(user=self.u, section=self.a).exists())
