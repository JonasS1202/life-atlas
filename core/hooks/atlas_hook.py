"""Atlas hook for Claude Code and Google Antigravity. Python 3 standard library only. Fail-open.

Registered by `atlas.py wire` (project scope: <ROOT>/.claude/settings.json, <ROOT>/.agents/hooks.json):
  python -I <ROOT>/brain/_atlas/hooks/atlas_hook.py <agent> <event>
    agent: claude | antigravity
    event: session  (Claude SessionStart,      Antigravity SessionStart)
           prompt   (Claude UserPromptSubmit,  Antigravity PreInvocation, first invocation of a turn only)
           stop     (Claude Stop,              Antigravity Stop)

session: heartbeat `Atlas · ok`; pull the brain from its remote; once a day (settings) a briefing with deadlines,
         inbox, due maintenance/update checks, things Antigravity's /learn wrote outside Atlas, one question; in other
         chats only what is overdue or due today.
prompt:  ROUTING first (project named by name, alias, folder, path, or the chat's folder; once per chat), learning
         signals ("remember", "from now on", corrections), then knowledge hints with their one-line summary.
         Prints nothing when nothing matches.
stop:    daily log line from the `Atlas:` line; auto-commit and push the brain; remind the agent to file what it
         learned (always after a learning signal, otherwise throttled; settings `reminder`).

Behaviour switches live in brain/settings.md (frontmatter). Any error ends with exit code 0 and no output.
"""
import datetime as dt
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import time

BRAIN = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
PROBE = bool(os.environ.get("ATLAS_PROBE"))  # set by `atlas.py probe`: run, but change nothing
STATE = os.path.join(BRAIN, "_atlas", "state.json")
CACHE = os.path.join(BRAIN, "_atlas", "cache", "index.json")
CAL_HTML = os.path.join(BRAIN, "_atlas", "cache", "calendar.html")
SKIP_DIRS = {".git", "_secrets", "_archive", "_atlas", "node_modules", "__pycache__", ".obsidian", ".trash"}
TEXT_EXTS = {".md", ".txt"}
CANONICAL = ("areas/", "projects/", "people/", "howto/", "self/", "calendar/", "glossary.md", "INDEX.md")
MAX_HINTS = 5
MAX_ROUTES = 3
INDEX_BUDGET_S = 2.5   # hooks parse at most this long per call; `atlas.py index` builds the full cache
MAX_DF_SHARE = 0.6
HEAD_BYTES = 16_000
UPDATE_DAYS = 30
PUSH_REMIND_DAYS = 7
PUSH_RETRY_MIN = 15
NAG_MINUTES = 15
LEARN_SUGGEST_TURNS = 20
MAINT_CHANGES = 50
DEFAULTS = {"briefing": "daily", "questions": "per-briefing", "maintenance_days": "7", "reminder": "signals",
            "deadline_days": "30", "daily_log": "on", "inbox_cloud": "", "calendar_ics": "", "evening_hour": "18"}
MARKER = re.compile(r"(?mi)^[\s>*_`-]*atlas\s*\**\s*:")
MARKER_LINE = re.compile(r"(?mi)^[\s>*_`-]*atlas\s*\**\s*:\s*(.+?)\s*$")
AUTOMATED = re.compile(r"<task-notification>|\[SYSTEM NOTIFICATION|Subagent hand-back|<side-sessions-event>")
DATE_ITEM = re.compile(r"^\s*(?:-\s*)?(\[[ xX-]\]\s*)?(?:yearly\s+|--)?(\d{4}-\d{2}-\d{2}|\d{2}-\d{2})"
                       r"(?:\.\.(\d{4}-\d{2}-\d{2}))?\b\s*[-:–—]?\s*(.*)$", re.I)
TIME_PREFIX = re.compile(r"^(\d{1,2}:\d{2}(?:-\d{1,2}:\d{2})?)\s+(.*)$")
KIND_PREFIX = re.compile(r"^\[([a-zA-Z]+)(?::([a-zA-Z0-9_-]+))?\]\s*(.*)$")
TODO_ITEM = re.compile(r"^\s*-\s*\[([ xX-])\]\s*(?:\[(high|med|low)\]\s*)?(.+)$", re.I)
TOKEN_ID = re.compile(r"\b(?=[A-Za-z0-9./-]*\d)[A-Za-z0-9][A-Za-z0-9./-]{3,}[A-Za-z0-9]\b")
NAME_STOP = {"test", "tests", "app", "apps", "demo", "tmp", "temp", "notes", "misc", "code", "project", "projekt",
             "projects", "projekte", "readme", "main", "src", "docs", "data", "web", "api", "tool", "tools"}
SIGNAL_EXPLICIT = re.compile(
    r"(?i)\b(merk dir|merke dir|merk es dir|speicher(?:e)? (?:das|dir|bitte)|notier(?:e)? (?:das|dir)|vergiss nicht|"
    r"ab jetzt|ab sofort|von nun an|in zukunft|künftig|kuenftig|nie wieder|immer wenn|remember (?:that|this)|"
    r"don'?t forget|from now on|going forward|never again)\b")
SIGNAL_CORRECTION = re.compile(
    r"(?i)(^\s*(nein|nee|falsch|no|wrong)\b[\s,.!]|\b(stimmt nicht|ist falsch|war falsch|nicht so|falsch verstanden|"
    r"korrigier|that'?s wrong|not like that|you misunderstood)\b)")
LEARN_HINTS = {"explicit": "keep it now where it belongs (a rule or preference goes to howto/ or brain/settings.md)",
               "correction": "a correction: fix the page it concerns and keep the lesson in howto/"}


# ---------------------------------------------------------------- small helpers

def _state():
    try:
        with open(STATE, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1, sort_keys=True, ensure_ascii=False)
    os.replace(tmp, path)


def _save_state(st):
    _save_json(STATE, st)


def _now():
    return dt.datetime.now(dt.timezone.utc)


def _now_iso():
    return _now().replace(microsecond=0).isoformat()


def _age_days(iso):
    try:
        then = dt.datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if then.tzinfo is None:
            then = then.replace(tzinfo=dt.timezone.utc)
        return (_now() - then).total_seconds() / 86400
    except ValueError:
        return None


def _root():
    root = _state().get("root")
    return os.path.normpath(root) if root else os.path.dirname(BRAIN)


def _norm_path(p):
    p = os.path.normpath(os.path.expanduser(str(p).strip().strip("\"'")))
    return p.replace("\\", "/").lower() if os.name == "nt" else p


def _norm_text(s):
    s = s.lower().replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")
    return s.replace("\\", "/")


def resolve(path):
    """Project paths may be relative to ROOT (`code/shop`, same on every device) or absolute."""
    p = os.path.expanduser(str(path).strip())
    return os.path.normpath(p if os.path.isabs(p) or re.match(r"^[A-Za-z]:", p) else os.path.join(_root(), p))


def _show(rel, cwd):
    """Path for the agent: relative to ROOT when the chat runs there (short), absolute otherwise."""
    root = _root()
    full = os.path.join(BRAIN, rel)
    if cwd and _norm_path(cwd).startswith(_norm_path(root)):
        return os.path.relpath(full, root).replace("\\", "/")
    return full.replace("\\", "/")


