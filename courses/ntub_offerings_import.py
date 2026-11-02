"""把 NTUB 官方專業選修開課（offerings.json）匯入 Django ORM 的共用服務。

來源：`data/ntub_import_ready/offerings.json`。這份來源裡有三個部分：

* `offerings[]`：校方課表查詢系統擷取到的「某班級在 115-1 開的某門課」，
  每一筆都有自己的班級、星期節次、教室、教師、名額。
* `curriculum_recommendations[]`：課綱上建議某個入學年度／專班／班級修的課。
* `ambiguities[]`：來源自己標記出來的疑義（跨班選修待確認、沒有推薦的開課…）。

本模組只做「官方開課基線」的儲存：

* `Term` / `Teacher` / `Course` / `Section` / `AcademicClassGroup` 照既有模型填。
* `NtubOfferingSource` 保存官方欄位與「來源鍵 → 本地 Section」的穩定對應。
* `NtubCurriculumRecommendation` / `NtubRecommendationCandidate` 保存課綱推薦。
* `NtubOfferingAmbiguity` 原樣保存來源列出的疑義。

刻意不做的事：

* 不建立 `Enrollment`：官方開課是課表基線，不是任何學生的個人選課結果。
* 不把 `official_enrolled`（校方公告已選人數）寫進任何加選統計。
* 不猜測個人資格：`StudentAcademicProfile` 沒有入學年度／專班類型，所以推薦
  只存到班級層級，不推導成任何個人可選課的資格。
* 不覆寫其他來源建立的資料：既有 `Course` / `Teacher` / `Section` 與來源衝突時
  記為衝突並回報，不直接改寫。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from django.db import transaction

from .models import (AcademicClassGroup, Course, NtubCurriculumRecommendation,
                     NtubOfferingAmbiguity, NtubOfferingSource,
                     NtubRecommendationCandidate, Section, Teacher, Term)

#: 預設來源檔（repo 根目錄下的 data/ntub_import_ready/offerings.json）。
DEFAULT_PAYLOAD_RELPATH = "data/ntub_import_ready/offerings.json"

MATCH_MATCHED = "matched"
MATCH_COURSE_NAME_ONLY = "course_name_only"

VALID_MATCH_STATUSES = (MATCH_MATCHED, MATCH_COURSE_NAME_ONLY)

#: 來源使用的模糊比對分類。
AMBIGUITY_RECOMMENDATION_CLASS_MISMATCH = "recommendation_class_mismatch"
AMBIGUITY_OFFERING_WITHOUT_RECOMMENDATION = "offering_without_recommendation"
AMBIGUITY_SOURCE_PARTIAL_KEYWORD_MATCH = "source_partial_keyword_match"

#: 來源認為「近似但不同」的課名對：這兩門課不可互相取代。
#: 這裡只列出來源明確點名的例子，其餘近似課名一律以來源自身的規則為準。
KNOWN_DISTINCT_COURSE_PAIRS = (
    ("財務管理", "財務管理(二)(技優領航專班)"),
)


class OfferingImportError(Exception):
    """來源資料有矛盾或無法映射，拒絕寫入。"""


@dataclass
class ImportSummary:
    """一次匯入的結果統計。"""

    offerings_created: int = 0
    offerings_updated: int = 0
    courses_created: int = 0
    courses_reused: int = 0
    sections_created: int = 0
    sections_reused: int = 0
    teachers_created: int = 0
    teachers_reused: int = 0
    recommendations_created: int = 0
    recommendations_updated: int = 0
    candidates_created: int = 0
    candidates_updated: int = 0
    ambiguities_created: int = 0
    enrollments_created: int = 0
    conflicts: list[dict[str, str]] = field(default_factory=list)

    @property
    def has_conflicts(self) -> bool:
        return bool(self.conflicts)

    def as_dict(self) -> dict[str, Any]:
        data = {
            "offerings_created": self.offerings_created,
            "offerings_updated": self.offerings_updated,
            "courses_created": self.courses_created,
            "courses_reused": self.courses_reused,
            "sections_created": self.sections_created,
            "sections_reused": self.sections_reused,
            "teachers_created": self.teachers_created,
            "teachers_reused": self.teachers_reused,
            "recommendations_created": self.recommendations_created,
            "recommendations_updated": self.recommendations_updated,
            "candidates_created": self.candidates_created,
            "candidates_updated": self.candidates_updated,
            "ambiguities_created": self.ambiguities_created,
            "enrollments_created": self.enrollments_created,
        }
        data["conflicts"] = list(self.conflicts)
        return data

    def add_conflict(self, kind: str, key: str, message: str) -> None:
        self.conflicts.append({"kind": kind, "key": key, "message": message})


# --------------------------------------------------------------------------- #
# 讀取來源
# --------------------------------------------------------------------------- #
def default_payload_path() -> Path:
    """`courses/` 套件的上層就是 repo 根目錄。"""
    return Path(__file__).resolve().parent.parent / DEFAULT_PAYLOAD_RELPATH


def load_payload(path: Path | str | None = None) -> dict[str, Any]:
    """讀來源 JSON；路徑不存在或格式錯誤時回報錯誤。"""
    payload_path = Path(path) if path else default_payload_path()
    if not payload_path.exists():
        raise OfferingImportError(f"找不到來源檔：{payload_path}")
    try:
        payload = json.loads(payload_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise OfferingImportError(f"來源檔不是合法 JSON：{payload_path}（{exc}）") from exc
    if not isinstance(payload, dict):
        raise OfferingImportError(f"來源檔最外層必須是物件：{payload_path}")
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


def _decimal(value: Any) -> float | None:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _require_int(payload: dict[str, Any], field_name: str, where: str) -> int:
    value = _int(payload.get(field_name))
    if value is None:
        raise OfferingImportError(f"{where} 缺少有效的 {field_name}。")
    return value


def _require_text(payload: dict[str, Any], field_name: str, where: str) -> str:
    value = _text(payload.get(field_name))
    if not value:
        raise OfferingImportError(f"{where} 缺少 {field_name}。")
    return value


def validate_offering(offering: dict[str, Any], index: int) -> None:
    """驗證單筆官方開課；任何矛盾都丟出 :class:`OfferingImportError`。"""
    where = f"offerings 第 {index + 1} 筆"
    if not isinstance(offering, dict):
        raise OfferingImportError(f"{where} 不是物件。")

    for field_name in ("academic_year", "semester", "class_code", "serial", "course_code",
                       "course_name", "term_code"):
        _require_text(offering, field_name, where)

    schedule = offering.get("schedule")
    if not isinstance(schedule, dict):
        raise OfferingImportError(f"{where} 缺少 schedule 欄位。")
    weekday = _int(schedule.get("weekday"))
    if weekday is None or not (1 <= weekday <= 7):
        raise OfferingImportError(f"{where} 的 weekday 必須介於 1~7。")
    start_period = _int(schedule.get("start_period"))
    end_period = _int(schedule.get("end_period"))
    if start_period is None or start_period < 1:
        raise OfferingImportError(f"{where} 缺少有效的 start_period。")
    if end_period is None or end_period < start_period:
        raise OfferingImportError(f"{where} 的 end_period 早於 start_period。")

    teacher = offering.get("teacher")
    if not isinstance(teacher, dict) or not _text(teacher.get("name")):
        raise OfferingImportError(f"{where} 缺少教師姓名。")

    credits = _decimal(offering.get("credits"))
    if credits is None:
        raise OfferingImportError(f"{where} 缺少有效的 credits。")

    capacity = offering.get("official_capacity")
    if capacity is not None and _int(capacity) is None:
        raise OfferingImportError(f"{where} 的 official_capacity 不是整數。")
    enrolled = offering.get("official_enrolled")
    if enrolled is not None and _int(enrolled) is None:
        raise OfferingImportError(f"{where} 的 official_enrolled 不是整數。")


def validate_recommendation(recommendation: dict[str, Any], index: int) -> None:
    """驗證單筆課綱推薦。"""
    where = f"curriculum_recommendations 第 {index + 1} 筆"
    if not isinstance(recommendation, dict):
        raise OfferingImportError(f"{where} 不是物件。")
    for field_name in ("admission_year", "class_code", "course_name", "match_status"):
        _require_text(recommendation, field_name, where)
    match_status = _text(recommendation.get("match_status"))
    if match_status not in VALID_MATCH_STATUSES:
        raise OfferingImportError(
            f"{where} 的 match_status({match_status}) 不在 {VALID_MATCH_STATUSES} 之中。")
    candidates = recommendation.get("candidates")
    if candidates is not None and not isinstance(candidates, list):
        raise OfferingImportError(f"{where} 的 candidates 必須是陣列。")


def validate_ambiguity(ambiguity: dict[str, Any], index: int) -> None:
    """驗證單筆疑義。"""
    where = f"ambiguities 第 {index + 1} 筆"
    if not isinstance(ambiguity, dict):
        raise OfferingImportError(f"{where} 不是物件。")
    _require_text(ambiguity, "kind", where)
    _require_text(ambiguity, "message", where)


def validate_payload(payload: dict[str, Any]) -> None:
    """驗證整份來源檔，任何矛盾都拒絕匯入。"""
    offerings = payload.get("offerings")
    if not isinstance(offerings, list) or not offerings:
        raise OfferingImportError("來源檔沒有 offerings。")
    for index, offering in enumerate(offerings):
        validate_offering(offering, index)

    keys = [_text(o.get("key")) for o in offerings]
    duplicates = {k for k in keys if keys.count(k) > 1}
    if duplicates:
        raise OfferingImportError(f"來源檔的 offerings 有重複的 key：{sorted(duplicates)}")

    recommendations = payload.get("curriculum_recommendations") or []
    if not isinstance(recommendations, list):
        raise OfferingImportError("curriculum_recommendations 必須是陣列。")
    for index, recommendation in enumerate(recommendations):
        validate_recommendation(recommendation, index)

    ambiguities = payload.get("ambiguities") or []
    if not isinstance(ambiguities, list):
        raise OfferingImportError("ambiguities 必須是陣列。")
    for index, ambiguity in enumerate(ambiguities):
        validate_ambiguity(ambiguity, index)

    stats = payload.get("stats")
    if isinstance(stats, dict):
        declared = _int(stats.get("raw_offerings"))
        if declared is not None and declared != len(offerings):
            raise OfferingImportError(
                f"stats.raw_offerings({declared}) 與 offerings 數量({len(offerings)}) 不一致。")
        declared = _int(stats.get("curriculum_recommendations"))
        if declared is not None and declared != len(recommendations):
            raise OfferingImportError(
                f"stats.curriculum_recommendations({declared}) 與實際推薦筆數"
                f"({len(recommendations)}) 不一致。")
        declared = _int(stats.get("ambiguities"))
        if declared is not None and declared != len(ambiguities):
            raise OfferingImportError(
                f"stats.ambiguities({declared}) 與實際疑義筆數({len(ambiguities)}) 不一致。")


# --------------------------------------------------------------------------- #
# 解析
# --------------------------------------------------------------------------- #
@dataclass
class _ParsedOffering:
    """驗證過、可直接寫入的一筆官方開課。"""

    source_key: str
    academic_year: str
    semester: str
    term_code: str
    class_code: str
    class_name: str
    official_serial: str
    course_code: str
    course_name: str
    teacher_source_id: str
    teacher_name: str
    weekday: int
    start_period: int
    end_period: int
    room: str
    classroom_code: str
    classroom_name: str
    credits: int
    elective_category: str
    official_capacity: int | None
    official_enrolled: int | None
    source_url: str
    source_file: str
    retrieved_at: Any
    exported_at: Any
    payload: dict[str, Any]

    @property
    def dedup_key(self) -> str:
        """來源唯一鍵：學年 | 學期 | 班級代碼 | 序號 | 課號。"""
        return "|".join([self.academic_year, self.semester, self.class_code,
                         self.official_serial, self.course_code])


def parse_offering(offering: dict[str, Any]) -> _ParsedOffering:
    """把來源的一筆開課轉成可寫入的結構。"""
    schedule = offering.get("schedule") or {}
    teacher = offering.get("teacher") or {}
    classroom = offering.get("classroom") or {}
    source = offering.get("source") or {}
    credits = _decimal(offering.get("credits"))

    return _ParsedOffering(
        source_key=_text(offering.get("key")),
        academic_year=_text(offering.get("academic_year")),
        semester=_text(offering.get("semester")),
        term_code=_text(offering.get("term_code")),
        class_code=_text(offering.get("class_code")),
        class_name=_text(offering.get("class_name")),
        official_serial=_text(offering.get("serial")),
        course_code=_text(offering.get("course_code")),
        course_name=_text(offering.get("course_name")),
        teacher_source_id=_text(teacher.get("id")),
        teacher_name=_text(teacher.get("name")),
        weekday=int(schedule["weekday"]),
        start_period=int(schedule["start_period"]),
        end_period=int(schedule["end_period"]),
        room=_text(classroom.get("code")) or _text(offering.get("room")),
        classroom_code=_text(classroom.get("code")),
        classroom_name=_text(classroom.get("name")),
        credits=int(round(credits)) if credits is not None else 3,
        elective_category=_text(offering.get("elective_category")) or _text(offering.get("category")),
        official_capacity=_int(offering.get("official_capacity")),
        official_enrolled=_int(offering.get("official_enrolled")),
        source_url=_text(source.get("source_url")),
        source_file=_text(source.get("file")),
        retrieved_at=source.get("retrieved_at"),
        exported_at=source.get("exported_at"),
        payload=offering,
    )


# --------------------------------------------------------------------------- #
# 寫入
# --------------------------------------------------------------------------- #
def _resolve_term(term_code: str) -> Term:
    """取得學期；不存在時建立一筆 `is_current=False` 的學期。

    絕對不會改動既有學期的 `is_current`，也不會把目前學期換掉。
    """
    term = Term.objects.filter(code=term_code).first()
    if term is not None:
        return term
    return Term.objects.create(code=term_code, is_current=False)


def _resolve_class_group(class_code: str, class_name: str) -> AcademicClassGroup | None:
    """依班級代碼找到既有班級；找不到回 None（由呼叫端記為衝突）。"""
    class_group = AcademicClassGroup.objects.filter(code=class_code).first()
    if class_group is None:
        return None
    if class_group.name_zh and class_name and class_group.name_zh != class_name:
        return None
    return class_group


def _resolve_teacher(item: _ParsedOffering, summary: ImportSummary) -> Teacher:
    """找到（或建立）對應的教師。

    教師以「姓名 + 來源教師 ID」配對：同名但來源 ID 不同的教師視為不同人，
    避免把兩位同名老師混為一談。
    """
    existing = NtubOfferingSource.objects.filter(
        teacher_source_id=item.teacher_source_id,
        teacher_name=item.teacher_name,
    ).select_related("teacher").first()
    if existing is not None:
        summary.teachers_reused += 1
        return existing.teacher

    teacher = Teacher.objects.filter(name=item.teacher_name).first()
    if teacher is None:
        teacher = Teacher.objects.create(name=item.teacher_name)
        summary.teachers_created += 1
    else:
        summary.teachers_reused += 1
    return teacher


def _resolve_course(item: _ParsedOffering, teacher: Teacher, term: Term,
                    summary: ImportSummary) -> Course:
    """依（學期, 課號）找到既有課程；同名不同課號／不同名稱的既有課程記為衝突。

    既有課程若課名或教師與來源不符，**不覆寫**（可能是別的來源建立的），
    只記衝突並沿用既有的課程。
    """
    course = Course.objects.filter(term=term, code=item.course_code).first()
    if course is None:
        course = Course.objects.create(
            term=term, code=item.course_code, name=item.course_name,
            teacher=teacher, credits=item.credits)
        summary.courses_created += 1
        return course

    summary.courses_reused += 1
    if course.name != item.course_name:
        summary.add_conflict(
            "course_name", item.source_key,
            f"課號 {item.course_code}（{term.code}）既有課名「{course.name}」，"
            f"來源為「{item.course_name}」；保留既有資料，未覆寫。")
    elif course.teacher_id != teacher.id:
        summary.add_conflict(
            "course_teacher", item.source_key,
            f"課號 {item.course_code}（{term.code}）既有教師「{course.teacher.name}」，"
            f"來源為「{teacher.name}」；保留既有資料，未覆寫。")
    elif course.credits != item.credits:
        summary.add_conflict(
            "course_credits", item.source_key,
            f"課號 {item.course_code}（{term.code}）既有學分 {course.credits}，"
            f"來源為 {item.credits}；保留既有資料，未覆寫。")
    return course


def _resolve_section(item: _ParsedOffering, course: Course,
                     summary: ImportSummary) -> Section:
    """依來源鍵找到對應的 Section；找不到就建立。

    重複匯入的判斷順序：

    1. 先看 `NtubOfferingSource` 已經記錄過的 Section（來源鍵相同即同一筆）。
    2. 同一門課下若已有其他官方開課建立的班別（不同來源鍵、但課號與班別相同），
       沿用該班別，不另建。
    3. 都不是才建立新的班別。

    既有班別若已被人工調整（有加選紀錄、或時間／名額與匯入時不同），
    不覆寫關鍵設定，改為記衝突並保留原值。
    """
    source_existing = NtubOfferingSource.objects.filter(
        source_key=item.dedup_key).select_related("section").first()
    if source_existing is not None:
        section = source_existing.section
        summary.sections_reused += 1
        _check_section_drift(item, section, summary)
        return section

    existing = Section.objects.filter(course=course).order_by("label", "id").first()
    if existing is not None and section_matches_offering(existing, item):
        summary.sections_reused += 1
        _check_section_drift(item, existing, summary)
        return existing

    section = Section.objects.create(
        course=course,
        label=item.official_serial or "A",
        day=item.weekday,
        start_period=item.start_period,
        end_period=item.end_period,
        room=item.room,
        capacity=item.official_capacity if item.official_capacity is not None else 40,
    )
    summary.sections_created += 1
    return section


def section_matches_offering(section: Section, item: _ParsedOffering) -> bool:
    """既有的班別是否就是這筆官方開課（時間與教室相同）。"""
    return (section.day == item.weekday
            and section.start_period == item.start_period
            and section.end_period == item.end_period
            and section.room == item.room)


def _check_section_drift(item: _ParsedOffering, section: Section,
                          summary: ImportSummary) -> None:
    """重複匯入時檢查既有班別有沒有人工調整；不覆寫，只記衝突。"""
    source = NtubOfferingSource.objects.filter(
        course__term__code=item.term_code, course__code=item.course_code,
        section=section).first()

    if source is not None:
        if (source.applied_day != section.day
                or source.applied_start_period != section.start_period
                or source.applied_end_period != section.end_period
                or source.applied_room != section.room
                or source.applied_capacity != section.capacity):
            summary.add_conflict(
                "section_adjusted", item.dedup_key,
                f"課號 {item.course_code} 的班別已被調整過（時間／教室／名額與匯入時不同）；"
                "保留現況，未覆寫。")
        return

    if section.enrollments.exists():
        summary.add_conflict(
            "section_has_enrollment", item.dedup_key,
            f"課號 {item.course_code} 的班別已有加選紀錄；保留現況，未覆寫。")


def _upsert_offering_source(item: _ParsedOffering, term: Term,
                             class_group: AcademicClassGroup, course: Course,
                             section: Section, teacher: Teacher,
                             summary: ImportSummary) -> NtubOfferingSource:
    """寫入（或更新）官方開課來源對應紀錄。"""
    fields = {
        "academic_year": item.academic_year,
        "semester": item.semester,
        "class_code": item.class_code,
        "official_serial": item.official_serial,
        "course_code": item.course_code,
        "term": term,
        "class_group": class_group,
        "course": course,
        "section": section,
        "teacher": teacher,
        "teacher_source_id": item.teacher_source_id,
        "teacher_name": item.teacher_name,
        "course_name": item.course_name,
        "elective_category": item.elective_category,
        "classroom_code": item.classroom_code,
        "classroom_name": item.classroom_name,
        "official_capacity": item.official_capacity,
        "official_enrolled": item.official_enrolled,
        "source_url": item.source_url,
        "source_file": item.source_file,
        "retrieved_at": item.retrieved_at,
        "exported_at": item.exported_at,
        "applied_day": section.day,
        "applied_start_period": section.start_period,
        "applied_end_period": section.end_period,
        "applied_room": section.room,
        "applied_capacity": section.capacity,
        "source_payload": item.payload,
    }
    source, created = NtubOfferingSource.objects.update_or_create(
        source_key=item.dedup_key, defaults=fields)
    if created:
        summary.offerings_created += 1
    else:
        summary.offerings_updated += 1
    return source


def _upsert_recommendation(recommendation: dict[str, Any],
                            offering_by_key: dict[str, NtubOfferingSource],
                            summary: ImportSummary) -> NtubCurriculumRecommendation:
    """寫入（或更新）一筆課綱推薦。"""
    where = _text(recommendation.get("class_code"))
    course_name = _text(recommendation.get("course_name"))
    match_status = _text(recommendation.get("match_status"))
    offering_key = _text(recommendation.get("offering_key"))
    offering = offering_by_key.get(offering_key)

    class_group = AcademicClassGroup.objects.filter(
        code=_text(recommendation.get("class_code"))).first()
    course = None
    if offering is not None:
        course = offering.course
    elif _text(recommendation.get("matched_course_code")):
        # 來源沒有帶學期代碼，因此只以課號比對；多學期同課號時取該學期的開課。
        course = Course.objects.filter(
            code=_text(recommendation.get("matched_course_code"))).order_by("-term__code").first()

    is_ambiguous = match_status == MATCH_COURSE_NAME_ONLY
    ambiguity_message = ""
    if is_ambiguous:
        matched_class = (_text(recommendation.get("matched_offering_class_code"))
                         or (offering.class_group.code if offering else ""))
        ambiguity_message = (
            f"課名相同但官方開課班級為 {matched_class}，"
            f"與推薦班級 {_text(recommendation.get('class_code'))} 不同；"
            "跨班選修尚未確認。")

    fields = {
        "admission_year": _text(recommendation.get("admission_year")),
        "track": _text(recommendation.get("track")),
        "program_code": _text(recommendation.get("program_code")),
        "program_name": _text(recommendation.get("program_name")),
        "department_code": _text(recommendation.get("department_code")),
        "department_name": _text(recommendation.get("department_name")),
        "grade": _int(recommendation.get("grade")),
        "semester": _text(recommendation.get("semester")),
        "class_code": _text(recommendation.get("class_code")),
        "course_name": course_name,
        "match_status": match_status,
        "matched_offering_class_code": _text(recommendation.get("matched_offering_class_code")),
        "matched_course_code": _text(recommendation.get("matched_course_code")),
        "offering": offering,
        "course": course,
        "class_group": class_group,
        "is_ambiguous": is_ambiguous,
        "ambiguity_message": ambiguity_message,
        "source_payload": recommendation,
    }
    lookup = {
        "admission_year": fields["admission_year"],
        "track": fields["track"],
        "program_code": fields["program_code"],
        "department_code": fields["department_code"],
        "grade": fields["grade"],
        "semester": fields["semester"],
        "class_code": fields["class_code"],
        "course_name": course_name,
    }
    obj, created = NtubCurriculumRecommendation.objects.update_or_create(
        defaults=fields, **lookup)
    if created:
        summary.recommendations_created += 1
    else:
        summary.recommendations_updated += 1

    # 候選開課關係
    for candidate in recommendation.get("candidates") or []:
        candidate_key = _text(candidate.get("offering_key"))
        _, created = NtubRecommendationCandidate.objects.update_or_create(
            recommendation=obj,
            offering_key=candidate_key,
            defaults={
                "offering": offering_by_key.get(candidate_key),
                "course_code": _text(candidate.get("course_code")),
                "class_code": _text(candidate.get("class_code")),
                "class_name": _text(candidate.get("class_name")),
                "course_name": _text(candidate.get("course_name")),
            })
        if created:
            summary.candidates_created += 1
        else:
            summary.candidates_updated += 1

    return obj


def _match_recommendation(ambiguity: dict[str, Any],
                          recommendations: list[NtubCurriculumRecommendation]
                          ) -> NtubCurriculumRecommendation | None:
    """找出這筆疑義對應到哪一筆推薦。

    以「班級代碼 + 課程名稱」精確對應；來源沒帶的欄位就不拿來比對。
    """
    class_code = _text(ambiguity.get("class_code"))
    course_name = _text(ambiguity.get("course_name"))
    if not class_code and not course_name:
        return None
    for rec in recommendations:
        if class_code and rec.class_code != class_code:
            continue
        if course_name and rec.course_name != course_name:
            continue
        return rec
    return None


def _match_partial_keyword_offering(ambiguity: dict[str, Any],
                                    offering_by_key: dict[str, NtubOfferingSource]
                                    ) -> NtubOfferingSource | None:
    """找出「關鍵字部分比對」這筆疑義指向哪一筆開課。

    這類疑義來自「關鍵字只命中課名的一部分」（例如關鍵字「財務管理」抓到了
    「財務管理(二)(技優領航專班)」），所以先找出「課名包含關鍵字但不完全相同」
    的那筆課——那才是被部分命中的對象；找不到才退回比對來源檔名。
    """
    message = _text(ambiguity.get("message"))
    match = re.search(r"「(.+?)」", message)
    keyword = match.group(1).strip() if match else ""
    if keyword:
        for source in offering_by_key.values():
            if keyword in source.course_name and source.course_name != keyword:
                return source

    source_file = _text(ambiguity.get("file"))
    if source_file:
        for source in offering_by_key.values():
            if source.source_file == source_file:
                return source
    return None


def _upsert_ambiguity(ambiguity: dict[str, Any],
                       offering_by_key: dict[str, NtubOfferingSource],
                       recommendations: list[NtubCurriculumRecommendation],
                       summary: ImportSummary) -> None:
    """原樣保存來源列出的疑義。"""
    kind = _text(ambiguity.get("kind"))
    message = _text(ambiguity.get("message"))

    offering = offering_by_key.get(_text(ambiguity.get("offering_key")))
    if offering is None and kind == AMBIGUITY_RECOMMENDATION_CLASS_MISMATCH:
        # 這類疑義帶的是「候選開課」，用候選的來源鍵找到對應的官方開課。
        for candidate in ambiguity.get("candidates") or []:
            candidate_offering = offering_by_key.get(_text(candidate.get("offering_key")))
            if candidate_offering is not None:
                offering = candidate_offering
                break

    recommendation = _match_recommendation(ambiguity, recommendations)
    if kind == AMBIGUITY_SOURCE_PARTIAL_KEYWORD_MATCH and offering is None:
        offering = _match_partial_keyword_offering(ambiguity, offering_by_key)

    exists = NtubOfferingAmbiguity.objects.filter(kind=kind, message=message).first()
    if exists is not None:
        if offering is not None and exists.offering_id is None:
            exists.offering = offering
            exists.save(update_fields=["offering"])
        if recommendation is not None and exists.recommendation_id is None:
            exists.recommendation = recommendation
            exists.save(update_fields=["recommendation"])
        return

    NtubOfferingAmbiguity.objects.create(
        kind=kind, message=message, offering=offering,
        recommendation=recommendation, source_payload=ambiguity)
    summary.ambiguities_created += 1


def import_payload(payload: dict[str, Any]) -> ImportSummary:
    """把來源 payload 寫進資料庫。

    整批匯入在同一個 `transaction.atomic()` 內完成：任何一筆有問題就整批回滾，
    不會留下半套課程資料。
    """
    validate_payload(payload)
    summary = ImportSummary()

    with transaction.atomic():
        parsed = [parse_offering(offering) for offering in payload["offerings"]]

        offering_by_key: dict[str, NtubOfferingSource] = {}
        for item in parsed:
            class_group = _resolve_class_group(item.class_code, item.class_name)
            if class_group is None:
                summary.add_conflict(
                    "class_group_missing", item.source_key,
                    f"資料庫沒有班級 {item.class_code}"
                    f"（來源名稱：{item.class_name}）；請先匯入學籍目錄。"
                    "這筆開課未匯入。")
                continue

            term = _resolve_term(item.term_code)
            teacher = _resolve_teacher(item, summary)
            course = _resolve_course(item, teacher, term, summary)
            section = _resolve_section(item, course, summary)
            source = _upsert_offering_source(item, term, class_group, course,
                                              section, teacher, summary)
            offering_by_key[item.source_key] = source
            offering_by_key[item.dedup_key] = source

        # 課綱推薦
        recommendations: list[NtubCurriculumRecommendation] = []
        for recommendation in payload.get("curriculum_recommendations") or []:
            recommendations.append(_upsert_recommendation(recommendation, offering_by_key, summary))

        # 疑義
        for ambiguity in payload.get("ambiguities") or []:
            _upsert_ambiguity(ambiguity, offering_by_key, recommendations, summary)

    return summary


def import_from_file(path: Path | str | None = None) -> ImportSummary:
    """讀來源檔後匯入。"""
    return import_payload(load_payload(path))
