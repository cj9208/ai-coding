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

Declaration notes (post-migration from argparse, repo-wide click convention):
every token and spelling is unchanged — ``download <model>``,
``parse <file> --out <dir> --device --pipeline-version --model-dir --raw-dir
--format-block-content``, ``container build|download|parse [--gpu]`` — and the
model names are still a ``Choice`` on the argument, so a typo fails at parse
time with the legal list. The ``_fetch`` / ``download`` / ``parse`` functions
stay module-level (adapters, tests and scripts call them directly); the click
bodies only map argv onto those and turn their errors into exit codes.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import click

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


@click.group()
def cli() -> None:
    """provision OCR models and run OCR"""


@cli.command("download")
@click.argument("model", type=click.Choice(sorted(MODELS)))
def download_cmd(model: str) -> None:
    """fetch a model snapshot into cache/ocr_backend/models/"""
    dest = download(model)
    if not is_ready(dest):
        raise click.ClickException(
            f"incomplete snapshot under {dest} — model.safetensors is missing"
        )
    click.echo(f"ready: {dest}")


@cli.command("parse")
@click.argument("source")
@click.option(
    "--out", required=True, help="directory to write <stem>.ocr.json/.md into"
)
@click.option(
    "--device", default=None, help="'cpu' / 'gpu' / 'gpu:0' (default: engine picks)"
)
@click.option("--pipeline-version", default="v1.6")
@click.option(
    "--model-dir",
    "model_dir_override",
    default=None,
    help="override the weights dir (default: repo snapshot if present)",
)
@click.option(
    "--raw-dir",
    default=None,
    help="also save Paddle's own per-page JSON here, for debugging",
)
@click.option(
    "--format-block-content",
    is_flag=True,
    help="ask the engine for block-level markdown/HTML/LaTeX formatting",
)
def parse_cmd(
    source: str,
    out: str,
    device: str | None,
    pipeline_version: str,
    model_dir_override: str | None,
    raw_dir: str | None,
    format_block_content: bool,
) -> None:
    """run OCR over a PDF/image and write the contract JSON"""
    if not Path(source).is_file():
        raise click.ClickException(f"source not found: {source}")
    try:
        parse(
            source,
            out,
            device=device,
            pipeline_version=pipeline_version,
            model_dir_override=model_dir_override,
            raw_dir=raw_dir,
            format_block_content=format_block_content,
        )
    except FileNotFoundError as exc:
        raise click.ClickException(str(exc)) from exc


@cli.group("container")
def container_cli() -> None:
    """run OCR through the Docker runner instead of in-process"""


def _docker(fn: Callable[..., int], *args: Any, **kwargs: Any) -> int:
    """Call a :mod:`ocr_backend.container` wrapper: Docker-missing is an
    ordinary error to report, not a Python traceback at the top level."""
    try:
        return fn(*args, **kwargs)
    except (RuntimeError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc


@container_cli.command("build")
@click.option("--gpu", is_flag=True, help="build the GPU image")
def container_build(gpu: bool) -> None:
    """docker compose build, one profile"""
    raise SystemExit(_docker(container.build, gpu=gpu))


@container_cli.command("download")
@click.argument("model", type=click.Choice(sorted(MODELS)))
@click.option("--gpu", is_flag=True, help="run the GPU image")
def container_download(model: str, gpu: bool) -> None:
    """provision a model snapshot inside the container"""
    raise SystemExit(_docker(container.download_model, model, gpu=gpu))


@container_cli.command(
    "parse",
    # Explicit help instead of a docstring: the mounted input directory is
    # derived from REPO_ROOT, so the text cannot be a static docstring (and
    # click arguments take no help= of their own).
    help=(
        "run one document through the container — SOURCE must live under "
        f"{container.IN_DIR}"
    ),
)
@click.argument("source")
@click.option(
    "--out",
    default=None,
    help=f"output directory under {container.OUT_DIR} (default)",
)
@click.option("--gpu", is_flag=True, help="run the GPU image")
@click.option("--device", default=None, help="override cpu/gpu detection")
@click.option(
    "--raw-dir",
    default=None,
    help="also save Paddle's own per-page JSON here, for debugging"
    f" (must live under {container.OUT_DIR})",
)
def container_parse(
    source: str,
    out: str | None,
    gpu: bool,
    device: str | None,
    raw_dir: str | None,
) -> None:
    raise SystemExit(
        _docker(
            container.parse,
            source,
            out_dir=out,
            raw_dir=raw_dir,
            gpu=gpu,
            device=device,
        )
    )


if __name__ == "__main__":
    cli()
