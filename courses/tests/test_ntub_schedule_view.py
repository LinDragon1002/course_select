"""NTUB 官方班級課表整合到 `/my/` 的測試。

測試對象：`courses/views.py` 的 `my_schedule()`、`courses/ntub_schedule_grid.py`
與 `courses/templates/courses/schedule.html`。

重點確認：

* 官方班級課表（`NtubTimetableSnapshot`）與個人加選（`Enrollment`）顯示在
  同一張週課表上。
* 學期一致性：官方快照與個人加選都只取 `Term.current()`，114-1 與 115-1
  不會混在一起。
* 兩個班級的課表不會互相混淆。
* 官方課表不會建立 `Enrollment`，官方活動也不會被當成必修或計入學分。
* 重疊的時段會標示衝突，且不會互相覆蓋。
* 缺少學籍或缺少官方快照時頁面仍然正常（不會 500）。

注意：這些測試跑在 Django 建立的隔離測試資料庫裡，所以可以在測試中把
115-1 設為當前學期，不會影響開發用的 SQLite 資料庫。
"""

import json
from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from ..models import (AcademicCatalog, AcademicClassGroup, AcademicDepartment,
                      AcademicProgramType, Course, Enrollment, NtubTimetableEntry,
                      NtubTimetableSnapshot, Section, StudentAcademicProfile,
                      Teacher, Term, OfficialTeachingSyllabus)
from ..ntub_schedule_grid import (KIND_ENROLLMENT, KIND_OFFICIAL_ACTIVITY,
                                  KIND_OFFICIAL_COURSE, GridItem,
                                  base_periods_for, build_week_grid)
from ..ntub_timetable_import import import_payload, load_payload

REPO_ROOT = Path(__file__).resolve().parents[2]
PAYLOAD_PATH = REPO_ROOT / "data" / "ntub_import_ready" / "timetables.json"


class ScheduleTestCase(TestCase):
    """共用的資料庫前置：真實的學籍目錄 + 真實的 115-1 官方課表。

    這裡刻意使用 `data/ntub_import_ready/timetables.json`（真實來源檔），
    讓測試看到的班級課表跟學校公布的一致，而不是自己捏造的資料。
    """

    def setUp(self):
        self.catalog = AcademicCatalog.objects.create(version="test-schedule",
                                                       source_key="ntub")
        self.program = AcademicProgramType.objects.create(
            catalog=self.catalog, code="4", name_zh="四技日間部")
        self.department = AcademicDepartment.objects.create(
            catalog=self.catalog, program_type=self.program, code="406",
            name_zh="四技資訊管理系")
        self.group_40620a = AcademicClassGroup.objects.create(
            catalog=self.catalog, department=self.department, code="40620A",
            name_zh="四技資管二年甲班", grade_no=2)
        self.group_40630b = AcademicClassGroup.objects.create(
            catalog=self.catalog, department=self.department, code="40630B",
            name_zh="四技資管三年乙班", grade_no=3)

        # 開發資料庫的當前學期是 114-1；測試資料庫裡另外建立 114-1 當作
        # 「舊學期」，用來確認不會跨學期混在一起。
        self.term_114 = Term.objects.create(code="114-1", is_current=False)
        self.term_115 = Term.objects.create(code="115-1", is_current=False)

        self.user = get_user_model().objects.create_user(
            "student", email="s1234567@ntub.edu.tw")
        self.teacher = Teacher.objects.create(name="王老師")
        # `/my/` 需要登入；這是本機建立的帳號（非 Google 登入），不受
        # 首次學籍設定中介層影響。
        self.client.force_login(self.user)

        if PAYLOAD_PATH.exists():
            import_payload(load_payload(PAYLOAD_PATH))

    def make_current(self, term):
        """把某個學期設為當前學期（只影響隔離的測試資料庫）。"""
        Term.objects.update(is_current=False)
        term.is_current = True
        term.save(update_fields=["is_current"])
        return term

    def profile_for(self, class_group, user=None):
        return StudentAcademicProfile.objects.create(
            user=user or self.user, class_group=class_group)

    def make_enrollment(self, term, code, name, day, start, end, label="A"):
        """在指定學期建立一門課並加選，回傳 Section。"""
        course = Course.objects.create(term=term, code=code, name=name,
                                       teacher=self.teacher, credits=3)
        section = Section.objects.create(course=course, label=label, day=day,
                                         start_period=start, end_period=end,
                                         capacity=40)
        Enrollment.objects.create(user=self.user, section=section)
        return section


