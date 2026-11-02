"""NTUB 官方開課的「班別修課政策」與「官方課表衝堂」整合測試。

測試對象：

* `courses/enrollment_policy.py`（政策判定與官方課表衝堂判準）
* `courses/verdict.py`（evaluate 的政策關卡與官方衝堂）
* `courses/services.py`（register：偽造 POST 也繞不過）
* `courses/waitlist.py`（候補遞補重新驗證政策與衝堂）
* `courses/views.py`（課程列表／詳情／課表：理由、空狀態、合併顯示）

情境（115-1 真實資料）：

* 課號 40627960「網站應用程式設計」只有一個開課班別（40620A，週四第 2～4 節）。
  - 對 40620A（二甲）＝本班必修，can_enroll=False。
  - 對 40630B（三乙）＝跨班專業選修，can_enroll=True。
* 40630B 的官方班級課表：週三 5～7 節「作業系統」、週五 5～7 節「社群大數據分析」。

這些政策只建立在測試資料庫（隔離的 SQLite 測試庫），不會寫入開發庫。
"""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from ..models import (AcademicCatalog, AcademicClassGroup, AcademicDepartment,
                      AcademicProgramType, Course, Enrollment, NtubOfferingSource,
                      NtubTimetableEntry, NtubTimetableSnapshot, Section,
                      SectionClassPolicy, StudentAcademicProfile, Teacher, Term,
                      Waitlist)
from ..ntub_schedule_grid import KIND_OFFICIAL_ENROLLMENT
from ..services import drop_enrollment, register
from ..waitlist import cancel as cancel_wait
from .session_helpers import force_admin_login


