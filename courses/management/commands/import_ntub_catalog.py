"""把 NTUB 學籍目錄的 MySQL 還原檔轉成 Django ORM 的資料。

來源檔：ntubook_academic_missing_rows_restore.sql
用法：python manage.py import_ntub_catalog --file <sql 路徑>

只讀來源 SQL（自己寫的極簡 INSERT 剖析器），再用 ORM 寫入，
不直接對 MySQL 執行 SQL。重複執行不會產生重複資料（以 code 為唯一鍵 upsert）。
"""

import json
import re
from datetime import datetime
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from ...models import (AcademicCatalog, AcademicClassGroup, AcademicDepartment,
                       AcademicProgramType)

# 來源檔預設位置（可用 --file 覆蓋）
DEFAULT_SOURCE = Path(
    r"C:\Users\user\OneDrive\Desktop\course_select\ntubook_academic_missing_rows_restore.sql")

# 來源 SQL 的表名
TABLE_PROGRAM_TYPES = "program_types"
TABLE_DEPARTMENTS = "departments"
TABLE_CLASS_GROUPS = "class_groups"
TABLE_CATALOGS = "academic_catalogs"
TABLE_SOURCES = "academic_catalog_sources"


# --- 極簡 SQL INSERT 剖析 --------------------------------------------------------

_INSERT_RE = re.compile(r"INSERT\s+INTO\s+`(?P<table>[A-Za-z_][A-Za-z0-9_]*)`", re.IGNORECASE)


def _read_paren_block(text, start):
    """從 text[start]（必須是 '('）讀到對應的 ')'，回傳 (內容, 結束位置)。"""
    if text[start] != "(":
        raise ValueError("不是括號開頭")
    depth = 0
    i = start
    in_quote = False
    while i < len(text):
        ch = text[i]
        if in_quote:
            if ch == "\\":
                i += 2
                continue
            if ch == "'":
                in_quote = False
        elif ch == "'":
            in_quote = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return text[start + 1:i], i + 1
        i += 1
    raise ValueError("括號沒有閉合")


def _split_top_level(body, sep=","):
    """依 sep 切分，但跳過引號內與括號內的 sep。"""
    parts = []
    depth = 0
    in_quote = False
    buf = []
    i = 0
    while i < len(body):
        ch = body[i]
        if in_quote:
            buf.append(ch)
            if ch == "\\":
                if i + 1 < len(body):
                    buf.append(body[i + 1])
                    i += 2
                    continue
            elif ch == "'":
                in_quote = False
            i += 1
            continue
        if ch == "'":
            in_quote = True
            buf.append(ch)
        elif ch == "(":
            depth += 1
            buf.append(ch)
        elif ch == ")":
            depth -= 1
            buf.append(ch)
        elif ch == sep and depth == 0:
            parts.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
        i += 1
    parts.append("".join(buf).strip())
    return parts


def _parse_value(token):
    """把一個欄位值轉成 Python 值：'x' → str、NULL → None、數字 → int/float。"""
    token = token.strip()
    if token.upper() == "NULL":
        return None
    if len(token) >= 2 and token.startswith("'") and token.endswith("'"):
        inner = token[1:-1]
        return inner.replace("''", "'").replace("\\'", "'").replace("\\\\", "\\")
    try:
        return int(token)
    except ValueError:
        pass
    try:
        return float(token)
    except ValueError:
        pass
    return token


def _statement_body(text, pos):
    """從 pos 取到分號結束的內容（跳過引號內的分號）。"""
    in_quote = False
    i = pos
    while i < len(text):
        ch = text[i]
        if in_quote:
            if ch == "\\":
                i += 2
                continue
            if ch == "'":
                in_quote = False
        elif ch == "'":
            in_quote = True
        elif ch == ";":
            return text[pos:i]
        i += 1
    return text[pos:]


def _split_value_rows(body):
    """把 VALUES 後面的 (...), (...), ... 切成一行一行的原始字串。"""
    rows = []
    i = 0
    n = len(body)
    while i < n:
        if body[i] != "(":
            i += 1
            continue
        try:
            content, end = _read_paren_block(body, i)
        except ValueError:
            break
        rows.append(content)
        i = end
        while i < n and body[i] in ", \t\r\n":
            i += 1
    return rows


