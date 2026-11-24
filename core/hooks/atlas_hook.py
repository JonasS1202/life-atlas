"""Atlas hook for Claude Code and Google Antigravity. Python 3 standard library only. Fail-open.

Registered by `atlas.py wire` (project scope: <ROOT>/.claude/settings.json, <ROOT>/.agents/hooks.json):
  python -I <ROOT>/brain/_atlas/hooks/atlas_hook.py <agent> <event>
    agent: claude | antigravity
    event: session  (Claude SessionStart,      Antigravity SessionStart warm-up)
           prompt   (Claude UserPromptSubmit,  Antigravity PreInvocation, first invocation of a turn only)
           stop     (Claude Stop,              Antigravity Stop)

session: heartbeat `Atlas · ok`; pull the brain from its remote; once a day (settings) a briefing with actionable task
         check-in (done / reschedule / tackle now), upcoming calendar items, inbox, due maintenance/update checks,
         things Antigravity's /learn wrote outside Atlas, one question; in other chats only what is overdue or due today.
         In Antigravity, `SessionStart` warms up git/index/calendar server and `PreInvocation` (`invocationNum == 0`,
         first turn of each conversation/day) injects `session_text` via `systemMessage` so briefings are never lost.
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
import secrets
import shutil
import subprocess
import sys
import threading
import time
import traceback

sys.dont_write_bytecode = True

_TOOLS_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools"))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)
from atlas_lib import cal_engine, cal_sync, coach_connectors, coach_engine, graph_engine, health_engine

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
MAX_HINTS = 3
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
SIGNAL_DO_NOT_STORE = re.compile(
    r"(?i)\b(nicht\s+(?:speichern|merken|aufnehmen|notieren|loggen|ablegen)|"
    r"speicher(?:e)?\s+(?:das\s+|es\s+)?nicht|merk\s+(?:dir\s+)?(?:das\s+|es\s+)?nicht|"
    r"behalt\s+das\s+f[uü]r\s+dich|off\s+the\s+record|"
    r"do\s+not\s+(?:store|save|keep|log|record)|don'?t\s+(?:store|save|keep|log|record))\b"
)
SIGNAL_EXPLICIT = re.compile(
    r"(?i)\b(merk(?:e)?\s+(?:dir|mir|es|das)|speicher(?:e)?(?:\s+(?:mir|dir|das|es|bitte|mal)){1,3}(?:\s+ab)?|"
    r"speicher(?:e)?\s+ab|notier(?:e)?(?:\s+(?:mir|dir|das|es|bitte))?|trag(?:e)?(?:\s+\w+){0,3}\s+ein|vergiss\s+nicht|"
    r"ab jetzt|ab sofort|von nun an|in zukunft|künftig|kuenftig|nie wieder|immer wenn|remember (?:that|this)|"
    r"save (?:this|that)|note (?:that|this)|add (?:this )?to (?:my )?calendar|"
    r"don'?t forget|from now on|going forward|never again)\b")
SIGNAL_CORRECTION = re.compile(
    r"(?i)(^\s*(nein|nee|falsch|no|wrong)\b[\s,.!]|\b(stimmt nicht|ist falsch|war falsch|nicht so|falsch verstanden|"
    r"korrigier|that'?s wrong|not like that|you misunderstood)\b)")
SIGNAL_FACT = re.compile(
    r"(?i)(?:\b\d{1,2}\.\d{1,2}\.20\d{2}\b|\b20\d{2}-\d{2}-\d{2}\b|\b\d{1,2}:\d{2}\s*uhr\b|"
    r"\b(?:diesen|n[aä]chste[n]?|kommenden)\s+(?:montag|dienstag|mittwoch|donnerstag|freitag|samstag|sonntag)\b|"
    r"\b\d+(?:[.,]\d+)?\s*(?:€|eur|euro|usd|\$)\b|"
    r"\b(?:hab(?:e)?\s+(?:heute|gerade|eben)|wir\s+haben\s+(?:heute|gerade|jetzt)|"
    r"termin\s+ist\s+am|kostet\s+jetzt|zahlen\s+jetzt|"
    r"gek[uü]ndigt|unterschrieben|gebucht|gekauft|vereinbart|entschieden|abgeschlossen)\b)"
)
SIGNAL_META_SKIP = re.compile(r"(?i)\b(?:atlas-update|screenshot|thinking\s+path|hook|bugfix|probe|test)\b")
LEARN_HINTS = {
    "explicit": "keep it now where it belongs (`atlas.py where <topic>`; rules/preferences go to howto/ or brain/settings.md) with Stand/verified date",
    "correction": "a correction: fix the page it concerns and keep the lesson in howto/",
    "fact": "capture-first: if this message states a new/updated fact, date, amount, contact, decision, or task status, file it now (`atlas.py where <topic>`) with Stand/verified date and end with `Atlas: <what changed where>`",
}
MAIL_RX = re.compile(
    r"(?i)(?:^|\s)/atlas-mail\b|\b(?:atlas_mail|postf[aä]ch\w*|posteingang\w*|inbox\b|imap\b|"
    r"e?-?mails?(?!-?adresse\w*))\b"
)
TODO_RX = re.compile(
    r"(?i)(?:^|\s)/atlas-(?:calendar|brief)\b|\b(?:offene[n]?\s+(?:aufgaben|to-?dos?|tasks?|punkte)|"
    r"meine\s+(?:aufgaben|to-?dos?|termine)|[oö]ffne\s+(?:meinen\s+)?kalender|in\s+den\s+kalender|"
    r"\b(?:to-?dos?|termine?|appointments?)\b|"
    r"(?:aufgabe|to-?do|task|termin)\w*\s+(?:abhaken|verschieben|erledigt|anlegen|eintragen|speichern))\b"
)


# ---------------------------------------------------------------- small helpers

def _state():
    try:
        with open(STATE, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.{secrets.token_hex(4)}.tmp"
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

def emit_context(agent, event, text, persistent=None):
    if not text:
        return
    if agent == "claude":
        name = "SessionStart" if event == "session" else "UserPromptSubmit"
        out = {"hookSpecificOutput": {"hookEventName": name, "additionalContext": text}}
    elif persistent if persistent is not None else (event == "session"):
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
    no_id, sync_id = cal_sync.strip_sync_comment(raw)
    m = DATE_ITEM.match(no_id)
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
    is_local = bool(cal_sync.LOCAL_FLAG_RX.search(rest))
    force_sync = bool(cal_sync.FORCE_SYNC_FLAG_RX.search(rest))
    if is_local or force_sync:
        rest = cal_sync.LOCAL_FLAG_RX.sub(" ", rest)
        rest = cal_sync.FORCE_SYNC_FLAG_RX.sub(" ", rest)
        rest = re.sub(r"\s+", " ", rest).strip()
    yearly = bool(re.match(r"^\s*(?:-\s*)?(?:\[[ xX-]\]\s*)?(?:yearly\s+|--)", no_id, re.I)) or len(d1_str) == 5 or kind == "birthday"
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
            "kind": kind, "subkind": subkind, "what": rest, "rel": rel, "yearly": yearly,
            "sync_id": sync_id, "local_only": is_local, "force_sync": force_sync}


def _is_dup_cal_item(candidate, existing_items):
    """Return True if candidate on the same date range is already represented (e.g. folder README + detail page).

    Within `calendar/` files themselves, only exact (what + time + kind) duplicates are suppressed so distinct
    same-day tasks sharing topic keywords are never silently dropped.
    """
    c_start, c_end = candidate["start"], candidate["end"]
    c_what = candidate["what"].lower().strip()
    c_tokens = _cal_tokens(c_what)
    c_rel = candidate["rel"]
    c_in_cal = c_rel.startswith("calendar/")
    c_area = c_rel.split("/")[:2]
    for ex in existing_items:
        if ex["start"] != c_start or ex["end"] != c_end:
            continue
        if c_in_cal and ex["rel"].startswith("calendar/"):
            if (
                ex["what"].lower().strip() == c_what
                and ex.get("time", "") == candidate.get("time", "")
                and ex.get("kind", "") == candidate.get("kind", "")
            ):
                return True
            continue
        if ex["what"].lower().strip() == c_what:
            return True
        if ex["rel"] == c_rel:
            continue
        ex_tokens = _cal_tokens(ex["what"])
        if not c_tokens or not ex_tokens:
            continue
        overlap = len(c_tokens & ex_tokens)
        min_len = max(min(len(c_tokens), len(ex_tokens)), 1)
        same_area = (ex["rel"].split("/")[:2] == c_area) or ex["rel"].startswith("calendar/")
        if (same_area and overlap >= 2 and overlap / min_len >= 0.5) or (overlap >= 3 and overlap / min_len >= 0.5):
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
            fm_layer = os.path.splitext(fn)[0].lower()
            fm_label = "Personal" if fn.lower() == "readme.md" else os.path.splitext(fn)[0]
            fm_color = ""
            fm_mode = "bidirectional"
            for i, line in enumerate(lines):
                if i == 0 and line.strip() == "---":
                    in_fm = True
                    continue
                if in_fm:
                    st_l = line.strip()
                    if st_l in ("---", "..."):
                        in_fm = False
                    elif ":" in st_l:
                        fk, fv = st_l.split(":", 1)
                        fk = fk.strip().lower()
                        fv = fv.strip().strip("\"'")
                        if fk == "cal_layer" and fv:
                            fm_layer = fv.lower()
                        elif fk == "cal_label" and fv:
                            fm_label = fv
                        elif fk == "cal_color" and fv:
                            fm_color = fv
                        elif fk == "cal_mode" and fv:
                            fm_mode = fv.lower()
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
                item["layer"] = fm_layer
                item["layer_label"] = fm_label
                if fm_color:
                    item["layer_color"] = fm_color
                item["layer_mode"] = fm_mode
                item["can_toggle"] = (fm_mode != "read-only") and bool(re.match(r"^\s*(?:[-*+]|\d+\.)?\s*\[[ xX-]\]\s+", line))
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
            item["can_toggle"] = (item["kind"] in ("todo", "deadline") and not item.get("yearly"))
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
            # Never fuzzy-drop two distinct user tasks inside `calendar/README.md`
            if rel == "calendar/README.md" and ex["rel"] == "calendar/README.md":
                continue
            t_ex = _cal_tokens(ex["what"])
            if t_new and t_ex:
                overlap = len(t_new & t_ex)
                if overlap >= 2 and overlap / max(min(len(t_new), len(t_ex)), 1) >= 0.6:
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
                        lines = fh.read(262_144).splitlines()
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


def _norm_pwr(raw_val, default=0.70, ftp_watts=275):
    """Parse percentage ('75%', '102'), watts ('388W'), or decimal ('0.75', '1.02') into a float factor."""
    if raw_val is None or raw_val == "":
        return default
    s = str(raw_val).replace(",", ".").strip()
    is_watts = s.lower().endswith("w")
    num_s = s[:-1].strip() if (is_watts or s.endswith("%")) else s
    try:
        v = float(num_s)
    except ValueError:
        return default
    if is_watts:
        return round(v / max(80.0, float(ftp_watts or 275)), 4)
    return round(v / 100.0, 4) if v > 3.0 else round(v, 4)


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


_CAD_SUFFIX_RE = re.compile(r"(?:\s*@?\s*(\d{2,3}(?:\s*[-–]\s*\d{2,3})?)\s*rpm)\s*$", re.I)


def _norm_cad(raw_val):
    """Parse cadence token ('90', '90rpm', '65-70', '105-115rpm') into (low_int, high_int) or (None, None)."""
    if raw_val is None or raw_val == "":
        return None, None
    s = re.sub(r"rpm$", "", str(raw_val).strip(), flags=re.I).strip()
    m = re.match(r"^(\d{2,3})(?:\s*[-–]\s*(\d{2,3}))?$", s)
    if not m:
        return None, None
    lo = max(30, min(200, int(m.group(1))))
    hi = max(30, min(200, int(m.group(2)))) if m.group(2) else lo
    if lo > hi:
        lo, hi = hi, lo
    return lo, hi


def _strip_cad_suffix(text):
    """Strip an optional trailing cadence target (`@90rpm`, `65-70rpm`) from a DSL token."""
    s = str(text or "").strip()
    m = _CAD_SUFFIX_RE.search(s)
    if not m:
        return s, None, None
    lo, hi = _norm_cad(m.group(1))
    return s[:m.start()].strip(), lo, hi


def _fmt_cad(lo, hi):
    if lo is None:
        return ""
    if hi is None or int(lo) == int(hi):
        return f"{int(lo)} rpm"
    return f"{int(lo)}–{int(hi)} rpm"


def _cad_xml_attrs(lo, hi):
    """Format ZWO cadence XML attributes (`Cadence` for exact target, `CadenceLow`/`CadenceHigh` for range)."""
    if lo is None:
        return ""
    lo_i = int(lo)
    hi_i = int(hi) if hi is not None else lo_i
    if lo_i == hi_i:
        return f' Cadence="{lo_i}"'
    return f' CadenceLow="{lo_i}" CadenceHigh="{hi_i}"'


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
    # Look for `( ... | ... )` first (allows 1 level of nested parens, e.g. `4x (45s@420W + 3.25m@350W)` or `3x (13x 30s/15s)`)
    m_pipe = re.search(r"\(((?:[^()]|\([^()]*\))*\|(?:[^()]|\([^()]*\))*)\)", clean_raw)
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
        elif re.search(r"\b\d+\s*[x×]\s*(?:\(|\d)|\b\d+\s*(?:m|min|s)\s*(?:wu|cd|@|z[1-5])", clean_raw, re.I):
            # Allow raw DSL strings directly (e.g., from `atlas.py coach simulate --dsl "..."`)
            spec_body = clean_raw

    segments = []
    unparsed_tokens = []
    if spec_body:
        tokens = [t.strip() for t in re.split(r"[,;]+", spec_body) if t.strip()]
        for tok in tokens:
            # 0a. Nested interval sets:
            #   `3x (13x 30s@394W@95-105rpm / 15s@206W@85rpm) / 4m@150W@85rpm`
            #   `3x13x 30s@394W / 15s@206W` (optional `/ 4m@150W` inter-set rest; default 4m @ 50%)
            _cad_opt = r"(?:\s*@?\s*(\d{2,3}(?:\s*[-–]\s*\d{2,3})?)\s*rpm)?"
            m_nset = re.match(
                r"^(\d+)\s*[x×]\s*\(\s*(\d+)\s*[x×]\s*(\d+(?:[.,]\d+)?)\s*(m|min|s)?\s*(?:@\s*(\d+(?:[.,]\d+)?\s*[wW%]?))?" + _cad_opt
                + r"(?:\s*(?:/|with|rest|pause)\s*(\d+(?:[.,]\d+)?)\s*(m|min|s)?\s*(?:@\s*(\d+(?:[.,]\d+)?\s*[wW%]?))?" + _cad_opt + r")?\s*\)"
                + r"(?:\s*(?:/|rest|pause)\s*(\d+(?:[.,]\d+)?)\s*(m|min|s)?\s*(?:@\s*(\d+(?:[.,]\d+)?\s*[wW%]?))?" + _cad_opt + r")?$",
                tok, re.I
            )
            if not m_nset:
                m_nset = re.match(
                    r"^(\d+)\s*[x×]\s*(\d+)\s*[x×]\s*(\d+(?:[.,]\d+)?)\s*(m|min|s)?\s*(?:@\s*(\d+(?:[.,]\d+)?\s*[wW%]?))?" + _cad_opt
                    + r"(?:\s*(?:/|with|rest|pause)\s*(\d+(?:[.,]\d+)?)\s*(m|min|s)?\s*(?:@\s*(\d+(?:[.,]\d+)?\s*[wW%]?))?" + _cad_opt + r")?"
                    + r"(?:\s*(?:/|rest|pause)\s*(\d+(?:[.,]\d+)?)\s*(m|min|s)?\s*(?:@\s*(\d+(?:[.,]\d+)?\s*[wW%]?))?" + _cad_opt + r")?$",
                    tok, re.I
                )
            if m_nset:
                n_sets = max(1, int(m_nset.group(1)))
                rep = max(1, int(m_nset.group(2)))
                on_sec = _dur_sec(m_nset.group(3), m_nset.group(4) or "s")
                on_p = _norm_pwr(m_nset.group(5), 1.15, ftp_watts=ftp_watts)
                on_cl, on_ch = _norm_cad(m_nset.group(6))
                off_sec = _dur_sec(m_nset.group(7), m_nset.group(8) or "s") if m_nset.group(7) else max(15, int(round(on_sec * 0.5)))
                off_p = _norm_pwr(m_nset.group(9), 0.60, ftp_watts=ftp_watts)
                off_cl, off_ch = _norm_cad(m_nset.group(10))
                set_rest_sec = _dur_sec(m_nset.group(11), m_nset.group(12) or "m") if m_nset.group(11) else 240
                set_rest_p = _norm_pwr(m_nset.group(13), 0.50, ftp_watts=ftp_watts)
                sr_cl, sr_ch = _norm_cad(m_nset.group(14))
                iv_lbl = (
                    f"{rep}× {on_sec // 60 if on_sec >= 60 else str(on_sec) + 's'}"
                    f"{'m' if on_sec >= 60 else ''} @ {int(round(on_p * 100))}% / "
                    f"{off_sec // 60 if off_sec >= 60 else str(off_sec) + 's'}"
                    f"{'m' if off_sec >= 60 else ''} @ {int(round(off_p * 100))}%"
                )
                for s_idx in range(n_sets):
                    seg_iv = {
                        "type": "intervals", "repeat": rep, "on_sec": on_sec, "off_sec": off_sec,
                        "on_p": on_p, "off_p": off_p, "label": iv_lbl,
                    }
                    if on_cl is not None:
                        seg_iv["on_cad_low"], seg_iv["on_cad_high"] = on_cl, on_ch
                    if off_cl is not None:
                        seg_iv["off_cad_low"], seg_iv["off_cad_high"] = off_cl, off_ch
                    segments.append(seg_iv)
                    if s_idx < n_sets - 1 and set_rest_sec > 0:
                        seg_sr = {
                            "type": "steady", "sec": set_rest_sec, "p": set_rest_p,
                            "label": f"{set_rest_sec // 60}m Rest @ {int(round(set_rest_p * 100))}%",
                        }
                        if sr_cl is not None:
                            seg_sr["cad_low"], seg_sr["cad_high"] = sr_cl, sr_ch
                        segments.append(seg_sr)
                continue

            # 0b. Compound multi-stage intervals (e.g. Hard-Start: `4x (45s@420W + 3.25m@350W) / 5m@147W`
            #     or Cadence-Switch: `3x (4m@235W@65-70rpm + 4m@235W@92-98rpm) / 4m@185W@85-90rpm`)
            m_cmp = re.match(
                r"^(\d+)\s*[x×]\s*\(([^()]+)\)(?:\s*(?:/|rest|pause)\s*(\d+(?:[.,]\d+)?)\s*(m|min|s)?\s*(?:@\s*(\d+(?:[.,]\d+)?\s*[wW%]?))?" + _cad_opt + r")?$",
                tok, re.I
            )
            if m_cmp:
                rep = int(m_cmp.group(1))
                inner = m_cmp.group(2)
                raw_stages = [st.strip() for st in re.split(r"(?:\+|->|→)", inner) if st.strip()]
                stages = []
                for st_s in raw_stages:
                    st_clean, st_cl, st_ch = _strip_cad_suffix(st_s)
                    m_st_part = re.match(
                        r"^(\d+(?:[.,]\d+)?)\s*(m|min|s)?\s*(?:@\s*(\d+(?:[.,]\d+)?\s*[wW%]?))?$",
                        st_clean, re.I
                    )
                    if m_st_part:
                        s_sec = _dur_sec(m_st_part.group(1), m_st_part.group(2) or "m")
                        s_p = _norm_pwr(m_st_part.group(3), 1.15, ftp_watts=ftp_watts)
                        if s_sec > 0:
                            stg_d = {"sec": s_sec, "p": s_p}
                            if st_cl is not None:
                                stg_d["cad_low"], stg_d["cad_high"] = st_cl, st_ch
                            stages.append(stg_d)
                if stages:
                    off_sec = _dur_sec(m_cmp.group(3), m_cmp.group(4) or "m") if m_cmp.group(3) else 240
                    off_p = _norm_pwr(m_cmp.group(5), 0.50, ftp_watts=ftp_watts)
                    off_cl, off_ch = _norm_cad(m_cmp.group(6))
                    stg_lbl = "+".join(f"{st['sec']}s@{int(round(st['p']*100))}%" for st in stages)
                    seg_cmp = {
                        "type": "compound_intervals", "repeat": rep, "stages": stages,
                        "off_sec": off_sec, "off_p": off_p,
                        "label": f"{rep}× ({stg_lbl}) / {off_sec // 60}m@{int(round(off_p * 100))}%",
                    }
                    if off_cl is not None:
                        seg_cmp["off_cad_low"], seg_cmp["off_cad_high"] = off_cl, off_ch
                    segments.append(seg_cmp)
                    continue

            # 1. Warmup: `15m WU 55-75%` or `12m WU 52-66% @88rpm`
            tok_nocad, tok_cl, tok_ch = _strip_cad_suffix(tok)
            m_wu = re.match(
                r"^(\d+(?:[.,]\d+)?)\s*(m|min|s|h)?\s*(?:wu|warm-?up)\s*(?:@\s*)?(\d+(?:[.,]\d+)?\s*[wW%]?)?\s*(?:[-–>to/]+\s*(\d+(?:[.,]\d+)?\s*[wW%]?))?$",
                tok_nocad, re.I
            )
            if m_wu:
                sec = _dur_sec(m_wu.group(1), m_wu.group(2) or "m")
                p_low = _norm_pwr(m_wu.group(3), 0.50, ftp_watts=ftp_watts)
                p_high = _norm_pwr(m_wu.group(4), 0.75, ftp_watts=ftp_watts)
                cad_s = f" · {_fmt_cad(tok_cl, tok_ch)}" if tok_cl is not None else ""
                seg = {"type": "warmup", "sec": sec, "p_low": p_low, "p_high": p_high,
                       "label": f"{sec // 60}m WU {int(round(p_low * 100))}–{int(round(p_high * 100))}%{cad_s}"}
                if tok_cl is not None:
                    seg["cad_low"], seg["cad_high"] = tok_cl, tok_ch
                segments.append(seg)
                continue

            # 2. Cooldown: `10m CD 65-50%` or `8m CD 62-48% @85rpm`
            m_cd = re.match(
                r"^(\d+(?:[.,]\d+)?)\s*(m|min|s|h)?\s*(?:cd|cool-?down)\s*(?:@\s*)?(\d+(?:[.,]\d+)?\s*[wW%]?)?\s*(?:[-–>to/]+\s*(\d+(?:[.,]\d+)?\s*[wW%]?))?$",
                tok_nocad, re.I
            )
            if m_cd:
                sec = _dur_sec(m_cd.group(1), m_cd.group(2) or "m")
                p_low = _norm_pwr(m_cd.group(3), 0.65, ftp_watts=ftp_watts)
                p_high = _norm_pwr(m_cd.group(4), 0.50, ftp_watts=ftp_watts)
                cad_s = f" · {_fmt_cad(tok_cl, tok_ch)}" if tok_cl is not None else ""
                seg = {"type": "cooldown", "sec": sec, "p_low": p_low, "p_high": p_high,
                       "label": f"{sec // 60}m CD {int(round(p_low * 100))}–{int(round(p_high * 100))}%{cad_s}"}
                if tok_cl is not None:
                    seg["cad_low"], seg["cad_high"] = tok_cl, tok_ch
                segments.append(seg)
                continue

            # 3. Intervals: `4x 8m@102% / 4m@55%`, `4x 30s@225W@105-115rpm / 90s@175W@85-90rpm`
            m_iv = re.match(
                r"^(\d+)\s*[x×]\s*(\d+(?:[.,]\d+)?)\s*(m|min|s)?\s*(?:@\s*(\d+(?:[.,]\d+)?\s*[wW%]?))?" + _cad_opt
                + r"(?:\s*(?:/|\(|with|rest|pause)\s*(\d+(?:[.,]\d+)?)\s*(m|min|s)?\s*(?:@\s*(\d+(?:[.,]\d+)?\s*[wW%]?))?" + _cad_opt + r"\s*\)?)?$",
                tok, re.I
            )
            if m_iv:
                rep = int(m_iv.group(1))
                on_sec = _dur_sec(m_iv.group(2), m_iv.group(3) or "m")
                on_p = _norm_pwr(m_iv.group(4), 0.95 if on_sec >= 480 else 1.10, ftp_watts=ftp_watts)
                on_cl, on_ch = _norm_cad(m_iv.group(5))
                off_sec = _dur_sec(m_iv.group(6), m_iv.group(7) or "m") if m_iv.group(6) else max(60, int(round(on_sec * 0.5)))
                off_p = _norm_pwr(m_iv.group(8), 0.55, ftp_watts=ftp_watts)
                off_cl, off_ch = _norm_cad(m_iv.group(9))
                seg_iv = {
                    "type": "intervals", "repeat": rep, "on_sec": on_sec, "off_sec": off_sec,
                    "on_p": on_p, "off_p": off_p,
                    "label": f"{rep}× {on_sec // 60 if on_sec >= 60 else str(on_sec) + 's'}"
                             f"{'m' if on_sec >= 60 else ''} @ {int(round(on_p * 100))}% / "
                             f"{off_sec // 60 if off_sec >= 60 else str(off_sec) + 's'}"
                             f"{'m' if off_sec >= 60 else ''} @ {int(round(off_p * 100))}%"
                }
                if on_cl is not None:
                    seg_iv["on_cad_low"], seg_iv["on_cad_high"] = on_cl, on_ch
                if off_cl is not None:
                    seg_iv["off_cad_low"], seg_iv["off_cad_high"] = off_cl, off_ch
                segments.append(seg_iv)
                continue

            # 4. Ramp: `12m ramp 70-95%` or `12m ramp 70-95% @90-95rpm`
            m_rmp = re.match(
                r"^(\d+(?:[.,]\d+)?)\s*(m|min|s)?\s*ramp\s*(?:@\s*)?(\d+(?:[.,]\d+)?\s*[wW%]?)\s*[-–>to/]+\s*(\d+(?:[.,]\d+)?\s*[wW%]?)$",
                tok_nocad, re.I
            )
            if m_rmp:
                sec = _dur_sec(m_rmp.group(1), m_rmp.group(2) or "m")
                p_low = _norm_pwr(m_rmp.group(3), 0.65, ftp_watts=ftp_watts)
                p_high = _norm_pwr(m_rmp.group(4), 0.95, ftp_watts=ftp_watts)
                cad_s = f" · {_fmt_cad(tok_cl, tok_ch)}" if tok_cl is not None else ""
                seg = {"type": "ramp", "sec": sec, "p_low": p_low, "p_high": p_high,
                       "label": f"{sec // 60}m Ramp {int(round(p_low * 100))}–{int(round(p_high * 100))}%{cad_s}"}
                if tok_cl is not None:
                    seg["cad_low"], seg["cad_high"] = tok_cl, tok_ch
                segments.append(seg)
                continue

            # 5. FreeRide: `15m free` or `15m free @90rpm`
            m_fr = re.match(r"^(\d+(?:[.,]\d+)?)\s*(m|min|s|h)?\s*(?:free|freeride)$", tok_nocad, re.I)
            if m_fr:
                sec = _dur_sec(m_fr.group(1), m_fr.group(2) or "m")
                cad_s = f" · {_fmt_cad(tok_cl, tok_ch)}" if tok_cl is not None else ""
                seg = {"type": "freeride", "sec": sec, "label": f"{sec // 60}m FreeRide{cad_s}"}
                if tok_cl is not None:
                    seg["cad_low"], seg["cad_high"] = tok_cl, tok_ch
                segments.append(seg)
                continue

            # 6. SteadyState: `19m Z2@68%` or `45m @ 215W @85-90rpm` or `8m@235W 65-70rpm`
            m_st = re.match(
                r"^(\d+(?:[.,]\d+)?)\s*(m|min|s|h)?\s*(?:[A-Za-z0-9_-]+\s*)?(?:@\s*(\d+(?:[.,]\d+)?\s*[wW%]?))?$",
                tok_nocad, re.I
            )
            if m_st:
                sec = _dur_sec(m_st.group(1), m_st.group(2) or "m")
                low_tok = tok_nocad.lower()
                def_p = 0.52 if "z1" in low_tok else (0.88 if ("z3" in low_tok or "ss" in low_tok or "tempo" in low_tok)
                        else (1.00 if "z4" in low_tok else (1.12 if "z5" in low_tok else 0.68)))
                p = _norm_pwr(m_st.group(3), def_p, ftp_watts=ftp_watts)
                cad_s = f" · {_fmt_cad(tok_cl, tok_ch)}" if tok_cl is not None else ""
                seg = {"type": "steady", "sec": sec, "p": p,
                       "label": f"{sec // 60}m @ {int(round(p * 100))}%{cad_s}"}
                if tok_cl is not None:
                    seg["cad_low"], seg["cad_high"] = tok_cl, tok_ch
                segments.append(seg)
                continue

            # Track unrecognized DSL tokens so `coach simulate` & `coach push` never silently drop interval blocks!
            unparsed_tokens.append(tok)

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
            vb = {"min": round(seg["sec"] / 60.0, 1), "pct": int(round(mid_p * 100)),
                  "zone": _zone_class(mid_p), "label": seg["label"]}
            if seg.get("cad_low") is not None:
                vb["cadence"] = _fmt_cad(seg["cad_low"], seg.get("cad_high"))
            visual_blocks.append(vb)
        elif st == "intervals":
            on_cs = _fmt_cad(seg.get("on_cad_low"), seg.get("on_cad_high"))
            off_cs = _fmt_cad(seg.get("off_cad_low"), seg.get("off_cad_high"))
            for _ in range(seg["repeat"]):
                calc_sec += seg["on_sec"] + seg["off_sec"]
                vb_on = {"min": round(seg["on_sec"] / 60.0, 1), "pct": int(round(seg["on_p"] * 100)),
                         "zone": _zone_class(seg["on_p"]),
                         "label": f"ON {int(round(seg['on_p'] * 100))}%{(' · ' + on_cs) if on_cs else ''}"}
                if on_cs:
                    vb_on["cadence"] = on_cs
                visual_blocks.append(vb_on)
                vb_off = {"min": round(seg["off_sec"] / 60.0, 1), "pct": int(round(seg["off_p"] * 100)),
                          "zone": _zone_class(seg["off_p"]),
                          "label": f"OFF {int(round(seg['off_p'] * 100))}%{(' · ' + off_cs) if off_cs else ''}"}
                if off_cs:
                    vb_off["cadence"] = off_cs
                visual_blocks.append(vb_off)
        elif st == "compound_intervals":
            off_cs = _fmt_cad(seg.get("off_cad_low"), seg.get("off_cad_high"))
            for r_i in range(seg["repeat"]):
                for stg in seg.get("stages") or []:
                    calc_sec += stg["sec"]
                    stg_cs = _fmt_cad(stg.get("cad_low"), stg.get("cad_high"))
                    vb_s = {"min": round(stg["sec"] / 60.0, 1), "pct": int(round(stg["p"] * 100)),
                            "zone": _zone_class(stg["p"]),
                            "label": f"ON {int(round(stg['p'] * 100))}%{(' · ' + stg_cs) if stg_cs else ''}"}
                    if stg_cs:
                        vb_s["cadence"] = stg_cs
                    visual_blocks.append(vb_s)
                if seg.get("off_sec") and r_i < seg["repeat"] - 1:
                    calc_sec += seg["off_sec"]
                    vb_o = {"min": round(seg["off_sec"] / 60.0, 1), "pct": int(round(seg["off_p"] * 100)),
                            "zone": _zone_class(seg["off_p"]),
                            "label": f"OFF {int(round(seg['off_p'] * 100))}%{(' · ' + off_cs) if off_cs else ''}"}
                    if off_cs:
                        vb_o["cadence"] = off_cs
                    visual_blocks.append(vb_o)
        elif st == "freeride":
            calc_sec += seg["sec"]
            vb = {"min": round(seg["sec"] / 60.0, 1), "pct": 68, "zone": "z2", "label": seg["label"]}
            if seg.get("cad_low") is not None:
                vb["cadence"] = _fmt_cad(seg["cad_low"], seg.get("cad_high"))
            visual_blocks.append(vb)
        else:
            calc_sec += seg["sec"]
            vb = {"min": round(seg["sec"] / 60.0, 1), "pct": int(round(seg["p"] * 100)),
                  "zone": _zone_class(seg["p"]), "label": seg["label"]}
            if seg.get("cad_low") is not None:
                vb["cadence"] = _fmt_cad(seg["cad_low"], seg.get("cad_high"))
            visual_blocks.append(vb)

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
        "unparsed_tokens": unparsed_tokens,
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
        cad_attr = _cad_xml_attrs(s.get("cad_low"), s.get("cad_high"))
        if t == "warmup":
            lines.append(f'    <Warmup Duration="{int(s["sec"])}" PowerLow="{s["p_low"]:.2f}" PowerHigh="{s["p_high"]:.2f}"{cad_attr}/>')
        elif t == "cooldown":
            lines.append(f'    <Cooldown Duration="{int(s["sec"])}" PowerLow="{s["p_low"]:.2f}" PowerHigh="{s["p_high"]:.2f}"{cad_attr}/>')
        elif t == "ramp":
            lines.append(f'    <Ramp Duration="{int(s["sec"])}" PowerLow="{s["p_low"]:.2f}" PowerHigh="{s["p_high"]:.2f}"{cad_attr}/>')
        elif t == "intervals":
            on_cl, on_ch = s.get("on_cad_low"), s.get("on_cad_high")
            off_cl, off_ch = s.get("off_cad_low"), s.get("off_cad_high")
            if on_cl is None and off_cl is None:
                lines.append(
                    f'    <IntervalsT Repeat="{int(s["repeat"])}" OnDuration="{int(s["on_sec"])}" '
                    f'OffDuration="{int(s["off_sec"])}" OnPower="{s["on_p"]:.2f}" OffPower="{s["off_p"]:.2f}"/>'
                )
            elif on_cl is not None and on_cl == on_ch and off_cl == on_cl and off_ch == on_ch:
                lines.append(
                    f'    <IntervalsT Repeat="{int(s["repeat"])}" OnDuration="{int(s["on_sec"])}" '
                    f'OffDuration="{int(s["off_sec"])}" OnPower="{s["on_p"]:.2f}" OffPower="{s["off_p"]:.2f}" Cadence="{int(on_cl)}"/>'
                )
            else:
                # Unroll into explicit <SteadyState> pairs when On/Off cadence targets differ or use ranges:
                # Hammerhead Karoo's ZWO importer ignores `CadenceResting` on `<IntervalsT>` and copies `Cadence`
                # onto the recovery step, whereas `<SteadyState>` renders exact per-step `Cadence` / `CadenceLow`/`CadenceHigh`.
                on_ca = _cad_xml_attrs(on_cl, on_ch)
                off_ca = _cad_xml_attrs(off_cl, off_ch)
                for _ in range(int(s["repeat"])):
                    lines.append(f'    <SteadyState Duration="{int(s["on_sec"])}" Power="{s["on_p"]:.2f}"{on_ca}/>')
                    lines.append(f'    <SteadyState Duration="{int(s["off_sec"])}" Power="{s["off_p"]:.2f}"{off_ca}/>')
        elif t == "compound_intervals":
            off_ca = _cad_xml_attrs(s.get("off_cad_low"), s.get("off_cad_high"))
            for r_i in range(int(s["repeat"])):
                for stg in s.get("stages") or []:
                    stg_ca = _cad_xml_attrs(stg.get("cad_low"), stg.get("cad_high"))
                    lines.append(f'    <SteadyState Duration="{int(stg["sec"])}" Power="{stg["p"]:.2f}"{stg_ca}/>')
                if s.get("off_sec") and r_i < int(s["repeat"]) - 1:
                    lines.append(f'    <SteadyState Duration="{int(s["off_sec"])}" Power="{s["off_p"]:.2f}"{off_ca}/>')
        elif t == "freeride":
            lines.append(f'    <FreeRide Duration="{int(s["sec"])}" FlatRoad="1"{cad_attr}/>')
        else:
            lines.append(f'    <SteadyState Duration="{int(s["sec"])}" Power="{s["p"]:.2f}"{cad_attr}/>')
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
    dna_txt = ""
    dna_full = os.path.join(BRAIN, "coach", "dna.md")
    if os.path.isfile(dna_full):
        try:
            with open(dna_full, encoding="utf-8", errors="replace") as fh:
                dna_txt = fh.read(HEAD_BYTES)
        except OSError:
            pass
    pb_txt = ""
    pb_full = os.path.join(BRAIN, "coach", "playbook.md")
    if os.path.isfile(pb_full):
        try:
            with open(pb_full, encoding="utf-8", errors="replace") as fh:
                pb_txt = fh.read(HEAD_BYTES)
        except OSError:
            pass
    bio_store = health_engine.load_biometrics_store(BRAIN)
    try:
        tel = coach_engine.build_coach_telemetry(
            profile_text=prof_txt_full,
            activities=c_state["activities"],
            readiness_text=rdy_txt,
            cal_items=cal_items,
            strava_connected=c_state["strava_connected"],
            fallback_ftp=ftp_watts,
            dna_text=dna_txt,
            playbook_text=pb_txt,
            macro_info=macro,
            biometrics_store=bio_store,
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
        "connectors": coach_connectors.connector_status_summary(BRAIN),
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


def actionable_tasks(idx=None, include_upcoming_days=0):
    """Return priority-ordered actionable tasks (overdue & due-today `[todo]`/`[deadline]` + undated `open_todos`)."""
    if idx is None:
        idx = load_index(budget=INDEX_BUDGET_S)
    out = []
    dated = calendar_items(idx, days=max(0, int(include_upcoming_days or 0)), past_days=3650, include_done=False)
    for it in dated:
        if it["kind"] not in ("todo", "deadline"):
            continue
        delta = it["delta"]
        if delta < 0:
            prio, badge = "overdue", f"OVERDUE {-delta} d · {it['start'].isoformat()}"
        elif delta == 0:
            prio, badge = "today", f"DUE TODAY · {it['start'].isoformat()}"
        else:
            prio, badge = "scheduled", f"in {delta} d · {it['start'].isoformat()}"
        what_clean = _clean_md_inline(it["what"])
        out.append({
            "source": "due_fm" if int(it.get("line") or 0) == 0 else "dated",
            "kind": it["kind"],
            "prio": prio,
            "badge": badge,
            "date": it["start"].isoformat(),
            "delta": delta,
            "what": what_clean,
            "rel": it["rel"],
            "line": int(it.get("line") or 0),
            "can_toggle": bool(it.get("can_toggle", False)),
        })

    for td in open_todos(include_done=False):
        w_clean = td["what"]
        t_new = _cal_tokens(w_clean)
        dup = False
        for ex in out:
            if ex["what"].lower() == w_clean.lower():
                dup = True
                break
            t_ex = _cal_tokens(ex["what"])
            if t_new and t_ex:
                overlap = len(t_new & t_ex)
                if td["rel"] == "calendar/README.md" and ex["rel"] == "calendar/README.md":
                    if overlap >= 3 and overlap / max(len(t_new), len(t_ex), 1) >= 0.85:
                        dup = True
                        break
                elif overlap >= 2 and overlap / max(min(len(t_new), len(t_ex)), 1) >= 0.6:
                    dup = True
                    break
        if dup:
            continue
        out.append({
            "source": "undated",
            "kind": "todo",
            "prio": td["prio"],
            "badge": f"OPEN · {td['prio']}",
            "date": "",
            "delta": None,
            "what": w_clean,
            "rel": td["rel"],
            "line": int(td.get("line") or 0),
            "can_toggle": bool(td.get("can_toggle", False)),
        })

    for i, item in enumerate(out, 1):
        item["num"] = i
    return out


CAL_TEMPLATE = cal_engine.load_calendar_template()
CAL_SERVER_SCRIPT = cal_engine.CAL_SERVER_SCRIPT


def _cal_http_ok(port):
    """Verify the calendar server returns HTTP 200 on `/healthz` (`atlas-cal-v7`)."""
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
    all_items = calendar_items(idx, days=3650, past_days=3650, include_done=True)
    # Always keep ALL open items (even >90d overdue or >365d future deadlines); cap completed history to [-90d, +365d]
    raw_items = [it for it in all_items if it["status"] == "open" or (-90 <= it["delta"] <= 365)]
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
            for k_act in ("np_w", "avg_hr", "tss", "w_bal_min_pct", "matches_burned", "cho_burn_g", "execution"):
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

    try:
        sync_meta_map = cal_sync.load_event_metadata_map(BRAIN)
        cal_sync_summary = cal_sync.get_sync_ui_summary(BRAIN)
    except Exception:
        sync_meta_map = {}
        cal_sync_summary = {"configured": False, "enabled": False, "accounts": [], "layers": [], "open_conflicts": 0, "auth_warnings": []}

    layer_cfg_by_file = {ly["file"]: ly for ly in (cal_sync_summary.get("layers") or []) if ly.get("file")}
    for it in serial:
        ly_cfg = layer_cfg_by_file.get(it.get("rel", ""))
        if ly_cfg:
            it["layer"] = ly_cfg["id"]
            it["layer_label"] = ly_cfg["label"]
            it["layer_color"] = ly_cfg["color"]
            it["layer_mode"] = ly_cfg["mode"]
        uid = it.get("sync_id") or ""
        meta = sync_meta_map.get(uid) if uid else None
        if meta:
            it["synced"] = True
            for mk in ("location", "meet_link", "description", "attendees", "self_rsvp", "organizer", "account_id", "html_link"):
                if meta.get(mk):
                    it[mk] = meta[mk]

    payload = {
        "today": dt.date.today().isoformat(),
        "csrf_token": csrf_tok,
        "items": serial,
        "todos": todos,
        "coach": coach,
        "graph": graph_data,
        "cal_sync": cal_sync_summary,
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


def ensure_calendar_ready(idx=None, start_server=False):
    """Render `CAL_HTML` only when stale/missing or on day rollover; always ensure loopback server when requested."""
    if _calendar_needs_render():
        return render_calendar_html(idx=idx, start_server=start_server)
    return _ensure_calendar_server(os.path.dirname(CAL_HTML), start_server=start_server)


def session_warmup(cwd, session):
    """Warm up git remote, SQLite index & loopback calendar server on Antigravity `SessionStart` without burning daily state."""
    st = _state()
    try:
        msg = pull()
    except Exception as exc:
        _log_hook_error("warmup.pull", exc)
        msg = ""
    if msg:
        prev = st.get("pending_pull_msg", "")
        st["pending_pull_msg"] = (prev + "\n" + msg).strip() if prev and msg not in prev else (msg or prev)
    idx = load_index(budget=INDEX_BUDGET_S)
    try:
        conn_w = coach_connectors.connector_status_summary(BRAIN)
        if conn_w.get("strava_connected") or conn_w.get("garmin_connected") or conn_w.get("google_health_connected"):
            last_s = st.get("last_strava_sync")
            age_m = ((dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(last_s)).total_seconds() / 60.0) if last_s else 9999
            if age_m >= 60:
                atlas_py = os.path.join(_TOOLS_DIR, "atlas.py")
                subprocess.run(
                    [sys.executable, "-I", "-B", atlas_py, "--root", _root(), "coach", "sync", "--days", "14", "--execute"],
                    capture_output=True, text=True, timeout=8,
                )
                st = _state()
    except Exception as exc:
        _log_hook_error("warmup.coach_sync", exc)
    try:
        ensure_calendar_ready(idx, start_server=True)
    except Exception as exc:
        _log_hook_error("warmup.render_calendar", exc)
    if session:
        _session(st, session)["warmed_at"] = _now_iso()
    st["last_hook_run"] = _now_iso()
    _save_state(st)
    _sync_antigravity_router(st)
    sys.stdout.write("{}")


def session_text(cwd, session, skip_pull=False):
    s = settings()
    st = _state()
    lines = ["Atlas · ok"]
    fresh_err = _recent_hook_error(st)
    if fresh_err:
        err_iso, err_sum = fresh_err
        lines.append(f"Hook warning (recent logged error in _atlas/cache/hook_errors.log): {err_sum}")
        if not PROBE:
            st["last_error_notified"] = err_iso
    if PROBE:
        msg = ""
    elif skip_pull:
        msg = st.pop("pending_pull_msg", "")
    else:
        try:
            msg = pull()
        except Exception as exc:
            _log_hook_error("session.pull", exc)
            msg = ""
        pend_msg = st.pop("pending_pull_msg", "")
        if pend_msg and pend_msg not in msg:
            msg = (pend_msg + "\n" + msg).strip()
    if msg:
        lines.append(msg)
    idx = load_index(budget=INDEX_BUDGET_S)
    today = dt.date.today().isoformat()
    horizon_days = _int(s.get("deadline_days"), 30)
    due = deadlines(idx, days=horizon_days)
    mode = s.get("briefing", "daily")
    last = st.get("last_briefing", "")
    brief = (mode == "daily" and last != today) or (mode == "weekly" and (not last or (
        dt.date.today() - dt.date.fromisoformat(last)).days >= 7))
    if brief:
        st["last_briefing"] = today
        parts = []
        tasks = actionable_tasks(idx, include_upcoming_days=0)
        shown_task_keys = set()
        if tasks:
            top_tasks = tasks[:5]
            t_lines = []
            for t in top_tasks:
                shown_task_keys.add((t["date"], t["what"].lower()))
                loc = f"{_show(t['rel'], cwd)}:{t['line']}" if t["line"] else _show(t["rel"], cwd)
                k_tag = "[deadline] " if t["kind"] == "deadline" else ""
                t_lines.append(f"  {t['num']}. [{t['badge']}] {k_tag}{t['what']}  [{loc}]")
            extra_note = f" (+{len(tasks) - len(top_tasks)} more open)" if len(tasks) > len(top_tasks) else ""
            tools_py = _show("_atlas/tools/atlas.py", cwd)
            py = _py_cmd()
            parts.append(
                f"Task check-in{extra_note} (once today — list numbered in the user's language and offer quick triage: "
                f"check off [abhaken/done], reschedule [verschieben/move], or tackle now [jetzt angehen]; "
                f"apply replies via `{py} -I {tools_py} todo done|move|skip ...`):\n"
                + "\n".join(t_lines)
            )
        cal_upcoming = []
        for when, delta, what, rel in due:
            if delta < 0:
                continue
            clean_w = _clean_md_inline(re.sub(r"^(?:\([^)]+\)\s+)?(?:\d{1,2}:\d{2}(?:-\d{1,2}:\d{2})?\s+)?(?:\[[a-zA-Z0-9_:-]+\]\s+)?", "", what))
            if delta == 0 and (when.isoformat(), clean_w.lower()) in shown_task_keys:
                continue
            tag = "today" if delta == 0 else f"in {delta} d"
            cal_upcoming.append(f"- {when.isoformat()} ({tag}): {what or '(no note)'}  [{_show(rel, cwd)}]")
            if len(cal_upcoming) >= 6:
                break
        if cal_upcoming:
            parts.append("Calendar & deadlines:\n" + "\n".join(cal_upcoming))
        if not PROBE:
            try:
                cal_uri = ensure_calendar_ready(idx, start_server=True)
                parts.append(f"Visual calendar: [📅 Kalender öffnen]({cal_uri}) (include this clickable link in the briefing)")
            except Exception as exc:
                _log_hook_error("session.render_calendar", exc)
        try:
            if "coach" in st.get("modules", []) or os.path.isdir(os.path.join(BRAIN, "coach")):
                c_state = load_coach_state()
                c_cal = calendar_items(idx, days=7, past_days=3, include_done=True)
                today_wo = [w for w in c_cal if w.get("kind") == "workout" and w.get("status") == "open" and w.get("delta") == 0]
                missed_wo = [w for w in c_cal if w.get("kind") == "workout" and w.get("status") == "open" and -3 <= w.get("delta", 0) < 0]
                coach_bits = []
                if today_wo:
                    coach_bits.append(f"Today's workout: {today_wo[0].get('what', 'Session')}")
                if missed_wo:
                    m0 = missed_wo[-1]
                    coach_bits.append(f"Missed/unmarked workout on {m0['start'].isoformat()} ('{m0.get('what', 'Workout')}') — ask why & adapt")
                pend = c_state.get("pending_feedback", [])
                if pend:
                    p0 = pend[0]
                    np_bit = f", NP {p0.get('np_w')}W" if p0.get("np_w") else ""
                    coach_bits.append(f"Session '{p0.get('name', 'Workout')}' on {p0.get('date')} ({p0.get('duration_min', 0)}m{np_bit}) has no feedback note yet -> ask 1 short question & save via `atlas.py coach note --date {p0.get('date')} --text \"...\"`")
                gh_st = coach_connectors.google_health_token_status(BRAIN)
                if gh_st.get("needs_renew"):
                    coach_bits.append("Google Health Token erneuern (`atlas.py coach auth-google-health --renew`)")
                if coach_bits:
                    parts.append("Coach pulse: " + " | ".join(coach_bits) + ".")
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
        lines.append("Atlas briefing (first chat today): give a concise daily briefing (with the numbered task check-in "
                     "if tasks are open), then handle the user's request right away."
                     + ("\n" + "\n".join(parts) if parts else "\nNothing pending; say so in one line."))
    else:
        if not PROBE:
            try:
                ensure_calendar_ready(idx, start_server=True)
            except Exception as exc:
                _log_hook_error("session.render_calendar", exc)
        urgent = [d for d in due if d[1] <= 0]
        if urgent:
            more_u = f" (+{len(urgent) - 5} more — run `atlas.py todo`)" if len(urgent) > 5 else ""
            lines.append("Due today/overdue (mention if due today, relevant, or when the user only sends a bare greeting like 'moin'): " + "; ".join(
                f"{w.isoformat()} {what} [{_show(rel, cwd)}]" for w, _d, what, rel in urgent[:5]) + more_u)
    hour = dt.datetime.now().hour
    if s.get("questions") == "evening-3" and hour >= _int(s.get("evening_hour"), 18) and st.get("last_evening") != today:
        st["last_evening"] = today
        lines.append("Evening (settings): offer the user three short diary questions; keep the answers in "
                     f"journal/{today[:4]}/{today}.md and anything lasting on its page.")
    if s.get("questions") == "weekly-session" and (_age_days(st.get("last_weekly", "1970-01-01T00:00:00+00:00"))
                                                   or 99) >= 7:
        st["last_weekly"] = _now_iso()
        lines.append("Weekly session (settings): offer a short /atlas-interview round today.")
    if session:
        _session(st, session)["session_injected"] = today
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

_GENERIC_STEM_HEADS = {
    "allgemein", "alltag", "analyse", "archiv", "archive", "area", "bereich", "business", "businessplan",
    "checkliste", "coaching", "custom", "datums", "der", "die", "das", "dokumente", "dossier", "entscheidungsanalyse",
    "experimente", "files", "financial", "finanzen", "fuhrpark", "funktionsspezifikation", "gaming", "gemeinsam",
    "geometrie", "gesundheit", "gruendungsleitfaden", "guide", "hausrat", "howto", "hund", "ideale", "ideen",
    "index", "insights", "karriere", "kognitive", "konten", "laufende", "laufwerke", "leitfaden", "leistungsnachweis",
    "master", "masterplan", "mentale", "mental", "misc", "model", "modelle", "muster", "notes", "offene",
    "optimierung", "overview", "people", "peripherie", "person", "persoenlicher", "philosophie", "physiologie",
    "plan", "planung", "playbook", "privat", "project", "projects", "projekt", "projekte", "protokoll",
    "psychometrie", "psychometrisches", "radsport", "readme", "rechnungsaufstellung", "referenzen", "regeln",
    "reibung", "report", "roadmap", "router", "settings", "simulation", "sonstiges", "spiele", "steuern",
    "strategische", "system", "tagesrhythmus", "technik", "template", "training", "uebersicht", "umfassende",
    "unterlagen", "unternehmertum", "vergleich", "versicherungen", "vertraege", "vorlage", "werte", "wissen",
    "wissensdatenbank", "wohnen", "wohnung", "zeitformate", "ziele",
}


def _py_cmd():
    if os.name != "nt" and shutil.which("python3") and not shutil.which("python"):
        return "python3"
    return "python"


def _vocab(idx, skip_pages):
    terms = {}
    derived = {}  # norm_term -> list of (rel, raw_term)

    def _add_derived(raw_t, dst_rel):
        cleaned = re.sub(r"\s+", " ", str(raw_t or "")).strip(" -–—:·,;/|")
        nl = graph_engine.norm_text(cleaned).strip()
        if not nl or len(nl) < 4 or len(nl) > 60:
            return
        if nl in graph_engine.PROMPT_STOPWORDS or nl in _GENERIC_STEM_HEADS:
            return
        if re.match(r"^(?:\d+|\d{4}-\d{2}-\d{2})$", nl):
            return
        derived.setdefault(nl, []).append((dst_rel, cleaned))

    for rel, e in idx.items():
        if rel in skip_pages or not graph_engine.is_user_knowledge_rel(rel):
            continue
        if "/files/" in rel:
            continue
        title = e.get("title", "")
        is_hub = rel.endswith("README.md") and not (rel.startswith("projects/") and rel.count("/") == 2)
        if 3 <= len(title) <= 70 and not is_hub:
            terms.setdefault(title, rel)
        if not is_hub:
            # Clean title without parentheticals or dash subtitles (e.g. "VeloClub e.V. (Charter)" -> "VeloClub e.V.")
            base_title = re.sub(r"^[^\wA-Za-zÄÖÜäöüß]+", "", title)
            base_title = re.sub(r"(?i)^(?:project|projekt|person|area|bereich)\s*:\s*", "", base_title).strip()
            base_title = re.split(r"\s+[—–]\s+|\s+-\s+(?=[A-ZÄÖÜ])", base_title)[0]
            base_title = re.sub(r"\s*\([^)]*\)\s*", " ", base_title)
            base_title = re.sub(r"\s+", " ", base_title).strip()
            if 4 <= len(base_title) <= 60 and base_title != title:
                _add_derived(base_title, rel)
            # Strip legal suffixes (e.g. "VeloClub e.V." -> "VeloClub")
            org_base = re.sub(r"(?i)\s+(?:e\.?\s*v\.?|gmbh|gbr|ag|ug|se|kg|inc\.?|llc)$", "", base_title).strip()
            if 4 <= len(org_base) <= 50 and org_base != base_title:
                _add_derived(org_base, rel)
            # Sub-phrases split by `: `, ` & `, ` / `, ` · ` (e.g. "Business Plan: vello.io", "Solar Roof & Battery")
            for part in re.split(r"\s*(?::|&|\+|/|·)\s*", base_title):
                part = part.strip()
                p_words = [graph_engine.norm_text(w) for w in re.findall(r"[\w.-]+", part)]
                if part and not all(w in _GENERIC_STEM_HEADS or w in graph_engine.PROMPT_STOPWORDS for w in p_words):
                    _add_derived(part, rel)
            # Stem & stem prefixes (e.g. "trek-madone-sl7" -> "trek madone", "hund-planung-pudel" -> "pudel")
            raw_stem = os.path.splitext(os.path.basename(rel))[0]
            stem = raw_stem.replace("-", " ").replace("_", " ").strip()
            if 4 <= len(stem) <= 60 and stem.lower() not in ("index", "readme") and not re.match(r"^\d{4}", stem):
                terms.setdefault(stem, rel)
                for chunk in re.split(r"\s+(?:und|and|vs|oder|mit)\s+", stem, flags=re.IGNORECASE):
                    c_words = [graph_engine.norm_text(w) for w in chunk.split() if w]
                    if c_words and not all(w in _GENERIC_STEM_HEADS or w in graph_engine.PROMPT_STOPWORDS for w in c_words):
                        _add_derived(chunk, rel)
                s_words = stem.split()
                if len(s_words) >= 3 and graph_engine.norm_text(s_words[0]) not in _GENERIC_STEM_HEADS and graph_engine.norm_text(s_words[0]) not in graph_engine.PROMPT_STOPWORDS:
                    if graph_engine.norm_text(s_words[1]) not in ("und", "and", "mit", "fuer", "fur", "von", "im", "in"):
                        _add_derived(f"{s_words[0]} {s_words[1]}", rel)
                for sw in s_words:
                    nsw = graph_engine.norm_text(sw)
                    if len(nsw) >= 6 and nsw.isalpha() and nsw not in _GENERIC_STEM_HEADS and nsw not in graph_engine.PROMPT_STOPWORDS:
                        _add_derived(sw, rel)
        if rel.startswith("people/") and title:
            words = title.split()
            if len(words) >= 2 and len(words[-1]) >= 4:
                terms.setdefault(words[-1], rel)
            if words and len(words[0]) >= 3:
                terms.setdefault(words[0], rel)
        for a in e.get("aliases", []):
            terms.setdefault(a, rel)

    existing_norm = {graph_engine.norm_text(k).strip() for k in terms}
    for nl, entries in derived.items():
        if nl in existing_norm:
            continue
        rels = sorted({r for r, _raw in entries}, key=lambda r: (len(r), r))
        # Accept if unambiguous, or if all candidate pages live in the exact same parent folder (pick shortest canonical path)
        parents = {os.path.dirname(r) for r in rels}
        if len(rels) == 1 or len(parents) == 1:
            chosen_rel = rels[0]
            disp = next(raw for r, raw in entries if r == chosen_rel)
            terms.setdefault(disp, chosen_rel)
            existing_norm.add(nl)
    for t in idx.get("glossary.md", {}).get("glossary", []):
        if graph_engine.norm_text(t).strip() not in existing_norm:
            terms.setdefault(t, "glossary.md")
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
    r"trainingsplan\w*|trainingswoche\w*|workouts?\s+(?:planen|erstellen|schreiben|bauen)|wochenplan\s+training|"
    r"(?:vo2max|schwellen|sweet\s*spot|grundlagen|ga[12]|over[\s-]*under|cadence|torque|intervall\w*)[-\s]*(?:einheit|workout|session))\b"
)
COACH_RX_DNA = re.compile(
    r"(?i)\b(?:rider\s*dna|athlete\s*dna|athleten-?dna|dna\s*(?:profil|analyse|report|scouting)|scouting\s*report|"
    r"wo\s+stehe\s+ich\s+leistungsm[aä][sß]ig|was\s+ist\s+mein\s+limiter|mein\s+limiter|formanalyse|leistungsprofil\s+analys\w*|"
    r"formkurve\w*|leistungsentwicklung|leistungskurve|saisonr[uü]ckblick|power\s*curve)\b"
)
COACH_RX_CHECKIN = re.compile(
    r"(?i)\b(?:rpe\s*\d|beine\s+(?:waren|sind|schwer|wie\s+beton|leer|frisch)|puls\s+war|"
    r"(?:ruhepuls|rhr|hrv|rmssd|kfa|k[oö]rperfett\w*|kreatin\w*)\s*(?:ist|war|liegt|betr[aä]gt|=|:|bei|nehm\w*|ja|nein|\d)|"
    r"(?:kein\s+)?kreatin\b.*\b(?:ja|nein|nehm\w*|supplement\w*)|"
    r"schlecht\s+geschlafen|nur\s+\d+(?:[.,]\d+)?\s*h\s+geschlafen|halsschmerzen|erk[aä]ltet|krank\s+train\w*|"
    r"strava\s+sync\w*|garmin\s+(?:sync\w*|verbinden|connect)|google\s+health\b|pixel\s+watch\b|"
    r"schlaf(?:daten|analyse|phasen|qualit[aä]t|schuld)\b|tiefschlaf\b|rem-?schlaf\b|hauttemperatur\b|atemfrequenz\b|"
    r"hammerhead|karoo\s+(?:sync\w*|verbinden|push)|"
    r"ride\s+check-?in|readiness\s+check|feedback\s+zum\s+(?:ride|training|workout|lauf)|"
    r"training\s+gestern\s+war|einheit\s+gestern|ausfahrt\s+war|"
    r"letzte[rn]?\s+(?:ride|ausfahrt|einheit|lauf)|(?:ride|ausfahrt|einheit|training)\s+am\s+\d{1,2}\.\d{1,2})\b"
)
COACH_RX_DAILY = re.compile(
    r"(?i)\b(?:mein\s+workout|workout\s+heute|workout\s+morgen|was\s+trainier\w+\s+ich|heutiges?\s+(?:workout|training)|"
    r"im\s+training\b|training\s+heute|training\s+morgen|n[aä]chste\s+einheit|"
    r"soll\s+ich\s+(?:heute\s+)?(?:trainieren|fahren|laufen|auf\s+die\s+rolle)|"
    r"auf\s+die\s+rolle|auf\s+der\s+rolle|intervall-?training|\.zwo\b|\.fit\b|karoo\s+workout|ftp\s+test|feld-?test|lsct\b|"
    r"sweet\s*spot\s+workout|tagesform\s+training|readiness\s+heute|7-?s[aä]ulen|"
    r"w/kg|watt/kg|wattzahl\w*|vlamax|laktat\w*|vo2max|durability|fatmax|critical\s*power|w['’]?\s*bal|5s-?peak|hard-?start|30/15\w*)\b|"
    r"\b(?:ftp|cp)\b.*\b(?:watt|w/kg|test|zone|schwelle|entfernt|aktuell|steigern|laktat|peak|rolle|laufen)\b"
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
    elif COACH_RX_DNA.search(low):
        mode, why = "dna", "Athlete / Rider DNA physiological scouting request"
    elif COACH_RX_CHECKIN.search(low):
        mode, why = "checkin", "ride feedback / Strava check-in"
    elif COACH_RX_DAILY.search(low) or explicit:
        mode, why = "daily", ("explicit /atlas-coach" if explicit else "daily workout / adaptation request")
    else:
        return None, "", None
    macro = parse_macro_plan()
    return mode, why, macro


def _detect_user_extensions(prompt):
    """Match user-grown custom skills/commands in `brain/skills/*/SKILL.md` (`triggers:` or `aliases:` frontmatter)."""
    skills_dir = os.path.join(BRAIN, "skills")
    if not os.path.isdir(skills_dir) or not prompt.strip():
        return []
    low = graph_engine.norm_text(prompt)
    core_skills_dir = os.path.join(BRAIN, "_atlas", "skills")
    core_names = set(os.listdir(core_skills_dir)) if os.path.isdir(core_skills_dir) else set()
    hits = []
    for name in sorted(os.listdir(skills_dir)):
        sk_md = os.path.join(skills_dir, name, "SKILL.md")
        if not os.path.isfile(sk_md):
            continue
        try:
            with open(sk_md, encoding="utf-8", errors="replace") as fh:
                fm = frontmatter(fh.read(4000))
        except OSError:
            continue
        trigs = list(_as_list(fm.get("triggers"))) + list(_as_list(fm.get("aliases")))
        if name not in core_names and len(name) >= 4:
            trigs.append(name)
            trigs.append(name.replace("-", " "))
        for tr in trigs:
            tn = graph_engine.norm_text(tr).strip()
            if len(tn) >= 4 and re.search(r"(?<![\w-])" + re.escape(tn) + r"(?![\w-])", low):
                cmd_cand = ""
                for sp in (
                    os.path.join(BRAIN, "howto", "scripts", f"{name}.py"),
                    os.path.join(skills_dir, name, "scripts", f"{name}.py"),
                ):
                    if os.path.isfile(sp):
                        cmd_cand = name
                        break
                hits.append((name, f"skills/{name}/SKILL.md", cmd_cand))
                break
    return hits


def prompt_text(prompt, cwd, session):
    idx = load_index(budget=INDEX_BUDGET_S)
    st = _state()
    sess = _session(st, session)
    lines = []

    # 1. Explicit privacy opt-out (`do-not-store` overrides all capture/learning nudges for this turn)
    do_not_store = bool(SIGNAL_DO_NOT_STORE.search(prompt))
    if do_not_store:
        sess["do_not_store"] = True
        sess.pop("signal", None)
        lines.append(
            "Atlas privacy guard (do-not-store): Do NOT store the facts or content of this message in brain/ or journal/; "
            "only record a general 'never store <topic>' rule in howto/ if requested."
        )
    else:
        sess.pop("do_not_store", None)

    # 2. Project routing (Charter §1.1 & §1.5: inside a project, step back and act like a normal coding agent)
    routes = route(prompt, cwd, idx)
    seen = set(sess.get("routed", []))
    root = _root()
    for p, why in routes:
        if p["slug"] in seen:
            continue
        where = ", ".join((os.path.relpath(x, root).replace("\\", "/") if _norm_path(x).startswith(_norm_path(root) + "/")
                           and cwd and _norm_path(cwd).startswith(_norm_path(root)) else x.replace("\\", "/"))
                          for x in p["resolved"]) or "no folder"
        proj_neighbors = [
            _show(n_rel, cwd)
            for n_rel, _sc, _via, _k in graph_engine.expand_1hop_neighbors(
                BRAIN, [p["page"]], skip_pages={p["page"]}, max_neighbors=2, db_path=CACHE
            )
            if not n_rel.startswith(("journal/", "_")) and not graph_engine.is_folder_hub(n_rel)
        ][:2]
        linked_ctx = f" Linked brain context (open only if needed): {', '.join(proj_neighbors)}." if proj_neighbors else ""
        lines.append(f"Atlas route -> project '{p['title']}' ({why}). Read {_show(p['page'], cwd)} first, then the "
                     f"repository's own agent files in {where} (CLAUDE.md, AGENTS.md, GEMINI.md, .claude/rules, "
                     f".agents/rules) once; after that work there like a normal coding agent.{linked_ctx}")
        sess.setdefault("routed", []).append(p["slug"])
        sess["project"] = p["slug"]

    explicit_atlas_cmd = bool(re.search(r"(?i)(?:^|\s)/atlas-[a-z-]+", prompt))
    in_project = bool(sess.get("project")) and not explicit_atlas_cmd

    # 3. Domain & user-extension routers (Coach, Mail, Tasks/Calendar, Custom Skills — only outside code-project mode)
    tools_py = _show("_atlas/tools/atlas.py", cwd)
    py = _py_cmd()
    if not in_project and not routes:
        coach_enabled = (
            "coach" in st.get("modules", [])
            or os.path.isdir(os.path.join(BRAIN, "coach"))
            or os.path.isfile(os.path.join(BRAIN, "_atlas", "procedures", "coach.md"))
        )
        c_mode = None
        if coach_enabled or COACH_RX_EXPLICIT.search(prompt):
            c_mode, c_why, c_macro = detect_coach_mode(prompt, idx)
            if c_mode:
                stale_note = ""
                if c_macro and c_macro.get("stale") and c_mode in ("plan-14d", "macro"):
                    stale_note = f" Macro plan is STALE ({c_macro.get('stale_reason')}) -> update brain/coach/macro-plan.md FIRST before writing 14d workouts."
                depth_flag = " --depth pro" if (c_mode == "dna" or coach_engine.PRO_QUERY_RX.search(prompt)) else ""
                lines.append(
                    f"Atlas coach -> mode '{c_mode}' ({c_why}). Run `{py} -I {tools_py} coach context --mode {c_mode}{depth_flag}` "
                    f"and follow {_show('_atlas/procedures/coach.md', cwd)}.{stale_note}"
                )
                sess["coach_mode"] = c_mode
            elif sess.get("coach_mode") and not MAIL_RX.search(prompt) and not TODO_RX.search(prompt):
                lines.append(
                    f"Atlas coach (active session follow-up): Persist any athlete answers/biometrics immediately in "
                    f"`brain/coach/{{profile,playbook}}.md` or via `{py} -I {tools_py} coach note ... --execute`; "
                    f"use `{py} -I {tools_py} coach history|compare|inspect|push` on demand and keep asking 1-2 sharp coach questions."
                )
        if not c_mode and MAIL_RX.search(prompt):
            mail_proc = _show("_atlas/procedures/mail.md", cwd)
            user_mail_sk = os.path.join(BRAIN, "skills", "atlas-mail", "SKILL.md")
            overlay_bit = f" + {_show('skills/atlas-mail/SKILL.md', cwd)}" if os.path.isfile(user_mail_sk) else ""
            lines.append(
                f"Atlas mail -> Run `{py} -I {tools_py} mail accounts|list|search|read ...` and follow {mail_proc}{overlay_bit}."
            )
        if not c_mode and TODO_RX.search(prompt):
            sync_extra = ""
            try:
                if cal_sync.get_sync_ui_summary(BRAIN).get("enabled"):
                    sync_extra = f" | Google Sync/FreeBusy/Invites: `{py} -I {tools_py} cal status|freebusy|show|create|rsvp|conflicts|sync`."
            except Exception:
                pass
            lines.append(
                f"Atlas tasks/calendar -> Use `{py} -I {tools_py} todo` (`list|done|move|skip|add \"<text>\" [--to YYYY-MM-DD] [--time HH:MM] [--kind event|todo|deadline|blocked|goal]`) or `{py} -I {tools_py} calendar`.{sync_extra}"
            )
        # Zero-token incremental Google Calendar auto-pull (TTL 60s on calendar/coach/briefing intent, else 15m; 2s fail-open)
        if not PROBE:
            try:
                is_cal_urgent = bool(
                    c_mode
                    or TODO_RX.search(prompt)
                    or re.search(r"(?i)\b(kalender|calendar|termin|meeting|frei|freebusy|slot|woche|morgen|heute|briefing|sync|einlad|rsvp|google\s*meet)\b", prompt)
                )
                pull_res = cal_sync.maybe_auto_pull(BRAIN, urgent=is_cal_urgent, timeout=2.0)
                if pull_res:
                    bits = []
                    if pull_res.get("pulled_new"):
                        bits.append(f"+{pull_res['pulled_new']} new")
                    if pull_res.get("pulled_updated"):
                        bits.append(f"~{pull_res['pulled_updated']} updated")
                    if pull_res.get("pulled_cancelled"):
                        bits.append(f"-{pull_res['pulled_cancelled']} cancelled")
                    if pull_res.get("open_conflicts"):
                        bits.append(f"{pull_res['open_conflicts']} open conflict(s) (`{py} -I {tools_py} cal conflicts`)")
                    if pull_res.get("auth_warnings"):
                        bits.append(f"auth renew needed: {', '.join(pull_res['auth_warnings'])}")
                    if bits:
                        lines.append(f"Atlas cal-sync -> {', '.join(bits)}.")
                        if pull_res.get("files_changed"):
                            render_calendar_html(idx, start_server=False)
            except Exception as exc:
                _log_hook_error("prompt.cal_sync", exc)
        for ext_name, ext_rel, ext_cmd in _detect_user_extensions(prompt)[:2]:
            cli_bit = f"; CLI: `{py} -I {tools_py} {ext_cmd} ...`" if ext_cmd else ""
            lines.append(f"Atlas user extension -> skill '{ext_name}' ({_show(ext_rel, cwd)}){cli_bit}.")

    # 4. Capture-first signals (explicit "remember", corrections, and substantive new facts/dates/amounts)
    if not do_not_store:
        if SIGNAL_EXPLICIT.search(prompt):
            kind = "explicit"
        elif SIGNAL_CORRECTION.search(prompt):
            kind = "correction"
        elif not in_project and SIGNAL_FACT.search(prompt) and not SIGNAL_META_SKIP.search(prompt):
            kind = "fact"
        else:
            kind = ""
        if kind:
            sess["signal"] = kind
            lines.append(f"Atlas learning signal ({kind}): {LEARN_HINTS[kind]}.")

    # 5. Surgical named-entity / alias / ID / domain-tag leads (zero speculative full-text noise; skipped inside code projects)
    if not in_project:
        skip = {p["page"] for p, _ in routes}
        plain = re.sub(r"[A-Za-z]:[\\/][^\s\"'`]*|~?/[\w.-]+/[^\s\"'`]*", " ", prompt)
        if len(plain.strip()) >= 3:
            found, _linked = graph_engine.compute_prompt_hints(
                BRAIN, plain, idx, tag_vocab(), _vocab(idx, skip), skip,
                max_hints=MAX_HINTS, max_df_share=MAX_DF_SHARE, db_path=CACHE,
            )
        else:
            found = []
        if found:
            lines.append(f"Atlas entity leads (read matched leads directly; run `{py} -I {tools_py} find <words>` only if a lead is missing):")
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
        payload = body + ("\n\n" if body.endswith("· Log") else "\n") + "\n".join(new) + "\n"
        tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.{secrets.token_hex(4)}.tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(payload)
        os.replace(tmp, path)


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
    if sess.pop("do_not_store", False):
        sess.pop("signal", None)
        _save_state(st)
        return None
    signal = sess.pop("signal", "")
    if marker_seen:
        sess["last_marker"], sess["since_marker"] = _now_iso(), 0
        _save_state(st)
        return None
    sess["since_marker"] = sess.get("since_marker", 0) + 1
    mode = s.get("reminder", "signals")
    reason = None
    if signal in ("explicit", "correction") and mode in ("signals", "every-turn", "rare"):
        reason = REMINDER_SIGNAL.format(kind=signal)
    elif signal == "fact" and mode in ("signals", "every-turn"):
        reason = REMINDER_SIGNAL.format(kind="new fact/date/amount")
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


def _auto_heal_dirty_brain_pages():
    """Run zero-token auto-integrity (`[[wikilinks]]`, tag synonyms, `related:` edges, SQLite sync) on dirty `.md` pages."""
    dirty = []
    if os.path.isdir(os.path.join(BRAIN, ".git")):
        res = _git("status", "--porcelain", "-uall")
        if res.returncode == 0 and res.stdout.strip():
            for raw_ln in res.stdout.splitlines():
                if len(raw_ln) < 4 or "D" in raw_ln[:2]:
                    continue
                p_part = raw_ln[3:].strip()
                if " -> " in p_part:
                    p_part = p_part.split(" -> ", 1)[1].strip()
                p_part = p_part.strip('"')
                if p_part.lower().endswith(".md") and not p_part.startswith(("_atlas/", "_secrets/", "_archive/")):
                    dirty.append(p_part)
    else:
        st = _state()
        cutoff = float(st.get("last_autoheal_ts") or (time.time() - 300.0))
        for dp, dns, fns in os.walk(BRAIN):
            dns[:] = [d for d in dns if not d.startswith((".", "_atlas", "_secrets", "_archive"))]
            for fn in fns:
                if not fn.lower().endswith(".md"):
                    continue
                full = os.path.join(dp, fn)
                try:
                    if os.path.getmtime(full) >= cutoff:
                        dirty.append(os.path.relpath(full, BRAIN).replace("\\", "/"))
                except OSError:
                    continue
        st["last_autoheal_ts"] = time.time()
        _save_state(st)
    if not dirty:
        return 0
    return graph_engine.auto_heal_pages(BRAIN, dirty, tag_vocab_map=tag_vocab(), db_path=CACHE)


def _calendar_needs_render():
    """Return True only when `CAL_HTML` is missing, from a prior day, or relevant calendar/coach/due sources are newer."""
    try:
        if not os.path.isfile(CAL_HTML):
            return True
        cal_mtime = os.path.getmtime(CAL_HTML)
        if dt.date.fromtimestamp(cal_mtime) != dt.date.today():
            return True
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


READONLY_AG_TOOLS = {
    "read_file", "view_file", "view_file_outline", "view_code_item", "grep_search", "find_by_name",
    "list_dir", "list_resources", "read_resource", "read_url", "read_url_content", "search_web",
    "ask_question", "list_plugin_accounts", "search_marketplace", "manage_task", "manage_subagents",
}
_BENIGN_REDIRECT_RX = re.compile(
    r"(?i)(?:\b\d+>&\d+|\b[12]>\s*(?:/dev/null|\$null|NUL)\b)"
)
_MUTATING_SHELL_RX = re.compile(
    r"(?i)(?:>>?|(?<!-)--(?:execute|fix)\b|\btodo\s+(?:done|move|skip|reopen|add)\b|"
    r"\bcoach\s+(?:note|sync|zwo|auth-strava)\b|\bstate\s+--set\b|"
    r"\bmail\s+(?:init|ingest|move|copy|trash|delete|flags|labels|mkdir)\b|"
    r"\bgit(?:\.exe)?\s+(?:commit|push|add|tag|checkout|switch|merge|rebase|reset|stash|clean|rm|mv)\b|"
    r"\b(?:rm|mv|cp|mkdir|rmdir|touch|chmod|chown|ln|tee|install|dd|truncate|"
    r"del|erase|ren|copy|move|md|rd|icacls|attrib|"
    r"Set-Content|Add-Content|Out-File|Remove-Item|Move-Item|Copy-Item|New-Item|Rename-Item|Clear-Content)\b|"
    r"\bsed\s+-i\b|\b(?:python(?:3(?:\.\d+)?)?|py)(?:\.exe)?\s+(?:-[A-Za-z0-9.]+\s+)*-c\b)"
)
_READONLY_CMD_HEAD_RX = re.compile(
    r"(?i)^(?:ls|dir|cat|head|tail|more|grep|egrep|fgrep|rg|find|findstr|wc|stat|file|pwd|whoami|id|uname|date|echo|ps|"
    r"which|where(?:\.exe)?|type|true|diff|sort|uniq|cut|tr|"
    r"Get-ChildItem|Get-Content|Get-Item|Get-ItemProperty|Get-Location|Select-String|Test-Path|"
    r"Where-Object|Select-Object|Measure-Object|Sort-Object|Format-Table|Format-List|Out-String|Write-Output|"
    r"git(?:\.exe)?\s+(?:-C\s+(?:\"[^\"]+\"|'[^']+'|\S+)\s+)?(?:status|log|diff|show|branch|rev-parse|ls-remote|remote)|"
    r"(?:[\"']?(?:[A-Za-z]:[\\/][^\"']+?|/[^\"'\s]+?)[\\/])?(?:python(?:3(?:\.\d+)?)?|py)(?:\.exe)?[\"']?"
    r"(?:\s+-\d+(?:\.\d+)?)?(?:\s+-[IBu]+)*\s+(?:[\"'][^\"']*atlas(?:_mail)?\.py[\"']|\S*atlas(?:_mail)?\.py))(?=$|\s)"
)


def _is_readonly_shell_cmd(cmd_raw):
    cmd = _BENIGN_REDIRECT_RX.sub(" ", str(cmd_raw or "")).strip()
    if not cmd:
        return True
    if _MUTATING_SHELL_RX.search(cmd):
        return False
    parts = [p.strip() for p in re.split(r"\|\||&&|[;|]", cmd) if p.strip()]
    if not parts:
        return True
    for seg in parts:
        seg_clean = seg.strip()
        if not seg_clean:
            continue
        if not _READONLY_CMD_HEAD_RX.match(seg_clean):
            return False
    return True


def _parse_antigravity_turn(transcript_path):
    """Parse Antigravity `transcript.jsonl` from the last `USER_INPUT` onward.

    Returns `(assistant_content_only, did_mutating_work)` so model `thinking` and tool outputs
    never leak into `daily_log`, and pure read-only lookups (`read_file`, `ls`, `atlas.py find`)
    never trigger false `Stop hook blocked termination` nudges.
    """
    if not transcript_path or not os.path.isfile(transcript_path):
        return "", False
    try:
        size = os.path.getsize(transcript_path)
        offset = max(0, size - 600_000)
        with open(transcript_path, "rb") as fh:
            fh.seek(offset)
            tail = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return "", False

    raw_lines = tail.splitlines()
    if offset > 0 and raw_lines:
        raw_lines = raw_lines[1:]

    rows = []
    for ln in raw_lines:
        ln_s = ln.strip()
        if not ln_s:
            continue
        try:
            obj = json.loads(ln_s)
            if isinstance(obj, dict):
                rows.append(obj)
        except ValueError:
            continue

    if not rows:
        return tail[-60_000:], bool(AG_WORK.search(tail[-60_000:]))

    last_user_idx = -1
    for i, r in enumerate(rows):
        if r.get("type") == "USER_INPUT":
            last_user_idx = i
    turn_rows = rows[last_user_idx + 1:] if last_user_idx >= 0 else rows

    contents = []
    did_work = False
    for r in turn_rows:
        if r.get("type") != "PLANNER_RESPONSE":
            continue
        c = r.get("content")
        if isinstance(c, str) and c.strip():
            contents.append(c)
        for tc in (r.get("tool_calls") or []):
            if not isinstance(tc, dict):
                continue
            tname = str(tc.get("name") or "").strip()
            tlow = tname.lower()
            if not tlow or IGNORED_TOOLS.search(tlow) or tlow in READONLY_AG_TOOLS:
                continue
            if tlow == "run_command":
                args_obj = tc.get("args") or {}
                raw_cmd = args_obj.get("CommandLine", "") if isinstance(args_obj, dict) else ""
                if isinstance(raw_cmd, str) and raw_cmd.startswith('"') and raw_cmd.endswith('"'):
                    try:
                        raw_cmd = json.loads(raw_cmd)
                    except ValueError:
                        pass
                if _is_readonly_shell_cmd(raw_cmd):
                    continue
            did_work = True

    return "\n\n".join(contents), did_work


def _detect_deleted_open_calendar_tasks():
    """Return open non-workout tasks present in `HEAD:calendar/README.md` that vanished completely from disk.

    Marking a task `[x]` or `[-]`, rescheduling its date/time, promoting an undated task into `## Timeline`,
    or slightly rewording it are all recognized as preserved (zero false positives). Only genuinely deleted
    open non-workout lines are returned so the Stop hook can alert before an accidental edit goes unnoticed.
    """
    if not os.path.isdir(os.path.join(BRAIN, ".git")):
        return []
    st_res = _git("status", "--porcelain", "--", "calendar/README.md")
    if st_res.returncode != 0 or not (st_res.stdout or "").strip():
        return []
    head_res = _git("show", "HEAD:calendar/README.md")
    if head_res.returncode != 0 or not head_res.stdout:
        return []
    head_lines = head_res.stdout.splitlines()
    cal_path = os.path.join(BRAIN, "calendar", "README.md")
    if os.path.isfile(cal_path):
        try:
            with open(cal_path, encoding="utf-8", errors="replace") as fh:
                cur_lines = fh.read().splitlines()
        except OSError:
            return []
    else:
        cur_lines = []

    cur_all = []
    for _idx, _box, rest in cal_engine.find_checkbox_lines(cur_lines):
        n = cal_engine._norm_todo_compare(rest)
        if n:
            cur_all.append((n, _cal_tokens(n)))

    deleted = []
    for _idx, box, rest in cal_engine.find_checkbox_lines(head_lines):
        if box != " ":
            continue
        if "[workout" in rest.lower():
            continue
        old_norm = cal_engine._norm_todo_compare(rest)
        if len(old_norm) < 3:
            continue
        old_toks = _cal_tokens(old_norm)
        found = False
        for c_norm, c_toks in cur_all:
            if old_norm == c_norm or old_norm in c_norm or c_norm in old_norm:
                found = True
                break
            if old_toks and c_toks:
                ov = len(old_toks & c_toks)
                if ov >= 2 and ov / max(min(len(old_toks), len(c_toks)), 1) >= 0.6:
                    found = True
                    break
        if not found:
            deleted.append(rest.strip()[:120])
    return deleted


def stop(agent, data):
    if PROBE:
        return
    st = _state()
    s = settings()
    ag_did_work = False
    if agent == "claude":
        session = str(data.get("session_id") or "")
        last = data.get("last_assistant_message") or ""
    else:
        session = str(data.get("conversationId") or "")
        last, ag_did_work = _parse_antigravity_turn(data.get("transcriptPath"))
    context = st.get("sessions", {}).get(session, {}).get("project") or "—"
    if s.get("daily_log", "on") != "off" and not st.get("sessions", {}).get(session, {}).get("do_not_store"):
        try:
            daily_log(last, context)
        except Exception as exc:
            _log_hook_error("stop.daily_log", exc)
    try:
        _auto_heal_dirty_brain_pages()
    except Exception as exc:
        _log_hook_error("stop.auto_heal", exc)
    deleted_cal_tasks = []
    try:
        deleted_cal_tasks = _detect_deleted_open_calendar_tasks()
    except Exception as exc:
        _log_hook_error("stop.calendar_guard", exc)
    cal_pushed_files = []
    try:
        push_res = cal_sync.auto_push_on_stop(BRAIN, timeout=4.0)
        if push_res and push_res.get("files_changed"):
            cal_pushed_files = push_res["files_changed"]
    except Exception as exc:
        _log_hook_error("stop.cal_sync_push", exc)
    try:
        if cal_pushed_files or _calendar_needs_render():
            render_calendar_html()
    except Exception as exc:
        _log_hook_error("stop.render_calendar", exc)
    try:
        autocommit_and_push()
    except Exception as exc:
        _log_hook_error("stop.autocommit", exc)
    guard_reason = None
    if deleted_cal_tasks:
        preview = "; ".join(deleted_cal_tasks[:4])
        if len(deleted_cal_tasks) > 4:
            preview += f" (+{len(deleted_cal_tasks) - 4} more)"
        guard_reason = (
            f"Atlas safety guard - {len(deleted_cal_tasks)} open task(s) were deleted from calendar/README.md "
            f"instead of marked [x] or [-]: {preview}. Restore any accidentally removed task(s) from git "
            f"(`git -C brain show HEAD~1:calendar/README.md`) or confirm intentional deletion with the user."
        )
    marker = bool(MARKER.search(last))
    if agent == "claude":
        if data.get("stop_hook_active"):
            return
        if guard_reason:
            emit_continue(agent, guard_reason)
            return
        tp = data.get("transcript_path")
        did_work = bool(tp and os.path.isfile(tp) and _claude_turn_did_work(tp))
        reason = _decide(session, marker, did_work)
        if reason:
            emit_continue(agent, reason)
        return
    if data.get("continuationActive") or data.get("fullyIdle") is False:
        return
    if data.get("terminationReason") not in (None, "model_stop", "NO_TOOL_CALL"):
        return
    # Extra state guard in case older Antigravity builds omit `continuationActive`.
    key = hashlib.sha256((session + (data.get("lastUserInput") or "")).encode("utf-8")).hexdigest()[:16]
    if _state().get("antigravity_last_nudge") == key:
        return
    reason = guard_reason or _decide(session, marker, ag_did_work)
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
        if agent == "antigravity" and not PROBE:
            session_warmup(cwd, session)
            return
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
        if agent == "antigravity":
            today = dt.date.today().isoformat()
            st_pre = _state()
            sess_pre = st_pre.get("sessions", {}).get(session, {}) if session else {}
            need_session = (not session) or (sess_pre.get("session_injected") != today)
            s_out = ""
            if need_session:
                # `SessionStart` (`session_warmup`) owns git pull in Antigravity so `PreInvocation` stays <300 ms
                # and never collides with a concurrent `SessionStart` on `.git/index.lock`.
                s_out = session_text(cwd, session, skip_pull=True)
            p_out = prompt_text(prompt, cwd, session)
            emit_context(agent, event, "\n".join(x for x in (s_out, p_out) if x), persistent=need_session)
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
