"""``ocr-backend`` CLI — model provisioning for the OCR stack.

    ocr-backend download paddleocr-vl-1.6

The snapshot lands in the repo model store (``ocr_backend.models.model_dir``),
where adapters pick it up automatically — no manual paths to maintain, and the
next model is one ``MODELS`` entry in ``models.py``, which also carries the
commit the download is pinned to.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ocr-backend", description="provision OCR model snapshots"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser(
        "download", help="fetch a model snapshot into data/ocr_backend/models/"
    )
    d.add_argument("model", choices=sorted(MODELS), help="model to download")

    args = parser.parse_args(argv)
    dest = download(args.model)
    if not is_ready(dest):
        print(
            f"incomplete snapshot under {dest} — model.safetensors is missing",
            file=sys.stderr,
        )
        return 1
    print(f"ready: {dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
