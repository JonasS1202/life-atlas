"""Unit & loopback-HTTP tests for `cal_engine.py` and `core/templates/calendar.html` (`v1.1.0`)."""
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


if __name__ == "__main__":
    unittest.main()