class NtubPolicyTestCase(TestCase):
    """共用前置：學籍目錄、115-1、兩個班級、官方開課與政策。"""

    def setUp(self):
        self.catalog = AcademicCatalog.objects.create(version="test-policy", source_key="ntub")
        self.program = AcademicProgramType.objects.create(
            catalog=self.catalog, code="4", name_zh="四技日間部")
        self.department = AcademicDepartment.objects.create(
            catalog=self.catalog, program_type=self.program, code="406", name_zh="四技資訊管理系")
        self.group_40620a = AcademicClassGroup.objects.create(
            catalog=self.catalog, department=self.department, code="40620A",
            name_zh="四技資管二年甲班", grade_no=2)
        self.group_40630b = AcademicClassGroup.objects.create(
            catalog=self.catalog, department=self.department, code="40630B",
            name_zh="四技資管三年乙班", grade_no=3)

        self.term = Term.objects.create(code="115-1", is_current=True)
        self.teacher = Teacher.objects.create(name="林俊杰")

        # 官方開課：40627960 網站應用程式設計，開課班級 40620A，週四第 2～4 節
        self.web = self.offering(code="40627960", name="網站應用程式設計",
                                 class_group=self.group_40620a, day=4,
                                 start=2, end=4, serial="19")

        # 官方班級課表
        self.snapshot_40630b = self.snapshot(self.group_40630b, [
            ("course", "40635210", "作業系統", "林志宗", "電703", 3, 5, 7),
            ("course", "40636390", "社群大數據分析", "劉智華", "電607", 5, 5, 7),
            ("activity", "", "共同空堂", "", "", 1, 8, 9),
        ])
        self.snapshot_40620a = self.snapshot(self.group_40620a, [
            ("course", "40626610", "資訊網路", "蒯思齊", "電606", 1, 2, 4),
            ("course", "40627960", "網站應用程式設計", "林俊杰", "電708", 4, 2, 4),
            ("activity", "", "課外活動", "", "", 3, 8, 9),
        ])

        # 政策：二甲本班必修不可自行加選；三乙跨班專選可以
        self.policy_required = SectionClassPolicy.objects.create(
            section=self.web.section, class_group=self.group_40620a,
            course_category=SectionClassPolicy.CourseCategory.REQUIRED,
            can_enroll=False, status=SectionClassPolicy.Status.PUBLISHED,
            confirmed_by=self.make_user("maintainer", is_staff=True), confirmed_at=None)
        self.policy_elective = SectionClassPolicy.objects.create(
            section=self.web.section, class_group=self.group_40630b,
            course_category=SectionClassPolicy.CourseCategory.PROFESSIONAL_ELECTIVE,
            can_enroll=True, status=SectionClassPolicy.Status.PUBLISHED)

        self.user_40620a = self.make_user("s20a", group=self.group_40620a)
        self.user_40630b = self.make_user("s30b", group=self.group_40630b)
        self.user_no_profile = self.make_user("noprofile")

    # --- 建立資料的小工具 -----------------------------------------------------

    def make_user(self, username, group=None, is_staff=False):
        user = get_user_model().objects.create_user(username)
        if is_staff:
            user.is_staff = True
            user.save(update_fields=["is_staff"])
        if group is not None:
            StudentAcademicProfile.objects.create(user=user, class_group=group)
        return user

    def offering(self, code, name, class_group, day, start, end, serial,
                 teacher=None, room="A4708", capacity=51):
        """建立一筆官方開課（課程 + 班別 + NtubOfferingSource 對應）。"""
        teacher = teacher or self.teacher
        course = Course.objects.create(term=self.term, code=code, name=name,
                                       teacher=teacher, credits=3)
        section = Section.objects.create(course=course, label="A", day=day,
                                         start_period=start, end_period=end,
                                         room=room, capacity=capacity)
        source = NtubOfferingSource.objects.create(
            source_key=f"115|1|{class_group.code}|{serial}|{code}",
            academic_year="115", semester="1", class_code=class_group.code,
            official_serial=serial, course_code=code, term=self.term,
            class_group=class_group, course=course, section=section,
            teacher=teacher, teacher_name=teacher.name, course_name=name,
            classroom_name=room, official_capacity=capacity, official_enrolled=0,
            source_url=f"https://example.test/{code}", source_file="offerings.json",
            applied_day=day, applied_start_period=start, applied_end_period=end,
            applied_room=room, applied_capacity=capacity)
        return source

    def snapshot(self, class_group, rows):
        """建立班級官方課表快照；rows = (類型, 課號, 課名, 老師, 教室, 星期, 起, 訖)。"""
        snap = NtubTimetableSnapshot.objects.create(
            class_group=class_group, term=self.term,
            class_name=class_group.name_zh, source_url="https://example.test/timetable")
        for entry_type, code, name, teacher, classroom, day, start, end in rows:
            NtubTimetableEntry.objects.create(
                snapshot=snap, entry_type=entry_type, course_code=code,
                course_name=name, teacher_name=teacher, classroom=classroom,
                weekday=day, start_period=start, end_period=end,
                periods=list(range(start, end + 1)))
        return snap

    def publish(self, section, class_group, can_enroll=True,
                category=SectionClassPolicy.CourseCategory.PROFESSIONAL_ELECTIVE,
                status=SectionClassPolicy.Status.PUBLISHED):
        policy, _ = SectionClassPolicy.objects.update_or_create(
            section=section, class_group=class_group,
            defaults=dict(course_category=category, can_enroll=can_enroll,
                           status=status))
        return policy


class CrossClassElectiveTests(NtubPolicyTestCase):
    """第二部分：跨班專選——三乙可加選已發布的二甲專選。"""

    def test_three_years_b_can_enroll_published_elective(self):
        result = register(self.user_40630b, self.web.section)
        self.assertEqual(result.code, "enrolled")
        self.assertTrue(Enrollment.objects.filter(
            user=self.user_40630b, section=self.web.section).exists())

    def test_own_class_required_cannot_enroll(self):
        result = register(self.user_40620a, self.web.section)
        self.assertEqual(result.code, "policy_closed")
        self.assertEqual(Enrollment.objects.count(), 0)

    def test_required_policy_keeps_official_timetable_without_enrollment(self):
        """二甲本班必修不需要（也不會產生）Enrollment。"""
        register(self.user_40620a, self.web.section)
        self.assertEqual(Enrollment.objects.count(), 0)
        entries = self.snapshot_40620a.entries.filter(entry_type="course")
        self.assertTrue(entries.filter(course_code="40627960").exists())

    def test_no_second_course_is_created(self):
        """跨班加選不會憑空多出一門課：還是同一門課、同一個班別。"""
        register(self.user_40630b, self.web.section)
        self.assertEqual(Course.objects.filter(term=self.term, code="40627960").count(), 1)
        self.assertEqual(
            Section.objects.filter(course__code="40627960").count(), 1)
        self.assertEqual(self.web.section.enrollments.count(), 1)

    def test_official_offering_enrolled_count_is_not_site_count(self):
        """官方公告的已選人數不會變成本站的加選紀錄。"""
        self.web.official_enrolled = 30
        self.web.save(update_fields=["official_enrolled"])
        register(self.user_40630b, self.web.section)
        self.assertEqual(self.web.section.enrollments.count(), 1)


