# Module: coach

For endurance and hybrid athletes (cycling, running, strength). Installed with `atlas.py init --modules coach` (or enabled on an existing Atlas via `atlas.py coach --init --execute` / `/atlas-update`).

## What it adds

| Part | Where | Purpose |
|---|---|---|
| Skill `/atlas-coach` | `brain/_atlas/skills/atlas-coach/` | Trigger for the coach mode (also triggered by natural language via `atlas_hook.py`) |
| Procedure `coach` | `brain/_atlas/procedures/coach.md` | Workflow for Strava onboarding, 3-tier context loading, ride feedback check-ins, macro periodization, 14-day workout planning, and `.zwo` compilation |
| Coach Hub | `brain/coach/README.md` | Entry point to the athlete's profile, daily readiness, macro plan, 3-epoch power/pace curves, activity log, and `.zwo` workouts |
| Athlete Profile | `brain/coach/profile.md` | Adaptive depth (`auto \| simple \| standard \| pro`), thresholds & zones, weekly time budget, weekday/weekend windows, bio-rules |
| Daily Readiness | `brain/coach/readiness.md` | Morning check-in log (`RHR`, sleep hours/quality, soreness `1–5`) driving `GREEN \| AMBER \| RED` gating via `atlas.py coach note` |
| Macro Plan | `brain/coach/macro-plan.md` | Season periodization table by ISO calendar week (`Base`, `Build`, `Peak`, `Taper`, `Deload`) with `updated:` staleness tracking (>14 days or new goals/blockers) |
| Power & Pace Curves | `brain/coach/power-curve.md` | 3-Epoch peak matrix (`42d` · `90d` · `365d`), calibrated `CP/W'` & `CS/D'`, multi-tier `kJ/kg` durability, fiber typology, and running splits |
| Activity & Feedback Log | `brain/coach/activities.md` | 3-Tier training history (`W'bal`, `matches`, torque/`F_opt`, steady-state `Pw:Hr`, CHO `g/h`, cross-sport PMC load) + athlete feedback |
| Workout Library | `brain/coach/workouts/` | Deterministically compiled `.zwo` cycling workouts (explicit decimal power factors for Hammerhead Karoo & smart trainers) |

## How the Coach Engine works (`atlas.py coach` + `atlas_lib/coach_engine.py`)

1. **Adaptive Depth (`simple | standard | pro`):**
   - `simple` (or `auto` for casual fitness without power meters/Strava): calm, 100% jargon-free guidance respecting `calendar/` blockers, with zero nagging for `FTP`, `W'bal`, `CTL`, or `.zwo`.
   - `standard`: zones, 14-day calendar workouts, macro phases, Strava sync, and `.zwo` workouts.
   - `pro` (or `auto` with 1Hz streams / `readiness.md` / physiology queries): full 0-token sports-science telemetry (`CP/W'` & `CS/D'` with `R^2 >= 0.98`, 1Hz Skiba `W'bal`, fiber typology, torque/`F_opt`/Q1–Q4, Storer/Uth/VDOT `VO2max`, honest Glycolytic Index, `g/h` CHO fueling, multi-tier `15/25/35 kJ/kg` + `>5 kJ/kg >CP` durability, cross-sport `CTL/ATL/TSB/ACWR/Monotony`, `AMPK/mTORC1` interference & Bosquet taper guardrails, Peronnet altitude & heat drift).
2. **Strava Connection & Sync (`atlas.py coach auth-strava` & `atlas.py coach sync`):**
   - Stores OAuth credentials in `brain/_secrets/strava.env` (never committed).
   - Caches heavy 1Hz raw streams (`watts,heartrate,cadence,velocity_smooth,altitude,temp`) and `coach_telemetry.json` git-ignored in `brain/_atlas/cache/` while writing distilled metrics into `brain/coach/` and matching completed workouts (`[x]`) in `brain/calendar/README.md`.
3. **Deterministic Mode Triage (`macro` | `plan-14d` | `checkin` | `daily`):**
   - `atlas_hook.py` detects coaching requests in natural language, resolves depth (`simple`/`standard`/`pro`), checks macro staleness and pending ride/readiness check-ins, and injects structured telemetry into `calendar.html`.
4. **Zero-Token `.zwo` Workout Compiler (`atlas.py coach zwo`):**
   - Compiles human-readable interval specs in `brain/calendar/README.md` (e.g. `90m | 15m WU 55-75%, 4x 8m@102% / 4m@55%, 19m Z2@68%, 10m CD 65-50%`) directly into valid `.zwo` XML files and visual interval bars in `calendar.html`.
