# Procedure: Atlas Sport-Coach (`/atlas-coach`, coach module)

Used when the user invokes `/atlas-coach` or asks about workouts, training plans, macro periodization, intervals,
Strava activities, ride feedback, or recovery.

## 1. Architecture & Data Sources (Atlas-First)

All coaching state lives in `brain/coach/` and `brain/calendar/README.md`:
- `brain/coach/profile.md` — Athlete physiology, thresholds/zones (Cycling & Running), time budget, weekday/weekend windows, bio-rules.
- `brain/coach/macro-plan.md` — Long-term season periodization (weekly phases `Base | Build | Peak | Taper | Deload`, target hours, sport split, key stimulus). Updated every 1–2 weeks or when goals/blockers change.
- `brain/coach/power-curve.md` — 3-Epoch Peak Matrix (`42d Current` · `90d Meso` · `365d Season/All-Time`), Fresh vs. Fatigued (`>1500 kJ`), plus Running Best Paces.
- `brain/coach/activities.md` — 3-Tier Strava activity log + subjective athlete feedback (`feedback:`). Raw 1Hz streams are cached git-ignored in `brain/_atlas/cache/strava/`.
- `brain/calendar/README.md` — Single source of truth for dated `[goal:A/B]`, `[blocked]` windows, and the concrete **14-day workouts** (`[workout:cycling]`, `[workout:running]`, `[workout:strength]`).
- `brain/coach/workouts/` — Generated `.zwo` files for cycling workouts.

## 2. Step 0 · Run the Coach Engine (`atlas.py coach context`)

Before answering or planning, load the surgical, token-efficient context slice for the active mode:

```bash
python -I <ROOT>/brain/_atlas/tools/atlas.py coach context --mode auto
```

(Or explicitly `--mode daily`, `--mode checkin`, `--mode plan-14d`, `--mode macro`, plus `--deep-dive YYYY-MM-DD` when
the athlete references a specific older activity or event.)

## 3. Step 1 · First-Time Setup & 1-Click Strava Connect

If `atlas.py coach context` reports `Strava: NOT CONNECTED`:
1. **Zero-Friction 1-Click OAuth (No API Keys Required):**
   Atlas comes pre-configured with the official shared Vulpiq / Atlas Strava application (`client_id=91804`), and the local
   background server (`http://127.0.0.1:8765`) handles `/strava-connect` and `/strava-callback` automatically.
   **Never** ask the user to visit `strava.com/settings/api` or enter a `Client ID` / `Client Secret`!
