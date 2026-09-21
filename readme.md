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

PDF text extraction uses PyMuPDF by default. For scanned PDFs, install the `ocr`
frontend plus one engine on a supported x86_64 platform (reported as `AMD64` on
Windows): `uv sync --extra ocr --extra paddle-cpu`, or `--extra ocr --extra
paddle-gpu` for the GPU engine, which resolves from Paddle's official cu126
index (Windows and Linux only).

Alternatively, run OCR in a one-shot Docker runner so the host needs neither
Paddle nor Poppler: `ocr-backend container build`, then
`ocr-backend container download paddleocr-vl-1.6`, then
`ocr-backend container parse data/ocr_backend/in/<file>.pdf` (add `--gpu` to
all three for the GPU image). See
`docs/service-containerization-exploration.md` §1.1 for the full guide.
