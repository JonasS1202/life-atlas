# Procedure: health check (`/atlas-check`)

1. Run `python -I <ROOT>/brain/_atlas/tools/atlas.py index` (refreshes the search cache) and then `atlas.py check`.
   It reports errors, warnings and info:
   - **token budget**: router size (loads into every chat), memory notepad lines, `INDEX.md` size, total length of the
     skill descriptions;
   - **routing**: number of projects, ambiguous project names, project folders that no longer exist;
   - **structure**: broken links, pages not reachable from `INDEX.md`, identical pages;
   - **safety**: credentials outside `_secrets/`, `_secrets/` tracked by Git, Git remote and last push;
   - **wiring**: agents wired, hooks registered, hook interpreter present; update age; overdue deadlines.
2. For each agent the user uses, run `atlas.py probe --agent claude` / `--agent antigravity`: all three hooks must say OK.
3. Fix what belongs to the grown layer right away: links; orphans (link them from their area or `INDEX.md`); duplicates
   (merge into one page and link); ambiguous project names (add distinct `aliases:`); a router personal part or
   `INDEX.md` that grew (move detail into the pages it points to). Say in one line what you changed.
4. Anything in the core (charter, hooks, tools, router core block), or anything that needs the user (a missing remote,
   credentials in a normal page, overdue deadlines): report it with a recommendation and wait.
5. Tidy the memory notepad: lessons in `_atlas/memory/` that are really rules or preferences move to `howto/`; remove
   what is done. Keep it under 60 lines.
6. After project folders moved or new repositories were adopted: `atlas.py routes --execute`.
7. End with a three-line summary and the `Atlas:` line.
