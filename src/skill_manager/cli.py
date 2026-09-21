"""``skills`` CLI — the one entry point for vendored AI skills.

    skills sync                       provision clones, rebuild every target
    skills list                       what is declared, pinned, and installed
    skills outdated                   how far each pin trails its upstream
    skills add https://github.com/x/y --prefix x   adopt a new upstream

``sync`` is the command that makes the layout reproducible: it clones what is
missing, checks each clone out at its declared commit, and rewrites
``.opencode/skills`` (and any further target in ``sources.TARGET_DIRS``) from
the declared skills — pruning names no longer declared. Provisioning and
reporting live in :mod:`skill_manager.vendor`, the copying in
:mod:`skill_manager.install`; this file only maps arguments to those and turns
their errors into exit codes.

A clone is a mirror of upstream, so it is never edited in place: ``sync``
refuses to move one that carries uncommitted changes, and the tracked
``skills/`` directory (:data:`skill_manager.sources.OWN_SKILLS_DIR`) is where
a local difference belongs — a folder there named as the skill installs
replaces that skill in every target.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

from utils.paths import REPO_ROOT

from . import install, vendor
from .sources import OWN_SKILLS_DIR, TARGET_DIRS, UPSTREAMS


def sync(provision: bool = True) -> int:
    """Make the clones, the plan, and the installed copies agree."""
    try:
        if provision:
            for name, upstream in UPSTREAMS.items():
                try:
                    vendor.ensure(upstream)
                except vendor.GitError as exc:
                    raise ValueError(f"{name}: {exc}") from exc
        skills = install.planned(UPSTREAMS)
        _reject_empty_upstreams(skills)
    except (vendor.GitError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    _warn_about_edited_clones()
    for target in install.install(skills):
        removed = f", pruned {', '.join(target.removed)}" if target.removed else ""
        print(
            f"{target.installed} skills in {target.directory.relative_to(REPO_ROOT)}{removed}"
        )
    return 0


def _warn_about_edited_clones() -> None:
    """An edited clone installs its edits as-is -- but nothing tracks them, so
    the next machine gets upstream's version instead."""
    for name, upstream in UPSTREAMS.items():
        edited = vendor.local_changes(upstream.clone_dir)
        if edited:
            print(
                f"warning: {upstream.clone} has {len(edited)} uncommitted edit(s)"
                f" ({', '.join(edited[:3])}); they install as-is but are recorded"
                f" nowhere -- move them into {OWN_SKILLS_DIR}/",
                file=sys.stderr,
            )


def _reject_empty_upstreams(skills: Sequence[install.Skill]) -> None:
    """An upstream that yields nothing means the declaration is wrong — and
    installing anyway would prune every skill it names.

    The question is what each upstream *provided*, not which layer won the
    install name: an upstream every one of whose skills is shadowed by a local
    override has still delivered them, and stays in the plan as the overridden
    version.
    """
    names = {skill.upstream for skill in skills} | {
        skill.overrides for skill in skills if skill.overrides
    }
    silent = sorted(set(UPSTREAMS) - names)
    if silent:
        raise ValueError(
            f"no skills found for: {', '.join(silent)}"
            " -- check clone, skills_dir, select"
        )


def list_declared() -> int:
    skills = install.planned(UPSTREAMS)
    # a local override is credited to the upstream whose skill it shadows, so an
    # entirely shadowed upstream does not read as if it delivered nothing
    counts = Counter(skill.overrides or skill.upstream for skill in skills)
    print(f"{'upstream':<12} {'pin':<9} {'clone':<9} {'edited':<7} skills")
    for name, upstream in UPSTREAMS.items():
        actual = vendor.short_head(upstream) or "-"
        edited = len(vendor.local_changes(upstream.clone_dir))
        print(
            f"{name:<12} {upstream.commit[:7]:<9} {actual:<9} "
            f"{edited or '-':<7} {counts.get(name, 0)}"
        )
    print(
        f"{install.LOCAL:<12} {'-':<9} {'-':<9} {'-':<7} {counts.get(install.LOCAL, 0)}"
    )

    overrides = [skill for skill in skills if skill.overrides]
    if overrides:
        print("\noverrides from the tracked skills/ directory:")
        for skill in overrides:
            print(f"  {skill.name} replaces the {skill.overrides} version")

    for target in target_dirs():
        installed = install.installed_skill_names(target)
        print(f"\n{target.relative_to(REPO_ROOT)}: {len(installed)}")
        print("\n".join(f"  {n}" for n in installed))
    return 0


