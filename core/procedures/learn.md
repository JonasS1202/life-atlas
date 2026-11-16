# Procedure: Capture-First Memory, Placement, Extensions & `/atlas-learn`

## Capture-First Principle ("Default-Keep with Timestamps")

Atlas defaults to **remembering rather than forgetting**: whenever the user shares a lasting fact, number, tariff, date,
contact, decision, preference, project gotcha, or task status, file it **immediately in the same turn** — unless the
user explicitly says not to store it (`"nicht speichern"`, `"merke dir das nicht"`, `"off the record"`).

1. **Check placement first (`atlas.py where <topic>`):** Before creating any new `.md` file, run
   `python -I brain/_atlas/tools/atlas.py where <keywords>` to see existing candidate pages (always update/extend an
   existing page instead of creating duplicates!) and the user's canonical `areas/<group>/` folders + hub `README.md`.
2. **Stamp facts with dates:** Set `verified: YYYY-MM-DD` (and optional `valid_until: YYYY-MM-DD`, `sources: [...]`) in
   frontmatter or `(Stand: YYYY-MM-DD)` inline so later chats know how fresh every number or status is.
3. **Link & let Stop auto-heal:** Write `[[Target Page]]` wikilinks or relative Markdown links. On turn end, the `Stop`
   hook automatically compiles `[[wikilinks]]`, normalizes tag synonyms against `tags.md`, adds unambiguous `related:`
   frontmatter edges, syncs `index.sqlite`, and logs your `Atlas: <what changed where>` line with `HH:MM` timestamp into
   `journal/<year>/<date>.md`.

## Keep

- **Life:** facts about people (who, relation, contact, birthdays), contracts and accounts (provider, numbers, cost,
  term, notice period), devices, places, health basics the user shares; amounts and dates that matter (`due:` on the
  page or in `brain/calendar/README.md` under `## Timeline`); appointments, vacations/blocked periods (`[blocked]`),
  race/macro goals (`[goal]`), planned workouts (`[workout]`), and open tasks (`atlas.py todo` / `## Open To-Dos` in
  `brain/calendar/README.md`); decisions and their reason; plans and goals; what the user said about themself (`self/`).
- **The user's ways:** preferences, rules ("from now on…", "never…"), corrections of Atlas (the lesson goes to `howto/`,
  the wrong page gets fixed), how they want answers.
- **Code (project page):** gotchas found while debugging, how to build/test/deploy, decisions and why, what was
  verified live, and equally what was proven *not* to work.
- **Findings** worth having again (research results, a comparison, an explanation the user asked to keep): journal or
  the area page.

## Do not keep

- Anything the user explicitly says **not** to store (prompt hook emits `do_not_store`; stop hook suppresses nudges and
  daily logging for that turn).
- General encyclopedia knowledge, transient intermediate shell noise, duplicates of an existing page (update the page
  instead), or unshared credentials.

## Signals

The prompt hook detects:
- **Opt-out (`do_not_store`):** `"nicht speichern"`, `"don't save"`, `"off the record"` -> store nothing in `brain/`.
- **Explicit / Correction (`explicit`, `correction`):** `"merke dir"`, `"ab jetzt"`, `"nein, das stimmt nicht"` ->
  mandatory update; stop hook reminds if nothing was written.
- **Substantive Fact (`fact`):** concrete numbers/currencies, ISO/DACH dates, IDs, or status updates -> check whether the
  fact is already on its canonical page; if new or changed, file it now with timestamp.

## Update-Safe User Individualization (Custom Skills, CLI Commands & Overlays)

Everything under `brain/_atlas/` belongs to **Core** and is replaced by `/atlas-update` (`core-apply`). Everything
outside `brain/_atlas/` belongs to the user's **Grown** layer and is **never overwritten** by updates. Build per-user
integrations (e.g. Google Drive, Notion, Home Assistant, custom tax/invoice scripts) strictly in the Grown layer:

1. **Custom Skill (`brain/skills/<name>/SKILL.md`):**
   - Frontmatter supports `name:`, `description:` (<= 200 chars), and optional `triggers: [phrase1, phrase2]` / `aliases:`
     so the prompt hook automatically points to `.agents/skills/<name>/SKILL.md` (or `.claude/skills/<name>/SKILL.md`)
     whenever a user prompt mentions those keywords.
   - Subdirectories (`scripts/`, `references/`, `assets/`) inside `brain/skills/<name>/` are copied recursively and
     binary-safely when running `atlas.py wire --agent <agent> --execute`.
2. **Custom Atlas CLI Command (`atlas.py <cmd>`):**
   - Drop an executable Python/shell script into `brain/howto/scripts/<cmd>.py` or `brain/skills/<skill>/scripts/<cmd>.py`.
   - `atlas.py <cmd> [args...]` (and `atlas.py run <cmd> [args...]`) auto-discovers it (`atlas.py ext` lists all
     installed user commands) and runs it with `ATLAS_BRAIN` and `ATLAS_CORE_TOOLS` set in the environment.
3. **Core Skill Overlay (`brain/skills/atlas-<core>/SKILL.md`):**
   - Need personal account mappings or standing rules on top of a core skill like `atlas-mail` or `atlas-coach`? Create
     `brain/skills/atlas-<core>/SKILL.md`. `atlas.py wire` keeps the upstream core skill and appends a pointer to your
     personal overlay automatically on every update.
4. **Credentials (`brain/_secrets/<service>.env`):**
   - Store API tokens/OAuth paths in `brain/_secrets/<service>.env` (`chmod 600`, gitignored) and document only the
     variable names and workflow in `brain/howto/<service>.md` or the skill.

## `/atlas-learn` (review a whole chat)

1. Go through the chat: what lasting facts, numbers, decisions, or preferences came up that were not filed yet?
2. **Facts:** run `atlas.py where <topic>` and file them directly, with `summary:`, `tags:`, and `verified: YYYY-MM-DD`.
3. **Behaviour rules** (how to work, what to avoid): propose them; on yes, write them to `howto/<topic>.md`. A rule that
   every chat needs gets one line in the personal part of `ROUTER.md`, the detail stays in `howto/`.
4. **Procedures & Integrations** worth repeating: propose a custom skill/command in `brain/skills/<name>/` (with
   optional `triggers:` and `scripts/<cmd>.py`) and run `atlas.py wire --agent <each> --execute` so all agents get it.
5. Report what was filed, ending with the `Atlas:` line.

Antigravity's own `/learn` writes rules and skills into `GEMINI.md`, `.agents/` or `~/.gemini/`. When it runs, steer its
proposal into the brain instead (`howto/`, `brain/skills/`). Whatever lands outside anyway is reported in the briefing
and moved in with `atlas.py adopt`.
