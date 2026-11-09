# Procedure: briefing (`/atlas-brief`, and the first chat of the day)

The session hook decides when a briefing is due (`brain/settings.md` → `briefing`) and hands over the facts. On
`/atlas-brief` collect them yourself: `atlas.py calendar --days <deadline_days>`, the files in `brain/inbox/` (and the
cloud inbox from settings), `atlas.py check` for maintenance/update/backup lines.

Give the user **3 to 6 short lines**, most urgent first:
1. Overdue and due-today items (events, workouts, blocked days, to-dos, deadlines), then the next dates within the
   horizon, plus top `[high]` open to-dos from `brain/calendar/README.md`. Always include the clickable visual calendar
   link `[📅 Atlas Kalender öffnen](http://127.0.0.1:.../calendar.html)` printed by the hook or `atlas.py calendar`
   (never use a `file://` link).
2. Inbox: how many items wait; offer `/atlas-sort`.
3. One line each only if due: maintenance (do it after their first request), update check, backup, items outside Atlas
   (Antigravity `/learn`) to adopt.
4. If `questions: per-briefing`: at most **one** short question about a real gap that matters now (a missing notice
   period, an unknown contact, a project without a goal). Nothing worth asking: skip it.

Then handle the user's actual request. Do not repeat the briefing in later chats today. In a chat that was routed to a
project, mention life matters only when they are due today.
