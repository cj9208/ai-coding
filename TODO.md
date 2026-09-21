# TODO

Loose ends worth picking up, newest first. Anything here should be deleted once
done rather than left as archaeology — the code and git history are the source
of truth for completed work.

## RAG incremental redesign — decisions owed (`docs/rag/05-incremental-design.md` §Open questions)

- [ ] **P0 — Run the 5k-doc synthetic measurement** (prerequisite, not an open
      question, but three of the four below decay into guesswork without it):
      generate ~5k bundles, time `stage vs publish vs projection`, record DB
      size and peak RSS; this is also the before/after baseline that justifies
      the schema surgery in 04.
- [ ] **P1 — Eval policy over a partially-enriched corpus**: while an enrich
      backfill is mid-sweep (`partial@v3` in `representations`), does `rag eval`
      score the lexical path against the new prompt version, pin the old
      `ready@v2`, or score both and compare? eval is the designated referee for
      every retrieval decision — including whether the next prompt version earns
      corpus-wide status — so this must be answered *before* the first full
      enrich sweep ships, and the golden set's next revision carries it.
- [ ] **P2 — Publish trigger for the daily path**: fixed schedule vs backlog
      threshold vs both. Only bites once production cron exists and ocr-review
      starts landing corrections mid-day; decide when wiring the first cron,
      not when writing `publish_diff`.
- [ ] **P2 — `inferred` hydration at assembly**: join-time lookup (one extra
      query per assemble) vs denormalized FTS-covering columns. Decide from the
      P0 profile; until then implement the simple lookup — enrich is opt-in and
      the lexical-only path doesn't touch this column.
- [ ] **P3 — Snapshot retention: N full manifests vs delta chain**: the design
      assumes N full manifests (daily publish × 100k manifest rows = years of
      headroom); revisit only at first ops data showing the copy is a cost.

## Verification owed (blocked on this machine)

- [ ] **Build the OCR runner image and run one real parse** (Docker Desktop,
      WSL2 backend): `ocr-backend container build` →
      `ocr-backend container download paddleocr-vl-1.6` →
      `ocr-backend container parse data/ocr_backend/in/<file>.pdf`.
      Every Docker file in this repo is so far unexecuted — `uv.lock` resolves
      the Linux wheels, but nothing proves the build succeeds or that
      `pdf2image` finds Poppler inside the image.
      `docs/service-containerization-exploration.md` §8 items 3.
- [ ] **GPU image on a Windows + WSL2 + NVIDIA host**: confirm the cu126 wheel
      combo actually loads on-device, and record model load time, per-page
      latency and peak VRAM. A GPU run that silently falls back to CPU is the
      failure to rule out. §8 item 4.
- [ ] **`ocr-backend container` tests fake `subprocess.run`**, so the real
      `docker compose` argument shape is untested. Re-check the built command
      against a live Docker once the first build passes.

## Known gaps in what shipped

- [ ] **Delete the leftover root `openspec-skills/` clone** (and its
      `.gitignore` line). Skills now have one home — `repo-skills/` — but the
      old clone could not be moved: a process (GitKraken?) held a handle, so
      `repo-skills/openspec-skills/` was re-cloned from the same public remote
      instead. The abandoned copy is harmless but now shadows the rule.
- [ ] **ocr-review: one real mouse drag-to-add-block in a visible browser
      window.** Everything else (pixel alignment, corrected/rejected/added
      blocks, save/409, export) was verified end-to-end, but the headless
      browser here can only dispatch synthetic pointer events — start
      `uv run ocr-review serve` and draw one box on http://127.0.0.1:8765 to
      close the gap.

## Next, if the demand is real

- [ ] file_manager → OCR integration (block-level contract for "search hit
      located on page / bbox"): `docs/ocr-backend-design.md` §7 step 4.
- [ ] OCR HTTP service: only once several callers share one GPU or need job
      status. Gate and rationale in
      `docs/service-containerization-exploration.md` §7.
