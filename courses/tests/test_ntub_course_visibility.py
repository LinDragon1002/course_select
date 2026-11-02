"""學生搜尋課程的可見性：只看得到自己班級已發布且 can_enroll=True 的政策所指的班別。

新規則（任務 A）：

* 有學籍的學生（`StudentAcademicProfile` 有綁班級）在課程列表／詳情頁，
  只看得到**自己班級**的 `SectionClassPolicy` 已經 `PUBLISHED` 且
  `can_enroll=True` 所指派的開課（課程詳情頁放寬成「已發布即可」，
  讓「本班必修」仍然看得到並標出原因）。
* 另外保留兩條收尾路徑：自己已加選的（可退選）、自己在等待名單的（可取消）。
* 沒有學籍的使用者（未登入／尚未設定學籍）沿用原本的顯示規則，不加限制。

這些測試只斷言「看得到／看不到」，不重新檢驗加選資格本身。
"""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from ..models import (AcademicCatalog, AcademicClassGroup, AcademicDepartment,
                      AcademicProgramType, Course, Enrollment, NtubOfferingSource,
                      Section, SectionClassPolicy, StudentAcademicProfile, Teacher,
                      Term, Waitlist)


class CourseVisibilityTestCase(TestCase):
    """共用前置：一個學期、兩個班級、三個開課、三種政策狀態。"""

    def setUp(self):
        self.catalog = AcademicCatalog.objects.create(version="test-visibility", source_key="ntub")
        self.program = AcademicProgramType.objects.create(
            catalog=self.catalog, code="4", name_zh="四技日間部")
        self.department = AcademicDepartment.objects.create(
            catalog=self.catalog, program_type=self.program, code="406", name_zh="四技資訊管理系")
        self.group_a = AcademicClassGroup.objects.create(
            catalog=self.catalog, department=self.department, code="40620A",
            name_zh="四技資管二年甲班", grade_no=2)
        self.group_b = AcademicClassGroup.objects.create(
            catalog=self.catalog, department=self.department, code="40630B",
            name_zh="四技資管三年乙班", grade_no=3)

        self.term = Term.objects.create(code="115-1", is_current=True)
        self.teacher = Teacher.objects.create(name="林俊杰")

        # 開課一：政策已發布且 can_enroll=True（本班可自行加選）
        self.open_source = self.offering("40627960", "網站應用程式設計", self.group_a,
                                         day=4, start=2, end=4, serial="19")
        # 開課二：政策已發布但 can_enroll=False（本班必修）
        self.required_source = self.offering("40626610", "資訊網路", self.group_a,
                                              day=1, start=2, end=4, serial="20")
        # 開課三：政策對另一個班級才發布，本班沒有政策
        self.other_source = self.offering("40636390", "社群大數據分析", self.group_b,
                                          day=5, start=5, end=7, serial="21")

        self.publish(self.open_source.section, self.group_a, can_enroll=True)
        self.publish(self.required_source.section, self.group_a, can_enroll=False,
                     category=SectionClassPolicy.CourseCategory.REQUIRED)
        self.publish(self.other_source.section, self.group_b, can_enroll=True)

        self.user_a = self.make_user("student_a", group=self.group_a)
        self.user_b = self.make_user("student_b", group=self.group_b)
        self.user_no_profile = self.make_user("student_no_profile")

    # --- 小工具 ---------------------------------------------------------------

    def make_user(self, username, group=None):
        user = get_user_model().objects.create_user(username)
        if group is not None:
            StudentAcademicProfile.objects.create(user=user, class_group=group)
        return user

    def offering(self, code, name, class_group, day, start, end, serial,
                 teacher=None, room="A4708", capacity=51):
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

    def publish(self, section, class_group, can_enroll=True,
                category=SectionClassPolicy.CourseCategory.PROFESSIONAL_ELECTIVE,
                status=SectionClassPolicy.Status.PUBLISHED):
        policy, _ = SectionClassPolicy.objects.update_or_create(
            section=section, class_group=class_group,
            defaults=dict(course_category=category, can_enroll=can_enroll,
                           status=status))
        return policy

    def listed_course_codes(self, response):
        """回傳列表中出現的課號（用來比對可見的開課）。"""
        return {s.course.code for s in response.context["sections"]}


