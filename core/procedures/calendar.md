# Procedure: Atlas-First Calendar & To-Dos (`/atlas-calendar`)

Used when the user says "öffne meinen Kalender", "zeig mir meinen Kalender", "was steht im Kalender", asks about
appointments/to-dos, or invokes `/atlas-calendar`.

## 1. Strict Atlas-First Rule

- **"My calendar" is ALWAYS the native Atlas calendar** (`brain/calendar/*.md` rendered via `atlas.py calendar`).
- **NEVER open or link to `calendar.google.com`, Apple Calendar, or Outlook** when the user asks to open or view their
  calendar. External calendars sync cleanly into `brain/calendar/*.md` via `atlas.py cal` (or passive `.ics` feeds via `calendar_ics`).
- **NEVER link to `file:///.../calendar.html` in chat**, because IDEs like Antigravity and VS Code open `file://` links
  as raw HTML source code in the text editor. Always use the `http://127.0.0.1:<port>/calendar.html` link printed by
  `atlas.py calendar`!

## 2. Opening & showing the calendar

Run:

```bash
python -I <ROOT>/brain/_atlas/tools/atlas.py calendar --open
# Or open directly in Brain Graph / Coach / To-Do tab:
python -I <ROOT>/brain/_atlas/tools/atlas.py calendar --view graph --open
```

(On Windows use `python -I` — never `python3`, which triggers the Microsoft Store stub; on Linux/macOS without `python` use `python3 -I`, matching the `Tool:` line in `ROUTER.md`.)

What this does automatically (0 LLM tokens):
1. Merges `brain/calendar/README.md` (`## Timeline` and `## Open To-Dos`) with all decentralized `due:` items across
   the brain, plus the SQLite knowledge graph (`index.sqlite`) and adaptive Coach state.
2. Regenerates the visual dashboard (`brain/_atlas/cache/calendar.html`) with 4 views (`Alle`, `🚴 Coach View`,
   `☑️ To-Dos & Fristen`, and `🕸️ Brain Graph`).
3. Starts the loopback server (`atlas-cal-v7`) on `http://127.0.0.1:<port>/calendar.html` (with CSRF-protected
   1-click To-Do/Workout checkbox toggling via `POST /toggle-todo`, OAuth loopback routes, and optional background calendar sync) and opens the rendered dashboard in the browser.

In your reply:
- Give the user the clickable `http://127.0.0.1:<port>/calendar.html` link printed by the command (`[📅 Atlas Kalender öffnen](http://127.0.0.1:.../calendar.html)`).
- Summarize the next upcoming items and top open to-dos in a few crisp lines.

## 3. Quick task triage (`atlas.py todo`) & manual calendar edits

Use `atlas.py todo` for atomic, zero-token task operations (automatically syncs `index.sqlite` and refreshes `calendar.html`):

```bash
# List numbered actionable tasks (overdue, due today, and open to-dos; add --all for future dates):
python -I <ROOT>/brain/_atlas/tools/atlas.py todo

# Check off (`[x]`) or skip (`[-]`) by briefing #number, task text, or --file/--line:
python -I <ROOT>/brain/_atlas/tools/atlas.py todo done 1
python -I <ROOT>/brain/_atlas/tools/atlas.py todo done "Tax return"
python -I <ROOT>/brain/_atlas/tools/atlas.py todo skip 2

# Reschedule a dated task/due: item OR schedule an undated Open To-Do onto a date:
python -I <ROOT>/brain/_atlas/tools/atlas.py todo move 2 --to 2026-11-20
python -I <ROOT>/brain/_atlas/tools/atlas.py todo move "Tax return" --to +3d

# Add a new dated `[todo]`, `[event]`, `[deadline]`, `[blocked]`, or `[goal]` (with optional `--time`) or undated Open To-Do:
python -I <ROOT>/brain/_atlas/tools/atlas.py todo add "Call dentist" --to tomorrow
python -I <ROOT>/brain/_atlas/tools/atlas.py todo add "Dentist appointment" --to 2026-11-25 --time 10:15 --kind event
python -I <ROOT>/brain/_atlas/tools/atlas.py todo add "Review backup disk" --prio high
```

