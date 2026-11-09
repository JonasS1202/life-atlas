# Changelog

Written for users: `/atlas-update` shows the sections between the installed and the new version.

## [0.5.1] - 2026-11-09

- **One-command `/atlas-update` (applies immediately):** `atlas.py core-apply --execute` (and `core-diff` / `core-accept`)
  now automatically clones `upstream` from `_atlas/state.json` (with fallback to the official repository) when `--source`
  is omitted, installs clean updates immediately without waiting for a second confirmation after the preview, and skips
  slow web searches or heal-mode probes when `Atlas · ok` is present. `atlas.py probe` without `--agent` now probes all
  wired agents.
- **Natural opening & real personality in `/atlas-deep`:** Explicitly forbids robotic meta-announcements (*"We are now in
  Deep Mode..."*, *"In your own pace, without a mask..."*) and generic blank-slate questions. Atlas now greets the user
  naturally by name like a close friend and opens directly with a concrete observation or hook from their `brain/`.

## [0.5.0] - 2026-11-09

- **Deep friend-and-psychologist mode (`/atlas-deep`):** Completely redesigned `procedures/deep.md` and unlocked natural
  chat invocation in `skills/atlas-deep/SKILL.md`. Instead of an abstract questionnaire, Atlas now scans `self/`,
  `people/`, `areas/`, `projects/`, and `journal/` for depth gaps (treating fact-only pages as blank canvases), holds a
  warm evening-with-a-best-friend conversation using psychological mirroring and bold hypotheses, follows up 2–3 layers
  deep before changing topics, and files insights continuously into the brain.
- **Smarter `/atlas-interview`:** Never stops with "I already know everything" once structural facts exist; automatically
  pivots to context/relationship questions on thin pages or offers a seamless transition into `/atlas-deep`.

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
