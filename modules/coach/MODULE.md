# Module: coach

For endurance and hybrid athletes (cycling, running, strength). Installed with `atlas.py init --modules coach` (or enabled on an existing Atlas via `atlas.py coach --init --execute` / `/atlas-update`).

## What it adds

| Part | Where | Purpose |
|---|---|---|
| Skill `/atlas-coach` | `brain/_atlas/skills/atlas-coach/` | Trigger for the coach mode (also triggered by natural language via `atlas_hook.py`) |
| Procedure `coach` | `brain/_atlas/procedures/coach.md` | Workflow for Strava onboarding, 3-tier context loading, ride feedback check-ins, macro periodization, 14-day workout planning, and `.zwo` compilation |
| Coach Hub | `brain/coach/README.md` | Entry point to the athlete's profile, macro plan, 3-epoch power/pace curves, activity log, and `.zwo` workouts |
| Athlete Profile | `brain/coach/profile.md` | Thresholds & zones (cycling FTP/CP/W'/HR, running threshold pace/HR), weekly time budget, weekday/weekend training windows, bio-rules |
| Macro Plan | `brain/coach/macro-plan.md` | Season periodization table by ISO calendar week (`Base`, `Build`, `Peak`, `Taper`, `Deload`) with `updated:` staleness tracking (>14 days or new goals/blockers) |
| Power & Pace Curves | `brain/coach/power-curve.md` | 3-Epoch peak matrix (`42d Current` · `90d Meso` · `365d Season/All-Time`), Fresh vs. Fatigued (`>1500 kJ` durability), and running split records |
| Activity & Feedback Log | `brain/coach/activities.md` | 3-Tier Strava training history (Tier 1: 14–21d full laps/peaks/feedback, Tier 2: weeks 4–12 compact, Tier 3: weeks 13–52 weekly rollups) |
| Workout Library | `brain/coach/workouts/` | Deterministically compiled `.zwo` cycling workouts (explicit decimal power factors for Hammerhead Karoo & smart trainers) |

## How the Coach Engine works (`atlas.py coach`)

1. **Strava Connection & Sync (`atlas.py coach auth-strava` & `atlas.py coach sync`):**
   - Stores OAuth credentials in `brain/_secrets/strava.env` (never committed).
   - Caches heavy 1Hz raw streams git-ignored in `brain/_atlas/cache/strava/` while writing distilled metrics, 3-epoch power curves, and athlete feedback into `brain/coach/` and matching completed workouts (`[x]`) in `brain/calendar/README.md`.
2. **Deterministic Mode Triage (`macro` | `plan-14d` | `checkin` | `daily`):**
   - `atlas_hook.py` detects coaching requests in natural language, checks whether Strava is connected, whether `brain/coach/macro-plan.md` is stale (>14 days old or new `[goal]`/`[blocked]` anchors), and whether recent activities lack athlete feedback (`[feedback: pending]`).
3. **Zero-Token `.zwo` Workout Compiler (`atlas.py coach zwo`):**
   - Compiles human-readable interval specs in `brain/calendar/README.md` (e.g. `90m | 15m WU 55-75%, 4x 8m@102% / 4m@55%, 19m Z2@68%, 10m CD 65-50%`) directly into valid `.zwo` XML files and visual interval bars in `calendar.html`.
