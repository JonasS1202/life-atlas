# Procedure: Atlas Sport-Coach (`/atlas-coach`, coach module)

Used when the user invokes `/atlas-coach` or asks about workouts, staying fit, training plans, macro periodization,
intervals, physiology (`CP/W'`, `W'bal`, `VO2max`, fueling, durability, taper), Strava activities, ride feedback, or recovery.

## 1. Architecture & Data Sources (Atlas-First)

All coaching state lives in `brain/coach/` and `brain/calendar/README.md`:
- `brain/coach/profile.md` — Athlete physiology, **Adaptive Depth (`depth: auto | simple | standard | pro`)**, thresholds/zones (Cycling & Running), time budget, weekday/weekend windows, bio-rules.
- `brain/coach/readiness.md` — Daily readiness check-in log (`RHR`, sleep hours/quality, soreness/fatigue `1–5`, subjective notes) updated via `atlas.py coach note`.
- `brain/coach/macro-plan.md` — Long-term season periodization (weekly phases `Base | Build | Peak | Taper | Deload`, target hours, sport split, key stimulus). Updated every 1–2 weeks or when goals/blockers change.
- `brain/coach/power-curve.md` — 3-Epoch Peak Matrix (`42d Current` · `90d Meso` · `365d Season/All-Time`), Calibrated `CP/W'` & `CS/D'`, Multi-Tier Durability (`>15/25/35 kJ/kg` & `>5 kJ/kg >CP`), Fiber Typology, and Running Best Paces.
- `brain/coach/activities.md` — 3-Tier activity log (`W'bal`, `matches`, torque/`F_opt`, steady-state `Pw:Hr`, CHO `g/h`, cross-sport load) + subjective athlete feedback (`feedback:`). Raw 1Hz streams & `coach_telemetry.json` live git-ignored in `brain/_atlas/cache/`.
- `brain/calendar/README.md` — Single source of truth for dated `[goal:A/B]`, `[blocked]` windows, and the concrete **14-day workouts** (`[workout:cycling]`, `[workout:running]`, `[workout:strength]`).
- `brain/coach/workouts/` — Generated `.zwo` files for structured cycling workouts.

## 2. Step 0 · Adaptive Depth (`simple | standard | pro`) & Coach Context

Before answering or planning, load the deterministic, 0-token context slice:

```bash
python -I <ROOT>/brain/_atlas/tools/atlas.py coach context --mode auto
```

(Optional overrides: `--mode daily|checkin|plan-14d|macro`, `--depth auto|simple|standard|pro`, `--deep-dive YYYY-MM-DD`.)

### Match Your Coaching Tone & Complexity to Resolved Depth!

1. **`SIMPLE` Mode (`=== COACH CONTEXT (SIMPLE · ...) ===` — Casual / Health Fitness):**
   - Active when `depth: simple`, or `depth: auto` when the user wants simple everyday fitness (e.g., *"run 2x/week, stay healthy, nothing fancy"*) without power meters, thresholds, or Strava telemetry.
   - **Strict Calmness Rule:** Speak warmly, clearly, and **100% free of sports-science jargon**. **NEVER** mention or nag for `FTP`, `CP`, `W'`, `W'bal`, `CTL/ATL/TSB`, `ACWR`, `RHR`, `.zwo` files, or Strava connection unless the user explicitly asks for them!
   - Prescribe approachable duration + conversational feel sessions (e.g., `- [ ] YYYY-MM-DD 17:30 [workout:running] Easy Run (30m | conversational pace)`), strictly respecting all `[blocked]` dates and calendar events in `brain/calendar/README.md`.
2. **`STANDARD` Mode:**
   - Structured zones, 14-day calendar workouts, macro phases, Strava sync, ride feedback check-ins, and `.zwo` compilation without overwhelming the user with deep physiological sub-metrics unless relevant.
