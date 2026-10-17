# ai-coding

A personal playground that records small everyday tools, uses **uv** to manage
the environment, and investigates AI coding tools along the way.

There is no single app. Each subproject lives in its own `src/<name>/` package
and stands on its own, except where it builds on a shared layer below.

## What's inside

**Shared layers** — the plumbing the tools build on:

- `llm_client` — the one place LLM calls go through (retries, JSON, rate limits).
- `storage` — shared SQLite + full-text-search helpers.
- `task_queue` — async task scheduling.
- `notify` — structured event ledger + delivery.
- `ocr_backend` — one OCR contract over pluggable engines.
- `utils` — small shared helpers (paths, timers).

**Tools & experiments** — each independent, each with its own CLI:

- `pdf_summarizer` — PDF → summary via map-reduce.
- `ai_market_radar` — scans AI-vendor news into a digest.
- `file_manager` — FastAPI file browser with metadata search.
- `research_agent` — research & recommendation agent.
- `rag` — lexical knowledge pipeline over OCR output.
- `orchestrator` — deterministic request-orchestration runtime.
- `skill_manager` — vendors and installs external AI skills.
- `quantdesk` — crypto market data + factor screening.
- `photo_desk` — family photo timeline & triage over a NAS folder.
- `exam_builder` — composes exam papers from a Word corpus.
- `ocr_review` — human-proofreading UI for OCR output.

**Exercises** — `coding/` and `modules/` hold standalone algorithm drills and
one-off scripts.

Design docs live in `docs/` (each explains *why* a choice was made); specs
authored by AI tools are archived under `specs/`. For the detailed,
agent-facing conventions, see `AGENTS.md`.

## Setup

Python >= 3.12, managed by uv.

```sh
uv sync --locked      # install the recorded environment
uv run pytest         # run tests
```

### Dependency management

`pyproject.toml` declares the project's direct dependencies and version policy.
`uv.lock` is the committed resolution snapshot for all transitive dependencies.
Set up a matching environment with `uv sync --locked`; use `uv add` or `uv remove`
to change dependencies, then commit the refreshed lock file.