def frontmatter(text):
    """Small YAML subset: `key: value`, `key: [a, b]` and `key:` followed by `- item` lines."""
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    out, key = {}, None
    for line in text[3:end].splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = re.match(r"^([A-Za-z_][\w-]*)\s*:\s*(.*)$", line)
        if m and not line.startswith((" ", "\t", "-")):
            key, val = m.group(1).lower(), re.sub(r"\s+#.*$", "", m.group(2)).strip()
            if val.startswith("[") and val.endswith("]"):
                out[key] = [v.strip().strip("\"'") for v in val[1:-1].split(",") if v.strip()]
            else:
                out[key] = val.strip("\"'") if val else []
            continue
        m = re.match(r"^\s*-\s+(.*)$", line)
        if m and key is not None:
            if not isinstance(out.get(key), list):
                out[key] = [out[key]] if out.get(key) else []
            out[key].append(m.group(1).strip().strip("\"'"))
    return out


def _as_list(v):
    return [v] if isinstance(v, str) and v else (v or [])


def settings():
    """brain/settings.md frontmatter over the defaults (charter: rhythms and style are the user's to set)."""
    out = dict(DEFAULTS)
    try:
        with open(os.path.join(BRAIN, "settings.md"), encoding="utf-8") as fh:
            fm = frontmatter(fh.read(8000))
    except OSError:
        fm = {}
    for k, v in fm.items():
        if isinstance(v, str):
            out[k] = v
    return out


def _int(v, default):
    try:
        return int(str(v).strip())
    except ValueError:
        return default


