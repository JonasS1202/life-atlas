# Changelog

Written for users: `/atlas-update` shows the sections between the installed and the new version.

## [0.4.0] - 2026-11-07

- **Safe updates:** three-way comparison of the core (`_atlas/.base/`), automatic merge with `git merge-file`, local
  changes kept, conflicts shown instead of overwritten; the router's core block is updated between its markers while the
  personal part stays; declarative upgrades for grown files (`core/upgrades/`); re-wire and probe after every update,
  automatic rollback on failure; `atlas.py core-accept` after a manual merge.
- **Atlas Dev** for maintainers: `atlas.py dev-check` (push rights) and `dev-setup` (clone into `code/life-atlas`,
  project page with aliases). Test suite `tests/run.py`, test Atlas `dev/sandbox.py`, `CONTRIBUTING.md`.

## [0.3.0] - 2026-11-07

- Every page has `summary:` and `tags:` from `brain/tags.md` (with synonyms); `atlas.py find`; hints show the summary.
- Daily briefing in the first chat of the day; behaviour switches in `brain/settings.md`, changed in plain words.
- Learning signals ("remember", "from now on", corrections), signal-based reminders, `/atlas-learn`; Antigravity's
  `/learn` steered into the brain and adopted with `atlas.py adopt`.
- Daily log from `Atlas:` lines; weekly self-maintenance (`/atlas-maintain`, `check --fix`).
- `brain/self/`, `/atlas-interview`, `/atlas-deep` (only on request, kept only with consent).
- Several devices: pull at session start, push after each turn; project paths relative to ROOT; `code-sync`.
- Heartbeat `Atlas · ok` and heal mode in `/atlas-update`; subagents `atlas-search` and `atlas-maintainer` (Claude).

## [0.2.0] - 2026-11-07

- One Atlas folder as the project/workspace (`ROOT` with `brain/` and `code/`), wiring in project scope.
- Routing by project registry (names, aliases, folders, paths), project rules loaded only when routed; token budget.

## [0.1.0] - 2026-11-07

- First version: charter, router, hooks for Claude Code and Antigravity, inventory, safe moves, deadlines, developer
  module.