class PolicyGateTests(NtubPolicyTestCase):
    """第一部分：沒有政策、草稿、或不可自行加選都不能加選。"""

    def setUp(self):
        super().setUp()
        # 沒有任何政策的官方開課
        self.no_policy = self.offering(code="40624660", name="財務管理",
                                       class_group=self.group_40620a, day=1,
                                       start=5, end=7, serial="21", room="A4607")

    def test_official_section_without_policy_is_blocked(self):
        result = register(self.user_40630b, self.no_policy.section)
        self.assertEqual(result.code, "policy_missing")
        self.assertEqual(Enrollment.objects.count(), 0)

    def test_draft_policy_is_blocked(self):
        self.publish(self.no_policy.section, self.group_40630b, can_enroll=True,
                     status=SectionClassPolicy.Status.DRAFT)
        result = register(self.user_40630b, self.no_policy.section)
        self.assertEqual(result.code, "policy_missing")
        self.assertIn("草稿", result.message)
        self.assertEqual(Enrollment.objects.count(), 0)

    def test_published_but_cannot_enroll_is_blocked(self):
        self.publish(self.no_policy.section, self.group_40630b, can_enroll=False)
        result = register(self.user_40630b, self.no_policy.section)
        self.assertEqual(result.code, "policy_closed")
        self.assertEqual(Enrollment.objects.count(), 0)

    def test_forged_post_cannot_bypass_policy(self):
        """偽造 POST 直接打加選 endpoint，也不能繞過政策。"""
        self.publish(self.no_policy.section, self.group_40630b, can_enroll=True,
                     status=SectionClassPolicy.Status.DRAFT)
        self.client.force_login(self.user_40630b)
        r = self.client.post(reverse("enroll", args=[self.no_policy.section.pk]), follow=True)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(Enrollment.objects.count(), 0)
        self.assertContains(r, "草稿")

    def test_forged_post_cannot_enroll_other_class_policy(self):
        """三乙不能借用二甲的必修政策。

        三乙自己的專選政策先撤回，此時唯一存在的政策是二甲的本班必修——
        那條政策不適用三乙，偽造 POST 也不該加選成功。
        """
        self.policy_elective.status = SectionClassPolicy.Status.DRAFT
        self.policy_elective.save(update_fields=["status"])
        self.client.force_login(self.user_40630b)
        r = self.client.post(reverse("enroll", args=[self.web.section.pk]), follow=True)
        self.assertEqual(Enrollment.objects.count(), 0)
        # 理由必須是「三乙自己的政策未發布」，不是借用二甲的必修政策
        self.assertContains(r, "草稿")
        self.assertNotContains(r, "本班必修")

    def test_student_without_academic_profile_cannot_enroll_official(self):
        result = register(self.user_no_profile, self.web.section)
        self.assertEqual(result.code, "policy_missing")
        self.assertIn("學籍", result.message)
        self.assertEqual(Enrollment.objects.count(), 0)

    def test_non_official_section_keeps_legacy_behavior(self):
        """沒有官方開課來源的班別（114-1 示範課程）沿用原本的加選規則。"""
        course = Course.objects.create(term=self.term, code="CS101", name="程式設計",
                                       teacher=self.teacher, credits=3)
        section = Section.objects.create(course=course, label="A", day=1,
                                         start_period=1, end_period=2)
        result = register(self.user_no_profile, section)
        self.assertEqual(result.code, "enrolled")
        self.assertEqual(Enrollment.objects.count(), 1)


