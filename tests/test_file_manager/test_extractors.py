"""extractors — 按扩展名分发的内容提取，每种格式一个真实小文件。

extract_for 的铁律是"提取失败永不阻塞上传"：坏文件必须得到空结果，
而不是异常。
"""

from email.message import EmailMessage

import docx
import fitz  # pymupdf
import openpyxl
import pytest

from file_manager.extractors import ExtractionResult, extract_for


def _write_pdf(path, text: str = "Machine text") -> None:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((40, 60), text, fontsize=12)
    doc.save(path)
    doc.close()


def test_text_and_unknown_extensions(tmp_path):
    txt = tmp_path / "note.txt"
    txt.write_text("hello 世界", encoding="utf-8")
    assert extract_for(txt).text == "hello 世界"

    assert extract_for(tmp_path / "empty.md") == ExtractionResult()  # 空文件
    assert extract_for(tmp_path / "x.exe") == ExtractionResult()  # 未注册


def test_uppercase_extension_routes_to_pdf(tmp_path):
    pdf = tmp_path / "DOC.PDF"
    _write_pdf(pdf)
    result = extract_for(pdf)
    assert "Machine text" in (result.text or "")


def test_pdf_title_metadata_and_body(tmp_path):
    pdf = tmp_path / "t.pdf"
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((40, 60), "body line", fontsize=12)
    doc.set_metadata({"title": "titled"})
    doc.save(pdf)
    doc.close()

    result = extract_for(pdf)
    assert result.title == "titled"
    assert "body line" in (result.text or "")


def test_broken_pdf_yields_empty_result_not_crash(tmp_path):
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"%PDF but not really")
    assert extract_for(bad) == ExtractionResult()


def test_docx_title_and_paragraphs(tmp_path):
    path = tmp_path / "doc.docx"
    document = docx.Document()
    document.add_paragraph("第一段")
    document.add_paragraph("第二段")
    document.core_properties.title = "季度小结"
    document.save(path)

    result = extract_for(path)
    assert result.title == "季度小结"
    assert result.text == "第一段\n第二段"


def test_xlsx_joins_cells_and_lists_sheets(tmp_path):
    path = tmp_path / "book.xlsx"
    wb = openpyxl.Workbook()
    wb.active.append(["姓名", 3])
    wb.create_sheet("备注").append(["hi"])
    wb.save(path)

    result = extract_for(path)
    assert "姓名\t3" in (result.text or "")
    assert result.extra["sheets"] == "Sheet,备注"


def _eml(tmp_path, msg: EmailMessage):
    path = tmp_path / "m.eml"
    path.write_bytes(msg.as_bytes())
    return path


def test_eml_single_part_plain(tmp_path):
    msg = EmailMessage()
    msg["From"] = "a@example.com"
    msg["To"] = "b@example.com"
    msg["Subject"] = "周会纪要"
    msg["Date"] = "Mon, 21 Sep 2026 10:00:00 +0800"
    msg.set_content("正文在此")

    result = extract_for(_eml(tmp_path, msg))
    assert result.title == "周会纪要"
    assert "正文在此" in (result.text or "")
    assert result.extra["email_from"] == "a@example.com"
    assert result.extra["email_date"].startswith("2026-09-21")


def test_eml_prefers_plain_over_html(tmp_path):
    msg = EmailMessage()
    msg["Subject"] = "mix"
    msg.set_content("plain 版")
    msg.add_alternative("<p>html 版</p>", subtype="html")

    result = extract_for(_eml(tmp_path, msg))
    assert "plain 版" in (result.text or "")
    assert "html 版" not in (result.text or "")


def test_eml_falls_back_to_html_and_bad_date_yields_empty(tmp_path):
    msg = EmailMessage()
    msg["Date"] = "not-a-date"
    msg.set_content("<p>只有 html</p>", subtype="html")

    result = extract_for(_eml(tmp_path, msg))
    assert "只有 html" in (result.text or "")
    # policy.default decodes a malformed Date to empty before the extractor sees it
    assert result.extra["email_date"] == ""


@pytest.mark.parametrize("suffix", ["txt", "md", "csv", "json", "log"])
def test_text_extensions_all_registered(tmp_path, suffix):
    f = tmp_path / f"f.{suffix}"
    f.write_text("x", encoding="utf-8")
    assert extract_for(f).text == "x"
