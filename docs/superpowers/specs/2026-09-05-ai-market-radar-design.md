# AI Market Radar — Design

Date: 2026-09-05

## Purpose

A knowledge base that automatically scans official sources for OpenAI, Anthropic,
and GitHub Copilot and surfaces **only new** developments, so the user can stay
current on (1) research/capability developments, (2) product changes, and
(3) deals/offers. Each finding must be traceable to its source.

## Non-goals (this milestone)

- LLM-generated summaries (config-gated hook reserved for later; excerpts used now).
- Streamlit dashboard presentation.
- OpenAI pricing page snapshot diffing (Cloudflare-blocked, 403).
- Scheduling/orchestration (scanner is a runnable CLI).

## Radar targets and signal taxonomy

Targets (companies/products):

- `openai` — OpenAI (ChatGPT, Codex, GPT models, API).
- `anthropic` — Anthropic (Claude, Claude Code, API).
- `copilot` — GitHub Copilot / Microsoft.

Signals per target:

- `research` — model/capability research developments, papers, new capabilities.
- `product` — product changes, features, release notes, limits.
- `deal` — pricing changes, plans, promotions/offers.
- `status` — availability/incidents (opt-in; helps correlate outages with news).

## Verified source registry (`sources.toml`)

Each source has: `key`, `entity`, `signal_type` (default), `kind`, `url`,
optional `filter`, optional `enabled`. The registry is TOML (parsed with the
stdlib `tomllib`) so no YAML dependency is needed.

| key | entity | default signal | kind | url |
|---|---|---|---|---|
| openai_news | openai | product | rss | https://openai.com/news/rss.xml |
| openai_blog | openai | research | rss | https://openai.com/blog/rss.xml |
| openai_changelog | openai | product | markdown | https://platform.openai.com/docs/changelog.md |
| openai_models | openai | research | markdown | https://platform.openai.com/docs/models.md |
| openai_status | openai | status | rss | https://status.openai.com/history.rss |
| anthropic_release_notes | anthropic | product | markdown | https://docs.anthropic.com/en/release-notes/api.md |
| anthropic_claude_code | anthropic | product | markdown | https://raw.githubusercontent.com/anthropics/claude-code/main/CHANGELOG.md |
| anthropic_news | anthropic | research | html_news | https://www.anthropic.com/news |
| anthropic_pricing | anthropic | deal | html_snapshot | https://www.anthropic.com/pricing |
| anthropic_status | anthropic | status | rss | https://status.anthropic.com/history.rss |
| github_blog | copilot | research | rss | https://github.blog/feed/ (Copilot/AI-tagged items only) |
| github_changelog | copilot | product | rss | https://github.blog/changelog/feed/ (Copilot-tagged items only) |
| copilot_plans | copilot | deal | html_snapshot | https://github.com/features/copilot/plans |

Notes from research:

- OpenAI and GitHub publish RSS; Anthropic has **no public RSS**. Its `docs`
  are Mintlify and serve clean Markdown via a `.md` suffix. Its `/news` index
  renders article links + excerpts server-side, and each article page exposes
  `og:title`, `og:description`, and canonical URL server-side — so `html_news`
  fetches the index, collects slugs, then fetches only *new* article pages for
  meta (bounded per run, default 25). Dates are not exposed in meta; published
  date is best-effort otherwise.
- `anthropic.com/pricing` and `github.com/features/copilot/plans` are
  snapshot-able; `openai.com/api/pricing` and `openai.com/chatgpt/pricing`
  return 403 to plain clients → excluded (OpenAI deal/product news flows via
  its RSS).
- Status pages (status.openai.com, status.anthropic.com) are Atlassian
  Statuspage and expose RSS history.

## Architecture

Package root: `src/ai_market_radar/`.

```
src/ai_market_radar/
  sources.toml        # source registry (above)
  models.py           # Entity, SignalType, SourceConfig, Item (dataclasses)
  tags.py             # thing-type taxonomy + classification rules
  fetchers/
    __init__.py       # dispatcher: source kind -> fetcher
    base.py           # shared HTTP session, UA, timeouts, text helpers
    rss.py            # feedparser -> raw entries
    markdown.py       # .md changelog pages (OpenAI/Anthropic docs)
    html_news.py      # index slug list -> per-article meta extraction
    html_snapshot.py  # readable-text snapshot of pricing/plans pages
  normalize.py        # raw entries -> Items (filter, signal, tag, fingerprint)
  store.py            # SQLite knowledge base + fingerprint dedupe
  digest.py           # new-item digest grouped company -> tag
  cli.py              # uv run ai-market-radar / python -m ai_market_radar
```

