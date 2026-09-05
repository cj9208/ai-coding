# AI Market Radar — Pipeline Explained

> Companion doc to the code in `src/ai_market_radar/`. It walks through the whole
> process end to end, and explains the *tricks and techniques* used at each step —
> not just *what* happens, but *why* it is done that way.

---

## 1. What this is

A scanner that polls **official** sources of OpenAI, Anthropic, and GitHub Copilot,
stores every seen item in a durable local knowledge base (SQLite), and on each run
reports **only the items that are new since the last run** in a readable, tagged
digest.

Guiding requirements (from the design):

1. **No duplicate runs / no duplicated data** — every run must be idempotent.
2. **New-info-first** — old data is skipped automatically.
3. **Traceability** — every digest bullet points back to its source URL.
4. **Readability** — items are classified into thing-types (tags) and grouped so
   that "a new model shipped" or "prices changed" jumps out, instead of a wall of
   undifferentiated links.

### Big picture

```
 sources.toml          The registry: WHAT to watch (13 sources)
      │
      ▼
┌─────────────┐   kind = rss | markdown | html_news | html_snapshot
│  FETCH      │   per-source adapter → list of raw entries (title/url/date/excerpt/tags)
└─────┬───────┘
      ▼
┌─────────────┐
│ NORMALIZE   │   filter → classify signal + thing-type tag → compute fingerprint
└─────┬───────┘
      ▼
┌─────────────┐   INSERT OR IGNORE on unique fingerprint  → only NEW rows return
│  SQLITE KB  │   (data/ai_market_radar/kb.db)
└─────┬───────┘
      ▼
┌─────────────┐   group NEW items by company → tag sections
│   DIGEST    │   write digests/YYYY-MM-DD-HHMM.md
└─────────────┘
```

The four data files:

| path (default under `data/ai_market_radar/`) | purpose |
|---|---|
| `kb.db` | knowledge base: `items` (fingerprinted history) + `source_state` (per-source health) |
| `digests/YYYY-MM-DD-HHMM.md` | human-readable report of **that run's** new items |

---

## 2. Stage 0 — The registry (`sources.toml`)

Before any code was written, every candidate source was **manually probed** over
HTTP to verify it exists, is reachable, and is machine-readable. That research is
what the registry encodes. Each `[[source]]` entry carries:

```toml
[[source]]
key    = "openai_changelog"      # unique id, used for --source and source_state
name   = "OpenAI API Changelog"  # display name
entity = "openai"                # openai | anthropic | copilot  (the radar targets)
signal = "product"               # default signal: research | product | deal | status
kind   = "markdown"              # selects the fetcher adapter below
url    = "https://platform.openai.com/docs/changelog.md"
```

### Tricks

- **One declarative row instead of hard-coded crawlers.** Adding/removing a source
  is a config change, not a code change. `kind` drives which adapter runs, so a new
  kind can be added without touching the registry format.
- **Registry is TOML parsed with the stdlib `tomllib`** — no YAML dependency.
- **Filters live in the registry too** (`filter = { tags_include = [...] }`), e.g.
  the GitHub blog feeds carry many non-Copilot posts, so `github_blog` only keeps
  items whose title/category mentions Copilot or artificial intelligence.
- **Known gaps are explicit.** Research showed `openai.com/api/pricing` returns
  HTTP 403 (Cloudflare) to plain clients, so OpenAI pricing is *not* a snapshot
  source; its deal news is covered via RSS instead. The Anthropic pricing page and
  the GitHub Copilot plans page *are* snapshotable, and status pages expose RSS
  history. Sources you can’t parse cleanly are better left out than half-broken.

---

## 3. Stage 1 — Fetch (per-kind adapters)

All adapters live in `fetchers/`. They share a single `httpx.Client`
(`fetchers/base.py`) configured with a browser-like User-Agent, timeouts, and
redirect-following — polite, reliable HTTP.

| kind | used by | what it fetches |
|---|---|---|
| `rss` | OpenAI feeds/status, Anthropic status, GitHub blog/changelog | a feed (RSS/Atom) |
| `markdown` | OpenAI changelog & models, Anthropic release notes, Claude Code changelog | one `.md` document |
| `html_news` | Anthropic news index | an index page + selected article pages |
| `html_snapshot` | pricing / plans pages | one HTML page |

Every fetcher returns **`list[RawEntry]`** — a normalized, adapter-agnostic record
(`title`, `url`, `published_at`, `excerpt`, `tags`, `hash_value`). Downstream code
never touches raw HTML/XML/feed internals.

### 3.1 RSS (`fetchers/rss.py`) — the easy case

`feedparser` does most of the work.

Tricks:

