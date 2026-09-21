# skills

Our own skills, and our differences from the vendored ones. Everything here is
tracked in git — unlike the upstream clones under `repo-skills/`, which are
regenerable mirrors and must stay untouched. `uv run skills sync` installs both
layers into every target directory (`.opencode/skills/` today).

| To | Do |
| --- | --- |
| add a skill we authored | put it in `skills/<name>/SKILL.md` |
| change a vendored skill | copy that skill to `skills/<install-name>/` and edit the copy — `<install-name>` is the name it installs under, upstream prefix included, e.g. `skills/superpowers-brainstorming/` |

An override replaces the whole folder, not parts of it: while it is in place,
upstream's later changes to that skill stop taking effect, so re-copy after a
pin bump if you want them. `skills list` reports which installed names come
from here.

Currently here as overrides: `openspec-proposal`, `openspec-apply`,
`openspec-archive` — upstream's copies hard-code their output to
`openspec/changes/<id>/`, these redirect every such path to `specs/openspec/…`
so a run lands in the tracked specs folder. `docs/skill-manager-guide.md` §6
covers the re-copy-after-a-pin-bump caveat.

Never edit inside `repo-skills/`: `skills sync` refuses to move a clone that
carries uncommitted changes, and edits made there exist on one machine only.
For a difference worth having upstream, fork the repository and point that
upstream's `repo` at the fork in `src/skill_manager/sources.py`.
