"""Unit & headless OAuth loopback tests for `cal_engine.py`, Smart Overdue Triage, and Chat-First hooks (`v1.7.0`)."""
import concurrent.futures
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

sys.dont_write_bytecode = True

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS_DIR = os.path.join(REPO, "core", "tools")
HOOK_PY = os.path.join(REPO, "core", "hooks", "atlas_hook.py")
if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)

import importlib.util  # noqa: E402
from atlas_lib import cal_engine  # noqa: E402


def _load_hook_for_brain(brain):
    spec = importlib.util.spec_from_file_location("atlas_hook_uitest", HOOK_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.BRAIN = brain
    mod.STATE = os.path.join(brain, "_atlas", "state.json")
    mod.CACHE = os.path.join(brain, "_atlas", "cache", "index.sqlite")
    mod.ERR_LOG = os.path.join(brain, "_atlas", "cache", "hook_errors.log")
    mod.CONFLICTS_FILE = os.path.join(brain, "_atlas", "conflicts.json")
    return mod


def _write_file(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class UiCalUnit(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="atlas-caleng-")
        self.brain = os.path.join(self.tmp, "Atlas", "brain")
        os.makedirs(os.path.join(self.brain, "_atlas", "cache"), exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_headless_oauth_server_routes_and_no_static_files(self):
        cache_dir = os.path.join(self.brain, "_atlas", "cache")
        _write_file(os.path.join(cache_dir, "secret_probe.txt"), "do-not-expose\n")
        srv_script = cal_engine.write_oauth_server_script(cache_dir)
        port_file = os.path.join(cache_dir, "cal_port.txt")
        if os.path.exists(port_file):
            os.remove(port_file)

        proc = subprocess.Popen(
            [sys.executable, "-I", "-B", srv_script, "0", cache_dir, TOOLS_DIR],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            bound_port = None
            for _ in range(40):
                if os.path.isfile(port_file):
                    try:
                        with open(port_file, encoding="utf-8") as pf:
                            p_val = int(pf.read().strip())
                        if p_val > 0 and cal_engine.oauth_http_ok(p_val):
                            bound_port = p_val
                            break
                    except (OSError, ValueError):
                        pass
                time.sleep(0.05)
            self.assertIsNotNone(bound_port, "Ephemeral _oauth_server.py failed to start and pass /healthz")

            base_url = f"http://127.0.0.1:{bound_port}"
            opener = urllib.request.build_opener(_NoRedirect)

            # 1. /healthz -> 200 atlas-oauth-v8
            with urllib.request.urlopen(f"{base_url}/healthz", timeout=3) as resp:
                self.assertEqual(resp.status, 200)
                self.assertEqual(resp.read(), b"atlas-oauth-v8")

            # 2. /strava-connect -> 302 redirect to Strava OAuth
            try:
                opener.open(f"{base_url}/strava-connect", timeout=3)
                self.fail("Expected 302 redirect on /strava-connect")
            except urllib.error.HTTPError as e:
                with e:
                    self.assertEqual(e.code, 302)
                    loc = e.headers.get("Location", "")
                    self.assertIn("strava.com/oauth/authorize", loc)
                    self.assertIn(f"127.0.0.1%3A{bound_port}%2Fstrava-callback", loc)

            # 3. /gcal-connect without client JSON -> clean setup page with ZERO calendar.html links
            with urllib.request.urlopen(f"{base_url}/gcal-connect?account=work", timeout=3) as resp:
                self.assertEqual(resp.status, 200)
                body = resp.read().decode("utf-8")
                self.assertIn("Google Calendar Sync Setup", body)
                self.assertNotIn("calendar.html", body)

            # 4. No static file serving (calendar.html and cache files must return 404!)
            for forbidden_path in ("/calendar.html", "/secret_probe.txt", "/cal_port.txt"):
                try:
                    urllib.request.urlopen(f"{base_url}{forbidden_path}", timeout=3)
                    self.fail(f"Expected 404 for {forbidden_path}")
                except urllib.error.HTTPError as e:
                    with e:
                        self.assertEqual(e.code, 404)
        finally:
            proc.terminate()
            proc.wait(timeout=3)

    def test_toggle_todo_security_fences_and_idempotency(self):
        cal_md = os.path.join(self.brain, "calendar", "README.md")
        _write_file(
            cal_md,
            "---\nsummary: Calendar\ntags: [calendar]\n---\n# Calendar\n\n"
            "```markdown\n- [ ] 2026-11-20 [todo] Steuererklärung abgeben\n```\n\n"
            "## Timeline\n\n"
            "- [ ] 2026-11-20 [todo] Steuererklärung abgeben\n\n"
            "## Open To-Dos\n\n"
            "- [ ] [high] Backup-Platte prüfen\n",
        )
        _write_file(os.path.join(self.brain, "_secrets", "secret.md"), "- [ ] secret\n")
        _write_file(os.path.join(self.brain, "_atlas", "core.md"), "- [ ] core\n")
        outside_md = os.path.join(self.tmp, "outside.md")
        _write_file(outside_md, "- [ ] outside\n")

        # 1. Fence line (line 8) must NOT be toggled; real line (line 13) IS toggled
        r1 = cal_engine.toggle_todo_in_brain(
            self.brain, "calendar/README.md", line_no=8, what="Steuererklärung abgeben"
        )
        self.assertTrue(r1["ok"])
        self.assertEqual(r1["line"], 13)
        self.assertTrue(r1["done"])
        with open(cal_md, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        self.assertEqual(lines[7], "- [ ] 2026-11-20 [todo] Steuererklärung abgeben")  # inside ``` untouched!
        self.assertEqual(lines[12], "- [x] 2026-11-20 [todo] Steuererklärung abgeben")

        # 2. Toggle back to open
        r2 = cal_engine.toggle_todo_in_brain(self.brain, "calendar/README.md", line_no=13, what="Steuererklärung abgeben")
        self.assertTrue(r2["ok"])
        self.assertFalse(r2["done"])

        # 3. Toggle undated Open To-Do
        r3 = cal_engine.toggle_todo_in_brain(self.brain, "calendar/README.md", line_no=17, what="Backup-Platte prüfen")
        self.assertTrue(r3["ok"])
        self.assertTrue(r3["done"])

        # 4. Reject path traversal, absolute paths, _secrets/, _atlas/, non-.md, and symlinks
        for bad_rel in ("../outside.md", "../../outside.md", outside_md, "_secrets/secret.md", "_atlas/core.md", "calendar/x.txt"):
            bad = cal_engine.toggle_todo_in_brain(self.brain, bad_rel, line_no=1)
            self.assertFalse(bad["ok"], f"Expected rejection for {bad_rel}")

        link_path = os.path.join(self.brain, "areas", "escape.md")
        os.makedirs(os.path.dirname(link_path), exist_ok=True)
        try:
            os.symlink(outside_md, link_path)
            has_symlink = True
        except OSError:
            has_symlink = False
        if has_symlink:
            esc_res = cal_engine.toggle_todo_in_brain(self.brain, "areas/escape.md", line_no=1)
            self.assertFalse(esc_res["ok"])

    def test_concurrent_brain_todo_mutations_lock(self):
        today = dt.date.today().isoformat()
        cal_md = os.path.join(self.brain, "calendar", "README.md")
        tasks = [f"Parallele Aufgabe {i}" for i in range(1, 7)]
        _write_file(
            cal_md,
            "---\nsummary: Calendar\ntags: [calendar]\n---\n# Calendar\n\n## Timeline\n\n"
            + "\n".join(f"- [ ] {today} [todo] {t}" for t in tasks)
            + "\n",
        )
        contract_md = os.path.join(self.brain, "areas", "work", "contract.md")
        _write_file(
            contract_md,
            f"---\nsummary: Contract\ntags: [work]\ndue:\n  - {today} Kündigungsfrist prüfen\n---\n# Contract\n",
        )

        jobs = [
            ("calendar/README.md", 9 + i, t) for i, t in enumerate(tasks)
        ] + [("areas/work/contract.md", 0, "Kündigungsfrist prüfen")]

        with concurrent.futures.ThreadPoolExecutor(max_workers=len(jobs)) as pool:
            results = list(pool.map(
                lambda j: cal_engine.toggle_todo_in_brain(self.brain, j[0], line_no=j[1], what=j[2], target_done=True),
                jobs,
            ))

        for res in results:
            self.assertTrue(res["ok"])
            self.assertTrue(res["done"])

        with open(cal_md, encoding="utf-8") as fh:
            cal_after = fh.read()
        for t in tasks:
            self.assertIn(f"- [x] {today} [todo] {t}", cal_after)
        with open(contract_md, encoding="utf-8") as fh:
            self.assertIn(f'"[x] {today} Kündigungsfrist prüfen"', fh.read())

        leftovers = [f for f in os.listdir(os.path.dirname(cal_md)) if f.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_adaptive_coach_simple_vs_pro_telemetry(self):
        today = dt.date.today().isoformat()
        _write_file(
            os.path.join(self.brain, "INDEX.md"),
            "---\nsummary: Root\ntags: [atlas]\n---\n# Index\n",
        )
        _write_file(
            os.path.join(self.brain, "calendar", "README.md"),
            f"---\nsummary: Cal\ntags: [calendar]\n---\n# Cal\n\n## Timeline\n- [x] {today} [workout:cycling] 60m Intervalle\n",
        )
        # Case A: Simple Mode
        _write_file(
            os.path.join(self.brain, "coach", "profile.md"),
            "---\nsummary: Profile\ntags: [self, sport]\ndepth: simple\n---\n# Profile\n",
        )
        hk = _load_hook_for_brain(self.brain)
        c_simple = hk._extract_coach_context(hk.load_index())
        self.assertEqual(c_simple["depth"], "simple")

        # Case B: Pro Mode with synced ride telemetry
        _write_file(
            os.path.join(self.brain, "coach", "profile.md"),
            "---\nsummary: Profile\ntags: [self, sport]\ndepth: pro\n---\n# Profile\n\n"
            "| Metric | Value |\n|---|---|\n| **Biometrie** | 180 cm · 70 kg |\n| **FTP (Cycling)** | 310 W |\n",
        )
        _write_file(
            os.path.join(self.brain, "coach", "activities.json"),
            json.dumps({
                "updated": today,
                "activities": [{
                    "id": 999,
                    "date": today,
                    "sport": "cycling",
                    "duration_min": 60,
                    "np_w": 295,
                    "avg_hr": 158,
                    "tss": 88.0,
                    "w_bal_min_pct": 18.4,
                    "matches_burned": 3,
                    "cho_burn_g": 112,
                    "peaks_fresh": {"5s": 960, "1m": 510, "3m": 385, "5m": 355, "10m": 334, "20m": 318},
                    "peaks_durability": {"15kj_kg": {"5m": 340}, "25kj_kg": {"5m": 332}},
                }],
            }),
        )
        c_pro = hk._extract_coach_context(hk.load_index())
        self.assertEqual(c_pro["depth"], "pro")
        self.assertTrue(c_pro["telemetry"]["cp_model"]["valid"])
        self.assertEqual(c_pro["durability_tiers"]["15kj_kg"]["5m"]["90d"], 340)

        # Case C: Pure runner with wrist-watt Run + virtual-watt Ride + morning deadline + window overlap
        _write_file(
            os.path.join(self.brain, "calendar", "README.md"),
            f"---\nsummary: Cal\ntags: [calendar]\n---\n# Cal\n\n## Timeline\n"
            f"- [ ] {today} 10:00 [deadline] Werkstatt Angebot einholen: Motorölwechsel Touran\n"
            f"- [ ] {today} 17:15 [event] Projekt-Sync im Trainingsfenster\n",
        )
        _write_file(
            os.path.join(self.brain, "coach", "profile.md"),
            "---\nsummary: Runner Profile\ntags: [self, sport]\ndepth: auto\n---\n# Profile\n\n"
            "- **Primary Sport:** Running\n"
            "- **Weight:** 71 kg\n"
            "- **Training Window:** 16:00-19:30\n",
        )
        _write_file(
            os.path.join(self.brain, "coach", "activities.json"),
            json.dumps({
                "updated": today,
                "activities": [
                    {
                        "id": 1001,
                        "date": today,
                        "sport": "running",
                        "device_watts": True,
                        "duration_min": 42,
                        "distance_km": 10.5,
                        "avg_hr": 165,
                        "peaks_fresh": {"5s": 602, "3m": 420, "5m": 400, "20m": 380},
                        "best_paces": {
                            "5km": {"sec_per_km": 230.0, "pace": "3:50/km"},
                            "10km": {"sec_per_km": 238.0, "pace": "3:58/km"},
                        },
                    },
                    {
                        "id": 1002,
                        "date": today,
                        "sport": "cycling",
                        "device_watts": False,
                        "duration_min": 50,
                        "peaks_fresh": {"5m": 305, "20m": 265},
                    },
                ],
            }),
        )
        idx_r = hk.load_index()
        c_runner = hk._extract_coach_context(idx_r, cal_items=hk.calendar_items(idx_r, days=365, past_days=21, include_done=True))
        self.assertEqual(c_runner["depth"], "pro")
        r_tel = c_runner["telemetry"]
        self.assertEqual(r_tel["readiness"]["status"], "GREEN")
        self.assertEqual(r_tel["readiness"]["reasons"], [])
        self.assertEqual(len(r_tel["readiness"]["schedule_conflicts"]), 1)
        self.assertIn("17:15 Projekt-Sync", r_tel["readiness"]["schedule_conflicts"][0])
        self.assertFalse(r_tel["cp_model"]["valid"])
        self.assertIsNone(r_tel["metabolic"]["fatmax_watts"])
        self.assertTrue(r_tel["cs_model"]["valid"])
        self.assertGreater(r_tel["metabolic"]["vo2max_est"], 50.0)

    def test_todo_triage_helpers_dates_ranges_and_cross_file_promotion(self):
        base = dt.date(2026, 11, 16)
        self.assertEqual(cal_engine.parse_target_date("today", today=base), "2026-11-16")
        self.assertEqual(cal_engine.parse_target_date("morgen", today=base), "2026-11-17")
        self.assertEqual(cal_engine.parse_target_date("uebermorgen", today=base), "2026-11-18")
        self.assertEqual(cal_engine.parse_target_date("+3d", today=base), "2026-11-19")
        self.assertEqual(cal_engine.parse_target_date("+2w", today=base), "2026-11-30")
        with self.assertRaises(ValueError):
            cal_engine.parse_target_date("not-a-date", today=base)

        cal_md = os.path.join(self.brain, "calendar", "README.md")
        _write_file(
            cal_md,
            "---\nsummary: Calendar\ntags: [calendar]\n---\n# Calendar\n\n"
            "## Timeline\n\n"
            "- [ ] 2026-11-14..2026-11-16 [blocked] Konferenz Berlin\n\n"
            "## Open To-Dos\n\n"
            "- [ ] [med] Angebot kalkulieren\n",
        )
        area_md = os.path.join(self.brain, "areas", "finanzen", "checkliste.md")
        _write_file(
            area_md,
            "---\nsummary: Checkliste\ntags: [work]\ndue:\n  - 2026-11-15 Quartalsabschluss prüfen\n---\n"
            "# Checkliste\n\n## Offene Punkte\n\n- [ ] [high] Belege archivieren\n",
        )

        def _rf(p):
            with open(p, encoding="utf-8") as fh:
                return fh.read()

        # Shift date range `2026-11-14..2026-11-16` -> `2026-11-20..2026-11-22`
        r_rng = cal_engine.reschedule_task_in_brain(
            self.brain, "calendar/README.md", "2026-11-20", what="Konferenz Berlin", today=base
        )
        self.assertTrue(r_rng["ok"])
        self.assertIn("2026-11-20..2026-11-22 [blocked] Konferenz Berlin", _rf(cal_md))

        # Promote undated task from `areas/finanzen/checkliste.md` into `calendar/README.md` Timeline
        r_prom = cal_engine.reschedule_task_in_brain(
            self.brain, "areas/finanzen/checkliste.md", "+5d", what="Belege archivieren", today=base
        )
        self.assertTrue(r_prom["ok"])
        self.assertTrue(r_prom["promoted"])
        self.assertEqual(r_prom["date"], "2026-11-21")
        cal_txt = _rf(cal_md)
        self.assertIn("- [ ] 2026-11-21 [todo] Belege archivieren (areas/finanzen/checkliste.md)", cal_txt)
        area_txt = _rf(area_md)
        self.assertIn("- [ ] 2026-11-21 [todo] Belege archivieren", area_txt)

        # Skip & reopen frontmatter `due:` item
        r_skip_fm = cal_engine.toggle_todo_in_brain(
            self.brain, "areas/finanzen/checkliste.md", line_no=0, what="Quartalsabschluss prüfen", target_status="skipped"
        )
        self.assertTrue(r_skip_fm["ok"])
        self.assertEqual(r_skip_fm["status"], "skipped")
        self.assertIn('"[-] 2026-11-15 Quartalsabschluss prüfen"', _rf(area_md))

        r_reopen_fm = cal_engine.toggle_todo_in_brain(
            self.brain, "areas/finanzen/checkliste.md", line_no=0, what="Quartalsabschluss prüfen", target_status="open"
        )
        self.assertTrue(r_reopen_fm["ok"])
        self.assertIn("- 2026-11-15 Quartalsabschluss prüfen", _rf(area_md))

        # Add dated & undated tasks via `add_todo_in_brain`
        r_add_d = cal_engine.add_todo_in_brain(self.brain, "Server-Patch einspielen", target_date_spec="tomorrow", today=base)
        self.assertTrue(r_add_d["ok"])
        self.assertEqual(r_add_d["date"], "2026-11-17")
        cal_final = _rf(cal_md)
        self.assertLess(cal_final.index("2026-11-17"), cal_final.index("2026-11-20"))
        self.assertLess(cal_final.index("2026-11-20"), cal_final.index("2026-11-21"))

    def test_todo_unschedule_backlog_and_deadline_guard(self):
        cal_md = os.path.join(self.brain, "calendar", "README.md")
        _write_file(
            cal_md,
            "---\nsummary: Calendar\ntags: [calendar]\n---\n# Calendar\n\n"
            "## Timeline\n\n"
            "- [ ] 2026-11-10 [todo] Anbieter anrufen <!-- g:abc12345 -->\n"
            "- [ ] 2026-11-12 [deadline] Steuerfrist abgeben\n\n"
            "## Open To-Dos\n\n"
            "- [ ] [high] Bestehende Aufgabe\n",
        )
        subprocess.run(["git", "init", "-q"], cwd=self.brain, check=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=self.brain, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=self.brain, check=True)
        subprocess.run(["git", "add", "calendar/README.md"], cwd=self.brain, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "baseline"], cwd=self.brain, check=True)

        # 1. Hard `[deadline]` and frontmatter `due:` (line_no=0) MUST be refused
        r_dead = cal_engine.unschedule_todo_in_brain(self.brain, "calendar/README.md", what="Steuerfrist abgeben")
        self.assertFalse(r_dead["ok"])
        self.assertIn("deadline", r_dead["error"].lower())
        r_fm = cal_engine.unschedule_todo_in_brain(self.brain, "calendar/README.md", line_no=0, what="x")
        self.assertFalse(r_fm["ok"])

        # 2. Dated `[todo]` moves cleanly into `## Open To-Dos`, strips `<!-- g:... -->`, zero stop-guard false positive
        r_ok = cal_engine.unschedule_todo_in_brain(self.brain, "calendar/README.md", what="Anbieter anrufen", prio="low")
        self.assertTrue(r_ok["ok"])
        self.assertEqual(r_ok["entry"], "- [ ] [low] Anbieter anrufen")
        with open(cal_md, encoding="utf-8") as fh:
            txt = fh.read()
        self.assertNotIn("2026-11-10 [todo] Anbieter anrufen", txt)
        self.assertNotIn("<!-- g:abc12345 -->", txt)
        self.assertIn("- [ ] [low] Anbieter anrufen", txt)

        hk = _load_hook_for_brain(self.brain)
        self.assertEqual(hk._detect_deleted_open_calendar_tasks(), [])

    def test_smart_overdue_brief_propose_stale_free_days_and_rollover_today(self):
        today = dt.date.today()
        d_stale = (today - dt.timedelta(days=11)).isoformat()
        d_fresh = (today - dt.timedelta(days=2)).isoformat()
        d_dead = (today - dt.timedelta(days=1)).isoformat()
        d_tmrw = (today + dt.timedelta(days=1)).isoformat()

        _write_file(
            os.path.join(self.brain, "INDEX.md"),
            "---\nsummary: Root\ntags: [atlas]\n---\n# Index\n- [Cal](calendar/README.md)\n",
        )
        _write_file(
            os.path.join(self.brain, "settings.md"),
            "---\nsummary: Settings\ntags: [atlas]\nbriefing: daily\noverdue_todo_policy: brief_propose\n---\n",
        )
        cal_md = os.path.join(self.brain, "calendar", "README.md")
        _write_file(
            cal_md,
            "---\nsummary: Calendar\ntags: [calendar]\n---\n# Calendar\n\n## Timeline\n\n"
            f"- [ ] {d_stale} [todo] Alte Telefonliste aufräumen\n"
            f"- [ ] {d_fresh} [todo] Versicherungsunterlagen sortieren\n"
            f"- [ ] {d_dead} [deadline] Vertragskündigung absenden\n"
            f"- [ ] {d_tmrw} [blocked] Dienstreise ganztags\n",
        )

        hk = _load_hook_for_brain(self.brain)
        idx = hk.load_index()
        free_days = cal_engine.suggest_free_days(
            hk.calendar_items(idx, days=10, past_days=0, include_done=False),
            today=today, horizon_days=7, count=2,
        )
        self.assertTrue(len(free_days) >= 1)
        # Blocked tomorrow must NOT be suggested
        self.assertNotIn(d_tmrw, [d["date"] for d in free_days])

        # Default policy = `brief_propose`: age badges, STALE 4-option prompt, hard deadline guard, free-day suggestions, ZERO calendar.html
        sess = hk.session_text(self.brain, "s-brief-1", skip_pull=True)
        self.assertIn("STALE +11 d", sess)
        self.assertIn("OVERDUE +2 d", sess)
        self.assertIn("DEADLINE OVERDUE +1 d", sess)
        self.assertIn("Suggested light/free days:", sess)
        self.assertIn("unschedule", sess)
        self.assertIn("Never auto-reschedule hard [deadline]", sess)
        self.assertNotIn("calendar.html", sess)
        self.assertFalse(os.path.exists(os.path.join(self.brain, "_atlas", "cache", "calendar.html")))

        # Policy = `rollover_today`: moves overdue `[todo]` to today with `(orig YYYY-MM-DD)`, NEVER moves `[deadline]`
        rr = cal_engine.rollover_overdue_todos_in_brain(self.brain, today=today)
        self.assertTrue(rr["ok"])
        self.assertEqual(len(rr["moved"]), 2)
        with open(cal_md, encoding="utf-8") as fh:
            after = fh.read()
        self.assertIn(f"- [ ] {today.isoformat()} [todo] Alte Telefonliste aufräumen (orig {d_stale})", after)
        self.assertIn(f"- [ ] {today.isoformat()} [todo] Versicherungsunterlagen sortieren (orig {d_fresh})", after)
        self.assertIn(f"- [ ] {d_dead} [deadline] Vertragskündigung absenden", after)

    def test_natural_language_todo_rx_and_autocreate_calendar(self):
        hk = _load_hook_for_brain(self.brain)
        positive_prompts = [
            "leg mir mal die und die Aufgabe an",
            "Erinnere mich morgen daran, die Versicherung anzurufen",
            "Remind me on Friday to call Bob",
            "Ich muss noch den Zahnarzt anrufen",
            "Pack mal Reifenwechsel auf meine Liste",
            "Schieb die Aufgabe auf Freitag",
            "Zeig mir meinen Kalender für diese Woche",
            "Add a task to review the backup disk",
            "Trag bitte die Frist für den Vertrag ein",
        ]
        for p in positive_prompts:
            self.assertIsNotNone(hk.TODO_RX.search(p), f"Expected TODO_RX match for: {p!r}")

        negative_prompts = [
            "Refactor the HTTP parser in src/server.py",
            "Wie funktioniert ein binärer Suchbaum?",
            "Explain Python dataclasses vs NamedTuple",
        ]
        for p in negative_prompts:
            self.assertIsNone(hk.TODO_RX.search(p), f"Unexpected TODO_RX false positive for: {p!r}")

        # `add_todo_in_brain` auto-creates `calendar/README.md` on fresh/legacy brains
        cal_md = os.path.join(self.brain, "calendar", "README.md")
        self.assertFalse(os.path.exists(cal_md))
        res = cal_engine.add_todo_in_brain(self.brain, "Erste Aufgabe anlegen", target_date_spec="2026-12-01")
        self.assertTrue(res["ok"])
        self.assertTrue(os.path.isfile(cal_md))
        with open(cal_md, encoding="utf-8") as fh:
            self.assertIn("- [ ] 2026-12-01 [todo] Erste Aufgabe anlegen", fh.read())

    def test_todo_persistence_no_false_fuzzy_drops_and_unbounded_visibility(self):
        today = dt.date.today()
        d_today = today.isoformat()
        d_old_120 = (today - dt.timedelta(days=120)).isoformat()
        d_fut_900 = (today + dt.timedelta(days=900)).isoformat()
        d_over = [(today - dt.timedelta(days=i)).isoformat() for i in range(1, 8)]

        _write_file(
            os.path.join(self.brain, "INDEX.md"),
            "---\nsummary: Root\ntags: [atlas]\n---\n# Index\n- [Cal](calendar/README.md)\n",
        )
        cal_lines = [
            "---",
            "summary: Calendar",
            "tags: [calendar]",
            "---",
            "# Calendar",
            "",
            "## Timeline",
            "",
            f"- [ ] {d_old_120} [todo] Überfällige Altaufgabe prüfen",
            f"- [x] {d_old_120} [todo] Historische erledigte Aufgabe",
            f"- [ ] {d_today} [todo] Projekt Alpha Deployment prüfen",
            f"- [ ] {d_today} [todo] Projekt Alpha Rechnung schreiben",
            f"- [ ] {d_fut_900} [deadline] Langfristige Vertragsfrist 2029",
        ]
        for idx_o, d_o in enumerate(d_over, 1):
            cal_lines.append(f"- [ ] {d_o} [todo] Überlauf-Aufgabe Nummer {idx_o}")
        cal_lines.extend([
            "",
            "## Open To-Dos",
            "",
            "- [ ] [high] Projekt Alpha Dokumentation fertigstellen",
            "- [ ] [med] Projekt Alpha Tests erweitern",
            "",
        ])
        _write_file(os.path.join(self.brain, "calendar", "README.md"), "\n".join(cal_lines))

        filler = ("Kontextzeile für eine ausführliche Projektdokumentation.\n") * 400
        _write_file(
            os.path.join(self.brain, "areas", "work", "checkliste.md"),
            "---\nsummary: Lange Checkliste\ntags: [work]\ndue:\n"
            f"  - {d_today} Vertragsverlängerung prüfen\n"
            "  - yearly 05-10 Geburtstag Beispiel\n"
            "---\n# Lange Checkliste\n\n"
            + filler
            + "\n## Offene Punkte\n\n"
            "- [ ] [high] Projekt Alpha Dokumentation fertigstellen\n"
            "- [ ] [med] Späte Checklisten-Aufgabe hinter 16KB\n",
        )

        hk = _load_hook_for_brain(self.brain)
        idx = hk.load_index()

        # 1. Both same-day similar tasks in calendar/README.md survive (no false-positive fuzzy drop)
        cal_all = hk.calendar_items(idx, days=3650, past_days=3650, include_done=False)
        whats = [i["what"] for i in cal_all]
        self.assertIn("Projekt Alpha Deployment prüfen", whats)
        self.assertIn("Projekt Alpha Rechnung schreiben", whats)
        self.assertIn("Überfällige Altaufgabe prüfen", whats)
        self.assertIn("Langfristige Vertragsfrist 2029", whats)
        self.assertNotIn("Historische erledigte Aufgabe", whats)

        # 2. Both similar undated tasks survive; long-file task (>16KB) harvested; cross-file dup merged
        ot = hk.open_todos(include_done=False)
        ot_whats = [t["what"] for t in ot]
        self.assertIn("Projekt Alpha Dokumentation fertigstellen", ot_whats)
        self.assertIn("Projekt Alpha Tests erweitern", ot_whats)
        self.assertIn("Späte Checklisten-Aufgabe hinter 16KB", ot_whats)
        self.assertEqual(ot_whats.count("Projekt Alpha Dokumentation fertigstellen"), 1)

        # 3. Frontmatter `due:` gets can_toggle=True (yearly birthday stays False) & actionable_tasks keeps source=due_fm
        fm_dead = next(i for i in cal_all if "Vertragsverlängerung prüfen" in i["what"])
        fm_bday = next(i for i in cal_all if "Geburtstag Beispiel" in i["what"])
        self.assertTrue(fm_dead["can_toggle"])
        self.assertFalse(fm_bday["can_toggle"])
        act = hk.actionable_tasks(idx, include_upcoming_days=0)
        act_fm = next(t for t in act if "Vertragsverlängerung prüfen" in t["what"])
        self.assertEqual(act_fm["source"], "due_fm")
        self.assertTrue(act_fm["can_toggle"])

        # 4. Non-briefing session output appends overflow count when >5 urgent items exist
        st = hk._state()
        st["last_briefing"] = d_today
        hk._save_state(st)
        sess_out = hk.session_text(self.brain, "sess-overflow", skip_pull=True)
        self.assertIn("Due today/overdue", sess_out)
        self.assertIn("more — run `atlas.py todo`)", sess_out)

    def test_stop_hook_calendar_deletion_guard(self):
        cal_md = os.path.join(self.brain, "calendar", "README.md")
        baseline_md = (
            "---\nsummary: Calendar\ntags: [calendar]\n---\n# Calendar\n\n"
            "## Timeline\n\n"
            "- [ ] 2026-11-20 [todo] Steuererklärung einreichen\n"
            "- [ ] 2026-11-22 [todo] Zahnarzt anrufen\n"
            "- [ ] 2026-11-23 17:30 [workout:cycling] 60m Grundlagenfahrt\n\n"
            "## Open To-Dos\n\n"
            "- [ ] [high] Backup-Platte prüfen\n"
        )
        _write_file(cal_md, baseline_md)
        subprocess.run(["git", "init", "-q"], cwd=self.brain, check=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=self.brain, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=self.brain, check=True)
        subprocess.run(["git", "add", "calendar/README.md"], cwd=self.brain, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "baseline"], cwd=self.brain, check=True)

        hk = _load_hook_for_brain(self.brain)

        # Case A: Legitimate edits (complete, reschedule, swap workout, promote undated) -> 0 false positives
        _write_file(
            cal_md,
            "---\nsummary: Calendar\ntags: [calendar]\n---\n# Calendar\n\n"
            "## Timeline\n\n"
            "- [x] 2026-11-20 [todo] Steuererklärung einreichen\n"
            "- [ ] 2026-11-24 [todo] Backup-Platte prüfen\n"
            "- [ ] 2026-11-25 18:00 [workout:cycling] 75m Tempo-Intervalle\n"
            "- [ ] 2026-11-28 [todo] Zahnarzt anrufen\n\n"
            "## Open To-Dos\n\n",
        )
        self.assertEqual(hk._detect_deleted_open_calendar_tasks(), [])

        # Case B: Accidental deletion of open task -> guard detects it and stop() emits safety block EVEN with `Atlas:` line
        _write_file(
            cal_md,
            "---\nsummary: Calendar\ntags: [calendar]\n---\n# Calendar\n\n"
            "## Timeline\n\n"
            "- [x] 2026-11-20 [todo] Steuererklärung einreichen\n"
            "- [ ] 2026-11-24 [todo] Backup-Platte prüfen\n\n"
            "## Open To-Dos\n\n",
        )
        deleted = hk._detect_deleted_open_calendar_tasks()
        self.assertEqual(len(deleted), 1)
        self.assertIn("Zahnarzt anrufen", deleted[0])

        captured = []
        orig_emit = hk.emit_continue
        hk.emit_continue = lambda agent, reason: captured.append((agent, reason))
        try:
            hk.stop("claude", {"session_id": "s-guard", "last_assistant_message": "Erledigt.\n\nAtlas: updated calendar"})
        finally:
            hk.emit_continue = orig_emit

        self.assertEqual(len(captured), 1)
        self.assertIn("Atlas safety guard", captured[0][1])
        self.assertIn("Zahnarzt anrufen", captured[0][1])
        self.assertIn("HEAD~1:calendar/README.md", captured[0][1])
        prev = subprocess.run(["git", "show", "HEAD~1:calendar/README.md"], cwd=self.brain, capture_output=True, text=True, check=True).stdout
        self.assertIn("Zahnarzt anrufen", prev)

    def test_v180_guard_sync_uid_and_open_todos_dated_dedup_and_rollover_push(self):
        # 1. Stop-hook safety guard recognizes preserved `<!-- g:<uid> -->` even when simultaneously rescheduled AND reworded!
        cal_md = os.path.join(self.brain, "calendar", "README.md")
        _write_file(
            cal_md,
            "---\nsummary: Calendar\ntags: [calendar]\n---\n# Calendar\n\n"
            "## Timeline\n\n"
            "- [ ] 2026-11-25 [todo] Call bank advisor <!-- g:a1b2c3d4 -->\n"
            "- [ ] 2026-12-04 [todo] Switch bank statements to paperless electronic inbox (areas/finance/todos.md) <!-- g:f0e1d2c3 -->\n\n"
            "## Open To-Dos\n\n",
        )
        subprocess.run(["git", "init", "-q"], cwd=self.brain, check=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=self.brain, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=self.brain, check=True)
        subprocess.run(["git", "add", "calendar/README.md"], cwd=self.brain, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "baseline"], cwd=self.brain, check=True)

        # Reschedule AND completely reword `<!-- g:a1b2c3d4 -->` -> MUST NOT false-positive!
        _write_file(
            cal_md,
            "---\nsummary: Calendar\ntags: [calendar]\n---\n# Calendar\n\n"
            "## Timeline\n\n"
            "- [ ] 2026-12-01 [todo] Schedule quarterly portfolio review with specialist <!-- g:a1b2c3d4 -->\n"
            "- [ ] 2026-12-04 [todo] Switch bank statements to paperless electronic inbox (areas/finance/todos.md) <!-- g:f0e1d2c3 -->\n\n"
            "## Open To-Dos\n\n",
        )
        hk = _load_hook_for_brain(self.brain)
        self.assertEqual(hk._detect_deleted_open_calendar_tasks(), [])

        # 2. `open_todos()` skips decentralized checklist items already scheduled as future dated `[todo]`s in `calendar/README.md`
        _write_file(
            os.path.join(self.brain, "areas", "finance", "todos.md"),
            "---\nsummary: Finance Checklist\ntags: [finance]\n---\n# Finance Checklist\n\n"
            "## Offene Punkte\n\n"
            "- [ ] Switch bank statements to paperless electronic inbox\n"
            "- [ ] Unscheduled separate finance item\n",
        )
        ot = hk.open_todos(include_done=False)
        ot_whats = [t["what"] for t in ot]
        self.assertNotIn("Switch bank statements to paperless electronic inbox", ot_whats)
        self.assertIn("Unscheduled separate finance item", ot_whats)

        # 3. `rollover_today` in `session_text` calls `cal_sync.auto_push_on_stop` immediately when calendar sync is enabled
        past_d = (dt.date.today() - dt.timedelta(days=2)).isoformat()
        _write_file(
            os.path.join(self.brain, "settings.md"),
            "---\nsummary: Settings\nbriefing: daily\noverdue_todo_policy: rollover_today\n---\n# Settings\n",
        )
        _write_file(
            cal_md,
            "---\nsummary: Calendar\ntags: [calendar]\n---\n# Calendar\n\n## Timeline\n\n"
            f"- [ ] {past_d} [todo] Overdue sync rollover task <!-- g:12345678 -->\n",
        )
        push_calls = []
        orig_summary = hk.cal_sync.get_sync_ui_summary
        orig_push = hk.cal_sync.auto_push_on_stop
        hk.cal_sync.get_sync_ui_summary = lambda _b: {"enabled": True, "open_conflicts": 0, "auth_warnings": []}
        hk.cal_sync.auto_push_on_stop = lambda b, timeout=4.0: push_calls.append((b, timeout))
        try:
            sess = hk.session_text(self.brain, "sess-rollover-push", skip_pull=True)
        finally:
            hk.cal_sync.get_sync_ui_summary = orig_summary
            hk.cal_sync.auto_push_on_stop = orig_push
        self.assertIn("Overdue rollover (policy=rollover_today)", sess)
        self.assertEqual(len(push_calls), 1)


if __name__ == "__main__":
    unittest.main()
