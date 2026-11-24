# Procedure: Chat-First Atlas Calendar & To-Dos (`/atlas-calendar`)

Used when the user asks about their calendar, schedule, free slots, appointments, deadlines, or to-dos, wants to connect
an external calendar, or invokes `/atlas-calendar`.

## 1. Chat-First & Atlas-Owned Source of Truth

- **Chat is ALWAYS the entry point.** Atlas does not maintain a local HTML dashboard (`calendar.html`). Show agendas,
  tasks, free slots, and event details cleanly right in chat.
- **`brain/calendar/` is ALWAYS the Source of Truth.** `brain/calendar/README.md` (plus optional layered files
  `brain/calendar/<layer>.md`) works **100 % standalone with zero connectors**, OR synced individually per layer
  (`bidirectional`, `read-only`, or `#local` per-line overrides) with the external calendar app the user prefers on
  desktop, phone, and watch (currently Google Calendar via `atlas.py cal`; passive `.ics` import/export via
  `atlas.py calendar --ics` / `--sync-ics`).

## 2. Viewing the calendar & agenda in chat

Run:

```bash
python -I <ROOT>/brain/_atlas/tools/atlas.py calendar --days 14
# Include undated tasks / all future items / sync Google Calendar first:
python -I <ROOT>/brain/_atlas/tools/atlas.py calendar --sync --days 30
python -I <ROOT>/brain/_atlas/tools/atlas.py calendar --all
```

(On Windows use `python -I` — never `python3`, which triggers the Microsoft Store stub; on Linux/macOS without `python`
use `python3 -I`, matching the `Tool:` line in `ROUTER.md`.)

What this does automatically (0 LLM tokens):
- Merges `brain/calendar/README.md` (`## Timeline` and `## Open To-Dos`) with all layered `brain/calendar/*.md` files
  and decentralized `due:` frontmatter items across the brain.
- Prints a concise chronological agenda + undated open to-dos for your reply. Summarize the next upcoming items and top
  open to-dos cleanly in chat.

## 3. Atomic task & event triage (`atlas.py todo`)

Use `atlas.py todo` for atomic, zero-token task & event operations (automatically creates `brain/calendar/README.md` if
missing and syncs `index.sqlite`; if external calendar sync is enabled, the Stop hook pushes changes automatically):

```bash
# List numbered actionable tasks (overdue with age/+Nd & STALE >=7d badges, due today, undated backlog, + free-day hints):
python -I <ROOT>/brain/_atlas/tools/atlas.py todo
python -I <ROOT>/brain/_atlas/tools/atlas.py todo list --all

# Check off (`[x]`), skip (`[-]`), or reopen by briefing #number, task text, or --file/--line:
python -I <ROOT>/brain/_atlas/tools/atlas.py todo done 1
python -I <ROOT>/brain/_atlas/tools/atlas.py todo done "Tax return"
python -I <ROOT>/brain/_atlas/tools/atlas.py todo skip 2

# Reschedule a dated task/due: item OR schedule an undated Open To-Do onto a concrete date:
python -I <ROOT>/brain/_atlas/tools/atlas.py todo move 2 --to 2026-11-28
python -I <ROOT>/brain/_atlas/tools/atlas.py todo move "Tax return" --to +3d

# Park an overdue/stale dated `[todo]` back into the undated `## Open To-Dos` backlog (clears calendar clutter; refuses hard `[deadline]`s):
python -I <ROOT>/brain/_atlas/tools/atlas.py todo unschedule 1

