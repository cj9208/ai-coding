# AGENTS.md

Orientation for AI agents (and humans) working in this repo: the repo-wide rules
plus a map saying which document owns each area's details.

**Current state, not change history.** No dates, no milestone logs. When a
convention changes, rewrite its sentence to the new final form instead of
appending the delta. History lives in `git log`; unpaid debt in TODO.md.
Per-area detail lives in `docs/` — this file states the rule and points at its
authority rather than restating it.

## What this repo is

A multi-project playground (see readme.md). **There is no single app**: each
subproject is its own top-level package under `src/<name>/`, independent except
for the shared layers (`llm_client`, `storage`, `task_queue`, `notify`,
`ocr_backend`, `utils`).

## Environment & commands

- Python >= 3.12, managed by **uv**; the committed `uv.lock` is the resolution
  of record. Clean machine: `uv sync --locked`. Change deps with `uv add` /
  `uv remove`, then commit the refreshed lock.
- **Gotcha:** `uv add` runs an implicit sync *without extras*, so it silently
  uninstalls the OCR stack. After any dependency edit, restore with
  `uv sync --locked --extra ocr --extra paddle-gpu`. The extras split
  (`ocr` frontend + `paddle-cpu` / `paddle-gpu` engine, the cu126 index, the
  x86_64 requirement) is documented in `docs/ocr-backend-design.md` §7.
- **Gotcha:** the project is editable-installed with an explicit package list —
  after adding a top-level package under `src/`, register it in
  `[tool.hatch.build.targets.wheel] packages` and re-run `uv pip install -e .`,
  or imports fail with a confusing `ModuleNotFoundError`. Imports are top-level
  (`from pdf_summarizer.config import ...`, never `src.pdf_summarizer`).
- Every CLI is declared with **click** (no argparse, no `main(argv)` shim):
  `[project.scripts]` points at the click object — a `@click.group` (`cli`), or
  a `@click.command` for the single-command tools (pdf_summarizer,
  ai_market_radar, file_manager). Tests invoke via `click.testing.CliRunner`.
  `src/quantdesk/cli.py` is the house pattern; keep CLI tokens compatible with
  the `03-usage` volumes.
- Tests: `uv run pytest` (`asyncio_mode = "auto"`, no markers needed). Which
  tests hit a real API or a real model, and what each skip means, is the L3
  table in `docs/verification-gates.md` — a 401 in `test_summarize_live` is a
  stale key in `.env`, not a regression.
- `src/coding/`, `src/modules/` are loose scripts; `seating_app.py` runs with
  `streamlit run src/modules/seating_app.py`.

## LLM access — one rule

**All LLM calls go through the shared `llm_client` package.** Never instantiate
`AsyncOpenAI` (or any provider SDK) inside a subproject; convert the project's
config into `llm_client.LLMSettings` and use `LLMClient.chat()/chat_json()`
(retries, timeouts, strict-JSON-with-one-repair are already there). Env
convention lives only in `src/llm_client/settings.py`: `LLM_API_KEY` /
`LLM_BASE_URL` / `LLM_MODEL` / `LLM_TEMPERATURE` / `LLM_TIMEOUT` /
`LLM_MAX_RETRIES`, read from repo-root `.env` (DeepSeek-compatible defaults).

## Subproject map

Each row: what it is, the document that owns its details, and the rulings an
agent would otherwise violate. Implementation state and evidence numbers live in
those docs and in TODO.md, not here.

