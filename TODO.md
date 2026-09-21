# TODO

Loose ends worth picking up, newest first. Anything here should be deleted once
done rather than left as archaeology — the code and git history are the source
of truth for completed work.

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
