"""Atlas test suite. Python 3 standard library only; black-box: it runs atlas.py and the hook as the agents do.

    python -I tests/run.py            all tests
    python -I tests/run.py -k route   only tests whose name contains "route"

Every test builds a throwaway Atlas in ~/.atlas-tests/<run>/ (outside temp folders, which Atlas treats as application
data and refuses to move from) and removes it afterwards. Nothing outside that folder is touched.
"""
import json
import os
import shutil
import stat
import subprocess
import sys
import time
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_TOOL = os.path.join(REPO, "core", "tools", "atlas.py")
BASE_DIR = os.path.join(os.path.expanduser("~"), ".atlas-tests")
os.environ["ATLAS_NO_CAL_SERVER"] = "1"


def fwd(p):
    return p.replace("\\", "/")


def rmtree(path):
    """Remove a test folder; Git marks its object files read-only on Windows."""
    def unlock(func, p, _exc):
        os.chmod(p, stat.S_IWRITE)
        func(p)
    if os.path.exists(path):
        shutil.rmtree(path, onerror=unlock)


class AtlasCase(unittest.TestCase):
    modules = "developer"

    def setUp(self):
        self.tmp = os.path.join(BASE_DIR, f"{self._testMethodName}-{int(time.time() * 1000)}")
        os.makedirs(self.tmp)
        self.root = os.path.join(self.tmp, "Atlas")
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        self.brain = os.path.join(self.root, "brain")
        self.tool = os.path.join(self.brain, "_atlas", "tools", "atlas.py")
        self.hookf = os.path.join(self.brain, "_atlas", "hooks", "atlas_hook.py")
        self.atlas("init", "--source", REPO, "--name", "Test", "--language", "Deutsch", "--modules", self.modules,
                   "--execute", tool=SRC_TOOL)

    def tearDown(self):
        rmtree(self.tmp)

    # helpers
    def atlas(self, *args, tool=None, check=True, root=None):
        res = subprocess.run([sys.executable, "-I", tool or self.tool, "--root", root or self.root, *args],
                             capture_output=True, text=True, encoding="utf-8", errors="replace")
        if check and res.returncode:
            self.fail(f"atlas {' '.join(args)} -> {res.returncode}\n{res.stdout}\n{res.stderr}")
        return res

    def hook(self, agent, event, payload, hookf=None):
        res = subprocess.run([sys.executable, "-I", hookf or self.hookf, agent, event], input=json.dumps(payload),
                             capture_output=True, text=True, encoding="utf-8", errors="replace")
        self.assertEqual(res.returncode, 0, res.stderr)
        out = res.stdout.strip()
        if not out:
            return ""
        data = json.loads(out)
        if "hookSpecificOutput" in data:
            return data["hookSpecificOutput"]["additionalContext"]
        if "injectSteps" in data:
            step = data["injectSteps"][0]
            return step.get("ephemeralMessage") or step["systemMessage"]["systemMessage"]
        return data.get("reason", out)

    def page(self, rel, text):
        p = os.path.join(self.brain, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        return p

    def read(self, path):
        with open(path, encoding="utf-8") as fh:
            return fh.read()

    def wire_both(self):
        for agent in ("claude", "antigravity"):
            self.atlas("wire", "--agent", agent, "--user-home", self.home, "--execute")

    def git(self, repo, *args):
        return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, encoding="utf-8",
                              errors="replace")

    def transcript(self, tool_name="Edit"):
        p = os.path.join(self.tmp, "transcript.jsonl")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "user", "promptId": "p1", "message": {"content": "x"}}) + "\n")
            fh.write(json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": tool_name,
                                                                               "input": {}}]}}) + "\n")
        return fwd(p)


class Install(AtlasCase):
    def test_init_wire_probe_unwire(self):
        for name in ("ROUTER.md", "INDEX.md", "settings.md", "tags.md", "self/README.md", "_atlas/.base/VERSION"):
            self.assertTrue(os.path.exists(os.path.join(self.brain, name)), name)
        self.wire_both()
        for rel in ("CLAUDE.md", "GEMINI.md", ".claude/settings.json", ".agents/hooks.json",
                    ".claude/skills/atlas-brief/SKILL.md", ".agents/skills/atlas-learn/SKILL.md",
                    ".claude/agents/atlas-search.md"):
            self.assertTrue(os.path.exists(os.path.join(self.root, rel)), rel)
        for agent in ("claude", "antigravity"):
            self.assertIn("OK", self.atlas("probe", "--agent", agent, "--user-home", self.home).stdout)
        for agent in ("claude", "antigravity"):
            self.atlas("unwire", "--agent", agent, "--user-home", self.home, "--execute")
        left = [f for f in ("CLAUDE.md", "GEMINI.md", ".claude/settings.json", ".agents/hooks.json")
                if os.path.exists(os.path.join(self.root, f))]
        self.assertEqual(left, [])

    def test_router_and_budget(self):
        self.wire_both()
        res = self.atlas("check", check=False)
        self.assertNotIn("ERROR", res.stdout, res.stdout)
        size = len(self.read(os.path.join(self.brain, "ROUTER.md")).encode("utf-8"))
        self.assertLess(size, 6000)