- **`bozo` guard**: `feedparser` sets `bozo` when a feed is malformed. We only raise
  if there are also **zero** entries — a mildly-broken feed with parseable entries
  still yields data rather than failing the source.
- **Two timestamp fallbacks**: use `published_parsed`, falling back to
  `updated_parsed`, converted to **UTC ISO-8601** immediately so downstream sorting
  and rendering never deal with feed-local formats.
- **Excerpt cleanup**: RSS summaries are HTML; we strip tags and collapse
  whitespace *once*, here, so the rest of the pipeline handles clean text.
- **Tags surfaced**: feed categories are kept on the entry so the registry filter
  can decide whether the item is in-scope (Copilot-specific posts).

### 3.2 Markdown changelogs (`fetchers/markdown.py`) — the trickiest parser

OpenAI’s docs and Anthropic’s docs run on **Mintlify**, which conveniently serves a
clean Markdown version of any page just by appending `.md`
(`platform.openai.com/docs/changelog.md`, `docs.anthropic.com/en/release-notes/api.md`).
Claude Code’s changelog is a raw GitHub Markdown file. So we parse Markdown, not
HTML — one of the biggest wins of the source research.

The parser (`parse_changelog`) must handle **three different document layouts**:

```text
OpenAI:        ## September, 2026          ← month group (level 2)
                   ### Sep 3               ← partial date (level 3)
Anthropic docs:    ### September 3, 2026   ← full date (level 3)
Claude Code:    ## 2.1.261                 ← semver version (level 2)
```

Tricks:

- **Token-based date sniffing, not format guessing.** `_date_parts` splits a
  heading into tokens and looks for a 4-digit year, a month name/abbreviation, and
  a day number. This handles “September 3, 2026”, “Sep 3”, and “2026-09-03” with
  one code path. It only *returns* a date if a real month name appears
  (`_FULL_MONTH_RE`), which prevents random headings like “2.1.261” or “gpt-5.1”
  from being misread as dates.
- **Context stack for partial dates.** In the OpenAI layout, `## September, 2026`
  has a month+year but no day, so it is treated as an enclosing *group*, not an
  entry, and stored as `(month, year)` context. A later `### Sep 3` (month+day but
  no year) is resolved against that context into a real date.
- **Heading-level heuristics.** Version headings are only accepted at level ≤ 2,
  dated entries at level ≥ 3, and month groups at level ≤ 2. These rules map
  directly to the three observed layouts and keep structural headings from being
  confused with entries.
- **`close()` on boundary.** When a new dated/version/group heading appears, the
  current entry is finalized and pushed — a simple streaming state machine, no
  tree building.
- **Whole-document fallback.** Some pages have no dated entries at all (e.g. the
  OpenAI *models reference*, which is a table of model cards). If the parser finds
  zero entries it emits **one** item whose identity is the SHA-256 of the whole
  page, so a models page edit still surfaces as a change.
- **Identity vs. display are decoupled.** The entry fingerprint is derived from
  `heading + raw body` (stable), while the *displayed* excerpt goes through
  `_plain_text` to strip markdown (`**bold**`, backticks, `[label](url)` links,
  bullet markers). This is deliberate: improving how we *display* content later can
  never change an item’s identity and re-report old history.
- **Titles come from the structure.** A leading `**Bold label**:` on the first body
  line becomes the title; otherwise the resolved date is used. Titles are anchor-
  regex constrained so mid-sentence bold words are not mis-captured, and markdown
  links inside titles are collapsed to their labels.

### 3.3 HTML news (`fetchers/html_news.py`) — scraping without a browser

Anthropic has **no public RSS**, and its `/news` page is a Next.js app. Rather than
attempting to parse the obfuscated flight/JS payload, the fetcher uses three
server-rendered facts:

1. The index HTML contains plain `<a href="/news/<slug>">` links.
2. Each article page serves real `<meta property="og:title">` / `og:description>`.
3. Everything is reachable without JavaScript.

Tricks:

- **Two-phase, dedupe-first.** Fetch the index → collect unique slugs → compare
  against slugs already in the knowledge base → **only then** fetch the article
  pages of unseen slugs to read their og-meta. History is never re-fetched.
- **Bounded enrichment.** Article fetches are capped (default 25 new pages/run) so
  the very first backfill stays polite and fast; each later run only enriches the
  small number of genuinely new articles.
- **Origin-based URL construction.** Slugs are absolute paths (`/news/xyz`), so the
  URL is built from the *site origin* — this fixed a real bug where a previous
  version concatenated the path onto `…/news`, producing broken `/news/news/xyz`
  links.
- **HTML entity unescape** on og-meta values (they contain `&#x27;`, `&amp;`, …).
- **Fail loudly, not silently.** If the index structure changes and zero slugs are
  found, the source raises instead of quietly reporting “0 items” — a scanner that
  silently returns nothing is worse than one that flags itself broken.

