"""Shared fixtures for the skill_manager tests.

Nothing here touches git, the network, or the real ``repo-skills/`` clones: the
repo root is redirected into ``tmp_path`` and the clones are plain directories
whose skill folders carry a ``SKILL.md`` marker.
"""

import pytest

from skill_manager import cli, install, sources
from skill_manager.sources import Upstream

#: Two upstreams, exercising both selection styles: a prefix over a subfolder,
#: and a name filter with no prefix.
UPSTREAMS: dict[str, Upstream] = {
    "alpha": Upstream(
        repo="https://example.invalid/alpha",
        commit="a" * 40,
        clone="repo-skills/alpha",
        skills_dir="skills",
        prefix="alpha",
    ),
    "beta": Upstream(
        repo="https://example.invalid/beta",
        commit="b" * 40,
        clone="repo-skills/beta",
        select="beta-",
    ),
}


def touch_skill(directory) -> object:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(f"# {directory.name}\n", encoding="utf-8")
    return directory


@pytest.fixture
def upstreams() -> dict[str, Upstream]:
    return UPSTREAMS


@pytest.fixture
def root(tmp_path, monkeypatch):
    """A repo root holding the declared clones; returns the root path."""
    monkeypatch.setattr(sources, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(install, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(cli, "UPSTREAMS", UPSTREAMS)
    monkeypatch.setattr(cli, "TARGET_DIRS", (".opencode/skills",))

    skills = tmp_path / "repo-skills/alpha/skills"
    touch_skill(skills / "brainstorming")
    touch_skill(skills / "writing-plans")
    touch_skill(tmp_path / "repo-skills/beta/beta-proposal")
    # folders without the marker are not skills, whatever they are named
    (skills / "shared").mkdir()
    (tmp_path / "repo-skills/beta/templates").mkdir()
    return tmp_path
