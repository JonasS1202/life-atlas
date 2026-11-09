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
  index       build the search cache in full (after a large import; hooks otherwise build it a little per call)
  find        ranked search: tags, aliases, title, summary, then full text; one line per page
  mark        record `maintenance` or `update-check` as a commit (seen by every device)
  adopt       move rules/skills/text written outside Atlas (e.g. Antigravity /learn) into the brain
  code-sync   clone project repositories missing on this device (`path:` + `remote:` on the project page)
  probe       run the registered hooks with sample input and check their output
  check       verify the charter: sizes, links, reachability, duplicates, secrets, Git, backup, hooks, registry
  deadlines   list `due:` items; `--ics FILE` writes a calendar file
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

DEFAULT_UPSTREAM = "https://github.com/JonasS1202/life-atlas.git"
TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
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
    mod.CACHE = os.path.join(brain, "_atlas", "cache", "index.json")
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
    for d in ("inbox", "journal", "people", "areas", "projects", "howto", "self", "calendar", "skills", "_secrets",
              "_archive", os.path.join("_atlas", "memory"), os.path.join("_atlas", "migrations")):
        os.makedirs(os.path.join(brain, d), exist_ok=True)
    if "coach" in modules:
        os.makedirs(os.path.join(brain, "coach", "workouts"), exist_ok=True)
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
    if scope == "project":
        return {"router": os.path.join(root, "GEMINI.md"), "hooks": os.path.join(root, ".agents", "hooks.json"),
                "skills": os.path.join(root, ".agents", "skills"), "rules": os.path.join(root, ".agents", "rules")}
    g = os.path.join(user_home, ".gemini")
    return {"router": os.path.join(g, "AGENTS.md"), "hooks": os.path.join(g, "config", "hooks.json"),
            "skills": os.path.join(g, "config", "skills"), "rules": None}


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
            if os.path.isdir(path):
                shutil.rmtree(path)
            elif os.path.exists(path):
                os.remove(path)
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
                               "timeout": 10}],
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
        if not os.path.exists(dst) or read(dst) != text:
            out.append((dst, text, f"skill {name}"))
        for extra in os.listdir(folder):  # supporting files of grown skills
            src = os.path.join(folder, extra)
            if extra != "SKILL.md" and os.path.isfile(src):
                d2 = os.path.join(target_dir, name, extra)
                if not os.path.exists(d2) or sha256_file(d2) != sha256_file(src):
                    out.append((d2, read(src), f"skill file {name}/{extra}"))
    return out


def _agent_changes(brain, target_dir, remove):
    """Claude Code subagents shipped with the core (atlas-search, atlas-maintainer). Antigravity has none."""
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
    """Ranked search over the cached frontmatter first (tags, aliases, title, summary), then the full text."""
    _root, brain = paths(args)
    hk = hook_module(brain)
    idx = hk.load_index()
    vocab = hk.tag_vocab()
    words = [w.lower() for w in args.terms]
    canon = {}
    for tag, syns in vocab.items():
        for w in [tag] + syns:
            canon[w] = tag
    scores, why = {}, {}
    for rel, e in idx.items():
        if rel.startswith("_") or rel in ("tags.md", "settings.md"):
            continue
        hay_title, hay_sum = e.get("title", "").lower(), e.get("summary", "").lower()
        aliases = [a.lower() for a in e.get("aliases", [])]
        page_tags = {canon.get(t, t) for t in e.get("tags", [])}
        for w in words:
            pts = 0
            if canon.get(w, w) in page_tags:
                pts += 5
            if any(w in a for a in aliases):
                pts += 4
            if w in hay_title:
                pts += 3
            if w in hay_sum:
                pts += 2
            if w in rel.lower():
                pts += 1
            if pts:
                scores[rel] = scores.get(rel, 0) + pts
                why.setdefault(rel, set()).add(w)
    if not args.no_text:
        for w in words:
            try:
                for rel, c in hk._grep_counts(w, False).items():
                    if rel in idx and rel not in ("tags.md", "settings.md"):
                        scores[rel] = scores.get(rel, 0) + min(c, 5) * 0.3
                        why.setdefault(rel, set()).add(w)
            except Exception:
                break
    # pages that match every word first
    ranked = sorted(scores, key=lambda r: (-len(why[r]), -scores[r], r))[: args.limit]
    for rel in ranked:
        e = idx.get(rel, {})
        tags = ",".join(e.get("tags", []))
        say(f"{rel} — {e.get('summary') or e.get('title') or ''}" + (f" [{tags}]" if tags else ""))
    if not ranked:
        say("(nothing found)")


# ================================================================= probe