class OfficialTimetableConflictTests(NtubPolicyTestCase):
    """第三部分：官方班級課表與選修 Section 的衝堂判斷。"""

    def setUp(self):
        super().setUp()
        # 週五 5～7 節：與 40630B 的官方課程「社群大數據分析」同時段，但不同課號
        self.other = self.offering(code="40627440", name="數位內容概論",
                                   class_group=self.group_40620a, day=5,
                                   start=5, end=7, serial="15", room="A1401")
        # 週五 5～7 節：與 40630B 的官方課程「社群大數據分析」同一課號
        self.same = self.offering(code="40636390", name="社群大數據分析",
                                  class_group=self.group_40620a, day=5,
                                  start=5, end=7, serial="16", room="A1401")
        self.publish(self.other.section, self.group_40630b)
        self.publish(self.same.section, self.group_40630b)

    def test_different_official_course_conflict_blocks(self):
        result = register(self.user_40630b, self.other.section)
        self.assertEqual(result.code, "conflict")
        self.assertIn("社群大數據分析", result.message)
        self.assertEqual(Enrollment.objects.count(), 0)

    def test_same_official_course_is_not_a_conflict(self):
        result = register(self.user_40630b, self.same.section)
        self.assertEqual(result.code, "enrolled")
        self.assertEqual(Enrollment.objects.count(), 1)

    def test_official_activity_does_not_block(self):
        """共同空堂只是時間框架，不是必修，不阻擋加選。"""
        activity = self.offering(code="40635555", name="自由活動時段的課",
                                  class_group=self.group_40620a, day=1,
                                  start=8, end=9, serial="30")
        self.publish(activity.section, self.group_40630b)
        result = register(self.user_40630b, activity.section)
        self.assertEqual(result.code, "enrolled")

    def test_conflict_uses_course_code_not_course_name(self):
        """同課名不同課號仍算衝堂（認定不靠課名）。"""
        twin = self.offering(code="40639999", name="社群大數據分析",
                             class_group=self.group_40620a, day=5,
                             start=5, end=7, serial="31")
        self.publish(twin.section, self.group_40630b)
        result = register(self.user_40630b, twin.section)
        self.assertEqual(result.code, "conflict")

    def test_conflict_is_scoped_to_own_class_and_term(self):
        """別的班級／別的學期的官方課表不會拿來擋人。

        這個班別排在週四 2～4 節：40630B 當學期的官方課表沒有這個時段，
        所以只有「114-1 的舊課表」或「別的班級的課表」才可能誤擋。
        """
        free = self.offering(code="40627441", name="另一門課",
                             class_group=self.group_40620a, day=4,
                             start=2, end=4, serial="17", room="A1402")
        self.publish(free.section, self.group_40630b)
        other_term = Term.objects.create(code="114-1", is_current=False)
        snapshot = NtubTimetableSnapshot.objects.create(
            class_group=self.group_40630b, term=other_term, class_name="舊學期")
        NtubTimetableEntry.objects.create(
            snapshot=snapshot, entry_type="course", course_code="40627441",
            course_name="另一門課", weekday=4, start_period=2, end_period=4,
            periods=[2, 3, 4])
        result = register(self.user_40630b, free.section)
        self.assertEqual(result.code, "enrolled")


