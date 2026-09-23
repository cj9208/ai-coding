# Verification Gates — the repo's checking story

One sentence: **every "it works" claim in this repo is backed by a named,
re-runnable check, and the gates are arranged from cheapest-and-most-frequent
(at every commit) to most-real-and-rarest (live hardware, real networks,
human eyes)** — the expensive ones are gated behind explicit triggers so they
never silently rot into "assumed passing".

```
            edit files
                │
   ┌────────────▼─────────────┐  commit-time (local, L1)
   │ git commit               │  pre-commit: whitespace/EOF/yaml → black
   │                          │  flake8 isort mypy bandit (venv hooks) →
   │                          │  commitizen (message gate)
   └────────────┬─────────────┘  auto-fixers abort the commit: re-stage, new commit
                │
   ┌────────────▼─────────────┐  push / PR (hosted, L2)   ← the cross-platform
   │ GitHub Actions CI        │  uv sync --locked → pre-commit --all-files
   │ (ubuntu-latest, no       │  → pytest --cov=src  (live tests self-skip)
   │  secrets, ~70s)          │  gate: this machine is Windows, runners are Linux
   └────────────┬─────────────┘
                │
   ┌────────────▼─────────────┐  opt-in, env/file-triggered (L3)
   │ live & real-env checks   │  LLM_API_KEY → summarize live · OCR_LIVE + model
   │ (run by a human, locally │  snapshot → OCR live · Docker runner · HEIC/NAS
   │  or a keyed machine)     │  — each skip reason is printed, never hidden
   └────────────┬─────────────┘
                │
   ┌────────────▼─────────────┐  acceptance evidence, tracked in git (L4)
   │ recorded verdicts        │  orchestrator golden suite · quantdesk trial
   │ (what a release means    │  ledger + byte-identical run manifests · golden
   │  to remember)            │  JSONL fixtures (photo groups, rag sample)
   └──────────────────────────┘
```

Four layers, one each: **L1 keeps each commit clean; L2 keeps main working
beyond this one machine; L3 is where reality is allowed to answer; L4 is where
a decision is frozen so the next session cannot quietly re-litigate it.**
This doc inventories every gate, its command, its trigger, and — for each —
why it is shaped that way. AGENTS.md stays the per-project detail; this file
is the single map.

---

## Design posture (the "why" behind all layers)

- **A gate must be re-runnable by anyone, anywhere.** Hence every default path
  is anchored to `REPO_ROOT` (`src/utils/paths.py`), every dependency
  resolution is pinned by the committed `uv.lock`, and the CI environment is
  rebuilt from `uv sync --locked` — a green run on a stranger's clean checkout
  is the unit of proof, not a green run on this laptop.
- **Real-environment checks are opt-in, and their absence is loud.** Tests
  that need a live API key, a model snapshot, or a GPU do not fail-or-hang in
  CI; they `skipif` on a named condition and *print the reason*. The debt then
  moves to `TODO.md` (below), so "skipped" never silently becomes "done".
- **Evidence is tracked even when data is not.** `data/` is gitignored —
  databases, downloads, demo trees. But anything that *decides* something
  (golden cases, trial ledgers, run manifests) lives under tracked `config/`
  or `tests/golden/`, because a verdict no one can re-check is folklore.

## L1 — pre-commit (every `git commit`)

Config: `.pre-commit-config.yaml`. Two families, and the split is deliberate:

| Hook | Catches | Why it lives here |
|---|---|---|
| trailing-whitespace, end-of-file, check-yaml | byte-level drift | zero-cost, tool-agnostic |
| black, flake8, isort, mypy, bandit | format → style → types → security | run as **local** hooks via `uv run` |
| commitizen | commit-message shape | the only gate on prose, not code |

- The five Python checkers are `repo: local` and shell out to `uv run <tool>`,
  so the hook, the editor and the CLI all execute **one version, pinned by
  `uv.lock`** — a version disagreement between "my editor says it's fine" and
  CI is structurally impossible. Bumping a tool = edit pyproject + `uv lock`.
- black/isort auto-fix, which means the commit is **aborted after rewriting
  files**: re-stage and commit again as a *new* commit, never `--amend` (the
  hook failure means the commit never happened — amending would target the
  previous, unrelated commit).
- mypy is in this family, not its own layer: its
  `ignore_missing_imports` list in `pyproject.toml` is maintained so that a CI
  env *without* OCR extras reports exactly the same thing as the full env
  (`paddle`, `huggingface_hub`, `paddleocr.*`, … were added 2026-09-23 for
  precisely this reason). Checking our own code is the point; phantom
  third-party errors are noise that would train us to ignore the gate.

## L2 — GitHub Actions CI (every push to `main`, every PR)

Config: `.github/workflows/ci.yml`. One ubuntu-latest job, three steps —
`uv sync --locked`, `pre-commit run --all-files`, `uv run pytest --cov=src`.
No secrets. Landed 2026-09-23, first run took 1m13s.

- **Why coverage is reported but not gated:** `--cov=src --cov-report=term`
  prints the per-module table so "which package is nearly untested" is visible
  in every CI log (repo-wide baseline: 78%, 2026-09-23). A threshold number
  would only train everyone to lower the bar; the report is the cheap, honest
  version of the same question.

- **Why ubuntu only, when this machine is Windows:** free quota and fast cold
  starts matter, but the real argument is that a Linux runner is a *second
  opinion* — the very first CI run caught a test asserting Windows path
  separators (`test_photo_desk/test_config.py::test_rel_abs_roundtrip`), a bug
  invisible to any amount of local green. Windows runners would double spend
  and catch little else.