SAMPLES = {
    ("claude", "session"): {"hook_event_name": "SessionStart", "source": "startup", "session_id": "probe"},
    ("claude", "prompt"): {"hook_event_name": "UserPromptSubmit", "prompt": "probe: glossary", "session_id": "probe"},
    ("claude", "stop"): {"hook_event_name": "Stop", "stop_hook_active": True, "last_assistant_message": "Atlas: probe"},
    ("antigravity", "session"): {"conversationId": "probe", "workspacePaths": []},
    ("antigravity", "prompt"): {"conversationId": "probe", "invocationNum": 0, "lastUserInput": "probe: glossary"},
    ("antigravity", "stop"): {"conversationId": "probe", "terminationReason": "error", "fullyIdle": True},
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
    by_base = {}
    for r in rels:
        by_base.setdefault(os.path.basename(r).lower(), []).append(r)
    graph, broken, fixes = {}, [], {}
    for rel, p in rels.items():
        text = read(p)
        targets = set()
        for m in LINK_RX.finditer(text):
            t = m.group(1).split("#")[0]
            if not t or re.match(r"^[a-z]+:", t) or t.startswith(("/", "~")) or "<" in t:
                continue
            tgt = fwd(os.path.normpath(os.path.join(os.path.dirname(rel), t)))
            full = os.path.join(brain, tgt)
            if not os.path.exists(full):
                if not rel.startswith("_atlas/"):
                    broken.append(f"{rel} -> {t}")
                    cand = by_base.get(os.path.basename(t).lower(), [])
                    if len(cand) == 1:  # moved page with a unique name: the fix is unambiguous
                        new_t = fwd(os.path.relpath(os.path.join(brain, cand[0]), os.path.join(brain, os.path.dirname(rel))))
                        fixes.setdefault(rel, []).append((m.group(1).split("#")[0], new_t))
            elif tgt.endswith(".md"):
                targets.add(tgt)
            elif os.path.isdir(full) and os.path.isfile(os.path.join(full, "README.md")):
                targets.add(fwd(os.path.join(tgt, "README.md")))
        for m in TICK_RX.finditer(text):
            if os.path.exists(os.path.join(brain, m.group(1))):
                targets.add(m.group(1))
        graph[rel] = targets
    if broken:
        warns.append(f"{len(broken)} broken link(s):\n    " + "\n    ".join(broken[:30]))
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
    reg = registry(brain)
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
    hk = hook_module(brain)
    idx = hk.load_index()
    vocab = hk.tag_vocab()
    canon = {s: t for t, syns in vocab.items() for s in syns}
    content = [r for r in idx if r.startswith(("areas/", "people/", "projects/", "howto/", "self/", "coach/"))
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
        _check_fix(args, root, brain, fixes, synonyms)
    sys.exit(1 if errors else 0)


def _check_fix(args, root, brain, link_fixes, synonyms):
    """Mechanical repairs only (0 tokens): links to moved pages with a unique name, tag synonyms -> tag, cache, routes."""
    done = []
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


def _open_visual_calendar(hk, idx, open_now=False):
    cal_url = hk.render_calendar_html(idx, start_server=True)
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
    if args.init:
        _calendar_init(root, brain, hk, args.execute)
        _open_visual_calendar(hk, hk.load_index(), getattr(args, "open", False))
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
        _open_visual_calendar(hk, hk.load_index(), getattr(args, "open", False))
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
        _open_visual_calendar(hk, idx, getattr(args, "open", False))
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
    _open_visual_calendar(hk, idx, getattr(args, "open", False))


# ================================================================= coach (Cycling, Running & Strength)

COACH_DURATIONS = (
    ("5s", 5), ("15s", 15), ("30s", 30), ("1m", 60), ("3m", 180),
    ("5m", 300), ("8m", 480), ("12m", 720), ("20m", 1200), ("30m", 1800), ("60m", 3600),
)

COACH_DEFAULT_README = """---
summary: Atlas Sport-Coach hub: athlete profile, thresholds, macro periodization, 3-epoch power curve, and activity log
tags: [atlas, sport]
aliases: [Sport Coach, Trainingssteuerung, Radsport Coach, Lauf Coach]
---
# Atlas · Sport-Coach Hub

Stand: {{DATE}}. Native training and physiology hub for {{USER_NAME}} (Cycling, Running & Strength).

## Architecture & Pages

| Page | Purpose |
|---|---|
| [profile.md](profile.md) | Athlete biometrics, cycling & running thresholds (FTP, zones, HRmax), weekly time budget, learned bio-rules |
| [macro-plan.md](macro-plan.md) | Season macro periodization (Base, Build, Peak, Taper, Deload) anchored to `[goal]` and `[blocked]` in [../calendar/README.md](../calendar/README.md) |
| [power-curve.md](power-curve.md) | 3-Epoch Mean Maximal Power (`42d` · `90d` · `365d`, Fresh vs. Fatigued `> 1500 kJ`) & Running Best Paces |
| [activities.md](activities.md) | 3-Tier Strava activity log, interval compliance, and subjective athlete feedback (`RPE` / feel) |
| `workouts/` | Deterministically generated `.zwo` workout files for Hammerhead Karoo / Zwift / Wahoo / Elite trainers |
"""

COACH_DEFAULT_PROFILE = """---
summary: Athlete physiology profile: biometrics, cycling FTP & power zones, running threshold & paces, weekly time budget, bio-rules
tags: [self, sport]
aliases: [Athletenprofil, FTP, Trainingszonen, Leistungsdaten, Schwellenwerte]
---
# Athlete Profile & Thresholds

Stand: {{DATE}}. Central physiological parameters and scheduling rules for {{USER_NAME}}.

## 1. Biometrics & Core Thresholds

| Metric | Value | Notes / Source |
|---|---|---|
| **Biometrie** | {biometrie} | Height & Current Weight |
| **FTP (Cycling)** | {ftp} | Functional Threshold Power |
| **Max HR / Rest HR** | {hr} | Heart rate anchors |
| **VO₂max (est.)** | {vo2max} | Aerobic ceiling |
| **Running Threshold** | — min/km | 10k / LT2 pace anchor |
| **Weekly Time Budget** | {volumen} | Target endurance + strength budget |
| **Athletik & Kraft** | {kraft} | Upper body / core / injury prevention |

## 2. Schedule & Time Windows

- **Weekday Window (Mo–Fr):** {schedule} (Indoor Smart-Trainer / Evening Run / Strength)
- **Weekend Window (Sa–So):** Flexible for long outdoor endurance rides (`2.5–4.5h`) or key race-pace sessions
- **Hard Rule:** Multi-day `[blocked]` entries and timed `[event]` entries in [../calendar/README.md](../calendar/README.md) are strict constraints.

## 3. Learned Coaching Rules & Bio-Feedback

- Document lasting physiological insights here (e.g., indoor cooling limits, nutrition per hour, recovery needs after `> 1500 kJ` rides, muscular vs. central fatigue patterns).
"""

COACH_DEFAULT_MACRO = """---
summary: Season macro periodization: weekly training phases (Base, Build, Peak, Taper, Deload) and target volume up to A/B goals
tags: [sport, planung]
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
summary: 3-Epoch Cycling Power Curve (42d Current, 90d Meso, 365d Season; Fresh vs. Fatigued >1500 kJ) and Running Best Paces
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
| **3m** | — | — | — | — |
| **5m** | — | — | — | — |
| **8m** | — | — | — | — |
| **12m** | — | — | — | — |
| **20m** | — | — | — | — |
| **30m** | — | — | — | — |
| **60m** | — | — | — | — |

## 2. Cycling Durability — Fatigued (`> 1500 kJ` Prior Work)

| Duration | 42d (`>1500 kJ`) | 90d (`>1500 kJ`) | 365d (`>1500 kJ`) | Retention vs. Fresh |
|---|---|---|---|---|
| **5s** | — | — | — | — |
| **1m** | — | — | — | — |
| **5m** | — | — | — | — |
| **20m** | — | — | — | — |

## 3. Running Best Split Paces

| Distance | 42d (Current) | 90d (Meso) | 365d (Season) |
|---|---|---|---|
| **1 km** | — | — | — |
| **3 km** | — | — | — |
| **5 km** | — | — | — |
| **10 km** | — | — | — |
"""

COACH_DEFAULT_ACTIVITIES = """---
summary: 3-Tier Strava activity log, physiological telemetry (NP, IF, HR, kJ, MMP peaks), and athlete ride feedback
tags: [sport]
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


def _coach_init(root, brain, hk, execute=True, quiet=False):
    """Scaffold brain/coach/ files, harvest existing athlete thresholds from brain/self/ or areas/, and link INDEX.md."""
    values = _router_values(brain, root)
    coach_dir = os.path.join(brain, "coach")
    workouts_dir = os.path.join(coach_dir, "workouts")
    idx = hk.load_index()
    idx_no_prof = {k: v for k, v in idx.items() if k != "coach/profile.md"}
    prof_path = os.path.join(brain, "coach", "profile.md")
    prof_is_skeleton = (not os.path.isfile(prof_path)) or any(
        s in read(prof_path) for s in ("186 cm", "75.0 kg", "180 cm", "— cm · — kg")
    )
    if prof_is_skeleton and os.path.isfile(prof_path):
        # Temporarily hide skeleton profile.md so _extract_coach_context harvests from self/ and areas/
        tmp_p = prof_path + ".tmp_init"
        os.replace(prof_path, tmp_p)
        try:
            ctx = hk._extract_coach_context(idx_no_prof)
        finally:
            os.replace(tmp_p, prof_path)
    else:
        ctx = hk._extract_coach_context(idx_no_prof if not os.path.isfile(prof_path) else idx)
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
        ("coach/macro-plan.md", macro_filled),
        ("coach/power-curve.md", render(COACH_DEFAULT_PCURVE, values)),
        ("coach/activities.md", render(COACH_DEFAULT_ACTIVITIES, values)),
    ]
    created = []
    if execute:
        os.makedirs(workouts_dir, exist_ok=True)
    for rel, content in files:
        dst = os.path.join(brain, rel)
        if not os.path.isfile(dst) or (not quiet and rel == "coach/profile.md" and prof_is_skeleton and m_map):
            created.append(rel)
            if execute:
                write(dst, content)
    macro_dst = os.path.join(brain, "coach", "macro-plan.md")
    if execute and not quiet and os.path.isfile(macro_dst):
        m_txt = read(macro_dst)
        if "goals_hash: none" in m_txt and ghash != "none":
            write(macro_dst, m_txt.replace("goals_hash: none", f"goals_hash: {ghash}", 1))
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
    """Return (ftp_watts, weight_kg) from brain/coach/profile.md or _extract_coach_context."""
    ftp_w = 275
    weight_kg = 67.0
    prof_path = os.path.join(brain, "coach", "profile.md")
    text = read(prof_path) if os.path.isfile(prof_path) else ""
    if text:
        for line in text.splitlines():
            low = line.lower()
            if "ftp" in low and "|" in line:
                m = re.search(r"(\d{2,3})\s*w\b", low)
                if m:
                    ftp_w = int(m.group(1))
            if ("biometrie" in low or "weight" in low or "körpermaße" in low) and "|" in line:
                m_kg = re.search(r"(\d{2,3}(?:[.,]\d)?)\s*kg\b", low)
                if m_kg:
                    weight_kg = float(m_kg.group(1).replace(",", "."))
    return ftp_w, weight_kg


def _mmp_from_watts(watts):
    """Compute Mean Maximal Power dict for COACH_DURATIONS in O(N) using prefix sums."""
    n = len(watts)
    if not n:
        return {}
    pref = [0.0] * (n + 1)
    for i, w in enumerate(watts):
        pref[i + 1] = pref[i] + max(0.0, float(w or 0.0))
    peaks = {}
    for label, sec in COACH_DURATIONS:
        if n < sec:
            continue
        best = 0.0
        for i in range(n - sec + 1):
            avg = (pref[i + sec] - pref[i]) / sec
            if avg > best:
                best = avg
        if best > 0:
            peaks[label] = int(round(best))
    return peaks


def _compute_stream_metrics(streams, ftp_watts=275, weight_kg=67.0, sport="cycling"):
    """Compute NP, work_kj, IF, TSS, Fresh MMP, Fatigued MMP (>1500 kJ), Pw:Hr decoupling, and running split paces."""
    watts = [float(x or 0.0) for x in (streams.get("watts") or [])]
    hr = [float(x or 0.0) for x in (streams.get("heartrate") or [])]
    vel = [float(x or 0.0) for x in (streams.get("velocity_smooth") or [])]
    out = {}
    if watts:
        n = len(watts)
        joules = sum(max(0.0, w) for w in watts)
        out["work_kj"] = int(round(joules / 1000.0))
        out["avg_w"] = int(round(joules / n))
        # 30-second rolling average for Normalized Power (NP)
        if n >= 30:
            w_sum = sum(watts[:30])
            fourth_sum = (w_sum / 30.0) ** 4
            cnt = 1
            for i in range(30, n):
                w_sum += watts[i] - watts[i - 30]
                fourth_sum += (max(0.0, w_sum) / 30.0) ** 4
                cnt += 1
            np_w = int(round((fourth_sum / cnt) ** 0.25))
        else:
            np_w = out["avg_w"]
        out["np_w"] = np_w
        if ftp_watts > 0:
            if_val = round(np_w / float(ftp_watts), 2)
            out["if"] = if_val
            out["tss"] = int(round((n * np_w * if_val) / (ftp_watts * 3600.0) * 100.0))
        out["peaks_fresh"] = _mmp_from_watts(watts)
        # Fatigued MMP after accumulating 1,500,000 Joules (1500 kJ)
        cum_j = 0.0
        idx_1500 = None
        for i, w in enumerate(watts):
            cum_j += max(0.0, w)
            if cum_j >= 1_500_000.0:
                idx_1500 = i + 1
                break
        if idx_1500 is not None and idx_1500 < n:
            out["peaks_fatigued"] = _mmp_from_watts(watts[idx_1500:])
        else:
            out["peaks_fatigued"] = {}
        # Aerobic Decoupling (Pw:Hr %) between 1st half and 2nd half (after warmup 10m)
        if n >= 1200 and len(hr) == n:
            start_i = 600 if n >= 2400 else 0
            mid_i = start_i + (n - start_i) // 2
            w1 = sum(watts[start_i:mid_i]) / max(1, mid_i - start_i)
            h1 = sum(hr[start_i:mid_i]) / max(1, mid_i - start_i)
            w2 = sum(watts[mid_i:n]) / max(1, n - mid_i)
            h2 = sum(hr[mid_i:n]) / max(1, n - mid_i)
            if h1 > 80 and h2 > 80 and w1 > 50:
                ef1 = w1 / h1
                ef2 = w2 / h2
                out["decoupling_pct"] = round(((ef1 - ef2) / ef1) * 100.0, 1)
    if hr:
        valid_hr = [x for x in hr if x > 30]
        if valid_hr:
            out["avg_hr"] = int(round(sum(valid_hr) / len(valid_hr)))
            out["max_hr"] = int(round(max(valid_hr)))
    if sport == "running" and vel:
        # Compute best split paces (1km, 3km, 5km, 10km) from 1Hz velocity_smooth (m/s)
        n_v = len(vel)
        pref_d = [0.0] * (n_v + 1)
        for i, v in enumerate(vel):
            pref_d[i + 1] = pref_d[i] + max(0.0, v)
        best_paces = {}
        for d_lbl, dist_m in (("1km", 1000.0), ("3km", 3000.0), ("5km", 5000.0), ("10km", 10000.0)):
            if pref_d[-1] < dist_m:
                continue
            best_sec = None
            j = 0
            for i in range(n_v):
                while j < n_v and (pref_d[j] - pref_d[i]) < dist_m:
                    j += 1
                if (pref_d[j] - pref_d[i]) >= dist_m:
                    sec = j - i
                    if best_sec is None or sec < best_sec:
                        best_sec = sec
            if best_sec and best_sec > 0:
                sec_per_km = best_sec / (dist_m / 1000.0)
                mm = int(sec_per_km // 60)
                ss = int(round(sec_per_km % 60))
                if ss == 60:
                    mm += 1
                    ss = 0
                best_paces[d_lbl] = {"sec_per_km": round(sec_per_km, 1), "fmt": f"{mm}:{ss:02d}/km"}
        if best_paces:
            out["best_paces"] = best_paces
    return out


def _rebuild_coach_markdown_files(brain, activities, ftp_watts=275, weight_kg=67.0):
    """Deterministically regenerate brain/coach/power-curve.md and brain/coach/activities.md from activities list."""
    today = dt.date.today()
    # Sort newest first
    acts = sorted(
        [a for a in activities if isinstance(a, dict) and a.get("date")],
        key=lambda x: (x.get("date", ""), x.get("id", 0)),
        reverse=True,
    )
    # 1. Compute 3-Epoch Power Curve (42d, 90d, 365d) Fresh & Fatigued (>1500 kJ) + Running Best Paces
    epochs = (("42d", 42), ("90d", 90), ("365d", 365))
    fresh_ep = {lbl: {} for lbl, _ in COACH_DURATIONS}
    fatig_ep = {lbl: {} for lbl, _ in COACH_DURATIONS}
    run_ep = {d: {} for d in ("1km", "3km", "5km", "10km")}

    for a in acts:
        try:
            adate = dt.date.fromisoformat(a["date"][:10])
        except ValueError:
            continue
        age = (today - adate).days
        if age < 0 or age > 365:
            continue
        pf = a.get("peaks_fresh") or {}
        p_fat = a.get("peaks_fatigued") or {}
        for dur_lbl, _sec in COACH_DURATIONS:
            val = pf.get(dur_lbl)
            if val:
                for ep_name, ep_days in epochs:
                    if age <= ep_days and val > fresh_ep[dur_lbl].get(ep_name, 0):
                        fresh_ep[dur_lbl][ep_name] = int(val)
            fval = p_fat.get(dur_lbl)
            if fval:
                for ep_name, ep_days in epochs:
                    if age <= ep_days and fval > fatig_ep[dur_lbl].get(ep_name, 0):
                        fatig_ep[dur_lbl][ep_name] = int(fval)
        bp = a.get("best_paces") or {}
        for d_lbl in ("1km", "3km", "5km", "10km"):
            pinfo = bp.get(d_lbl)
            if isinstance(pinfo, dict) and pinfo.get("sec_per_km"):
                spk = float(pinfo["sec_per_km"])
                for ep_name, ep_days in epochs:
                    cur_best = run_ep[d_lbl].get(ep_name)
                    if age <= ep_days and (cur_best is None or spk < cur_best[0]):
                        run_ep[d_lbl][ep_name] = (spk, pinfo.get("fmt", ""))

    pc_lines = [
        "---",
        "summary: 3-Epoch Cycling Power Curve (42d Current, 90d Meso, 365d Season; Fresh vs. Fatigued >1500 kJ) and Running Best Paces",
        "tags: [sport]",
        f"updated: {today.isoformat()}",
        "---",
        "# 3-Epoch Power & Pace Curve",
        "",
        f"Stand: {today.isoformat()}. Precomputed peak performance matrix across 3 time horizons (`42d` Current Form · `90d` Meso Block · `365d` Season/All-Time).",
        "",
        "## 1. Cycling Mean Maximal Power — Fresh (0 kJ+)",
        "",
        "| Duration | 42d (Current) | 90d (Meso) | 365d (Season) | W/kg (42d) |",
        "|---|---|---|---|---|",
    ]
    for dur_lbl, _sec in COACH_DURATIONS:
        r = fresh_ep[dur_lbl]
        v42 = f"{r['42d']} W" if r.get("42d") else "—"
        v90 = f"{r['90d']} W" if r.get("90d") else "—"
        v365 = f"{r['365d']} W" if r.get("365d") else "—"
        wkg = f"{r['42d'] / weight_kg:.2f} W/kg" if r.get("42d") and weight_kg > 0 else "—"
        pc_lines.append(f"| **{dur_lbl}** | {v42} | {v90} | {v365} | {wkg} |")

    pc_lines += [
        "",
        "## 2. Cycling Durability — Fatigued (`> 1500 kJ` Prior Work)",
        "",
        "| Duration | 42d (`>1500 kJ`) | 90d (`>1500 kJ`) | 365d (`>1500 kJ`) | Retention vs. Fresh |",
        "|---|---|---|---|---|",
    ]
    for dur_lbl in ("5s", "1m", "5m", "20m"):
        rf = fatig_ep[dur_lbl]
        r0 = fresh_ep[dur_lbl]
        v42 = f"{rf['42d']} W" if rf.get("42d") else "—"
        v90 = f"{rf['90d']} W" if rf.get("90d") else "—"
        v365 = f"{rf['365d']} W" if rf.get("365d") else "—"
        base_f = rf.get("42d") or rf.get("90d") or rf.get("365d")
        base_0 = r0.get("42d") or r0.get("90d") or r0.get("365d")
        ret = f"{(base_f / base_0) * 100.0:.1f} %" if base_f and base_0 else "—"
        pc_lines.append(f"| **{dur_lbl}** | {v42} | {v90} | {v365} | {ret} |")

    pc_lines += [
        "",
        "## 3. Running Best Split Paces",
        "",
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
            b["kj"] += int(a.get("work_kj") or 0)
            if len(b["names"]) < 3 and a.get("name"):
                b["names"].append(a["name"])

    act_lines = [
        "---",
        "summary: 3-Tier Strava activity log, physiological telemetry (NP, IF, HR, kJ, MMP peaks), and athlete ride feedback",
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
        dur_m = a.get("duration_min", 0)
        dist_km = a.get("distance_km", 0)
        elev_m = a.get("elevation_m", 0)
        np_s = f"NP {a['np_w']} W (Avg {a.get('avg_w', '—')} W, IF {a.get('if', '—')}, {a.get('work_kj', 0)} kJ)" if a.get("np_w") else ""
        hr_s = f"HR {a['avg_hr']}/{a.get('max_hr', '—')} bpm" if a.get("avg_hr") else ""
        dec_s = f" · Pw:Hr {a['decoupling_pct']}%" if a.get("decoupling_pct") is not None else ""
        pf = a.get("peaks_fresh") or {}
        p_str = ", ".join(f"{k}: {pf[k]}W" for k in ("5s", "1m", "5m", "20m") if pf.get(k))
        pfat = a.get("peaks_fatigued") or {}
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
        if p_str:
            act_lines.append(f"- **Peaks (Fresh):** {p_str}" + (f" | **Fatigued (`>1500 kJ`):** {pfat_str}" if pfat_str else ""))
        if a.get("best_paces"):
            bp_s = ", ".join(f"{k}: {v.get('fmt')}" for k, v in a["best_paces"].items() if isinstance(v, dict))
            if bp_s:
                act_lines.append(f"- **Split Paces:** {bp_s}")
        if a.get("laps"):
            act_lines.append("- **Laps:** " + " | ".join(str(lp) for lp in a["laps"][:8]))
        act_lines.append(f"- **Athlete Feedback:** {fb_disp}")
        act_lines.append("")

    act_lines += [
        "## Tier 2 · Meso History (Weeks 4–12 — Compact Log)",
        "",
        "| Date | Sport | Activity | Duration | Load / NP | HR (Avg/Max) | Feedback |",
        "|---|---|---|---|---|---|---|",
    ]
    for a in tier2:
        ld = f"NP {a['np_w']}W ({a.get('work_kj', 0)}kJ)" if a.get("np_w") else (f"{a.get('distance_km', 0):.1f} km" if a.get("distance_km") else "—")
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


def _strava_refresh_if_needed(brain, env_dict):
    """Refresh Strava OAuth access token if expired or expiring within 120 seconds."""
    import urllib.parse
    import urllib.request
    exp = int(env_dict.get("STRAVA_EXPIRES_AT") or 0)
    if env_dict.get("STRAVA_ACCESS_TOKEN") and exp > time.time() + 120:
        return env_dict["STRAVA_ACCESS_TOKEN"]
    cid = env_dict.get("STRAVA_CLIENT_ID")
    csec = env_dict.get("STRAVA_CLIENT_SECRET")
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


def _coach_sync(root, brain, hk, days=90, mock_file=None, execute=True, quiet=False):
    """Sync Strava activities + 1Hz streams (or load mock JSON), compute 3-epoch curves & match calendar workouts."""
    _coach_init(root, brain, hk, execute=execute, quiet=True)
    ftp_w, weight_kg = _extract_profile_numbers(brain, hk)
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
            if not quiet:
                say("strava sync: not connected yet (brain/_secrets/strava.env missing). Run `atlas.py coach auth-strava`.")
            return 0
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
        entry = dict(prev)
        entry.update({
            "id": aid,
            "date": start_date,
            "name": raw.get("name") or prev.get("name") or "Workout",
            "sport": sport,
            "duration_min": dur_min,
            "distance_km": dist_km,
            "elevation_m": elev_m,
        })
        if raw.get("average_watts") and not entry.get("avg_w"):
            entry["avg_w"] = int(round(float(raw["average_watts"])))
        if raw.get("weighted_average_watts") and not entry.get("np_w"):
            entry["np_w"] = int(round(float(raw["weighted_average_watts"])))
        if raw.get("average_heartrate") and not entry.get("avg_hr"):
            entry["avg_hr"] = int(round(float(raw["average_heartrate"])))
        if raw.get("max_heartrate") and not entry.get("max_hr"):
            entry["max_hr"] = int(round(float(raw["max_heartrate"])))
        if raw.get("kilojoules") and not entry.get("work_kj"):
            entry["work_kj"] = int(round(float(raw["kilojoules"])))
        if raw.get("laps") and isinstance(raw["laps"], list):
            entry["laps"] = raw["laps"]

        # Load or fetch 1Hz streams
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
                s_url = f"https://www.strava.com/api/v3/activities/{aid}/streams?keys=watts,heartrate,cadence,velocity_smooth,time&key_by_type=true"
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
            m_calc = _compute_stream_metrics(streams, ftp_watts=ftp_w, weight_kg=weight_kg, sport=sport)
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
        say(f"coach sync ({'applied' if execute else 'dry-run'}): {synced_count} activity/ies processed, "
            f"{matched} calendar workout(s) marked [x], {pend_cnt} ride check-in(s) pending.")
    return synced_count


def _coach_note(root, brain, hk, date_str, text, rpe=None):
    """Save subjective athlete ride feedback (RPE, legs, feel) to activities.json, activities.md, and calendar/README.md."""
    _coach_init(root, brain, hk, execute=True, quiet=True)
    ftp_w, weight_kg = _extract_profile_numbers(brain, hk)
    note_str = f"RPE {rpe}/10 — {text.strip()}" if rpe else text.strip()
    act_json_path = os.path.join(brain, "coach", "activities.json")
    acts = load_json(act_json_path, [])
    if not isinstance(acts, list):
        acts = []
    updated_act = False
    for a in acts:
        if isinstance(a, dict) and str(a.get("date", ""))[:10] == date_str:
            a["feedback"] = note_str
            updated_act = True
    if not updated_act:
        acts.insert(0, {
            "id": f"manual-{date_str}",
            "date": date_str,
            "name": "Logged Workout",
            "sport": "cycling",
            "duration_min": 60,
            "feedback": note_str,
        })
    dump_json(act_json_path, acts)
    _rebuild_coach_markdown_files(brain, acts, ftp_watts=ftp_w, weight_kg=weight_kg)

    cal_path = os.path.join(brain, "calendar", "README.md")
    if os.path.isfile(cal_path):
        cal_txt = read(cal_path)
        lines = cal_txt.splitlines()
        for i, line in enumerate(lines):
            m = re.match(r"^(\s*-\s*)\[([ xX\-])\](\s+)(\d{4}-\d{2}-\d{2})\b(.*)$", line)
            if m and m.group(4) == date_str and "[workout" in m.group(5).lower():
                rest = m.group(5)
                if re.search(r"\[feedback:[^\]]*\]", rest, re.I):
                    rest = re.sub(r"\[feedback:[^\]]*\]", f"[feedback: {note_str}]", rest, flags=re.I)
                else:
                    rest = rest.rstrip() + f" [feedback: {note_str}]"
                lines[i] = f"{m.group(1)}[x]{m.group(3)}{date_str}{rest}"
        write(cal_path, "\n".join(lines) + ("\n" if cal_txt.endswith("\n") else ""))
    hk.load_index()
    hk.render_calendar_html(hk.load_index(), start_server=False)
    say(f"coach note: saved feedback for {date_str} -> '{note_str}'")


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
    """Interactive loopback OAuth flow (or headless --code exchange) for Strava API v3."""
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

    cid = env_dict.get("STRAVA_CLIENT_ID")
    csec = env_dict.get("STRAVA_CLIENT_SECRET")
    if not cid or not csec:
        say("=== Strava Onboarding (Step 1 of 2) ===")
        say("1. Open https://www.strava.com/settings/api (Authorization Callback Domain: `127.0.0.1` or `localhost`).")
        say("2. Run:")
        say("   python -I brain/_atlas/tools/atlas.py coach auth-strava --client-id <ID> --client-secret <SECRET>")
        return

    if getattr(args, "access_token", None) and not getattr(args, "code", None):
        say(f"strava auth: saved credentials and token to {fwd(_strava_env_path(brain))}.")
        return

    port = getattr(args, "port", 8766) or 8766
    code = getattr(args, "code", None)
    if not code:
        import http.server
        import urllib.parse
        import webbrowser
        redirect_uri = f"http://127.0.0.1:{port}/callback"
        auth_url = (
            f"https://www.strava.com/oauth/authorize?client_id={cid}&response_type=code"
            f"&redirect_uri={urllib.parse.quote(redirect_uri, safe='')}"
            f"&approval_prompt=force&scope=read,activity:read_all,profile:read_all"
        )
        say(f"Opening Strava authorization in browser:\n  {auth_url}")
        captured = {"code": None}

        class _OAuthHandler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a, **kw):
                pass

            def do_GET(self):
                parsed = urllib.parse.urlparse(self.path)
                qs = urllib.parse.parse_qs(parsed.query)
                if "code" in qs:
                    captured["code"] = qs["code"][0]
                    body = b"<html><body style='font-family:sans-serif;background:#070a0f;color:#34d399;padding:40px'><h2>&#10003; Atlas Coach mit Strava verbunden!</h2><p>Du kannst dieses Browser-Tab jetzt schliessen.</p></body></html>"
                    self.send_response(200)
                else:
                    body = b"<html><body>Authorization failed or cancelled.</body></html>"
                    self.send_response(400)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        srv = http.server.HTTPServer(("127.0.0.1", port), _OAuthHandler)
        srv.timeout = 120
        try:
            webbrowser.open(auth_url)
        except Exception:
            pass
        srv.handle_request()
        srv.server_close()
        code = captured["code"]

    if not code:
        sys.exit("strava auth: did not receive authorization code.")

    import urllib.parse
    import urllib.request
    data = urllib.parse.urlencode({
        "client_id": cid,
        "client_secret": csec,
        "code": code,
        "grant_type": "authorization_code",
    }).encode("utf-8")
    req = urllib.request.Request("https://www.strava.com/oauth/token", data=data, method="POST")
    with urllib.request.urlopen(req, timeout=20) as resp:
        res = json.loads(resp.read().decode("utf-8", errors="replace"))
    env_dict["STRAVA_ACCESS_TOKEN"] = res.get("access_token", "")
    env_dict["STRAVA_REFRESH_TOKEN"] = res.get("refresh_token", "")
    env_dict["STRAVA_EXPIRES_AT"] = str(res.get("expires_at", int(time.time()) + 21600))
    athlete = res.get("athlete") or {}
    if athlete.get("id"):
        env_dict["STRAVA_ATHLETE_ID"] = str(athlete["id"])
    _save_strava_env(brain, env_dict)
    say(f"strava auth: connected athlete {env_dict.get('STRAVA_ATHLETE_ID', 'ok')} -> {fwd(_strava_env_path(brain))}")


def _coach_context(root, brain, hk, mode="auto", deep_dive=None, open_cal=False, no_sync=False):
    """Assemble and print the mode-specific 3-Tier Context Package for the Atlas Sport-Coach."""
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

    say(f"=== 1. ATLAS SPORT-COACH CONTEXT (mode: {eff_mode}) ===")
    say(f"  Macro Plan: active_phase='{macro['active_phase']}', updated='{macro['updated'] or 'never'}', "
        f"goals_hash='{macro['goals_hash']} -> {macro['current_goals_hash']}', stale={macro['stale']}"
        + (f" ({macro['stale_reason']})" if macro["stale"] else ""))
    if macro["stale"] and eff_mode in ("plan-14d", "macro"):
        say("  [!] STALE MACRO PLAN DETECTED: You MUST update `brain/coach/macro-plan.md` (with current `updated:` and "
            f"`goals_hash: {macro['current_goals_hash']}`) BEFORE writing 14-day workouts in `brain/calendar/README.md`!")
    if c_state["strava_connected"]:
        say(f"  Strava: connected ({len(c_state['activities'])} cached activities)")
    else:
        say("  Strava: NOT CONNECTED (`brain/_secrets/strava.env` missing). Offer onboarding via `atlas.py coach auth-strava`.")

    pend = c_state.get("pending_feedback", [])
    if pend:
        say(f"  [!] PENDING RIDE CHECK-IN ({len(pend)} recent activity/ies without athlete feedback):")
        for p in pend[:3]:
            say(f"      - {p.get('date')} '{p.get('name')}' ({p.get('duration_min', 0)}m, NP {p.get('np_w') or '—'}W): "
                f"Ask 1 concise question how it felt (RPE/legs) and save via `atlas.py coach note --date {p.get('date')} --text \"...\"`")

    say("\n=== 2. ATHLETE PROFILE & THRESHOLDS (brain/coach/profile.md) ===")
    prof_path = os.path.join(brain, "coach", "profile.md")
    if os.path.isfile(prof_path):
        say(read(prof_path).strip())

    say("\n=== 3. MACRO PERIODIZATION (brain/coach/macro-plan.md) ===")
    macro_path = os.path.join(brain, "coach", "macro-plan.md")
    if os.path.isfile(macro_path):
        if eff_mode == "macro" or macro["stale"]:
            say(read(macro_path).strip())
        else:
            # Slice current + next 4 weeks from macro-plan.md
            cur_kw = dt.date.today().isocalendar()[1]
            say(f"Active Phase: {macro['active_phase']} (updated {macro['updated']})")
            for w in macro.get("weeks", [])[:8]:
                say(f"  KW {w['kw']} ({w['monday']}): Phase={w['phase']} | Vol={w['hours']} | "
                    f"Cycling={w['cycling']} | Run/Strength={w['running_strength']} | Notes={w['notes']}")

    if eff_mode in ("plan-14d", "macro"):
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
        say(f"coach status: module={'coach' in get_state(brain).get('modules', [])}, "
            f"strava_connected={c_state['strava_connected']}, "
            f"activities={len(c_state['activities'])}, "
            f"pending_feedback={len(c_state['pending_feedback'])}, "
            f"macro_phase='{macro['active_phase']}', macro_stale={macro['stale']} ({macro['stale_reason']})")
    elif action == "auth-strava":
        _coach_auth_strava(root, brain, args)
    elif action == "sync":
        _coach_sync(root, brain, hk, days=args.days, mock_file=args.mock_file, execute=args.execute, quiet=False)
    elif action == "note":
        if not args.date or not args.text:
            sys.exit("coach note requires --date YYYY-MM-DD and --text \"...\"")
        _coach_note(root, brain, hk, args.date, args.text, rpe=args.rpe)
    elif action == "zwo":
        _coach_zwo(root, brain, hk, date_str=args.date, days=args.days)
    else:
        _coach_context(root, brain, hk, mode=args.mode, deep_dive=args.deep_dive,
                       open_cal=getattr(args, "open", False), no_sync=getattr(args, "no_sync", False))


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
            if fn.startswith("state.json") or fn == ROUTER_BASE:
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


def _plan_core(brain, root, src):
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
        items.append((rel, _three_way(local.get(rel), base.get(rel), new.get(rel, (None, None))[0])))
    # The router's core block (in brain/ROUTER.md) takes part like a file.
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
    router = _three_way(local_block, base_block, up_block) if local_block is not None else "missing"
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
        plan = _plan_core(brain, root, src)
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
    set_state(brain, version=plan["v_new"])
    dump_json(os.path.join(rdir, "applied.json"), {"source": fwd(src), "from": plan["v_old"], "to": plan["v_new"],
                                                   "result": result, "notes": notes})
    # Re-wire with the NEW tool and probe every wired agent; any failure rolls everything back.
    tool = os.path.join(atlas_dir, "tools", "atlas.py")
    st = get_state(brain)
    wired = st.get("wired", {}) if isinstance(st.get("wired"), dict) else {}
    failed = []
    for key, info in wired.items():
        agent, scope = key.split(":")
        base_cmd = [sys.executable, "-I", tool, "--root", root]
        r1 = subprocess.run(base_cmd + ["wire", "--agent", agent, "--scope", scope, "--execute"] +
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
            subprocess.run([sys.executable, "-I", tool, "--root", root, "wire", "--agent", agent, "--scope", scope,
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
    subprocess.run([sys.executable, "-I", os.path.join(brain, "_atlas", "tools", "atlas.py"), "--root", root,
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


# ================================================================= main

def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # agents read this output; avoid code-page mojibake
        except (AttributeError, ValueError):
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
    p.add_argument("--fix", action="store_true", help="mechanical repairs: moved-page links, tag synonyms, cache, routes")

    p = sub.add_parser("deadlines")
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--all", action="store_true")
    p.add_argument("--ics")

    p = sub.add_parser("calendar")
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--all", action="store_true")
    p.add_argument("--include-done", action="store_true")
    p.add_argument("--for", dest="for_role", choices=("coach",), help="filtered view for a specific role (e.g. coach)")
    p.add_argument("--init", action="store_true", help="harvest existing dates, birthdays and open to-dos into calendar/README.md")
    p.add_argument("--open", action="store_true", help="open the zero-token visual HTML calendar in the browser")
    p.add_argument("--ics", help="export calendar timeline to an .ics file")
    p.add_argument("--sync-ics", action="store_true", help="merge upcoming events from settings.md calendar_ics or --ics-source")
    p.add_argument("--ics-source", help="URL or file path of an .ics feed")
    p.add_argument("--execute", action="store_true")

    p = sub.add_parser("coach")
    p.add_argument("action", nargs="?", default="context",
                   choices=("context", "status", "auth-strava", "sync", "note", "zwo"),
                   help="coach action (default: context)")
    p.add_argument("--init", action="store_true", help="scaffold brain/coach/ and harvest existing athlete thresholds")
    p.add_argument("--mode", choices=("auto", "daily", "checkin", "plan-14d", "macro"), default="auto",
                   help="context slicing mode (default: auto)")
    p.add_argument("--deep-dive", help="date (YYYY-MM-DD) for full activity stream/lap telemetry")
    p.add_argument("--date", help="target date (YYYY-MM-DD) for `note` or `zwo`")
    p.add_argument("--text", help="athlete feedback note text for `note`")
    p.add_argument("--rpe", type=int, help="optional RPE (1-10) for `note`")
    p.add_argument("--days", type=int, default=14, help="horizon in days for `sync` (default 90) or `zwo` (default 14)")
    p.add_argument("--mock-file", help="offline Strava JSON fixture for `sync`")
    p.add_argument("--client-id", help="Strava API Client ID for `auth-strava`")
    p.add_argument("--client-secret", help="Strava API Client Secret for `auth-strava`")
    p.add_argument("--access-token", help="Strava OAuth Access Token for `auth-strava`")
    p.add_argument("--refresh-token", help="Strava OAuth Refresh Token for `auth-strava`")
    p.add_argument("--code", help="Strava OAuth authorization code for headless `auth-strava`")
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
        p.add_argument("--execute", action="store_true")
        if name == "core-apply":
            p.add_argument("--rollback", help="restore the core backed up by this run id")
            p.add_argument("--user-home", help="for tests: use this folder instead of ~ when re-wiring")
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

    args = ap.parse_args()
    handlers = {"init": cmd_init, "routes": cmd_routes, "projects": cmd_projects, "index": cmd_index, "probe": cmd_probe,
                "find": cmd_find, "mark": cmd_mark, "adopt": cmd_adopt, "code-sync": cmd_code_sync,
                "core-accept": cmd_core_accept, "dev-check": cmd_dev_check, "dev-setup": cmd_dev_setup,
                "check": cmd_check, "deadlines": cmd_deadlines, "calendar": cmd_calendar, "coach": cmd_coach,
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