| Path | What it is | LLM? |
|---|---|---|
| `src/llm_client/` | the shared LLM access point — see the rule above | it *is* the LLM layer |
| `src/storage/` | shared storage layer, one module per DB type (SQLite today: `sqlite.py`, `fts.py`). Only generic access knowledge belongs here — table definitions and business stores stay in each project. `docs/storage-usage-guide.md` (schema-evolution graduation rule in §4) | no |
| `src/task_queue/` | shared async task scheduling: lane-isolated `TaskQueue`, retry with backoff, `async with` lifecycle. `docs/queue-service-design.md` is the authority (§12 A-1..A-3). **Construct `TaskQueue` directly** — do not re-add a process-wide default queue or a `@queued` decorator. `rag/enrich.py` is the only production consumer; do **not** migrate pdf_summarizer (its local `Semaphore` is right for a one-shot map-reduce); research_agent's collector stays serial until a real demand signal appears (§12.4 — do not re-derive its three shared-state breaches) | no |
| `src/notify/` | shared notification layer: one facade `notify.emit(project, kind, **payload)` appends to the ledger and **never raises**; delivery happens only in short-lived `notify dispatch`, never a daemon. `docs/notify-design.md` is the contract (rulings D-1..D-6). Until the daily digest exists, **info-severity events are ledger-only and never delivered** — that is by design, not a bug to fix | no |
| `src/ocr_backend/` | shared OCR layer: one versioned `OcrDocument` contract + render projections. **Consumers read the contract, never a backend's native output.** `docs/ocr-backend-design.md` (models pinned to a commit sha under `cache/ocr_backend/models/`; GPU-index gotcha in §7) | no |
| `src/ocr_review/` | human-proofreading UI over `ocr-backend parse` bundles (FastAPI + Jinja + vanilla JS; no build chain, no DB, no auth). **The machine JSON is never mutated** — fixes live only in the sparse `review.json` sidecar. `docs/ocr-review-ui-exploration.md` | no |
| `src/pdf_summarizer/` | CLI: PDF → chunks → map-reduce summary (`pdf-summarize`) | yes |
| `src/ai_market_radar/` | scans OpenAI/Anthropic/Copilot **news sources** into SQLite and digests new items — "OpenAI" is a watched entity, not a dependency. Deterministic parsing on purpose. `docs/ai-market-radar-pipeline.md` | no |
| `src/file_manager/` | FastAPI file manager, metadata-first search (FTS5). `docs/file-manager.md` | no |
| `src/research_agent/` | research & recommendation agent (`research-agent new/answer/status/report`). `docs/research-recommendation-agent/00-overview.md` first, `06-usage-guide.md` to run it; the package layout mirrors those volumes | yes |
| `src/rag/` | knowledge pipeline over OCR output (`rag build/query/eval/status/traces/enrich`) — **lexical-only, vector-free by design**. Enrich is an independent background step, not part of build: it persists LLM annotations per chunk, so chunks carry different enrich states. `docs/rag/00-overview.md` first | yes, query-time + `rag enrich` |
| `src/orchestrator/` | enterprise request-orchestration runtime: the LLM only proposes, the budget-bounded harness decides. `docs/orchestrator-design.md` is the decisions authority (DP-1..DP-10 — do not re-derive it from the blog notes); `docs/orchestrator/00-overview.md` maps the volumes and carries current status | yes, front-half flash + query-time capabilities |
| `src/skill_manager/` | vendored AI skills end to end. `docs/skill-manager-guide.md`; see "AI skills & specs" below | no |
| `src/quantdesk/` | personal-quant research data plane + factor screening over the Binance archive (`quant download/convert/list/verify/universe/record/screen`). Verdicts are read **only at stressed 20 bp/side**, and the last 12 months are clamped out in code with **no peek flag**. `docs/quantdesk/00-overview.md` first; `docs/personal-quant-bootstrap-plan.md` is the scope authority | no |
| `src/photo_desk/` | 家庭照片管理：NAS 目录只读 + 可逆筛查 + 无账号时间线（`photos triage [--apply]` / `photos restore`）。`triage.py` 是全系统唯一写通道，原件字节不可变，隔离/放回落 move ledger。`docs/photo-desk-design.md`（裁决 D-1..D-5 在 §2，勿重新推导） | no |
| `src/exam_builder/` | 组卷 over a local Word corpus (`exam ingest/ls/search/compose/serve`): `compose` is docx **package surgery**, not python-docx, and question blocks are stored as verbatim OOXML fragments, never flattened to text. By D-3 formulas are **neither rendered nor searchable**. `docs/exam-builder-design.md` (rulings D-1..D-8 in §2) | no |
| `src/coding/`, `src/modules/` | standalone algorithm exercises and one-off scripts (incl. the Streamlit `seating_app.py` — why streamlit/pandas/openpyxl/xlsxwriter sit in core deps) | no |
| `scripts/` | kept-for-the-record verification scripts, one per investigation. Machine-local helpers here are gitignored, not deleted — add new ones to `.gitignore` deliberately | no |
| `specs/` | spec/proposal documents *authored by* AI coding tools, one subfolder per producing tool or change — the evidence trail behind what shipped in `src/`. Hand-written design docs are the different thing and live in `docs/` | — |

