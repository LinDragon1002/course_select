"""`audit_class_offerings` 指令的測試。

這支指令是**唯讀**稽核：只從 `SectionClassPolicy` 讀出某學期、某學生班級
真正開放的可選課程，絕不修改任何資料。

測試重點：

* 可選課程只認「已發布 + can_enroll=True」的政策。
* 草稿（DRAFT）與已發布但 can_enroll=False 都不列入可選課程。
* 同一門課不因多筆政策重複計數。
* 開課來源班級不等於學生班級時仍然列得出來。
* `--expected-codes` 完全一致 → PASS；不一致 → 顯示差異並以非零退出碼結束。
* 班級／學期不存在時明確報錯。
* 執行前後政策與 Enrollment 筆數完全不變。
"""

from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from ..models import (AcademicCatalog, AcademicClassGroup, AcademicDepartment,
                      AcademicProgramType, Course, Enrollment, NtubOfferingSource,
                      Section, SectionClassPolicy, Teacher, Term)


class AuditClassOfferingsTestCase(TestCase):
    """共用前置：115-1 學期、兩個班級、三門官方開課。"""

    def setUp(self):
        self.catalog = AcademicCatalog.objects.create(version="test-audit", source_key="ntub")
        self.program = AcademicProgramType.objects.create(
            catalog=self.catalog, code="4", name_zh="四技日間部")
        self.department = AcademicDepartment.objects.create(
            catalog=self.catalog, program_type=self.program, code="406", name_zh="四技資訊管理系")
        self.group_student = AcademicClassGroup.objects.create(
            catalog=self.catalog, department=self.department, code="40630B",
            name_zh="四技資管三年乙班", grade_no=3)
        self.group_source = AcademicClassGroup.objects.create(
            catalog=self.catalog, department=self.department, code="40620A",
            name_zh="四技資管二年甲班", grade_no=2)

        self.term = Term.objects.create(code="115-1", is_current=True)
        self.teacher = Teacher.objects.create(name="林俊杰")

        # 三門官方開課：開課來源班級都是 40620A（不等於學生班級 40630B）
        self.web = self.offering("40627960", "網站應用程式設計", day=4, start=2, end=4, serial="19")
        self.srv = self.offering("40637100", "伺服器架設與規劃", day=1, start=5, end=7, serial="21")
        self.case = self.offering("40635420", "資管個案研討", day=2, start=2, end=4, serial="23")

    # --- 建立資料的小工具 -----------------------------------------------------

    def offering(self, code, name, day, start, end, serial,
                 class_group=None, teacher=None, room="A4708", capacity=51):
        """建立一筆官方開課（課程 + 班別 + NtubOfferingSource 對應）。"""
        class_group = class_group or self.group_source
        teacher = teacher or self.teacher
        course = Course.objects.create(term=self.term, code=code, name=name,
                                       teacher=teacher, credits=3)
        section = Section.objects.create(course=course, label="A", day=day,
                                         start_period=start, end_period=end,
                                         room=room, capacity=capacity)
        NtubOfferingSource.objects.create(
            source_key=f"115|1|{class_group.code}|{serial}|{code}",
            academic_year="115", semester="1", class_code=class_group.code,
            official_serial=serial, course_code=code, term=self.term,
            class_group=class_group, course=course, section=section,
            teacher=teacher, teacher_name=teacher.name, course_name=name,
            classroom_name=room, official_capacity=capacity, official_enrolled=0,
            source_url=f"https://example.test/{code}", source_file="offerings.json",
            applied_day=day, applied_start_period=start, applied_end_period=end,
            applied_room=room, applied_capacity=capacity)
        return section

    def policy(self, section, can_enroll=True,
               status=SectionClassPolicy.Status.PUBLISHED,
               class_group=None):
        return SectionClassPolicy.objects.create(
            section=section, class_group=class_group or self.group_student,
            course_category=SectionClassPolicy.CourseCategory.PROFESSIONAL_ELECTIVE,
            can_enroll=can_enroll, status=status)

    def run_audit(self, expected_codes=None, term="115-1", class_code="40630B"):
        """執行指令並回傳 (輸出, 錯誤訊息)；成功時錯誤訊息是 None。"""
        out = StringIO()
        err = StringIO()
        kwargs = {}
        if expected_codes is not None:
            kwargs["expected_codes"] = expected_codes
        try:
            call_command("audit_class_offerings", term=term, class_code=class_code,
                         stdout=out, stderr=err, **kwargs)
        except CommandError as exc:
            return out.getvalue(), str(exc)
        return out.getvalue(), None

    def run_audit_ok(self, expected_codes=None, term="115-1", class_code="40630B"):
        out = StringIO()
        kwargs = {}
        if expected_codes is not None:
            kwargs["expected_codes"] = expected_codes
        call_command("audit_class_offerings", term=term, class_code=class_code,
                     stdout=out, **kwargs)
        return out.getvalue()


