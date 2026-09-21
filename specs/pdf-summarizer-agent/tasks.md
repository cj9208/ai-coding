# Tasks — PDF Summarizer Agent (v3 · Simplified)

---

## Task 1 — Scaffolding & dependencies
**Priority**: high | **Size**: S

- Add to `pyproject.toml`: `pymupdf`, `paddleocr`, `paddlepaddle`, `pdf2image`, `openai`, `tiktoken`, `tenacity`, `python-dotenv`.
- `uv sync`.
- Create file tree with placeholder `__init__.py` files:
  ```
  src/pdf_summarizer/
  ├── __init__.py
  ├── models.py
  ├── config.py
  ├── extractor/__init__.py, base.py, pymupdf.py, paddle_ocr.py
  ├── chunker.py
  ├── llm_client.py
  ├── summarizer.py
  └── cli.py
  ```

**Accept**: `uv run python -c "import pymupdf; import openai; from pdf_summarizer import summarize"` succeeds.

---

## Task 2 — Shared models (`models.py`)
**Priority**: high | **Size**: S

- `Page`: `page_number: int`, `text: str`.
- `Document`: `pages: list[Page]`, `filename: str`. Property `total_pages: int`.
- `Chunk`: `text: str`, `token_count: int`, `start_page: int`, `end_page: int`.
- `SummaryStyle`: `CONCISE`, `DETAILED`, `BULLETS`, `EXECUTIVE`.

**Accept**: Instantiate all dataclasses; `Document.total_pages` returns `len(pages)`.

---

## Task 3 — Centralized Config (`config.py`)
**Priority**: high | **Size**: S

Single `Config` dataclass with **all defaults visible in one place**:

```python
@dataclass
class Config:
    # Extractor
    extractor_backend: str = "auto"          # "pymupdf" | "paddleocr" | "auto"

    # LLM
    api_key: str = ""                         # env: LLM_API_KEY
    base_url: str = "https://api.deepseek.com/v1"  # env: LLM_BASE_URL
    model: str = "deepseek-chat"              # env: LLM_MODEL
    temperature: float = 0.3

    # Chunking
    chunk_size: int = 3000                    # tokens
    chunk_overlap: int = 200

    # Summarization
    summary_style: SummaryStyle = SummaryStyle.CONCISE
    max_concurrency: int = 4

    # Resilience
    timeout: int = 120                        # seconds
    max_retries: int = 3
```

`load_config(**overrides) -> Config`:
1. Start with `Config()` (all defaults).
2. Load `.env` + env vars → apply matching fields (`LLM_API_KEY` → `api_key`, etc.).
3. Apply `**overrides` (from argparse) → strongest precedence.

**Accept**: `load_config()` returns all defaults. `load_config(api_key="sk-xxx")` overrides just that field. `LLM_API_KEY=envkey` populates `api_key`.

---

## Task 4 — Extractor base + PyMuPDF backend
**Priority**: high | **Size**: M

**`extractor/base.py`**:
- `ExtractorBackend(ABC)` with `async def extract(self, filepath: str) -> Document`.
- `auto_extract(filepath: str, backends: list[ExtractorBackend]) -> Document` — tries backends in order, stops when total text > 50 chars.

**`extractor/pymupdf.py`**:
- `PyMuPDFBackend(ExtractorBackend)`:
  - Sync core logic `_extract_sync(filepath) -> Document`.
  - `extract()` wraps it via `await asyncio.to_thread(self._extract_sync, filepath)`.
  - Open with `fitz.open()`, iterate pages, `page.get_text("text")`.
  - Unicode NFKC normalize + whitespace collapse.
  - Catch `fitz.FileDataError`, `RuntimeError` (password) → `ValueError`.

**`extractor/__init__.py`**:
- `create_extractor(name: str) -> ExtractorBackend`:
  - `"pymupdf"` → `PyMuPDFBackend()`
  - `"auto"` → wrapper that calls `auto_extract` with `[PyMuPDFBackend(), PaddleOCRBackend()]`

**Accept**: `be = create_extractor("pymupdf"); doc = await be.extract("test.pdf")` returns `Document` with non-empty pages.

---

## Task 5 — PaddleOCR extractor backend
**Priority**: medium | **Size**: M

**`extractor/paddle_ocr.py`**:
- `PaddleOCRBackend(ExtractorBackend)`:
  - `_init_ocr()` — lazy singleton via `PaddleOCR(use_angle_cls=True, lang="en")`.
  - Convert PDF pages to images via `pdf2image.convert_from_path()`.
  - OCR each image → `ocr.ocr(img, cls=True)` → join recognized text.
  - Same `asyncio.to_thread` async wrapping.
  - Handle `PaddleOCR` import optional; if missing, raise clear "install paddleocr" error.

**Accept**: Scanned PDF → `PaddleOCRBackend.extract()` returns `Document` with recognized text. Auto fallback works: pymupdf empty → paddleocr kicks in.

---

## Task 6 — Text chunker (`chunker.py`)
**Priority**: high | **Size**: M

- `chunk_document(doc: Document, chunk_size: int, chunk_overlap: int, model_name: str = "gpt-4o") -> list[Chunk]`:
  1. Flatten doc into text with `[Page N]` markers between pages.
  2. Token count via `tiktoken` (try model encoding, fallback `cl100k_base`).
  3. Recursive split on separators: `["\n\n", "\n", ". ", " "]`.
  4. Merge chunks with overlap.
  5. Track page ranges by matching `[Page N]` markers.