class Routing(AtlasCase):
    def setUp(self):
        super().setUp()
        os.makedirs(os.path.join(self.root, "code", "shop", ".git"))
        self.page("projects/shop/README.md", "---\nsummary: Shop\ntags: [project]\naliases: [Onlineshop]\n"
                                             "path: code/shop\n---\n# Project: Shop\n")
        self.page("projects/hausbau/README.md", "---\nsummary: Hausbau\ntags: [project]\naliases: [Neubau]\n---\n"
                                                "# Project: Hausbau\n")

    def ask(self, prompt, session="s", cwd=None):
        return self.hook("claude", "prompt", {"prompt": prompt, "cwd": cwd or fwd(self.root), "session_id": session})

    def test_route_by_name_alias_path_cwd(self):
        self.assertIn("project 'Hausbau'", self.ask("wie steht es um den Hausbau", "a"))
        self.assertIn("project 'Hausbau'", self.ask("bau im Neubau den Export", "b"))
        self.assertIn("project 'Shop'", self.ask("fix den Checkout im Onlineshop", "c"))
        self.assertIn("project 'Shop'", self.ask(f"schau in {fwd(self.root)}/code/shop/src", "d"))
        self.assertIn("project 'Shop'", self.ask("fix den bug", "e", cwd=fwd(os.path.join(self.root, "code", "shop"))))
        self.assertEqual(self.ask("erklär mir Rekursion", "f"), "")

    def test_route_once_per_chat(self):
        self.assertIn("route", self.ask("weiter im Onlineshop", "same"))
        self.assertNotIn("route", self.ask("noch was im Onlineshop", "same"))

    def test_antigravity_first_invocation_only(self):
        first = self.hook("antigravity", "prompt", {"invocationNum": 0, "lastUserInput": "weiter am Hausbau",
                                                    "conversationId": "c1"})
        later = self.hook("antigravity", "prompt", {"invocationNum": 2, "lastUserInput": "weiter am Hausbau",
                                                    "conversationId": "c1"})
        self.assertIn("Hausbau", first)
        self.assertEqual(later, "")


class Learning(AtlasCase):
    def test_signal_reminder_throttle_and_daily_log(self):
        tp = self.transcript()
        self.assertIn("learning signal (correction)",
                      self.hook("claude", "prompt", {"prompt": "Nein, das ist falsch", "session_id": "s1"}))
        self.assertIn("learning signal", self.hook("claude", "stop", {"session_id": "s1", "transcript_path": tp,
                                                                      "last_assistant_message": "ok"}))
        self.assertEqual(self.hook("claude", "stop", {"session_id": "s1", "transcript_path": tp,
                                                      "last_assistant_message": "ok"}), "")
        self.hook("claude", "stop", {"session_id": "s1", "transcript_path": tp,
                                     "last_assistant_message": "done\n\nAtlas: Lektion in howto/x.md"})
        logs = os.path.join(self.brain, "journal")
        found = [os.path.join(dp, f) for dp, _d, fs in os.walk(logs) for f in fs if f[:4].isdigit()]
        self.assertTrue(found)
        self.assertIn("Lektion in howto/x.md", self.read(found[0]))

    def test_briefing_once_per_day_and_probe_keeps_it(self):
        self.wire_both()
        self.atlas("probe", "--agent", "claude", "--user-home", self.home)
        first = self.hook("claude", "session", {"session_id": "a", "cwd": fwd(self.root)})
        second = self.hook("claude", "session", {"session_id": "b", "cwd": fwd(self.root)})
        self.assertIn("Atlas · ok", first)
        self.assertIn("briefing", first)
        self.assertNotIn("briefing", second)


