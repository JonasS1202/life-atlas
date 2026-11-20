# Changelog

Written for users: `/atlas-update` shows the sections between the installed and the new version.

## [1.4.10] - 2026-11-20

- **Zero-Loss To-Do & Calendar Persistence Hardening (`atlas_hook.py`, `cal_engine.py`, `atlas.py`, `calendar.md`):**
  - **Eliminates Silent False-Positive Deduplication:** Within `calendar/README.md`, distinct same-day tasks or undated to-dos sharing topic keywords are never fuzzy-dropped (only exact duplicates are merged), while cross-file mirror deduplication now requires `>= 2` overlapping tokens and reads decentralized checklists up to `256 KB` instead of truncating at `16 KB`.
  - **Unbounded Open-Task Visibility & Overflow Indicator:** `render_calendar_html()` now keeps **all** open items in `calendar.html` regardless of age or future horizon (including `> 90d` overdue tasks and multi-year deadlines up to 10 years out) while keeping completed history compact (`[-90d, +365d]`); the `PreInvocation` urgent-task line appends `(+N more — run atlas.py todo)` whenever more than 5 overdue/due-today items exist (0 extra tokens otherwise).
  - **Thread- & Process-Safe Atomic Writes (`brain_write_lock` + `atlas-cal-v5`):** Serializes all `calendar/README.md` and task mutations across threads and processes via cross-platform locking (`fcntl.flock` / `msvcrt`) + unique temp filenames (`pid + thread + nonce` + `os.fsync` + `os.replace`) across `cal_engine.py`, `atlas.py` (`write()`, Coach sync/field-test), and `atlas_hook.py` (`daily_log()`, `_save_json()`), preventing lost updates on parallel browser clicks or concurrent CLI runs.
  - **Stop-Hook Calendar Deletion Guard:** Before auto-committing, `stop()` compares `HEAD:calendar/README.md` against disk and fires a loud safety alert (with the exact recovery command `git -C brain show HEAD~1:calendar/README.md`) if any open non-workout task was deleted instead of marked `[x]`/`[-]`, rescheduled, or promoted (0 extra tokens on normal turns).
  - **Instant Browser-Toggle Git Persistence & Frontmatter `due:` Checkboxes:** Successful `POST /toggle-todo` clicks in the loopback dashboard (`atlas-cal-v5`, auto-reloading older `v4` servers) asynchronously commit and push only the toggled file without touching unrelated dirty files; non-yearly frontmatter `due:` deadlines now expose 1-click checkboxes in the dashboard (`can_toggle = True`).

## [1.4.9] - 2026-11-20

- **Native Cadence Targets (`@90rpm` / `@65-70rpm`) for `.zwo` (Hammerhead Karoo) & `.fit` Workouts (`atlas_hook.py`, `fit_writer.py`, `coach_connectors.py`, `atlas.py`, `coach.md`):**
  - **Extended Workout DSL:** Every cycling step (`WU`, `CD`, `ramp`, `free`, `SteadyState`, `Intervals`, `Nested Sets`, and `Compound Intervals`) now accepts optional exact (`@88rpm`) or range (`@65-70rpm`, `@105-115rpm`) cadence targets and surfaces them in Coach Dashboard workout blocks.
  - **Hammerhead Karoo-Safe `.zwo` Compiler:** Emits `Cadence="..."` (exact) and `CadenceLow="..." CadenceHigh="..."` (range) directly on ZWO blocks so Hammerhead Karoo displays live secondary RPM targets alongside primary Watt targets. Automatically unrolls repeating intervals with distinct On/Off cadence targets or ranges into explicit `<SteadyState>` pairs — working around Hammerhead's `.zwo` importer ignoring `CadenceResting` on `<IntervalsT>`.
  - **ANT+ `.FIT` Secondary Cadence Targets:** Encodes official FIT SDK `workout_step` secondary cadence fields (`secondary_target_type=3`, `custom_target_cadence_low/high`) whenever a workout specifies cadence targets (keeping non-cadence `.fit` files byte-identical).
  - **Clean Remote Re-Push (`atlas.py coach push --recreate --execute`):** Includes the compiled `.zwo` hash in sync signatures and adds `--recreate` to cleanly delete and re-upload existing Hammerhead workouts when structural upgrades land.

## [1.4.8] - 2026-11-19

- **Individual `tau_w_mult` W'-Reconstitution Calibration & Seasonal Updates (`pillars_engine.py`, `coach_engine.py`, `coach_connectors.py`, `atlas.py`, `coach.md`):**
  - **Exact 1-Hz Inverse Chorley Solver (`fit_tau_w_mult_from_bouts`):** Deterministically inverts the 1-Hz bi-exponential Chorley ODE (`W'_FC` + `W'_SC` with `creatine_flag` pool split) on 2-bout maximal reconstitution tests (`3m All-Out + 2m Z1 + 3m All-Out`) to calibrate an individual `tau_w_mult` (`0.35–2.50x`), exposed directly via `atlas.py coach simulate --fit-tau "3m=...,2m=...,3m=..."` and `atlas.py coach set-profile --tau-w-mult <val> --execute`.
  - **Automatic Stream Detection & Dynamic Seasonal Re-Solve:** `compute_stream_metrics()` detects genuine 2-bout maximal reconstitution tests on 1-Hz cycling streams (with a multi-set interval guard rejecting normal `3x3m`/`4x3m` workouts) and stores the raw bout anchors so Stage 1 automatically re-solves `tau_w_mult` whenever 90-day `(CP, W')` updates across the season. Calibrations older than `90` days are flagged `STALE >90d` in `pro` mode; legacy profiles without `tau_w_mult` default cleanly to `1.00x` (100% backwards-compatible).
  - **Full Pipeline Propagation:** Individual `tau_w_mult` multiplies cleanly with Townsend altitude hypoxia (`base_tau_w_mult * tau_w_altitude_mult`) across `[3_kinetics S3]`, `[3B_prescriptions]` (30/15s Float/Classic, Hard-Start 4x4m, 1:4 Shuttle), `coach simulate`, and the `coach push` 1-Hz Compiler-Guard.
- **Depth-Adaptive `Unit 1` & Fast-Myotype 3-Day Field-Test Split (`atlas.py`, `pillars_engine.py`, `coach.md`):**
  - **Depth-Adaptive `Unit 1`:** `atlas.py coach field-test --unit 1` automatically schedules `15s + 3m/2m/3m` (`W'`-Reconstitution) in `pro` mode and the streamlined `15s + 1x 3m All-Out` in `standard` mode (explicit overrides `--unit 1-std` and `--unit reconst` / `1-pro` always available).
  - **Fast-Myotype 3-Day Test Split (`--unit 2a` & `--unit 2b`):** Adds fresh single-anchor protocols `--unit 2a` (`12m` CP anchor) and `--unit 2b` (`5m` VO2max roof). Whenever `Z_Myo > +0.5` or `W' >= 24 kJ`, the Calibration Gate (and a `--unit 2` advisory) automatically recommends the 3-day split (`Unit 1 -> 48h -> Unit 2a -> 48-72h -> Unit 2b`) instead of combining `5m + 12m` in one session (preventing 3–6% peripheral SR-fatigue suppression on the 12m anchor).

## [1.4.7] - 2026-11-19

- **Complete 7-Pillar Physics Core Migration & Zero-Ad-Hoc `coach simulate` (`pillars_engine.py`, `coach_engine.py`, `atlas.py`, `coach.md`):**
  - **Intensity-Weighted Pre-Fatigue (`w_sub` vs. `w_sup`) & Sex Scaling:** Extends `compute_effective_cp_w_prime()` and `atlas.py coach simulate` with `--preload-sup-kj` (`kJ > CP`) and `--sex {male,female}` calibrated to Mateo-March et al. (2024/2025/2026: `2.5 / 5.0 / 7.5 kJ/kg > CP -> -2.2% / -6.1% / -16.2%` CP drop for males; `-29.6%` @ `5.0 kJ/kg > CP` for females, while preserving female sub-CP retention advantage) — works seamlessly with `--solve` bisection and scenario overrides (`--cp`, `--w-prime`, `--mass-kg`).
  - **Environment & Sensor Flags + Unified Townsend Altitude:** Replaces legacy Peronnet (1991) with Townsend 2-segment hypoxia (`CP > 1500m`, `W'` 100% up to `2760m` then `-18.3%/km`, `tau_W'` `1.35–1.50x` at `3000m` wired directly into the 1-Hz Chorley ODE), and exposes `--alt-m`, `--temp-c`, `--heat-accl-days`, `--indoor` (`indoor_cp_offset_w`), and `--drivetrain-loss` (`2.4%` default without PowerMatch) directly on `coach simulate`.
  - **OmniPD Myotype/`W'` Fallback & `>10%` Spike Quarantine:** Adds deterministic Myotype/`W'`-coupled fallback for `omni_decay_a_w` + `predict_omnipd_power()` when no `>30m` all-out anchor exists, and sharpens `filter_power_spikes()` to isolate non-progressive `>10%` (`>=35 W`) 1–2s glitches while preserving legitimate interval steps and sprint ramps (`>50` artifacts hard-locked out of records).
  - **Running Equivalence Bridge, Deterministic RED-S Guard & Readiness Overrides:** Adds `solve_running_cs_d_prime()` (`CS/D'` OLS), `compute_running_effectiveness()` (`RE`), and live stream/CLI wiring (`--run-splits`, `--grade`, `--morin`, `--re`) for Minetti `C_r(g)` and Morin `k_leg` (with automatic `run_k_leg_kn_m` & drop propagation into Pillars 4 & 7). Hardens `reds_warning_flag` to strict 3-factor detection (`>3%` 28d weight loss + suppressed `ln RMSSD_7d` + performance stagnation), fixes a float-rounding false-positive in `hrv_below_swc`, and adds `HRmax` ceiling blunting (`ROT`) and eccentric `k_leg` drop (`GELB-B`) overrides over green morning RHR/HRV.
  - **Unified Mader `FatMax`:** Standardizes `p_fatmax_w` and `fatmax_band_w` strictly to the recalibrated Mader `F_Ox(P)` `>=95%-MFO` plateau (removing the parallel `% CP` heuristic).
