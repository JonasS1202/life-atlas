---
summary: Atlas Sport-Coach Hub: Adaptive Simple-to-Pro athlete profile, macro plan, 3-epoch power/pace curves, readiness, activity log & .zwo workouts
tags: [atlas, sport]
aliases: [Sport-Coach, Atlas Coach, Trainingssteuerung, Coaching Hub]
---
# Atlas · Sport-Coach Hub

Stand: {{DATE}}. Central coaching hub for **{{USER_NAME}}** (Cycling, Running & Strength), scaling seamlessly via `depth: auto | simple | standard | pro` in [profile.md](profile.md). Concrete daily workouts, race goals (`[goal:A/B]`), and unavailable windows (`[blocked]`) live in [`../calendar/README.md`](../calendar/README.md).

## Canonical Coach Pages

| Page | Purpose |
|---|---|
| [profile.md](profile.md) | Coaching depth (`auto \| simple \| standard \| pro`), biometrics, thresholds & zones (`FTP`, `CP/W'`, `CS/D'`, `LTHR`), typology, weekly schedule & bio-rules |
| [macro-plan.md](macro-plan.md) | Active season macro periodization table by ISO calendar week (`Base`, `Build`, `Peak`, `Taper`, `Deload`) |
| [power-curve.md](power-curve.md) | 3-Epoch Peak Matrix (`42d` · `90d` · `365d`), PDC Fiber Typology, Multi-Tier Durability (`15/25/35 kJ/kg` + `>CP`), Running Splits |
| [activities.md](activities.md) | 3-Tier training history with `W'bal`, torque/cadence, CHO oxidation, steady `Pw:Hr`, and subjective athlete feedback |
| [readiness.md](readiness.md) | Daily readiness & recovery log (`RHR`, sleep, soreness, `GREEN/AMBER/RED`) + longitudinal `CTL/ATL/TSB/ACWR` state |
| `workouts/` | Deterministically generated `.zwo` cycling workouts (explicit decimal power factors for Hammerhead Karoo & smart trainers) |

## Quick Commands (`atlas.py coach`)

- **Coach context & status (adaptive Simple/Standard/Pro slice):** `python -I _atlas/tools/atlas.py coach context --mode auto`
- **Sync Strava activities & 1Hz streams:** `python -I _atlas/tools/atlas.py coach sync --execute`
- **Save ride feedback & readiness check-in:** `python -I _atlas/tools/atlas.py coach note --date YYYY-MM-DD --rhr 48 --sleep 7.5 --rpe 7 --text "..."`
- **Compile `.zwo` workouts from 14d calendar:** `python -I _atlas/tools/atlas.py coach zwo --all-14d --execute`