- **Why no OCR extras in CI:** `--extra ocr --extra paddle-*` is hundreds of
  MB and Paddle needs x86 wheels; the test suite was designed model-free
  around that (adapters import `paddleocr` lazily, inside functions), so CI
  gets the full behavioural check at a fraction of the install.
- **Why `--locked`:** resolution drift is a class of bug CI must not be able
  to hide — if `pyproject.toml` and `uv.lock` disagree, CI fails instead of
  quietly resolving something new.
- **Why the live tests are CI-safe without any configuration:** `.env` and
  `data/` are gitignored, so a runner has no key to leak, no DB to clobber,
  and the two live tests (`test_summarize_live`, the OCR live file) self-skip
  on their env/file triggers. A 401 in `test_summarize_live` *locally* means a
  stale key — environment, not regression.

`push` triggers CI on `main`; there is no deploy stage because there is nothing
hosted to deploy — the honest CD for this repo would be a GHCR publish of
`docker/ocr` on tag, and it waits until a tagged image is actually a thing we
consume.

## L3 — live and real-environment checks (human-triggered)

Not in CI by design: they need keys, GPUs, Docker, a NAS mount, or a network
with specific behaviour. Each has a named trigger so it can be re-run, and an
owner row in `TODO.md` so "not yet verified" stays a debt, not a myth:

| Check | Trigger / command | Status & owed verification |
|---|---|---|
| pdf_summarizer live LLM | `LLM_API_KEY` in `.env`, then `uv run pytest tests/test_pdf_summarizer/test_summarizer.py` | runs when a key is present; 401 = stale key |
| PaddleOCR-VL end-to-end | `OCR_LIVE=1` + snapshot from `ocr-backend download paddleocr-vl-1.6` | model-free golden tests cover the contract; live parse is this file's job |
| Docker OCR runner | `ocr-backend container build\|download\|parse [--gpu]` on a Docker Desktop host | **never verified end-to-end** — unit tests fake `subprocess.run`; TODO.md |
| photo_desk HEIC + NAS | real device tree mounted at `PHOTO_ROOT` | acceptance debt recorded in TODO.md (both M0 and M1 lines) |
| quantdesk WS recording | a network where fstream actually pushes | live-verified gap: this machine's WS is silent; liquidation *positive* path awaits TODO.md |
| skill sync reproducibility | `uv run skills sync` on a deleted clone | verified: clone returned at its pin; keep as the check whenever UPSTREAMS changes |

The rule that keeps this layer honest: **a connectivity that *pretends* to
work (connects, returns nothing) is a failure, not a skip** — quantdesk's
silence logging to `recorded/gaps.log` is the embodiment, and why "CI green"
is never the whole story for the data plane.

## L4 — recorded acceptance evidence (tracked in git)

These are gates whose *output already happened* and is committed, so the next
session inherits a verdict instead of re-deriving one:

- **Orchestrator golden suite** — `config/orchestrator/golden_cases.jsonl`,
  run by `orchestrate golden run` (zero-LLM, deterministic). This is the repo's
  closest thing to a release gate: `docs/orchestrator-design.md` fixes it as
  the acceptance record for config-as-release (G-3). A behaviour change that
  cannot pass goldens is a contract change, and contract changes are edits to
  the design doc first.
- **quantdesk trial ledger + run manifests** — every `quant screen` run appends
  a row to `config/quantdesk/trial_ledger.csv` plus a byte-identical manifest
  under `config/quantdesk/runs/`. The three pre-registered M1 runs (all
  failing the stressed 20 bp gate) are recorded *as evidence* — a negative
  result you can reproduce byte-for-byte is what stops p-hacking-by-retry.
- **Golden JSONL fixtures** — `tests/golden/photo_groups.jsonl` locks the
  burst-grouping rules (the only spec of the 3-level rule), `tests/golden/rag_sample.jsonl`
  the rag contract. Unlike L1–L3 these don't *check* new code; they *are* the
  behaviour, in a diffable form.
- **Evidence trail scripts** — `scripts/verify_paddle_vl_16.py` is the raw
  investigation output behind `docs/ocr-backend-design.md` §5.3; kept
  deliberately so a doc claim can be re-derived. Machine-local helpers in
  `scripts/` are gitignored on purpose — kept, not deleted.

## Adding a gate (checklist for a new subsystem)

1. Tests at `tests/test_<project>/test_<module>.py`, model-free and
   fixture-based; anything needing a key/network/GPU goes behind a named
   `skipif` whose reason states the missing trigger.
2. Its DB and data live under `data/<project>/` (gitignored), paths derived
   from `utils.paths.data_dir` — never cwd-relative.
3. Anything that records a *decision* (golden cases, ledger, manifest) goes
   under tracked `config/<project>/` or `tests/golden/`.
4. If reality is still owed (a device, a mount, a network), write the row in
   root `TODO.md` **in the same commit** that ships the code.
5. No new CI step unless the check is deterministic and dependency-light;
   L2's 70-second budget is the gate's own value.

## Commands (all from repo root)

```console
uv sync --locked                                  # L1/L2 environment, fails on lock drift
uv run pre-commit run --all-files                 # L1 without committing
uv run pre-commit run mypy --all-files            # just the type gate
uv run pytest                                     # L2's real check, locally
uv run pytest tests/test_photo_desk -q            # one subsystem
OCR_LIVE=1 uv run pytest tests/test_ocr_backend/test_paddleocr_vl_live.py   # L3
LLM_API_KEY=... uv run pytest tests/test_pdf_summarizer -q                   # L3
uv run orchestrate golden run                     # L4 re-check
gh run list / gh run watch <id>                   # L2 hosted runs
```
