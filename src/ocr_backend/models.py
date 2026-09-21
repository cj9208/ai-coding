"""Local model snapshots — one directory per model, under ``data/``.

Downloaded weights are bulk data, not code: the repo keeps them in
``data/ocr_backend/models/<name>/`` — gitignored and root-anchored like every
other default data path (``utils.paths``). One store keeps the repo root clean
as more engines arrive, and adapters resolve their default snapshot through
:func:`model_dir` instead of consumers hard-coding a location. Provisioning is
one CLI command — ``ocr-backend download <name>`` (see :mod:`ocr_backend.cli`)
— so the name → repo / revision → path mapping lives in exactly one file.
"""

from pathlib import Path
from typing import NamedTuple

from utils.paths import data_dir


class ModelSpec(NamedTuple):
    """Where a snapshot comes from.

    ``revision`` is a full commit sha, not a branch: the download stays
    reproducible (same effect as the committed ``uv.lock``) and an upstream
    push can never silently change what a machine provisions.
    """

    repo_id: str
    revision: str


#: Local store name → spec; a new model is one entry. Bump ``revision`` to
#: re-provision after an upstream update.
MODELS: dict[str, ModelSpec] = {
    "paddleocr-vl-1.6": ModelSpec(
        repo_id="PaddlePaddle/PaddleOCR-VL-1.6",
        revision="c5630abae1d940eafe0697512a0325494b02ab42",
    ),
}


def model_dir(name: str) -> Path:
    """``<repo>/data/ocr_backend/models/<name>`` — the path, existing or not."""
    return data_dir("ocr_backend") / "models" / name


def is_ready(directory: Path) -> bool:
    """Whether ``directory`` is a complete snapshot. ``model.safetensors`` is
    the marker adapters use to accept the repo copy over the engine cache."""
    return (directory / "model.safetensors").is_file()
