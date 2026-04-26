import logging
import re

from .models import Chunk, Document

logger = logging.getLogger(__name__)

PAGE_MARKER_RE = re.compile(r"\[Page (\d+)\]")
CHARS_PER_TOKEN = 4


def _get_tokenizer(model_name: str):
    try:
        import tiktoken

        try:
            return tiktoken.encoding_for_model(model_name)
        except KeyError:
            return tiktoken.get_encoding("cl100k_base")
    except Exception:
        logger.debug("tiktoken unavailable, using character-based token count")
        return None


def _count_tokens(text: str, tokenizer) -> int:
    if tokenizer is not None:
        return len(tokenizer.encode(text))
    return max(1, len(text) // CHARS_PER_TOKEN)


def _get_last_n_chars(text: str, n: int) -> str:
    if len(text) <= n:
        return ""
    return text[-n:]


def _detect_page_range(text: str) -> tuple[int, int]:
    matches = PAGE_MARKER_RE.findall(text)
    if not matches:
        return (1, 1)
    pages = [int(m) for m in matches]
    return (min(pages), max(pages))


def _recursive_split(
    text: str,
    separators: list[str],
    chunk_size: int,
    tokenizer,
) -> list[str]:
    if _count_tokens(text, tokenizer) <= chunk_size:
        return [text] if text.strip() else []

    if not separators:
        return [text] if text.strip() else []

    sep = separators[0]
    remaining = separators[1:]

    parts = []
    current = ""
    for part in text.split(sep):
        candidate = current + (sep if current else "") + part
        if _count_tokens(candidate, tokenizer) > chunk_size and current:
            parts.append(current)
            current = part
        else:
            current = candidate

    if current:
        parts.append(current)

    result = []
    for part in parts:
        if _count_tokens(part, tokenizer) > chunk_size and remaining:
            result.extend(_recursive_split(part, remaining, chunk_size, tokenizer))
        elif part.strip():
            result.append(part)

    return result


def chunk_document(
    doc: Document,
    chunk_size: int,
    chunk_overlap: int,
    model_name: str = "gpt-4o",
) -> list[Chunk]:
    if doc.total_pages == 0:
        raise ValueError("Document contains no extractable text")

    text_parts = []
    for page in doc.pages:
        text_parts.append(f"\n[Page {page.page_number}]\n{page.text}")

    full_text = "\n".join(text_parts)

    if not full_text.strip():
        raise ValueError("Document contains no extractable text")

    tokenizer = _get_tokenizer(model_name)
    overlap_chars = chunk_overlap * CHARS_PER_TOKEN

    separators = ["\n\n", "\n", ". ", " "]
    chunks = _recursive_split(full_text, separators, chunk_size, tokenizer)

    result: list[Chunk] = []
    for i, chunk in enumerate(chunks):
        current_text = chunk

        if i > 0 and chunk_overlap > 0:
            overlap_text = _get_last_n_chars(result[-1].text, overlap_chars)
            if overlap_text:
                current_text = overlap_text + "\n\n" + current_text

        start_page, end_page = _detect_page_range(current_text)
        token_count = _count_tokens(current_text, tokenizer)

        result.append(
            Chunk(
                text=current_text.strip(),
                token_count=token_count,
                start_page=start_page,
                end_page=end_page,
            )
        )

    return result
