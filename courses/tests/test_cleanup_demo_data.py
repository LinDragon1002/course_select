"""`cleanup_demo_data` 指令的測試。

重點確認：

* 預設（dry-run）完全不寫入資料庫。
* `--apply` 只刪掉 `seed.py` 建立的示範資料。
* 真實資料（Google 帳號、學籍、官方開課、課表快照、修課政策、115-1 課程）不動。
* 可以重複執行：第二次執行不會出錯、也不會再刪任何東西。
* `seed` 不會搶走既有的當前學期。
"""

from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase

from ..models import (AcademicCatalog, AcademicClassGroup, AcademicDepartment,
                      AcademicProgramType, Course, Enrollment, NtubTimetableSnapshot,
                      Section, SectionClassPolicy, StudentAcademicProfile, Teacher,
                      Term)


class CleanupDemoDataTestCase(TestCase):
    """建立一個「有示範資料也有真實資料」的資料庫。"""

    def setUp(self):
        User = get_user_model()

        # --- 學期 ---
        self.term_1141 = Term.objects.create(code="114-1", is_current=True)
        self.term_1142 = Term.objects.create(code="114-2")
        self.term_1132 = Term.objects.create(code="113-2")
        self.term_1151 = Term.objects.create(code="115-1")

        # --- 示範課程（seed.DATA 的形狀）---
        self.demo_teacher = Teacher.objects.create(name="王大明")
        self.demo_course = Course.objects.create(
            term=self.term_1141, code="CS101", name="程式設計",
            teacher=self.demo_teacher, credits=3, syllabus="示範")
        self.demo_section = Section.objects.create(
            course=self.demo_course, label="A", day=1, start_period=1,
            end_period=3, room="A101", capacity=40)
        self.past_demo = Course.objects.create(
            term=self.term_1132, code="CS201", name="資料結構",
            teacher=self.demo_teacher, credits=3)
        self.past_demo_section = Section.objects.create(
            course=self.past_demo, label="A", day=2, start_period=2,
            end_period=4, room="A102", capacity=40)

        # --- 示範帳號 ---
        self.demo_user = User.objects.create_user("demo", email="demo@example.com")
        self.demo_fill_user = User.objects.create_user("demo_fill_1")
        Enrollment.objects.create(user=self.demo_user, section=self.demo_section)
        Enrollment.objects.create(user=self.demo_user, section=self.past_demo_section)
        Enrollment.objects.create(user=self.demo_fill_user, section=self.demo_section)

        # --- 真實資料 ---
        self.real_teacher = Teacher.objects.create(name="林俊杰")
        self.real_course = Course.objects.create(
            term=self.term_1151, code="40627960", name="網站應用程式設計",
            teacher=self.real_teacher, credits=3)
        self.real_section = Section.objects.create(
            course=self.real_course, label="A", day=4, start_period=2,
            end_period=4, room="電708", capacity=40)

        self.superuser = User.objects.create_superuser("admin", "admin@example.com", "pw")
        self.student = User.objects.create_user("11346064", "11346064@ntub.edu.tw")
        self.profile = StudentAcademicProfile.objects.create(
            user=self.student, class_group=self.make_class_group())

        # --- 官方課表快照 ---
        self.snapshot = NtubTimetableSnapshot.objects.create(
            class_group=self.profile.class_group, term=self.term_1151,
            class_name="四技資管三年乙班")

        # --- 修課政策 ---
        self.policy = SectionClassPolicy.objects.create(
            section=self.real_section, class_group=self.profile.class_group)

    def make_class_group(self):
        catalog = AcademicCatalog.objects.create(version="v-test")
        program = AcademicProgramType.objects.create(catalog=catalog, code="4", name_zh="四技日間部")
        department = AcademicDepartment.objects.create(
            catalog=catalog, program_type=program, code="406", name_zh="資訊管理系")
        return AcademicClassGroup.objects.create(
            catalog=catalog, department=department, code="40630B", name_zh="四技資管三年乙班")

    def run_cleanup(self, *args):
        out = StringIO()
        call_command("cleanup_demo_data", *args, stdout=out)
        return out.getvalue()


class DryRunTest(CleanupDemoDataTestCase):
    def test_default_run_does_not_write(self):
        before = (Course.objects.count(), Section.objects.count(),
                  Enrollment.objects.count(), Term.objects.count(),
                  get_user_model().objects.count())
        output = self.run_cleanup()
        after = (Course.objects.count(), Section.objects.count(),
                 Enrollment.objects.count(), Term.objects.count(),
                 get_user_model().objects.count())
        self.assertEqual(before, after)
        self.assertIn("dry-run", output)
        self.assertIn("CS101", output)

    def test_dry_run_flag_does_not_write(self):
        before = Course.objects.count()
        self.run_cleanup("--dry-run")
        self.assertEqual(before, Course.objects.count())

    def test_output_lists_every_demo_category(self):
        output = self.run_cleanup()
        for model in ("Course", "Section", "Enrollment", "Teacher", "Term", "User"):
            self.assertIn(model, output)


