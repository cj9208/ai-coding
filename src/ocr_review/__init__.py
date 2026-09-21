"""``ocr_review`` — human proofreading UI for ``OcrDocument`` outputs.

Decouples machine time from human time: ``ocr-backend parse`` runs whenever
the GPU is free and writes ``<stem>.ocr.json`` (+ source file) into
``data/ocr_backend/out/``; a reviewer opens a browser later, sees those
artifacts in a list, compares them against the rendered PDF page by page, and
saves corrections. Machine output is never mutated — corrections live in a
sparse ``review.json`` overlay per document (design rationale:
docs/ocr-review-ui-exploration.md). The reviewed result is exported as a new,
fully valid ``OcrDocument`` via :func:`ocr_review.patch.apply_review`, so
consumers never learn that a review step exists.
"""