- **Comprehensive Physics Verification Suite (`tests/test_coach_physics.py`):**
  - Adds a 5-block deterministic unit & Golden-Master suite (`unittest` + `pytest`-compatible, auto-run by `tests/run.py`) verifying Kumpel A–E PDF archetypes, 64-kg Diesel vs. Fast-Myotype solver invariants (`-2%` form survival, `1:4` Shuttle, Compiler-Guard), Chorley conservation & `tau_FC=22.3s`/`tau_SC=503.5s`, Townsend/heat/indoor/drivetrain/spike/Mateo-March durability, and the Running Bridge + RED-S + `HRmax`/`k_leg` readiness overrides.

## [1.4.6] - 2026-11-19

- **Direct `--solve` 1-Hz Bisection Solver & Token-Saving Helpers (`atlas.py`, `pillars_engine.py`, `coach.md`):**
  - **One-Call Pre-Fatigue & Form Solver (`atlas.py coach simulate --solve`):** Solves individualized 1-Hz `W'bal` + `VO2(t)` targets (`Float 30/15`, `Classic 30/15`, `Hard-Start 4x4m`, `1:4 Lactate-Shuttle`, `MLSS`) directly under `--prior-kj` / `--preload-kj`, CSV batch `--cho-g-h` (e.g. `42.5,90`), and `--form-pct` (e.g. `0,-2`) alongside a Fresh baseline — eliminating manual trial-and-error bisection calls.
  - **Atomic Write Helpers & Context Date Header:** Adds `atlas.py coach set-profile` and `atlas.py coach add-rule` for single-command profile/playbook updates without extra file reads, plus `today: <Weekday> YYYY-MM-DD KW <W>` directly in the `coach context` header.
- **Physics & Parser Bugfixes (`atlas_hook.py`, `pillars_engine.py`, `coach_connectors.py`):**
  - **Nested-Set DSL Parser & Zero Silent Drops:** `parse_workout_spec()` now parses `3x (13x 30s@394W / 15s@206W) / 4m@150W` and `3x13x 30s@394W / 15s@206W` natively (including balanced nested parentheses inside calendar lines) and reports `unparsed_tokens` loudly in `coach simulate` & `coach push` instead of silently falling back to `RECOVERY_Z1`.
  - **Extreme-Diesel `FatMax` Calibration:** Caps the upper end of `f(CP, vLamax_met)` at `<= 76.5% CP` and reconciles low-`vLamax` (`< 0.45`) athletes with Mader's stoichiometric `95%-MFO` band and `<= 94% LT1` headroom so `FatMax` never collides with `LT1`.
  - **Diesel Safety Floor in `simulate_workout_stream()`:** Fixes inverted `min(3.0, 0.15 * W')` guard to `min(0.30 * W', max(0.10 * W', 3.0))` so small-`W'` Diesel profiles receive the full `3.0 kJ` blowout floor without false-positive blowouts on large-`W'` athletes.
  - **`creatine_flag` Biophysical Calibration:** Wires `creatine: true` into Meixner `t_alac` (`+0.35 s`) and Chorley fast PCr pool fraction (`fc_frac = 0.43` vs. `0.40`).
  - **Sub-Threshold (`4x10m @ MLSS`) Classification Fix:** Excludes long passive recovery intervals (`> 60 s`) from `work_block_watts` in `expand_spec_to_1hz_watts()` so `4x10m @ 95% CP` is classified as `THRESHOLD_OR_SHUTTLE_OPENER` rather than `AEROBIC_BASE_Z2`.

## [1.4.5] - 2026-11-19

- **Inverse Mader `vLamax_met`, Dynamic Sub-CP & 1-Hz `W'bal`/`VO2(t)` Solver (`pillars_engine.py`, `coach_engine.py`, `atlas.py`, `coach.md`):**
  - Separates metabolic `vLamax_met` (inverse Mader equilibrium) from the alactic 15s sprint proxy, computes phenotype-scaled `f(CP, vLamax_met)` Sub-CP anchors (`FatMax`, `LT1`, `MLSS`), adds constrained 1-Hz interval prescriptions (`[3B_prescriptions]`), `atlas.py coach simulate` with pre-fatigue `(prior_kj, cho_g_h)`, and the 1-Hz `coach push` Compiler-Guard.

## [1.4.4] - 2026-11-18

- **Maximal-Effort Durability Validity Gate (`pillars_engine.py`, `coach_engine.py`, `atlas.py`, `coach.md`):**
  - **Eliminates Passive Zone-2 Censoring Bias in Durability Curves:** `compute_pillar5_durability_and_tid()` now distinguishes **verified maximal efforts** (`> 1.03 * CP` in the Severe Domain with anaerobic `W'bal` depletion or `>= 91% HRmax`) from **passive submaximal Zone-2/tempo cruises** (`submax_untested`) across `>15 / >25 / >35 kJ/kg` tiers. Athletes who ride disciplined Zone 2 on 3–4h rides without a late all-out sprint/climb are no longer misdiagnosed with a `25–35%` late-ride durability drop; `power-curve.md`, `durability_5m_90d`, and `[CALIBRATION]` mark submaximal tiers explicitly as `submax-only, untested` until a Preload test (`Field-Test Unit 3`) or late race effort occurs.

## [1.4.3] - 2026-11-18

- **Zero-Truncation Progressive Context & Canonical File Schemas (`atlas.py`, `coach.md`):**
  - **Untruncated Core Context Slice:** `atlas.py coach context` now prints the live `Dashboard URL` explicitly and omits the verbose 65-row Tier-2 Meso walk/ride table across all modes (keeping Tier-1 last 21 days + Tier-3 monthly totals while delegating `> 21d` session lookups to `atlas.py coach history`), cutting output size by >50 % so tool calls never hit truncation limits.
  - **Inline File & DSL Schemas in `coach.md`:** Documents exact `macro-plan.md`, `dna.md` (`statcard` JSON), and `[workout:...]` interval DSL schemas directly in `coach.md` so the Coach agent writes valid files immediately without inspecting `_atlas/tools/*.py`.

## [1.4.2] - 2026-11-18

- **Broadened Natural-Language Coach Routing & Full Playbook Context (`atlas_hook.py`, `atlas.py`, `coach.md`):**
  - **Zero-Miss Prompt Routing (`COACH_RX_*`):** Natural-language German queries such as *"Was steht heute im Training an?"*, *"Formkurve der letzten 6 Monate"*, *"Mein Ruhepuls ist 48 bpm und ich nehme kein Kreatin"*, *"Wie war mein letzter Ride am 06.11.?"*, *"VO2max-Einheit für morgen"*, and *"4 W/kg"* now reliably trigger `Atlas coach ->` instead of falling through to generic text responses.
  - **Full Playbook Context & History Guidance:** Raises the `playbook.md` context cap in `atlas.py coach context` from 2200 to 6000 chars so empirical body rules in Section 3 are never truncated mid-heading, and documents `coach sync --days 180` + dynamic dashboard ports in `coach.md`.

## [1.4.1] - 2026-11-18

