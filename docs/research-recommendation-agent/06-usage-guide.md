# Research Agent — Usage Guide

> **One-sentence core:** how to actually drive the MVP — the four CLI commands,
> where answers/cost/data live, and what to touch to extend it. The design
> docs (01–05) answer *why this shape*; this doc answers *how to run it*.

Part of the [Overview](./00-overview.md). Written against the MVP implemented
2026-09-20 (`src/research_agent/`), verified with a live run
(session `20260920-160231-068bec`, topic "1500 元以内的降噪耳机").

---

## 1. Prerequisites

- `LLM_API_KEY` in the repo-root `.env` (see `src/llm_client/settings.py`;
  DeepSeek-compatible defaults otherwise).
- Python env via uv; the package is installed editable, so either works:
  `uv run research-agent …` or `.venv/Scripts/python.exe -m research_agent …`.
- On Windows consoles, set `PYTHONIOENCODING=utf-8` — Chinese output is valid
  UTF-8 in the DB/reports but displays as GBK mojibake in a default console.

## 2. The loop, as a user sees it

```
new ──► (PLAN→COLLECT⇄REFLECT) ──► AWAIT_USER ──► answer ──► (DEEPEN→RECOMMEND) ──► DONE
                                       │ questions             │ report path printed
                                       │ printed to stdout     │
                                       └── process may die here ──┘  resume from SQLite
```

```console
$ research-agent new --topic "1500 元以内的降噪耳机推荐" \
    --context "主要通勤地铁用，戴眼镜，安卓手机" --max-calls 12
session 20260920-160231-068bec — researching…
...
1. 你更倾向哪种佩戴形态？
   为什么问：F17 和 F2 把半入耳 Redmi Buds 6S 的舒适与性价比单列，…
     [1.1] 半入耳式，如 Redmi Buds 6S：…
     [1.2] 入耳式真无线，如 华为 FreeBuds 6 / 索尼 WF-1000XM5：…
回答后运行: python -m research_agent answer 20260920-160231-068bec
```

```console
$ research-agent answer 20260920-160231-068bec
> 1.2
> 2.2
> 3.4
> 4.3
phase=DONE
report: data\research_agent\reports\20260920-160231-068bec.md
```

The pause is *state, not a live process*: kill the terminal between the two
commands and `answer` still works — it reloads the session from SQLite.

### Answer syntax (per question, one line each)

| Input | Meaning |
|---|---|
| `1.2` | question 1, option 2 |
| `2` (bare number) | same, when answering strictly in order |
| free text | off-axis evidence about taste — the profile builder reads it, not just option values |
| empty / `s` | skip → becomes a declared default assumption in the report |
| final prompt | global comment (e.g. "其实我更在意续航") |

### Other commands

- `status <session>` — phase + `stop_reason` (`budget`/`saturated`/… — a
  first-class output per 00 §4, not an error).
- `report <session>` — print the rendered markdown report.
- `new --fake <dir>` — collect from local fixture files instead of the web
  (same code path as tests; zero network, still uses the LLM).

## 3. Where the money goes (LLM only when necessary)

The user-level rule this implementation follows: **LLM calls happen only when
code cannot decide.** Concretely:

| Lever | Effect |
|---|---|
| `--max-calls N` | collector-call budget; on exhaustion the session *degrades* (recommendation with `stop_reason=budget`, confidence capped at medium) rather than dying |
| deduped capture | URL+content-hash hit → text fetched once, extraction *not* re-called |
| zero preference-gap candidates | `nothing_to_ask` → straight to RECOMMEND, no clarify LLM call |
| no user signal at all | equal-weight default profile built in code, no profile LLM call |
| score margin > 0.05 | adversarial re-rank skipped |
| `--context "…"` filled well | fewer decision-relevant gaps → fewer questions → cheaper DEEPEN |

A typical MVP run: 1 plan + N batch extractions + 1–2 reflects + 1 clarify +
1 profile + 1 score (+1 adversarial at most). The offline e2e test asserts
these call counts exactly (`ScriptedLLM.calls`) — if a change adds a call,
a test fails on purpose.

## 4. Where things live

| Path | What |
|---|---|
| `data/research_agent/agent.db` | sessions, captures, findings, artifacts, gap events, budget counters (override dir with `RESEARCH_AGENT_DATA_DIR`) |
| `data/research_agent/reports/<session>.md` | the deliverable; also printed by `report` |
| `src/research_agent/contracts/prompts/*.md` | the 7 phase prompts; bump `PROMPT_VERSION` when wording changes materially (it is stamped onto sessions) |
| `tests/test_research_agent/fixtures/laptops/` | offline fixture pages for `--fake` and the e2e test |

## 5. Extending it

- **New source** → one file implementing `SourceAdapter`
  (`search(q,k)->[Hit]`, `fetch(hit)->Fetched`) in
  `src/research_agent/research/adapters/`, register in `default_adapters()`.
  `local_kb` (reuse `ai_market_radar` SQLite) is the planned next adapter.
- **Better prompts** → edit the markdown; the JSON schema lives in
  `contracts/models.py` and is the validator itself (`chat_json(schema=…)`,
  one repair turn, then hard fail — malformed output never ships silently).
- **New invariants** → put them in pure code (see
  `clarifying/validator.py`, `research/normalizer.py` quote audit,
  `recommendation/scorer.py` citation audit), not in prompt prose.
  That is the whole architecture argument of 00 §1 against "upload the md
  files to Claude".

## 6. Known limits (MVP scope, per 00 §7 roadmap)

CLI only (FastAPI/Streamlit = v1); one clarification round; no findings FTS
(needs the `fold_cjk` trick from `file_manager`); no re-consult loop yet —
but the report's "什么情况会改变这个排名" section is designed as its entry
point; brand-alias merging is a small curated map, not world knowledge.

Next: back to [00-overview](./00-overview.md) for the design rationale.
