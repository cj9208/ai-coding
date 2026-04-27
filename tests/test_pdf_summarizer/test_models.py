from pdf_summarizer.models import Document, Page


def test_document_properties():
    pages = [Page(page_number=1, text="Hello"), Page(page_number=2, text="World")]
    doc = Document(pages=pages, filename="test.pdf")
    assert doc.total_pages == 2
    assert doc.total_text == "Hello\n\nWorld"
