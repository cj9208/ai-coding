"""Install planning and the rebuild: naming, the marker filter, the prune,
and the override-drift question the plan can answer without touching git."""

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
    target = root / ".agents/skills"
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
    target = root / ".agents/skills"
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
    target = root / ".agents/skills"

    install.install(install.planned(upstreams), [target])

    installed = target / "beta-proposal/SKILL.md"
    assert installed.read_text(encoding="utf-8") == "# ours, rewritten\n"


class FakeDiffers:
    """A ``vendor.folder_differs`` stand-in: fixed answer, records the query."""

    def __init__(self, answer: bool = True):
        self.answer = answer
        self.calls: list[tuple[str, str, str, str]] = []

    def __call__(self, upstream, folder, older, newer):
        self.calls.append((upstream.clone, folder, older, newer))
        return self.answer


def override_plan(root, upstreams):
    """A plan in which ``skills/beta-proposal`` shadows beta's own skill."""
    touch_skill(root / "skills/beta-proposal")
    return install.planned(upstreams)


def test_an_override_keeps_the_shadowed_skills_upstream_folder(root, upstreams):
    """With a prefix in play the two names differ, and a drift check has to ask
    the clone about the upstream one."""
    touch_skill(root / "skills/alpha-brainstorming")

    skills = {s.name: s for s in install.planned(upstreams)}

    assert skills["alpha-brainstorming"].upstream == "local"
    assert skills["alpha-brainstorming"].upstream_folder == "brainstorming"
    assert skills["beta-proposal"].upstream_folder == "beta-proposal"


def test_an_override_cut_from_the_current_pin_is_not_stale(root, upstreams):
    differs = FakeDiffers()

    stale = install.stale_overrides(
        override_plan(root, upstreams), upstreams, {"beta-proposal": "b" * 40}, differs
    )

    assert stale == []
    assert differs.calls == [], "base == pin cannot have drifted, so do not ask git"


def test_an_override_the_pin_has_moved_past_is_stale(root, upstreams):
    differs = FakeDiffers(answer=True)

    stale = install.stale_overrides(
        override_plan(root, upstreams), upstreams, {"beta-proposal": "0" * 40}, differs
    )

    assert stale == [
        install.StaleOverride(
            name="beta-proposal",
            upstream="beta",
            base="0" * 40,
            pin="b" * 40,
        )
    ]
    assert differs.calls == [("repo-skills/beta", "beta-proposal", "0" * 40, "b" * 40)]


def test_a_pin_bump_that_never_touched_the_folder_is_not_stale(root, upstreams):
    """The pin moving is not the same as this skill moving: reporting drift here
    would cry wolf on every unrelated bump and teach the reader to skip it."""
    differs = FakeDiffers(answer=False)

    stale = install.stale_overrides(
        override_plan(root, upstreams), upstreams, {"beta-proposal": "0" * 40}, differs
    )

    assert stale == []
    assert differs.calls, "the content question must actually have been asked"


def test_missing_bases_names_only_overrides_that_lack_one(root, upstreams):
    """A first-party skill shadows nothing, so it has no upstream copy to drift
    from and needs no base."""
    touch_skill(root / "skills/beta-proposal")
    touch_skill(root / "skills/ours")

    missing = install.missing_bases(install.planned(upstreams), {})

    assert missing == ["beta-proposal"]


def test_missing_bases_is_empty_once_the_base_is_recorded(root, upstreams):
    assert (
        install.missing_bases(
            override_plan(root, upstreams), {"beta-proposal": "b" * 40}
        )
        == []
    )