def tag_vocab():
    """brain/tags.md: {tag: [synonyms]} from rows `| **tag** | meaning | syn1, syn2 |`."""
    vocab = {}
    try:
        with open(os.path.join(BRAIN, "tags.md"), encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return vocab
    for line in text.splitlines():
        if not line.startswith("|") or line.count("|") < 3:
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        m = re.match(r"^\*\*([^*]+)\*\*$", cells[0])
        if m:
            syn = cells[2] if len(cells) > 2 else ""
            vocab[m.group(1).strip().lower()] = [s.strip().lower() for s in syn.split(",") if s.strip()]
    return vocab


def _git(*args, timeout=10):
    return subprocess.run(["git", "-c", "core.quotePath=false", "-C", BRAIN, *args], capture_output=True, text=True,
                          timeout=timeout, encoding="utf-8", errors="replace")


# ---------------------------------------------------------------- cache

def _parse(path, rel):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read() if rel == "glossary.md" else fh.read(HEAD_BYTES)
    except OSError:
        return None
    fm = frontmatter(text)
    m = re.search(r"(?m)^#\s+(.+)$", text)
    title = re.sub(r"[`*_]", "", m.group(1)).strip() if m else ""
    summary = fm.get("summary") if isinstance(fm.get("summary"), str) else ""
    entry = {"title": title, "summary": summary[:160],
             "tags": [t.lower() for t in _as_list(fm.get("tags"))],
             "aliases": [a for a in _as_list(fm.get("aliases")) if len(a) >= 3],
             "due": _as_list(fm.get("due")),
             "paths": [p for p in (_as_list(fm.get("paths")) or _as_list(fm.get("path"))) if p and "<" not in p],
             "status": fm.get("status") if isinstance(fm.get("status"), str) else ""}
    if rel == "glossary.md":
        terms = []
        for line in text.splitlines():
            cell = line.split("|")[1] if line.startswith("|") and line.count("|") >= 3 else line
            terms += [t.strip() for t in re.findall(r"\*\*([^*]{3,60})\*\*", cell)]
        entry["glossary"] = terms
    return entry


def _priority(rel):
    """Parse order on a cold cache: routing registry first, then definitions, then the rest."""
    if rel.startswith("projects/") or rel in ("glossary.md", "tags.md", "settings.md"):
        return 0
    return 1 if rel.startswith(("people/", "self/", "calendar/")) else 2


def load_index(budget=None):
    """Cached metadata of every page. Only new or changed files are parsed. With `budget` (seconds) parsing stops in
    time and the partial cache is saved; the next call continues (a huge import never makes the hook time out)."""
    start = time.monotonic()
    try:
        with open(CACHE, encoding="utf-8") as fh:
            cached = json.load(fh)
        old = cached.get("files", {}) if cached.get("version") == 2 else {}
    except (OSError, ValueError):
        old = {}
    files, todo = {}, []
    for dp, dns, fns in os.walk(BRAIN):
        dns[:] = sorted(d for d in dns if d not in SKIP_DIRS and not d.startswith("."))
        for fn in fns:
            if os.path.splitext(fn)[1].lower() not in TEXT_EXTS:
                continue
            full = os.path.join(dp, fn)
            rel = os.path.relpath(full, BRAIN).replace("\\", "/")
            try:
                st = os.stat(full)
            except OSError:
                continue
            sig = [int(st.st_mtime), st.st_size]
            prev = old.get(rel)
            if prev and prev.get("sig") == sig:
                files[rel] = prev
            else:
                todo.append((_priority(rel), rel, full, sig))
    changed = len(files) != len(old)
    for _p, rel, full, sig in sorted(todo):
        if budget is not None and time.monotonic() - start > budget:
            break
        entry = _parse(full, rel)
        if entry is not None:
            entry["sig"] = sig
            files[rel] = entry
            changed = True
    if changed:
        try:
            _save_json(CACHE, {"version": 2, "files": files})
        except OSError:
            pass
    return files


# ---------------------------------------------------------------- output per agent

def emit_context(agent, event, text):
    if not text:
        return
    if agent == "claude":
        name = "SessionStart" if event == "session" else "UserPromptSubmit"
        out = {"hookSpecificOutput": {"hookEventName": name, "additionalContext": text}}
    elif event == "session":
        out = {"injectSteps": [{"systemMessage": {"systemMessage": text}}]}
    else:
        out = {"injectSteps": [{"ephemeralMessage": text}]}
    sys.stdout.write(json.dumps(out))


def emit_continue(agent, reason):
    decision = "block" if agent == "claude" else "continue"
    sys.stdout.write(json.dumps({"decision": decision, "reason": reason}))


# ---------------------------------------------------------------- session & calendar

def _next_yearly(month, day, today):
    for y in (today.year, today.year + 1):
        try:
            d = dt.date(y, month, day)
        except ValueError:
            d = dt.date(y, 2, 28)
        if d >= today:
            return d
    return dt.date(today.year + 1, month, day)


def _parse_cal_line(raw, rel, today, default_kind="deadline"):
    m = DATE_ITEM.match(raw)
    if not m:
        return None
    box = (m.group(1) or "").strip().lower()
    status = "done" if box == "[x]" else ("skipped" if box == "[-]" else "open")
    d1_str, d2_str, rest = m.group(2), m.group(3), m.group(4).strip()
    tm = TIME_PREFIX.match(rest)
    t_str = ""
    if tm:
        t_str, rest = tm.group(1), tm.group(2).strip()
    km = KIND_PREFIX.match(rest)
    kind, subkind = default_kind, ""
    if km:
        kind = km.group(1).lower()
        subkind = (km.group(2) or "").strip()
        rest = km.group(3).strip()
    elif re.search(r"\b(geburtstag|birthday)\b", rest, re.I):
        kind = "birthday"
    yearly = bool(re.match(r"^\s*(?:-\s*)?(?:\[[ xX-]\]\s*)?(?:yearly\s+|--)", raw, re.I)) or len(d1_str) == 5 or kind == "birthday"
    try:
        if len(d1_str) == 5:
            mm, dd = map(int, d1_str.split("-"))
            start = end = _next_yearly(mm, dd, today)
        else:
            start = dt.date.fromisoformat(d1_str)
            if yearly and not d2_str:
                birth_year = start.year
                start = end = _next_yearly(start.month, start.day, today)
                if birth_year < start.year and f"{start.year - birth_year}" not in rest:
                    rest = f"{rest} ({start.year - birth_year})"
            else:
                end = dt.date.fromisoformat(d2_str) if d2_str else start
    except ValueError:
        return None
    if end < start:
        end = start
    delta = 0 if start <= today <= end else ((start - today).days if start > today else (end - today).days)
    return {"start": start, "end": end, "delta": delta, "status": status, "time": t_str,
            "kind": kind, "subkind": subkind, "what": rest, "rel": rel}


def calendar_items(idx, days=30, past_days=3650, include_done=False):
    today = dt.date.today()
    items, seen = [], set()
    # 1. Central calendar files in brain/calendar/
    cal_dir = os.path.join(BRAIN, "calendar")
    if os.path.isdir(cal_dir):
        for fn in sorted(os.listdir(cal_dir)):
            if not fn.lower().endswith(".md"):
                continue
            rel = f"calendar/{fn}"
            try:
                with open(os.path.join(cal_dir, fn), encoding="utf-8", errors="replace") as fh:
                    lines = fh.read().splitlines()
            except OSError:
                continue
            in_fm = False
            for i, line in enumerate(lines):
                if i == 0 and line.strip() == "---":
                    in_fm = True
                    continue
                if in_fm:
                    if line.strip() in ("---", "..."):
                        in_fm = False
                    continue
                item = _parse_cal_line(line, rel, today, default_kind="event")
                if not item:
                    continue
                if not include_done and item["status"] != "open":
                    continue
                if -past_days <= item["delta"] <= days:
                    key = (item["start"], item["end"], item["what"].lower())
                    if key not in seen:
                        seen.add(key)
                        items.append(item)
    # 2. Decentralized `due:` frontmatter items across all brain pages
    for rel, e in idx.items():
        for raw in e.get("due", []):
            item = _parse_cal_line(raw, rel, today, default_kind="deadline")
            if not item:
                continue
            if not include_done and item["status"] != "open":
                continue
            if -past_days <= item["delta"] <= days:
                key = (item["start"], item["end"], item["what"].lower())
                if key not in seen:
                    seen.add(key)
                    items.append(item)
    return sorted(items, key=lambda x: (x["delta"], x["start"], x["time"], x["what"]))


def open_todos(include_done=False):
    cal_readme = os.path.join(BRAIN, "calendar", "README.md")
    if not os.path.isfile(cal_readme):
        return []
    try:
        with open(cal_readme, encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return []
    out, in_todos = [], False
    prio_rank = {"high": 0, "med": 1, "low": 2}
    for line in lines:
        if line.startswith("## "):
            in_todos = "to-do" in line.lower() or "todo" in line.lower() or "aufgaben" in line.lower()
            continue
        if DATE_ITEM.match(line):
            continue
        m = TODO_ITEM.match(line)
        if not m:
            continue
        if not in_todos and not m.group(2):
            continue
        box = m.group(1).lower()
        status = "done" if box == "x" else ("skipped" if box == "-" else "open")
        if not include_done and status != "open":
            continue
        prio = (m.group(2) or "med").lower()
        out.append({"status": status, "prio": prio, "what": m.group(3).strip(), "rel": "calendar/README.md",
                    "_r": prio_rank.get(prio, 1)})
    return sorted(out, key=lambda x: (x["_r"], x["what"]))


def deadlines(idx, days=30):
    today = dt.date.today()
    out = []
    for it in calendar_items(idx, days=days, past_days=3650, include_done=False):
        if it["delta"] < 0 and it["kind"] not in ("todo", "deadline"):
            continue
        when = today if it["start"] <= today <= it["end"] else it["start"]
        prefix = ""
        if it["rel"].startswith("calendar/") and it["kind"] != "deadline":
            k = f"{it['kind']}:{it['subkind']}" if it["subkind"] else it["kind"]
            prefix = f"[{k}] "
        if it["time"]:
            prefix = f"{it['time']} {prefix}"
        if it["start"] != it["end"]:
            prefix = f"({it['start'].isoformat()}..{it['end'].isoformat()}) {prefix}"
        out.append((when, it["delta"], (prefix + it["what"]).strip(), it["rel"]))
    return sorted(out)


CAL_TEMPLATE = """<!DOCTYPE html>
<html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="20">
<title>Atlas · Calendar & Coach Hub</title>
<style>
:root{--bg:#0b0f17;--card:#131a26;--border:#222d40;--text:#e6edf7;--muted:#8b9bb4;--accent:#38bdf8;
--workout:#10b981;--workout-plan:#0ea5e9;--goal:#f59e0b;--blocked:#f43f5e;--event:#8b5cf6;--todo:#f97316;--bday:#ec4899}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:system-ui,-apple-system,sans-serif;background:var(--bg);color:var(--text);padding:20px;line-height:1.4}
.top{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:center;gap:12px;margin-bottom:18px;padding-bottom:14px;border-bottom:1px solid var(--border)}
h1{font-size:1.3rem;font-weight:700;display:flex;align-items:center;gap:10px}
.badge-live{font-size:.72rem;background:#064e3b;color:#34d399;padding:3px 8px;border-radius:99px;font-weight:600}
.controls{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
button{background:var(--card);color:var(--text);border:1px solid var(--border);padding:6px 12px;border-radius:8px;cursor:pointer;font-size:.82rem;font-weight:500}
button:hover,button.active{border-color:var(--accent);background:#17253b;color:#fff}
.layout{display:grid;grid-template-columns:1fr 340px;gap:18px}
@media(max-width:1024px){.layout{grid-template-columns:1fr}}
.grid-wrap{background:var(--card);border:1px solid var(--border);border-radius:12px;overflow:hidden}
.dow{display:grid;grid-template-columns:repeat(7,1fr);background:#0f1520;border-bottom:1px solid var(--border)}
.dow div{padding:8px;text-align:center;font-size:.75rem;color:var(--muted);font-weight:600;text-transform:uppercase}
.days{display:grid;grid-template-columns:repeat(7,1fr);grid-auto-rows:minmax(108px,auto)}
.day{border-right:1px solid var(--border);border-bottom:1px solid var(--border);padding:6px;display:flex;flex-direction:column;gap:4px}
.day:nth-child(7n){border-right:none}
.day.other{opacity:.35;background:#0d121c}
.day.today{background:#152238;box-shadow:inset 0 0 0 1.5px var(--accent)}
.dnum{font-size:.78rem;font-weight:600;color:var(--muted);margin-bottom:2px;display:flex;justify-content:space-between}
.day.today .dnum{color:var(--accent)}
.pill{font-size:.72rem;padding:3px 6px;border-radius:5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;border-left:3px solid var(--accent);background:#1b2638}
.pill.workout-done{border-color:var(--workout);background:#06392c;color:#a7f3d0}
.pill.workout-open{border-color:var(--workout-plan);background:#0c2d48;color:#bae6fd}
.pill.workout-skipped{border-color:#64748b;background:#1e293b;color:#94a3b8;text-decoration:line-through}
.pill.goal{border-color:var(--goal);background:#3b2606;color:#fde68a;font-weight:600}
.pill.blocked{border-color:var(--blocked);background:#3b0a18;color:#fecdd3}
.pill.event{border-color:var(--event);background:#251447;color:#ddd6fe}
.pill.todo,.pill.deadline{border-color:var(--todo);background:#3b1a06;color:#fed7aa}
.pill.birthday{border-color:var(--bday);background:#3b0a28;color:#fbcfe8}
.side{display:flex;flex-direction:column;gap:14px}
.card{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:14px}
.card h2{font-size:.88rem;text-transform:uppercase;letter-spacing:.04em;color:var(--muted);margin-bottom:10px}
.list{display:flex;flex-direction:column;gap:7px;max-height:280px;overflow-y:auto}
.row{font-size:.8rem;padding:7px 9px;border-radius:7px;background:#0f1520;border:1px solid var(--border);display:flex;justify-content:space-between;gap:8px;align-items:flex-start}
.tag{font-size:.68rem;padding:1px 6px;border-radius:4px;background:#1e293b;color:var(--muted);white-space:nowrap}
.tag.high{background:#450a0a;color:#fca5a5}
.tag.med{background:#451a03;color:#fdba74}
.tag.goal{background:#451a03;color:#fde68a}
.empty{font-size:.8rem;color:var(--muted);font-style:italic}
</style></head><body>
<div class="top">
  <h1>📅 Atlas · Calendar & Coach Hub <span class="badge-live">Atlas-First · Live</span></h1>
  <div class="controls">
    <button id="f-all" class="active" onclick="setFilter('all')">Alle</button>
    <button id="f-coach" onclick="setFilter('coach')">🚴 Coach View</button>
    <button id="f-todo" onclick="setFilter('todo')">☑️ To-Dos & Fristen</button>
    <span style="width:12px"></span>
    <button onclick="moveMonth(-1)">◀</button>
    <button onclick="goToday()" id="mlabel" style="min-width:140px;font-weight:700"></button>
    <button onclick="moveMonth(1)">▶</button>
  </div>
</div>
<div class="layout">
  <div class="grid-wrap">
    <div class="dow"><div>Mo</div><div>Di</div><div>Mi</div><div>Do</div><div>Fr</div><div>Sa</div><div>So</div></div>
    <div class="days" id="grid"></div>
  </div>
  <div class="side">
    <div class="card"><h2>🏆 Goals & Wettkämpfe</h2><div class="list" id="goals"></div></div>
    <div class="card"><h2>📅 Nächste 14 Tage</h2><div class="list" id="agenda"></div></div>
    <div class="card"><h2>☑️ Offene To-Dos (Backlog)</h2><div class="list" id="todos"></div></div>
  </div>
</div>
<script>
const DATA = __DATA_JSON__;
let cur = new Date(DATA.today + "T00:00:00");
let filter = "all";
function setFilter(f){
  filter = f;
  for(const k of ['all','coach','todo']) document.getElementById('f-'+k).classList.toggle('active', k===f);
  render();
}
function moveMonth(d){ cur = new Date(cur.getFullYear(), cur.getMonth()+d, 1); render(); }
function goToday(){ cur = new Date(DATA.today + "T00:00:00"); render(); }
function iso(d){ return d.getFullYear()+'-'+String(d.getMonth()+1).padStart(2,'0')+'-'+String(d.getDate()).padStart(2,'0'); }
function keepItem(it){
  if(filter==='coach') return ['workout','blocked','goal'].includes(it.kind) || (it.kind==='event' && it.time);
  if(filter==='todo') return ['todo','deadline'].includes(it.kind);
  return true;
}
function pillClass(it){
  if(it.kind==='workout') return 'pill workout-' + it.status;
  return 'pill ' + it.kind;
}
function icon(it){
  if(it.kind==='workout') return (it.status==='done'?'✓ ':it.status==='skipped'?'– ':'🚴 ');
  if(it.kind==='goal') return '🏆 ';
  if(it.kind==='blocked') return '🚫 ';
  if(it.kind==='birthday') return '🎂 ';
  if(it.kind==='todo'||it.kind==='deadline') return '☑️ ';
  return '📅 ';
}
function render(){
  const y = cur.getFullYear(), m = cur.getMonth();
  const months = ['Januar','Februar','März','April','Mai','Juni','Juli','August','September','Oktober','November','Dezember'];
  document.getElementById('mlabel').textContent = months[m] + ' ' + y;
  const first = new Date(y, m, 1);
  const offset = (first.getDay() + 6) % 7;
  const start = new Date(y, m, 1 - offset);
  const grid = document.getElementById('grid');
  grid.innerHTML = '';
  for(let i=0;i<42;i++){
    const d = new Date(start.getFullYear(), start.getMonth(), start.getDate()+i);
    const ds = iso(d);
    const cell = document.createElement('div');
    cell.className = 'day' + (d.getMonth()!==m?' other':'') + (ds===DATA.today?' today':'');
    cell.innerHTML = `<div class="dnum"><span>${d.getDate()}</span></div>`;
    for(const it of DATA.items){
      if(!keepItem(it)) continue;
      if(it.start <= ds && ds <= it.end){
        const p = document.createElement('div');
        p.className = pillClass(it);
        const t = it.time ? it.time+' ' : '';
        p.title = `${it.start}${it.end!==it.start?'..'+it.end:''} ${t}[${it.kind}${it.subkind?':'+it.subkind:''}] ${it.what} (${it.rel})`;
        p.textContent = icon(it) + t + it.what;
        cell.appendChild(p);
      }
    }
    grid.appendChild(cell);
  }
  const goalsEl = document.getElementById('goals');
  const goals = DATA.items.filter(i => i.kind==='goal' && i.delta>=0 && i.status==='open');
  goalsEl.innerHTML = goals.length ? goals.map(g =>
    `<div class="row"><div><strong>${g.what}</strong><div style="color:var(--muted);font-size:.72rem">${g.start}</div></div><span class="tag goal">in ${g.delta}d (${(g.delta/7).toFixed(1)}w)</span></div>`
  ).join('') : '<div class="empty">Keine Goals eingetragen</div>';
  const agEl = document.getElementById('agenda');
  const ag = DATA.items.filter(i => keepItem(i) && i.delta>=0 && i.delta<=14 && i.status==='open');
  agEl.innerHTML = ag.length ? ag.map(a =>
    `<div class="row"><div><strong>${icon(a)}${a.time?a.time+' ':''}${a.what}</strong><div style="color:var(--muted);font-size:.72rem">${a.start}${a.end!==a.start?'..'+a.end:''} · ${a.rel}</div></div><span class="tag">${a.delta===0?'heute':'in '+a.delta+'d'}</span></div>`
  ).join('') : '<div class="empty">Keine Einträge in den nächsten 14 Tagen</div>';
  const tdEl = document.getElementById('todos');
  tdEl.innerHTML = DATA.todos.length ? DATA.todos.map(t =>
    `<div class="row"><div>${t.what}</div><span class="tag ${t.prio}">${t.prio}</span></div>`
  ).join('') : '<div class="empty">Keine offenen To-Dos</div>';
}
render();
</script></body></html>
"""


def _ensure_calendar_server(cache_dir, start_server=False):
    """Ensure a local HTTP server serves cache_dir so http://127.0.0.1:<port>/calendar.html opens rendered in browsers
    instead of as raw HTML source code in IDE editors (which intercept file:// links)."""
    import socket
    port_file = os.path.join(cache_dir, "cal_port.txt")
    port = 8765
    try:
        with open(port_file, encoding="utf-8") as fh:
            port = int(fh.read().strip())
    except (OSError, ValueError):
        pass
    if not start_server or PROBE or os.environ.get("ATLAS_NO_CAL_SERVER"):
        return f"http://127.0.0.1:{port}/calendar.html"
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.1):
            return f"http://127.0.0.1:{port}/calendar.html"
    except OSError:
        pass
    for cand in range(8765, 8795):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("127.0.0.1", cand))
                port = cand
                break
        except OSError:
            continue
    try:
        with open(port_file, "w", encoding="utf-8") as fh:
            fh.write(str(port))
        py_exe = sys.executable
        if os.name == "nt":
            pyw = os.path.join(os.path.dirname(py_exe), "pythonw.exe")
            if os.path.isfile(pyw):
                py_exe = pyw
        server_cmd = [py_exe, "-I", "-m", "http.server", str(port), "--bind", "127.0.0.1",
                      "--directory", cache_dir]
        if os.name == "nt":
            # Antigravity/VS Code run_command on Windows uses a Job Object with KILL_ON_JOB_CLOSE that kills normal
            # child processes when atlas.py exits. Spawning via WMI Win32_Process.Create launches outside the Job Object.
            cmdline = subprocess.list2cmdline(server_cmd).replace("'", "''")
            wdir = cache_dir.replace("'", "''")
            ps = (f"$si = New-CimInstance -ClientOnly -ClassName Win32_ProcessStartup -Property @{{ShowWindow=[uint16]0}}; "
                  f"Invoke-CimMethod -ClassName Win32_Process -MethodName Create "
                  f"-Arguments @{{CommandLine='{cmdline}'; CurrentDirectory='{wdir}'; ProcessStartupInformation=$si}} | Out-Null")
            subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", ps],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=5, creationflags=0x08000000)
        else:
            subprocess.Popen(server_cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True)
        for _ in range(15):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    break
            except OSError:
                time.sleep(0.1)
    except Exception:
        pass
    return f"http://127.0.0.1:{port}/calendar.html"


