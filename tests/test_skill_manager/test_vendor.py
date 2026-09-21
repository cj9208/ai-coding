"""``vendor`` tests — the git seam of the package, with ``vendor._git`` faked.

No test here clones anything or reaches the network. The one exception is
``_git`` itself, whose subprocess plumbing is checked against a faked
``subprocess.run``.
"""

from pathlib import Path

import pytest

from skill_manager import sources, vendor
from skill_manager.sources import Upstream

PIN = "a" * 40
TIP = "t" * 40


class FakeGit:
    """A ``vendor._git`` stand-in: records calls, keeps a HEAD per clone."""

    def __init__(
        self, heads=None, tip=TIP, behind="0", diff="", unknown_pins=0, dirty=()
    ):
        self.heads = dict(heads or {})
        self.tip = tip
        self.behind = behind
        self.diff = diff
        self.unknown_pins = unknown_pins
        self.dirty = list(dirty)
        self.calls = []

    def __call__(self, *args, cwd=None):
        cmd = list(args)
        dest = Path(cwd) if cwd is not None else None
        self.calls.append((cmd, dest))
        handler = getattr(self, f"_cmd_{cmd[0].replace('-', '_')}", None)
        if handler is None:
            raise AssertionError(f"unexpected git call: {cmd}")
        return handler(cmd, dest)

    def names(self):
        return [cmd[0] for cmd, _ in self.calls]

    def _cmd_clone(self, cmd, dest):
        new = Path(cmd[2])
        (new / ".git").mkdir(parents=True, exist_ok=True)
        self.heads[new] = self.tip
        return ""

    def _cmd_status(self, cmd, dest):
        return "\n".join(f" M {path}" for path in self.dirty)

    def _cmd_rev_parse(self, cmd, dest):
        if cmd[1] == "origin/HEAD":
            return self.tip
        commit = self.heads.get(dest)
        if commit is None:
            raise vendor.GitError(f"git rev-parse HEAD: not a repository: {dest}")
        return commit

    def _cmd_checkout(self, cmd, dest):
        if self.unknown_pins:
            self.unknown_pins -= 1
            raise vendor.GitError(f"git checkout {cmd[-1]}: reference is not a tree")
        self.heads[dest] = [a for a in cmd[1:] if not a.startswith("-")][0]
        return ""

    def _cmd_fetch(self, cmd, dest):
        if "--unshallow" in cmd:
            (dest / ".git" / "shallow").unlink()
        return ""

    def _cmd_rev_list(self, cmd, dest):
        return self.behind

    def _cmd_diff(self, cmd, dest):
        return self.diff


def make_upstream(**overrides):
    fields = {
        "repo": "https://example.invalid/alpha",
        "commit": PIN,
        "clone": "repo-skills/alpha",
    }
    fields.update(overrides)
    return Upstream(**fields)


@pytest.fixture
def pinned_root(tmp_path, monkeypatch):
    """A repo root to resolve clone paths against; returns the root."""
    monkeypatch.setattr(sources, "REPO_ROOT", tmp_path)
    return tmp_path


def test_git_failures_surface_as_their_own_error(monkeypatch):
    def failing_run(cmd, *args, **kwargs):
        class Result:
            returncode = 1
            stdout = ""
            stderr = "fatal: repository not found"

        return Result()

    monkeypatch.setattr(vendor.subprocess, "run", failing_run)

    with pytest.raises(vendor.GitError, match="repository not found"):
        vendor._git("clone", "https://example.invalid/x", "somewhere")


def test_ensure_clones_a_missing_upstream_and_pins_it(pinned_root, monkeypatch):
    upstream = make_upstream()
    fake = FakeGit()
    monkeypatch.setattr(vendor, "_git", fake)

    vendor.ensure(upstream)

    assert fake.names() == [
        "clone",
        "rev-parse",
        "rev-parse",
        "rev-parse",
        "status",
        "checkout",
    ]
    assert vendor.head(upstream.clone_dir) == PIN


def test_ensure_leaves_a_clone_already_on_the_pin(pinned_root, monkeypatch):
    upstream = make_upstream()
    (upstream.clone_dir / ".git").mkdir(parents=True)
    fake = FakeGit(heads={upstream.clone_dir: PIN})
    monkeypatch.setattr(vendor, "_git", fake)

    vendor.ensure(upstream)

    assert fake.names() == ["rev-parse", "rev-parse"]


def test_ensure_checks_the_pin_out_on_an_existing_clone(pinned_root, monkeypatch):
    upstream = make_upstream()
    (upstream.clone_dir / ".git").mkdir(parents=True)
    fake = FakeGit(heads={upstream.clone_dir: TIP})
    monkeypatch.setattr(vendor, "_git", fake)

    vendor.ensure(upstream)

    assert ["checkout", "--quiet", PIN] in [cmd for cmd, _ in fake.calls]
    assert vendor.head(upstream.clone_dir) == PIN


def test_ensure_refuses_to_move_a_clone_with_local_edits(pinned_root, monkeypatch):
    upstream = make_upstream()
    (upstream.clone_dir / ".git").mkdir(parents=True)
    fake = FakeGit(heads={upstream.clone_dir: TIP}, dirty=["skills/x/SKILL.md"])
    monkeypatch.setattr(vendor, "_git", fake)

    with pytest.raises(vendor.GitError, match=f"{sources.OWN_SKILLS_DIR}/"):
        vendor.ensure(upstream)

    assert "checkout" not in fake.names()
    assert vendor.head(upstream.clone_dir) == TIP


def test_ensure_deepens_a_shallow_clone_before_blaming_the_pin(
    pinned_root, monkeypatch
):
    upstream = make_upstream()
    shallow = upstream.clone_dir / ".git" / "shallow"
    shallow.parent.mkdir(parents=True)
    shallow.write_text(PIN, encoding="utf-8")
    fake = FakeGit(heads={upstream.clone_dir: TIP}, unknown_pins=1)
    monkeypatch.setattr(vendor, "_git", fake)

    vendor.ensure(upstream)

    assert fake.names() == [
        "rev-parse",
        "rev-parse",
        "rev-parse",
        "status",
        "checkout",
        "fetch",
        "fetch",
        "checkout",
    ]
    assert "--unshallow" in fake.calls[6][0]
    assert not shallow.exists()


def test_check_updates_reports_how_far_the_pin_trails(pinned_root, monkeypatch):
    upstream = make_upstream(skills_dir="skills/cloud")
    fake = FakeGit(
        heads={upstream.clone_dir: PIN},
        behind="3",
        diff="skills/cloud/gke/SKILL.md\nskills/cloud/gke/extra.py\nREADME.md",
    )
    monkeypatch.setattr(vendor, "_git", fake)

    update = vendor.check_updates(upstream)

    assert update.tip == TIP[:7]
    assert update.behind == 3
    assert update.changed == ["gke"]


def test_check_updates_reports_nothing_changed_when_pinned_at_the_tip(
    pinned_root, monkeypatch
):
    upstream = make_upstream(skills_dir="skills")
    monkeypatch.setattr(
        vendor, "_git", FakeGit(heads={upstream.clone_dir: PIN}, behind="0")
    )

    assert vendor.check_updates(upstream) == vendor.Update(
        tip=TIP[:7], behind=0, changed=[]
    )
