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
        self.assertTrue(os.path.isfile(os.path.join(self.brain, "coach", "playbook.md")))
        self.assertIn("[readiness.md](readiness.md)", self.read(coach_readme))
        self.assertIn("[playbook.md](playbook.md)", self.read(coach_readme))

        # 6. Coach v2: .FIT + Hammerhead/Garmin mock push/update/delete, Garmin wellness sync, & history/compare
        with open(prof_path, "w", encoding="utf-8") as fh:
            fh.write(
                "---\nsummary: Pro Profile\ntags: [self, sport]\ndepth: pro\nsex: male\nage: 26\ncreatine: yes\n---\n"
                "# Athlete Profile\n\n"
                "| Metric | Value |\n|---|---|\n"
                "| **Biometrie** | 184 cm · 74.0 kg · 11.5 % KFA |\n"
                "| **FTP (Cycling)** | 300 W |\n"
                "| **Max HR / Rest HR** | 198 bpm / 46 bpm |\n"
            )
        mock_dev_dir = os.path.join(self.tmp, "device_mock")
        push_out1 = self.atlas("coach", "push", "--days", "14", "--mock-dir", mock_dev_dir, "--execute").stdout
        self.assertIn("1 .zwo file(s)", push_out1)
        self.assertIn("1 .fit file(s)", push_out1)
        self.assertIn("Hammerhead mock POST", push_out1)
        self.assertIn("Garmin mock PUSH", push_out1)
        fit_path = os.path.join(self.brain, "coach", "workouts", f"{d_tmrw}_threshold-4x8m.fit")
        self.assertTrue(os.path.isfile(fit_path))
        with open(fit_path, "rb") as fh:
            fit_raw = fh.read()
        self.assertEqual(fit_raw[8:12], b".FIT")

        # Mark tomorrow's workout skipped `[-]` -> `coach push` must DELETE remote Karoo & Garmin workouts!
        cal_cur = self.read(cal_path).replace(f"- [ ] {d_tmrw} 17:30 [workout:cycling]", f"- [-] {d_tmrw} 17:30 [workout:cycling]")
        with open(cal_path, "w", encoding="utf-8") as fh:
            fh.write(cal_cur)
        push_out2 = self.atlas("coach", "push", "--days", "14", "--mock-dir", mock_dev_dir, "--execute").stdout
        self.assertIn("Hammerhead mock DELETE", push_out2)
        self.assertIn("Garmin mock DELETE", push_out2)

        # Wellness mock sync (RHR + HRV + Sleep + Weight) + On-Demand history & field-test
        w_mock = os.path.join(self.tmp, "garmin_wellness.json")
        with open(w_mock, "w", encoding="utf-8") as fh:
            json.dump([{"date": today.isoformat(), "rhr": 46, "sleep_h": 7.9, "sleep_score": 86, "hrv_rmssd": 74.0, "weight_kg": 73.8}], fh)
        w_sync_out = self.atlas("coach", "sync", "--wellness-mock", w_mock, "--execute").stdout
        self.assertIn("1 wellness day(s) imported", w_sync_out)
        self.assertIn("[hrv:74ms]", self.read(os.path.join(self.brain, "coach", "readiness.md")))

        hist_out = self.atlas("coach", "history", "--months", "6").stdout
        self.assertIn("COACH ON-DEMAND HISTORY", hist_out)
        ft_out = self.atlas("coach", "field-test", "--unit", "1", "--date", d_tmrw, "--execute").stdout
        self.assertIn("Field-Test Unit 1", ft_out)

        # Multi-turn hook continuity on follow-up answer
        follow_out = self.hook("claude", "prompt", {
            "prompt": "Ich nehme 5g Kreatin täglich und hatte nur wenig Zeit gestern",
            "session_id": "coach-sess",
            "cwd": fwd(self.root),
        })
        self.assertIn("Atlas coach (active session follow-up)", follow_out)

        # 7. Verify atlas.py check passes with 0 errors, 0 orphans, and within skill description budget
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
from test_coach_health import CoachHealthUnit  # noqa: E402,F401
from test_coach_physics import (  # noqa: E402,F401
    TestBlock1GoldenMasterKumpelMatrix,
    TestBlock2SolverAndPhenotypeInvariants,
    TestBlock3ChorleyAndSubstrateConservation,
    TestBlock4EnvironmentSpikesAndDurability,
    TestBlock5RunningBridgeRedsAndReadinessOverrides,
)
from test_ui_cal import UiCalUnit  # noqa: E402,F401
from test_cal_sync import CalSyncUnit  # noqa: E402,F401


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

        # 4. `atlas.py find` (Agentic Pull: FTS5 + 1-hop `↳ linked:`) vs. surgical prompt hook (entity leads only)
        f_out = self.atlas("find", "rückgewinnung").stdout
        self.assertIn("areas/finanzen/nordbank.md", f_out)
        self.assertIn("↳ linked: people/max-mustermann.md", f_out)
        p_body_only = self.hook("claude", "prompt", {"prompt": "was war bei der Kundenrückgewinnung?", "session_id": "wg1"})
        self.assertEqual(p_body_only, "")
        p_entity = self.hook("claude", "prompt", {
            "prompt": "wie erreiche ich Max Mustermann bei der Nordbank?",
            "session_id": "wg1",
        })
        self.assertIn("people/max-mustermann.md", p_entity)
        self.assertIn("areas/finanzen/nordbank.md", p_entity)
        self.assertNotIn("↳ linked:", p_entity)

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

        expected_ver = self.read(os.path.join(REPO, "core", "VERSION")).strip()
        diff_out = self.atlas("core-diff", "--source", REPO).stdout
        self.assertIn("update (upstream changed, yours untouched): _atlas/templates/calendar.html", diff_out)
        self.assertIn("kept: changed only in this Atlas: _atlas/templates/person.md", diff_out)
        self.assertIn(expected_ver, diff_out)

        self.atlas("core-apply", "--source", REPO, "--execute")
        self.assertIn("🕸️ Brain Graph", self.read(inst_tpl))
        self.assertIn("## Custom Local Section", self.read(person_tpl))
        self.assertEqual(self.read(os.path.join(self.brain, "_atlas", "VERSION")).strip(), expected_ver)
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

    def test_antigravity_session_delivery_stop_and_daily_task_triage(self):
        import datetime as dt
        today = dt.date.today()
        d_over3 = (today - dt.timedelta(days=3)).isoformat()
        d_over1 = (today - dt.timedelta(days=1)).isoformat()
        d_today = today.isoformat()
        d_plus4 = (today + dt.timedelta(days=4)).isoformat()
        d_plus5 = (today + dt.timedelta(days=5)).isoformat()
        d_tom = (today + dt.timedelta(days=1)).isoformat()

        self.page(
            "calendar/README.md",
            "---\nsummary: Central calendar and to-do hub\ntags: [atlas, calendar]\n---\n# Calendar & To-Dos\n\n"
            "## Timeline\n\n"
            f"- [ ] {d_over3} [todo] Offerte prüfen\n"
            f"- [ ] {d_today} [todo] Steuerunterlagen einreichen\n"
            f"- [ ] {d_plus5} 10:00 [event] Quartalsplanung\n\n"
            "## Open To-Dos\n\n"
            "- [ ] [high] Backup-Konzept testen\n",
        )
        self.page(
            "areas/vertraege/strom.md",
            f"---\nsummary: Stromvertrag\ntags: [contract]\ndue:\n  - {d_over1} Kündigungsfrist Stromvertrag\n---\n# Strom\n",
        )

        # 1. Antigravity SessionStart warm-up emits `{}` and NEVER burns `last_briefing` on empty tabs
        res_warm = subprocess.run(
            [sys.executable, "-I", "-B", self.hookf, "antigravity", "session"],
            input=json.dumps({"conversationId": "empty-tab", "workspacePaths": [fwd(self.root)]}),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        self.assertEqual(res_warm.returncode, 0)
        self.assertEqual(json.loads(res_warm.stdout.strip()), {})
        st_after_warm = json.loads(self.read(os.path.join(self.brain, "_atlas", "state.json")))
        self.assertEqual(st_after_warm.get("last_briefing", ""), "")

        # 2. First real Antigravity user turn (`PreInvocation`, invocationNum=0) delivers `Atlas · ok` + daily briefing
        #    + numbered Task check-in via non-deprecated `systemMessage`
        res_c1_raw = subprocess.run(
            [sys.executable, "-I", "-B", self.hookf, "antigravity", "prompt"],
            input=json.dumps({
                "conversationId": "c1",
                "invocationNum": 0,
                "lastUserInput": "moin, was liegt heute an?",
                "workspacePaths": [fwd(self.root)],
            }),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        self.assertEqual(res_c1_raw.returncode, 0)
        c1_json = json.loads(res_c1_raw.stdout.strip())
        self.assertIn("systemMessage", c1_json["injectSteps"][0])
        self.assertNotIn("ephemeralMessage", c1_json["injectSteps"][0])
        c1_text = c1_json["injectSteps"][0]["systemMessage"]["systemMessage"]
        self.assertIn("Atlas · ok", c1_text)
        self.assertIn("Atlas briefing (first chat today)", c1_text)
        self.assertIn("Task check-in", c1_text)
        self.assertIn("abhaken/done", c1_text)
        self.assertIn("verschieben/move", c1_text)
        self.assertIn("jetzt angehen", c1_text)
        self.assertIn(f"1. [OVERDUE 3 d · {d_over3}] Offerte prüfen", c1_text)
        self.assertIn(f"2. [OVERDUE 1 d · {d_over1}] [deadline] Kündigungsfrist Stromvertrag", c1_text)
        self.assertIn(f"3. [DUE TODAY · {d_today}] Steuerunterlagen einreichen", c1_text)
        self.assertIn("4. [OPEN · high] Backup-Konzept testen", c1_text)
        self.assertIn("Quartalsplanung", c1_text)

        # 3. Turn 2 in the SAME conversation `c1` (`invocationNum=0`) does NOT repeat `Atlas · ok` or briefing,
        #    ignores pronoun/greeting filler (`Ich`, `moin`, `atlas`), and uses hidden `ephemeralMessage` for real hints
        c1_turn2 = self.hook("antigravity", "prompt", {
            "conversationId": "c1",
            "invocationNum": 0,
            "lastUserInput": "Ich habe moin atlas gesagt, erklär mir das bitte kurz",
            "workspacePaths": [fwd(self.root)],
        })
        self.assertEqual(c1_turn2, "")

        res_c1_turn3 = subprocess.run(
            [sys.executable, "-I", "-B", self.hookf, "antigravity", "prompt"],
            input=json.dumps({
                "conversationId": "c1",
                "invocationNum": 0,
                "lastUserInput": "was steht im Stromvertrag?",
                "workspacePaths": [fwd(self.root)],
            }),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        c1_t3_json = json.loads(res_c1_turn3.stdout.strip())
        self.assertIn("ephemeralMessage", c1_t3_json["injectSteps"][0])
        self.assertNotIn("systemMessage", c1_t3_json["injectSteps"][0])
        self.assertIn("strom.md", c1_t3_json["injectSteps"][0]["ephemeralMessage"])

        # 4. Turn 1 in a SECOND conversation `c2` on the same day gets `Atlas · ok` + `Due today/overdue`, NOT briefing
        c2_turn1 = self.hook("antigravity", "prompt", {
            "conversationId": "c2",
            "invocationNum": 0,
            "lastUserInput": "Nein, merk dir ab jetzt immer UTF-8",
            "workspacePaths": [fwd(self.root)],
        })
        self.assertIn("Atlas · ok", c2_turn1)
        self.assertIn("Due today/overdue", c2_turn1)
        self.assertNotIn("Atlas briefing", c2_turn1)
        self.assertNotIn("Task check-in", c2_turn1)

        # 5. Antigravity `Stop` JSONL parser:
        #    - Read-only `read_file` + `ls -la` + `Atlas:` inside `thinking` NEVER nudges and NEVER leaks `thinking` to daily log
        tp_ro = os.path.join(self.tmp, "ag_readonly.jsonl")
        with open(tp_ro, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "USER_INPUT", "content": "moin atlas"}) + "\n")
            fh.write(json.dumps({
                "type": "PLANNER_RESPONSE",
                "thinking": "Maybe write\nAtlas: leaked thinking should never enter journal\n",
                "content": "",
                "tool_calls": [
                    {"name": "read_file", "args": {"Path": "brain/settings.md"}},
                    {"name": "run_command", "args": {"CommandLine": "\"ls -la brain/inbox/\""}},
                ],
            }) + "\n")
            fh.write(json.dumps({"type": "PLANNER_RESPONSE", "content": "Moin! Was steht an?"}) + "\n")
        stop_ro = self.hook("antigravity", "stop", {
            "conversationId": "c-readonly",
            "terminationReason": "NO_TOOL_CALL",
            "continuationActive": False,
            "fullyIdle": True,
            "lastUserInput": "moin atlas",
            "transcriptPath": fwd(tp_ro),
        })
        self.assertEqual(stop_ro, "")
        j_dir = os.path.join(self.brain, "journal")
        j_files = [os.path.join(dp, f) for dp, _d, fs in os.walk(j_dir) for f in fs if f.endswith(".md")] if os.path.isdir(j_dir) else []
        for jf in j_files:
            self.assertNotIn("leaked thinking", self.read(jf))

        #    - Mutating `write_file` without `Atlas:` DOES trigger reminder; `continuationActive` blocks loops
        tp_mut = os.path.join(self.tmp, "ag_mutating.jsonl")
        with open(tp_mut, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "USER_INPUT", "content": "leg eine notiz an"}) + "\n")
            fh.write(json.dumps({
                "type": "PLANNER_RESPONSE",
                "content": "Habe die Datei angelegt.",
                "tool_calls": [{"name": "write_file", "args": {"Path": "brain/areas/x.md"}}],
            }) + "\n")
        stop_mut = self.hook("antigravity", "stop", {
            "conversationId": "c-mut",
            "terminationReason": "NO_TOOL_CALL",
            "continuationActive": False,
            "fullyIdle": True,
            "lastUserInput": "leg eine notiz an",
            "transcriptPath": fwd(tp_mut),
        })
        self.assertIn("work was done without an `Atlas:` line", stop_mut)

        #    - Learning signal on `c2` triggers nudge once and respects `continuationActive`
        tp_ag = os.path.join(self.tmp, "ag_transcript.jsonl")
        with open(tp_ag, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "USER_INPUT", "content": "Nein, merk dir ab jetzt immer UTF-8"}) + "\n")
            fh.write(json.dumps({"type": "PLANNER_RESPONSE", "content": "Alles klar."}) + "\n")
        stop_cont = self.hook("antigravity", "stop", {
            "conversationId": "c2",
            "terminationReason": "NO_TOOL_CALL",
            "continuationActive": True,
            "fullyIdle": True,
            "lastUserInput": "Nein, merk dir ab jetzt immer UTF-8",
            "transcriptPath": fwd(tp_ag),
        })
        self.assertEqual(stop_cont, "")
        stop_nudge = self.hook("antigravity", "stop", {
            "conversationId": "c2",
            "terminationReason": "NO_TOOL_CALL",
            "continuationActive": False,
            "fullyIdle": True,
            "lastUserInput": "Nein, merk dir ab jetzt immer UTF-8",
            "transcriptPath": fwd(tp_ag),
        })
        self.assertIn("learning signal", stop_nudge)

        # 6. `atlas.py todo` end-to-end triage: list -> done #1 -> move undated -> move due_fm -> skip -> done due_fm -> add
        todo_list = self.atlas("todo").stdout
        self.assertIn("1. [OVERDUE 3 d", todo_list)
        self.assertIn("4. [OPEN · high] Backup-Konzept testen", todo_list)

        self.assertIn("done:", self.atlas("todo", "done", "1").stdout)
        self.assertIn(f"scheduled -> {d_plus4}:", self.atlas("todo", "move", "Backup-Konzept", "--to", "+4d").stdout)
        self.assertIn(f"rescheduled -> {d_tom}:", self.atlas("todo", "move", "Kündigungsfrist Stromvertrag", "--to", "tomorrow").stdout)
        self.assertIn("skipped:", self.atlas("todo", "skip", "Steuerunterlagen").stdout)
        self.assertIn("added:", self.atlas("todo", "add", "Neues Ticket anlegen", "--prio", "high").stdout)

        cal_after = self.read(os.path.join(self.brain, "calendar", "README.md"))
        self.assertIn(f"- [x] {d_over3} [todo] Offerte prüfen", cal_after)
        self.assertIn(f"- [-] {d_today} [todo] Steuerunterlagen einreichen", cal_after)
        self.assertIn(f"- [ ] {d_plus4} [todo] Backup-Konzept testen", cal_after)
        # Promoted out of `## Open To-Dos`
        open_sec = cal_after.split("## Open To-Dos", 1)[1]
        self.assertNotIn("Backup-Konzept testen", open_sec)
        self.assertIn("- [ ] [high] Neues Ticket anlegen", open_sec)

        strom_after_move = self.read(os.path.join(self.brain, "areas", "vertraege", "strom.md"))
        self.assertIn(f"- {d_tom} Kündigungsfrist Stromvertrag", strom_after_move)

        # Mark frontmatter `due:` deadline as done
        self.assertIn("done:", self.atlas("todo", "done", "Kündigungsfrist Stromvertrag").stdout)
        strom_after_done = self.read(os.path.join(self.brain, "areas", "vertraege", "strom.md"))
        self.assertIn(f'"[x] {d_tom} Kündigungsfrist Stromvertrag"', strom_after_done)

        todo_remaining = self.atlas("todo").stdout
        self.assertNotIn("Offerte prüfen", todo_remaining)
        self.assertNotIn("Steuerunterlagen einreichen", todo_remaining)
        self.assertNotIn("Kündigungsfrist Stromvertrag", todo_remaining)
        self.assertIn("Neues Ticket anlegen", todo_remaining)

        todo_with_upcoming = self.atlas("todo", "list", "--all").stdout
        self.assertIn(f"[in 4 d · {d_plus4}] Backup-Konzept testen", todo_with_upcoming)

    def test_v130_surgical_hook_capture_first_where_autoheal_and_extensions(self):
        # Set up entities, tags, and an active code project
        self.page(
            "tags.md",
            "---\nsummary: Tag list\ntags: [atlas]\n---\n# Tags\n\n"
            "| Tag | Meaning | Synonyms |\n"
            "|---|---|---|\n"
            "| **atlas** | About Atlas | |\n"
            "| **contract** | Contracts | vertrag |\n"
            "| **auto** | Vehicle | fahrzeug, kfz |\n"
            "| **person** | Contacts | kontakt |\n"
            "| **project** | Projects | projekt |\n",
        )
        self.page(
            "people/max-mustermann.md",
            "---\nsummary: Steuerberater Max Mustermann\ntags: [person]\naliases: [Mustermann]\n---\n"
            "# Max Mustermann\n\nKontakt für Mandanten.\n",
        )
        self.page(
            "areas/vertraege/stadtwerke-nord.md",
            "---\nsummary: Gastarif Stadtwerke Nord\ntags: [contract]\naliases: [SWN-Gas]\n---\n"
            "# Stadtwerke Nord\n\nVertragsnummer KD-884210.\n",
        )
        os.makedirs(os.path.join(self.root, "code", "webapp", ".git"), exist_ok=True)
        self.page(
            "projects/webapp/README.md",
            "---\nsummary: Webapp Repo\ntags: [project]\npath: code/webapp\n---\n# Project: Webapp\n",
        )
        self.atlas("index")

        # 1. Surgical prompt hook:
        #    - Meta/conversational prose produces ZERO false-positive page hints
        meta_out = self.hook("claude", "prompt", {
            "prompt": "Ich habe jetzt ein Atlas-Update gemacht und noch mal den gleichen Prompt wieder benutzt. Sieht das jetzt besser aus?",
            "session_id": "s-meta",
        })
        self.assertEqual(meta_out, "")

        #    - Exact named entity / alias / contract ID produces surgical lead
        ent_out = self.hook("claude", "prompt", {
            "prompt": "Was steht bei Stadtwerke Nord zu KD-884210?",
            "session_id": "s-ent",
        })
        self.assertIn("areas/vertraege/stadtwerke-nord.md", ent_out)

        #    - Mail & Todo domain pointers fire deterministically
        mail_out = self.hook("claude", "prompt", {
            "prompt": "Schau bitte in mein Postfach nach neuen E-Mails",
            "session_id": "s-dom",
        })
        self.assertIn("atlas.py mail", mail_out)
        todo_out = self.hook("claude", "prompt", {
            "prompt": "Welche offenen Aufgaben und To-Dos stehen heute an?",
            "session_id": "s-dom",
        })
        self.assertIn("atlas.py todo", todo_out)

        #    - Charter §1.5 project isolation: once routed into a code project, subsequent prompts stay quiet
        r1 = self.hook("claude", "prompt", {
            "prompt": "arbeite weiter im Projekt Webapp",
            "session_id": "s-proj",
            "cwd": fwd(self.root),
        })
        self.assertIn("project 'Webapp'", r1)
        r2 = self.hook("claude", "prompt", {
            "prompt": "baue für Max Mustermann einen neuen Button mit Preis 49,90 EUR ab 2026-12-01",
            "session_id": "s-proj",
            "cwd": fwd(os.path.join(self.root, "code", "webapp")),
        })
        self.assertEqual(r2, "")

        # 2. Capture-First signals & opt-out (`do_not_store`):
        fact_out = self.hook("claude", "prompt", {
            "prompt": "Der neue Abschlag liegt ab 2026-12-01 bei 115,50 EUR monatlich.",
            "session_id": "s-fact",
        })
        self.assertIn("Atlas learning signal (fact): capture-first", fact_out)
        self.assertIn("atlas.py where", fact_out)

        optout_prompt = "Bitte nicht speichern: Einmal-Code 774411 für den Test."
        opt_out = self.hook("claude", "prompt", {
            "prompt": optout_prompt,
            "session_id": "s-optout",
        })
        self.assertIn("Do NOT store", opt_out)
        self.assertNotIn("Atlas learning signal (fact)", opt_out)
        tp_opt = os.path.join(self.tmp, "optout.jsonl")
        with open(tp_opt, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "USER_INPUT", "content": optout_prompt}) + "\n")
            fh.write(json.dumps({
                "type": "PLANNER_RESPONSE",
                "content": "Verstanden, nichts gespeichert.\nAtlas: secret-leak-should-not-log",
            }) + "\n")
        stop_opt = self.hook("antigravity", "stop", {
            "conversationId": "s-optout",
            "terminationReason": "NO_TOOL_CALL",
            "continuationActive": False,
            "fullyIdle": True,
            "lastUserInput": optout_prompt,
            "transcriptPath": fwd(tp_opt),
        })
        self.assertEqual(stop_opt, "")
        j_dir = os.path.join(self.brain, "journal")
        j_files = [os.path.join(dp, f) for dp, _d, fs in os.walk(j_dir) for f in fs if f.endswith(".md")] if os.path.isdir(j_dir) else []
        for jf in j_files:
            self.assertNotIn("secret-leak-should-not-log", self.read(jf))

        # 3. `atlas.py where` placement & grouping advisor:
        w_out = self.atlas("where", "SWN-Gas", "Vertrag").stdout
        self.assertIn("areas/vertraege/stadtwerke-nord.md", w_out)
        self.assertIn("areas/vertraege/", w_out)

        # 4. Zero-token `Stop` auto-heal on newly written dirty brain pages:
        self.page(
            "areas/auto/fahrzeugschein.md",
            "---\nsummary: Zulassung Kombi\ntags: [kfz]\nverified: 2026-11-16\n---\n"
            "# Fahrzeugschein\n\nUnterlagen liegen bei [[Max Mustermann]]. Tarif läuft parallel zu Stadtwerke Nord.\n",
        )
        tp_heal = os.path.join(self.tmp, "heal.jsonl")
        with open(tp_heal, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "USER_INPUT", "content": "speichere fahrzeugschein"}) + "\n")
            fh.write(json.dumps({
                "type": "PLANNER_RESPONSE",
                "content": "Gespeichert.\nAtlas: areas/auto/fahrzeugschein.md angelegt",
                "tool_calls": [{"name": "write_file", "args": {"Path": "brain/areas/auto/fahrzeugschein.md"}}],
            }) + "\n")
        self.hook("antigravity", "stop", {
            "conversationId": "s-heal",
            "terminationReason": "NO_TOOL_CALL",
            "continuationActive": False,
            "fullyIdle": True,
            "lastUserInput": "speichere fahrzeugschein",
            "transcriptPath": fwd(tp_heal),
        })
        healed = self.read(os.path.join(self.brain, "areas", "auto", "fahrzeugschein.md"))
        self.assertIn("tags: [auto]", healed)
        self.assertIn("[Max Mustermann](../../people/max-mustermann.md)", healed)
        self.assertIn("related:", healed)
        self.assertIn("areas/vertraege/stadtwerke-nord.md", healed)

        # 5. Update-safe User Individualization (custom skill + `scripts/` + `triggers:` + `howto/scripts/` + overlay + secrets):
        self.page(
            "skills/gdrive/SKILL.md",
            "---\nname: gdrive\ndescription: \"Sync and search personal Google Drive folders.\"\n"
            "triggers: [google drive, gdrive]\n---\n# Google Drive Skill\n\nRun `atlas.py gdrive`.\n",
        )
        gdrive_script = os.path.join(self.brain, "skills", "gdrive", "scripts", "gdrive.py")
        os.makedirs(os.path.dirname(gdrive_script), exist_ok=True)
        with open(gdrive_script, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(
                "import os, sys\n"
                "print('GDRIVE_OK', bool(os.environ.get('ATLAS_BRAIN')), ' '.join(sys.argv[1:]))\n"
            )
        custom_sync = os.path.join(self.brain, "howto", "scripts", "custom_sync.py")
        os.makedirs(os.path.dirname(custom_sync), exist_ok=True)
        with open(custom_sync, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("print('CUSTOM_SYNC_OK')\n")
        self.page(
            "skills/atlas-mail/SKILL.md",
            "---\nname: atlas-mail\ndescription: \"Personal mail account mapping.\"\n---\n# Mail Overlay\n",
        )
        sec_file = os.path.join(self.brain, "_secrets", "gdrive.env")
        os.makedirs(os.path.dirname(sec_file), exist_ok=True)
        with open(sec_file, "w", encoding="utf-8") as fh:
            fh.write("GDRIVE_FOLDER_ID=dummy123\n")

        self.wire_both()
        # Subdirectory `scripts/gdrive.py` copied to both agent skill trees; core skill `atlas-mail` keeps overlay note
        self.assertTrue(os.path.isfile(os.path.join(self.root, ".agents", "skills", "gdrive", "scripts", "gdrive.py")))
        self.assertTrue(os.path.isfile(os.path.join(self.root, ".claude", "skills", "gdrive", "scripts", "gdrive.py")))
        mail_wired = self.read(os.path.join(self.root, ".agents", "skills", "atlas-mail", "SKILL.md"))
        self.assertIn("brain/skills/atlas-mail/SKILL.md", mail_wired)

        # Prompt hook detects `triggers: [google drive, gdrive]`
        ext_hint = self.hook("antigravity", "prompt", {
            "conversationId": "s-ext",
            "invocationNum": 0,
            "lastUserInput": "Lade den Vertrag bitte in Google Drive hoch",
            "workspacePaths": [fwd(self.root)],
        })
        self.assertIn("skill 'gdrive'", ext_hint)

        # `atlas.py ext` lists both commands and `atlas.py gdrive` / `atlas.py custom-sync` execute directly
        ext_json = json.loads(self.atlas("ext", "--json").stdout)
        ext_names = {a for c in ext_json["custom_commands"] for a in c["aliases"]}
        self.assertIn("gdrive", ext_names)
        self.assertIn("custom-sync", ext_names)
        gd_run = self.atlas("gdrive", "--folder", "Steuern").stdout.strip()
        self.assertEqual(gd_run, "GDRIVE_OK True --folder Steuern")
        cs_run = self.atlas("custom-sync").stdout.strip()
        self.assertEqual(cs_run, "CUSTOM_SYNC_OK")

        # Simulate upstream `/atlas-update` (`core-apply` + `wire`) -> all user customizations survive 100% intact!
        up_repo = os.path.join(self.tmp, "upstream_next")
        shutil.copytree(
            REPO, up_repo,
            ignore=shutil.ignore_patterns(".git", "__pycache__", "_atlas"),
        )
        with open(os.path.join(up_repo, "core", "VERSION"), "w", encoding="utf-8") as fh:
            fh.write("9.9.9\n")
        self.atlas("core-apply", "--source", up_repo, "--execute")
        self.wire_both()

        self.assertEqual(self.read(os.path.join(self.brain, "_atlas", "VERSION")).strip(), "9.9.9")
        self.assertTrue(os.path.isfile(gdrive_script))
        self.assertTrue(os.path.isfile(custom_sync))
        self.assertTrue(os.path.isfile(sec_file))
        self.assertTrue(os.path.isfile(os.path.join(self.root, ".agents", "skills", "gdrive", "scripts", "gdrive.py")))
        self.assertEqual(self.atlas("gdrive", "--ping").stdout.strip(), "GDRIVE_OK True --ping")
        chk_final = self.atlas("check", check=False).stdout
        self.assertNotIn("ERROR", chk_final, chk_final)
        self.assertNotIn("exceeds 2400", chk_final, chk_final)

    def test_v131_niche_entity_recall_numeric_filter_where_phrases_and_py_cmd(self):
        # 1. Niche leaf pages WITHOUT explicit `aliases:` (long titles with subtitles/parens, stem prefixes, org suffixes)
        self.page("tags.md", (
            "# Tags\n\n| Tag | Meaning | Synonyms |\n|---|---|---|\n"
            "| **finanzen** | Money | budget, haushaltsbudget, cashflow, konto |\n"
            "| **versicherung** | Insurance | police, kasko |\n"
            "| **mobilitaet** | Vehicles | auto, fahrzeug |\n"
            "| **technik** | Tech | hardware, gpu |\n"
        ))
        self.page("glossary.md", "---\nsummary: Glossary\ntags: [atlas]\n---\n# Glossary\n- **VeloClub**: Local cycling club\n")
        self.page("areas/sport/trek-madone-sl7.md", (
            "---\nsummary: Trek Madone SL7 (7.90 kg scale weight) build dossier\ntags: [technik]\n---\n"
            "# Trek Madone SL7 (2024) Custom Race Build — Master Dossier\n\nScale weight: 7.90 kg.\n"
        ))
        self.page("areas/sport/veloclub-ev.md", (
            "---\nsummary: Club charter and registration\ntags: [technik]\n---\n"
            "# VeloClub e.V. (Founding & Charter)\n\nRegistration notes.\n"
        ))
        self.page("areas/technik/gpu-benchmark.md", (
            "---\nsummary: GPU benchmark at 145 FPS target for 01.01.2027\ntags: [technik]\n---\n"
            "# GPU Upgrade Analysis (2026)\n\nTarget: 145 FPS by 01.01.2027 for 8.499 points.\n"
        ))
        self.page("areas/wohnen/versicherungen.md", (
            "---\nsummary: All policies including Car Insurance for Polestar 2\ntags: [versicherung, finanzen]\n"
            "aliases: [Kfz-Versicherung, Versicherungen]\n---\n# Versicherungen (Privat)\n\nPolestar 2 policy: 980 EUR/yr.\n"
        ))
        self.page("areas/wohnen/fuhrpark-polestar.md", (
            "---\nsummary: Polestar 2 Long Range (VIN PS99887766) fleet & charging\ntags: [mobilitaet, finanzen]\n"
            "aliases: [Polestar, Polestar 2]\n---\n# Fuhrpark: Polestar 2\n\nInsurance linked to versicherungen.md.\n"
        ))
        # Create >= 3 finance leaves + an Area Hub README to verify Area Hub routing on broad domain queries
        self.page("areas/finanzen/README.md", "---\nsummary: Household finances hub\ntags: [finanzen]\n---\n# Finanzen\n")
        for i, slug in enumerate(("konto-a", "depot-b", "steuern-c"), 1):
            self.page(f"areas/finanzen/{slug}.md", f"---\nsummary: Finance leaf {i} (99001122{i})\ntags: [finanzen]\n---\n# Leaf {i}\nRef 01.01.2027 and 99001122{i}.\n")
        self.atlas("index")

        # 2. Niche recall: "Trek Madone" (2-word stem/title prefix without SL7) & "VeloClub" (canonical page beats glossary.md)
        h_niche = self.hook("antigravity", "prompt", {
            "conversationId": "v131-niche",
            "invocationNum": 0,
            "lastUserInput": "Ich brauche kurz für mich selbst eine Antwort: Was wiegt mein Trek Madone und was ist bei VeloClub offen?",
            "workspacePaths": [fwd(self.root)],
        })
        self.assertIn("areas/sport/trek-madone-sl7.md", h_niche)
        self.assertIn("areas/sport/veloclub-ev.md", h_niche)
        self.assertNotIn("glossary.md", h_niche)

        # 3. Numeric/date false-positive filter + Area Hub preference on broad domain queries
        h_budget = self.hook("antigravity", "prompt", {
            "conversationId": "v131-budget",
            "invocationNum": 0,
            "lastUserInput": "Rein hypothetisch (off the record): Wenn ab 01.01.2027 145 € für ein Rad abgingen, was macht das mit dem Haushaltsbudget für 8.499 €?",
            "workspacePaths": [fwd(self.root)],
        })
        self.assertIn("Atlas privacy guard (do-not-store)", h_budget)
        self.assertNotIn("gpu-benchmark.md", h_budget)
        self.assertIn("areas/finanzen/README.md", h_budget)
        # Meanwhile >=6-digit contract IDs still resolve directly to the exact leaf page
        h_id = self.hook("antigravity", "prompt", {
            "conversationId": "v131-id",
            "invocationNum": 0,
            "lastUserInput": "Prüfe Vertrag 990011222",
            "workspacePaths": [fwd(self.root)],
        })
        self.assertIn("areas/finanzen/depot-b.md", h_id)

        # 4. `atlas.py where "<multi-word phrase>"` tokenizes phrases & compounds and skips structural hubs in Section 1
        w_out = self.atlas("where", "Kfz-Versicherung Polestar 2").stdout
        self.assertNotIn("(no existing page matched", w_out)
        sec1 = w_out.split("=== 2. BRAIN GROUPS")[0]
        self.assertIn("areas/wohnen/versicherungen.md", sec1)
        self.assertIn("areas/wohnen/fuhrpark-polestar.md", sec1)
        self.assertNotIn("areas/finanzen/README.md", sec1)

        # 5. `core-apply --execute` records `last_update_check` in `state.json`
        up_repo = os.path.join(self.tmp, "up_131")
        shutil.copytree(REPO, up_repo, ignore=shutil.ignore_patterns(".git", "__pycache__", "_atlas"))
        with open(os.path.join(up_repo, "core", "VERSION"), "w", encoding="utf-8") as fh:
            fh.write("9.9.9\n")
        self.wire_both()
        self.atlas("core-apply", "--source", up_repo, "--execute")
        st = json.loads(self.read(os.path.join(self.brain, "_atlas", "state.json")))
        self.assertTrue(bool(st.get("last_update_check")))

    def test_v132_mail_todo_rx_event_add_batch_mail_and_project_linked_context(self):
        # 1. `MAIL_RX` matches natural plural "5 neusten mails zusammen", ignores "E-Mail-Adresse"
        h_mail = self.hook("antigravity", "prompt", {
            "conversationId": "v132-mail",
            "invocationNum": 0,
            "lastUserInput": "Fasse mir meine 5 neusten mails zusammen von privat und geschäftlich",
            "workspacePaths": [fwd(self.root)],
        })
        self.assertIn("Atlas mail ->", h_mail)

        h_addr = self.hook("antigravity", "prompt", {
            "conversationId": "v132-addr",
            "invocationNum": 0,
            "lastUserInput": "Wie lautet die E-Mail-Adresse vom Support?",
            "workspacePaths": [fwd(self.root)],
        })
        self.assertNotIn("Atlas mail ->", h_addr)

        # 2. `TODO_RX` + `SIGNAL_EXPLICIT` on "Speicher mir ab ... Zahnarzt Termin" + `todo add --to --time --kind event`
        h_sched = self.hook("antigravity", "prompt", {
            "conversationId": "v132-sched",
            "invocationNum": 0,
            "lastUserInput": "Speicher mir ab dass ich nächste Woche Mittwoch 10:15 Uhr einen Zahnarzt Termin habe",
            "workspacePaths": [fwd(self.root)],
        })
        self.assertIn("Atlas tasks/calendar ->", h_sched)
        self.assertIn("--kind event", h_sched)
        self.assertIn("Atlas learning signal (explicit)", h_sched)

        add_ev = self.atlas("todo", "add", "Dentist checkup", "--to", "2026-11-25", "--time", "10:15", "--kind", "event").stdout
        self.assertIn("2026-11-25 10:15 [event] Dentist checkup", add_ev)
        cal_txt = self.read(os.path.join(self.brain, "calendar", "README.md"))
        self.assertIn("- [ ] 2026-11-25 10:15 [event] Dentist checkup", cal_txt)

        # 3. `Atlas route` surfaces 1-hop linked brain context on Turn 1 and stays silent on Turn 2
        os.makedirs(os.path.join(self.root, "code", "game-mod"), exist_ok=True)
        self.page("areas/tech/workstation-drives.md", (
            "---\nsummary: Steam library paths and GPU setup\ntags: [project]\n---\n# Workstation Drives\nSteam at /mnt/d/SteamLibrary.\n"
        ))
        self.page("projects/game-mod/README.md", (
            "---\nsummary: Game mod repo\ntags: [project]\npath: code/game-mod\nrelated: [areas/tech/workstation-drives.md]\n---\n"
            "# Project: Game Mod\n\nSee [Workstation Drives](../../areas/tech/workstation-drives.md).\n"
        ))
        self.atlas("index")
        h_proj1 = self.hook("antigravity", "prompt", {
            "conversationId": "v132-proj",
            "invocationNum": 0,
            "lastUserInput": "Baue in Game Mod ein kleines Menü-Banner ein",
            "workspacePaths": [fwd(self.root)],
        })
        self.assertIn("Atlas route -> project 'Game Mod'", h_proj1)
        self.assertIn("areas/tech/workstation-drives.md", h_proj1)
        self.assertNotIn("Atlas mail ->", h_proj1)
        h_proj2 = self.hook("antigravity", "prompt", {
            "conversationId": "v132-proj",
            "invocationNum": 0,
            "lastUserInput": "Schreib noch einen Termin-Kommentar für mails ins Skript",
            "workspacePaths": [fwd(self.root)],
        })
        # Inside active code project: zero mail/todo/entity noise on Turn 2!
        self.assertEqual(h_proj2, "")

        # 4. `atlas_mail.py` strips invisible preheader padding & parses multi-UID `read` + `--body-chars`
        sys.path.insert(0, os.path.join(REPO, "core", "tools"))
        import atlas_mail
        raw_mime = (
            b"From: Promo <hello@example.com>\r\n"
            b"To: user@example.com\r\n"
            b"Subject: Deal\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
            + ("Sale starts soon! " + "\u034f\u200c\xad " * 120 + "Real content after padding.").encode("utf-8")
        )
        parsed_m = atlas_mail.parse_message_bytes("42", "INBOX", raw_mime)
        self.assertIn("Real content after padding.", parsed_m["snippet"])
        self.assertNotIn("\u034f", parsed_m["snippet"])
        p_mail = atlas_mail.build_parser()
        ns_rd = p_mail.parse_args(["read", "101", "102", "103", "-a", "WORK"])
        self.assertEqual(ns_rd.uid, ["101", "102", "103"])
        ns_ls = p_mail.parse_args(["list", "-a", "WORK", "--body-chars", "900"])
        self.assertEqual(ns_ls.body_chars, 900)

    def test_v133_small_brain_custom_folders_flat_areas_and_windows_readonly(self):
        self.wire_both()

        # 1. Fresh tiny brain (< 10 pages total) with flat `areas/car.md` (no subfolder hub!) + custom top-level `notes/`
        self.page("tags.md", (
            "# Tags\n\n| Tag | Meaning | Synonyms |\n|---|---|---|\n"
            "| **mobility** | Mobility & vehicles | car, vehicle, insurance |\n"
            "| **study** | Study notes | math, university |\n"
        ))
        self.page("areas/car.md", (
            "---\nsummary: Flat car policy page without subfolder hub\ntags: [mobility]\n---\n"
            "# Family Car\n\nPolicy number `9876543210`.\n"
        ))
        self.page("notes/README.md", (
            "---\nsummary: Custom top-level study notes hub\ntags: [study]\n---\n"
            "# Study Notes\n\n- [Linear Algebra](linear-algebra.md)\n"
        ))
        self.page("notes/linear-algebra.md", (
            "---\nsummary: Eigenvalues, SVD, and spectral theorem\ntags: [study]\n---\n"
            "# Linear Algebra\n\nNotes on matrix eigenvalues.\n"
        ))
        self.atlas("index")

        # Tiny brain (< 12 pages): ID `9876543210` and unhubbed tag `vehicle` (#mobility) must NOT be dropped by 0.12*n < 1
        h_id = self.hook("antigravity", "prompt", {
            "conversationId": "v133-id",
            "invocationNum": 0,
            "lastUserInput": "What is contract 9876543210?",
            "workspacePaths": [fwd(self.root)],
        })
        self.assertIn("areas/car.md", h_id)

        h_flat_tag = self.hook("antigravity", "prompt", {
            "conversationId": "v133-tag",
            "invocationNum": 0,
            "lastUserInput": "Check my vehicle insurance details",
            "workspacePaths": [fwd(self.root)],
        })
        self.assertIn("areas/car.md", h_flat_tag)

        # Custom top-level folder `notes/linear-algebra.md` matched directly in PreInvocation + `atlas.py where`
        h_custom = self.hook("antigravity", "prompt", {
            "conversationId": "v133-custom",
            "invocationNum": 0,
            "lastUserInput": "Explain my Linear Algebra summary",
            "workspacePaths": [fwd(self.root)],
        })
        self.assertIn("notes/linear-algebra.md", h_custom)

        wh_out = self.atlas("where", "eigenvalues").stdout
        self.assertIn("notes/linear-algebra.md", wh_out)
        self.assertIn("notes/ (1 pages; custom folder, hub: notes/README.md)", wh_out)
        self.assertIn("areas/<topic>.md (1 flat pages", wh_out)

        # Retroactive journal linking works for custom top-level folders (`brain/notes/linear-algebra.md`)
        import datetime as dt
        today = dt.date.today().isoformat()
        j_rel = f"journal/{today[:4]}/{today}.md"
        self.page(j_rel, f"# {today}\n\n- 12:00 · — · updated brain/notes/linear-algebra.md\n")
        self.atlas("link", "--journals", "--execute")
        j_linked = self.read(os.path.join(self.brain, j_rel))
        self.assertIn("[brain/notes/linear-algebra.md](../../notes/linear-algebra.md)", j_linked)

        # 2. Native Windows (`python.exe`, `py -3`, quoted paths with spaces, PowerShell read-only vs write cmdlets)
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "atlas_hook_v133", os.path.join(REPO, "core", "hooks", "atlas_hook.py")
        )
        hk_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(hk_mod)

        readonly_cmds = [
            r'"C:\Users\Alex Morgan\AppData\Local\Programs\Python\Python312\python.exe" -I "C:\Users\Alex Morgan\Atlas\brain\_atlas\tools\atlas.py" find Matrix',
            r'py -3 -I C:\Atlas\brain\_atlas\tools\atlas.py mail list -a WORK 2>$null',
            r'Get-ChildItem C:\Atlas\brain | Select-String "Matrix"',
            r'git.exe -C "C:\Users\Alex Morgan\Atlas\brain" status --short',
            r'dir C:\Atlas\brain && where.exe python',
            r'python3 -I brain/_atlas/tools/atlas.py check 2>/dev/null',
        ]
        for c in readonly_cmds:
            self.assertTrue(hk_mod._is_readonly_shell_cmd(c), f"Expected read-only True for: {c}")

        mutating_cmds = [
            r'Set-Content -Path C:\Atlas\brain\notes\x.md -Value "hi"',
            r'Get-ChildItem | Out-File C:\Atlas\out.txt',
            r'Remove-Item C:\Atlas\brain\notes\x.md',
            r'python -I C:\Atlas\brain\_atlas\tools\atlas.py todo add "Task" --to tomorrow',
            r'python -I C:\Atlas\brain\_atlas\tools\atlas.py mail move 42 --to Archive -a WORK --yes',
            r'echo test > C:\Atlas\out.txt',
        ]
        for c in mutating_cmds:
            self.assertFalse(hk_mod._is_readonly_shell_cmd(c), f"Expected mutating False for: {c}")

    def test_coach_v145_physics_and_guardrails(self):
        """v1.4.5: Mader vLamax_met + dynamic Sub-CP, W'-scaled 1-Hz solver, VO2(t) guard, pre-fatigue & polish."""
        import datetime as dt
        import importlib.util
        sys.path.insert(0, os.path.join(REPO, "core", "tools"))
        from atlas_lib import pillars_engine as pe, coach_engine as ce

        # 1. Separate alactic 15s sprint proxy from inverse-Mader vLamax_met + dynamic f(CP, vLamax_met)
        body = {"mass_kg_7d": 75.0, "kfa_pct": 11.5, "kfa_calibrated": True, "ffm_kg": 66.38, "vol_rel_ind": 0.4458, "v_la_l": 33.43}
        p1 = pe.compute_pillar1_oxidative(418.0, 31400.0, 75.0, 66.38, True)
        curves_fast = {"fresh": {"15s": {"42d": 766, "90d": 1094, "365d": 1094}}}
        p2_fast = pe.compute_pillar2_metabolic_mader(curves_fast, 294.0, p1["vo2max_rel_clean"], body, t_alac_s=2.0)
        self.assertGreater(p2_fast["vlamax_sprint_proxy"], 0.85)
        self.assertTrue(0.52 <= p2_fast["vlamax_met_mmol_l_s"] <= 0.66, p2_fast)
        self.assertEqual(p2_fast["fatmax_band_w"], p2_fast["mader_fatmax_95_band_w"])
        self.assertEqual(p2_fast["p_fatmax_w"], p2_fast["mader_mfo_peak_w"])
        self.assertTrue(150 <= p2_fast["fatmax_band_w"][0] <= 175, p2_fast)
        self.assertTrue(200 <= p2_fast["fatmax_band_w"][1] <= 220, p2_fast)
        self.assertTrue(225 <= p2_fast["p_lt1_w"] <= 236, p2_fast)

        # Diesel phenotype (low vLamax_met) gets higher FatMax & LT1 % CP than high-vLamax Sprinter at identical CP
        p2_diesel = pe.compute_pillar2_metabolic_mader({"fresh": {"15s": {"90d": 620}}}, 294.0, 56.0, body, t_alac_s=3.0)
        self.assertGreater(p2_diesel["p_fatmax_w"], p2_fast["p_fatmax_w"])
        self.assertGreater(p2_diesel["p_lt1_w"], p2_fast["p_lt1_w"])
        self.assertGreater(p2_diesel["p_mlss_w"], p2_fast["p_mlss_w"])

        # 2. Constrained 1-Hz inverse W'bal + VO2(t) solver (Fast-Myotype 31.4 kJ vs Diesel 10.5 kJ)
        rx_fast = pe.solve_individual_interval_prescriptions(294.0, 31400.0, 418.0, p2_fast, {}, 75.0)
        self.assertLess(rx_fast["re_entry_opener"]["t90_min"], 2.0)
        self.assertGreaterEqual(rx_fast["vo2max_float_30_15"]["t90_min"], 7.0)
        self.assertLessEqual(rx_fast["vo2max_float_30_15"]["on_w"], rx_fast["iix_cap_w"])
        self.assertGreaterEqual(rx_fast["vo2max_float_30_15"]["w_bal_min_kj"], 5.0)
        self.assertGreaterEqual(rx_fast["vo2max_hard_start_4x4"]["t90_min"], 9.0)
        self.assertGreater(rx_fast["vo2max_hard_start_4x4"]["start_w"], rx_fast["vo2max_hard_start_4x4"]["hold_w"] + 40)
        # 1:4 Shuttle anchored near LT1 (80-86% CP) and Over capped well below 460W
        self.assertTrue(235 <= rx_fast["lactate_shuttle_1_4"]["under_w"] <= 253, rx_fast["lactate_shuttle_1_4"])
        self.assertTrue(340 <= rx_fast["lactate_shuttle_1_4"]["over_w"] <= 385, rx_fast["lactate_shuttle_1_4"])

        # Diesel safety floor >= 3.0 kJ
        rx_diesel = pe.solve_individual_interval_prescriptions(294.0, 10500.0, 330.0, p2_diesel, {}, 65.0)
        self.assertGreaterEqual(rx_diesel["target_reserve_kj"], 3.0)
        self.assertGreaterEqual(rx_diesel["vo2max_float_30_15"]["w_bal_min_kj"], 2.8)
        self.assertLess(rx_diesel["vo2max_float_30_15"]["on_w"], rx_fast["vo2max_float_30_15"]["on_w"])

        # 3. 1-Hz VO2(t) Compiler-Guard catches fake VO2 (`4x 40s@705W / 4m@155W`), blowout (`450/225W`), and pre-fatigue
        fake_vo2 = [180.0] * 600 + ([705.0] * 40 + [155.0] * 240) * 4
        sim_fake = pe.simulate_workout_stream(fake_vo2, 294.0, 31400.0, 418.0, 75.0, title="VO2max Killer")
        self.assertIn("STIMULUS MISMATCH", sim_fake["stimulus_mismatch"])
        self.assertLess(sim_fake["time_ge_90_vo2max_min"], 2.0)

        blow_set = ([450.0] * 30 + [225.0] * 15) * 12
        blow_stream = [180.0] * 600 + blow_set + [153.0] * 300 + blow_set + [153.0] * 300 + blow_set
        sim_blow = pe.simulate_workout_stream(blow_stream, 294.0, 31400.0, 418.0, 75.0, title="Float 30/15")
        self.assertEqual(sim_blow["stimulus_class"], "UNSUSTAINABLE_BLOWOUT")
        self.assertIn("BLOWOUT WARNING", sim_blow["stimulus_mismatch"])

        good_fl = [180.0] * 600 + ([388.0] * 30 + [205.0] * 15) * 13 + [153.0] * 300 + ([388.0] * 30 + [205.0] * 15) * 13 + [153.0] * 300 + ([388.0] * 30 + [205.0] * 15) * 13
        sim_fresh = pe.simulate_workout_stream(good_fl, 294.0, 31400.0, 418.0, 75.0, title="VO2max Float 30/15")
        sim_fatig = pe.simulate_workout_stream(good_fl, 294.0, 31400.0, 418.0, 75.0, prior_kj=1600.0, cho_g_h=30.0, title="VO2max Float 30/15")
        self.assertEqual(sim_fresh["stimulus_class"], "TRUE_VO2MAX_SEVERE")
        self.assertEqual(sim_fresh["stimulus_mismatch"], "")
        self.assertLess(sim_fatig["cp_eff_w"], sim_fresh["cp_eff_w"])
        self.assertLess(sim_fatig["w_bal_min_kj"], sim_fresh["w_bal_min_kj"])

        # 4. Polish: solve_cp_w_prime rejects unpaced single-month submax 20m collapse
        bad_cp = ce.solve_cp_w_prime({"3m": 455, "5m": 418, "12m": 265, "20m": 235})
        self.assertFalse(bad_cp["valid"])

        # 5. CLI integration: coach simulate + coach push superseded archival + coach note no premature [x] + COACH_RX
        today = dt.date.today()
        t_iso = today.isoformat()
        tom_iso = (today + dt.timedelta(days=1)).isoformat()
        self.atlas("coach", "context", "--init", "--execute")
        self.page("coach/profile.md", (
            "---\nsummary: Profile\ntags: [sport]\ndepth: pro\nsex: male\nage: 25\ncreatine: false\n---\n"
            "# Athlete Profile\n\n| Metric | Value | Notes |\n|---|---|---|\n"
            "| Biometrie | 186 cm · 75,0 kg · 11,5 % KFA | Test |\n"
            "| CP / W' | 294 W / 31.4 kJ | Anchor |\n"
            "| Max HR / Rest HR | 200 bpm / 48 bpm | Test |\n"
        ))
        self.page("calendar/README.md", (
            "---\nsummary: Cal\ntags: [atlas, calendar]\n---\n# Calendar\n\n## Timeline\n\n"
            f"- [ ] {t_iso} 17:30 [workout:strength] Upper Body Strength (35m)\n"
            f"- [ ] {tom_iso} 17:30 [workout:cycling] Base Z2 Old (60m | 10m WU 55-68%, 40m Z2@68%, 10m CD 62-48%)\n"
        ))
        # Morning readiness note must NOT mark today's open evening strength workout `[x]`
        self.atlas("coach", "note", "--date", t_iso, "--sleep", "4.5", "--sleep-quality", "poor", "--text", "Tired morning", "--execute")
        cal_after = self.read(os.path.join(self.brain, "calendar", "README.md"))
        self.assertIn(f"- [ ] {t_iso} 17:30 [workout:strength]", cal_after)

        # Historical RPE note must NOT append a spurious AMBER row to readiness.md
        old_d = (today - dt.timedelta(days=12)).isoformat()
        self.atlas("coach", "note", "--date", old_d, "--rpe", "7", "--text", "RPE 7: tough hills", "--execute")
        rdy_after = self.read(os.path.join(self.brain, "coach", "readiness.md"))
        self.assertNotIn(f"| {old_d} |", rdy_after)

        # `coach push` archives superseded workouts into `_superseded/` and prints 1Hz-Guard audit
        self.atlas("coach", "push", "--execute")
        self.page("calendar/README.md", (
            "---\nsummary: Cal\ntags: [atlas, calendar]\n---\n# Calendar\n\n## Timeline\n\n"
            f"- [ ] {tom_iso} 17:30 [workout:cycling] VO2max Hard-Start (60m | 10m WU 55-68%, 4x (45s@420W + 3.25m@350W) / 5m@147W, 8m CD 60-48%)\n"
        ))
        push_out = self.atlas("coach", "push", "--execute").stdout
        self.assertIn("1Hz-Guard", push_out)
        self.assertIn("superseded archived", push_out)
        sup_dir = os.path.join(self.brain, "coach", "workouts", "_superseded")
        self.assertTrue(os.path.isdir(sup_dir) and any("base-z2-old" in f for f in os.listdir(sup_dir)))

        # `coach simulate` CLI works with watts, Hard-Start syntax, and `--prior-kj`
        sim_out = self.atlas("coach", "simulate", "--dsl", "10m WU 55-68%, 4x (45s@420W + 3.25m@350W) / 5m@147W", "--prior-kj", "1600", "--cho-g-h", "35").stdout
        self.assertIn("1-HZ BIOPHYSICAL WORKOUT SIMULATION", sim_out)
        self.assertIn("Pre-Fatigue Applied", sim_out)
        self.assertIn("TRUE_VO2MAX_SEVERE", sim_out)

        # COACH_RX matches Test 7 sensor/lactate/CP query on Step 1
        spec_hk = importlib.util.spec_from_file_location("atlas_hook_v145", os.path.join(REPO, "core", "hooks", "atlas_hook.py"))
        hk_m = importlib.util.module_from_spec(spec_hk)
        spec_hk.loader.exec_module(hk_m)
        mode7, _, _ = hk_m.detect_coach_mode("Wenn ich heute beim Laufen 310 W hatte und gestern auf der Rolle ein 1580 W 5s-Peak, was heißt das für CP und Laktatbildung?")
        self.assertEqual(mode7, "daily")

    def test_coach_v146_solver_helpers_and_bugfixes(self):
        """v1.4.6: `--solve` pre-fatigue/form bisection, nested-set DSL, Diesel FatMax/safety-floor, creatine physics, MLSS guard & atomic helpers."""
        import importlib.util
        sys.path.insert(0, os.path.join(REPO, "core", "tools"))
        from atlas_lib import pillars_engine as pe

        spec_hk = importlib.util.spec_from_file_location("atlas_hook_v146", os.path.join(REPO, "core", "hooks", "atlas_hook.py"))
        hk_m = importlib.util.module_from_spec(spec_hk)
        spec_hk.loader.exec_module(hk_m)

        # 1. Extreme Diesel FatMax uses unified Mader >=95%-MFO plateau and stays cleanly below LT1
        body_d = {"mass_kg_7d": 64.0, "kfa_pct": 10.0, "kfa_calibrated": True, "ffm_kg": 57.6, "vol_rel_ind": 0.4533, "v_la_l": 29.01}
        p1_d = pe.compute_pillar1_oxidative(345.0, 10500.0, 64.0, 57.6, True)
        p2_d = pe.compute_pillar2_metabolic_mader({"fresh": {"15s": {"90d": 680}}}, 310.0, p1_d["vo2max_rel_clean"], body_d)
        self.assertEqual(p2_d["fatmax_band_w"], p2_d["mader_fatmax_95_band_w"])
        self.assertEqual(p2_d["p_fatmax_w"], p2_d["mader_mfo_peak_w"])
        self.assertLess(p2_d["fatmax_band_w"][1], p2_d["p_lt1_w"], p2_d)
        self.assertLessEqual(p2_d["p_fatmax_w"] / 310.0, 0.75, p2_d)

        # 2. Diesel safety floor in simulate_workout_stream enforces 3.0 kJ floor for small W' (10.5 kJ)
        diesel_push = [180.0] * 300 + ([342.0] * 30 + [217.0] * 15) * 13 + [155.0] * 240 + ([342.0] * 30 + [217.0] * 15) * 13
        sim_d = pe.simulate_workout_stream(diesel_push, 310.0, 10500.0, 345.0, 64.0, title="VO2max Float 30/15")
        if sim_d["w_bal_min_kj"] < 3.0:
            self.assertEqual(sim_d["stimulus_class"], "UNSUSTAINABLE_BLOWOUT")
            self.assertIn("BLOWOUT WARNING", sim_d["stimulus_mismatch"])

        # 3. creatine_flag=True calibrates Meixner t_alac (+0.35s) and Chorley fast PCr pool (0.43 vs 0.40)
        body_j = {"mass_kg_7d": 75.0, "kfa_pct": 11.5, "kfa_calibrated": True, "ffm_kg": 66.38, "vol_rel_ind": 0.4458, "v_la_l": 33.43}
        p2_no_cr = pe.compute_pillar2_metabolic_mader({"fresh": {"15s": {"90d": 1094}}}, 294.0, 62.3, body_j, t_alac_s=3.0, creatine_flag=False)
        p2_cr = pe.compute_pillar2_metabolic_mader({"fresh": {"15s": {"90d": 1094}}}, 294.0, 62.3, body_j, t_alac_s=3.0, creatine_flag=True)
        self.assertAlmostEqual(p2_cr["t_alac_s"], p2_no_cr["t_alac_s"] + 0.35, places=2)
        test_burst = [180.0] * 300 + [550.0] * 30 + [150.0] * 120
        s_no_cr = pe.simulate_workout_stream(test_burst, 294.0, 31400.0, 418.0, 75.0, creatine_flag=False)
        s_cr = pe.simulate_workout_stream(test_burst, 294.0, 31400.0, 418.0, 75.0, creatine_flag=True)
        self.assertGreater(s_cr["fc_pcr_min_pct"], s_no_cr["fc_pcr_min_pct"])

        # 4. Nested-set DSL parsing (`3x13x ...` and `3x (13x ...) / 4m@150W`) + unparsed_tokens detection
        sp1 = hk_m.parse_workout_spec("15m WU 55-68%, 3x13x 30s@394W / 15s@206W, 10m CD 62-48%", subkind="cycling", ftp_watts=294)
        self.assertEqual(sp1["unparsed_tokens"], [])
        self.assertEqual(sum(1 for s in sp1["segments"] if s["type"] == "intervals"), 3)
        sp2 = hk_m.parse_workout_spec("Float 30/15 (75m | 10m WU 55-68%, 3x (13x 30s@394W / 15s@206W) / 4m@150W, 10m CD 62-48%)", subkind="cycling", ftp_watts=294)
        self.assertEqual(sp2["unparsed_tokens"], [])
        self.assertEqual(sum(1 for s in sp2["segments"] if s["type"] == "intervals"), 3)
        sp_bad = hk_m.parse_workout_spec("10m WU 55-68%, gibberish_token, 10m CD 62-48%", subkind="cycling", ftp_watts=294)
        self.assertEqual(sp_bad["unparsed_tokens"], ["gibberish_token"])

        # 5. Sub-Threshold `4x 10m@278W / 4m@160W` classified as THRESHOLD_OR_SHUTTLE_OPENER (not AEROBIC_BASE_Z2!)
        sp_mlss = hk_m.parse_workout_spec("15m WU 52-68%, 4x 10m@278W / 4m@160W, 9m CD 62-48%", subkind="cycling", ftp_watts=294)
        w_mlss, wb_mlss = pe.expand_spec_to_1hz_watts(sp_mlss, cp_watts=294.0)
        sim_mlss = pe.simulate_workout_stream(w_mlss, 294.0, 31400.0, 418.0, 75.0, work_block_watts=wb_mlss)
        self.assertEqual(sim_mlss["stimulus_class"], "THRESHOLD_OR_SHUTTLE_OPENER", sim_mlss)
        self.assertGreaterEqual(sim_mlss["work_block_avg_w"], 275.0)

        # 6. CLI `--solve` with `--preload-kj`, batch `--cho-g-h 42.5,90`, `--form-pct`, plus `set-profile`, `add-rule` & `today:` header
        self.atlas("coach", "context", "--init", "--execute")
        self.page("coach/profile.md", (
            "---\nsummary: Profile\ntags: [sport]\ndepth: pro\nsex: male\nage: 25\n---\n"
            "# Athlete Profile\n\nStand: 2026-11-18.\n\n| Metric | Value | Notes |\n|---|---|---|\n"
            "| **Biometrie** | 186 cm · 75,0 kg · ~11,5 % KFA | Test |\n"
            "| **CP / W'** | 294 W / 31.4 kJ | Anchor |\n"
            "| **Max HR / Rest HR** | ~200 bpm / — bpm | Test |\n"
        ))
        self.atlas("coach", "set-profile", "--rhr", "45", "--creatine", "yes", "--execute")
        prof_txt = self.read(os.path.join(self.brain, "coach", "profile.md"))
        self.assertIn("creatine: true", prof_txt)
        self.assertIn("45 bpm", prof_txt)

        self.atlas("coach", "add-rule", "--title", "Gut-Test", "--text", "90-100 g KH/h stabil", "--execute")
        pb_txt = self.read(os.path.join(self.brain, "coach", "playbook.md"))
        self.assertIn("Gut-Test", pb_txt)
        self.assertIn("90-100 g KH/h stabil", pb_txt)

        ctx_out = self.atlas("coach", "context", "--mode", "daily", "--depth", "pro", "--no-sync").stdout
        self.assertIn("today:", ctx_out)
        self.assertIn("KW ", ctx_out)
        self.assertIn("creatine=True", ctx_out)

        solve_out = self.atlas(
            "coach", "simulate", "--solve", "float-30-15",
            "--preload-kj", "1782", "--cho-g-h", "42.5,90", "--form-pct", "0,-2",
        ).stdout
        self.assertIn("1-HZ INVERSE PRESCRIPTION SOLVER", solve_out)
        self.assertIn("Fresh Baseline", solve_out)
        self.assertIn("Preload 1782kJ @ 42.5g/h CHO", solve_out)
        self.assertIn("Preload 1782kJ @ 90g/h CHO", solve_out)
        self.assertIn("Form -2%", solve_out)

        # Nested-set CLI simulate works without dropping into RECOVERY_Z1
        sim_ns = self.atlas("coach", "simulate", "--dsl", "15m WU 55-68%, 3x13x 30s@394W / 15s@206W, 10m CD 62-48%").stdout
        self.assertIn("TRUE_VO2MAX_SEVERE", sim_ns)
        self.assertNotIn("UNPARSED DSL", sim_ns)

        # 7. v1.4.8: Depth-adaptive Unit 1 (pro vs standard), Fast-Myotype 2a/2b split & `--fit-tau` / `--tau-w-mult`
        ft_pro = self.atlas("coach", "field-test", "--unit", "1", "--depth", "pro", "--date", "2026-12-01").stdout
        self.assertIn("3m/2m/3m W'-Reconstitution", ft_pro)
        ft_std = self.atlas("coach", "field-test", "--unit", "1", "--depth", "standard", "--date", "2026-12-01").stdout
        self.assertIn("15s Sprint & 3m All-Out Anchor", ft_std)
        self.assertNotIn("3m/2m/3m", ft_std)
        ft_2a = self.atlas("coach", "field-test", "--unit", "2a", "--date", "2026-12-03").stdout
        self.assertIn("Fresh 12m All-Out CP Anchor", ft_2a)
        ft_2b = self.atlas("coach", "field-test", "--unit", "2b", "--date", "2026-12-06").stdout
        self.assertIn("Fresh 5m All-Out VO2max Roof", ft_2b)
        ft_2_warn = self.atlas("coach", "field-test", "--unit", "2", "--date", "2026-12-03").stdout
        self.assertIn("Fast-Myotype Advisory", ft_2_warn)

        fit_out = self.atlas("coach", "simulate", "--fit-tau", "3m=468,2m=147,3m=385").stdout
        self.assertIn("INDIVIDUAL W'-RECONSTITUTION CALIBRATION", fit_out)
        self.assertIn("tau_w_mult=", fit_out)
        self.atlas("coach", "set-profile", "--tau-w-mult", "1.18", "--execute")
        prof_tau = self.read(os.path.join(self.brain, "coach", "profile.md"))
        self.assertIn("tau_w_mult: 1.18", prof_tau)
        self.assertIn("tau_w_date:", prof_tau)
        ctx_tau = self.atlas("coach", "context", "--mode", "daily", "--depth", "pro", "--no-sync").stdout
        self.assertIn("tau_mult=1.18x, profile", ctx_tau)

    def test_coach_v150_google_health_temporal_alignment_and_multiyear(self):
        """v1.5.0: Google Health API v4 sync, 2-layer biometrics/intraday store, Strava timestamps, temporal inspect & multi-year search."""
        import datetime as dt
        from test_coach_health import _synthetic_gh_bundle

        self.atlas("coach", "--init", "--execute")
        today = dt.date.today()
        d1 = (today - dt.timedelta(days=1)).isoformat()
        d0 = today.isoformat()

        # 1. Import Desktop OAuth JSON & check auth-google-health CLI
        c_json = os.path.join(self.tmp, "client_secret_demo.json")
        with open(c_json, "w", encoding="utf-8") as fh:
            json.dump({"installed": {"client_id": "demo-cid.apps.googleusercontent.com", "client_secret": "demo-client-secret"}}, fh)
        auth_out = self.atlas("coach", "auth-google-health", "--client-json", c_json, "--login-hint", "athlete@example.com").stdout
        self.assertIn("imported OAuth client credentials", auth_out)
        self.assertIn("Google Health verbinden / erneuern (1-Klick)", auth_out)

        # 2. Sync Strava multi-year fixture (with full ISO timestamps!) + Google Health v4 bundle fixture
        strava_mock = os.path.join(self.tmp, "strava_v150.json")
        with open(strava_mock, "w", encoding="utf-8") as fh:
            json.dump([
                {
                    "id": 99001,
                    "name": "Evening Tempo Ride",
                    "sport_type": "Ride",
                    "start_date": f"{d1}T16:30:00Z",
                    "start_date_local": f"{d1}T17:30:00Z",
                    "utc_offset": 3600.0,
                    "timezone": "(GMT+01:00) Europe/Berlin",
                    "moving_time": 5400,
                    "elapsed_time": 5400,
                    "distance": 45000.0,
                    "total_elevation_gain": 420.0,
                    "device_watts": True,
                    "average_watts": 255,
                    "weighted_average_watts": 272,
                    "kilojoules": 1377,
                    "average_heartrate": 156,
                    "max_heartrate": 184,
                },
                {
                    "id": 88001,
                    "name": "Alpine Classic 2024",
                    "sport_type": "Ride",
                    "start_date": "2024-07-14T06:00:00Z",
                    "start_date_local": "2024-07-14T08:00:00Z",
                    "utc_offset": 7200.0,
                    "moving_time": 10800,
                    "elapsed_time": 11100,
                    "distance": 112000.0,
                    "total_elevation_gain": 1650.0,
                    "device_watts": True,
                    "average_watts": 268,
                    "weighted_average_watts": 292,
                    "kilojoules": 2894,
                    "average_heartrate": 162,
                    "max_heartrate": 191,
                },
            ], fh)

        gh_mock = os.path.join(self.tmp, "gh_v150.json")
        with open(gh_mock, "w", encoding="utf-8") as fh:
            json.dump(_synthetic_gh_bundle(today), fh)

        sync_out = self.atlas(
            "coach", "sync", "--mock-file", strava_mock, "--google-health-mock", gh_mock, "--days", "1095", "--execute"
        ).stdout
        self.assertIn("wellness day(s) imported (google_health)", sync_out)
        self.assertIn("intraday cached", sync_out)

        # Verify persisted timestamps in activities.json and canonical biometrics.json + intraday cache
        acts_saved = json.loads(self.read(os.path.join(self.brain, "coach", "activities.json")))
        a_recent = next(a for a in acts_saved if str(a["id"]) == "99001")
        self.assertEqual(a_recent["start_time_utc"], f"{d1}T16:30:00Z")
        self.assertEqual(a_recent["start_time_local"], f"{d1}T17:30:00")
        self.assertEqual(a_recent["end_time_utc"], f"{d1}T18:00:00Z")

        bio_saved = json.loads(self.read(os.path.join(self.brain, "coach", "biometrics.json")))
        self.assertIn(d0, bio_saved["days"])
        self.assertIn(d1, bio_saved["days"])
        self.assertEqual(bio_saved["days"][d0]["autonomic"]["rhr_bpm"], 49)
        self.assertTrue(os.path.isfile(os.path.join(self.brain, "_atlas", "cache", "health", "intraday", f"{d0}.json")))

        # 3. Chat-First coach context surfaces 24/7 wearable summary & coach inspect renders full Temporal Timeline
        ctx_out = self.atlas("coach", "context", "--mode", "daily", "--depth", "pro", "--no-sync").stdout
        self.assertIn("[7B_wearable", ctx_out)
        self.assertIn("HRV 78.4ms", ctx_out)
        self.assertIn("Temp -0.08°C", ctx_out)

        insp_out = self.atlas("coach", "inspect", "--date", d1, "--no-stream-fetch").stdout
        self.assertIn("TEMPORAL BIOMETRICS & WORKOUT TIMELINE", insp_out)
        self.assertIn("pre-NEAT 10800 steps", insp_out)
        self.assertIn("finish->bed 6.0h", insp_out)
        self.assertIn(f"Following Night Recovery ({d0})", insp_out)

        # 4. Multi-year history --search & --year + epoch compare with biometrics
        h_out = self.atlas("coach", "history", "--search", "Alpine Classic").stdout
        self.assertIn("Alpine Classic 2024", h_out)
        h_yr = self.atlas("coach", "history", "--year", "2024").stdout
        self.assertIn("Alpine Classic 2024", h_yr)
        cmp_out = self.atlas(
            "coach", "compare", "--epoch-a", "2024-07-01..2024-07-31", "--epoch-b", f"{d1}..{d0}"
        ).stdout
        self.assertIn("[Biometrics B", cmp_out)


if __name__ == "__main__":
    os.makedirs(BASE_DIR, exist_ok=True)
    prog = unittest.main(argv=[sys.argv[0], "-v", *sys.argv[1:]], exit=False)
    if not os.listdir(BASE_DIR):
        os.rmdir(BASE_DIR)
    sys.exit(0 if prog.result.wasSuccessful() else 1)
