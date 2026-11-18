"""Atlas command-line tool. Python 3 standard library only.

Layout: ROOT (the folder you open in Claude Code or Antigravity, e.g. C:/Atlas)
          brain/   the knowledge base, a Git repository (this tool lives in brain/_atlas/tools/)
          code/    code repositories Atlas manages (each its own Git repository; optional)
          CLAUDE.md, GEMINI.md, .claude/, .agents/   wiring written by `wire`

Run with:  python -I <ROOT>/brain/_atlas/tools/atlas.py [--root ROOT] <command> [options]
Every command that changes something is a dry run unless `--execute` is given.

Commands
  init        create ROOT/brain from a life-atlas checkout (skeleton, core, modules, Git) and ROOT/code
  wire        connect an agent (claude | antigravity), scope project (default: ROOT) or user (~); `unwire` reverses
  routes      (re)write the per-project path rules for projects inside ROOT (part of `wire`)
  projects    show the project registry the router uses
  index       build the SQLite search & graph cache in full (`_atlas/cache/index.sqlite`)
  find        ranked search: tags, aliases, title, summary, FTS5 + trigram full text, plus 1-hop linked leads
  where       canonical placement & grouping guide before saving (`existing pages` + `areas/` folders + hub READMEs)
  graph       inspect a page's outlinks, backlinks (with context & #anchor), and unlinked mentions
  context     assemble a token-budgeted Markdown context dossier (target page + top specificity-ranked neighbors)
  link        retroactive wiki-linking (`--scan`, `--journals`, `--related`, `--inline`, `--execute`)
  conflicts   manage unresolved fact contradictions in `_atlas/conflicts.json` (`list`, `add`, `resolve`)
  mark        record `maintenance` or `update-check` as a commit (seen by every device)
  adopt       move rules/skills/text written outside Atlas (e.g. Antigravity /learn) into the brain
  code-sync   clone project repositories missing on this device (`path:` + `remote:` on the project page)
  probe       run the registered hooks with sample input and check their output
  check       verify the charter: sizes, links, reachability, duplicates, secrets, Git, backup, hooks, registry
  deadlines   list `due:` items; `--ics FILE` writes a calendar file
  todo        quick task triage (`list`, `done`, `skip`, `reopen`, `move --to <date|+Nd>`, `add`)
  ext         inspect update-safe user extensions (`skills/`, `howto/scripts/`, `_secrets/`); `run <cmd>` executes one
  mail        multi-account IMAP mail engine (`accounts`, `folders`, `list`, `read`, `search`, `audit`, plus
              safety-gated `move` to `target_folder`, `copy`, `trash`, `delete`, `flags`, `labels`, `mkdir`)
  inventory   read-only map of the computer (names, sizes, types, markers; never file contents or credential stores)
  move        execute a move/copy plan with checksums, journal and undo; `verify` and `undo` work on the journal
  core-diff   three-way comparison of this core with a life-atlas checkout (what updates, what stays, conflicts)
  core-apply  apply it: merge, router core block, upgrades, re-wire, probe, automatic rollback; `core-accept` after a
              manual merge
  dev-check   do I have push rights to the life-atlas repository?  `dev-setup` sets up Atlas Dev (maintainers only)
  state       read or set values in brain/_atlas/state.json
"""
import argparse
import ctypes
import datetime as dt
import hashlib
import importlib.util
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time

sys.dont_write_bytecode = True

DEFAULT_UPSTREAM = "https://github.com/JonasS1202/life-atlas.git"
TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)
from atlas_lib import cal_engine, coach_connectors, coach_engine, fit_writer, graph_engine

INSTALLED_BRAIN = os.path.normpath(os.path.join(TOOLS_DIR, "..", ".."))
HOOK_FILE = os.path.normpath(os.path.join(TOOLS_DIR, "..", "hooks", "atlas_hook.py"))
CORE_MARK = ("<!-- atlas:start -->", "<!-- atlas:end -->")
ROUTER_MARK = ("<!-- atlas:router:start -->", "<!-- atlas:router:end -->")
HOOK_REL = "_atlas/hooks/atlas_hook.py"
RULE_PREFIX = "atlas-"
LIMITS = {"router_warn": 6000, "router_max": 10000, "index_warn": 8000, "memory_lines": 60, "skills_desc": 2400}
CLOUD = re.compile(r"(?i)(^|[\\/])(onedrive[^\\/]*|dropbox|icloud ?drive|iclouddrive|google ?drive|my drive|"
                   r"mobile documents|nextcloud|pcloud ?drive|box sync)([\\/]|$)")
SECRET_RX = re.compile(r"(?i)(?:api[_-]?key|secret|token|passwor[dt]|passwd|pin)\s*[:=]\s*[\"']?[^\s\"'`<>]{8,}|"
                       r"\bsk-[A-Za-z0-9_-]{20,}|\bghp_[A-Za-z0-9]{30,}|\bAKIA[0-9A-Z]{16}\b|"
                       r"-----BEGIN [A-Z ]*PRIVATE KEY-----")


# ================================================================= helpers

def now_iso():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def stamp():
    return dt.datetime.now().strftime("%Y-%m-%d_%H%M%S")


def fwd(p):
    return os.path.normpath(p).replace("\\", "/")


def say(msg=""):
    sys.stdout.write(msg + "\n")


def read(path, limit=None):
    with open(path, encoding="utf-8", errors="replace") as fh:
        return fh.read(limit) if limit else fh.read()


def write(path, text):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8-sig") as fh:
        text = fh.read()
    return json.loads(text) if text.strip() else default