class ApplyTest(CleanupDemoDataTestCase):
    def test_apply_deletes_demo_data(self):
        self.run_cleanup("--apply")
        self.assertFalse(Course.objects.filter(code="CS101").exists())
        self.assertFalse(Course.objects.filter(term=self.term_1132).exists())
        self.assertFalse(Section.objects.filter(course__code="CS101").exists())
        self.assertFalse(Enrollment.objects.filter(user=self.demo_user).exists())
        self.assertFalse(get_user_model().objects.filter(username="demo").exists())
        self.assertFalse(get_user_model().objects.filter(username="demo_fill_1").exists())
        self.assertFalse(Teacher.objects.filter(name="王大明").exists())

    def test_apply_deletes_demo_terms(self):
        self.run_cleanup("--apply")
        self.assertFalse(Term.objects.filter(code="114-1").exists())
        self.assertFalse(Term.objects.filter(code="114-2").exists())
        self.assertFalse(Term.objects.filter(code="113-2").exists())
        self.assertTrue(Term.objects.filter(code="115-1").exists())

    def test_apply_keeps_real_data(self):
        self.run_cleanup("--apply")
        self.assertTrue(Course.objects.filter(code="40627960").exists())
        self.assertTrue(Section.objects.filter(course__code="40627960").exists())
        self.assertTrue(Teacher.objects.filter(name="林俊杰").exists())
        self.assertTrue(get_user_model().objects.filter(username="11346064").exists())
        self.assertTrue(get_user_model().objects.filter(username="admin").exists())
        self.assertTrue(StudentAcademicProfile.objects.filter(user=self.student).exists())
        self.assertTrue(NtubTimetableSnapshot.objects.filter(pk=self.snapshot.pk).exists())
        self.assertTrue(SectionClassPolicy.objects.filter(pk=self.policy.pk).exists())
        self.assertTrue(AcademicClassGroup.objects.filter(code="40630B").exists())
        self.assertTrue(AcademicDepartment.objects.filter(code="406").exists())
        self.assertTrue(AcademicProgramType.objects.filter(code="4").exists())
        self.assertTrue(AcademicCatalog.objects.filter(version="v-test").exists())

    def test_oauth_account_is_never_deleted(self):
        from allauth.socialaccount.models import SocialAccount
        SocialAccount.objects.create(user=self.student, provider="google", uid="uid-1")
        self.run_cleanup("--apply")
        self.assertTrue(get_user_model().objects.filter(username="11346064").exists())
        self.assertTrue(SocialAccount.objects.filter(user=self.student).exists())

    def test_demo_named_user_with_real_email_is_kept(self):
        """demo 帳號若帶了非示範信箱（例如真實信箱），不猜測、保留並回報。"""
        user = get_user_model().objects.create_user("demo_fill_9", "real.person@example.com")
        output = self.run_cleanup("--apply")
        self.assertTrue(get_user_model().objects.filter(pk=user.pk).exists())
        self.assertIn("demo_fill_9", output)

    def test_referenced_demo_section_is_kept(self):
        """若 Demo 班別被修課政策引用，就不能刪（安全閘）。"""
        SectionClassPolicy.objects.create(
            section=self.demo_section, class_group=self.profile.class_group)
        output = self.run_cleanup("--apply")
        self.assertTrue(Section.objects.filter(pk=self.demo_section.pk).exists())
        self.assertIn("待確認", output)

    def test_is_repeatable(self):
        self.run_cleanup("--apply")
        counts = (Course.objects.count(), Section.objects.count(),
                  Enrollment.objects.count(), Term.objects.count())
        output = self.run_cleanup("--apply")
        self.assertEqual(counts, (Course.objects.count(), Section.objects.count(),
                                  Enrollment.objects.count(), Term.objects.count()))
        self.assertIn("合計預計刪除 0 筆", output)


class SeedDoesNotStealCurrentTermTest(CleanupDemoDataTestCase):
    def test_seed_keeps_existing_current_term(self):
        Term.objects.update(is_current=False)
        Term.objects.filter(pk=self.term_1151.pk).update(is_current=True)
        call_command("seed", stdout=StringIO(), verbosity=0)
        self.assertTrue(Term.objects.get(pk=self.term_1151.pk).is_current)
        self.assertFalse(Term.objects.get(pk=self.term_1141.pk).is_current)

    def test_seed_sets_current_when_none_exists(self):
        Term.objects.update(is_current=False)
        call_command("seed", stdout=StringIO(), verbosity=0)
        self.assertTrue(Term.objects.get(pk=self.term_1141.pk).is_current)