- **True 1-Click Hammerhead Karoo Connect (`coach_connectors.py`, `cal_engine.py`, `atlas.py`, `calendar.html`):**
  - **Zero Developer-App / API-Key Setup:** Replaces the manual `HAMMERHEAD_CLIENT_ID` / `HAMMERHEAD_CLIENT_SECRET` prompt on `/hammerhead-connect` with a true 1-Click OAuth flow (matching Strava 1-Click) backed by a CSRF-verified loopback state (`atlas:<port>:<nonce>`) and stateless OAuth token broker (`atlas-cal-v4` auto-reloads any running `v3` background calendar server upon `/atlas-update`). Athletes simply click **Karoo verbinden (1-Klick)** -> **Annehmen** on Hammerhead -> connected; tokens remain stored exclusively in local `brain/_secrets/hammerhead.env` (`0o600`), with optional custom app override preserved.

## [1.4.0] - 2026-11-18

- **7-Pillar Biophysical Engine & Zero-Fallback Missing-Data Gate (`pillars_engine.py`, `coach_engine.py`, `atlas.py`):**
  - **Deterministic 7-Pillar State Vector:** Adds 1Hz powermeter spike filtering (Mateo-March 2025, `> 50` artifact record lock), 7-day smoothed body composition (`FFM`, `M_akt`, individualized lactate distribution space `Vol_rel,ind`), `W'`-cleaned `VO2max_rel_clean` & `VO2max_FFM`, Meixner `vLamax_sprint` with Anchor Decay Guard, Mader-Heck-Hauser MLSS bisection solver + Lactate-Shuttle Efficiency `eta_shuttle`, stoichiometric `CHO/Fat` curves (`P_FatMax`, `MFO`, `P_CarbMax_90`), `CP/W'` OLS `SEE` gating + `MPR` + OmniPD `A`, Chorley dynamic bi-exponential `W'bal` (`tau_FC`, `tau_SC`), non-ERG sprint torque-velocity (`T_max`, `C_opt`) + functional Myotype Z-score `Z_Myo`, intensity-weighted durability (`w_sub <= CP` vs. `w_sup > CP`, cadence fade, Dual-TID), Bassett/measured `CdA` + `Compound Score` + learned `indoor_cp_offset_w` + Townsend altitude + RED-S guard + Minetti/Morin running bridge, and 4-stage readiness (`GRÜN / GELB-A / GELB-B / ROT` where peripheral leg conflict overrides green HRV) with Sanders `Q_Strain` / `Q_RPE:PO` ratios and Readiness TTL expiration.
  - **Zero Silent Fallbacks & Two-Stage Progressive Context:** `atlas.py coach context` explicitly flags missing biometric/calibration inputs (`weight_kg`, `kfa_pct`, `rhr`, `hr_max`, `age/sex`, `creatine_flag`, stale `15s`/`12m` anchors) with concrete questions instead of silent numeric defaults, while keeping the always-on slice compact and providing on-demand `atlas.py coach history`, `compare`, `inspect`, and `field-test` commands.
- **Autonomous Coach Agent, Accountability & Personal `playbook.md` (`coach.md`, `skeleton/coach/playbook.md`, `atlas_hook.py`):**
  - **Self-Driving Coaching Files & Coach Voice:** Audits and autonomously builds/updates stale `macro-plan.md`, `dna.md`, and `< 7d` workout horizons; detects unexcused skipped workouts and Plan-vs-Actual interval/Zone-2 discipline breaches (`evaluate_workout_execution`); pairs praise and blunt accountability (`coach_style`, `accountability`) with the Socratic Question Radar backed by `brain/coach/playbook.md` and empirical dose-response correlation (`compute_individual_response_model`).
  - **Multi-Turn Hook Continuity & Warmup Sync:** Broadens natural-language coach routing (fatigue, sleep, illness, devices), maintains follow-up context across multi-turn coaching Q&A, adds non-blocking delta sync on `SessionStart`, and surfaces a concise Coach Pulse in the daily briefing.
- **Hammerhead Karoo, Garmin Connect, Intervals.icu & Pure-Stdlib `.FIT` Sync (`coach_connectors.py`, `fit_writer.py`, `cal_engine.py`, `calendar.html`):**
  - **Hardware & Biometric Connectors:** Adds pure-stdlib ANT+ `.FIT` binary workout compilation (`fit_writer.py`), Hammerhead Karoo 1-Click OAuth & idempotent `.zwo` calendar CRUD (`POST/PUT/DELETE`), Garmin Connect wellness pull (`RHR`, `HRV RMSSD`, `sleep`, `weight` -> `readiness.md`) + structured watch/Edge workout push, optional Intervals.icu REST bridge, and `atlas.py coach push` / dashboard controls.

## [1.3.3] - 2026-11-16

- **Universal Brain Layouts & Small-Brain Recall (`graph_engine.py`, `atlas_hook.py`, `atlas.py`):**
  - **Fresh / Small-Brain DF Floor (`max_df >= 3`):** Ensures newly initialized or compact brains (`< 25` pages) never drop contract IDs, alphanumeric tickers, or unhubbed domain tags due to relative document-frequency thresholds (`max(3, ceil(max_df_share * n))`).
  - **Custom Top-Level Folders & Flat `areas/*.md` Support:** Replaces hardcoded top-level folder whitelists in `_vocab()`, `compute_prompt_hints()`, `build_entity_lexicon()`, `scan_retroactive_links()`, `atlas.py where`, and `atlas.py check` with `is_user_knowledge_rel()`, giving custom user directories (`brain/notes/`, `brain/work/`, `brain/study/`, etc.) and flat `areas/<topic>.md` setups first-class entity recall, hub routing, journal auto-linking, and health validation out of the box.
- **Native Windows & PowerShell `Stop`-Hook Hardening (`atlas_hook.py`, `calendar.md`, `mail.md`, `calendar.html`):**
  - **Windows / PowerShell Read-Only Command Recognition:** `_is_readonly_shell_cmd()` now recognizes Windows `python.exe` / `py -3` launchers (including quoted paths with spaces like `"C:\Users\...\python.exe"`), PowerShell/`cmd.exe` read-only commands (`Get-ChildItem`, `Get-Content`, `Select-String`, `Test-Path`, `dir`, `where.exe`, `findstr`), and benign null redirections (`2>$null`, `2>NUL`, `2>/dev/null`), while catching PowerShell write cmdlets (`Set-Content`, `Out-File`, `Remove-Item`, etc.) — preventing false `Stop` nudges on pure read-only turns on Windows.
  - **Consistent Cross-Platform Python Guidance:** Unifies `python -I` / `python3 -I` guidance across `calendar.md`, `mail.md`, and `calendar.html` clipboard hints so Windows users never hit the Microsoft Store `python3` alias stub.

## [1.3.2] - 2026-11-16

- **Zero-Friction Domain & Project-Mode Switching (`atlas_hook.py`, `router.md`):**
  - **Broadened `MAIL_RX`, `TODO_RX` & `SIGNAL_EXPLICIT`:** Recognizes plural and natural-language mail/calendar/save phrasings (`"neusten mails"`, `"inbox"`, `"Zahnarzt Termin"`, `"10:15 Uhr"`, `"speicher mir ab"`, `"trag ein"`, `"add to calendar"`) while ignoring mere `"E-Mail-Adresse"` mentions and staying strictly silent inside routed code projects (`Charter §1.5`).
  - **1-Hop Linked Brain Context on `Atlas route`:** When routing into a code project on Turn 1, surfaces up to 2 directly linked non-hub `brain/` pages (`Linked brain context (open only if needed): ...`) so host/environment specs are reachable in zero hops without filesystem scans; clarifies in `ROUTER.md` that `brain/` (not `ROOT`) is the Git repository and that project branches/paths belong on the project page.
  - **Direct Lead Read Guidance:** Prompt entity leads now instruct reading matched leads directly and calling `atlas.py find` only when a lead is missing, saving redundant search roundtrips.
- **Atomic Event & Time Scheduling in `atlas.py todo add` (`atlas.py`, `cal_engine.py`, `calendar.md`):**
  - **`--kind` & `--time` Flags:** `atlas.py todo add "<text>" --to YYYY-MM-DD [--time HH:MM[-HH:MM]] [--kind event|todo|deadline|blocked|goal]` inserts timed events, deadlines, and tasks chronologically into `calendar/README.md` and refreshes `calendar.html` in a single command.
- **Single-Connection Batch `mail read` & Clean Excerpts (`atlas_mail.py`, `mail.md`):**
  - **Multi-UID `mail read <UID1> [UID2 ...]` & `--body-chars N`:** Fetches multiple full messages over a single read-only IMAP connection (`BODY.PEEK[]`), strips invisible marketing preheader padding (`U+034F`, `U+200C`, soft hyphens) from snippets, and raises default `--with-body` excerpt length to 500 chars (configurable via `--body-chars`).

## [1.3.1] - 2026-11-16