def dump_json(path, data):
    write(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def sha256_file(path, bufsize=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(bufsize)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def manifest(path):
    """{relative path: sha256} for a file or a directory tree."""
    if os.path.isfile(path):
        return {".": sha256_file(path)}
    out = {}
    for dp, _dns, fns in os.walk(path):
        for fn in fns:
            full = os.path.join(dp, fn)
            out[fwd(os.path.relpath(full, path))] = sha256_file(full)
    return out


def paths(args):
    """(root, brain) from --root, $ATLAS_ROOT or the location of this tool."""
    root = getattr(args, "root", None) or os.environ.get("ATLAS_ROOT")
    if root:
        root = os.path.normpath(os.path.abspath(os.path.expanduser(root)))
        return root, os.path.join(root, "brain")
    brain = INSTALLED_BRAIN
    st = load_json(os.path.join(brain, "_atlas", "state.json"), {}) if os.path.isdir(brain) else {}
    return os.path.normpath(st.get("root") or os.path.dirname(brain)), brain


def state_path(brain):
    return os.path.join(brain, "_atlas", "state.json")


def get_state(brain):
    try:
        return load_json(state_path(brain), {})
    except ValueError:
        return {}


def set_state(brain, **kv):
    st = get_state(brain)
    st.update(kv)
    dump_json(state_path(brain), st)


def run_id(brain, suffix):
    """Unique folder name under _atlas/migrations (two runs in the same second must not share a journal)."""
    base = os.path.join(brain, "_atlas", "migrations")
    suffix = re.sub(r"[^\w-]+", "-", suffix).strip("-")[:40] or "run"
    rid, n = f"{stamp()}_{suffix}", 1
    while os.path.exists(os.path.join(base, rid)):
        n += 1
        rid = f"{stamp()}_{suffix}-{n}"
    return rid


def render(text, values):
    for k, v in values.items():
        text = text.replace("{{" + k + "}}", v)
    return text


def replace_block(text, start, end, block):
    """Replace the managed block between markers, or append it. Returns the new text."""
    if start in text and end in text and text.index(start) < text.index(end):
        return text[: text.index(start)] + block + text[text.index(end) + len(end):]
    sep = "" if not text or text.endswith("\n\n") else ("\n" if text.endswith("\n") else "\n\n")
    return text + sep + block + "\n"


def remove_block(text, start, end):
    if start in text and end in text:
        new = text[: text.index(start)] + text[text.index(end) + len(end):]
        return re.sub(r"\n{3,}", "\n\n", new).strip() + "\n" if new.strip() else ""
    return text


def short_path(path):
    """Windows: 8.3 path without spaces, so hook commands work in cmd, PowerShell and bash alike."""
    if os.name != "nt" or " " not in path:
        return path
    try:
        buf = ctypes.create_unicode_buffer(1024)
        if ctypes.windll.kernel32.GetShortPathNameW(path, buf, 1024):
            return buf.value
    except Exception:
        pass
    return path


def cmd_quote(path):
    p = fwd(short_path(path))
    return f'"{p}"' if " " in p else p


def backup(brain, path, tag):
    if not os.path.exists(path):
        return None
    dst_dir = os.path.join(brain, "_atlas", "migrations", tag, "wiring-backup")
    os.makedirs(dst_dir, exist_ok=True)
    src = os.path.abspath(path)
    name = hashlib.sha1(src.encode("utf-8")).hexdigest()[:10] + "_" + os.path.basename(src)
    dst = os.path.join(dst_dir, name)
    with open(os.path.join(dst_dir, "index.txt"), "a", encoding="utf-8", newline="\n") as fh:
        fh.write(f"{name}\t{fwd(src)}\n")
    if os.path.isdir(path):
        shutil.copytree(path, dst, dirs_exist_ok=True)
    else:
        shutil.copy2(path, dst)
    return dst


def _git(repo, *a, timeout=60):
    return subprocess.run(["git", "-C", repo, *a], capture_output=True, text=True, timeout=timeout,
                          encoding="utf-8", errors="replace")


def hook_module(brain):
    """The installed hook as a module, pointed at `brain` (shares the registry and cache logic)."""
    path = os.path.join(brain, HOOK_REL) if os.path.isfile(os.path.join(brain, HOOK_REL)) else HOOK_FILE
    spec = importlib.util.spec_from_file_location("atlas_hook", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.BRAIN = brain
    mod.STATE = state_path(brain)
    mod.CACHE = os.path.join(brain, "_atlas", "cache", "index.sqlite")
    mod.CAL_HTML = os.path.join(brain, "_atlas", "cache", "calendar.html")
    mod.ERR_LOG = os.path.join(brain, "_atlas", "cache", "hook_errors.log")
    mod.CONFLICTS_FILE = os.path.join(brain, "_atlas", "conflicts.json")
    return mod


def registry(brain):
    hk = hook_module(brain)
    return hk.projects(hk.load_index())


def inside(path, root):
    p, r = fwd(os.path.abspath(path)).lower(), fwd(os.path.abspath(root)).lower().rstrip("/")
    return p == r or p.startswith(r + "/")


# ================================================================= init

def cmd_init(args):
    src = os.path.abspath(args.source)
    root, brain = paths(args)
    for need in ("core", "skeleton"):
        if not os.path.isdir(os.path.join(src, need)):
            sys.exit(f"--source {src} is not a life-atlas checkout (missing {need}/)")
    if CLOUD.search(root):
        sys.exit(f"Refusing: {root} is inside a cloud-sync folder (charter §6). Choose a local folder.")
    if os.path.exists(brain) and os.listdir(brain):
        sys.exit(f"Refusing: {brain} exists and is not empty. For an existing Atlas use core-apply.")
    if os.path.isdir(os.path.join(root, ".git")):
        sys.exit(f"Refusing: {root} is itself a Git repository. ROOT must be a plain folder; the brain is the repo.")
    modules = [m for m in (args.modules or "").split(",") if m]
    for m in modules:
        if not os.path.isdir(os.path.join(src, "modules", m)):
            sys.exit(f"Unknown module: {m}")
    values = {"ROOT": fwd(root), "USER_NAME": args.name or "the user", "LANGUAGE": args.language or "English",
              "DATE": dt.date.today().isoformat()}
    plan = _copy_plan(os.path.join(src, "skeleton"), brain) + _copy_plan(os.path.join(src, "core"),
                                                                         os.path.join(brain, "_atlas"))
    for m in modules:
        mdir = os.path.join(src, "modules", m)
        if os.path.isdir(os.path.join(mdir, "core")):
            plan += _copy_plan(os.path.join(mdir, "core"), os.path.join(brain, "_atlas"))
        if os.path.isdir(os.path.join(mdir, "home")):
            plan += _copy_plan(os.path.join(mdir, "home"), brain)
    say(f"init: ROOT {root}, brain with {len(plan)} files, modules: {', '.join(modules) or 'none'}")
    if not args.execute:
        say("Dry run. Add --execute to create it.")
        return
    core_dir = os.path.join(brain, "_atlas") + os.sep
    for s, d in plan:
        os.makedirs(os.path.dirname(d), exist_ok=True)
        # Skeleton and module home files get placeholders filled; the core is copied verbatim so that core-diff can
        # compare it byte for byte with a newer life-atlas checkout.
        if not d.startswith(core_dir) and d.endswith((".md", ".gitignore", ".ignore")):
            write(d, render(read(s), values))
        else:
            shutil.copy2(s, d)
    write(os.path.join(brain, "ROUTER.md"), render(read(os.path.join(src, "core", "templates", "router.md")), values))
    for d in ("inbox", "journal", "people", "areas", "projects", "howto", "self", "calendar", "coach",
              os.path.join("coach", "workouts"), "skills", "_secrets",
              "_archive", os.path.join("_atlas", "memory"), os.path.join("_atlas", "migrations")):
        os.makedirs(os.path.join(brain, d), exist_ok=True)
    _ensure_coach_index_link(brain, execute=True)
    os.makedirs(os.path.join(root, "code"), exist_ok=True)
    mem = os.path.join(brain, "_atlas", "memory", "MEMORY.md")
    if not os.path.exists(mem):
        write(mem, "# Atlas memory (notepad)\n\nShort notes only. Lessons move to `howto/` at the next /atlas-check; "
                   f"keep this file under {LIMITS['memory_lines']} lines (it loads into every Claude chat).\n")
    set_state(brain, installed=now_iso(), version=read(os.path.join(brain, "_atlas", "VERSION")).strip(),
              language=values["LANGUAGE"], modules=modules, source=fwd(src), root=fwd(root),
              user_name=args.name or "the user")
    _write_base(brain, src, modules, root)
    if not os.path.isdir(os.path.join(brain, ".git")):
        _git(brain, "init", "-q")
        _git(brain, "symbolic-ref", "HEAD", "refs/heads/main")
    if not _git(brain, "config", "user.email").stdout.strip():
        _git(brain, "config", "user.name", "Atlas")
        _git(brain, "config", "user.email", "atlas@localhost")
    _git(brain, "add", "-A")
    _git(brain, "commit", "-q", "-m", "atlas: initial structure")
    say(f"Created {brain} (Git) and {os.path.join(root, 'code')}. Next: wire.")


def _copy_plan(base, target):
    out = []
    for dp, dns, fns in os.walk(base):
        dns[:] = [d for d in dns if d != "__pycache__"]
        for fn in fns:
            if fn.endswith((".pyc", ".pyo")):
                continue
            s = os.path.join(dp, fn)
            out.append((s, os.path.join(target, os.path.relpath(s, base))))
    return out


# ================================================================= wire / unwire / routes

def _hook_cmd(brain, python, agent, event):
    return f"{cmd_quote(python)} -I {cmd_quote(os.path.join(brain, HOOK_REL))} {agent} {event}"


def _skills(brain):
    """[(name, folder)]: core skills (_atlas/skills) and the user's grown skills (brain/skills, e.g. from /atlas-learn)."""
    out = {}
    for sdir in (os.path.join(brain, "_atlas", "skills"), os.path.join(brain, "skills")):
        if os.path.isdir(sdir):
            for d in sorted(os.listdir(sdir)):
                if os.path.isfile(os.path.join(sdir, d, "SKILL.md")):
                    out.setdefault(d, os.path.join(sdir, d))
    return sorted(out.items())


def _home_rel(user_home, path):
    rel = os.path.relpath(path, user_home)
    return "~/" + fwd(rel) if not rel.startswith("..") and not os.path.isabs(rel) else fwd(path)


def _targets(agent, scope, root, user_home):
    """Where the wiring goes. Project scope: ROOT (the folder you open). User scope: the user's agent config."""
    if agent == "claude":
        base = os.path.join(root if scope == "project" else user_home, ".claude")
        return {"router": os.path.join(root, "CLAUDE.md") if scope == "project" else os.path.join(base, "CLAUDE.md"),
                "hooks": os.path.join(base, "settings.json"), "skills": os.path.join(base, "skills"),
                "rules": os.path.join(base, "rules") if scope == "project" else None,
                "agents": os.path.join(base, "agents")}
    slug = re.sub(r"[^a-z0-9-]+", "-", os.path.basename(os.path.abspath(root)).lower()).strip("-") or "atlas"
    pname = f"atlas-subagents-{slug}"
    if scope == "project":
        return {"router": os.path.join(root, "GEMINI.md"), "hooks": os.path.join(root, ".agents", "hooks.json"),
                "skills": os.path.join(root, ".agents", "skills"), "rules": os.path.join(root, ".agents", "rules"),
                "ag_plugin": os.path.join(root, ".agents", "plugins", "atlas-subagents"), "ag_plugin_name": pname}
    g = os.path.join(user_home, ".gemini")
    return {"router": os.path.join(g, "AGENTS.md"), "hooks": os.path.join(g, "config", "hooks.json"),
            "skills": os.path.join(g, "config", "skills"), "rules": None,
            "ag_plugin": os.path.join(g, "config", "plugins", pname), "ag_plugin_name": pname}


def cmd_wire(args, remove=False):
    root, brain = paths(args)
    user_home = os.path.abspath(os.path.expanduser(args.user_home or "~"))
    python = os.path.abspath(args.python or sys.executable)
    key = f"{args.agent}:{args.scope}"
    if not remove and not os.path.isfile(os.path.join(brain, HOOK_REL)):
        sys.exit(f"No Atlas brain at {brain} (missing {HOOK_REL}).")
    t = _targets(args.agent, args.scope, root, user_home)
    changes = (_wire_claude if args.agent == "claude" else _wire_antigravity)(args, root, brain, user_home, python,
                                                                              remove, t)
    changes += _skill_changes(brain, t["skills"], remove)
    if t["rules"]:
        changes += _rule_changes(brain, root, args.agent, t["rules"], remove)
    if t.get("agents"):
        changes += _agent_changes(brain, t["agents"], remove)
    if t.get("ag_plugin"):
        changes += _ag_subagent_changes(brain, t["ag_plugin"], t["ag_plugin_name"], remove)
    say(f"{'unwire' if remove else 'wire'} {key}: {len(changes)} change(s)")
    for path, _new, desc in changes:
        say(f"  {fwd(path)}: {desc}")
    if not args.execute:
        say("Dry run. Add --execute to apply (each changed file is backed up first).")
        return
    tag = run_id(brain, f"{'unwire' if remove else 'wire'}-{args.agent}-{args.scope}")
    _apply(brain, changes, tag)
    st = get_state(brain)
    wired = dict(st.get("wired", {})) if isinstance(st.get("wired"), dict) else {}
    if remove:
        wired.pop(key, None)
        granted = st.get("granted", {})
        granted.pop(key, None)
        set_state(brain, granted=granted)
    else:
        wired[key] = {"hooks": fwd(t["hooks"]), "python": fwd(python), "router_mode": args.router_mode}
        if os.path.isdir(os.path.join(brain, "coach")):
            os.makedirs(os.path.join(brain, "coach", "workouts"), exist_ok=True)
            _ensure_coach_index_link(brain, execute=True)
    extra = {}
    if args.agent == "antigravity":
        extra["antigravity_router_copy"] = fwd(t["router"]) if (not remove and args.router_mode == "copy") else ""
        if not remove:  # global Antigravity rules that existed before Atlas are the user's, not /learn output
            pre = [os.path.normcase(i["path"]) for i in hook_module(brain).foreign_items(root, user_home) if i["global"]]
            extra["gemini_snapshot"] = sorted(set(st.get("gemini_snapshot", [])) | set(pre))
    set_state(brain, wired=wired, **extra)
    say(f"Applied. Backups: _atlas/migrations/{tag}/wiring-backup/")


def _apply(brain, changes, tag):
    for path, _new, _desc in changes:  # all backups first, so a failure leaves nothing half-wired
        backup(brain, path, tag)
    for path, new, _desc in changes:
        if new is None:
            parent = os.path.dirname(path)
            if os.path.isdir(path):
                shutil.rmtree(path)
            elif os.path.exists(path):
                os.remove(path)
            if os.path.basename(parent) == "plugins" and os.path.isdir(parent) and not os.listdir(parent):
                try:
                    os.rmdir(parent)
                except OSError:
                    pass
        elif isinstance(new, bytes):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as fh:
                fh.write(new)
        else:
            write(path, new)


def _router_block(agent, scope, root, brain, user_home, mode):
    router = os.path.join(brain, "ROUTER.md")
    if agent == "claude":
        target = "@" + (fwd(os.path.relpath(router, root)) if scope == "project" else fwd(router))
    elif mode == "copy":
        target = f"{ROUTER_MARK[0]}\n{read(router).strip()}\n{ROUTER_MARK[1]}"
    else:
        target = "@[Atlas router](" + (fwd(os.path.relpath(router, root)) if scope == "project"
                                        else _home_rel(user_home, router)) + ")"
    return (f"{CORE_MARK[0]}\n<!-- Managed by Atlas (atlas.py wire). The router of this Atlas: -->\n{target}\n"
            f"{CORE_MARK[1]}")


def _wire_claude(args, root, brain, user_home, python, remove, t):
    changes = []
    old = read(t["router"]) if os.path.exists(t["router"]) else ""
    block = _router_block("claude", args.scope, root, brain, user_home, args.router_mode)
    new = remove_block(old, *CORE_MARK) if remove else replace_block(old, *CORE_MARK, block)
    if new != old:
        changes.append((t["router"], new if new.strip() else None, "router import"))
    try:
        settings = load_json(t["hooks"], {})
    except ValueError:
        sys.exit(f"{t['hooks']} is not valid JSON. Fix it first; Atlas never overwrites a file it cannot read.")
    before = json.dumps(settings, sort_keys=True)
    hooks = settings.setdefault("hooks", {})
    for event, hook_event, timeout in (("session", "SessionStart", 15), ("prompt", "UserPromptSubmit", 10),
                                       ("stop", "Stop", 30)):
        entries = [e for e in hooks.get(hook_event, []) if "atlas_hook.py" not in json.dumps(e)]
        if not remove:
            entries.append({"hooks": [{"type": "command", "command": _hook_cmd(brain, python, "claude", event),
                                       "timeout": timeout}]})
        if entries:
            hooks[hook_event] = entries
        else:
            hooks.pop(hook_event, None)
    if not hooks:
        settings.pop("hooks", None)
    mem = fwd(os.path.join(brain, "_atlas", "memory"))
    if remove:
        if settings.get("autoMemoryDirectory") == mem:
            settings.pop("autoMemoryDirectory")
    else:
        settings["autoMemoryDirectory"] = mem
    # Permissions: project scope needs nothing for ROOT itself (it is the working folder). External project folders
    # (outside ROOT) become additional directories so Atlas can work in them from the Atlas chat.
    perms = settings.setdefault("permissions", {})
    dirs, allow = perms.get("additionalDirectories", []), perms.get("allow", [])
    wanted_dirs, wanted_rules = [], []
    if args.grant and not remove:
        ext = [p for proj in registry(brain) for p in proj["resolved"] if not inside(p, root)]
        if args.scope == "user":
            ext.append(root)
        for p in ext:
            hp = _home_rel(user_home, os.path.abspath(os.path.expanduser(p)))
            wanted_dirs.append(hp)
            perm = hp if hp.startswith("~/") else "//" + hp.lstrip("/")
            wanted_rules += [f"Edit({perm}/**)", f"Read({perm}/**)"]
    managed = set(get_state(brain).get("granted", {}).get(f"claude:{args.scope}", []))
    dirs = [d for d in dirs if d not in managed] + [d for d in wanted_dirs if d not in dirs]
    allow = [r for r in allow if r not in managed] + [r for r in wanted_rules if r not in allow]
    perms["additionalDirectories"], perms["allow"] = dirs, allow
    for k in ("additionalDirectories", "allow"):
        if not perms[k]:
            perms.pop(k)
    if not perms:
        settings.pop("permissions", None)
    if json.dumps(settings, sort_keys=True) != before:
        changes.append((t["hooks"], (json.dumps(settings, indent=2, ensure_ascii=False) + "\n") if settings else None,
                        "hooks, autoMemoryDirectory" + (", permissions" if (wanted_dirs or managed) else "")))
    if args.execute and not remove:
        granted = get_state(brain).get("granted", {})
        granted[f"claude:{args.scope}"] = sorted(set(wanted_dirs) | set(wanted_rules))
        set_state(brain, granted=granted)
    return changes


def _wire_antigravity(args, root, brain, user_home, python, remove, t):
    changes = []
    old = read(t["router"]) if os.path.exists(t["router"]) else ""
    block = _router_block("antigravity", args.scope, root, brain, user_home, args.router_mode)
    new = remove_block(old, *CORE_MARK) if remove else replace_block(old, *CORE_MARK, block)
    if new != old:
        changes.append((t["router"], new if new.strip() else None, f"router block ({args.router_mode})"))
    try:
        hooks = load_json(t["hooks"], {})
    except ValueError:
        sys.exit(f"{t['hooks']} is not valid JSON. Fix it first; Atlas never overwrites a file it cannot read.")
    before = json.dumps(hooks, sort_keys=True)
    if remove:
        hooks.pop("atlas", None)
    else:
        hooks["atlas"] = {
            "SessionStart": [{"type": "command", "command": _hook_cmd(brain, python, "antigravity", "session"),
                              "timeout": 15}],
            "PreInvocation": [{"type": "command", "command": _hook_cmd(brain, python, "antigravity", "prompt"),
                               "timeout": 15}],
            "Stop": [{"type": "command", "command": _hook_cmd(brain, python, "antigravity", "stop"), "timeout": 30}],
        }
    if json.dumps(hooks, sort_keys=True) != before:
        changes.append((t["hooks"], (json.dumps(hooks, indent=2, ensure_ascii=False) + "\n") if hooks else None,
                        "hook 'atlas'"))
    return changes


def _skill_changes(brain, target_dir, remove):
    out = []
    values = {"BRAIN": fwd(brain)}
    for name, folder in _skills(brain):
        dst = os.path.join(target_dir, name, "SKILL.md")
        if remove:
            if os.path.exists(dst):
                out.append((os.path.join(target_dir, name), None, f"remove skill {name}"))
            continue
        text = render(read(os.path.join(folder, "SKILL.md")), values)
        grown_md = os.path.join(brain, "skills", name, "SKILL.md")
        if os.path.isfile(grown_md) and os.path.normcase(os.path.abspath(os.path.join(folder, "SKILL.md"))) != os.path.normcase(os.path.abspath(grown_md)):
            note = f"\nAlso read `{fwd(grown_md)}` for user-specific accounts, rules, and context mappings.\n"
            if fwd(grown_md) not in text:
                text = text.rstrip("\n") + "\n" + note
        if not os.path.exists(dst) or read(dst) != text:
            out.append((dst, text, f"skill {name}"))
        grown_dir = os.path.join(brain, "skills", name)
        asset_roots = [folder]
        if os.path.isdir(grown_dir) and os.path.normcase(os.path.abspath(grown_dir)) != os.path.normcase(os.path.abspath(folder)):
            asset_roots.append(grown_dir)
        seen_rel = set()
        for aroot in reversed(asset_roots):  # user grown files override core extras if both exist
            for dp, dns, fns in os.walk(aroot):
                dns[:] = sorted(d for d in dns if not d.startswith(".") and d != "__pycache__")
                for fn in sorted(fns):
                    if fn == "SKILL.md" or fn.startswith(".") or fn.endswith((".pyc", ".pyo")):
                        continue
                    src = os.path.join(dp, fn)
                    rel_sub = fwd(os.path.relpath(src, aroot))
                    if rel_sub in seen_rel:
                        continue
                    seen_rel.add(rel_sub)
                    d2 = os.path.join(target_dir, name, *rel_sub.split("/"))
                    if not os.path.exists(d2) or sha256_file(d2) != sha256_file(src):
                        with open(src, "rb") as fh:
                            raw_bytes = fh.read()
                        out.append((d2, raw_bytes, f"skill file {name}/{rel_sub}"))
    return out


def _agent_changes(brain, target_dir, remove):
    """Claude Code subagents shipped with the core (atlas-search, atlas-maintainer)."""
    out, keep = [], set()
    src_dir = os.path.join(brain, "_atlas", "agents")
    if not remove and os.path.isdir(src_dir):
        for fn in sorted(os.listdir(src_dir)):
            if fn.startswith(RULE_PREFIX) and fn.endswith(".md"):
                keep.add(fn)
                text = render(read(os.path.join(src_dir, fn)), {"BRAIN": fwd(brain)})
                dst = os.path.join(target_dir, fn)
                if not os.path.exists(dst) or read(dst) != text:
                    out.append((dst, text, f"subagent {fn}"))
    if os.path.isdir(target_dir):
        for fn in os.listdir(target_dir):
            if fn.startswith(RULE_PREFIX) and fn.endswith(".md") and fn not in keep:
                out.append((os.path.join(target_dir, fn), None, f"remove subagent {fn}"))
    return out


def _ag_subagent_changes(brain, plugin_dir, plugin_name, remove):
    """Antigravity local plugin bundle (`plugin.json` + `agents/*.md`) exposing `atlas-search` & `atlas-maintainer`."""
    if remove:
        if os.path.exists(plugin_dir):
            return [(plugin_dir, None, f"remove Antigravity plugin {os.path.basename(plugin_dir)}")]
        return []
    src_dir = os.path.join(brain, "_atlas", "agents")
    if not os.path.isdir(src_dir):
        return []
    out, keep = [], set()
    manifest_path = os.path.join(plugin_dir, "plugin.json")
    manifest_text = json.dumps(
        {"name": plugin_name, "description": "Atlas search and maintenance subagents."},
        indent=2,
        ensure_ascii=False,
    ) + "\n"
    if not os.path.exists(manifest_path) or read(manifest_path) != manifest_text:
        out.append((manifest_path, manifest_text, "Antigravity subagent plugin manifest"))
    agents_dir = os.path.join(plugin_dir, "agents")
    for fn in sorted(os.listdir(src_dir)):
        if not (fn.startswith(RULE_PREFIX) and fn.endswith(".md")):
            continue
        keep.add(fn)
        rendered = render(read(os.path.join(src_dir, fn)), {"BRAIN": fwd(brain)})
        fm = graph_engine.parse_frontmatter(rendered)
        body = rendered[graph_engine.frontmatter_end_offset(rendered):].lstrip("\n")
        name = graph_engine.as_str(fm.get("name")) or os.path.splitext(fn)[0]
        desc = graph_engine.as_str(fm.get("description")) or f"Atlas subagent {name}"
        raw_tools = str(fm.get("tools") or "")
        if "Edit" in raw_tools or "Write" in raw_tools or "maintainer" in name:
            ag_tools = "[read_file, edit_file, write_file, run_command, manage_task]"
        else:
            ag_tools = "[read_file, run_command, manage_task]"
        ag_md = (
            "---\n"
            f"name: {name}\n"
            f"description: {json.dumps(desc, ensure_ascii=False)}\n"
            "subagent: true\n"
            "mainAgent: false\n"
            f"tools: {ag_tools}\n"
            "---\n\n"
            + body.rstrip("\n") + "\n"
        )
        dst = os.path.join(agents_dir, fn)
        if not os.path.exists(dst) or read(dst) != ag_md:
            out.append((dst, ag_md, f"Antigravity subagent {fn}"))
    if os.path.isdir(agents_dir):
        for fn in os.listdir(agents_dir):
            if fn.startswith(RULE_PREFIX) and fn.endswith(".md") and fn not in keep:
                out.append((os.path.join(agents_dir, fn), None, f"remove Antigravity subagent {fn}"))
    return out


def _rule_text(agent, proj, rel_path, root, brain):
    page = fwd(os.path.relpath(os.path.join(brain, proj["page"]), root))
    body = (f"<!-- Generated by Atlas (atlas.py routes). Edit {page} instead; this file is rewritten. -->\n"
            f"Project **{proj['title']}**: before working in `{rel_path}/` read `{page}` (never-miss rules, how to "
            "build and test) and the repository's own agent files. Then work there like a normal coding agent; "
            "file lasting insights on that page at the end of a work unit.\n")
    if agent == "claude":
        return f'---\npaths:\n  - "{rel_path}/**"\n---\n' + body
    return f"---\ntrigger: glob\nglobs: {rel_path}/**\n---\n" + body


def _rule_changes(brain, root, agent, rules_dir, remove):
    """Per-project path rules for projects inside ROOT: loaded by the agent only when it touches that folder."""
    out, keep = [], set()
    if not remove:
        for proj in registry(brain):
            for full in proj["resolved"]:
                if not inside(full, root) or inside(full, brain):
                    continue
                rel = fwd(os.path.relpath(full, root))
                name = f"{RULE_PREFIX}{re.sub(r'[^a-z0-9-]+', '-', proj['slug'].lower()).strip('-')}.md"
                keep.add(name)
                text = _rule_text(agent, proj, rel, root, brain)
                dst = os.path.join(rules_dir, name)
                if not os.path.exists(dst) or read(dst) != text:
                    out.append((dst, text, f"project rule {name}"))
    if os.path.isdir(rules_dir):
        for fn in os.listdir(rules_dir):
            if fn.startswith(RULE_PREFIX) and fn.endswith(".md") and fn not in keep:
                out.append((os.path.join(rules_dir, fn), None, f"remove stale rule {fn}"))
    return out


def cmd_routes(args):
    root, brain = paths(args)
    st = get_state(brain)
    wired = st.get("wired", {}) if isinstance(st.get("wired"), dict) else {}
    changes = []
    for key in wired:
        agent, scope = key.split(":")
        t = _targets(agent, scope, root, os.path.expanduser("~"))
        if t["rules"]:
            changes += _rule_changes(brain, root, agent, t["rules"], False)
    say(f"routes: {len(changes)} change(s)")
    for path, _n, desc in changes:
        say(f"  {fwd(path)}: {desc}")
    if args.execute and changes:
        _apply(brain, changes, run_id(brain, "routes"))
        say("Applied.")
    elif changes:
        say("Dry run. Add --execute to apply.")


def cmd_index(args):
    _root, brain = paths(args)
    t0 = time.monotonic()
    hk = hook_module(brain)
    idx = hk.load_index()
    say(f"index: {len(idx)} pages, {len(hk.projects(idx))} projects, {time.monotonic() - t0:.1f} s "
        f"({fwd(hk.CACHE)})")


def cmd_projects(args):
    root, brain = paths(args)
    reg = registry(brain)
    say(f"{len(reg)} project(s) in {fwd(os.path.join(brain, 'projects'))}")
    for p in sorted(reg, key=lambda x: x["slug"]):
        where = ", ".join(("[in ROOT] " if inside(full, root) else "") + raw for raw, full in zip(p["paths"], p["resolved"]))
        say(f"- {p['slug']}: {p['title']} | names: {', '.join(p['names'])} | {where or 'no folder'} | {p['page']}"
            + (f" | {p['summary']}" if p.get("summary") else ""))


def cmd_find(args):
    """Ranked search over SQLite FTS5 + trigram cache (`tags`, `aliases`, `title`, `summary`, `norm_body`) + 1-hop leads."""
    _root, brain = paths(args)
    hk = hook_module(brain)
    idx = hk.load_index()
    vocab = hk.tag_vocab()
    ranked, neighbors = graph_engine.search_pages(
        brain, args.terms, idx=idx, tag_vocab_map=vocab, limit=args.limit,
        text_search=not args.no_text, db_path=hk.CACHE,
    )
    for rel in ranked:
        e = idx.get(rel, {})
        tags = ",".join(e.get("tags", []))
        say(f"{rel} — {e.get('summary') or e.get('title') or ''}" + (f" [{tags}]" if tags else ""))
    for n_rel, _sc, via_rel, _kind in neighbors:
        ne = idx.get(n_rel, {})
        say(f"  ↳ linked: {n_rel} — {ne.get('summary') or ne.get('title') or ''} (via {via_rel})")
    if not ranked:
        say("(nothing found)")


def cmd_where(args):
    """Canonical placement & grouping advisor before creating or updating pages (`Charter §1.3: One place per thing`)."""
    _root, brain = paths(args)
    hk = hook_module(brain)
    idx = hk.load_index()
    vocab = hk.tag_vocab()
    terms = [t for t in (args.terms or []) if str(t).strip()]

    if terms:
        hub_skip = {r for r in idx if graph_engine.is_folder_hub(r)}
        ranked, neighbors = graph_engine.search_pages(
            brain, terms, idx=idx, tag_vocab_map=vocab, skip_pages=hub_skip, limit=6, text_search=True, db_path=hk.CACHE
        )
        say("=== 1. EXISTING CANDIDATE PAGES (extend an existing page before creating a new one!) ===")
        if ranked:
            for rel in ranked:
                e = idx.get(rel, {})
                tags = ",".join(e.get("tags", []))
                say(f"  - {rel} — {e.get('summary') or e.get('title') or ''}" + (f" [{tags}]" if tags else ""))
            for n_rel, _sc, via_rel, _kind in neighbors[:3]:
                ne = idx.get(n_rel, {})
                say(f"    ↳ related: {n_rel} — {ne.get('summary') or ne.get('title') or ''} (via {via_rel})")
        else:
            say("  (no existing page matched — create a new canonical page in the matching group below)")

    say("=== 2. BRAIN GROUPS & CANONICAL FOLDERS (`brain/`) ===")
    areas_dir = os.path.join(brain, "areas")
    if os.path.isdir(areas_dir):
        area_subs = sorted(
            d for d in os.listdir(areas_dir)
            if os.path.isdir(os.path.join(areas_dir, d)) and not d.startswith(".")
        )
        for ad in area_subs:
            ar_readme = f"areas/{ad}/README.md"
            ar_sum = (idx.get(ar_readme, {}).get("summary") or "").strip()
            cnt = sum(1 for r in idx if r.startswith(f"areas/{ad}/") and r != ar_readme)
            say(f"  - areas/{ad}/ ({cnt} pages; hub: {ar_readme})" + (f" — {ar_sum}" if ar_sum else ""))
        flat_areas = sum(
            1 for r in idx
            if r.startswith("areas/") and r.count("/") == 1 and r != "areas/README.md"
        )
        if flat_areas or not area_subs:
            say(f"  - areas/<topic>.md ({flat_areas} flat pages; hub: areas/README.md)")
    std_top = {
        "areas", "people", "projects", "howto", "skills", "calendar", "self", "coach",
        "journal", "inbox", "_atlas", "_secrets", "_archive",
    }
    custom_tops = sorted(
        d for d in os.listdir(brain)
        if os.path.isdir(os.path.join(brain, d)) and not d.startswith((".", "_")) and d not in std_top
    )
    for cd in custom_tops:
        cd_readme = f"{cd}/README.md"
        cd_sum = (idx.get(cd_readme, {}).get("summary") or "").strip()
        cnt = sum(1 for r in idx if r.startswith(f"{cd}/") and r != cd_readme)
        say(f"  - {cd}/ ({cnt} pages; custom folder" + (f", hub: {cd_readme}" if cd_readme in idx else "") + ")"
            + (f" — {cd_sum}" if cd_sum else ""))
    say("  - people/<first-last>.md (hub: people/README.md) — one page per person")
    say("  - projects/<slug>/README.md (hub: projects/README.md) — undertakings with a goal or code repos")
    say("  - howto/<topic>.md (hub: howto/README.md; scripts: howto/scripts/) — rules, workflows, lessons, user scripts")
    say("  - skills/<name>/SKILL.md (hub: skills/README.md) — custom user skills & `triggers:` (survive /atlas-update)")
    say("  - calendar/README.md (`## Timeline` if dated, `## Open To-Dos` if undated) — tasks, deadlines, events")
    say("  - self/ (hub: self/README.md) | settings.md (rhythms/style) | _secrets/<topic>.env (credentials only)")
    say("=== 3. FILING CHECKLIST ===")
    say("  Frontmatter: `summary:` + `tags:` (from tags.md) + `verified: YYYY-MM-DD` (on facts) + link in folder `README.md`.")


def cmd_graph(args):
    """Show a page's outlinks, backlinks (with context & #anchor), and unlinked mentions."""
    _root, brain = paths(args)
    hk = hook_module(brain)
    idx = hk.load_index()
    info = graph_engine.inspect_page_graph(brain, args.page, idx=idx, db_path=hk.CACHE)
    if args.json:
        say(json.dumps(info, indent=2, ensure_ascii=False))
        if "error" in info:
            sys.exit(1)
        return
    if "error" in info:
        say(f"ERROR {info['error']}" + (f" (candidates: {', '.join(info['candidates'])})" if info.get("candidates") else ""))
        sys.exit(1)
    p = info["page"]
    say(f"Page: {p['rel']}" + (f" — {p['title']}" if p.get("title") else ""))
    if p.get("summary"):
        say(f"  summary: {p['summary']}")
    meta = []
    if p.get("tags"):
        meta.append(f"tags=[{', '.join(p['tags'])}]")
    if p.get("verified"):
        meta.append(f"verified={p['verified']}")
    if p.get("valid_until"):
        meta.append(f"valid_until={p['valid_until']}")
    if p.get("sources"):
        meta.append(f"sources={p['sources']}")
    if meta:
        say("  " + "  ".join(meta))
    say(f"Outlinks ({len(info['outlinks'])}):")
    for o in info["outlinks"]:
        anc = f"#{o['anchor']}" if o.get("anchor") else ""
        say(f"  - [{o['kind']}] {o['dst']}{anc}" + (f" — {o['summary']}" if o.get("summary") else ""))
    say(f"Backlinks ({len(info['backlinks'])}):")
    for b in info["backlinks"]:
        anc = f"#{b['anchor']}" if b.get("anchor") else ""
        say(f"  - [{b['kind']}] {b['src']}{anc}: {b['context']}")
    say(f"Unlinked Mentions ({len(info['unlinked_mentions'])}):")
    for u in info["unlinked_mentions"]:
        say(f"  - {u['src']}:{u['line']} (\"{u['matched']}\"): {u['context']}")


def cmd_context(args):
    """Assemble a token-budgeted Markdown context dossier (target page + top specificity-ranked neighbors)."""
    _root, brain = paths(args)
    hk = hook_module(brain)
    idx = hk.load_index()
    dossier = graph_engine.build_context_dossier(
        brain, args.target, hops=args.hops, budget=args.budget,
        tag_vocab_map=hk.tag_vocab(), idx=idx, db_path=hk.CACHE,
    )
    say(dossier.rstrip("\n"))


def cmd_link(args):
    """Retroactive wiki-linking for existing brains (`--scan`, `--journals`, `--related`, `--inline`, `--execute`)."""
    _root, brain = paths(args)
    hk = hook_module(brain)
    do_any = bool(args.journals or args.related or args.inline)
    if args.scan and not do_any or not do_any:
        scan = graph_engine.scan_retroactive_links(brain, idx=hk.load_index(), db_path=hk.CACHE)
        if args.json:
            say(json.dumps(scan, indent=2, ensure_ascii=False))
            return
        j_count = sum(len(v) for v in scan["journal_paths"].values())
        rel_cands = sum(1 for v in scan["mentions"].values() for h in v if not h["has_related"] and not h["has_body"])
        inl_cands = sum(1 for v in scan["mentions"].values() for h in v if not h["has_body"])
        say(f"link scan: {scan['lexicon_size']} canonical entities ({scan['ambiguous_skipped']} ambiguous skipped)")
        say(f"  journal paths ready to link: {j_count} across {len(scan['journal_paths'])} file(s)")
        for j_rel, hits in list(scan["journal_paths"].items())[:8]:
            say(f"    - {j_rel}: {', '.join(h['dst'] for h in hits[:4])}")
        say(f"  unlinked entity mentions: {inl_cands} inline / {rel_cands} frontmatter `related:` across {len(scan['mentions'])} page(s)")
        for s_rel, hits in list(scan["mentions"].items())[:12]:
            unlinked = [h for h in hits if not h["has_body"] and not h["has_related"]]
            if unlinked:
                say(f"    - {s_rel}: " + ", ".join(f"'{h['matched']}' -> {h['dst']}" for h in unlinked[:4]))
        say("Next: run `atlas.py link --journals --related --execute` (safe zero-prose-risk) or add `--inline`.")
        return

    res = graph_engine.apply_retroactive_links(
        brain, do_journals=args.journals, do_related=args.related, do_inline=args.inline,
        execute=args.execute, db_path=hk.CACHE,
    )
    if args.json:
        say(json.dumps(res, indent=2, ensure_ascii=False))
        return
    mode_lbl = "applied" if args.execute else "dry-run"
    say(f"link ({mode_lbl}): {res['files_changed']} file(s), {res['links_added']} link(s)")
    for act in res["actions"][:30]:
        say(f"  {act}")
    if not args.execute and res["files_changed"]:
        say("Dry run. Add --execute to write changes.")


def cmd_conflicts(args):
    """Manage structured fact contradictions in `brain/_atlas/conflicts.json` (`list`, `add`, `resolve`)."""
    _root, brain = paths(args)
    hk = hook_module(brain)
    cpath = hk.CONFLICTS_FILE
    if args.action == "add":
        if not args.summary:
            sys.exit("conflicts add requires --summary \"...\" (and optional --pages p1 p2)")
        entry = graph_engine.add_conflict(brain, args.pages or [], args.summary, conflicts_path=cpath)
        if args.json:
            say(json.dumps(entry, indent=2, ensure_ascii=False))
        else:
            pgs = ", ".join(entry.get("pages", []))
            say(f"[{entry['id']}] {entry['status']}: {entry['summary']}" + (f" ({pgs})" if pgs else ""))
        return
    if args.action == "resolve":
        cid = args.target_id or args.id
        if not cid:
            sys.exit("conflicts resolve requires a conflict ID (positional or --id)")
        resolved = graph_engine.resolve_conflict(brain, cid, resolution=args.resolution or "", conflicts_path=cpath)
        if not resolved:
            sys.exit(f"Conflict not found: {cid}")
        if args.json:
            say(json.dumps(resolved, indent=2, ensure_ascii=False))
        else:
            say(f"Resolved [{resolved['id']}]: {resolved['summary']}")
        return
    items = graph_engine.load_conflicts(brain, cpath) if args.all else graph_engine.open_conflicts(brain, cpath)
    if args.json:
        say(json.dumps(items, indent=2, ensure_ascii=False))
        return
    if not items:
        say("(no open conflicts)" if not args.all else "(no conflicts recorded)")
        return
    for it in items:
        pgs = ", ".join(it.get("pages", []))
        say(f"[{it.get('id')}] {it.get('status', 'open')}: {it.get('summary', '')}" + (f" ({pgs})" if pgs else ""))


# ================================================================= probe

SAMPLES = {
    ("claude", "session"): {"hook_event_name": "SessionStart", "source": "startup", "session_id": "probe"},
    ("claude", "prompt"): {"hook_event_name": "UserPromptSubmit", "prompt": "probe: glossary", "session_id": "probe"},
    ("claude", "stop"): {"hook_event_name": "Stop", "stop_hook_active": True, "last_assistant_message": "Atlas: probe"},
    ("antigravity", "session"): {"conversationId": "probe", "workspacePaths": []},
    ("antigravity", "prompt"): {"conversationId": "probe", "invocationNum": 0, "lastUserInput": "probe: glossary"},
    ("antigravity", "stop"): {"conversationId": "probe", "terminationReason": "ERROR", "fullyIdle": True},
}


def cmd_probe(args):
    root, brain = paths(args)
    user_home = os.path.abspath(os.path.expanduser(args.user_home or "~"))
    if args.agent:
        pairs = [(args.agent, args.scope)]
    else:
        wired = get_state(brain).get("wired", {})
        pairs = [tuple(k.split(":")) for k in wired if ":" in k] or [("claude", args.scope), ("antigravity", args.scope)]
    ok = True
    for agent, scope in pairs:
        t = _targets(agent, scope, root, user_home)
        cmds = {}
        if agent == "claude":
            settings = load_json(t["hooks"], {})
            for ev, he in (("session", "SessionStart"), ("prompt", "UserPromptSubmit"), ("stop", "Stop")):
                for e in settings.get("hooks", {}).get(he, []):
                    for h in e.get("hooks", []):
                        if "atlas_hook.py" in h.get("command", ""):
                            cmds[ev] = h["command"]
        else:
            hooks = load_json(t["hooks"], {}).get("atlas", {})
            for ev, he in (("session", "SessionStart"), ("prompt", "PreInvocation"), ("stop", "Stop")):
                for h in hooks.get(he, []):
                    cmds[ev] = h.get("command", "")
        for ev in ("session", "prompt", "stop"):
            cmd = cmds.get(ev)
            if not cmd:
                say(f"FAIL {agent}:{scope} {ev}: not registered in {fwd(t['hooks'])}")
                ok = False
                continue
            sample = dict(SAMPLES[(agent, ev)])
            sample["cwd" if agent == "claude" else "workspacePaths"] = fwd(root) if agent == "claude" else [fwd(root)]
            t0 = time.monotonic()
            try:
                res = subprocess.run(cmd, shell=True, input=json.dumps(sample), capture_output=True, text=True,
                                     timeout=30, cwd=root, encoding="utf-8", errors="replace",
                                     env=dict(os.environ, ATLAS_PROBE="1"))
            except subprocess.TimeoutExpired:
                say(f"FAIL {agent} {ev}: timeout")
                ok = False
                continue
            ms = int((time.monotonic() - t0) * 1000)
            out = res.stdout.strip()
            try:
                json.loads(out) if out else None
                valid = res.returncode == 0
            except ValueError:
                valid = False
            ok &= valid
            say(f"{'OK  ' if valid else 'FAIL'} {agent}:{scope} {ev}: exit {res.returncode}, {ms} ms, "
                f"{len(out)} bytes{'' if valid else ' -> ' + (out or res.stderr)[:300]}")
    sys.exit(0 if ok else 1)


# ================================================================= check

LINK_RX = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
TICK_RX = re.compile(r"`((?:areas|projects|people|howto|journal|inbox|self|skills|calendar|coach|_atlas)/[^`\s]+\.md)`")
HUB_SKIP = ("journal/", "inbox/", "_archive/", "_secrets/", "_atlas/")


def _pages(brain):
    out = []
    for dp, dns, fns in os.walk(brain):
        dns[:] = [d for d in dns if d not in (".git", "_secrets", "_archive", "node_modules") and not d.startswith(".")]
        for fn in fns:
            if fn.lower().endswith(".md"):
                out.append(os.path.join(dp, fn))
    return out


def cmd_check(args):
    root, brain = paths(args)
    errors, warns, infos = [], [], []

    def size(rel):
        p = os.path.join(brain, rel)
        return len(read(p).encode("utf-8")) if os.path.isfile(p) else None

    rs = size("ROUTER.md")
    if rs is None:
        errors.append("ROUTER.md missing")
    else:
        (errors if rs > LIMITS["router_max"] else warns if rs > LIMITS["router_warn"] else infos).append(
            f"router {rs} bytes, loads into every chat (aim < {LIMITS['router_warn']}, limit {LIMITS['router_max']})")
        if "atlas:core:start" not in read(os.path.join(brain, "ROUTER.md")):
            warns.append("router: core markers missing (<!-- atlas:core:start/end -->)")
    ix = size("INDEX.md")
    if ix is None:
        errors.append("INDEX.md missing")
    elif ix > LIMITS["index_warn"]:
        warns.append(f"INDEX.md {ix} bytes: move detail into the folder READMEs (aim < {LIMITS['index_warn']})")
    memf = os.path.join(brain, "_atlas", "memory", "MEMORY.md")
    if os.path.isfile(memf):
        n = len(read(memf).splitlines())
        if n > LIMITS["memory_lines"]:
            warns.append(f"MEMORY.md has {n} lines (loads into every Claude chat): move lessons to howto/, "
                         f"keep < {LIMITS['memory_lines']}")
    desc = 0
    for name, folder in _skills(brain):
        m = re.search(r'description:\s*"(.*?)"', read(os.path.join(folder, "SKILL.md")), re.S)
        desc += len(m.group(1)) if m else 0
    if desc > LIMITS["skills_desc"]:
        warns.append(f"skill descriptions total {desc} chars (listed in every chat; aim < {LIMITS['skills_desc']})")
    for name in ("AGENTS.md", "CLAUDE.md", "GEMINI.md"):
        if os.path.exists(os.path.join(brain, name)):
            warns.append(f"brain/{name} exists: agents load it natively, the router would load twice")
    pages = _pages(brain)
    rels = {fwd(os.path.relpath(p, brain)): p for p in pages}
    hk = hook_module(brain)
    idx = hk.load_index()
    gcheck = graph_engine.validate_and_analyze_brain(brain, root, rels, idx=idx, db_path=hk.CACHE)
    graph = gcheck["graph"]
    broken = gcheck["broken"]
    fixes = gcheck["fixes"]
    if broken:
        warns.append(f"{len(broken)} broken link(s):\n    " + "\n    ".join(broken[:30]))
    if gcheck["broken_anchors"]:
        warns.append(f"{len(gcheck['broken_anchors'])} broken #section anchor(s):\n    "
                     + "\n    ".join(gcheck["broken_anchors"][:20]))
    if gcheck["ambiguous_wikilinks"]:
        warns.append(f"{len(gcheck['ambiguous_wikilinks'])} ambiguous [[wikilink]](s):\n    "
                     + "\n    ".join(gcheck["ambiguous_wikilinks"][:20]))
    if gcheck["missing_sources"]:
        warns.append(f"{len(gcheck['missing_sources'])} missing local file(s) in `sources:`:\n    "
                     + "\n    ".join(gcheck["missing_sources"][:20]))
    if gcheck["wikilink_count"]:
        infos.append(f"{gcheck['wikilink_count']} [[wikilink]](s) in {len(gcheck['wikilink_files'])} page(s) ready "
                     "to compile (check --fix)")
    if gcheck["isolated"]:
        infos.append(f"{len(gcheck['isolated'])} horizontally isolated leaf page(s) (0 body/related wiki edges; "
                     "run `atlas.py link --scan`)")
    if gcheck["stale_verified"]:
        infos.append(f"{len(gcheck['stale_verified'])} page(s) with `verified:` older than 365 days: "
                     + ", ".join(gcheck["stale_verified"][:8]))
    today_chk = dt.date.today()
    expiring_valid = []
    for r_chk, e_chk in sorted(idx.items()):
        vu = str(e_chk.get("valid_until") or "").strip()
        if re.match(r"^\d{4}-\d{2}-\d{2}$", vu):
            try:
                rem_d = (dt.date.fromisoformat(vu) - today_chk).days
                if rem_d < 0:
                    expiring_valid.append(f"{r_chk} (expired {vu})")
                elif rem_d <= 60:
                    expiring_valid.append(f"{r_chk} (valid_until {vu}, in {rem_d}d)")
            except ValueError:
                pass
    if expiring_valid:
        infos.append(f"{len(expiring_valid)} page(s) with expired or upcoming `valid_until:`: "
                     + ", ".join(expiring_valid[:8]))
    seen, todo = set(), [r for r in ("ROUTER.md", "INDEX.md") if r in graph]
    while todo:
        r = todo.pop()
        if r in seen:
            continue
        seen.add(r)
        todo += [t for t in graph.get(r, ()) if t not in seen]
        if r.endswith("README.md"):  # a folder index covers its folder's pages
            folder = os.path.dirname(r)
            todo += [x for x in graph if os.path.dirname(x) == folder and x not in seen]
    reg = hk.projects(idx)
    reg_pages = {p["page"] for p in reg}
    orphans = [r for r in rels if r not in seen and r not in reg_pages and not r.startswith(HUB_SKIP) and "/" in r]
    if orphans:
        warns.append(f"{len(orphans)} page(s) not reachable from INDEX.md:\n    " + "\n    ".join(sorted(orphans)[:30]))
    by_hash = {}
    for rel, p in rels.items():
        if not rel.startswith("_atlas/"):
            by_hash.setdefault(hashlib.sha256(read(p).strip().encode("utf-8")).hexdigest(), []).append(rel)
    dups = [v for v in by_hash.values() if len(v) > 1]
    if dups:
        warns.append("identical pages: " + "; ".join(" = ".join(v) for v in dups[:10]))
    # Tags and summaries (D22): every content page carries `summary:` and `tags:` from brain/tags.md.
    vocab = hk.tag_vocab()
    canon = {s: t for t, syns in vocab.items() for s in syns}
    content = [r for r in idx if graph_engine.is_user_knowledge_rel(r)
               and not r.endswith("README.md")]
    no_sum = [r for r in content if not idx[r].get("summary")]
    no_tag = [r for r in content if not idx[r].get("tags")]
    unknown, synonyms, usage = {}, {}, {}
    for r, e in idx.items():
        for t in e.get("tags", []):
            usage[t] = usage.get(t, 0) + 1
            if t in canon:
                synonyms.setdefault(r, []).append((t, canon[t]))
            elif vocab and t not in vocab:
                unknown.setdefault(t, []).append(r)
    if no_sum:
        warns.append(f"{len(no_sum)} page(s) without `summary:` (search reads summaries first): " + ", ".join(no_sum[:8]))
    if no_tag:
        warns.append(f"{len(no_tag)} page(s) without `tags:`: " + ", ".join(no_tag[:8]))
    if unknown:
        warns.append("tags not in tags.md (add them there or use an existing one): "
                     + "; ".join(f"{t} ({len(v)})" for t, v in list(unknown.items())[:15]))
    if synonyms:
        warns.append(f"{len(synonyms)} page(s) use a synonym instead of the tag (check --fix normalises them)")
    lonely = sorted(t for t, n in usage.items() if n == 1 and t in vocab)
    if lonely:
        infos.append("tags used by one page only (merge?): " + ", ".join(lonely[:15]))
    st0 = get_state(brain)
    if st0.get("wired") and st0.get("last_hook_run"):
        age = (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(st0["last_hook_run"])).days
        if age > 3:
            warns.append(f"session hook last ran {age} days ago: are the hooks still firing? (/atlas-update heals)")
    sset = hk.settings()
    allowed = {"briefing": {"daily", "weekly", "off"}, "questions": {"per-briefing", "evening-3", "weekly-session", "off"},
               "reminder": {"signals", "every-turn", "rare"}, "daily_log": {"on", "off"}}
    bad = [f"{k}={sset.get(k)}" for k, ok in allowed.items() if sset.get(k) not in ok]
    md = hk._int(sset.get("maintenance_days"), -1)
    if not 7 <= md <= 30:
        bad.append(f"maintenance_days={sset.get('maintenance_days')} (7-30)")
    if bad:
        warns.append("settings.md has invalid values (defaults apply): " + ", ".join(bad))
    names, missing = {}, []
    for p in reg:
        for nm in p["names"]:
            names.setdefault(nm.lower(), set()).add(p["slug"])
        missing += [f"{p['slug']} ({raw})" for raw, x in zip(p["paths"], p["resolved"]) if not os.path.isdir(x)]
    if missing:
        warns.append(f"{len(missing)} project folder(s) not found (moved? update `path:`): " + ", ".join(missing[:10])
                     + (" …" if len(missing) > 10 else ""))
    clash = {k: v for k, v in names.items() if len(v) > 1}
    if clash:
        warns.append("ambiguous project names (routing must ask): " +
                     "; ".join(f"{k} -> {', '.join(sorted(v))}" for k, v in list(clash.items())[:10]))
    infos.append(f"{len(reg)} project(s) in the registry")
    gi = os.path.join(brain, ".gitignore")
    if not os.path.isfile(gi) or "_secrets/" not in read(gi):
        errors.append(".gitignore does not exclude _secrets/")
    if os.path.isdir(os.path.join(brain, ".git")):
        tracked = _git(brain, "ls-files", "_secrets").stdout.split()
        if tracked:
            errors.append(f"_secrets/ is tracked by Git ({len(tracked)} files): run git rm --cached -r _secrets")
        leaks = []
        for rel, p in rels.items():
            if rel.startswith("_atlas/"):
                continue
            for i, line in enumerate(read(p).splitlines(), 1):
                if SECRET_RX.search(line):
                    leaks.append(f"{rel}:{i}")
        if leaks:
            warns.append("possible credentials outside _secrets/ (they would be pushed): " + ", ".join(leaks[:20]))
        if _git(brain, "status", "--porcelain").stdout.strip():
            infos.append("uncommitted changes (the stop hook commits them)")
        st = get_state(brain)
        if not _git(brain, "remote").stdout.strip():
            warns.append("no Git remote: the brain has no off-site backup (charter §6)")
        else:
            last = st.get("last_push")
            age = (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(last)).days if last else None
            if age is None or age > 7:
                warns.append(f"last successful push: {'never' if age is None else f'{age} days ago'}")
    else:
        errors.append("brain is not a Git repository")
    if CLOUD.search(root):
        errors.append("ROOT is inside a cloud-sync folder (charter §6)")
    st = get_state(brain)
    wired = st.get("wired", {}) if isinstance(st.get("wired"), dict) else {}
    for key, info in wired.items():
        try:
            text = read(info["hooks"])
        except OSError:
            text = ""
        if "atlas_hook.py" not in text:
            errors.append(f"{key}: Atlas hooks not registered in {info['hooks']}")
        if info.get("python") and not os.path.exists(info["python"]):
            errors.append(f"{key}: hook interpreter {info['python']} not found")
    if not wired:
        warns.append("no agent wired (atlas.py wire); without hooks Atlas runs in rule mode")
    upd = hk.last_mark("update-check")
    if not upd:
        warns.append("/atlas-update never ran (no `atlas: update-check` commit)")
    elif (hk._age_days(upd[0]) or 0) > 30:
        warns.append(f"last /atlas-update check {int(hk._age_days(upd[0]))} days ago")
    overdue = [d for d in _deadlines(brain, 0) if d[1] < 0]
    if overdue:
        warns.append(f"{len(overdue)} overdue deadline(s): " + "; ".join(f"{d[0]} {d[2]} [{d[3]}]" for d in overdue[:5]))
    for title, items in (("ERROR", errors), ("WARN", warns), ("INFO", infos)):
        for it in items:
            say(f"{title:5} {it}")
    say(f"check: {len(errors)} error(s), {len(warns)} warning(s), {len(pages)} pages")
    if args.fix:
        _check_fix(args, root, brain, fixes, synonyms, gcheck["wikilink_files"])
    sys.exit(1 if errors else 0)


def _check_fix(args, root, brain, link_fixes, synonyms, wikilink_files=None):
    """Mechanical repairs only (0 tokens): [[wikilink]] compilation, moved-page links, tag synonyms -> tag, cache, routes."""
    done = []
    for rel, (compiled_text, n_comp) in (wikilink_files or {}).items():
        p = os.path.join(brain, rel)
        write(p, compiled_text)
        done.append(f"wikilinks in {rel}: {n_comp}")
    for rel, pairs in link_fixes.items():
        p = os.path.join(brain, rel)
        text = read(p)
        for old, new in pairs:
            text = text.replace(f"]({old})", f"]({new})").replace(f"]({old}#", f"]({new}#")
        write(p, text)
        done.append(f"links in {rel}: {len(pairs)}")
    for rel, pairs in synonyms.items():
        p = os.path.join(brain, rel)
        text = read(p)
        end = text.find("\n---", 3)
        head, body = text[:end], text[end:]
        for syn, tag in pairs:
            head = re.sub(r"(?m)^(tags:.*?)(?<![\w-])" + re.escape(syn) + r"(?![\w-])", lambda m: m.group(1) + tag, head)
            head = re.sub(r"(?m)^(\s*-\s+)" + re.escape(syn) + r"\s*$", lambda m: m.group(1) + tag, head)
        write(p, head + body)
        done.append(f"tags in {rel}: " + ", ".join(f"{s}->{t}" for s, t in pairs))
    hook_module(brain).load_index()
    st = get_state(brain)
    wired = st.get("wired", {}) if isinstance(st.get("wired"), dict) else {}
    changes = []
    for key in wired:
        agent, scope = key.split(":")
        t = _targets(agent, scope, root, os.path.expanduser("~"))
        if t["rules"]:
            changes += _rule_changes(brain, root, agent, t["rules"], False)
    if changes:
        _apply(brain, changes, run_id(brain, "routes"))
        done.append(f"project rules: {len(changes)}")
    say("fix: " + ("; ".join(done) if done else "nothing mechanical to fix"))


# ================================================================= deadlines

def _deadlines(brain, days):
    hk = hook_module(brain)
    out = []
    for when, delta, what, rel in hk.deadlines(hk.load_index(), days=10 ** 6 if days is None else days):
        out.append((when.isoformat(), delta, what, rel))
    return out


def cmd_deadlines(args):
    _root, brain = paths(args)
    items = _deadlines(brain, None if args.all else args.days)
    for when, delta, what, rel in items:
        say(f"{when}  ({'overdue ' + str(-delta) + ' d' if delta < 0 else 'in ' + str(delta) + ' d'})  {what}  [{rel}]")
    if args.ics:
        _write_ics(args.ics, items)


def _write_ics(ics_path, items):
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//life-atlas//calendar//EN", "CALSCALE:GREGORIAN"]
    for when, _d, what, rel in items:
        day = when.replace("-", "")
        nxt = (dt.date.fromisoformat(when) + dt.timedelta(days=1)).strftime("%Y%m%d")
        uid = hashlib.sha1(f"{when}|{what}|{rel}".encode("utf-8")).hexdigest()
        summary = (what or rel).replace(",", "\\,").replace(";", "\\;")
        lines += ["BEGIN:VEVENT", f"UID:{uid}@life-atlas",
                  f"DTSTAMP:{dt.datetime.now(dt.timezone.utc):%Y%m%dT%H%M%SZ}",
                  f"DTSTART;VALUE=DATE:{day}", f"DTEND;VALUE=DATE:{nxt}", f"SUMMARY:{summary}",
                  f"DESCRIPTION:Atlas page: {rel}", "BEGIN:VALARM", "ACTION:DISPLAY", f"DESCRIPTION:{summary}",
                  "TRIGGER:-P3D", "END:VALARM", "END:VEVENT"]
    lines.append("END:VCALENDAR")
    with open(ics_path, "w", encoding="utf-8", newline="\r\n") as fh:
        fh.write("\n".join(lines) + "\n")
    say(f"Wrote {len(items)} event(s) to {ics_path}")


def _parse_ics_text(text, today, horizon_days=90):
    """Parse VEVENT blocks from an .ics string (stdlib only) into timeline lines."""
    out = []
    for block in re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", text, re.S):
        unfolded = re.sub(r"\r?\n[ \t]", "", block)
        dtstart = re.search(r"(?m)^DTSTART(?:;[^:]*)?:(\d{8})(?:T(\d{2})(\d{2}))?", unfolded)
        dtend = re.search(r"(?m)^DTEND(?:;[^:]*)?:(\d{8})(?:T(\d{2})(\d{2}))?", unfolded)
        summ = re.search(r"(?m)^SUMMARY(?:;[^:]*)?:(.+)$", unfolded)
        if not dtstart or not summ:
            continue
        try:
            s_date = dt.date.fromisoformat(f"{dtstart.group(1)[:4]}-{dtstart.group(1)[4:6]}-{dtstart.group(1)[6:8]}")
        except ValueError:
            continue
        e_date = s_date
        if dtend:
            try:
                ed = dt.date.fromisoformat(f"{dtend.group(1)[:4]}-{dtend.group(1)[4:6]}-{dtend.group(1)[6:8]}")
                if not dtend.group(2) and ed > s_date:
                    ed -= dt.timedelta(days=1)
                if ed >= s_date:
                    e_date = ed
            except ValueError:
                pass
        delta = 0 if s_date <= today <= e_date else ((s_date - today).days if s_date > today else (e_date - today).days)
        if not (0 <= delta <= horizon_days):
            continue
        t_part = ""
        if dtstart.group(2):
            t_part = f"{dtstart.group(2)}:{dtstart.group(3)} "
            if dtend and dtend.group(2) and s_date == e_date:
                t_part = f"{dtstart.group(2)}:{dtstart.group(3)}-{dtend.group(2)}:{dtend.group(3)} "
        d_part = s_date.isoformat() if s_date == e_date else f"{s_date.isoformat()}..{e_date.isoformat()}"
        title = summ.group(1).strip().replace("\\,", ",").replace("\\;", ";")
        kind = "blocked" if s_date != e_date else "event"
        out.append((s_date, f"- [ ] {d_part} {t_part}[{kind}] {title}"))
    return [line for _d, line in sorted(out)]


def _calendar_init(root, brain, hk, execute):
    """Initialzündung: ensure calendar/README.md + INDEX.md link exist and harvest dates, birthdays & open to-dos."""
    cal_readme = os.path.join(brain, "calendar", "README.md")
    idx_file = os.path.join(brain, "INDEX.md")
    values = _router_values(brain, root)
    created_cal = False
    if not os.path.isfile(cal_readme):
        created_cal = True
        tpl = (
            "---\n"
            "summary: Central calendar and to-do hub: timeline (events, blocked dates, goals, workouts, deadlines, birthdays) and open to-dos\n"
            "tags: [atlas, calendar]\n"
            "---\n"
            "# Calendar & To-Dos\n\n"
            f"Stand: {values['DATE']}. The Atlas-First calendar and task hub for {values['USER_NAME']}.\n\n"
            "## Timeline\n\n"
            "## Open To-Dos\n"
        )
        if execute:
            write(cal_readme, tpl)
    else:
        tpl = read(cal_readme)
    linked_index = False
    if os.path.isfile(idx_file):
        idx_text = read(idx_file)
        if "calendar/README.md" not in idx_text:
            linked_index = True
            if execute:
                if "| `self/` |" in idx_text:
                    idx_text = idx_text.replace(
                        "| `self/` |",
                        "| `calendar/` | Central calendar timeline (events, blocked dates, goals, workouts, birthdays) & open to-dos | `calendar/README.md` |\n| `self/` |",
                        1,
                    )
                else:
                    idx_text = idx_text.rstrip() + "\n- Calendar & To-Dos: `calendar/README.md`\n"
                write(idx_file, idx_text)
    idx = hk.load_index()
    existing_cal = tpl.lower()
    existing_due = {raw.lower() for e in idx.values() for raw in e.get("due", [])}
    harvest_timeline, harvest_todos = [], []
    bday_rx = re.compile(r"(?i)\b(?:geburtstag|geburtsdatum|birthday|geboren|geb\.|born)\b[^0-9\n]{0,25}(\d{4}-\d{2}-\d{2}|\d{1,2}\.\d{1,2}\.\d{4})")
    task_rx = re.compile(r"^\s*-\s*\[\s\]\s+(.+)$")
    for dp, dns, fns in os.walk(brain):
        dns[:] = [d for d in dns if d not in (".git", "_secrets", "_archive", "_atlas", "calendar", "journal", "inbox")
                  and not d.startswith(".")]
        for fn in sorted(fns):
            if not fn.lower().endswith(".md"):
                continue
            full = os.path.join(dp, fn)
            rel = fwd(os.path.relpath(full, brain))
            if rel in ("ROUTER.md", "INDEX.md", "settings.md", "tags.md", "glossary.md"):
                continue
            text = read(full)
            title = idx.get(rel, {}).get("title") or os.path.splitext(fn)[0]
            if rel.startswith(("people/", "self/")):
                for m in bday_rx.finditer(text):
                    raw_d = m.group(1)
                    if "." in raw_d:
                        dd, mm, yyyy = raw_d.split(".")
                        iso = f"{int(yyyy):04d}-{int(mm):02d}-{int(dd):02d}"
                    else:
                        iso = raw_d
                    mm_dd = iso[4:]
                    entry = f"- [ ] {iso} [birthday] {title}"
                    if iso.lower() not in existing_cal and not any(iso in d or mm_dd in d for d in existing_due):
                        harvest_timeline.append(entry)
                        existing_cal += "\n" + entry.lower()
            is_checklist_file = any(w in fn.lower() for w in ("todo", "checkliste", "aufgaben"))
            in_open_sec = False
            for line in text.splitlines():
                if line.startswith("#"):
                    low_h = line.lower()
                    in_open_sec = any(w in low_h for w in ("offene punkte", "offene to-dos", "open to-dos",
                                                           "noch offen", "checkliste", "backlog"))
                    if "erledigt" in low_h and "offen" not in low_h:
                        in_open_sec = False
                    continue
                tm = task_rx.match(line)
                if tm:
                    task_text = tm.group(1).strip()
                    if len(task_text) < 4 or task_text.startswith("("):
                        continue
                    if task_text.lower() in existing_cal:
                        continue
                    dm = re.match(r"^(\d{4}-\d{2}-\d{2})\b\s*[-:–—]?\s*(.+)$", task_text)
                    if dm:
                        entry = f"- [ ] {dm.group(1)} [todo] {dm.group(2).strip()} ({rel})"
                        harvest_timeline.append(entry)
                    else:
                        entry = f"- [ ] [med] {task_text} ({rel})"
                        harvest_todos.append(entry)
                    existing_cal += "\n" + task_text.lower()
                    continue
                if is_checklist_file and in_open_sec:
                    nm = re.match(r"^\d+\.\s+\*\*(.+?)\*\*(.*)$", line.strip())
                    if nm:
                        headline = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", (nm.group(1) + " " + nm.group(2))).replace("**", "").replace("`", "").strip()
                        headline = re.sub(r"\s*:\s*$", "", headline).strip()
                        if re.search(r"\b(erledigt|abgeschlossen|done)\b", headline, re.I) or headline.lower() in existing_cal:
                            continue
                        prio = "high" if re.search(r"\b(phase 5|nachziehen|nachtragen|frist)\b", headline, re.I) else "med"
                        harvest_todos.append(f"- [ ] [{prio}] {headline} ({rel})")
                        existing_cal += "\n" + headline.lower()
    if execute and (harvest_timeline or harvest_todos):
        cur = read(cal_readme)
        if harvest_timeline:
            if "\n## Timeline" in cur:
                cur = cur.replace("\n## Timeline", "\n## Timeline\n\n" + "\n".join(harvest_timeline), 1)
            else:
                cur = cur.rstrip() + "\n\n## Timeline\n\n" + "\n".join(harvest_timeline) + "\n"
        if harvest_todos:
            if "\n## Open To-Dos" in cur:
                cur = cur.replace("\n## Open To-Dos", "\n## Open To-Dos\n\n" + "\n".join(harvest_todos), 1)
            else:
                cur = cur.rstrip() + "\n\n## Open To-Dos\n\n" + "\n".join(harvest_todos) + "\n"
        write(cal_readme, cur)
    due_count = sum(len(e.get("due", [])) for e in idx.values())
    say(f"calendar init ({'applied' if execute else 'dry-run'}): "
        f"calendar/README.md {'created' if created_cal else 'exists'}, "
        f"INDEX.md {'linked' if linked_index else 'ok'}, "
        f"{due_count} decentralized due: item(s), "
        f"{len(harvest_timeline)} timeline item(s) and {len(harvest_todos)} open to-do(s) harvested from brain.")


def _open_visual_calendar(hk, idx, open_now=False, view=None):
    cal_url = hk.render_calendar_html(idx, start_server=True)
    if view and view in ("coach", "todo", "graph"):
        sep = "&" if "?" in cal_url else "?"
        cal_url = f"{cal_url}{sep}view={view}"
    say(f"Visual calendar: [📅 Atlas Kalender öffnen]({cal_url})")
    if open_now:
        try:
            if os.name == "nt":
                os.startfile(cal_url)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", cal_url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                import webbrowser
                webbrowser.open(cal_url)
        except Exception:
            pass
    return cal_url


def cmd_calendar(args):
    root, brain = paths(args)
    hk = hook_module(brain)
    req_view = getattr(args, "view", None)
    if args.init:
        _calendar_init(root, brain, hk, args.execute)
        _open_visual_calendar(hk, hk.load_index(), getattr(args, "open", False), view=req_view)
        return
    today = dt.date.today()
    if args.sync_ics:
        src = args.ics_source or hk.settings().get("calendar_ics", "").strip()
        if not src:
            sys.exit("No .ics source: pass --ics-source <url_or_path> or set `calendar_ics:` in brain/settings.md.")
        if re.match(r"^https?://", src):
            import urllib.request
            with urllib.request.urlopen(src, timeout=15) as resp:
                ics_text = resp.read().decode("utf-8", errors="replace")
        else:
            ics_text = read(os.path.expanduser(src))
        lines = _parse_ics_text(ics_text, today)
        cal_readme = os.path.join(brain, "calendar", "README.md")
        cur = read(cal_readme) if os.path.isfile(cal_readme) else ""
        added = [ln for ln in lines if ln.lower() not in cur.lower()]
        say(f"sync-ics: {len(lines)} upcoming event(s) in feed, {len(added)} new.")
        for ln in added[:25]:
            say(f"  {ln}")
        if args.execute and added and os.path.isfile(cal_readme):
            if "\n## Timeline" in cur:
                cur = cur.replace("\n## Timeline", "\n## Timeline\n\n" + "\n".join(added), 1)
            else:
                cur = cur.rstrip() + "\n\n## Timeline\n\n" + "\n".join(added) + "\n"
            write(cal_readme, cur)
            say("Applied to brain/calendar/README.md.")
        _open_visual_calendar(hk, hk.load_index(), getattr(args, "open", False), view=req_view)
        return
    idx = hk.load_index()
    if getattr(args, "for_role", None) == "coach":
        all_items = hk.calendar_items(idx, days=365, past_days=14, include_done=True)
        goals = [i for i in all_items if i["kind"] == "goal" and i["delta"] >= 0 and i["status"] == "open"]
        blocked = [i for i in all_items if 0 <= i["delta"] <= 28 and i["status"] == "open"
                   and (i["kind"] == "blocked" or (i["kind"] == "event" and i["delta"] <= 7 and i["time"]))]
        workouts = [i for i in all_items if -14 <= i["delta"] <= 14 and i["kind"] == "workout"]
        coach_ctx = hk._extract_coach_context(idx) if hasattr(hk, "_extract_coach_context") else {}
        if coach_ctx.get("metrics"):
            say("=== ATHLETE PROFILE (from brain) ===")
            for m in coach_ctx["metrics"]:
                say(f"  {m['label']}: {m['value']}")
        say("=== GOALS & RACES (next 365 d) ===")
        for g in goals:
            k = f"goal:{g['subkind']}" if g["subkind"] else "goal"
            say(f"  {g['start'].isoformat()} (in {g['delta']} d / {g['delta'] / 7:.1f} w) [{k}] {g['what']}")
        for mg in coach_ctx.get("macro_goals", []):
            say(f"  [{mg.get('cat', 'macro')}] {mg['title']}: {mg['detail']}")
        if not goals and not coach_ctx.get("macro_goals"):
            say("  (none)")
        say("=== BLOCKED & BUSY WINDOWS (next 28 d) ===")
        for b in blocked:
            span = b["start"].isoformat() if b["start"] == b["end"] else f"{b['start'].isoformat()}..{b['end'].isoformat()}"
            t = f" {b['time']}" if b["time"] else ""
            say(f"  {span}{t} [{b['kind']}] {b['what']}")
        if not blocked:
            say("  (none)")
        say("=== WORKOUTS (last 14 d .. next 14 d) ===")
        box_map = {"open": "[ ]", "done": "[x]", "skipped": "[-]"}
        for w in workouts:
            k = f"workout:{w['subkind']}" if w["subkind"] else "workout"
            say(f"  {box_map.get(w['status'], '[ ]')} {w['start'].isoformat()} [{k}] {w['what']}")
        if not workouts:
            say("  (none)")
        _open_visual_calendar(hk, idx, getattr(args, "open", False), view=req_view or "coach")
        return
    horizon = 10 ** 6 if args.all else args.days
    items = hk.calendar_items(idx, days=horizon, past_days=365 if args.include_done else 3650,
                              include_done=args.include_done)
    box_map = {"open": "[ ]", "done": "[x]", "skipped": "[-]"}
    shown = 0
    for it in items:
        if not args.include_done and it["delta"] < 0 and it["kind"] not in ("todo", "deadline"):
            continue
        span = it["start"].isoformat() if it["start"] == it["end"] else f"{it['start'].isoformat()}..{it['end'].isoformat()}"
        tag = f"overdue {-it['delta']} d" if it["delta"] < 0 else ("today" if it["delta"] == 0 else f"in {it['delta']} d")
        k = f"{it['kind']}:{it['subkind']}" if it["subkind"] else it["kind"]
        t = f"{it['time']} " if it["time"] else ""
        say(f"{box_map.get(it['status'], '[ ]')} {span}  ({tag})  {t}[{k}] {it['what']}  [{it['rel']}]")
        shown += 1
    todos = hk.open_todos(include_done=args.include_done)
    if todos:
        say("Open To-Dos (undated):")
        for td in todos:
            say(f"  {box_map.get(td['status'], '[ ]')} [{td['prio']}] {td['what']}  [{td['rel']}]")
    if shown == 0 and not todos:
        say("calendar: no upcoming items or open to-dos")
    if args.ics:
        _write_ics(args.ics, _deadlines(brain, None if args.all else args.days))
    _open_visual_calendar(hk, idx, getattr(args, "open", False), view=req_view)


def _resolve_todo_target(hk, idx, query, file_rel=None, line_no=None):
    """Resolve `query` (1-based number `#1`/`1` or text substring) and/or `--file`/`--line` to `(rel, line, what)`."""
    clean_q = (query or "").strip()
    if file_rel:
        return file_rel, line_no, clean_q
    tasks = hk.actionable_tasks(idx, include_upcoming_days=365)
    m_num = re.match(r"^#?(\d+)$", clean_q)
    if m_num:
        n = int(m_num.group(1))
        if 1 <= n <= len(tasks):
            t = tasks[n - 1]
            return t["rel"], (t["line"] if t["line"] > 0 else 0), t["what"]
        sys.exit(f"todo: task #{n} out of range (1..{len(tasks)} active task(s)).")
    if not clean_q:
        sys.exit("todo: specify a task number (e.g. `1`), task text, or `--file <rel> --line <N>`.")
    want = cal_engine._norm_todo_compare(clean_q)
    exact = [t for t in tasks if cal_engine._norm_todo_compare(t["what"]) == want]
    if len(exact) == 1:
        t = exact[0]
        return t["rel"], (t["line"] if t["line"] > 0 else 0), t["what"]
    subs = [t for t in tasks if want in cal_engine._norm_todo_compare(t["what"]) or cal_engine._norm_todo_compare(t["what"]) in want]
    if len(subs) == 1:
        t = subs[0]
        return t["rel"], (t["line"] if t["line"] > 0 else 0), t["what"]
    if len(subs) > 1:
        preview = "; ".join(f"#{t['num']} {t['what']} [{t['rel']}]" for t in subs[:5])
        sys.exit(f"todo: ambiguous match '{clean_q}' ({len(subs)} hits: {preview}). Pass task `#num` or `--file`/`--line`.")
    return "calendar/README.md", line_no, clean_q


def cmd_todo(args):
    _root, brain = paths(args)
    hk = hook_module(brain)
    idx = hk.load_index()
    action = (args.action or "list").lower()
    query = " ".join(args.target or []).strip()

    if action == "list":
        horizon = (args.days if args.days else 365) if args.all else 0
        tasks = hk.actionable_tasks(idx, include_upcoming_days=horizon)
        if not tasks:
            say("todo: no actionable open or overdue tasks")
        else:
            for t in tasks:
                loc = f"{t['rel']}:{t['line']}" if t["line"] else t["rel"]
                k_tag = "[deadline] " if t["kind"] == "deadline" else ""
                say(f"{t['num']:2d}. [{t['badge']}] {k_tag}{t['what']}  [{loc}]")
        if getattr(args, "open", False):
            _open_visual_calendar(hk, idx, open_now=True, view="todo")
        return

    if action == "add":
        text = query or (args.text or "").strip()
        if not text:
            sys.exit("todo add: provide item text (e.g. `atlas.py todo add \"Dentist\" --to 2026-11-25 --time 10:15 --kind event`).")
        res = cal_engine.add_todo_in_brain(
            brain, text, target_date_spec=args.to, prio=args.prio or "med",
            kind=getattr(args, "kind", None) or "todo", time_spec=getattr(args, "time", None),
        )
        if not res.get("ok"):
            sys.exit(f"todo add failed: {res.get('error')}")
        idx = hk.load_index()
        if getattr(args, "open", False):
            _open_visual_calendar(hk, idx, open_now=True, view="todo")
        else:
            hk.render_calendar_html(idx, start_server=False)
        say(f"added: {res['entry']}  [{res['rel']}:{res['line']}]")
        return

    rel, ln, what = _resolve_todo_target(hk, idx, query, file_rel=args.file, line_no=args.line)
    if action in ("done", "skip", "reopen"):
        st_map = {"done": "done", "skip": "skipped", "reopen": "open"}
        res = cal_engine.toggle_todo_in_brain(brain, rel, line_no=ln, what=what, target_status=st_map[action])
        if not res.get("ok"):
            sys.exit(f"todo {action} failed: {res.get('error')}")
        idx = hk.load_index()
        if getattr(args, "open", False):
            _open_visual_calendar(hk, idx, open_now=True, view="todo")
        else:
            hk.render_calendar_html(idx, start_server=False)
        say(f"{res['status']}: {what or rel}  [{res['rel']}:{res['line']}]")
        return

    if action == "move":
        if not args.to:
            sys.exit("todo move: pass `--to <YYYY-MM-DD|+Nd|+Nw|tomorrow>`.")
        res = cal_engine.reschedule_task_in_brain(brain, rel, args.to, line_no=ln, what=what)
        if not res.get("ok"):
            sys.exit(f"todo move failed: {res.get('error')}")
        idx = hk.load_index()
        if getattr(args, "open", False):
            _open_visual_calendar(hk, idx, open_now=True, view="todo")
        else:
            hk.render_calendar_html(idx, start_server=False)
        kind_lbl = "scheduled" if res.get("promoted") else "rescheduled"
        say(f"{kind_lbl} -> {res['date']}: {what or rel}  [{res['rel']}:{res['line']}]")
        return


# ================================================================= coach (Cycling, Running & Strength)

COACH_DURATIONS = coach_engine.COACH_DURATIONS

COACH_DEFAULT_README = """---
summary: Atlas Sport-Coach hub: adaptive Simple-to-Pro athlete profile, macro periodization, 3-epoch power curve, readiness, and activity log
tags: [atlas, sport]
aliases: [Sport Coach, Trainingssteuerung, Radsport Coach, Lauf Coach]
---
# Atlas · Sport-Coach Hub

Stand: {{DATE}}. Native training and physiology hub for {{USER_NAME}} (Cycling, Running & Strength), scaling seamlessly via `depth: auto | simple | standard | pro` in [profile.md](profile.md).

## Architecture & Pages

| Page | Purpose |
|---|---|
| [profile.md](profile.md) | Coaching depth (`auto \\| simple \\| standard \\| pro`), biometrics, cycling & running thresholds (`FTP`, `CP/W'`, `CS/D'`), weekly time budget, bio-rules |
| [playbook.md](playbook.md) | Personal coach notebook & feelings-file: athlete motivation, gut/fueling reality, indoor/sensor setup, empirical dose-response rules & open discovery |
| [macro-plan.md](macro-plan.md) | Season macro periodization (Base, Build, Peak, Taper, Deload) anchored to `[goal]` and `[blocked]` in [../calendar/README.md](../calendar/README.md) |
| [power-curve.md](power-curve.md) | 3-Epoch Mean Maximal Power (`42d` · `90d` · `365d`), PDC Fiber Typology, Multi-Tier Durability (`15/25/35 kJ/kg` + `>CP`) & Running Splits |
| [activities.md](activities.md) | 3-Tier Strava activity log, `W'bal`, torque/cadence, CHO burn, steady `Pw:Hr`, and subjective athlete feedback (`RPE` / feel) |
| [readiness.md](readiness.md) | Daily readiness & recovery log (`RHR`, sleep, soreness, `GREEN/AMBER/RED`) + longitudinal `CTL/ATL/TSB/ACWR` state |
| `workouts/` | Deterministically generated `.zwo` workout files for Hammerhead Karoo / Zwift / Wahoo / Elite trainers |
"""

COACH_DEFAULT_PROFILE = """---
summary: Athlete physiology profile: coaching depth, biometrics, cycling FTP/CP & zones, running CS/D' threshold, weekly time budget, bio-rules
tags: [self, sport]
aliases: [Athletenprofil, FTP, Trainingszonen, Leistungsdaten, Schwellenwerte]
depth: auto
---
# Athlete Profile & Thresholds

Stand: {{DATE}}. Central physiological parameters and scheduling rules for {{USER_NAME}} (`depth: auto | simple | standard | pro`).

## 1. Biometrics & Core Thresholds

| Metric | Value | Notes / Source |
|---|---|---|
| **Biometrie** | {biometrie} | Height & Current Weight |
| **FTP (Cycling)** | {ftp} | Functional Threshold Power |
| **CP & W' (Cycling)** | CP: — W · W': — kJ | 2-param Critical Power & anaerobic work capacity |
| **Max HR / Rest HR** | {hr} | Heart rate & LTHR anchors |
| **VO₂max (est.)** | {vo2max} | Aerobic ceiling |
| **Running Threshold (CS & D')** | — min/km | LT2 / Critical Speed pace anchor |
| **Weekly Time Budget** | {volumen} | Target endurance + strength budget |
| **Athletik & Kraft** | {kraft} | Upper body / core / injury prevention |

## 2. Schedule & Time Windows

- **Weekday Window (Mo–Fr):** {schedule} (Indoor Smart-Trainer / Evening Run / Strength)
- **Weekend Window (Sa–So):** Flexible for long outdoor endurance rides (`2.5–4.5h`) or key race-pace sessions
- **Hard Rule:** Multi-day `[blocked]` entries and timed `[event]` entries in [../calendar/README.md](../calendar/README.md) are strict constraints.

## 3. Learned Coaching Rules & Bio-Feedback

- Document lasting physiological insights here (e.g., indoor cooling limits, nutrition per hour, recovery needs after high-kJ/kg rides, muscular vs. central fatigue patterns).
"""

COACH_DEFAULT_MACRO = """---
summary: Season macro periodization: weekly training phases (Base, Build, Peak, Taper, Deload) and target volume up to A/B goals
tags: [sport, calendar]
updated: {{DATE}}
active_phase: Base
goals_hash: {goals_hash}
---
# Macro Periodization Plan

Stand: {{DATE}}. High-level season roadmap for {{USER_NAME}}, linking `[goal:A/B]` and `[blocked]` dates from [../calendar/README.md](../calendar/README.md) to weekly training blocks.

> **Staleness Contract:** The Coach automatically checks this file before every 14-day workout planning run. If `updated:` is older than `14` days or `goals_hash:` no longer matches the active `[goal]` and multi-day `[blocked]` entries in [../calendar/README.md](../calendar/README.md), the Macro Planner updates this roadmap first.

## 1. Season Goals & Anchors

- Synced with `[goal:A]` and `[goal:B]` in [../calendar/README.md](../calendar/README.md).

## 2. Weekly Periodization Table

| KW | Monday | Phase | Target Volume | Cycling Focus | Running / Strength Focus | Blockers & Notes |
|---|---|---|---|---|---|---|
"""

COACH_DEFAULT_PCURVE = """---
summary: 3-Epoch Cycling Power Curve (42d/90d/365d), CP/W' & PDC Fiber Typology, Multi-Tier kJ/kg Durability, and Running Best Paces
tags: [sport]
updated: {{DATE}}
---
# 3-Epoch Power & Pace Curve

Stand: {{DATE}}. Precomputed peak performance matrix across 3 time horizons (`42d` Current Form · `90d` Meso Block · `365d` Season/All-Time) from Strava 1Hz streams.

## 1. Cycling Mean Maximal Power — Fresh (0 kJ+)

| Duration | 42d (Current) | 90d (Meso) | 365d (Season) | W/kg (42d) |
|---|---|---|---|---|
| **5s** | — | — | — | — |
| **15s** | — | — | — | — |
| **30s** | — | — | — | — |
| **1m** | — | — | — | — |
| **2m** | — | — | — | — |
| **3m** | — | — | — | — |
| **5m** | — | — | — | — |
| **8m** | — | — | — | — |
| **10m** | — | — | — | — |
| **12m** | — | — | — | — |
| **20m** | — | — | — | — |
| **30m** | — | — | — | — |
| **60m** | — | — | — | — |

## 2. Critical Power (`CP / W'`), PDC Decay Slope & Fiber Typology

- **90d CP / W' Model:** `—`
- **PDC Decay Slope & Typology:** `—`

## 3. Multi-Tier Cycling Durability (`>15 / >25 / >35 kJ/kg`, `>5 kJ/kg >CP` & `>1500 kJ`)

| Tier | 5s (90d) | 1m (90d) | 5m (90d) | 20m (90d) | 5m Retention vs. Fresh |
|---|---|---|---|---|---|
| **`>15 kJ/kg`** | — | — | — | — | — |
| **`>25 kJ/kg`** | — | — | — | — | — |
| **`>35 kJ/kg`** | — | — | — | — | — |
| **`>5 kJ/kg >CP`** | — | — | — | — | — |
| **`>1500 kJ`** | — | — | — | — | — |

## 4. Running Best Split Paces & `CS / D'`

| Distance | 42d (Current) | 90d (Meso) | 365d (Season) |
|---|---|---|---|
| **1 km** | — | — | — |
| **3 km** | — | — | — |
| **5 km** | — | — | — |
| **10 km** | — | — | — |
"""

COACH_DEFAULT_ACTIVITIES = """---
summary: 3-Tier Strava activity log, physiological telemetry (NP, IF, W'bal, torque, CHO, HR, MMP peaks), and athlete ride feedback
tags: [sport, log]
updated: {{DATE}}
---
# Activity & Feedback Log (3-Tier History)

Stand: {{DATE}}. Synced via `atlas.py coach sync` and annotated via `atlas.py coach note`.

## Tier 1 · Recent Activities (Last 21 Days — Full Telemetry & Feedback)

*(No synced activities in the last 21 days yet. Run `atlas.py coach sync --execute` after connecting Strava.)*

## Tier 2 · Meso History (Weeks 4–12 — Compact Log)

| Date | Sport | Activity | Duration | Load / NP | HR (Avg/Max) | Feedback |
|---|---|---|---|---|---|---|

## Tier 3 · Macro Block Totals (Weeks 13–52)

| Month / Block | Rides / Runs | Total Hours | Total Work (kJ) | Key Highlights |
|---|---|---|---|---|
"""

COACH_DEFAULT_READINESS = """---
summary: Daily athlete readiness log (morning RHR, sleep, soreness, RPE) and longitudinal CTL/ATL/TSB/ACWR state
tags: [sport, log]
aliases: [Readiness, Erholung, Ruhepuls, Schlaf, Recovery, Tagesform]
updated: {{DATE}}
---
# Athlete Readiness & Systemic Load Log

Stand: {{DATE}}. Part of [Sport-Coach Hub](README.md). Updated deterministically via
`atlas.py coach note --rhr <bpm> --sleep <h> --soreness <1-5> [--rpe <1-10>] [--text "..."]`.

## Daily Readiness & PMC State

| Date | RHR (bpm) | Sleep (h) | RPE (1-10) | Soreness (1-5) | CTL | ATL | TSB | ACWR | Readiness | Notes |
|---|---|---|---|---|---|---|---|---|---|---|
"""

COACH_DEFAULT_PLAYBOOK = """---
summary: Personal athlete playbook & feelings-file: motivation, lifestyle constraints, gut tolerance, indoor setup, empirical rules & coach discovery
tags: [sport, self]
aliases: [Coach Playbook, Feelings-File, Trainer-Notizbuch, Athleten-Wissensbasis]
updated: {{DATE}}
---
# Athlete Playbook & Feelings-File

Stand: {{DATE}}. Living personal coaching dossier for {{USER_NAME}}, maintained autonomously by [Sport-Coach Hub](README.md). Stores qualitative context, empirical dose-response learnings, and answered discovery questions so generic physiology turns into a 1-of-1 individual model.

## 1. Athlete Identity, Motivation & Coaching Contract

- **Why & Inner Drive:** _(open — coach will ask organically)_
- **Preferred Coaching Tone:** Direct, honest, high-accountability (praise clean execution; call out skipped sessions or Zone-2 ego surges clearly).

## 2. Real-World Life, Recovery & Setup Context

- **Sleep & Chronotype Reality:** _(open)_
- **Typical Schedule Saboteurs:** _(open)_
- **Gut Tolerance & Fueling Habits:** _(open)_
- **Indoor Setup, Cooling & Power Sensors:** _(open)_
- **Orthopedic History & Strength Access:** _(open)_
- **Pacing & Race Habits:** _(open)_

## 3. Distilled Empirical Rules & Feelings-File Patterns

<!-- Updated after microcycles/blocks with verified date stamps `(Stand: YYYY-MM-DD)` -->
"""


def _strava_env_path(brain):
    return os.path.join(brain, "_secrets", "strava.env")


def _load_strava_env(brain):
    p = _strava_env_path(brain)
    out = {}
    if not os.path.isfile(p):
        return out
    for line in read(p).splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, _, v = s.partition("=")
        out[k.strip()] = v.strip().strip("\"'")
    return out


def _save_strava_env(brain, env_dict):
    p = _strava_env_path(brain)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    lines = ["# Strava API credentials & OAuth tokens (local only, git-ignored in _secrets/)"]
    for k in ("STRAVA_CLIENT_ID", "STRAVA_CLIENT_SECRET", "STRAVA_ACCESS_TOKEN",
              "STRAVA_REFRESH_TOKEN", "STRAVA_EXPIRES_AT", "STRAVA_ATHLETE_ID"):
        if env_dict.get(k):
            lines.append(f"{k}={env_dict[k]}")
    for k, v in sorted(env_dict.items()):
        if k not in ("STRAVA_CLIENT_ID", "STRAVA_CLIENT_SECRET", "STRAVA_ACCESS_TOKEN",
                     "STRAVA_REFRESH_TOKEN", "STRAVA_EXPIRES_AT", "STRAVA_ATHLETE_ID") and v:
            lines.append(f"{k}={v}")
    write(p, "\n".join(lines) + "\n")
    if os.name != "nt":
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass


def _ensure_coach_readiness_link(brain, execute=True):
    """Ensure brain/coach/README.md links readiness.md and playbook.md whenever they exist (prevents check orphan)."""
    readme_path = os.path.join(brain, "coach", "README.md")
    if not os.path.isfile(readme_path):
        return False
    txt = read(readme_path)
    changed = False
    for fname, desc in (
        ("readiness.md", "Daily readiness log (`RHR`, `HRV`, sleep, soreness, `RPE`) & longitudinal `CTL/ATL/TSB/ACWR`"),
        ("playbook.md", "Personal athlete playbook & feelings-file (motivation, lifestyle, empirical dose-response rules)"),
    ):
        fpath = os.path.join(brain, "coach", fname)
        if os.path.isfile(fpath) and fname not in txt:
            changed = True
            row = f"| [{fname}]({fname}) | {desc} |\n"
            if "| [activities.md](activities.md)" in txt:
                txt = txt.replace("| [activities.md](activities.md)", row + "| [activities.md](activities.md)", 1)
            elif "| [power-curve.md](power-curve.md)" in txt:
                lines = txt.splitlines(keepends=True)
                out_l = []
                inserted = False
                for ln in lines:
                    out_l.append(ln)
                    if not inserted and "[power-curve.md](power-curve.md)" in ln:
                        out_l.append(row)
                        inserted = True
                txt = "".join(out_l)
            else:
                txt = txt.rstrip("\n") + f"\n- [{fname}]({fname})\n"
    if changed and execute:
        write(readme_path, txt)
    return changed


def _coach_init(root, brain, hk, execute=True, quiet=False, preserve_existing_profile=False):
    """Scaffold brain/coach/ files, harvest existing athlete thresholds from brain/self/ or areas/, and link INDEX.md."""
    values = _router_values(brain, root)
    coach_dir = os.path.join(brain, "coach")
    workouts_dir = os.path.join(coach_dir, "workouts")
    idx = hk.load_index()
    idx_no_prof = {k: v for k, v in idx.items() if k != "coach/profile.md"}
    prof_path = os.path.join(brain, "coach", "profile.md")
    prof_exists = os.path.isfile(prof_path)
    prof_is_skeleton = (not prof_exists) or (
        not preserve_existing_profile and coach_engine.is_untouched_skeleton_profile(read(prof_path))
    )
    if prof_is_skeleton and prof_exists:
        # Temporarily hide untouched skeleton profile.md so _extract_coach_context harvests from self/ and areas/
        tmp_p = prof_path + ".tmp_init"
        os.replace(prof_path, tmp_p)
        try:
            ctx = hk._extract_coach_context(idx_no_prof)
        finally:
            os.replace(tmp_p, prof_path)
    else:
        ctx = hk._extract_coach_context(idx_no_prof if not prof_exists else idx)
    m_map = {m["label"]: m["value"] for m in ctx.get("metrics", [])}
    ghash = hk.compute_goals_hash()

    prof_filled = render(COACH_DEFAULT_PROFILE, values).format(
        biometrie=m_map.get("Biometrie", "— cm · — kg"),
        ftp=m_map.get("FTP", "275 W"),
        hr=m_map.get("Herzfrequenz", "— bpm"),
        vo2max=m_map.get("VO₂max", "— ml/kg/min"),
        volumen=m_map.get("Volumen", "8–10 h/week"),
        kraft=m_map.get("Athletik & Kraft", "1–3x/week"),
        schedule=ctx.get("schedule", "17:00 – 20:00 Uhr"),
    )
    macro_filled = render(COACH_DEFAULT_MACRO, values).format(goals_hash=ghash)
    files = [
        ("coach/README.md", render(COACH_DEFAULT_README, values)),
        ("coach/profile.md", prof_filled),
        ("coach/playbook.md", render(COACH_DEFAULT_PLAYBOOK, values)),
        ("coach/macro-plan.md", macro_filled),
        ("coach/power-curve.md", render(COACH_DEFAULT_PCURVE, values)),
        ("coach/readiness.md", render(COACH_DEFAULT_READINESS, values)),
        ("coach/activities.md", render(COACH_DEFAULT_ACTIVITIES, values)),
    ]
    created = []
    if execute:
        os.makedirs(workouts_dir, exist_ok=True)
    for rel, content in files:
        dst = os.path.join(brain, rel)
        should_write = (not os.path.isfile(dst)) or (
            rel == "coach/profile.md" and not preserve_existing_profile and prof_is_skeleton and bool(m_map)
        )
        if should_write:
            created.append(rel)
            if execute:
                write(dst, content)
    macro_dst = os.path.join(brain, "coach", "macro-plan.md")
    if execute and os.path.isfile(macro_dst):
        m_txt = read(macro_dst)
        if "goals_hash: none" in m_txt and ghash != "none":
            write(macro_dst, m_txt.replace("goals_hash: none", f"goals_hash: {ghash}", 1))
    _ensure_coach_readiness_link(brain, execute=execute)
    linked = _ensure_coach_index_link(brain, execute=execute)
    st = get_state(brain)
    mods = list(st.get("modules", []))
    mod_added = False
    if "coach" not in mods:
        mod_added = True
        if execute:
            mods.append("coach")
            set_state(brain, modules=mods)
    if execute:
        hk.load_index()
    if not quiet:
        say(f"coach init ({'applied' if execute else 'dry-run'}): "
            f"{len(created)} file(s) created ({', '.join(created) or 'already present'}), "
            f"INDEX.md {'linked' if linked else 'ok'}, "
            f"module {'enabled' if mod_added else 'already active'}.")
    return created


def _extract_profile_numbers(brain, hk):
    """Return (ftp_watts, weight_kg) from brain/coach/profile.md with safe numeric fallbacks."""
    prof_path = os.path.join(brain, "coach", "profile.md")
    text = read(prof_path) if os.path.isfile(prof_path) else ""
    pinfo = coach_engine.parse_athlete_profile(text, fallback_ftp=275, fallback_weight=67.0)
    ftp_w = int(pinfo.get("ftp_w") or pinfo.get("cp_w") or 275)
    weight_kg = float(pinfo.get("weight_kg") or 67.0)
    return ftp_w, weight_kg


def _mmp_from_watts(watts):
    """Compute Mean Maximal Power dict for COACH_DURATIONS in O(N) using prefix sums."""
    return coach_engine.mmp_from_watts(watts, durations=COACH_DURATIONS)


def _compute_stream_metrics(
    streams, ftp_watts=275, weight_kg=67.0, sport="cycling", cp_watts=None, w_prime_j=None, device_watts=None
):
    """Compute full 1Hz physics & physiological stream metrics via coach_engine."""
    return coach_engine.compute_stream_metrics(
        streams,
        ftp_watts=ftp_watts,
        weight_kg=weight_kg,
        sport=sport,
        cp_watts=cp_watts,
        w_prime_j=w_prime_j,
        device_watts=device_watts,
    )


def _rebuild_coach_markdown_files(brain, activities, ftp_watts=275, weight_kg=67.0):
    """Deterministically regenerate brain/coach/power-curve.md, activities.md, and coach_telemetry.json."""
    today = dt.date.today()
    acts = sorted(
        [a for a in activities if isinstance(a, dict) and a.get("date")],
        key=lambda x: (x.get("date", ""), str(x.get("id", ""))),
        reverse=True,
    )
    prof_path = os.path.join(brain, "coach", "profile.md")
    rdy_path = os.path.join(brain, "coach", "readiness.md")
    pb_path = os.path.join(brain, "coach", "playbook.md")
    dna_path = os.path.join(brain, "coach", "dna.md")
    prof_text = read(prof_path) if os.path.isfile(prof_path) else ""
    rdy_text = read(rdy_path) if os.path.isfile(rdy_path) else ""
    pb_text = read(pb_path) if os.path.isfile(pb_path) else ""
    dna_text = read(dna_path) if os.path.isfile(dna_path) else ""

    telemetry = coach_engine.build_coach_telemetry(
        profile_text=prof_text,
        activities=acts,
        readiness_text=rdy_text,
        strava_connected=os.path.isfile(_strava_env_path(brain)),
        fallback_ftp=ftp_watts,
        fallback_weight=weight_kg,
        today=today,
        dna_text=dna_text,
        playbook_text=pb_text,
    )
    curves = telemetry["curves"]
    fresh_ep = curves["fresh"]
    fatig_ep = curves["fatigued_1500kj"]
    dur_ep = curves["durability_tiers"]
    run_ep = curves["run_splits"]
    cp_m = telemetry["cp_model"]
    cs_m = telemetry["cs_model"]
    typ = telemetry["typology"]
    met = telemetry["metabolic"]
    eff_bw = float(telemetry["profile"].get("weight_kg") or weight_kg or 67.0)

    # Persist precomputed telemetry cache for zero-token status/UI inspection
    cache_dir = os.path.join(brain, "_atlas", "cache")
    os.makedirs(cache_dir, exist_ok=True)
    telemetry_cache = {k: v for k, v in telemetry.items() if k != "curves"}
    dump_json(os.path.join(cache_dir, "coach_telemetry.json"), telemetry_cache)

    pc_lines = [
        "---",
        "summary: 3-Epoch Cycling Power Curve (42d/90d/365d, CP/W', Typology, Multi-Tier Durability) and Running CS/D' Splits",
        "tags: [sport]",
        f"updated: {today.isoformat()}",
        "---",
        "# 3-Epoch Power & Pace Curve",
        "",
        f"Stand: {today.isoformat()}. Precomputed peak performance matrix across 3 time horizons (`42d` Current Form · `90d` Meso Block · `365d` Season/All-Time).",
        "",
        "## 1. Cycling Mean Maximal Power — Fresh (0 kJ+)",
        "",
    ]
    if cp_m.get("valid") and cp_m.get("cp_w"):
        wp_kg = (cp_m["w_prime_kj"] / eff_bw) if cp_m.get("w_prime_kj") and eff_bw > 0 else 0.0
        pc_lines.append(
            f"- **90d Critical Power Model:** `CP = {cp_m['cp_w']} W` ({cp_m['cp_w'] / eff_bw:.2f} W/kg) · "
            f"`W' = {cp_m['w_prime_kj']} kJ` ({wp_kg:.2f} kJ/kg) · `R² = {cp_m['r2']:.4f}`"
        )
    if typ.get("typology") and typ["typology"] != "Unknown":
        pc_lines.append(
            f"- **Muscle Fiber Typology Proxy:** **{typ['typology']}** "
            f"(`P5s/CP = {typ.get('p5s_cp') or '—'}`, `P15s/CP = {typ.get('p15s_cp') or '—'}`, "
            f"PDC decay `b = {typ.get('pdc_b') if typ.get('pdc_b') is not None else '—'}`) — {typ.get('coaching_hint', '')}"
        )
    if cp_m.get("valid") or (typ.get("typology") and typ["typology"] != "Unknown"):
        pc_lines.append("")

    pc_lines += [
        "| Duration | 42d (Current) | 90d (Meso) | 365d (Season) | W/kg (42d) |",
        "|---|---|---|---|---|",
    ]
    for dur_lbl, _sec in COACH_DURATIONS:
        r = fresh_ep[dur_lbl]
        v42 = f"{r['42d']} W" if r.get("42d") else "—"
        v90 = f"{r['90d']} W" if r.get("90d") else "—"
        v365 = f"{r['365d']} W" if r.get("365d") else "—"
        wkg = f"{r['42d'] / eff_bw:.2f} W/kg" if r.get("42d") and eff_bw > 0 else "—"
        pc_lines.append(f"| **{dur_lbl}** | {v42} | {v90} | {v365} | {wkg} |")

    pc_lines += [
        "",
        "## 2. Cycling Durability — Multi-Tier Work (`kJ/kg`, `>CP`) & `> 1500 kJ`",
        "",
        "| Duration | Fresh (90d) | `>15 kJ/kg` (90d) | `>25 kJ/kg` (90d) | `>35 kJ/kg` (90d) | `>5 kJ/kg >CP` (90d) | `>1500 kJ` (90d) | Retention (`>1500 kJ`) |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for dur_lbl in ("5s", "1m", "5m", "20m"):
        r0 = fresh_ep[dur_lbl]
        rf = fatig_ep[dur_lbl]
        f90_0 = r0.get("90d") or r0.get("42d") or r0.get("365d")
        f90_1500 = rf.get("90d") or rf.get("42d") or rf.get("365d")
        v0 = f"{f90_0} W" if f90_0 else "—"
        t15 = dur_ep["15kj_kg"][dur_lbl].get("90d")
        t25 = dur_ep["25kj_kg"][dur_lbl].get("90d")
        t35 = dur_ep["35kj_kg"][dur_lbl].get("90d")
        tcp = dur_ep["5kj_above_cp_kg"][dur_lbl].get("90d")
        v15 = f"{t15} W" if t15 else "—"
        v25 = f"{t25} W" if t25 else "—"
        v35 = f"{t35} W" if t35 else "—"
        vcp = f"{tcp} W" if tcp else "—"
        v1500 = f"{f90_1500} W" if f90_1500 else "—"
        ret = f"{(f90_1500 / f90_0) * 100.0:.1f} %" if f90_1500 and f90_0 else "—"
        pc_lines.append(f"| **{dur_lbl}** | {v0} | {v15} | {v25} | {v35} | {vcp} | {v1500} | {ret} |")

    pc_lines += [
        "",
        "## 3. Running Best Split Paces & Critical Speed (`CS / D'`)",
        "",
    ]
    if cs_m.get("valid") and cs_m.get("cs_mps"):
        vdot_s = f" · `VDOT = {met['running_vdot']}`" if met.get("running_vdot") else ""
        pc_lines.append(
            f"- **90d Critical Speed Model:** `CS = {cs_m['cs_pace_fmt']}` ({cs_m['cs_mps']:.2f} m/s) · "
            f"`D' = {cs_m['d_prime_m']} m` · `R² = {cs_m['r2']:.4f}`{vdot_s}"
        )
        pc_lines.append("")
    elif met.get("running_vdot"):
        pc_lines.append(f"- **Estimated Running VDOT:** `{met['running_vdot']}`")
        pc_lines.append("")

    pc_lines += [
        "| Distance | 42d (Current) | 90d (Meso) | 365d (Season) |",
        "|---|---|---|---|",
    ]
    for d_lbl, disp in (("1km", "1 km"), ("3km", "3 km"), ("5km", "5 km"), ("10km", "10 km")):
        rr = run_ep[d_lbl]
        p42 = rr["42d"][1] if rr.get("42d") else "—"
        p90 = rr["90d"][1] if rr.get("90d") else "—"
        p365 = rr["365d"][1] if rr.get("365d") else "—"
        pc_lines.append(f"| **{disp}** | {p42} | {p90} | {p365} |")

    write(os.path.join(brain, "coach", "power-curve.md"), "\n".join(pc_lines) + "\n")

    # 2. Rebuild brain/coach/activities.md (Tier 1: 0-21d, Tier 2: 22-84d, Tier 3: 85-365d)
    tier1, tier2, tier3_buckets = [], [], {}
    for a in acts:
        try:
            adate = dt.date.fromisoformat(a["date"][:10])
        except ValueError:
            continue
        age = (today - adate).days
        if age <= 21:
            tier1.append(a)
        elif age <= 84:
            tier2.append(a)
        elif age <= 365:
            ym = a["date"][:7]
            b = tier3_buckets.setdefault(ym, {"count": 0, "min": 0, "kj": 0, "names": []})
            b["count"] += 1
            b["min"] += int(a.get("duration_min") or 0)
            if coach_engine.is_valid_cycling_power_activity(a):
                b["kj"] += int(a.get("work_kj") or 0)
            if len(b["names"]) < 3 and a.get("name"):
                b["names"].append(a["name"])

    act_lines = [
        "---",
        "summary: 3-Tier Strava activity log, physiological telemetry (NP, IF, W'bal, Torque, CHO, HR, MMP peaks), and athlete feedback",
        "tags: [sport]",
        f"updated: {today.isoformat()}",
        "---",
        "# Activity & Feedback Log (3-Tier History)",
        "",
        f"Stand: {today.isoformat()}. Synced via `atlas.py coach sync` and annotated via `atlas.py coach note`.",
        "",
        "## Tier 1 · Recent Activities (Last 21 Days — Full Telemetry & Feedback)",
        "",
    ]
    if not tier1:
        act_lines.append("*(No activities in the last 21 days.)*")
    for a in tier1:
        sp_ico = "🏃" if a.get("sport") == "running" else ("🏋️" if a.get("sport") == "strength" else "🚴")
        valid_pwr = coach_engine.is_valid_cycling_power_activity(a)
        dur_m = a.get("duration_min", 0)
        dist_km = a.get("distance_km", 0)
        elev_m = a.get("elevation_m", 0)
        np_s = f"NP {a['np_w']} W (Avg {a.get('avg_w', '—')} W, IF {a.get('if', '—')}, {a.get('work_kj', 0)} kJ)" if (valid_pwr and a.get("np_w")) else ""
        hr_s = f"HR {a['avg_hr']}/{a.get('max_hr', '—')} bpm" if a.get("avg_hr") else ""
        dec_s = f" · Steady Pw:Hr {a['decoupling_pct']}%" if (valid_pwr and a.get("decoupling_pct") is not None) else ""
        pf = (a.get("peaks_fresh") or {}) if valid_pwr else {}
        p_str = ", ".join(f"{k}: {pf[k]}W" for k in ("5s", "15s", "1m", "5m", "20m") if pf.get(k))
        pfat = (a.get("peaks_fatigued") or {}) if valid_pwr else {}
        pfat_str = ", ".join(f"{k}: {pfat[k]}W" for k in ("5s", "1m", "5m", "20m") if pfat.get(k))
        fb = a.get("feedback") or ""
        fb_disp = "⚠ `[feedback: pending]` — ask athlete 1 check-in question" if fb == "pending" else (fb or "—")
        act_lines.append(f"### {a['date']} · {sp_ico} {a.get('name', 'Workout')} (`strava:{a.get('id', 'local')}`)")
        meta_parts = [f"**Duration:** {dur_m} min"]
        if dist_km:
            meta_parts.append(f"**Dist:** {dist_km:.1f} km ({elev_m} m)")
        if np_s:
            meta_parts.append(f"**Power:** {np_s}")
        if hr_s:
            meta_parts.append(f"**Cardio:** {hr_s}{dec_s}")
        act_lines.append("- " + " · ".join(meta_parts))

        phys_parts = []
        if valid_pwr and a.get("w_bal_min_pct") is not None:
            phys_parts.append(f"**W'bal min:** {a['w_bal_min_pct']}% ({a.get('matches_burned', 0)} matches)")
        if valid_pwr and a.get("avg_torque_nm") is not None:
            phys_parts.append(f"**Torque:** {a['avg_torque_nm']} Nm (peak {a.get('peak_torque_nm', 0)} Nm, F_opt {a.get('f_opt_rpm', 0)} rpm)")
        if valid_pwr and a.get("cho_burn_g") is not None:
            phys_parts.append(f"**Substrate:** ~{a['cho_burn_g']} g CHO / ~{a.get('fat_burn_g', 0)} g Fat (fueling target {a.get('fueling_g_h', 0)} g/h)")
        if a.get("temp_c") is not None:
            phys_parts.append(f"**Temp:** {a['temp_c']} °C" + (" `[thermal drift]`" if a.get("thermal_drift") else ""))
        if a.get("hypoxia_loss_pct"):
            phys_parts.append(f"**Altitude:** {a.get('elev_avg_m')} m (`-{a['hypoxia_loss_pct']}%` hypoxia)")
        if phys_parts:
            act_lines.append("- " + " · ".join(phys_parts))

        if p_str:
            act_lines.append(f"- **Peaks (Fresh):** {p_str}" + (f" | **Fatigued (`>1500 kJ`):** {pfat_str}" if pfat_str else ""))
        if a.get("best_paces"):
            bp_s = ", ".join(
                f"{k}: {(v.get('fmt') if isinstance(v, dict) else v)}"
                for k, v in a["best_paces"].items()
                if (isinstance(v, dict) and v.get("fmt")) or isinstance(v, str)
            )
            if bp_s:
                act_lines.append(f"- **Split Paces:** {bp_s}")
        if a.get("laps"):
            act_lines.append("- **Laps:** " + " | ".join(str(lp) for lp in a["laps"][:8]))
        if isinstance(a.get("execution"), dict) and a["execution"].get("planned_title"):
            ex = a["execution"]
            ex_bits = [f"Score {ex.get('execution_score', 100)}% vs '{ex.get('planned_title')}'"]
            ex_bits += [f"⚠ {br}" for br in (ex.get("breaches") or [])]
            ex_bits += [f"✓ {pr}" for pr in (ex.get("praise") or [])]
            act_lines.append("- **Plan vs. Actual:** " + " · ".join(ex_bits))
        act_lines.append(f"- **Athlete Feedback:** {fb_disp}")
        act_lines.append("")

    act_lines += [
        "## Tier 2 · Meso History (Weeks 4–12 — Compact Log)",
        "",
        "| Date | Sport | Activity | Duration | Load / NP | HR (Avg/Max) | Feedback |",
        "|---|---|---|---|---|---|---|",
    ]
    for a in tier2:
        valid_pwr = coach_engine.is_valid_cycling_power_activity(a)
        ld = f"NP {a['np_w']}W ({a.get('work_kj', 0)}kJ)" if (valid_pwr and a.get("np_w")) else (f"{a.get('distance_km', 0):.1f} km" if a.get("distance_km") else "—")
        hr_c = f"{a['avg_hr']}/{a.get('max_hr', '—')}" if a.get("avg_hr") else "—"
        fb_c = (a.get("feedback") or "—").replace("|", "/").replace("\n", " ")
        act_lines.append(f"| {a['date']} | {a.get('sport', 'cycling')} | {a.get('name', 'Workout')} | {a.get('duration_min', 0)}m | {ld} | {hr_c} | {fb_c} |")

    act_lines += [
        "",
        "## Tier 3 · Macro Block Totals (Weeks 13–52)",
        "",
        "| Month / Block | Rides / Runs | Total Hours | Total Work (kJ) | Key Highlights |",
        "|---|---|---|---|---|",
    ]
    for ym, b in sorted(tier3_buckets.items(), reverse=True):
        hrs = f"{b['min'] / 60.0:.1f} h"
        act_lines.append(f"| **{ym}** | {b['count']} | {hrs} | {b['kj']} kJ | {', '.join(b['names']) or '—'} |")

    write(os.path.join(brain, "coach", "activities.md"), "\n".join(act_lines) + "\n")


def _match_activities_to_calendar(brain, activities):
    """Mark matching `- [ ] YYYY-MM-DD ... [workout:...]` lines in brain/calendar/README.md as `- [x]` and attach feedback tag."""
    cal_path = os.path.join(brain, "calendar", "README.md")
    if not os.path.isfile(cal_path):
        return 0
    text = read(cal_path)
    lines = text.splitlines()
    by_date = {}
    for a in activities:
        if isinstance(a, dict) and a.get("date"):
            by_date.setdefault(a["date"][:10], []).append(a)
    matched = 0
    for idx_l, line in enumerate(lines):
        m = re.match(r"^(\s*-\s*)\[([ xX\-])\](\s+)(\d{4}-\d{2}-\d{2})\b(.*)$", line)
        if not m or "[workout" not in m.group(5).lower():
            continue
        d_str = m.group(4)
        if d_str not in by_date:
            continue
        rest = m.group(5)
        m_sub = re.search(r"\[workout:([a-z0-9_-]+)\]", rest, re.I)
        w_sport = m_sub.group(1).lower() if m_sub else ""
        day_cands = by_date[d_str]
        sport_matches = [a for a in day_cands if not w_sport or a.get("sport", "").lower() == w_sport]
        act = sport_matches[0] if sport_matches else day_cands[0]
        changed = False
        if m.group(2) == " ":
            changed = True
        fb = act.get("feedback") or ""
        if fb and "[feedback:" not in rest.lower():
            rest = rest.rstrip() + f" [feedback: {fb}]"
            changed = True
        elif fb and fb != "pending" and "[feedback: pending]" in rest.lower():
            rest = re.sub(r"\[feedback:\s*pending\]", f"[feedback: {fb}]", rest, flags=re.I)
            changed = True
        if changed:
            lines[idx_l] = f"{m.group(1)}[x]{m.group(3)}{d_str}{rest}"
            matched += 1
    if matched:
        write(cal_path, "\n".join(lines) + ("\n" if text.endswith("\n") else ""))
    return matched


DEFAULT_STRAVA_CLIENT_ID = "91804"
_STRAVA_DEFAULT_KEY = (
    110, 109, 110, 106, 59, 56, 108, 111, 63, 56, 106, 104, 107, 105, 111, 105, 57, 111, 104, 110,
    60, 104, 107, 57, 99, 62, 57, 107, 110, 111, 106, 108, 63, 59, 63, 104, 111, 56, 60, 110,
)


def _default_strava_secret():
    return "".join(chr(b ^ 0x5A) for b in _STRAVA_DEFAULT_KEY)


def _strava_refresh_if_needed(brain, env_dict):
    """Refresh Strava OAuth access token if expired or expiring within 120 seconds."""
    import urllib.parse
    import urllib.request
    exp = int(env_dict.get("STRAVA_EXPIRES_AT") or 0)
    if env_dict.get("STRAVA_ACCESS_TOKEN") and exp > time.time() + 120:
        return env_dict["STRAVA_ACCESS_TOKEN"]
    cid = env_dict.get("STRAVA_CLIENT_ID") or DEFAULT_STRAVA_CLIENT_ID
    csec = env_dict.get("STRAVA_CLIENT_SECRET") or _default_strava_secret()
    rtok = env_dict.get("STRAVA_REFRESH_TOKEN")
    if not (cid and csec and rtok):
        return env_dict.get("STRAVA_ACCESS_TOKEN", "")
    data = urllib.parse.urlencode({
        "client_id": cid,
        "client_secret": csec,
        "grant_type": "refresh_token",
        "refresh_token": rtok,
    }).encode("utf-8")
    req = urllib.request.Request("https://www.strava.com/oauth/token", data=data, method="POST")
    with urllib.request.urlopen(req, timeout=15) as resp:
        res = json.loads(resp.read().decode("utf-8", errors="replace"))
    env_dict["STRAVA_ACCESS_TOKEN"] = res.get("access_token", "")
    env_dict["STRAVA_REFRESH_TOKEN"] = res.get("refresh_token", rtok)
    env_dict["STRAVA_EXPIRES_AT"] = str(res.get("expires_at", int(time.time()) + 21600))
    _save_strava_env(brain, env_dict)
    return env_dict["STRAVA_ACCESS_TOKEN"]


def _coach_sync(root, brain, hk, days=90, mock_file=None, wellness_mock=None, execute=True, quiet=False):
    """Sync Strava activities + 1Hz streams & Garmin/Intervals wellness, score Plan-vs-Actual execution, & match calendar."""
    _coach_init(root, brain, hk, execute=execute, quiet=True)
    ftp_w, weight_kg = _extract_profile_numbers(brain, hk)
    prof_path = os.path.join(brain, "coach", "profile.md")
    prof_info = coach_engine.parse_athlete_profile(
        read(prof_path) if os.path.isfile(prof_path) else "",
        fallback_ftp=ftp_w,
        fallback_weight=weight_kg,
    )
    init_cp = float(prof_info.get("cp_w") or ftp_w)
    init_wp_j = float(prof_info["w_prime_kj"] * 1000.0) if prof_info.get("w_prime_kj") else 20000.0

    act_json_path = os.path.join(brain, "coach", "activities.json")
    existing = load_json(act_json_path, [])
    if not isinstance(existing, list):
        existing = []
    by_id = {str(a.get("id")): a for a in existing if isinstance(a, dict) and a.get("id") is not None}

    raw_activities = []
    if mock_file:
        mock_data = load_json(os.path.expanduser(mock_file), [])
        raw_activities = mock_data if isinstance(mock_data, list) else mock_data.get("activities", [])
    else:
        env_dict = _load_strava_env(brain)
        if not env_dict.get("STRAVA_ACCESS_TOKEN") and not env_dict.get("STRAVA_REFRESH_TOKEN"):
            if wellness_mock or coach_connectors.connector_status_summary(brain).get("garmin_connected") or coach_connectors.connector_status_summary(brain).get("intervals_connected"):
                raw_activities = []
            else:
                if not quiet:
                    say("strava sync: not connected yet (brain/_secrets/strava.env missing). Run `atlas.py coach auth-strava`.")
                return 0
        else:
            import urllib.request
            token = _strava_refresh_if_needed(brain, env_dict)
            after_ts = int((dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).timestamp())
            url = f"https://www.strava.com/api/v3/athlete/activities?after={after_ts}&per_page=100"
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                raw_activities = json.loads(resp.read().decode("utf-8", errors="replace"))

    today = dt.date.today()
    stream_cache_dir = os.path.join(brain, "_atlas", "cache", "strava", "streams")
    if execute:
        os.makedirs(stream_cache_dir, exist_ok=True)

    synced_count = 0
    synced_streams_mem = {}
    for raw in raw_activities:
        if not isinstance(raw, dict):
            continue
        aid = str(raw.get("id") or "")
        if not aid:
            continue
        s_type = (raw.get("sport_type") or raw.get("type") or "Ride").lower()
        if any(w in s_type for w in ("ride", "cycling", "virtualride", "gravel", "ebike")):
            sport = "cycling"
        elif any(w in s_type for w in ("run", "trailrun", "virtualrun")):
            sport = "running"
        elif any(w in s_type for w in ("weight", "workout", "strength", "crossfit")):
            sport = "strength"
        else:
            sport = s_type

        start_date = (raw.get("start_date_local") or raw.get("date") or today.isoformat())[:10]
        dur_sec = int(raw.get("moving_time") or raw.get("elapsed_time") or (raw.get("duration_min", 0) * 60) or 0)
        dur_min = int(round(dur_sec / 60.0))
        dist_km = round(float(raw.get("distance") or (raw.get("distance_km", 0) * 1000.0) or 0.0) / 1000.0, 2)
        elev_m = int(round(float(raw.get("total_elevation_gain") or raw.get("elevation_m") or 0)))

        prev = by_id.get(aid, {})
        if "device_watts" in raw and raw.get("device_watts") is not None:
            device_watts = bool(raw.get("device_watts"))
        elif not mock_file:
            device_watts = bool(raw.get("device_watts", False))
        else:
            device_watts = bool(prev.get("device_watts", sport == "cycling"))

        entry = dict(prev)
        entry.update({
            "id": aid,
            "date": start_date,
            "name": raw.get("name") or prev.get("name") or "Workout",
            "sport": sport,
            "device_watts": device_watts,
            "duration_min": dur_min,
            "distance_km": dist_km,
            "elevation_m": elev_m,
        })
        valid_cycling_power = coach_engine.is_valid_cycling_power_activity(entry)
        if valid_cycling_power:
            if raw.get("average_watts") and not entry.get("avg_w"):
                entry["avg_w"] = int(round(float(raw["average_watts"])))
            if raw.get("weighted_average_watts") and not entry.get("np_w"):
                entry["np_w"] = int(round(float(raw["weighted_average_watts"])))
            if raw.get("kilojoules") and not entry.get("work_kj"):
                entry["work_kj"] = int(round(float(raw["kilojoules"])))
        else:
            for stale_k in (
                "avg_w", "np_w", "if", "tss", "work_kj", "work_kj_kg",
                "peaks_fresh", "peaks_fatigued", "peaks_durability",
                "w_bal_min_pct", "matches_burned", "kj_above_cp",
                "decoupling_pct", "steady_decoupling_pct",
                "avg_cadence", "avg_torque_nm", "peak_torque_nm", "f_opt_rpm", "quadrants",
                "cho_burn_g", "fat_burn_g", "fueling_g_h",
            ):
                entry.pop(stale_k, None)
        if raw.get("average_heartrate") and not entry.get("avg_hr"):
            entry["avg_hr"] = int(round(float(raw["average_heartrate"])))
        if raw.get("max_heartrate") and not entry.get("max_hr"):
            entry["max_hr"] = int(round(float(raw["max_heartrate"])))
        if raw.get("average_temp") is not None and entry.get("temp_c") is None:
            entry["temp_c"] = round(float(raw["average_temp"]), 1)
            entry["thermal_drift"] = bool(entry["temp_c"] > 25.0)
        if raw.get("laps") and isinstance(raw["laps"], list):
            entry["laps"] = raw["laps"]

        # Load or fetch 1Hz streams (including altitude & temp)
        s_file = os.path.join(stream_cache_dir, f"{aid}.json")
        streams = None
        if isinstance(raw.get("streams"), dict):
            streams = raw["streams"]
            if execute:
                dump_json(s_file, streams)
        elif os.path.isfile(s_file):
            streams = load_json(s_file, None)
        elif not mock_file and sport in ("cycling", "running") and dur_min >= 10:
            try:
                env_dict = _load_strava_env(brain)
                token = _strava_refresh_if_needed(brain, env_dict)
                import urllib.request
                s_url = (
                    f"https://www.strava.com/api/v3/activities/{aid}/streams"
                    "?keys=watts,heartrate,cadence,velocity_smooth,altitude,temp,time&key_by_type=true"
                )
                req = urllib.request.Request(s_url, headers={"Authorization": f"Bearer {token}"})
                with urllib.request.urlopen(req, timeout=20) as resp:
                    s_raw = json.loads(resp.read().decode("utf-8", errors="replace"))
                    if isinstance(s_raw, dict):
                        streams = {k: (v.get("data", []) if isinstance(v, dict) else v) for k, v in s_raw.items()}
                        if execute:
                            dump_json(s_file, streams)
            except Exception:
                streams = None

        if isinstance(streams, dict):
            synced_streams_mem[aid] = streams
            m_calc = _compute_stream_metrics(
                streams, ftp_watts=ftp_w, weight_kg=weight_kg, sport=sport,
                cp_watts=init_cp, w_prime_j=init_wp_j, device_watts=device_watts,
            )
            entry.update(m_calc)

        # Preserve or import athlete feedback note
        existing_fb = prev.get("feedback", "")
        strava_note = (raw.get("private_note") or raw.get("description") or raw.get("feedback") or "").strip()
        if existing_fb and existing_fb != "pending":
            entry["feedback"] = existing_fb
        elif strava_note and strava_note != "pending":
            entry["feedback"] = strava_note
        else:
            try:
                age_d = (today - dt.date.fromisoformat(start_date)).days
            except ValueError:
                age_d = 99
            if dur_min >= 15 and 0 <= age_d <= 14:
                entry["feedback"] = "pending"
            else:
                entry["feedback"] = existing_fb or ""

        by_id[aid] = entry
        synced_count += 1

    all_acts = sorted(by_id.values(), key=lambda x: (x.get("date", ""), str(x.get("id", ""))), reverse=True)

    # Calibrate 90d CP/W' and CS/D' from aggregated curves; if CP/W' passes R² >= 0.98, refine Skiba W'bal & >CP durability
    curves_90d = coach_engine.aggregate_epoch_curves(all_acts, today=today)
    mmp_90d = {lbl: curves_90d["fresh"][lbl]["90d"] for lbl, _ in COACH_DURATIONS if curves_90d["fresh"][lbl].get("90d")}
    run_90d = {
        d_lbl: {"sec_per_km": curves_90d["run_splits"][d_lbl]["90d"][0], "fmt": curves_90d["run_splits"][d_lbl]["90d"][1]}
        for d_lbl in ("1km", "3km", "5km", "10km")
        if curves_90d["run_splits"][d_lbl].get("90d")
    }
    cp_sol = coach_engine.solve_cp_w_prime(mmp_90d)
    cs_sol = coach_engine.solve_cs_d_prime(run_90d)
    eff_cp_final = float(cp_sol["cp_w"]) if (cp_sol["valid"] and cp_sol["cp_w"]) else init_cp
    eff_wp_final = float(cp_sol["w_prime_j"]) if (cp_sol["valid"] and cp_sol["w_prime_j"]) else init_wp_j
    eff_cs_final = float(cs_sol["cs_mps"]) if (cs_sol["valid"] and cs_sol["cs_mps"]) else prof_info.get("cs_mps")

    if cp_sol["valid"] and synced_streams_mem:
        for a in all_acts:
            aid = str(a.get("id") or "")
            if aid in synced_streams_mem and coach_engine.is_valid_cycling_power_activity(a):
                a.update(_compute_stream_metrics(
                    synced_streams_mem[aid],
                    ftp_watts=ftp_w,
                    weight_kg=weight_kg,
                    sport="cycling",
                    cp_watts=eff_cp_final,
                    w_prime_j=eff_wp_final,
                    device_watts=a.get("device_watts"),
                ))

    # Score Plan-vs-Actual Interval Execution & Z2 Discipline against scheduled calendar workouts
    idx_cal = hk.load_index()
    cal_for_exec = hk.calendar_items(idx_cal, days=30, past_days=min(max(days, 30), 180), include_done=True)
    planned_by_date_sport = {}
    for ci in cal_for_exec:
        if ci.get("kind") == "workout" and isinstance(ci.get("start"), dt.date):
            planned_by_date_sport.setdefault((ci["start"].isoformat(), (ci.get("subkind") or "cycling").lower()), ci)

    for a in all_acts:
        sys_tss, tss_m = coach_engine.compute_activity_load(
            a, profile=prof_info, cs_mps=eff_cs_final, cp_watts=eff_cp_final,
        )
        a["systemic_tss"] = sys_tss
        a["tss_method"] = tss_m
        ad_s = str(a.get("date") or "")[:10]
        asp = str(a.get("sport") or "cycling").lower()
        p_item = planned_by_date_sport.get((ad_s, asp))
        if p_item:
            p_spec = hk.parse_workout_spec(p_item.get("what") or "", subkind=asp, ftp_watts=int(round(eff_cp_final)))
            aid = str(a.get("id") or "")
            s_mem = synced_streams_mem.get(aid)
            if not s_mem:
                sf_p = os.path.join(stream_cache_dir, f"{aid}.json")
                if os.path.isfile(sf_p):
                    s_mem = load_json(sf_p, None)
            w_arr = [float(x or 0.0) for x in ((s_mem or {}).get("watts") or [])] if isinstance(s_mem, dict) else None
            ex_eval = coach_engine.evaluate_workout_execution(
                a, p_item, p_spec, ftp_watts=int(round(eff_cp_final)), watts_stream=w_arr,
            )
            if ex_eval:
                a["execution"] = ex_eval

    # Pull Garmin / Intervals.icu wellness biometrics (RHR, Sleep, HRV RMSSD, Weight) into readiness.md
    wellness_rows = coach_connectors.garmin_pull_wellness(brain, days=min(days, 14), mock_file=wellness_mock, today=today)
    if not wellness_rows and not wellness_mock:
        wellness_rows = coach_connectors.intervals_pull_wellness(brain, days=min(days, 14), today=today)
    wellness_imported = 0
    if wellness_rows and execute:
        rdy_path = os.path.join(brain, "coach", "readiness.md")
        rdy_existing = read(rdy_path) if os.path.isfile(rdy_path) else ""
        r_by_d = {r["date"]: r for r in coach_engine.parse_readiness_log(rdy_existing)}
        for w_row in wellness_rows:
            wd = w_row["date"]
            cur_r = dict(r_by_d.get(wd) or {
                "date": wd, "rhr": None, "sleep_h": None, "sleep_quality": None, "rpe": None, "soreness": None,
                "ctl": None, "atl": None, "tsb": None, "acwr": None, "status": "", "notes": "",
            })
            for fld in ("rhr", "sleep_h", "sleep_quality", "hrv_rmssd", "weight_kg"):
                if w_row.get(fld) is not None and cur_r.get(fld) is None:
                    cur_r[fld] = w_row[fld]
            if w_row.get("soreness") is not None and cur_r.get("soreness") is None:
                cur_r["soreness"] = w_row["soreness"]
            r_by_d[wd] = cur_r
            wellness_imported += 1
        merged_w = sorted(r_by_d.values(), key=lambda x: x["date"], reverse=True)
        pmc_now = coach_engine.compute_pmc_metrics(all_acts, profile=prof_info, cs_mps=eff_cs_final, cp_watts=eff_cp_final, today=today)
        for wr in merged_w[:7]:
            d_obj = dt.date.fromisoformat(wr["date"])
            ev_d = coach_engine.evaluate_readiness_and_guardrails(merged_w, prof_info, pmc_now, all_acts, cal_items=cal_for_exec, today=d_obj)
            wr["ctl"] = pmc_now["ctl"]
            wr["atl"] = pmc_now["atl"]
            wr["tsb"] = pmc_now["tsb"]
            wr["acwr"] = pmc_now["acwr"]
            wr["status"] = ev_d["status"]
        write(rdy_path, coach_engine.render_readiness_markdown(rdy_existing, merged_w, today.isoformat()))
        _ensure_coach_readiness_link(brain, execute=True)

    if execute:
        dump_json(act_json_path, all_acts)
        _rebuild_coach_markdown_files(brain, all_acts, ftp_watts=ftp_w, weight_kg=weight_kg)
        matched = _match_activities_to_calendar(brain, all_acts)
        set_state(brain, last_strava_sync=now_iso())
        hk.load_index()
        hk.render_calendar_html(hk.load_index(), start_server=False)
    else:
        matched = 0
    if not quiet:
        pend_cnt = sum(1 for a in all_acts if a.get("feedback") == "pending")
        w_bit = f", {wellness_imported} wellness day(s) imported" if wellness_imported else ""
        say(f"coach sync ({'applied' if execute else 'dry-run'}): {synced_count} activity/ies processed, "
            f"{matched} calendar workout(s) marked [x], {pend_cnt} ride check-in(s) pending{w_bit}.")
    return synced_count


def _coach_note(
    root,
    brain,
    hk,
    date_str=None,
    text=None,
    rpe=None,
    rhr=None,
    sleep_h=None,
    sleep_quality=None,
    soreness=None,
    hrv_rmssd=None,
    weight_kg_in=None,
    leg_pain=None,
    fatigue_7d=None,
):
    """Save subjective ride feedback (RPE, feel) and/or daily readiness check-in (RHR, HRV, sleep, soreness, leg_pain, weight)."""
    _coach_init(root, brain, hk, execute=True, quiet=True)
    eff_date = (date_str or dt.date.today().isoformat())[:10]
    ftp_w, weight_kg = _extract_profile_numbers(brain, hk)
    prof_path = os.path.join(brain, "coach", "profile.md")
    prof_text = read(prof_path) if os.path.isfile(prof_path) else ""
    prof_info = coach_engine.parse_athlete_profile(prof_text, fallback_ftp=ftp_w, fallback_weight=weight_kg)

    clean_text = (text or "").strip()
    clean_sq = (sleep_quality or "").strip() or None
    if rpe is not None and clean_text:
        note_str = f"RPE {rpe}/10 — {clean_text}"
    elif rpe is not None and not clean_text and all(v is None for v in (rhr, sleep_h, clean_sq, soreness, hrv_rmssd, weight_kg_in, leg_pain, fatigue_7d)):
        note_str = f"RPE {rpe}/10"
    else:
        note_str = clean_text

    act_json_path = os.path.join(brain, "coach", "activities.json")
    acts = load_json(act_json_path, [])
    if not isinstance(acts, list):
        acts = []

    has_readiness_args = any(v is not None for v in (rhr, sleep_h, clean_sq, soreness, hrv_rmssd, weight_kg_in, leg_pain, fatigue_7d))
    # Update activity log when workout feedback (`text` or `rpe` without pure morning check-in) is provided
    if note_str or (rpe is not None and not has_readiness_args):
        updated_act = False
        for a in acts:
            if isinstance(a, dict) and str(a.get("date", ""))[:10] == eff_date:
                if note_str:
                    a["feedback"] = note_str
                if rpe is not None:
                    a["rpe"] = rpe
                updated_act = True
        if not updated_act and not has_readiness_args:
            new_act = {
                "id": f"manual-{eff_date}",
                "date": eff_date,
                "name": "Logged Workout",
                "sport": "cycling",
                "duration_min": 60,
                "feedback": note_str,
            }
            if rpe is not None:
                new_act["rpe"] = rpe
            acts.insert(0, new_act)
        dump_json(act_json_path, acts)

        cal_path = os.path.join(brain, "calendar", "README.md")
        if os.path.isfile(cal_path) and note_str:
            cal_txt = read(cal_path)
            lines = cal_txt.splitlines()
            for i, line in enumerate(lines):
                m = re.match(r"^(\s*-\s*)\[([ xX\-])\](\s+)(\d{4}-\d{2}-\d{2})\b(.*)$", line)
                if m and m.group(4) == eff_date and "[workout" in m.group(5).lower():
                    rest = m.group(5)
                    if re.search(r"\[feedback:[^\]]*\]", rest, re.I):
                        rest = re.sub(r"\[feedback:[^\]]*\]", f"[feedback: {note_str}]", rest, flags=re.I)
                    else:
                        rest = rest.rstrip() + f" [feedback: {note_str}]"
                    lines[i] = f"{m.group(1)}[x]{m.group(3)}{eff_date}{rest}"
            write(cal_path, "\n".join(lines) + ("\n" if cal_txt.endswith("\n") else ""))

    # Update brain/coach/readiness.md when readiness metrics (RHR, sleep, soreness, or RPE) are logged
    rdy_status_msg = ""
    if has_readiness_args or rpe is not None:
        rdy_path = os.path.join(brain, "coach", "readiness.md")
        rdy_existing = read(rdy_path) if os.path.isfile(rdy_path) else ""
        rows = coach_engine.parse_readiness_log(rdy_existing)
        by_date_r = {r["date"]: r for r in rows}
        cur_row = dict(by_date_r.get(eff_date) or {
            "date": eff_date, "rhr": None, "sleep_h": None, "sleep_quality": None, "rpe": None, "soreness": None,
            "ctl": None, "atl": None, "tsb": None, "acwr": None, "status": "", "notes": "",
        })
        if rhr is not None:
            cur_row["rhr"] = int(rhr)
        if sleep_h is not None:
            cur_row["sleep_h"] = float(sleep_h)
        if clean_sq is not None:
            cur_row["sleep_quality"] = clean_sq
        if rpe is not None:
            cur_row["rpe"] = float(rpe)
        if soreness is not None:
            cur_row["soreness"] = int(soreness)
        if hrv_rmssd is not None:
            cur_row["hrv_rmssd"] = float(hrv_rmssd)
        if weight_kg_in is not None:
            cur_row["weight_kg"] = float(weight_kg_in)
        if leg_pain is not None:
            cur_row["leg_pain"] = int(leg_pain)
            if cur_row.get("soreness") is None:
                cur_row["soreness"] = int(leg_pain)
        if fatigue_7d is not None:
            cur_row["fatigue_7d"] = int(fatigue_7d)
        if clean_text:
            cur_row["notes"] = clean_text
        by_date_r[eff_date] = cur_row
        merged_rows = sorted(by_date_r.values(), key=lambda x: x["date"], reverse=True)

        idx_tmp = hk.load_index()
        cal_items = hk.calendar_items(idx_tmp, days=60, past_days=14, include_done=True)
        pmc = coach_engine.compute_pmc_metrics(acts, profile=prof_info, today=dt.date.today())
        rdy_eval = coach_engine.evaluate_readiness_and_guardrails(
            merged_rows, prof_info, pmc, acts, cal_items=cal_items, today=dt.date.today()
        )
        cur_row["ctl"] = pmc["ctl"]
        cur_row["atl"] = pmc["atl"]
        cur_row["tsb"] = pmc["tsb"]
        cur_row["acwr"] = pmc["acwr"]
        cur_row["status"] = rdy_eval["status"]
        by_date_r[eff_date] = cur_row
        final_rows = sorted(by_date_r.values(), key=lambda x: x["date"], reverse=True)
        write(rdy_path, coach_engine.render_readiness_markdown(rdy_existing, final_rows, dt.date.today().isoformat()))
        _ensure_coach_readiness_link(brain, execute=True)
        rdy_status_msg = f" [readiness={rdy_eval['status']}, TSB={pmc['tsb']:+.1f}]"

    _rebuild_coach_markdown_files(brain, acts, ftp_watts=ftp_w, weight_kg=weight_kg)
    hk.load_index()
    hk.render_calendar_html(hk.load_index(), start_server=False)
    summary_label = note_str or ", ".join(
        s for s in (
            f"RHR {rhr} bpm" if rhr is not None else "",
            f"Sleep {sleep_h}h" if sleep_h is not None else "",
            f"Sleep quality {clean_sq}" if clean_sq is not None else "",
            f"Soreness {soreness}/5" if soreness is not None else "",
        ) if s
    )
    say(f"coach note: saved feedback for {eff_date} -> '{summary_label}'{rdy_status_msg}")


def _coach_zwo(root, brain, hk, date_str=None, days=14):
    """Compile structured cycling workouts from brain/calendar/README.md into .zwo XML files in brain/coach/workouts/."""
    _coach_init(root, brain, hk, execute=True, quiet=True)
    ftp_w, _wkg = _extract_profile_numbers(brain, hk)
    idx = hk.load_index()
    items = hk.calendar_items(idx, days=max(days, 30), past_days=14, include_done=True)
    out_dir = os.path.join(brain, "coach", "workouts")
    os.makedirs(out_dir, exist_ok=True)
    written = []
    for it in items:
        if it.get("kind") != "workout" or (it.get("subkind") or "cycling") != "cycling":
            continue
        ds = it["start"].isoformat()
        if date_str and ds != date_str:
            continue
        if not date_str and not (0 <= it["delta"] <= days):
            continue
        spec = hk.parse_workout_spec(it.get("what", ""), ftp_watts=ftp_w, sport="cycling")
        if not spec.get("has_intervals"):
            continue
        xml_str = hk.compile_zwo_xml(spec, date_str=ds, ftp_watts=ftp_w)
        slug = re.sub(r"[^a-z0-9]+", "-", spec.get("title", "workout").lower()).strip("-") or "workout"
        fname = f"{ds}_{slug}.zwo"
        fpath = os.path.join(out_dir, fname)
        write(fpath, xml_str)
        written.append(fpath)
    hk.render_calendar_html(idx, start_server=False)
    if written:
        say(f"coach zwo: generated {len(written)} .zwo file(s) (FTP={ftp_w}W):")
        for p in written:
            say(f"  {fwd(p)}")
    else:
        say("coach zwo: no matching cycling workouts with interval specs found.")
    return written


def _coach_auth_strava(root, brain, args):
    """Zero-friction 1-Click OAuth flow (via background calendar server on 127.0.0.1:8765 or --code exchange) for Strava API v3."""
    import urllib.parse
    import urllib.request
    hk = hook_module(brain)
    env_dict = _load_strava_env(brain)
    if getattr(args, "client_id", None):
        env_dict["STRAVA_CLIENT_ID"] = args.client_id.strip()
    if getattr(args, "client_secret", None):
        env_dict["STRAVA_CLIENT_SECRET"] = args.client_secret.strip()
    if getattr(args, "access_token", None):
        env_dict["STRAVA_ACCESS_TOKEN"] = args.access_token.strip()
    if getattr(args, "refresh_token", None):
        env_dict["STRAVA_REFRESH_TOKEN"] = args.refresh_token.strip()
        if not env_dict.get("STRAVA_EXPIRES_AT"):
            env_dict["STRAVA_EXPIRES_AT"] = str(int(time.time()) + 21600)
    if env_dict:
        _save_strava_env(brain, env_dict)

    cid = env_dict.get("STRAVA_CLIENT_ID") or DEFAULT_STRAVA_CLIENT_ID
    csec = env_dict.get("STRAVA_CLIENT_SECRET") or _default_strava_secret()

    if getattr(args, "access_token", None) and not getattr(args, "code", None):
        say(f"strava auth: saved credentials and token to {fwd(_strava_env_path(brain))}.")
        return

    code = getattr(args, "code", None)
    if not code:
        cal_url = hk.render_calendar_html(hk.load_index(), start_server=True)
        base_url = cal_url.rsplit("/", 1)[0]
        redirect_uri = f"{base_url}/strava-callback"
        connect_url = f"{base_url}/strava-connect"
        auth_url = (
            f"https://www.strava.com/oauth/authorize?client_id={cid}&response_type=code"
            f"&redirect_uri={urllib.parse.quote(redirect_uri, safe='')}"
            f"&approval_prompt=force&scope=read,activity:read,activity:read_all,profile:read_all"
        )
        if not os.environ.get("ATLAS_NO_CAL_SERVER"):
            try:
                import webbrowser
                webbrowser.open(auth_url)
            except Exception:
                pass
        say("=== Strava 1-Click Connect (Ready) ===")
        say(f"Click this link to connect Strava with 1 click (no API keys needed):\n  [🧡 Jetzt mit Strava verbinden (1-Klick)]({connect_url})")
        say(f"Direct Strava OAuth URL:\n  {auth_url}")
        say(f"Visual Coach Dashboard:\n  [📅 Atlas Coach Dashboard]({cal_url}?view=coach)")
        return

    data = urllib.parse.urlencode({
        "client_id": cid,
        "client_secret": csec,
        "code": code,
        "grant_type": "authorization_code",
    }).encode("utf-8")
    req = urllib.request.Request("https://www.strava.com/oauth/token", data=data, method="POST")
    with urllib.request.urlopen(req, timeout=20) as resp:
        res = json.loads(resp.read().decode("utf-8", errors="replace"))
    env_dict["STRAVA_CLIENT_ID"] = cid
    env_dict["STRAVA_ACCESS_TOKEN"] = res.get("access_token", "")
    env_dict["STRAVA_REFRESH_TOKEN"] = res.get("refresh_token", "")
    env_dict["STRAVA_EXPIRES_AT"] = str(res.get("expires_at", int(time.time()) + 21600))
    athlete = res.get("athlete") or {}
    if athlete.get("id"):
        env_dict["STRAVA_ATHLETE_ID"] = str(athlete["id"])
    _save_strava_env(brain, env_dict)
    say(f"strava auth: connected athlete {env_dict.get('STRAVA_ATHLETE_ID', 'ok')} -> {fwd(_strava_env_path(brain))}")
    try:
        _coach_sync(root, brain, hk, days=90, execute=True, quiet=False)
    except Exception as exc:
        say(f"strava auth: initial sync note ({exc})")


def _coach_context(
    root,
    brain,
    hk,
    mode="auto",
    deep_dive=None,
    open_cal=False,
    no_sync=False,
    depth_override=None,
    query_text="",
):
    """Assemble and print the Adaptive Simple-to-Pro Context Package for the Atlas Sport-Coach."""
    _coach_init(root, brain, hk, execute=True, quiet=True)
    st = get_state(brain)
    c_state = hk.load_coach_state()
    if c_state["strava_connected"] and not no_sync:
        last_sync = st.get("last_strava_sync")
        age_min = ((dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(last_sync)).total_seconds() / 60.0) if last_sync else 9999
        if age_min >= 30:
            try:
                _coach_sync(root, brain, hk, days=30, execute=True, quiet=True)
                c_state = hk.load_coach_state()
            except Exception:
                pass

    idx = hk.load_index()
    all_cal = hk.calendar_items(idx, days=365, past_days=21, include_done=True)
    macro = hk.parse_macro_plan(cal_items=all_cal)
    eff_mode = mode if mode != "auto" else ("macro" if macro["stale"] else "daily")

    prof_path = os.path.join(brain, "coach", "profile.md")
    rdy_path = os.path.join(brain, "coach", "readiness.md")
    dna_path = os.path.join(brain, "coach", "dna.md")
    pb_path = os.path.join(brain, "coach", "playbook.md")
    prof_text = read(prof_path) if os.path.isfile(prof_path) else ""
    rdy_text = read(rdy_path) if os.path.isfile(rdy_path) else ""
    dna_text = read(dna_path) if os.path.isfile(dna_path) else ""
    pb_text = read(pb_path) if os.path.isfile(pb_path) else ""

    telemetry = coach_engine.build_coach_telemetry(
        profile_text=prof_text,
        activities=c_state.get("activities", []),
        readiness_text=rdy_text,
        cal_items=all_cal,
        strava_connected=bool(c_state.get("strava_connected")),
        explicit_depth=depth_override if depth_override and depth_override != "auto" else None,
        query_text=query_text or "",
        dna_text=dna_text,
        playbook_text=pb_text,
        macro_info=macro,
    )
    depth = telemetry["depth"]

    # SIMPLE TIER: Calm, 100% jargon-free output (zero CP/W'/FTP/CTL/RHR/.zwo/Strava nagging)
    if depth == "simple":
        say(coach_engine.format_simple_context_block(
            mode=eff_mode,
            profile_info=telemetry["profile"],
            activities=c_state.get("activities", []),
            cal_items=all_cal,
        ))
        if open_cal:
            _open_visual_calendar(hk, idx, open_now=True)
        return

    # STANDARD & PRO TIERS
    conn = coach_connectors.connector_status_summary(brain)
    say(f"=== 1. ATLAS SPORT-COACH CONTEXT (mode: {eff_mode}, depth: {depth}) ===")
    say(f"  Macro Plan: active_phase='{macro['active_phase']}', updated='{macro['updated'] or 'never'}', "
        f"goals_hash='{macro['goals_hash']} -> {macro['current_goals_hash']}', stale={macro['stale']}"
        + (f" ({macro['stale_reason']})" if macro["stale"] else ""))
    if macro["stale"] and eff_mode in ("plan-14d", "macro"):
        say("  [!] STALE MACRO PLAN DETECTED: You MUST update `brain/coach/macro-plan.md` (with current `updated:` and "
            f"`goals_hash: {macro['current_goals_hash']}`) BEFORE writing 14-day workouts in `brain/calendar/README.md`!")
    cal_url_ctx = hk.render_calendar_html(idx, start_server=True)
    base_url_ctx = cal_url_ctx.rsplit("/", 1)[0]
    if c_state["strava_connected"]:
        say(f"  Strava: connected ({len(c_state['activities'])} cached activities)")
    else:
        import urllib.parse
        connect_url = f"{base_url_ctx}/strava-connect"
        redir = urllib.parse.quote(f"{base_url_ctx}/strava-callback", safe="")
        auth_url = (f"https://www.strava.com/oauth/authorize?client_id={DEFAULT_STRAVA_CLIENT_ID}&response_type=code"
                    f"&redirect_uri={redir}&approval_prompt=force&scope=read,activity:read,activity:read_all,profile:read_all")
        say("  Strava: NOT CONNECTED. Give the user this direct 1-Click link in your reply (no API keys or setup needed — clicking connects & syncs automatically):")
        say(f"    [🧡 Jetzt mit Strava verbinden (1-Klick)]({connect_url})")
        say(f"    Direct OAuth URL: {auth_url}")
    hh_status_str = (
        "connected"
        if conn["hammerhead_connected"]
        else f"not connected — 1-Click link (NO API keys needed): `[💛 Karoo verbinden (1-Klick)]({base_url_ctx}/hammerhead-connect)`"
    )
    say(
        f"  Devices & Wellness: Hammerhead Karoo={hh_status_str} | "
        f"Garmin Connect={'connected' if conn['garmin_connected'] else 'not connected (`atlas.py coach auth-garmin` + `.fit` ready)'} | "
        f"Intervals.icu={'connected' if conn['intervals_connected'] else 'optional'}"
    )
    say("  On-Demand Pull CLI (call when user asks about >21d history, past blocks, or a specific session):")
    say("    - `atlas.py coach history --months 6 [--sport cycling|running]`")
    say("    - `atlas.py coach compare --epoch-a YYYY-MM-DD..YYYY-MM-DD --epoch-b YYYY-MM-DD..YYYY-MM-DD`")
    say("    - `atlas.py coach inspect --date YYYY-MM-DD` | `atlas.py coach push --execute` | `atlas.py coach field-test --unit 1|2|preload|lsct --date YYYY-MM-DD --execute`")

    pend = c_state.get("pending_feedback", [])
    if pend:
        say(f"  [!] PENDING RIDE CHECK-IN ({len(pend)} recent activity/ies without athlete feedback):")
        for p in pend[:3]:
            p_pwr = f", NP {p.get('np_w')}W" if (coach_engine.is_valid_cycling_power_activity(p) and p.get("np_w")) else ""
            say(f"      - {p.get('date')} '{p.get('name')}' ({p.get('duration_min', 0)}m{p_pwr}): "
                f"Ask 1 concise question how it felt (RPE/legs) and save via `atlas.py coach note --date {p.get('date')} --text \"...\"`")

    if depth == "pro":
        say("\n" + coach_engine.format_pro_telemetry_block(telemetry))
    say("\n" + coach_engine.format_seven_pillar_blocks(telemetry))

    if pb_text.strip():
        say("\n=== 2A. ATHLETE PLAYBOOK & FEELINGS-FILE (brain/coach/playbook.md) ===")
        say(pb_text.strip()[:6000])

    say("\n=== 2. ATHLETE PROFILE & THRESHOLDS (brain/coach/profile.md) ===")
    if prof_text:
        say(prof_text.strip())

    say("\n=== 3. MACRO PERIODIZATION (brain/coach/macro-plan.md) ===")
    macro_path = os.path.join(brain, "coach", "macro-plan.md")
    if os.path.isfile(macro_path):
        if eff_mode == "macro" or macro["stale"]:
            say(read(macro_path).strip())
        else:
            say(f"Active Phase: {macro['active_phase']} (updated {macro['updated']})")
            for w in macro.get("weeks", [])[:8]:
                say(f"  KW {w['kw']} ({w['monday']}): Phase={w['phase']} | Vol={w['hours']} | "
                    f"Cycling={w['cycling']} | Run/Strength={w['running_strength']} | Notes={w['notes']}")

    if eff_mode in ("plan-14d", "macro") or depth == "pro":
        say("\n=== 4. 3-EPOCH POWER & PACE CURVE (brain/coach/power-curve.md) ===")
        pc_path = os.path.join(brain, "coach", "power-curve.md")
        if os.path.isfile(pc_path):
            say(read(pc_path).strip())

    say("\n=== 5. CALENDAR HORIZON (brain/calendar/README.md) ===")
    goals = [i for i in all_cal if i["kind"] == "goal" and i["delta"] >= 0 and i["status"] == "open"]
    blk_horizon = 365 if (eff_mode == "macro" or macro["stale"]) else 28
    blocked = [i for i in all_cal if 0 <= i["delta"] <= blk_horizon and i["status"] == "open"
               and (i["kind"] == "blocked" or (i["kind"] == "event" and i["time"]))]
    workouts = [i for i in all_cal if -14 <= i["delta"] <= 14 and i["kind"] == "workout"]
    say("Upcoming Goals (365d):")
    for g in goals:
        say(f"  - {g['start'].isoformat()} (in {g['delta']}d / {g['delta']/7:.1f}w) [goal:{g['subkind'] or 'A'}] {g['what']}")
    if not goals:
        say("  (none)")
    say(f"Blocked & Busy Windows ({blk_horizon}d):")
    for b in blocked:
        span = b["start"].isoformat() if b["start"] == b["end"] else f"{b['start'].isoformat()}..{b['end'].isoformat()}"
        t_s = f" {b['time']}" if b["time"] else ""
        say(f"  - {span}{t_s} [{b['kind']}] {b['what']}")
    if not blocked:
        say("  (none)")
    say("Planned & Recent Calendar Workouts (-14d .. +14d):")
    box_map = {"open": "[ ]", "done": "[x]", "skipped": "[-]"}
    for w in workouts:
        t_s = f" {w['time']}" if w["time"] else ""
        say(f"  - {box_map.get(w['status'], '[ ]')} {w['start'].isoformat()}{t_s} [workout:{w['subkind'] or 'cycling'}] {w['what']}")
    if not workouts:
        say("  (none)")

    say("\n=== 6. ACTIVITY & FEEDBACK LOG (brain/coach/activities.md) ===")
    act_md_path = os.path.join(brain, "coach", "activities.md")
    if os.path.isfile(act_md_path):
        act_text = read(act_md_path).strip()
        if eff_mode in ("daily", "checkin") and "## Tier 2" in act_text:
            say(act_text[:act_text.index("## Tier 2")].strip())
        elif eff_mode == "plan-14d" and "## Tier 3" in act_text:
            say(act_text[:act_text.index("## Tier 3")].strip())
        else:
            say(act_text)

    if eff_mode == "dna":
        say("\n" + coach_engine.build_dna_context_block(
            activities=c_state.get("activities", []),
            telemetry=telemetry,
            dna_snapshot_text=dna_text,
        ))

    if deep_dive:
        say(f"\n=== 7. DEEP-DIVE ACTIVITY ({deep_dive}) ===")
        matches = [a for a in c_state.get("activities", []) if str(a.get("date", ""))[:10] == deep_dive]
        if matches:
            for m_act in matches:
                say(json.dumps(m_act, indent=2, ensure_ascii=False))
        else:
            say(f"  (no activity found on {deep_dive})")

    if open_cal:
        _open_visual_calendar(hk, idx, open_now=True)


def _coach_auth_hammerhead(root, brain, hk, args):
    """Zero-friction 1-Click OAuth flow (via background calendar server on 127.0.0.1:8765 or --code exchange) for Hammerhead Karoo."""
    env = coach_connectors.load_secret_env(brain, "hammerhead")
    if getattr(args, "client_id", None):
        env["HAMMERHEAD_CLIENT_ID"] = args.client_id.strip()
    if getattr(args, "client_secret", None):
        env["HAMMERHEAD_CLIENT_SECRET"] = args.client_secret.strip()
    if getattr(args, "access_token", None):
        env["HAMMERHEAD_ACCESS_TOKEN"] = args.access_token.strip()
    if getattr(args, "refresh_token", None):
        env["HAMMERHEAD_REFRESH_TOKEN"] = args.refresh_token.strip()
    if env:
        coach_connectors.save_secret_env(brain, "hammerhead", env)

    if getattr(args, "access_token", None) and not getattr(args, "code", None):
        say(f"hammerhead auth: saved credentials to {fwd(coach_connectors.secret_env_path(brain, 'hammerhead'))}.")
        return

    cal_url = hk.render_calendar_html(hk.load_index(), start_server=True)
    base_url = cal_url.rsplit("/", 1)[0]
    local_cb = f"{base_url}/hammerhead-callback"
    connect_url = f"{base_url}/hammerhead-connect"
    srv_port = 8765
    try:
        srv_port = int(base_url.rsplit(":", 1)[1])
    except Exception:
        pass
    cfg = coach_connectors.hammerhead_oauth_config(brain, local_redirect_uri=local_cb)

    code = getattr(args, "code", None)
    if not code:
        state = coach_connectors.mint_hammerhead_oauth_state(brain, port=srv_port)
        auth_url = coach_connectors.build_hammerhead_authorize_url(cfg["client_id"], cfg["redirect_uri"], state=state)
        if not os.environ.get("ATLAS_NO_CAL_SERVER"):
            try:
                import webbrowser
                webbrowser.open(auth_url)
            except Exception:
                pass
        say("=== Hammerhead Karoo 1-Click Connect (Ready) ===")
        say(f"Click this link to connect Hammerhead Karoo with 1 click (no API keys or developer app needed):\n  [💛 Jetzt mit Hammerhead Karoo verbinden (1-Klick)]({connect_url})")
        say(f"Direct Hammerhead OAuth URL:\n  {auth_url}")
        say(f"Visual Coach Dashboard:\n  [📅 Atlas Coach Dashboard]({cal_url}?view=coach)")
        return

    ok, msg = coach_connectors.hammerhead_exchange_code(
        brain, code=code, redirect_uri=cfg["redirect_uri"],
        client_id=getattr(args, "client_id", None),
        client_secret=getattr(args, "client_secret", None),
    )
    if not ok:
        sys.exit(f"hammerhead auth failed: {msg}")
    say(f"hammerhead auth: connected -> {fwd(msg)}")


def _coach_auth_garmin(brain, args):
    """Connect Garmin Connect (discards plaintext password immediately, stores session tokens in `_secrets/garmin.env`)."""
    ok, msg = coach_connectors.garmin_authenticate(
        brain,
        email=getattr(args, "email", None) or "",
        password=getattr(args, "password", None) or "",
        mfa_code=getattr(args, "mfa_code", None) or "",
        tokens_json=getattr(args, "tokens_json", None) or "",
    )
    if not ok:
        say(f"garmin auth: {msg}")
        return
    say(f"garmin auth: {msg}")


def _coach_auth_intervals(brain, args):
    """Connect Intervals.icu pure-stdlib REST bridge (`_secrets/intervals.env`)."""
    env = coach_connectors.load_secret_env(brain, "intervals")
    if getattr(args, "athlete_id", None):
        env["INTERVALS_ATHLETE_ID"] = args.athlete_id.strip()
    if getattr(args, "api_key", None):
        env["INTERVALS_API_KEY"] = args.api_key.strip()
    if not (env.get("INTERVALS_ATHLETE_ID") and env.get("INTERVALS_API_KEY")):
        say("intervals auth: pass `--athlete-id <i12345>` and `--api-key <API_KEY>` (saved locally to `brain/_secrets/intervals.env`).")
        return
    p = coach_connectors.save_secret_env(brain, "intervals", env)
    say(f"intervals auth: saved Intervals.icu credentials -> {fwd(p)}")


def _coach_push(root, brain, hk, days=14, execute=True, mock_dir=None):
    """Compile `.zwo` + `.fit` files and push/update/delete idempotently on Hammerhead Karoo & Garmin Connect."""
    _coach_init(root, brain, hk, execute=execute, quiet=True)
    ftp_w, weight_kg = _extract_profile_numbers(brain, hk)
    prof_path = os.path.join(brain, "coach", "profile.md")
    prof_info = coach_engine.parse_athlete_profile(
        read(prof_path) if os.path.isfile(prof_path) else "",
        fallback_ftp=ftp_w,
        fallback_weight=weight_kg,
    )
    c_state = hk.load_coach_state()
    curves = coach_engine.aggregate_epoch_curves(c_state.get("activities", []))
    cp_sol = coach_engine.solve_cp_w_prime({lbl: curves["fresh"][lbl]["90d"] for lbl, _ in COACH_DURATIONS if curves["fresh"][lbl].get("90d")})
    run_90d = {d: {"sec_per_km": curves["run_splits"][d]["90d"][0], "fmt": curves["run_splits"][d]["90d"][1]}
               for d in ("1km", "3km", "5km", "10km") if curves["run_splits"][d].get("90d")}
    cs_sol = coach_engine.solve_cs_d_prime(run_90d)
    eff_ftp = int(cp_sol["cp_w"]) if (cp_sol["valid"] and cp_sol["cp_w"]) else ftp_w
    eff_cs = float(cs_sol["cs_mps"]) if (cs_sol["valid"] and cs_sol["cs_mps"]) else prof_info.get("cs_mps")

    idx = hk.load_index()
    cal_items = hk.calendar_items(idx, days=max(days, 30), past_days=7, include_done=True)
    res = coach_connectors.sync_calendar_workouts_to_devices(
        brain=brain,
        cal_items=cal_items,
        parse_workout_spec_fn=hk.parse_workout_spec,
        compile_zwo_xml_fn=hk.compile_zwo_xml,
        ftp_watts=eff_ftp,
        cs_mps=eff_cs,
        days=days,
        execute=execute,
        mock_dir=mock_dir,
    )
    if execute:
        hk.render_calendar_html(idx, start_server=False)
    say(f"coach push ({'applied' if execute else 'dry-run'}): {len(res['zwo_files'])} .zwo file(s), "
        f"{len(res['fit_files'])} .fit file(s), {len(res['device_actions'])} remote sync op(s) (anchor={eff_ftp}W).")
    for act_msg in res["device_actions"]:
        say(f"  - {act_msg}")
    return res


def _coach_compare_epochs(brain, hk, epoch_a, epoch_b):
    """Compare the 7-Pillar Biophysical State Vector across two historical date ranges (`YYYY-MM-DD..YYYY-MM-DD`)."""
    def _parse_range(spec_s):
        s = str(spec_s or "").strip()
        if ".." in s:
            a, b = s.split("..", 1)
            return a.strip()[:10], b.strip()[:10]
        # Allow `YYYY-MM` shorthand
        if re.match(r"^\d{4}-\d{2}$", s):
            return f"{s}-01", f"{s}-31"
        return s[:10], s[:10]

    a_lo, a_hi = _parse_range(epoch_a)
    b_lo, b_hi = _parse_range(epoch_b)
    c_state = hk.load_coach_state()
    acts = c_state.get("activities", [])
    prof_p = os.path.join(brain, "coach", "profile.md")
    prof_txt = read(prof_p) if os.path.isfile(prof_p) else ""

    acts_a = [a for a in acts if isinstance(a, dict) and a_lo <= str(a.get("date", ""))[:10] <= a_hi]
    acts_b = [a for a in acts if isinstance(a, dict) and b_lo <= str(a.get("date", ""))[:10] <= b_hi]
    ref_a = dt.date.fromisoformat(a_hi if re.match(r"^\d{4}-\d{2}-\d{2}$", a_hi) and a_hi[-2:] <= "28" else a_lo)
    ref_b = dt.date.fromisoformat(b_hi if re.match(r"^\d{4}-\d{2}-\d{2}$", b_hi) and b_hi[-2:] <= "28" else b_lo)

    tel_a = coach_engine.build_coach_telemetry(prof_txt, acts_a, today=ref_a)
    tel_b = coach_engine.build_coach_telemetry(prof_txt, acts_b, today=ref_b)
    say(f"=== COACH 7-PILLAR EPOCH COMPARISON ({epoch_a} [{len(acts_a)} acts] vs {epoch_b} [{len(acts_b)} acts]) ===")
    say(f"\n--- Epoch A ({epoch_a}) ---\n" + coach_engine.format_seven_pillar_blocks(tel_a))
    say(f"\n--- Epoch B ({epoch_b}) ---\n" + coach_engine.format_seven_pillar_blocks(tel_b))


def _coach_field_test(root, brain, hk, unit, date_str=None, execute=True):
    """Schedule a standardized 7-Pillar field test (Unit 1, Unit 2, Durability Preload, or LSCT) into calendar/README.md."""
    _coach_init(root, brain, hk, execute=execute, quiet=True)
    d_iso = (date_str or (dt.date.today() + dt.timedelta(days=1)).isoformat())[:10]
    templates = {
        "1": f"- [ ] {d_iso} 17:30 [workout:cycling] Field-Test Unit 1: Sprint, C_max & W'-Reconstitution (65m | 15m WU 55-75%, 1x 15s@200% / 5m@50%, 20m Z1@52%, 1x 3m@135% / 2m@50%, 1x 3m@130% / 10m@50%, 10m CD 60-45%)",
        "2": f"- [ ] {d_iso} 17:30 [workout:cycling] Field-Test Unit 2: Aerobic Roof & CP Anchor 5m+12m (75m | 15m WU 55-75%, 1x 5m@122% / 32m@52%, 1x 12m@105% / 5m@50%, 6m CD 60-45%)",
        "preload": f"- [ ] {d_iso} 10:00 [workout:cycling] Field-Test Unit 3: Durability Preload 25kJ/kg + 5m All-Out (165m | 20m WU 55-72%, 60m Z2@68%, 3x 8m@103% / 4m@55%, 34m Z2@68%, 1x 5m@120% / 10m@50%)",
        "lsct": f"- [ ] {d_iso} 17:30 [workout:cycling] LSCT Submaximal Monitoring + Base (60m | 6m@58%, 6m@78%, 3m@92%, 2m@40%, 35m Z2@68%, 8m CD 60-45%)",
    }
    u_key = str(unit or "1").lower()
    if u_key not in templates:
        sys.exit("coach field-test requires `--unit 1|2|preload|lsct`")
    line = templates[u_key]
    cal_path = os.path.join(brain, "calendar", "README.md")
    if execute and os.path.isfile(cal_path):
        txt = read(cal_path)
        if "## Timeline" in txt:
            idx_t = txt.index("## Timeline") + len("## Timeline")
            nl = txt.find("\n", idx_t)
            ins_at = nl + 1 if nl >= 0 else len(txt)
            txt = txt[:ins_at] + "\n" + line + "\n" + txt[ins_at:].lstrip("\n")
        else:
            txt = txt.rstrip() + f"\n\n## Timeline\n\n{line}\n"
        write(cal_path, txt)
        _coach_push(root, brain, hk, days=21, execute=True)
    say(f"coach field-test ({'scheduled' if execute else 'dry-run'}): {line}")


def cmd_coach(args):
    root, brain = paths(args)
    hk = hook_module(brain)
    action = getattr(args, "action", None) or "context"
    if getattr(args, "init", False) and action == "context" and not getattr(args, "mode_explicit", False):
        _coach_init(root, brain, hk, execute=args.execute, quiet=False)
        return
    if action == "status":
        _coach_init(root, brain, hk, execute=False, quiet=True)
        c_state = hk.load_coach_state()
        macro = hk.parse_macro_plan()
        conn = coach_connectors.connector_status_summary(brain)
        prof_p = os.path.join(brain, "coach", "profile.md")
        rdy_p = os.path.join(brain, "coach", "readiness.md")
        tel = coach_engine.build_coach_telemetry(
            profile_text=read(prof_p) if os.path.isfile(prof_p) else "",
            activities=c_state.get("activities", []),
            readiness_text=read(rdy_p) if os.path.isfile(rdy_p) else "",
            strava_connected=bool(c_state.get("strava_connected")),
            explicit_depth=getattr(args, "depth", None),
        )
        say(f"coach status: module={'coach' in get_state(brain).get('modules', [])}, "
            f"depth={tel['depth']}, readiness={tel['readiness']['status']} ({tel['readiness'].get('flag_4stage')}), "
            f"ctl={tel['pmc']['ctl']:.1f}, tsb={tel['pmc']['tsb']:+.1f}, "
            f"strava_connected={c_state['strava_connected']}, "
            f"hammerhead_connected={conn['hammerhead_connected']}, garmin_connected={conn['garmin_connected']}, "
            f"activities={len(c_state['activities'])}, "
            f"pending_feedback={len(c_state['pending_feedback'])}, "
            f"macro_phase='{macro['active_phase']}', macro_stale={macro['stale']} ({macro['stale_reason']})")
    elif action == "auth-strava":
        _coach_auth_strava(root, brain, args)
    elif action == "auth-hammerhead":
        _coach_auth_hammerhead(root, brain, hk, args)
    elif action == "auth-garmin":
        _coach_auth_garmin(brain, args)
    elif action == "auth-intervals":
        _coach_auth_intervals(brain, args)
    elif action == "sync":
        eff_days = args.days if getattr(args, "days", None) not in (None, 14) else 90
        _coach_sync(
            root, brain, hk, days=eff_days, mock_file=args.mock_file,
            wellness_mock=getattr(args, "wellness_mock", None), execute=args.execute, quiet=False,
        )
    elif action == "note":
        has_rdy = any(getattr(args, k, None) is not None for k in (
            "rhr", "sleep", "sleep_quality", "soreness", "rpe", "hrv", "weight", "leg_pain", "fatigue_7d"
        ))
        if not args.text and not has_rdy:
            sys.exit("coach note requires --text \"...\" and/or readiness flags (--rhr, --hrv, --sleep, --soreness, --leg-pain, --rpe)")
        if args.text and not args.date and not has_rdy:
            sys.exit("coach note requires --date YYYY-MM-DD when logging workout text without readiness flags")
        _coach_note(
            root,
            brain,
            hk,
            date_str=args.date,
            text=args.text,
            rpe=args.rpe,
            rhr=getattr(args, "rhr", None),
            sleep_h=getattr(args, "sleep", None),
            sleep_quality=getattr(args, "sleep_quality", None),
            soreness=getattr(args, "soreness", None),
            hrv_rmssd=getattr(args, "hrv", None),
            weight_kg_in=getattr(args, "weight", None),
            leg_pain=getattr(args, "leg_pain", None),
            fatigue_7d=getattr(args, "fatigue_7d", None),
        )
    elif action == "zwo":
        _coach_zwo(root, brain, hk, date_str=args.date, days=args.days)
    elif action in ("push", "fit"):
        _coach_push(root, brain, hk, days=args.days, execute=args.execute, mock_dir=getattr(args, "mock_dir", None))
    elif action == "history":
        c_state = hk.load_coach_state()
        say(coach_engine.query_coach_history(
            c_state.get("activities", []),
            date_from=getattr(args, "from_date", None),
            date_to=getattr(args, "to_date", None),
            months=getattr(args, "months", 6),
            sport_filter=getattr(args, "sport", None),
        ))
    elif action == "inspect":
        target_d = getattr(args, "date", None) or getattr(args, "deep_dive", None)
        if not target_d:
            sys.exit("coach inspect requires `--date YYYY-MM-DD`")
        c_state = hk.load_coach_state()
        matches = [a for a in c_state.get("activities", []) if str(a.get("date", ""))[:10] == target_d[:10] or str(a.get("id", "")) == target_d]
        say(f"=== COACH ACTIVITY INSPECT ({target_d} | {len(matches)} match(es)) ===")
        for ma in matches:
            say(json.dumps(ma, indent=2, ensure_ascii=False))
    elif action == "compare":
        if not (getattr(args, "epoch_a", None) and getattr(args, "epoch_b", None)):
            sys.exit("coach compare requires `--epoch-a YYYY-MM-DD..YYYY-MM-DD --epoch-b YYYY-MM-DD..YYYY-MM-DD`")
        _coach_compare_epochs(brain, hk, args.epoch_a, args.epoch_b)
    elif action == "field-test":
        _coach_field_test(root, brain, hk, unit=getattr(args, "unit", "1"), date_str=args.date, execute=args.execute)
    else:
        _coach_context(
            root,
            brain,
            hk,
            mode=args.mode,
            deep_dive=args.deep_dive,
            open_cal=getattr(args, "open", False),
            no_sync=getattr(args, "no_sync", False),
            depth_override=getattr(args, "depth", None),
            query_text=getattr(args, "query", None) or "",
        )


# ================================================================= inventory

SKIP_NAMES = {"node_modules", "__pycache__", ".venv", "venv", "site-packages", ".cache", ".npm", ".gradle", ".m2",
              ".cargo", ".rustup", "$recycle.bin", ".trash", "system volume information", ".nuget", ".vscode-server",
              ".pyenv", ".conda", "anaconda3", "miniconda3", ".docker", ".android", ".dotnet", ".pytest_cache"}
APPDATA_NAMES = {"appdata", "library", ".config", ".local", "application data", "local settings"}
CRED_NAMES = {".ssh", ".gnupg", ".password-store", "keychains", ".aws", ".azure", ".kube"}
AGENT_FILES = {"claude.md", "agents.md", "gemini.md", ".cursorrules", ".windsurfrules", "copilot-instructions.md"}
AGENT_DIRS = {".claude", ".agents", ".agent", ".cursor", ".windsurf", ".gemini", ".codex", ".continue", ".aider"}
DOC_EXT = {".pdf", ".doc", ".docx", ".odt", ".xls", ".xlsx", ".ods", ".csv", ".ppt", ".pptx", ".txt", ".md", ".rtf"}
MEDIA_EXT = {".jpg", ".jpeg", ".png", ".heic", ".gif", ".mp4", ".mov", ".mp3", ".m4a", ".wav", ".webp"}


def _redact_url(url):
    return re.sub(r"(https?://)[^/@\s]+@", r"\1<redacted>@", url)


def _repo_remote(path):
    try:
        text = read(os.path.join(path, ".git", "config"), 20000)
    except OSError:
        return ""
    m = re.search(r'\[remote "origin"\][^\[]*?url\s*=\s*(\S+)', text)
    return _redact_url(m.group(1)) if m else ""


def _run(cmd, timeout=20):
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace",
                             shell=isinstance(cmd, str))
        return res.stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _programs():
    names = set()
    if os.name == "nt":
        try:
            import winreg
            roots = [(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
                     (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
                     (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall")]
            for hive, path in roots:
                try:
                    key = winreg.OpenKey(hive, path)
                except OSError:
                    continue
                for i in range(winreg.QueryInfoKey(key)[0]):
                    try:
                        sub = winreg.OpenKey(key, winreg.EnumKey(key, i))
                        names.add(str(winreg.QueryValueEx(sub, "DisplayName")[0]).strip())
                    except OSError:
                        continue
        except ImportError:
            pass
    elif sys.platform == "darwin":
        for base in ("/Applications", os.path.expanduser("~/Applications")):
            if os.path.isdir(base):
                names |= {n[:-4] for n in os.listdir(base) if n.endswith(".app")}
    else:
        out = _run(["dpkg-query", "-W", "-f", "${binary:Package}\n"]) or _run(["rpm", "-qa"])
        names |= {l.strip() for l in out.splitlines() if l.strip()}
        for base in ("/usr/share/applications", os.path.expanduser("~/.local/share/applications")):
            if os.path.isdir(base):
                names |= {n[:-8] for n in os.listdir(base) if n.endswith(".desktop")}
    return sorted(n for n in names if n)


def _autostart():
    out = []
    if os.name == "nt":
        try:
            import winreg
            for hive, label in ((winreg.HKEY_CURRENT_USER, "HKCU"), (winreg.HKEY_LOCAL_MACHINE, "HKLM")):
                try:
                    key = winreg.OpenKey(hive, r"Software\Microsoft\Windows\CurrentVersion\Run")
                except OSError:
                    continue
                for i in range(winreg.QueryInfoKey(key)[1]):
                    out.append(f"{label} Run: {winreg.EnumValue(key, i)[0]}")
        except ImportError:
            pass
        sf = os.path.expandvars(r"%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup")
        if os.path.isdir(sf):
            out += [f"Startup folder: {n}" for n in os.listdir(sf)]
    elif sys.platform == "darwin":
        for base in (os.path.expanduser("~/Library/LaunchAgents"), "/Library/LaunchAgents"):
            if os.path.isdir(base):
                out += [f"LaunchAgent: {n}" for n in os.listdir(base)]
    else:
        base = os.path.expanduser("~/.config/autostart")
        if os.path.isdir(base):
            out += [f"autostart: {n}" for n in os.listdir(base)]
    return sorted(out)


def _network():
    if os.name == "nt":
        text = _run(["ipconfig", "/all"])
        wlan = _run(["netsh", "wlan", "show", "profiles"])
        profiles = re.findall(r":\s*(.+)$", "\n".join(l for l in wlan.splitlines() if "All User Profile" in l
                                                      or "Profil für alle Benutzer" in l), re.M)
        return {"config": text[:20000], "wifi_profiles": [p.strip() for p in profiles]}
    text = _run(["ip", "addr"]) or _run(["ifconfig"])
    return {"config": text[:20000], "wifi_profiles": []}


def _agent_homes(user_home):
    out = {}
    for d in sorted(AGENT_DIRS):
        p = os.path.join(user_home, d)
        if not os.path.isdir(p):
            continue
        items = []
        for dp, dns, fns in os.walk(p):
            dns[:] = [x for x in dns if x not in ("cache", "statsig", "shell-snapshots", "todos", "node_modules",
                                                   "file-history", "plugins", "brain", "debug", "telemetry")]
            if os.path.relpath(dp, p).count(os.sep) > 4:
                dns[:] = []
            for fn in fns:
                low = fn.lower()
                if low.endswith((".md", ".json", ".mdc")) and not any(w in low for w in ("credential", "token", "auth")):
                    items.append(fwd(os.path.relpath(os.path.join(dp, fn), user_home)))
        out[d] = items[:400]
    return out


def cmd_inventory(args):
    root = os.path.abspath(os.path.expanduser(args.scan or "~"))
    user_home = os.path.expanduser("~")
    outdir = os.path.abspath(args.out)
    os.makedirs(outdir, exist_ok=True)
    deadline = time.monotonic() + args.budget
    folders, repos, vaults, agent_files, env_files, cloud, creds, skipped = {}, [], [], [], [], [], [], []
    truncated = False

    def walk(path, depth, top):
        nonlocal truncated
        if time.monotonic() > deadline:
            truncated = True
            return
        try:
            entries = list(os.scandir(path))
        except OSError:
            return
        names = {e.name.lower() for e in entries}
        if ".git" in names and depth > 0:
            repos.append({"path": fwd(path), "remote": _repo_remote(path)})
        if ".obsidian" in names:
            vaults.append({"path": fwd(path), "kind": "obsidian"})
        if "logseq" in names and "pages" in names:
            vaults.append({"path": fwd(path), "kind": "logseq"})
        for e in entries:
            low = e.name.lower()
            try:
                if e.is_symlink():
                    continue
                if e.is_dir():
                    full = e.path
                    if low in CRED_NAMES:
                        creds.append({"path": fwd(full)})
                        continue
                    if CLOUD.search(e.name):
                        cloud.append(fwd(full))
                    if low in AGENT_DIRS:
                        agent_files.append(fwd(full) + "/")
                        if depth == 0 and os.path.normcase(os.path.abspath(path)) == \
                                os.path.normcase(os.path.abspath(user_home)):
                            continue  # covered by _agent_homes
                    if low in SKIP_NAMES or low == ".git" or (depth <= 1 and low in APPDATA_NAMES):
                        skipped.append(fwd(full))
                        continue
                    walk(full, depth + 1, top or e.name)
                elif e.is_file():
                    if low in AGENT_FILES:
                        agent_files.append(fwd(e.path))
                    if low == ".env" or low.startswith(".env."):
                        env_files.append(fwd(e.path))
                    st = e.stat()
                    key = top or "(root files)"
                    if depth >= 2 and top:
                        key = top + "/" + fwd(os.path.relpath(path, os.path.join(root, top))).split("/")[0]
                    f = folders.setdefault(key, {"files": 0, "bytes": 0, "docs": 0, "media": 0, "ext": {},
                                                 "newest": 0})
                    ext = os.path.splitext(low)[1]
                    f["files"] += 1
                    f["bytes"] += st.st_size
                    f["docs"] += ext in DOC_EXT
                    f["media"] += ext in MEDIA_EXT
                    f["ext"][ext] = f["ext"].get(ext, 0) + 1
                    f["newest"] = max(f["newest"], st.st_mtime)
            except OSError:
                continue

    walk(root, 0, "")
    system = {"os": platform.platform(), "machine": platform.machine(), "hostname": platform.node(),
              "user": os.environ.get("USERNAME") or os.environ.get("USER", ""), "python": sys.version.split()[0],
              "git": _run(["git", "--version"]).strip(), "shell": os.environ.get("SHELL") or os.environ.get("ComSpec", ""),
              "env_var_names": sorted(os.environ.keys())}
    data = {"created": now_iso(), "root": fwd(root), "truncated_by_time_budget": truncated, "system": system,
            "programs": _programs(), "autostart": _autostart(), "network": _network(),
            "agent_homes": _agent_homes(user_home), "agent_files": sorted(set(agent_files)), "repos": repos,
            "vaults": vaults, "env_files": env_files, "cloud_sync_folders": sorted(set(cloud)),
            "credential_stores_not_opened": creds, "skipped": skipped[:500], "folders": folders}
    dump_json(os.path.join(outdir, "inventory.json"), data)
    md = [f"# Inventory {data['created']}", "", f"Scanned: `{data['root']}` · OS: {system['os']} · "
          f"time budget hit: {truncated}", "", "## Folders (largest first)", "",
          "| Folder | Files | MB | Docs | Media | Top types | Newest |", "|---|---|---|---|---|---|---|"]
    for k, f in sorted(folders.items(), key=lambda kv: -kv[1]["bytes"])[:80]:
        top = ", ".join(f"{e or '(none)'}:{n}" for e, n in sorted(f["ext"].items(), key=lambda x: -x[1])[:4])
        newest = dt.datetime.fromtimestamp(f["newest"]).date().isoformat() if f["newest"] else ""
        md.append(f"| {k} | {f['files']} | {f['bytes'] / 1e6:.1f} | {f['docs']} | {f['media']} | {top} | {newest} |")
    md += ["", f"## Git repositories ({len(repos)})", ""] + [f"- `{r['path']}` {r['remote']}" for r in repos]
    md += ["", f"## Note vaults ({len(vaults)})", ""] + [f"- {v['kind']}: `{v['path']}`" for v in vaults]
    md += ["", "## Agent files and folders", ""] + [f"- `{a}`" for a in data["agent_files"][:200]]
    md += ["", "## Cloud-sync folders (never move into or out of these)", ""] + [f"- `{c}`" for c in data["cloud_sync_folders"]]
    md += ["", "## Credential stores (not opened)", ""] + [f"- `{c['path']}`" for c in creds]
    md += ["", f"## .env files (not read): {len(env_files)}", ""] + [f"- `{e}`" for e in env_files[:50]]
    md += ["", f"## Programs: {len(data['programs'])}", "", ", ".join(data["programs"][:400])]
    md += ["", "## Autostart", ""] + [f"- {a}" for a in data["autostart"]]
    md += ["", "## Wi-Fi profiles (names only)", "", ", ".join(data["network"]["wifi_profiles"]) or "(none found)"]
    write(os.path.join(outdir, "inventory.md"), "\n".join(md) + "\n")
    say(f"Inventory written to {fwd(outdir)} (inventory.md, inventory.json); {len(folders)} folders, "
        f"{len(repos)} repos, {len(vaults)} vaults{', TRUNCATED by time budget' if truncated else ''}")


# ================================================================= move / verify / undo

def _protected(path, brain, cloud_inbox=None):
    p = os.path.abspath(path)
    low = fwd(p).lower()
    if CLOUD.search(p) and not (cloud_inbox and inside(p, cloud_inbox) and p != os.path.abspath(cloud_inbox)):
        return "inside a cloud-sync folder (only the cloud inbox named in settings.md may be emptied)"
    brain_low = fwd(os.path.abspath(brain)).lower()
    if low == brain_low or low.startswith(brain_low + "/_atlas") or "/.git/" in low + "/":
        return "Atlas core or Git internals"
    if os.name == "nt":
        win = fwd(os.environ.get("WINDIR", "C:/Windows")).lower()
        bad = [win, "c:/program files", "c:/program files (x86)", "c:/programdata"]
        if any(low == b or low.startswith(b + "/") for b in bad) or "/appdata/" in low + "/":
            return "system, program or application-data folder"
    else:
        for b in ("/system", "/library", "/applications", "/usr", "/etc", "/bin", "/sbin", "/lib", "/opt", "/var",
                  "/boot", "/proc", "/dev"):
            if low == b or low.startswith(b + "/"):
                return "system or program folder"
        uh = fwd(os.path.expanduser("~")).lower()
        for b in ("/library", "/.config", "/.local"):
            if low.startswith(uh + b):
                return "application-data folder"
    parent = os.path.dirname(p)
    while parent and parent != os.path.dirname(parent):
        if os.path.isdir(os.path.join(parent, ".git")) and fwd(parent).lower() != brain_low:
            return f"inside Git repository {fwd(parent)} (a repo moves only as a whole)"
        parent = os.path.dirname(parent)
    return None


def _same_volume(a, b):
    try:
        return os.stat(a).st_dev == os.stat(os.path.dirname(os.path.abspath(b)) or ".").st_dev
    except OSError:
        return False


def cmd_move(args):
    _root, brain = paths(args)
    plan = load_json(args.plan, {})
    sections = plan.get("sections") or [{"name": "all", "operations": plan.get("operations", [])}]
    if args.section:
        sections = [s for s in sections if s.get("name") == args.section]
        if not sections:
            sys.exit(f"No section named {args.section!r} in the plan.")
    ops, problems = [], []
    cloud_inbox = hook_module(brain).settings().get("inbox_cloud", "").strip()
    cloud_inbox = os.path.abspath(os.path.expanduser(cloud_inbox)) if cloud_inbox else None
    for s in sections:
        for op in s.get("operations", []):
            kind = op.get("op", "move")
            src = os.path.abspath(os.path.expanduser(op["src"]))
            dst = os.path.abspath(os.path.expanduser(op["dst"]))
            why = _protected(src, brain, cloud_inbox) if kind == "move" else None
            if kind not in ("move", "copy"):
                problems.append(f"{src}: unknown op {kind}")
            elif not os.path.exists(src):
                problems.append(f"{src}: does not exist")
            elif os.path.exists(dst):
                problems.append(f"{dst}: already exists (Atlas never overwrites)")
            elif why:
                problems.append(f"{src}: protected source ({why})")
            elif CLOUD.search(dst):
                problems.append(f"{dst}: target inside a cloud-sync folder")
            elif fwd(dst).lower().startswith(fwd(src).lower() + "/"):
                problems.append(f"{dst}: target inside its own source")
            else:
                ops.append((s.get("name", ""), kind, src, dst))
    say(f"move: {len(ops)} operation(s) valid, {len(problems)} problem(s)")
    for p in problems:
        say(f"  PROBLEM {p}")
    for sec, kind, src, dst in ops[:200]:
        say(f"  [{sec}] {kind}: {fwd(src)} -> {fwd(dst)}")
    if problems:
        sys.exit("Fix the plan first; nothing was changed.")
    if not args.execute:
        say("Dry run. Add --execute (and --section NAME for one approved section).")
        return
    rid = run_id(brain, "move-" + (args.section or "all"))
    jdir = os.path.join(brain, "_atlas", "migrations", rid)
    os.makedirs(jdir, exist_ok=True)
    journal = os.path.join(jdir, "journal.jsonl")
    done = 0
    for sec, kind, src, dst in ops:
        before = manifest(src)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        entry = {"time": now_iso(), "section": sec, "op": kind, "src": fwd(src), "dst": fwd(dst), "files": len(before)}
        if kind == "move" and _same_volume(src, dst):
            os.rename(src, dst)
            entry["method"] = "rename"
        else:
            if os.path.isdir(src):
                shutil.copytree(src, dst, copy_function=shutil.copy2)
            else:
                shutil.copy2(src, dst)
            entry["method"] = "copy"
        after = manifest(dst)
        if after != before:
            entry["status"] = "VERIFY-FAILED"
            _append(journal, entry)
            sys.exit(f"Checksum mismatch for {dst}. Stopped; source untouched where copied. Journal: {journal}")
        if kind == "move" and entry["method"] == "copy":
            recovery = f"{src}.atlas-recovery-{rid}"
            os.rename(src, recovery)
            entry["recovery"] = fwd(recovery)
        entry["status"] = "ok"
        entry["sha256"] = hashlib.sha256(json.dumps(after, sort_keys=True).encode()).hexdigest()
        _append(journal, entry)
        done += 1
    tool = fwd(os.path.join(brain, "_atlas", "tools", "atlas.py"))
    write(os.path.join(jdir, "README.md"),
          f"# Move run {rid}\n\n{done} operation(s). Undo: `python -I {tool} undo --journal {fwd(journal)} --execute`.\n"
          f"Cross-volume moves left the source as `*.atlas-recovery-{rid}`; delete those only after checking the "
          "targets (`atlas.py verify`).\n")
    say(f"Done: {done} operation(s). Journal: {fwd(journal)}")


def _append(path, entry):
    with open(path, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _journal(path):
    with open(path, encoding="utf-8") as fh:
        return [json.loads(l) for l in fh if l.strip()]


def cmd_verify(args):
    bad = 0
    for e in _journal(args.journal):
        if e.get("status") != "ok":
            continue
        ok = os.path.exists(e["dst"]) and hashlib.sha256(json.dumps(manifest(e["dst"]), sort_keys=True).encode()) \
            .hexdigest() == e.get("sha256")
        bad += not ok
        say(f"{'OK  ' if ok else 'FAIL'} {e['dst']}")
    sys.exit(1 if bad else 0)


def cmd_undo(args):
    entries = [e for e in _journal(args.journal) if e.get("status") == "ok"]
    undo_id = os.path.basename(os.path.dirname(os.path.abspath(args.journal)))
    steps = []
    for e in reversed(entries):
        if e["op"] == "move" and e.get("method") == "rename":
            steps.append((e["dst"], e["src"]))
        elif e["op"] == "move":
            steps.append((e["recovery"], e["src"]))
            steps.append((e["dst"], f"{e['dst']}.atlas-undone-{undo_id}"))
        else:
            steps.append((e["dst"], f"{e['dst']}.atlas-undone-{undo_id}"))
    for a, b in steps:
        say(f"  {a} -> {b}")
    if not args.execute:
        say("Dry run. Add --execute to undo. Nothing is deleted; copies are renamed to *.atlas-undone-*.")
        return
    for a, b in steps:
        if os.path.exists(b):
            sys.exit(f"Stopped: {b} exists.")
        os.makedirs(os.path.dirname(b), exist_ok=True)
        os.rename(a, b)
    say(f"Undone: {len(steps)} step(s).")


# ================================================================= core-diff / core-apply

BASE = ".base"            # _atlas/.base: the upstream core as last installed (the "base" of a three-way update)
ROUTER_BASE = "__router_core__.md"
CORE_SKIP_DIRS = ("memory", "migrations", "cache", "__pycache__", BASE)


def _core_files(base):
    out = {}
    if not os.path.isdir(base):
        return out
    for dp, dns, fns in os.walk(base):
        dns[:] = [d for d in dns if d not in CORE_SKIP_DIRS]
        for fn in fns:
            if fn.startswith("state.json") or fn.startswith("conflicts.json") or fn == ROUTER_BASE or fn.endswith((".pyc", ".pyo")):
                continue
            full = os.path.join(dp, fn)
            out[fwd(os.path.relpath(full, base))] = sha256_file(full)
    return out


def _source_core(src, modules):
    files = {}
    for base in [os.path.join(src, "core")] + [os.path.join(src, "modules", m, "core") for m in modules]:
        if os.path.isdir(base):
            for rel, h in _core_files(base).items():
                files[rel] = (h, os.path.join(base, rel))
    return files


def _vtuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", v or "0")[:3]) or (0,)


def _router_values(brain, root):
    st = get_state(brain)
    return {"ROOT": fwd(root), "USER_NAME": st.get("user_name") or "the user",
            "LANGUAGE": st.get("language") or "English", "DATE": (st.get("installed") or now_iso())[:10]}


def _router_core(text):
    a, b = "<!-- atlas:core:start -->", "<!-- atlas:core:end -->"
    if a in text and b in text:
        return text[text.index(a): text.index(b) + len(b)]
    return None


def _write_base(brain, src, modules, root):
    """Remember the upstream core (and the rendered router core block) this Atlas now runs on."""
    base = os.path.join(brain, "_atlas", BASE)
    for rel, (_h, path) in _source_core(src, modules).items():
        os.makedirs(os.path.dirname(os.path.join(base, rel)), exist_ok=True)
        shutil.copy2(path, os.path.join(base, rel))
    block = _router_core(render(read(os.path.join(src, "core", "templates", "router.md")), _router_values(brain, root)))
    if block:
        write(os.path.join(base, ROUTER_BASE), block + "\n")


def _three_way(local, base, upstream):
    """Decision for one file (hashes or texts; None = missing)."""
    if local == upstream:
        return "same"
    if upstream is None:
        return "remove" if base is not None and local == base else "keep-local"
    if local is None:
        return "add"
    if base is None or local == base:
        return "update"
    if upstream == base:
        return "keep-local"
    return "conflict"


def _merge_text(local_path, base_path, upstream_path):
    """git merge-file: (merged text, clean?)."""
    res = subprocess.run(["git", "merge-file", "-p", "-L", "yours (this Atlas)", "-L", "base", "-L", "upstream",
                          local_path, base_path, upstream_path], capture_output=True, text=True, encoding="utf-8",
                         errors="replace")
    return res.stdout, res.returncode == 0


def _upgrades(src, v_from, v_to):
    """Declarative upgrade steps from core/upgrades/<version>.json for v_from < version <= v_to."""
    udir = os.path.join(src, "core", "upgrades")
    out = []
    if os.path.isdir(udir):
        for fn in os.listdir(udir):
            if fn.endswith(".json"):
                v = fn[:-5]
                if _vtuple(v_from) < _vtuple(v) <= _vtuple(v_to):
                    out.append((_vtuple(v), v, load_json(os.path.join(udir, fn), {})))
    return [(v, data) for _t, v, data in sorted(out)]


def _ensure_coach_index_link(brain, execute=True):
    idx_file = os.path.join(brain, "INDEX.md")
    if not os.path.isfile(idx_file):
        return False
    idx_text = read(idx_file)
    if "coach/README.md" in idx_text:
        return False
    if execute:
        row = "| `coach/` | Sport-Coach hub: athlete profile, thresholds, macro periodization, 3-epoch power curve, activity log | `coach/README.md` |\n"
        if "| `calendar/` |" in idx_text:
            idx_text = idx_text.replace("| `calendar/` |", row + "| `calendar/` |", 1)
        elif "| `self/` |" in idx_text:
            idx_text = idx_text.replace("| `self/` |", row + "| `self/` |", 1)
        else:
            idx_text = idx_text.rstrip() + "\n- Sport-Coach Hub: `coach/README.md`\n"
        write(idx_file, idx_text)
    return True


def _apply_upgrade(brain, src, values, data, dry):
    """Mechanical, additive and idempotent: never overwrites the user's content. Returns (actions, notes)."""
    acts = []
    for d in data.get("dirs", []):
        if not os.path.isdir(os.path.join(brain, d)):
            acts.append(f"create folder {d}/")
            if not dry:
                os.makedirs(os.path.join(brain, d), exist_ok=True)
    for rel in data.get("skeleton_add", []):
        dst, s = os.path.join(brain, rel), os.path.join(src, "skeleton", rel)
        if not os.path.exists(dst) and os.path.exists(s):
            acts.append(f"add {rel} (from the skeleton)")
            if not dry:
                write(dst, render(read(s), values))
    for m in data.get("modules_add", []):
        st = get_state(brain)
        mods = list(st.get("modules", []))
        if m not in mods:
            acts.append(f"enable module `{m}`")
            if not dry:
                mods.append(m)
                set_state(brain, modules=mods)
        mhome = os.path.join(src, "modules", m, "home")
        if os.path.isdir(mhome):
            for s_path, d_path in _copy_plan(mhome, brain):
                rel_d = fwd(os.path.relpath(d_path, brain))
                if not os.path.exists(d_path):
                    acts.append(f"add {rel_d} (from module {m})")
                    if not dry:
                        os.makedirs(os.path.dirname(d_path), exist_ok=True)
                        if d_path.endswith((".md", ".gitignore", ".ignore")):
                            write(d_path, render(read(s_path), values))
                        else:
                            shutil.copy2(s_path, d_path)
        if m == "coach":
            if _ensure_coach_index_link(brain, execute=not dry):
                acts.append("INDEX.md: link `coach/README.md`")
    readme_coach = os.path.join(brain, "coach", "README.md")
    rdy_coach = os.path.join(brain, "coach", "readiness.md")
    pb_coach = os.path.join(brain, "coach", "playbook.md")
    if os.path.isfile(readme_coach):
        rc_txt = read(readme_coach)
        need_link = (
            ("readiness.md" not in rc_txt and (os.path.isfile(rdy_coach) or "coach/readiness.md" in data.get("skeleton_add", [])))
            or ("playbook.md" not in rc_txt and (os.path.isfile(pb_coach) or "coach/playbook.md" in data.get("skeleton_add", [])))
        )
        if need_link:
            acts.append("coach/README.md: link `readiness.md` / `playbook.md`")
            if not dry:
                _ensure_coach_readiness_link(brain, execute=True)
    sp = os.path.join(brain, "settings.md")
    for key, val in (data.get("settings_add") or {}).items():
        if os.path.exists(sp):
            text = read(sp)
            end = text.find("\n---", 3)
            if end > 0 and not re.search(rf"(?m)^{re.escape(key)}\s*:", text[:end]):
                acts.append(f"settings.md: add `{key}: {val.split('#')[0].strip()}` (default)")
                if not dry:
                    write(sp, text[:end] + f"\n{key}: {val}" + text[end:])
    tp = os.path.join(brain, "tags.md")
    for row in data.get("tags_add", []):
        if os.path.exists(tp) and f"**{row[0]}**" not in read(tp):
            acts.append(f"tags.md: add tag `{row[0]}`")
            if not dry:
                write(tp, read(tp).rstrip("\n") + f"\n| **{row[0]}** | {row[1]} | {row[2] if len(row) > 2 else ''} |\n")
    return acts, list(data.get("notes", []))


HARD_CORE_PREFIXES = (
    "tools/", "hooks/", "skills/", "agents/", "upgrades/",
    "templates/calendar.html", "VERSION", "charter.md", "adapters.md",
)


def _plan_core(brain, root, src, hard=False):
    st = get_state(brain)
    modules = list(st.get("modules", []))
    atlas_dir = os.path.join(brain, "_atlas")
    try:
        v_old = read(os.path.join(atlas_dir, "VERSION")).strip()
        v_new = read(os.path.join(src, "core", "VERSION")).strip()
    except OSError:
        v_old, v_new = "0", "0"
    for _v, udata in _upgrades(src, v_old, v_new):
        for m in udata.get("modules_add", []):
            if m not in modules:
                modules.append(m)
    new = _source_core(src, modules)
    local = _core_files(atlas_dir)
    base = _core_files(os.path.join(atlas_dir, BASE))
    items = []
    for rel in sorted(set(new) | set(local) | set(base)):
        if rel.startswith("upgrades/") and rel not in new:
            continue
        up_h = new.get(rel, (None, None))[0]
        loc_h = local.get(rel)
        if (hard or rel.startswith(HARD_CORE_PREFIXES)) and up_h is not None:
            decision = "same" if loc_h == up_h else ("add" if loc_h is None else "update")
        else:
            decision = _three_way(loc_h, base.get(rel), up_h)
        items.append((rel, decision))
    # The router's core block (in brain/ROUTER.md) is always updated to upstream; personal rules after <!-- atlas:core:end --> stay untouched.
    router_path = os.path.join(brain, "ROUTER.md")
    local_block = _router_core(read(router_path)) if os.path.exists(router_path) else None
    up_block = _router_core(render(read(os.path.join(src, "core", "templates", "router.md")),
                                   _router_values(brain, root)))
    bp = os.path.join(atlas_dir, BASE, ROUTER_BASE)
    if os.path.exists(bp):
        base_block = read(bp).strip()
    else:  # Atlas installed before 0.4: the installed template is the base
        tpl = os.path.join(atlas_dir, "templates", "router.md")
        base_block = _router_core(render(read(tpl), _router_values(brain, root))) if os.path.exists(tpl) else None
    if local_block is None:
        router = "missing"
    elif local_block == up_block or up_block is None:
        router = "same"
    else:
        router = "update"
    return {"items": items, "new": new, "router": (router, local_block, base_block, up_block), "v_old": v_old,
            "v_new": v_new, "modules": modules}


def _changelog(src, v_old, v_new):
    p = os.path.join(src, "CHANGELOG.md")
    if not os.path.exists(p):
        return []
    out, take = [], False
    for line in read(p).splitlines():
        m = re.match(r"^##\s*\[?v?(\d+\.\d+\.\d+)", line)
        if m:
            take = _vtuple(v_old) < _vtuple(m.group(1)) <= _vtuple(v_new)
        if take:
            out.append(line)
    return out


def _resolve_source(brain, src_arg):
    """Resolve a local life-atlas path or clone upstream into a temporary folder."""
    if src_arg and os.path.isdir(src_arg):
        return os.path.abspath(src_arg), None
    st = get_state(brain)
    url = src_arg or st.get("upstream") or DEFAULT_UPSTREAM
    if os.path.isdir(url):
        return os.path.abspath(url), None
    tmp = tempfile.mkdtemp(prefix="atlas-up-")
    res = subprocess.run(["git", "clone", "--depth", "1", url, tmp], capture_output=True, text=True)
    if res.returncode != 0:
        shutil.rmtree(tmp, ignore_errors=True)
        sys.exit(f"Could not clone upstream {url}: {(res.stderr or res.stdout).strip()}")
    return tmp, tmp


def cmd_core_diff(args, apply=False):
    root, brain = paths(args)
    src, cleanup = _resolve_source(brain, getattr(args, "source", None))
    try:
        if apply and args.execute and not getattr(args, "bootstrapped", False):
            src_tool = os.path.join(src, "core", "tools", "atlas.py")
            cur_tool = os.path.abspath(__file__)
            if os.path.isfile(src_tool) and sha256_file(src_tool) != sha256_file(cur_tool):
                try:
                    compile(read(src_tool), src_tool, "exec")
                    src_ok = True
                except Exception:
                    src_ok = False
                if src_ok:
                    cmd = [sys.executable, "-I", "-B", src_tool, "--root", root, "core-apply", "--source", src,
                           "--execute", "--bootstrapped"]
                    if getattr(args, "user_home", None):
                        cmd += ["--user-home", args.user_home]
                    if getattr(args, "hard", False):
                        cmd.append("--hard")
                    res = subprocess.run(cmd)
                    sys.exit(res.returncode)
        plan = _plan_core(brain, root, src, hard=getattr(args, "hard", False))
        groups = {}
        for rel, what in plan["items"]:
            groups.setdefault(what, []).append(rel)
        router = plan["router"][0]
        ups = _upgrades(src, plan["v_old"], plan["v_new"])
        values = _router_values(brain, root)
        say(f"update: installed {plan['v_old']} -> source {plan['v_new']}")
        labels = {"update": "update (upstream changed, yours untouched)", "add": "new from upstream",
                  "remove": "dropped upstream", "keep-local": "kept: changed only in this Atlas",
                  "conflict": "CONFLICT: changed here and upstream -> three-way merge"}
        for what in ("update", "add", "remove", "keep-local", "conflict"):
            for rel in groups.get(what, []):
                say(f"  {labels[what]}: _atlas/{rel}")
        if router not in ("same", "missing"):
            say(f"  router core block: {labels.get(router, router)} (your personal part always stays)")
        notes = []
        for v, data in ups:
            acts, n = _apply_upgrade(brain, src, values, data, dry=True)
            for a in acts:
                say(f"  upgrade {v}: {a}")
            notes += [f"{v}: {x}" for x in n]
        for x in notes:
            say(f"  agent task {x}")
        log = _changelog(src, plan["v_old"], plan["v_new"])
        if log:
            say("changelog:\n  " + "\n  ".join(log[:40]))
        pending = [w for _r, w in plan["items"] if w not in ("same",)] or router not in ("same", "missing") or ups
        if not pending:
            if apply and args.execute:
                _git(brain, "commit", "-q", "--allow-empty", "-m", "atlas: update-check")
            say("up to date: nothing to apply")
            return
        if not apply:
            return
        if not args.execute:
            say("Dry run. Add --execute: backs up, applies, re-wires, probes, and rolls back by itself if a probe fails.")
            return
        _core_apply(args, root, brain, src, plan, ups, values)
    finally:
        if cleanup:
            shutil.rmtree(cleanup, ignore_errors=True)


def _core_apply(args, root, brain, src, plan, ups, values):
    atlas_dir = os.path.join(brain, "_atlas")
    rid = run_id(brain, "core")
    rdir = os.path.join(atlas_dir, "migrations", rid)
    bdir = os.path.join(rdir, "core-backup")
    for sub in ("", BASE):  # back up the whole current core and its base
        srcdir = os.path.join(atlas_dir, sub)
        for rel in _core_files(srcdir) if sub == "" else list(_core_files(srcdir)) + [ROUTER_BASE]:
            p = os.path.join(srcdir, rel)
            if os.path.exists(p):
                os.makedirs(os.path.dirname(os.path.join(bdir, sub, rel)), exist_ok=True)
                shutil.copy2(p, os.path.join(bdir, sub, rel))
    for rel in ("ROUTER.md", "settings.md", "tags.md"):
        if os.path.exists(os.path.join(brain, rel)):
            os.makedirs(os.path.join(rdir, "grown-backup"), exist_ok=True)
            shutil.copy2(os.path.join(brain, rel), os.path.join(rdir, "grown-backup", rel))
    result = {"updated": [], "added": [], "removed": [], "kept": [], "merged": [], "conflicts": []}
    for rel, what in plan["items"]:
        cur = os.path.join(atlas_dir, rel)
        up = plan["new"].get(rel, (None, None))[1]
        if what in ("update", "add"):
            os.makedirs(os.path.dirname(cur), exist_ok=True)
            shutil.copy2(up, cur)
            result["updated" if what == "update" else "added"].append(rel)
        elif what == "remove":
            os.remove(cur)
            result["removed"].append(rel)
        elif what == "keep-local":
            result["kept"].append(rel)
        elif what == "conflict":
            basef = os.path.join(atlas_dir, BASE, rel)
            merged, clean = _merge_text(cur, basef, up) if os.path.exists(basef) else ("", False)
            if clean:
                write(cur, merged)
                result["merged"].append(rel)
            else:
                cdir = os.path.join(rdir, "conflicts")
                os.makedirs(os.path.dirname(os.path.join(cdir, rel)), exist_ok=True)
                if merged:
                    write(os.path.join(cdir, rel), merged)
                shutil.copy2(up, os.path.join(cdir, rel + ".upstream"))
                result["conflicts"].append(rel)
    router, local_block, base_block, up_block = plan["router"]
    rp = os.path.join(brain, "ROUTER.md")
    if router in ("update", "conflict") and up_block:
        new_block = up_block
        if router == "conflict" and base_block:
            tmp = os.path.join(rdir, "router")
            os.makedirs(tmp, exist_ok=True)
            for name, text in (("local", local_block), ("base", base_block), ("up", up_block)):
                write(os.path.join(tmp, name), text + "\n")
            merged, clean = _merge_text(*(os.path.join(tmp, n) for n in ("local", "base", "up")))
            new_block = merged.strip() if clean else None
            (result["merged"] if clean else result["conflicts"]).append("ROUTER.md core block")
        else:
            result["updated"].append("ROUTER.md core block")
        if new_block:
            text = read(rp)
            write(rp, text.replace(local_block, new_block))
    notes = []
    for v, data in ups:
        acts, n = _apply_upgrade(brain, src, values, data, dry=False)
        result.setdefault("upgrades", []).extend(f"{v}: {a}" for a in acts)
        notes += [f"{v}: {x}" for x in n]
    try:
        hk_new = hook_module(brain)
        _coach_init(root, brain, hk_new, execute=True, quiet=True, preserve_existing_profile=True)
        act_json_p = os.path.join(brain, "coach", "activities.json")
        if os.path.isfile(act_json_p):
            raw_acts = load_json(act_json_p, [])
            acts_list = raw_acts.get("activities", []) if isinstance(raw_acts, dict) else (raw_acts if isinstance(raw_acts, list) else [])
            if acts_list:
                ftp_w_u, w_kg_u = _extract_profile_numbers(brain, hk_new)
                _rebuild_coach_markdown_files(brain, acts_list, ftp_watts=ftp_w_u, weight_kg=w_kg_u)
        if os.path.isfile(os.path.join(atlas_dir, "cache", "calendar.html")):
            hk_new.render_calendar_html(hk_new.load_index(), start_server=False)
    except Exception:
        pass
    # The new base is the upstream we just considered; unresolved conflicts keep their old base until accepted.
    unresolved = set(result["conflicts"])
    base_dir = os.path.join(atlas_dir, BASE)
    for rel, (_h, path) in plan["new"].items():
        if rel not in unresolved:
            os.makedirs(os.path.dirname(os.path.join(base_dir, rel)), exist_ok=True)
            shutil.copy2(path, os.path.join(base_dir, rel))
    for rel in result["removed"]:
        if os.path.exists(os.path.join(base_dir, rel)):
            os.remove(os.path.join(base_dir, rel))
    if up_block and "ROUTER.md core block" not in unresolved:
        write(os.path.join(base_dir, ROUTER_BASE), up_block + "\n")
    set_state(brain, version=plan["v_new"], last_update_check=now_iso())
    dump_json(os.path.join(rdir, "applied.json"), {"source": fwd(src), "from": plan["v_old"], "to": plan["v_new"],
                                                   "result": result, "notes": notes})
    # Re-wire with the NEW tool and probe every wired agent; any failure rolls everything back.
    tool = os.path.join(atlas_dir, "tools", "atlas.py")
    st = get_state(brain)
    wired = dict(st.get("wired", {})) if isinstance(st.get("wired"), dict) else {}
    # Auto-detect project-scoped agent wiring in root if state.json lost it
    if "antigravity:project" not in wired and (
        os.path.isfile(os.path.join(root, "GEMINI.md"))
        or os.path.isfile(os.path.join(root, ".agents", "hooks.json"))
        or os.path.isdir(os.path.join(root, ".agents", "skills"))
    ):
        g_md = read(os.path.join(root, "GEMINI.md")) if os.path.isfile(os.path.join(root, "GEMINI.md")) else ""
        wired["antigravity:project"] = {
            "hooks": fwd(os.path.join(root, ".agents", "hooks.json")),
            "python": fwd(sys.executable),
            "router_mode": "copy" if "atlas:router:start" in g_md else "include",
        }
    if "claude:project" not in wired and (
        os.path.isfile(os.path.join(root, "CLAUDE.md"))
        or os.path.isfile(os.path.join(root, ".claude", "settings.json"))
        or os.path.isdir(os.path.join(root, ".claude", "skills"))
    ):
        wired["claude:project"] = {
            "hooks": fwd(os.path.join(root, ".claude", "settings.json")),
            "python": fwd(sys.executable),
            "router_mode": "include",
        }
    failed = []
    for key, info in wired.items():
        agent, scope = key.split(":")
        rmode = (info or {}).get("router_mode") or "include"
        if agent == "antigravity" and scope == "project" and os.path.isfile(os.path.join(root, "GEMINI.md")):
            if "atlas:router:start" in read(os.path.join(root, "GEMINI.md")):
                rmode = "copy"
        base_cmd = [sys.executable, "-I", "-B", tool, "--root", root]
        r1 = subprocess.run(base_cmd + ["wire", "--agent", agent, "--scope", scope, "--router-mode", rmode, "--execute"] +
                            (["--user-home", args.user_home] if getattr(args, "user_home", None) else []),
                            capture_output=True, text=True, encoding="utf-8", errors="replace")
        r2 = subprocess.run(base_cmd + ["probe", "--agent", agent, "--scope", scope] +
                            (["--user-home", args.user_home] if getattr(args, "user_home", None) else []),
                            capture_output=True, text=True, encoding="utf-8", errors="replace")
        if r1.returncode or r2.returncode:
            failed.append(f"{key}: {(r1.stdout + r1.stderr + r2.stdout + r2.stderr).strip()[-400:]}")
    if failed:
        _restore(brain, rdir)
        for key in wired:
            agent, scope = key.split(":")
            subprocess.run([sys.executable, "-I", "-B", tool, "--root", root, "wire", "--agent", agent, "--scope", scope,
                            "--execute"] + (["--user-home", args.user_home] if getattr(args, "user_home", None) else []),
                           capture_output=True, text=True)
        say("ROLLED BACK: the new core failed its probe; this Atlas runs on the previous version again.")
        for f in failed:
            say(f"  {f}")
        sys.exit(1)
    _git(brain, "add", "-A")
    _git(brain, "commit", "-q", "-m", f"atlas: update-check core {plan['v_old']} -> {plan['v_new']}")
    for k, v in result.items():
        if v:
            say(f"{k}: {', '.join(v)}")
    for n in notes:
        say(f"agent task {n}")
    if result["conflicts"]:
        say(f"Conflicts need a merge: see _atlas/migrations/{rid}/conflicts/ (merged text with markers, *.upstream). "
            "After merging run `atlas.py core-accept --source <checkout> <file>`.")
    say(f"Applied {plan['v_old']} -> {plan['v_new']}, wired and probed. Rollback id: {rid}.")


def _restore(brain, rdir):
    atlas_dir = os.path.join(brain, "_atlas")
    info = load_json(os.path.join(rdir, "applied.json"), {})
    res = info.get("result", {})
    for rel in res.get("added", []):
        if os.path.exists(os.path.join(atlas_dir, rel)):
            os.remove(os.path.join(atlas_dir, rel))
    bdir = os.path.join(rdir, "core-backup")
    for dp, _dns, fns in os.walk(bdir):
        for fn in fns:
            full = os.path.join(dp, fn)
            dst = os.path.join(atlas_dir, os.path.relpath(full, bdir))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(full, dst)
    gdir = os.path.join(rdir, "grown-backup")
    if os.path.isdir(gdir):
        for fn in os.listdir(gdir):
            shutil.copy2(os.path.join(gdir, fn), os.path.join(brain, fn))
    if info.get("from"):
        set_state(brain, version=info["from"])


def cmd_core_rollback(args):
    _root, brain = paths(args)
    rdir = os.path.join(brain, "_atlas", "migrations", args.rollback)
    if not os.path.exists(os.path.join(rdir, "applied.json")):
        sys.exit(f"No applied.json in {rdir}")
    _restore(brain, rdir)
    say(f"Rolled back {args.rollback}. Re-wire each agent (`atlas.py wire --agent … --execute`) and probe.")


def cmd_core_accept(args):
    """After merging a conflict by hand: take the upstream version of these files as the new base."""
    root, brain = paths(args)
    src, cleanup = _resolve_source(brain, getattr(args, "source", None))
    try:
        plan_new = _source_core(src, get_state(brain).get("modules", []))
        for rel in args.files:
            rel = fwd(rel).replace("_atlas/", "", 1) if rel.startswith("_atlas/") else fwd(rel)
            if rel == "ROUTER.md":
                block = _router_core(render(read(os.path.join(src, "core", "templates", "router.md")),
                                            _router_values(brain, root)))
                write(os.path.join(brain, "_atlas", BASE, ROUTER_BASE), block + "\n")
            elif rel in plan_new:
                dst = os.path.join(brain, "_atlas", BASE, rel)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(plan_new[rel][1], dst)
            else:
                say(f"  not in the source core: {rel}")
                continue
            say(f"  accepted: {rel}")
    finally:
        if cleanup:
            shutil.rmtree(cleanup, ignore_errors=True)


# ================================================================= Atlas Dev (maintainers only)

def can_push(url):
    """(True/False, how): does this user have write access to the life-atlas repository?"""
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never", GIT_ASKPASS="echo")
    m = re.match(r"^(?:https://github\.com/|git@github\.com:)([^/]+)/([^/.]+)", url or "")
    if m and shutil.which("gh"):
        res = subprocess.run(["gh", "api", f"repos/{m.group(1)}/{m.group(2)}", "--jq", ".permissions.push"],
                             capture_output=True, text=True, timeout=30, env=env)
        if res.returncode == 0 and res.stdout.strip() in ("true", "false"):
            return res.stdout.strip() == "true", "GitHub API (gh)"
    import tempfile
    tmp = tempfile.mkdtemp(prefix="atlas-push-probe-")
    try:
        subprocess.run(["git", "init", "-q", tmp], capture_output=True, env=env)
        subprocess.run(["git", "-C", tmp, "-c", "user.name=atlas", "-c", "user.email=atlas@localhost", "commit", "-q",
                        "--allow-empty", "-m", "probe"], capture_output=True, env=env)
        res = subprocess.run(["git", "-C", tmp, "push", "--dry-run", "-q", url,
                              "HEAD:refs/heads/atlas-permission-probe"], capture_output=True, text=True, timeout=60,
                             env=env, encoding="utf-8", errors="replace")
        return res.returncode == 0, "git push --dry-run"
    except (OSError, subprocess.SubprocessError):
        return False, "git push --dry-run (failed to run)"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def cmd_dev_check(args):
    _root, brain = paths(args)
    url = args.repo or get_state(brain).get("upstream")
    if not url:
        sys.exit("No repository: pass --repo or set `atlas.py state --set upstream=<url>`.")
    ok, how = can_push(url)
    say(f"push rights to {url}: {'yes' if ok else 'no'} ({how})")
    sys.exit(0 if ok else 2)


DEV_PAGE = """---
summary: Developing Atlas itself: the life-atlas source repository, tests, test Atlas, releases
tags: [project, atlas]
aliases: [Atlas Dev, atlas-dev, life-atlas, Atlas weiterentwickeln, Atlas entwickeln, develop Atlas]
status: active
path: {path}
remote: {url}
---
# Project: Atlas Dev

Stand: {date}. The source of Atlas. This Atlas is user number one: it is never edited in `brain/_atlas/` directly;
changes are made here, tested, released, and then arrive through `/atlas-update` like for everyone else.

## Never miss

- Work only in `{path}` (the repository), never in `brain/_atlas/` of a running Atlas.
- Follow `CONTRIBUTING.md` in the repository: tests (`python -I tests/run.py`) must pass before a push.
- Every change to wiring or hook formats is checked against the official docs listed in `core/adapters.md`.
- A release bumps `core/VERSION`, adds a `CHANGELOG.md` entry and, if grown files need additions, an
  `core/upgrades/<version>.json`.
- Try changes live in the test Atlas first: `python -I dev/sandbox.py` (separate folder, own project/workspace).

## Work

| Task | How |
|---|---|
| Tests | `python -I tests/run.py` |
| Test Atlas (create or update from the working copy) | `python -I dev/sandbox.py`, then open the printed folder as a project |
| Release | `CONTRIBUTING.md` → Release |
| Update this Atlas to the new version | `/atlas-update` |
"""


def cmd_dev_setup(args):
    """Only for people who may push to the life-atlas repository: clone it into ROOT/code and register Atlas Dev."""
    root, brain = paths(args)
    url = args.repo or get_state(brain).get("upstream")
    if not url:
        sys.exit("No repository: pass --repo or set `atlas.py state --set upstream=<url>`.")
    ok, how = can_push(url)
    if not ok:
        say(f"No push rights to {url} ({how}): Atlas Dev is only set up for maintainers. Nothing changed.")
        return
    target = os.path.join(root, "code", "life-atlas")
    page = os.path.join(brain, "projects", "atlas-dev", "README.md")
    say(f"Atlas Dev: push rights confirmed ({how}).")
    say(f"  clone {url} -> {fwd(target)}" if not os.path.isdir(target) else f"  repository already at {fwd(target)}")
    say(f"  project page {fwd(os.path.relpath(page, root))}" + (" (exists, kept)" if os.path.exists(page) else ""))
    if not args.execute:
        say("Dry run. Add --execute.")
        return
    if not os.path.isdir(target):
        res = subprocess.run(["git", "clone", url, target], capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=600)
        if res.returncode:
            sys.exit(f"clone failed: {res.stderr.strip()[:300]}")
    if not os.path.exists(page):
        write(page, DEV_PAGE.format(path="code/life-atlas", url=url, date=dt.date.today().isoformat()))
    set_state(brain, dev={"repo": url, "path": "code/life-atlas"})
    subprocess.run([sys.executable, "-I", "-B", os.path.join(brain, "_atlas", "tools", "atlas.py"), "--root", root,
                    "routes", "--execute"], capture_output=True, text=True)
    _git(brain, "add", "-A")
    _git(brain, "commit", "-q", "-m", "atlas: Atlas Dev set up")
    say("Done. Say 'continue in Atlas Dev' (or 'Atlas weiterentwickeln') and Atlas routes into the repository.")


# ================================================================= mark / adopt / code-sync

def cmd_mark(args):
    """Record maintenance or an update check as an empty commit: all devices see it through Git (D29)."""
    _root, brain = paths(args)
    _git(brain, "add", "-A")
    res = _git(brain, "commit", "-q", "--allow-empty", "-m", f"atlas: {args.kind}")
    if args.kind == "update-check":
        set_state(brain, last_update_check=now_iso())
    say(f"marked: atlas: {args.kind}" if res.returncode == 0 else f"commit failed: {res.stderr.strip()}")


def _slug(name):
    return re.sub(r"[^a-z0-9]+", "-", os.path.splitext(name)[0].lower()).strip("-") or "item"


def cmd_adopt(args):
    """Move rules, skills and text that Antigravity's /learn (or anyone) wrote outside Atlas into the brain (D28)."""
    root, brain = paths(args)
    hk = hook_module(brain)
    if args.keep:
        st = get_state(brain)
        st.setdefault("adopt_ignored", []).append(os.path.normcase(os.path.abspath(args.keep)))
        set_state(brain, adopt_ignored=st["adopt_ignored"])
        say(f"kept as is (not reported again): {args.keep}")
        return
    items = hk.foreign_items(root, os.path.expanduser("~") if args.include_global else None)
    if not items:
        say("adopt: nothing outside Atlas")
        return
    plan = []
    for it in items:
        name = _slug(os.path.basename(it["path"]))
        if it["kind"] == "skill":
            plan.append((it, os.path.join(brain, "skills", name), False, "skill -> brain/skills/ (wired into both agents)"))
            continue
        text = read(it["path"])
        if it["kind"] == "rule":
            trig = hk.frontmatter(text).get("trigger", "always_on")
            always = trig == "always_on"
            note = {"always_on": "router line + howto page (stays active in every chat)",
                    "glob": "howto page; if it concerns one repository, move it to that project's 'Never miss'",
                    }.get(trig, "howto page (found by search when needed)")
        else:
            always, note = True, "text outside the Atlas block -> router line + howto page"
        prefix = "learned-gemini-" if it["global"] else "learned-"
        plan.append((it, os.path.join(brain, "howto", f"{prefix}{name}.md"), always, note))
    say(f"adopt: {len(plan)} item(s)")
    for it, dst, _always, note in plan:
        say(f"  {it['kind']}: {fwd(it['path'])} -> {fwd(os.path.relpath(dst, root))} ({note})")
    if not args.execute:
        say("Dry run. --execute to adopt (originals are backed up), --keep <path> to leave one item where it is.")
        return
    rid = run_id(brain, "adopt")
    router_lines = []
    for it, dst, always, _note in plan:
        backup(brain, it["path"], rid)
        if it["kind"] == "skill":
            while os.path.exists(dst):
                dst += "-learned"
            shutil.move(it["path"], dst)
            continue
        text = read(it["path"])
        if it["kind"] == "rule":
            end = text.find("\n---", 3) if text.startswith("---") else -1
            body = text[end + 4:].strip() if end > 0 else text.strip()
            os.remove(it["path"])
        else:
            a, b = CORE_MARK
            if a in text and b in text:
                body = (text[: text.index(a)] + text[text.index(b) + len(b):]).strip()
                write(it["path"], text[text.index(a): text.index(b) + len(b)] + "\n")
            else:
                body = text.strip()
                os.remove(it["path"])
        first = re.sub(r"^[#>*\-\s]+", "", next((l for l in body.splitlines() if l.strip()), "learned rule"))[:110]
        page = (f"---\nsummary: {first}\ntags: [learned]\nsource: {fwd(it['path'])} (adopted {dt.date.today()})\n---\n"
                f"# {first}\n\n{body}\n")
        write(dst, page)
        if always:
            router_lines.append(f"- {first} (details: `brain/{fwd(os.path.relpath(dst, brain))}`)")
    if router_lines:
        rp = os.path.join(brain, "ROUTER.md")
        write(rp, read(rp).rstrip("\n") + "\n" + "\n".join(router_lines) + "\n")
    _git(brain, "add", "-A")
    _git(brain, "commit", "-q", "-m", f"atlas: adopt {len(plan)} item(s) from outside the brain")
    say(f"Adopted. Backups: _atlas/migrations/{rid}/wiring-backup/. Next: `atlas.py wire --agent <each> --execute` "
        "so adopted skills reach both agents; add `learned` to tags.md if it is missing.")


def cmd_code_sync(args):
    """Second device: clone the repositories that project pages list (`path:` + `remote:`) but that are missing."""
    _root, brain = paths(args)
    hk = hook_module(brain)
    todo = []
    for p in registry(brain):
        fm = hk.frontmatter(read(os.path.join(brain, p["page"])))
        remote = fm.get("remote") if isinstance(fm.get("remote"), str) and "<" not in fm.get("remote") else ""
        for full in p["resolved"]:
            if not os.path.exists(full):
                todo.append((p["slug"], full, remote))
    if not todo:
        say("code-sync: every project folder exists")
        return
    for slug, full, remote in todo:
        say(f"  {slug}: {fwd(full)} " + (f"<- {remote}" if remote else "(no `remote:` on the page, cannot clone)"))
    if not args.execute:
        say("Dry run. Add --execute to clone.")
        return
    for slug, full, remote in todo:
        if remote:
            os.makedirs(os.path.dirname(full), exist_ok=True)
            res = subprocess.run(["git", "clone", remote, full], capture_output=True, text=True, encoding="utf-8",
                                 errors="replace", timeout=600)
            say(f"  {slug}: {'cloned' if res.returncode == 0 else 'FAILED ' + res.stderr.strip()[:200]}")


# ================================================================= state

def cmd_state(args):
    _root, brain = paths(args)
    if args.set:
        kv = {}
        for item in args.set:
            k, _, v = item.partition("=")
            kv[k] = now_iso() if v == "now" else v
        set_state(brain, **kv)
    say(json.dumps(get_state(brain), indent=2, ensure_ascii=False))


BUILTIN_COMMANDS = {
    "init", "wire", "unwire", "routes", "projects", "index", "find", "where", "graph", "context",
    "link", "conflicts", "mark", "adopt", "code-sync", "probe", "check", "deadlines", "calendar",
    "todo", "coach", "inventory", "move", "verify", "undo", "core-diff", "core-apply", "core-accept",
    "dev-check", "dev-setup", "state", "mail", "ext", "run",
}


def _discover_user_commands(brain):
    """Discover update-safe user CLI commands in `brain/howto/scripts/` and `brain/skills/*/scripts/`.

    Returns `{cmd_name: {"path": abs_path, "rel": brain_rel, "runner": "python"|"sh"|"pwsh"}}`.
    Built-in core commands can never be shadowed.
    """
    cmds = {}
    search_dirs = []
    howto_scripts = os.path.join(brain, "howto", "scripts")
    if os.path.isdir(howto_scripts):
        search_dirs.append(howto_scripts)
    skills_root = os.path.join(brain, "skills")
    if os.path.isdir(skills_root):
        for sk in sorted(os.listdir(skills_root)):
            sk_scripts = os.path.join(skills_root, sk, "scripts")
            if os.path.isdir(sk_scripts):
                search_dirs.append(sk_scripts)

    for sdir in search_dirs:
        for fn in sorted(os.listdir(sdir)):
            if fn.startswith((".", "_")):
                continue
            full = os.path.join(sdir, fn)
            if not os.path.isfile(full):
                continue
            stem, ext = os.path.splitext(fn)
            ext_low = ext.lower()
            if ext_low not in (".py", ".sh", ".ps1"):
                continue
            runner = "python" if ext_low == ".py" else ("sh" if ext_low == ".sh" else "pwsh")
            slug = re.sub(r"[^a-z0-9-]+", "-", stem.lower().replace("_", "-")).strip("-")
            if not slug or slug in BUILTIN_COMMANDS:
                continue
            rel = fwd(os.path.relpath(full, brain))
            cmds.setdefault(slug, {"path": full, "rel": rel, "runner": runner})
            if stem.lower() != slug and stem.lower() not in BUILTIN_COMMANDS:
                cmds.setdefault(stem.lower(), {"path": full, "rel": rel, "runner": runner})
    return cmds


def _exec_user_command(root, brain, cmd_info, extra_argv):
    env = dict(
        os.environ,
        ATLAS_ROOT=root,
        ATLAS_BRAIN=brain,
        ATLAS_SECRETS=os.path.join(brain, "_secrets"),
    )
    runner = cmd_info["runner"]
    script = cmd_info["path"]
    if runner == "python":
        inv = [sys.executable, "-I", "-B", script] + list(extra_argv)
    elif runner == "sh":
        inv = ["bash", script] + list(extra_argv)
    else:
        pwsh = "powershell" if os.name == "nt" else "pwsh"
        inv = [pwsh, "-NoProfile", "-File", script] + list(extra_argv)
    res = subprocess.run(inv, env=env)
    sys.exit(res.returncode)


def cmd_ext(args):
    """Inspect update-safe user individualizations in the grown layer (`skills/`, `howto/scripts/`, `_secrets/`)."""
    root, brain = paths(args)
    hk = hook_module(brain)
    core_skills_dir = os.path.join(brain, "_atlas", "skills")
    core_names = set(os.listdir(core_skills_dir)) if os.path.isdir(core_skills_dir) else set()
    user_skills_dir = os.path.join(brain, "skills")
    custom_skills, overlays = [], []
    if os.path.isdir(user_skills_dir):
        for name in sorted(os.listdir(user_skills_dir)):
            sk_md = os.path.join(user_skills_dir, name, "SKILL.md")
            if not os.path.isfile(sk_md):
                continue
            fm = hk.frontmatter(read(sk_md))
            trigs = list(hk._as_list(fm.get("triggers"))) + list(hk._as_list(fm.get("aliases")))
            info = {"name": name, "rel": f"skills/{name}/SKILL.md", "triggers": trigs}
            if name in core_names:
                overlays.append(info)
            else:
                custom_skills.append(info)

    raw_cmds = _discover_user_commands(brain)
    seen_paths = {}
    for cname, cinfo in sorted(raw_cmds.items()):
        seen_paths.setdefault(cinfo["rel"], []).append(cname)

    sec_dir = os.path.join(brain, "_secrets")
    secrets_files = sorted(
        f for f in os.listdir(sec_dir) if not f.startswith(".") and os.path.isfile(os.path.join(sec_dir, f))
    ) if os.path.isdir(sec_dir) else []

    if getattr(args, "json", False):
        say(json.dumps({
            "root": fwd(root),
            "custom_commands": [{"aliases": aliases, "rel": rel} for rel, aliases in sorted(seen_paths.items())],
            "custom_skills": custom_skills,
            "core_skill_overlays": overlays,
            "secret_files": [f"_secrets/{f}" for f in secrets_files],
        }, indent=2, ensure_ascii=False))
        return

    say("=== UPDATE-SAFE USER INDIVIDUALIZATION (`brain/` grown layer — never overwritten by `/atlas-update`) ===")
    say(f"Custom CLI commands ({len(seen_paths)} — invoke via `atlas.py <cmd> ...` or `atlas.py run <cmd> ...`):")
    if seen_paths:
        for rel, aliases in sorted(seen_paths.items()):
            say(f"  - {', '.join(aliases)}  ->  brain/{rel}")
    else:
        say("  (none yet — drop `brain/howto/scripts/<name>.py` or `brain/skills/<name>/scripts/<name>.py`)")
    say(f"Custom user skills ({len(custom_skills)} in `brain/skills/` — wired to Claude & Antigravity):")
    for sk in custom_skills:
        tr = f" [triggers: {', '.join(sk['triggers'])}]" if sk["triggers"] else ""
        say(f"  - {sk['name']}  (brain/{sk['rel']}){tr}")
    if not custom_skills:
        say("  (none yet)")
    say(f"Core skill overlays ({len(overlays)} — user account/rule extensions appended on `wire`):")
    for ov in overlays:
        say(f"  - {ov['name']}  (brain/{ov['rel']})")
    if not overlays:
        say("  (none)")
    say(f"Secret config files ({len(secrets_files)} in `brain/_secrets/`, git-ignored & never read by hooks):")
    for sf in secrets_files:
        say(f"  - brain/_secrets/{sf}")
    if not secrets_files:
        say("  (none)")


def cmd_run(args, extra_argv=None):
    """Execute a custom user command from `brain/howto/scripts/` or `brain/skills/*/scripts/`."""
    root, brain = paths(args)
    cmds = _discover_user_commands(brain)
    target = (getattr(args, "target_cmd", None) or "").strip().lower()
    rest = list(extra_argv if extra_argv is not None else (getattr(args, "cmd_args", None) or []))
    if not target or target not in cmds:
        avail = ", ".join(sorted(cmds)) or "(none)"
        sys.exit(f"Unknown user command '{target}'. Available custom commands: {avail}")
    _exec_user_command(root, brain, cmds[target], rest)


def cmd_mail(args, extra_argv=None):
    """Delegate `atlas.py mail ...` to `atlas_mail.py` in the same tools directory."""
    root, _brain = paths(args)
    mail_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "atlas_mail.py")
    if not os.path.isfile(mail_script):
        sys.exit(f"Missing mail engine: {mail_script}")
    mail_args = list(extra_argv or [])
    if "--root" not in mail_args and "-h" not in mail_args and "--help" not in mail_args:
        mail_args = ["--root", root] + mail_args
    res = subprocess.run([sys.executable, "-I", "-B", mail_script] + mail_args)
    sys.exit(res.returncode)


# ================================================================= main

def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # agents read this output; avoid code-page mojibake
        except (AttributeError, ValueError):
            pass
    # Intercept `atlas.py [--root ROOT] mail ...`, `atlas.py [--root ROOT] run <cmd> ...`,
    # and direct user custom commands `atlas.py [--root ROOT] <custom_cmd> ...` before top-level flag parsing
    argv = sys.argv[1:]
    root_val = None
    pos_idx = None
    i_arg = 0
    while i_arg < len(argv):
        tok = argv[i_arg]
        if tok == "--root" and i_arg + 1 < len(argv):
            root_val = argv[i_arg + 1]
            i_arg += 2
            continue
        if tok.startswith("--root="):
            root_val = tok.split("=", 1)[1]
            i_arg += 1
            continue
        if tok.startswith("-"):
            break
        pos_idx = i_arg
        break
    if pos_idx is not None:
        first_cmd = argv[pos_idx]
        ns = argparse.Namespace(root=root_val)
        if first_cmd == "mail":
            cmd_mail(ns, argv[pos_idx + 1:])
        if first_cmd == "run" and pos_idx + 1 < len(argv) and not argv[pos_idx + 1].startswith("-"):
            ns.target_cmd = argv[pos_idx + 1]
            cmd_run(ns, argv[pos_idx + 2:])
        if first_cmd not in BUILTIN_COMMANDS:
            try:
                r_probe, b_probe = paths(ns)
                user_cmds = _discover_user_commands(b_probe)
                if first_cmd.lower() in user_cmds:
                    _exec_user_command(r_probe, b_probe, user_cmds[first_cmd.lower()], argv[pos_idx + 1:])
            except SystemExit:
                raise
            except Exception:
                pass
    ap = argparse.ArgumentParser(prog="atlas.py", description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--root", help="the Atlas folder (default: the one this tool is installed in, or $ATLAS_ROOT)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init")
    p.add_argument("--source", required=True)
    p.add_argument("--name")
    p.add_argument("--language")
    p.add_argument("--modules", default="")
    p.add_argument("--execute", action="store_true")

    for name in ("wire", "unwire"):
        p = sub.add_parser(name)
        p.add_argument("--agent", choices=("claude", "antigravity"), required=True)
        p.add_argument("--scope", choices=("project", "user"), default="project")
        p.add_argument("--python", help="interpreter for the hooks (default: the one running this tool)")
        p.add_argument("--user-home", help="for tests: use this folder instead of ~")
        p.add_argument("--router-mode", choices=("include", "copy"), default="include")
        p.add_argument("--grant", action="store_true",
                       help="claude: let Atlas work in project folders outside ROOT without permission prompts")
        p.add_argument("--execute", action="store_true")

    p = sub.add_parser("routes")
    p.add_argument("--execute", action="store_true")
    sub.add_parser("projects")
    sub.add_parser("index")
    p = sub.add_parser("find")
    p.add_argument("terms", nargs="+")
    p.add_argument("--limit", type=int, default=12)
    p.add_argument("--no-text", action="store_true", help="frontmatter only, skip the full-text pass")

    p = sub.add_parser("where", help="canonical placement & grouping guide before filing knowledge")
    p.add_argument("terms", nargs="*", help="topic words to check for existing pages & matching folder")

    p = sub.add_parser("graph", help="inspect a page's outlinks, backlinks, and unlinked mentions")
    p.add_argument("page", help="page path, stem, title, or alias")
    p.add_argument("--json", action="store_true", help="output JSON")

    p = sub.add_parser("context", help="assemble a token-budgeted Markdown context dossier")
    p.add_argument("target", help="page path, Target#Section, or search query")
    p.add_argument("--hops", type=int, default=1, help="neighbor hops to expand (default: 1)")
    p.add_argument("--budget", type=int, default=24000, help="character budget for dossier (default: 24000)")

    p = sub.add_parser("link", help="retroactive wiki-linking for existing brains")
    p.add_argument("--scan", action="store_true", help="scan unlinked entity mentions and journal paths (default)")
    p.add_argument("--journals", action="store_true", help="link valid literal brain/ file paths in journal/**/*.md")
    p.add_argument("--related", action="store_true", help="add unambiguous entity mentions to YAML frontmatter related:")
    p.add_argument("--inline", action="store_true", help="link first unambiguous mention per target in body prose")
    p.add_argument("--json", action="store_true", help="output JSON")
    p.add_argument("--execute", action="store_true")

    p = sub.add_parser("conflicts", help="manage unresolved fact contradictions in _atlas/conflicts.json")
    p.add_argument("action", nargs="?", default="list", choices=("list", "add", "resolve"))
    p.add_argument("target_id", nargs="?", help="conflict ID for `resolve`")
    p.add_argument("--id", help="conflict ID for `resolve`")
    p.add_argument("--pages", nargs="+", help="affected brain page paths for `add`")
    p.add_argument("--summary", help="contradiction summary for `add`")
    p.add_argument("--resolution", help="resolution note for `resolve`")
    p.add_argument("--all", action="store_true", help="include resolved conflicts in `list`")
    p.add_argument("--json", action="store_true", help="output JSON")

    p = sub.add_parser("mark")
    p.add_argument("kind", choices=("maintenance", "update-check"))
    p = sub.add_parser("adopt")
    p.add_argument("--execute", action="store_true")
    p.add_argument("--global", dest="include_global", action="store_true", help="also ~/.gemini (affects all workspaces)")
    p.add_argument("--keep", help="leave this item where it is and stop reporting it")
    p = sub.add_parser("code-sync")
    p.add_argument("--execute", action="store_true")

    p = sub.add_parser("probe")
    p.add_argument("--agent", choices=("claude", "antigravity"), help="default: all wired agents")
    p.add_argument("--scope", choices=("project", "user"), default="project")
    p.add_argument("--user-home")

    p = sub.add_parser("check")
    p.add_argument("--fix", action="store_true",
                   help="mechanical repairs: [[wikilinks]], moved-page links, tag synonyms, SQLite cache, routes")

    p = sub.add_parser("deadlines")
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--all", action="store_true")
    p.add_argument("--ics")

    p = sub.add_parser("calendar")
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--all", action="store_true")
    p.add_argument("--include-done", action="store_true")
    p.add_argument("--for", dest="for_role", choices=("coach",), help="filtered view for a specific role (e.g. coach)")
    p.add_argument("--view", choices=("all", "coach", "todo", "graph"),
                   help="initial dashboard tab (`all`, `coach`, `todo`, or `graph`)")
    p.add_argument("--init", action="store_true", help="harvest existing dates, birthdays and open to-dos into calendar/README.md")
    p.add_argument("--open", action="store_true", help="open the zero-token visual HTML calendar in the browser")
    p.add_argument("--ics", help="export calendar timeline to an .ics file")
    p.add_argument("--sync-ics", action="store_true", help="merge upcoming events from settings.md calendar_ics or --ics-source")
    p.add_argument("--ics-source", help="URL or file path of an .ics feed")
    p.add_argument("--execute", action="store_true")

    p = sub.add_parser("todo", help="quick task triage: list, done, skip, reopen, move, add")
    p.add_argument("action", nargs="?", default="list",
                   choices=("list", "done", "skip", "reopen", "move", "add"),
                   help="task action (default: list)")
    p.add_argument("target", nargs="*", help="task `#number` from briefing/list, task substring, or text for `add`")
    p.add_argument("--to", "--date", dest="to", help="target date (`YYYY-MM-DD`, `+3d`, `+1w`, `today`, `tomorrow`) for `move` or `add`")
    p.add_argument("--time", help="optional time (`HH:MM` or `HH:MM-HH:MM`) for dated `add`")
    p.add_argument("--kind", default="todo", help="timeline kind (`todo`, `event`, `deadline`, `blocked`, `goal`) for dated `add` (default: todo)")
    p.add_argument("--prio", choices=("high", "med", "low"), default="med", help="priority for undated `add` (default: med)")
    p.add_argument("--file", help="brain-relative `.md` path (optional when matching by `#number` or text)")
    p.add_argument("--line", type=int, help="1-based line number (`0` for frontmatter `due:`)")
    p.add_argument("--text", help="optional task text alias for `add`")
    p.add_argument("--days", type=int, default=365, help="upcoming horizon in days when `--all` is passed to `list`")
    p.add_argument("--all", action="store_true", help="include upcoming scheduled tasks in `list`")
    p.add_argument("--open", action="store_true", help="open visual calendar in To-Do view")
    p.add_argument("--execute", action="store_true", help="accepted for CLI consistency")

    p = sub.add_parser("coach")
    p.add_argument(
        "action",
        nargs="?",
        default="context",
        choices=(
            "context", "status", "auth-strava", "auth-hammerhead", "auth-garmin", "auth-intervals",
            "sync", "note", "zwo", "fit", "push", "history", "inspect", "compare", "field-test",
        ),
        help="coach action (default: context)",
    )
    p.add_argument("--init", action="store_true", help="scaffold brain/coach/ and harvest existing athlete thresholds")
    p.add_argument("--mode", choices=("auto", "daily", "checkin", "plan-14d", "macro", "dna"), default="auto",
                   help="context slicing mode (default: auto)")
    p.add_argument("--depth", choices=("auto", "simple", "standard", "pro"), default="auto",
                   help="coaching depth override (default: auto from coach/profile.md & streams)")
    p.add_argument("--query", help="optional natural-language user prompt for auto depth escalation")
    p.add_argument("--deep-dive", help="date (YYYY-MM-DD) for full activity stream/lap telemetry")
    p.add_argument("--date", help="target date (YYYY-MM-DD) for `note`, `zwo`, `inspect`, or `field-test`")
    p.add_argument("--text", help="athlete feedback note text for `note`")
    p.add_argument("--rpe", type=int, help="optional RPE (1-10) for `note`")
    p.add_argument("--rhr", type=int, help="morning resting heart rate (bpm) for `note` readiness check-in")
    p.add_argument("--hrv", type=float, help="morning HRV RMSSD (ms) for `note` readiness check-in")
    p.add_argument("--weight", type=float, help="morning body mass (kg) for `note` readiness check-in")
    p.add_argument("--sleep", type=float, help="sleep duration (hours, e.g. 7.5) for `note` readiness check-in")
    p.add_argument("--sleep-quality", help="sleep quality (e.g. good, fair, poor, or 1-5) for `note` readiness check-in")
    p.add_argument("--soreness", type=int, help="muscle soreness (1-5) for `note` readiness check-in")
    p.add_argument("--leg-pain", type=int, help="peripheral local leg soreness/pain (1-5) for `note` GELB-B detection")
    p.add_argument("--fatigue-7d", type=int, help="weekly 7-day global fatigue rating (1-5) for `note`")
    p.add_argument("--days", type=int, default=14, help="horizon in days for `sync` (default 90), `zwo`, or `push` (default 14)")
    p.add_argument("--months", type=int, default=6, help="lookback months for `history` (default 6)")
    p.add_argument("--from-date", help="start date YYYY-MM-DD for `history`")
    p.add_argument("--to-date", help="end date YYYY-MM-DD for `history`")
    p.add_argument("--sport", help="sport filter (`cycling`, `running`, `strength`) for `history`")
    p.add_argument("--epoch-a", help="first date window `YYYY-MM-DD..YYYY-MM-DD` for `compare`")
    p.add_argument("--epoch-b", help="second date window `YYYY-MM-DD..YYYY-MM-DD` for `compare`")
    p.add_argument("--unit", default="1", help="field-test protocol (`1`, `2`, `preload`, `lsct`) for `field-test`")
    p.add_argument("--all-14d", action="store_true", help="compile all cycling workouts in the next 14 days")
    p.add_argument("--mock-file", help="offline Strava JSON fixture for `sync`")
    p.add_argument("--wellness-mock", help="offline Garmin/Intervals wellness JSON fixture for `sync`")
    p.add_argument("--mock-dir", help="offline device sync directory for `push`")
    p.add_argument("--client-id", help="OAuth Client ID for `auth-strava` or `auth-hammerhead`")
    p.add_argument("--client-secret", help="OAuth Client Secret for `auth-strava` or `auth-hammerhead`")
    p.add_argument("--access-token", help="OAuth Access Token for `auth-strava` or `auth-hammerhead`")
    p.add_argument("--refresh-token", help="OAuth Refresh Token for `auth-strava` or `auth-hammerhead`")
    p.add_argument("--code", help="OAuth authorization code for headless `auth-strava` or `auth-hammerhead`")
    p.add_argument("--email", help="Garmin Connect email for `auth-garmin`")
    p.add_argument("--password", help="Garmin Connect password (discarded immediately after token exchange)")
    p.add_argument("--mfa-code", help="Garmin 6-digit MFA code for `auth-garmin`")
    p.add_argument("--tokens-json", help="Garmin serialized session tokens JSON for `auth-garmin`")
    p.add_argument("--athlete-id", help="Intervals.icu athlete ID for `auth-intervals`")
    p.add_argument("--api-key", help="Intervals.icu API key for `auth-intervals`")
    p.add_argument("--port", type=int, default=8766, help="loopback OAuth callback port (default: 8766)")
    p.add_argument("--open", action="store_true", help="open visual calendar in Coach View")
    p.add_argument("--no-sync", action="store_true", help="skip automatic Strava background sync during `context`")
    p.add_argument("--execute", action="store_true")

    p = sub.add_parser("inventory")
    p.add_argument("--out", required=True)
    p.add_argument("--scan", help="folder to map (default: the home directory)")
    p.add_argument("--budget", type=int, default=600, help="seconds for the file walk")

    p = sub.add_parser("move")
    p.add_argument("--plan", required=True)
    p.add_argument("--section")
    p.add_argument("--execute", action="store_true")
    p = sub.add_parser("verify")
    p.add_argument("--journal", required=True)
    p = sub.add_parser("undo")
    p.add_argument("--journal", required=True)
    p.add_argument("--execute", action="store_true")

    for name in ("core-diff", "core-apply"):
        p = sub.add_parser(name)
        p.add_argument("--source", help="life-atlas folder or URL (default: state upstream)")
        p.add_argument("--hard", action="store_true", help="hard-overwrite all _atlas/ core files (user brain/ files stay untouched)")
        p.add_argument("--execute", action="store_true")
        if name == "core-apply":
            p.add_argument("--rollback", help="restore the core backed up by this run id")
            p.add_argument("--user-home", help="for tests: use this folder instead of ~ when re-wiring")
            p.add_argument("--bootstrapped", action="store_true", help=argparse.SUPPRESS)
    p = sub.add_parser("core-accept")
    p.add_argument("--source", help="life-atlas folder or URL (default: state upstream)")
    p.add_argument("files", nargs="+", help="core files (or ROUTER.md) whose conflict you merged by hand")
    p = sub.add_parser("dev-check")
    p.add_argument("--repo", help="life-atlas repository URL (default: state upstream)")
    p = sub.add_parser("dev-setup")
    p.add_argument("--repo")
    p.add_argument("--execute", action="store_true")

    p = sub.add_parser("state")
    p.add_argument("--set", nargs="*", help="key=value (value 'now' = current time)")

    p = sub.add_parser("ext", help="list update-safe user extensions (`skills/`, `howto/scripts/`, `_secrets/`)")
    p.add_argument("--json", action="store_true", help="output JSON")

    p = sub.add_parser("run", help="run a custom user command from `brain/howto/scripts/` or `brain/skills/*/scripts/`")
    p.add_argument("target_cmd", help="custom command name (see `atlas.py ext`)")
    p.add_argument("cmd_args", nargs=argparse.REMAINDER, help="arguments passed to the user script")

    sub.add_parser("mail", help="multi-account IMAP client & safety-gated organizer (delegates to atlas_mail.py)")

    args = ap.parse_args()
    handlers = {"init": cmd_init, "routes": cmd_routes, "projects": cmd_projects, "index": cmd_index, "probe": cmd_probe,
                "find": cmd_find, "where": cmd_where, "graph": cmd_graph, "context": cmd_context, "link": cmd_link,
                "conflicts": cmd_conflicts,
                "mark": cmd_mark, "adopt": cmd_adopt, "code-sync": cmd_code_sync,
                "core-accept": cmd_core_accept, "dev-check": cmd_dev_check, "dev-setup": cmd_dev_setup,
                "check": cmd_check, "deadlines": cmd_deadlines, "calendar": cmd_calendar, "todo": cmd_todo,
                "coach": cmd_coach, "ext": cmd_ext, "run": cmd_run,
                "inventory": cmd_inventory, "move": cmd_move, "verify": cmd_verify, "undo": cmd_undo,
                "core-diff": cmd_core_diff, "state": cmd_state}
    if args.cmd == "init" and not args.root:
        args.root = r"C:\Atlas" if os.name == "nt" else os.path.join("~", "Atlas")
    if args.cmd in ("wire", "unwire"):
        cmd_wire(args, remove=args.cmd == "unwire")
    elif args.cmd == "core-apply":
        if args.rollback:
            cmd_core_rollback(args)
        else:
            cmd_core_diff(args, apply=True)
    else:
        handlers[args.cmd](args)


if __name__ == "__main__":
    main()