class Search(AtlasCase):
    def test_find_tags_synonyms_and_fix(self):
        with open(os.path.join(self.brain, "tags.md"), "a", encoding="utf-8") as fh:
            fh.write("| **auto** | Car | kfz, fahrzeug |\n")
        self.page("areas/mobil/kfz.md", "---\nsummary: Kfz-Versicherung HUK\ntags: [contract, kfz]\n---\n# Kfz\n")
        self.page("areas/geld/konto.md", "---\nsummary: Konto\ntags: [contract]\n---\n# Konto\n\n"
                                         "[Kfz](../alt/kfz.md)\n")
        self.assertIn("areas/mobil/kfz.md", self.atlas("find", "fahrzeug").stdout)
        hint = self.hook("claude", "prompt", {"prompt": "was ist mit meinem Fahrzeug", "session_id": "x"})
        self.assertIn("Kfz-Versicherung HUK", hint)
        out = self.atlas("check", "--fix", check=False).stdout
        self.assertIn("kfz->auto", out)
        self.assertIn("../mobil/kfz.md", self.read(os.path.join(self.brain, "areas", "geld", "konto.md")))


class Adopt(AtlasCase):
    def test_adopt_learn_output(self):
        self.wire_both()
        os.makedirs(os.path.join(self.root, ".agents", "rules"), exist_ok=True)
        with open(os.path.join(self.root, ".agents", "rules", "lang.md"), "w") as fh:
            fh.write("---\ntrigger: always_on\n---\nAlways answer briefly.\n")
        os.makedirs(os.path.join(self.root, ".agents", "skills", "build-check"))
        with open(os.path.join(self.root, ".agents", "skills", "build-check", "SKILL.md"), "w") as fh:
            fh.write('---\nname: build-check\ndescription: "Build"\n---\nnpm test\n')
        self.atlas("adopt", "--execute")
        self.assertTrue(os.path.exists(os.path.join(self.brain, "skills", "build-check", "SKILL.md")))
        self.assertIn("Always answer briefly", self.read(os.path.join(self.brain, "ROUTER.md")))
        self.assertFalse(os.path.exists(os.path.join(self.root, ".agents", "rules", "lang.md")))
        self.atlas("wire", "--agent", "claude", "--user-home", self.home, "--execute")
        self.assertTrue(os.path.exists(os.path.join(self.root, ".claude", "skills", "build-check", "SKILL.md")))


class Move(AtlasCase):
    def test_protections_move_verify_undo(self):
        out = os.path.join(self.tmp, "outside")
        os.makedirs(os.path.join(out, "OneDrive", "x"))
        os.makedirs(os.path.join(out, "repo", ".git"))
        os.makedirs(os.path.join(out, "repo", "src"))
        for rel in ("OneDrive/x/a.pdf", "repo/src/c.py", "doc.pdf"):
            with open(os.path.join(out, rel), "w") as fh:
                fh.write(rel)
        b = fwd(self.brain)
        plan = {"sections": [
            {"name": "bad", "operations": [{"op": "move", "src": fwd(os.path.join(out, "OneDrive/x/a.pdf")),
                                            "dst": b + "/a.pdf"},
                                           {"op": "move", "src": fwd(os.path.join(out, "repo/src/c.py")),
                                            "dst": b + "/c.py"}]},
            {"name": "ok", "operations": [{"op": "move", "src": fwd(os.path.join(out, "doc.pdf")),
                                           "dst": b + "/areas/x/files/doc.pdf"}]}]}
        pf = os.path.join(self.tmp, "plan.json")
        with open(pf, "w") as fh:
            json.dump(plan, fh)
        bad = self.atlas("move", "--plan", pf, "--section", "bad", check=False)
        self.assertEqual(bad.stdout.count("PROBLEM"), 2, bad.stdout)
        self.atlas("move", "--plan", pf, "--section", "ok", "--execute")
        self.assertTrue(os.path.exists(os.path.join(self.brain, "areas", "x", "files", "doc.pdf")))
        mig = os.path.join(self.brain, "_atlas", "migrations")
        journal = [os.path.join(mig, d, "journal.jsonl") for d in os.listdir(mig) if "move-ok" in d][0]
        self.atlas("verify", "--journal", journal)
        self.atlas("undo", "--journal", journal, "--execute")
        self.assertTrue(os.path.exists(os.path.join(out, "doc.pdf")))