- **Zero-Turn-Waste Python Command & Anti-Cache-Grep Guard (`atlas_hook.py`, `router.md`, `atlas-search.md`, `atlas.py`):**
  - **Auto-Detected `python3 -I` vs. `python -I`:** `_py_cmd()` in `atlas_hook.py` emits `python3 -I` on POSIX systems where `python` is absent, and `ROUTER.md` documents `python3 -I` (Windows: `python -I`), eliminating wasted `python: command not found` tool roundtrips on Turn 1.
  - **Markdown-Only Header Grep Guard:** Clarifies in `ROUTER.md` and `atlas-search.md` never to raw-grep `_atlas/` (`calendar.html`) or `files/` attachment directories.
  - **`core-apply` Updates `last_update_check`:** Running `/atlas-update` (`core-apply --execute`) now stamps `last_update_check` in `_atlas/state.json` so subsequent sessions do not create a redundant empty `atlas: update-check` commit.
- **Niche Entity Recall, Strict Numeric/Date Filtering & Area Hub Routing (`atlas_hook.py`, `graph_engine.py`):**
  - **Unaliased Niche Entity Recall (`_vocab`):** Extracts clean title heads (stripping `(...)` and `—` subtitles, legal suffixes like `e.V.`/`GmbH`/`GbR`, `: `/`&` sub-phrases), 2-word non-generic stem prefixes, and unambiguous `>= 6`-char stem nouns, while giving dedicated canonical pages priority over `glossary.md`.
  - **Strict ID vs. Date/Currency/Unit Filter:** Prevents localized dates (`01.01.2027`, `11/2026`), formatted amounts (`8.499`, `4,90`), units (`145fps`, `14d`), and `< 6`-digit numbers (`145` EUR) from triggering false-positive ID leads, while preserving alphanumeric tickers/plates (`DBX0AN`, `C24`, `IB-88663`) and `>= 6`-digit contract/policy IDs; caps prompt leads at `MAX_HINTS = 3`.
  - **Area Hub Routing on Broad Domain Queries:** When a domain tag (`#finanzen`, etc.) spans `>= 3` leaf pages without a specific leaf entity match, `compute_prompt_hints` surfaces the canonical `areas/<group>/README.md` hub instead of arbitrary alphabetical leaves.
- **Multi-Word Phrase & Compound Tokenization in `atlas.py where` & `atlas.py find` (`graph_engine.py`, `atlas.py`):**
  - **`_tokenize_search_terms`:** Splits quoted multi-word queries (`atlas.py where "Kfz-Versicherung Polestar 2"`) and hyphenated compounds into atomic non-stopword tokens while preserving exact phrase and model-bigram (`Polestar 2`, `Model Y`) boosts, and excludes structural folder hubs from `where` Section 1 candidate pages.

## [1.3.0] - 2026-11-16

- **Surgical Push + Agentic Pull Architecture (`atlas_hook.py`, `graph_engine.py`, `router.md`, Core Skills):**
  - **Zero Speculative Prompt Noise:** Removed speculative full-text (`free_tokens` FTS5) and blind 1-hop graph expansion from `PreInvocation` / `UserPromptSubmit`. The prompt hook now emits only deterministic signals: session bootstrap (`Atlas · ok` + once-daily briefing/task check-in), project routing (strictly silent once inside an active code project per Charter §1.5), domain pointers (`coach`, `mail`, `todo`), custom user extension triggers, capture/opt-out signals, and up to 3 exact named-entity / alias / contract-ID / concrete-domain-tag leads. Deep FTS5 + trigram + 1-hop graph traversal stays strictly on-demand via `atlas.py find` and `atlas.py context`.
  - **Core Skill Token-Budget Trim:** Condensed core `SKILL.md` descriptions (~700 chars saved per chat, comfortably inside the `< 2400` char budget).
- **Capture-First Memory, `atlas.py where` Placement Advisor & Zero-Token `Stop` Auto-Heal (`atlas_hook.py`, `atlas.py`, `graph_engine.py`, `learn.md`):**
  - **Capture-First ("Default-Keep with Timestamps"):** Atlas defaults to filing every new lasting fact, number, tariff, date, contact, decision, preference, or task update immediately with `verified: YYYY-MM-DD` / `(Stand: YYYY-MM-DD)` and `HH:MM` journal logs — unless the user explicitly opts out (`"nicht speichern"`, `"off the record"` -> `do_not_store` suppresses storage and daily logging).
  - **`atlas.py where <topic>` Placement & Grouping Advisor:** One-command lookup before creating a page that surfaces matching existing pages (extend before creating!) alongside the user's canonical `areas/<group>/` folders and hub `README.md` targets.
  - **Zero-Token Background Integrity on `Stop` (`auto_heal_pages`):** Before `autocommit_and_push()`, `stop()` inspects `git status` for newly created/modified `brain/` pages and automatically compiles `[[Wikilinks]]`, normalizes tag synonyms against `tags.md`, adds unambiguous `related:` frontmatter edges, and syncs `index.sqlite` with zero LLM overhead.
- **Update-Safe User Individualization (`atlas.py ext`, `atlas.py <custom_cmd>`, Recursive Skill Bundling):**
  - **Custom CLI Commands & Skills Survive `/atlas-update`:** Users can build custom integrations in the grown brain (`brain/skills/<name>/SKILL.md` with `triggers:`/`aliases:` and recursive `scripts/`/`references/` subdirectories, `brain/howto/scripts/<cmd>.py`, `brain/_secrets/<service>.env`, plus `brain/skills/atlas-<core>/SKILL.md` overlays). `atlas.py wire` bundles skill subdirectories binary-safely into both Claude Code and Antigravity, `atlas.py <cmd>` (and `atlas.py ext` / `atlas.py run`) executes custom commands directly with `ATLAS_BRAIN` set, and `core-apply` never touches them.

## [1.2.2] - 2026-11-16

- **Clean Antigravity `Stop` JSONL Parsing & Read-Only Turn Silence (`atlas_hook.py`):**
  - **Structured JSONL Turn Parser (`_parse_antigravity_turn`):** Replaces raw-byte substring scanning in `stop()` with proper JSONL parsing from the last `USER_INPUT` step onward. Only `PLANNER_RESPONSE.content` is inspected for `Atlas:` daily-log entries (preventing model `thinking` or tool outputs from leaking into `journal/`), and pure read-only lookups (`read_file`, `grep_search`, `ls`, `git status`, `atlas.py find/calendar/check`) no longer trigger false `Stop hook blocked termination` nudges.
- **Hidden Follow-Up `PreInvocation` Hints & Zero Pronoun/Meta-Word Hint Noise (`atlas_hook.py`, `graph_engine.py`, `skeleton/tags.md`):**
  - **Hidden `ephemeralMessage` After Session Bootstrap:** Turn 1 of each conversation/day uses persistent `systemMessage` for `Atlas · ok` + daily briefing, while subsequent turns emit `ephemeralMessage` so routine prompt hints stay invisible in the Thinking Path UI and never accumulate across turns.
  - **Eliminate `#self` Pronoun & Conversational Filler Noise:** Filtered pronouns and meta-chat words (`ich`, `selbst`, `moin`, `atlas`, `antwort`, `nachricht`, `prompt`, `screenshot`, etc.) out of tag-synonym and FTS5 prompt-hint matching, restricted 1-hop graph neighbor expansion to strong/entity-anchored matches, and let bare greetings (`"moin"`) surface overdue items concisely even in later chats of the day.

## [1.2.1] - 2026-11-16

- **Critical Hook Performance Fix — Eliminate Catastrophic Regex Backtracking (`graph_engine.py`, `atlas_hook.py`, `atlas.py`):**
  - **`O(N)` Linear-Time Prose & Code Masking (`mask_prose_for_mentions`, `mask_all_code`):** Replaced the multiline table-separator regex (`r"(?m)^\s*\|?[\s:-]+\|[\s|:-]*$"` — where `\s` matched across `\n` inside large blanked fenced code blocks and caused multi-minute `O(N³)` CPU hangs exceeding the 10s/15s hook timeout) and backtracking backtick regexes with pre-compiled single-line patterns and deterministic linear-time scanners (`>200 s` hang $\rightarrow$ `36 ms` across 100+ pages).
  - **Conditional `calendar.html` Rendering & Lock-Free `PreInvocation`:** `session_warmup()` and `session_text()` now skip rebuilding `calendar.html` when already fresh today (`ensure_calendar_ready`, `<2 ms`), and Antigravity `PreInvocation` never runs a second concurrent `git pull` alongside `SessionStart` (raising `PreInvocation` timeout to `15 s`).

## [1.2.0] - 2026-11-16

- **Reliable Antigravity 2.0 Hook Delivery (`atlas_hook.py`, `adapters.md`):**
  - **`SessionStart` Warm-Up + First-Turn `PreInvocation` Delivery:** Antigravity 2.0 executes `SessionStart` before the first user message and drops `SessionStart.injectSteps` from the chat trajectory. `SessionStart` now warms up Git pull, `index.sqlite`, and the loopback calendar server without consuming `last_briefing`, while `PreInvocation` (`invocationNum == 0`) reliably injects `Atlas · ok` and the daily briefing once per conversation/day via `systemMessage` (replacing deprecated `ephemeralMessage`).
  - **Antigravity 2.0 `Stop` Contract Parity:** Recognizes `terminationReason == "NO_TOOL_CALL"` alongside `model_stop` and honors `continuationActive` to prevent duplicate stop nudges.
