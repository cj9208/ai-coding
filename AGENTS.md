# AGENTS.md

Orientation for AI agents (and humans) working in this repo. Keep this file
updated when conventions change — it exists so the next session does not
re-derive it.

## What this repo is

A multi-project playground (`records small functions in daily life, uses uv,
investigates AI coding tools` — see readme.md). **There is no single app**;
each subproject lives in its own `src/<name>/` package and is independent of
the others, except where noted below.

## Environment & commands

- Python >= 3.12, managed by **uv** (`pyproject.toml` declares dependencies;
  the committed `uv.lock` records their exact resolution; venv at `.venv/`).
- Change dependencies with `uv add` / `uv remove`, then commit the refreshed
  `uv.lock`. On a clean machine, use `uv sync --locked` to install the recorded
  environment without re-resolving versions.
- OCR is optional and split into independently installable extras: the
  frontend stack `ocr`, plus an engine — `paddle-cpu` (PyPI) or `paddle-gpu`
  (resolved from Paddle's official cu126 index declared in pyproject.toml;
  PyPI's `paddlepaddle-gpu` is stuck at 2.6.2, incompatible with paddlex 3.x).
  Use `uv sync --extra ocr --extra paddle-cpu` or
  `uv sync --extra ocr --extra paddle-gpu`. Paddle requires x86_64
  (reported as `AMD64` on Windows); GPU is limited to Windows and Linux.
- The project is installed **editable with explicit top-level packages**
  (`[tool.hatch.build.targets.wheel] packages` in pyproject.toml). Imports are
  top-level (`from pdf_summarizer.config import ...`, never `src.pdf_summarizer`).
- **Gotcha:** after creating a new top-level package under `src/`, add it to
  the `packages` list and run `uv pip install -e .` — otherwise imports fail
  with a confusing `ModuleNotFoundError`.
- Run tests: `uv run pytest`. Async tests work via `asyncio_mode = "auto"`
  (no markers needed).
- CLI entry points: `pdf-summarize`, `ai-market-radar`, `file-manager`,
  `research-agent` (`[project.scripts]`).
- `test_summarize_live` is the only test hitting a real LLM API (skipped when
  `LLM_API_KEY` unset). A 401 there means the key in `.env` is stale — an
  environment issue, not a code regression.
- `tests/test_ocr_backend/test_paddleocr_vl_live.py` runs the real OCR model
  (skipped unless `OCR_LIVE=1` and the local snapshot `paddleocr-vl-1.6/` is
  present); the rest of `test_ocr_backend` is model-free golden-fixture work.

## LLM access — one rule

**All LLM calls go through the shared `llm_client` package.** Never
instantiate `AsyncOpenAI` (or any provider SDK) inside a subproject; convert
the project's config into `llm_client.LLMSettings` and use
`LLMClient.chat()/chat_json()` (retries, timeouts, strict-JSON-with-one-repair
are already there). Env convention lives only in
`src/llm_client/settings.py`: `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL` /
`LLM_TEMPERATURE` / `LLM_TIMEOUT` / `LLM_MAX_RETRIES`, read from repo-root
`.env` (DeepSeek-compatible defaults).

## Subproject map

| Path | What it is | LLM? |
|---|---|---|
| `src/llm_client/` | shared LLM access point (see rule above) | — it *is* the LLM layer |
| `src/storage/` | shared storage layer, one module per DB type in use (currently SQLite: `sqlite.py` engine/PRAGMA/session/ensure_columns/sha256_hex, `fts.py` fold_cjk/match_expr/FtsTable). Only generic access knowledge belongs here — table definitions and business stores stay in each project; see `docs/storage-usage-guide.md` | no |
| `src/ocr_backend/` | shared OCR layer: one versioned `OcrDocument` contract (page → block, pixel bbox) + render projections (`page_text` / `document_markdown`); PaddleOCR-VL 1.6 is the first adapter (engine comes from the `paddle-cpu` / `paddle-gpu` extras — see `docs/ocr-backend-design.md` §7 for the GPU index gotcha). Consumers read the contract, never a backend's native output | no |
| `src/pdf_summarizer/` | CLI: PDF → chunks → map-reduce summary | yes, via `llm_client` |
| `src/ai_market_radar/` | scans OpenAI/Anthropic/Copilot **news sources** into SQLite, digest of new items. "OpenAI" here is a watched entity, not a dependency. Deterministic parsing on purpose — no LLM | no |
| `src/file_manager/` | FastAPI file manager, metadata-first search (FTS5) | no |
| `src/research_agent/` | research & recommendation agent — **MVP implemented & live-verified 2026-09-20** (`research-agent new/answer/status/report`); design docs in `docs/research-recommendation-agent/` (read `00-overview.md` first, `06-usage-guide.md` to run it), module layout mirrors the docs (orchestrator/research/clarifying/recommendation/contracts/persistence — the storage subpackage was renamed to dodge the top-level `storage` clash) | yes, via `llm_client` |
| `src/coding/`, `src/modules/` | standalone algorithm exercises and small one-off scripts (e.g. `analyze_birth.py`, `analyze_package_size.py` — root-level scripts were moved into `modules/` to keep `src/` clean) | no |
| `opencode/` | design proposals produced by AI coding tools (documentation) | — |

## Storage conventions

- Each project owns a SQLite file under `data/<project>/` (e.g.
  `data/ai_market_radar/kb.db`; `file_manager` respects `FM_DATABASE_URL`).
- **Default data paths are anchored to the repo root** — the expression
  lives **once** in `src/utils/paths.py` (`REPO_ROOT`, plus
  `data_dir("<project>")`); `file_manager.config` / `research_agent.config`
  / `ai_market_radar.cli` / `modules/*` import it, never cwd-relative.
  Launching elsewhere must not create a second DB or stray outputs.
  Env vars (`FM_DATA_DIR`, `RESEARCH_AGENT_DATA_DIR`) and `--data-dir` still
  override.
- `src/utils/` is the shared-helper package (currently `paths.py`,
  `timer.py`) — add generic helpers here rather than copying them into
  projects; don't prune it as dead code.
- **FTS5 + CJK quirk (verified on this machine):** the default `unicode61`
  tokenizer drops CJK tokens; `trigram` and `editdist3` are unavailable.
  The `fold_cjk` folding now lives in the shared layer — build indexes with
  `storage.FtsTable` and queries with `storage.match_expr`/`token_expr` so
  the write and query ends can't drift (`src/storage/fts.py`).

## Style & checks

- Pre-commit hooks run on commit: black (`py312`), flake8 (max-line 100),
  isort (black profile), mypy, bandit, commitizen. Format violations are
  auto-fixed by the hook and the **commit is aborted** — re-stage the
  reformatted files and commit again (new commit, never amend).
- Tests mirror the source layout: `tests/test_<project>/test_<module>.py`.
- Commit messages: short, lowercase, imperative ("add file manager with
  graded metadata search"); mention the *why* in the body when non-obvious.

## Docs

- Design/explanation docs live in `docs/`; the house style explains *why*
  each choice was made, with a big-picture ASCII diagram up front
  (see `docs/ai-market-radar-pipeline.md` as the reference example).
- Multi-part designs get a folder (`docs/research-recommendation-agent/`)
  with a numbered overview as the entry point.
