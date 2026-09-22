"""``skills`` CLI wiring: what sync refuses to do, and what it reports.

The git side is stubbed at ``vendor.ensure`` / ``vendor.clone``; the filesystem
is the ``tmp_path`` root from the shared fixtures, so these exercise the
command logic rather than git or copying. The click commands wrap the
module-level int-returning functions, so tests go through ``CliRunner`` and
assert on the captured streams.
"""

import pytest
from click.testing import CliRunner, Result
from test_skill_manager.conftest import UPSTREAMS, touch_skill

from skill_manager import cli, vendor
from skill_manager.sources import Upstream


def run(*args: str) -> Result:
    return CliRunner().invoke(cli.cli, list(args))


@pytest.fixture
def no_git(monkeypatch):
    """Records provisioning calls instead of making them."""
    calls: list[str] = []
    monkeypatch.setattr(vendor, "ensure", lambda upstream: calls.append(upstream.clone))
    return calls


def test_sync_provisions_every_upstream_then_installs(root, no_git):
    target = root / ".opencode/skills"
    touch_skill(target / "superpowers-_brainstorming")

    result = run("sync")

    assert result.exit_code == 0, result.output
    assert no_git == ["repo-skills/alpha", "repo-skills/beta"]
    assert sorted(p.name for p in target.iterdir()) == [
        "alpha-brainstorming",
        "alpha-writing-plans",
        "beta-proposal",
    ]
    assert "pruned superpowers-_brainstorming" in result.stdout


def test_sync_refuses_to_install_an_incomplete_declaration(root, no_git, monkeypatch):
    monkeypatch.setattr(
        cli,
        "UPSTREAMS",
        {
            **UPSTREAMS,
            "gamma": Upstream(
                repo="https://example.invalid/gamma",
                commit="c" * 40,
                clone="repo-skills/gamma",
                select="nothing-matches-this",
            ),
        },
    )
    (root / "repo-skills/gamma").mkdir(parents=True)
    kept = touch_skill(root / ".opencode/skills/beta-proposal")

    result = run("sync")

    assert result.exit_code == 1
    assert "no skills found for: gamma" in result.stderr
    assert kept.is_dir(), "a failed sync must leave the install untouched"


def test_sync_accepts_an_upstream_whose_skills_are_all_overridden(root, no_git):
    """Shadowing every name is not the same as an upstream providing nothing."""
    touch_skill(root / "skills/beta-proposal")

    result = run("sync")

    assert result.exit_code == 0, result.output
    installed = root / ".opencode/skills/beta-proposal"
    assert (installed / "SKILL.md").read_text(encoding="utf-8") == "# beta-proposal\n"
    assert "3 skills in" in result.stdout


def test_sync_can_skip_provisioning(root, monkeypatch):
    def unexpected(upstream):
        raise AssertionError("sync --no-provision must not touch git")

    monkeypatch.setattr(vendor, "ensure", unexpected)

    result = run("sync", "--no-provision")

    assert result.exit_code == 0, result.output
    assert (root / ".opencode/skills/alpha-brainstorming").is_dir()


def test_list_reports_pins_and_installed_names(root):
    cli.install.install(cli.install.planned(UPSTREAMS), [root / ".opencode/skills"])

    result = run("list")

    assert result.exit_code == 0, result.output
    out = result.stdout
    assert "alpha" in out and "skills: 3" in out
    assert "beta-proposal" in out


def test_add_prints_the_declaration_to_paste(root, monkeypatch):
    cloned: list[str] = []

    def fake_clone(repo, dest):
        cloned.append(str(dest))
        return "c" * 40

    monkeypatch.setattr(vendor, "clone", fake_clone)

    result = run(
        "add",
        "https://example.invalid/gamma",
        "--prefix",
        "gamma",
        "--skills-dir",
        "skills",
    )

    assert result.exit_code == 0, result.output
    assert cloned == [str(root / "repo-skills/gamma")]
    printed = result.stdout
    assert '    "gamma": Upstream(' in printed
    assert '        commit="' + "c" * 40 + '",' in printed
    assert '        skills_dir="skills",' in printed
    assert '        prefix="gamma",' in printed
    assert "select=" not in printed