def parse_sql_inserts(text):
    """剖析整份 SQL，回傳 {表名: [dict, ...]}（欄位名 → 值）。"""
    tables = {}
    for match in _INSERT_RE.finditer(text):
        table = match.group("table")
        pos = match.end()
        while pos < len(text) and text[pos] in " \t\r\n":
            pos += 1
        if pos >= len(text) or text[pos] != "(":
            continue
        try:
            column_block, pos = _read_paren_block(text, pos)
        except ValueError:
            continue
        columns = [c.strip().strip("`").strip() for c in _split_top_level(column_block)]
        rest = text[pos:]
        values_at = re.search(r"\bVALUES\b", rest, re.IGNORECASE)
        if not values_at:
            continue
        pos = pos + values_at.end()
        rows = []
        for raw in _split_value_rows(_statement_body(text, pos)):
            cells = _split_top_level(raw)
            if len(cells) != len(columns):
                raise ValueError(f"{table}: 欄位數({len(columns)})與值數({len(cells)})不符")
            rows.append({c: _parse_value(v) for c, v in zip(columns, cells)})
        tables.setdefault(table, []).extend(rows)
    return tables


# --- 值轉換 ---------------------------------------------------------------------

def _to_bool(value):
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in ("1", "true", "t", "yes", "y")


def _to_int(value):
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_text(value):
    if value is None:
        return ""
    return str(value)


def _to_datetime(value):
    """'2026-07-24 10:36:26' → aware datetime。"""
    if value in (None, ""):
        return None
    text = str(value).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            naive = datetime.strptime(text, fmt)
        except ValueError:
            continue
        return timezone.make_aware(naive, timezone.get_current_timezone())
    return None


def _to_payload(value):
    """source_payload 是 JSON 字串，轉成 dict 存進 JSONField。"""
    if value in (None, ""):
        return {}
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {"raw": str(value)}
    return parsed if isinstance(parsed, dict) else {"raw": parsed}


