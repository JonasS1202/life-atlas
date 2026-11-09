---
summary: 3-Tier Strava activity history with laps, peak powers/paces, decoupling, and subjective athlete feedback notes
tags: [sport, log]
aliases: [Activities, Strava Log, Trainingshistorie, Ride Notes, Fahrt-Feedback]
updated: 1970-01-01
---
# Training History & Ride Feedback

Stand: {{DATE}}. Part of [Sport-Coach Hub](README.md). Synchronized via `atlas.py coach sync --execute`; athlete
feedback is attached via `atlas.py coach note --date YYYY-MM-DD --text "..." --execute` and is never overwritten on
re-sync.

## Tier 1 · Recent Detail (Last 21 Days — Laps, Peaks & Athlete Feedback)

<!-- Detailed activity blocks with laps, peaks, decoupling, and subjective feedback -->
*(No Strava activities synced yet. Run `python -I brain/_atlas/tools/atlas.py coach sync --execute` after connecting Strava.)*

## Tier 2 · Meso Compact History (Weeks 4–12 — One Line per Activity)

| Date | Sport | Title | Duration | Distance | Load (NP / Pace) | Avg HR | Feedback / Note |
|---|---|---|---|---|---|---|---|

## Tier 3 · Macro Weekly Arc (Weeks 13–52 — Weekly Rollups)

| KW / Monday | Sessions | Total Hours | Bike / Run / Other | Total kJ / TSS | Key Peak / Highlight |
|---|---|---|---|---|---|