### 3.4 HTML snapshot (`fetchers/html_snapshot.py`) — content-addressed diffing

Pricing/plans pages (Anthropic pricing, GitHub Copilot plans) have no feeds and no
dates. Trick: **treat the whole page as one versioned document**.

- Fetch the page, strip `<script>/<style>/<nav>/<header>/<footer>` blocks, convert
  to readable text.
- Compute `sha256(cleaned_text)` as the item’s identity.
- If the hash equals a previously stored item, nothing is inserted. If the page
  text changed (a price bump, a new plan), a **new** item appears automatically —
  the "deal signal". No diffing library needed; content-addressing gives you a
  change detector for free.

---

## 4. Stage 2 — Normalize (`normalize.py`, `tags.py`)

A `RawEntry` becomes a full `Item` with three derived attributes plus a fingerprint.

### 4.1 Filtering

`_passes_filter` keeps an entry only if one of the source’s `tags_include` words
appears in the title **or** the feed’s category tags. Matching on both surfaces is
more forgiving than categories alone (some feed entries lack category tags).

### 4.2 Signal classification (`classify_signal`)

`signal` is the *coarse* reason to care: research / product / deal / status.

- The registry provides the default.
- Keyword rules then refine: strong money words (`price`, `per month`,
  `subscription`, `credit`, `free tier`, `$…`) upgrade a product-default item to
  `deal`; on a product feed, research-y titles (`paper`, `benchmark`,
  `reasoning model`, `model card`) upgrade to `research`.
- `html_snapshot` sources are hard-assigned to `deal`.

### 4.3 Thing-type tags (`tags.py`) — the readable axis

Signal alone produced poor section headers (“Product changes” was a dumping
ground). So each item also gets one **thing-type tag**:

```
model      pricing     feature     research     security     company     ecosystem     status
```

The rules are ordered by precedence, which is the real trick — *classification is a
decision funnel*:

```text
1. html_snapshot source                      → pricing   (hard rule)
   status source                             → status    (hard rule)
2. money words                              → pricing
3. "powered by"                             → ecosystem (it's a customer-built thing)
4. launch verb + STRONG model id (gpt-5,     → model
   claude-4, o3, opus 4.1, …)
5. launch verb + weak family word (gpt,      → model, but only if no "noise" word
   codex, claude, …)                           like store/api/sdk/spec/devday/program
6. security/safety words                     → security
7. research words (paper, arxiv, benchmark)  → research
8. "How …", partnerships                     → ecosystem
9. company/org news words                    → company
10. product/feature verbs                    → feature
11. else → signal-based fallback
```

Why the strong/weak split and the noise list? Because naive keyword matching over-
tagged things like “Introducing the GPT **Store**”, “Introducing improvements to
the fine-tuning **API** and custom **models** program”, and “Introducing the Model
**Spec**” as model releases. Requiring an explicit launch verb plus either a
*versioned* model name, or a *bare* family name that is not adjacent to
product-context words, keeps “New models” honest. In the same spirit `security`
beats `model` when a title is really about safeguards, and customer stories
(`How X builds…`, `…powered by GPT-4o`) are routed to `ecosystem`, not `model`.

Rules live in one module and are unit-tested against a table of real titles
(`tests/test_ai_market_radar/test_tags.py`).

### 4.4 Fingerprints (`fingerprint_for`, `fingerprint_url`) — the dedupe core

Two fingerprint families, chosen by source kind:

- **URL-based** (`url:<host><path>`) for RSS and news articles — after normalizing
  host to lowercase and dropping the fragment, query string, and trailing slash.
  Trick: because OpenAI news and OpenAI blog feeds contain the *same* articles,
  and GitHub may cross-post, the URL fingerprint makes the **first** source that
  sees an article win and the second report **0 new** — cross-source dedupe for
  free.
- **Content-hash** (`hash:<sha256>`) for markdown changelog entries (hash over
  heading + body) and snapshots (hash over the page text). Trick: content hashing
  means that when a changelog entry or pricing page is *edited*, the changed item
  is detected as new; nothing needs to remember offsets.

The raw hash is also stored separately (`raw_hash`), so a future "what changed"
view could diff old vs new excerpts.

---

## 5. Stage 3 — Knowledge base (`store.py`)

SQLite, no ORM, single file.

- Schema (`items`): unique `fingerprint`, plus `source_key`, `entity`, `signal`,
  `tag`, `title`, `url`, `published_at`, `excerpt`, `raw_hash`, `fetched_at`.
  `source_state` tracks per-source `last_ok_at`, `last_items`, and `last_error`.