## Storage conventions

- Each project owns a SQLite file under `data/<project>/` (e.g.
  `data/ai_market_radar/kb.db`; `file_manager` respects `FM_DATABASE_URL`).
- **`data/` vs `cache/` is a contract, not a naming preference**: `data/` holds
  assets no command can rebuild (DBs, ledgers, live recordings); `cache/` holds
  bulk its provisioning command rebuilds. Both are gitignored, but deleting
  `cache/` whole is always safe while `data/` deserves a backup. Classify a new
  directory deliberately. Anchors: `utils.paths.data_dir()` / `cache_dir()`.
- **Default data paths are anchored to the repo root** — the expression lives
  once in `src/utils/paths.py` (`REPO_ROOT`, `data_dir("<project>")`); projects
  import it, never cwd-relative. Launching elsewhere must not create a second DB
  or stray outputs. Env vars (`FM_DATA_DIR`, `RESEARCH_AGENT_DATA_DIR`) and
  `--data-dir` still override.
- `src/utils/` is the shared-helper package (`paths.py`, `timer.py`) — add
  generic helpers here rather than copying them into projects; don't prune it as
  dead code.
- **FTS5 + CJK:** the default `unicode61` tokenizer drops CJK tokens, and
  `trigram`/`editdist3` are unavailable here. Folding lives in the shared layer —
  build indexes with `storage.FtsTable` and queries with
  `storage.match_expr`/`token_expr` so the two ends can't drift
  (`src/storage/fts.py`; `docs/storage-usage-guide.md` §3).

## Docker

