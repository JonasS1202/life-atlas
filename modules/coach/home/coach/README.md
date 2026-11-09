---
summary: Atlas Sport-Coach Hub: Athlete profile & zones, season macro plan, 3-epoch power/pace curves, Strava activity log & .zwo workouts
tags: [atlas, sport]
aliases: [Sport-Coach, Atlas Coach, Trainingssteuerung, Coaching Hub]
---
# Atlas · Sport-Coach Hub

Stand: {{DATE}}. Central coaching hub for **{{USER_NAME}}** (Cycling, Running & Strength). Concrete daily workouts, race
goals (`[goal:A/B]`), and unavailable windows (`[blocked]`) live in [`../calendar/README.md`](../calendar/README.md).

## Canonical Coach Pages

| Page | Purpose |
|---|---|
| [profile.md](profile.md) | Athlete biometrics, thresholds & zones (FTP, CP, W', HRmax, Running Pace), weekly time budget, weekday/weekend slots & learned bio-rules |
| [macro-plan.md](macro-plan.md) | Active season macro periodization table by ISO calendar week (`Base`, `Build`, `Peak`, `Taper`, `Deload`) |
| [power-curve.md](power-curve.md) | 3-Epoch Peak Matrix (`42d Current` · `90d Meso` · `365d Season/All-Time`), Fresh vs. Fatigued (`>1500 kJ`), Running Best Paces |
| [activities.md](activities.md) | 3-Tier Strava training history with laps, peaks, and subjective post-ride feedback (`feedback:`) |
| `workouts/` | Deterministically generated `.zwo` cycling workouts (explicit decimal power factors for Hammerhead Karoo & smart trainers) |

## Quick Commands (`atlas.py coach`)

- **Coach context & status (3-tier slice):** `python -I _atlas/tools/atlas.py coach context --mode auto`
- **Sync Strava activities & 1Hz streams:** `python -I _atlas/tools/atlas.py coach sync --execute`
- **Save ride feedback note:** `python -I _atlas/tools/atlas.py coach note --date YYYY-MM-DD --text "..." --execute`
- **Compile `.zwo` workouts from 14d calendar:** `python -I _atlas/tools/atlas.py coach zwo --all-14d --execute`