class OfficialTimetableContentTests(ScheduleTestCase):
    """1. 兩個班級的官方課表內容正確（5 門／2 項、7 門／2 項）。"""

    def setUp(self):
        super().setUp()
        self.make_current(self.term_115)

    def test_40630b_has_five_courses_and_two_activities(self):
        snapshot = NtubTimetableSnapshot.objects.get(class_group=self.group_40630b)
        self.assertEqual(snapshot.term.code, "115-1")
        self.assertEqual(snapshot.entries.filter(entry_type="course").count(), 5)
        self.assertEqual(snapshot.entries.filter(entry_type="activity").count(), 2)

    def test_40620a_has_seven_courses_and_two_activities(self):
        snapshot = NtubTimetableSnapshot.objects.get(class_group=self.group_40620a)
        self.assertEqual(snapshot.term.code, "115-1")
        self.assertEqual(snapshot.entries.filter(entry_type="course").count(), 7)
        self.assertEqual(snapshot.entries.filter(entry_type="activity").count(), 2)

    def test_schedule_shows_40630b_courses(self):
        self.profile_for(self.group_40630b)
        r = self.client.get(reverse("my_schedule"))

        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "作業系統")
        self.assertContains(r, "智慧綠能")
        self.assertContains(r, "社群大數據分析")
        self.assertContains(r, "生活藝術賞析")
        self.assertContains(r, "體育三(上)")
        self.assertEqual(r.context["official_courses_count"], 5)

    def test_course_list_v21_uses_weekdays_and_source_toggle_markup(self):
        self.profile_for(self.group_40630b)
        response = self.client.get(reverse("course_list"))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(
            response, "本站為模擬選課系統，非 NTUB 正式校務選課紀錄。")
        self.assertContains(response, 'data-schedule-filter="official"')
        self.assertContains(response, 'data-schedule-filter="enrolled"')
        self.assertContains(response, 'aria-pressed="true"')
        self.assertNotContains(response, "週六")
        self.assertNotContains(response, "週日")
        self.assertNotContains(response, "作業系統")
        self.assertNotContains(response, "官方班級課程")
        self.assertNotContains(response, "展開更多進階限額")

    def test_course_list_credit_total_uses_existing_summary(self):
        self.profile_for(self.group_40630b)
        response = self.client.get(reverse("course_list"))

        self.assertContains(response, "學分上下限")
        self.assertContains(response, "總學分－已選學分")
        self.assertContains(
            response, f"{response.context['credit_summary'].total} 學分")

    def test_official_course_links_to_scoped_detail_page(self):
        self.profile_for(self.group_40630b)
        entry = NtubTimetableEntry.objects.get(
            snapshot__class_group=self.group_40630b,
            course_name="生活藝術賞析")
        OfficialTeachingSyllabus.objects.create(
            academic_year="115", semester="1", class_code="40630B",
            official_serial=entry.official_serial, course_code=entry.course_code,
            course_name="生活藝術賞析", course_name_en="APPRECIATION OF LIFE ART",
            credits="2.0", weekly_hours="2.0", teacher_name="張婕",
            teaching_target="<script>alert(1)</script>\n教學目標第二行",
            teaching_matter="課程內容",
            weekly_progress={"week_01": "表演藝術的性別平權", "week_18": "期末考"},
            assessment="平時30%", references="表演的藝術", status="success")
        response = self.client.get(reverse("my_schedule"))
        detail_url = reverse("official_course_detail", args=[entry.pk])
        self.assertContains(response, f'href="{detail_url}"')
        detail = self.client.get(detail_url)
        self.assertEqual(detail.status_code, 200)
        self.assertContains(detail, 'aria-label="麵包屑"')
        self.assertContains(detail, 'href="/my/"')
        self.assertContains(detail, "我的課表")
        self.assertContains(detail, "生活藝術賞析")
        self.assertNotContains(detail, "返回我的課表")
        self.assertNotContains(detail, "每週教學進度")
        self.assertContains(detail, "張婕")
        self.assertContains(detail, "表演藝術的性別平權")
        self.assertContains(detail, "期末考")
        self.assertContains(detail, "國立臺北商業大學 教學大綱")
        self.assertContains(detail, "項目")
        self.assertContains(detail, "內容")
        self.assertEqual(len(detail.context["weekly_rows"]), 18)
        self.assertEqual(detail.context["weekly_rows"][0]["label"], "第一週")
        self.assertEqual(detail.context["weekly_rows"][-1]["label"], "第十八週")
        self.assertContains(detail, 'class="official-syllabus-week-row"', count=18)
        self.assertContains(detail, "&lt;script&gt;alert(1)&lt;/script&gt;")
        self.assertNotContains(detail, "<script>alert(1)</script>")
        self.assertNotContains(detail, "查看官方原始教學大綱")

    def test_official_activity_is_not_a_detail_page(self):
        self.profile_for(self.group_40630b)
        activity = NtubTimetableEntry.objects.get(
            snapshot__class_group=self.group_40630b, entry_type="activity",
            course_name="共同空堂")
        self.assertEqual(
            self.client.get(reverse("official_course_detail", args=[activity.pk])).status_code,
            404)

    def test_official_course_detail_is_scoped_to_student_class(self):
        self.profile_for(self.group_40630b)
        other = NtubTimetableEntry.objects.filter(
            snapshot__class_group=self.group_40620a, entry_type="course").first()
        self.assertEqual(
            self.client.get(reverse("official_course_detail", args=[other.pk])).status_code,
            404)

    def test_schedule_shows_40630b_activities_as_gray_kind(self):
        self.profile_for(self.group_40630b)
        r = self.client.get(reverse("my_schedule"))

        self.assertContains(r, "共同空堂")
        self.assertContains(r, "課外活動")
        activities = [i for i in r.context["grid"].items
                      if i.kind == KIND_OFFICIAL_ACTIVITY]
        self.assertEqual(len(activities), 2)
        self.assertTrue(all(item.url is None for item in activities))
        activity_ids = NtubTimetableEntry.objects.filter(
            snapshot__class_group=self.group_40630b,
            entry_type="activity").values_list("pk", flat=True)
        for activity_id in activity_ids:
            self.assertNotContains(r, f'href="/official-course/{activity_id}/"')

    def test_schedule_shows_40620a_courses(self):
        self.profile_for(self.group_40620a)
        r = self.client.get(reverse("my_schedule"))

        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "資訊網路")
        self.assertContains(r, "財務管理")
        self.assertContains(r, "網站應用程式設計")
        self.assertContains(r, "資料庫管理")
        self.assertContains(r, "資料結構")
        self.assertContains(r, "數位內容概論")
        self.assertContains(r, "軍訓(選修)")
        self.assertEqual(r.context["official_courses_count"], 7)

    def test_elective_military_training_is_not_labelled_required(self):
        """「軍訓(選修)」是官方課程，但不能被說成全部都是必修。"""
        self.profile_for(self.group_40620a)
        r = self.client.get(reverse("my_schedule"))

        self.assertContains(r, "軍訓(選修)")
        self.assertContains(r, "班級官方課程")
        self.assertNotContains(r, "必修課程")
        self.assertNotContains(r, "軍訓(選修)必修")


