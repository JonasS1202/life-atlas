---
summary: How Atlas behaves for this user: briefing, questions, rhythms, reminders, style
tags: [atlas, preference]
briefing: daily           # daily | weekly | off
questions: per-briefing   # per-briefing | evening-3 | weekly-session | off
evening_hour: 18          # for questions: evening-3 (first chat after this hour)
maintenance_days: 7       # 7-30: Atlas tidies itself this often (never off)
reminder: signals         # signals | every-turn | rare: when the stop hook asks to file what was learned
deadline_days: 30         # how far ahead the briefing looks
daily_log: on             # on | off: `Atlas:` lines into journal/<year>/<date>.md
inbox_cloud:              # optional: a cloud folder the phone saves into; Atlas empties it into inbox/
calendar_ics:             # optional: private .ics URL or path (Google/Apple Calendar) merged by atlas.py calendar
---
# How Atlas works for {{USER_NAME}}

Stand: {{DATE}}. These are defaults. The user changes them in plain words ("from now on three questions every evening",
"only a weekly briefing", "no questions"); Atlas edits the switches above, confirms in one line, and notes finer wishes
below. What can *not* be changed here is in `_atlas/charter.md` (safety, one brain, core protection).

## Finer wishes

- (Atlas adds wishes about tone, length, address, timing here.)
