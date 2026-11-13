# Changelog

Written for users: `/atlas-update` shows the sections between the installed and the new version.

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
