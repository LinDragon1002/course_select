from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile
from xml.sax.saxutils import escape

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase

from courses.syllabus_docx import _label_value, parse_docx_syllabus


WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
DOCX_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument."
    "wordprocessingml.document.main+xml"
)


def _cell(paragraphs):
    return (
        "<w:tc>"
        + "".join(
            f"<w:p><w:r><w:t>{escape(paragraph)}</w:t></w:r></w:p>"
            for paragraph in paragraphs
        )
        + "</w:tc>"
    )


def make_pdf_converted_docx():
    info_rows = [
        ("學年", "115", "學期", "第 1 學期"),
        ("開課班級", "40630B", "科目名稱", "課程名稱"),
    ]
    week_labels = [
        "第 ⼀ 週", "第 ⼆ 週", "第 03 週", "第四週", "第五週",
        "第六週", "第七週", "第 ⼋ 週", "第九週", "第 ⼗ 週",
        "第⼗⼀週", "第十二週", "第十三週", "第十四週", "第十五週",
        "第十六週", "第十七週", "第⼗⼋週",
    ]
    syllabus_rows = [
        (["項目"], ["內容"]),
        (["教學　⽬標："], ["學生能透過Django框架自行建置一應用型網站……"]),
        (["教學內容"], ["網站開發與實作"]),
    ]
    for week, label in enumerate(week_labels, 1):
        content = [f"第 {week} 週內容"]
        if week == 1:
            content = ["Web Server環境與Http知識介紹"]
        elif week == 2:
            content = ["網站開發環境建置(python、Django、VS code)"]
        elif week == 3:
            content = ["Django網站快速入門", "github建置"]
        elif week == 10:
            content = ["實務網站-借還書系統實作-Django Admin"]
        elif week == 18:
            content = ["期末系統與上線demo"]
        syllabus_rows.append(([label], content))
    syllabus_rows.extend((
        (["成績評量"], ["期末專題"]),
        (["指定教科書及參考書籍"], ["參考書籍內容"]),
        (["核心能力指標"], ["不應匯入"]),
        (["外語教學相關欄位"], ["不應匯入"]),
        (["SDGs"], ["不應匯入"]),
        (["其他欄位 1"], ["不應匯入"]),
        (["其他欄位 2"], ["不應匯入"]),
        (["其他欄位 3"], ["不應匯入"]),
    ))
    assert len(syllabus_rows) == 29

    tables = []
    for rows in (info_rows, syllabus_rows):
        row_xml = "".join(
            "<w:tr>" + "".join(
                _cell(value if isinstance(value, list) else [value])
                for value in row
            ) + "</w:tr>"
            for row in rows
        )
        tables.append(f"<w:tbl>{row_xml}</w:tbl>")
    document = (
        f'<w:document xmlns:w="{WORD_NS}"><w:body>'
        f'{"".join(tables)}</w:body></w:document>'
    )
    content_types = (
        f'<Types><Override ContentType="{DOCX_CONTENT_TYPE}"/></Types>'
    )
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as package:
        package.writestr("[Content_Types].xml", content_types)
        package.writestr("word/document.xml", document)
    return SimpleUploadedFile(
        "converted.docx", output.getvalue(),
        content_type=(
            "application/vnd.openxmlformats-officedocument."
            "wordprocessingml.document"
        ),
    )


class DocxSyllabusParsingTests(SimpleTestCase):
    def test_week_label_variants_map_to_their_week_numbers(self):
        labels = {
            "第一週": "week_01",
            "第 一 週": "week_01",
            "第⼀週": "week_01",
            "第十週": "week_10",
            "第 ⼗ 週": "week_10",
            "第十一週": "week_11",
            "第⼗⼀週": "week_11",
            "第十八週": "week_18",
            "第⼗⼋週": "week_18",
            "第1週": "week_01",
            "第 01 週": "week_01",
            "Week 01": "week_01",
        }
        for label, expected_field in labels.items():
            with self.subTest(label=label):
                self.assertEqual(_label_value(label), (expected_field, ""))

    def test_pdf_converted_unicode_table_parses_fields_and_all_weeks(self):
        parsed = parse_docx_syllabus(make_pdf_converted_docx())

        self.assertEqual(
            parsed["teaching_target"],
            "學生能透過Django框架自行建置一應用型網站……",
        )
        self.assertEqual(parsed["teaching_matter"], "網站開發與實作")
        self.assertEqual(
            parsed["week_01"], "Web Server環境與Http知識介紹",
        )
        self.assertEqual(
            parsed["week_02"],
            "網站開發環境建置(python、Django、VS code)",
        )
        self.assertEqual(
            parsed["week_03"], "Django網站快速入門\ngithub建置",
        )
        self.assertEqual(parsed["week_08"], "第 8 週內容")
        self.assertEqual(
            parsed["week_10"],
            "實務網站-借還書系統實作-Django Admin",
        )
        self.assertEqual(parsed["week_11"], "第 11 週內容")
        self.assertEqual(parsed["week_18"], "期末系統與上線demo")
        self.assertEqual(
            parsed["assessment"], "期末專題",
        )
        self.assertEqual(parsed["references"], "參考書籍內容")
        self.assertEqual(
            {key for key in parsed if key.startswith("week_")},
            {f"week_{week:02d}" for week in range(1, 19)},
        )
        expected_week_contents = {
            week: f"第 {week} 週內容" for week in range(1, 19)
        }
        expected_week_contents.update({
            1: "Web Server環境與Http知識介紹",
            2: "網站開發環境建置(python、Django、VS code)",
            3: "Django網站快速入門\ngithub建置",
            10: "實務網站-借還書系統實作-Django Admin",
            18: "期末系統與上線demo",
        })
        for week, expected in expected_week_contents.items():
            with self.subTest(week=week):
                self.assertEqual(parsed[f"week_{week:02d}"], expected)
        self.assertFalse(any("不應匯入" in value for value in parsed.values()))
