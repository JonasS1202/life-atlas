"""Unit & loopback-HTTP tests for `cal_engine.py` and `core/templates/calendar.html` (`v1.1.1`)."""
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
from atlas_lib import cal_engine, graph_engine  # noqa: E402


def _load_hook_for_brain(brain):
    spec = importlib.util.spec_from_file_location("atlas_hook_uitest", HOOK_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.BRAIN = brain
    mod.STATE = os.path.join(brain, "_atlas", "state.json")
    mod.CACHE = os.path.join(brain, "_atlas", "cache", "index.sqlite")
    mod.CAL_HTML = os.path.join(brain, "_atlas", "cache", "calendar.html")
    mod.ERR_LOG = os.path.join(brain, "_atlas", "cache", "hook_errors.log")
    mod.CONFLICTS_FILE = os.path.join(brain, "_atlas", "conflicts.json")
    return mod


def _write_file(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


class UiCalUnit(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="atlas-uical-")
        self.brain = os.path.join(self.tmp, "Atlas", "brain")
        os.makedirs(os.path.join(self.brain, "_atlas", "cache"), exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_template_loading_and_script_escaping(self):
        tpl = cal_engine.load_calendar_template()
        self.assertIn("🕸️ Brain Graph", tpl)
        self.assertIn("id=\"f-graph\"", tpl)
        self.assertIn("minmax(0,1fr)", tpl)
        self.assertIn("postToggleTodo", tpl)
        self.assertIn("Simple Mode", tpl)
        self.assertIn("renderPmcSparkline", tpl)
        self.assertNotIn("http-equiv=\"refresh\"", tpl)

        tricky = {
            "note": "Danger </script><script>window.HACKED=1</script> & line\u2028sep\u2029ok",
            "nested": ["</SCRIPT>", "normal"],
        }
        encoded = cal_engine.safe_json_for_script(tricky)
        self.assertNotIn("</script>", encoded.lower())
        self.assertNotIn("\u2028", encoded)
        self.assertNotIn("\u2029", encoded)
        # Must remain 100% valid JSON for `JSON.parse()` in 15s background sync
        decoded = json.loads(encoded)
        self.assertEqual(decoded, tricky)

    def test_brain_graph_payload_nodes_edges_warnings_and_mentions(self):
        _write_file(
            os.path.join(self.brain, "INDEX.md"),
            "---\nsummary: Root index\ntags: [atlas]\n---\n# Index\n\n"
            "- [People](people/README.md)\n- [Projects](projects/README.md)\n- [Areas](areas/README.md)\n",
        )
        _write_file(
            os.path.join(self.brain, "people", "README.md"),
            "---\nsummary: People hub\ntags: [person]\n---\n# People\n\n- [Anna](anna-beispiel.md)\n",
        )
        _write_file(
            os.path.join(self.brain, "people", "anna-beispiel.md"),
            "---\nsummary: Anna Beispiel — Projektpartnerin\ntags: [person]\nverified: 2024-01-10\n---\n"
            "# Anna Beispiel\n\n## Kontakt\n\nTelefon & Mail.\n",
        )
        _write_file(
            os.path.join(self.brain, "projects", "README.md"),
            "---\nsummary: Projects hub\ntags: [project]\n---\n# Projects\n\n- [Alpha](alpha/README.md)\n",
        )
        _write_file(
            os.path.join(self.brain, "projects", "alpha", "README.md"),
            "---\nsummary: Alpha Launch\ntags: [project]\n---\n# Projekt Alpha\n\n"
            "Abstimmung mit [Anna](../../people/anna-beispiel.md#kontakt) und [Fehlt](nicht-da.md).\n",
        )
        _write_file(
            os.path.join(self.brain, "areas", "README.md"),
            "---\nsummary: Areas hub\ntags: [atlas]\n---\n# Areas\n\n- [Notiz](notiz.md)\n",
        )
        _write_file(
            os.path.join(self.brain, "areas", "notiz.md"),
            "---\nsummary: Isolierte Fachnotiz\ntags: [work]\n---\n# Fachnotiz\n\n"
            "Rücksprache mit Anna Beispiel nächste Woche einplanen.\n",
        )

        idx = graph_engine.sync_index(self.brain)
        gp = cal_engine.build_brain_graph_payload(self.brain, idx=idx, today=dt.date(2026, 11, 13))
        by_rel = {n["rel"]: n for n in gp["nodes"]}

        self.assertIn("people/anna-beispiel.md", by_rel)
        anna = by_rel["people/anna-beispiel.md"]
        self.assertEqual(anna["domain"], "people")
        self.assertTrue(anna["stale_verified"])
        self.assertTrue(any(b["src"] == "projects/alpha/README.md" and b["anchor"] == "kontakt" for b in anna["backlinks"]))
        self.assertTrue(any(u["src"] == "areas/notiz.md" and "Anna Beispiel" in u["matched"] for u in anna["unlinked_mentions"]))

        alpha = by_rel["projects/alpha/README.md"]
        self.assertTrue(len(alpha["broken_links"]) >= 1)

        notiz = by_rel["areas/notiz.md"]
        self.assertTrue(notiz["isolated"])
        self.assertGreaterEqual(gp["stats"]["horizontal_edges"], 1)
        self.assertGreaterEqual(gp["stats"]["stale_verified"], 1)
        self.assertGreaterEqual(gp["stats"]["broken_links"], 1)
        self.assertGreaterEqual(gp["stats"]["unlinked_mentions"], 1)

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

    def test_live_cal_server_csrf_origin_and_rerender(self):
        _write_file(
            os.path.join(self.brain, "INDEX.md"),
            "---\nsummary: Root\ntags: [atlas]\n---\n# Index\n- [Cal](calendar/README.md)\n",
        )
        cal_md = os.path.join(self.brain, "calendar", "README.md")
        _write_file(
            cal_md,
            "---\nsummary: Calendar\ntags: [calendar]\n---\n# Calendar\n\n"
            "## Timeline\n\n- [ ] 2026-11-22 [todo] Zahnarzttermin bestätigen\n\n"
            "## Open To-Dos\n\n- [ ] [high] Reisepass verlängern\n",
        )

        hk = _load_hook_for_brain(self.brain)
        hk.render_calendar_html(start_server=False)
        cache_dir = os.path.join(self.brain, "_atlas", "cache")
        csrf_tok = cal_engine.get_or_create_csrf_token(cache_dir)
        self.assertTrue(len(csrf_tok) >= 32)

        srv_script = cal_engine.write_server_script(cache_dir)
        port_file = os.path.join(cache_dir, "cal_port.txt")
        if os.path.exists(port_file):
            os.remove(port_file)

        proc = subprocess.Popen(
            [sys.executable, "-I", "-B", srv_script, "0", cache_dir, TOOLS_DIR, HOOK_PY],
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
                        if p_val > 0 and cal_engine.cal_http_ok(p_val):
                            bound_port = p_val
                            break
                    except (OSError, ValueError):
                        pass
                time.sleep(0.05)
            self.assertIsNotNone(bound_port, "Ephemeral _cal_server.py failed to start and pass /healthz")

            base_url = f"http://127.0.0.1:{bound_port}"

            def _post(body_dict, headers=None):
                req_headers = {"Content-Type": "application/json"}
                if headers:
                    req_headers.update(headers)
                req = urllib.request.Request(
                    f"{base_url}/toggle-todo",
                    data=json.dumps(body_dict).encode("utf-8"),
                    headers=req_headers,
                    method="POST",
                )
                try:
                    with urllib.request.urlopen(req, timeout=3) as resp:
                        return resp.status, json.loads(resp.read().decode("utf-8"))
                except urllib.error.HTTPError as e:
                    with e:
                        return e.code, json.loads(e.read().decode("utf-8"))

            # 1. Missing CSRF -> 403
            code, _ = _post({"rel": "calendar/README.md", "line": 13, "what": "Reisepass verlängern"})
            self.assertEqual(code, 403)

            # 2. Wrong CSRF -> 403
            code, _ = _post(
                {"rel": "calendar/README.md", "line": 13, "what": "Reisepass verlängern"},
                headers={"X-Atlas-CSRF": "0" * 48},
            )
            self.assertEqual(code, 403)

            # 3. Cross-origin Origin header -> 403 even with valid CSRF
            code, _ = _post(
                {"rel": "calendar/README.md", "line": 13, "what": "Reisepass verlängern"},
                headers={"X-Atlas-CSRF": csrf_tok, "Origin": "https://evil.example"},
            )
            self.assertEqual(code, 403)

            # 4. Path traversal -> 400
            code, _ = _post(
                {"rel": "../outside.md", "line": 1},
                headers={"X-Atlas-CSRF": csrf_tok, "Origin": base_url},
            )
            self.assertEqual(code, 400)

            # 5. Valid loopback toggle -> 200, updates Markdown on disk AND re-renders calendar.html
            code, body = _post(
                {"rel": "calendar/README.md", "line": 9, "what": "Zahnarzttermin bestätigen"},
                headers={"X-Atlas-CSRF": csrf_tok, "Origin": base_url},
            )
            self.assertEqual(code, 200)
            self.assertTrue(body["ok"])
            self.assertTrue(body["done"])

            with open(cal_md, encoding="utf-8") as fh:
                md_after = fh.read()
            self.assertIn("- [x] 2026-11-22 [todo] Zahnarzttermin bestätigen", md_after)

            with open(os.path.join(cache_dir, "calendar.html"), encoding="utf-8") as fh:
                html_after = fh.read()
            m_line = next(ln for ln in html_after.splitlines() if ln.startswith("let DATA = "))
            rendered_data = json.loads(m_line[len("let DATA = "):-1])
            z_item = next(i for i in rendered_data["items"] if "Zahnarzttermin" in i["what"])
            self.assertEqual(z_item["status"], "done")
        finally:
            proc.terminate()
            proc.wait(timeout=3)

    def test_adaptive_coach_simple_vs_pro_payload(self):
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
        hk.render_calendar_html(start_server=False)
        with open(hk.CAL_HTML, encoding="utf-8") as fh:
            html_simple = fh.read()
        d_simple = json.loads(next(ln for ln in html_simple.splitlines() if ln.startswith("let DATA = "))[11:-1])
        self.assertEqual(d_simple["coach"]["depth"], "simple")

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
        hk.render_calendar_html(start_server=False)
        with open(hk.CAL_HTML, encoding="utf-8") as fh:
            html_pro = fh.read()
        d_pro = json.loads(next(ln for ln in html_pro.splitlines() if ln.startswith("let DATA = "))[11:-1])
        self.assertEqual(d_pro["coach"]["depth"], "pro")
        self.assertTrue(d_pro["coach"]["telemetry"]["cp_model"]["valid"])
        self.assertEqual(d_pro["coach"]["durability_tiers"]["15kj_kg"]["5m"]["90d"], 340)
        wo_item = next(i for i in d_pro["items"] if i["kind"] == "workout")
        self.assertAlmostEqual(wo_item["w_bal_min_pct"], 18.4, places=1)
        self.assertEqual(wo_item["matches_burned"], 3)

        # Case C (v1.1.1): Pure runner with wrist-watt Run + virtual-watt Ride + morning deadline + window overlap
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
        hk.render_calendar_html(start_server=False)
        with open(hk.CAL_HTML, encoding="utf-8") as fh:
            html_runner = fh.read()
        self.assertIn("let graphShowJournals = false;", html_runner)
        self.assertIn("📓 Logs", html_runner)
        d_runner = json.loads(next(ln for ln in html_runner.splitlines() if ln.startswith("let DATA = "))[11:-1])
        self.assertEqual(d_runner["coach"]["depth"], "pro")
        r_tel = d_runner["coach"]["telemetry"]
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
        # Chronological ordering inside `## Timeline`: 2026-11-17 appears BEFORE 2026-11-20 and 2026-11-21
        self.assertLess(cal_final.index("2026-11-17"), cal_final.index("2026-11-20"))
        self.assertLess(cal_final.index("2026-11-20"), cal_final.index("2026-11-21"))

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

        # Long checklist file (> 16 KB HEAD_BYTES) + cross-file mirror duplicate + frontmatter due (deadline + yearly)
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

        # 2. Both similar undated tasks in calendar/README.md survive; long-file task (>16KB) harvested; cross-file dup merged
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

        # 4. Calendar HTML includes >90d open overdue AND >365d open deadline, while filtering >90d done item
        hk.render_calendar_html(idx, start_server=False)
        with open(hk.CAL_HTML, encoding="utf-8") as fh:
            html = fh.read()
        data = json.loads(next(ln for ln in html.splitlines() if ln.startswith("let DATA = "))[11:-1])
        ui_whats = [i["what"] for i in data["items"]]
        self.assertIn("Überfällige Altaufgabe prüfen", ui_whats)
        self.assertIn("Langfristige Vertragsfrist 2029", ui_whats)
        self.assertNotIn("Historische erledigte Aufgabe", ui_whats)

        # 5. Non-briefing session output appends overflow count when >5 urgent items exist
        st = hk._state()
        st["last_briefing"] = d_today
        hk._save_state(st)
        prev_no_srv = os.environ.get("ATLAS_NO_CAL_SERVER")
        os.environ["ATLAS_NO_CAL_SERVER"] = "1"
        try:
            sess_out = hk.session_text(self.brain, "sess-overflow", skip_pull=True)
        finally:
            if prev_no_srv is None:
                os.environ.pop("ATLAS_NO_CAL_SERVER", None)
            else:
                os.environ["ATLAS_NO_CAL_SERVER"] = prev_no_srv
        self.assertIn("Due today/overdue", sess_out)
        self.assertIn("more — run `atlas.py todo`)", sess_out)

    def test_concurrent_loopback_toggles_and_background_git_commit(self):
        today = dt.date.today().isoformat()
        _write_file(
            os.path.join(self.brain, "INDEX.md"),
            "---\nsummary: Root\ntags: [atlas]\n---\n# Index\n- [Cal](calendar/README.md)\n",
        )
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
        notes_md = os.path.join(self.brain, "areas", "notes.md")
        _write_file(notes_md, "---\nsummary: Notes\ntags: [work]\n---\n# Notes\nInitial.\n")

        # Initialize git repo inside brain/ and commit baseline
        subprocess.run(["git", "init", "-q"], cwd=self.brain, check=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=self.brain, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=self.brain, check=True)
        subprocess.run(["git", "add", "INDEX.md", "calendar/README.md", "areas/work/contract.md", "areas/notes.md"], cwd=self.brain, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "baseline"], cwd=self.brain, check=True)
        # Leave an unrelated dirty edit that browser toggles must NOT commit
        _write_file(notes_md, "---\nsummary: Notes\ntags: [work]\n---\n# Notes\nUncommitted draft.\n")

        hk = _load_hook_for_brain(self.brain)
        hk.render_calendar_html(start_server=False)
        cache_dir = os.path.join(self.brain, "_atlas", "cache")
        csrf_tok = cal_engine.get_or_create_csrf_token(cache_dir)
        srv_script = cal_engine.write_server_script(cache_dir)
        port_file = os.path.join(cache_dir, "cal_port.txt")
        if os.path.exists(port_file):
            os.remove(port_file)

        env = dict(os.environ)
        env["ATLAS_NO_GIT_PUSH"] = "1"
        proc = subprocess.Popen(
            [sys.executable, "-I", "-B", srv_script, "0", cache_dir, TOOLS_DIR, HOOK_PY],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
        )
        try:
            bound_port = None
            for _ in range(40):
                if os.path.isfile(port_file):
                    try:
                        with open(port_file, encoding="utf-8") as pf:
                            p_val = int(pf.read().strip())
                        if p_val > 0 and cal_engine.cal_http_ok(p_val):
                            bound_port = p_val
                            break
                    except (OSError, ValueError):
                        pass
                time.sleep(0.05)
            self.assertIsNotNone(bound_port, "atlas-cal-v5 server failed /healthz")

            base_url = f"http://127.0.0.1:{bound_port}"

            def _post_one(payload):
                req = urllib.request.Request(
                    f"{base_url}/toggle-todo",
                    data=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json", "X-Atlas-CSRF": csrf_tok, "Origin": base_url},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=5) as resp:
                    return resp.status, json.loads(resp.read().decode("utf-8"))

            payloads = [
                {"rel": "calendar/README.md", "line": 9 + i, "what": t, "done": True}
                for i, t in enumerate(tasks)
            ] + [{"rel": "areas/work/contract.md", "line": 0, "what": "Kündigungsfrist prüfen", "done": True}]

            with concurrent.futures.ThreadPoolExecutor(max_workers=len(payloads)) as pool:
                results = list(pool.map(_post_one, payloads))

            for status, body in results:
                self.assertEqual(status, 200)
                self.assertTrue(body["ok"])
                self.assertTrue(body["done"])

            with open(cal_md, encoding="utf-8") as fh:
                cal_after = fh.read()
            for t in tasks:
                self.assertIn(f"- [x] {today} [todo] {t}", cal_after)

            with open(contract_md, encoding="utf-8") as fh:
                self.assertIn(f'"[x] {today} Kündigungsfrist prüfen"', fh.read())

            # Zero leftover .tmp files in calendar/
            leftovers = [f for f in os.listdir(os.path.dirname(cal_md)) if f.endswith(".tmp")]
            self.assertEqual(leftovers, [])

            # Background git commit lands for toggled files without committing unrelated dirty `areas/notes.md`
            committed = False
            for _ in range(40):
                log = subprocess.run(["git", "log", "-n", "5", "--oneline"], cwd=self.brain, capture_output=True, text=True).stdout
                if "atlas: calendar toggle" in log:
                    st_cal = subprocess.run(["git", "status", "--porcelain", "--", "calendar/README.md", "areas/work/contract.md"], cwd=self.brain, capture_output=True, text=True).stdout.strip()
                    if not st_cal:
                        committed = True
                        break
                time.sleep(0.1)
            self.assertTrue(committed, "Expected background git commit after /toggle-todo")
            st_notes = subprocess.run(["git", "status", "--porcelain", "--", "areas/notes.md"], cwd=self.brain, capture_output=True, text=True).stdout.strip()
            self.assertIn("areas/notes.md", st_notes)
        finally:
            proc.terminate()
            proc.wait(timeout=3)

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
        # Pre-deletion content remains recoverable in git HEAD~1
        prev = subprocess.run(["git", "show", "HEAD~1:calendar/README.md"], cwd=self.brain, capture_output=True, text=True, check=True).stdout
        self.assertIn("Zahnarzt anrufen", prev)


if __name__ == "__main__":
    unittest.main()