class AuditOutputTests(AuditClassOfferingsTestCase):
    """第一部分：輸出內容。"""

    def test_three_published_policies_output_three_courses(self):
        """三筆已發布政策 → 正確輸出三門可選課程。"""
        self.policy(self.web)
        self.policy(self.srv)
        self.policy(self.case)
        out = self.run_audit_ok()
        self.assertIn("已發布且可加選的課程數：3", out)
        for code in ("40627960", "40637100", "40635420"):
            self.assertIn(code, out)
        self.assertIn("網站應用程式設計", out)
        self.assertIn("伺服器架設與規劃", out)
        self.assertIn("資管個案研討", out)

    def test_output_contains_term_class_teacher_and_time(self):
        """輸出要包含學期、班級代碼與名稱、教師與上課時間。"""
        self.policy(self.web)
        out = self.run_audit_ok()
        self.assertIn("學期：115-1", out)
        self.assertIn("40630B", out)
        self.assertIn("四技資管三年乙班", out)
        self.assertIn("林俊杰", out)
        self.assertIn("週四 第2-4節", out)

    def test_draft_policy_is_not_counted(self):
        """未發布的 DRAFT 不列入可選課程。"""
        self.policy(self.web, status=SectionClassPolicy.Status.DRAFT)
        out = self.run_audit_ok()
        self.assertIn("已發布且可加選的課程數：0", out)
        self.assertNotIn("40627960", out)
        self.assertIn("草稿政策數（不列入可選課程）：1", out)

    def test_published_but_cannot_enroll_is_not_counted(self):
        """已發布但 can_enroll=False 不列入可選課程。"""
        self.policy(self.web, can_enroll=False)
        out = self.run_audit_ok()
        self.assertIn("已發布且可加選的課程數：0", out)
        self.assertNotIn("40627960", out)
        self.assertIn("已發布但 can_enroll=False 的數量：1", out)

    def test_same_course_is_not_double_counted(self):
        """同一門課查到多筆政策時只算一次。"""
        # 同一門課再開一個班別，兩邊都對 40630B 開放 → 仍只有一門課
        second = Section.objects.create(course=self.web.course, label="B", day=5,
                                        start_period=7, end_period=9, room="A4709")
        self.policy(self.web)
        self.policy(second)
        out = self.run_audit_ok()
        self.assertIn("已發布且可加選的課程數：1", out)
        self.assertEqual(out.count("40627960 網站應用程式設計"), 1)

    def test_source_class_different_from_student_class_is_listed(self):
        """開課來源班級不等於學生班級時仍然列得出來，並標示來源班級。"""
        self.policy(self.web)
        out = self.run_audit_ok()
        self.assertIn("40627960", out)
        self.assertIn("40620A", out)
        self.assertIn("四技資管二年甲班", out)
        self.assertIn("開課來源班級", out)

    def test_no_policy_explains_clearly(self):
        """沒有正式政策時要明確說明。"""
        out = self.run_audit_ok()
        self.assertIn("已發布且可加選的課程數：0", out)
        self.assertIn("沒有任何 SectionClassPolicy 紀錄", out)

    def test_curriculum_recommendation_is_not_treated_as_enrollable(self):
        """課綱推薦不算學生可選課程。"""
        from ..models import NtubCurriculumRecommendation
        NtubCurriculumRecommendation.objects.create(
            admission_year="115", program_code="4", program_name="四技日間部",
            department_code="406", department_name="四技資訊管理系",
            grade=3, semester="1", class_code="40630B",
            course_name="伺服器架設與規劃", match_status="matched",
            offering=NtubOfferingSource.objects.get(course_code="40637100"),
            course=self.srv.course, class_group=self.group_student)
        out = self.run_audit_ok()
        self.assertIn("已發布且可加選的課程數：0", out)
        self.assertNotIn("40637100", out)

    def test_policy_of_other_class_is_not_counted(self):
        """別的班級的政策不算這個班級的可選課程。"""
        self.policy(self.web, class_group=self.group_source)
        out = self.run_audit_ok()
        self.assertIn("已發布且可加選的課程數：0", out)


