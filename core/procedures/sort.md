# Procedure: file the inbox or a folder (`/atlas-sort`)

Goal: everything in `inbox/` (or in a folder the user names) ends up where it belongs, and the user knows where.

## Inbox

0. **Cloud inbox** (optional, `settings.md` → `inbox_cloud`, e.g. a folder the phone saves into): move its files into
   `brain/inbox/` first, with a plan section `cloud-inbox` and `atlas.py move` (this one cloud folder is the only one
   Atlas may empty; it moves files, never the folder itself).
1. List `inbox/` (ignore `README.md`). For each item, read enough to know what it is: open text and Markdown, read PDFs
   and images if the agent can, use the file name and date otherwise. Voice notes: use the transcript the phone made.
2. Decide its place with `INDEX.md` → *Where things go*:
   - **Knowledge** (a fact, a contract's key data, a letter's content): write or update the page in its area, person
     or project, with `summary:` and `tags:` (from `tags.md`). Add `due:` items for every date that matters (notice
     periods, payment dates) and file appointments, trips/blocked dates, goals, workouts or open action items in
     `brain/calendar/README.md`.
   - **The document itself** (PDF, scan, photo): it is evidence. Keep it next to its page:
     `areas/<area>/files/<YYYY-MM-DD>_<short-name>.<ext>` and link it from the page. Large media collections do not
     belong in Atlas; leave a pointer instead and ask the user where they want them.
   - **A dated event or conversation**: `journal/<year>/<YYYY-MM-DD>_<topic>.md`, plus the update on the area page.
   - **Credentials** in a document: copy them to `_secrets/` only if the user gave the document for that purpose;
     otherwise ask once (charter §1.6).
3. Move the item out of `inbox/` with Git (`git mv`) so history follows it. Nothing is deleted.
4. Report in a short table: item → where it went → what changed (pages, `due:` dates). End with the `Atlas:` line.

## A folder elsewhere on the computer

1. Run the inventory on just that folder:
   `python -I <ROOT>/brain/_atlas/tools/atlas.py inventory --scan "<folder>" --out <ROOT>/brain/_atlas/migrations/<date>_sort`
2. Read what is relevant. Distil knowledge into pages (as above). Originals stay where they are unless moving them
   clearly helps; then write a plan (`plan.json`, sections with `op`/`src`/`dst`, see `_atlas/procedures/move.md`),
   show it, and run `atlas.py move` only after the user's go for that section.
3. Note the folder on the page it relates to ("Originals: `<path>`") so Atlas finds them later.
4. Report as above.
