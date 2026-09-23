"""PaddleOCR-VL 1.6 adapter — maps ``paddleocr.PaddleOCRVL`` output to the contract.

Engine facts this adapter absorbs (all verified live on 2026-09-21 against
paddleocr 3.7.0 / PaddleOCR-VL-1.6-0.9B; raw evidence in
``data/ocr_backend/verify/``, findings summarized in
``docs/ocr-backend-design.md`` §5.3):

- ``predict()`` yields one Result per page. ``res`` carries ``width``/``height``
  (the raster the bboxes live in — PDF pages render at 2x of 72 dpi, i.e. a
  ~144 dpi A4 raster of 1191x1684; images keep native size), plus
  ``layout_det_res.boxes[]`` (label / score / coordinate) and
  ``parsing_res_list[]`` (``block_label`` / ``block_content`` / ``block_bbox`` /
  ``block_id`` / ``block_order``).
- ``block_order`` is 1-based and consecutive over the main flow; blocks the
  engine keeps out of it (captions, figures, page furniture) carry ``None``.
  ``block_id`` is the 0-based list position.
- Recognition has no confidence — "the VLMs are almost unable to provide
  confidence scores" (PaddleOCR issue #16899). The only usable score is the
  layout box's; we attach the score of the layout box with the highest IoU
  against the block bbox, and ``None`` when nothing overlaps.
- Content format follows the label: tables come back as ``<table>`` HTML,
  display formulas as ``$$...$$``-wrapped LaTeX, ``ocr`` (layout detection
  off) as whole-page markdown, everything else plain text.
- Image input (vs PDF) reports ``page_index``/``page_count`` as null; the
  adapter falls back to the enumeration position.

The mapping core is :func:`map_page`, a pure function over the ``res`` dict —
which is exactly the shape ``Result.json`` returns in memory and the sidecar
dumps on disk. Tests feed it golden JSON and never load the model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from storage import sha256_hex

from ..contract import (
    BlockKind,
    ContentFormat,
    OcrBackendInfo,
    OcrBlock,
    OcrDocument,
    OcrPage,
    OcrSource,
)
from ..models import is_ready, model_dir

_LABEL_TO_KIND: dict[str, BlockKind] = {
    # PP-DocLayoutV3's full vocabulary (25 labels, read from the model's
    # inference.yml). Unknown labels fall back to BlockKind.other with
    # raw_label preserved, so a newer model's new labels never break us.
    "doc_title": BlockKind.title,
    "paragraph_title": BlockKind.title,
    "text": BlockKind.paragraph,
    "vertical_text": BlockKind.paragraph,
    "abstract": BlockKind.paragraph,
    "reference": BlockKind.paragraph,
    "reference_content": BlockKind.paragraph,
    "content": BlockKind.paragraph,
    "ocr": BlockKind.paragraph,  # whole-page markdown, layout detection off
    "table": BlockKind.table,
    "display_formula": BlockKind.formula,
    "inline_formula": BlockKind.formula,  # rarely a block; usually inline in text
    "image": BlockKind.figure,
    "chart": BlockKind.figure,
    "seal": BlockKind.figure,
    "figure_title": BlockKind.caption,
    "vision_footnote": BlockKind.caption,
    "formula_number": BlockKind.caption,
    "header": BlockKind.header,
    "header_image": BlockKind.header,
    "footer": BlockKind.footer,
    "footer_image": BlockKind.footer,
    "number": BlockKind.page_number,
    "footnote": BlockKind.footnote,
    "algorithm": BlockKind.other,
    "aside_text": BlockKind.other,
}

_FORMULA_LABELS = frozenset({"display_formula", "formula", "inline_formula"})
_MARKDOWN_LABELS = frozenset({"ocr"})

_HEADING_PREFIX = re.compile(r"#{1,6}\s+")


def _normalize_content(
    raw_label: str, content: str, *, format_block_content: bool
) -> tuple[str, ContentFormat]:
    """Classify one block's content and normalize its wrapping form.

    The contract consumer should never need to know about engine switches:
    table -> HTML, formula -> bare LaTeX (``render`` re-wraps), everything
    else text unless the engine was told to emit markdown.
    """
    if raw_label == "table":
        return content, ContentFormat.html
    if raw_label in _FORMULA_LABELS:
        return _strip_math_delims(content), ContentFormat.latex
    if format_block_content or raw_label in _MARKDOWN_LABELS:
        return content, ContentFormat.markdown
    return content, ContentFormat.text


def _strip_math_delims(content: str) -> str:
    """`` $$ ... $$ `` (or ``$...$``) -> the bare LaTeX body.

    Display formulas come back wrapped (verified live); the contract keeps the
    math source without delimiters and ``render._wrap_latex`` re-adds what it
    needs — idempotently, so either side can be skipped.
    """
    text = content.strip()
    for delim in ("$$", "$"):
        if (
            len(text) >= 2 * len(delim) + 1
            and text.startswith(delim)
            and text.endswith(delim)
        ):
            return text[len(delim) : -len(delim)].strip()
    return text


def _strip_heading_prefix(content: str) -> str:
    """Drop the engine's ``### `` title prefix (only added under
    ``format_block_content=True``): the contract renders headings from
    ``kind`` + ``raw_label``, not from decorations inside the content."""
    stripped = content.lstrip()
    match = _HEADING_PREFIX.match(stripped)
    return stripped[match.end() :] if match else content


def _bbox(value: Any) -> tuple[float, float, float, float]:
    """Coerce ``[x1, y1, x2, y2]`` (list / tuple / numpy array) to floats."""
    if value is None:
        return (0.0, 0.0, 0.0, 0.0)
    x1, y1, x2, y2 = (float(v) for v in value)
    return (x1, y1, x2, y2)


def _iou(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
    ix2, iy2 = min(a[2], b[2]), min(a[3], b[3])
    iw, ih = ix2 - ix1, iy2 - iy1
    if iw <= 0 or ih <= 0:
        return 0.0
    intersection = iw * ih
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - intersection
    return intersection / union if union > 0 else 0.0


def _layout_boxes(
    res: dict[str, Any],
) -> list[tuple[tuple[float, float, float, float], float]]:
    """``(coordinate, score)`` pairs from ``layout_det_res``; ``[]`` when the
    key is absent (layout detection off) or there are no boxes (blank page)."""
    layout = res.get("layout_det_res") or {}
    boxes: list[tuple[tuple[float, float, float, float], float]] = []
    for box in layout.get("boxes") or []:
        coordinate = box.get("coordinate")
        if coordinate is None:
            continue
        boxes.append((_bbox(coordinate), float(box.get("score") or 0.0)))
    return boxes


def _best_layout_score(
    bbox: tuple[float, float, float, float],
    layout_boxes: list[tuple[tuple[float, float, float, float], float]],
) -> float | None:
    """Layout-detection score of the box overlapping ``bbox`` most (IoU > 0).

    This is *layout* confidence, not recognition confidence — the contract's
    ``score`` field documents that. ``None`` when nothing overlaps.
    """
    best_iou, best_score = 0.0, None
    for other, score in layout_boxes:
        value = _iou(bbox, other)
        if value > best_iou:
            best_iou, best_score = value, score
    return best_score


def _map_block(
    raw: dict[str, Any],
    layout_boxes: list[tuple[tuple[float, float, float, float], float]],
    position: int,
    *,
    format_block_content: bool,
) -> OcrBlock:
    raw_label = str(raw.get("block_label") or "")
    kind = _LABEL_TO_KIND.get(raw_label, BlockKind.other)
    content = str(raw.get("block_content") or "")
    content, content_format = _normalize_content(
        raw_label, content, format_block_content=format_block_content
    )
    if kind is BlockKind.title:
        content = _strip_heading_prefix(content)
    bbox = _bbox(raw.get("block_bbox"))
    raw_id = raw.get("block_id")
    raw_order = raw.get("block_order")
    return OcrBlock(
        id=position if raw_id is None else int(raw_id),
        kind=kind,
        raw_label=raw_label,
        content=content,
        content_format=content_format,
        bbox=bbox,
        order=None if raw_order is None else int(raw_order),
        score=_best_layout_score(bbox, layout_boxes),
    )


def map_page(
    res: dict[str, Any],
    *,
    fallback_index: int = 0,
    format_block_content: bool = False,
) -> OcrPage:
    """Map one native ``res`` dict to an :class:`OcrPage` — pure and total.

    ``res`` is ``Result.json["res"]`` or a sidecar JSON dump (same shape:
    plain JSON types). Blank pages (empty lists) and image inputs (null
    ``page_index``) go through unchanged — the adapter must never be the
    thing that breaks on a degenerate page.
    """
    page_index = res.get("page_index")
    layout_boxes = _layout_boxes(res)
    blocks = [
        _map_block(
            raw, layout_boxes, position, format_block_content=format_block_content
        )
        for position, raw in enumerate(res.get("parsing_res_list") or [])
    ]
    return OcrPage(
        page_index=fallback_index if page_index is None else int(page_index),
        width=int(res.get("width") or 0),
        height=int(res.get("height") or 0),
        blocks=blocks,
    )


@dataclass
class PaddleOCRVLConfig:
    """Common switches only; anything else the engine accepts goes through
    ``extra_params`` verbatim."""

    pipeline_version: str = "v1.6"
    """Pinned on purpose: the contract is calibrated against v1.6 output."""

    device: str | None = None
    """``"gpu"`` / ``"cpu"`` / ``"gpu:0"``; ``None`` = engine default (GPU if available)."""

    model_dir: str | Path | None = None
    """Override for the VL weights directory. ``None`` (default) prefers the
    repo-local snapshot in ``cache/ocr_backend/models/paddleocr-vl-1.6/`` when
    present, else the engine's official download/cache."""

    use_layout_detection: bool = True
    use_doc_orientation_classify: bool = False
    use_doc_unwarping: bool = False
    use_chart_recognition: bool = False
    use_seal_recognition: bool = False
    use_ocr_for_image_block: bool = False
    format_block_content: bool = False
    """Keep False for contract use — the render layer owns markdown. When True
    the engine decorates text blocks (titles get ``### ``, tables get inline
    styles) and the adapter flags text-ish content ``markdown``."""

    vl_rec_backend: str | None = None
    """e.g. ``"vllm-server"`` once the model runs as a service."""

    vl_rec_server_url: str | None = None

    raw_dir: Path | None = None
    """When set, Paddle's own per-page JSON is dumped here via its
    ``save_to_json`` — a sidecar for debugging and upgrade diffs; the contract
    never carries it."""

    extra_params: dict[str, Any] = field(default_factory=dict)


