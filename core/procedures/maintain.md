# Procedure: maintenance (`/atlas-maintain`, weekly by default)

Due when the last commit `atlas: maintenance` is older than `maintenance_days` (settings, 7–30) or 50+ pages changed
since; the session hook says so in the first chat of the day. Do it **after** the user's first request. Hand it to the
`atlas-maintainer` subagent (available in both Claude Code and Antigravity) so the main chat stays clean. Scope: the
grown layer only (charter §2).

1. **Mechanics (script, no judgment):** `python -I <ROOT>/brain/_atlas/tools/atlas.py check --fix`, then
   `atlas.py link --journals --related --execute`. `check --fix` compiles `[[Target#Section|Label]]` wikilinks (outside
   code blocks) into relative Markdown links, repairs links to moved pages, replaces tag synonyms by their tag, refreshes
   the SQLite search & graph cache (`_atlas/cache/index.sqlite`) and project rules, and lists what remains; `link` weaves
   unlinked journal paths and canonical entity mentions into `related:`.
2. **Judgment, only on what the check reported:**
   - pages without `summary:`/`tags:` → write them (tags from `tags.md`; a new tag goes into `tags.md` first);
   - identical or overlapping pages → merge into the canonical one, link from the other places;
   - pages not reachable from `INDEX.md` → link them from their area or folder README;
   - horizontally isolated pages or unlinked prose mentions (`atlas.py link --scan`) → connect related entities where helpful;
   - stale `verified:` dates (`> 365` days) or missing `sources:` paths → fix broken local paths or note for review;
   - `_atlas/memory/MEMORY.md` → lessons and preferences to `howto/` (or `settings.md`), done items out; < 60 lines;
   - projects that are clearly finished → `status: done` (they stay; nothing moves to `_archive/` without reason);
   - `ROUTER.md` personal part and `INDEX.md` grown → move detail into the pages they point to;
   - tags used once → merge into a broader tag if that loses nothing.
3. **Never on your own:** the core, anything outside `brain/`, deleting. Two pages with contradicting facts: do not
   pick one; record each contradiction via `atlas.py conflicts add --pages <p1> <p2> --summary "<mismatch>"` so the
   next briefing surfaces it from `_atlas/conflicts.json` until resolved (`atlas.py conflicts resolve <id>`).
4. One commit per kind of change (`git -C <ROOT>/brain commit -m "maintenance: …"`), then
   `atlas.py mark maintenance`.
5. Report in **at most three lines** ("Maintenance: 2 duplicates merged, 5 summaries added, 3 notes moved to howto/").
