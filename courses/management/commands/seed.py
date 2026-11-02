from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.utils import timezone

from courses.models import Course, Enrollment, Section, Teacher, Term

# (課號, 課名, 老師, 學分, 大綱, [(班別, 星期, 起始, 結束, 教室, 名額), ...])
DATA = [
    ("CS101", "程式設計", "王大明", 3,
     "變數、流程控制、函式、基礎資料結構。\n評分：作業 40%、期中 30%、期末 30%",
     [("A", 1, 1, 3, "A101", 40), ("B", 3, 1, 3, "A101", 20)]),
    ("CS201", "資料結構", "王大明", 3,
     "陣列、鏈結串列、堆疊、佇列、樹與圖。",
     [("A", 2, 2, 4, "A102", 40), ("B", 4, 2, 4, "A102", 15)]),
    ("CS301", "資料庫系統", "李小華", 3,
     "關聯式模型、SQL、正規化、交易。",
     [("A", 3, 3, 5, "B201", 30)]),
    ("CS302", "網頁程式設計", "李小華", 3,
     "HTTP、Django、模板與 ORM，期末繳交小型專題。",
     [("A", 3, 6, 8, "B202", 30)]),
    ("CS401", "機器學習", "陳美玲", 3,
     "監督式學習、評估指標、簡單神經網路。",
     [("A", 4, 2, 4, "C301", 25), ("B", 1, 6, 8, "C301", 25)]),
    ("MA101", "微積分", "林志明", 3,
     "極限、微分、積分與應用。",
     [("A", 1, 2, 4, "D101", 60)]),
    ("MA201", "線性代數", "林志明", 3,
     "向量空間、矩陣、特徵值。",
     [("A", 5, 1, 3, "D102", 50)]),
    ("EN101", "英文閱讀與寫作", "Sarah Chen", 2,
     "學術閱讀與短文寫作練習。",
     [("A", 4, 6, 7, "E101", 35)]),
    ("PE101", "游泳", "張大偉", 1,
     "基礎泳姿與水中安全。",
     [("A", 5, 6, 7, "體育館", 30), ("B", 5, 8, 9, "體育館", 3)]),
    ("GE101", "設計思考", "陳美玲", 2,
     "以小組專案練習同理、發想與原型。",
     [("A", 2, 6, 7, "E202", 24)]),
]


class Command(BaseCommand):
    help = "載入範例課程與班別（可重複執行）"

    def handle(self, *args, **opts):
        now = timezone.now()
        # 示範學期 114-1 只有在「目前沒有任何當前學期」時才會成為當前學期。
        # 正式的本機開發資料庫已經有真實的當前學期（例如 115-1），此時再跑 seed
        # 不應該把它搶走——否則學生會突然看到示範課程。清除示範資料請用
        # `manage.py cleanup_demo_data`。
        steal_current = not Term.objects.filter(is_current=True).exists()
        term, _ = Term.objects.update_or_create(code="114-1", defaults={
            "is_current": steal_current,
            "enroll_open": now - timedelta(days=1),    # 昨天開放
            "enroll_close": now + timedelta(days=14),  # 兩週後截止 → 「加選中」
        })
        if not steal_current:
            self.stdout.write(
                self.style.WARNING(
                    "資料庫已有當前學期，示範學期 114-1 不會被設為當前學期（避免蓋掉真實資料）。"))
        for code, name, teacher, cr, syl, sections in DATA:
            t, _ = Teacher.objects.get_or_create(name=teacher)
            course, _ = Course.objects.update_or_create(term=term, code=code,
                                                        defaults=dict(name=name, teacher=t, credits=cr, syllabus=syl))
            for label, day, s, e, room, cap in sections:
                Section.objects.update_or_create(course=course, label=label,
                                                  defaults=dict(day=day, start_period=s, end_period=e, room=room, capacity=cap))
        # 等待名單演示：PE101 B 班（容量 3）額滿，加選會進等待名單
        full = Section.objects.get(course__term=term, course__code="PE101", label="B")
        for i in range(1, full.capacity + 1):
            u, _ = get_user_model().objects.get_or_create(username=f"demo_fill_{i}")
            Enrollment.objects.get_or_create(user=u, section=full)
        self.stdout.write(f"已載入 {len(DATA)} 門課到 {term.code}（選課加選中、PE101 B 已額滿）")
        future, _ = Term.objects.update_or_create(code="114-2", defaults={
            "is_current": False,
            "enroll_open": now + timedelta(days=30),   # 尚未開放（admin 可演示）
            "enroll_close": now + timedelta(days=44),
        })
        self.stdout.write(f"另建未來學期 {future.code}（選課未開放，供演示）")

        # 過去學期＋先修鏈＋演示帳號：demo 在 113-2 修過 CS201
        past, _ = Term.objects.update_or_create(code="113-2", defaults={"is_current": False})
        teacher_wd, _ = Teacher.objects.get_or_create(name="王大明")
        past_cs201, _ = Course.objects.update_or_create(term=past, code="CS201",
                                                        defaults=dict(name="資料結構", teacher=teacher_wd,
                                                                      credits=3, syllabus="（113-2 舊課，供先修示範）"))
        past_sec, _ = Section.objects.update_or_create(course=past_cs201, label="A",
                                                       defaults=dict(day=2, start_period=2, end_period=4,
                                                                     room="A102", capacity=40))
        user_model = get_user_model()
        demo, _ = user_model.objects.get_or_create(username="demo", defaults={"email": "demo@example.com"})
        if not demo.check_password("demo1234"):
            demo.set_password("demo1234")
            demo.save()
        Enrollment.objects.get_or_create(user=demo, section=past_sec)
        Course.objects.get(term=term, code="CS301").prerequisites.set([past_cs201])
        Course.objects.get(term=term, code="CS401").prerequisites.set(
            [Course.objects.get(term=term, code="CS301")])
        self.stdout.write("先修示範：demo（密碼 demo1234）113-2 修過 CS201 → CS301 可選、CS401 被擋")
