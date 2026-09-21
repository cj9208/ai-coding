"""Turn the declaration into installed skill folders.

Two layers make up the plan: the skills the declared upstreams provide, then
the tracked ``skills/`` directory on top of it, where a same-named folder
replaces an upstream skill. Every target directory is rebuilt as a *full* view
of that plan, and copying — not linking — keeps the installed tree valid for a
tool that knows nothing about git. A prune is therefore part of the install,
not a separate cleanup: the stale-name residue this replaces (upstream renames
a folder and the old copy lingers beside the new one) is exactly what made the
hand-synced layout untrustworthy.
"""

import shutil
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import NamedTuple

from utils.paths import REPO_ROOT

from .sources import OWN_SKILLS_DIR, TARGET_DIRS, UPSTREAMS, Upstream

#: what marks a directory as a skill
SKILL_MARKER = "SKILL.md"

#: the plan's label for a skill living in the tracked first-party directory
LOCAL = "local"


class Skill(NamedTuple):
    source: Path
    name: str
    upstream: str
    overrides: str = ""


class Target(NamedTuple):
    directory: Path
    installed: int
    removed: list[str]


def planned(
    upstreams: Mapping[str, Upstream] = UPSTREAMS,
    own_dir: Path | None = None,
) -> list[Skill]:
    """The skills to install: the declaration, with the local layer on top.

    A folder under ``skills/`` is ours, so it never needs a prefix; it wins
    against an upstream skill of the same install name, which is how a local
    edit survives a pin bump while the clone stays a replaceable mirror.
    """
    own_dir = own_dir if own_dir is not None else REPO_ROOT / OWN_SKILLS_DIR
    by_name: dict[str, Skill] = {}
    for name, upstream in upstreams.items():
        prefix = f"{upstream.prefix}-" if upstream.prefix else ""
        for folder in skill_folders(upstream.source_dir, upstream.select):
            skill_name = prefix + folder.name
            if skill_name in by_name:
                raise ValueError(
                    f"{skill_name} comes from both {by_name[skill_name].upstream} and "
                    f"{name} -- give one of them a prefix"
                )
            by_name[skill_name] = Skill(folder, skill_name, name)
    for folder in skill_folders(own_dir):
        shadowed = by_name.get(folder.name)
        overrides = shadowed.upstream if shadowed else ""
        by_name[folder.name] = Skill(folder, folder.name, LOCAL, overrides)
    return [by_name[name] for name in sorted(by_name)]


def skill_folders(directory: Path, select: str = "") -> list[Path]:
    """Subdirectories of ``directory`` that are skills, by the marker file."""
    if not directory.is_dir():
        return []
    folders = [p for p in sorted(directory.iterdir()) if p.is_dir()]
    if select:
        folders = [p for p in folders if p.name.startswith(select)]
    return [p for p in folders if (p / SKILL_MARKER).is_file()]


def installed_skill_names(target: Path) -> list[str]:
    return (
        sorted(p.name for p in target.iterdir() if p.is_dir())
        if target.is_dir()
        else []
    )


def install(
    skills: Sequence[Skill], targets: Sequence[Path] | None = None
) -> list[Target]:
    """Rebuild each target dir from ``skills``; returns what each now holds."""
    dirs = (
        targets
        if targets is not None
        else [REPO_ROOT / relative for relative in TARGET_DIRS]
    )
    return [_install_one(directory, skills) for directory in dirs]


def _install_one(directory: Path, skills: Sequence[Skill]) -> Target:
    directory.mkdir(parents=True, exist_ok=True)
    wanted = {skill.name for skill in skills}
    removed = sorted(set(installed_skill_names(directory)) - wanted)
    for name in removed:
        shutil.rmtree(directory / name)
    for skill in skills:
        destination = directory / skill.name
        shutil.rmtree(destination, ignore_errors=True)
        shutil.copytree(skill.source, destination)
    return Target(directory, len(wanted), removed)
