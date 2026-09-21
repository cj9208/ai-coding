"""Declared skill upstreams — one entry per repository, pinned to a commit.

Which skill came from which repository at which revision is the one fact that
must not live machine-locally, so it sits here in git while everything derived
from it (the clones under ``repo-skills/``, the installed copies under
``.opencode/skills/``) stays gitignored and regenerable: ``skills sync``
provisions both from scratch on any machine. Same posture as
:py:mod:`ocr_backend.models` for model snapshots — a new upstream is one
entry, an upstream update is one ``commit`` bump.
"""

from pathlib import Path
from typing import NamedTuple

from utils.paths import REPO_ROOT


class Upstream(NamedTuple):
    """One upstream: ``repo`` at ``commit``, installed as ``prefix``-named skills.

    ``commit`` is a full sha rather than a branch so an upstream push can never
    silently change what a machine provisions. ``clone`` is where the
    repository lands, ``skills_dir`` the subdirectory of it that holds one
    folder per skill, ``select`` restricts an upstream to part of its skills,
    and ``prefix`` keeps same-named skills of different upstreams apart.
    """

    repo: str
    commit: str
    clone: str
    skills_dir: str = ""
    select: str = ""
    prefix: str = ""

    @property
    def clone_dir(self) -> Path:
        return REPO_ROOT / self.clone

    @property
    def source_dir(self) -> Path:
        return self.clone_dir / self.skills_dir


#: Declared upstreams; the key is how commands and reports name them.
UPSTREAMS: dict[str, Upstream] = {
    "openspec": Upstream(
        repo="https://github.com/chyiiiiiiiiiiii/openspec-skills",
        commit="092825896ed788b40c7ea987c17f1c5f75ed843a",
        clone="repo-skills/openspec-skills",
        select="openspec-",
    ),
    "superpowers": Upstream(
        repo="https://github.com/obra/superpowers",
        commit="6efe32c9e2dd002d0c394e861e0529675d1ab32e",
        clone="repo-skills/superpowers",
        skills_dir="skills",
        prefix="superpowers",
    ),
    "karpathy": Upstream(
        repo="https://github.com/forrestchang/andrej-karpathy-skills",
        commit="2c606141936f1eeef17fa3043a72095b4765b9c2",
        clone="repo-skills/andrej-karpathy-skills",
        skills_dir="skills",
    ),
    "google": Upstream(
        repo="https://github.com/google/skills",
        commit="6f0b8771638112948f03601e54f4602dd3cb7cc7",
        clone="repo-skills/skills",
        skills_dir="skills/cloud",
        prefix="google",
    ),
}

#: Generated skill directories to rebuild, one per agent tool that reads skills
#: from the workspace. Each is a full view of ``UPSTREAMS`` after a sync.
TARGET_DIRS: tuple[str, ...] = (".opencode/skills",)

#: First-party skills, tracked in git unlike the clones: a new skill we author
#: lives here as its own folder, and a folder named exactly as an upstream
#: skill *installs* (``superpowers-brainstorming``, prefix included) replaces
#: that skill. This is where a local edit goes: the clone stays a pristine,
#: replaceable mirror of upstream, while the difference from it is reviewable
#: in a diff and survives every pin bump — the sidecar-over-machine-output
#: posture ``ocr_review`` uses for proofreadings.
OWN_SKILLS_DIR = "skills"
