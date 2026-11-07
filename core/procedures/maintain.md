# Procedure: maintenance (`/atlas-maintain`, weekly by default)

Due when the last commit `atlas: maintenance` is older than `maintenance_days` (settings, 7–30) or 50+ pages changed
since; the session hook says so in the first chat of the day. Do it **after** the user's first request. In Claude Code
hand it to the `atlas-maintainer` subagent so the main chat stays clean. Scope: the grown layer only (charter §2).

1. **Mechanics (script, no judgment):** `python -I <ROOT>/brain/_atlas/tools/atlas.py check --fix`. It repairs links to
   moved pages, replaces tag synonyms by their tag, refreshes the search cache and the project rules, and lists what
   remains.
2. **Judgment, only on what the check reported:**
   - pages without `summary:`/`tags:` → write them (tags from `tags.md`; a new tag goes into `tags.md` first);
   - identical or overlapping pages → merge into the canonical one, link from the other places;
   - pages not reachable from `INDEX.md` → link them from their area or folder README;
   - `_atlas/memory/MEMORY.md` → lessons and preferences to `howto/` (or `settings.md`), done items out; < 60 lines;
   - projects that are clearly finished → `status: done` (they stay; nothing moves to `_archive/` without reason);
   - `ROUTER.md` personal part and `INDEX.md` grown → move detail into the pages they point to;
   - tags used once → merge into a broader tag if that loses nothing.
3. **Never on your own:** the core, anything outside `brain/`, deleting. Two pages with contradicting facts: do not
   pick one; collect such questions and put them into the next briefing.
4. One commit per kind of change (`git -C <ROOT>/brain commit -m "maintenance: …"`), then
   `atlas.py mark maintenance`.
5. Report in **at most three lines** ("Maintenance: 2 duplicates merged, 5 summaries added, 3 notes moved to howto/").