- **Daily Actionable Task Check-In & Zero-Token `atlas.py todo` Triage (`atlas_hook.py`, `atlas.py`, `cal_engine.py`, `brief.md`, `calendar.md`):**
  - **Once-Daily Task Check-In:** The first chat each day presents up to 5 priority-ordered numbered tasks (`1..N`: overdue `[todo]`/`[deadline]` first, then due today, then `[high]`/`[med]`/`[low]` `Open To-Dos`) with quick triage options to **check off**, **reschedule**, or **tackle now**.
  - **Atomic `atlas.py todo` CLI (`list`, `done`, `skip`, `reopen`, `move`, `add`):** Targets tasks by briefing `#number`, text match, or `--file`/`--line` across `calendar/README.md`, `areas/` checklists, and decentralized `due:` YAML frontmatter entries; supports natural date specs (`YYYY-MM-DD`, `+3d`, `+1w`, `today`, `tomorrow`), promotes undated tasks into `## Timeline` chronologically, and immediately refreshes `calendar.html`.
- **Athlete / Rider DNA Physiological Scouting (`coach_engine.py`, `atlas.py`, `atlas_hook.py`, `coach.md`, `calendar.html`):**
  - **Mode `dna` (`--mode dna` / `"Rider DNA"`, `"Athlete DNA"`, `"Limiter"`):** Surfaces enriched efforts, multi-quarter trajectory, ceilings, and 0–100 calibration rubrics (`peak_shape`, `freshness`, `durability`, `trajectory`) backed by an anti-hallucination evidence contract, persisting structured `statcard` reports in `brain/coach/dna.md` and displaying a `🧬 Athlete DNA` summary strip in Pro Mode inside `calendar.html`.

## [1.1.1] - 2026-11-13

- **Strict Physiological Readiness vs. Calendar Deadlines (`coach_engine.py`, `calendar.html`):**
  - **Physiology-Only `GREEN | AMBER | RED` Readiness:** `evaluate_readiness_and_guardrails()` now derives daily Readiness status and reasons strictly from physiological and systemic load signals (`RHR`, `sleep_h`, `sleep_quality`, `soreness`, `TSB`, `ACWR`) plus true `[blocked]` days. Normal `[deadline]` and `[event]` items (e.g. morning errands) never degrade Athlete Readiness to `AMBER` or `RED`.
  - **Training-Window Overlap Detection (`schedule_conflicts`):** Parses the athlete's primary training window from `coach/profile.md` (e.g. `16:00–19:30`) and surfaces an informational guardrail (`🕒 Training-window overlap today: <time> <what>`) only when a timed `event` or `deadline` today overlaps that training window, keeping physiological Readiness `GREEN`.
- **Strict Sport & Power-Sensor Separation — Zero Running Wrist-Watt or Virtual-Watt Pollution (`coach_engine.py`, `atlas.py`):**
  - **`is_valid_cycling_power_activity` Gate (`sport == "cycling"` & `device_watts is not False`):** Excludes Garmin/Coros/Apple Watch running wrist power (`sport == "running"`) and Strava estimated virtual watts (`device_watts == False`) from `compute_stream_metrics()`, `aggregate_epoch_curves()`, `compute_activity_load()`, `resolve_coach_depth()`, and `build_coach_telemetry()`—including already-cached `activities.json` entries without requiring a re-sync.
  - **Runner-Clean Metabolic & `VO₂max` Profiling (`Daniels VDOT`):** Athletes without valid cycling `5m` power no longer receive fabricated cycling `CP / W'`, `Typology`, Storer cycling `VO₂max`, or cycling `FatMax` watts; instead, `compute_metabolic_profile()` estimates `VO₂max` cleanly from **Daniels `VDOT`** (`3km`/`5km`/`10km` best paces -> `vo2max_vdot` / `vo2max_est`) or Uth HR ratio alongside running `CS / D'`.
- **Airy `🕸️ Brain Graph` Physics & Coach Dashboard Polish (`calendar.html`):**
  - **Journal Logs Off by Default (`📓 Logs` Toggle) & Domain-Sector Physics:** Hides daily `journal/` log nodes by default with a 1-click `📓 Logs` toggle pill, replaces center-crushing gravity with wide domain-sector anchors, stronger charge repulsion, hard circle-collision separation, cooling `alpha` settlement, and automatic `fitGraphToViewport` framing.
  - **Priority Bounding-Box Anti-Overlap Label Culling & UI Fixes:** Eliminates overlapping graph labels via screen-space bounding-box culling sorted by selection/focus/degree, formats `FatMax / LT1` ranges with en-dashes (`227–297 W`), and fixes literal HTML-entity rendering in `.textContent` assignments.

## [1.1.0] - 2026-11-13

- **Interactive `🕸️ Brain Graph` Explorer (`calendar.html?view=graph`, `atlas.py calendar --view graph`):**
  - **Zero-Dependency Force-Directed Knowledge Graph (`core/templates/calendar.html` + `cal_engine.py`):** Renders the entire `brain/` knowledge graph directly from `index.sqlite` on an offline-capable HTML5 `<canvas>` with high-DPI scaling, smooth pan/zoom, node dragging, search (`/`), and domain filters (`All`, `People`, `Areas`, `Projects`, `Howto`, `Orphans`, `Stale / Broken`, plus a `Hubs` toggle).
  - **Visual Health Encoding & Side Inspector:** Encodes domain by color, node radius by horizontal degree, dashed amber warning rings for stale `verified:` dates (`> 365` days) or expired `valid_until:`, and solid red rings for broken links. Clicking any node opens the Side Inspector with `summary:`, clickable `#tags`, `verified:` freshness, **Backlinks** (including `#anchor` targets and surrounding line context), **Outlinks**, and **Unlinked Mentions** ready for `atlas.py link`.
- **Interactive Loopback Dashboard (`atlas-cal-v3`) & 1-Click To-Do Toggling (`POST /toggle-todo`):**
  - **CSRF-Protected Markdown Checkbox Write-Back:** Clicking checkboxes on to-dos or workouts across Month, Week, Agenda, Sidebar, or Detail Modal views sends a loopback-only, CSRF-verified (`cal_csrf.txt` + `Origin`/`Host` validation) `POST /toggle-todo` request that safely toggles `- [ ]` $\leftrightarrow$ `- [x]` in the source Markdown file (skipping YAML frontmatter and fenced code blocks, with strict `realpath` confinement inside `brain/`) and re-renders `calendar.html` immediately.
  - **Modular Template Extraction:** Extracted the embedded dashboard HTML/CSS/JS and loopback server out of `core/hooks/atlas_hook.py` into `core/templates/calendar.html` and `core/tools/atlas_lib/cal_engine.py` (with safe `<\/script>` JSON serialization and deterministic `core-apply` updates that preserve user-customized `templates/*.md` files).
- **Adaptive Simple-to-Pro `🚴 Coach View` UI:**
  - **Simple Mode (`depth: simple`):** Presents a calm, clutter-free weekly habit and movement view (completed vs. planned sessions, active minutes, rhythm window, and 28-day availability) with zero FTP, `CP/W'`, `CTL`, or `kJ/kg` jargon.
  - **Pro Mode (`depth: pro` or active power/stream telemetry):** Displays the daily `Readiness` badge (`GREEN | AMBER | RED`) with physiological guardrails, `CP / W'` & Muscle Fiber `Typology` (`Fast-Twitch`, `Allrounder`, `Diesel`), running `CS / D'`, `VO₂max` & `FatMax` chips, a `CTL / ATL / TSB / ACWR` load strip with an inline 42-day SVG trend sparkline, per-ride `W'bal min %` & `Matches`, and a switchable **Multi-Tier Durability (`>15`, `>25`, `>35 kJ/kg`)** table.

## [1.0.0] - 2026-11-13

