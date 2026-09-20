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

- Python >= 3.12, managed by **uv** (`uv.lock`, venv at `.venv/`).
- The project is installed **editable with explicit top-level packages**
  (`[tool.hatch.build.targets.wheel] packages` in pyproject.toml). Imports are
  top-level (`from pdf_summarizer.config import ...`, never `src.pdf_summarizer`).
- **Gotcha:** after creating a new top-level package under `src/`, add it to
  the `packages` list and run `uv pip install -e .` — otherwise imports fail
  with a confusing `ModuleNotFoundError`.
- Run tests: `uv run pytest`. Async tests work via `asyncio_mode = "auto"`
  (no markers needed).
- CLI entry points: `pdf-summarize`, `ai-market-radar`, `file-manager`
  (`[project.scripts]`).
- `test_summarize_live` is the only test hitting a real LLM API (skipped when
  `LLM_API_KEY` unset). A 401 there means the key in `.env` is stale — an
  environment issue, not a code regression.

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
| `src/pdf_summarizer/` | CLI: PDF → chunks → map-reduce summary | yes, via `llm_client` |
| `src/ai_market_radar/` | scans OpenAI/Anthropic/Copilot **news sources** into SQLite, digest of new items. "OpenAI" here is a watched entity, not a dependency. Deterministic parsing on purpose — no LLM | no |
| `src/file_manager/` | FastAPI file manager, metadata-first search (FTS5) | no |
| `src/research_agent/` | research & recommendation agent — **in progress**; design docs in `docs/research-recommendation-agent/` (read `00-overview.md` first), scaffold follows its module layout (orchestrator/research/clarifying/recommendation/contracts/storage) | yes, via `llm_client` |
| `src/coding/`, `src/modules/` | standalone algorithm exercises and small one-off scripts (e.g. `analyze_birth.py`, `analyze_package_size.py` — root-level scripts were moved into `modules/` to keep `src/` clean) | no |
| `opencode/` | design proposals produced by AI coding tools (documentation) | — |

## Storage conventions

- Each project owns a SQLite file under `data/<project>/` (e.g.
  `data/ai_market_radar/kb.db`; `file_manager` respects `FM_DATABASE_URL`).
- **FTS5 + CJK quirk (verified on this machine):** the default `unicode61`
  tokenizer drops CJK tokens; `trigram` and `editdist3` are unavailable.
  Reuse the `fold_cjk` approach from `src/file_manager/fts.py` for Chinese
  full-text search.

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
