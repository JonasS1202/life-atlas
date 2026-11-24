# Procedure: briefing (`/atlas-brief`, and the first chat of the day)

The session hook decides when a briefing is due (`brain/settings.md` → `briefing`) and hands over the facts. On
`/atlas-brief` collect them yourself: `atlas.py todo`, `atlas.py calendar --days <deadline_days>`, the files in
`brain/inbox/` (and the cloud inbox from settings), `atlas.py check` for maintenance/update/backup lines.

Atlas is **Chat-First**: present everything cleanly right in chat (no local HTML dashboards).
Give the user **3 to 6 short lines** (plus the numbered task check-in when tasks are open), most urgent first:

1. **Smart task check-in (once per day when tasks are open/overdue):**
   - Show the numbered urgent/open tasks (`1..N`, overdue & due-today first, then top `[high]` open to-dos) with their
     age badge (`+Nd` when overdue).
   - **Strict distinction — `[deadline]` vs. `[todo]`:**
     - Hard **`[deadline]`** items (contracts, taxes, notice dates) are **never** auto-moved. If overdue, flag clearly
       and ask whether done or if a new official deadline applies.
     - Flexible **`[todo]`** items follow `overdue_todo_policy` in `brain/settings.md` (default `brief_propose`):
       - **1–6 days overdue:** proactively propose a concrete light/free day from the hook's `Suggested light/free days`
         (never move silently without the user's go!).
       - **Stale (`≥ 7 days` overdue):** proactively offer 4 crisp options — **check off** (`done`), **reschedule** to a
         suggested free day (`move`), **park in undated backlog** (`unschedule` — clears calendar clutter without losing
         the task), or **drop** (`skip`).
   - Add one short triage line in the user's language, e.g. *"Kurz sagen: z. B. `1 erledigt`, `2 auf Do schieben`, `3 ins Backlog` oder `1 jetzt angehen`."*
   - When the user replies, execute immediately via `python -I <ROOT>/brain/_atlas/tools/atlas.py todo done|move|unschedule|skip ...`
     (or open the linked page and execute/draft the task when they choose *tackle now*). If external calendar sync is
     active, the Stop hook syncs the change automatically.
2. **Upcoming calendar & deadlines:** today's and upcoming events, workouts, blocked days, and future deadlines within
   the horizon directly in chat (plus any calendar sync conflict or token-renew notice if flagged).
3. **Inbox:** how many items wait; offer `/atlas-sort`.
4. **System health (one line each only if due):** maintenance (do it after their first request), update check, backup,
   items outside Atlas (Antigravity `/learn`) to adopt.
5. **Gap question:** if `questions: per-briefing`, at most **one** short question about a real gap that matters now (a
   missing notice period, an unknown contact, a project without a goal). Nothing worth asking: skip it.

Then handle the user's actual request right away without waiting for their task-triage reply. Do not repeat the briefing
or task list in later chats today.