- **Adaptive Simple-to-Pro Sports-Science & Physiology Engine (`coach_engine.py`, `/atlas-coach`):**
  - **Progressive Disclosure (`depth: auto | simple | standard | pro`):** Automatically adapts both CLI context (`atlas.py coach context`) and coaching guidance from beginner-friendly habit building (`simple`) to full sports-science telemetry (`pro`) without breaking existing customized `brain/coach/profile.md` setups.
  - **Zero-Token 1Hz Stream Physics & Biochemistry:** Computes 2-parameter **Critical Power (`CP` & `W'`)** and Running **Critical Speed (`CS` & `D'`)** with `R²` quality gates, **Skiba Differential `W'` Balance (`W'bal`)** (`w_bal_min_pct` & burned matches), **PDC Muscle Fiber Typology** (`P5s/CP`, `P15s/CP`, power-law decay exponent `b`), **Torque-Cadence (`Nm` vs. `rpm`)** quadrant profiling, **Storer / Uth / Daniels `VO₂max`** cross-checks, honest **Glycolytic / Lactate Clearance Index** bands (`CP/P5m`, `P15s/P5m`), **FatMax & Carbohydrate (`g CHO/h`)** fueling targets, and **Multi-Tier Durability** (`>15`, `>25`, `>35 kJ/kg` + `>5 kJ/kg` above `CP`).
  - **Cross-Sport Systemic Load (`CTL / ATL / TSB / ACWR`), Readiness & Guardrails (`brain/coach/readiness.md`):** Unifies cycling `TSS`, running `rTSS`, HR `hrTSS`, and strength `sRPE` into longitudinal `CTL`, `ATL`, `TSB`, `7d Ramp`, and `ACWR`, paired with morning readiness check-ins (`atlas.py coach note --rhr ... --sleep ... --soreness ...`), Peronnet altitude & indoor/heat `Pw:Hr` normalization, **Bosquet A-Race Taper** windows, and `AMPK/mTORC1` interference guardrails.
- **High-Throughput IMAP Header Sync & Zero-Token Mail-to-Brain Bridge (`atlas_mail.py`, `atlas.py mail ingest`):**
  - **Fast Header-Only Scanning & Server-Side `UID SEARCH`:** `list`, `search`, and `audit` now fetch lightweight `BODY.PEEK[HEADER.FIELDS ...] RFC822.SIZE` metadata instead of full MIME bodies during listing, compile ASCII criteria into server-side IMAP `UID SEARCH` queries, and reuse pooled read-only IMAP connections across multi-folder audits.
  - **Mail Ingest (`atlas.py mail ingest`):** Deterministically extracts `.ics` calendar invitations and `.pdf` documents from matching emails into `brain/inbox/` with provenance sidecar stubs (`dry-run` by default; writes only with `--execute`, never setting `\Seen`).
- **Bi-Temporal Provenance (`verified:`, `valid_until:`, `sources:`) & Conflict Tracking (`atlas.py conflicts`):**
  - Added frontmatter support and validation for `verified: YYYY-MM-DD`, `valid_until: YYYY-MM-DD`, and `sources:` across `brain/`, `atlas.py conflicts` (`list | add | resolve`) backed by `_atlas/conflicts.json`, and upgraded `/atlas-interview` to prioritize open contradictions, expired `valid_until:` dates, and stale facts (`verified:` older than 365 days).

## [0.9.0] - 2026-11-13

- **Dual-Tokenizer SQLite FTS5 & Knowledge-Graph Engine (`graph_engine.py`, `_atlas/cache/index.sqlite`):**
  - **Incremental SQLite Index (`index.sqlite`):** Replaces flat JSON index scans with a self-healing SQLite cache combining `unicode61` FTS5 (with German umlaut normalization `ä/ae`, `ö/oe`, `ü/ue`, `ß/ss`), a substring `trigram` FTS5 table for compound words (e.g. `haftpflicht` inside `Privathaftpflichtversicherung`), and a typed `edges` + `headings` graph (`related`, `body`, `journal`, `tree`).
  - **Code-Block-Safe `[[Wikilink]]` Compiler & Graph CLI:** `atlas.py check --fix` compiles `[[Target#Section|Label]]` outside fenced/inline code blocks into portable relative Markdown links and validates `#anchor` fragments. Added `atlas.py graph <page>`, `atlas.py context <page> [--budget N]`, and `atlas.py link` (`--scan`, `--journals`, `--related`, `--inline`, `--execute`) for safe retroactive wiki-linking.
  - **Specificity-Weighted 1-Hop Hints & Cross-Agent Subagent Parity:** `prompt()` now surfaces 1-hop graph neighbors weighted by inverse hub degree (`1 / log2(2 + degree)`), `stop()` skips regenerating `calendar.html` when inputs are unchanged, `pull()` fails fast when offline, `hook_errors.log` auto-rotates at 50 KB, and `wire --agent antigravity` installs native `.agents/agents/atlas-search.md` and `atlas-maintainer.md` subagents alongside Claude Code.

## [0.8.0] - 2026-11-11

- **Native Multi-Account IMAP Mail Engine (`/atlas-mail`, `atlas_mail.py`, `atlas.py mail`):**
  - **Zero-Dependency Multi-Account IMAP Client (`core/tools/atlas_mail.py`):** Built on Python's standard library (`imaplib`) with credentials stored strictly in git-ignored `brain/_secrets/mail.env` (`chmod 600`). Supports `atlas.py mail init` for 1-step template creation across Gmail / Google Workspace, iCloud, Microsoft 365 / Outlook, Posteo, Mailbox.org, Fastmail, GMX, Web.de, T-Online, Strato, IONOS, and custom IMAP servers.
  - **Read-Only First Inspection & Auditing (`readonly=True` + `BODY.PEEK[]`):** `accounts`, `folders`, `list`, `read`, `search`, and deduplicated multi-folder `audit` strictly open mailboxes in read-only mode and fetch via `BODY.PEEK[]`, ensuring unread states (`\Seen`) are never mutated when Atlas scans or extracts attachments (`--save-attachments`).
  - **3-Stage Safety-Gated Write & Organization Tools (`MOVE`, `COPY`, `TRASH`, `DELETE`, `FLAGS`, `LABELS`, `MKDIR`):**
    - **`move` & `copy` (`target_folder`):** Moves or copies messages by UID into any `target_folder` (with automatic RFC 3501 §5.1.3 Modified UTF-7 encoding/decoding and `--create-folder` support) using atomic **RFC 6851 `UID MOVE`** when supported by the server, or safe fallback (`COPY` + `STORE (\Deleted)` + **RFC 4315 `UID EXPUNGE`**).
    - **`trash` (Soft-Delete) vs. `delete --hard`:** Automatically detects the account's Trash folder via **RFC 6154 `\Trash`** special-use flags (or localized names like `[Gmail]/Papierkorb`, `[Gmail]/Trash`, `Trash`, `Papierkorb`, `Deleted Items`) for recoverable soft-deletion.
    - **`flags` & `labels`:** Inspects (read-only) or modifies **RFC 3501 IMAP `FLAGS`** (`--read`, `--unread`, `--star`, `--unstar`, `--answered`, `--add-flags`, `--remove-flags`, `--set-flags`) and **Gmail `X-GM-LABELS`** (`X-GM-EXT-1` / `--use-keywords`).
    - **Multi-Stage Safety Gate:** Enforces per-account `MAIL_<ID>_ALLOW_WRITE` guards, mandatory read-only Pre-Flight Previews of affected UIDs (Subject, From, Date, current `FLAGS`/`LABELS`, and `target_folder`), `--dry-run` mode, and blocks non-interactive write execution (`CONFIRMATION_REQUIRED`) unless explicitly confirmed by the user (`--yes`).

## [0.7.2] - 2026-11-10

- **Zero-Friction 1-Click Strava Connect (`/strava-connect` & Shared App `91804`):**
  - **No API Keys or Developer Setup Required:** Atlas now ships pre-configured with the shared Vulpiq / Atlas Strava application (`client_id=91804`), so users never need to visit `strava.com/settings/api` or enter a `Client ID` / `Client Secret`.
  - **Always-On Background OAuth Callback (`127.0.0.1:8765`):** Upgraded the background calendar server (`_cal_server.py`) with `/strava-connect`, `/strava-callback`, `/strava-sync`, and `/healthz` (`atlas-cal-v2`). Clicking `[🧡 Jetzt mit Strava verbinden (1-Klick)](http://127.0.0.1:8765/strava-connect)` in chat or in the Coach Dashboard (`calendar.html?view=coach`) redirects directly to Strava consent, exchanges the OAuth token in the background (`brain/_secrets/strava.env`), automatically syncs the last 90 days of rides + 1Hz streams + 3-epoch power curve, and lands back in the Coach Dashboard.

## [0.7.1] - 2026-11-09

