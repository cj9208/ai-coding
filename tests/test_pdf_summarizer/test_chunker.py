from pdf_summarizer.chunker import _count_tokens, _detect_page_range, chunk_document
from pdf_summarizer.models import Chunk, Document, Page


def test_count_tokens():
    text = "Hello world. This is a test."
    c = _count_tokens(text, None)
    assert c > 0
    assert isinstance(c, int)


def test_detect_page_range():
    text = "[Page 2]\n Some content \n[Page 5]\n More content"
    start, end = _detect_page_range(text)
    assert start == 2
    assert end == 5


def test_detect_page_range_single():
    text = "[Page 42]\n Only one page marker"
    start, end = _detect_page_range(text)
    assert start == 42
    assert end == 42


def test_detect_page_range_none():
    text = "No page markers here"
    start, end = _detect_page_range(text)
    assert start == 1
    assert end == 1


def test_chunk_document_single_page():
    pages = [Page(page_number=1, text="Short document. " * 20)]
    doc = Document(pages=pages, filename="test.pdf")
    chunks = chunk_document(doc, chunk_size=3000, chunk_overlap=50)
    assert len(chunks) >= 1
    assert all(isinstance(c, Chunk) for c in chunks)
    assert chunks[0].start_page == 1
    assert chunks[0].end_page == 1


def test_chunk_document_empty():
    doc = Document(pages=[], filename="empty.pdf")
    try:
        chunk_document(doc, chunk_size=100, chunk_overlap=0)
        assert False, "Should have raised"
    except ValueError as e:
        assert "no extractable text" in str(e).lower()


def test_chunk_document_multi_page():
    pages = [
        Page(page_number=1, text="First page content. " * 50),
        Page(page_number=2, text="Second page different content. " * 50),
        Page(page_number=3, text="Third page final content. " * 50),
    ]
    doc = Document(pages=pages, filename="test.pdf")
    chunks = chunk_document(doc, chunk_size=500, chunk_overlap=50)
    assert len(chunks) > 1
    for c in chunks:
        assert c.token_count > 0
        assert 1 <= c.start_page <= 3
        assert 1 <= c.end_page <= 3
        assert c.start_page <= c.end_page
