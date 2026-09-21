"""Rasterize PDF pages onto the contract's pixel grid.

The contract declares each page's raster size (``OcrPage.width/height`` — the
grid its bboxes live in, produced by Paddle's internal renderer at ~2x page
points). A review overlay only aligns if the background image is rendered to
*exactly that grid*, so target size comes from the contract, never from a DPI
guess. PyMuPDF (core dependency) renders; Paddle's own pypdfium2 was never in
the picture on this side — same MediaBox, same pixel target, so the grids
agree by construction.

Output is a pure cache: deterministic from (pdf, contract), safe to delete,
regenerated on demand.
"""

from __future__ import annotations

from pathlib import Path

from ocr_backend.contract import OcrDocument


def render_pages(doc: OcrDocument, source_pdf: Path, pages_dir: Path) -> list[Path]:
    """Render every contract page to ``pages_dir/page-{i:03d}.png`` at the
    page's declared pixel size. Existing files are skipped (cache)."""
    import fitz  # PyMuPDF; deferred so importing this module stays light

    pages_dir.mkdir(parents=True, exist_ok=True)
    out: list[Path] = []
    pdf = fitz.open(source_pdf)
    try:
        if len(doc.pages) != pdf.page_count:
            raise ValueError(
                f"contract has {len(doc.pages)} pages, PDF has {pdf.page_count}"
            )
        for page in doc.pages:
            dest = pages_dir / f"page-{page.page_index:03d}.png"
            out.append(dest)
            if dest.exists():
                continue
            rect = pdf[page.page_index].rect
            matrix = fitz.Matrix(page.width / rect.width, page.height / rect.height)
            pix = pdf[page.page_index].get_pixmap(matrix=matrix, alpha=False)
            # get_pixmap rounds; assert the grid actually matches the contract
            if (pix.width, pix.height) != (page.width, page.height):
                raise ValueError(
                    f"page {page.page_index}: rendered {pix.width}x{pix.height}, "
                    f"contract declares {page.width}x{page.height}"
                )
            pix.save(dest)
    finally:
        pdf.close()
    return out