#: Repo-local weights snapshot, provisioned by ``ocr-backend download``.
_SNAPSHOT_NAME = "paddleocr-vl-1.6"


def _resolve_model_dir(cfg: PaddleOCRVLConfig) -> Path | None:
    """Explicit ``cfg.model_dir`` wins; ``None`` prefers the repo snapshot when
    it is fully downloaded (see :func:`ocr_backend.models.model_dir`), else the
    engine's official download/cache."""
    if cfg.model_dir:
        return Path(cfg.model_dir)
    snapshot = model_dir(_SNAPSHOT_NAME)
    return snapshot if is_ready(snapshot) else None


class PaddleOCRVLBackend:
    """Thin orchestration around ``paddleocr.PaddleOCRVL``.

    The pipeline is created lazily on the first :meth:`parse` — construction
    loads the models (~4-15 s and ~3 GiB VRAM measured on this machine) — and
    kept for the backend's lifetime: one instance per worker, not thread-safe,
    no pooling. Call :meth:`close` to release GPU memory when done.
    """

    name = "paddleocr_vl"

    def __init__(self, config: PaddleOCRVLConfig | None = None) -> None:
        self.config = config or PaddleOCRVLConfig()
        self._pipeline: Any = None

    @property
    def capabilities(self) -> frozenset[str]:
        if not self.config.use_layout_detection:
            # One whole-page markdown block: no per-block geometry or order.
            return frozenset()
        return frozenset({"block_bbox", "reading_order", "table_html", "formula_latex"})

    def close(self) -> None:
        """Drop the pipeline (frees GPU memory with the native backend)."""
        if self._pipeline is not None:
            self._pipeline.close()
            self._pipeline = None

    def parse(self, source: str | Path) -> OcrDocument:
        path = Path(source)
        if not path.is_file():
            raise FileNotFoundError(f"OCR source not found: {path}")
        pipeline = self._ensure_pipeline()
        raw_dir = Path(self.config.raw_dir) if self.config.raw_dir is not None else None
        if raw_dir is not None:
            raw_dir.mkdir(parents=True, exist_ok=True)
        pages: list[OcrPage] = []
        engine_settings: dict[str, Any] = {}
        page_count: int | None = None
        for position, result in enumerate(pipeline.predict(str(path))):
            if raw_dir is not None:
                result.save_to_json(save_path=str(raw_dir))
            res = result.json["res"]
            if not engine_settings:
                engine_settings = dict(res.get("model_settings") or {})
            if res.get("page_count") is not None:
                page_count = int(res["page_count"])
            pages.append(
                map_page(
                    res,
                    fallback_index=position,
                    format_block_content=self.config.format_block_content,
                )
            )
        return OcrDocument(
            source=OcrSource(
                kind="pdf" if path.suffix.lower() == ".pdf" else "image",
                path=str(path),
                sha256=sha256_hex(path.read_bytes()),
                page_count=page_count if page_count is not None else len(pages),
            ),
            backend=OcrBackendInfo(
                name=self.name,
                library_version=_paddleocr_version(),
                model=_model_name(pipeline),
                pipeline_version=self.config.pipeline_version,
                options=engine_settings,
            ),
            created_at=datetime.now().astimezone().isoformat(timespec="seconds"),
            pages=pages,
        )

    def _ensure_pipeline(self) -> Any:
        if self._pipeline is None:
            try:
                from paddleocr import PaddleOCRVL
            except ImportError as e:
                raise ImportError(
                    "paddleocr is required for the PaddleOCR-VL backend. Install "
                    "the OCR extras with: uv sync --extra ocr --extra paddle-cpu "
                    "(CPU) or uv sync --extra ocr --extra paddle-gpu (GPU)"
                ) from e
            self._pipeline = PaddleOCRVL(**self._pipeline_kwargs())
        return self._pipeline

    def _pipeline_kwargs(self) -> dict[str, Any]:
        cfg = self.config
        kwargs: dict[str, Any] = {
            "pipeline_version": cfg.pipeline_version,
            "use_layout_detection": cfg.use_layout_detection,
            "use_doc_orientation_classify": cfg.use_doc_orientation_classify,
            "use_doc_unwarping": cfg.use_doc_unwarping,
            "use_chart_recognition": cfg.use_chart_recognition,
            "use_seal_recognition": cfg.use_seal_recognition,
            "use_ocr_for_image_block": cfg.use_ocr_for_image_block,
            "format_block_content": cfg.format_block_content,
        }
        if cfg.device:
            kwargs["device"] = cfg.device
        resolved_model_dir = _resolve_model_dir(cfg)
        if resolved_model_dir is not None:
            kwargs["vl_rec_model_dir"] = str(resolved_model_dir)
        if cfg.vl_rec_backend:
            kwargs["vl_rec_backend"] = cfg.vl_rec_backend
        if cfg.vl_rec_server_url:
            kwargs["vl_rec_server_url"] = cfg.vl_rec_server_url
        kwargs.update(cfg.extra_params)
        return kwargs


def _paddleocr_version() -> str:
    import paddleocr

    return getattr(paddleocr, "__version__", "unknown")


def _model_name(pipeline: Any) -> str:
    """VL recognition model name from the merged paddlex config Paddle itself
    resolved. Metadata only — falls back to the pipeline name if the (private)
    config layout ever changes."""
    try:
        config = pipeline._merged_paddlex_config
        return str(config["SubModules"]["VLRecognition"]["model_name"])
    except Exception:  # pragma: no cover - defensive, engine version dependent
        return f"PaddleOCR-VL {getattr(pipeline, 'pipeline_version', '?')}"