class ClassIsolationTests(ScheduleTestCase):
    """2. 兩個班級的課表不會互相混淆。"""

    def setUp(self):
        super().setUp()
        self.make_current(self.term_115)

    def test_40620a_student_does_not_see_40630b_courses(self):
        self.profile_for(self.group_40620a)
        r = self.client.get(reverse("my_schedule"))

        self.assertNotContains(r, "作業系統")
        self.assertNotContains(r, "社群大數據分析")
        self.assertNotContains(r, "智慧綠能")

    def test_40630b_student_does_not_see_40620a_courses(self):
        self.profile_for(self.group_40630b)
        r = self.client.get(reverse("my_schedule"))

        self.assertNotContains(r, "資訊網路")
        self.assertNotContains(r, "財務管理")
        self.assertNotContains(r, "資料結構")

    def test_only_own_class_snapshot_is_used(self):
        self.profile_for(self.group_40630b)
        r = self.client.get(reverse("my_schedule"))

        self.assertEqual(r.context["snapshot"].class_group, self.group_40630b)
        self.assertEqual(r.context["official_courses_count"], 5)


class NoEnrollmentCreatedTests(ScheduleTestCase):
    """3. 官方課表不會建立 Enrollment。"""

    def setUp(self):
        super().setUp()
        self.make_current(self.term_115)

    def test_official_timetable_creates_no_enrollment(self):
        self.profile_for(self.group_40630b)

        self.client.get(reverse("my_schedule"))

        self.assertEqual(Enrollment.objects.count(), 0)
        self.assertEqual(Section.objects.count(), 0)

    def test_official_courses_use_automatic_credits_without_enrollment(self):
        """官方課程依課綱或節次計學分，但不建立 Enrollment。"""
        self.profile_for(self.group_40630b)
        r = self.client.get(reverse("my_schedule"))

        self.assertEqual(r.context["total"], 10)
        self.assertEqual(r.context["mine"], [])