class ListVisibilityTests(CourseVisibilityTestCase):
    """課程列表的可見性。"""

    def test_only_published_and_open_policy_for_own_class_is_visible(self):
        """本班已發布且 can_enroll=True 的開課看得到。"""
        self.client.force_login(self.user_a)
        r = self.client.get(reverse("course_list"))
        self.assertIn("40627960", self.listed_course_codes(r))
        self.assertContains(r, "網站應用程式設計")

    def test_required_policy_section_is_hidden_from_list(self):
        """本班政策已發布但 can_enroll=False（必修）：列表看不到。"""
        self.client.force_login(self.user_a)
        r = self.client.get(reverse("course_list"))
        self.assertNotIn("40626610", self.listed_course_codes(r))
        self.assertNotContains(r, "資訊網路")

    def test_section_without_policy_for_own_class_is_hidden(self):
        """只有別的班級有政策、本班沒有的開課：列表看不到。"""
        self.client.force_login(self.user_a)
        r = self.client.get(reverse("course_list"))
        self.assertNotIn("40636390", self.listed_course_codes(r))
        self.assertNotContains(r, "社群大數據分析")

    def test_draft_policy_section_is_hidden(self):
        """政策仍是草稿：列表看不到。"""
        self.publish(self.open_source.section, self.group_a, can_enroll=True,
                     status=SectionClassPolicy.Status.DRAFT)
        self.client.force_login(self.user_a)
        r = self.client.get(reverse("course_list"))
        self.assertNotIn("40627960", self.listed_course_codes(r))

    def test_other_class_student_sees_own_class_open_policy_only(self):
        """三乙只看得見三乙自己的政策所指派的開課。"""
        self.client.force_login(self.user_b)
        r = self.client.get(reverse("course_list"))
        codes = self.listed_course_codes(r)
        self.assertIn("40636390", codes)
        self.assertNotIn("40627960", codes)
        self.assertNotIn("40626610", codes)

    def test_user_without_academic_profile_keeps_legacy_visibility(self):
        """尚未設定學籍的使用者沿用原本的顯示規則（不因新規則被清空）。"""
        self.client.force_login(self.user_no_profile)
        r = self.client.get(reverse("course_list"))
        codes = self.listed_course_codes(r)
        self.assertIn("40627960", codes)
        self.assertIn("40626610", codes)
        self.assertIn("40636390", codes)

    def test_enrolled_section_stays_visible_for_drop(self):
        """自己已加選的開課仍然看得到，才能退選。"""
        Enrollment.objects.create(user=self.user_a, section=self.required_source.section)
        self.client.force_login(self.user_a)
        r = self.client.get(reverse("course_list"))
        self.assertIn("40626610", self.listed_course_codes(r))

    def test_waitlisted_section_stays_visible_for_cancel(self):
        """自己在等待名單的開課仍然看得到，才能取消等待。"""
        Waitlist.objects.create(user=self.user_a, section=self.required_source.section)
        self.client.force_login(self.user_a)
        r = self.client.get(reverse("course_list"))
        self.assertIn("40626610", self.listed_course_codes(r))

    def test_empty_state_explains_reason_for_scoped_student(self):
        """有學籍但沒有任何可見班別時，仍然說明「目前沒有可加選的課程」。"""
        # 這個班級完全沒有政策：所有開課都看不到。
        group_c = AcademicClassGroup.objects.create(
            catalog=self.catalog, department=self.department, code="40640C",
            name_zh="四技資管四年丙班", grade_no=4)
        user_c = self.make_user("student_c", group=group_c)
        self.client.force_login(user_c)
        r = self.client.get(reverse("course_list"))
        self.assertEqual(self.listed_course_codes(r), set())
        self.assertContains(r, "目前沒有可加選的課程")


class DetailVisibilityTests(CourseVisibilityTestCase):
    """課程詳情頁的可見性：已發布的政策都看得到（含必修），未發布看不到。"""

    def test_required_policy_section_visible_on_detail_with_reason(self):
        """本班必修在詳情頁看得到，並標出不能加選的原因。"""
        self.client.force_login(self.user_a)
        r = self.client.get(reverse("course_detail", args=[self.required_source.course_id]))
        self.assertContains(r, "資訊網路")
        self.assertContains(r, "本班必修")

    def test_other_class_policy_section_hidden_on_detail(self):
        """只有別的班級有政策的開課，詳情頁也不看得到。"""
        self.client.force_login(self.user_a)
        r = self.client.get(reverse("course_detail", args=[self.other_source.course_id]))
        self.assertEqual(list(r.context["sections"]), [])
        self.assertContains(r, "這門課還沒開放任何班別。")

    def test_enrolled_section_visible_on_detail(self):
        """已加選的開課在詳情頁看得到。"""
        Enrollment.objects.create(user=self.user_a, section=self.other_source.section)
        self.client.force_login(self.user_a)
        r = self.client.get(reverse("course_detail", args=[self.other_source.course_id]))
        self.assertContains(r, "社群大數據分析")