Data flow: `cli` → load registry → for each enabled source: fetch (adapter)
→ normalize → `Item`s → `INSERT OR IGNORE` into KB → write digest of newly
inserted items to `data/ai_market_radar/digests/YYYY-MM-DD.md`.

## Knowledge base and dedupe

- SQLite file: `data/ai_market_radar/kb.db` (gitignored via `data/`).
- Schema: `items(id INTEGER PK, fingerprint TEXT UNIQUE NOT NULL, source_key TEXT,
  entity TEXT, signal TEXT, tag TEXT, title TEXT, url TEXT, published_at TEXT,
  excerpt TEXT, raw_hash TEXT, fetched_at TEXT)`; `source_state(source_key TEXT PK,
  last_ok_at TEXT, last_error TEXT, last_items INTEGER)`.
- Fingerprint derivation (first that applies):
  1. canonical/permalink URL (RSS `link`, article canonical URL),
  2. `sha256(heading + body)` for markdown changelog entries,
  3. `sha256(normalized_text)` for html_snapshot (only inserted when the text
     hash changes relative to previously stored snapshots).
- A run inserts new rows only; `INSERT OR IGNORE` on the unique fingerprint
  guarantees no duplicates across runs.
- RSS entries that resolve to the same article from blog + news feeds share the
  URL fingerprint → naturally deduplicated across sources (first source wins).

## Digest (v2 — tagged and curated)

- Grouped by company, then by primary **tag** in fixed high-to-low-signal order:
  New models & capabilities, Pricing & offers, Features & platform,
  Research & science, Security & safety, Company news, Customers &
  partnerships, Status / availability.
- Opens with `New this run — OpenAI n · Anthropic n · GitHub Copilot n.`
- Each bullet: `- **Title** <url> · date — excerpt (~160 chars)`; changelog
  excerpts are markdown-cleaned for readability.
- Per-company section count in the header. Daily file per run appends only new
  items; identical re-runs do not rewrite the file.

## Tag taxonomy and signal tagging

- Primary thing-type tags (rules in `tags.py`): `model`, `pricing`, `feature`,
  `research`, `security`, `company`, `ecosystem`, `status`. The source-level
  `signal` (research/product/deal/status) is retained as a secondary axis.
- Order of checks: status source or html_snapshot hard-assign → pricing words →
  `powered by`/`How X…`/partnerships (ecosystem) → strong versioned model
  identifier + launch verb (`model`) → weak family token (`gpt`, `codex`, …)
  without product-context noise (`store`, `api`, `sdk`, `spec`, …) + launch verb
  → security → research → ecosystem → company → feature → signal-based fallback.

## Error handling

- Each source runs in its own try/except. Failures record `last_error` in
  `source_state`, print a warning, and never abort other sources.
- Polite HTTP: browser-like UA, timeouts; a shared `httpx.Client`.
- html_news is best-effort: if the index structure changes and extraction yields
  zero slugs, that source reports an error instead of silently producing nothing.

## Testing

pytest (`tests/test_ai_market_radar/`):
- fingerprint: URL normalization, markdown heading/content fingerprints, snapshot hash.
- markdown changelog parser fixtures (OpenAI month-group style, Anthropic
  full-date style, Claude Code version style, whole-document fallback).
- tag taxonomy examples + section order stability.
- digest grouping/output shape and idempotent file append (tmp dir).
- store dedupe on unique fingerprint; registry loads all sources.
- Network-dependent scans are exercised manually (see README/run commands), not
  in the test suite.

## Dependencies and repo conventions

- Add: `feedparser`, `httpx`, `markdownify`.
- Register package under `[tool.hatch.build.targets.wheel] packages`; console
  script `ai-market-radar = "ai_market_radar.cli:main"`.
- Python >= 3.12, managed via `uv` (`uv run`, `uv add`).
- Pre-commit: black, flake8, isort, mypy, bandit all must pass.
- Do not commit `data/`, `.env`, or generated DBs (already gitignored).

## Milestone plan

1. Spec (this doc) — commit.
2. Dependency + package registration.
3. Package scaffold: models, sources.toml, fetchers, normalize, store, digest, cli.
4. Tests.
5. End-to-end trial scan against real sources; verify dedupe on a second run.
6. (Later, not in this milestone) LLM summaries, dashboard, scheduler.
