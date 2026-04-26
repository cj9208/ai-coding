# PDF Summarizer Agent — Proposal (v3 · Simplified)

## Overview
A CLI agent that loads a PDF, extracts text, and produces a summary via LLM (DeepSeek by default). OpenAI-compatible API means one client, zero routing overhead. Extensible extraction via Strategy pattern. All defaults centralized in one place.

## Architecture

```
┌──────────┐    ┌─────────────────┐    ┌───────────┐    ┌──────────────┐    ┌────────────┐
│   CLI    │───▶│   Extractor     │───▶│  Chunker  │───▶│  LLM Client  │───▶│ Summarizer │
│ argparse │    │ (Strategy)      │    │ (tiktoken)│    │ (openai SDK) │    │ (map-red)  │
└──────────┘    │  · PyMuPDF      │    └───────────┘    │ base_url→DS  │    └────────────┘
                │  · PaddleOCR    │                     └──────────────┘
                └─────────────────┘
```

## Key Design Decisions

### 1. Single LLM client — no router
DeepSeek is an OpenAI-compatible provider. The `openai` SDK supports arbitrary `base_url`:

```python
client = openai.AsyncOpenAI(
    api_key="sk-...",
    base_url="https://api.deepseek.com/v1",  # ← only this changes per provider
)
```

One `LLMClient` class. Changing provider is a config change, not a code change.
Default: DeepSeek (`deepseek-chat`). To switch: `--base-url` + `--api-key` + `--model`.

### 2. Two extraction backends — Strategy pattern
Abstract base class (`ExtractorBackend`) with two implementations:
- `PyMuPDFBackend` — fast native text extraction (default)
- `PaddleOCRBackend` — OCR for scanned/image PDFs

Extensible: to add a third backend, subclass `ExtractorBackend` and register it.
`AutoExtractor` tries pymupdf → paddleocr. Sync libs wrapped via `asyncio.to_thread`.

```python
class ExtractorBackend(ABC):
    @abstractmethod
    async def extract(self, filepath: str) -> Document: ...

def create_extractor(name: str = "auto") -> ExtractorBackend: ...
```

### 3. All defaults in one place — `Config`
Single `Config` dataclass. Every tunable has a default defined right there. Override chain:

```
Config defaults  →  env vars  →  argparse overrides
   (weakest)          │             (strongest)
```

No hidden defaults in individual modules. You can read `Config` and see every knob.

## Detailed Techniques

### PDF Text Extraction
- `PyMuPDFBackend`: `fitz.open()` → iterate pages → `page.get_text("text")` → normalize.
- `PaddleOCRBackend`: `pdf2image` to render pages → `PaddleOCR.ocr()` on each image → join text.
- Lazy init for PaddleOCR (model loading is heavy, ~5s on first use).
- Async wrapper: `await asyncio.to_thread(self._extract_sync, filepath)`.

### Text Chunking
- `tiktoken` for token counting with fallback to `cl100k_base`.
- Recursive split: `\n\n` → `\n` → `. ` → ` `.
- Default: 3000 tokens/chunk, 200 token overlap.
- Page range tracking via `[Page N]` markers injected during flattening.

### LLM Client
- Wraps `openai.AsyncOpenAI`, configured with `base_url` and `api_key`.
- Single method: `async def chat(prompt: str, system_prompt: str, **kwargs) -> str`.
- Retry: 3 attempts, exponential backoff (1s/2s/4s), via `tenacity.retry`.
- Timeout: 120s.
- Any OpenAI-compatible provider works by changing `base_url`.

### Summarization (async)
- **Map**: `asyncio.gather()` with `Semaphore(max_concurrency)`. Each chunk summarized independently.
- **Reduce**: Merge summaries pairwise until one remains.
- Style prompts injected based on `SummaryStyle` enum.

### CLI
- `argparse`. All options map 1:1 to `Config` fields.
- `asyncio.run(main())`.
- `-v/--verbose` for progress logging.

## Dependencies
| Package         | Purpose                |
|------------------|------------------------|
| `pymupdf`        | PDF extraction         |
| `paddleocr`      | OCR extraction         |
| `pdf2image`      | PDF → image for OCR    |
| `paddlepaddle`   | PaddleOCR runtime      |
| `openai`         | LLM API client         |
| `tiktoken`       | Token counting         |
| `tenacity`       | Retry logic            |
| `python-dotenv`  | .env loading           |

## File layout
```
src/pdf_summarizer/
├── __init__.py           # summarize()
├── models.py             # Document, Page, Chunk, SummaryStyle
├── config.py             # Config dataclass + load_config()
├── extractor/
│   ├── __init__.py       # create_extractor(), AutoExtractor
│   ├── base.py           # ExtractorBackend ABC
│   ├── pymupdf.py        # PyMuPDFBackend
│   └── paddle_ocr.py     # PaddleOCRBackend
├── chunker.py            # chunk_document()
├── llm_client.py         # LLMClient (openai SDK)
├── summarizer.py         # summarize()
└── cli.py                # argparse + asyncio.run()
```
