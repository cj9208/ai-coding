This repo
* records various of small functions in daily life
* uses uv to manage the repo
* investigates various AI coding tools

## Dependency management

`pyproject.toml` declares the project's direct dependencies and version policy.
`uv.lock` is the committed resolution snapshot for all transitive dependencies.
Set up a matching environment with `uv sync --locked`; use `uv add` or `uv remove`
to change dependencies, then commit the refreshed lock file.

## Optional OCR

PDF text extraction uses PyMuPDF by default. For scanned PDFs, install one OCR
profile on a supported x86_64 platform (reported as `AMD64` on Windows):
`uv sync --extra ocr` for CPU or `uv sync --extra ocr-gpu` for GPU. The GPU
profile supports Windows and Linux. OCR also requires Poppler to be installed
on the host for `pdf2image`.