For multi-line edits or events/workouts/goals/blockers in `brain/calendar/README.md`:
- **Dated items** go under `## Timeline`:
  `- [ ] YYYY-MM-DD [kind] What` (or `YYYY-MM-DD..YYYY-MM-DD`, optional `HH:MM` or `HH:MM-HH:MM`).
  Kinds: `[event]`, `[blocked]`, `[goal:A]`, `[workout:cycling]`, `[workout:running]`, `[todo]`, `[birthday]`, `[deadline]`.
- **Undated tasks** go under `## Open To-Dos`:
  `- [ ] [high|med|low] Task description` (moving to a date via `atlas.py todo move` promotes it to `## Timeline`).
- After manual edits to `brain/calendar/*.md`, run `atlas.py calendar` (or `--open` if requested) so `calendar.html` updates immediately.

## 4. Bidirectional Google Calendar Sync, FreeBusy, Invites & RSVP (`atlas.py cal`)

Optional, zero-token bidirectional multi-account sync powered by Google Calendar REST API v3 (`brain/_secrets/gcal.env`, `chmod 600`; config in `brain/calendar/sync.yaml`; 3-way state & metadata cache in `brain/_atlas/cache/cal_sync.sqlite`).

### Setup (once per account)

```bash
# 1. Import Google Cloud Desktop-App OAuth JSON (set Consent Screen to "In Production" so refresh tokens do not expire after 7 days):
python -I <ROOT>/brain/_atlas/tools/atlas.py cal auth --account personal --client-json ~/Downloads/client_secret_*.json
# 2. Authorize additional accounts (e.g. work / company workspace) via 1-click loopback callback:
python -I <ROOT>/brain/_atlas/tools/atlas.py cal auth --account work
# 3. Discover layered calendars across all accounts, deduplicate shared calendarIds, and write `brain/calendar/sync.yaml`:
python -I <ROOT>/brain/_atlas/tools/atlas.py cal discover
python -I <ROOT>/brain/_atlas/tools/atlas.py cal discover --execute
# 4. Run initial 3-stage deduplication dry-run, inspect, then apply:
python -I <ROOT>/brain/_atlas/tools/atlas.py cal sync --dry-run
python -I <ROOT>/brain/_atlas/tools/atlas.py cal sync --execute
```

### Daily Commands & Safety-Gate Rules

- **Read-Only (always free):**
  - `atlas.py cal status` — connected accounts, layers, last sync timestamp, open conflicts.
  - `atlas.py cal list [--days 30] [--layer <id>] [--query <text>]` — upcoming synced items across all layers.
  - `atlas.py cal show "<query_or_uid>"` — full Google details (location, Google Meet link, attendees, RSVP, description).
  - `atlas.py cal freebusy --days 7 [--start-hour 8 --end-hour 20 --min-min 30]` — consolidated free slots across ALL accounts and layers (including read-only mirrors & workouts).
- **Own events without external attendees:**
  - Edit `brain/calendar/<layer>.md` or run `atlas.py todo add "<text>" --to <date> --time <HH:MM> --kind event [--cal <layer>]`.
  - The Stop hook automatically pushes changes to Google (0 LLM tokens); PreInvocation pulls incremental `syncToken` deltas automatically.
  - Per-line overrides: add `#local` to keep a specific entry strictly in Atlas (never pushed to Google), or `#sync` to force-sync a local-only kind (e.g. a single `[birthday]`).
- **External invites, Google Meet creation & RSVP (Strict Safety-Gate):**
  - **NEVER pass `--yes` without first showing the preview and getting explicit user confirmation** (Google sends real emails to third parties!).
  - Preview first:
    - `atlas.py cal create --title "Project Kickoff" --date 2026-12-03 --time 10:00-11:00 --account work --attendees "alice@example.com" --meet`
    - `atlas.py cal rsvp "Project Kickoff" --status accepted`
  - Only after explicit approval, re-run with `--yes`.
- **Conflicts & Bulk-Delete Protection:**
  - Uncertain duplicate matches or simultaneous conflicting edits are queued rather than silently duplicated/overwritten: inspect with `atlas.py cal conflicts` and resolve with `--resolve <id> --action link|keep-both|use-atlas|use-google|dismiss`.
  - Remote deletes exceeding `bulk_delete_limit` (default `3`) trip the Bulk-Delete Circuit Breaker until explicitly confirmed with `--execute --allow-bulk-delete`.
