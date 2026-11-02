"""把 NTUB 官方班級課表 JSON 匯入 Django ORM 的共用服務。

來源：`data/ntub_import_ready/timetables.json`（由 `courses/ntub_timetable_payload.py`
標準化後的結果）。本模組只做「班級課表基線」的儲存：

* 每個班級／學期一筆 `NtubTimetableSnapshot`。
* 每個時段一筆 `NtubTimetableEntry`，`entry_type` 保留官方的 `course` / `activity`。

刻意不做的事：

* 不碰 `Course` / `Section`：官方班級課表不是選課結果。
* 不建立 `Enrollment`：官方課表沒有對應到任何學生帳號。
* 不推測缺失欄位：來源互相矛盾就整批拒絕寫入。
* 不動現有學期的 `is_current`：匯入不會自動改掉目前學期設定。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from django.db import transaction

from .models import (AcademicClassGroup, NtubTimetableEntry,
                      NtubTimetableSnapshot, Term)

ENTRY_COURSE = "course"
ENTRY_ACTIVITY = "activity"
VALID_ENTRY_TYPES = (ENTRY_COURSE, ENTRY_ACTIVITY)

#: 預設來源檔（repo 根目錄下的 data/ntub_import_ready/timetables.json）。
DEFAULT_PAYLOAD_RELPATH = "data/ntub_import_ready/timetables.json"

WEEKDAY_MIN = 1
WEEKDAY_MAX = 7


class TimetableImportError(Exception):
    """來源資料有矛盾，拒絕寫入。"""


@dataclass
class ImportSummary:
    """一次匯入的結果統計。"""

    snapshots_created: int = 0
    snapshots_updated: int = 0
    entries_created: int = 0
    entries_updated: int = 0
    entries_deleted: int = 0
    terms_created: int = 0
    course_entries: int = 0
    activity_entries: int = 0

    @property
    def entries_total(self) -> int:
        return self.entries_created + self.entries_updated

    def as_dict(self) -> dict[str, int]:
        return {
            "snapshots_created": self.snapshots_created,
            "snapshots_updated": self.snapshots_updated,
            "entries_created": self.entries_created,
            "entries_updated": self.entries_updated,
            "entries_deleted": self.entries_deleted,
            "terms_created": self.terms_created,
            "entries_total": self.entries_total,
            "course_entries": self.course_entries,
            "activity_entries": self.activity_entries,
        }


@dataclass
class _PreparedEntry:
    """已驗證、可以直接寫入的一筆明細。"""

    entry_type: str
    course_code: str
    official_serial: str
    course_name: str
    teacher_name: str
    classroom: str
    weekday: int
    start_period: int
    end_period: int
    periods: list[int]
    raw_text: str
    course_href: str

    @property
    def identity(self) -> tuple:
        """以官方課程識別碼對上資料，時段變更時更新同一筆課程。"""
        if self.entry_type == ENTRY_COURSE:
            key = f"course-{self.course_code}-{self.official_serial}"
            return (self.entry_type, key)
        else:
            key = f"activity-{self.course_name}"
        return (self.weekday, self.start_period, self.end_period, self.entry_type, key)

    def as_fields(self) -> dict[str, Any]:
        return {
            "entry_type": self.entry_type,
            "course_code": self.course_code,
            "official_serial": self.official_serial,
            "course_name": self.course_name,
            "teacher_name": self.teacher_name,
            "classroom": self.classroom,
            "weekday": self.weekday,
            "start_period": self.start_period,
            "end_period": self.end_period,
            "periods": list(self.periods),
            "raw_text": self.raw_text,
            "course_href": self.course_href,
        }


# --------------------------------------------------------------------------- #
# 讀取來源
# --------------------------------------------------------------------------- #
def default_payload_path() -> Path:
    """`courses/` 套件的上層就是 repo 根目錄。"""
    return Path(__file__).resolve().parent.parent / DEFAULT_PAYLOAD_RELPATH


def load_payload(path: Path | str | None = None) -> dict[str, Any]:
    """讀來源 JSON；傳入路徑不存在時直接回報錯誤。"""
    payload_path = Path(path) if path else default_payload_path()
    if not payload_path.exists():
        raise TimetableImportError(f"找不到來源檔：{payload_path}")
    try:
        payload = json.loads(payload_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise TimetableImportError(f"來源檔不是合法 JSON：{payload_path}（{exc}）") from exc
    if not isinstance(payload, dict):
        raise TimetableImportError(f"來源檔最外層必須是物件：{payload_path}")
    return payload


# --------------------------------------------------------------------------- #
# 驗證
# --------------------------------------------------------------------------- #
def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _int(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _expected_term_code(class_payload: dict[str, Any]) -> str:
    """班級的學年／學期應該組成哪個學期代碼。"""
    year = _text(class_payload.get("academic_year"))
    semester = _text(class_payload.get("semester"))
    if not year or not semester:
        return ""
    return f"{int(year)}-{int(semester)}"


def validate_class_payload(class_payload: dict[str, Any]) -> None:
    """檢查單一班級的來源資料；有任何矛盾就丟出 :class:`TimetableImportError`。

    檢查項目：班級代碼／名稱、學期代碼、entry 數量與來源宣告是否一致、每筆
    entry 的星期、節次、課程與活動欄位。
    """
    class_code = _text(class_payload.get("class_code"))
    if not class_code:
        raise TimetableImportError("來源缺少 class_code。")

    class_name = _text(class_payload.get("class_name"))
    if not class_name:
        raise TimetableImportError(f"{class_code} 缺少 class_name。")

    term_code = _text(class_payload.get("term_code"))
    if not term_code:
        raise TimetableImportError(f"{class_code} 缺少 term_code。")
    expected_term = _expected_term_code(class_payload)
    if expected_term and expected_term != term_code:
        raise TimetableImportError(
            f"{class_code} 的 term_code({term_code}) 與學年／學期"
            f"({class_payload.get('academic_year')}-{class_payload.get('semester')}) 不一致。")

    entries = class_payload.get("entries")
    if not isinstance(entries, list):
        raise TimetableImportError(f"{class_code} 的 entries 必須是陣列。")

    counts = class_payload.get("counts") or {}
    if isinstance(counts, dict):
        course_count = sum(1 for e in entries if _text(e.get("entry_type")) == ENTRY_COURSE)
        activity_count = sum(1 for e in entries if _text(e.get("entry_type")) == ENTRY_ACTIVITY)
        declared_total = _int(counts.get("total"))
        declared_course = _int(counts.get("course"))
        declared_activity = _int(counts.get("activity"))
        if declared_total is not None and declared_total != len(entries):
            raise TimetableImportError(
                f"{class_code} 的 counts.total({declared_total}) 與 entries 數量"
                f"({len(entries)}) 不一致。")
        if declared_course is not None and declared_course != course_count:
            raise TimetableImportError(
                f"{class_code} 的 counts.course({declared_course}) 與實際課程筆數"
                f"({course_count}) 不一致。")
        if declared_activity is not None and declared_activity != activity_count:
            raise TimetableImportError(
                f"{class_code} 的 counts.activity({declared_activity}) 與實際活動筆數"
                f"({activity_count}) 不一致。")

    for index, entry in enumerate(entries):
        _validate_entry(class_payload, entry, index)


def _validate_entry(class_payload: dict[str, Any], entry: Any, index: int) -> None:
    """檢查單筆 entry 的星期、節次、課程與活動欄位。"""
    class_code = _text(class_payload.get("class_code"))
    where = f"{class_code} 第 {index + 1} 筆"

    if not isinstance(entry, dict):
        raise TimetableImportError(f"{where} 不是物件。")

    entry_type = _text(entry.get("entry_type"))
    if entry_type not in VALID_ENTRY_TYPES:
        raise TimetableImportError(
            f"{where} 的 entry_type({entry_type or '空'}) 不在 {VALID_ENTRY_TYPES} 之中。")

    weekday = _int(entry.get("weekday"))
    if weekday is None or not (WEEKDAY_MIN <= weekday <= WEEKDAY_MAX):
        raise TimetableImportError(
            f"{where} 的 weekday({entry.get('weekday')}) 必須介於 {WEEKDAY_MIN}~{WEEKDAY_MAX}。")

    start_period = _int(entry.get("start_period"))
    end_period = _int(entry.get("end_period"))
    if start_period is None or start_period < 1:
        raise TimetableImportError(f"{where} 缺少有效的 start_period。")
    if end_period is None or end_period < start_period:
        raise TimetableImportError(
            f"{where} 的 end_period({entry.get('end_period')}) 早於 start_period({start_period})。")

    periods = entry.get("periods")
    if isinstance(periods, list) and periods:
        period_ints = [_int(p) for p in periods]
        if any(p is None for p in period_ints):
            raise TimetableImportError(f"{where} 的 periods 含有非整數節次。")
        expected = list(range(start_period, end_period + 1))
        if period_ints != expected:
            raise TimetableImportError(
                f"{where} 的 periods({period_ints}) 與節次範圍 {start_period}~{end_period} 不一致。")

    course = entry.get("course")
    activity = entry.get("activity")

    if entry_type == ENTRY_COURSE:
        if not isinstance(course, dict):
            raise TimetableImportError(f"{where} 是 course 但缺少 course 欄位。")
        if activity is not None:
            raise TimetableImportError(f"{where} 是 course 卻同時帶了 activity 欄位。")
        if not _text(course.get("code")):
            raise TimetableImportError(f"{where} 缺少課號。")
        if not _text(course.get("name")):
            raise TimetableImportError(f"{where} 缺少課名。")
    else:
        if not isinstance(activity, dict):
            raise TimetableImportError(f"{where} 是 activity 但缺少 activity 欄位。")
        if course is not None:
            raise TimetableImportError(f"{where} 是 activity 卻同時帶了 course 欄位。")
        if not _text(activity.get("name")):
            raise TimetableImportError(f"{where} 缺少活動名稱。")

    source = entry.get("source")
    if source is not None and not isinstance(source, dict):
        raise TimetableImportError(f"{where} 的 source 欄位格式錯誤。")


def validate_payload(payload: dict[str, Any]) -> None:
    """檢查整份來源檔（含總計數），任何矛盾都拒絕匯入。"""
    classes = payload.get("classes")
    if not isinstance(classes, list) or not classes:
        raise TimetableImportError("來源檔沒有 classes。")

    for class_payload in classes:
        if not isinstance(class_payload, dict):
            raise TimetableImportError("classes 含有非物件的項目。")
        validate_class_payload(class_payload)

    totals = payload.get("totals")
    if isinstance(totals, dict):
        declared_classes = _int(totals.get("class_count"))
        if declared_classes is not None and declared_classes != len(classes):
            raise TimetableImportError(
                f"totals.class_count({declared_classes}) 與班級數({len(classes)}) 不一致。")
        actual_entries = sum(len(c.get("entries") or []) for c in classes)
        declared_entries = _int(totals.get("entry_count"))
        if declared_entries is not None and declared_entries != actual_entries:
            raise TimetableImportError(
                f"totals.entry_count({declared_entries}) 與明細總數({actual_entries}) 不一致。")
        actual_courses = sum(
            1 for c in classes for e in (c.get("entries") or [])
            if _text(e.get("entry_type")) == ENTRY_COURSE)
        declared_courses = _int(totals.get("course_count"))
        if declared_courses is not None and declared_courses != actual_courses:
            raise TimetableImportError(
                f"totals.course_count({declared_courses}) 與課程總數({actual_courses}) 不一致。")
        actual_activities = sum(
            1 for c in classes for e in (c.get("entries") or [])
            if _text(e.get("entry_type")) == ENTRY_ACTIVITY)
        declared_activities = _int(totals.get("activity_count"))
        if declared_activities is not None and declared_activities != actual_activities:
            raise TimetableImportError(
                f"totals.activity_count({declared_activities}) 與活動總數"
                f"({actual_activities}) 不一致。")


# --------------------------------------------------------------------------- #
# 轉換
# --------------------------------------------------------------------------- #
def prepare_entries(class_payload: dict[str, Any]) -> list[_PreparedEntry]:
    """把來源 entries 轉成可寫入的明細（呼叫前必須先驗證）。"""
    prepared: list[_PreparedEntry] = []
    for entry in class_payload.get("entries") or []:
        entry_type = _text(entry.get("entry_type"))
        source = entry.get("source") or {}
        periods = entry.get("periods")
        if not isinstance(periods, list):
            periods = list(range(int(entry["start_period"]), int(entry["end_period"]) + 1))

        if entry_type == ENTRY_COURSE:
            course = entry.get("course") or {}
            prepared.append(_PreparedEntry(
                entry_type=ENTRY_COURSE,
                course_code=_text(course.get("code")),
                official_serial=_text(course.get("serial")),
                course_name=_text(course.get("name")),
                teacher_name=_text(course.get("teacher")),
                classroom=_text(course.get("classroom")),
                weekday=int(entry["weekday"]),
                start_period=int(entry["start_period"]),
                end_period=int(entry["end_period"]),
                periods=[int(p) for p in periods],
                raw_text=_text(source.get("raw_text")),
                course_href=_text(source.get("href")),
            ))
        else:
            activity = entry.get("activity") or {}
            prepared.append(_PreparedEntry(
                entry_type=ENTRY_ACTIVITY,
                course_code="",
                official_serial="",
                course_name=_text(activity.get("name")),
                teacher_name="",
                classroom="",
                weekday=int(entry["weekday"]),
                start_period=int(entry["start_period"]),
                end_period=int(entry["end_period"]),
                periods=[int(p) for p in periods],
                raw_text=_text(source.get("raw_text")),
                course_href="",
            ))
    return prepared


# --------------------------------------------------------------------------- #
# 寫入
# --------------------------------------------------------------------------- #
def _resolve_term(term_code: str, summary: ImportSummary) -> Term:
    """取得學期；不存在時建立一筆 `is_current=False` 的學期。

    絕對不會改動既有學期的 `is_current`，也不會把目前學期換掉。
    """
    term = Term.objects.filter(code=term_code).first()
    if term is not None:
        return term
    term = Term.objects.create(code=term_code, is_current=False)
    summary.terms_created += 1
    return term


def _resolve_class_group(class_code: str, class_name: str) -> AcademicClassGroup:
    """依 class_code 找到既有班級；找不到就拒絕，不自行建立。"""
    class_group = AcademicClassGroup.objects.filter(code=class_code).first()
    if class_group is None:
        raise TimetableImportError(
            f"資料庫沒有班級 {class_code}；請先匯入學籍目錄，不要在課表匯入時硬編班級。")
    if class_group.name_zh and class_name and class_group.name_zh != class_name:
        raise TimetableImportError(
            f"班級 {class_code} 的來源名稱({class_name}) 與資料庫名稱"
            f"({class_group.name_zh}) 不一致。")
    return class_group


def _entry_identity(entry: NtubTimetableEntry) -> tuple:
    """已存在明細的識別碼（與 :attr:`_PreparedEntry.identity` 一致）。"""
    if entry.entry_type == ENTRY_COURSE:
        key = f"course-{entry.course_code}-{entry.official_serial}"
        return (entry.entry_type, key)
    else:
        key = f"activity-{entry.course_name}"
    return (entry.weekday, entry.start_period, entry.end_period, entry.entry_type, key)


def _upsert_entries(snapshot: NtubTimetableSnapshot, prepared: list[_PreparedEntry],
                    summary: ImportSummary) -> None:
    """把明細寫進快照：同一位時段更新、新的新增、來源已不存在的刪除。"""
    existing = {_entry_identity(entry): entry for entry in snapshot.entries.all()}
    incoming = {item.identity for item in prepared}

    for item in prepared:
        entry = existing.get(item.identity)
        if entry is None:
            NtubTimetableEntry.objects.create(snapshot=snapshot, **item.as_fields())
            summary.entries_created += 1
        else:
            for field_name, value in item.as_fields().items():
                setattr(entry, field_name, value)
            entry.save()
            summary.entries_updated += 1

    for identity, entry in existing.items():
        if identity not in incoming:
            entry.delete()
            summary.entries_deleted += 1


def import_payload(payload: dict[str, Any]) -> ImportSummary:
    """把來源 payload 寫進資料庫。

    整批匯入在同一個 `transaction.atomic()` 內完成：任何一筆有問題就整批回滾。
    重複匯入同一組 (班級, 學期) 會更新既有快照與明細，不會產生重複資料。
    """
    validate_payload(payload)
    summary = ImportSummary()

    with transaction.atomic():
        for class_payload in payload["classes"]:
            class_code = _text(class_payload.get("class_code"))
            class_name = _text(class_payload.get("class_name"))
            term_code = _text(class_payload.get("term_code"))

            class_group = _resolve_class_group(class_code, class_name)
            term = _resolve_term(term_code, summary)

            source = class_payload.get("source") or {}
            retrieved_at = None
            raw_retrieved = _text(source.get("retrieved_at"))
            if raw_retrieved:
                from django.utils.dateparse import parse_datetime
                parsed = parse_datetime(raw_retrieved)
                if parsed is None:
                    raise TimetableImportError(
                        f"{class_code} 的 retrieved_at({raw_retrieved}) 不是合法的時間格式。")
                retrieved_at = parsed

            snapshot, created = NtubTimetableSnapshot.objects.get_or_create(
                class_group=class_group,
                term=term,
                source_kind=NtubTimetableSnapshot.SourceKind.IMPORTED,
                defaults={
                    "class_name": class_name,
                    "source_url": _text(source.get("source_url")),
                    "retrieved_at": retrieved_at,
                },
            )
            if created:
                summary.snapshots_created += 1
            else:
                summary.snapshots_updated += 1
                snapshot.class_name = class_name
                snapshot.source_url = _text(source.get("source_url"))
                snapshot.retrieved_at = retrieved_at
                snapshot.save()

            prepared = prepare_entries(class_payload)
            summary.course_entries += sum(1 for p in prepared if p.entry_type == ENTRY_COURSE)
            summary.activity_entries += sum(1 for p in prepared if p.entry_type == ENTRY_ACTIVITY)
            _upsert_entries(snapshot, prepared, summary)

    return summary


def import_from_file(path: Path | str | None = None) -> ImportSummary:
    """讀來源檔後匯入。"""
    return import_payload(load_payload(path))