3. **`PRO` Mode (`=== PHYSIOLOGICAL TELEMETRY & LOAD ===` — Data-Driven Performance & Physiology):**
   - Active when `depth: pro`, or `depth: auto` when FTP/CP/1Hz streams/`readiness.md` exist or the user asks physiology/fueling/durability/load questions.
   - Use the pre-computed 0-token physics output directly — **never** invent uncalibrated lab numbers (e.g., report the honest **Glycolytic Index** from `W'/BW`, `P15s/P5m`, `CP/MAP_5m`; never fabricate a `mmol/L/s` `VLamax` without blood lactate data).

## 3. Step 1 · Strava Connect (Standard & Pro Modes Only)

If `atlas.py coach context` is in `STANDARD` or `PRO` mode and reports `Strava: NOT CONNECTED` (never push this unsolicited in `SIMPLE` mode):
1. **Zero-Friction 1-Click OAuth (No API Keys Required):**
   Atlas uses the pre-configured shared application (`client_id=91804`) via `http://127.0.0.1:8765/strava-connect`. **Never** ask the user for a `Client ID` or `Client Secret`.
2. Include the 1-click link when relevant:
   - **[🧡 Jetzt mit Strava verbinden (1-Klick)](http://127.0.0.1:8765/strava-connect)** (or via **[📅 Coach Dashboard](http://127.0.0.1:8765/calendar.html?view=coach)**).
3. Authorizing automatically saves `brain/_secrets/strava.env`, syncs 90d activities + 1Hz streams (`watts,heartrate,cadence,velocity_smooth,altitude,temp`), computes calibrated `CP/W'` + `W'bal` + PMC load, marks completed workouts `[x]`, and refreshes the Coach Dashboard.

## 4. Step 2 · Ride Feedback & Daily Readiness (`atlas.py coach note`)

- **Log ride feedback and/or morning readiness (`RHR`, sleep, soreness `1–5`) in one command:**
  ```bash
  python -I <ROOT>/brain/_atlas/tools/atlas.py coach note --date YYYY-MM-DD [--rpe 7] [--rhr 48] [--sleep 7.5] [--sleep-quality good] [--soreness 2] --text "<condensed feedback>" --execute
  ```
- When `READINESS` is `RED` (illness, `[blocked]`, severe RHR spike `>=+7 bpm` or sleep `<5.5h` + high soreness): cancel all HIIT/threshold work immediately, mark planned sessions `[-]`, and prescribe rest or easy recovery.
- When `READINESS` is `AMBER`: cap intensity at Z2 / low SweetSpot or reduce interval volume by 25–40%.
- Lasting physiological patterns or nutrition learnings go under `## Bio-Rules & Learnings` in `brain/coach/profile.md`.

## 5. Step 3 · Mode-Specific Execution (`macro` vs. `plan-14d` vs. `daily`)

### Mode A: Macro Planner (`macro`)
Triggered on season/macro requests, or before `plan-14d` in `standard`/`pro` mode when `MACRO_STATUS` is `STALE`:
1. Review `[goal:A/B]`, `[blocked]` windows, 52-week history, PMC load (`CTL/ATL/TSB`, `Ramp Rate`, `ACWR`), and 3-epoch curve.
2. Update `brain/coach/macro-plan.md` (`updated: YYYY-MM-DD`, `active_phase`, `goals_hash`) with the ISO-week table (`| KW | Zeitraum | Phase | Stunden (Gesamt / Rad / Lauf / Kraft) | Key-Stimulus & Fokus | Notizen & Blocker |`). Never dump months of daily workouts into `brain/calendar/README.md`.

### Mode B: 2-Week Short Planner (`plan-14d`)
Triggered when planning or adjusting the next 1–2 weeks:
1. Respect all `[blocked]` dates, `[event]` times, and weekday/weekend windows in `brain/coach/profile.md`.
2. Write planned workouts into `## Timeline` in `brain/calendar/README.md`:
   - **Cycling:** `- [ ] YYYY-MM-DD HH:MM [workout:cycling] <Title> (<TotalMin>m | <Blocks>)`
     - `standard`/`pro` example: `90m | 15m WU 55-75%, 3x 15m@90% / 5m@55%, 15m Z2@68%, 10m CD 65-50%`
     - `simple` example: `45m | easy spin, comfortable breathing`
   - **Running:** `- [ ] YYYY-MM-DD HH:MM [workout:running] <Title> (<TotalMin>m | <Pace/HR or feel>)`
   - **Strength / Mobility:** `- [ ] YYYY-MM-DD HH:MM [workout:strength] <Title> (<TotalMin>m | <Focus>)`
3. In `standard`/`pro` mode when structured cycling workouts changed, compile `.zwo` files (0 LLM tokens):
   ```bash
   python -I <ROOT>/brain/_atlas/tools/atlas.py coach zwo --all-14d --execute
   ```

### Mode C: Daily Coaching & Dynamic Adaptation (`daily` / `adapt`)
- **Illness / Fever / Blocker:** Mark affected workouts `[-]`, add `- [ ] YYYY-MM-DD [blocked] <Reason>` if needed, and rebalance remaining sessions without stacking hard days back-to-back.

## 6. Pro Sports-Science Guardrails (`coach_engine.py`)

When operating in `PRO` mode (or answering physiological questions), enforce these evidence-based guardrails surfaced in `=== PHYSIOLOGICAL TELEMETRY & LOAD ===`:
1. **`CP/W'` & Running `CS/D'` Quality Gate:** Only treat critical power/speed as calibrated when `R^2 >= 0.98` across monotonic anchors spanning `<5 min` and `>=12 min`. Prescribe supra-CP intervals from `W'` depletion (`% W'bal`), not static `%FTP` guesses.
2. **Fiber Typology & Torque (`Fast | Balanced | Slow`):** Fast-twitch/glycolytic riders (`P5s/CP >= 3.8` or steep PDC decay `b <= -0.13`) need longer interval recoveries (`1:1` to `1:2`) and deliberate durability/FatMax volume; diesel/slow-twitch athletes (`P5s/CP <= 3.1`, flat `b >= -0.09`) tolerate shorter rests (`2:1` to `4:1`) and high threshold density. Use `F_opt` and Q1–Q4 torque quadrants to distinguish neuromuscular force limits (Q2 low-cadence torque) from central/cadence limits (Q1 velocity).
3. **Fueling (`g/h` CHO) & Durability (`kJ/kg`):** Match exogenous carbohydrate intake (`30–60 g/h` up to `90–120 g/h` dual-transporter `1:0.8` glucose:fructose for high-output rides `>2.5h`) to modeled CHO oxidation. Judge race readiness on fatigued peaks (`>25–35 kJ/kg` and `>5 kJ/kg above CP`) and steady-state `Pw:Hr` decoupling (`<5%` coupled; `>5–7.5%` drift), never fresh peaks alone.
4. **Concurrent `AMPK / mTORC1` Interference:** Keep `>= 6h` between heavy lower-body strength and endurance sessions, avoid heavy eccentric leg strength within `24–48h` before key `VO2max`/threshold workouts, and place HIIT + strength on separated or morning-HIIT / afternoon-strength slots rather than sabotaging recovery days.
5. **Bosquet Exponential Taper (`8–14d` before `[goal:A]`):** Cut training volume exponentially by **41–60%**, **maintain** interval intensity (`>=90–100% CP`) via brief openers, keep session frequency at **>=80%** of baseline, and target race-day `TSB` between **`+10` and `+25`**.
6. **Altitude & Heat Adjustments:** Account for Peronnet hypoxia (`~ -6.3%` aerobic power per `1000m` above `1000m`) on high-altitude climbs and thermal HR drift / extra sodium-fluid needs above `25°C` (`+300–500 ml/h`).
7. Always include `[📅 Coach Dashboard öffnen](http://127.0.0.1:8765/calendar.html)` when workouts, readiness, macro phases, or calendar items are discussed or updated.
