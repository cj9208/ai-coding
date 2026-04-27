import os
import tempfile

import fitz
import pytest

from pdf_summarizer.config import Config


@pytest.fixture
def sample_text():
    return (
        "This is a test document. It contains multiple sentences. "
        "The purpose is to test the PDF summarizer agent. "
        "We want to make sure text extraction, chunking, and summarization work correctly. "
        "This document has several paragraphs.\n\n"
        "Second paragraph. Here we discuss the architecture of the system. "
        "The system uses a map-reduce approach for summarization. "
        "This is an efficient strategy for long documents. "
        "It processes chunks in parallel and then merges results.\n\n"
        "Third paragraph. The extractor supports PyMuPDF as the primary backend. "
        "It can also use PaddleOCR for scanned documents. "
        "The LLM client uses the OpenAI-compatible API."
    )


@pytest.fixture
def sample_pdf_path(sample_text):
    fd, path = tempfile.mkstemp(suffix=".pdf")
    os.close(fd)
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((50, 50), sample_text, fontsize=11)
    doc.save(path)
    doc.close()
    yield path
    os.unlink(path)


@pytest.fixture
def multi_page_pdf_path():
    fd, path = tempfile.mkstemp(suffix=".pdf")
    os.close(fd)
    doc = fitz.open()
    for i in range(5):
        page = doc.new_page()
        page.insert_text((50, 50), f"Page {i + 1} content. " * 50, fontsize=11)
    doc.save(path)
    doc.close()
    yield path
    os.unlink(path)


@pytest.fixture
def empty_pdf_path():
    fd, path = tempfile.mkstemp(suffix=".pdf")
    os.close(fd)
    doc = fitz.open()
    doc.new_page()
    doc.save(path)
    doc.close()
    yield path
    os.unlink(path)


@pytest.fixture
def config():
    return Config(
        api_key="test-key",
        chunk_size=500,
        chunk_overlap=50,
        timeout=10,
        max_retries=1,
    )