class Update(AtlasCase):
    """Three-way update from a newer life-atlas checkout."""

    def make_upstream(self, version="9.9.9"):
        up = os.path.join(self.tmp, "upstream")
        shutil.copytree(REPO, up, ignore=shutil.ignore_patterns(".git", "__pycache__", "_design", "tests"))
        with open(os.path.join(up, "core", "VERSION"), "w") as fh:
            fh.write(version + "\n")
        return up

    def edit(self, path, old, new):
        text = self.read(path)
        self.assertIn(old, text)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text.replace(old, new, 1))

    def test_three_way_router_upgrades(self):
        self.wire_both()
        up = self.make_upstream()
        a = os.path.join(self.brain, "_atlas")
        # upstream only: procedure changed
        self.edit(os.path.join(up, "core", "procedures", "brief.md"), "3 to 6 short lines", "3 to 5 short lines")
        # local only: template changed in this Atlas
        self.edit(os.path.join(a, "templates", "person.md"), "## Notes", "## Notizen")
        # both, different places: clean merge
        self.edit(os.path.join(a, "procedures", "sort.md"), "## Inbox", "## Inbox (lokal)")
        self.edit(os.path.join(up, "core", "procedures", "sort.md"), "4. Report", "4. Report briefly")
        # both, same line: conflict, local kept
        self.edit(os.path.join(a, "procedures", "deep.md"), "Only when the user asks for it.", "Nur auf Wunsch.")
        self.edit(os.path.join(up, "core", "procedures", "deep.md"), "Only when the user asks for it.",
                  "Only on explicit request.")
        # router: upstream core line changed, personal line added locally
        self.edit(os.path.join(up, "core", "templates", "router.md"), "## Route first", "## Route first (v9)")
        with open(os.path.join(self.brain, "ROUTER.md"), "a", encoding="utf-8") as fh:
            fh.write("- Persönliche Regel: duzen.\n")
        # upgrade: a new setting
        with open(os.path.join(up, "core", "upgrades", "9.9.9.json"), "w") as fh:
            json.dump({"settings_add": {"weekly_review": "off   # test"}, "notes": ["test note"]}, fh)
        diff = self.atlas("core-diff", "--source", up).stdout
        self.assertIn("CONFLICT", diff)
        res = self.atlas("core-apply", "--source", up, "--user-home", self.home, "--execute")
        self.assertIn("3 to 5", self.read(os.path.join(a, "procedures", "brief.md")))
        self.assertIn("## Notizen", self.read(os.path.join(a, "templates", "person.md")))
        sort = self.read(os.path.join(a, "procedures", "sort.md"))
        self.assertIn("(lokal)", sort)
        self.assertIn("Report briefly", sort)
        self.assertIn("Nur auf Wunsch.", self.read(os.path.join(a, "procedures", "deep.md")))
        self.assertIn("deep.md", res.stdout)
        router = self.read(os.path.join(self.brain, "ROUTER.md"))
        self.assertIn("## Route first (v9)", router)
        self.assertIn("Persönliche Regel: duzen.", router)
        settings = self.read(os.path.join(self.brain, "settings.md"))
        self.assertIn("weekly_review: off", settings)
        self.assertIn("briefing: daily", settings)
        self.assertEqual(self.read(os.path.join(a, "VERSION")).strip(), "9.9.9")
        self.assertIn("test note", res.stdout)

    def test_rollback_when_new_hook_is_broken(self):
        self.wire_both()
        up = self.make_upstream()
        hook_before = self.read(self.hookf)
        with open(os.path.join(up, "core", "hooks", "atlas_hook.py"), "a") as fh:
            fh.write("\nthis is not python (\n")
        res = self.atlas("core-apply", "--source", up, "--user-home", self.home, "--execute", check=False)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("ROLLED BACK", res.stdout)
        self.assertEqual(self.read(self.hookf), hook_before)
        self.assertNotEqual(self.read(os.path.join(self.brain, "_atlas", "VERSION")).strip(), "9.9.9")

    def test_upgrade_heals_missing_coach_skill_and_protects_user_data(self):
        self.wire_both()
        a = os.path.join(self.brain, "_atlas")
        # Simulate a broken v0.7.0 / v0.6.x state where atlas-coach skill and procedure were missing
        for p in (
            os.path.join(a, "skills", "atlas-coach", "SKILL.md"),
            os.path.join(a, ".base", "skills", "atlas-coach", "SKILL.md"),
            os.path.join(a, "procedures", "coach.md"),
            os.path.join(a, ".base", "procedures", "coach.md"),
            os.path.join(self.root, ".agents", "skills", "atlas-coach", "SKILL.md"),
            os.path.join(self.root, ".claude", "skills", "atlas-coach", "SKILL.md"),
        ):
            if os.path.exists(p):
                os.remove(p)
        # Simulate local drift in a core skill file (must be hard-overwritten by update)
        with open(os.path.join(a, "skills", "atlas-brief", "SKILL.md"), "a", encoding="utf-8") as fh:
            fh.write("\n<!-- stale local drift -->\n")
        # Simulate sacred user data in brain/coach/profile.md and brain/ROUTER.md (must be 100% protected)
        prof_path = os.path.join(self.brain, "coach", "profile.md")
        with open(prof_path, "w", encoding="utf-8") as fh:
            fh.write("---\nsummary: Custom Athlete Profile\ntags: [sport, self]\n---\n# Athlete Profile\n\n| **FTP** | `295 W (4.30 W/kg)` | Custom User Data |\n")
        with open(os.path.join(self.brain, "ROUTER.md"), "a", encoding="utf-8") as fh:
            fh.write("- Eigene goldene Regel: Niemals überschreiben.\n")
        with open(os.path.join(a, "VERSION"), "w", encoding="utf-8") as fh:
            fh.write("0.7.0\n")

        up = self.make_upstream(version="0.7.1")
        self.atlas("core-apply", "--source", up, "--user-home", self.home, "--execute")

        self.assertTrue(os.path.isfile(os.path.join(a, "skills", "atlas-coach", "SKILL.md")))
        self.assertTrue(os.path.isfile(os.path.join(a, "procedures", "coach.md")))
        self.assertTrue(os.path.isfile(os.path.join(self.root, ".agents", "skills", "atlas-coach", "SKILL.md")))
        self.assertTrue(os.path.isfile(os.path.join(self.root, ".claude", "skills", "atlas-coach", "SKILL.md")))
        self.assertNotIn("stale local drift", self.read(os.path.join(a, "skills", "atlas-brief", "SKILL.md")))
        self.assertIn("295 W (4.30 W/kg)", self.read(prof_path))
        self.assertIn("Eigene goldene Regel: Niemals überschreiben.", self.read(os.path.join(self.brain, "ROUTER.md")))