def outdated() -> int:
    for name, upstream in UPSTREAMS.items():
        try:
            update = vendor.check_updates(upstream)
        except vendor.GitError as exc:
            print(f"{name}: {exc}", file=sys.stderr)
            return 1
        if not update.behind:
            print(f"{name}: up to date at {upstream.commit[:7]}")
            continue
        changed = f" changed: {_truncated(update.changed)}" if update.changed else ""
        print(
            f"{name}: {upstream.commit[:7]} -> {update.tip}, {update.behind} ahead{changed}"
        )
    return 0


def _truncated(names: Sequence[str], keep: int = 6) -> str:
    """An active upstream can move hundreds of skills; name a few of them."""
    shown = ", ".join(names[:keep])
    return shown if len(names) <= keep else f"{shown} +{len(names) - keep} more"


def add(
    repo: str,
    name: str,
    clone_dir: str,
    *,
    skills_dir: str = "",
    select: str = "",
    prefix: str = "",
) -> int:
    """Clone a new upstream and print the declaration it needs."""
    commit = vendor.clone(repo, REPO_ROOT / clone_dir)
    print(
        f"cloned at {commit[:7]}; add to UPSTREAMS in src/skill_manager/sources.py:\n"
    )
    print(_entry(name, repo, commit, clone_dir, skills_dir, select, prefix))
    print("\nthen: skills sync")
    return 0


def _entry(
    name: str,
    repo: str,
    commit: str,
    clone: str,
    skills_dir: str,
    select: str,
    prefix: str,
) -> str:
    lines = [
        f'    "{name}": Upstream(',
        f'        repo="{_https(repo)}",',
        f'        commit="{commit}",',
        f'        clone="{clone}",',
    ]
    lines += [
        f'        {field}="{value}",'
        for field, value in (
            ("skills_dir", skills_dir),
            ("select", select),
            ("prefix", prefix),
        )
        if value
    ]
    lines.append("    ),")
    return "\n".join(lines)


def _https(repo: str) -> str:
    return repo.removesuffix(".git")


def target_dirs() -> list[Path]:
    return [REPO_ROOT / relative for relative in TARGET_DIRS]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="skills", description="manage the vendored AI skills in repo-skills/"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("sync", help="provision clones and rebuild the install targets")
    s.add_argument(
        "--no-provision",
        action="store_true",
        help="skip git; rebuild targets from the clones as they are",
    )

    sub.add_parser("list", help="show declared pins and installed skills")
    sub.add_parser("outdated", help="fetch and report pins that trail their upstream")

    a = sub.add_parser("add", help="clone a new upstream and print its declaration")
    a.add_argument("repo", help="git repository URL")
    a.add_argument("--name", default=None, help="upstream label (default: repo stem)")
    a.add_argument(
        "--clone",
        default=None,
        help="directory to clone into (default: repo-skills/<repo stem>)",
    )
    a.add_argument("--skills-dir", default="", help="clone subdir holding the skills")
    a.add_argument(
        "--select", default="", help="only skills whose name starts with this"
    )
    a.add_argument(
        "--prefix", default="", help="prefix installed skill names with this"
    )

    args = parser.parse_args(argv)

    if args.cmd == "sync":
        return sync(provision=not args.no_provision)
    if args.cmd == "list":
        return list_declared()
    if args.cmd == "outdated":
        return outdated()
    if args.cmd == "add":
        name = args.name or Path(args.repo).stem.removesuffix("-skills")
        clone_dir = args.clone or f"repo-skills/{Path(args.repo).stem}"
        return add(
            args.repo,
            name,
            clone_dir,
            skills_dir=args.skills_dir,
            select=args.select,
            prefix=args.prefix,
        )
    return 1


if __name__ == "__main__":
    sys.exit(main())