Only OCR is containerized, as a **one-shot runner** (not a service):
`docker/ocr/Dockerfile.{cpu,gpu}` + `docker/ocr/compose.yaml`, two profiles.
Prefer `ocr-backend container build|download|parse [--gpu]` over retyping
`docker compose …` — it resolves path/profile/service from `REPO_ROOT` and
rejects a file outside `data/ocr_backend/{in,out}` with a clear error instead of
a silent Docker failure. Rationale and the mount layout:
`docs/service-containerization-exploration.md`. **Not verified end-to-end on this
machine** (no Docker Desktop here; the wrapper's unit tests fake `subprocess.run`)
— TODO.md.

## Style & checks

- Tool settings have **one home**: black + isort + mypy in `pyproject.toml`,
  flake8 in `.flake8` (max-line 100, extend-ignore E203). All of them run as
  pre-commit hooks **from the project venv** (dev group, pinned by `uv.lock`), so
  hook, editor and `uv run black` can't fight over versions; bump a tool by
  editing pyproject + `uv lock`. Every `src/` package ships `py.typed`.
  `docs/verification-gates.md` L1 is the full map.
- A format violation is auto-fixed and the **commit is aborted** — re-stage and
  commit again (new commit, never amend).
- Tests mirror the source layout: `tests/test_<project>/test_<module>.py`.
- Commit messages: short, lowercase, imperative ("add file manager with graded
  metadata search"); the *why* goes in the body when non-obvious.
- **One feature, one branch**, off `main` (`git checkout -b <feature>`). Never
  stack a second task onto a branch carrying another's uncommitted or unmerged
  work — `git status` and every diff must describe exactly one task.
- **main is protected** (required `check`, enforced for admins, no force-push):
  push the branch, open a PR (`gh pr create`), merge only on green.
- **After the PR is open, don't babysit CI** — enable auto-merge and let GitHub
  track the check: `gh pr merge <n> --auto --merge --delete-branch`. **Merge
  commit, not squash** (owner preference: the second parent keeps the branch's
  per-commit history reachable after its ref is deleted). Never poll or sleep on
  CI; a red or pending check simply never merges. **Guardrails:** this standing
  authorization covers only this repo's own PRs, only merges on a green required
  check, and never `--admin`-bypasses one unless explicitly asked in the moment.
- **Local branch cleanup is per-branch, not a blanket sweep.** After merges,
  `git fetch --prune`, then delete only branches whose work is already in `main`
  (`git branch --merged main` is the candidate list). Hold any branch backing an
  open PR and any local-only branch never pushed or integrated; when the remote
  is unreachable, integrate locally rather than delete. Never `git branch -D`
  across the lot.

## AI skills & specs

- `skills` (`src/skill_manager/`) is the only entry point to the vendored-skills
  model. `docs/skill-manager-guide.md` carries the how-tos, the error-message
  decoding and the rationale: sync / list / outdated / add, updating a pin,
  adding an upstream or an install target, and re-copying overrides.
- The declaration is tracked: `sources.UPSTREAMS` (one entry per repository, each
  pinned to a **full commit sha**) and `sources.TARGET_DIRS`. An upstream update
  is a `commit` bump plus `skills sync`, **never a `git pull`** — a pull leaves
  the machine holding state the repo knows nothing about.
- `.agents/skills` is the single install target because it is harness-neutral.
  Another tool that needs its own path is one `sources.TARGET_DIRS` entry — but
  **never a second root some harness also scans**: OpenCode reads
  `.opencode/skills`, `.claude/skills` and `.agents/skills` together and requires
  skill names to be unique across them.
- The generated views (`repo-skills/` clones and the install dirs) are never
  hand-edited and stay gitignored — and gitignored is not the same as untracked:
  `git ls-files repo-skills .agents` must print nothing.
- A clone is a mirror, so local differences go to the tracked **`skills/`**
  directory: a folder named as the skill *installs* (`superpowers-brainstorming`,
  prefix included) replaces it in every target, which is how an edit survives the
  next pin bump.
- The spec-writing skills (`openspec-proposal` / `-apply` / `-archive`,
  `superpowers-brainstorming` / `-writing-plans`) produce material archived under
  **`specs/`**, keyed by the tool or change that authored it. Upstream's openspec
  skills hard-code `openspec/changes/<id>/`, so this repo carries its own copies
  redirected to `specs/openspec/`; `superpowers-*` output is filed under
  `specs/superpowers/` by hand (guide §6).

## Docs

- Design/explanation docs live in `docs/`; the house style explains *why* each
  choice was made, with a big-picture ASCII diagram up front
  (`docs/ai-market-radar-pipeline.md` is the reference example).
- `docs/verification-gates.md` is the single map of every check the repo runs
  (pre-commit L1 → CI L2 → live/real-env L3 → recorded evidence L4) plus the
  add-a-gate checklist; keep its tables honest when a gate lands or a TODO.md
  debt is paid.
- Multi-part designs get a folder whose numbered `00-overview.md` is the entry
  point, maps the volumes and carries current status: `docs/rag/`,
  `docs/orchestrator/`, `docs/quantdesk/`,
  `docs/research-recommendation-agent/`. Read the overview before the volumes.
- `docs/scaling-lessons.md` is the cross-cutting digest — the scale problems rag
  and orchestrator actually hit, reorganized as six design-time questions plus a
  checklist. Read it before designing schemas; per-system evidence stays in the
  two `04-scaling` docs.
- Decisions authorities, i.e. the documents to start from and never re-derive:
  `docs/orchestrator-design.md` (DP-1..DP-10) for orchestrator,
  `docs/personal-quant-bootstrap-plan.md` for quantdesk scope, and each design
  contract's numbered ruling section (notify D-1..D-6, photo_desk D-1..D-5,
  exam_builder D-1..D-8).