class Devices(AtlasCase):
    def test_two_devices_sync_and_conflict(self):
        remote = os.path.join(self.tmp, "remote.git")
        subprocess.run(["git", "init", "-q", "--bare", remote])
        self.git(remote, "symbolic-ref", "HEAD", "refs/heads/main")
        self.git(self.brain, "remote", "add", "origin", fwd(remote))
        self.assertEqual(self.git(self.brain, "push", "-q", "-u", "origin", "main").returncode, 0)
        self.atlas("state", "--set", f"remote={fwd(remote)}")
        root2 = os.path.join(self.tmp, "Atlas2")
        subprocess.run(["git", "clone", "-q", fwd(remote), os.path.join(root2, "brain")])
        hook2 = os.path.join(root2, "brain", "_atlas", "hooks", "atlas_hook.py")
        self.atlas("state", "--set", f"root={fwd(root2)}", f"remote={fwd(remote)}",
                   tool=os.path.join(root2, "brain", "_atlas", "tools", "atlas.py"), root=root2)
        with open(os.path.join(root2, "brain", "people", "weber.md"), "w") as fh:
            fh.write("---\nsummary: Praxis am Markt\ntags: [person]\n---\n# Dr. Weber\n")
        self.hook("claude", "stop", {"session_id": "d2", "last_assistant_message": "Atlas: weber"}, hookf=hook2)
        self.hook("claude", "session", {"session_id": "d1", "cwd": fwd(self.root)})
        self.assertTrue(os.path.exists(os.path.join(self.brain, "people", "weber.md")))
        # conflict on the same line
        p2, p1 = os.path.join(root2, "brain", "people", "weber.md"), os.path.join(self.brain, "people", "weber.md")
        for p, txt in ((p2, "Bahnhof"), (p1, "Altstadt")):
            with open(p, "w") as fh:
                fh.write(f"---\nsummary: Praxis {txt}\ntags: [person]\n---\n# Dr. Weber\n")
        self.hook("claude", "stop", {"session_id": "d2b", "last_assistant_message": "x"}, hookf=hook2)
        out = self.hook("claude", "session", {"session_id": "d1b", "cwd": fwd(self.root)})
        self.assertIn("Sync:", out)
        self.assertIn("Altstadt", self.read(p1))


class Dev(AtlasCase):
    def test_dev_check_and_setup(self):
        remote = os.path.join(self.tmp, "life-atlas.git")
        subprocess.run(["git", "init", "-q", "--bare", remote])
        work = os.path.join(self.tmp, "seed")
        subprocess.run(["git", "clone", "-q", fwd(remote), work], capture_output=True)
        with open(os.path.join(work, "README.md"), "w") as fh:
            fh.write("seed\n")
        self.git(work, "add", "-A")
        self.git(work, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "seed")
        self.git(work, "push", "-q", "origin", "HEAD:main")
        self.git(remote, "symbolic-ref", "HEAD", "refs/heads/main")
        self.assertEqual(self.atlas("dev-check", "--repo", fwd(remote), check=False).returncode, 0)
        missing = fwd(os.path.join(self.tmp, "does-not-exist.git"))
        self.assertEqual(self.atlas("dev-check", "--repo", missing, check=False).returncode, 2)
        self.assertIn("No push rights", self.atlas("dev-setup", "--repo", missing, "--execute").stdout)
        self.atlas("dev-setup", "--repo", fwd(remote), "--execute")
        self.assertTrue(os.path.exists(os.path.join(self.root, "code", "life-atlas", "README.md")))
        out = self.hook("claude", "prompt", {"prompt": "ich will an Atlas Dev weiterentwickeln", "session_id": "z",
                                             "cwd": fwd(self.root)})
        self.assertIn("project 'Atlas Dev'", out)


