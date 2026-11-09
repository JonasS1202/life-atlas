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
CANONICAL = ("areas/", "projects/", "people/", "howto/", "self/", "calendar/", "coach/", "glossary.md", "INDEX.md")
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
    return 1 if rel.startswith(("people/", "self/", "calendar/", "coach/")) else 2


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

_KIND_ALIASES = {
    "workout": ("workout", ""), "training": ("workout", ""), "cycling": ("workout", "cycling"),
    "ride": ("workout", "cycling"), "rad": ("workout", "cycling"), "rennrad": ("workout", "cycling"),
    "running": ("workout", "running"), "run": ("workout", "running"), "lauf": ("workout", "running"),
    "strength": ("workout", "strength"), "kraft": ("workout", "strength"), "hypertrophy": ("workout", "strength"),
    "hypertrophie": ("workout", "strength"), "gym": ("workout", "strength"), "swim": ("workout", "swim"),
    "goal": ("goal", ""), "race": ("goal", "A"), "wettkampf": ("goal", "A"), "ziel": ("goal", ""),
    "blocked": ("blocked", ""), "blocker": ("blocked", ""), "urlaub": ("blocked", "vacation"),
    "vacation": ("blocked", "vacation"), "reise": ("blocked", "travel"), "trip": ("blocked", "travel"),
    "busy": ("blocked", ""), "abwesend": ("blocked", ""),
    "birthday": ("birthday", ""), "geburtstag": ("birthday", ""), "bday": ("birthday", ""),
    "anniversary": ("birthday", "anniversary"), "jahrestag": ("birthday", "anniversary"),
    "todo": ("todo", ""), "task": ("todo", ""), "aufgabe": ("todo", ""),
    "deadline": ("deadline", ""), "frist": ("deadline", ""),
    "event": ("event", ""), "termin": ("event", ""), "meeting": ("event", ""),
}
_CAL_STOP = {"und", "oder", "fuer", "für", "der", "die", "das", "mit", "bei", "von", "auf", "des", "dem", "den",
             "ein", "eine", "monat", "monats", "jahr", "tag", "tage", "ab", "bis", "inkl", "bzw", "sowie", "pro",
             "jonas", "lisa", "uhr", "euro", "soll", "eingang"}


def _cal_tokens(text):
    words = re.findall(r"[a-z0-9äöüß]{3,}", text.lower())
    return {w for w in words if w not in _CAL_STOP}


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
        raw_k = km.group(1).lower()
        raw_sub = (km.group(2) or "").strip()
        mapped_k, mapped_sub = _KIND_ALIASES.get(raw_k, (raw_k, ""))
        kind = mapped_k
        subkind = raw_sub or mapped_sub
        rest = km.group(3).strip()
    elif re.search(r"\b(geburtstag|birthday)\b", rest, re.I):
        kind = "birthday"
    elif re.search(r"\b(jahrestag|anniversary|hochzeitstag)\b", rest, re.I):
        kind, subkind = "birthday", "anniversary"
    elif d2_str and re.search(r"\b(urlaub|dienstreise|trainingslager|reise|blocked)\b", rest, re.I):
        kind = "blocked"
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


def _is_dup_cal_item(candidate, existing_items):
    """Return True if candidate on the same date range is already represented (e.g. folder README + detail page)."""
    c_start, c_end = candidate["start"], candidate["end"]
    c_what = candidate["what"].lower().strip()
    c_tokens = _cal_tokens(c_what)
    c_area = candidate["rel"].split("/")[:2]
    for ex in existing_items:
        if ex["start"] != c_start or ex["end"] != c_end:
            continue
        if ex["what"].lower().strip() == c_what:
            return True
        ex_tokens = _cal_tokens(ex["what"])
        if not c_tokens or not ex_tokens:
            continue
        overlap = len(c_tokens & ex_tokens)
        min_len = max(min(len(c_tokens), len(ex_tokens)), 1)
        same_area = (ex["rel"].split("/")[:2] == c_area) or ex["rel"].startswith("calendar/")
        if (same_area and overlap >= 2 and overlap / min_len >= 0.35) or (overlap >= 3 and overlap / min_len >= 0.5):
            return True
    return False


def calendar_items(idx, days=30, past_days=3650, include_done=False):
    today = dt.date.today()
    items = []
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
                if -past_days <= item["delta"] <= days and not _is_dup_cal_item(item, items):
                    items.append(item)
    # 2. Decentralized `due:` frontmatter items across all brain pages (specific pages before folder READMEs)
    ordered_idx = sorted(
        idx.items(),
        key=lambda kv: (1 if kv[0].endswith("/README.md") and not kv[0].startswith("self/") else 0, kv[0]),
    )
    for rel, e in ordered_idx:
        for raw in e.get("due", []):
            item = _parse_cal_line(raw, rel, today, default_kind="deadline")
            if not item:
                continue
            if not include_done and item["status"] != "open":
                continue
            if -past_days <= item["delta"] <= days and not _is_dup_cal_item(item, items):
                items.append(item)
    return sorted(items, key=lambda x: (x["delta"], x["start"], x["time"], x["what"]))


def _clean_md_inline(text):
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = text.replace("**", "").replace("`", "").strip()
    return re.sub(r"\s*:\s*$", "", text).strip()


def open_todos(include_done=False):
    out = []
    prio_rank = {"high": 0, "med": 1, "low": 2}

    def _add_todo(status, prio, what, rel):
        what_clean = _clean_md_inline(what)
        if len(what_clean) < 4:
            return
        t_new = _cal_tokens(what_clean)
        for ex in out:
            if ex["what"].lower() == what_clean.lower():
                return
            t_ex = _cal_tokens(ex["what"])
            if t_new and t_ex and len(t_new & t_ex) / max(min(len(t_new), len(t_ex)), 1) >= 0.6:
                return
        out.append({"status": status, "prio": prio, "what": what_clean, "rel": rel, "_r": prio_rank.get(prio, 1)})

    cal_readme = os.path.join(BRAIN, "calendar", "README.md")
    if os.path.isfile(cal_readme):
        try:
            with open(cal_readme, encoding="utf-8", errors="replace") as fh:
                lines = fh.read().splitlines()
        except OSError:
            lines = []
        in_todos = False
        for line in lines:
            if line.startswith("## "):
                in_todos = any(w in line.lower() for w in ("to-do", "todo", "aufgaben", "backlog"))
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
            _add_todo(status, prio, m.group(3).strip(), "calendar/README.md")

    # Also harvest open tasks from checklist/todo pages in brain/ so nothing is missed
    for sub in ("areas", "projects", "self"):
        sdir = os.path.join(BRAIN, sub)
        if not os.path.isdir(sdir):
            continue
        for dp, dns, fns in os.walk(sdir):
            dns[:] = sorted(d for d in dns if not d.startswith(".") and d not in SKIP_DIRS)
            for fn in sorted(fns):
                if not fn.lower().endswith(".md"):
                    continue
                full = os.path.join(dp, fn)
                rel = os.path.relpath(full, BRAIN).replace("\\", "/")
                is_checklist_file = any(w in fn.lower() for w in ("todo", "checkliste", "aufgaben"))
                try:
                    with open(full, encoding="utf-8", errors="replace") as fh:
                        lines = fh.read(HEAD_BYTES).splitlines()
                except OSError:
                    continue
                in_open_sec = False
                for line in lines:
                    if line.startswith("#"):
                        low_h = line.lower()
                        in_open_sec = any(w in low_h for w in ("offene punkte", "offene to-dos", "open to-dos",
                                                               "noch offen", "checkliste", "backlog"))
                        if "erledigt" in low_h and "offen" not in low_h:
                            in_open_sec = False
                        continue
                    if DATE_ITEM.match(line):
                        continue
                    tm = TODO_ITEM.match(line)
                    if tm:
                        box = tm.group(1).lower()
                        status = "done" if box == "x" else ("skipped" if box == "-" else "open")
                        if not include_done and status != "open":
                            continue
                        raw_w = tm.group(3).strip()
                        if raw_w.startswith("(") or len(raw_w) < 4:
                            continue
                        _add_todo(status, (tm.group(2) or "med").lower(), raw_w, rel)
                        continue
                    if is_checklist_file and in_open_sec:
                        nm = re.match(r"^\d+\.\s+\*\*(.+?)\*\*(.*)$", line.strip())
                        if nm:
                            headline = (nm.group(1) + " " + nm.group(2)).strip()
                            if re.search(r"\b(erledigt|abgeschlossen|done)\b", headline, re.I):
                                continue
                            prio = "high" if re.search(r"\b(phase 5|nachziehen|nachtragen|frist)\b", headline, re.I) else "med"
                            _add_todo("open", prio, headline, rel)
    return sorted(out, key=lambda x: (x["_r"], x["what"]))


def _zone_class(p):
    """Map decimal power factor (e.g. 0.70, 1.02) to Z1..Z6."""
    if p < 0.56:
        return "z1"
    if p <= 0.75:
        return "z2"
    if p <= 0.90:
        return "z3"
    if p <= 1.05:
        return "z4"
    if p <= 1.20:
        return "z5"
    return "z6"


def _norm_pwr(raw_val, default=0.70):
    """Parse percentage ('75%', '102') or decimal ('0.75', '1.02') into a float factor (e.g. 0.75)."""
    if raw_val is None or raw_val == "":
        return default
    try:
        v = float(str(raw_val).replace(",", ".").replace("%", "").strip())
    except ValueError:
        return default
    return round(v / 100.0, 3) if v > 3.0 else round(v, 3)


def _dur_sec(val_str, unit_str="m"):
    try:
        v = float(str(val_str).replace(",", ".").strip())
    except ValueError:
        return 0
    u = (unit_str or "m").lower()
    if u.startswith("h"):
        return int(round(v * 3600))
    if u.startswith("s"):
        return int(round(v))
    return int(round(v * 60))


