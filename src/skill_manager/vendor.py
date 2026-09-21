"""Provision the gitignored clones that the declaration points at.

Docker is the only other external tool this repo shells out to, so this module
is the whole git interface: it clones what is missing, parks a clone on the
declared commit (detached — a pin is a snapshot, not a branch to work on), and
reports how far a pin trails the upstream's default branch. A clone with
uncommitted edits is never moved: those belong in the tracked override
directory, and displacing them silently is the failure this guards.
"""

from __future__ import annotations

import subprocess  # nosec B404 - git is the point; called as an argv list
from pathlib import Path, PurePosixPath
from typing import NamedTuple

from .sources import OWN_SKILLS_DIR, Upstream


class GitError(RuntimeError):
    """git refused; the message carries the command's own stderr."""


class Update(NamedTuple):
    """How much an upstream has moved since its pin was recorded."""

    tip: str
    behind: int
    changed: list[str]


def _git(*args: str, cwd: Path | None = None) -> str:
    cmd = ("git", *args)
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)  # nosec B603
    if proc.returncode:
        raise GitError(f"git {' '.join(args)}: {proc.stderr.strip()}")
    return proc.stdout.strip()


def head(clone_dir: Path) -> str | None:
    """Current commit of a directory, or None when it is not a clone."""
    if not (clone_dir / ".git").exists():
        return None
    try:
        return _git("rev-parse", "HEAD", cwd=clone_dir)
    except GitError:
        return None


def clone(repo: str, dest: Path) -> str:
    """Full clone of ``repo`` into ``dest`` — history included, so any commit can
    be pinned later; returns the resulting HEAD."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    _git("clone", repo, str(dest))
    commit = head(dest)
    if commit is None:
        raise GitError(f"{repo} cloned without a HEAD")
    return commit


def local_changes(clone_dir: Path) -> list[str]:
    """Paths edited inside a clone but recorded in no commit — invisible to
    git, and lost the moment the clone is re-provisioned."""
    if head(clone_dir) is None:
        return []
    status = _git("status", "--porcelain", cwd=clone_dir)
    return [line[3:] for line in status.splitlines() if line]


def ensure(upstream: Upstream) -> Path:
    """Make the clone exist and sit on the declared commit; returns its dir."""
    dest = upstream.clone_dir
    if head(dest) is None:
        clone(upstream.repo, dest)
    if head(dest) == upstream.commit:
        return dest
    changes = local_changes(dest)
    if changes:
        raise GitError(
            f"{upstream.clone} has {len(changes)} local edit(s)"
            f" ({', '.join(changes[:3])}) and moving it to the pinned commit"
            f" would displace them -- move the difference into"
            f" {OWN_SKILLS_DIR}/ as an override, or fork the upstream"
        )
    try:
        _git("checkout", "--quiet", upstream.commit, cwd=dest)
    except GitError:
        # An unknown revision means a shallow or stale clone: fetch (deepening
        # once) before blaming the pin.
        _git("fetch", "--quiet", "origin", cwd=dest)
        if (dest / ".git" / "shallow").is_file():
            _git("fetch", "--quiet", "--unshallow", "origin", cwd=dest)
        _git("checkout", "--quiet", upstream.commit, cwd=dest)
    return dest


def short_head(upstream: Upstream) -> str | None:
    commit = head(upstream.clone_dir)
    return commit[:7] if commit else None


def check_updates(upstream: Upstream) -> Update:
    """Fetch and compare the pin against the upstream's default branch."""
    dest = upstream.clone_dir
    _git("fetch", "--quiet", "origin", cwd=dest)
    tip = _git("rev-parse", "origin/HEAD", cwd=dest)
    behind = int(_git("rev-list", "--count", f"{upstream.commit}..{tip}", cwd=dest))
    return Update(
        tip=tip[:7],
        behind=behind,
        changed=_changed_skills(upstream, tip) if behind else [],
    )


def _changed_skills(upstream: Upstream, tip: str) -> list[str]:
    """Skill folders whose contents differ between the pin and ``tip``."""
    scope = upstream.skills_dir or "."
    paths = _git(
        "diff", "--name-only", upstream.commit, tip, "--", scope, cwd=upstream.clone_dir
    ).splitlines()
    folders = {_folder(path, upstream.skills_dir) for path in paths}
    return sorted(folders - {""})


def _folder(path: str, skills_dir: str) -> str:
    """The skill folder a changed file belongs to, inside ``skills_dir``."""
    depth = len(PurePosixPath(skills_dir).parts) if skills_dir else 0
    parts = PurePosixPath(path).parts
    return parts[depth] if len(parts) > depth else ""
