"""``ocr-backend container`` — the repetitive Docker calls, wrapped once.

Why this exists: the raw Compose invocation (see ``docs/
service-containerization-exploration.md``) has four things a person has to
retype correctly every time — the ``-f docker/ocr/compose.yaml`` path, the
``--profile cpu/gpu`` flag, the ``run --rm ocr-{cpu,gpu}`` service name, and
the ``/work/in`` ↔ ``data/ocr_backend/in`` host-path translation (the bind
mount the container can actually see). Getting any of those wrong is a silent
failure (wrong profile, file outside the mount, stale image). This module
derives all four from ``REPO_ROOT`` and the compose file's own mount points,
so the common flows — build, provision a model, parse a document — are one
short command each, and stay correct on Windows and Linux alike (subprocess,
not a bash script).

The rule this wrapper enforces: an input file must live under
``data/ocr_backend/in/`` (the same directory Compose bind-mounts read-only at
``/work/in``). That keeps "what the container can see" identical to "where the
repo already puts project data" — no second path to remember, and no copying
files into a location only Docker knows about.
"""

from __future__ import annotations

import shutil
import subprocess  # nosec B404 - docker is the point; called as an argv list
from pathlib import Path

from utils.paths import REPO_ROOT, data_dir

from .models import MODELS

COMPOSE_FILE = REPO_ROOT / "docker" / "ocr" / "compose.yaml"
IN_DIR = data_dir("ocr_backend") / "in"
OUT_DIR = data_dir("ocr_backend") / "out"

_IN_MOUNT = "/work/in"
_OUT_MOUNT = "/work/out"


def _service(gpu: bool) -> str:
    return "ocr-gpu" if gpu else "ocr-cpu"


def _profile(gpu: bool) -> str:
    return "gpu" if gpu else "cpu"


def _require_docker() -> None:
    if shutil.which("docker") is None:
        raise RuntimeError(
            "docker is not on PATH — install Docker Desktop (WSL2 backend) first"
        )


def _compose(*args: str, gpu: bool = False) -> list[str]:
    return [
        "docker",
        "compose",
        "-f",
        str(COMPOSE_FILE),
        "--profile",
        _profile(gpu),
        *args,
    ]


def _to_mount(path: str | Path, host_root: Path, mount_root: str) -> str:
    """Translate ``host_root/something`` into ``mount_root/something`` — the
    same file as the container sees it. Raises if the path is outside the
    mount, since Compose cannot see it and Docker would silently 404."""
    resolved = Path(path).resolve()
    root = host_root.resolve()
    try:
        relative = resolved.relative_to(root)
    except ValueError:
        raise ValueError(
            f"{resolved} is not under {root} — move it there first "
            f"(that directory is the only place the container can read from)"
        ) from None
    posix = relative.as_posix()
    return f"{mount_root}/{posix}" if posix not in ("", ".") else mount_root


def build(*, gpu: bool = False) -> int:
    """Build the runner image (``docker compose build`` for one profile)."""
    _require_docker()
    # argv list, never shell=True; the only variables are the fixed service
    # name and profile, so there is no string to break out of.
    return subprocess.run(  # nosec B603
        _compose("build", _service(gpu), gpu=gpu)
    ).returncode


def download_model(name: str, *, gpu: bool = False) -> int:
    """Provision a model snapshot *inside* the container, so the host needs
    no OCR install; the snapshot lands in the bind-mounted model store."""
    if name not in MODELS:
        raise ValueError(f"unknown model {name!r}; known: {', '.join(sorted(MODELS))}")
    _require_docker()
    cmd = _compose("run", "--rm", _service(gpu), "download", name, gpu=gpu)
    return subprocess.run(cmd).returncode  # nosec B603 - argv list, name checked


def parse(
    source: str | Path,
    *,
    out_dir: str | Path | None = None,
    gpu: bool = False,
    device: str | None = None,
) -> int:
    """Run one document through the containerized runner.

    ``source`` must live under ``IN_DIR`` (see module docstring); ``out_dir``
    defaults to ``OUT_DIR`` and must likewise live under it, so the container
    writes straight onto the host filesystem.
    """
    _require_docker()
    IN_DIR.mkdir(parents=True, exist_ok=True)

    container_in = _to_mount(source, IN_DIR, _IN_MOUNT)
    out = Path(out_dir) if out_dir is not None else OUT_DIR
    out.mkdir(parents=True, exist_ok=True)
    container_out = _to_mount(out, OUT_DIR, _OUT_MOUNT)

    device = device or ("gpu" if gpu else "cpu")
    cmd = _compose(
        "run",
        "--rm",
        _service(gpu),
        "parse",
        container_in,
        "--out",
        container_out,
        "--device",
        device,
        gpu=gpu,
    )
    # Both paths come from _to_mount, which prefixes them with the mount root,
    # so a crafted filename cannot reach docker as anything but a path.
    returncode = subprocess.run(cmd).returncode  # nosec B603
    if returncode == 0:
        print(f"result: {out / (Path(source).stem + '.ocr.json')}")
    return returncode