2. Simply include the clickable 1-click link printed by `atlas.py coach context` (or `atlas.py coach auth-strava`)
   directly in your reply:
   - **[🧡 Jetzt mit Strava verbinden (1-Klick)](http://127.0.0.1:8765/strava-connect)**
   - Or tell them they can click **`🧡 Mit Strava verbinden (1-Klick)`** directly in the top banner of the
     **[📅 Coach Dashboard](http://127.0.0.1:8765/calendar.html?view=coach)**.
3. The moment the user clicks that link and clicks **"Autorisieren"** on Strava, the background server automatically
   exchanges the OAuth token, stores it in `brain/_secrets/strava.env`, syncs the last 90 days of rides + 1Hz streams +
   3-epoch power curve (`atlas.py coach sync --execute`), marks completed calendar workouts `[x]`, and redirects their
   browser back to the Coach Dashboard.
4. If `brain/coach/profile.md` still has placeholder values, ask for (or extract from `brain/` and Strava) their current
   thresholds, weekly time budget, and weekday/weekend training slots.

## 4. Step 2 · Ride Feedback Check-in (`[feedback: pending]`)

Whenever `atlas.py coach context` lists recent activities with `⚠ FEEDBACK PENDING` (or the user shares how a ride/run felt):
- **If the user just shared feedback** on a ride/run: persist it immediately with:
  ```bash
  python -I <ROOT>/brain/_atlas/tools/atlas.py coach note --date YYYY-MM-DD --text "<condensed 1st-person feedback: RPE, legs, fueling, context>" --execute
  ```
  If the feedback reveals a lasting biological pattern or preference, also add it under `## Bio-Rules & Learnings` in
  `brain/coach/profile.md`.
- **If recent activities have no feedback yet**: ask one concise, natural check-in question citing the ride's actual
  numbers (duration, NP/pace, HR, intervals) before or alongside your answer.

## 5. Step 3 · Mode-Specific Execution (`macro` vs. `plan-14d` vs. `daily`)

### Mode A: Macro Planner (`macro`)
Triggered when the user asks for season/macro planning, OR automatically before `plan-14d` when `MACRO_STATUS` is
`STALE` (`updated:` in `brain/coach/macro-plan.md` is >14 days old, missing, or `[goal]`/`[blocked]` anchors changed):
1. Review all `[goal:A]`, `[goal:B]` and multi-day `[blocked]` entries in `brain/calendar/README.md`, the 52-week
   Tier 1–3 history, and the 3-epoch power/pace curve.
2. Update `brain/coach/macro-plan.md`:
   - Set frontmatter `updated: YYYY-MM-DD` (today), `active_phase: <Phase>`, and `goals_hash: <hash from coach context>`.
   - Write the week-by-week table (`| KW | Zeitraum | Phase | Stunden (Gesamt / Rad / Lauf / Kraft) | Key-Stimulus & Fokus | Notizen & Blocker |`) from the current week up to the main season goals.
   - Do **not** write individual daily workouts months in advance into `brain/calendar/README.md`.

### Mode B: 2-Week Short Planner (`plan-14d`)
Triggered when the user asks to plan or adjust the next 1–2 weeks:
1. **Macro Prerequisite:** If `MACRO_STATUS` is `STALE`, update `brain/coach/macro-plan.md` first (Mode A) so the 14-day
   plan aligns with the current macro block.
2. **Schedule Concrete 14-Day Workouts in `brain/calendar/README.md`:**
   - Strictly respect all `[blocked]` dates, dated appointments (`[event]` with times), and the weekday vs. weekend time
     windows from `brain/coach/profile.md`.
   - Write each planned session into `## Timeline` in `brain/calendar/README.md` using the structured workout syntax:
     - **Cycling (with deterministic `.zwo` spec in parentheses):**
       `- [ ] YYYY-MM-DD HH:MM [workout:cycling] <Title> (<TotalMin>m | <Blocks>)`
       Example: `- [ ] 2026-11-10 17:30 [workout:cycling] SweetSpot 3x15m (90m | 15m WU 55-75%, 3x 15m@90% / 5m@55%, 15m Z2@68%, 10m CD 65-50%)`
     - **Running:**
       `- [ ] YYYY-MM-DD HH:MM [workout:running] <Title> (<TotalMin>m | <Pace/HR structure>)`
     - **Strength / Mobility:**
       `- [ ] YYYY-MM-DD HH:MM [workout:strength] <Title> (<TotalMin>m | <Focus>)`
3. **Compile `.zwo` Files & Refresh Visual Dashboard (0 LLM tokens):**
   After writing the 14-day workouts to `brain/calendar/README.md`, always run:
   ```bash
   python -I <ROOT>/brain/_atlas/tools/atlas.py coach zwo --all-14d --execute
   ```
   This compiles `.zwo` files (with explicit decimal power factors for Hammerhead Karoo / Smart-Trainer compatibility)
   into `brain/coach/workouts/` and updates the visual dashboard (`http://127.0.0.1:8765/calendar.html`).

### Mode C: Daily Coaching & Dynamic Adaptation (`daily` / `adapt`)
Triggered when the athlete asks *"What is my workout today?"*, reports illness/fatigue, or announces a spontaneous blocker:
- **Illness / Fever:** Enforce strict rest (zero intensity), mark affected planned workouts in `brain/calendar/README.md`
  as `[-]` and add `- [ ] YYYY-MM-DD [blocked] <Symptom/Rest>`.
- **Missed Workout / Spontaneous Blocker:** Mark the missed session `[-]`, add the `[blocked]` window if applicable, and
  rebalance the remaining days of the week without stacking hard interval days back-to-back. Re-run
  `atlas.py coach zwo --all-14d --execute` if cycling workouts changed.

## 6. Coaching Intelligence & Guardrails (Template — Customized by Athlete)

<!-- Additional physiological laws, zone models, and coaching style rules are expanded here and in brain/coach/profile.md -->
- Ground every assessment in actual telemetry (`brain/coach/power-curve.md`, `brain/coach/activities.md`) read through the
  lens of the athlete's subjective `feedback:` notes.
- Always include the clickable `[📅 Coach Dashboard öffnen](http://127.0.0.1:8765/calendar.html)` link when workouts,
  macro phases, or calendar items are discussed or updated.