- **Hardened & Self-Bootstrapping Update Engine (`core-apply` / `/atlas-update`):**
  - **Promoted `/atlas-coach` (`SKILL.md`, `procedures/coach.md`, and `skeleton/coach/*.md`) into Core & Skeleton:** Fixed an upgrade bug where updating from `v0.6.x` to `v0.7.0` executed the old `v0.6.x` `atlas.py` (which did not recognize `"modules_add": ["coach"]` in `0.7.0.json`), leaving `brain/_atlas/skills/atlas-coach/SKILL.md`, `brain/_atlas/procedures/coach.md`, and `.agents/skills/atlas-coach/SKILL.md` uninstalled until manually wired. Moving them into `core/` and `skeleton/` ensures both `0.6.x -> 0.7.1` and `0.7.0 -> 0.7.1` updates install and wire `/atlas-coach` in one step.
  - **Self-Bootstrapping `core-apply`:** `atlas.py core-apply --execute` now automatically delegates execution to the newly downloaded upstream `src/core/tools/atlas.py` (`--bootstrapped`) after verifying it compiles cleanly, so future updates always run with the newest upgrade engine.
  - **Deterministic Hard Engine Sync with 100% User-Data Protection:** Engine files (`tools/`, `hooks/`, `skills/`, `agents/`, `upgrades/`, `VERSION`, `charter.md`, `adapters.md`) and the `ROUTER.md` core block (`<!-- atlas:core:start --> ... <!-- atlas:core:end -->`) are now always deterministically updated to upstream (never stuck in `keep-local` or `conflict`), while all user brain pages (`self/`, `people/`, `areas/`, `projects/`, `howto/`, `journal/`, `inbox/`, `skills/`, `_secrets/`, `calendar/README.md`, existing `coach/*.md` files, and the personal `## About <Name>` section of `ROUTER.md`) remain 100% untouched.
  - **Automatic Agent Re-Wiring & Router-Mode Preservation:** `core-apply` now auto-detects project-wired agents (`GEMINI.md` / `.agents/` and `CLAUDE.md` / `.claude/`) even if `state.json` lost an entry, preserves `router_mode` (`copy` vs. `include`), and runs `_coach_init` deterministically in Python.

## [0.7.0] - 2026-11-09

- **Native Atlas Sport-Coach (`modules/coach/`, `/atlas-coach`, `atlas.py coach`):**
  - **Zero-Overhead Coach Architecture:** Added the `coach` module (`modules/coach/`), deterministic Coach Intent Router in `atlas_hook.py` (`macro`, `plan-14d`, `checkin`, `daily`), and canonical `brain/coach/` knowledge hub (`profile.md`, `macro-plan.md`, `power-curve.md`, `activities.md`, `workouts/`). Costs 0 extra tokens in normal chats and loads a mode-specific 3-tier context package when coaching.
  - **2-Tier Planning Architecture (Macro Planner vs. 14-Day Short Planner):**
    - `brain/coach/macro-plan.md` manages the season periodization (`Base`, `Build`, `Peak`, `Taper`, `Deload`) anchored to `[goal:A/B]` and multi-day `[blocked]` dates in `brain/calendar/README.md`.
    - Automatic staleness detection (`> 14` days old or changed `goals_hash` from `[goal]` / `[blocked]` entries) prompts the Coach to update `macro-plan.md` first before writing concrete 14-day workouts into `brain/calendar/README.md`.
  - **Strava OAuth Loopback Onboarding & 3-Tier Activity Sync (`atlas.py coach auth-strava` & `sync`):**
    - Local loopback OAuth flow (`127.0.0.1:8766`) stores credentials exclusively in git-ignored `brain/_secrets/strava.env`.
    - Caches heavy 1Hz raw streams (`watts`, `heartrate`, `cadence`, `velocity_smooth`) in git-ignored `brain/_atlas/cache/strava/streams/` and computes Normalized Power (`NP`), `kJ`, `IF`, `TSS`, aerobic decoupling (`Pw:Hr %`), **3-Epoch Mean Maximal Power (`42d`, `90d`, `365d`)** for both **Fresh (`0 kJ+`)** and **Fatigued (`> 1500 kJ`)** states, plus Running Best Split Paces (`1km`, `3km`, `5km`, `10km`).
    - Automatically marks completed workouts `[x]` in `brain/calendar/README.md` and flags unannotated rides (`> 15 min` in the last 14 days) as `[feedback: pending]` for a 1-question check-in (`atlas.py coach note`).
  - **Compact Workout DSL & Zero-Token `.zwo` Compiler (`atlas.py coach zwo`):**
    - Parses compact interval notation directly from `brain/calendar/README.md` (e.g. `(90m | 15m WU 55-75%, 4x 8m@102% / 4m@55%, 19m Z2@68%, 10m CD 65-50%)`) and compiles explicit decimal `.zwo` XML files (`brain/coach/workouts/`) for Hammerhead Karoo, Zwift, Wahoo, and Elite smart trainers.
  - **Upgraded Visual Coach Dashboard (`calendar.html`):**
    - Added Macro Phase & Strava status banner, KW phase badges (`Base`, `Build`, `Peak`, `Taper`, `Deload`), visual interval bar charts inside workout pills & modals, 1-click `.zwo` download button, ride feedback badges (`✓ Notiz` vs. `⚠ Check-in`), and a 3-Epoch Power Curve table (`42d`, `90d`, `365d`, `>1500 kJ`).

## [0.6.6] - 2026-11-09

- **Fixed `ERR_EMPTY_RESPONSE` on Native Windows (`pythonw.exe` `sys.stderr = None` Crash in `http.server`):**
  - **Root Cause:** On native Windows installations, `pythonw.exe` runs in the Windows GUI subsystem without a console, setting `sys.stdout = None` and `sys.stderr = None`. When `pythonw.exe -I -m http.server` received `GET /calendar.html`, `BaseHTTPRequestHandler.send_response(200)` called `self.log_message()` $\rightarrow$ `sys.stderr.write(...)` *before* writing a single byte of the HTTP response, throwing `AttributeError: 'NoneType' object has no attribute 'write'` and closing the TCP socket immediately (`ERR_EMPTY_RESPONSE`). Meanwhile, raw TCP `socket.create_connection` still saw port `8765` as open.
  - **Fix:** Replaced `-m http.server` with a dedicated `_cal_server.py` (`ThreadingHTTPServer` + `sys.stdout = sys.stderr = open(os.devnull, "w")` + silenced `log_message`), upgraded the health check from raw TCP connect to a real `HTTP 200` GET probe (`_cal_http_ok`), automatically terminates any legacy broken `pythonw.exe ... http.server` process squatting on port `8765` on Windows, and starts/verifies the calendar server on every `SessionStart` hook so direct browser opens always succeed.

## [0.6.5] - 2026-11-09

