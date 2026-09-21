# RAG Subsystem — Scalable Validation

Status: **Phase 1 implemented** (expanded deterministic checks). Phase 2
(perplexity scoring) and Phase 3 (sampled QC) are design-only — not yet
coded. This document records the research, the four-layer architecture,
and the concrete implementation plan.

## Big picture

```text
                        the four layers of trust
 ┌──────────────────────────────────────────────────────────────────┐
 │                                                                  │
 │  Layer 1: deterministic proxy checks (zero cost)                 │
 │  ┌────────────────────────────────────────────────────────────┐  │
 │  │ text density │ read order │ table struct │ page numbers    │  │
 │  │ char dist    │ ngram ent  │ bbox overlap │ formula parse   │  │
 │  │ table cells  │ empty blks │ block scores │                 │  │
 │  └────────────────────────────────────────────────────────────┘  │
 │                        ▼ all docs, every time                     │
 │  Layer 2: LLM perplexity scoring (~$85 / 100k docs)              │
 │  ┌────────────────────────────────────────────────────────────┐  │
 │  │ per-block token log-prob via cheap model (DeepSeek Flash)  │  │
 │  │ → corpus-level anomaly detection (outlier documents)       │  │
 │  └────────────────────────────────────────────────────────────┘  │
 │                        ▼ all docs, batch job                      │
 │  Layer 3: sampled QC (ISO 2859 / stratified)                     │
 │  ┌────────────────────────────────────────────────────────────┐  │
 │  │ ~1,000 docs sampled (stratified by complexity)             │  │
 │  │ → human review of sample → estimate corpus defect rate     │  │
 │  │ → switching rules auto-adjust scrutiny (tighten/relax)     │  │
 │  └────────────────────────────────────────────────────────────┘  │
 │                        ▼ sample only                              │
 │  Layer 4: human review (escalated)                               │
 │  ┌────────────────────────────────────────────────────────────┐  │
 │  │ active-learning priority queue                              │  │
 │  │ → reviewed samples calibrate Layer 1+2 thresholds          │  │
 │  │ → drift detection (SPC) triggers re-review                 │  │
 │  └────────────────────────────────────────────────────────────┘  │
 │                        ▼ escalated only                           │
 └──────────────────────────────────────────────────────────────────┘
```

The core insight: **you do not need to review all 100k documents.** A
random sample of ~1,000 documents gives a statistically valid estimate
of corpus-level quality (95% confidence, +/-3% precision). The rest is
automated — deterministic checks catch structural failures, perplexity
catches semantic incoherence, and the human gate is reserved for the
documents that need it most.

## Why the current trust model does not scale

The M1 trust model (`01-design-rationale.md` §"Deviations") is binary
and human-centric:

```text
OCR backend (no recognition confidence, layout scores only)
  → optional human review (review.json sidecar)
  → validate.assess() deterministic proxy checks
  → publish_decision: pass / pass_with_warning / quarantine / fail
  → reviewed docs escape quarantine; unreviewed docs with 2+ high flags quarantine
```

The `reviewed` boolean is the strongest trust signal. At 100k documents,
per-doc human review is the bottleneck — it was never going to survive
(`04-scaling.md` §7). The trust model must shift from "review every
document" to "review a sample, automate the rest, calibrate from
feedback."

## Layer 1: expanded deterministic checks

**What shipped in Phase 1.** The original `validate.py` had three checks
(text density, reading-order coverage, table structure). Phase 1 adds
seven more, all zero-cost (no LLM, no external dependency):

| Check | Flag | Severity | Catches |
|---|---|---|---|
| *existing* `low_text_density` | high | blank / near-blank pages |
| *existing* `reading_order_sparse` | medium | layout detection failure |
| *existing* `table_unstructured` | high (per table) | table recognition failure |
| `page_number_non_monotonic` | high | missing/duplicated pages, order breaks |
| `char_distribution_anomaly` | medium | encoding errors, mojibake, symbol floods |
| `low_ngram_entropy` | medium | OCR hallucination loops ("the the the") |
| `bbox_overlap` | high (per overlap) | layout detection failure (blocks overlap) |
| `table_cell_inconsistent` | medium (per table) | broken table structure (uneven rows) |
| `formula_unparseable` | low (per formula) | malformed LaTeX |
| `empty_blocks` | medium | layout detected but nothing recognized |

Severity rules (unchanged shape, more signals):