# Add a new dated `[todo]`, `[event]`, `[deadline]`, `[blocked]`, or `[goal]` (with optional `--time` & `--cal <layer>`) or undated Open To-Do:
python -I <ROOT>/brain/_atlas/tools/atlas.py todo add "Call dentist" --to tomorrow
python -I <ROOT>/brain/_atlas/tools/atlas.py todo add "Dentist appointment" --to 2026-11-25 --time 10:15 --kind event
python -I <ROOT>/brain/_atlas/tools/atlas.py todo add "Client sync" --to 2026-12-03 --time 10:00-11:00 --kind event --cal work
python -I <ROOT>/brain/_atlas/tools/atlas.py todo add "Review backup disk" --prio high
```

### Overdue Task Policy (`overdue_todo_policy` in `brain/settings.md`)
- **`brief_propose` (default):** Never silently shift dates. Surface age (`+Nd`) + suggested free days in the daily
  briefing; for stale `[todo]` items (`≥ 7d`), proactively offer `done` / `move` / `unschedule` (backlog) / `skip`.
  Hard `[deadline]` entries are never auto-moved.
- **`manual`:** Show overdue items in the briefing without proactive reschedule suggestions.
- **`rollover_today` (opt-in):** Automatically rolls open overdue `[todo]` entries (never `[deadline]`) onto today on
  the first chat of the day, appending `(orig YYYY-MM-DD)` so the real age stays transparent.

For multi-line manual edits in `brain/calendar/README.md`:
- **Dated items** go under `## Timeline`:
  `- [ ] YYYY-MM-DD [kind] What` (or `YYYY-MM-DD..YYYY-MM-DD`, optional `HH:MM` or `HH:MM-HH:MM`).
  Kinds: `[event]`, `[blocked]`, `[goal:A]`, `[workout:cycling]`, `[workout:running]`, `[todo]`, `[birthday]`, `[deadline]`.
- **Undated tasks** go under `## Open To-Dos`:
  `- [ ] [high|med|low] Task description` (moving to a date via `atlas.py todo move` promotes it to `## Timeline`).

## 4. Optional Bidirectional Google Calendar Sync (`atlas.py cal`)

Completely opt-in, zero-token multi-account 3-way sync via Google Calendar REST API v3 (`brain/_secrets/gcal.env`,
`chmod 600`; layer config in `brain/calendar/sync.yaml`; 3-way state & metadata cache in
`brain/_atlas/cache/cal_sync.sqlite`).

### 5-Step Google Cloud Setup (once, 0 EUR, no 7-day token expiry)

1. Open [Google Cloud Console](https://console.cloud.google.com/) and create a **dedicated Calendar project** (tip: do
   **not** mix restricted Google Health scopes into the same project so you can publish cleanly without 7-day token
   expiry).
2. Enable **Google Calendar API** under *APIs & Services → Library*.
3. Open *Google Auth Platform → Audience* (OAuth Consent Screen): choose **External**, add your email(s) under *Test
   users* (or fill Branding + click **Publish App / In Production** for permanent refresh tokens).
4. Open *Clients → + Create client* → Application type **Desktop app** → **Download JSON** (`client_secret_*.json`).
5. Run in Atlas (starts the minimal headless loopback OAuth callback on `127.0.0.1:8765` automatically):

```bash
# 1. Import OAuth Desktop-App JSON & authorize first account:
python -I <ROOT>/brain/_atlas/tools/atlas.py cal auth --account personal --client-json ~/Downloads/client_secret_*.json
# 2. Optional: authorize additional accounts (e.g. work / company workspace):
python -I <ROOT>/brain/_atlas/tools/atlas.py cal auth --account work
# 3. Discover calendars across all accounts, deduplicate shared calendarIds, and write `brain/calendar/sync.yaml`:
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
  - Individual per-layer modes (`bidirectional` or `read-only` in `sync.yaml`) plus per-line overrides: add `#local` to keep a specific entry strictly in Atlas (never pushed), or `#sync` to force-sync a local-only kind (e.g. a single `[birthday]`).
- **External invites, Google Meet creation & RSVP (Strict Safety-Gate):**
  - **NEVER pass `--yes` without first showing the preview and getting explicit user confirmation** (Google sends real emails to third parties!).
  - Preview first:
    - `atlas.py cal create --title "Project Kickoff" --date 2026-12-03 --time 10:00-11:00 --account work --attendees "alice@example.com" --meet`
    - `atlas.py cal rsvp "Project Kickoff" --status accepted`
  - Only after explicit approval, re-run with `--yes`.
- **Conflicts & Bulk-Delete Protection:**
  - Uncertain duplicate matches or simultaneous conflicting edits are queued rather than silently duplicated/overwritten: inspect with `atlas.py cal conflicts` and resolve with `--resolve <id> --action link|keep-both|use-atlas|use-google|dismiss`.
  - Remote deletes exceeding `bulk_delete_limit` (default `3`) trip the Bulk-Delete Circuit Breaker until explicitly confirmed with `--execute --allow-bulk-delete`.