def parse_workout_spec(what_text, subkind="cycling", ftp_watts=275, sport=None):
    """Deterministically parse a workout description into structured segments and visual blocks (0 LLM tokens).

    Supports both full DSL specs in parentheses:
      `Threshold 4x8m (90m | 15m WU 55-75%, 4x 8m@102% / 4m@55%, 19m Z2@68%, 10m CD 65-50%)`
    and shorthand descriptions:
      `90m SweetSpot (2x20m)` or `120m Zone 2 @ 70%`
    """
    if sport:
        subkind = sport
    raw = str(what_text or "").strip()
    clean_raw = re.sub(r"\s*\[(?:strava|feedback):[^\]]+\].*$", "", raw, flags=re.I)
    clean_raw = re.sub(r"\s*[—–-]\s*Done:.*$", "", clean_raw, flags=re.I).strip()

    # Extract total duration if stated at start or inside parens
    total_min = None
    m_tot = re.search(r"\b(\d+)\s*(?:m|min)\b", clean_raw, re.I)
    if m_tot:
        total_min = int(m_tot.group(1))
    else:
        m_hr = re.search(r"\b(\d+(?:[.,]\d+)?)\s*h\b", clean_raw, re.I)
        if m_hr:
            total_min = int(round(float(m_hr.group(1).replace(",", ".")) * 60))

    title = clean_raw
    spec_body = ""
    # Look for `( ... | ... )` first
    m_pipe = re.search(r"\(([^()]*\|[^()]*)\)", clean_raw)
    if m_pipe:
        title = (clean_raw[:m_pipe.start()] + clean_raw[m_pipe.end():]).strip()
        parts = m_pipe.group(1).split("|", 1)
        m_pipe_dur = re.search(r"(\d+)\s*(?:m|min)\b", parts[0], re.I)
        if m_pipe_dur:
            total_min = int(m_pipe_dur.group(1))
        spec_body = parts[1].strip()
    else:
        # Check if there is a top-level pipe `Title | 15m WU ...`
        if "|" in clean_raw:
            left, right = clean_raw.split("|", 1)
            if re.search(r"\d+\s*(?:m|min|s|x)\b", right, re.I):
                title = left.strip(" ()")
                spec_body = right.strip(" ()")

    segments = []
    if spec_body:
        tokens = [t.strip() for t in re.split(r"[,;]+", spec_body) if t.strip()]
        for tok in tokens:
            # 1. Warmup: `15m WU 55-75%` or `10m warmup 0.50->0.75`
            m_wu = re.match(
                r"^(\d+(?:[.,]\d+)?)\s*(m|min|s|h)?\s*(?:wu|warm-?up)\s*(?:@\s*)?(\d+(?:[.,]\d+)?)?\s*%?\s*(?:[-–>to/]+\s*(\d+(?:[.,]\d+)?)\s*%?)?$",
                tok, re.I
            )
            if m_wu:
                sec = _dur_sec(m_wu.group(1), m_wu.group(2) or "m")
                p_low = _norm_pwr(m_wu.group(3), 0.50)
                p_high = _norm_pwr(m_wu.group(4), 0.75)
                segments.append({"type": "warmup", "sec": sec, "p_low": p_low, "p_high": p_high,
                                 "label": f"{sec // 60}m WU {int(round(p_low * 100))}–{int(round(p_high * 100))}%"})
                continue

            # 2. Cooldown: `10m CD 65-50%`
            m_cd = re.match(
                r"^(\d+(?:[.,]\d+)?)\s*(m|min|s|h)?\s*(?:cd|cool-?down)\s*(?:@\s*)?(\d+(?:[.,]\d+)?)?\s*%?\s*(?:[-–>to/]+\s*(\d+(?:[.,]\d+)?)\s*%?)?$",
                tok, re.I
            )
            if m_cd:
                sec = _dur_sec(m_cd.group(1), m_cd.group(2) or "m")
                p_low = _norm_pwr(m_cd.group(3), 0.65)
                p_high = _norm_pwr(m_cd.group(4), 0.50)
                segments.append({"type": "cooldown", "sec": sec, "p_low": p_low, "p_high": p_high,
                                 "label": f"{sec // 60}m CD {int(round(p_low * 100))}–{int(round(p_high * 100))}%"})
                continue

            # 3. Intervals: `4x 8m@102% / 4m@55%` or `3x15m @ 90% (5m @ 55%)` or `5x4m@112%`
            m_iv = re.match(
                r"^(\d+)\s*[x×]\s*(\d+(?:[.,]\d+)?)\s*(m|min|s)?\s*(?:@\s*(\d+(?:[.,]\d+)?)\s*%?)?"
                r"(?:\s*(?:/|\(|with|rest|pause)\s*(\d+(?:[.,]\d+)?)\s*(m|min|s)?\s*(?:@\s*(\d+(?:[.,]\d+)?)\s*%?)?\s*\)?)?$",
                tok, re.I
            )
            if m_iv:
                rep = int(m_iv.group(1))
                on_sec = _dur_sec(m_iv.group(2), m_iv.group(3) or "m")
                on_p = _norm_pwr(m_iv.group(4), 0.95 if on_sec >= 480 else 1.10)
                off_sec = _dur_sec(m_iv.group(5), m_iv.group(6) or "m") if m_iv.group(5) else max(60, int(round(on_sec * 0.5)))
                off_p = _norm_pwr(m_iv.group(7), 0.55)
                segments.append({
                    "type": "intervals", "repeat": rep, "on_sec": on_sec, "off_sec": off_sec,
                    "on_p": on_p, "off_p": off_p,
                    "label": f"{rep}× {on_sec // 60 if on_sec >= 60 else str(on_sec) + 's'}"
                             f"{'m' if on_sec >= 60 else ''} @ {int(round(on_p * 100))}% / "
                             f"{off_sec // 60 if off_sec >= 60 else str(off_sec) + 's'}"
                             f"{'m' if off_sec >= 60 else ''} @ {int(round(off_p * 100))}%"
                })
                continue

            # 4. Ramp: `12m ramp 70-95%`
            m_rmp = re.match(
                r"^(\d+(?:[.,]\d+)?)\s*(m|min|s)?\s*ramp\s*(?:@\s*)?(\d+(?:[.,]\d+)?)\s*%?\s*[-–>to/]+\s*(\d+(?:[.,]\d+)?)\s*%?$",
                tok, re.I
            )
            if m_rmp:
                sec = _dur_sec(m_rmp.group(1), m_rmp.group(2) or "m")
                p_low = _norm_pwr(m_rmp.group(3), 0.65)
                p_high = _norm_pwr(m_rmp.group(4), 0.95)
                segments.append({"type": "ramp", "sec": sec, "p_low": p_low, "p_high": p_high,
                                 "label": f"{sec // 60}m Ramp {int(round(p_low * 100))}–{int(round(p_high * 100))}%"})
                continue

            # 5. FreeRide: `15m free`
            m_fr = re.match(r"^(\d+(?:[.,]\d+)?)\s*(m|min|s|h)?\s*(?:free|freeride)$", tok, re.I)
            if m_fr:
                sec = _dur_sec(m_fr.group(1), m_fr.group(2) or "m")
                segments.append({"type": "freeride", "sec": sec, "label": f"{sec // 60}m FreeRide"})
                continue

            # 6. SteadyState: `19m Z2@68%` or `45m @ 70%` or `30m Z2`
            m_st = re.match(
                r"^(\d+(?:[.,]\d+)?)\s*(m|min|s|h)?\s*(?:[A-Za-z0-9_-]+\s*)?(?:@\s*(\d+(?:[.,]\d+)?)\s*%?)?$",
                tok, re.I
            )
            if m_st:
                sec = _dur_sec(m_st.group(1), m_st.group(2) or "m")
                low_tok = tok.lower()
                def_p = 0.52 if "z1" in low_tok else (0.88 if ("z3" in low_tok or "ss" in low_tok or "tempo" in low_tok)
                        else (1.00 if "z4" in low_tok else (1.12 if "z5" in low_tok else 0.68)))
                p = _norm_pwr(m_st.group(3), def_p)
                segments.append({"type": "steady", "sec": sec, "p": p,
                                 "label": f"{sec // 60}m @ {int(round(p * 100))}%"})
                continue

    # Fallback shorthand synthesis for cycling workouts if no pipe DSL was given (e.g. `90m SweetSpot (2x20m)`)
    if not segments and subkind == "cycling" and total_min and total_min >= 20:
        low_c = clean_raw.lower()
        m_sh_iv = re.search(r"\b(\d+)\s*[x×]\s*(\d+)\s*(m|min|s)?(?:\s*@\s*(\d+(?:[.,]\d+)?)\s*%?)?", clean_raw, re.I)
        wu_sec = min(900, max(300, int(total_min * 60 * 0.15)))
        cd_sec = min(600, max(300, int(total_min * 60 * 0.10)))
        segments.append({"type": "warmup", "sec": wu_sec, "p_low": 0.52, "p_high": 0.72,
                         "label": f"{wu_sec // 60}m WU 52–72%"})
        used = wu_sec + cd_sec
        if m_sh_iv:
            rep = int(m_sh_iv.group(1))
            on_sec = _dur_sec(m_sh_iv.group(2), m_sh_iv.group(3) or "m")
            def_iv_p = 0.90 if ("sweet" in low_c or "ss" in low_c or on_sec >= 900) else (1.00 if on_sec >= 420 else 1.12)
            on_p = _norm_pwr(m_sh_iv.group(4), def_iv_p)
            off_sec = max(60, min(300, int(round(on_sec * 0.4))))
            segments.append({"type": "intervals", "repeat": rep, "on_sec": on_sec, "off_sec": off_sec,
                             "on_p": on_p, "off_p": 0.55,
                             "label": f"{rep}× {on_sec // 60}m @ {int(round(on_p * 100))}% / {off_sec // 60}m @ 55%"})
            used += rep * (on_sec + off_sec)
        rem = max(0, total_min * 60 - used)
        if rem >= 60:
            base_p = 0.55 if ("rec" in low_c or "z1" in low_c or "locker" in low_c) else 0.68
            segments.append({"type": "steady", "sec": rem, "p": base_p,
                             "label": f"{rem // 60}m Z2 @ {int(round(base_p * 100))}%"})
        segments.append({"type": "cooldown", "sec": cd_sec, "p_low": 0.65, "p_high": 0.50,
                         "label": f"{cd_sec // 60}m CD 65–50%"})

    visual_blocks = []
    calc_sec = 0
    for seg in segments:
        st = seg["type"]
        if st in ("warmup", "cooldown", "ramp"):
            calc_sec += seg["sec"]
            mid_p = round((seg["p_low"] + seg["p_high"]) / 2.0, 2)
            visual_blocks.append({"min": round(seg["sec"] / 60.0, 1), "pct": int(round(mid_p * 100)),
                                  "zone": _zone_class(mid_p), "label": seg["label"]})
        elif st == "intervals":
            for _ in range(seg["repeat"]):
                calc_sec += seg["on_sec"] + seg["off_sec"]
                visual_blocks.append({"min": round(seg["on_sec"] / 60.0, 1), "pct": int(round(seg["on_p"] * 100)),
                                      "zone": _zone_class(seg["on_p"]), "label": f"ON {int(round(seg['on_p'] * 100))}%"})
                visual_blocks.append({"min": round(seg["off_sec"] / 60.0, 1), "pct": int(round(seg["off_p"] * 100)),
                                      "zone": _zone_class(seg["off_p"]), "label": f"OFF {int(round(seg['off_p'] * 100))}%"})
        elif st == "freeride":
            calc_sec += seg["sec"]
            visual_blocks.append({"min": round(seg["sec"] / 60.0, 1), "pct": 68, "zone": "z2", "label": seg["label"]})
        else:
            calc_sec += seg["sec"]
            visual_blocks.append({"min": round(seg["sec"] / 60.0, 1), "pct": int(round(seg["p"] * 100)),
                                  "zone": _zone_class(seg["p"]), "label": seg["label"]})

    if calc_sec > 0 and not total_min:
        total_min = int(round(calc_sec / 60.0))

    return {
        "title": title or clean_raw,
        "raw": clean_raw,
        "total_min": total_min or 0,
        "segments": segments,
        "visual_blocks": visual_blocks,
        "has_structure": bool(segments),
        "has_intervals": bool(segments),
    }


def _xml_esc(s):
    return (str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&apos;"))


def compile_zwo_xml(spec_or_title, description="", parsed_spec=None, date_str="", ftp_watts=275, ftp=None, author="Atlas Coach"):
    """Compile a parsed workout spec into valid Zwift/Hammerhead .zwo XML with explicit decimal power factors."""
    if isinstance(spec_or_title, dict):
        parsed_spec = spec_or_title
        title = parsed_spec.get("title") or "Atlas Workout"
        if date_str:
            title = f"{date_str} · {title}"
        description = parsed_spec.get("raw") or title
    else:
        title = str(spec_or_title or "Atlas Workout")
        parsed_spec = parsed_spec or {}
    eff_ftp = ftp if ftp is not None else ftp_watts
    segs = parsed_spec.get("segments") or []
    if not segs:
        return ""
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        "<workout_file>",
        f"  <author>{_xml_esc(author)}</author>",
        f"  <name>{_xml_esc(title)}</name>",
        f"  <description>{_xml_esc(description)} (FTP reference: {eff_ftp}W)</description>",
        "  <sportType>bike</sportType>",
        "  <tags>",
        '    <tag name="AtlasCoach"/>',
        "  </tags>",
        "  <workout>",
    ]
    for s in segs:
        t = s["type"]
        if t == "warmup":
            lines.append(f'    <Warmup Duration="{int(s["sec"])}" PowerLow="{s["p_low"]:.2f}" PowerHigh="{s["p_high"]:.2f}"/>')
        elif t == "cooldown":
            lines.append(f'    <Cooldown Duration="{int(s["sec"])}" PowerLow="{s["p_low"]:.2f}" PowerHigh="{s["p_high"]:.2f}"/>')
        elif t == "ramp":
            lines.append(f'    <Ramp Duration="{int(s["sec"])}" PowerLow="{s["p_low"]:.2f}" PowerHigh="{s["p_high"]:.2f}"/>')
        elif t == "intervals":
            lines.append(
                f'    <IntervalsT Repeat="{int(s["repeat"])}" OnDuration="{int(s["on_sec"])}" '
                f'OffDuration="{int(s["off_sec"])}" OnPower="{s["on_p"]:.2f}" OffPower="{s["off_p"]:.2f}"/>'
            )
        elif t == "freeride":
            lines.append(f'    <FreeRide Duration="{int(s["sec"])}" FlatRoad="1"/>')
        else:
            lines.append(f'    <SteadyState Duration="{int(s["sec"])}" Power="{s["p"]:.2f}"/>')
    lines += ["  </workout>", "</workout_file>\n"]
    return "\n".join(lines)


def compute_goals_hash(cal_items=None):
    """Short deterministic hash of open [goal] and multi-day [blocked] items in the calendar."""
    if cal_items is None:
        try:
            cal_items = calendar_items(load_index(budget=INDEX_BUDGET_S), days=365, past_days=14, include_done=True)
        except Exception:
            cal_items = []
    anchors = []
    for it in cal_items or []:
        if it.get("status") != "open" or it.get("delta", 0) < 0:
            continue
        if it.get("kind") == "goal":
            anchors.append(f"G:{it['start']}:{it.get('subkind','')}:{it['what']}")
        elif it.get("kind") == "blocked" and (it["end"] - it["start"]).days >= 2:
            anchors.append(f"B:{it['start']}..{it['end']}:{it['what']}")
    if not anchors:
        return "none"
    return hashlib.sha1("\n".join(sorted(anchors)).encode("utf-8")).hexdigest()[:8]


