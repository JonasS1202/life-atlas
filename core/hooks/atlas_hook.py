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
import traceback

sys.dont_write_bytecode = True

_TOOLS_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools"))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)
from atlas_lib import cal_engine, coach_engine, graph_engine

BRAIN = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
PROBE = bool(os.environ.get("ATLAS_PROBE"))  # set by `atlas.py probe`: run, but change nothing
STATE = os.path.join(BRAIN, "_atlas", "state.json")
CACHE = os.path.join(BRAIN, "_atlas", "cache", "index.sqlite")
CAL_HTML = os.path.join(BRAIN, "_atlas", "cache", "calendar.html")
ERR_LOG = os.path.join(BRAIN, "_atlas", "cache", "hook_errors.log")
CONFLICTS_FILE = os.path.join(BRAIN, "_atlas", "conflicts.json")
MAX_ERR_LOG_BYTES = 50_000
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


def _log_hook_error(where, exc):
    """Append a bounded diagnostic entry to `_atlas/cache/hook_errors.log` (strictly <= MAX_ERR_LOG_BYTES, fail-open)."""
    try:
        os.makedirs(os.path.dirname(ERR_LOG), exist_ok=True)
        stamp = _now_iso()
        err_msg = str(exc).splitlines()[0].strip() if str(exc) else ""
        header = f"[{stamp}] {where}: {type(exc).__name__}" + (f": {err_msg}" if err_msg else "")
        tb_lines = [ln.rstrip() for ln in traceback.format_exception(type(exc), exc, exc.__traceback__)[-4:]]
        entry = (header[:400] + "\n  " + "\n  ".join(tb_lines)[:1200]).rstrip() + "\n"
        entry_b = entry.encode("utf-8", errors="replace")[:MAX_ERR_LOG_BYTES]
        old_b = b""
        if os.path.isfile(ERR_LOG):
            try:
                with open(ERR_LOG, "rb") as fh:
                    old_b = fh.read()
            except OSError:
                old_b = b""
        combined = old_b + entry_b
        if len(combined) > MAX_ERR_LOG_BYTES:
            tail = combined[-MAX_ERR_LOG_BYTES:]
            nl = tail.find(b"\n[")
            if nl >= 0:
                combined = tail[nl + 1:]
            else:
                nl2 = tail.find(b"\n")
                combined = tail[nl2 + 1:] if nl2 >= 0 and nl2 + 1 < len(tail) else tail
        with open(ERR_LOG, "wb") as fh:
            fh.write(combined[:MAX_ERR_LOG_BYTES])
    except Exception:
        pass