class ExpectedCodesTests(AuditClassOfferingsTestCase):
    """第二部分：--expected-codes 比對。"""

    def setUp(self):
        super().setUp()
        self.policy(self.web)
        self.policy(self.srv)
        self.policy(self.case)

    def test_exact_match_passes(self):
        out = self.run_audit_ok(expected_codes="40637100,40635420,40627960")
        self.assertIn("PASS", out)
        self.assertNotIn("FAIL", out)

    def test_mismatch_reports_difference_and_fails(self):
        """不一致時列出缺少與多出，並以 CommandError（非零退出碼）結束。"""
        out, message = self.run_audit(expected_codes="40627960,40637100,40699999")
        self.assertIn("FAIL", out)
        self.assertIn("40699999", out)
        self.assertIn("缺少", out)
        self.assertIn("40635420", out)
        self.assertIn("多出", out)
        self.assertIn("可選課號比對失敗", message)

    def test_missing_only_reports_missing(self):
        out, message = self.run_audit(
            expected_codes="40627960,40637100,40635420,40688888")
        self.assertIn("FAIL", out)
        self.assertIn("40688888", out)
        self.assertIn("缺少", out)
        self.assertNotIn("多出（", out)

    def test_extra_only_reports_extra(self):
        out, message = self.run_audit(expected_codes="40627960,40637100")
        self.assertIn("FAIL", out)
        self.assertIn("多出", out)
        self.assertIn("40635420", out)
        self.assertNotIn("缺少（", out)

    def test_without_expected_codes_no_comparison(self):
        out = self.run_audit_ok()
        self.assertNotIn("PASS", out)
        self.assertNotIn("FAIL", out)


class ErrorTests(AuditClassOfferingsTestCase):
    """第三部分：不存在的班級／學期。"""

    def test_unknown_class_code_raises(self):
        with self.assertRaises(CommandError) as ctx:
            self.run_audit_ok(class_code="ZZZZZZ")
        self.assertIn("查無班級代碼", str(ctx.exception))

    def test_unknown_term_raises(self):
        with self.assertRaises(CommandError) as ctx:
            self.run_audit_ok(term="999-9")
        self.assertIn("查無學期", str(ctx.exception))


class ReadOnlyTests(AuditClassOfferingsTestCase):
    """第四部分：指令完全唯讀。"""

    def test_database_counts_unchanged_after_run(self):
        self.policy(self.web)
        self.policy(self.srv, can_enroll=False)
        self.policy(self.case, status=SectionClassPolicy.Status.DRAFT)

        user = self.make_user()
        Enrollment.objects.create(user=user, section=self.web)

        before = {
            "policies": SectionClassPolicy.objects.count(),
            "enrollments": Enrollment.objects.count(),
            "courses": Course.objects.count(),
            "sections": Section.objects.count(),
            "sources": NtubOfferingSource.objects.count(),
        }
        self.run_audit_ok(expected_codes="40627960")
        after = {
            "policies": SectionClassPolicy.objects.count(),
            "enrollments": Enrollment.objects.count(),
            "courses": Course.objects.count(),
            "sections": Section.objects.count(),
            "sources": NtubOfferingSource.objects.count(),
        }
        self.assertEqual(before, after)
        self.assertEqual(after["policies"], 3)
        self.assertEqual(after["enrollments"], 1)

    def test_failed_comparison_also_leaves_data_untouched(self):
        self.policy(self.web)
        before = SectionClassPolicy.objects.count()
        self.run_audit(expected_codes="40600000")
        self.assertEqual(SectionClassPolicy.objects.count(), before)

    def test_current_term_is_not_switched(self):
        """稽核不切換目前學期。"""
        self.policy(self.web)
        self.run_audit_ok()
        self.assertTrue(Term.objects.get(code="115-1").is_current)
        self.assertEqual(Term.objects.filter(is_current=True).count(), 1)

    def make_user(self):
        from django.contrib.auth import get_user_model
        return get_user_model().objects.create_user("auditee")