class WaitlistConsistencyTests(NtubPolicyTestCase):
    """第四部分：候補與遞補使用同一份政策與衝堂判準。"""

    def setUp(self):
        super().setUp()
        self.web.section.capacity = 1
        self.web.section.save(update_fields=["capacity"])
        self.occupant = self.make_user("occupant")
        Enrollment.objects.create(user=self.occupant, section=self.web.section)

    def test_waitlist_join_follows_the_same_policy(self):
        result = register(self.user_40630b, self.web.section)
        self.assertEqual(result.code, "waitlisted")
        self.assertTrue(Waitlist.objects.filter(
            user=self.user_40630b, section=self.web.section).exists())

    def test_promotion_revalidates_policy(self):
        """政策被撤回後，候補不能被自動遞補成加選。"""
        register(self.user_40630b, self.web.section)
        self.policy_elective.status = SectionClassPolicy.Status.DRAFT
        self.policy_elective.save(update_fields=["status"])
        drop_enrollment(self.occupant, self.web.section)
        self.assertFalse(Enrollment.objects.filter(
            user=self.user_40630b, section=self.web.section).exists())
        self.assertFalse(Waitlist.objects.filter(
            user=self.user_40630b, section=self.web.section).exists())

    def test_promotion_skips_waiter_with_official_timetable_conflict(self):
        """與班級官方課表衝堂的候補者被跳過，順延下一位。

        情境：候補者先排隊（當下官方課表還沒有衝突），之後官方課表才補進
        同一時段；遞補時必須重新驗證，把這個候補者移出、順延下一位。
        """
        # 週一 5～7 節：40630B 當下的官方課表沒有這個時段，可以先排隊
        conflict = self.offering(code="40627440", name="數位內容概論",
                                 class_group=self.group_40620a, day=1,
                                 start=5, end=7, serial="15", room="A1401")
        conflict.section.capacity = 1
        conflict.section.save(update_fields=["capacity"])
        self.publish(conflict.section, self.group_40630b)
        self.publish(conflict.section, self.group_40620a)
        Enrollment.objects.create(user=self.make_user("occupant2"),
                                  section=conflict.section)
        first = self.user_40630b
        second = self.make_user("second", group=self.group_40620a)
        register(first, conflict.section)     # 第 1 位
        register(second, conflict.section)    # 第 2 位
        # 官方課表之後才補進同時段 → 對 40630B 的候補者變成衝堂
        NtubTimetableEntry.objects.create(
            snapshot=self.snapshot_40630b, entry_type="course",
            course_code="40629999", course_name="後來才公告的課",
            weekday=1, start_period=5, end_period=7, periods=[5, 6, 7])
        drop_enrollment(Enrollment.objects.get(section=conflict.section).user,
                        conflict.section)
        self.assertFalse(Enrollment.objects.filter(
            user=first, section=conflict.section).exists())
        self.assertTrue(Enrollment.objects.filter(
            user=second, section=conflict.section).exists())

    def test_withdrawn_policy_still_allows_drop(self):
        """已加選者即使政策被撤回，仍可退選。"""
        self.web.section.capacity = 5
        self.web.section.save(update_fields=["capacity"])
        register(self.user_40630b, self.web.section)
        self.policy_elective.status = SectionClassPolicy.Status.DRAFT
        self.policy_elective.save(update_fields=["status"])
        self.assertTrue(drop_enrollment(self.user_40630b, self.web.section))
        self.assertEqual(Enrollment.objects.count(), 1)  # 只剩原本那位佔位者

    def test_withdrawn_policy_still_allows_cancel_wait(self):
        """已排隊者即使政策被撤回，仍可取消等待。"""
        register(self.user_40630b, self.web.section)
        self.policy_elective.status = SectionClassPolicy.Status.DRAFT
        self.policy_elective.save(update_fields=["status"])
        self.assertTrue(cancel_wait(self.user_40630b, self.web.section))
        self.assertFalse(Waitlist.objects.filter(user=self.user_40630b).exists())

    def test_fifo_order_is_kept(self):
        """政策有效時，遞補仍然照 FIFO。"""
        first = self.make_user("first", group=self.group_40630b)
        second = self.make_user("second2", group=self.group_40630b)
        register(first, self.web.section)
        register(second, self.web.section)
        drop_enrollment(self.occupant, self.web.section)
        self.assertTrue(Enrollment.objects.filter(
            user=first, section=self.web.section).exists())
        self.assertFalse(Enrollment.objects.filter(
            user=second, section=self.web.section).exists())


class StudentInterfaceTests(NtubPolicyTestCase):
    """第五部分：學生介面。"""

    def setUp(self):
        super().setUp()
        self.client.force_login(self.user_40630b)

    def test_list_shows_enroll_button_for_published_policy(self):
        r = self.client.get(reverse("course_list"))
        self.assertContains(r, "加選</button>")
        self.assertContains(r, "網站應用程式設計")

    def test_list_hides_section_when_not_enrollable(self):
        """搜尋頁只顯示本班已發布且允許自行加選的課程。"""
        self.publish(self.web.section, self.group_40630b, can_enroll=False)
        self.client.force_login(self.user_40620a)

        r = self.client.get(reverse("course_list"))

        self.assertEqual(r.status_code, 200)
        self.assertNotIn(
            self.web.section.pk,
            [s.pk for s in r.context["sections"]],
        )
        self.assertNotContains(r, "網站應用程式設計")

    def test_detail_page_shows_policy_reason(self):
        self.client.force_login(self.user_40620a)
        r = self.client.get(reverse("course_detail", args=[self.web.course_id]))
        self.assertContains(r, "本班必修")
        self.assertNotContains(r, "加選</button>")

    def test_section_is_not_duplicated_by_multiple_class_policies(self):
        """同一個 Section 不會因為多個適用班級而重複顯示。"""
        r = self.client.get(reverse("course_list"))
        rows = r.context["sections"]
        self.assertEqual(len(rows), len({s.pk for s in rows}))
        self.assertEqual(r.content.decode().count("網站應用程式設計"), 1)

    def test_empty_state_when_nothing_is_enrollable(self):
        """沒有任何可加選的課程時，呈現清楚的空狀態。"""
        self.client.force_login(self.user_40620a)
        r = self.client.get(reverse("course_list"))
        self.assertContains(r, "目前沒有可加選的課程")

    def test_empty_database_shows_empty_state(self):
        NtubOfferingSource.objects.all().delete()
        Section.objects.all().delete()
        r = self.client.get(reverse("course_list"))
        self.assertContains(r, "目前沒有可選課程")

    def test_seats_are_labelled_as_site_simulation(self):
        r = self.client.get(reverse("course_list"))
        self.assertContains(r, "本站模擬")

    def test_disclaimer_is_not_shown_on_course_list(self):
        response = self.client.get(reverse("course_list"))
        self.assertNotContains(
            response, "本站為模擬選課系統，非 NTUB 正式校務選課紀錄。")

    def test_disclaimer_is_not_shown_on_schedule_page(self):
        response = self.client.get(reverse("my_schedule"))
        self.assertNotContains(
            response, "本站為模擬選課系統，非 NTUB 正式校務選課紀錄。")