- **Expert Frontend Overhaul of the Visual Calendar & Coach Hub (`calendar.html`):**
  - **Bulletproof CSS Grid (`minmax(0, 1fr)` & `min-width: 0`):** Fixed the CSS Grid column blowout bug where long event descriptions stretched a single weekday column across the entire viewport and pushed Wednesday–Sunday off-screen. Added a clickable **KW (ISO Calendar Week)** column, smart 5/6-week row sizing, 2-line clamped pills with extracted euro/meta subtitles, and a glowing `Heute` pill badge.
  - **Three Layout Modes (`Monat` | `Woche` | `Agenda`) & Instant Search (`/`)**: Switch seamlessly between the 7-column Month Grid, a focused 7-day Week View, and a chronological Agenda Timeline, complete with keyboard shortcuts (`←`/`→`, `T`, `1`/`2`/`3`, `M`/`W`/`A`, `/`, `Esc`) and an interactive **Detail & Markdown-Template Modal** on click.
  - **Zero-Flicker Background Live-Sync:** Replaced the disruptive `<meta http-equiv="refresh" content="20">` full-page reload (which reset the user's selected tab and month every 20 seconds) with a silent background `fetch` sync that preserves view state, active filter, selected month/week, and scroll position in `sessionStorage`.
- **Upgraded `🚴 Coach View` & Athlete Telemetry Engine (`_extract_coach_context`):**
  - Automatically extracts athlete physiology and training parameters from `brain/` (`FTP`, `W/kg`, `VO₂max`, `MMP Peaks`, weekly volume, strength/hypertrophy, biometrics, sleep & primary training window) plus strategic macro-goals and target events (0 LLM tokens).
  - Switching to **`🚴 Coach View`** now reveals an **Athlete Telemetry Ribbon**, 4 Coach KPI cards (Saisonziele, ±14d Workout Compliance, 28d Trainings-Verfügbarkeit, Fokus-Rhythmus), and a dedicated Coach Sidebar (`🏆 Saisonziele & Athletik-Fokus`, `🚴 Trainingsblock -14d..+14d` including completed `[x]` workouts, and `🚫 Blocker & Zeitfenster 28d`).
- **Smart Same-Day Deduplication & Checklist Harvesting:**
  - `calendar_items()` now automatically deduplicates redundant `due:` entries on the same date when both a folder `README.md` (`areas/*/README.md`) and a detail page (`areas/*/*.md`) list the same milestone.
  - `open_todos()` and `atlas.py calendar --init` now also harvest open tasks from checklist sections (`offene-todos-und-checkliste.md`) and fixed a bug where `## Timeline` / `## Open To-Dos` replacement matched backticked inline code in the intro text.

## [0.6.4] - 2026-11-09

- **Reliable Local Port Binding in Linux & Container Environments (`s.bind`):**
  Fixed candidate port scanning in `_ensure_calendar_server`. Replaced blocking `s.connect_ex` without timeout with `s.bind`, avoiding TCP SYN timeouts and hanging processes when scanning unmapped ports in container, sandbox or Linux loopback environments without RST.

## [0.6.3] - 2026-11-09

- **Persistent Calendar Server on Windows (`Win32_Process.Create`):** Fixed `ERR_CONNECTION_REFUSED` on
  `http://127.0.0.1:8765/calendar.html` in Antigravity on Windows, where the IDE's command runner kills child processes
  in its Job Object as soon as `atlas.py` exits. The local calendar server now spawns outside the Job Object via WMI and
  verifies the socket is listening before opening the right-hand browser pane.

## [0.6.2] - 2026-11-09

- **Rendered Browser View (`http://127.0.0.1:<port>/calendar.html`) & `/atlas-calendar` Skill:**
  - Fixed IDE `file://` interception: Antigravity and VS Code open `file://` chat links as raw HTML source code in the
    editor (`</> calendar.html`). Atlas now serves `_atlas/cache/calendar.html` via a lightweight local `http.server` on
    `http://127.0.0.1:8765/calendar.html` so clicking `[📅 Atlas Kalender öffnen](http://127.0.0.1:8765/calendar.html)`
    or running `atlas.py calendar --open` always opens the **rendered visual dashboard** in the browser.
  - Added dedicated `/atlas-calendar` skill (`skills/atlas-calendar/SKILL.md` + `procedures/calendar.md`) and router rule
    enforcing **Atlas First**: saying *"Öffne meinen Kalender"* always opens the native Atlas calendar and never redirects
    to Google Calendar (`calendar.google.com`) or Apple Calendar.

## [0.6.1] - 2026-11-09

- **Zero-Token Visual Calendar & Coach Dashboard (`_atlas/cache/calendar.html` + `atlas.py calendar --open`):**
  - Automatically renders an interactive, dark-mode HTML calendar & coach hub directly in Python (0 LLM tokens, < 2 ms)
    on every session start, end of turn (`Stop` hook), and `atlas.py calendar` call.
  - Features an interactive month grid with color-coded pills (workouts done/planned/skipped, multi-day blocked bars,
    race goals, events, deadlines, birthdays), 1-click view filters (`Alle` | `🚴 Coach View` | `☑️ To-Dos & Fristen`),
    goal week-countdowns, 14-day agenda, open to-do backlog, and live 20-second auto-refresh.
  - Every briefing and `atlas.py calendar` output now includes a 1-click `[📅 Kalender öffnen](file:///...)` hyperlink,
    and `atlas.py calendar --open` launches it automatically in the browser.

## [0.6.0] - 2026-11-09

- **Atlas First Principle & Native Calendar/To-Do Core (`brain/calendar/README.md` + `atlas.py calendar`):**
  - Anchored **Atlas First** in `charter.md`: all knowledge, timeline events, blocked periods, race/macro goals,
    workouts, and to-dos live first as open, versioned Markdown in `brain/`; external calendars and sensors are optional
    sync mirrors.
  - Added `brain/calendar/README.md` with two unified sections: `## Timeline` (supporting single dates, multi-day ranges
    `YYYY-MM-DD..YYYY-MM-DD`, times `HH:MM`, yearly birthdays with automatic age calculation, status markers `[ ]` /
    `[x]` / `[-]`, and category tags `[event]`, `[blocked]`, `[goal]`, `[workout]`, `[todo]`, `[birthday]`) and
    `## Open To-Dos` (prioritized `[high]`, `[med]`, `[low]` tasks that move to `## Timeline` once scheduled).
  - Automatically merges decentralized `due:` frontmatter items from all brain pages and supports `.ics` export
    (`--ics`) and external Google/Apple Calendar `.ics` feed import (`--sync-ics` via `settings.md` → `calendar_ics`).
  - Added role-filtered **Sportcoach view** (`atlas.py calendar --for coach`) that strips out everyday noise (birthdays,
    contract renewals, general to-dos) and outputs only `GOALS & RACES`, `BLOCKED & BUSY WINDOWS`, and `WORKOUTS`.
  - **Initialzündung (`atlas.py calendar --init --execute` & upgrade `0.6.0.json`):** Automatically scans existing
    brains during `/atlas-update` to harvest birthdays, dates, and open `- [ ]` tasks into `brain/calendar/README.md`.

## [0.5.2] - 2026-11-09

- **Fact-Check & Gap Radar (`/atlas-interview`):** Upgraded `procedures/interview.md` and `skills/atlas-interview/SKILL.md`
  into a structured fact-checker across core life pillars (finances, insurances & contracts with notice periods, housing,
  projects, key people/birthdays, health). Opens with a crisp 3–5 bullet Gap Radar showing where hard data is missing and
  why it helps Atlas make better decisions, supports dropping PDFs/screenshots directly into the chat, and files facts
  plus `due:` deadlines immediately.
- **All-in `/atlas-deep`:** Clarified that `/atlas-deep` is the Pro-Ultra mode that covers both deep psychological/relational
  layers and naturally picks up missing surface facts whenever they come up.

## [0.5.1] - 2026-11-09

- **One-command `/atlas-update` (applies immediately):** `atlas.py core-apply --execute` (and `core-diff` / `core-accept`)
  now automatically clones `upstream` from `_atlas/state.json` (with fallback to the official repository) when `--source`
  is omitted, installs clean updates immediately without waiting for a second confirmation after the preview, and skips
  slow web searches or heal-mode probes when `Atlas · ok` is present. `atlas.py probe` without `--agent` now probes all
  wired agents.
- **Natural opening & real personality in `/atlas-deep`:** Explicitly forbids robotic meta-announcements (*"We are now in
  Deep Mode..."*, *"In your own pace, without a mask..."*) and generic blank-slate questions. Atlas now greets the user
  naturally by name like a close friend and opens directly with a concrete observation or hook from their `brain/`.

## [0.5.0] - 2026-11-09

- **Deep friend-and-psychologist mode (`/atlas-deep`):** Completely redesigned `procedures/deep.md` and unlocked natural
  chat invocation in `skills/atlas-deep/SKILL.md`. Instead of an abstract questionnaire, Atlas now scans `self/`,
  `people/`, `areas/`, `projects/`, and `journal/` for depth gaps (treating fact-only pages as blank canvases), holds a
  warm evening-with-a-best-friend conversation using psychological mirroring and bold hypotheses, follows up 2–3 layers
  deep before changing topics, and files insights continuously into the brain.
- **Smarter `/atlas-interview`:** Never stops with "I already know everything" once structural facts exist; automatically
  pivots to context/relationship questions on thin pages or offers a seamless transition into `/atlas-deep`.

## [0.4.0] - 2026-11-07

- **Safe updates:** three-way comparison of the core (`_atlas/.base/`), automatic merge with `git merge-file`, local
  changes kept, conflicts shown instead of overwritten; the router's core block is updated between its markers while the
  personal part stays; declarative upgrades for grown files (`core/upgrades/`); re-wire and probe after every update,
  automatic rollback on failure; `atlas.py core-accept` after a manual merge.
- **Atlas Dev** for maintainers: `atlas.py dev-check` (push rights) and `dev-setup` (clone into `code/life-atlas`,
  project page with aliases). Test suite `tests/run.py`, test Atlas `dev/sandbox.py`, `CONTRIBUTING.md`.

## [0.3.0] - 2026-11-07

- Every page has `summary:` and `tags:` from `brain/tags.md` (with synonyms); `atlas.py find`; hints show the summary.
- Daily briefing in the first chat of the day; behaviour switches in `brain/settings.md`, changed in plain words.
- Learning signals ("remember", "from now on", corrections), signal-based reminders, `/atlas-learn`; Antigravity's
  `/learn` steered into the brain and adopted with `atlas.py adopt`.
- Daily log from `Atlas:` lines; weekly self-maintenance (`/atlas-maintain`, `check --fix`).
- `brain/self/`, `/atlas-interview`, `/atlas-deep` (only on request, kept only with consent).
- Several devices: pull at session start, push after each turn; project paths relative to ROOT; `code-sync`.
- Heartbeat `Atlas · ok` and heal mode in `/atlas-update`; subagents `atlas-search` and `atlas-maintainer` (Claude).

## [0.2.0] - 2026-11-07

- One Atlas folder as the project/workspace (`ROOT` with `brain/` and `code/`), wiring in project scope.
- Routing by project registry (names, aliases, folders, paths), project rules loaded only when routed; token budget.

## [0.1.0] - 2026-11-07

- First version: charter, router, hooks for Claude Code and Antigravity, inventory, safe moves, deadlines, developer
  module.