class Calendar(AtlasCase):
    def test_calendar_todos_coach_and_init(self):
        import datetime as dt
        today = dt.date.today()
        d_trip_s = (today + dt.timedelta(days=2)).isoformat()
        d_trip_e = (today + dt.timedelta(days=4)).isoformat()
        d_race = (today + dt.timedelta(days=60)).isoformat()
        d_wo = (today + dt.timedelta(days=1)).isoformat()
        d_bday = f"1995-{today.month:02d}-{today.day:02d}"
        with open(os.path.join(self.brain, "people", "max.md"), "w", encoding="utf-8") as fh:
            fh.write(f"---\nsummary: Bruder Max\ntags: [person]\n---\n# Max\n\nGeburtstag: {d_bday}\n")
        os.makedirs(os.path.join(self.brain, "areas", "finanzen"), exist_ok=True)
        with open(os.path.join(self.brain, "areas", "finanzen", "steuern.md"), "w", encoding="utf-8") as fh:
            fh.write(f"---\nsummary: Steuern\ntags: [contract]\ndue:\n  - {d_wo} Erste Ausführung DBX0AN-Sparplan (140 Euro)\n---\n# Steuern\n\n- [ ] Steuerbelege 2025 sortieren\n")
        with open(os.path.join(self.brain, "areas", "finanzen", "README.md"), "w", encoding="utf-8") as fh:
            fh.write(f"---\nsummary: Finanzen\ntags: [contract]\ndue:\n  - {d_wo} Start DBX0AN-Sparplan (140 Euro)\n---\n# Finanzen\n")
        with open(os.path.join(self.brain, "self", "README.md"), "w", encoding="utf-8") as fh:
            fh.write("---\nsummary: Profil\ntags: [self, sport]\n---\n# Profil\n\n| Parameter | Wert | Details |\n|---|---|---|\n| **FTP** | **300 W (4,0 W/kg)** | Schwelle |\n")
        res_init = self.atlas("calendar", "--init", "--execute").stdout
        self.assertIn("harvested from brain", res_init)
        cal_path = os.path.join(self.brain, "calendar", "README.md")
        cal_text = self.read(cal_path)
        self.assertIn("[birthday] Max", cal_text)
        self.assertIn("Steuerbelege 2025 sortieren", cal_text)
        cal_text = cal_text.replace(
            "\n## Timeline",
            "\n## Timeline\n\n"
            f"- [ ] {d_trip_s}..{d_trip_e} [blocked] Dienstreise München\n"
            f"- [ ] {d_race} [goal:A] Eschborn-Frankfurt (Sub 2:35h)\n"
            f"- [ ] {d_wo} [workout:cycling] 90m SweetSpot (2x20m)\n",
            1,
        )
        with open(cal_path, "w", encoding="utf-8") as fh:
            fh.write(cal_text)
        out_all = self.atlas("calendar", "--days", "90").stdout
        self.assertIn("Dienstreise München", out_all)
        self.assertIn("Eschborn-Frankfurt", out_all)
        self.assertIn("Steuerbelege 2025 sortieren", out_all)
        self.assertIn("Max", out_all)
        self.assertIn("Erste Ausführung DBX0AN-Sparplan", out_all)
        self.assertNotIn("Start DBX0AN-Sparplan", out_all)
        out_coach = self.atlas("calendar", "--for", "coach").stdout
        self.assertIn("300 W (4,0 W/kg)", out_coach)
        self.assertIn("Eschborn-Frankfurt", out_coach)
        self.assertIn("Dienstreise München", out_coach)
        self.assertIn("90m SweetSpot", out_coach)
        self.assertNotIn("Steuerbelege 2025 sortieren", out_coach)
        self.assertNotIn("Max", out_coach)
        self.assertIn("Visual calendar: [📅 Atlas Kalender öffnen](http://127.0.0.1:", out_all)
        html_path = os.path.join(self.brain, "_atlas", "cache", "calendar.html")
        self.assertTrue(os.path.isfile(html_path))
        html_content = self.read(html_path)
        self.assertIn("Eschborn-Frankfurt", html_content)
        self.assertIn("minmax(0,1fr)", html_content)
        self.assertNotIn("http-equiv=\"refresh\"", html_content)
        # Verify CAL_SERVER_SCRIPT serves HTTP 200 even when stdout/stderr are None (Windows pythonw.exe behavior)
        import socket, urllib.request
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            free_port = s.getsockname()[1]
        srv_py = os.path.join(self.tmp, "_test_cal_srv.py")
        with open(srv_py, "w", encoding="utf-8") as fh:
            fh.write("import sys; sys.stdout = sys.stderr = None\n" +
                     "import functools, http.server, os\n"
                     "sys.stdout = sys.stderr = open(os.devnull, 'w')\n"
                     "class _QuietHandler(http.server.SimpleHTTPRequestHandler):\n"
                     "    def log_message(self, *args, **kwargs): pass\n"
                     "handler = functools.partial(_QuietHandler, directory=sys.argv[2])\n"
                     "http.server.ThreadingHTTPServer(('127.0.0.1', int(sys.argv[1])), handler).serve_forever()\n")
        proc = subprocess.Popen([sys.executable, "-I", srv_py, str(free_port), os.path.dirname(html_path)],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            ok = False
            for _ in range(20):
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{free_port}/calendar.html", timeout=0.25) as r:
                        if r.status == 200 and b"Eschborn-Frankfurt" in r.read():
                            ok = True
                            break
                except Exception:
                    time.sleep(0.05)
            self.assertTrue(ok, "CAL_SERVER_SCRIPT failed to serve HTTP 200 under pythonw.exe stdout/stderr=None")
        finally:
            proc.terminate()
            proc.wait(timeout=2)


class Coach(AtlasCase):
    modules = "developer,coach"

    def test_coach_workflow_zwo_strava_sync_and_routing(self):
        import datetime as dt
        today = dt.date.today()
        d_yest = (today - dt.timedelta(days=1)).isoformat()
        d_tmrw = (today + dt.timedelta(days=1)).isoformat()
        d_race = (today + dt.timedelta(days=90)).isoformat()

        with open(os.path.join(self.brain, "self", "README.md"), "w", encoding="utf-8") as fh:
            fh.write(
                "---\nsummary: Athletenprofil\ntags: [self, sport]\n---\n# Profil\n\n"
                "| Parameter | Wert | Details |\n|---|---|---|\n"
                "| **Biometrie** | **175 cm · 67 kg** | Gewicht |\n"
                "| **FTP** | **275 W (4,10 W/kg)** | Indoor/Outdoor |\n"
            )
        cal_path = os.path.join(self.brain, "calendar", "README.md")
        with open(cal_path, "w", encoding="utf-8") as fh:
            fh.write(
                "---\nsummary: Calendar\ntags: [atlas, calendar]\n---\n# Calendar & To-Dos\n\n## Timeline\n\n"
                f"- [ ] {d_race} [goal:A] Münsterland Giro (125 km)\n"
                f"- [ ] {d_yest} 17:30 [workout:cycling] Endurance + Finale (105m | 100m Z2@70%, 5m Z5@120%)\n"
                f"- [ ] {d_tmrw} 17:30 [workout:cycling] Threshold 4x8m (90m | 15m WU 55-75%, 4x 8m@102% / 4m@55%, 19m Z2@68%, 10m CD 65-50%)\n\n"
                "## Open To-Dos\n"
            )

        # 1. Initialize coach hub & harvest profile thresholds
        res_init = self.atlas("coach", "--init", "--execute").stdout
        self.assertIn("coach init (applied)", res_init)
        prof_text = self.read(os.path.join(self.brain, "coach", "profile.md"))
        self.assertIn("275 W", prof_text)
        self.assertIn("67 kg", prof_text)

        # 2. Generate .zwo workout files from compact interval DSL in calendar/README.md
        res_zwo = self.atlas("coach", "zwo", "--days", "14").stdout
        self.assertIn("generated 1 .zwo file(s) (FTP=275W)", res_zwo)
        zwo_path = os.path.join(self.brain, "coach", "workouts", f"{d_tmrw}_threshold-4x8m.zwo")
        self.assertTrue(os.path.isfile(zwo_path))
        zwo_xml = self.read(zwo_path)
        self.assertIn('<Warmup Duration="900" PowerLow="0.55" PowerHigh="0.75"/>', zwo_xml)
        self.assertIn('<IntervalsT Repeat="4" OnDuration="480" OffDuration="240" OnPower="1.02" OffPower="0.55"/>', zwo_xml)
        self.assertIn('<SteadyState Duration="1140" Power="0.68"/>', zwo_xml)
        self.assertIn('<Cooldown Duration="600" PowerLow="0.65" PowerHigh="0.50"/>', zwo_xml)

        # 3. Mock Strava sync with >1500 kJ 1Hz power stream & running velocity stream
        # 6000s @ 260W = 1,560,000 J (1560 kJ), then 300s @ 340W after >1500 kJ!
        watts_stream = [260] * 6000 + [340] * 300
        hr_stream = [142] * 6000 + [174] * 300
        mock_acts = [
            {
                "id": "9001",
                "start_date_local": f"{d_yest}T17:30:00Z",
                "name": "Outdoor Endurance + Finale",
                "sport_type": "Ride",
                "moving_time": 6300,
                "distance": 62500.0,
                "total_elevation_gain": 480,
                "streams": {"watts": watts_stream, "heartrate": hr_stream},
            },
            {
                "id": "9002",
                "start_date_local": f"{d_yest}T08:00:00Z",
                "name": "Morning Tempo Run",
                "sport_type": "Run",
                "moving_time": 1500,
                "distance": 6000.0,
                "total_elevation_gain": 35,
                "streams": {"velocity_smooth": [4.0] * 1500, "heartrate": [158] * 1500},
                "private_note": "Lockerer Schritt",
            },
        ]
        mock_path = os.path.join(self.tmp, "strava_mock.json")
        with open(mock_path, "w", encoding="utf-8") as fh:
            json.dump(mock_acts, fh)
        res_sync = self.atlas("coach", "sync", "--mock-file", mock_path, "--execute").stdout
        self.assertIn("2 activity/ies processed", res_sync)
        self.assertIn("1 calendar workout(s) marked [x]", res_sync)
        self.assertIn("1 ride check-in(s) pending", res_sync)

        # Check 3-Epoch Power Curve (Fresh & Fatigued >1500 kJ) and Running Paces
        pc_text = self.read(os.path.join(self.brain, "coach", "power-curve.md"))
        self.assertIn("340 W", pc_text)
        self.assertIn("4:10/km", pc_text)

        # Check calendar [x] auto-match and pending feedback check-in
        cal_after = self.read(cal_path)
        self.assertIn(f"- [x] {d_yest} 17:30 [workout:cycling]", cal_after)
        self.assertIn("[feedback: pending]", cal_after)

        # Save athlete ride feedback note
        res_note = self.atlas("coach", "note", "--date", d_yest, "--text", "Beine top im Finale", "--rpe", "7").stdout
        self.assertIn("RPE 7/10 — Beine top im Finale", res_note)
        self.assertIn("[feedback: RPE 7/10 — Beine top im Finale]", self.read(cal_path))
        self.assertNotIn("[feedback: pending]", self.read(cal_path))

        # 4. Verify Coach Context Slicing, 1-Click Strava Connect Link & Hook Routing + Staleness Detection
        auth_out = self.atlas("coach", "auth-strava").stdout
        self.assertIn("/strava-connect", auth_out)
        self.assertIn("client_id=91804", auth_out)
        ctx_out = self.atlas("coach", "context", "--mode", "plan-14d", "--no-sync").stdout
        self.assertIn("ATHLETE PROFILE & THRESHOLDS", ctx_out)
        self.assertIn("3-EPOCH POWER & PACE CURVE", ctx_out)
        self.assertIn("/strava-connect", ctx_out)
        cal_html = self.read(os.path.join(self.brain, "_atlas", "cache", "calendar.html"))
        self.assertIn("/strava-connect", cal_html)

        # Add a new goal to trigger goals_hash staleness detection
        with open(cal_path, "a", encoding="utf-8") as fh:
            fh.write(f"\n- [ ] {(today + dt.timedelta(days=45)).isoformat()} [goal:B] Jedermann Kriterium\n")
        hook_out = self.hook("claude", "prompt", {
            "prompt": "Coach, bitte erstelle meinen Trainingsplan für die nächsten 2 Wochen",
            "session_id": "coach-sess",
            "cwd": fwd(self.root),
        })
        self.assertIn("Atlas coach -> mode 'plan-14d'", hook_out)
        self.assertIn("Macro plan is STALE", hook_out)

        # 5. Verify atlas.py check passes with 0 errors, 0 orphans, and within skill description budget
        self.wire_both()
        chk = self.atlas("check", check=False).stdout
        self.assertNotIn("ERROR", chk, chk)
        self.assertNotIn("not reachable from INDEX.md", chk, chk)
        self.assertNotIn("skill descriptions total", chk, chk)


if __name__ == "__main__":
    os.makedirs(BASE_DIR, exist_ok=True)
    prog = unittest.main(argv=[sys.argv[0], "-v", *sys.argv[1:]], exit=False)
    if not os.listdir(BASE_DIR):
        os.rmdir(BASE_DIR)
    sys.exit(0 if prog.result.wasSuccessful() else 1)
