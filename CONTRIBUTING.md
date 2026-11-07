# Developing Atlas

Atlas exists in two forms: this **repository** (the source) and **installed copies** (`<ROOT>/brain/_atlas/` in every
user's Atlas, including the maintainer's own). Installed copies are never edited by hand. Every change is made here,
tested, released, and reaches each Atlas, the maintainer's first, through `/atlas-update`.

## Set-up (Atlas Dev)

Only for people with push rights to this repository. Atlas checks that itself:

- during installation, or later by saying "I want to develop Atlas" / "Atlas weiterentwickeln", the agent runs
  `atlas.py dev-check` (GitHub API via `gh` if available, otherwise `git push --dry-run`);
- with push rights, `atlas.py dev-setup --execute` clones this repository into `<ROOT>/code/life-atlas` and registers
  the project **Atlas Dev** (aliases "Atlas Dev", "atlas-dev", "life-atlas", "Atlas weiterentwickeln"); without push
  rights nothing is set up.

From then on "continue in Atlas Dev" routes the chat into this repository like any other project.

## The loop

1. **Change** files here (`core/`, `skeleton/`, `modules/`, `INSTALL.md`, …). Rules:
   - Python standard library only; hooks fail-open (any error → exit 0, no output).
   - Hook formats and wiring only after checking the official docs listed in `core/adapters.md`; record the date there.
   - The router template (`core/templates/router.md`) stays under 6 KB; skill descriptions stay short.
   - Core vs. grown (charter §2): users' grown files (`skeleton/` → their `brain/`) are never overwritten by an update.
     If a release needs something added there, describe it in `core/upgrades/<version>.json` (below).
2. **Test**: `python -I tests/run.py` (all) or `python -I tests/run.py -k route` (some). Black-box tests that build
   throwaway Atlases in `~/.atlas-tests/` and remove them. Add a test for every behaviour you change.
3. **Try it live**: `python -I dev/sandbox.py` creates a separate test Atlas next to yours (`<ROOT>-dev`, or
   `~/Atlas-dev`) with demo content, wired for both agents in project scope; later runs update it with `core-apply`
   from your working copy, so you also exercise the update path. Open that folder as a project or workspace and talk to
   it. Your real Atlas is not touched.
4. **Release** (below), push.
5. **Update your own Atlas** with `/atlas-update`, like every user.

## Release

1. `core/VERSION`: bump (`0.4.0` → `0.4.1` fixes, `0.5.0` features, `1.0.0` breaking).
2. `CHANGELOG.md`: a `## [x.y.z] - YYYY-MM-DD` section, written for users (Atlas shows it during `/atlas-update`).
3. `core/upgrades/<x.y.z>.json` if users' grown files need additions. Declarative and additive only:
   - `"dirs": ["self"]` create folders;
   - `"skeleton_add": ["settings.md"]` copy a skeleton file only if the user has none;
   - `"settings_add": {"key": "default  # comment"}` add a switch to `settings.md` only if missing;
   - `"tags_add": [["tag", "meaning", "synonyms"]]` add a tag row only if missing;
   - `"notes": ["…"]` tasks for the agent that need judgment (e.g. merge a new INDEX section into the user's localised
     INDEX). Never ship instructions that overwrite user content.
4. `python -I tests/run.py` green, then `git commit`, `git tag vX.Y.Z`, `git push --follow-tags`.

## How an update reaches an Atlas

`/atlas-update` runs `atlas.py core-diff` and, after one yes, `core-apply`:

| Case per core file | Result |
|---|---|
| changed upstream only | updated |
| changed only in this Atlas (e.g. by heal mode) | kept, reported |
| changed on both sides | three-way merge (`git merge-file`); clean → applied, otherwise kept and the conflict is shown to the user |
| new / dropped upstream | added / removed (backed up) |
| router core block | replaced between its markers (or merged); the personal part always stays |
| upgrades | applied additively, notes handed to the agent |

Then every wired agent is re-wired with the new tool and probed; if any probe fails, the whole update is rolled back.
`_atlas/.base/` keeps the upstream version an Atlas runs on; it is the base of the next three-way comparison.
