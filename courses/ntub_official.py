"""NTUB 公開課表與教師教學大綱的安全抓取、解析與快取服務。"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from urllib.parse import urlencode
from urllib.error import HTTPError
from urllib.request import (HTTPRedirectHandler, HTTPCookieProcessor, Request,
                            build_opener)
from http.cookiejar import CookieJar

from django.utils import timezone

from .models import OfficialTeachingSyllabus

CLASS_URL = "https://ntcbadm.ntub.edu.tw/pub/Cur_Class.aspx"
SYLLABUS_URL = "https://ntcbadm.ntub.edu.tw/TchWeb/Cur_SelTeaching.aspx"
ALLOWED_HOST = "ntcbadm.ntub.edu.tw"
CONNECT_RE = re.compile(
    r"ConnectCos_Short\(\s*['\"]([^'\"]+)['\"]\s*,\s*['\"]([^'\"]+)['\"]\s*,"
    r"\s*['\"]([^'\"]+)['\"]\s*,\s*['\"]([^'\"]+)['\"]\s*,\s*['\"]([^'\"]+)['\"]",
    re.I,
)


class OfficialFetchError(Exception):
    """官方公開頁面無法取得或內容無法驗證。"""


class _FormFieldParser(HTMLParser):
    """取得當次 WebForms 表單的 named controls，不保存敏感欄位內容。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.fields = {}
        self._select = None
        self._option = None
        self._option_value = ""
        self._option_selected = False

    def _add(self, name, value):
        if name:
            self.fields[name] = value

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        name = attrs.get("name")
        if tag == "input" and name:
            input_type = attrs.get("type", "text").lower()
            if input_type in {"checkbox", "radio"} and "checked" not in attrs:
                return
            self._add(name, attrs.get("value", ""))
        elif tag == "select" and name:
            self._select = name
            self.fields[name] = ""
        elif tag == "option" and self._select:
            self._option = []
            self._option_value = attrs.get("value", "")
            self._option_selected = "selected" in attrs

    def handle_data(self, data):
        if self._option is not None:
            self._option.append(data)

    def handle_endtag(self, tag):
        if tag == "option" and self._select and self._option is not None:
            if self._option_selected or not self.fields[self._select]:
                self.fields[self._select] = self._option_value or "".join(self._option).strip()
            self._option = None
        elif tag == "select":
            self._select = None


class _TextParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.ids = {}
        self.links = []
        self._stack = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        ident = attrs.get("id")
        if ident:
            self._stack.append((ident, []))
        if tag == "a" and attrs.get("href"):
            self.links.append(attrs["href"])

    def handle_endtag(self, tag):
        if self._stack:
            ident, chunks = self._stack.pop()
            self.ids[ident] = " ".join("".join(chunks).split())

    def handle_data(self, data):
        if self._stack:
            self._stack[-1][1].append(data)