class TermConsistencyTests(ScheduleTestCase):
    """4. 只載入與目前學期相符的官方快照與個人加選。"""

    def test_114_1_current_term_shows_no_official_snapshot(self):
        """目前學期是 114-1，但官方課表是 115-1：不能混著顯示。"""
        self.make_current(self.term_114)
        self.profile_for(self.group_40630b)

        r = self.client.get(reverse("my_schedule"))

        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.context["snapshot"])
        self.assertEqual(r.context["official_courses_count"], 0)
        self.assertNotContains(r, "社群大數據分析")

    def test_114_1_demo_enrollment_does_not_appear_in_115_1(self):
        """114-1 的加選不會混進 115-1 的課表。"""
        self.make_enrollment(self.term_114, "CS101", "程式設計", 1, 1, 3)
        self.make_current(self.term_115)
        self.profile_for(self.group_40630b)

        r = self.client.get(reverse("my_schedule"))

        self.assertEqual(r.context["mine"], [])
        self.assertNotContains(r, "程式設計")
        enrollment_items = [i for i in r.context["grid"].items
                            if i.kind == KIND_ENROLLMENT]
        self.assertEqual(enrollment_items, [])

    def test_115_1_enrollment_appears_with_115_1_official_snapshot(self):
        self.make_enrollment(self.term_115, "CS101", "程式設計", 1, 1, 3)
        self.make_current(self.term_115)
        self.profile_for(self.group_40630b)

        r = self.client.get(reverse("my_schedule"))

        self.assertContains(r, "程式設計")
        self.assertEqual(len(r.context["mine"]), 1)
        self.assertIsNotNone(r.context["snapshot"])
        self.assertEqual(r.context["snapshot"].term.code, "115-1")

    def test_snapshot_of_other_term_is_not_used(self):
        """同一班級在別的學期有快照，也只顯示當前學期那份。"""
        self.make_current(self.term_114)
        self.profile_for(self.group_40630b)

        r = self.client.get(reverse("my_schedule"))

        self.assertIsNone(r.context["snapshot"])

    def test_mixed_terms_do_not_appear_together(self):
        """114-1 加選 + 115-1 官方課表：當前學期只認 115-1。"""
        self.make_enrollment(self.term_114, "CS101", "舊學期課程", 1, 1, 3)
        self.make_current(self.term_115)
        self.profile_for(self.group_40630b)

        r = self.client.get(reverse("my_schedule"))

        self.assertNotContains(r, "舊學期課程")
        self.assertContains(r, "作業系統")


