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

(Use `python3` if `python` is not found.)

What this does automatically (0 LLM tokens):
1. Merges `brain/calendar/README.md` (`## Timeline` and `## Open To-Dos`) with all decentralized `due:` items across
   the brain, plus the SQLite knowledge graph (`index.sqlite`) and adaptive Coach state.
2. Regenerates the visual dashboard (`brain/_atlas/cache/calendar.html`) with 4 views (`Alle`, `🚴 Coach View`,
   `☑️ To-Dos & Fristen`, and `🕸️ Brain Graph`).
3. Starts the loopback server (`atlas-cal-v3`) on `http://127.0.0.1:<port>/calendar.html` (with CSRF-protected
   1-click To-Do/Workout checkbox toggling via `POST /toggle-todo`) and opens the rendered dashboard in the browser.

In your reply:
- Give the user the clickable `http://127.0.0.1:<port>/calendar.html` link printed by the command (`[📅 Atlas Kalender öffnen](http://127.0.0.1:.../calendar.html)`).
- Summarize the next upcoming items and top open to-dos in a few crisp lines.

## 3. Adding or updating events, blockers, goals, workouts & to-dos

- **Dated items** go into `brain/calendar/README.md` under `## Timeline`:
  `- [ ] YYYY-MM-DD [kind] What` (or `YYYY-MM-DD..YYYY-MM-DD`, optional `HH:MM` or `HH:MM-HH:MM`).
  Kinds: `[event]`, `[blocked]`, `[goal:A]`, `[workout:cycling]`, `[workout:running]`, `[todo]`, `[birthday]`, `[deadline]`.
- **Undated tasks** go into `brain/calendar/README.md` under `## Open To-Dos`:
  `- [ ] [high|med|low] Task description` (when the user assigns a date to a task later, move it up to `## Timeline`).
- After editing `brain/calendar/README.md`, run `atlas.py calendar` (or `--open` if requested) so the live HTML view
  updates immediately.