class _SyllabusParser(HTMLParser):
    """保留指定課綱欄位的段落與換行，不收集其他頁面區塊。"""

    BLOCK_TAGS = {"br", "p", "div", "li", "tr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.values = {}
        self._active = []

    def handle_starttag(self, tag, attrs):
        ident = dict(attrs).get("id")
        if tag in self.BLOCK_TAGS:
            for _, _, chunks in self._active:
                chunks.append("\n")
        if ident:
            self._active.append((tag, ident, []))

    def handle_endtag(self, tag):
        if tag in self.BLOCK_TAGS:
            for _, _, chunks in self._active:
                chunks.append("\n")
        if self._active and self._active[-1][0] == tag:
            _, ident, chunks = self._active.pop()
            self.values[ident] = _clean_syllabus_text("".join(chunks))

    def handle_data(self, data):
        for _, _, chunks in self._active:
            chunks.append(data)

    def close(self):
        super().close()
        while self._active:
            _, ident, chunks = self._active.pop()
            self.values[ident] = _clean_syllabus_text("".join(chunks))


def _clean_syllabus_text(value: str) -> str:
    value = value.replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ")
    value = re.sub(r"[^\S\n]+", " ", value)
    return "\n".join(line.strip() for line in value.split("\n")).strip()


class _TimetableRowParser(HTMLParser):
    """保留課表列的文字，讓連結之外的課名與時段也能被解析。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows = []
        self._row = None
        self._cell = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._row is not None and self._cell is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


class _OfficialGridParser(HTMLParser):
    """解析官方課表的列、欄位置，不依賴頁面文字編碼。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows = []
        self._row = None
        self._cell = None
        self._href = ""

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []
            self._href = ""
        elif tag == "a" and self._cell is not None:
            self._href = attrs.get("href", "")

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._row is not None and self._cell is not None:
            self._row.append({
                "text": " ".join("".join(self._cell).split()),
                "href": self._href,
            })
            self._cell = None
            self._href = ""
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None


def _text(parser: _TextParser, ident: str) -> str:
    return parser.ids.get(ident, "").strip()


def _decimal(value: str) -> Decimal | None:
    match = re.search(r"\d+(?:\.\d+)?", value or "")
    if not match:
        return None
    try:
        return Decimal(match.group())
    except InvalidOperation:
        return None


def parse_syllabus_html(html: str, expected: dict[str, str]) -> dict:
    """解析並核對官方課綱；不接受只回 HTTP 200 的錯誤頁。"""
    parser = _SyllabusParser()
    parser.feed(html)
    parser.close()

    def field(*names):
        for name in names:
            if name in parser.values:
                return parser.values[name]
        for name in names:
            for ident, value in parser.values.items():
                if ident.endswith(name):
                    return value
        return ""

    values = {
        "academic_year": field("lblYears", "lblYear"),
        "semester": field("lblTerm"),
        "class_code": field("lblOP_Class", "lblClass"),
        "course_code": field("lblCos_ID", "lblC_CourseNo"),
        "course_name": field("lblCos_Name", "lblC_CName"),
        "course_name_en": field("lblCos_Eng", "lblC_EName"),
        "credits": _decimal(field("lblCos_Credit", "lblC_Credit")),
        "weekly_hours": _decimal(field("lblCos_Hours", "lblC_Hour")),
        "teacher_name": field("lblTchName", "lblC_Teacher"),
        "teaching_target": field("lblC_Target"),
        "teaching_matter": field("lblC_Matter"),
        "assessment": field("lblScore"),
        "references": field("lblReference"),
    }
    values["weekly_progress"] = {
        f"week_{week:02d}": field(f"lblWeek{week:02d}")
        for week in range(1, 19)
    }
    for key in ("academic_year", "semester", "class_code", "course_code"):
        expected_value = str(expected.get(key, "")).strip()
        if expected_value and values[key] and values[key] != expected_value:
            raise OfficialFetchError(
                f"官方課綱識別資料不一致：{key}={values[key]}，要求 {expected_value}")
        if expected_value and not values[key]:
            raise OfficialFetchError(f"官方課綱缺少核對欄位：{key}")
    if not values["course_code"]:
        raise OfficialFetchError("官方頁面不是有效課程教學大綱。")
    return values


def parse_timetable_html(html: str, academic_year: str, semester: str,
                         class_code: str, class_name: str = "") -> dict:
    """從官方班級頁的 ConnectCos_Short 連結產生既有匯入器 payload。"""
    grid_parser = _OfficialGridParser()
    grid_parser.feed(html)
    header_index = next(
        (index for index, row in enumerate(grid_parser.rows)
         if len(row) >= 8 and all(cell["text"] for cell in row[:3])),
        None,
    )
    if header_index is None:
        raise OfficialFetchError("官方課表缺少可辨識的星期欄位。")
    schedule_rows = [
        row for row in grid_parser.rows[header_index + 1:]
        if len(row) >= 8 and row[0]["text"]
    ]
    positions = {}
    for period, row in enumerate(schedule_rows, start=1):
        for column, cell in enumerate(row[1:8], start=1):
            match = CONNECT_RE.search(cell["href"])
            if match:
                positions.setdefault((match.group(4), match.group(5)), []).append(
                    (period, column, cell["text"]))

    entries = []
    seen = set()
    for match in CONNECT_RE.finditer(html):
        year, term, op_class, serial, course_id = match.groups()
        if (year, term, op_class) != (str(academic_year), str(semester), str(class_code)):
            raise OfficialFetchError("課表連結的學年、學期或班級與要求不一致。")
        key = (serial, course_id)
        if key in seen:
            continue
        seen.add(key)
        located = positions.get(key)
        if not located:
            raise OfficialFetchError(
                f"官方課表無法定位課程時段：序號 {serial}、課號 {course_id}。")
        periods = [item[0] for item in located]
        columns = {item[1] for item in located}
        if len(columns) != 1:
            raise OfficialFetchError(
                f"官方課表課程跨越不同星期欄：序號 {serial}、課號 {course_id}。")
        weekday_value = located[0][1]
        start_period = min(periods)
        end_period = max(periods)
        row_text = located[0][2]
        link_tail = html[match.end():match.end() + 240]
        name_match = re.search(r">\s*([^<]+?)\s*</a>", link_tail, re.S)
        course_name = " ".join(name_match.group(1).split()) if name_match else ""
        detail_match = re.search(
            r"</a>\s*<br\s*/?>\s*([^<]*)<br\s*/?>\s*([^<]*)",
            link_tail, re.I | re.S)
        teacher_name = " ".join(detail_match.group(1).split()) if detail_match else ""
        classroom = " ".join(detail_match.group(2).split()) if detail_match else ""
        name_match = re.search(
            r"(?:課名|科目名稱|科目)\s*[:：]?\s*([^|;；,，]+)", row_text)
        if name_match and not course_name:
            course_name = name_match.group(1).strip()
        if not course_name:
            quoted = re.search(r">\s*([^<]{2,80})\s*<", html[max(0, match.start() - 80):match.start()])
            course_name = quoted.group(1).strip() if quoted else ""
        entries.append({
            "entry_type": "course",
            "weekday": weekday_value,
            "start_period": start_period,
            "end_period": end_period,
            "periods": list(range(start_period, end_period + 1)),
            "course": {"code": course_id, "serial": serial, "name": course_name,
                       "teacher": teacher_name, "classroom": classroom},
            "activity": None,
            "source": {"raw_text": row_text, "href": match.group(0)},
        })
    if not entries:
        raise OfficialFetchError("官方課表沒有可驗證的 ConnectCos_Short 課程。")
    return {
        "schema_version": "1.0",
        "scope": "class_timetable",
        "produces_enrollments": False,
        "classes": [{
            "class_code": class_code,
            "class_name": class_name,
            "academic_year": str(academic_year),
            "semester": str(semester),
            "term_code": f"{academic_year}-{semester}",
            "entries": entries,
            "counts": {"total": len(entries), "course": len(entries), "activity": 0},
            "source": {"source_url": CLASS_URL,
                       "retrieved_at": timezone.now().isoformat()},
        }],
        "totals": {"class_count": 1, "entry_count": len(entries),
                   "course_count": len(entries), "activity_count": 0},
    }


@dataclass
class OfficialFetchClient:
    timeout: float = 20.0
    user_agent: str = "NTUB-course-demo/1.0"

    def __post_init__(self):
        self.cookie_jar = CookieJar()
        self.opener = build_opener(HTTPRedirectHandler(),
                                   HTTPCookieProcessor(self.cookie_jar))
        self.last_trace = []

    def get(self, url: str, params: dict[str, str] | None = None) -> str:
        if not url.startswith("https://" + ALLOWED_HOST + "/"):
            raise OfficialFetchError("只允許抓取 NTUB 公開 HTTPS 路徑。")
        if params:
            url += "?" + urlencode(params)
        request = Request(url, headers={"User-Agent": self.user_agent})
        return self._open_text(request, "GET")

    def _open_text(self, request: Request, step: str) -> str:
        path = request.full_url.split("?", 1)[0]
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                status = response.status
                content_type = response.headers.get_content_type()
                raw_body = response.read()
                declared_charset = response.headers.get_content_charset()
                charset = declared_charset or "utf-8"
                body = raw_body.decode(charset, errors="replace")
                if not declared_charset and "\ufffd" in body:
                    body = raw_body.decode("cp950", errors="replace")
        except HTTPError as exc:
            self.last_trace.append({
                "step": step, "status": exc.code, "path": path,
                "fields": getattr(request, "_ntub_fields", {}),
                "response_type": "http-error",
            })
            raise OfficialFetchError(
                f"官方課表查詢失敗：步驟 {step} HTTP {exc.code} {path}") from exc
        except Exception as exc:
            self.last_trace.append({
                "step": step, "status": None, "path": path,
                "fields": getattr(request, "_ntub_fields", {}),
                "response_type": "transport-error",
            })
            raise OfficialFetchError(f"官方網站擷取失敗：步驟 {step}：{exc}") from exc
        self.last_trace.append({
            "step": step, "status": status, "path": path,
            "fields": getattr(request, "_ntub_fields", {}),
            "response_type": content_type,
        })
        if status != 200 or not body.strip():
            raise OfficialFetchError(
                f"官方頁面無效：步驟 {step} HTTP {status} {path}")
        return body

    def fetch_syllabus(self, identifiers: dict[str, str]) -> dict:
        url = SYLLABUS_URL + "?" + urlencode({
            "Years": identifiers["academic_year"],
            "Term": identifiers["semester"],
            "OpClass": identifiers["class_code"],
            "Serial": identifiers["official_serial"],
            "COSID": identifiers["course_code"],
        })
        html = self.get(url)
        values = parse_syllabus_html(html, identifiers)
        values["source_url"] = url
        return values

    def fetch_timetable(self, identifiers: dict[str, str]) -> str:
        """以同一個 WebForms session 送出當次完整表單查詢。"""
        initial = self.get(CLASS_URL)
        parser = _FormFieldParser()
        parser.feed(initial)
        fields = dict(parser.fields)
        required = ("txtYear", "txtTerm", "ddlEdu", "ddlDept", "ddlClass")
        missing = [field for field in required if field not in fields]
        if missing:
            raise OfficialFetchError(
                f"官方表單缺少必要欄位：{', '.join(missing)}")
        base_values = {
            "txtYear": identifiers["academic_year"],
            "txtTerm": identifiers["semester"],
            "ddlEdu": identifiers.get("program_code", ""),
            "ddlDept": identifiers.get("department_code", ""),
            "ddlClass": identifiers["class_code"],
        }
        try:
            result = self._submit_form(
                fields, base_values, "", "timetable-query")
            self._validate_timetable_response(result, identifiers)
            return result
        except OfficialFetchError:
            # ddlDept/ddlClass are empty in the first GET. The public page
            # populates them with normal WebForms postbacks before querying.
            fields = self._postback(
                fields, {"txtYear": identifiers["academic_year"],
                         "txtTerm": identifiers["semester"],
                         "ddlEdu": identifiers.get("program_code", "")},
                "ddlEdu", "timetable-education")
            fields = self._postback(
                fields, {"txtYear": identifiers["academic_year"],
                         "txtTerm": identifiers["semester"],
                         "ddlEdu": identifiers.get("program_code", ""),
                         "ddlDept": identifiers.get("department_code", "")},
                "ddlDept", "timetable-department")
            result = self._submit_form(
                fields, base_values, "", "timetable-query")
            self._validate_timetable_response(result, identifiers)
            return result

    @staticmethod
    def _validate_timetable_response(html: str, identifiers: dict[str, str]) -> None:
        if re.search(r"<title[^>]*>[^<]*(?:Server Error|Runtime Error)",
                     html, re.I):
            raise OfficialFetchError("官方課表回傳 ASP.NET 錯誤頁，未建立預覽。")
        expected = (
            str(identifiers["academic_year"]),
            str(identifiers["semester"]),
            str(identifiers["class_code"]),
        )
        if not any(match.groups()[:3] == expected
                   for match in CONNECT_RE.finditer(html)):
            raise OfficialFetchError(
                "官方回應不是指定學年、學期與班級的課表，未建立預覽。")

    def _submit_form(self, fields: dict[str, str], values: dict[str, str],
                     event_target: str, step: str) -> str:
        payload = dict(fields)
        if event_target == "ddlEdu":
            payload.pop("ddlDept", None)
            payload.pop("ddlClass", None)
        elif event_target == "ddlDept":
            payload.pop("ddlClass", None)
        payload.update(values)
        payload["__EVENTTARGET"] = event_target
        payload["__EVENTARGUMENT"] = ""
        payload["__LASTFOCUS"] = ""
        payload["doQuery"] = "Y" if not event_target else ""
        payload["ToExcel"] = ""
        for field in ("Years", "Term", "OpClass", "Serial", "Cos_Short", "COSID"):
            if field in payload:
                payload[field] = ""
        request = Request(
            CLASS_URL, data=urlencode(payload).encode(),
            headers={"User-Agent": self.user_agent,
                     "Referer": CLASS_URL,
                     "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                     "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
                     "Content-Type": "application/x-www-form-urlencoded"})
        request._ntub_fields = {
            key: payload.get(key, "")
            for key in ("txtYear", "txtTerm", "ddlEdu", "ddlDept", "ddlClass",
                        "doQuery")
        }
        return self._open_text(request, step)

    def _postback(self, fields: dict[str, str], values: dict[str, str],
                  event_target: str, step: str) -> dict[str, str]:
        html = self._submit_form(fields, values, event_target, step)
        parser = _FormFieldParser()
        parser.feed(html)
        return parser.fields


def _matching_snapshot(identifiers):
    from .models import NtubTimetableSnapshot

    snapshot = (NtubTimetableSnapshot.objects
                .select_related("term", "class_group")
                .filter(
                    term__code=f"{identifiers['academic_year']}-{identifiers['semester']}",
                    class_group__code=identifiers["class_code"],
                    source_kind=NtubTimetableSnapshot.SourceKind.IMPORTED,
                ).first())
    if snapshot is None:
        return None
    return snapshot


def _matching_course_entry(snapshot, identifiers):
    return snapshot.entries.filter(
        entry_type="course",
        official_serial=identifiers["official_serial"],
        course_code=identifiers["course_code"],
    ).first()


def sync_syllabus(identifiers: dict[str, str], client: OfficialFetchClient | None = None,
                  snapshot=None):
    """單門課綱失敗只更新自身狀態，不影響其他快取。"""
    from .models import OfficialTeachingSyllabus

    if snapshot is None:
        snapshot = _matching_snapshot(identifiers)
    if snapshot is None or _matching_course_entry(snapshot, identifiers) is None:
        raise OfficialFetchError(
            "找不到與官方課綱五欄識別相符的班級課表快照與課程明細。")

    client = client or OfficialFetchClient()
    existing = OfficialTeachingSyllabus.objects.filter(
        academic_year=identifiers["academic_year"],
        semester=identifiers["semester"],
        class_code=identifiers["class_code"],
        official_serial=identifiers["official_serial"],
        course_code=identifiers["course_code"],
    ).first()
    defaults = {
        "status": "failed",
        "error_message": "",
        "source_url": SYLLABUS_URL,
        "fetched_at": timezone.now(),
        "snapshot": snapshot,
    }
    try:
        values = client.fetch_syllabus(identifiers)
        defaults.update(values, status="success", error_message="")
    except OfficialFetchError as exc:
        if existing and existing.status == "success":
            return existing
        defaults["error_message"] = str(exc)
    record, _ = OfficialTeachingSyllabus.objects.update_or_create(
        academic_year=identifiers["academic_year"],
        semester=identifiers["semester"],
        class_code=identifiers["class_code"],
        official_serial=identifiers["official_serial"],
        course_code=identifiers["course_code"],
        defaults=defaults,
    )
    return record


def sync_timetable_syllabi(class_group, term, client=None, limit=50,
                            interval=0.0, retry=False):
    """同步指定班級課表的課綱；不修改課表與人工核對學分。"""
    from .models import OfficialCourseCredit, NtubTimetableEntry

    client = client or OfficialFetchClient()
    year, semester = str(term.code).split("-", 1)
    entries = (NtubTimetableEntry.objects.filter(
        snapshot__class_group=class_group, snapshot__term=term,
        snapshot__source_kind="IMPORTED",
        entry_type="course").exclude(course_code="").order_by("id")[:limit])
    records = []
    seen = set()
    for entry in entries:
        identity = (year, semester, class_group.code,
                    entry.official_serial, entry.course_code)
        if identity in seen:
            continue
        seen.add(identity)
        if not retry:
            cached = OfficialTeachingSyllabus.objects.filter(
                academic_year=year, semester=semester,
                class_code=class_group.code,
                official_serial=entry.official_serial,
                course_code=entry.course_code,
                status="success").first()
            if cached:
                cached.sync_result = _syllabus_sync_result(
                    cached, OfficialCourseCredit.objects.filter(
                        term=term, class_group=class_group,
                        course_code=entry.course_code).first(), cached=True)
                records.append(cached)
                continue
        record = sync_syllabus({
            "academic_year": year, "semester": semester,
            "class_code": class_group.code,
            "official_serial": entry.official_serial,
            "course_code": entry.course_code,
        }, client, snapshot=entry.snapshot)
        record.sync_result = _syllabus_sync_result(
            record, OfficialCourseCredit.objects.filter(
                term=term, class_group=class_group,
                course_code=entry.course_code).first(), cached=False)
        records.append(record)
        if interval:
            time.sleep(interval)
    return records


def _syllabus_sync_result(record, confirmed_credit, *, cached):
    if record.status != "success":
        return "抓取失敗"
    if (confirmed_credit is not None and record.credits is not None
            and confirmed_credit.credits != record.credits):
        return "學分與既有核對值不一致"
    return "已有快取" if cached else "已成功"


def sync_offering_syllabi(queryset=None, client: OfficialFetchClient | None = None):
    """依官方開課來源的五欄識別值批次補抓課綱，不改動來源資料。"""
    from .models import NtubOfferingSource

    client = client or OfficialFetchClient()
    sources = queryset if queryset is not None else NtubOfferingSource.objects.all()
    records = []
    for source in sources.iterator():
        identifiers = {
            "academic_year": source.academic_year,
            "semester": source.semester,
            "class_code": source.class_code,
            "official_serial": source.official_serial,
            "course_code": source.course_code,
        }
        snapshot = _matching_snapshot(identifiers)
        if snapshot is None or _matching_course_entry(snapshot, identifiers) is None:
            continue
        records.append(sync_syllabus(identifiers, client, snapshot=snapshot))
    return records