- **Idempotency**: `INSERT OR IGNORE` on the unique fingerprint. A re-run inserts
  nothing for already-seen items; `insert()` returns `True` only when a row was
  genuinely new — that return value is exactly what feeds the digest.
- **Pragmatic migration**: `_init_schema` runs `PRAGMA table_info` and issues an
  `ALTER TABLE … ADD COLUMN tag` when the `tag` column is missing, so an existing
  database upgrades in place instead of requiring a rebuild. (This happened once —
  the v1 KB gained the tag column without losing history.)
- **WAL journal mode** for safer concurrent-ish reads.
- `data/` is gitignored: the KB and digests are derived data, never committed.

---

## 6. Stage 4 — Digest & presentation (`digest.py`)

- Only the **newly inserted** items of the run are rendered.
- Grouping is `company → thing-type tag`, using a fixed section order that encodes
  *importance* (New models → Pricing → Features → Research → Security → Company →
  Customers/partnerships → Status), so high-signal items are always at the top and
  long-tail noise sinks to its own labeled section at the bottom instead of mixing
  in.
- Each bullet: `- **Title** <url> · date — excerpt (~160 chars)`.
- **Per-run files, minute precision**: digest names are
  `digests/YYYY-MM-DD-HHMM.md` (UTC). Running the radar several times a day —
  which is the plan — produces one distinct, dated, timestamped report per run
  instead of merging everything into a single daily file.
- **Same-minute idempotency**: if a run is repeated within the same minute and the
  content is byte-identical, the file is not rewritten.
- File encoding is explicitly UTF-8 so the markdown renders correctly regardless
  of the OS locale.

---

## 7. Orchestration (`cli.py`)

The CLI is deliberately a thin loop over the stages:

1. Parse flags: `--data-dir`, `--source KEY` (repeatable), `--list-sources`,
   `--max-articles`.
2. Load the registry, select enabled sources (or the requested keys).
3. Open one HTTP client **and one KB** for the whole run; load the set of already-
   known URL fingerprints **once** (used by `html_news` to skip known articles).
4. Per source: fetch → normalize → insert; accumulate truly-new items.
5. **Failure isolation**: each source runs in its own `try/except`. A timeout or a
   403 on OpenAI news logs an error into `source_state`, prints `fail […]`, and
   the remaining eleven sources still complete. One broken source never sinks the
   run.
6. After the loop: write the digest (if there are new items) and print a compact
   per-company/signal summary.

```
$ uv run ai-market-radar                     # everything
$ uv run ai-market-radar --source openai_news --source copilot_plans   # subset
$ uv run ai-market-radar --list-sources
```

---

## 8. Failure modes & how the design handles them

| scenario | technique |
|---|---|
| Feed/XML malformed but entries present | `bozo` + `entries` guard (3.1) |
| JS-rendered site with no RSS (Anthropic) | server-rendered links + og-meta, no browser (3.3) |
| Anti-bot 403 on OpenAI pricing pages | excluded at registry level; news RSS covers deals (Stage 0) |
| Same article in two feeds | URL fingerprint → second source reports 0 new (4.4) |
| Changelog entry edited / page text changed | content-hash identity → edited item re-surfaces (3.2, 3.4, 4.4) |
| Changelog display parser improved later | identity decoupled from display text → no history re-report (3.2) |
| Index page layout changes | zero-slug detection raises, flagged not silent (3.3) |
| One source down / transient DNS (getaddrinfo) | per-source try/except + `source_state.last_error` (7) |
| Running several times a day | per-minute digest files + idempotent inserts (5, 6) |
| Existing v1 database needs new column | `PRAGMA table_info` + `ALTER TABLE` migration (5) |

---

## 9. Operational notes

- **First run is a backfill**: expect ~2,000+ new items and one large digest. From
  the second run onward output is small because nothing is duplicated — re-run the
  same command and you’ll see `No new items.`
- Digests live under `data/ai_market_radar/digests/`. All of `data/` is
  gitignored.
- Tests (`tests/test_ai_market_radar/`) are offline — parser fixtures, taxonomy
  tables, digest rendering, store dedupe, registry load. Live scans are verified
  manually. Lint via the repo’s pre-commit hooks (black, flake8, isort, mypy,
  bandit).

## 10. Intentionally left out (for later)

- LLM-written summaries (hook exists in the KB: excerpt + raw_hash are stored so a
  summarizer can run over *new* items only).
- A Streamlit dashboard over the KB.
- Scheduling (Windows Task Scheduler / cron wrapping `uv run ai-market-radar`).
- More entities (e.g. Google Gemini coding, Meta, xAI) — each is just a new
  `[[source]]` row plus, if needed, a new fetcher kind.