- Short doc (< chunk_size) → single chunk.
- Empty doc → `ValueError`.

**Accept**: 10-page PDF → chunks under `chunk_size` tokens, correct page ranges.

---

## Task 7 — LLM client (`llm_client.py`)
**Priority**: high | **Size**: S

- `LLMClient` class:
  ```python
  class LLMClient:
      def __init__(self, config: Config):
          self._client = openai.AsyncOpenAI(
              api_key=config.api_key,
              base_url=config.base_url,
              timeout=config.timeout,
          )
          self._model = config.model
          self._temperature = config.temperature

      async def chat(self, prompt: str, system_prompt: str = "") -> str:
          ...
  ```
- `chat()` calls `self._client.chat.completions.create()` with retry via `tenacity`:
  - 3 attempts, wait 1s/2s/4s.
  - Retry on: `RateLimitError`, `APITimeoutError`, `APIConnectionError`, `InternalServerError`.
- Returns `choice.message.content`.

**Accept**: Against DeepSeek API → `await client.chat("Hi")` returns string response. Retry works on transient failures.

---

## Task 8 — Async summarizer pipeline (`summarizer.py`)
**Priority**: high | **Size**: L

`async def summarize(pdf_path: str, config: Config) -> str`:

1. **Extract**: `extractor = create_extractor(config.extractor_backend); doc = await extractor.extract(pdf_path)`
2. **Chunk**: `chunks = chunk_document(doc, config.chunk_size, config.chunk_overlap)`
3. **Map** (concurrent):
   ```python
   sem = asyncio.Semaphore(config.max_concurrency)
   async def summarize_one(chunk: Chunk) -> str:
       async with sem:
           return await llm.chat(chunk.text, system_prompt=MAP_PROMPT)
   summaries = await asyncio.gather(*[summarize_one(c) for c in chunks])
   ```
4. **Reduce** (iterative pairwise merge):
   ```python
   while len(summaries) > 1:
       pairs = [summaries[i] + "\n---\n" + summaries[i+1] for i in range(0, len(summaries), 2)]
       summaries = await asyncio.gather(*[llm.chat(p, system_prompt=REDUCE_PROMPT) for p in pairs])
   return summaries[0]
   ```
5. Style prompts injected from `config.summary_style`.
6. Verbose logging via `logging` module.

**Accept**: `await summarize("doc.pdf", config)` returns coherent multi-paragraph summary.

---

## Task 9 — CLI entry point (`cli.py`)
**Priority**: medium | **Size**: M

- `argparse` with arguments mapping 1:1 to `Config` fields:
  ```
  pdf_path          (positional)
  -o, --output      output file path
  --backend         extractor backend (auto|pymupdf|paddleocr)
  --model           LLM model name
  --api-key         API key override
  --base-url        API base URL override
  -s, --style       concise|detailed|bullets|executive
  --chunk-size      tokens per chunk
  --chunk-overlap   overlap tokens
  --concurrent      max parallel workers
  -v, --verbose     progress logging
  ```
- `main()` → `asyncio.run(async_main(args))`.
- `async_main`: parse args → `load_config(**args_dict)` → `summarize()` → write to file or stdout.
- `pyproject.toml` entry: `pdf-summarize = "pdf_summarizer.cli:main"`.

**Accept**: `uv run pdf-summarize doc.pdf -o summary.md -v` works end-to-end.

---

## Task 10 — Tests
**Priority**: medium | **Size**: S

- `tests/conftest.py`: fixtures for in-memory sample PDF, `Config` with short timeouts.
- `tests/test_extractor.py`: test pymupdf backend with sample PDF.
- `tests/test_chunker.py`: test chunk sizes, page ranges, short-doc edge case.
- `tests/test_config.py`: test default resolution, env var overrides, kwarg overrides.
- `tests/test_summarizer.py`: mock `LLMClient.chat` → test map-reduce flow returns non-empty string. Skip if no `LLM_API_KEY`.
- `tests/test_cli.py`: `--help` smoke test.

**Accept**: `uv run pytest tests/` passes.

---

## Task 11 — Error handling
**Priority**: low | **Size**: S

- No text extracted → `ValueError("No extractable text found in PDF")`.
- Missing API key → `RuntimeError("LLM_API_KEY not set. Export it or pass --api-key.")`.
- Corrupted PDF → wrapped as `ValueError`.
- File not found → argparse default.
- Rate limit → retried automatically by LLMClient.
- All user-facing errors: clear message, no raw traceback.

**Accept**: Each failure mode produces a friendly message.

---

## Dependency graph

```
Task 1 ──▶ Task 2 ──▶ Task 3 (config)
                        ├──▶ Task 4 (extractor base + pymupdf)
                        │     └──▶ Task 5 (paddleocr)
                        ├──▶ Task 6 (chunker)
                        ├──▶ Task 7 (LLM client)
                        └──────▶ Task 8 (summarizer) ──▶ Task 9 (CLI)
                                                           └──▶ Task 10 (tests)
                                                                  └──▶ Task 11 (errors)
```
