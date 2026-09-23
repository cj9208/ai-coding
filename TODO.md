# TODO

Loose ends worth picking up, newest first. Anything here should be deleted once
done rather than left as archaeology — the code and git history are the source
of truth for completed work.

Swept 2026-09-22: the RAG incremental-redesign decisions closed (the 5k
measurement ran at 50k instead — `docs/rag/04-scaling.md` §"Benchmark results";
the four remaining questions are recorded as resolved-as-deferred in
`docs/rag/05-incremental-design.md` §Open questions), and the abandoned root
`openspec-skills/` clone is gone along with its `.gitignore` line.

## RAG scalable validation — the uncoded phases (`docs/rag/06-scalable-validation.md`)

- [ ] **Phase 2 — LLM perplexity scoring**: a batch job that computes per-block
      perplexity through `llm_client` (deliberately separate from ingest, so
      ingest stays deterministic), a corpus-level statistics module
      (mean/stddev/outlier detection), and a `perplexity_outlier` flag in
      `validate.py`. Phase 1 (seven additional zero-cost deterministic checks)
      has shipped; the price on the table is ~$85 per 100k docs.
- [ ] **Phase 3 — Sampled QC**: document strata from the Layer 1 metrics,
      ISO 2859 sampling with switching rules, a review queue (own UI or an
      `ocr-review` extension), active-learning prioritization. Gate: not before
      Phase 2 has been calibrated against real reviews.
- [ ] **Phase 4 — Drift detection**: quality-metric time series in the DB, SPC
      rules, periodic re-calibration of the Layer 1 thresholds from accumulated
      reviews.
- [ ] **peak RSS was never measured**: the 50k run recorded ingest/publish
      timings, DB size, latency percentiles and hit_rate
      (`scripts/rag_bench_run.py --tiers 50000` reproduces them), but not
      memory. Add a sampler to the harness only if ingest RSS becomes the
      binding constraint.
- **Explicitly not owed:** the vector path (M2). The benchmark gives
  hit_rate@5 = 1.0 at 250k chunks, so the P99 tail (850 ms) is a latency
  problem, not a recall problem — vectors need an eval-shown lexical miss, not
  a slow percentile. Levers and ordering are already in `04-scaling.md`
  §"Query-side optimization options".

## Verification owed (blocked on this machine)

- [ ] **Build the OCR runner image and run one real parse** (Docker Desktop,
      WSL2 backend): `ocr-backend container build` →
      `ocr-backend container download paddleocr-vl-1.6` →
      `ocr-backend container parse data/ocr_backend/in/<file>.pdf`.
      Every Docker file in this repo is so far unexecuted — `uv.lock` resolves
      the Linux wheels, but nothing proves the build succeeds or that
      `pdf2image` finds Poppler inside the image.
      `docs/service-containerization-exploration.md` §8 items 3.
      (Still blocked 2026-09-22: no `docker` on this shell's PATH.)
- [ ] **GPU image on a Windows + WSL2 + NVIDIA host**: confirm the cu126 wheel
      combo actually loads on-device, and record model load time, per-page
      latency and peak VRAM. A GPU run that silently falls back to CPU is the
      failure to rule out. §8 item 4.
- [ ] **`ocr-backend container` tests fake `subprocess.run`**, so the real
      `docker compose` argument shape is untested. Re-check the built command
      against a live Docker once the first build passes.

## Known gaps in what shipped

- [ ] **Two scheduler proofs still owed (notify's own round is registered and
      running).** `notify schedule install` registered the `notify-dispatch` task
      on 2026-09-23 and its first unattended round delivered an alert to a real
      phone with nobody typing `dispatch` — evidence in
      `docs/notify-design.md` §4.9. What that does *not* prove:
      (a) **sleep-wake catch-up** — close the lid for 20 minutes, wake it, and
      confirm `data/notify/dispatch.log` shows `StartWhenAvailable` replaying the
      missed rounds (needs a physical action; until then catch-up is paper logic);
      (b) **the long-running tasks are still not scheduled at all** — `quant
      record` / `ai-market-radar scan` are background-shaped but human-launched,
      which is also why `config/notify/expectations.yaml`'s one silence rule
      ships `enabled: false`. `notify schedule` wraps only the dispatch task, so
      (b) is its own wrapper decision (a different task, different executable,
      different log), not a flag on this one.
- [ ] **ocr-review: one real mouse drag-to-add-block in a visible browser
      window.** Everything else (pixel alignment, corrected/rejected/added
      blocks, save/409, export) was verified end-to-end, but the headless
      browser here can only dispatch synthetic pointer events — start
      `uv run ocr-review serve` and draw one box on http://127.0.0.1:8765 to
      close the gap.
- [ ] **quantdesk recorder: one real liquidation row.** Funding and open
      interest were recorded live 2026-09-22, but this machine's network path
      to `wss://fstream.binance.com` connects and answers pings while pushing
      zero data frames (spot WS and fapi REST work fine), so the
      `!forceOrder@arr` positive path is proven only by a fake-WS test; the
      silence itself is detected and logged as a gap. Re-run
      `uv run quant record --streams liquidations --minutes 10` from a network
      where futures WS works and check `data/quantdesk/recorded/liquidations/`
      is non-empty.
- [ ] **photo_desk: one real HEIC read + one NAS-mounted scan (now incl. M1
      triage).** M0+M1 are verified on generated JPEG trees only: this machine's
      pillow-heif wheel decodes but cannot encode, so no `.heic` fixture exists,
      and `PHOTO_ROOT` has never pointed at a live Synology share. After the NAS
      arrives: `uv run photos scan --root <mounted> --data-dir <out>` over a
      few hundred real photos (check EXIF/GPS parse and first-scan wall clock
      over SMB), open one iPhone burst in the timeline and confirm HEIC
      thumbnails render, then run one real `photos triage` dry-run and eyeball
      the ranking against a human pick. This also closes the burst-identifier
      field check (`docs/photo-desk-design.md` §6-3): grouping level 1
      (`burst_id`) ships as constant None until a real Apple BurstIdentifier is
      confirmed readable, so on-device bursts currently lean on the
      EXIF-millisecond rule — verify it actually fires on iPhone bursts.

## Next, if the demand is real

- [ ] file_manager → OCR integration (block-level contract for "search hit
      located on page / bbox"): `docs/ocr-backend-design.md` §7 step 4.
- [ ] OCR HTTP service: only once several callers share one GPU or need job
      status. Gate and rationale in
      `docs/service-containerization-exploration.md` §7.