def parse_macro_plan(cal_items=None):
    """Parse brain/coach/macro-plan.md and evaluate its freshness (>14d or goal/blocker drift)."""
    today = dt.date.today()
    iso_yr, iso_kw, _ = today.isocalendar()
    macro_path = os.path.join(BRAIN, "coach", "macro-plan.md")
    if cal_items is None:
        try:
            cal_items = calendar_items(load_index(budget=INDEX_BUDGET_S), days=365, past_days=14, include_done=True)
        except Exception:
            cal_items = []
    cur_hash = compute_goals_hash(cal_items)
    res = {
        "exists": os.path.isfile(macro_path),
        "updated": "1970-01-01",
        "age_days": 999,
        "active_phase": "Uninitialized",
        "goals_hash": "none",
        "current_goals_hash": cur_hash,
        "status": "STALE",
        "stale": True,
        "stale_reason": "macro-plan.md not initialized yet",
        "current_week": None,
        "next_week": None,
        "weeks": [],
    }
    if not res["exists"]:
        return res
    try:
        with open(macro_path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return res

    fm = frontmatter(text)
    upd_str = str(fm.get("updated") or "1970-01-01").strip()
    res["updated"] = upd_str
    res["active_phase"] = str(fm.get("active_phase") or "Base").strip()
    res["goals_hash"] = str(fm.get("goals_hash") or "none").strip()
    try:
        upd_d = dt.date.fromisoformat(upd_str[:10])
        res["age_days"] = (today - upd_d).days
    except ValueError:
        res["age_days"] = 999

    weeks = []
    for line in text.splitlines():
        s = line.strip()
        if not s.startswith("|") or s.count("|") < 5:
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        c0 = cells[0].lower()
        if c0 in ("kw", "---") or set(c0) <= {"-", ":"} or "run `/atlas-coach`" in c0:
            continue
        m_kw = re.search(r"\b(\d{1,2})\b", cells[0])
        if not m_kw:
            continue
        kw_num = int(m_kw.group(1))
        row = {
            "kw": kw_num,
            "monday": cells[1] if len(cells) > 1 else "",
            "range": cells[1] if len(cells) > 1 else "",
            "phase": _clean_md_inline(cells[2]) if len(cells) > 2 else "",
            "hours": _clean_md_inline(cells[3]) if len(cells) > 3 else "",
            "cycling": _clean_md_inline(cells[4]) if len(cells) > 4 else "",
            "stimulus": _clean_md_inline(cells[4]) if len(cells) > 4 else "",
            "running_strength": _clean_md_inline(cells[5]) if len(cells) > 5 else "",
            "notes": _clean_md_inline(cells[6] if len(cells) > 6 else (cells[5] if len(cells) > 5 else "")),
        }
        weeks.append(row)
        if kw_num == iso_kw and res["current_week"] is None:
            res["current_week"] = row
        elif kw_num == (iso_kw % 52) + 1 and res["next_week"] is None:
            res["next_week"] = row

    res["weeks"] = weeks
    if res["current_week"] and res["current_week"].get("phase"):
        res["active_phase"] = res["current_week"]["phase"]

    if upd_str == "1970-01-01":
        res["status"] = "STALE"
        res["stale"] = True
        res["stale_reason"] = "Macro plan not updated yet"
    elif res["age_days"] > 14:
        res["status"] = "STALE"
        res["stale"] = True
        res["stale_reason"] = f"Last updated {res['age_days']} days ago (> 14d cadence)"
    elif res["goals_hash"] not in ("none", "", cur_hash) or (cur_hash != "none" and res["goals_hash"] != cur_hash):
        res["status"] = "STALE"
        res["stale"] = True
        res["stale_reason"] = f"Calendar goals or blockers changed (hash {res['goals_hash']} -> {cur_hash})"
    elif not weeks:
        res["status"] = "STALE"
        res["stale"] = True
        res["stale_reason"] = "Macro plan has no weekly periodization rows yet"
    else:
        res["status"] = "OK"
        res["stale"] = False
        res["stale_reason"] = ""
    return res


def load_coach_state():
    """Load Strava connection status, synced activities + feedback, and 3-epoch power/pace curves (0 LLM tokens)."""
    strava_env = os.path.join(BRAIN, "_secrets", "strava.env")
    strava_connected = False
    if os.path.isfile(strava_env):
        try:
            with open(strava_env, encoding="utf-8", errors="replace") as fh:
                env_txt = fh.read()
            strava_connected = bool(
                re.search(r"(?m)^STRAVA_REFRESH_TOKEN\s*=\s*\S+", env_txt)
                or re.search(r"(?m)^STRAVA_ACCESS_TOKEN\s*=\s*\S+", env_txt)
            )
        except OSError:
            pass

    act_json_path = os.path.join(BRAIN, "coach", "activities.json")
    data = {"updated": "", "activities": [], "power_curve": {}, "pace_curve": {}}
    if os.path.isfile(act_json_path):
        try:
            with open(act_json_path, encoding="utf-8", errors="replace") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                data.update(loaded)
            elif isinstance(loaded, list):
                data["activities"] = loaded
        except (OSError, ValueError):
            pass

    today = dt.date.today()
    cutoff_14d = (today - dt.timedelta(days=14)).isoformat()
    pending_feedback = []
    for a in sorted(data.get("activities") or [], key=lambda x: str(x.get("date", "")), reverse=True):
        if not isinstance(a, dict):
            continue
        d_str = str(a.get("date") or "")[:10]
        dur_min = int(a.get("duration_min") or round((a.get("moving_time") or 0) / 60.0))
        fb = str(a.get("feedback") or "").strip()
        if d_str >= cutoff_14d and dur_min >= 15 and (not fb or fb.lower() == "pending"):
            pending_feedback.append({
                "id": a.get("id"),
                "date": d_str,
                "name": a.get("name") or "Activity",
                "sport": a.get("sport") or "cycling",
                "duration_min": dur_min,
                "np_w": a.get("np_w") or a.get("np_watts") or a.get("avg_w") or 0,
                "avg_hr": a.get("avg_hr") or 0,
            })

    return {
        "strava_connected": strava_connected,
        "activities": data.get("activities") or [],
        "pending_feedback": pending_feedback,
        "power_curve": data.get("power_curve") or {},
        "pace_curve": data.get("pace_curve") or {},
    }


def _extract_coach_context(idx, cal_items=None):
    """Extract athlete physiology metrics, training schedule, macro plan, Strava state & power curves (0 LLM tokens)."""
    metrics, macro_goals, schedule = [], [], ""
    seen_m, seen_g = set(), set()
    ftp_watts = 300

    def _add_m(label, val, icon=""):
        val = _clean_md_inline(val)
        if not val or val in ("—", "-") or label in seen_m:
            return
        seen_m.add(label)
        metrics.append({"label": label, "value": val, "icon": icon})

    def _add_g(title, detail="", cat="Macro-Goal", rel=""):
        title = _clean_md_inline(title)
        detail = _clean_md_inline(detail)
        if not title or title.lower() in seen_g:
            return
        seen_g.add(title.lower())
        macro_goals.append({"title": title, "detail": detail, "cat": cat, "rel": rel})

    sport_tags = {"radsport", "sport", "cycling", "running", "gesundheit", "health", "training", "fitness"}
    cand_rels = []
    # Prioritize brain/coach/profile.md first so canonical coach thresholds take precedence
    if os.path.isfile(os.path.join(BRAIN, "coach", "profile.md")):
        cand_rels.append("coach/profile.md")
    for rel, e in sorted(idx.items()):
        if rel in cand_rels:
            continue
        tset = set(e.get("tags", []))
        if rel.startswith(("self/", "coach/")) or (tset & sport_tags) or any(w in rel.lower() for w in ("radsport", "sport", "training", "physiologie")):
            cand_rels.append(rel)

    for rel in cand_rels:
        full = os.path.join(BRAIN, rel)
        try:
            with open(full, encoding="utf-8", errors="replace") as fh:
                text = fh.read(HEAD_BYTES)
        except OSError:
            continue
        for line in text.splitlines():
            s = line.strip()
            if s.startswith("|") and s.count("|") >= 3:
                cells = [c.strip() for c in s.strip("|").split("|")]
                k_low = cells[0].lower()
                val = cells[1] if len(cells) > 1 else ""
                if "ftp" in k_low:
                    short_v = re.sub(r"\s*/\s*~?[\d,.]+\s*W/kg\s+auf.*$", ")", val)
                    _add_m("FTP", short_v, "⚡")
                    m_w = re.search(r"(\d{2,3})\s*W\b", short_v, re.I)
                    if m_w:
                        ftp_watts = int(m_w.group(1))
                elif "vo2max" in k_low:
                    _add_m("VO₂max", val, "🫁")
                elif "hauptsport" in k_low or "volumen" in k_low or "umfang" in k_low or "weekly time budget" in k_low:
                    _add_m("Volumen", val, "🚴")
                elif "all-out" in k_low or "peaks" in k_low or "mmp" in k_low:
                    _add_m("MMP Peaks", val.replace("\\|", "·"), "🔥")
                elif "krafttraining" in k_low or "hypertrophie" in k_low:
                    _add_m("Athletik & Kraft", val, "🏋️")
                elif "körpermaße" in k_low or "biometrie" in k_low or "height & weight" in k_low:
                    _add_m("Biometrie", val, "⚖️")
                elif "max hr" in k_low:
                    _add_m("Herzfrequenz", val, "❤️")
            if not schedule and re.search(r"\d{2}:\d{2}\s*[–-]\s*\d{2}:\d{2}.*(?:Vitalitäts|Rennrad|Workout|Training|Weekday Window)", s, re.I):
                m_sch = re.search(r"(\d{2}:\d{2}\s*[–-]\s*\d{2}:\d{2})", s)
                if m_sch:
                    schedule = f"{m_sch.group(1)} Uhr (Mo–Fr)"
            elif not schedule and "Weekday Window" in s:
                m_sch = re.search(r"(\d{2}:\d{2}\s*[–-]\s*\d{2}:\d{2})", s)
                if m_sch:
                    schedule = f"{m_sch.group(1)} Uhr (Mo–Fr)"
            if "Chronotyp" in s and "Schlaf" in s:
                m_sl = re.search(r"(\d{2}:\d{2}\s*[–-]\s*\d{2}:\d{2})", s)
                if m_sl:
                    _add_m("Schlaf-Fenster", f"{m_sl.group(1)} Uhr", "🌙")
            if any(w in s for w in ("Ausdauer-Motor:", "Oberkörper-Hypertrophie:")):
                parts = s.lstrip("-* ").split(":", 1)
                if len(parts) == 2:
                    _add_g(parts[0], parts[1], "Physiologie-Säule", rel)
            elif "Münsterland Giro" in s or ("Events & Ziele" in text and s.startswith("- **") and "Enge " not in s and "Indoor" not in s):
                _add_g("Ziel-Events & Wettkämpfe", s.lstrip("-* "), "Wettkämpfe", rel)

    macro = parse_macro_plan(cal_items=cal_items)
    c_state = load_coach_state()
    return {
        "metrics": metrics,
        "ftp_watts": ftp_watts,
        "macro_goals": macro_goals,
        "schedule": schedule or "17:00 – 20:00 Uhr",
        "macro": macro,
        "strava_connected": c_state["strava_connected"],
        "pending_feedback": c_state["pending_feedback"],
        "power_curve": c_state["power_curve"],
        "pace_curve": c_state["pace_curve"],
        "activities_count": len(c_state["activities"]),
    }


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
<title>Atlas · Calendar & Coach Hub</title>
<style>
:root{
  --bg:#070a0f;--surface:#0d121b;--surface-2:#121926;--surface-3:#182234;--border:rgba(255,255,255,0.075);
  --border-hi:rgba(255,255,255,0.15);--text:#f1f5f9;--text-dim:#cbd5e1;--muted:#64748b;--muted-hi:#94a3b8;
  --accent:#38bdf8;--accent-glow:rgba(56,189,248,0.16);
  --workout:#10b981;--workout-bg:rgba(16,185,129,0.14);--workout-tx:#a7f3d0;
  --plan:#0ea5e9;--plan-bg:rgba(14,165,233,0.14);--plan-tx:#bae6fd;
  --goal:#f59e0b;--goal-bg:rgba(245,158,11,0.15);--goal-tx:#fde68a;
  --blocked:#f43f5e;--blocked-bg:rgba(244,63,94,0.15);--blocked-tx:#fecdd3;
  --event:#8b5cf6;--event-bg:rgba(139,92,246,0.15);--event-tx:#ddd6fe;
  --deadline:#f97316;--deadline-bg:rgba(249,115,22,0.14);--deadline-tx:#fed7aa;
  --todo:#38bdf8;--todo-bg:rgba(56,189,248,0.14);--todo-tx:#e0f2fe;
  --bday:#ec4899;--bday-bg:rgba(236,72,153,0.15);--bday-tx:#fbcfe8;
  --radius:12px;--radius-sm:8px
}
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%;background:var(--bg);color:var(--text);font-family:Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;font-size:14px;line-height:1.45;-webkit-font-smoothing:antialiased;font-variant-numeric:tabular-nums}
body{background-image:radial-gradient(ellipse 80% 35% at 50% -10%,rgba(56,189,248,0.09),transparent);min-height:100vh}
.shell{max-width:1760px;margin:0 auto;padding:16px 20px 32px;display:flex;flex-direction:column;gap:14px}
@media(max-width:768px){.shell{padding:12px}}