class MissingDataTests(ScheduleTestCase):
    """5. 缺少學籍或官方快照時不會 500。"""

    def setUp(self):
        super().setUp()
        self.make_current(self.term_115)

    def test_user_without_academic_profile_gets_page(self):
        r = self.client.get(reverse("my_schedule"))

        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.context["profile"])
        self.assertIsNone(r.context["snapshot"])
        self.assertContains(r, "尚未設定學籍")

    def test_profile_without_snapshot_gets_explanation(self):
        """有學籍但這個學期沒有官方快照時，要說清楚原因。"""
        # 這個班級在來源檔裡沒有課表，所以不會有快照。
        group_without_snapshot = AcademicClassGroup.objects.create(
            catalog=self.catalog, department=self.department, code="40640C",
            name_zh="四技資管四年丙班", grade_no=4)
        self.profile_for(group_without_snapshot)
        r = self.client.get(reverse("my_schedule"))

        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.context["snapshot"])
        self.assertContains(r, "尚無已發布的班級固定課表")

    def test_empty_schedule_without_any_data(self):
        r = self.client.get(reverse("my_schedule"))

        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context["grid"].items, [])
        self.assertGreaterEqual(r.context["grid"].periods, 1)


class RenderingTests(ScheduleTestCase):
    """6. 顏色、連續節次、衝突標示都正確。"""

    def setUp(self):
        super().setUp()
        self.make_current(self.term_115)

    def test_official_course_uses_yellow_class(self):
        self.profile_for(self.group_40630b)
        r = self.client.get(reverse("my_schedule"))

        self.assertContains(r, "wk-slot--official_course")
        self.assertContains(r, "wk-key--official")

    def test_official_activity_uses_gray_class(self):
        self.profile_for(self.group_40630b)
        r = self.client.get(reverse("my_schedule"))

        self.assertContains(r, "wk-slot--official_activity")
        self.assertContains(r, "wk-key--activity")

    def test_enrollment_uses_blue_class(self):
        self.make_enrollment(self.term_115, "CS101", "程式設計", 1, 1, 3)
        self.profile_for(self.group_40630b)
        r = self.client.get(reverse("my_schedule"))

        self.assertContains(r, "wk-slot--enrollment")
        self.assertContains(r, "wk-key--enrolled")

    def test_official_and_enrolled_render_on_same_grid(self):
        self.make_enrollment(self.term_115, "CS101", "程式設計", 6, 1, 3)
        self.profile_for(self.group_40630b)
        r = self.client.get(reverse("my_schedule"))

        kinds = {i.kind for i in r.context["grid"].items}
        self.assertIn(KIND_OFFICIAL_COURSE, kinds)
        self.assertIn(KIND_ENROLLMENT, kinds)

    def test_multi_period_course_keeps_continuous_span(self):
        """3 節的課在版面裡是同一格、跨 3 節，不會被拆開。"""
        self.profile_for(self.group_40630b)
        r = self.client.get(reverse("my_schedule"))

        items = [i for i in r.context["grid"].items if i.title == "作業系統"]
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item.start_period, 5)
        self.assertEqual(item.end_period, 7)
        self.assertEqual(item.span, 3)
        self.assertEqual(item.row_offset, 4)

    def test_multi_period_enrollment_keeps_continuous_span(self):
        self.make_enrollment(self.term_115, "CS101", "程式設計", 6, 2, 4)
        self.profile_for(self.group_40630b)
        r = self.client.get(reverse("my_schedule"))

        items = [i for i in r.context["grid"].items if i.title == "程式設計"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].span, 3)
        self.assertEqual(items[0].row_offset, 1)

    def test_conflicting_enrollment_is_marked_and_not_covered(self):
        """個人加選撞到官方課程時：兩者都看得到，且都標示衝突。"""
        # 40630B 週三第 5~7 節是「作業系統」
        self.make_enrollment(self.term_115, "CS101", "撞課的課", 3, 6, 7)
        self.profile_for(self.group_40630b)
        r = self.client.get(reverse("my_schedule"))

        grid = r.context["grid"]
        self.assertTrue(grid.has_conflict)
        official = [i for i in grid.items if i.title == "作業系統"][0]
        enrolled = [i for i in grid.items if i.title == "撞課的課"][0]
        self.assertTrue(official.conflict)
        self.assertTrue(enrolled.conflict)
        # 兩者被分到不同欄位，不會互相覆蓋
        self.assertNotEqual(official.lane, enrolled.lane)
        self.assertEqual(official.lanes, 2)
        self.assertEqual(enrolled.lanes, 2)
        self.assertContains(r, "wk-slot--conflict")
        self.assertContains(r, "衝突")

    def test_official_activity_does_not_create_false_conflict(self):
        """官方活動只是時間框架，不是必修，不該跟個人加選判成衝突。"""
        # 40630B 週一第 8~9 節是「共同空堂」（activity）
        self.make_enrollment(self.term_115, "CS101", "空堂時段的課", 1, 8, 9)
        self.profile_for(self.group_40630b)
        r = self.client.get(reverse("my_schedule"))

        grid = r.context["grid"]
        self.assertFalse(grid.has_conflict)

    def test_days_cover_monday_to_sunday(self):
        self.profile_for(self.group_40630b)
        r = self.client.get(reverse("my_schedule"))

        labels = [label for _, label in r.context["grid"].days]
        self.assertEqual(labels, ["週一", "週二", "週三", "週四", "週五", "週六", "週日"])

    def test_base_periods_for_four_year_day_program(self):
        """四技日間部基底是第 1～9 節。"""
        self.profile_for(self.group_40630b)
        r = self.client.get(reverse("my_schedule"))

        self.assertEqual(base_periods_for("4"), 9)
        self.assertEqual(r.context["grid"].periods, 9)

    def test_unknown_program_type_has_no_base_periods(self):
        self.assertEqual(base_periods_for("S"), 0)
        self.assertEqual(base_periods_for(None), 0)


