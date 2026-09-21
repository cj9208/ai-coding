"""``ocr-backend`` CLI — provision models and run OCR over a document.

    ocr-backend download paddleocr-vl-1.6
    ocr-backend parse scan.pdf --out results/ --device cpu
    ocr-backend container parse data/ocr_backend/in/scan.pdf --gpu

``download`` fetches a model snapshot into the repo model store
(``ocr_backend.models.model_dir``), where adapters pick it up automatically —
no manual paths to maintain, and the next model is one ``MODELS`` entry in
``models.py``, which also carries the commit the download is pinned to.

``parse`` is the one-shot runner entry point: it takes a PDF or image, runs
``PaddleOCRVLBackend.parse()`` over it, and writes the resulting
:class:`~ocr_backend.contract.OcrDocument` as JSON (the contract itself), a
markdown projection, and a copy of the source file into ``--out``. The source
copy makes each ``--out`` bundle self-contained: a consumer holding only the
directory (e.g. the review UI, docs/ocr-review-ui-exploration.md) can still
render the pages the bboxes refer to. It is the seam a Docker runner shells out
to — see ``docs/service-containerization-exploration.md`` for why this, rather
than an HTTP service, is the first containerization target.

``container`` shells out to that Docker runner instead of the in-process one:
``build`` / ``download`` / ``parse`` wrap the repetitive ``docker compose
-f ... --profile ... run --rm ocr-cpu|ocr-gpu ...`` invocation (see
:mod:`ocr_backend.container`).
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from . import container
from .models import MODELS, ModelSpec, is_ready, model_dir


def _fetch(spec: ModelSpec, dest: Path) -> None:
    try:
        from huggingface_hub import snapshot_download
    except ImportError as exc:
        raise ImportError(
            "huggingface_hub is needed to download models and ships with the "
            "OCR stack: uv sync --extra ocr --extra paddle-cpu (or paddle-gpu)"
        ) from exc
    snapshot_download(repo_id=spec.repo_id, revision=spec.revision, local_dir=str(dest))


def download(name: str) -> Path:
    """Fetch the snapshot for ``name`` into the model store; returns its dir."""
    dest = model_dir(name)
    dest.mkdir(parents=True, exist_ok=True)
    _fetch(MODELS[name], dest)
    return dest


def parse(
    source: str | Path,
    out_dir: str | Path,
    *,
    device: str | None = None,
    pipeline_version: str = "v1.6",
    model_dir_override: str | Path | None = None,
    raw_dir: str | Path | None = None,
    format_block_content: bool = False,
) -> Path:
    """Run the PaddleOCR-VL backend over ``source``; write the contract JSON,
    markdown projection, and a copy of the source file into ``out_dir``.
    Returns the JSON path.

    The backend import is deferred into this function: ``parse`` is the only
    command that needs ``paddleocr`` installed, and this keeps ``download``
    and ``--help`` working on a plain (no-OCR-extras) install.
    """
    from .backends.paddleocr_vl import PaddleOCRVLBackend, PaddleOCRVLConfig
    from .render import document_markdown

    config = PaddleOCRVLConfig(
        pipeline_version=pipeline_version,
        device=device,
        model_dir=model_dir_override,
        raw_dir=Path(raw_dir) if raw_dir is not None else None,
        format_block_content=format_block_content,
    )
    backend = PaddleOCRVLBackend(config)
    try:
        doc = backend.parse(source)
    finally:
        backend.close()

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    src = Path(source)
    stem = src.stem
    json_path = out / f"{stem}.ocr.json"
    json_path.write_text(doc.model_dump_json(indent=2), encoding="utf-8")
    md_path = out / f"{stem}.ocr.md"
    md_path.write_text(document_markdown(doc), encoding="utf-8")
    source_copy = out / src.name
    if source_copy.resolve() != src.resolve():
        shutil.copy2(src, source_copy)
    print(f"parsed {len(doc.pages)} page(s): {json_path}, {md_path}, {source_copy}")
    return json_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ocr-backend", description="provision OCR models and run OCR"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser(
        "download", help="fetch a model snapshot into data/ocr_backend/models/"
    )
    d.add_argument("model", choices=sorted(MODELS), help="model to download")

    p = sub.add_parser(
        "parse", help="run OCR over a PDF/image and write the contract JSON"
    )
    p.add_argument("source", help="PDF or image file to parse")
    p.add_argument(
        "--out", required=True, help="directory to write <stem>.ocr.json/.md into"
    )
    p.add_argument(
        "--device", default=None, help="'cpu' / 'gpu' / 'gpu:0' (default: engine picks)"
    )
    p.add_argument("--pipeline-version", default="v1.6")
    p.add_argument(
        "--model-dir",
        default=None,
        help="override the weights dir (default: repo snapshot if present)",
    )
    p.add_argument(
        "--raw-dir",
        default=None,
        help="also save Paddle's own per-page JSON here, for debugging",
    )
    p.add_argument(
        "--format-block-content",
        action="store_true",
        help="ask the engine for block-level markdown/HTML/LaTeX formatting",
    )

    c = sub.add_parser(
        "container",
        help="run OCR through the Docker runner instead of in-process",
    )
    c_sub = c.add_subparsers(dest="container_cmd", required=True)

    cb = c_sub.add_parser("build", help="docker compose build, one profile")
    cb.add_argument("--gpu", action="store_true", help="build the GPU image")

    cd = c_sub.add_parser(
        "download", help="provision a model snapshot inside the container"
    )
    cd.add_argument("model", choices=sorted(MODELS), help="model to download")
    cd.add_argument("--gpu", action="store_true", help="run the GPU image")

    cp = c_sub.add_parser("parse", help="run one document through the container")
    cp.add_argument(
        "source",
        help=f"file to parse — must live under {container.IN_DIR}",
    )
    cp.add_argument(
        "--out",
        default=None,
        help=f"output directory under {container.OUT_DIR} (default)",
    )
    cp.add_argument("--gpu", action="store_true", help="run the GPU image")
    cp.add_argument("--device", default=None, help="override cpu/gpu detection")

    args = parser.parse_args(argv)

    if args.cmd == "download":
        dest = download(args.model)
        if not is_ready(dest):
            print(
                f"incomplete snapshot under {dest} — model.safetensors is missing",
                file=sys.stderr,
            )
            return 1
        print(f"ready: {dest}")
        return 0

    if args.cmd == "parse":
        if not Path(args.source).is_file():
            print(f"source not found: {args.source}", file=sys.stderr)
            return 1
        try:
            parse(
                args.source,
                args.out,
                device=args.device,
                pipeline_version=args.pipeline_version,
                model_dir_override=args.model_dir,
                raw_dir=args.raw_dir,
                format_block_content=args.format_block_content,
            )
        except FileNotFoundError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        return 0

    if args.cmd == "container":
        return _run_container(args)

    return 1


def _run_container(args: argparse.Namespace) -> int:
    """Dispatch ``ocr-backend container ...``; Docker-missing is an ordinary
    error to report, not a Python traceback at the top level."""
    try:
        if args.container_cmd == "build":
            return container.build(gpu=args.gpu)
        if args.container_cmd == "download":
            return container.download_model(args.model, gpu=args.gpu)
        if args.container_cmd == "parse":
            return container.parse(
                args.source, out_dir=args.out, gpu=args.gpu, device=args.device
            )
    except (RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    sys.exit(main())