/* Topbar */
.topbar{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:center;gap:12px;padding:12px 16px;background:rgba(13,18,27,0.85);backdrop-filter:blur(12px);border:1px solid var(--border);border-radius:var(--radius);box-shadow:0 8px 30px rgba(0,0,0,0.35)}
.brand{display:flex;align-items:center;gap:11px;min-width:0}
.brand-icon{width:32px;height:32px;border-radius:9px;background:linear-gradient(135deg,#0284c7,#38bdf8);display:flex;align-items:center;justify-content:center;font-size:1rem;box-shadow:0 0 18px rgba(56,189,248,0.35);flex-shrink:0}
.brand-title{font-size:1.05rem;font-weight:700;letter-spacing:-0.02em;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.brand-sub{font-size:.72rem;color:var(--muted-hi);display:flex;align-items:center;gap:6px}
.live-pill{display:inline-flex;align-items:center;gap:5px;font-size:.68rem;font-weight:600;color:#34d399;background:rgba(16,185,129,0.12);border:1px solid rgba(16,185,129,0.28);padding:2px 8px;border-radius:99px}
.live-dot{width:6px;height:6px;border-radius:50%;background:#10b981;box-shadow:0 0 8px #10b981;animation:pulse 2.2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.35}}

.toolbar{display:flex;flex-wrap:wrap;align-items:center;gap:8px}
.seg{display:inline-flex;background:var(--surface-2);padding:3px;border-radius:9px;border:1px solid var(--border);gap:2px}
.seg button{background:transparent;color:var(--muted-hi);border:none;padding:5px 11px;border-radius:6px;cursor:pointer;font-size:.78rem;font-weight:600;display:inline-flex;align-items:center;gap:6px;transition:all .15s ease;white-space:nowrap}
.seg button:hover{color:var(--text);background:rgba(255,255,255,0.04)}
.seg button.active{background:var(--surface-3);color:#fff;box-shadow:0 1px 4px rgba(0,0,0,0.4),inset 0 0 0 1px rgba(56,189,248,0.35)}
.cnt{font-size:.66rem;padding:1px 5px;border-radius:99px;background:rgba(255,255,255,0.07);color:var(--text-dim);font-weight:600}
.seg button.active .cnt{background:rgba(56,189,248,0.22);color:var(--accent)}

.search-wrap{position:relative;display:flex;align-items:center}
.search-in{background:var(--surface-2);color:var(--text);border:1px solid var(--border);border-radius:8px;padding:5px 10px 5px 26px;font-size:.78rem;width:155px;transition:all .15s ease;outline:none}
.search-in:focus{border-color:var(--accent);width:200px;background:var(--surface-3)}
.search-ico{position:absolute;left:8px;font-size:.72rem;color:var(--muted);pointer-events:none}

.nav-grp{display:inline-flex;align-items:center;background:var(--surface-2);border:1px solid var(--border);border-radius:9px;padding:2px}
.nav-btn{background:transparent;color:var(--text-dim);border:none;padding:5px 9px;border-radius:6px;cursor:pointer;font-size:.78rem;font-weight:600;transition:all .15s}
.nav-btn:hover{background:var(--surface-3);color:#fff}
.nav-label{min-width:138px;text-align:center;font-weight:700;font-size:.82rem;color:var(--text);padding:0 8px;cursor:pointer}

/* Telemetry & KPI Strip */
.kpi-strip{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px}
@media(max-width:960px){.kpi-strip{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media(max-width:520px){.kpi-strip{grid-template-columns:1fr}}
.kpi{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:10px 13px;display:flex;flex-direction:column;gap:3px;min-width:0;position:relative;overflow:hidden}
.kpi::before{content:"";position:absolute;top:0;left:0;right:0;height:2px;background:var(--kpi-color,var(--accent));opacity:.75}
.kpi-lbl{font-size:.68rem;text-transform:uppercase;letter-spacing:.05em;color:var(--muted-hi);font-weight:600;display:flex;justify-content:space-between;align-items:center}
.kpi-val{font-size:1.05rem;font-weight:700;color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.kpi-sub{font-size:.72rem;color:var(--muted-hi);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}

/* Athlete Telemetry Ribbon (Coach View) */
.coach-ribbon{background:linear-gradient(180deg,rgba(16,185,129,0.08),rgba(13,18,27,0.9));border:1px solid rgba(16,185,129,0.25);border-radius:var(--radius);padding:11px 14px;display:none;flex-direction:column;gap:9px}
.coach-ribbon.show{display:flex}
.coach-hdr{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px}
.coach-title{font-size:.78rem;font-weight:700;text-transform:uppercase;letter-spacing:.05em;color:#34d399;display:flex;align-items:center;gap:6px}
.phys-chips{display:flex;flex-wrap:wrap;gap:7px}
.phys-chip{background:rgba(15,23,42,0.85);border:1px solid rgba(255,255,255,0.09);border-radius:7px;padding:4px 9px;font-size:.74rem;display:inline-flex;align-items:center;gap:6px}
.phys-chip span.k{color:var(--muted-hi);font-weight:500}
.phys-chip strong.v{color:var(--text);font-weight:700}
.macro-banner{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:8px;padding:7px 10px;border-radius:8px;background:rgba(15,23,42,0.75);border:1px solid rgba(56,189,248,0.2);font-size:.74rem}
.macro-badges{display:flex;flex-wrap:wrap;gap:6px;align-items:center}
.kw-phase{font-size:.54rem;font-weight:700;text-transform:uppercase;padding:1px 4px;border-radius:3px;background:rgba(56,189,248,0.16);color:var(--accent);margin-top:3px;letter-spacing:.03em;max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.wko-bars{display:flex;align-items:flex-end;gap:1.5px;height:12px;margin-top:3px;padding:1px 2px;background:rgba(0,0,0,0.25);border-radius:3px;overflow:hidden}
.wko-bar{min-width:2px;border-radius:1px 1px 0 0;opacity:.92}
.fb-badge{font-size:.58rem;font-weight:700;padding:0 4px;border-radius:3px;margin-left:auto;flex-shrink:0}
.fb-badge.ok{background:rgba(16,185,129,0.2);color:#6ee7b7}
.fb-badge.warn{background:rgba(245,158,11,0.22);color:#fde68a}
.pcurve-tbl{width:100%;border-collapse:collapse;font-size:.71rem;margin-top:4px}
.pcurve-tbl th,.pcurve-tbl td{padding:4px 6px;text-align:right;border-bottom:1px solid var(--border)}
.pcurve-tbl th:first-child,.pcurve-tbl td:first-child{text-align:left;font-weight:700;color:var(--muted-hi)}
.pcurve-tbl th{font-size:.64rem;text-transform:uppercase;color:var(--muted)}

/* Main Workspace Layout */
.workspace{display:grid;grid-template-columns:minmax(0,1fr) 360px;gap:14px;align-items:start}
@media(max-width:1120px){.workspace{grid-template-columns:minmax(0,1fr)}}

/* Calendar Grid Container */
.cal-box{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);overflow:hidden;box-shadow:0 10px 30px rgba(0,0,0,0.3);min-width:0}

/* Month View Grid — STRICT minmax(0,1fr) prevents column blowouts! */
.dow-row{display:grid;grid-template-columns:32px repeat(7,minmax(0,1fr));background:var(--surface-2);border-bottom:1px solid var(--border)}
.dow-cell{padding:8px 4px;text-align:center;font-size:.7rem;font-weight:700;text-transform:uppercase;letter-spacing:.06em;color:var(--muted-hi)}
.dow-cell.wknd{color:var(--muted)}
.dow-cell.kw-hdr{font-size:.62rem;color:var(--muted);border-right:1px solid var(--border)}

.month-grid{display:grid;grid-template-columns:32px repeat(7,minmax(0,1fr));grid-auto-rows:minmax(112px,auto)}
@media(max-width:680px){
  .dow-row,.month-grid{grid-template-columns:24px repeat(7,minmax(0,1fr))}
  .month-grid{grid-auto-rows:minmax(92px,auto)}
}
.kw-cell{border-right:1px solid var(--border);border-bottom:1px solid var(--border);background:rgba(15,21,32,0.6);display:flex;flex-direction:column;align-items:center;justify-content:flex-start;padding:7px 2px 4px;font-size:.66rem;font-weight:600;color:var(--muted);cursor:pointer;user-select:none;transition:color .15s,background .15s}
.kw-cell:hover{color:var(--accent);background:rgba(56,189,248,0.07)}

.day{min-width:0;overflow:hidden;border-right:1px solid var(--border);border-bottom:1px solid var(--border);padding:6px;display:flex;flex-direction:column;gap:4px;position:relative;background:var(--surface);transition:background .12s ease;cursor:pointer}
.day:nth-child(8n){border-right:none}
.day.wknd{background:rgba(255,255,255,0.01)}
.day.other{background:rgba(7,10,15,0.65)}
.day.other .dnum-txt,.day.other .pill{opacity:.42}
.day:hover{background:var(--surface-2)}
.day.today{background:linear-gradient(180deg,rgba(56,189,248,0.09),rgba(13,18,27,0.95))}
.day.today::before{content:"";position:absolute;top:0;left:0;right:0;height:2px;background:var(--accent);box-shadow:0 0 10px var(--accent)}

.dhead{display:flex;justify-content:space-between;align-items:center;margin-bottom:2px;min-width:0}
.dnum-txt{font-size:.76rem;font-weight:600;color:var(--muted-hi);display:inline-flex;align-items:center;gap:4px;line-height:1}
.day.today .dnum-txt{background:var(--accent);color:#041019;font-weight:800;padding:3px 7px;border-radius:99px;box-shadow:0 0 12px rgba(56,189,248,0.45)}
.today-tag{font-size:.58rem;font-weight:800;text-transform:uppercase;letter-spacing:.05em;color:var(--accent)}
.dcount{font-size:.62rem;color:var(--muted);font-weight:600}

/* Event Pills inside Day Cells */
.pill{min-width:0;max-width:100%;overflow:hidden;padding:3.5px 6px;border-radius:5px;border-left:3px solid var(--accent);background:var(--surface-3);color:var(--text);font-size:.71rem;line-height:1.25;display:flex;flex-direction:column;gap:1px;cursor:pointer;transition:transform .1s ease,filter .12s ease}
.pill:hover{filter:brightness(1.15);transform:translateY(-1px)}
.pill-top{display:flex;align-items:center;gap:4px;min-width:0}
.pill-ico{font-size:.68rem;flex-shrink:0;line-height:1}
.pill-time{font-size:.63rem;font-weight:700;opacity:.85;flex-shrink:0}
.pill-txt{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;word-break:break-word;white-space:normal;font-weight:500;min-width:0;flex:1}
.pill-meta{font-size:.62rem;opacity:.75;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.more-pill{font-size:.66rem;font-weight:600;color:var(--accent);padding:2px 5px;border-radius:4px;background:rgba(56,189,248,0.08);text-align:center}

.pill.workout-done{border-left-color:var(--workout);background:var(--workout-bg);color:var(--workout-tx)}
.pill.workout-open{border-left-color:var(--plan);background:var(--plan-bg);color:var(--plan-tx)}
.pill.workout-skipped{border-left-color:#64748b;background:rgba(100,116,139,0.12);color:#94a3b8;text-decoration:line-through}
.pill.goal{border-left-color:var(--goal);background:var(--goal-bg);color:var(--goal-tx);font-weight:600}
.pill.blocked{border-left-color:var(--blocked);background:var(--blocked-bg);color:var(--blocked-tx)}
.pill.event{border-left-color:var(--event);background:var(--event-bg);color:var(--event-tx)}
.pill.deadline{border-left-color:var(--deadline);background:var(--deadline-bg);color:var(--deadline-tx)}
.pill.todo{border-left-color:var(--todo);background:var(--todo-bg);color:var(--todo-tx)}
.pill.birthday{border-left-color:var(--bday);background:var(--bday-bg);color:var(--bday-tx)}

/* Week View */
.week-list{display:flex;flex-direction:column;divide-y:1px solid var(--border)}
.wday-row{display:grid;grid-template-columns:130px minmax(0,1fr);border-bottom:1px solid var(--border);min-height:76px;transition:background .12s}
.wday-row:last-child{border-bottom:none}
.wday-row:hover{background:rgba(255,255,255,0.015)}
.wday-row.today{background:linear-gradient(90deg,rgba(56,189,248,0.09),transparent)}
@media(max-width:600px){.wday-row{grid-template-columns:92px minmax(0,1fr)}}
.wday-left{padding:12px;border-right:1px solid var(--border);display:flex;flex-direction:column;gap:2px}
.wday-dow{font-size:.72rem;font-weight:700;text-transform:uppercase;color:var(--muted-hi)}
.wday-date{font-size:1.05rem;font-weight:800;color:var(--text)}
.wday-row.today .wday-date{color:var(--accent)}
.wday-right{padding:10px 12px;display:flex;flex-direction:column;gap:7px;justify-content:center;min-width:0}
.wday-empty{font-size:.76rem;color:var(--muted);font-style:italic}

/* Agenda View */
.agenda-view{padding:14px;display:flex;flex-direction:column;gap:16px}
.ag-group{display:flex;flex-direction:column;gap:7px}
.ag-month-hdr{font-size:.76rem;font-weight:700;text-transform:uppercase;letter-spacing:.06em;color:var(--accent);padding-bottom:4px;border-bottom:1px solid var(--border);display:flex;justify-content:space-between}
.ag-item{display:grid;grid-template-columns:105px minmax(0,1fr) auto;gap:12px;align-items:center;padding:10px 12px;background:var(--surface-2);border:1px solid var(--border);border-radius:9px;cursor:pointer;transition:border-color .15s,background .15s}
.ag-item:hover{border-color:var(--border-hi);background:var(--surface-3)}
@media(max-width:600px){.ag-item{grid-template-columns:82px minmax(0,1fr)}}
.ag-date-col{display:flex;flex-direction:column}
.ag-date-main{font-size:.8rem;font-weight:700;color:var(--text)}
.ag-date-sub{font-size:.68rem;color:var(--muted-hi)}

/* Sidebar */
.side{display:flex;flex-direction:column;gap:12px;min-width:0}
.card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:14px;display:flex;flex-direction:column;gap:10px;box-shadow:0 6px 20px rgba(0,0,0,0.25);min-width:0}
.card-hdr{display:flex;justify-content:space-between;align-items:center;gap:8px}
.card-title{font-size:.76rem;font-weight:700;text-transform:uppercase;letter-spacing:.05em;color:var(--muted-hi);display:flex;align-items:center;gap:6px}
.card-badge{font-size:.68rem;font-weight:600;padding:1px 7px;border-radius:99px;background:var(--surface-3);color:var(--text-dim)}
.list{display:flex;flex-direction:column;gap:7px;max-height:320px;overflow-y:auto;padding-right:2px}
.list::-webkit-scrollbar{width:5px}
.list::-webkit-scrollbar-thumb{background:var(--surface-3);border-radius:99px}

.row{font-size:.79rem;padding:9px 11px;border-radius:8px;background:var(--surface-2);border:1px solid var(--border);display:flex;justify-content:space-between;gap:10px;align-items:flex-start;cursor:pointer;transition:border-color .15s,background .15s;min-width:0}
.row:hover{border-color:var(--border-hi);background:var(--surface-3)}
.row-main{min-width:0;flex:1;display:flex;flex-direction:column;gap:3px}
.row-title{font-weight:600;color:var(--text);word-break:break-word;line-height:1.3}
.row-sub{font-size:.7rem;color:var(--muted-hi);display:flex;flex-wrap:wrap;gap:6px;align-items:center}
.tag{font-size:.67rem;font-weight:600;padding:2px 7px;border-radius:5px;background:var(--surface-3);color:var(--text-dim);white-space:nowrap;flex-shrink:0}
.tag.high,.tag.overdue{background:rgba(244,63,94,0.18);color:#fda4af;border:1px solid rgba(244,63,94,0.3)}
.tag.med{background:rgba(249,115,22,0.16);color:#fdba74;border:1px solid rgba(249,115,22,0.3)}
.tag.low{background:rgba(56,189,248,0.14);color:#bae6fd}
.tag.goal{background:rgba(245,158,11,0.18);color:#fde68a;border:1px solid rgba(245,158,11,0.3)}
.tag.ok{background:rgba(16,185,129,0.16);color:#6ee7b7;border:1px solid rgba(16,185,129,0.3)}
.empty-box{font-size:.78rem;color:var(--muted-hi);padding:12px;border-radius:8px;background:rgba(255,255,255,0.02);border:1px dashed var(--border);display:flex;flex-direction:column;gap:6px}
.code-hint{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.7rem;background:var(--bg);padding:5px 8px;border-radius:5px;color:var(--accent);border:1px solid var(--border);cursor:pointer}

/* Modal */
.modal-backdrop{position:fixed;inset:0;background:rgba(3,6,12,0.75);backdrop-filter:blur(6px);display:none;align-items:center;justify-content:center;padding:16px;z-index:100}
.modal-backdrop.open{display:flex}
.modal{background:var(--surface);border:1px solid var(--border-hi);border-radius:14px;max-width:540px;width:100%;padding:20px;box-shadow:0 24px 60px rgba(0,0,0,0.65);display:flex;flex-direction:column;gap:14px}
.modal-top{display:flex;justify-content:space-between;align-items:flex-start;gap:12px}
.modal-title{font-size:1.05rem;font-weight:700;color:var(--text);line-height:1.35}
.modal-close{background:var(--surface-2);border:1px solid var(--border);color:var(--muted-hi);width:28px;height:28px;border-radius:7px;cursor:pointer;font-size:.9rem}
.modal-close:hover{color:#fff;background:var(--surface-3)}
.modal-meta{display:flex;flex-wrap:wrap;gap:8px}
.modal-body{font-size:.84rem;color:var(--text-dim);background:var(--surface-2);padding:12px;border-radius:9px;border:1px solid var(--border);display:flex;flex-direction:column;gap:8px}
.modal-actions{display:flex;justify-content:flex-end;gap:8px}
.btn-act{background:var(--surface-3);color:var(--text);border:1px solid var(--border-hi);padding:6px 12px;border-radius:7px;font-size:.76rem;font-weight:600;cursor:pointer}
.btn-act:hover{border-color:var(--accent);color:var(--accent)}
</style></head><body>
<div class="shell">
  <header class="topbar">
    <div class="brand">
      <div class="brand-icon">📅</div>
      <div>
        <div class="brand-title">Atlas · Calendar & Coach Hub</div>
        <div class="brand-sub">
          <span class="live-pill"><span class="live-dot"></span>Atlas-First · Live</span>
          <span id="today-sub"></span>
        </div>
      </div>
    </div>
    <div class="toolbar">
      <div class="seg" role="tablist">
        <button id="f-all" class="active" onclick="setFilter('all')">Alle <span class="cnt" id="cnt-all">0</span></button>
        <button id="f-coach" onclick="setFilter('coach')">🚴 Coach View <span class="cnt" id="cnt-coach">0</span></button>
        <button id="f-todo" onclick="setFilter('todo')">☑️ To-Dos & Fristen <span class="cnt" id="cnt-todo">0</span></button>
      </div>
      <div class="search-wrap">
        <span class="search-ico">🔍</span>
        <input id="q-in" class="search-in" type="search" placeholder="Filtern… (/)" oninput="setQuery(this.value)">
      </div>
      <div class="seg">
        <button id="v-month" class="active" onclick="setLayout('month')">Monat</button>
        <button id="v-week" onclick="setLayout('week')">Woche</button>
        <button id="v-agenda" onclick="setLayout('agenda')">Agenda</button>
      </div>
      <div class="nav-grp">
        <button class="nav-btn" onclick="stepNav(-1)" title="Zurück (◀)">◀</button>
        <span class="nav-label" id="mlabel" onclick="goToday()" title="Springe zu Heute (T)"></span>
        <button class="nav-btn" onclick="stepNav(1)" title="Vor (▶)">▶</button>
        <button class="nav-btn" onclick="goToday()" style="border-left:1px solid var(--border);color:var(--accent)">Heute</button>
      </div>
    </div>
  </header>

  <section id="coach-ribbon" class="coach-ribbon">
    <div class="coach-hdr">
      <div class="coach-title">⚡ Athleten-Telemetrie & Physiologie-Profil (aus Brain)</div>
      <div style="font-size:.72rem;color:var(--muted-hi)" id="coach-sched"></div>
    </div>
    <div class="macro-banner" id="macro-banner"></div>
    <div class="phys-chips" id="phys-chips"></div>
  </section>

  <section class="kpi-strip" id="kpi-strip"></section>

  <div class="workspace">
    <main class="cal-box" id="cal-main"></main>
    <aside class="side">
      <div class="card"><div class="card-hdr"><div class="card-title" id="s1-title"></div><span class="card-badge" id="s1-cnt"></span></div><div class="list" id="s1-list"></div></div>
      <div class="card"><div class="card-hdr"><div class="card-title" id="s2-title"></div><span class="card-badge" id="s2-cnt"></span></div><div class="list" id="s2-list"></div></div>
      <div class="card"><div class="card-hdr"><div class="card-title" id="s3-title"></div><span class="card-badge" id="s3-cnt"></span></div><div class="list" id="s3-list"></div></div>
    </aside>
  </div>
</div>

<div class="modal-backdrop" id="modal-bg" onclick="if(event.target===this)closeModal()">
  <div class="modal">
    <div class="modal-top">
      <div class="modal-title" id="m-title"></div>
      <button class="modal-close" onclick="closeModal()">✕</button>
    </div>
    <div class="modal-meta" id="m-meta"></div>
    <div class="modal-body" id="m-body"></div>
    <div class="modal-actions">
      <button class="btn-act" id="m-copy">📋 Quelle / Markdown kopieren</button>
      <button class="btn-act" onclick="closeModal()">Schließen</button>
    </div>
  </div>
</div>

<script>
let DATA = __DATA_JSON__;
const MONTHS = ['Januar','Februar','März','April','Mai','Juni','Juli','August','September','Oktober','November','Dezember'];
const MONTHS_SHORT = ['Jan','Feb','Mär','Apr','Mai','Jun','Jul','Aug','Sep','Okt','Nov','Dez'];
const DOWS = ['Mo','Di','Mi','Do','Fr','Sa','So'];

let cur = new Date(DATA.today + "T00:00:00");
let filter = sessionStorage.getItem("atlas_cal_filter") || "all";
let layout = sessionStorage.getItem("atlas_cal_layout") || "month";
let query = "";

function iso(d){ return d.getFullYear()+'-'+String(d.getMonth()+1).padStart(2,'0')+'-'+String(d.getDate()).padStart(2,'0'); }
function parseIso(s){ const [y,m,d]=s.split('-').map(Number); return new Date(y,m-1,d); }
function getKW(d){
  const t = new Date(Date.UTC(d.getFullYear(), d.getMonth(), d.getDate()));
  const dayNum = t.getUTCDay() || 7;
  t.setUTCDate(t.getUTCDate() + 4 - dayNum);
  const yearStart = new Date(Date.UTC(t.getUTCFullYear(),0,1));
  return Math.ceil((((t - yearStart) / 86400000) + 1)/7);
}
function fmtDateDE(s){
  const [y,m,d] = s.split('-');
  return `${d}.${m}.${y}`;
}
function esc(s){
  return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
function compactTitle(what){
  let s = String(what||'');
  const mAmt = s.match(/(\\d[\\d.,]*\\s*€(?:\\s*\\/\\s*(?:Monat|M|Jahr|J|Quartal))?)/);
  let main = s.replace(/\\s*\\((?:Eingang|doppeltes|übergesetzliche|30 Tage|seit \\d+|je \\d+).*?\\)/gi, '').trim();
  if(main.length > 68 && main.includes('(')) main = main.split('(')[0].trim();
  return {main: main || s, meta: mAmt ? mAmt[1] : ''};
}
function isCoachItem(it){
  return ['workout','blocked','goal'].includes(it.kind) || (it.kind==='event' && Boolean(it.time));
}
function isTodoItem(it){
  return ['todo','deadline'].includes(it.kind);
}
function keepItem(it){
  if(filter==='coach' && !isCoachItem(it)) return false;
  if(filter==='todo' && !isTodoItem(it)) return false;
  if(query){
    const q = query.toLowerCase();
    const hay = `${it.what} ${it.kind} ${it.subkind||''} ${it.rel||''} ${it.start}`.toLowerCase();
    if(!hay.includes(q)) return false;
  }
  return true;
}
function pillClass(it){
  if(it.kind==='workout') return 'pill workout-' + it.status;
  return 'pill ' + it.kind;
}
function icon(it){
  if(it.kind==='workout') return (it.status==='done'?'✓':it.status==='skipped'?'–':(it.subkind==='strength'?'🏋️':it.subkind==='running'?'🏃':'🚴'));
  if(it.kind==='goal') return '🏆';
  if(it.kind==='blocked') return '🚫';
  if(it.kind==='birthday') return (it.subkind==='anniversary'?'❤️':'🎂');
  if(it.kind==='deadline') return '⏳';
  if(it.kind==='todo') return '☑️';
  return '📅';
}
function deltaLabel(delta){
  if(delta===0) return 'heute';
  if(delta===1) return 'morgen';
  if(delta===-1) return 'gestern';
  return delta < 0 ? `vor ${-delta}d` : `in ${delta}d`;
}
function setFilter(f){
  filter = f;
  sessionStorage.setItem("atlas_cal_filter", f);
  render();
}
function setLayout(l){
  layout = l;
  sessionStorage.setItem("atlas_cal_layout", l);
  render();
}
function setQuery(q){
  query = (q||'').trim();
  render();
}
function stepNav(dir){
  if(layout==='week'){
    cur = new Date(cur.getFullYear(), cur.getMonth(), cur.getDate() + dir*7);
  } else {
    cur = new Date(cur.getFullYear(), cur.getMonth() + dir, 1);
  }
  render();
}
function goToday(){
  cur = new Date(DATA.today + "T00:00:00");
  render();
}
function openWeekOf(dateStr){
  cur = parseIso(dateStr);
  setLayout('week');
}
function zoneColor(z){
  if(z<=1) return '#64748b';
  if(z===2) return '#38bdf8';
  if(z===3) return '#10b981';
  if(z===4) return '#f59e0b';
  if(z===5) return '#f43f5e';
  return '#a855f7';
}
function renderWkoBars(it, tall=false){
  const blks = it.visual_blocks || [];
  if(!blks.length) return '';
  const tot = blks.reduce((s,b)=>s+(b.sec||60), 0) || 1;
  const h = tall ? 28 : 11;
  return `<div class="wko-bars" style="height:${h}px" title="Intervall-Struktur (${it.total_min||0} min)">` +
    blks.map(b => {
      const wPct = Math.max(2, (b.sec/tot)*100);
      const hPct = Math.min(100, Math.max(22, Math.round((b.pct||0.65)*78)));
      return `<div class="wko-bar" style="width:${wPct.toFixed(1)}%;height:${hPct}%;background:${zoneColor(b.zone||2)}" title="${esc(b.label||'Block')}"></div>`;
    }).join('') + `</div>`;
}
function downloadZwo(idx){
  const it = DATA.items[idx];
  if(!it || !it.zwo_xml) return;
  const blob = new Blob([it.zwo_xml], {type: 'application/xml'});
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = it.zwo_filename || `${it.start}_workout.zwo`;
  document.body.appendChild(a);
  a.click();
  setTimeout(()=>{ document.body.removeChild(a); URL.revokeObjectURL(url); }, 150);
}
function kwPhaseMap(){
  const m = (DATA.coach && DATA.coach.macro && DATA.coach.macro.weeks) || [];
  const out = {};
  for(const w of m){ out[String(w.kw).replace(/^0+/,'')] = w; }
  return out;
}
function showItemModal(idx){
  const it = DATA.items[idx];
  if(!it) return;
  const bg = document.getElementById('modal-bg');
  document.getElementById('m-title').textContent = `${icon(it)} ${it.what}`;
  const kw = getKW(parseIso(it.start));
  const kindLbl = it.subkind ? `${it.kind.toUpperCase()} · ${it.subkind.toUpperCase()}` : it.kind.toUpperCase();
  document.getElementById('m-meta').innerHTML = `
    <span class="tag goal">${esc(kindLbl)}</span>
    <span class="tag">${esc(fmtDateDE(it.start))}${it.end!==it.start?' – '+esc(fmtDateDE(it.end)):''} · KW ${kw}</span>
    ${it.time?`<span class="tag">🕒 ${esc(it.time)}</span>`:''}
    <span class="tag ${it.delta<0?'overdue':'ok'}">${esc(deltaLabel(it.delta))} (${(it.delta/7).toFixed(1)}w)</span>
    ${it.np_w?`<span class="tag ok">⚡ NP ${it.np_w} W</span>`:''}
    ${it.avg_hr?`<span class="tag">❤️ ${it.avg_hr} bpm</span>`:''}
  `;
  let extra = '';
  if(it.visual_blocks && it.visual_blocks.length){
    extra += `<div><strong>Intervall-Struktur (${it.total_min||0} min):</strong>${renderWkoBars(it, true)}</div>`;
  }
  if(it.feedback){
    extra += `<div><strong>Athleten-Feedback:</strong><br><span style="color:#6ee7b7">${esc(it.feedback)}</span></div>`;
  } else if(it.feedback_pending){
    extra += `<div style="color:#fde68a"><strong>⚠ Check-in offen:</strong> Noch keine subjektive Notiz (RPE / Beine) hinterlegt.<br><code>atlas.py coach note --date ${esc(it.start)} --text "..."</code></div>`;
  }
  if(it.zwo_xml){
    extra += `<div style="margin-top:4px"><button class="btn-act" style="border-color:var(--workout);color:#6ee7b7" onclick="downloadZwo(${idx})">⬇ .zwo Workout herunterladen (${esc(it.zwo_filename||'workout.zwo')})</button></div>`;
  }
  document.getElementById('m-body').innerHTML = `
    <div><strong>Beschreibung:</strong><br>${esc(it.what)}</div>
    ${extra}
    <div style="color:var(--muted-hi);font-size:.76rem"><strong>Quelle im Brain:</strong> <code>${esc(it.rel)}</code></div>
  `;
  const copyBtn = document.getElementById('m-copy');
  copyBtn.onclick = () => {
    navigator.clipboard.writeText(`${it.start} [${it.kind}] ${it.what} (${it.rel})`);
    copyBtn.textContent = '✓ Kopiert!';
    setTimeout(()=>copyBtn.textContent='📋 Quelle / Markdown kopieren', 1500);
  };
  bg.classList.add('open');
}
function showDayModal(ds){
  const d = parseIso(ds);
  const kw = getKW(d);
  const dayItems = DATA.items.map((it,idx)=>({it,idx})).filter(x => keepItem(x.it) && x.it.start <= ds && ds <= x.it.end);
  const bg = document.getElementById('modal-bg');
  const dowName = DOWS[(d.getDay()+6)%7];
  document.getElementById('m-title').textContent = `${dowName}, ${fmtDateDE(ds)} (KW ${kw})`;
  document.getElementById('m-meta').innerHTML = `<span class="tag">${dayItems.length} Einträge</span>`;
  const defKind = filter==='coach' ? 'workout:cycling' : (filter==='todo' ? 'todo' : 'event');
  const snippet = `- [ ] ${ds} [${defKind}] `;
  document.getElementById('m-body').innerHTML = (dayItems.length ? dayItems.map(({it,idx}) => `
    <div class="row" onclick="showItemModal(${idx})">
      <div class="row-main">
        <div class="row-title">${icon(it)} ${it.time?esc(it.time)+' ':''}${esc(it.what)}</div>
        <div class="row-sub"><span>${esc(it.kind)}${it.subkind?':'+esc(it.subkind):''}</span> · <span>${esc(it.rel)}</span></div>
      </div>
    </div>
  `).join('') : '<div style="color:var(--muted-hi)">Keine Einträge an diesem Tag.</div>') + `
    <div style="margin-top:6px;font-size:.73rem;color:var(--muted-hi)">Markdown-Vorlage für <code>brain/calendar/README.md</code>:</div>
    <div class="code-hint" onclick="navigator.clipboard.writeText('${snippet}')">${esc(snippet)}… (Klicken zum Kopieren)</div>
  `;
  const copyBtn = document.getElementById('m-copy');
  copyBtn.onclick = () => {
    navigator.clipboard.writeText(snippet);
    copyBtn.textContent = '✓ Vorlage kopiert!';
    setTimeout(()=>copyBtn.textContent='📋 Quelle / Markdown kopieren', 1500);
  };
  bg.classList.add('open');
}
function closeModal(){ document.getElementById('modal-bg').classList.remove('open'); }

function renderKPIs(){
  const todayD = parseIso(DATA.today);
  const kw = getKW(todayD);
  const dow = DOWS[(todayD.getDay()+6)%7];
  document.getElementById('today-sub').textContent = `${dow}, ${fmtDateDE(DATA.today)} · KW ${kw}`;

  const coachRib = document.getElementById('coach-ribbon');
  const coach = DATA.coach || {metrics:[], macro_goals:[], schedule:''};
  if(filter==='coach'){
    coachRib.classList.add('show');
    document.getElementById('coach-sched').textContent = coach.schedule ? `🕒 Primäres Trainingsfenster: ${coach.schedule}` : '';
    const mBanner = document.getElementById('macro-banner');
    const mp = coach.macro || {active_phase:'Off-Season', stale:true, stale_reason:'Initialisierung offen', updated:''};
    const fbPend = (coach.pending_feedback||[]).length;
    mBanner.innerHTML = `
      <div><strong>🧭 Makro-Phase:</strong> <span style="color:var(--accent);font-weight:700">${esc(mp.active_phase||'Base')}</span> ${mp.updated?`<span style="color:var(--muted-hi)">(Stand ${esc(mp.updated)})</span>`:''}</div>
      <div class="macro-badges">
        <span class="tag ${mp.stale?'med':'ok'}">${mp.stale?'⚠ Makroplan fällig ('+esc(mp.stale_reason)+')':'✓ Makroplan aktuell'}</span>
        <span class="tag ${coach.strava_connected?'ok':'low'}">${coach.strava_connected?'✓ Strava verbunden':'⚡ Strava via atlas.py coach auth-strava'}</span>
        ${fbPend?`<span class="tag med">💬 ${fbPend} Ride-Check-in(s) offen</span>`:''}
      </div>
    `;
    const chips = document.getElementById('phys-chips');
    chips.innerHTML = (coach.metrics && coach.metrics.length) ? coach.metrics.map(m =>
      `<div class="phys-chip"><span>${esc(m.icon||'⚡')}</span><span class="k">${esc(m.label)}:</span><strong class="v">${esc(m.value)}</strong></div>`
    ).join('') : '<div class="phys-chip"><span class="k">Tipp:</span><strong class="v">Leistungsdaten in coach/profile.md hinterlegen</strong></div>';
  } else {
    coachRib.classList.remove('show');
  }

  const kpiEl = document.getElementById('kpi-strip');
  const openItems = DATA.items.filter(i => i.status==='open');
  if(filter==='coach'){
    const goals = openItems.filter(i => i.kind==='goal' && i.delta>=0);
    const woDone = DATA.items.filter(i => i.kind==='workout' && i.delta>=-14 && i.delta<=14 && i.status==='done').length;
    const woOpen = DATA.items.filter(i => i.kind==='workout' && i.delta>=0 && i.delta<=14 && i.status==='open').length;
    const blockers = openItems.filter(i => i.delta>=0 && i.delta<=28 && (i.kind==='blocked' || (i.kind==='event' && i.time)));
    const nextGoal = goals[0];
    const macroCnt = (coach.macro_goals||[]).length;
    kpiEl.innerHTML = `
      <div class="kpi" style="--kpi-color:var(--goal)">
        <div class="kpi-lbl"><span>🏆 Saisonziele & Rennen</span><span>${goals.length + macroCnt} aktiv</span></div>
        <div class="kpi-val">${nextGoal ? esc(nextGoal.what) : (macroCnt ? esc(coach.macro_goals[0].title) : 'Off-Season / Basis')}</div>
        <div class="kpi-sub">${nextGoal ? `Countdown: in ${nextGoal.delta}d (${(nextGoal.delta/7).toFixed(1)} Wochen)` : `${macroCnt} strategische Säulen im Brain`}</div>
      </div>
      <div class="kpi" style="--kpi-color:var(--workout)">
        <div class="kpi-lbl"><span>🚴 Workouts (±14 Tage)</span><span>Compliance</span></div>
        <div class="kpi-val">${woDone} erledigt · ${woOpen} geplant</div>
        <div class="kpi-sub">${woDone+woOpen>0 ? 'Aus calendar/README.md' : 'Bereit für Wochenplanung (.zwo / Karoo)'}</div>
      </div>
      <div class="kpi" style="--kpi-color:${blockers.length?'var(--blocked)':'var(--workout)'}">
        <div class="kpi-lbl"><span>🛡️ Trainings-Verfügbarkeit</span><span>28 Tage</span></div>
        <div class="kpi-val">${28 - blockers.length} / 28 Tage frei</div>
        <div class="kpi-sub">${blockers.length ? `${blockers.length} Blocker/Termine beachten` : 'Keine Reisen oder Blocker eingetragen'}</div>
      </div>
      <div class="kpi" style="--kpi-color:var(--plan)">
        <div class="kpi-lbl"><span>⏱️ Fokus-Rhythmus</span><span>Chronotyp</span></div>
        <div class="kpi-val">${esc(coach.schedule || '17:00 – 20:00 Uhr')}</div>
        <div class="kpi-sub">8–10h/Woche Ausdauer + 1–3x Kraft</div>
      </div>
    `;
  } else if(filter==='todo'){
    const overdue = openItems.filter(i => isTodoItem(i) && i.delta<0);
    const next30 = openItems.filter(i => isTodoItem(i) && i.delta>=0 && i.delta<=30);
    const next365 = openItems.filter(i => isTodoItem(i) && i.delta>=0 && i.delta<=365);
    const highTodos = DATA.todos.filter(t => t.prio==='high').length;
    kpiEl.innerHTML = `
      <div class="kpi" style="--kpi-color:${overdue.length?'var(--blocked)':'var(--workout)'}">
        <div class="kpi-lbl"><span>⚠️ Überfällig</span><span>Status</span></div>
        <div class="kpi-val">${overdue.length ? `${overdue.length} Frist(en)` : '0 Überfällig'}</div>
        <div class="kpi-sub">${overdue.length ? esc(overdue[0].what) : 'Alle vergangenen Stichtage erledigt'}</div>
      </div>
      <div class="kpi" style="--kpi-color:var(--deadline)">
        <div class="kpi-lbl"><span>⏳ Nächste 30 Tage</span><span>Fristen</span></div>
        <div class="kpi-val">${next30.length} Stichtage</div>
        <div class="kpi-sub">${next30[0] ? `Nächster: in ${next30[0].delta}d (${fmtDateDE(next30[0].start)})` : 'Keine Fristen in 30d'}</div>
      </div>
      <div class="kpi" style="--kpi-color:var(--todo)">
        <div class="kpi-lbl"><span>☑️ Offene To-Dos</span><span>Backlog</span></div>
        <div class="kpi-val">${DATA.todos.length} Aufgaben</div>
        <div class="kpi-sub">${highTodos ? `${highTodos} mit hoher Priorität` : 'Aus calendar/ & Checklisten'}</div>
      </div>
      <div class="kpi" style="--kpi-color:var(--event)">
        <div class="kpi-lbl"><span>📆 Jahres-Radar</span><span>365 Tage</span></div>
        <div class="kpi-val">${next365.length} Meilensteine</div>
        <div class="kpi-sub">Verträge, Steuern, Gehalt & Sparpläne</div>
      </div>
    `;
  } else {
    const next30 = openItems.filter(i => i.delta>=0 && i.delta<=30);
    const nxt = next30[0] || openItems.find(i => i.delta>=0);
    const bdays = openItems.filter(i => i.kind==='birthday' && i.delta>=0);
    kpiEl.innerHTML = `
      <div class="kpi" style="--kpi-color:var(--accent)">
        <div class="kpi-lbl"><span>📍 Heute</span><span>KW ${kw}</span></div>
        <div class="kpi-val">${dow}, ${fmtDateDE(DATA.today)}</div>
        <div class="kpi-sub">${openItems.filter(i=>i.delta===0).length || 'Keine'} Termine heute</div>
      </div>
      <div class="kpi" style="--kpi-color:var(--deadline)">
        <div class="kpi-lbl"><span>📅 Nächste 30 Tage</span><span>Horizont</span></div>
        <div class="kpi-val">${next30.length} Einträge</div>
        <div class="kpi-sub">${nxt ? `Als Nächstes: in ${nxt.delta}d (${fmtDateDE(nxt.start)})` : 'Freier Horizont'}</div>
      </div>
      <div class="kpi" style="--kpi-color:var(--todo)">
        <div class="kpi-lbl"><span>☑️ To-Do Backlog</span><span>Offen</span></div>
        <div class="kpi-val">${DATA.todos.length} Aufgaben</div>
        <div class="kpi-sub">${DATA.todos[0] ? esc(DATA.todos[0].what) : 'Keine offenen Backlog-To-Dos'}</div>
      </div>
      <div class="kpi" style="--kpi-color:var(--bday)">
        <div class="kpi-lbl"><span>🎂 Geburtstage & Ziele</span><span>365 Tage</span></div>
        <div class="kpi-val">${bdays.length} Ehrentage</div>
        <div class="kpi-sub">${bdays[0] ? `${esc(bdays[0].what)} (in ${bdays[0].delta}d)` : 'Alle Jahrestage synchronisiert'}</div>
      </div>
    `;
  }
}

function renderMain(){
  const mainEl = document.getElementById('cal-main');
  const y = cur.getFullYear(), m = cur.getMonth();

  if(layout==='month'){
    document.getElementById('mlabel').textContent = `${MONTHS[m]} ${y}`;
    const first = new Date(y, m, 1);
    const offset = (first.getDay() + 6) % 7;
    const start = new Date(y, m, 1 - offset);
    const sixthWeekStart = new Date(start.getFullYear(), start.getMonth(), start.getDate() + 35);
    const totalCells = (sixthWeekStart.getMonth() !== m) ? 35 : 42;

    let html = `<div class="dow-row"><div class="dow-cell kw-hdr">KW</div>` +
      DOWS.map((d,i)=>`<div class="dow-cell${i>=5?' wknd':''}">${d}</div>`).join('') +
      `</div><div class="month-grid">`;

    const kwMap = kwPhaseMap();
    for(let i=0;i<totalCells;i++){
      const d = new Date(start.getFullYear(), start.getMonth(), start.getDate()+i);
      const ds = iso(d);
      const dowIdx = i % 7;
      if(dowIdx === 0){
        const kw = getKW(d);
        const mw = filter==='coach' ? kwMap[String(kw)] : null;
        const phBadge = mw && mw.phase ? `<span class="kw-phase" title="${esc(mw.focus||mw.phase)}">${esc(mw.phase.slice(0,6))}</span>` : '';
        html += `<div class="kw-cell" onclick="openWeekOf('${ds}')" title="Wochenansicht KW ${kw} öffnen"><span>${kw}</span>${phBadge}</div>`;
      }
      const isOther = d.getMonth() !== m;
      const isToday = ds === DATA.today;
      const isWknd = dowIdx >= 5;
      const dayMatches = [];
      DATA.items.forEach((it, idx) => {
        if(keepItem(it) && it.start <= ds && ds <= it.end) dayMatches.push({it, idx});
      });
      const dLabel = (d.getDate()===1 && !isToday) ? `1. ${MONTHS_SHORT[d.getMonth()]}` : d.getDate();
      const shown = dayMatches.slice(0, 3);
      const more = dayMatches.length - shown.length;

      html += `<div class="day${isOther?' other':''}${isToday?' today':''}${isWknd?' wknd':''}" onclick="if(event.target===this||event.target.classList.contains('dhead')||event.target.classList.contains('dnum-txt'))showDayModal('${ds}')">`;
      html += `<div class="dhead"><span class="dnum-txt">${dLabel}</span>${isToday?'<span class="today-tag">Heute</span>':(dayMatches.length>1?`<span class="dcount">${dayMatches.length}</span>`:'')}</div>`;
      for(const {it, idx} of shown){
        const c = compactTitle(it.what);
        const spanInfo = it.start !== it.end ? ` (${it.start.slice(8)}–${it.end.slice(8)})` : '';
        const fbBadge = it.kind==='workout' && it.status==='done' ? (it.feedback ? `<span class="fb-badge ok" title="${esc(it.feedback)}">✓</span>` : (it.feedback_pending ? `<span class="fb-badge warn" title="Check-in Feedback fehlt">⚠</span>` : '')) : '';
        html += `<div class="${pillClass(it)}" onclick="event.stopPropagation();showItemModal(${idx})" title="${esc(it.what)} (${esc(it.rel)})">
          <div class="pill-top">
            <span class="pill-ico">${icon(it)}</span>
            ${it.time?`<span class="pill-time">${esc(it.time)}</span>`:''}
            <span class="pill-txt">${esc(c.main)}${esc(spanInfo)}</span>
            ${fbBadge}
          </div>
          ${c.meta?`<div class="pill-meta">${esc(c.meta)}</div>`:''}
          ${it.kind==='workout' ? renderWkoBars(it, false) : ''}
        </div>`;
      }
      if(more > 0){
        html += `<div class="more-pill" onclick="event.stopPropagation();showDayModal('${ds}')">+${more} weitere</div>`;
      }
      html += `</div>`;
    }
    html += `</div>`;
    mainEl.innerHTML = html;
  } else if(layout==='week'){
    const dowOffset = (cur.getDay() + 6) % 7;
    const wStart = new Date(cur.getFullYear(), cur.getMonth(), cur.getDate() - dowOffset);
    const wEnd = new Date(wStart.getFullYear(), wStart.getMonth(), wStart.getDate() + 6);
    const kw = getKW(wStart);
    document.getElementById('mlabel').textContent = `KW ${kw} · ${wStart.getDate()}. ${MONTHS_SHORT[wStart.getMonth()]} – ${wEnd.getDate()}. ${MONTHS_SHORT[wEnd.getMonth()]} ${wEnd.getFullYear()}`;

    let html = `<div class="week-list">`;
    for(let i=0;i<7;i++){
      const d = new Date(wStart.getFullYear(), wStart.getMonth(), wStart.getDate()+i);
      const ds = iso(d);
      const isToday = ds === DATA.today;
      const dayMatches = [];
      DATA.items.forEach((it, idx) => {
        if(keepItem(it) && it.start <= ds && ds <= it.end) dayMatches.push({it, idx});
      });
      html += `<div class="wday-row${isToday?' today':''}" onclick="if(event.target===this)showDayModal('${ds}')">
        <div class="wday-left" onclick="showDayModal('${ds}')">
          <span class="wday-dow">${DOWS[i]}${isToday?' · HEUTE':''}</span>
          <span class="wday-date">${d.getDate()}. ${MONTHS_SHORT[d.getMonth()]}</span>
        </div>
        <div class="wday-right">`;
      if(dayMatches.length){
        for(const {it, idx} of dayMatches){
          html += `<div class="row" onclick="showItemModal(${idx})">
            <div class="row-main">
              <div class="row-title">${icon(it)} ${it.time?`<strong>${esc(it.time)}</strong> `:''}${esc(it.what)}</div>
              <div class="row-sub"><span>${esc(it.kind)}${it.subkind?':'+esc(it.subkind):''}</span> · <span>${esc(it.rel)}</span></div>
            </div>
            <span class="tag">${esc(deltaLabel(it.delta))}</span>
          </div>`;
        }
      } else {
        html += `<div class="wday-empty" onclick="showDayModal('${ds}')">${filter==='coach'?'Freies Trainingsfenster (Klicken für Workout-Vorlage)':'Keine Termine eingetragen'}</div>`;
      }
      html += `</div></div>`;
    }
    html += `</div>`;
    mainEl.innerHTML = html;
  } else {
    document.getElementById('mlabel').textContent = `Agenda · ${cur.getFullYear()}`;
    const groups = new Map();
    DATA.items.forEach((it, idx) => {
      if(!keepItem(it) || (it.delta < -14 && it.status !== 'open')) return;
      const ym = it.start.slice(0, 7);
      if(!groups.has(ym)) groups.set(ym, []);
      groups.get(ym).push({it, idx});
    });
    if(!groups.size){
      mainEl.innerHTML = `<div class="agenda-view"><div class="empty-box">Keine Einträge für diesen Filter gefunden.</div></div>`;
    } else {
      let html = `<div class="agenda-view">`;
      for(const [ym, arr] of groups.entries()){
        const [yy, mm] = ym.split('-').map(Number);
        html += `<div class="ag-group"><div class="ag-month-hdr"><span>${MONTHS[mm-1]} ${yy}</span><span>${arr.length} Einträge</span></div>`;
        for(const {it, idx} of arr){
          const dObj = parseIso(it.start);
          const dow = DOWS[(dObj.getDay()+6)%7];
          html += `<div class="ag-item" onclick="showItemModal(${idx})">
            <div class="ag-date-col">
              <span class="ag-date-main">${dow}, ${fmtDateDE(it.start).slice(0,6)}</span>
              <span class="ag-date-sub">KW ${getKW(dObj)}${it.time?' · '+esc(it.time):''}</span>
            </div>
            <div class="row-main">
              <div class="row-title">${icon(it)} ${esc(it.what)}</div>
              <div class="row-sub"><span>${esc(it.rel)}</span></div>
            </div>
            <span class="tag ${it.delta<0?'overdue':(it.kind==='goal'?'goal':'')}">${esc(deltaLabel(it.delta))}</span>
          </div>`;
        }
        html += `</div>`;
      }
      html += `</div>`;
      mainEl.innerHTML = html;
    }
  }
}

function renderSidebar(){
  const s1T = document.getElementById('s1-title'), s1C = document.getElementById('s1-cnt'), s1L = document.getElementById('s1-list');
  const s2T = document.getElementById('s2-title'), s2C = document.getElementById('s2-cnt'), s2L = document.getElementById('s2-list');
  const s3T = document.getElementById('s3-title'), s3C = document.getElementById('s3-cnt'), s3L = document.getElementById('s3-list');
  const coach = DATA.coach || {metrics:[], macro_goals:[], schedule:''};

  if(filter==='coach'){
    // Card 1: Goals & Macro Pillars
    const datedGoals = DATA.items.map((it,idx)=>({it,idx})).filter(x => x.it.kind==='goal' && x.it.delta>=0 && x.it.status==='open');
    const macros = coach.macro_goals || [];
    s1T.textContent = '🏆 Saisonziele & Athletik-Fokus';
    s1C.textContent = datedGoals.length + macros.length;
    let gHtml = datedGoals.map(({it,idx}) => `
      <div class="row" onclick="showItemModal(${idx})">
        <div class="row-main">
          <div class="row-title">🏆 ${esc(it.what)}</div>
          <div class="row-sub">${fmtDateDE(it.start)} · ${esc(it.subkind||'Race')}</div>
        </div>
        <span class="tag goal">in ${it.delta}d (${(it.delta/7).toFixed(1)}w)</span>
      </div>
    `).join('');
    gHtml += macros.map(m => `
      <div class="row" style="cursor:default">
        <div class="row-main">
          <div class="row-title">🎯 ${esc(m.title)}</div>
          <div class="row-sub">${esc(m.detail)}</div>
        </div>
        <span class="tag goal">${esc(m.cat||'Fokus')}</span>
      </div>
    `).join('');
    s1L.innerHTML = gHtml || `<div class="empty-box">Keine Goals eingetragen.<div class="code-hint">- [ ] YYYY-MM-DD [goal:A] Rennen</div></div>`;

    // Card 2: Workouts (-14d .. +14d)
    const wos = DATA.items.map((it,idx)=>({it,idx})).filter(x => x.it.kind==='workout' && x.it.delta>=-14 && x.it.delta<=14);
    s2T.textContent = '🚴 Trainingsblock (-14d bis +14d)';
    s2C.textContent = wos.length;
    s2L.innerHTML = wos.length ? wos.map(({it,idx}) => `
      <div class="row" onclick="showItemModal(${idx})">
        <div class="row-main">
          <div class="row-title">${icon(it)} ${esc(it.what)}</div>
          <div class="row-sub">
            <span>${fmtDateDE(it.start)} · ${esc(it.subkind||'workout')}</span>
            ${it.zwo_xml?`<span class="tag low" onclick="event.stopPropagation();downloadZwo(${idx})" title=".zwo herunterladen">⬇ .zwo</span>`:''}
            ${it.feedback?`<span class="tag ok" title="${esc(it.feedback)}">✓ Notiz</span>`:(it.feedback_pending?`<span class="tag med">⚠ Check-in</span>`:'')}
          </div>
          ${renderWkoBars(it, false)}
        </div>
        <span class="tag ${it.status==='done'?'ok':''}">${it.status==='done'?'✓ erledigt':deltaLabel(it.delta)}</span>
      </div>
    `).join('') : `<div class="empty-box">
      <div>Noch keine Einzel-Workouts für diese Woche in <code>calendar/README.md</code> geplant.</div>
      <div class="code-hint" onclick="navigator.clipboard.writeText('- [ ] ${DATA.today} [workout:cycling] 90m Zone 2 / SweetSpot')">📋 Workout-Zeile kopieren</div>
    </div>`;

    // Card 3: 3-Epoch Power Curve & Blockers (28d)
    const blk = DATA.items.map((it,idx)=>({it,idx})).filter(x => x.it.delta>=0 && x.it.delta<=28 && x.it.status==='open' && (x.it.kind==='blocked' || (x.it.kind==='event' && x.it.time)));
    const pc = (coach.power_curve && coach.power_curve.fresh) || {};
    const pcf = (coach.power_curve && coach.power_curve.fatigued) || {};
    const durs = ['5s','1m','5m','20m','60m'].filter(d => pc[d]);
    s3T.textContent = '⚡ 3-Epochen Power Curve & Blocker';
    s3C.textContent = blk.length ? `${blk.length} Blocker` : 'MMP';
    let c3Html = '';
    if(durs.length){
      c3Html += `<table class="pcurve-tbl"><thead><tr><th>Dauer</th><th>42d</th><th>90d</th><th>365d</th><th>&gt;1500kJ</th></tr></thead><tbody>` +
        durs.map(d => {
          const r = pc[d] || {};
          const rf = pcf[d] || {};
          return `<tr><td>${d}</td><td>${esc(r['42d']||'—')}</td><td>${esc(r['90d']||'—')}</td><td>${esc(r['365d']||'—')}</td><td>${esc(rf['42d']||rf['90d']||'—')}</td></tr>`;
        }).join('') + `</tbody></table>`;
    }
    c3Html += blk.length ? blk.map(({it,idx}) => `
      <div class="row" onclick="showItemModal(${idx})">
        <div class="row-main">
          <div class="row-title">${icon(it)} ${it.time?esc(it.time)+' ':''}${esc(it.what)}</div>
          <div class="row-sub">${fmtDateDE(it.start)}${it.end!==it.start?'..'+fmtDateDE(it.end):''}</div>
        </div>
        <span class="tag high">${deltaLabel(it.delta)}</span>
      </div>
    `).join('') : `<div class="empty-box" style="border-color:rgba(16,185,129,0.3);color:#a7f3d0">✅ 100 % freie Trainingsverfügbarkeit in den nächsten 28 Tagen.</div>`;
    s3L.innerHTML = c3Html;
  } else if(filter==='todo'){
    // Card 1: Open Backlog To-Dos
    s1T.textContent = '☑️ Offene To-Dos (Backlog)';
    s1C.textContent = DATA.todos.length;
    s1L.innerHTML = DATA.todos.length ? DATA.todos.map(t => `
      <div class="row">
        <div class="row-main">
          <div class="row-title">${esc(t.what)}</div>
          <div class="row-sub">${esc(t.rel)}</div>
        </div>
        <span class="tag ${esc(t.prio)}">${esc(t.prio)}</span>
      </div>
    `).join('') : `<div class="empty-box">Keine offenen Backlog-To-Dos.</div>`;

    // Card 2: Upcoming Deadlines (Next 90d)
    const d90 = DATA.items.map((it,idx)=>({it,idx})).filter(x => isTodoItem(x.it) && x.it.delta>=-7 && x.it.delta<=90 && x.it.status==='open');
    s2T.textContent = '⏳ Fristen & Stichtage (90 Tage)';
    s2C.textContent = d90.length;
    s2L.innerHTML = d90.length ? d90.map(({it,idx}) => `
      <div class="row" onclick="showItemModal(${idx})">
        <div class="row-main">
          <div class="row-title">${icon(it)} ${esc(it.what)}</div>
          <div class="row-sub">${fmtDateDE(it.start)} · ${esc(it.rel)}</div>
        </div>
        <span class="tag ${it.delta<0?'overdue':(it.delta<=14?'med':'')}">${deltaLabel(it.delta)}</span>
      </div>
    `).join('') : `<div class="empty-box">Keine Fristen in den nächsten 90 Tagen.</div>`;

    // Card 3: Long-term Deadlines (> 90d)
    const dLong = DATA.items.map((it,idx)=>({it,idx})).filter(x => isTodoItem(x.it) && x.it.delta>90 && x.it.status==='open');
    s3T.textContent = '📆 Langfristige Stichtage (> 90d)';
    s3C.textContent = dLong.length;
    s3L.innerHTML = dLong.length ? dLong.map(({it,idx}) => `
      <div class="row" onclick="showItemModal(${idx})">
        <div class="row-main">
          <div class="row-title">${icon(it)} ${esc(it.what)}</div>
          <div class="row-sub">${fmtDateDE(it.start)} · ${esc(it.rel)}</div>
        </div>
        <span class="tag">${deltaLabel(it.delta)}</span>
      </div>
    `).join('') : `<div class="empty-box">Keine langfristigen Fristen.</div>`;
  } else {
    // Default 'all' view:
    // Card 1: Next 30 Days Agenda (matches settings.md deadline_days=30!)
    const ag30 = DATA.items.map((it,idx)=>({it,idx})).filter(x => keepItem(x.it) && x.it.delta>=0 && x.it.delta<=30 && x.it.status==='open');
    s1T.textContent = '📅 Nächste 30 Tage (Agenda)';
    s1C.textContent = ag30.length;
    s1L.innerHTML = ag30.length ? ag30.map(({it,idx}) => `
      <div class="row" onclick="showItemModal(${idx})">
        <div class="row-main">
          <div class="row-title">${icon(it)} ${it.time?esc(it.time)+' ':''}${esc(it.what)}</div>
          <div class="row-sub">${fmtDateDE(it.start)}${it.end!==it.start?'..'+fmtDateDE(it.end):''} · ${esc(it.rel)}</div>
        </div>
        <span class="tag ${it.delta<=7?'med':''}">${deltaLabel(it.delta)}</span>
      </div>
    `).join('') : `<div class="empty-box">Keine Einträge in den nächsten 30 Tagen.</div>`;

    // Card 2: Open To-Dos (Backlog)
    s2T.textContent = '☑️ Offene To-Dos & Checklisten';
    s2C.textContent = DATA.todos.length;
    s2L.innerHTML = DATA.todos.length ? DATA.todos.map(t => `
      <div class="row">
        <div class="row-main">
          <div class="row-title">${esc(t.what)}</div>
          <div class="row-sub">${esc(t.rel)}</div>
        </div>
        <span class="tag ${esc(t.prio)}">${esc(t.prio)}</span>
      </div>
    `).join('') : `<div class="empty-box">Keine offenen To-Dos.</div>`;

    // Card 3: Goals, Birthdays & Milestones
    const hi = DATA.items.map((it,idx)=>({it,idx})).filter(x => x.it.delta>=0 && x.it.status==='open' && (x.it.kind==='goal' || x.it.kind==='birthday' || x.it.delta>30));
    s3T.textContent = '🏆 Ziele, Geburtstage & Ausblick';
    s3C.textContent = hi.length;
    s3L.innerHTML = hi.length ? hi.map(({it,idx}) => `
      <div class="row" onclick="showItemModal(${idx})">
        <div class="row-main">
          <div class="row-title">${icon(it)} ${esc(it.what)}</div>
          <div class="row-sub">${fmtDateDE(it.start)} · ${esc(it.rel)}</div>
        </div>
        <span class="tag ${it.kind==='goal'?'goal':''}">${deltaLabel(it.delta)}</span>
      </div>
    `).join('') : `<div class="empty-box">Keine weiteren Jahrestermine.</div>`;
  }
}

function render(){
  for(const k of ['all','coach','todo']){
    const el = document.getElementById('f-'+k);
    if(el) el.classList.toggle('active', k===filter);
  }
  for(const l of ['month','week','agenda']){
    const el = document.getElementById('v-'+l);
    if(el) el.classList.toggle('active', l===layout);
  }
  const coachCnt = DATA.items.filter(isCoachItem).length + ((DATA.coach&&DATA.coach.macro_goals)||[]).length;
  const todoCnt = DATA.items.filter(isTodoItem).length + DATA.todos.length;
  document.getElementById('cnt-all').textContent = DATA.items.length;
  document.getElementById('cnt-coach').textContent = coachCnt;
  document.getElementById('cnt-todo').textContent = todoCnt;

  renderKPIs();
  renderMain();
  renderSidebar();
}

// Keyboard shortcuts
document.addEventListener('keydown', e => {
  if(e.target.tagName==='INPUT' || e.target.tagName==='TEXTAREA'){
    if(e.key==='Escape') e.target.blur();
    return;
  }
  if(e.key==='Escape') closeModal();
  else if(e.key==='ArrowLeft') stepNav(-1);
  else if(e.key==='ArrowRight') stepNav(1);
  else if(e.key==='t' || e.key==='T') goToday();
  else if(e.key==='1') setFilter('all');
  else if(e.key==='2') setFilter('coach');
  else if(e.key==='3') setFilter('todo');
  else if(e.key==='m' || e.key==='M') setLayout('month');
  else if(e.key==='w' || e.key==='W') setLayout('week');
  else if(e.key==='a' || e.key==='A') setLayout('agenda');
  else if(e.key==='/'){ e.preventDefault(); document.getElementById('q-in').focus(); }
});

// Seamless background live-sync (preserves filter, month & scroll state without page reload flash!)
setInterval(async () => {
  try {
    const res = await fetch(window.location.href, {cache: 'no-store'});
    if(!res.ok) return;
    const html = await res.text();
    const m = html.match(/let DATA = (\\{.*?\\});\\n/);
    if(m){
      const nextData = JSON.parse(m[1]);
      if(JSON.stringify(nextData) !== JSON.stringify(DATA)){
        DATA = nextData;
        render();
      }
    }
  } catch(_e){}
}, 15000);

render();
</script></body></html>
"""


CAL_SERVER_SCRIPT = """import functools, http.server, os, sys
sys.stdout = sys.stderr = open(os.devnull, "w")
class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args, **kwargs):
        pass
handler = functools.partial(_QuietHandler, directory=sys.argv[2])
http.server.ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), handler).serve_forever()
"""


def _cal_http_ok(port):
    """Verify the calendar server actually returns HTTP 200 (catches pythonw.exe stderr=None ERR_EMPTY_RESPONSE)."""
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/calendar.html", timeout=0.25) as resp:
            return resp.status == 200
    except Exception:
        return False


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
    if _cal_http_ok(port):
        return f"http://127.0.0.1:{port}/calendar.html"
    if os.name == "nt":
        # Clean up any legacy pythonw.exe -m http.server (v0.6.2-v0.6.5) whose sys.stderr=None caused ERR_EMPTY_RESPONSE
        try:
            kill_ps = ("Get-CimInstance Win32_Process -Filter \"Name LIKE 'python%' AND CommandLine LIKE '%http.server%'\" "
                       "| Invoke-CimMethod -MethodName Terminate | Out-Null")
            subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", kill_ps],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=4, creationflags=0x08000000)
            time.sleep(0.15)
        except Exception:
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
        srv_script = os.path.join(cache_dir, "_cal_server.py")
        with open(srv_script, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(CAL_SERVER_SCRIPT)
        with open(port_file, "w", encoding="utf-8") as fh:
            fh.write(str(port))
        py_exe = sys.executable
        if os.name == "nt":
            pyw = os.path.join(os.path.dirname(py_exe), "pythonw.exe")
            if os.path.isfile(pyw):
                py_exe = pyw
        server_cmd = [py_exe, "-I", srv_script, str(port), cache_dir]
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
        for _ in range(20):
            if _cal_http_ok(port):
                break
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
    try:
        coach = _extract_coach_context(idx, cal_items=raw_items)
        c_state = load_coach_state()
    except Exception:
        coach = {"metrics": [], "ftp_watts": 275, "macro_goals": [], "schedule": ""}
        c_state = {"activities": []}
    ftp_w = coach.get("ftp_watts", 275) or 275
    acts_by_date = {}
    for a in c_state.get("activities", []):
        if isinstance(a, dict) and a.get("date"):
            acts_by_date.setdefault(a["date"], []).append(a)
    for it in serial:
        if it.get("kind") != "workout":
            continue
        sport = it.get("subkind") or "cycling"
        spec = parse_workout_spec(it.get("what", ""), ftp_watts=ftp_w, sport=sport)
        it["visual_blocks"] = spec.get("visual_blocks", [])
        it["total_min"] = spec.get("total_min", 0)
        if sport == "cycling" and spec.get("has_intervals"):
            it["zwo_xml"] = compile_zwo_xml(spec, date_str=it["start"], ftp_watts=ftp_w)
            slug = re.sub(r"[^a-z0-9]+", "-", spec.get("title", "workout").lower()).strip("-") or "workout"
            it["zwo_filename"] = f"{it['start']}_{slug}.zwo"
        m_fb = re.search(r"\[feedback:\s*([^\]]+)\]", it.get("what", ""), re.I)
        if m_fb:
            fb_val = m_fb.group(1).strip()
            if fb_val.lower() == "pending":
                it["feedback_pending"] = True
            else:
                it["feedback"] = fb_val
        day_acts = acts_by_date.get(it["start"], [])
        if day_acts:
            act = day_acts[0]
            it["strava_id"] = act.get("id")
            if act.get("np_w"):
                it["np_w"] = act.get("np_w")
            if act.get("avg_hr"):
                it["avg_hr"] = act.get("avg_hr")
            if act.get("feedback") and str(act["feedback"]).lower() != "pending":
                it["feedback"] = str(act["feedback"]).strip()
                it["feedback_pending"] = False
            elif it.get("status") == "done" and not it.get("feedback"):
                it["feedback_pending"] = True
    payload = {"today": dt.date.today().isoformat(), "items": serial, "todos": todos, "coach": coach}
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
                cal_uri = render_calendar_html(idx, start_server=True)
                parts.append(f"Visual calendar: [📅 Kalender öffnen]({cal_uri}) (include this clickable link in the briefing)")
            except Exception:
                pass
        try:
            if "coach" in st.get("modules", []) or os.path.isdir(os.path.join(BRAIN, "coach")):
                c_state = load_coach_state()
                pend = c_state.get("pending_feedback", [])
                if pend:
                    p0 = pend[0]
                    parts.append(f"Coach check-in: Ride '{p0.get('name', 'Workout')}' on {p0.get('date')} ({p0.get('duration_min', 0)}m, NP {p0.get('np_w') or '—'}W) has no athlete feedback note yet. Ask 1 short question how it felt (RPE/legs) and save via `atlas.py coach note --date {p0.get('date')} --text \"...\"`.")
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
        if not PROBE:
            try:
                render_calendar_html(idx, start_server=True)
            except Exception:
                pass
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


COACH_RX_EXPLICIT = re.compile(r"(?i)(?:^|\s)/atlas-coach\b|\b(?:sport-?coach|atlas\s+coach)\b|^\s*coach\b")
COACH_RX_MACRO = re.compile(
    r"(?i)\b(?:makro-?plan\w*|macro\s*plan\w*|saison-?plan\w*|jahres-?plan\w*|periodisier\w*|"
    r"trainings-?phas\w*|saison-?ziel\w*|bis\s+zum\s+(?:giro|rennen|event|wettkampf))\b"
)
COACH_RX_PLAN14 = re.compile(
    r"(?i)\b(?:2-?wochen-?plan\w*|zwei-?wochen\w*|14-?\s*tage\w*|14d\b|n[aä]chsten?\s+(?:zwei\s+)?wochen?\s+(?:planen|train\w*|workouts?)|"
    r"trainingsplan\s+(?:f[uü]r|schreiben|erstellen|planen)|workouts?\s+(?:planen|erstellen|schreiben)|wochenplan\s+training)\b"
)
COACH_RX_CHECKIN = re.compile(
    r"(?i)\b(?:rpe\s*\d|beine\s+waren|puls\s+war|strava\s+sync\w*|ride\s+check-?in|feedback\s+zum\s+(?:ride|training|workout)|"
    r"training\s+gestern\s+war|einheit\s+gestern)\b"
)
COACH_RX_DAILY = re.compile(
    r"(?i)\b(?:mein\s+workout|workout\s+heute|workout\s+morgen|was\s+trainier\w+\s+ich|heutiges?\s+(?:workout|training)|"
    r"training\s+heute|training\s+morgen|intervall-?training|\.zwo\b|karoo\s+workout|ftp\s+test|sweet\s*spot\s+workout)\b"
)


def detect_coach_mode(prompt, idx=None):
    """Return (mode, reason, macro_info) when prompt requests the Sport-Coach, else (None, '', None)."""
    low = prompt.strip()
    if not low:
        return None, "", None
    explicit = bool(COACH_RX_EXPLICIT.search(low))
    if COACH_RX_MACRO.search(low):
        mode, why = "macro", "macro periodization request"
    elif COACH_RX_PLAN14.search(low):
        mode, why = "plan-14d", "14-day workout planning request"
    elif COACH_RX_CHECKIN.search(low):
        mode, why = "checkin", "ride feedback / Strava check-in"
    elif COACH_RX_DAILY.search(low) or explicit:
        mode, why = "daily", ("explicit /atlas-coach" if explicit else "daily workout / adaptation request")
    else:
        return None, "", None
    macro = parse_macro_plan()
    return mode, why, macro


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
    coach_enabled = "coach" in st.get("modules", []) or os.path.isdir(os.path.join(BRAIN, "coach"))
    if (coach_enabled or COACH_RX_EXPLICIT.search(prompt)) and not routes:
        c_mode, c_why, c_macro = detect_coach_mode(prompt, idx)
        if c_mode:
            stale_note = ""
            if c_macro and c_macro.get("stale") and c_mode in ("plan-14d", "macro"):
                stale_note = f" Macro plan is STALE ({c_macro.get('stale_reason')}) -> update brain/coach/macro-plan.md FIRST before writing 14d workouts."
            lines.append(
                f"Atlas coach -> mode '{c_mode}' ({c_why}). Run `python -I {_show('_atlas/tools/atlas.py', cwd)} coach context --mode {c_mode}` "
                f"and follow {_show('_atlas/procedures/coach.md', cwd)}.{stale_note}"
            )
            sess["coach_mode"] = c_mode
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
