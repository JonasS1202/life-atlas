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

sys.dont_write_bytecode = True

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
        res = subprocess.run([sys.executable, "-I", "-B", tool or self.tool, "--root", root or self.root, *args],
                             capture_output=True, text=True, encoding="utf-8", errors="replace")
        if check and res.returncode:
            self.fail(f"atlas {' '.join(args)} -> {res.returncode}\n{res.stdout}\n{res.stderr}")
        return res

    def hook(self, agent, event, payload, hookf=None):
        res = subprocess.run([sys.executable, "-I", "-B", hookf or self.hookf, agent, event], input=json.dumps(payload),
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
        proc = subprocess.Popen([sys.executable, "-I", "-B", srv_py, str(free_port), os.path.dirname(html_path)],
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

        # 3. Mock Strava sync with >1500 kJ 1Hz power + cadence + altitude + temp streams & running velocity stream
        # 6000s @ 260W = 1,560,000 J (1560 kJ), then 300s @ 340W after >1500 kJ!
        watts_stream = [260] * 6000 + [340] * 300
        hr_stream = [142] * 6000 + [174] * 300
        cad_stream = [88] * 6000 + [96] * 300
        alt_stream = [1850.0] * 6300
        temp_stream = [29.0] * 6300
        mock_acts = [
            {
                "id": "9001",
                "start_date_local": f"{d_yest}T17:30:00Z",
                "name": "Outdoor Endurance + Finale",
                "sport_type": "Ride",
                "moving_time": 6300,
                "distance": 62500.0,
                "total_elevation_gain": 480,
                "streams": {
                    "watts": watts_stream,
                    "heartrate": hr_stream,
                    "cadence": cad_stream,
                    "altitude": alt_stream,
                    "temp": temp_stream,
                },
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

        # Check 3-Epoch Power Curve (Fresh & Fatigued >1500 kJ + Multi-Tier Durability) and Running Paces
        pc_text = self.read(os.path.join(self.brain, "coach", "power-curve.md"))
        self.assertIn("340 W", pc_text)
        self.assertIn("4:10/km", pc_text)
        self.assertIn("Multi-Tier Durability", pc_text)

        # Check calendar [x] auto-match and pending feedback check-in
        cal_after = self.read(cal_path)
        self.assertIn(f"- [x] {d_yest} 17:30 [workout:cycling]", cal_after)
        self.assertIn("[feedback: pending]", cal_after)

        # Save athlete ride feedback note + morning readiness (RHR, sleep, sleep-quality, soreness)
        res_note = self.atlas(
            "coach", "note", "--date", d_yest, "--text", "Beine top im Finale",
            "--rpe", "7", "--rhr", "48", "--sleep", "7.8", "--sleep-quality", "good", "--soreness", "2", "--execute",
        ).stdout
        self.assertIn("RPE 7/10 — Beine top im Finale", res_note)
        self.assertIn("[readiness=GREEN", res_note)
        self.assertIn("[feedback: RPE 7/10 — Beine top im Finale]", self.read(cal_path))
        self.assertNotIn("[feedback: pending]", self.read(cal_path))
        rdy_md = self.read(os.path.join(self.brain, "coach", "readiness.md"))
        self.assertIn("| 48 | 7.8 (good) | 7 | 2 |", rdy_md)

        # Also test standalone morning readiness check-in with `--sleep-quality poor` triggering AMBER
        res_sq_poor = self.atlas(
            "coach", "note", "--date", today.isoformat(),
            "--rhr", "49", "--sleep", "7.0", "--sleep-quality", "poor", "--soreness", "2", "--execute",
        ).stdout
        self.assertIn("[readiness=AMBER", res_sq_poor)
        self.assertIn("| 49 | 7.0 (poor) |", self.read(os.path.join(self.brain, "coach", "readiness.md")))

        # 4. Verify Pro Telemetry Context, 1-Click Strava Connect Link & Hook Routing + Staleness Detection
        auth_out = self.atlas("coach", "auth-strava").stdout
        self.assertIn("/strava-connect", auth_out)
        self.assertIn("client_id=91804", auth_out)
        ctx_out = self.atlas("coach", "context", "--mode", "plan-14d", "--no-sync").stdout
        self.assertIn("PHYSIOLOGICAL TELEMETRY & LOAD", ctx_out)
        self.assertIn("VO2max", ctx_out)
        self.assertIn("hypoxia", ctx_out)
        self.assertIn("[thermal drift]", ctx_out)
        self.assertIn("Torque", ctx_out)
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

        # 5. Adaptive SIMPLE mode (jargon-free casual user) + v1.0.0 upgrade profile guard & readiness link
        prof_path = os.path.join(self.brain, "coach", "profile.md")
        custom_casual_profile = (
            "---\nsummary: Casual Fitness Profile\ntags: [sport]\ndepth: auto\n---\n"
            "# Athlete Profile\n\n"
            "- **Primary Sport:** Running & Mobility\n"
            "- **Weekly Budget:** Run 2x/week, stay healthy, nothing fancy\n"
            "- **Schedule:** Tue & Sat mornings\n"
        )
        with open(prof_path, "w", encoding="utf-8") as fh:
            fh.write(custom_casual_profile)
        with open(os.path.join(self.brain, "coach", "activities.json"), "w", encoding="utf-8") as fh:
            fh.write("[]")
        with open(os.path.join(self.brain, "coach", "readiness.md"), "w", encoding="utf-8") as fh:
            fh.write("---\nsummary: Readiness\ntags: [sport, log]\n---\n# Readiness\n")
        with open(cal_path, "a", encoding="utf-8") as fh:
            fh.write(f"\n- [ ] {(today + dt.timedelta(days=3)).isoformat()} [blocked] Familienausflug\n")

        simple_ctx = self.atlas("coach", "context", "--mode", "plan-14d", "--no-sync").stdout
        self.assertIn("COACH CONTEXT (SIMPLE", simple_ctx)
        self.assertIn("Familienausflug", simple_ctx)
        for jargon in ("PHYSIOLOGICAL TELEMETRY", "FTP", "W'bal", "CTL", "ACWR", ".zwo", "/strava-connect"):
            self.assertNotIn(jargon, simple_ctx)

        # Simulate upgrading an existing v0.9.0 coach brain to v1.0.0:
        # must preserve customized profile.md AND auto-link readiness.md in coach/README.md (0 orphans)
        coach_readme = os.path.join(self.brain, "coach", "README.md")
        with open(coach_readme, "w", encoding="utf-8") as fh:
            fh.write(
                "---\nsummary: Coach Hub\ntags: [sport]\n---\n# Coach Hub\n\n"
                "- [profile.md](profile.md)\n- [macro-plan.md](macro-plan.md)\n"
                "- [power-curve.md](power-curve.md)\n- [activities.md](activities.md)\n"
            )
        os.replace(os.path.join(self.brain, "coach", "readiness.md"), os.path.join(self.tmp, "readiness.bak"))
        with open(os.path.join(self.brain, "_atlas", "VERSION"), "w", encoding="utf-8") as fh:
            fh.write("0.9.0\n")
        self.atlas("core-apply", "--source", REPO, "--execute", tool=SRC_TOOL)
        self.assertEqual(self.read(prof_path), custom_casual_profile)
        self.assertTrue(os.path.isfile(os.path.join(self.brain, "coach", "readiness.md")))
        self.assertIn("[readiness.md](readiness.md)", self.read(coach_readme))

        # 6. Verify atlas.py check passes with 0 errors, 0 orphans, and within skill description budget
        self.wire_both()
        chk = self.atlas("check", check=False).stdout
        self.assertNotIn("ERROR", chk, chk)
        self.assertNotIn("not reachable from INDEX.md", chk, chk)
        self.assertNotIn("skill descriptions total", chk, chk)


class Mail(AtlasCase):
    def test_mail_imap_readonly_and_safety_gated_write_tools(self):
        import socketserver
        import threading

        state = {
            "examine_count": 0,
            "select_rw_count": 0,
            "fetch_specs": [],
            "search_queries": [],
            "folders": {
                "INBOX": {
                    "flags": r"\HasNoChildren",
                    "messages": {
                        "101": {
                            "flags": [],
                            "labels": ["Inbox"],
                            "raw": (
                                b"From: Finanzamt <finanzamt@example.de>\r\n"
                                b"To: test@example.com\r\n"
                                b"Subject: Steuerbescheid 2026\r\n"
                                b"Date: Wed, 11 Nov 2026 10:00:00 +0100\r\n"
                                b"Message-ID: <msg-101@example.de>\r\n"
                                b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
                                b"Bitte pruefen Sie Ihren Steuerbescheid.\r\n"
                            ),
                        },
                        "102": {
                            "flags": [r"\Seen"],
                            "labels": [],
                            "raw": (
                                b"From: Newsletter <news@example.com>\r\n"
                                b"To: test@example.com\r\n"
                                b"Subject: Weekly Digest\r\n"
                                b"Date: Wed, 11 Nov 2026 11:00:00 +0100\r\n"
                                b"Message-ID: <msg-102@example.com>\r\n"
                                b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
                                b"Unsubscribe anytime.\r\n"
                            ),
                        },
                        "103": {
                            "flags": [],
                            "labels": ["Travel"],
                            "raw": (
                                b"From: Konferenzbuero <orga@example.org>\r\n"
                                b"To: test@example.com\r\n"
                                b"Subject: Ticket & Termin Projektkongress\r\n"
                                b"Date: Thu, 12 Nov 2026 09:15:00 +0100\r\n"
                                b"Message-ID: <msg-103@example.org>\r\n"
                                b"MIME-Version: 1.0\r\n"
                                b'Content-Type: multipart/mixed; boundary="BOUND103"\r\n\r\n'
                                b"--BOUND103\r\n"
                                b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
                                b"Anbei Ihre Rechnung und der Kalendereintrag.\r\n"
                                b"--BOUND103\r\n"
                                b'Content-Type: application/pdf; name="rechnung-2026.pdf"\r\n'
                                b'Content-Disposition: attachment; filename="rechnung-2026.pdf"\r\n\r\n'
                                b"%PDF-1.4 mock-pdf-bytes\r\n"
                                b"--BOUND103\r\n"
                                b'Content-Type: text/calendar; charset=utf-8; name="kongress.ics"\r\n'
                                b'Content-Disposition: attachment; filename="kongress.ics"\r\n\r\n'
                                b"BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nSUMMARY:Projektkongress\r\n"
                                b"DTSTART:20261125T100000Z\r\nDTEND:20261125T160000Z\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
                                b"--BOUND103--\r\n"
                            ),
                        },
                    },
                },
                "Papierkorb": {"flags": r"\Trash \HasNoChildren", "messages": {}},
                "Entw&APw-rfe": {"flags": r"\Drafts \HasNoChildren", "messages": {}},
            },
        }

        class MockIMAPHandler(socketserver.StreamRequestHandler):
            def send_line(self, line: str) -> None:
                self.wfile.write((line + "\r\n").encode("utf-8"))
                self.wfile.flush()

            def handle(self) -> None:
                cur_folder = "INBOX"
                self.send_line("* OK [CAPABILITY IMAP4rev1 MOVE UIDPLUS X-GM-EXT-1] MockIMAP Ready")
                while True:
                    raw_line = self.rfile.readline()
                    if not raw_line:
                        break
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    parts = line.split(" ", 2)
                    tag = parts[0]
                    cmd = parts[1].upper() if len(parts) > 1 else ""
                    rest = parts[2] if len(parts) > 2 else ""

                    if cmd == "CAPABILITY":
                        self.send_line("* CAPABILITY IMAP4rev1 MOVE UIDPLUS X-GM-EXT-1")
                        self.send_line(f"{tag} OK CAPABILITY completed")
                    elif cmd == "LOGIN":
                        self.send_line(f"{tag} OK LOGIN completed")
                    elif cmd == "LIST":
                        for fname, fdata in state["folders"].items():
                            self.send_line(f'* LIST ({fdata["flags"]}) "/" "{fname}"')
                        self.send_line(f"{tag} OK LIST completed")
                    elif cmd in ("EXAMINE", "SELECT"):
                        fname = rest.strip().strip('"')
                        if fname not in state["folders"]:
                            self.send_line(f"{tag} NO No such mailbox")
                            continue
                        cur_folder = fname
                        if cmd == "EXAMINE":
                            state["examine_count"] += 1
                        else:
                            state["select_rw_count"] += 1
                        cnt = len(state["folders"][cur_folder]["messages"])
                        self.send_line(f"* {cnt} EXISTS")
                        self.send_line("* OK [UIDVALIDITY 4242] UIDs valid")
                        rw = "READ-ONLY" if cmd == "EXAMINE" else "READ-WRITE"
                        self.send_line(f"{tag} OK [{rw}] {cmd} completed")
                    elif cmd == "CREATE":
                        fname = rest.strip().strip('"')
                        state["folders"].setdefault(fname, {"flags": r"\HasNoChildren", "messages": {}})
                        self.send_line(f"{tag} OK CREATE completed")
                    elif cmd == "UID":
                        sub_parts = rest.split(" ", 2)
                        sub_cmd = sub_parts[0].upper()
                        sub_rest = sub_parts[1] if len(sub_parts) == 2 else (sub_parts[1] + " " + sub_parts[2] if len(sub_parts) > 2 else "")
                        msgs = state["folders"][cur_folder]["messages"]
                        if sub_cmd == "SEARCH":
                            state["search_queries"].append(sub_rest)
                            q_m = None
                            if 'SUBJECT "' in sub_rest.upper():
                                idx_s = sub_rest.upper().find('SUBJECT "') + 9
                                q_m = sub_rest[idx_s:sub_rest.find('"', idx_s)].lower()
                            uids = []
                            for u, m in sorted(msgs.items(), key=lambda kv: int(kv[0])):
                                if "UNSEEN" in sub_rest.upper() and r"\Seen" in m["flags"]:
                                    continue
                                if q_m and q_m not in m["raw"].decode("utf-8", errors="replace").lower():
                                    continue
                                uids.append(u)
                            self.send_line("* SEARCH " + " ".join(uids) if uids else "* SEARCH")
                            self.send_line(f"{tag} OK UID SEARCH completed")
                        elif sub_cmd == "FETCH":
                            uid, _spec = sub_rest.split(" ", 1)
                            state["fetch_specs"].append((uid, _spec))
                            if uid not in msgs:
                                self.send_line(f"{tag} OK UID FETCH empty")
                                continue
                            m = msgs[uid]
                            f_str = " ".join(m["flags"])
                            l_str = " ".join(f'"{x}"' for x in m["labels"])
                            if "HEADER.FIELDS" in _spec.upper():
                                hdr_only = m["raw"].split(b"\r\n\r\n", 1)[0] + b"\r\n\r\n"
                                payload = hdr_only
                                body_tok = "BODY[HEADER.FIELDS (FROM TO CC SUBJECT DATE MESSAGE-ID CONTENT-TYPE)]"
                            else:
                                payload = m["raw"]
                                body_tok = "BODY[]"
                            header_resp = f"* 1 FETCH (UID {uid} FLAGS ({f_str}) X-GM-LABELS ({l_str}) {body_tok} {{{len(payload)}}}"
                            self.wfile.write((header_resp + "\r\n").encode("utf-8"))
                            self.wfile.write(payload)
                            self.wfile.write(b")\r\n")
                            self.wfile.flush()
                            self.send_line(f"{tag} OK UID FETCH completed")
                        elif sub_cmd == "MOVE":
                            uid_set, tgt_raw = sub_rest.split(" ", 1)
                            tgt = tgt_raw.strip().strip('"')
                            if tgt not in state["folders"]:
                                self.send_line(f"{tag} NO Target mailbox does not exist")
                                continue
                            for u in uid_set.split(","):
                                if u in msgs:
                                    state["folders"][tgt]["messages"][u] = msgs.pop(u)
                            self.send_line(f"{tag} OK UID MOVE completed")
                        elif sub_cmd == "STORE":
                            st_parts = sub_rest.split(" ", 2)
                            uid_set, op, val_raw = st_parts[0], st_parts[1].upper(), st_parts[2].strip()
                            inner = val_raw.strip("()").replace('"', "").split()
                            for u in uid_set.split(","):
                                if u not in msgs:
                                    continue
                                if "X-GM-LABELS" in op:
                                    if op.startswith("+"):
                                        for item in inner:
                                            if item not in msgs[u]["labels"]:
                                                msgs[u]["labels"].append(item)
                                    elif op.startswith("-"):
                                        msgs[u]["labels"] = [x for x in msgs[u]["labels"] if x not in inner]
                                    else:
                                        msgs[u]["labels"] = list(inner)
                                else:
                                    if op.startswith("+"):
                                        for item in inner:
                                            if item not in msgs[u]["flags"]:
                                                msgs[u]["flags"].append(item)
                                    elif op.startswith("-"):
                                        msgs[u]["flags"] = [x for x in msgs[u]["flags"] if x not in inner]
                                    else:
                                        msgs[u]["flags"] = list(inner)
                            self.send_line(f"{tag} OK UID STORE completed")
                    elif cmd == "LOGOUT":
                        self.send_line("* BYE MockIMAP logging out")
                        self.send_line(f"{tag} OK LOGOUT completed")
                        break
                    else:
                        self.send_line(f"{tag} OK {cmd} completed")

        srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), MockIMAPHandler)
        srv.daemon_threads = True
        port = srv.server_address[1]
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            # 1. Test `atlas.py mail init`
            init_out = self.atlas("mail", "init", "-a", "WORK", "--label", "Work Account", "--host", "127.0.0.1",
                                  "--port", str(port), "--user", "test@example.com", "--password", "secret123").stdout
            self.assertIn("MAIL_WORK_*", init_out)
            env_file = os.path.join(self.brain, "_secrets", "mail.env")
            with open(env_file, "a", encoding="utf-8") as fh:
                fh.write('MAIL_WORK_SSL="false"\n')

            # 2. Test Read-Only `accounts`, `folders` (UTF-7 decoding), header-only `list`, server-side `search`, and 0-token `ingest`
            acc_out = self.atlas("mail", "accounts").stdout
            self.assertIn("Status: OK | INBOX: 3 | IMAP: MOVE,UIDPLUS,X-GM-EXT-1", acc_out)
            fld_out = self.atlas("mail", "folders", "-a", "work").stdout
            self.assertIn("Entwürfe", fld_out)
            self.assertIn("Papierkorb", fld_out)

            state["fetch_specs"].clear()
            lst_out = self.atlas("mail", "list", "-a", "work", "--unread").stdout
            self.assertIn("UID 101 [UNSEEN]", lst_out)
            self.assertIn("Steuerbescheid 2026", lst_out)
            self.assertIn("UID 103 [UNSEEN]", lst_out)
            # Verify `mail list` fetched ONLY headers (`HEADER.FIELDS`), never full `BODY.PEEK[]`
            self.assertTrue(all("HEADER.FIELDS" in sp for _u, sp in state["fetch_specs"]), state["fetch_specs"])

            # Verify server-side `UID SEARCH` + Gmail label fallback
            state["search_queries"].clear()
            srch_out = self.atlas("mail", "search", "Steuerbescheid", "-a", "work").stdout
            self.assertIn("UID 101", srch_out)
            self.assertTrue(any('SUBJECT "Steuerbescheid"' in q for q in state["search_queries"]), state["search_queries"])
            srch_lbl = self.atlas("mail", "search", "Travel", "-a", "work").stdout
            self.assertIn("UID 103", srch_lbl)

            # Verify 0-token `mail ingest --execute`: extracts .pdf + .ics + provenance .md stub with UIDVALIDITY cursor
            ing_out = self.atlas("mail", "ingest", "-a", "work", "--execute").stdout
            self.assertIn("1 item(s) saved to brain/inbox/", ing_out)
            inbox_files = sorted(os.listdir(os.path.join(self.brain, "inbox")))
            self.assertTrue(any(f.endswith("rechnung-2026.pdf") for f in inbox_files), inbox_files)
            self.assertTrue(any(f.endswith("kongress.ics") for f in inbox_files), inbox_files)
            stub_files = [f for f in inbox_files if f.endswith(".md") and f != "README.md"]
            self.assertEqual(len(stub_files), 1)
            stub_txt = self.read(os.path.join(self.brain, "inbox", stub_files[0]))
            self.assertIn("Projektkongress", stub_txt)
            self.assertIn("uidvalidity=4242;uid=103", stub_txt)

            st_json = json.loads(self.read(os.path.join(self.brain, "_atlas", "state.json")))
            self.assertEqual(st_json["mail_ingest"]["work:INBOX"]["uidvalidity"], "4242")
            self.assertEqual(st_json["mail_ingest"]["work:INBOX"]["last_uid"], 103)

            # Second `mail ingest` run is 100% idempotent (0 duplicates)
            ing_idem = self.atlas("mail", "ingest", "-a", "work", "--execute").stdout
            self.assertIn("0 item(s) saved to brain/inbox/", ing_idem)

            # Ensure ALL read/search/ingest commands NEVER opened a folder in read-write mode (SELECT)
            self.assertEqual(state["select_rw_count"], 0)
            self.assertGreater(state["examine_count"], 0)

            # Verify ingested provenance stub passes `atlas.py check` with 0 missing_sources or errors
            with open(os.path.join(self.brain, "tags.md"), "a", encoding="utf-8") as fh:
                fh.write("| **mail** | Mail | email, postfach |\n")
            chk_mail = self.atlas("check", check=False).stdout
            self.assertNotIn("ERROR", chk_mail, chk_mail)
            self.assertNotIn("missing source", chk_mail, chk_mail)

            # 3. Test Safety Gate: calling `move` without `--yes` in non-interactive mode MUST block with exit code 2
            res_blocked = self.atlas("mail", "move", "101", "-t", "Steuern & Belege", "-a", "work", "--create-folder", check=False)
            self.assertEqual(res_blocked.returncode, 2)
            self.assertIn("SAFETY GATE", res_blocked.stderr)
            self.assertEqual(state["select_rw_count"], 0)
            self.assertIn("101", state["folders"]["INBOX"]["messages"])

            # 4. Test `--dry-run` exits 0 and shows preview without mutating mailbox
            res_dry = self.atlas("mail", "trash", "102", "-a", "work", "--dry-run")
            self.assertIn("[DRY-RUN]", res_dry.stdout)
            self.assertEqual(state["select_rw_count"], 0)

            # 5. Test approved write operations with `--yes`: FLAGS, LABELS, MOVE to target_folder, TRASH
            self.atlas("mail", "flags", "101", "-a", "work", "--read", "--star", "--yes")
            self.assertIn(r"\Seen", state["folders"]["INBOX"]["messages"]["101"]["flags"])
            self.assertIn(r"\Flagged", state["folders"]["INBOX"]["messages"]["101"]["flags"])

            self.atlas("mail", "labels", "101", "-a", "work", "--add", "Finanzamt", "--yes")
            self.assertIn("Finanzamt", state["folders"]["INBOX"]["messages"]["101"]["labels"])

            self.atlas("mail", "move", "101", "-t", "Steuern & Belege", "-a", "work", "--create-folder", "--yes")
            self.assertNotIn("101", state["folders"]["INBOX"]["messages"])
            self.assertIn("101", state["folders"]["Steuern &- Belege"]["messages"])

            self.atlas("mail", "trash", "102", "-a", "work", "--yes")
            self.assertNotIn("102", state["folders"]["INBOX"]["messages"])
            self.assertIn("102", state["folders"]["Papierkorb"]["messages"])
        finally:
            srv.shutdown()
            srv.server_close()


TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if TESTS_DIR not in sys.path:
    sys.path.insert(0, TESTS_DIR)
from test_graph_engine import GraphEngineUnit  # noqa: E402,F401
from test_coach_mail import CoachMailUnit  # noqa: E402,F401
from test_ui_cal import UiCalUnit  # noqa: E402,F401


class WikiGraph(AtlasCase):
    def test_sqlite_graph_context_retroactive_link_check_and_conflicts(self):
        with open(os.path.join(self.brain, "tags.md"), "a", encoding="utf-8") as fh:
            fh.write("| **finance** | Finance | geld, bank |\n")

        self.page(
            "areas/finanzen/README.md",
            "---\nsummary: Finanzen Übersicht\ntags: [finance]\n---\n# Finanzen\n\n"
            "- [Nordbank](nordbank.md)\n- [Steuern](steuern.md)\n",
        )
        with open(os.path.join(self.brain, "INDEX.md"), "a", encoding="utf-8") as fh:
            fh.write("\n- [Finanzen](areas/finanzen/README.md)\n- [Max Mustermann](people/max-mustermann.md)\n")

        doc_path = os.path.join(self.brain, "areas", "finanzen", "files", "vertrag.pdf")
        os.makedirs(os.path.dirname(doc_path), exist_ok=True)
        with open(doc_path, "w", encoding="utf-8") as fh:
            fh.write("pdf-placeholder\n")

        self.page(
            "people/max-mustermann.md",
            "---\nsummary: Ansprechpartner Nordbank\ntags: [person]\naliases: [Berater Mustermann]\n---\n"
            "# Max Mustermann\n\n## Konditionen & Kontakt\n\nDirektdurchwahl vereinbart.\n",
        )
        self.page(
            "areas/finanzen/nordbank.md",
            "---\nsummary: Tagesgeld & Giro Nordbank\ntags: [finance, contract]\n"
            "verified: 2020-01-15\nsources: [files/vertrag.pdf, files/missing-scan.pdf]\n---\n"
            "# Konto Nordbank\n\n"
            "Betreut durch [[Max Mustermann#Konditionen & Kontakt|Bankberater]].\n"
            "Siehe auch [Falscher Anker](../finanzen/steuern.md#nicht-da).\n"
            "Code-Beispiel: `[[Max Mustermann]]` bleibt unverändert.\n"
            "Notiz zur Kundenrückgewinnung.\n",
        )
        self.page(
            "areas/finanzen/steuern.md",
            "---\nsummary: Steuererklärung\ntags: [finance]\n---\n"
            "# Steuern\n\nBelege der Konto Nordbank und Rückfrage bei Berater Mustermann prüfen.\n"
            "URL https://example.org/Max-Mustermann darf nicht verlinkt werden.\n",
        )
        self.page(
            "journal/2026/2026-05-01.md",
            "---\nsummary: Daily log 2026-05-01\ntags: [log]\n---\n# 2026-05-01 · Log\n\n"
            "- 10:00 · — · Update in areas/finanzen/nordbank.md und `people/max-mustermann.md`\n",
        )

        # 1. `atlas.py index` builds `_atlas/cache/index.sqlite`
        idx_out = self.atlas("index").stdout
        self.assertIn("index.sqlite", idx_out)
        self.assertTrue(os.path.isfile(os.path.join(self.brain, "_atlas", "cache", "index.sqlite")))

        # 2. `atlas.py check` flags broken #anchor, missing local source, stale verified info, and ready wikilink
        chk1 = self.atlas("check", check=False).stdout
        self.assertIn("broken #section anchor", chk1)
        self.assertIn("steuern.md#nicht-da", chk1)
        self.assertIn("missing-scan.pdf", chk1)
        self.assertIn("`verified:` older than 365 days", chk1)
        self.assertIn("ready to compile", chk1)

        # 3. `atlas.py check --fix` compiles `[[Max Mustermann#...]]` outside code blocks into relative Markdown link
        fix_out = self.atlas("check", "--fix", check=False).stdout
        self.assertIn("wikilinks in areas/finanzen/nordbank.md: 1", fix_out)
        nb_txt = self.read(os.path.join(self.brain, "areas", "finanzen", "nordbank.md"))
        self.assertIn("[Bankberater](../../people/max-mustermann.md#konditionen-kontakt)", nb_txt)
        self.assertIn("`[[Max Mustermann]]`", nb_txt)

        # 4. `atlas.py find` + 1-hop `↳ linked:` expansion + prompt hook 1-hop hints
        f_out = self.atlas("find", "rückgewinnung").stdout
        self.assertIn("areas/finanzen/nordbank.md", f_out)
        self.assertIn("↳ linked: people/max-mustermann.md", f_out)
        p_hint = self.hook("claude", "prompt", {"prompt": "was war bei der Kundenrückgewinnung?", "session_id": "wg1"})
        self.assertIn("areas/finanzen/nordbank.md", p_hint)
        self.assertIn("↳ linked:", p_hint)
        self.assertIn("people/max-mustermann.md", p_hint)

        # 5. `atlas.py graph` shows outlinks, backlinks, and unlinked mentions (before `link` runs)
        g_json = json.loads(self.atlas("graph", "Max Mustermann", "--json").stdout)
        self.assertEqual(g_json["page"]["rel"], "people/max-mustermann.md")
        self.assertTrue(any(b["src"] == "areas/finanzen/nordbank.md" and b["anchor"] == "konditionen-kontakt"
                            for b in g_json["backlinks"]))
        self.assertTrue(any(u["src"] == "areas/finanzen/steuern.md" for u in g_json["unlinked_mentions"]))

        # 6. `atlas.py link` (`--scan`, `--journals --related --execute`, `--inline --execute`, idempotency)
        scan_out = self.atlas("link", "--scan").stdout
        self.assertIn("areas/finanzen/steuern.md", scan_out)
        self.assertIn("journal/2026/2026-05-01.md", scan_out)

        self.atlas("link", "--journals", "--related", "--execute")
        j_txt = self.read(os.path.join(self.brain, "journal", "2026", "2026-05-01.md"))
        self.assertIn("[areas/finanzen/nordbank.md](../../areas/finanzen/nordbank.md)", j_txt)
        self.assertIn("[people/max-mustermann.md](../../people/max-mustermann.md)", j_txt)
        st_txt = self.read(os.path.join(self.brain, "areas", "finanzen", "steuern.md"))
        self.assertIn("related: [areas/finanzen/nordbank.md, people/max-mustermann.md]", st_txt)

        self.atlas("link", "--inline", "--execute")
        st_txt2 = self.read(os.path.join(self.brain, "areas", "finanzen", "steuern.md"))
        self.assertIn("[Konto Nordbank](nordbank.md)", st_txt2)
        self.assertIn("[Berater Mustermann](../../people/max-mustermann.md)", st_txt2)
        self.assertIn("https://example.org/Max-Mustermann", st_txt2)
        # Idempotent second run changes 0 files
        idem = self.atlas("link", "--journals", "--related", "--inline", "--execute").stdout
        self.assertIn("0 file(s)", idem)

        # 7. `atlas.py context` builds token-budgeted dossier with `#section` extraction
        ctx = self.atlas("context", "areas/finanzen/nordbank.md", "--hops", "1", "--budget", "2500").stdout
        self.assertIn("## Primary: areas/finanzen/nordbank.md", ctx)
        self.assertIn("people/max-mustermann.md#konditionen-kontakt", ctx)
        self.assertLessEqual(len(ctx), 2600)

        # 8. `atlas.py conflicts` + briefing integration + `core-diff` ignores `_atlas/conflicts.json`
        c_added = json.loads(
            self.atlas("conflicts", "add", "--pages", "areas/finanzen/nordbank.md", "areas/finanzen/steuern.md",
                       "--summary", "Zinssatz widersprüchlich", "--json").stdout
        )
        cid = c_added["id"]
        self.assertIn(cid, self.atlas("conflicts", "list").stdout)
        brief = self.hook("claude", "session", {"session_id": "wg-brief", "cwd": fwd(self.root)})
        self.assertIn("Conflicts (1 open", brief)
        self.assertIn("Zinssatz widersprüchlich", brief)
        diff_out = self.atlas("core-diff", "--source", REPO).stdout
        self.assertNotIn("conflicts.json", diff_out)
        self.atlas("conflicts", "resolve", cid, "--resolution", "Geklärt laut Vertrag")
        self.assertIn("(no open conflicts)", self.atlas("conflicts", "list").stdout)


class HookUpgrades(AtlasCase):
    def test_cal_html_conditional_stop_autolink_offline_pull_errlog_and_ag_subagents(self):
        # 1. `hook_module(brain)` sets `CAL_HTML` inside the target brain, not the source repo
        sys.path.insert(0, os.path.join(REPO, "core", "tools"))
        import atlas as atlas_cli
        hk = atlas_cli.hook_module(self.brain)
        self.assertEqual(fwd(hk.CAL_HTML), fwd(os.path.join(self.brain, "_atlas", "cache", "calendar.html")))

        # 2. Antigravity subagent plugin wiring (project & user scope) + clean unwire
        self.wire_both()
        ag_plug = os.path.join(self.root, ".agents", "plugins", "atlas-subagents")
        self.assertTrue(os.path.isfile(os.path.join(ag_plug, "plugin.json")))
        self.assertTrue(os.path.isfile(os.path.join(ag_plug, "agents", "atlas-search.md")))
        self.assertTrue(os.path.isfile(os.path.join(ag_plug, "agents", "atlas-maintainer.md")))
        ag_search = self.read(os.path.join(ag_plug, "agents", "atlas-search.md"))
        self.assertIn("subagent: true", ag_search)
        self.assertIn("mainAgent: false", ag_search)
        self.assertIn("tools: [read_file, run_command, manage_task]", ag_search)
        self.assertNotIn("model:", ag_search)

        self.atlas("wire", "--agent", "antigravity", "--scope", "user", "--user-home", self.home, "--execute")
        user_plugs = os.path.join(self.home, ".gemini", "config", "plugins")
        self.assertTrue(any(d.startswith("atlas-subagents-") for d in os.listdir(user_plugs)))
        self.atlas("unwire", "--agent", "antigravity", "--scope", "user", "--user-home", self.home, "--execute")
        self.assertFalse(os.path.exists(user_plugs))

        # 3. `daily_log()` auto-links existing brain paths + conditional `render_calendar_html()` in `stop()`
        self.page("howto/git-regeln.md", "---\nsummary: Git Regeln\ntags: [howto]\n---\n# Git Regeln\n")
        # First session renders `calendar.html`
        self.hook("claude", "session", {"session_id": "h1", "cwd": fwd(self.root)})
        cal_html = os.path.join(self.brain, "_atlas", "cache", "calendar.html")
        self.assertTrue(os.path.isfile(cal_html))
        mtime_before_stop = os.path.getmtime(cal_html)
        time.sleep(0.05)

        # Stop turn touching only `howto/` mention in daily log -> `calendar.html` mtime must NOT change
        self.hook("claude", "stop", {
            "session_id": "h1",
            "stop_hook_active": True,
            "last_assistant_message": "Fertig.\n\nAtlas: Regel ergänzt in howto/git-regeln.md und howto/nicht-da.md",
        })
        self.assertEqual(os.path.getmtime(cal_html), mtime_before_stop)
        logs = [os.path.join(dp, f) for dp, _d, fs in os.walk(os.path.join(self.brain, "journal"))
                for f in fs if f[:4].isdigit()]
        log_txt = self.read(logs[0])
        self.assertIn("[howto/git-regeln.md](../../howto/git-regeln.md)", log_txt)
        self.assertIn("howto/nicht-da.md", log_txt)
        self.assertNotIn("[howto/nicht-da.md]", log_txt)

        # Modifying `calendar/README.md` -> next `stop()` MUST re-render `calendar.html`
        time.sleep(0.05)
        self.page("calendar/README.md", "---\nsummary: Kalender\ntags: [calendar]\n---\n# Calendar\n\n## Timeline\n- [ ] 2026-12-24 [event] Heiligabend\n")
        self.hook("claude", "stop", {"session_id": "h1", "stop_hook_active": True, "last_assistant_message": "ok"})
        self.assertGreater(os.path.getmtime(cal_html), mtime_before_stop)

        # 4. Offline remote in `pull()` fails silently (no false `Sync:` conflict warning)
        self.git(self.brain, "remote", "add", "origin", "http://127.0.0.1:1/offline-brain.git")
        sess_offline = self.hook("claude", "session", {"session_id": "h2", "cwd": fwd(self.root)})
        self.assertIn("Atlas · ok", sess_offline)
        self.assertNotIn("Sync:", sess_offline)

        # 5. Bounded `_atlas/cache/hook_errors.log` (<= 50 KB) + 1-line warning in next `session` hook
        for i in range(120):
            hk._log_hook_error(f"test.loop.{i}", RuntimeError("x" * 600))
        self.assertTrue(os.path.isfile(hk.ERR_LOG))
        self.assertLessEqual(os.path.getsize(hk.ERR_LOG), 50_000)
        sess_warn = self.hook("claude", "session", {"session_id": "h3", "cwd": fwd(self.root)})
        self.assertIn("Hook warning", sess_warn)
        # Second session does not repeat the same error warning
        sess_warn2 = self.hook("claude", "session", {"session_id": "h4", "cwd": fwd(self.root)})
        self.assertNotIn("Hook warning", sess_warn2)


class UiReleaseV11(AtlasCase):
    def test_v110_calendar_graph_view_and_core_apply_template_sync(self):
        # 1. Installed `_atlas/templates/calendar.html` is present after `init`
        inst_tpl = os.path.join(self.brain, "_atlas", "templates", "calendar.html")
        self.assertTrue(os.path.isfile(inst_tpl))
        self.assertIn("🕸️ Brain Graph", self.read(inst_tpl))

        # 2. `atlas.py calendar --view graph` prints `?view=graph` URL and compiles graph payload
        self.page("people/erika-muster.md", "---\nsummary: Erika Muster\ntags: [person]\nverified: 2024-02-01\n---\n# Erika Muster\n")
        self.atlas("index")
        out_graph = self.atlas("calendar", "--view", "graph").stdout
        self.assertIn("calendar.html?view=graph", out_graph)
        cal_html = os.path.join(self.brain, "_atlas", "cache", "calendar.html")
        html_txt = self.read(cal_html)
        self.assertIn("erika-muster.md", html_txt)
        self.assertIn("csrf_token", html_txt)

        # 3. `core-apply` deterministically updates `templates/calendar.html` (`HARD_CORE_PREFIXES`)
        #    while preserving user customizations in `templates/person.md` (`keep-local` 3-way merge)!
        person_tpl = os.path.join(self.brain, "_atlas", "templates", "person.md")
        with open(person_tpl, "a", encoding="utf-8", newline="\n") as fh:
            fh.write("\n## Custom Local Section\n")
        with open(inst_tpl, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("<!DOCTYPE html><html><body>outdated-v1.0-template</body></html>\n")
        with open(os.path.join(self.brain, "_atlas", "VERSION"), "w", encoding="utf-8", newline="\n") as fh:
            fh.write("1.0.0\n")
        self.git(self.brain, "add", "-A")
        self.git(self.brain, "commit", "-q", "-m", "local template edits")

        diff_out = self.atlas("core-diff", "--source", REPO).stdout
        self.assertIn("update (upstream changed, yours untouched): _atlas/templates/calendar.html", diff_out)
        self.assertIn("kept: changed only in this Atlas: _atlas/templates/person.md", diff_out)
        self.assertIn("1.1.1", diff_out)

        self.atlas("core-apply", "--source", REPO, "--execute")
        self.assertIn("🕸️ Brain Graph", self.read(inst_tpl))
        self.assertIn("## Custom Local Section", self.read(person_tpl))
        self.assertEqual(self.read(os.path.join(self.brain, "_atlas", "VERSION")).strip(), "1.1.1")
        self.assertIn("ok", self.atlas("check").stdout.lower())

        # 4. v1.1.1 Coach Sync & Readiness Regression:
        #    390W Garmin wrist-watt Run (`device_watts: true`) + virtual-watt Ride (`device_watts: false`)
        #    + morning Touran deadline MUST NOT pollute cycling power curve or turn Readiness AMBER!
        import datetime as dt
        today_iso = dt.date.today().isoformat()
        self.page(
            "coach/profile.md",
            "---\nsummary: Runner Profile\ntags: [self, sport]\ndepth: pro\n---\n# Profile\n\n"
            "- **Primary Sport:** Running\n- **Weight:** 71 kg\n- **Training Window:** 16:00-19:30\n",
        )
        self.page(
            "calendar/README.md",
            f"---\nsummary: Calendar\ntags: [calendar]\n---\n# Calendar\n\n## Timeline\n"
            f"- [ ] {today_iso} 10:00 [deadline] Werkstatt Angebot einholen: Motorölwechsel Touran\n"
            f"- [ ] {today_iso} 17:30 [event] Vereinssitzung\n",
        )
        mock_v111 = [
            {
                "id": "9101",
                "start_date_local": f"{today_iso}T07:30:00Z",
                "name": "Garmin Wrist Power 10k Run",
                "sport_type": "Run",
                "device_watts": True,
                "moving_time": 2400,
                "distance": 10200.0,
                "total_elevation_gain": 45,
                "streams": {
                    "watts": [395] * 2400,
                    "velocity_smooth": [4.35] * 1200 + [4.15] * 1200,
                    "heartrate": [165] * 2400,
                },
            },
            {
                "id": "9102",
                "start_date_local": f"{today_iso}T12:00:00Z",
                "name": "Commute Ride Virtual Watts",
                "sport_type": "Ride",
                "device_watts": False,
                "moving_time": 1800,
                "distance": 13500.0,
                "total_elevation_gain": 90,
                "streams": {"watts": [250] * 1800, "heartrate": [136] * 1800},
            },
        ]
        mock_v111_path = os.path.join(self.tmp, "strava_v111.json")
        with open(mock_v111_path, "w", encoding="utf-8") as fh:
            json.dump(mock_v111, fh)
        self.atlas("coach", "sync", "--mock-file", mock_v111_path, "--execute")

        acts_raw = json.loads(self.read(os.path.join(self.brain, "coach", "activities.json")))
        acts_saved = acts_raw if isinstance(acts_raw, list) else acts_raw["activities"]
        by_id = {str(a["id"]): a for a in acts_saved}
        self.assertTrue(by_id["9101"]["device_watts"])
        self.assertFalse(by_id["9102"]["device_watts"])
        self.assertNotIn("peaks_fresh", by_id["9101"])
        self.assertNotIn("peaks_fresh", by_id["9102"])
        self.assertIn("best_paces", by_id["9101"])

        pc_v111 = self.read(os.path.join(self.brain, "coach", "power-curve.md"))
        self.assertNotIn("395 W", pc_v111)
        self.assertNotIn("250 W", pc_v111)
        self.assertIn("/km", pc_v111)

        ctx_v111 = self.atlas("coach", "context", "--mode", "plan-14d", "--no-sync").stdout
        self.assertIn("Readiness: GREEN", ctx_v111)
        self.assertIn("Daniels VDOT", ctx_v111)
        self.assertIn("Training-window overlap today: 17:30 Vereinssitzung", ctx_v111)
        self.assertNotIn("FatMax", ctx_v111)
        self.assertNotIn("Storer", ctx_v111)


if __name__ == "__main__":
    os.makedirs(BASE_DIR, exist_ok=True)
    prog = unittest.main(argv=[sys.argv[0], "-v", *sys.argv[1:]], exit=False)
    if not os.listdir(BASE_DIR):
        os.rmdir(BASE_DIR)
    sys.exit(0 if prog.result.wasSuccessful() else 1)
