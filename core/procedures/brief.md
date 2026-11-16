# Procedure: briefing (`/atlas-brief`, and the first chat of the day)

The session hook decides when a briefing is due (`brain/settings.md` → `briefing`) and hands over the facts. On
`/atlas-brief` collect them yourself: `atlas.py todo`, `atlas.py calendar --days <deadline_days>`, the files in
`brain/inbox/` (and the cloud inbox from settings), `atlas.py check` for maintenance/update/backup lines.

Give the user **3 to 6 short lines** (plus the numbered task check-in when tasks are open), most urgent first:
1. **Task check-in (once per day when tasks are open/overdue):**
   - Show the numbered urgent/open tasks (`1..N`, overdue & due-today first, then top `[high]` open to-dos).
   - Add one short triage line in the user's language offering the three quick actions: **check off** (*abhaken / erledigt*), **reschedule to a date** (*verschieben*), or **tackle right now** (*jetzt angehen*) — e.g. *"Kurz sagen: z. B. `1 erledigt`, `2 auf Fr verschieben` oder `1 jetzt angehen`."*
   - When the user responds with task choices, execute them immediately via `python -I <ROOT>/brain/_atlas/tools/atlas.py todo done|move|skip ...` (or open the linked page and execute/draft the task when they choose *tackle now*).
2. **Upcoming calendar & visual hub:** today's and upcoming events, workouts, blocked days, and future deadlines within the horizon, plus the clickable visual calendar link `[📅 Atlas Kalender öffnen](http://127.0.0.1:.../calendar.html)` printed by the hook or `atlas.py calendar` (never use a `file://` link).
3. **Inbox:** how many items wait; offer `/atlas-sort`.
4. **System health (one line each only if due):** maintenance (do it after their first request), update check, backup, items outside Atlas (Antigravity `/learn`) to adopt.
5. **Gap question:** if `questions: per-briefing`, at most **one** short question about a real gap that matters now (a missing notice period, an unknown contact, a project without a goal). Nothing worth asking: skip it.

Then handle the user's actual request right away without waiting for their task-triage reply. Do not repeat the briefing or task list in later chats today.
