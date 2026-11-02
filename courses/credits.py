"""官方課表有效學分與學期學分摘要。"""
from dataclasses import dataclass
from decimal import Decimal

from .models import OfficialTeachingSyllabus


@dataclass(frozen=True)
class CreditLimits:
    minimum: int
    maximum: int


@dataclass(frozen=True)
class EntryCredit:
    value: Decimal | None
    basis: str


@dataclass(frozen=True)
class CreditSummary:
    official_confirmed: Decimal
    enrolled: Decimal
    total: Decimal
    complete: bool
    pending_course_codes: tuple[str, ...]
    limits: CreditLimits | None


def credit_limits(class_group):
    """依學制與年級解析學分上下限；未配置的學制不套用預設規則。"""
    if class_group is None or class_group.grade_no is None:
        return None
    program_type = class_group.department.program_type
    if program_type.code != "4":
        return None
    return CreditLimits(minimum=9 if class_group.grade_no == 4 else 16,
                        maximum=25)


def _entry_key(entry):
    snapshot = entry.snapshot
    return (
        snapshot.term.code,
        snapshot.class_group.code,
        entry.official_serial,
        entry.course_code,
    )


def _syllabus_for_entry(entry):
    return OfficialTeachingSyllabus.objects.filter(
        snapshot_id=entry.snapshot_id,
        academic_year=entry.snapshot.term.code.split("-", 1)[0],
        semester=entry.snapshot.term.code.split("-", 1)[1],
        class_code=entry.snapshot.class_group.code,
        official_serial=entry.official_serial,
        course_code=entry.course_code,
        status="success",
    ).first()


def _period_count(entries):
    slots = {
        (entry.weekday, period)
        for entry in entries
        for period in range(entry.start_period, entry.end_period + 1)
    }
    return len(slots)


def effective_entry_credit(entry):
    """回傳單一官方課程的有效學分與來源，不計官方活動。"""
    if entry.entry_type != "course" or not entry.course_code:
        return EntryCredit(None, "活動")
    if entry.snapshot.source_kind == "MAINTAINED" and entry.credits is not None:
        return EntryCredit(Decimal(entry.credits), "系所維護")
    syllabus = _syllabus_for_entry(entry)
    if syllabus is not None and syllabus.credits is not None:
        return EntryCredit(syllabus.credits, "官方教學大綱")
    snapshot = entry.snapshot
    if (
        snapshot.term.code == "115-1"
        and snapshot.class_group.code == "40630B"
        and entry.course_code == "40630991"
    ):
        return EntryCredit(Decimal("0"), "體育例外")
    periods = _period_count([entry])
    if periods <= 0:
        return EntryCredit(None, "待同步")
    return EntryCredit(Decimal(periods), "節次暫估")


def official_entry_credits(official_entries):
    """依官方五欄課程識別去重，回傳每門課的有效學分。"""
    grouped = {}
    for entry in official_entries:
        if entry.entry_type != "course" or not entry.course_code:
            continue
        grouped.setdefault(_entry_key(entry), []).append(entry)
    result = {}
    for key, entries in grouped.items():
        syllabus_credit = next(
            (effective_entry_credit(entry) for entry in entries
             if effective_entry_credit(entry).basis == "官方教學大綱"),
            None)
        if syllabus_credit is not None:
            result[key] = syllabus_credit
            continue
        first = entries[0]
        if (
            first.snapshot.term.code == "115-1"
            and first.snapshot.class_group.code == "40630B"
            and first.course_code == "40630991"
        ):
            result[key] = EntryCredit(Decimal("0"), "體育例外")
        else:
            periods = _period_count(entries)
            result[key] = EntryCredit(
                Decimal(periods) if periods > 0 else None,
                "節次暫估" if periods > 0 else "待同步",
            )
    return result


def summarize_credits(class_group, term, sections=(), official_entries=()):
    """計算官方課表與本站加選的聯集學分，同一課程只計一次。"""
    entries = [entry for entry in official_entries
               if entry.entry_type == "course" and entry.course_code]
    official = official_entry_credits(entries)
    official_confirmed = sum(
        (item.value for item in official.values() if item.value is not None),
        Decimal("0"),
    )
    pending = tuple(sorted(
        {key[3] for key, item in official.items() if item.value is None}))
    enrolled_by_code = {}
    for section in sections:
        enrolled_by_code.setdefault(section.course.code, Decimal(section.course.credits))
    enrolled = sum(enrolled_by_code.values(), Decimal("0"))
    official_codes = {key[3] for key in official}
    enrolled_not_official = sum(
        (credits for code, credits in enrolled_by_code.items()
         if code not in official_codes),
        Decimal("0"),
    )
    return CreditSummary(
        official_confirmed=official_confirmed,
        enrolled=enrolled,
        total=official_confirmed + enrolled_not_official,
        complete=not pending,
        pending_course_codes=pending,
        limits=credit_limits(class_group),
    )