def _recent_hook_error(st):
    """Return `(iso, summary)` when a fresh hook error (< 24h) newer than `last_error_notified` exists."""
    if not os.path.isfile(ERR_LOG):
        return None
    try:
        with open(ERR_LOG, encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return None
    notified = str((st or {}).get("last_error_notified") or "")
    for line in reversed(lines):
        m = re.match(r"^\[([^\]]+)\]\s+(.+)$", line)
        if m:
            iso, summ = m.group(1).strip(), m.group(2).strip()
            age = _age_days(iso)
            if age is not None and age < 1.0 and iso > notified:
                return iso, summ[:200]
            return None
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
    """Cached metadata & graph of every page in SQLite (`_atlas/cache/index.sqlite`).

    Only new or changed files are parsed. With `budget` (seconds) parsing stops in time and the partial cache is saved;
    the next call continues. Corrupt or schema-outdated databases self-heal automatically.
    """
    return graph_engine.sync_index(BRAIN, budget=budget, db_path=CACHE)


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
             "uhr", "euro", "soll", "eingang"}


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
            in_fence = False
            for i, line in enumerate(lines):
                if i == 0 and line.strip() == "---":
                    in_fm = True
                    continue
                if in_fm:
                    if line.strip() in ("---", "..."):
                        in_fm = False
                    continue
                if re.match(r"^\s*(`{3,}|~{3,})", line):
                    in_fence = not in_fence
                    continue
                if in_fence:
                    continue
                item = _parse_cal_line(line, rel, today, default_kind="event")
                if not item:
                    continue
                item["line"] = i + 1
                item["can_toggle"] = bool(re.match(r"^\s*(?:[-*+]|\d+\.)?\s*\[[ xX-]\]\s+", line))
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
            item["line"] = 0
            item["can_toggle"] = False
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

    def _add_todo(status, prio, what, rel, line_no=0, can_toggle=False):
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
        out.append({
            "status": status,
            "prio": prio,
            "what": what_clean,
            "rel": rel,
            "line": line_no,
            "can_toggle": can_toggle,
            "_r": prio_rank.get(prio, 1),
        })

    cal_readme = os.path.join(BRAIN, "calendar", "README.md")
    if os.path.isfile(cal_readme):
        try:
            with open(cal_readme, encoding="utf-8", errors="replace") as fh:
                lines = fh.read().splitlines()
        except OSError:
            lines = []
        in_todos = False
        in_fence = False
        for idx_ln, line in enumerate(lines, 1):
            if re.match(r"^\s*(`{3,}|~{3,})", line):
                in_fence = not in_fence
                continue
            if in_fence:
                continue
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
            _add_todo(status, prio, m.group(3).strip(), "calendar/README.md", line_no=idx_ln, can_toggle=True)

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
                in_fence = False
                for idx_ln, line in enumerate(lines, 1):
                    if re.match(r"^\s*(`{3,}|~{3,})", line):
                        in_fence = not in_fence
                        continue
                    if in_fence:
                        continue
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
                        _add_todo(status, (tm.group(2) or "med").lower(), raw_w, rel, line_no=idx_ln, can_toggle=True)
                        continue
                    if is_checklist_file and in_open_sec:
                        nm = re.match(r"^\d+\.\s+\*\*(.+?)\*\*(.*)$", line.strip())
                        if nm:
                            headline = (nm.group(1) + " " + nm.group(2)).strip()
                            if re.search(r"\b(erledigt|abgeschlossen|done)\b", headline, re.I):
                                continue
                            prio = "high" if re.search(r"\b(phase 5|nachziehen|nachtragen|frist)\b", headline, re.I) else "med"
                            _add_todo("open", prio, headline, rel, line_no=idx_ln, can_toggle=False)
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
    prof_full = os.path.join(BRAIN, "coach", "profile.md")
    prof_txt_full = ""
    prof_customized = False
    if os.path.isfile(prof_full):
        try:
            with open(prof_full, encoding="utf-8", errors="replace") as fh:
                prof_txt_full = fh.read(HEAD_BYTES)
            prof_customized = not coach_engine.is_untouched_skeleton_profile(prof_txt_full)
        except OSError:
            pass
    # Prioritize customized brain/coach/profile.md first so canonical coach thresholds take precedence
    if prof_customized and "coach/profile.md" in idx:
        cand_rels.append("coach/profile.md")
    for rel, e in sorted(idx.items()):
        if rel in cand_rels or (rel == "coach/profile.md" and not prof_customized):
            continue
        tset = set(e.get("tags", []))
        if rel.startswith(("self/", "coach/")) or (tset & sport_tags) or any(w in rel.lower() for w in ("radsport", "sport", "training", "physiologie")):
            cand_rels.append(rel)
    if os.path.isfile(prof_full) and "coach/profile.md" not in cand_rels:
        cand_rels.append("coach/profile.md")

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
    rdy_txt = ""
    rdy_full = os.path.join(BRAIN, "coach", "readiness.md")
    if os.path.isfile(rdy_full):
        try:
            with open(rdy_full, encoding="utf-8", errors="replace") as fh:
                rdy_txt = fh.read(HEAD_BYTES)
        except OSError:
            pass
    try:
        tel = coach_engine.build_coach_telemetry(
            profile_text=prof_txt_full,
            activities=c_state["activities"],
            readiness_text=rdy_txt,
            cal_items=cal_items,
            strava_connected=c_state["strava_connected"],
            fallback_ftp=ftp_watts,
        )
        tel_compact = {k: v for k, v in tel.items() if k != "curves"}
        raw_pc = c_state["power_curve"]
        if isinstance(raw_pc, dict) and ("fresh" in raw_pc or "fatigued" in raw_pc):
            p_curve = raw_pc
        else:
            fresh_fmt = {
                dur: {ep: f"{w}W" for ep, w in (ep_map or {}).items() if w}
                for dur, ep_map in (tel["curves"].get("fresh") or {}).items()
                if any((ep_map or {}).values())
            }
            fatig_fmt = {
                dur: {ep: f"{w}W" for ep, w in (ep_map or {}).items() if w}
                for dur, ep_map in (tel["curves"].get("fatigued_1500kj") or {}).items()
                if any((ep_map or {}).values())
            }
            p_curve = {"fresh": fresh_fmt, "fatigued": fatig_fmt} if (fresh_fmt or fatig_fmt) else {}
        dur_tiers = tel["curves"].get("durability_tiers", {})
        s_curve = c_state["pace_curve"] or {
            d: {ep: pair[1] for ep, pair in ep_map.items()}
            for d, ep_map in (tel["curves"].get("run_splits") or {}).items()
            if ep_map
        }
        depth = tel["depth"]
    except Exception:
        tel_compact = {}
        p_curve = c_state["power_curve"]
        dur_tiers = {}
        s_curve = c_state["pace_curve"]
        depth = "standard"

    return {
        "depth": depth,
        "metrics": metrics,
        "ftp_watts": ftp_watts,
        "macro_goals": macro_goals,
        "schedule": schedule or "17:00 – 20:00 Uhr",
        "macro": macro,
        "strava_connected": c_state["strava_connected"],
        "pending_feedback": c_state["pending_feedback"],
        "power_curve": p_curve,
        "durability_tiers": dur_tiers,
        "pace_curve": s_curve,
        "activities_count": len(c_state["activities"]),
        "telemetry": tel_compact,
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


CAL_TEMPLATE = cal_engine.load_calendar_template()
CAL_SERVER_SCRIPT = cal_engine.CAL_SERVER_SCRIPT


def _cal_http_ok(port):
    """Verify the calendar server returns HTTP 200 on `/healthz` (`atlas-cal-v3`)."""
    return cal_engine.cal_http_ok(port)


def _ensure_calendar_server(cache_dir, start_server=False):
    """Ensure a loopback HTTP server serves `cache_dir` (`127.0.0.1:<port>/calendar.html`)."""
    return cal_engine.ensure_calendar_server(
        cache_dir,
        start_server=start_server,
        probe=PROBE,
        tools_dir=_TOOLS_DIR,
        hook_py=os.path.abspath(__file__),
    )


def render_calendar_html(idx=None, start_server=False):
    """Generate `brain/_atlas/cache/calendar.html` (0 LLM tokens) and return its rendered `http://127.0.0.1` URL."""
    if idx is None:
        idx = load_index(budget=INDEX_BUDGET_S)
    raw_items = calendar_items(idx, days=365, past_days=90, include_done=True)
    serial = [{k: (v.isoformat() if isinstance(v, dt.date) else v) for k, v in it.items()} for it in raw_items]
    todos = [
        {
            "status": t["status"],
            "prio": t["prio"],
            "what": t["what"],
            "rel": t["rel"],
            "line": t.get("line", 0),
            "can_toggle": bool(t.get("can_toggle", False)),
        }
        for t in open_todos(include_done=False)
    ]
    try:
        coach = _extract_coach_context(idx, cal_items=raw_items)
        c_state = load_coach_state()
    except Exception:
        coach = {"depth": "standard", "metrics": [], "ftp_watts": 275, "macro_goals": [], "schedule": ""}
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
            for k_act in ("np_w", "avg_hr", "tss", "w_bal_min_pct", "matches_burned", "cho_burn_g"):
                if act.get(k_act) is not None:
                    it[k_act] = act[k_act]
            if act.get("feedback") and str(act["feedback"]).lower() != "pending":
                it["feedback"] = str(act["feedback"]).strip()
                it["feedback_pending"] = False
            elif it.get("status") == "done" and not it.get("feedback"):
                it["feedback_pending"] = True

    cache_dir = os.path.dirname(CAL_HTML)
    os.makedirs(cache_dir, exist_ok=True)
    csrf_tok = cal_engine.get_or_create_csrf_token(cache_dir)
    try:
        graph_data = cal_engine.build_brain_graph_payload(BRAIN, idx=idx, db_path=CACHE)
    except Exception:
        graph_data = {"nodes": [], "edges": [], "stats": {"nodes": 0, "edges": 0}}

    payload = {
        "today": dt.date.today().isoformat(),
        "csrf_token": csrf_tok,
        "items": serial,
        "todos": todos,
        "coach": coach,
        "graph": graph_data,
    }
    atlas_dir = os.path.join(BRAIN, "_atlas")
    tpl = cal_engine.load_calendar_template(atlas_dir=atlas_dir)
    html = tpl.replace("__DATA_JSON__", cal_engine.safe_json_for_script(payload))
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


OFFLINE_ERR_RX = re.compile(
    r"(?i)(could not resolve host|unable to access|network is unreachable|connection refused|"
    r"connection timed out|operation timed out|timed out|temporary failure|no route to host|"
    r"failed to connect|ssh: connect to host|could not read from remote repository|"
    r"does not appear to be a git repository)"
)
CONFLICT_ERR_RX = re.compile(r"(?i)(\bconflict\b|could not apply|merge conflict|unmerged files)")


def pull():
    """Multi-device: fetch the newest brain before the chat starts.

    Offline / DNS / unreachable remotes fail silently; real rebase/merge conflicts abort cleanly and notify.
    """
    if not os.path.isdir(os.path.join(BRAIN, ".git")) or not _git("remote").stdout.strip():
        return ""
    if _git("status", "--porcelain").stdout.strip():
        _git("add", "-A")
        _git("commit", "-q", "-m", "atlas: auto-commit before pull")
    try:
        res = _git("pull", "-q", "--rebase", timeout=8)
    except subprocess.TimeoutExpired:
        return ""
    if res.returncode == 0:
        return ""
    err_out = f"{res.stderr or ''}\n{res.stdout or ''}"
    in_rebase = (
        os.path.exists(os.path.join(BRAIN, ".git", "rebase-merge"))
        or os.path.exists(os.path.join(BRAIN, ".git", "rebase-apply"))
    )
    if in_rebase or CONFLICT_ERR_RX.search(err_out):
        _git("rebase", "--abort")
        return ("Sync: pulling the brain from its remote hit a merge conflict (rebase aborted cleanly; local files "
                "untouched). Tell the user and resolve it with them (git -C brain pull --rebase).")
    if OFFLINE_ERR_RX.search(err_out):
        return ""
    _git("rebase", "--abort")
    return ("Sync: pulling the brain from its remote failed. Tell the user; if it is a conflict, "
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
    fresh_err = _recent_hook_error(st)
    if fresh_err:
        err_iso, err_sum = fresh_err
        lines.append(f"Hook warning (recent logged error in _atlas/cache/hook_errors.log): {err_sum}")
        if not PROBE:
            st["last_error_notified"] = err_iso
    try:
        msg = "" if PROBE else pull()
    except Exception as exc:
        _log_hook_error("session.pull", exc)
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
            except Exception as exc:
                _log_hook_error("session.render_calendar", exc)
        try:
            if "coach" in st.get("modules", []) or os.path.isdir(os.path.join(BRAIN, "coach")):
                c_state = load_coach_state()
                pend = c_state.get("pending_feedback", [])
                if pend:
                    p0 = pend[0]
                    np_bit = f", NP {p0.get('np_w')}W" if p0.get("np_w") else ""
                    parts.append(f"Coach check-in: Session '{p0.get('name', 'Workout')}' on {p0.get('date')} ({p0.get('duration_min', 0)}m{np_bit}) has no feedback note yet. Ask 1 short question how it felt and save via `atlas.py coach note --date {p0.get('date')} --text \"...\"`.")
        except Exception as exc:
            _log_hook_error("session.coach", exc)
        try:
            open_conf = graph_engine.open_conflicts(BRAIN, CONFLICTS_FILE)
        except Exception:
            open_conf = []
        if open_conf:
            c_preview = "; ".join(f"[{c.get('id')}] {c.get('summary')}" for c in open_conf[:3])
            parts.append(f"Conflicts ({len(open_conf)} open in _atlas/conflicts.json): {c_preview}. "
                         "Resolve with the user (`atlas.py conflicts`).")
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
                         "/atlas-maintain (in the atlas-maintainer subagent), report in at most 3 lines.")
        upd = last_mark("update-check")
        if not upd or (_age_days(upd[0]) or 0) > UPDATE_DAYS:
            parts.append("Update check is due: offer /atlas-update.")
        if st.get("remote"):
            pushed = _age_days(st["last_push"]) if st.get("last_push") else None
            if pushed is None or pushed > PUSH_REMIND_DAYS:
                parts.append(f"Backup: last push of the brain is older than {PUSH_REMIND_DAYS} days.")
        try:
            foreign = foreign_items(_root(), os.path.expanduser("~"))
        except Exception as exc:
            _log_hook_error("session.foreign_items", exc)
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
            except Exception as exc:
                _log_hook_error("session.render_calendar", exc)
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
    direct, _neighbors = graph_engine.compute_prompt_hints(
        BRAIN, prompt, idx, tag_vocab(), _vocab(idx, skip_pages), skip_pages,
        max_hints=MAX_HINTS, max_df_share=MAX_DF_SHARE, db_path=CACHE,
    )
    return direct


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
    r"(?i)\b(?:rpe\s*\d|beine\s+waren|puls\s+war|ruhepuls\s+\d|rhr\s+\d|strava\s+sync\w*|"
    r"ride\s+check-?in|readiness\s+check|feedback\s+zum\s+(?:ride|training|workout|lauf)|"
    r"training\s+gestern\s+war|einheit\s+gestern)\b"
)
COACH_RX_DAILY = re.compile(
    r"(?i)\b(?:mein\s+workout|workout\s+heute|workout\s+morgen|was\s+trainier\w+\s+ich|heutiges?\s+(?:workout|training)|"
    r"training\s+heute|training\s+morgen|intervall-?training|\.zwo\b|karoo\s+workout|ftp\s+test|sweet\s*spot\s+workout|"
    r"tagesform\s+training|readiness\s+heute)\b"
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
    coach_enabled = (
        "coach" in st.get("modules", [])
        or os.path.isdir(os.path.join(BRAIN, "coach"))
        or os.path.isfile(os.path.join(BRAIN, "_atlas", "procedures", "coach.md"))
    )
    if (coach_enabled or COACH_RX_EXPLICIT.search(prompt)) and not routes:
        c_mode, c_why, c_macro = detect_coach_mode(prompt, idx)
        if c_mode:
            stale_note = ""
            if c_macro and c_macro.get("stale") and c_mode in ("plan-14d", "macro"):
                stale_note = f" Macro plan is STALE ({c_macro.get('stale_reason')}) -> update brain/coach/macro-plan.md FIRST before writing 14d workouts."
            depth_flag = " --depth pro" if coach_engine.PRO_QUERY_RX.search(prompt) else ""
            lines.append(
                f"Atlas coach -> mode '{c_mode}' ({c_why}). Run `python -I {_show('_atlas/tools/atlas.py', cwd)} coach context --mode {c_mode}{depth_flag}` "
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
    if len(plain.strip()) >= 3:
        found, linked = graph_engine.compute_prompt_hints(
            BRAIN, plain, idx, tag_vocab(), _vocab(idx, skip), skip,
            max_hints=MAX_HINTS, max_df_share=MAX_DF_SHARE, db_path=CACHE,
        )
    else:
        found, linked = [], []
    if found:
        lines.append("Atlas pages for this message (leads; open only what you need):")
        for rel, summary, terms in found:
            lines.append(f"- {_show(rel, cwd)}" + (f" — {summary}" if summary else "") + f" [{', '.join(terms)}]")
        for n_rel, n_sum, _via, _kind in linked:
            lines.append(f"  ↳ linked: {_show(n_rel, cwd)}" + (f" — {n_sum}" if n_sum else ""))
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


def _autolink_daily_entry(line_text, journal_dir):
    """Turn recognized existing `brain/` paths in a daily-log line into relative Markdown links."""
    masked = re.sub(r"!?\[[^\]]*\]\([^)]*\)", lambda m: " " * len(m.group(0)), line_text)
    out = line_text
    for m in reversed(list(graph_engine.BRAIN_PATH_RX.finditer(masked))):
        raw_p = m.group(2)
        rel = raw_p[6:] if raw_p.startswith("brain/") else raw_p
        full = os.path.join(BRAIN, rel)
        if os.path.isfile(full):
            href = os.path.relpath(full, journal_dir).replace("\\", "/")
            out = out[:m.start()] + f"[{raw_p}]({href})" + out[m.end():]
    return out


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
    jdir = os.path.dirname(path)
    os.makedirs(jdir, exist_ok=True)
    try:
        with open(path, encoding="utf-8") as fh:
            current = fh.read()
    except OSError:
        current = (f"---\nsummary: Daily log {today} (written by the stop hook from `Atlas:` lines)\n"
                   f"tags: [log]\n---\n# {today} · Log\n\n")
    stamp = dt.datetime.now().strftime("%H:%M")
    new = []
    for f in found:
        raw_cut = f[:300]
        linked = _autolink_daily_entry(raw_cut, jdir)
        if raw_cut not in current and linked not in current:
            new.append(f"- {stamp} · {context} · {linked}")
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


def _calendar_needs_render():
    """Return True only when `CAL_HTML` is missing or relevant calendar/coach/due sources are newer than `CAL_HTML`."""
    try:
        if not os.path.isfile(CAL_HTML):
            return True
        cal_mtime = os.path.getmtime(CAL_HTML)
    except OSError:
        return True
    for single in (os.path.join(BRAIN, "settings.md"), os.path.join(BRAIN, "_secrets", "strava.env")):
        try:
            if os.path.isfile(single) and os.path.getmtime(single) > cal_mtime:
                return True
        except OSError:
            pass
    for sub in ("calendar", "coach", "areas", "projects", "people", "self"):
        base = os.path.join(BRAIN, sub)
        if not os.path.isdir(base):
            continue
        for dp, dns, fns in os.walk(base):
            dns[:] = [d for d in dns if not d.startswith(".")]
            for fn in fns:
                if not fn.lower().endswith((".md", ".json")):
                    continue
                try:
                    if os.path.getmtime(os.path.join(dp, fn)) > cal_mtime:
                        return True
                except OSError:
                    continue
    return False


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
        except Exception as exc:
            _log_hook_error("stop.daily_log", exc)
    try:
        if _calendar_needs_render():
            render_calendar_html()
    except Exception as exc:
        _log_hook_error("stop.render_calendar", exc)
    try:
        autocommit_and_push()
    except Exception as exc:
        _log_hook_error("stop.autocommit", exc)
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
    except Exception as exc:
        _log_hook_error("main", exc)
    sys.exit(0)
    sys.exit(0)