```text
missing_source_hash or no_text                        → fail
2+ high flags, or 1 high + reading_order_sparse       → quarantine (or pass_with_warning if reviewed)
any flags but not severe                              → pass_with_warning
clean                                                 → pass
```

### Why these checks

Each check targets a distinct failure mode of VLM-based OCR:

- **Page number monotonicity**: catches missing/duplicated pages — a
  layout-level failure that text-density checks miss (a page with a
  page number but no body text still passes text density if other pages
  are dense enough).

- **Character distribution anomaly**: VLMs sometimes produce encoding
  errors (mojibake) or symbol floods when the input is degraded. A
  simple CJK/Latin/digit/symbol ratio, compared against the corpus
  median, catches these without any model.

- **N-gram entropy**: OCR hallucination loops (the model "sees" text
  that isn't there and repeats it) produce abnormally low n-gram
  entropy. Character-level 4-gram entropy < 2.0 is a strong signal.

- **Bbox overlap**: the OcrDocument contract guarantees pixel-space
  bboxes. Two body blocks with significant IoU overlap means the layout
  detector assigned the same pixels to two different blocks — a
  structural failure that downstream chunking cannot recover from.

- **Table cell consistency**: the existing `table_unstructured` check
  asks "does this look like a table at all?" (has `<table` or `|`).
  The new check goes deeper: "do the rows have consistent column
  counts?" — catches tables that have structure markers but are still
  broken.

- **Formula parseability**: LaTeX formulas with unbalanced braces or
  empty content are almost certainly recognition failures.

- **Empty content blocks**: a layout box was detected but the VLM
  produced no text — either a false positive or a recognition failure.

### Why these severities

High severity is reserved for checks that indicate **fundamental
structural failure** — the document's geometry is wrong (overlapping
bboxes, non-monotonic page numbers). These documents produce garbage
chunks no matter what.

Medium severity is for checks that indicate **degraded quality** — the
document is usable but likely has errors (character anomalies, low
entropy, inconsistent tables). These documents benefit from human
review but are not automatically quarantined.

Low severity (formula_unparseable) is informational — a few broken
formulas do not sink a document, but many do (the flag count still
contributes to the pass_with_warning threshold).

## Layer 2: perplexity scoring (designed, not implemented)

**The idea.** arXiv 2505.00746 ("Entropy Heat-Mapping") demonstrates
that LLM token-level log-probabilities can localize OCR errors without
ground truth: high-entropy regions correlate strongly with recognition
failures.

**Implementation plan.** Batch-compute per-block perplexity via
DeepSeek Flash (~$0.14/1M input tokens). For 100k docs at ~10 pages
each, ~500 tokens per page: 500M input tokens = ~$70 total. Store as
`quality["mean_perplexity"]` alongside existing proxy metrics.

**How it enters the trust decision.** Documents with mean perplexity
> 2 sigma above corpus mean, or any single block > 4 sigma, get flagged
as `perplexity_outlier`. This is a medium-severity flag — it does not
quarantine by itself, but it contributes to the pass_with_warning
threshold and feeds the active-learning priority queue (Layer 4).

**Why not yet implemented.** Requires an LLM call per document during
ingest. The deterministic checks (Layer 1) catch the most egregious
failures without any cost. Perplexity scoring earns its keep only after
Layer 1 thresholds are calibrated against reviewed samples — otherwise
we'd be adding a noisy signal without knowing its baseline.

## Layer 3: sampled QC (designed, not implemented)

**The idea.** Instead of reviewing every document, review a
statistically valid sample. ISO 2859-1 (Acceptable Quality Limit)
provides the sampling tables and switching rules.

**Sample size.** For N=100,000, 95% confidence, +/-3% precision:
n = (1.96² × 0.5 × 0.5) / 0.03² ≈ 1,067 documents.

**Stratified sampling.** Not all documents are equal. Stratify by
complexity:

| Stratum | Expected share | Sample allocation |
|---|---|---|
| Simple (single-column, text-only) | 60% | 200 |
| Moderate (mixed content, some tables) | 30% | 400 |
| Complex (dense tables, formulas, multi-column) | 10% | 400 |

Over-sampling the high-risk strata gives tighter confidence intervals
where quality problems are most likely.

**Switching rules (ISO 2859).** The powerful part: the system
auto-adjusts scrutiny based on observed quality.

```text
Normal inspection (default)
  → Tightened: if 2 of 5 consecutive batches rejected
  → Reduced: after 10 consecutive batches accepted

Tightened inspection (larger samples)
  → Normal: after 5 consecutive batches accepted
```

