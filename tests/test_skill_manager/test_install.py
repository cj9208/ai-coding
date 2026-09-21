"""Install planning and the rebuild: naming, the marker filter, the prune."""

import pytest
from test_skill_manager.conftest import touch_skill

from skill_manager import install
from skill_manager.sources import Upstream


def test_planned_names_skills_by_prefix_and_select(root, upstreams):
    skills = install.planned(upstreams)

    assert [s.name for s in skills] == [
        "alpha-brainstorming",
        "alpha-writing-plans",
        "beta-proposal",
    ]
    assert all(s.source.is_dir() for s in skills)


def test_planned_refuses_two_upstreams_naming_the_same_skill(root, upstreams):
    colliding = dict(upstreams)
    colliding["gamma"] = Upstream(
        repo="https://example.invalid/gamma",
        commit="c" * 40,
        clone="repo-skills/gamma",
    )
    touch_skill(root / "repo-skills/gamma/beta-proposal")

    with pytest.raises(ValueError, match="beta-proposal comes from both"):
        install.planned(colliding)


def test_install_rebuilds_the_target_and_prunes_undeclared_names(root, upstreams):
    target = root / ".opencode/skills"
    touch_skill(target / "alpha-_brainstorming")  # an upstream rename
    touch_skill(target / "beta-proposal")

    result = install.install(install.planned(upstreams), [target])

    assert sorted(p.name for p in target.iterdir()) == [
        "alpha-brainstorming",
        "alpha-writing-plans",
        "beta-proposal",
    ]
    assert result[0].removed == ["alpha-_brainstorming"]


def test_install_overwrites_previously_copied_content(root, upstreams):
    target = root / ".opencode/skills"
    install.install(install.planned(upstreams), [target])

    source = root / "repo-skills/alpha/skills/brainstorming/SKILL.md"
    source.write_text("# proofread version\n", encoding="utf-8")
    install.install(install.planned(upstreams), [target])

    installed = target / "alpha-brainstorming/SKILL.md"
    assert installed.read_text(encoding="utf-8") == "# proofread version\n"


def test_a_local_folder_replaces_the_upstream_skill_it_names(root, upstreams):
    touch_skill(root / "skills/beta-proposal")

    skills = install.planned(upstreams)

    winners = [s for s in skills if s.name == "beta-proposal"]
    assert len(winners) == 1
    assert winners[0].upstream == "local"
    assert winners[0].overrides == "beta"


def test_a_local_skill_with_a_new_name_installs_beside_the_upstreams(root, upstreams):
    touch_skill(root / "skills/ours")

    skills = install.planned(upstreams)

    assert [s.name for s in skills] == [
        "alpha-brainstorming",
        "alpha-writing-plans",
        "beta-proposal",
        "ours",
    ]
    assert skills[-1].overrides == ""


def test_install_copies_the_local_content_into_the_target(root, upstreams):
    local = root / "skills/beta-proposal"
    local.mkdir(parents=True)
    (local / "SKILL.md").write_text("# ours, rewritten\n", encoding="utf-8")
    target = root / ".opencode/skills"

    install.install(install.planned(upstreams), [target])

    installed = target / "beta-proposal/SKILL.md"
    assert installed.read_text(encoding="utf-8") == "# ours, rewritten\n"
