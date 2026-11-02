"""Safe, dependency-free extraction of structured fields from DOCX syllabi."""

import re
import unicodedata
import zipfile
from xml.etree import ElementTree


MAX_UNCOMPRESSED_SIZE = 20 * 1024 * 1024
MAX_DOCUMENT_XML_SIZE = 10 * 1024 * 1024
WORD_NAMESPACE = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
MAIN_DOCUMENT_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument."
    "wordprocessingml.document.main+xml"
)


class DocxSyllabusError(ValueError):
    pass


ALIASES = {
    "teaching_target": ("教學目標", "教學宗旨", "教學目的"),
    "teaching_matter": ("教學內容", "課程內容", "授課內容"),
    "assessment": ("成績評量", "成績考核", "評量方式", "評量標準"),
    "references": (
        "指定教科書及參考書籍", "指定教科書及參考資料",
        "指定教科書", "參考書籍", "參考資料",
    ),
}
CHINESE_NUMBERS = {
    "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6,
    "七": 7, "八": 8, "九": 9, "十": 10, "十一": 11, "十二": 12,
    "十三": 13, "十四": 14, "十五": 15, "十六": 16, "十七": 17,
    "十八": 18,
}
WEEK_PATTERN = re.compile(
    r"^(?:第)?(?:(?:week[_-]*0*(\d{1,2})(?:週|周)?)|(\d{1,2})|"
    r"([一二三四五六七八九十]{1,3}))(?:週|周)?(?:進度)?(.*)$",
    re.IGNORECASE,
)
NON_SYLLABUS_HEADINGS = (
    "學年", "學期", "開課班級", "課程代碼", "課號", "中文科目名稱",
    "英文科目名稱", "課名", "科目名稱", "學分數", "每週上課", "任課教師",
    "授課教師", "授課老師", "核心能力指標", "外語教學", "SDGs",
    "課程屬性", "授課方式", "教學方法", "教學設備", "課程備註",
)


def _paragraph_text(paragraph):
    parts = []
    for node in paragraph.iter():
        if node.tag == f"{WORD_NAMESPACE}t" and node.text:
            parts.append(node.text)
        elif node.tag == f"{WORD_NAMESPACE}tab":
            parts.append("\t")
        elif node.tag == f"{WORD_NAMESPACE}br":
            parts.append("\n")
    return "".join(parts).strip()


def _normalize_label(text):
    return "".join(
        character
        for character in unicodedata.normalize("NFKC", text)
        if not character.isspace()
    ).rstrip(":：")


def _raw_suffix_after_label(text, normalized_label):
    normalized_length = 0
    for index, character in enumerate(text):
        normalized_length += sum(
            not part.isspace()
            for part in unicodedata.normalize("NFKC", character)
        )
        if normalized_length >= len(normalized_label):
            return text[index + 1:].lstrip(" \t\r\n:：-—")
    return ""


def _cell_text(cell):
    return "\n".join(
        text for text in
        (_paragraph_text(paragraph)
         for paragraph in cell.findall(f".//{WORD_NAMESPACE}p"))
        if text
    )


def _table_rows(root):
    for table in root.findall(f".//{WORD_NAMESPACE}tbl"):
        for row in table.findall(f"{WORD_NAMESPACE}tr"):
            yield [_cell_text(cell)
                   for cell in row.findall(f"{WORD_NAMESPACE}tc")]


def _document_blocks(root):
    body = root.find(f".//{WORD_NAMESPACE}body")
    if body is None:
        raise DocxSyllabusError("Word 文件沒有可讀取的本文。")
    for child in list(body):
        if child.tag == f"{WORD_NAMESPACE}p":
            text = _paragraph_text(child)
            if text:
                yield text
        elif child.tag == f"{WORD_NAMESPACE}tbl":
            for row in child.findall(f"{WORD_NAMESPACE}tr"):
                for cell in row.findall(f"{WORD_NAMESPACE}tc"):
                    text = "\n".join(
                        text for text in
                        (_paragraph_text(p) for p in cell.findall(f"{WORD_NAMESPACE}p"))
                        if text
                    )
                    if text:
                        yield text