class GridUnitTests(TestCase):
    """`ntub_schedule_grid` 的版面計算。"""

    def test_lanes_separate_overlapping_items(self):
        items = [
            GridItem(kind=KIND_OFFICIAL_COURSE, day=1, start_period=1,
                     end_period=2, title="A"),
            GridItem(kind=KIND_ENROLLMENT, day=1, start_period=2,
                     end_period=3, title="B"),
        ]
        grid = build_week_grid(items)

        self.assertEqual(grid.lanes_per_day[1], 2)
        self.assertEqual([i.lane for i in grid.items], [0, 1])

    def test_non_overlapping_items_share_a_lane(self):
        items = [
            GridItem(kind=KIND_OFFICIAL_COURSE, day=1, start_period=1,
                     end_period=2, title="A"),
            GridItem(kind=KIND_ENROLLMENT, day=1, start_period=3,
                     end_period=4, title="B"),
        ]
        grid = build_week_grid(items)

        self.assertEqual(grid.lanes_per_day[1], 1)
        self.assertEqual([i.lane for i in grid.items], [0, 0])
        self.assertFalse(grid.has_conflict)

    def test_conflict_is_marked_for_overlapping_courses(self):
        items = [
            GridItem(kind=KIND_OFFICIAL_COURSE, day=1, start_period=1,
                     end_period=3, title="A"),
            GridItem(kind=KIND_ENROLLMENT, day=1, start_period=2,
                     end_period=4, title="B"),
        ]
        grid = build_week_grid(items)

        self.assertTrue(grid.has_conflict)
        self.assertEqual(grid.conflict_count, 2)

    def test_base_periods_extend_the_grid(self):
        grid = build_week_grid(
            [GridItem(kind=KIND_OFFICIAL_COURSE, day=1, start_period=1,
                      end_period=2, title="A")],
            base_periods=9)

        self.assertEqual(grid.periods, 9)

    def test_actual_periods_extend_beyond_base(self):
        grid = build_week_grid(
            [GridItem(kind=KIND_ENROLLMENT, day=1, start_period=10,
                      end_period=13, title="A")],
            base_periods=9)

        self.assertEqual(grid.periods, 13)

    def test_day_columns_are_ready_for_the_template(self):
        grid = build_week_grid(
            [GridItem(kind=KIND_OFFICIAL_COURSE, day=2, start_period=1,
                      end_period=2, title="A")])

        columns = grid.day_columns
        self.assertEqual(len(columns), 7)
        self.assertEqual(columns[0][0], 1)
        self.assertEqual(columns[1][2][0].title, "A")
        self.assertEqual(columns[2][2], [])