def render_calendar_html(idx=None, start_server=False):
    """Generate brain/_atlas/cache/calendar.html (0 LLM tokens) and return its rendered http://127.0.0.1 URL."""
    if idx is None:
        idx = load_index(budget=INDEX_BUDGET_S)
    raw_items = calendar_items(idx, days=365, past_days=90, include_done=True)
    serial = [{k: (v.isoformat() if isinstance(v, dt.date) else v) for k, v in it.items()} for it in raw_items]
    todos = [{"status": t["status"], "prio": t["prio"], "what": t["what"], "rel": t["rel"]}
             for t in open_todos(include_done=False)]
    payload = {"today": dt.date.today().isoformat(), "items": serial, "todos": todos}
    html = CAL_TEMPLATE.replace("__DATA_JSON__", json.dumps(payload, ensure_ascii=False))
    cache_dir = os.path.dirname(CAL_HTML)
    os.makedirs(cache_dir, exist_ok=True)
    try:
        with open(CAL_HTML, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(html)
    except OSError:
        pass
    return _ensure_calendar_server(cache_dir, start_server=start_server)


def last_mark(kind):
    """(date, commit) of the newest commit `atlas: <kind>` (shared by all devices through Git), else None."""
    res = _git("log", "-1", "--format=%cI %H", f"--grep=^atlas: {kind}")
    out = res.stdout.strip().split()
    return (out[0], out[1]) if res.returncode == 0 and len(out) == 2 else None


def maintenance_due(s):
    mark = last_mark("maintenance")
    if mark:
        age = _age_days(mark[0]) or 0
        changed = len(_git("diff", "--name-only", f"{mark[1]}..HEAD").stdout.split())
    else:
        first = _git("log", "--reverse", "--format=%cI").stdout.split()
        age, changed = (_age_days(first[0]) or 0) if first else 0, 0
    due = age >= _int(s.get("maintenance_days"), 7) or changed >= MAINT_CHANGES
    return due, int(age), changed


def pull():
    """Multi-device: fetch the newest brain before the chat starts. On conflict abort cleanly and say so."""
    if not os.path.isdir(os.path.join(BRAIN, ".git")) or not _git("remote").stdout.strip():
        return ""
    if _git("status", "--porcelain").stdout.strip():
        _git("add", "-A")
        _git("commit", "-q", "-m", "atlas: auto-commit before pull")
    res = _git("pull", "-q", "--rebase", timeout=8)
    if res.returncode == 0:
        return ""
    _git("rebase", "--abort")
    return ("Sync: pulling the brain from its remote failed (conflict or offline). Tell the user; if it is a conflict, "
            "resolve it with them (git -C brain pull --rebase).")


def foreign_items(root, user_home=None):
    """Rules, skills and text that Antigravity's /learn (or anyone) put outside Atlas. Charter: one brain."""
    out = []
    skills = set()
    for base in (os.path.join(BRAIN, "_atlas", "skills"), os.path.join(BRAIN, "skills")):
        if os.path.isdir(base):
            skills |= set(os.listdir(base))
    places = [(os.path.join(root, ".agents", "rules"), "rule", False),
              (os.path.join(root, ".agents", "skills"), "skill", False)]
    files = [(os.path.join(root, "GEMINI.md"), False), (os.path.join(root, "AGENTS.md"), False)]
    if user_home:
        g = os.path.join(user_home, ".gemini")
        places += [(os.path.join(g, "config", "rules"), "rule", True), (os.path.join(g, "config", "skills"), "skill", True)]
        files += [(os.path.join(g, "AGENTS.md"), True), (os.path.join(g, "GEMINI.md"), True)]
    for folder, kind, glob in places:
        if not os.path.isdir(folder):
            continue
        for name in sorted(os.listdir(folder)):
            if kind == "rule" and name.endswith(".md") and not name.startswith("atlas-"):
                out.append({"kind": "rule", "path": os.path.join(folder, name), "global": glob})
            elif kind == "skill" and os.path.isdir(os.path.join(folder, name)) and name not in skills:
                out.append({"kind": "skill", "path": os.path.join(folder, name), "global": glob})
    for path, glob in files:
        try:
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
        except OSError:
            continue
        a, b = "<!-- atlas:start -->", "<!-- atlas:end -->"
        rest = (text[: text.index(a)] + text[text.index(b) + len(b):]) if a in text and b in text else text
        if rest.strip():
            out.append({"kind": "text", "path": path, "global": glob})
    st = _state()
    ignored = set(st.get("adopt_ignored", []))
    snapshot = set(st.get("gemini_snapshot", []))
    return [i for i in out if os.path.normcase(i["path"]) not in ignored
            and not (i["global"] and os.path.normcase(i["path"]) in snapshot)]


def session_text(cwd, session):
    s = settings()
    st = _state()
    lines = ["Atlas · ok"]
    try:
        msg = "" if PROBE else pull()
    except Exception:
        msg = ""
    if msg:
        lines.append(msg)
    idx = load_index(budget=INDEX_BUDGET_S)
    today = dt.date.today().isoformat()
    due = deadlines(idx, days=_int(s.get("deadline_days"), 30))
    mode = s.get("briefing", "daily")
    last = st.get("last_briefing", "")
    brief = (mode == "daily" and last != today) or (mode == "weekly" and (not last or (
        dt.date.today() - dt.date.fromisoformat(last)).days >= 7))
    if brief:
        st["last_briefing"] = today
        parts = []
        if due:
            items = []
            for when, delta, what, rel in due[:8]:
                tag = f"OVERDUE {-delta} d" if delta < 0 else ("today" if delta == 0 else f"in {delta} d")
                items.append(f"- {when.isoformat()} ({tag}): {what or '(no note)'}  [{_show(rel, cwd)}]")
            parts.append("Calendar & deadlines:\n" + "\n".join(items))
        todos = open_todos(include_done=False)
        if todos:
            top = [f"[{t['prio']}] {t['what']}" for t in todos[:5]]
            parts.append(f"Open to-dos ({len(todos)} in brain/calendar/README.md): " + "; ".join(top))
        if not PROBE:
            try:
                cal_uri = render_calendar_html(idx)
                parts.append(f"Visual calendar: [📅 Kalender öffnen]({cal_uri}) (include this clickable link in the briefing)")
            except Exception:
                pass
        inbox = os.path.join(BRAIN, "inbox")
        waiting = [f for f in os.listdir(inbox) if not f.startswith(".") and f.lower() != "readme.md"] \
            if os.path.isdir(inbox) else []
        cloud = s.get("inbox_cloud", "").strip()
        cloud_waiting = [f for f in os.listdir(os.path.expanduser(cloud)) if not f.startswith(".")] \
            if cloud and os.path.isdir(os.path.expanduser(cloud)) else []
        if waiting or cloud_waiting:
            parts.append(f"Inbox: {len(waiting)} item(s) in brain/inbox"
                         + (f", {len(cloud_waiting)} in the cloud inbox {cloud}" if cloud_waiting else "")
                         + ". Offer /atlas-sort.")
        mdue, mage, mchanged = maintenance_due(s)
        if mdue:
            parts.append(f"Maintenance due ({mage} d, {mchanged} pages changed): after the user's first request run "
                         "/atlas-maintain (Claude: in a subagent), report in at most 3 lines.")
        upd = last_mark("update-check")
        if not upd or (_age_days(upd[0]) or 0) > UPDATE_DAYS:
            parts.append("Update check is due: offer /atlas-update.")
        if st.get("remote"):
            pushed = _age_days(st["last_push"]) if st.get("last_push") else None
            if pushed is None or pushed > PUSH_REMIND_DAYS:
                parts.append(f"Backup: last push of the brain is older than {PUSH_REMIND_DAYS} days.")
        try:
            foreign = foreign_items(_root(), os.path.expanduser("~"))
        except Exception:
            foreign = []
        if foreign:
            parts.append(f"{len(foreign)} rule/skill/text item(s) outside Atlas (e.g. from Antigravity /learn): "
                         "offer `atlas.py adopt` to move them into the brain.")
        if s.get("questions") == "per-briefing":
            parts.append("Question: ask the user one short question about a real gap that matters now (a missing "
                         "notice period, an unknown contact); skip it if nothing matters. Settings may turn this off.")
        lines.append("Atlas briefing (first chat today): give the user 3-6 short lines, then handle their request."
                     + ("\n" + "\n".join(parts) if parts else "\nNothing pending; say so in one line."))
    else:
        urgent = [d for d in due if d[1] <= 0]
        if urgent:
            lines.append("Due today/overdue (mention only if relevant or due today): " + "; ".join(
                f"{w.isoformat()} {what} [{_show(rel, cwd)}]" for w, _d, what, rel in urgent[:5]))
    hour = dt.datetime.now().hour
    if s.get("questions") == "evening-3" and hour >= _int(s.get("evening_hour"), 18) and st.get("last_evening") != today:
        st["last_evening"] = today
        lines.append("Evening (settings): offer the user three short diary questions; keep the answers in "
                     f"journal/{today[:4]}/{today}.md and anything lasting on its page.")
    if s.get("questions") == "weekly-session" and (_age_days(st.get("last_weekly", "1970-01-01T00:00:00+00:00"))
                                                   or 99) >= 7:
        st["last_weekly"] = _now_iso()
        lines.append("Weekly session (settings): offer a short /atlas-interview round today.")
    st["last_hook_run"] = _now_iso()
    if not PROBE:  # `atlas.py probe` must not use up the day's briefing
        _save_state(st)
        _sync_antigravity_router(st)
    return "\n".join(lines)


def _sync_antigravity_router(st):
    """Fallback when file includes do not work for Antigravity: keep a copy of the router between markers."""
    target = st.get("antigravity_router_copy")
    if not target or not os.path.isfile(target):
        return
    try:
        with open(os.path.join(BRAIN, "ROUTER.md"), encoding="utf-8") as fh:
            router = fh.read().strip()
        with open(target, encoding="utf-8") as fh:
            current = fh.read()
    except OSError:
        return
    start, end = "<!-- atlas:router:start -->", "<!-- atlas:router:end -->"
    if start not in current or end not in current:
        return
    new = current[: current.index(start)] + f"{start}\n{router}\n{end}" + current[current.index(end) + len(end):]
    if new != current:
        with open(target, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(new)


# ---------------------------------------------------------------- prompt: routing

def projects(idx):
    """Registry: one entry per project page (projects/<slug>/README.md or projects/<slug>.md)."""
    out = []
    for rel, e in idx.items():
        parts = rel.split("/")
        if parts[0] != "projects" or rel == "projects/README.md":
            continue
        if len(parts) == 3 and parts[2].lower() == "readme.md":
            slug = parts[1]
        elif len(parts) == 2:
            slug = os.path.splitext(parts[1])[0]
        else:
            continue
        title = re.sub(r"(?i)^(project|projekt)\s*:\s*", "", e.get("title", "")).strip()
        names = {slug, title, *e.get("aliases", [])}
        names |= {os.path.basename(os.path.normpath(p)) for p in e.get("paths", [])}
        out.append({"slug": slug, "page": rel, "title": title or slug, "paths": e.get("paths", []),
                    "resolved": [resolve(p) for p in e.get("paths", [])], "summary": e.get("summary", ""),
                    "names": sorted(n for n in names if n), "status": e.get("status", "")})
    return out


def _name_rx(name):
    tokens = [t for t in re.split(r"[\s_\-./]+", _norm_text(name)) if t]
    if not tokens or (len(tokens) == 1 and (len(tokens[0]) < 4 or tokens[0] in NAME_STOP)):
        return None
    return re.compile(r"(?<![a-z0-9])" + r"[\s_\-./]*".join(map(re.escape, tokens)) + r"(?![a-z0-9])")


def route(prompt, cwd, idx):
    """Projects the message or the working folder points to: [(project, reason)]."""
    found = {}
    here = _norm_path(cwd) if cwd else ""
    text = _norm_text(prompt)
    for p in projects(idx):
        for full in p["resolved"]:
            r = _norm_path(full).rstrip("/")
            if here and (here == r or here.startswith(r + "/")):
                found.setdefault(p["slug"], (p, "chat runs in its folder"))
            if r and r in text:
                found.setdefault(p["slug"], (p, "path in message"))
        for name in p["names"]:
            rx = _name_rx(name)
            if rx and rx.search(text):
                found.setdefault(p["slug"], (p, f"'{name}' in message"))
                break
    return list(found.values())[:MAX_ROUTES]


# ---------------------------------------------------------------- prompt: knowledge hints

def _vocab(idx, skip_pages):
    terms = {}
    for t in idx.get("glossary.md", {}).get("glossary", []):
        terms[t] = "glossary.md"
    for rel, e in idx.items():
        if rel in skip_pages or not rel.startswith(("people/", "areas/", "howto/", "projects/", "self/")):
            continue
        title = e.get("title", "")
        if 3 <= len(title) <= 60 and not rel.endswith("README.md"):
            terms.setdefault(title, rel)
        if rel.startswith("people/") and title:
            words = title.split()
            if len(words) >= 2 and len(words[-1]) >= 4:
                terms.setdefault(words[-1], rel)
            if words and len(words[0]) >= 3:
                terms.setdefault(words[0], rel)
        for a in e.get("aliases", []):
            terms.setdefault(a, rel)
    return terms


def _grep_counts(term, word):
    args = ["grep", "--untracked", "-I", "-i", "-F", "-c"] + (["-w"] if word else [])
    args += ["-e", term, "--", ".", ":(exclude)_secrets", ":(exclude)_archive", ":(exclude)_atlas"]
    res = _git(*args, timeout=5)
    if res.returncode not in (0, 1):
        raise RuntimeError(res.stderr)
    out = {}
    for line in res.stdout.splitlines():
        path, _, n = line.rpartition(":")
        if path.endswith((".md", ".txt")) and n.isdigit():
            out[path] = int(n)
    return out


def hints(prompt, idx, skip_pages):
    vocab = _vocab(idx, skip_pages)
    low, comp = _norm_text(prompt), re.sub(r"[^a-z0-9]", "", _norm_text(prompt))
    cands = {}
    for t in vocab:
        tl = _norm_text(t)
        if re.search(r"(?<![\w-])" + re.escape(tl) + r"(?![\w-])", low):
            cands[t] = True
        elif len(re.sub(r"[^a-z0-9]", "", tl)) >= 6 and re.sub(r"[^a-z0-9]", "", tl) in comp:
            cands[t] = True
    for m in TOKEN_ID.finditer(prompt):
        cands.setdefault(m.group(0), False)
    # Tags (and their synonyms from tags.md) point straight at the pages carrying them; a page that still uses a
    # synonym as tag counts too (maintenance normalises it later).
    vocab_tags = tag_vocab()
    canon = {s: t for t, syns in vocab_tags.items() for s in syns}
    tag_pages = {}
    for tag, syns in vocab_tags.items():
        for word in [tag] + syns:
            if len(word) >= 3 and re.search(r"(?<![\w-])" + re.escape(_norm_text(word)) + r"(?![\w-])", low):
                tag_pages[tag] = [r for r, e in idx.items() if r not in skip_pages
                                  and tag in {canon.get(t, t) for t in e.get("tags", [])}]
                break
    if not cands and not tag_pages:
        return []
    n = max(len(idx), 1)
    scores, hits = {}, {}
    for tag, pages in tag_pages.items():
        if pages and len(pages) <= MAX_DF_SHARE * n:
            idf = math.log(1 + n / len(pages))
            for rel in pages:
                scores[rel] = scores.get(rel, 0.0) + 1.5 * idf
                hits.setdefault(rel, set()).add("#" + tag)
    for term, word in list(cands.items())[:12]:
        try:
            counts = _grep_counts(term, word)
        except Exception:
            continue
        counts = {p: c for p, c in counts.items() if p not in skip_pages}
        if not counts or len(counts) > MAX_DF_SHARE * n:
            continue
        idf = math.log(1 + n / len(counts))
        for rel, c in counts.items():
            score = idf * (1 + math.log(c)) * (1.3 if rel.startswith(CANONICAL) else 1.0)
            if vocab.get(term) == rel:
                score += 2 * idf
            scores[rel] = scores.get(rel, 0.0) + score
            hits.setdefault(rel, set()).add(term)
    ranked = sorted(scores, key=lambda r: (-scores[r], r))[:MAX_HINTS]
    return [(rel, idx.get(rel, {}).get("summary", ""), sorted(hits[rel], key=str.lower)[:4]) for rel in ranked]


def _session(st, session):
    sessions = st.setdefault("sessions", {})
    s = sessions.setdefault(session or "unknown", {})
    s["t"] = _now_iso()
    if len(sessions) > 200:
        for old in sorted(sessions, key=lambda k: sessions[k].get("t", ""))[:-200]:
            sessions.pop(old, None)
    return s


def prompt_text(prompt, cwd, session):
    idx = load_index(budget=INDEX_BUDGET_S)
    st = _state()
    sess = _session(st, session)
    lines = []
    routes = route(prompt, cwd, idx)
    seen = set(sess.get("routed", []))
    root = _root()
    for p, why in routes:
        if p["slug"] in seen:
            continue
        where = ", ".join((os.path.relpath(x, root).replace("\\", "/") if _norm_path(x).startswith(_norm_path(root) + "/")
                           and cwd and _norm_path(cwd).startswith(_norm_path(root)) else x.replace("\\", "/"))
                          for x in p["resolved"]) or "no folder"
        lines.append(f"Atlas route -> project '{p['title']}' ({why}). Read {_show(p['page'], cwd)} first, then the "
                     f"repository's own agent files in {where} (CLAUDE.md, AGENTS.md, GEMINI.md, .claude/rules, "
                     ".agents/rules) once; after that work there like a normal coding agent.")
        sess.setdefault("routed", []).append(p["slug"])
        sess["project"] = p["slug"]
    kind = "explicit" if SIGNAL_EXPLICIT.search(prompt) else ("correction" if SIGNAL_CORRECTION.search(prompt) else "")
    if kind:
        sess["signal"] = kind
        lines.append(f"Atlas learning signal ({kind}): {LEARN_HINTS[kind]}.")
    skip = {p["page"] for p, _ in routes}
    # File paths in the message are routing input, not search terms ("C:/Atlas/code/shop" must not hint "Atlas").
    plain = re.sub(r"[A-Za-z]:[\\/][^\s\"'`]*|~?/[\w.-]+/[^\s\"'`]*", " ", prompt)
    found = hints(plain, idx, skip) if len(plain.strip()) >= 3 else []
    if found:
        lines.append("Atlas pages for this message (leads; open only what you need):")
        for rel, summary, terms in found:
            lines.append(f"- {_show(rel, cwd)}" + (f" — {summary}" if summary else "") + f" [{', '.join(terms)}]")
    _save_state(st)
    return "\n".join(lines)


# ---------------------------------------------------------------- stop

def autocommit_and_push():
    if not os.path.isdir(os.path.join(BRAIN, ".git")):
        return
    if _git("status", "--porcelain").stdout.strip():
        _git("add", "-A")
        _git("commit", "-q", "-m", "atlas: auto-commit at end of turn")
    if not _git("remote").stdout.strip():
        return
    st = _state()
    # Push after every turn with new commits (other devices should see them soon); only after a failed push (offline)
    # wait PUSH_RETRY_MIN before trying again, so turns do not keep stalling on a dead network.
    failed = _age_days(st["push_failed"]) if st.get("push_failed") else None
    if failed is not None and failed * 1440 < PUSH_RETRY_MIN:
        return
    ahead = _git("rev-list", "--count", "@{u}..HEAD")
    if ahead.returncode == 0 and ahead.stdout.strip() == "0":
        return
    branch = _git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip() or "main"
    ok = _git("push", "-q", "-u", "origin", branch, timeout=12).returncode == 0
    st = _state()
    if ok:
        st["last_push"] = _now_iso()
        st.pop("push_failed", None)
    else:
        st["push_failed"] = _now_iso()
    _save_state(st)


def daily_log(text, context):
    """Append the `Atlas:` line(s) to journal/<year>/<date>.md (0 tokens: the script writes it)."""
    # Antigravity transcripts are JSON: cut the line where the JSON string ends ("}, ", ...).
    found = [re.split(r'(?<!\\)"\s*[,}\]]', m.group(1))[0].strip().strip('"\\')
             for m in MARKER_LINE.finditer(text or "")]
    found = [f for f in found if f and not f.lower().startswith("reminder")]
    if not found:
        return
    today = dt.date.today().isoformat()
    path = os.path.join(BRAIN, "journal", today[:4], f"{today}.md")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        with open(path, encoding="utf-8") as fh:
            current = fh.read()
    except OSError:
        current = (f"---\nsummary: Daily log {today} (written by the stop hook from `Atlas:` lines)\n"
                   f"tags: [log]\n---\n# {today} · Log\n\n")
    stamp = dt.datetime.now().strftime("%H:%M")
    new = [f"- {stamp} · {context} · {f[:300]}" for f in found if f[:300] not in current]
    if new:
        body = current.rstrip("\n")
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(body + ("\n\n" if body.endswith("· Log") else "\n") + "\n".join(new) + "\n")


IGNORED_TOOLS = re.compile(r"(?i)todo|plan|wrap_up|toolsearch|askuserquestion|read_widget|mark_chapter")
AG_WORK = re.compile(r"(?i)run_command|write_to_file|replace_file_content|multi_replace|edit_file|create_file|"
                     r"view_file|read_url|search_web|\"tool_?(?:name|call)s?\"")
REMINDER = ("Atlas reminder - work was done without an `Atlas:` line. If something lasting came up (a decision, a "
            "gotcha, how to build or test, a fact about the user's life), file it where it belongs now; then end "
            "with one line `Atlas: <what changed where>` or `Atlas: nothing new`. Do not repeat the rest of the answer.")
REMINDER_SIGNAL = ("Atlas reminder - the user gave a learning signal ({kind}) this turn and nothing was filed. Keep it "
                   "now (correction: fix the page and record the lesson in howto/; 'from now on': a rule in howto/ or "
                   "brain/settings.md), then end with one line `Atlas: <what changed where>`. Do not repeat the answer.")
LEARN_SUGGEST = (" This chat has run {n} turns without filing anything: suggest /atlas-learn to the user once.")


def _claude_turn_did_work(transcript_path):
    rows = []
    with open(transcript_path, encoding="utf-8") as fh:
        for line in fh:
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    ids = [r.get("promptId") for r in rows if r.get("type") == "user" and r.get("promptId")]
    start = 0
    if ids:
        start = next(i for i, r in enumerate(rows) if r.get("type") == "user" and r.get("promptId") == ids[-1])
    for r in rows[start:]:
        if r.get("type") != "assistant" or r.get("isSidechain"):
            continue
        content = (r.get("message") or {}).get("content")
        for b in content if isinstance(content, list) else []:
            if b.get("type") == "tool_use" and not IGNORED_TOOLS.search(b.get("name") or ""):
                return True
    return False


def _decide(session, marker_seen, did_work):
    """Reminder text or None. Settings `reminder`: signals (default) | every-turn | rare."""
    s = settings()
    st = _state()
    sess = _session(st, session)
    sess["turns"] = sess.get("turns", 0) + 1
    signal = sess.pop("signal", "")
    if marker_seen:
        sess["last_marker"], sess["since_marker"] = _now_iso(), 0
        _save_state(st)
        return None
    sess["since_marker"] = sess.get("since_marker", 0) + 1
    mode = s.get("reminder", "signals")
    reason = None
    if signal and mode in ("signals", "every-turn", "rare"):
        reason = REMINDER_SIGNAL.format(kind=signal)
    elif did_work and mode == "every-turn":
        reason = REMINDER
    elif did_work and mode == "signals":
        last = max(sess.get("last_marker", ""), sess.get("last_nag", ""))
        if not last or (_age_days(last) or 0) * 1440 >= NAG_MINUTES:
            reason = REMINDER
    if reason and sess.get("since_marker", 0) >= LEARN_SUGGEST_TURNS and not sess.get("learn_suggested"):
        sess["learn_suggested"] = True
        reason += LEARN_SUGGEST.format(n=sess["since_marker"])
    if reason:
        sess["last_nag"] = _now_iso()
    _save_state(st)
    return reason


def stop(agent, data):
    if PROBE:
        return
    st = _state()
    s = settings()
    if agent == "claude":
        session = str(data.get("session_id") or "")
        last = data.get("last_assistant_message") or ""
    else:
        session = str(data.get("conversationId") or "")
        last = ""
        tp, user = data.get("transcriptPath"), (data.get("lastUserInput") or "")
        if tp and os.path.isfile(tp):
            with open(tp, "rb") as fh:
                fh.seek(max(0, os.path.getsize(tp) - 400_000))
                tail = fh.read().decode("utf-8", errors="replace")
            anchor = json.dumps(user[:80])[1:-1] if user else ""
            last = (tail[tail.rfind(anchor):] if anchor and anchor in tail else tail[-60_000:]).replace("\\n", "\n")
    context = st.get("sessions", {}).get(session, {}).get("project") or "—"
    if s.get("daily_log", "on") != "off":
        try:
            daily_log(last, context)
        except Exception:
            pass
    try:
        render_calendar_html()
    except Exception:
        pass
    try:
        autocommit_and_push()
    except Exception:
        pass
    marker = bool(MARKER.search(last))
    if agent == "claude":
        if data.get("stop_hook_active"):
            return
        tp = data.get("transcript_path")
        did_work = bool(tp and os.path.isfile(tp) and _claude_turn_did_work(tp))
        reason = _decide(session, marker, did_work)
        if reason:
            emit_continue(agent, reason)
        return
    if data.get("terminationReason") not in (None, "model_stop") or data.get("fullyIdle") is False:
        return
    # Antigravity has no stop_hook_active: one reminder per user message at most.
    key = hashlib.sha256((session + (data.get("lastUserInput") or "")).encode("utf-8")).hexdigest()[:16]
    if _state().get("antigravity_last_nudge") == key:
        return
    reason = _decide(session, marker, bool(AG_WORK.search(last)))
    if reason:
        st = _state()
        st["antigravity_last_nudge"] = key
        _save_state(st)
        emit_continue(agent, reason)


# ---------------------------------------------------------------- main

def main():
    agent = sys.argv[1] if len(sys.argv) > 1 else ""
    event = sys.argv[2] if len(sys.argv) > 2 else ""
    if agent not in ("claude", "antigravity") or event not in ("session", "prompt", "stop"):
        return
    raw = sys.stdin.buffer.read().decode("utf-8", errors="replace") if not sys.stdin.isatty() else ""
    data = json.loads(raw or "{}")
    if agent == "antigravity":
        paths = data.get("workspacePaths") or []
        cwd, session = (paths[0] if paths else os.getcwd()), str(data.get("conversationId") or "")
    else:
        cwd, session = data.get("cwd") or os.getcwd(), str(data.get("session_id") or "")
    if event == "session":
        emit_context(agent, event, session_text(cwd, session))
    elif event == "prompt":
        if agent == "antigravity":
            if int(data.get("invocationNum") or 0) != 0:
                return
            prompt = data.get("lastUserInput") or ""
        else:
            prompt = data.get("prompt") or ""
        if AUTOMATED.search(prompt):
            return
        emit_context(agent, event, prompt_text(prompt, cwd, session))
    else:
        stop(agent, data)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