class Command(BaseCommand):
    help = "匯入 NTUB 學籍目錄（學制 / 系所 / 班級），可重複執行"

    def add_arguments(self, parser):
        parser.add_argument("--file", default=str(DEFAULT_SOURCE),
                            help="來源 SQL 檔路徑（預設：%s）" % DEFAULT_SOURCE)
        parser.add_argument("--prune", action="store_true",
                            help="刪除本次目錄批次中、但來源已不存在的資料")
        parser.add_argument("--dry-run", action="store_true",
                            help="只解析與統計，不寫入資料庫")

    def handle(self, *args, **opts):
        path = Path(opts["file"])
        if not path.exists():
            raise CommandError(f"找不到來源檔：{path}")
        tables = parse_sql_inserts(path.read_text(encoding="utf-8"))

        for required in (TABLE_CATALOGS, TABLE_SOURCES, TABLE_PROGRAM_TYPES,
                         TABLE_DEPARTMENTS, TABLE_CLASS_GROUPS):
            if not tables.get(required):
                raise CommandError(f"來源檔缺少 {required} 的 INSERT 資料")

        summary = self._import(tables, prune=opts["prune"], dry_run=opts["dry_run"])
        if opts["dry_run"]:
            self.stdout.write(self.style.WARNING("dry-run：未寫入資料庫"))
            return
        self.stdout.write(self.style.SUCCESS(
            "匯入完成：學制 {program_types} 筆、系所 {departments} 筆、班級 {class_groups} 筆"
            "（可選 {selectable} 筆）".format(**summary)))

    # --- 實際寫入 ---------------------------------------------------------------

    def _import(self, tables, prune, dry_run):
        catalog_row = tables[TABLE_CATALOGS][0]
        source_row = tables[TABLE_SOURCES][0]

        program_type_rows = tables[TABLE_PROGRAM_TYPES]
        department_rows = tables[TABLE_DEPARTMENTS]
        class_group_rows = tables[TABLE_CLASS_GROUPS]

        summary = {
            "program_types": len(program_type_rows),
            "departments": len(department_rows),
            "class_groups": len(class_group_rows),
            "selectable": sum(1 for r in class_group_rows
                              if _to_bool(r.get("is_registration_selectable"))),
        }
        if dry_run:
            return summary

        with transaction.atomic():
            catalog = self._upsert_catalog(catalog_row, source_row)
            program_types = self._upsert_program_types(program_type_rows, catalog)
            departments = self._upsert_departments(department_rows, program_types, catalog)
            self._upsert_class_groups(class_group_rows, departments, catalog)
            if prune:
                self._prune(catalog, program_type_rows, department_rows, class_group_rows)
        return summary

    def _upsert_catalog(self, catalog_row, source_row):
        catalog, _ = AcademicCatalog.objects.update_or_create(
            version=catalog_row["version"],
            defaults={
                "source_key": _to_text(source_row.get("key")) or "ntub",
                "source_name": _to_text(source_row.get("name")),
                "last_successful_sync_at": _to_datetime(source_row.get("last_successful_sync_at")),
                "schema_version": _to_int(catalog_row.get("schema_version")) or 1,
                "source_file_name": _to_text(catalog_row.get("source_file_name")),
                "file_sha256": _to_text(catalog_row.get("file_sha256")),
                "academic_year": catalog_row.get("academic_year"),
                "term": catalog_row.get("term"),
                "status": _to_text(catalog_row.get("status")) or "ACTIVE",
                "fetched_at": _to_datetime(catalog_row.get("fetched_at")),
                "imported_at": _to_datetime(catalog_row.get("imported_at")) or timezone.now(),
                "source_row_count": _to_int(catalog_row.get("source_row_count")) or 0,
                "program_count": _to_int(catalog_row.get("program_count")) or 0,
                "department_count": _to_int(catalog_row.get("department_count")) or 0,
                "class_group_count": _to_int(catalog_row.get("class_group_count")) or 0,
                "selectable_class_group_count": _to_int(catalog_row.get("selectable_class_group_count")) or 0,
            },
        )
        return catalog

    def _upsert_program_types(self, rows, catalog):
        """回傳 {來源表 id: AcademicProgramType}，供系所解析外鍵。"""
        result = {}
        for row in rows:
            code = _to_text(row["code"])
            program_type, _ = AcademicProgramType.objects.update_or_create(
                code=code,
                defaults={
                    "catalog": catalog,
                    "name_zh": _to_text(row.get("name_zh")),
                    "name_en": _to_text(row.get("name_en")),
                    "is_active": _to_bool(row.get("is_active")),
                    "is_registration_selectable": _to_bool(row.get("is_registration_selectable")),
                    "sort_order": _to_int(row.get("sort_order")) or 0,
                    "source_last_seen_at": _to_datetime(row.get("source_last_seen_at")),
                },
            )
            result[row.get("id")] = program_type
        return result

    def _upsert_departments(self, rows, program_types, catalog):
        """回傳 {來源表 id: AcademicDepartment}，供班級解析外鍵。"""
        result = {}
        for row in rows:
            program_type = program_types.get(row.get("program_type_id"))
            if program_type is None:
                raise CommandError(
                    f"department {row.get('code')} 找不到 program_type_id={row.get('program_type_id')}")
            code = _to_text(row["code"])
            department, _ = AcademicDepartment.objects.update_or_create(
                code=code,
                defaults={
                    "catalog": catalog,
                    "program_type": program_type,
                    "name_zh": _to_text(row.get("name_zh")),
                    "name_en": _to_text(row.get("name_en")),
                    "is_active": _to_bool(row.get("is_active")),
                    "is_registration_selectable": _to_bool(row.get("is_registration_selectable")),
                    "sort_order": _to_int(row.get("sort_order")) or 0,
                    "unit_type": _to_text(row.get("unit_type")),
                    "source_last_seen_at": _to_datetime(row.get("source_last_seen_at")),
                },
            )
            result[row.get("id")] = department
        return result

    def _upsert_class_groups(self, rows, departments, catalog):
        for row in rows:
            department = departments.get(row.get("department_id"))
            if department is None:
                raise CommandError(
                    f"class_group {row.get('code')} 找不到 department_id={row.get('department_id')}")
            code = _to_text(row["code"])
            AcademicClassGroup.objects.update_or_create(
                code=code,
                defaults={
                    "catalog": catalog,
                    "department": department,
                    "name_zh": _to_text(row.get("name_zh")),
                    "grade_no": _to_int(row.get("grade_no")),
                    "section_code": row.get("section_code") or None,
                    "group_type": _to_text(row.get("group_type")),
                    "is_active": _to_bool(row.get("is_active")),
                    "is_registration_selectable": _to_bool(row.get("is_registration_selectable")),
                    "sort_order": _to_int(row.get("sort_order")) or 0,
                    "source_payload": _to_payload(row.get("source_payload")),
                },
            )

    def _prune(self, catalog, program_type_rows, department_rows, class_group_rows):
        """刪掉本批次中來源已不存在的列（依 code 比對）。"""
        keep_types = {_to_text(r["code"]) for r in program_type_rows}
        keep_depts = {_to_text(r["code"]) for r in department_rows}
        keep_groups = {_to_text(r["code"]) for r in class_group_rows}
        AcademicClassGroup.objects.filter(catalog=catalog).exclude(code__in=keep_groups).delete()
        AcademicDepartment.objects.filter(catalog=catalog).exclude(code__in=keep_depts).delete()
        AcademicProgramType.objects.filter(catalog=catalog).exclude(code__in=keep_types).delete()