**How it enters the trust decision.** A document that passes Layer 1
checks but is selected for sampled QC gets held until the sample is
reviewed. If the sample passes (defect rate below AQL), the document
is published with `trust_tier = "silver"`. If the sample fails, the
document goes to Layer 4 (human review queue).

**Why not yet implemented.** Requires a review queue UI, a sampling
engine, and a definition of "defect" (which is itself a calibration
problem). Layer 1 must be calibrated first — otherwise the sampling
stratification has no basis.

## Layer 4: feedback loop (designed, not implemented)

**Calibrating thresholds from reviewed samples.** Every human-reviewed
document (via `ocr-review`) is a labeled data point: the review tells
us which blocks were wrong. Correlate the automated quality metrics
(Layer 1 + Layer 2) with human judgment:

```text
P(human_finds_errors) = sigmoid(w1 × text_density + w2 × perplexity + ...)
```

After 100+ reviewed samples, fit the logistic regression. After 500+,
the thresholds stabilize. This is how the system *learns* what "good"
looks like for this specific corpus and OCR backend.

**Active learning: which documents to prioritize for review.**

```text
review_priority = (
    anomaly_score × 0.3           # corpus outlier (Layer 2)
    + (1 - quality_score) × 0.3   # low automated quality (Layer 1)
    + stratum_weight × 0.2        # complex documents are more informative
    + recency × 0.1               # recent documents (drift detection)
    + uncertainty × 0.1           # model is uncertain about this type
)
```

Every human review should maximally reduce uncertainty about corpus
quality. Reviewing a document that the automated system is uncertain
about is more informative than reviewing one it's confident about.

**Drift detection (SPC).** Track quality metrics over time (by
week/batch). Apply statistical process control rules:

- Western Electric rules: flag if 3 of 5 consecutive points > 1 sigma,
  or 8 consecutive points on one side of the mean.
- CUSUM: cumulative sum chart detects small persistent shifts.

If drift is detected: halt auto-publishing for the affected document
type, increase sampling rate (ISO 2859 switching to tightened
inspection), investigate root cause.

## Multi-tier trust model (future)

The current `PublishDecision` enum has four values. The scalable model
adds a `trust_tier` dimension:

| Tier | Meaning | How earned |
|---|---|---|
| Gold | Human-reviewed | `reviewed=True` (existing) |
| Silver | High automated score + passed sampled QC | Layer 1 + Layer 3 |
| Bronze | Moderate automated score, not sampled | Layer 1 only |
| Unverified | Low automated score, not reviewed | Layer 1 flagged |

Downstream consumers (RAG query, answering) can weight results by
tier: a Gold chunk is cited preferentially over a Bronze chunk. The
`trust_level` field on `Chunk` already carries the document's
publish decision; adding `trust_tier` is a compatible extension.

## Implementation phases

**Phase 1 (shipped).** Expand `validate.py` with seven new deterministic
checks. Zero cost, zero dependencies. Catches the most egregious
structural failures. Tests cover each check in isolation and in
combination.

**Phase 2 (next).** Add perplexity scoring via `llm_client`. Requires:
- a batch job that computes per-block perplexity (separate from ingest,
  so ingest stays deterministic);
- a corpus-level statistics module (mean, stddev, outlier detection);
- a new `perplexity_outlier` flag in `validate.py`.

**Phase 3 (after Phase 2 is calibrated).** Implement sampled QC:
- define document strata (simple/moderate/complex, based on Layer 1
  metrics);
- implement ISO 2859 sampling with switching rules;
- build a review queue (separate UI or extend `ocr-review`);
- implement active-learning prioritization.

**Phase 4 (ongoing).** Drift detection and continuous improvement:
- track quality metrics time series in the DB;
- apply SPC rules;
- periodically re-calibrate Layer 1 thresholds from accumulated reviews;
- adjust sampling rates based on observed quality trends.

## References

- arXiv 2505.00746: "Entropy Heat-Mapping: Localizing GPT-Based OCR
  Errors" — LLM perplexity as OCR error locator.
- arXiv 2604.06160: "SpACER: Decomposable errors for page-level OCR
  evaluation" — spatial awareness as a lower bound on CER.
- ISO 2859-1: Acceptable Quality Limit (AQL) — sampling tables and
  switching rules for acceptance sampling.
- EDRM: "Statistical Sampling Applied to Electronic Discovery" —
  practical guide to sampling for quality estimation.
- British Library OCR Quality Assessment — stratified sampling by
  document type, historical mass-digitization lessons.