def _label_value(text):
    normalized = _normalize_label(text)
    week_match = WEEK_PATTERN.match(normalized)
    if week_match:
        number_text = week_match.group(1) or week_match.group(2)
        number = (int(number_text) if number_text
                  else CHINESE_NUMBERS.get(week_match.group(3)))
        if number is not None and 1 <= number <= 18:
            suffix = _raw_suffix_after_label(text, normalized[:week_match.start(4)])
            return f"week_{number:02d}", suffix

    for field_name, aliases in ALIASES.items():
        for alias in aliases:
            normalized_alias = _normalize_label(alias)
            if normalized.startswith(normalized_alias):
                suffix = _raw_suffix_after_label(text, normalized_alias)
                return field_name, suffix
    return None, text


def _values_from_tables(root, values):
    found = False
    for cells in _table_rows(root):
        if len(cells) < 2:
            continue
        field_name, _ = _label_value(cells[0])
        if field_name is None:
            continue
        found = True
        content = "\n".join(cell for cell in cells[1:] if cell)
        if content:
            values[field_name] = "\n".join(
                part for part in (values[field_name], content) if part
            )
        if field_name == "references":
            break
    return found


def parse_docx_syllabus(uploaded_file):
    """Return recognized field text; unrecognized fields remain empty for review."""
    if getattr(uploaded_file, "size", 0) > 5 * 1024 * 1024:
        raise DocxSyllabusError("檔案不可大於 5 MB。")
    try:
        uploaded_file.seek(0)
        with zipfile.ZipFile(uploaded_file) as package:
            infos = package.infolist()
            if (len(infos) > 1000
                    or sum(info.file_size for info in infos) > MAX_UNCOMPRESSED_SIZE):
                raise DocxSyllabusError("Word 文件內容大小不安全，無法匯入。")
            names = {info.filename: info for info in infos}
            content_types_info = names.get("[Content_Types].xml")
            document_info = names.get("word/document.xml")
            if content_types_info is None or document_info is None:
                raise DocxSyllabusError("檔案不是有效的 DOCX Word 文件。")
            if document_info.file_size > MAX_DOCUMENT_XML_SIZE:
                raise DocxSyllabusError("Word 文件本文過大，無法匯入。")
            content_types = package.read(content_types_info)
            if MAIN_DOCUMENT_CONTENT_TYPE.encode() not in content_types:
                raise DocxSyllabusError("檔案不是有效的 DOCX Word 文件。")
            document_xml = package.read(document_info)
            normalized_xml = document_xml.upper().replace(b"\x00", b"")
            if (b"<!DOCTYPE" in normalized_xml
                    or b"<!ENTITY" in normalized_xml):
                raise DocxSyllabusError("Word 文件包含不安全的 XML 宣告，無法匯入。")
            root = ElementTree.fromstring(document_xml)
    except DocxSyllabusError:
        raise
    except (zipfile.BadZipFile, ElementTree.ParseError, KeyError, RuntimeError) as exc:
        raise DocxSyllabusError("無法讀取此 DOCX 文件，請確認檔案完整且未損毀。") from exc
    finally:
        uploaded_file.seek(0)

    values = {
        "teaching_target": "",
        "teaching_matter": "",
        "assessment": "",
        "references": "",
        **{f"week_{week:02d}": "" for week in range(1, 19)},
    }
    if _values_from_tables(root, values):
        return values

    active_field = None
    for block in _document_blocks(root):
        for line in block.splitlines():
            line = line.strip()
            if not line:
                continue
            field_name, content = _label_value(line)
            if field_name is not None:
                active_field = field_name
                if content:
                    values[field_name] = "\n".join(
                        part for part in (values[field_name], content) if part
                    )
                continue
            if any(line.startswith(heading) for heading in NON_SYLLABUS_HEADINGS):
                active_field = None
                continue
            if active_field is not None:
                values[active_field] = "\n".join(
                    part for part in (values[active_field], content) if part
                )
    return values
