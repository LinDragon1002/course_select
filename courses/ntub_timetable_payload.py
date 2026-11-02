"""NTUB 官方班級課表 JSON 的標準化工具。

來源是 ntcbadm.ntub.edu.tw/pub/Cur_Class.aspx 抓下來的班級課表 JSON（`data/ntub_timetables/`）。
原始檔每個 entry 帶有星期、起訖節次、課名、老師、教室與課程參照，本模組把它整理成兩層輸出：

1. **班級快照（class snapshot）**：官方公布的「這個班每週怎麼上課」。屬於班級層級的課表，
   每個班一筆快照，裡面分 `course`（真正的課程）與 `activity`（共同空堂、課外活動等
   非課程的時段）。
2. **ORM 投影（orm_records）**：同一份資料轉成 `courses.models` 的 Term / Teacher /
   Course / Section 欄位形狀，讓後續的匯入程式不必再回頭解析課表。

兩件刻意不做的事：

* 官方課表**不是**任何學生的個人選課結果，所以本模組不產生 Enrollment，也把
  `produces_enrollments` 標成 false；快照層級固定為 `scope: "class"`。
* 沒有來源就沒有資料：缺欄位會被記成 warning，絕不補造課名、老師或教室。

純函式模組，不碰 Django settings，也不需要資料庫，方便直接用 `unittest` 測試。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = "1.0"

#: 官方課表 JSON 所在的來源目錄（可用環境變數 `NTUB_TIMETABLE_SOURCE_DIR` 覆寫）。
DEFAULT_SOURCE_DIR = Path(
    r"C:\Users\user\OneDrive\Desktop\course_select\coursepuzzle-main\data\ntub_timetables"
)

#: 標準化後 JSON 的輸出路徑（相對於 repo 根目錄）。
DEFAULT_OUTPUT_RELPATH = "data/ntub_import_ready/timetables.json"

WEEKDAY_LABELS = {
    1: "週一",
    2: "週二",
    3: "週三",
    4: "週四",
    5: "週五",
    6: "週六",
    7: "週日",
}

ENTRY_COURSE = "course"
ENTRY_ACTIVITY = "activity"

#: `javascript:ConnectCos_Short('115','1','40620A','10','40626610');`
_HREF_RE = re.compile(r"ConnectCos_Short\(([^)]*)\)")


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def term_code(academic_year: Any, semester: Any) -> str:
    """把學年/學期組成 `115-1` 這種代碼，對應 `courses.models.Term.code`。"""
    year = str(academic_year).strip()
    sem = str(semester).strip()
    if not year or not sem:
        raise ValueError("學年與學期不可為空")
    return f"{int(year)}-{int(sem)}"


def academic_year_ad(academic_year: Any) -> int:
    """民國學年轉西元年（115 學年度 → 2026）。"""
    return int(str(academic_year).strip()) + 1911


def _clean(value: Any) -> str:
    """去除空白字串；None 視為空字串。"""
    if value is None:
        return ""
    return str(value).strip()


def _as_int(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def parse_course_href(href: str) -> dict[str, str]:
    """從 `course_href` 的 javascript 呼叫裡挖出課程參照，作為 course_ref 的備援。

    回傳的鍵：academic_year / semester / class_code / serial / course_id。
    解析不出來就回空字典（呼叫端自行決定要不要記 warning）。
    """
    match = _HREF_RE.search(href or "")
    if not match:
        return {}
    args = [part.strip().strip("'\"") for part in match.group(1).split(",")]
    if len(args) < 5:
        return {}
    keys = ("academic_year", "semester", "class_code", "serial", "course_id")
    return dict(zip(keys, args))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# 資料結構
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SourceRef:
    """一筆資料的來源出處，保留到輸出 JSON 裡。"""

    file: str
    source_url: str = ""
    retrieved_at: str = ""
    exported_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "file": self.file,
            "source_url": self.source_url,
            "retrieved_at": self.retrieved_at,
            "exported_at": self.exported_at,
        }


@dataclass
class NormalizedEntry:
    """標準化後的一筆課表明細。"""

    entry_type: str
    weekday: int
    start_period: int
    end_period: int
    periods: list[int]
    weekday_label: str
    weekday_name: str = ""
    course: dict[str, Any] | None = None
    activity: dict[str, Any] | None = None
    source: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def period_count(self) -> int:
        return len(self.periods)

    @property
    def is_consecutive(self) -> bool:
        """節次是否連續（沒有跳節）。"""
        return self.periods == list(range(self.start_period, self.end_period + 1))

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_type": self.entry_type,
            "weekday": self.weekday,
            "weekday_label": self.weekday_label,
            "weekday_name": self.weekday_name,
            "start_period": self.start_period,
            "end_period": self.end_period,
            "periods": list(self.periods),
            "period_count": self.period_count,
            "is_consecutive": self.is_consecutive,
            "course": self.course,
            "activity": self.activity,
            "source": self.source,
            "warnings": list(self.warnings),
        }


@dataclass
class ClassSnapshot:
    """一個班級的官方課表快照。"""

    class_code: str
    class_name: str
    academic_year: str
    semester: str
    term: str
    entries: list[NormalizedEntry]
    department: str = ""
    education_level: str = ""
    source: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def counts(self) -> dict[str, int]:
        courses = sum(1 for e in self.entries if e.entry_type == ENTRY_COURSE)
        activities = sum(1 for e in self.entries if e.entry_type == ENTRY_ACTIVITY)
        return {
            "total": len(self.entries),
            "course": courses,
            "activity": activities,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "class_code": self.class_code,
            "class_name": self.class_name,
            "academic_year": self.academic_year,
            "semester": self.semester,
            "term_code": self.term,
            "academic_year_ad": academic_year_ad(self.academic_year),
            "department": self.department,
            "education_level": self.education_level,
            "scope": "class",
            "is_student_enrollment": False,
            "counts": self.counts,
            "entries": [e.to_dict() for e in self.entries],
            "source": self.source,
            "warnings": list(self.warnings),
        }


# --------------------------------------------------------------------------- #
# 正規化
# --------------------------------------------------------------------------- #
def _entry_periods(raw: dict[str, Any], warnings: list[str]) -> tuple[int, int, list[int]]:
    """取出起訖節次與展開後的節次清單。

    以 `start_period` / `end_period` 為準（官方給的就是這兩個），`periods` 若與之
    不一致則記 warning 並以起訖節次重建，避免出現跳節卻被當成連續節。
    """
    start = _as_int(raw.get("start_period"))
    end = _as_int(raw.get("end_period"))
    raw_periods = [_as_int(p) for p in (raw.get("periods") or [])]
    raw_periods = [p for p in raw_periods if p is not None]

    if start is None or end is None:
        warnings.append("缺少節次資訊，已略過此筆。")
        return 0, 0, []

    if end < start:
        warnings.append(f"結束節次 {end} 早於起始節次 {start}，已交換。")
        start, end = end, start

    periods = list(range(start, end + 1))
    if raw_periods and raw_periods != periods:
        warnings.append(
            f"來源 periods {raw_periods} 與節次範圍 {start}-{end} 不一致，以節次範圍為準。"
        )
    return start, end, periods


def normalize_entry(raw: dict[str, Any], *, class_code: str, term: str) -> NormalizedEntry | None:
    """把來源的一筆 entry 轉成 :class:`NormalizedEntry`。

    來源沒有 `entry_type` 時，用課名是否為空推斷：`course` 有課名，`activity` 沒有。
    回傳 None 代表這筆資料不足以成案（缺節次），該筆會被丢掉並留下 warning。
    """
    warnings: list[str] = []

    weekday = _as_int(raw.get("weekday"))
    if weekday is None:
        weekday = 0
        warnings.append("缺少星期欄位。")

    start, end, periods = _entry_periods(raw, warnings)
    if not periods:
        return None

    course_name = _clean(raw.get("course_name"))
    teacher = _clean(raw.get("teacher"))
    classroom = _clean(raw.get("classroom"))
    raw_text = _clean(raw.get("raw_text"))

    entry_type = _clean(raw.get("entry_type")).lower()
    if entry_type not in (ENTRY_COURSE, ENTRY_ACTIVITY):
        entry_type = ENTRY_COURSE if course_name else ENTRY_ACTIVITY
        warnings.append(f"未標示 entry_type，依課名推斷為 {entry_type}。")

    course_ref = raw.get("course_ref") or {}
    if not isinstance(course_ref, dict):
        course_ref = {}
    course_ref = {k: _clean(v) for k, v in course_ref.items()}
    if not course_ref:
        course_ref = parse_course_href(_clean(raw.get("course_href")))
        if course_ref:
            warnings.append("course_ref 缺漏，改由 course_href 解析。")

    course: dict[str, Any] | None = None
    activity: dict[str, Any] | None = None

    if entry_type == ENTRY_COURSE:
        if not course_name:
            warnings.append("course 缺少課名。")
        course = {
            "code": course_ref.get("course_id", ""),
            "serial": course_ref.get("serial", ""),
            "name": course_name,
            "teacher": teacher,
            "classroom": classroom,
        }
        if not course["code"]:
            warnings.append("course 缺少 course_id（課號）。")
    else:
        activity_name = course_name or raw_text
        if not activity_name:
            warnings.append("activity 缺少名稱。")
        activity = {"name": activity_name}

    source = {
        "raw_text": raw_text,
        "href": _clean(raw.get("course_href")),
        "notes": raw.get("notes") or [],
        "course_ref": course_ref or None,
    }

    return NormalizedEntry(
        entry_type=entry_type,
        weekday=weekday,
        weekday_label=WEEKDAY_LABELS.get(weekday, ""),
        weekday_name=_clean(raw.get("weekday_name")),
        start_period=start,
        end_period=end,
        periods=periods,
        course=course,
        activity=activity,
        source=source,
        warnings=warnings,
    )


def normalize_class(raw: dict[str, Any], *, source: SourceRef) -> ClassSnapshot:
    """把一份班級課表 JSON 轉成 :class:`ClassSnapshot`。"""
    warnings: list[str] = []

    class_info = raw.get("class") or {}
    request_info = raw.get("request") or {}

    class_code = _clean(class_info.get("code")) or _clean(request_info.get("class_code"))
    class_name = _clean(class_info.get("name"))
    academic_year = _clean(class_info.get("academic_year")) or _clean(
        request_info.get("academic_year")
    )
    semester = _clean(class_info.get("semester")) or _clean(request_info.get("semester"))

    if not class_code:
        raise ValueError(f"{source.file} 缺少班級代碼。")
    if not academic_year or not semester:
        raise ValueError(f"{source.file} 缺少學年或學期。")

    term = term_code(academic_year, semester)

    entries: list[NormalizedEntry] = []
    for raw_entry in raw.get("entries") or []:
        entry = normalize_entry(raw_entry, class_code=class_code, term=term)
        if entry is None:
            warnings.append("有一筆 entry 缺少節次，已略過。")
            continue
        warnings.extend(f"entry {index}: {w}" for index, w in enumerate(entry.warnings))
        entries.append(entry)

    source_info = {
        **source.to_dict(),
        "request": request_info,
        "stats": raw.get("stats") or {},
        "ambiguities": raw.get("ambiguities") or [],
    }

    return ClassSnapshot(
        class_code=class_code,
        class_name=class_name,
        academic_year=academic_year,
        semester=semester,
        term=term,
        entries=entries,
        department=_clean(request_info.get("dept")),
        education_level=_clean(request_info.get("edu")),
        source=source_info,
        warnings=warnings,
    )


def load_source(path: Path) -> tuple[dict[str, Any], SourceRef]:
    """讀一份來源 JSON，並取出來源出處。"""
    path = Path(path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    ref = SourceRef(
        file=path.name,
        source_url=_clean(raw.get("source_url")),
        retrieved_at=_clean(raw.get("retrieved_at")),
        exported_at=_clean(raw.get("exported_at")),
    )
    return raw, ref


def load_classes(source_dir: Path | str = DEFAULT_SOURCE_DIR) -> list[ClassSnapshot]:
    """讀取來源目錄下所有課表 JSON，轉成班級快照（依班級代碼排序）。"""
    source_dir = Path(source_dir)
    if not source_dir.is_dir():
        raise FileNotFoundError(f"來源目錄不存在：{source_dir}")

    snapshots: list[ClassSnapshot] = []
    for path in sorted(source_dir.glob("*.json")):
        raw, ref = load_source(path)
        snapshots.append(normalize_class(raw, source=ref))
    snapshots.sort(key=lambda s: s.class_code)
    return snapshots


# --------------------------------------------------------------------------- #
# ORM 投影
# --------------------------------------------------------------------------- #
def to_orm_records(snapshots: Iterable[ClassSnapshot]) -> dict[str, list[dict[str, Any]]]:
    """把班級快照轉成 `courses.models` 的 Term / Teacher / Course / Section 欄位形狀。

    * Term：一份課表一個學期代碼（`115-1`）。
    * Teacher：以老師姓名去重。
    * Course：以官方 course_id 當課號；同一門課在不同班可能是不同 course_id，
      這裡忠實保留官方代號，不自己合併。
    * Section：label 用班級代碼；同一課號在同一班若有多筆（例：一門課一週兩次），
      label 會加上 serial 避免撞到 `(course, label)` 唯一性限制。

    刻意不產生 Enrollment：官方課表沒有對應到任何學生帳號。
    """
    snapshots = list(snapshots)

    terms: list[dict[str, Any]] = []
    teachers: list[dict[str, Any]] = []
    courses: list[dict[str, Any]] = []
    sections: list[dict[str, Any]] = []

    seen_terms: set[str] = set()
    seen_teachers: set[str] = set()
    seen_courses: set[tuple[str, str]] = set()
    used_labels: set[tuple[str, str]] = set()

    for snapshot in snapshots:
        if snapshot.term not in seen_terms:
            seen_terms.add(snapshot.term)
            terms.append({"code": snapshot.term})

        for entry in snapshot.entries:
            if entry.entry_type != ENTRY_COURSE or not entry.course:
                continue
            course = entry.course

            teacher_name = course.get("teacher", "")
            if teacher_name and teacher_name not in seen_teachers:
                seen_teachers.add(teacher_name)
                teachers.append({"name": teacher_name})

            course_code = course.get("code", "")
            key = (snapshot.term, course_code)
            if key not in seen_courses:
                seen_courses.add(key)
                courses.append(
                    {
                        "code": course_code,
                        "name": course.get("name", ""),
                        "teacher": teacher_name,
                        "term": snapshot.term,
                    }
                )

            label = snapshot.class_code
            if (course_code, label) in used_labels:
                serial = course.get("serial", "")
                label = f"{snapshot.class_code}-{serial}" if serial else f"{snapshot.class_code}-2"
            used_labels.add((course_code, label))

            sections.append(
                {
                    "course": course_code,
                    "label": label,
                    "day": entry.weekday,
                    "start_period": entry.start_period,
                    "end_period": entry.end_period,
                    "room": course.get("classroom", ""),
                    "class_code": snapshot.class_code,
                }
            )

    return {
        "terms": terms,
        "teachers": sorted(teachers, key=lambda t: t["name"]),
        "courses": courses,
        "sections": sections,
    }


# --------------------------------------------------------------------------- #
# 組裝輸出
# --------------------------------------------------------------------------- #
def build_payload(
    snapshots: Iterable[ClassSnapshot],
    *,
    generated_at: str | None = None,
    source_dir: str | None = None,
) -> dict[str, Any]:
    """組出完整、可匯入 Django ORM 的標準 JSON。"""
    snapshots = list(snapshots)
    payload_orm = to_orm_records(snapshots)

    totals = {
        "class_count": len(snapshots),
        "entry_count": sum(len(s.entries) for s in snapshots),
        "course_count": sum(s.counts["course"] for s in snapshots),
        "activity_count": sum(s.counts["activity"] for s in snapshots),
    }

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at or _now_iso(),
        "scope": "class_timetable",
        "produces_enrollments": False,
        "source": {
            "provider": "NTUB 官方班級課表",
            "source_url": snapshots[0].source.get("source_url", "") if snapshots else "",
            "directory": str(source_dir) if source_dir else "",
            "files": [s.source.get("file", "") for s in snapshots],
        },
        "terms": [{"code": term} for term in dict.fromkeys(s.term for s in snapshots)],
        "classes": [s.to_dict() for s in snapshots],
        "totals": totals,
        "orm_records": payload_orm,
    }


def build_payload_from_dir(
    source_dir: Path | str = DEFAULT_SOURCE_DIR, *, generated_at: str | None = None
) -> dict[str, Any]:
    """從來源目錄一路組到標準 JSON。"""
    snapshots = load_classes(source_dir)
    return build_payload(snapshots, generated_at=generated_at, source_dir=str(source_dir))


def write_payload(payload: dict[str, Any], output_path: Path | str) -> Path:
    """把標準 JSON 寫檔（UTF-8、保留中文、兩格縮排）。"""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return output_path


def repo_root() -> Path:
    """`courses/` 套件的上層就是 repo 根目錄。"""
    return Path(__file__).resolve().parent.parent


def default_output_path() -> Path:
    return repo_root() / DEFAULT_OUTPUT_RELPATH


def resolve_source_dir(source_dir: Path | str | None = None) -> Path:
    """來源目錄：優先使用傳入值，其次環境變數，最後預設常數。"""
    import os

    if source_dir:
        return Path(source_dir)
    env = os.environ.get("NTUB_TIMETABLE_SOURCE_DIR")
    if env:
        return Path(env)
    return DEFAULT_SOURCE_DIR