class MergedScheduleTests(NtubPolicyTestCase):
    """第五部分：官方課表與本站 Enrollment 合併顯示。"""

    def setUp(self):
        super().setUp()
        # 二甲的「資訊網路」在自己的官方課表裡（週一 2～4 節），政策允許自行加選
        self.network = self.offering(code="40626610", name="資訊網路",
                                     class_group=self.group_40620a, day=1,
                                     start=2, end=4, serial="10", room="電606")
        self.publish(self.network.section, self.group_40620a)
        self.client.force_login(self.user_40620a)

    def test_official_and_enrollment_are_merged_in_one_slot(self):
        register(self.user_40620a, self.network.section)
        r = self.client.get(reverse("my_schedule"))
        items = [i for i in r.context["grid"].items if i.title == "資訊網路"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].kind, KIND_OFFICIAL_ENROLLMENT)
        # 兩邊的資料都保留
        self.assertIn("官方", items[0].meta)
        self.assertIn("蒯思齊", items[0].meta)

    def test_official_course_list_still_shows_the_merged_course(self):
        register(self.user_40620a, self.network.section)
        r = self.client.get(reverse("my_schedule"))
        names = [row["name"] for row in r.context["official_course_rows"]]
        self.assertIn("資訊網路", names)
        self.assertEqual(r.context["official_courses_count"], 2)

    def test_merged_item_is_not_a_false_conflict(self):
        register(self.user_40620a, self.network.section)
        r = self.client.get(reverse("my_schedule"))
        self.assertFalse(r.context["grid"].has_conflict)

    def test_different_official_course_still_conflicts_on_schedule(self):
        """不同課號的官方課程撞到加選，仍然標示衝突。

        官方課表比加選晚公告時會出現這種狀態：加選當下沒有官方課表可擋，
        官方課表補上後課表上就要標出衝突。
        """
        other = self.offering(code="40627440", name="數位內容概論",
                              class_group=self.group_40620a, day=2,
                              start=5, end=7, serial="15", room="A1401")
        self.publish(other.section, self.group_40620a)
        register(self.user_40620a, other.section)
        # 官方課表之後才公告同一時段
        NtubTimetableEntry.objects.create(
            snapshot=self.snapshot_40620a, entry_type="course",
            course_code="40628888", course_name="後來才公告的課",
            weekday=2, start_period=5, end_period=7, periods=[5, 6, 7])
        r = self.client.get(reverse("my_schedule"))
        self.assertTrue(r.context["grid"].has_conflict)


class RegressionTests(NtubPolicyTestCase):
    """第十部分：OAuth、學籍設定、管理後台都正常。"""

    def test_login_page_and_google_entry(self):
        r = self.client.get(reverse("login"))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "/accounts/google/login/")

    def test_admin_still_renders(self):
        admin = get_user_model().objects.create_superuser("root", "root@example.test", "pw")
        force_admin_login(self.client, admin)
        r = self.client.get("/admin/")
        self.assertEqual(r.status_code, 200)

    def test_onboarding_still_works(self):
        """沒有學籍的使用者仍會被導到學籍設定頁。"""
        self.client.force_login(self.user_no_profile)
        r = self.client.get(reverse("onboarding"))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "學制")
