# Procedure: Atlas-First Calendar & To-Dos (`/atlas-calendar`)

Used when the user says "öffne meinen Kalender", "zeig mir meinen Kalender", "was steht im Kalender", asks about
appointments/to-dos, or invokes `/atlas-calendar`.

## 1. Strict Atlas-First Rule

- **"My calendar" is ALWAYS the native Atlas calendar** (`brain/calendar/README.md` rendered via `atlas.py calendar`).
- **NEVER open or link to `calendar.google.com`, Apple Calendar, or Outlook** when the user asks to open or view their
  calendar. External calendars are only passive `.ics` sync sources (`calendar_ics` in `settings.md`).
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
3. Starts the loopback server (`atlas-cal-v6`) on `http://127.0.0.1:<port>/calendar.html` (with CSRF-protected
   1-click To-Do/Workout checkbox toggling via `POST /toggle-todo`) and opens the rendered dashboard in the browser.

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
- After manual edits to `brain/calendar/README.md`, run `atlas.py calendar` (or `--open` if requested) so `calendar.html` updates immediately.
