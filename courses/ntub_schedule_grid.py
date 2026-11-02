"""把「班級官方課表」與「個人加選」合併成同一張週課表。

這是純資料的版面計算模組（不碰資料庫）：把兩種來源的時段換算成同一組
「星期 × 節次」的版面，並處理三件事：

1. **保留連續節次**：一門 3 節的課在格子裡佔 3 節，不會被拆成三塊。
2. **不互相覆蓋**：同一天重疊的時段會分到不同的並排欄位（lane），
   絕不會有一門課把另一門課蓋掉。
3. **標示衝突**：重疊的時段會被標成衝突，讓學生看得出時間打架。

來源分類（`kind`）：

* `official_course`：班級官方課程（淺黃色）。
* `official_activity`：官方活動，例如共同空堂、課外活動（淺灰色）。
* `enrollment`：個人加選（淺藍色）。
* `official_enrollment`：同一門課既有班級官方課表、又有本站加選紀錄，
  合併成一個時段顯示（淺綠色），官方與本站的資料都保留。

官方活動只是課表的時間框架，不代表必修，也不計入任何學分。
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: 星期一至星期日。
DAYS = [(1, "週一"), (2, "週二"), (3, "週三"), (4, "週四"),
        (5, "週五"), (6, "週六"), (7, "週日")]

KIND_OFFICIAL_COURSE = "official_course"
KIND_OFFICIAL_ACTIVITY = "official_activity"
KIND_ENROLLMENT = "enrollment"
KIND_OFFICIAL_ENROLLMENT = "official_enrollment"

#: 顯示用的中文名稱（模板與測試共用，避免兩邊各寫一套）。
KIND_LABELS = {
    KIND_OFFICIAL_COURSE: "班級官方課程",
    KIND_OFFICIAL_ACTIVITY: "官方活動",
    KIND_ENROLLMENT: "個人加選",
    KIND_OFFICIAL_ENROLLMENT: "官方課程＋本站加選",
}

#: 四技日間部的基底節次：第 1～9 節。
BASE_PERIODS_FOUR_YEAR_DAY = 9

#: 各學制的基底節次。找不到對應學制時不設基底，只依實際排到的節次顯示。
PROGRAM_BASE_PERIODS = {"4": BASE_PERIODS_FOUR_YEAR_DAY}

#: 算得出「學分」的課程類型；官方活動只是時間框架，不列入衝突判斷。
COURSE_KINDS = (KIND_OFFICIAL_COURSE, KIND_ENROLLMENT, KIND_OFFICIAL_ENROLLMENT)


def base_periods_for(program_type_code: str | None) -> int:
    """該學制的基底節次；沒有對應設定回 0（完全依實際課表決定）。"""
    if not program_type_code:
        return 0
    return PROGRAM_BASE_PERIODS.get(str(program_type_code), 0)


@dataclass
class GridItem:
    """課表上的一個時段（官方課程、官方活動或個人加選）。"""

    kind: str
    day: int
    start_period: int
    end_period: int
    title: str
    subtitle: str = ""
    meta: str = ""
    url: str = ""
    lane: int = 0
    lanes: int = 1
    conflict: bool = False

    @property
    def span(self) -> int:
        """佔幾節（含頭尾）。"""
        return max(1, self.end_period - self.start_period + 1)

    @property
    def row_offset(self) -> int:
        """從第 1 節起算的位移（0 表示第 1 節）。"""
        return max(0, self.start_period - 1)

    @property
    def kind_label(self) -> str:
        return KIND_LABELS.get(self.kind, self.kind)

    @property
    def left_pct(self) -> float:
        """在該天欄位中的左側百分比（並排欄位用）。"""
        return round(self.lane * 100 / max(1, self.lanes), 4)

    @property
    def width_pct(self) -> float:
        """在該天欄位中的寬度百分比。"""
        return round(100 / max(1, self.lanes), 4)


@dataclass
class WeekGrid:
    """整張週課表的版面。"""

    periods: int
    days: list[tuple[int, str]]
    lanes_per_day: dict[int, int]
    items: list[GridItem]
    by_day: dict[int, list[GridItem]] = field(default_factory=dict)

    @property
    def conflict_count(self) -> int:
        return sum(1 for i in self.items if i.conflict)

    @property
    def has_conflict(self) -> bool:
        return self.conflict_count > 0

    @property
    def period_labels(self) -> list[int]:
        """節次標籤（1 到 periods）。"""
        return list(range(1, self.periods + 1))

    @property
    def day_columns(self) -> list[tuple[int, str, list[GridItem]]]:
        """每一天一列：(星期代碼, 星期名稱, 該天的時段)。

        模板直接迭代這個結構，不需要在模板裡做字典查詢。
        """
        return [(day_no, label, list(self.by_day.get(day_no, [])))
                for day_no, label in self.days]

    def items_of(self, kind: str) -> list[GridItem]:
        return [i for i in self.items if i.kind == kind]


def _overlaps(a: GridItem, b: GridItem) -> bool:
    """兩個時段是否在同一天重疊。"""
    return (a.day == b.day and a.start_period <= b.end_period
            and b.start_period <= a.end_period)


def _assign_lanes(items: list[GridItem]) -> dict[int, int]:
    """同一天重疊的時段分到不同並排欄位；回傳各天需要幾個欄位。

    這是典型的「區間圖著色」：依開始時間排序後，填進第一個已經結束的欄位。
    """
    lane_ends: dict[int, list[int]] = {}
    for item in sorted(items, key=lambda i: (i.day, i.start_period, i.end_period)):
        ends = lane_ends.setdefault(item.day, [])
        for lane, end in enumerate(ends):
            if end < item.start_period:
                ends[lane] = item.end_period
                item.lane = lane
                break
        else:
            ends.append(item.end_period)
            item.lane = len(ends) - 1
    return {day: max(1, len(ends)) for day, ends in lane_ends.items()}


def _mark_conflicts(items: list[GridItem]) -> None:
    """把互相重疊的「課程」標成衝突。

    官方活動（共同空堂、課外活動）只是時間框架，不是必修，所以不參與衝突判斷：
    學生在活動時段加選並不算時間衝突。
    """
    courses = [i for i in items if i.kind in COURSE_KINDS]
    for i, a in enumerate(courses):
        for b in courses[i + 1:]:
            if _overlaps(a, b):
                a.conflict = True
                b.conflict = True


def build_week_grid(items: list[GridItem], *,
                    days: list[tuple[int, str]] | None = None,
                    base_periods: int = 0) -> WeekGrid:
    """把時段排成週課表版面。

    `base_periods` 是這個學制的基本節次（例如四技日間部 1～9 節）；實際節次
    超過基底時以實際為準。
    """
    day_list = list(days or DAYS)
    grid_items = list(items)

    for item in grid_items:
        item.lane = 0
        item.lanes = 1
        item.conflict = False

    lanes_per_day = _assign_lanes(grid_items)
    _mark_conflicts(grid_items)

    for item in grid_items:
        item.lanes = lanes_per_day.get(item.day, 1)

    for day, _ in day_list:
        lanes_per_day.setdefault(day, 1)

    max_period = max((i.end_period for i in grid_items), default=0)
    periods = max(base_periods, max_period, 1)

    by_day: dict[int, list[GridItem]] = {day: [] for day, _ in day_list}
    for item in sorted(grid_items, key=lambda i: (i.day, i.lane, i.start_period)):
        by_day.setdefault(item.day, []).append(item)

    return WeekGrid(periods=periods, days=day_list, lanes_per_day=lanes_per_day,
                    items=grid_items, by_day=by_day)
