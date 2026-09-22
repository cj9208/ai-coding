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
  `research-agent`, `ocr-backend`, `ocr-review`, `rag`, `skills`, `orchestrate`,
  `quant`, `photos`, `notify`
  (`[project.scripts]`). `skills` is the one owner of vendored AI skills —
  `sync` / `list` / `outdated` / `add` (see "AI skills & specs").
  `ocr-backend` has three subcommands: `download <model>` (provisions a
  snapshot), `parse <file> --out <dir>` (one-shot OCR runner entry point —
  writes the `OcrDocument` JSON + markdown **and copies the source file
  beside them**, so `out/` holds self-contained bundles the review UI can
  pick up), and `container build|download|parse [--gpu]` (shells out to the
  Docker runner below so the compose invocation doesn't have to be retyped —
  see "Docker").
- `ocr-review serve --inbox data/ocr_backend/out` runs the human-proofreading
  web app over those bundles (see the subproject map); `ocr-review add <pdf>
  --ocr <json>` registers a bundle that didn't come from `ocr-backend parse`.
- `test_summarize_live` is the only test hitting a real LLM API (skipped when
  `LLM_API_KEY` unset). A 401 there means the key in `.env` is stale — an
  environment issue, not a code regression.
- `tests/test_ocr_backend/test_paddleocr_vl_live.py` runs the real OCR model
  (skipped unless `OCR_LIVE=1` and the local snapshot under
  `data/ocr_backend/models/` is present, provisioned by
  `ocr-backend download paddleocr-vl-1.6`); the rest of `test_ocr_backend` is
  model-free golden-fixture work.

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
| `src/notify/` | shared notification layer (contract: `docs/notify-design.md`, rulings D-1..D-6 — do not re-derive): task code calls one facade `notify.emit(project, kind, **payload)` which appends a structured event to the SQLite ledger at `data/notify/events.db` and **never raises**; delivery happens only in short-lived `notify dispatch` (scheduler-launched, never a daemon), channels are pluggable adapters sharing one shape (stdout now; telegram + external ping in M1/M2, gated on `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID`/`TELEGRAM_PROXY` in `.env`); "who watches the watcher" is closed at four stacked failure domains L1-L4, L4 being the human inversion "no daily digest = something is wrong" — the acknowledged limit, no VPS for it; **M0 shipped 2026-09-22** (config/events/ledger/channels/cli, 16 tests); CLI `notify emit / status / dispatch` | no |
| `src/ocr_backend/` | shared OCR layer: one versioned `OcrDocument` contract (page → block, pixel bbox) + render projections (`page_text` / `document_markdown`); PaddleOCR-VL 1.6 is the first adapter (engine comes from the `paddle-cpu` / `paddle-gpu` extras; model snapshots live under `data/ocr_backend/models/<name>/`, provisioned by `ocr-backend download <name>` (each model is one `models.MODELS` entry, pinned to a commit sha) and resolved via `ocr_backend.models.model_dir`; see `docs/ocr-backend-design.md` §7 for the GPU index gotcha). Consumers read the contract, never a backend's native output | no |
| `src/ocr_review/` | human-proofreading UI for OCR output (FastAPI + Jinja + vanilla JS, no build chain, no DB, no auth — same posture as file_manager). Loads `ocr-backend parse` bundles from an inbox dir, shows PDF page rasters (PyMuPDF, rendered to the contract's exact pixel grid) with an SVG block overlay, and records fixes as a **sparse sidecar** `review.json` keyed by (page, block id) — the machine JSON is never mutated, so the ground-truth pairing survives. `patch.apply_review` folds the overlay into a corrected `OcrDocument` on export; `patch.reanchor` re-matches entries (IoU + text ratio) after a model re-run shifts ids. Workspaces under `data/ocr_review/<sha12>/`; design doc `docs/ocr-review-ui-exploration.md` | no |
| `src/pdf_summarizer/` | CLI: PDF → chunks → map-reduce summary | yes, via `llm_client` |
| `src/ai_market_radar/` | scans OpenAI/Anthropic/Copilot **news sources** into SQLite, digest of new items. "OpenAI" here is a watched entity, not a dependency. Deterministic parsing on purpose — no LLM | no |
| `src/file_manager/` | FastAPI file manager, metadata-first search (FTS5) | no |
| `src/research_agent/` | research & recommendation agent — **MVP implemented & live-verified 2026-09-20** (`research-agent new/answer/status/report`); design docs in `docs/research-recommendation-agent/` (read `00-overview.md` first, `06-usage-guide.md` to run it), module layout mirrors the docs (orchestrator/research/clarifying/recommendation/contracts/persistence — the storage subpackage was renamed to dodge the top-level `storage` clash) | yes, via `llm_client` |
| `src/rag/` | knowledge pipeline over OCR output: `rag build/query/eval/status/traces/enrich` — M1 (lexical-only, vector-free by design) implemented & live-verified 2026-09-21; docs in `docs/rag/` (read `00-overview.md` first — it maps rationale / implementation / usage to the three volumes), sample golden file `tests/golden/rag_sample.jsonl`. Enrich is an independent background step (`rag enrich`), not part of build — it persists LLM annotations to the `inferred` table per chunk, so different chunks can have different enrich states | yes, query-time + `rag enrich` (independent), via `llm_client` |
| `src/orchestrator/` | enterprise request-orchestration runtime — the deterministic harness (4 decision tables with row ids, budget-bounded state machine, 7 typed runtime objects persisted to SQLite via `src/storage`) in which the LLM only proposes and the harness decides; implementation contract is `docs/orchestrator-design.md` (the decisions authority — DP-1..DP-10 and M0-M3 are fixed there; do not re-derive it from the blog notes), with rag-style usage volumes in `docs/orchestrator/` (read `00-overview.md` first — it maps rationale / implementation / usage to `01`/`02`/`03`, scaling analysis to `04-scaling.md`, and the scaling sub-plans to `05a`–`05d`; `03-usage.md` is the full CLI reference and the add-a-capability recipe). **M0-M3 shipped 2026-09-22** (M0: contracts/store/policy/runtime/registry/golden + CLI `status/replay/handoff/golden run`, zero-LLM; M1: deterministic front half — `safety` pattern gate, `normalize` Chinese alias table, flash `interpret` via `llm_client`, CLI `ask`/`ask --resume`; M2: production registry from `config/orchestrator/capabilities.yaml` + `capabilities/rag.py` adapter over `rag query`'s store + CLI `registry check`; M3: `capabilities/lookup.py` over file_manager metadata search, declared as rag's runnable fallback so `switch_capability` (e4/v3) is live — verified end-to-end on the real file_manager db). Wall clock budgets machine work only: human clarification gaps are accounted out (`wall_clock_paused_ms`). Golden cases: `config/orchestrator/golden_cases.jsonl` | yes, front-half flash + query-time capabilities, via `llm_client` |
| `src/skill_manager/` | vendored AI skills, end to end: `sources.UPSTREAMS` declares each repository pinned to a commit sha (mirrors `ocr_backend.models`), `vendor.py` provisions the gitignored clones under `repo-skills/`, `install.py` rebuilds every generated install dir (`.opencode/skills/`) as a full view of the declaration — pruning names no longer declared, `cli.py` is the `skills` entry point. No LLM, no DB, no network beyond `git clone`/`fetch` | no |
| `src/quantdesk/` | personal-quant research data plane + factor screening harness (docs set: `docs/quantdesk/` — read `00-overview.md` first, it maps the four volumes; plan: `docs/personal-quant-bootstrap-plan.md`, context: `docs/personal-quant-trading-exploration.md`) — diff-sync of the Binance public archive with a sha256 inventory that records upstream *replacement events* (`config.DATASETS` registry mirrors `ocr_backend.models`), Parquet hive (`data/quantdesk/parquet/<ds>/symbol=…/year=…/month=…`) as the single source of truth, absorbed format traps (2025 ms→µs switch, header sniffing); CLI `quant download/convert/list/verify/universe( sync|show|rank )/record/screen`; **M0.5 `quant record` shipped** — the recorder for what archives don't carry: USD-M liquidations (WS `!forceOrder@arr`), funding/premium (REST poll), open interest (REST poll), appended as typed parquets under `data/quantdesk/recorded/<stream>/day=…/`; a network that connects but pushes nothing is treated as a gap (silence logged to `recorded/gaps.log` — live-verified: this machine's fstream WS is silent while fapi REST and spot WS work, so the liquidations *positive* path still awaits a working network, see TODO.md); **M1 factor layer shipped 2026-09-22** — `signals.py` holds three pure as-of factors (csm / tsm / funding reversal; first statement filters `date <= as_of`, so a factor cannot see tomorrow's bar), `screen.py` is the only place time moves forward (decide at t close, fill+cost at t+1), costs run at two levels with the verdict only at stressed 20 bp/side, the last 12 months are clamped out by code with no peek flag, and every run appends a row to `config/quantdesk/trial_ledger.csv` plus a byte-identical manifest under `config/quantdesk/runs/` (evidence is tracked, unlike `data/`); **the three M1 pre-registered runs landed 2026-09-22** on the crypto-native top-20 universe (owner scope call: today's volume ranking's tokenized stock/commodity perps skipped — large caps only), 2022-01-01..2026-06-30, holdout sealed before 2025-08-31 and unread — **all three fail the stressed 20 bp gate** (Sharpe csm 0.36 / tsm 0.18 / funding −0.45 vs the 0.5 bar), and the byte-identical-manifest reproducibility check is the acceptance record in `config/quantdesk/` | no |
| `src/photo_desk/` | 家庭照片管理（设计契约 `docs/photo-desk-design.md`，裁决 D-1..D-5 在 §2 勿重新推导）——NAS 目录只读 + 可逆筛查 + 无账号时间线；**M0 shipped 2026-09-22**（config 双根地址 PHOTO_ROOT/PHOTO_DESK_DATA_DIR、DB 只存相对路径供迁 NAS、mtime+size 先筛 sha256 后算的增量 scan、Pillow+pillow-heif EXIF/GPS/亚秒提取、512/1600 两档 WebP 派生缓存、FastAPI+Jinja 时间线按拍摄日分组），真机演练在 `data/photo_desk_demo/`（gitignored），HEIC 真读与 NAS 挂载验收欠账见 TODO.md；M1 隔离账本（move_ledger + 一键放回）/ M2 tag+FTS 未开工 | no |
| `src/coding/`, `src/modules/` | standalone algorithm exercises and small one-off scripts (e.g. `analyze_birth.py`, `analyze_package_size.py` — root-level scripts were moved into `modules/` to keep `src/` clean), plus `seating_app.py`: a Streamlit classroom-seating app (`streamlit run src/modules/seating_app.py`, uploads its own Excel) — it is why streamlit/pandas/openpyxl/xlsxwriter sit in the core deps | no |
| `scripts/` | kept-for-the-record verification scripts, one per investigation (`verify_paddle_vl_16.py` is the evidence trail behind `docs/ocr-backend-design.md` §5.3). Machine-local helpers here are gitignored, not deleted — add new ones to `.gitignore` deliberately | no |
| `specs/` | spec/proposal documents *authored by* AI coding tools, one subfolder per producing tool or change — the evidence trail behind what shipped in `src/`. Hand-written design docs are the different thing and live in `docs/`; see "AI skills & specs" for where these come from | — |

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

## Docker

- Only OCR is containerized so far, as a **one-shot runner** (not a service):
  `docker/ocr/Dockerfile.{cpu,gpu}` + `docker/ocr/compose.yaml`, with two
  Compose profiles (`--profile cpu` / `--profile gpu`). Build/run commands are
  in the compose file's header comment.
- Prefer `ocr-backend container build|download|parse [--gpu]`
  (`src/ocr_backend/container.py`) over retyping `docker compose ...`: it
  resolves the compose path, profile, and service name from `REPO_ROOT`, and
  maps host paths under `data/ocr_backend/{in,out}` to the container's
  `/work/{in,out}` mounts — a file outside those directories is rejected with
  a clear error instead of a silent Docker failure.
- The runner mounts `data/ocr_backend/{models,in,out}` — models stay outside
  the image (bind-mounted at the container's `REPO_ROOT`-anchored default
  path), inputs are read-only, results land in `out/` and survive `--rm`.
- Rationale (why a runner, not an HTTP service, and why CPU/GPU are separate
  images): `docs/service-containerization-exploration.md`.
- Not yet verified end-to-end on this machine — Docker CLI is unavailable in
  the current shell, so the image build and a real container parse run still
  need to be confirmed on a host with Docker Desktop installed (the wrapper's
  own unit tests fake `subprocess.run` and don't need Docker).

## Style & checks

- Tool settings have **one home**: black + isort + mypy in `pyproject.toml`
  (`[tool.black]`, `[tool.isort]`, `[tool.mypy]` — including
  `ignore_missing_imports` overrides for the stub-less third-party list),
  flake8 in `.flake8` (max-line 100, extend-ignore E203).
- black / flake8 / isort / mypy / bandit are **local pre-commit hooks running
  the project venv** (they sit in the dev group, pinned by `uv.lock`) — one
  version everywhere, so the hook, the editor and `uv run black` can never
  fight over style. Bump a tool by editing pyproject + `uv lock`.
- The venv-based mypy sees the real installed deps, so optional extras like
  `paddleocr` don't produce phantom import errors (documented ignores cover
  the stub-less ones). Every `src/` package ships `py.typed`.
- Pre-commit also runs: trailing whitespace, end-of-file, check-yaml,
  commitizen. Format violations are auto-fixed by the hook and the
  **commit is aborted** — re-stage the reformatted files and commit again
  (new commit, never amend).
- Tests mirror the source layout: `tests/test_<project>/test_<module>.py`.
- Commit messages: short, lowercase, imperative ("add file manager with
  graded metadata search"); mention the *why* in the body when non-obvious.

## AI skills & specs

- **`src/skill_manager/` is the whole model**; `skills` is its only entry point:
  `uv run skills sync` provisions what is missing and rebuilds the installs,
  `skills list` shows pins vs. what is actually checked out, `skills outdated`
  fetches and reports how far each pin trails its upstream (and which skills
  moved), `skills add <repo>` clones a new upstream and prints the declaration
  to paste. The two generated views — `repo-skills/` clones and
  `.opencode/skills/` installs — are never hand-edited and stay gitignored.
  Being gitignored is not the same as being untracked: `git ls-files repo-skills
  .opencode` must print nothing, and a stray path goes out with
  `git rm --cached` (the files are on disk because `sync` rebuilds them).
  How-tos, error-message decoding, and the rationale for each choice are in
  `docs/skill-manager-guide.md`.
- What gets installed is one tracked table: `sources.UPSTREAMS`, one entry per
  repository, each pinned to a **full commit sha** (same posture as
  `ocr_backend.models`). So an upstream update is a `commit` bump plus
  `skills sync`, never a `git pull` — a pull would leave the machine holding
  state the repo knows nothing about. Another agent tool that reads skills from
  the workspace is one entry in `sources.TARGET_DIRS`; a new upstream is one
  `UPSTREAMS` entry. `uv run skills sync` on a clean machine reproduces the
  whole set (verified: a deleted clone came back at its pin).
- A clone is a mirror, so local differences go to the tracked **`skills/`**
  directory (`sources.OWN_SKILLS_DIR`) instead: a folder there named as the
  skill *installs* (`superpowers-brainstorming`, prefix included) replaces it
  in every target, which is how an edit survives the next pin bump. `sync`
  refuses to move a clone carrying uncommitted changes precisely so the fix
  lands here rather than in a place git never sees.
- The skills that *write* specs are `openspec-proposal` / `-apply` /
  `-archive` (SDD workflow) and `superpowers-brainstorming` /
  `-writing-plans`. Whatever they produce gets archived under **`specs/`**,
  keyed by the tool or change that authored it. Upstream's openspec skills
  hard-code their output to `openspec/changes/<id>/`, so this repo carries its
  own copies at `skills/openspec-{proposal,apply,archive}/` with those paths
  redirected to `specs/openspec/` — a run lands in the tracked folder, and no
  root-level `openspec/` ever appears. `superpowers-*` output has no such
  hard-coding and is filed under `specs/superpowers/` by hand; re-copying the
  overrides after a pin bump is covered in `docs/skill-manager-guide.md` §6.

## Docs

- Design/explanation docs live in `docs/`; the house style explains *why*
  each choice was made, with a big-picture ASCII diagram up front
  (see `docs/ai-market-radar-pipeline.md` as the reference example).
- Multi-part designs get a folder (`docs/research-recommendation-agent/`)
  with a numbered overview as the entry point.
- `docs/scaling-lessons.md` is the cross-cutting digest: the scale
  problems rag + orchestrator actually hit (and the shared-layer bugs
  they exposed), reorganized as six design-time questions plus a
  checklist to copy into the next subsystem's design doc. Read it
  before designing schemas; the per-system evidence stays in the two
  04-scaling docs.
- `docs/orchestrator-design.md` is the implementation contract for
  `src/orchestrator/` (enterprise request-orchestration runtime; **M0–M3
  all shipped, live-verified**). It fixes the DP-1..DP-10 positions, the M0–M3 build order,
  and the per-milestone implementation notes (fallback legality, id scheme,
  wall-clock-vs-human-time, switch-path signal derivation, resolved open
  questions). Start there before implementing — do not re-derive it from the
  source blog note set.
- `docs/orchestrator/` mirrors the `docs/rag/` set: `00-overview.md` is the
  entry point, then `01-design-rationale` / `02-implementation` /
  `03-usage` (CLI reference, golden-case format, live-proof recipes,
  add-a-capability recipe) / `04-scaling` (request-concurrency and
  deployment-shape investigation — the storage numbers are now
  **measured** via `scripts/orch_bench_run.py`, and its "Problem status
  ledger" is the single table of which scale breaks are fixed vs. open),
  executed through four sub-plans `05a`–`05d` (data plane / trust
  boundary / cost gate / lifecycle-governance). **05a is complete
  (2026-09-22)**: write batching, version CAS (`store.ConflictError`),
  seq-on-envelope, clarification TTL, two indexes, busy_timeout — the
  before/after bench is the acceptance record and Postgres is closed as
  "won't trigger" — plus the async surface (`FrontHalf.interpret`/
  `Capability.run` are coroutines; `run_turn_async`/`resume_async`
  embed in a running loop, sync shims keep CLI/golden/bench unchanged).
  **05b step 1 landed the same day**: front-half assets are per-locale
  packs in `src/orchestrator/packs/` (zh verbatim + en first tranche;
  `safety.evaluate(text, locale)`; an unknown locale clarifies via
  `s_unsupported_locale` — the old silent-allow fallthrough for
  non-Chinese input is gone). **05b steps 4–5 landed too**: the
  adversarial test class (`test_adversarial.py`) fixed two real holes —
  the safety gate now also sees clarification *answers*, and gate
  constraints can't be revoked by a model proposal — while the gateway
  contract (authN/rate-limit/moderation before `run_turn`) is written
  into `03-usage.md`. **05c steps 1–3 landed the same day**: cost model
  written (`docs/orchestrator/05c-cost-gate.md` §1), per-user daily
  LLM-call quota gate enforced by routing row r10 through a contract
  amendment (g18/g19), and the deterministic fast path behind
  `ORCHESTRATOR_FAST_PATH=1` (default off; parity test: same input both
  paths fires identical row ids, fast path spends zero calls) — the
  answer cache is parked by the model's NO-GO until real traffic prices
  its hit rate. **05d steps 1–3 landed**: the contract's new "Governance
  Records" section holds G-1 (erasure decided — crypto-erase, effective at
  the first EU/PIPL tenant), G-2 (handoff worklist: `handoff_tickets` +
  `worklist|claim|resolve`, a pure consumer of unmutated packets, no
  state-machine edge), and G-3 (config-as-release split: `envelope.config_hash`
  + replay drift display shipped now, per-region release process gated on
  the host; golden suite = the written release gate). The service host that
  would consume the
  rest is unbuilt; remaining 05b–05d steps are gated on their triggers.
  The contract stays the decisions authority.
- `docs/quantdesk/` mirrors the `docs/rag/` set for `src/quantdesk/`
  (decision D-2 of the plan: one doc until M0 shipped, then the numbered
  set): `00-overview.md` is the entry point, then
  `01-design-rationale` (inventory-and-replacement posture, silence-is-a-gap,
  as-of purity, stressed-cost verdict, holdout clamping in code) /
  `02-implementation` (the 9 modules, every tuned constant, the absorbed
  Binance format traps, deviations from the plan) / `03-usage` (full `quant`
  CLI reference, live-proof recipes incl. the top-20 backfill and the
  byte-identical-rerun check, add-a-dataset / add-a-factor recipes). The
  upstream *why* stays in `docs/personal-quant-trading-exploration.md`
  (viability) and `docs/personal-quant-bootstrap-plan.md` (decisions
  authority for scope — do not re-derive it from the volumes).
