"""Provider-agnostic 3-Way Calendar Sync Engine for Atlas (Python 3 stdlib only).

Features:
- Layered calendar files under `brain/calendar/*.md` (`README.md`, `<layer>.md`, read-only mirrors)
- Declarative config in `brain/calendar/sync.yaml` (pure stdlib YAML parser/serializer)
- Multi-account calendar discovery with automatic `calendarId` deduplication
- Stable identity via invisible Markdown comments (`<!-- g:<uid> -->`) + Google `extendedProperties`
  + local SQLite 3-way state & rich metadata cache (`brain/_atlas/cache/cal_sync.sqlite`)
- 3-stage intelligent deduplication (exact ID/iCalUID -> high-confidence auto-link -> conflict queue)
- Full bidirectional 3-way sync (Create, Update, `[x]` -> `✓`, `[-]` <-> delete) with Smart-Merge
  and Bulk-Delete Circuit Breaker (`bulk_delete_limit`, default 3)
- Smart-Formatter (clean titles, auto workout durations from `(82m | ...)`, details in description)
- Safety-gated external invites / Google Meet creation / RSVP + cross-layer FreeBusy slot finder
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import secrets
import sqlite3
import subprocess
import time
from typing import Any

from . import cal_engine
from . import caldav_provider
from . import gcal_provider

# ---------------------------------------------------------------------------
# Constants & Regexes
# ---------------------------------------------------------------------------

SYNC_CONFIG_REL = "calendar/sync.yaml"
SYNC_DB_FILENAME = "cal_sync.sqlite"

SYNC_ID_RX = re.compile(r"\s*<!--\s*g:([A-Za-z0-9_-]{4,24})\s*-->\s*$")
LOCAL_FLAG_RX = re.compile(r"(?:^|\s)#local(?=\s|$)", re.I)
FORCE_SYNC_FLAG_RX = re.compile(r"(?:^|\s)#sync(?=\s|$)", re.I)
LAYER_TAG_RX = re.compile(r"(?:^|\s)@([a-zA-Z0-9_-]{2,24})(?=\s|$)")
BRAIN_PATH_TAIL_RX = re.compile(
    r"\s*\(((?:areas|projects|people|self|howto|coach|calendar)/[^\s)]+\.md)\)\s*$"
)
FEEDBACK_BLOCK_RX = re.compile(r"\s*\[feedback:\s*(.+?)\]\s*$", re.I)
WORKOUT_PARENS_RX = re.compile(r"\s*\((\d{1,3})\s*(?:m|min)\s*\|\s*(.+)\)\s*$", re.I)
DURATION_HINT_RX = re.compile(r"\b(\d{1,3})\s*(?:m|min)\b", re.I)
LEADING_SYNC_ICON_RX = re.compile(
    r"^(?:(?:✓|✔|✅|☐|⬜|☑️?|🚴|🏃|🏋️?|🏊|🏆|🚫|⏳|📅|🎉|🎂|❤️?)\s*)+"
)
DONE_TODO_COLOR_ID = "8"  # Google Calendar Graphite (muted grey for completed `[todo]` items)
CANCELLED_TITLE_RX = re.compile(r"^(?:abgesagt|canceled|cancelled|storniert)\s*[:\-–—]\s*", re.I)
AUTO_BOOKING_TITLE_RX = re.compile(r"^(?:stay at|übernachtung(?:\s+in|\s+bei)?|hotel\s+reservation|flight\s+to|flug\s+nach)\b", re.I)
FREEBUSY_EXCLUDE_NAME_RX = re.compile(r"(?:geburtstag|birthday|abfall|müll|muell|waste|trash|bin|feiertag|holiday|ferien)", re.I)

DATE_LINE_RX = re.compile(
    r"^\s*(?:[-*+]|\d+\.)?\s*(?:(\[[ xX-]\])\s+)?(?:yearly\s+)?"
    r"(\d{4}-\d{2}-\d{2}|--?\d{2}-\d{2})(?:\.\.(\d{4}-\d{2}-\d{2}))?\s+(.+?)\s*$"
)
TIME_PREFIX_RX = re.compile(r"^(\d{1,2}:\d{2}(?:-\d{1,2}:\d{2})?)\s+(.+)$")
KIND_PREFIX_RX = re.compile(r"^\[([a-zA-Z_-]+)(?::([a-zA-Z0-9_-]+))?\]\s*(.+)$")

_KIND_MAP = {
    "workout": ("workout", ""), "training": ("workout", ""),
    "cycling": ("workout", "cycling"), "ride": ("workout", "cycling"),
    "rad": ("workout", "cycling"), "rennrad": ("workout", "cycling"),
    "running": ("workout", "running"), "run": ("workout", "running"), "lauf": ("workout", "running"),
    "strength": ("workout", "strength"), "kraft": ("workout", "strength"),
    "gym": ("workout", "strength"), "swim": ("workout", "swim"),
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

_DEDUP_STOPWORDS = {
    "und", "oder", "fuer", "für", "der", "die", "das", "mit", "bei", "von", "auf", "des", "dem", "den",
    "ein", "eine", "einen", "einem", "zum", "zur", "im", "in", "am", "an", "ab", "bis", "uhr", "inkl",
    "the", "and", "for", "with", "from", "call", "meeting", "google", "meet", "zoom", "teams", "online",
    "termin", "event", "todo", "task", "sync",
}

_DEFAULT_LAYER_COLORS = ("#38bdf8", "#a855f7", "#f59e0b", "#10b981", "#ec4899", "#6366f1", "#14b8a6")

DEFAULT_CONFIG: dict[str, Any] = {
    "version": 1,
    "enabled": True,
    "provider": "gcal",
    "timezone": "",
    "window_past_days": 14,
    "window_recurring_days": 90,
    "window_single_days": 365,
    "bulk_delete_limit": 3,
    "auto_sync": {
        "prompt_ttl_minutes": 15,
        "calendar_prompt_ttl_seconds": 60,
        "server_poll_minutes": 10,
        "push_on_stop": True,
    },
    "routing": {
        "default_layer": "README.md",
        "sync_kinds": ["event", "blocked", "goal", "workout", "todo", "deadline"],
        "local_only_kinds": ["birthday"],
        "sync_decentralized_due": True,
        "workout_calendar_name": "Atlas Coach",
        "tasks_calendar_name": "Atlas Tasks & Fristen",
    },
    "layers": [],
}


# ---------------------------------------------------------------------------
# 1. Pure-Stdlib Structured YAML Config Loader / Saver (`brain/calendar/sync.yaml`)
# ---------------------------------------------------------------------------

def sync_config_path(brain: str) -> str:
    return os.path.join(brain, "calendar", "sync.yaml")


def sync_db_path(brain: str) -> str:
    return os.path.join(brain, "_atlas", "cache", SYNC_DB_FILENAME)


def _strip_yaml_comment(val: str) -> str:
    in_q = ""
    out: list[str] = []
    for i, ch in enumerate(val):
        if ch in ("'", '"') and (i == 0 or val[i - 1] != "\\"):
            if not in_q:
                in_q = ch
            elif in_q == ch:
                in_q = ""
        elif ch == "#" and not in_q and (i == 0 or val[i - 1].isspace()):
            break
        out.append(ch)
    return "".join(out).strip()


def _parse_yaml_scalar(raw: str) -> Any:
    s = _strip_yaml_comment(raw)
    if not s or s in ("null", "~", "None"):
        return ""
    if s in ("true", "True", "yes"):
        return True
    if s in ("false", "False", "no"):
        return False
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        inner = s[1:-1]
        if s.startswith('"'):
            inner = inner.replace('\\"', '"').replace("\\\\", "\\")
        return inner
    if s.startswith("[") and s.endswith("]"):
        inner = s[1:-1].strip()
        if not inner:
            return []
        parts: list[str] = []
        buf: list[str] = []
        in_q = ""
        for ch in inner:
            if ch in ("'", '"'):
                in_q = "" if in_q == ch else (ch if not in_q else in_q)
                buf.append(ch)
            elif ch == "," and not in_q:
                parts.append("".join(buf).strip())
                buf = []
            else:
                buf.append(ch)
        if buf:
            parts.append("".join(buf).strip())
        return [_parse_yaml_scalar(p) for p in parts if p]
    if s.startswith("{") and s.endswith("}"):
        try:
            return json.loads(s)
        except Exception:
            return {}
    if re.match(r"^-?\d+$", s):
        return int(s)
    if re.match(r"^-?\d+\.\d+$", s):
        return float(s)
    return s


def parse_simple_yaml(text: str) -> dict[str, Any]:
    """Parse structured Atlas `sync.yaml` (supports nested dicts, lists of dicts, inline lists, JSON fallback)."""
    stripped_all = text.strip()
    if not stripped_all:
        return {}
    if stripped_all.startswith("{"):
        try:
            obj = json.loads(stripped_all)
            return obj if isinstance(obj, dict) else {}
        except json.JSONDecodeError:
            pass

    raw_lines: list[tuple[int, str]] = []
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        raw_lines.append((indent, line.strip()))

    def _parse_block(idx: int, min_indent: int) -> tuple[Any, int]:
        if idx >= len(raw_lines):
            return {}, idx
        first_ind, first_txt = raw_lines[idx]
        if first_txt.startswith("- "):
            arr: list[Any] = []
            while idx < len(raw_lines):
                ind, txt = raw_lines[idx]
                if ind < min_indent or not txt.startswith("- "):
                    break
                after_dash = txt[2:].strip()
                item_indent = ind + 2
                if ":" in after_dash and not after_dash.startswith(('"', "'")):
                    k, v = after_dash.split(":", 1)
                    k = k.strip()
                    v_s = _strip_yaml_comment(v)
                    obj: dict[str, Any] = {}
                    idx += 1
                    if v_s:
                        obj[k] = _parse_yaml_scalar(v_s)
                    elif idx < len(raw_lines) and raw_lines[idx][0] > item_indent:
                        obj[k], idx = _parse_block(idx, raw_lines[idx][0])
                    else:
                        obj[k] = ""
                    while idx < len(raw_lines) and raw_lines[idx][0] >= item_indent:
                        sub_ind, sub_txt = raw_lines[idx]
                        if sub_txt.startswith("- ") and sub_ind == ind:
                            break
                        if ":" not in sub_txt:
                            idx += 1
                            continue
                        sk, sv = sub_txt.split(":", 1)
                        sk = sk.strip()
                        sv_s = _strip_yaml_comment(sv)
                        idx += 1
                        if sv_s:
                            obj[sk] = _parse_yaml_scalar(sv_s)
                        elif idx < len(raw_lines) and raw_lines[idx][0] > sub_ind:
                            obj[sk], idx = _parse_block(idx, raw_lines[idx][0])
                        else:
                            obj[sk] = ""
                    arr.append(obj)
                else:
                    arr.append(_parse_yaml_scalar(after_dash))
                    idx += 1
            return arr, idx

        mapping: dict[str, Any] = {}
        while idx < len(raw_lines):
            ind, txt = raw_lines[idx]
            if ind < min_indent or txt.startswith("- "):
                break
            if ":" not in txt:
                idx += 1
                continue
            k, v = txt.split(":", 1)
            k = k.strip()
            v_s = _strip_yaml_comment(v)
            idx += 1
            if v_s:
                mapping[k] = _parse_yaml_scalar(v_s)
            elif idx < len(raw_lines) and raw_lines[idx][0] > ind:
                mapping[k], idx = _parse_block(idx, raw_lines[idx][0])
            else:
                mapping[k] = ""
        return mapping, idx

    parsed, _ = _parse_block(0, 0)
    return parsed if isinstance(parsed, dict) else {}


def _fmt_yaml_scalar(val: Any) -> str:
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, (int, float)):
        return str(val)
    if val is None or val == "":
        return '""'
    s = str(val)
    if re.search(r"[:#\[\]{},&*?|>!%@`'\"]|^\s|\s$", s) or s.lower() in ("true", "false", "null", "yes", "no"):
        escaped = s.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return s


def dump_simple_yaml(cfg: dict[str, Any]) -> str:
    """Serialize sync config dictionary into clean, human-editable YAML."""
    lines = ["# Atlas Calendar Sync configuration (managed by `atlas.py cal discover` / editable by hand)"]

    def _emit_dict(d: dict[str, Any], indent: int) -> None:
        pad = " " * indent
        for k, v in d.items():
            if isinstance(v, dict):
                if not v:
                    lines.append(f"{pad}{k}: {{}}")
                else:
                    lines.append(f"{pad}{k}:")
                    _emit_dict(v, indent + 2)
            elif isinstance(v, list):
                if not v:
                    lines.append(f"{pad}{k}: []")
                elif all(not isinstance(x, (dict, list)) for x in v):
                    inner = ", ".join(_fmt_yaml_scalar(x) for x in v)
                    lines.append(f"{pad}{k}: [{inner}]")
                else:
                    lines.append(f"{pad}{k}:")
                    for item in v:
                        if isinstance(item, dict):
                            items_kv = list(item.items())
                            if not items_kv:
                                lines.append(f"{pad}  - {{}}")
                                continue
                            first_k, first_v = items_kv[0]
                            if isinstance(first_v, dict):
                                lines.append(f"{pad}  - {first_k}:")
                                _emit_dict(first_v, indent + 6)
                            else:
                                lines.append(f"{pad}  - {first_k}: {_fmt_yaml_scalar(first_v)}")
                            for rk, rv in items_kv[1:]:
                                if isinstance(rv, dict):
                                    if not rv:
                                        lines.append(f"{pad}    {rk}: {{}}")
                                    else:
                                        lines.append(f"{pad}    {rk}:")
                                        _emit_dict(rv, indent + 6)
                                elif isinstance(rv, list) and all(not isinstance(x, (dict, list)) for x in rv):
                                    inner = ", ".join(_fmt_yaml_scalar(x) for x in rv)
                                    lines.append(f"{pad}    {rk}: [{inner}]")
                                else:
                                    lines.append(f"{pad}    {rk}: {_fmt_yaml_scalar(rv)}")
                        else:
                            lines.append(f"{pad}  - {_fmt_yaml_scalar(item)}")
            else:
                lines.append(f"{pad}{k}: {_fmt_yaml_scalar(v)}")

    _emit_dict(cfg, 0)
    return "\n".join(lines) + "\n"


def _deep_merge_defaults(raw: dict[str, Any]) -> dict[str, Any]:
    out = json.loads(json.dumps(DEFAULT_CONFIG))
    for k, v in (raw or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k].update(v)
        else:
            out[k] = v
    if not out.get("timezone"):
        out["timezone"] = gcal_provider.local_timezone_name()
    if not isinstance(out.get("layers"), list):
        out["layers"] = []
    return out


def load_sync_config(brain: str) -> dict[str, Any] | None:
    """Load `brain/calendar/sync.yaml`; returns `None` if calendar sync is not configured yet."""
    p = sync_config_path(brain)
    if not os.path.isfile(p):
        return None
    try:
        with open(p, encoding="utf-8", errors="replace") as fh:
            raw = parse_simple_yaml(fh.read())
    except OSError:
        return None
    if not isinstance(raw, dict):
        return None
    return _deep_merge_defaults(raw)


def save_sync_config(brain: str, cfg: dict[str, Any]) -> str:
    p = sync_config_path(brain)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    merged = _deep_merge_defaults(cfg)
    content = dump_simple_yaml(merged)
    err = cal_engine._atomic_write_text(p, content)
    if err:
        raise OSError(err)
    return p


# ---------------------------------------------------------------------------
# 1b. Multi-Provider Registry (`GoogleCalendarProvider` + `CalDAVProvider`)
# ---------------------------------------------------------------------------

def list_all_configured_accounts(brain: str) -> list[dict[str, Any]]:
    """Return configured calendar accounts across ALL providers (Google Calendar + CalDAV/Apple iCloud)."""
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for acct in gcal_provider.list_configured_accounts(brain):
        entry = dict(acct)
        entry.setdefault("provider", "gcal")
        key = (entry["provider"], entry["id"])
        if key not in seen:
            seen.add(key)
            out.append(entry)
    for acct in caldav_provider.list_configured_accounts(brain):
        entry = dict(acct)
        entry.setdefault("provider", "caldav")
        key = (entry["provider"], entry["id"])
        if key not in seen:
            seen.add(key)
            out.append(entry)
    return out


class CompositeCalendarProvider(gcal_provider.CalendarProvider):
    """Routes calendar operations seamlessly across Google Calendar and CalDAV/Apple iCloud accounts."""

    provider_name = "multi"

    def __init__(
        self,
        brain: str,
        timeout: float = 15.0,
        gcal: gcal_provider.CalendarProvider | None = None,
        caldav: gcal_provider.CalendarProvider | None = None,
    ):
        self.brain = brain
        self.timeout = float(timeout)
        self.gcal: gcal_provider.CalendarProvider = gcal or gcal_provider.GoogleCalendarProvider(brain, timeout=timeout)
        self.caldav: gcal_provider.CalendarProvider = caldav or caldav_provider.CalDAVProvider(brain, timeout=timeout)

    def _account_provider_map(self) -> dict[str, str]:
        mapping: dict[str, str] = {}
        # Check sync.yaml layer hints first so explicit `provider:` always wins
        cfg = load_sync_config(self.brain)
        if cfg:
            for ly in (cfg.get("layers") or []):
                if isinstance(ly, dict) and ly.get("account") and ly.get("provider"):
                    pr = str(ly["provider"]).strip().lower()
                    if pr in ("caldav", "icloud", "apple", "nextcloud", "fastmail"):
                        mapping[gcal_provider.sanitize_account_id(str(ly["account"]))] = "caldav"
                    elif pr == "gcal":
                        mapping[gcal_provider.sanitize_account_id(str(ly["account"]))] = "gcal"
        for a in self.gcal.list_accounts():
            mapping.setdefault(a["id"], "gcal")
        for a in self.caldav.list_accounts():
            mapping.setdefault(a["id"], "caldav")
        return mapping

    def provider_name_for_account(self, account_id: str) -> str:
        aid = gcal_provider.sanitize_account_id(account_id)
        return self._account_provider_map().get(aid, "gcal")

    def _sub(self, account_id: str) -> gcal_provider.CalendarProvider:
        return self.caldav if self.provider_name_for_account(account_id) == "caldav" else self.gcal

    def list_accounts(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for a in self.gcal.list_accounts():
            entry = dict(a)
            entry.setdefault("provider", "gcal")
            out.append(entry)
        for a in self.caldav.list_accounts():
            entry = dict(a)
            entry.setdefault("provider", "caldav")
            out.append(entry)
        return out

    def list_calendars(self, account_id: str) -> list[dict[str, Any]]:
        return self._sub(account_id).list_calendars(account_id)

    def create_calendar(self, account_id: str, summary: str, time_zone: str = "UTC") -> dict[str, Any]:
        return self._sub(account_id).create_calendar(account_id, summary, time_zone=time_zone)

    def pull_events(
        self,
        account_id: str,
        calendar_id: str,
        time_min: str = "",
        time_max: str = "",
        sync_token: str = "",
        single_events: bool = True,
    ) -> dict[str, Any]:
        return self._sub(account_id).pull_events(
            account_id, calendar_id, time_min=time_min, time_max=time_max,
            sync_token=sync_token, single_events=single_events,
        )

    def list_event_instances(
        self,
        account_id: str,
        calendar_id: str,
        event_id: str,
        time_min: str = "",
        time_max: str = "",
    ) -> list[dict[str, Any]]:
        return self._sub(account_id).list_event_instances(
            account_id, calendar_id, event_id, time_min=time_min, time_max=time_max
        )

    def upsert_event(
        self,
        account_id: str,
        calendar_id: str,
        event_body: dict[str, Any],
        event_id: str | None = None,
        send_updates: str = "none",
        conference_data: bool = False,
    ) -> dict[str, Any]:
        return self._sub(account_id).upsert_event(
            account_id, calendar_id, event_body, event_id=event_id,
            send_updates=send_updates, conference_data=conference_data,
        )

    def delete_event(
        self,
        account_id: str,
        calendar_id: str,
        event_id: str,
        send_updates: str = "none",
    ) -> bool:
        return self._sub(account_id).delete_event(account_id, calendar_id, event_id, send_updates=send_updates)

    def rsvp_event(
        self,
        account_id: str,
        calendar_id: str,
        event_id: str,
        response_status: str,
        send_updates: str = "all",
    ) -> dict[str, Any]:
        return self._sub(account_id).rsvp_event(
            account_id, calendar_id, event_id, response_status, send_updates=send_updates
        )

    def freebusy(
        self,
        account_id: str,
        calendar_ids: list[str],
        time_min: str,
        time_max: str,
        time_zone: str = "UTC",
    ) -> dict[str, list[dict[str, str]]]:
        return self._sub(account_id).freebusy(
            account_id, calendar_ids, time_min, time_max, time_zone=time_zone
        )


def build_default_provider(brain: str, timeout: float = 15.0) -> CompositeCalendarProvider:
    """Create the default multi-provider dispatcher covering both Google Calendar and CalDAV/Apple iCloud."""
    return CompositeCalendarProvider(brain, timeout=timeout)


def _layer_freebusy_enabled(layer: dict[str, Any]) -> bool:
    """Determine whether a calendar layer contributes to `cal freebusy` busy windows."""
    if "freebusy" in layer and layer["freebusy"] != "":
        return bool(layer["freebusy"])
    if str(layer.get("default_kind") or "").lower() == "birthday":
        return False
    return True


# ---------------------------------------------------------------------------
# 2. SQLite 3-Way State & Metadata Store (`brain/_atlas/cache/cal_sync.sqlite`)
# ---------------------------------------------------------------------------

def _open_db(brain: str) -> sqlite3.Connection:
    p = sync_db_path(brain)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    conn = sqlite3.connect(p, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS sync_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS calendar_cursors (
            account_id TEXT NOT NULL,
            calendar_id TEXT NOT NULL,
            sync_token TEXT DEFAULT '',
            last_full_sync_ts REAL DEFAULT 0,
            last_pull_ts REAL DEFAULT 0,
            PRIMARY KEY (account_id, calendar_id)
        );
        CREATE TABLE IF NOT EXISTS event_links (
            atlas_uid TEXT PRIMARY KEY,
            layer_file TEXT NOT NULL,
            account_id TEXT NOT NULL,
            calendar_id TEXT NOT NULL,
            gcal_event_id TEXT NOT NULL,
            ical_uid TEXT DEFAULT '',
            etag TEXT DEFAULT '',
            recurring_event_id TEXT DEFAULT '',
            date_start TEXT DEFAULT '',
            date_end TEXT DEFAULT '',
            last_atlas_hash TEXT DEFAULT '',
            last_gcal_hash TEXT DEFAULT '',
            last_atlas_snapshot TEXT DEFAULT '{}',
            last_gcal_snapshot TEXT DEFAULT '{}',
            meta_json TEXT DEFAULT '{}',
            status TEXT DEFAULT 'active',
            updated_at REAL DEFAULT 0
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_links_remote
            ON event_links(account_id, calendar_id, gcal_event_id);
        CREATE INDEX IF NOT EXISTS idx_links_layer
            ON event_links(layer_file, status);
        CREATE TABLE IF NOT EXISTS conflicts (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            atlas_uid TEXT DEFAULT '',
            layer_file TEXT DEFAULT '',
            account_id TEXT DEFAULT '',
            calendar_id TEXT DEFAULT '',
            gcal_event_id TEXT DEFAULT '',
            date_iso TEXT DEFAULT '',
            summary TEXT DEFAULT '',
            atlas_json TEXT DEFAULT '{}',
            gcal_json TEXT DEFAULT '{}',
            score REAL DEFAULT 0,
            status TEXT DEFAULT 'open',
            created_at REAL DEFAULT 0
        );
        """
    )
    conn.commit()
    return conn


def _get_meta(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM sync_meta WHERE key=?", (key,)).fetchone()
    return str(row["value"]) if row else default


def _set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO sync_meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )


def load_event_metadata_map(brain: str) -> dict[str, dict[str, Any]]:
    """Fast read-only lookup of cached Google metadata keyed by `atlas_uid` for CLI and hooks."""
    p = sync_db_path(brain)
    if not os.path.isfile(p):
        return {}
    out: dict[str, dict[str, Any]] = {}
    try:
        conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=2.0)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT atlas_uid, account_id, calendar_id, gcal_event_id, layer_file, status, meta_json "
                "FROM event_links WHERE status IN ('active', 'deleted_remote')"
            ).fetchall()
            for r in rows:
                try:
                    meta = json.loads(r["meta_json"] or "{}")
                except Exception:
                    meta = {}
                if not isinstance(meta, dict):
                    meta = {}
                meta["atlas_uid"] = r["atlas_uid"]
                meta["account_id"] = r["account_id"]
                meta["calendar_id"] = r["calendar_id"]
                meta["gcal_event_id"] = r["gcal_event_id"]
                meta["sync_status"] = r["status"]
                out[r["atlas_uid"]] = meta
        finally:
            conn.close()
    except Exception:
        return {}
    return out


def get_sync_ui_summary(brain: str) -> dict[str, Any]:
    """Return lightweight sync status payload for CLI and session hooks (0 network calls)."""
    cfg = load_sync_config(brain)
    accounts = list_all_configured_accounts(brain)
    acct_prov = {a["id"]: a.get("provider", "gcal") for a in accounts}
    if not cfg:
        return {
            "configured": False,
            "enabled": False,
            "accounts": accounts,
            "layers": [],
            "last_pull_ts": 0,
            "last_pull_iso": "",
            "open_conflicts": 0,
            "auth_warnings": [a["id"] for a in accounts if a.get("auth_error")],
        }
    last_pull_ts = 0.0
    last_push_ts = 0.0
    open_conflicts = 0
    p = sync_db_path(brain)
    if os.path.isfile(p):
        try:
            conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=2.0)
            try:
                r1 = conn.execute("SELECT value FROM sync_meta WHERE key='last_pull_ts'").fetchone()
                if r1:
                    last_pull_ts = float(r1[0] or 0)
                r2 = conn.execute("SELECT value FROM sync_meta WHERE key='last_push_ts'").fetchone()
                if r2:
                    last_push_ts = float(r2[0] or 0)
                r3 = conn.execute("SELECT COUNT(*) FROM conflicts WHERE status='open'").fetchone()
                if r3:
                    open_conflicts = int(r3[0] or 0)
            finally:
                conn.close()
        except Exception:
            pass
    last_pull_iso = (
        dt.datetime.fromtimestamp(last_pull_ts).astimezone().strftime("%Y-%m-%d %H:%M")
        if last_pull_ts > 0
        else ""
    )
    layers_ui = [
        {
            "id": str(ly.get("id") or os.path.splitext(str(ly.get("file") or ""))[0]),
            "file": f"calendar/{ly.get('file', 'README.md')}",
            "label": str(ly.get("label") or ly.get("id") or "Calendar"),
            "color": str(ly.get("color") or "#38bdf8"),
            "mode": str(ly.get("mode") or "bidirectional"),
            "account": str(ly.get("account") or ""),
            "provider": str(ly.get("provider") or acct_prov.get(str(ly.get("account") or ""), "gcal")),
            "freebusy": _layer_freebusy_enabled(ly),
        }
        for ly in (cfg.get("layers") or [])
        if isinstance(ly, dict) and ly.get("mode") != "ignore"
    ]
    return {
        "configured": True,
        "enabled": bool(cfg.get("enabled", True)),
        "accounts": accounts,
        "layers": layers_ui,
        "last_pull_ts": int(last_pull_ts),
        "last_pull_iso": last_pull_iso,
        "last_push_ts": int(last_push_ts),
        "open_conflicts": open_conflicts,
        "auth_warnings": [a["id"] for a in accounts if a.get("auth_error")],
    }


# ---------------------------------------------------------------------------
# 3. Markdown Parsing, Smart-Formatting & 3-Stage Deduplication
# ---------------------------------------------------------------------------

def strip_sync_comment(text: str) -> tuple[str, str]:
    """Strip trailing `<!-- g:<uid> -->` comment; returns `(clean_text, uid_or_empty)`."""
    s = str(text or "")
    m = SYNC_ID_RX.search(s)
    if not m:
        return s, ""
    return s[:m.start()].rstrip(), m.group(1)


def _new_atlas_uid() -> str:
    return secrets.token_hex(4)


def _due_uid(rel: str, raw_line: str) -> str:
    norm = re.sub(r"\s+", " ", raw_line.strip().lower())
    h = hashlib.sha1(f"{rel}|{norm}".encode("utf-8")).hexdigest()[:7]
    return f"d{h}"


def parse_atlas_timeline_line(raw: str, rel: str, line_no: int = 0, today: dt.date | None = None) -> dict[str, Any] | None:
    """Parse one dated Atlas timeline line including `<!-- g:uid -->`, `#local`, `#sync`, and `@layer`."""
    today = today or dt.date.today()
    no_id, sync_id = strip_sync_comment(raw)
    m = DATE_LINE_RX.match(no_id)
    if not m:
        return None
    box = (m.group(1) or "").strip().lower()
    status = "done" if box == "[x]" else ("skipped" if box == "[-]" else "open")
    d1_str, d2_str, rest = m.group(2), m.group(3), m.group(4).strip()
    if len(d1_str) < 10:
        # Yearly `--MM-DD` entries are treated as recurring birthdays/yearly milestones
        return None
    try:
        start_d = dt.date.fromisoformat(d1_str)
        end_d = dt.date.fromisoformat(d2_str) if d2_str else start_d
    except ValueError:
        return None
    if end_d < start_d:
        end_d = start_d

    time_str = ""
    tm = TIME_PREFIX_RX.match(rest)
    if tm:
        time_str, rest = tm.group(1), tm.group(2).strip()

    kind, subkind = "event", ""
    km = KIND_PREFIX_RX.match(rest)
    if km:
        rk = km.group(1).lower()
        rs = (km.group(2) or "").strip()
        mk, ms = _KIND_MAP.get(rk, (rk, ""))
        kind, subkind = mk, rs or ms
        rest = km.group(3).strip()
    elif re.search(r"\b(geburtstag|birthday)\b", rest, re.I):
        kind = "birthday"
    elif d2_str and re.search(r"\b(urlaub|dienstreise|trainingslager|reise|blocked|vacation)\b", rest, re.I):
        kind = "blocked"

    is_local = bool(LOCAL_FLAG_RX.search(rest))
    force_sync = bool(FORCE_SYNC_FLAG_RX.search(rest))
    m_layer = LAYER_TAG_RX.search(rest)
    layer_override = m_layer.group(1).lower() if m_layer else ""

    clean_what = LOCAL_FLAG_RX.sub(" ", rest)
    clean_what = FORCE_SYNC_FLAG_RX.sub(" ", clean_what)
    clean_what = re.sub(r"\s+", " ", clean_what).strip()

    return {
        "rel": rel,
        "line": line_no,
        "raw": raw,
        "sync_id": sync_id,
        "status": status,
        "start": start_d.isoformat(),
        "end": end_d.isoformat(),
        "time": time_str,
        "kind": kind,
        "subkind": subkind,
        "what": clean_what,
        "raw_what": rest,
        "is_local": is_local,
        "force_sync": force_sync,
        "layer_override": layer_override,
    }


def parse_calendar_md_items(brain: str, rel: str, today: dt.date | None = None) -> tuple[list[str], list[dict[str, Any]]]:
    """Read `brain/<rel>` and return `(raw_lines, parsed_timeline_items)` outside frontmatter & code fences."""
    full = os.path.join(brain, *rel.split("/"))
    if not os.path.isfile(full):
        return [], []
    with open(full, encoding="utf-8", errors="replace") as fh:
        lines = fh.read().splitlines()
    items: list[dict[str, Any]] = []
    in_fm = False
    in_fence = False
    for idx, line in enumerate(lines):
        st = line.strip()
        if idx == 0 and st == "---":
            in_fm = True
            continue
        if in_fm:
            if st in ("---", "..."):
                in_fm = False
            continue
        if re.match(r"^\s*(`{3,}|~{3,})", line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        parsed = parse_atlas_timeline_line(line, rel, line_no=idx + 1, today=today)
        if parsed:
            items.append(parsed)
    return lines, items


def _parse_time_window(date_start: str, date_end: str, time_str: str, what: str, kind: str) -> tuple[str | None, str | None, int]:
    """Parse start/end clock times (`HH:MM`, `HH:MM`) and duration in minutes (None for all-day)."""
    if not time_str:
        return None, None, 0
    if "-" in time_str:
        a, b = time_str.split("-", 1)
        try:
            sh, sm = map(int, a.split(":"))
            eh, em = map(int, b.split(":"))
            dur = (eh * 60 + em) - (sh * 60 + sm)
            if dur <= 0:
                dur = 60
            return f"{sh:02d}:{sm:02d}", f"{eh:02d}:{em:02d}", dur
        except ValueError:
            return None, None, 0
    try:
        sh, sm = map(int, time_str.split(":"))
    except ValueError:
        return None, None, 0
    dur = 60
    clean_for_dur = FEEDBACK_BLOCK_RX.sub("", str(what or "")).strip()
    mw = WORKOUT_PARENS_RX.search(clean_for_dur)
    if mw:
        dur = max(10, min(600, int(mw.group(1))))
    elif kind == "workout":
        mh = DURATION_HINT_RX.search(clean_for_dur)
        if mh:
            dur = max(10, min(600, int(mh.group(1))))
    total = sh * 60 + sm + dur
    eh = (total // 60) % 24
    em = total % 60
    return f"{sh:02d}:{sm:02d}", f"{eh:02d}:{em:02d}", dur


def _kind_icon(kind: str, subkind: str = "") -> str:
    if kind == "workout":
        if subkind == "running":
            return "🏃"
        if subkind == "strength":
            return "🏋️"
        if subkind == "swim":
            return "🏊"
        return "🚴"
    if kind == "goal":
        return "🏆"
    if kind == "blocked":
        return "🚫"
    if kind == "todo":
        return "☐"
    if kind == "deadline":
        return "⏳"
    if kind == "birthday":
        return "🎂"
    return ""


def format_atlas_for_gcal(
    item: dict[str, Any],
    atlas_uid: str,
    timezone: str = "UTC",
    existing_description: str = "",
    existing_color_id: str = "",
) -> dict[str, Any]:
    """Smart-Formatter: convert an Atlas item into a clean Google Calendar Event resource."""
    what = str(item.get("what") or "").strip()
    kind = str(item.get("kind") or "event")
    subkind = str(item.get("subkind") or "")
    status = str(item.get("status") or "open")
    full_kind = f"{kind}:{subkind}" if subkind else kind

    desc_parts: list[str] = []
    # Extract trailing `[feedback: ...]` block -> description (keeps Google title crisp!)
    feedback_txt = ""
    m_fb = FEEDBACK_BLOCK_RX.search(what)
    if m_fb:
        feedback_txt = m_fb.group(1).strip()
        what = what[:m_fb.start()].strip()

    # Extract trailing brain path `(areas/foo.md)` -> description
    m_path = BRAIN_PATH_TAIL_RX.search(what)
    brain_ref = ""
    if m_path:
        brain_ref = m_path.group(1)
        what = what[:m_path.start()].strip()
    elif item.get("rel") and not str(item["rel"]).startswith("calendar/"):
        brain_ref = str(item["rel"])

    # Extract workout duration & interval prescription `(82m | 12m WU ...)` -> description
    wko_detail = ""
    m_wko = WORKOUT_PARENS_RX.search(what)
    if m_wko:
        wko_min = m_wko.group(1)
        wko_detail = f"Dauer: {wko_min} min\n{m_wko.group(2).strip()}"
        what = what[:m_wko.start()].strip()

    # Strip `@layer` tag from Google summary
    clean_title = LAYER_TAG_RX.sub(" ", what)
    clean_title = re.sub(r"\s+", " ", clean_title).strip()
    clean_title = LEADING_SYNC_ICON_RX.sub("", clean_title).strip()

    # Smart-Split very long `[todo]` / `[deadline]` headlines (>110 chars) outside parentheses so Google stays legible
    long_detail = ""
    if kind in ("todo", "deadline") and len(clean_title) > 110:
        for sep in (" — ", " – ", ": "):
            start_search = 0
            found_pos = -1
            while True:
                pos = clean_title.find(sep, start_search)
                if pos == -1 or pos > 110:
                    break
                prefix_sub = clean_title[:pos]
                if pos >= 18 and prefix_sub.count("(") == prefix_sub.count(")"):
                    found_pos = pos
                    break
                start_search = pos + len(sep)
            if found_pos != -1:
                long_detail = clean_title
                clean_title = clean_title[:found_pos].strip()
                break

    ico = _kind_icon(kind, subkind)
    if kind == "todo":
        # Unmistakable open (`☐`) vs. done (`✅`) checkbox — never stack two check icons!
        summary = f"{'✅' if status == 'done' else '☐'} {clean_title}".strip()
    else:
        prefix = ""
        if status == "done":
            prefix += "✓ "
        if ico:
            prefix += f"{ico} "
        summary = f"{prefix}{clean_title}".strip()

    if long_detail:
        desc_parts.append(long_detail)
    if wko_detail:
        desc_parts.append(wko_detail)
    if feedback_txt:
        desc_parts.append(f"Feedback: {feedback_txt}")
    if existing_description:
        # Keep non-Atlas lines from existing remote description so user notes in Google survive
        kept = [
            ln for ln in existing_description.splitlines()
            if not ln.startswith(("Atlas:", "Dauer:", "Feedback:")) and ln.strip() != (m_wko.group(2).strip() if m_wko else "")
        ]
        kept_txt = "\n".join(kept).strip()
        if kept_txt and kept_txt not in "\n".join(desc_parts):
            desc_parts.append(kept_txt)
    if brain_ref:
        desc_parts.append(f"Atlas: {brain_ref}")
    description = "\n\n".join(p for p in desc_parts if p).strip()

    start_iso = item["start"]
    end_iso = item["end"]
    t_start, t_end, dur_min = _parse_time_window(start_iso, end_iso, item.get("time", ""), item.get("what", ""), kind)

    body: dict[str, Any] = {
        "summary": summary,
        "extendedProperties": {
            "private": {
                "atlas_uid": atlas_uid,
                "atlas_kind": full_kind,
                "atlas_layer": str(item.get("rel") or "calendar/README.md"),
                "atlas_status": status,
            }
        },
    }
    if description:
        body["description"] = description
    if kind == "todo":
        if status == "done":
            body["colorId"] = DONE_TODO_COLOR_ID
        elif str(existing_color_id or "") == DONE_TODO_COLOR_ID:
            # Reopening a previously completed task clears Graphite back to the calendar default
            body["colorId"] = None

    if t_start and t_end:
        sd = dt.date.fromisoformat(start_iso)
        sh, sm = map(int, t_start.split(":"))
        start_dt = dt.datetime(sd.year, sd.month, sd.day, sh, sm).astimezone()
        end_dt = start_dt + dt.timedelta(minutes=dur_min)
        body["start"] = {"dateTime": gcal_provider.to_rfc3339(start_dt), "timeZone": timezone}
        body["end"] = {"dateTime": gcal_provider.to_rfc3339(end_dt), "timeZone": timezone}
        body["transparency"] = "opaque"
    else:
        ed_excl = dt.date.fromisoformat(end_iso) + dt.timedelta(days=1)
        body["start"] = {"date": start_iso}
        body["end"] = {"date": ed_excl.isoformat()}
        # Only `[blocked]` all-day events block FreeBusy; tasks/deadlines/goals stay transparent
        body["transparency"] = "opaque" if kind == "blocked" else "transparent"
        if kind == "birthday":
            body["recurrence"] = ["RRULE:FREQ=YEARLY"]

    return body


def parse_gcal_event(ev: dict[str, Any], default_kind: str = "event") -> dict[str, Any]:
    """Normalize a Google Calendar Event resource into Atlas fields + rich cached metadata."""
    ev_id = str(ev.get("id") or "")
    status_remote = str(ev.get("status") or "confirmed").lower()
    priv = ((ev.get("extendedProperties") or {}).get("private") or {})
    if not isinstance(priv, dict):
        priv = {}

    raw_summary = str(ev.get("summary") or "(Ohne Titel)").strip()
    explicit_open_mark = raw_summary.startswith(("☐ ", "☐", "⬜ ", "⬜"))
    is_done_mark = (
        raw_summary.startswith(("✓ ", "✔ ", "✅ ", "✅"))
        or (priv.get("atlas_status") == "done" and not explicit_open_mark)
    )
    clean_title = LEADING_SYNC_ICON_RX.sub("", raw_summary).strip() or raw_summary
    is_cancelled_title = bool(CANCELLED_TITLE_RX.match(clean_title))

    start_obj = ev.get("start") or {}
    end_obj = ev.get("end") or {}
    start_date = ""
    end_date = ""
    time_str = ""
    all_day = False

    if start_obj.get("dateTime"):
        s_dt = gcal_provider.parse_rfc3339(str(start_obj["dateTime"]))
        e_dt = gcal_provider.parse_rfc3339(str(end_obj.get("dateTime") or ""))
        if s_dt:
            s_loc = s_dt.astimezone()
            start_date = s_loc.date().isoformat()
            if e_dt:
                e_loc = e_dt.astimezone()
                end_date = e_loc.date().isoformat()
                if end_date == start_date:
                    time_str = f"{s_loc.strftime('%H:%M')}-{e_loc.strftime('%H:%M')}"
                else:
                    time_str = s_loc.strftime("%H:%M")
            else:
                end_date = start_date
                time_str = s_loc.strftime("%H:%M")
    elif start_obj.get("date"):
        all_day = True
        start_date = str(start_obj["date"])[:10]
        raw_end = str(end_obj.get("date") or start_date)[:10]
        try:
            sd = dt.date.fromisoformat(start_date)
            ed = dt.date.fromisoformat(raw_end)
            if ed > sd:
                ed = ed - dt.timedelta(days=1)
            end_date = ed.isoformat()
        except ValueError:
            end_date = start_date

    # Infer Atlas kind/subkind
    kind, subkind = (default_kind or "event"), ""
    if priv.get("atlas_kind"):
        ak = str(priv["atlas_kind"])
        if ":" in ak:
            kind, subkind = ak.split(":", 1)
        else:
            kind = ak
    elif default_kind == "birthday":
        kind = "birthday"
    elif str(ev.get("eventType") or "") == "outOfOffice":
        kind = "blocked"
    elif start_date and end_date and end_date != start_date:
        kind = "blocked"
    elif re.search(r"\b(urlaub|dienstreise|vacation|out of office|ooo|blocked|abwesend)\b", clean_title, re.I):
        kind = "blocked"

    atlas_status = "skipped" if (status_remote == "cancelled" or is_cancelled_title) else ("done" if is_done_mark else "open")

    # Extract Google Meet / video link
    meet_link = str(ev.get("hangoutLink") or "").strip()
    if not meet_link:
        conf = ev.get("conferenceData") or {}
        for ep in (conf.get("entryPoints") or []):
            if isinstance(ep, dict) and ep.get("entryPointType") == "video" and ep.get("uri"):
                meet_link = str(ep["uri"]).strip()
                break

    attendees_clean: list[dict[str, str]] = []
    self_rsvp = ""
    for att in (ev.get("attendees") or []):
        if not isinstance(att, dict) or not att.get("email"):
            continue
        item_a = {
            "email": str(att.get("email") or ""),
            "name": str(att.get("displayName") or ""),
            "status": str(att.get("responseStatus") or "needsAction"),
            "self": bool(att.get("self")),
        }
        attendees_clean.append(item_a)
        if item_a["self"]:
            self_rsvp = item_a["status"]

    # If user explicitly declined an external invitation, reflect as skipped `[-]`
    if self_rsvp == "declined" and atlas_status == "open":
        atlas_status = "skipped"

    meta = {
        "location": str(ev.get("location") or "").strip(),
        "description": str(ev.get("description") or "").strip(),
        "meet_link": meet_link,
        "html_link": str(ev.get("htmlLink") or "").strip(),
        "attendees": attendees_clean,
        "self_rsvp": self_rsvp,
        "organizer": str((ev.get("organizer") or {}).get("email") or ""),
        "organizer_self": bool((ev.get("organizer") or {}).get("self", True)),
        "color_id": str(ev.get("colorId") or ""),
        "raw_summary": raw_summary,
        "updated": str(ev.get("updated") or ""),
        "recurring_event_id": str(ev.get("recurringEventId") or ""),
    }

    return {
        "gcal_event_id": ev_id,
        "ical_uid": str(ev.get("iCalUID") or ""),
        "etag": str(ev.get("etag") or ""),
        "atlas_uid": str(priv.get("atlas_uid") or ""),
        "atlas_layer": str(priv.get("atlas_layer") or ""),
        "status_remote": status_remote,
        "status": atlas_status,
        "start": start_date,
        "end": end_date or start_date,
        "time": time_str,
        "all_day": all_day,
        "kind": kind,
        "subkind": subkind,
        "title": clean_title,
        "raw_summary": raw_summary,
        "recurring_event_id": str(ev.get("recurringEventId") or ""),
        "meta": meta,
    }


def render_atlas_line(
    start: str,
    end: str,
    time_str: str,
    kind: str,
    subkind: str,
    what: str,
    status: str = "open",
    atlas_uid: str = "",
    preserve_suffix_from: str = "",
) -> str:
    """Format a clean Atlas Markdown timeline line with optional invisible `<!-- g:<uid> -->`."""
    box = "[x]" if status == "done" else ("[-]" if status == "skipped" else "[ ]")
    d_part = start if (not end or end == start) else f"{start}..{end}"
    t_part = f"{time_str} " if time_str else ""
    k_part = f"[{kind}:{subkind}]" if subkind else f"[{kind}]"
    title = str(what or "").strip()

    # Preserve rich local suffixes (workout interval prescription, brain path, feedback, `#sync`) when updating from Google
    if preserve_suffix_from:
        src_no_fb = FEEDBACK_BLOCK_RX.sub("", preserve_suffix_from).strip()
        m_wko = WORKOUT_PARENS_RX.search(src_no_fb)
        if m_wko and not WORKOUT_PARENS_RX.search(title):
            title = f"{title} ({m_wko.group(1)}m | {m_wko.group(2).strip()})"
        m_path = BRAIN_PATH_TAIL_RX.search(src_no_fb)
        if m_path and m_path.group(1) not in title:
            title = f"{title} ({m_path.group(1)})"
        m_fb = FEEDBACK_BLOCK_RX.search(preserve_suffix_from)
        if m_fb and not FEEDBACK_BLOCK_RX.search(title):
            title = f"{title} [feedback: {m_fb.group(1).strip()}]"
        if FORCE_SYNC_FLAG_RX.search(preserve_suffix_from) and not FORCE_SYNC_FLAG_RX.search(title):
            title = f"{title} #sync"

    id_part = f" <!-- g:{atlas_uid} -->" if atlas_uid else ""
    return f"- {box} {d_part} {t_part}{k_part} {title}{id_part}"


def _canonical_atlas_snapshot(item: dict[str, Any]) -> dict[str, Any]:
    """Canonical representation of an Atlas item for 3-way hash & field-level diffing."""
    what = str(item.get("what") or "").strip()
    what = FEEDBACK_BLOCK_RX.sub("", what).strip()
    m_path = BRAIN_PATH_TAIL_RX.search(what)
    clean_w = what[:m_path.start()].strip() if m_path else what
    m_wko = WORKOUT_PARENS_RX.search(clean_w)
    wko_spec = m_wko.group(0).strip() if m_wko else ""
    core_title = clean_w[:m_wko.start()].strip() if m_wko else clean_w
    core_title = LEADING_SYNC_ICON_RX.sub("", LAYER_TAG_RX.sub(" ", core_title)).strip()
    return {
        "start": str(item.get("start") or ""),
        "end": str(item.get("end") or ""),
        "time": str(item.get("time") or ""),
        "kind": str(item.get("kind") or "event"),
        "subkind": str(item.get("subkind") or ""),
        "status": str(item.get("status") or "open"),
        "title": core_title,
        "wko_spec": wko_spec,
        "is_local": bool(item.get("is_local")),
    }


def _canonical_gcal_snapshot(g_ev: dict[str, Any]) -> dict[str, Any]:
    return {
        "start": str(g_ev.get("start") or ""),
        "end": str(g_ev.get("end") or ""),
        "time": str(g_ev.get("time") or ""),
        "status": str(g_ev.get("status") or "open"),
        "title": str(g_ev.get("title") or "").strip(),
        "location": str((g_ev.get("meta") or {}).get("location") or ""),
        "meet_link": str((g_ev.get("meta") or {}).get("meet_link") or ""),
    }


def _hash_snapshot(snap: dict[str, Any]) -> str:
    raw = json.dumps(snap, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _normalize_title_for_dedup(text: str) -> str:
    s = str(text or "").lower()
    s = BRAIN_PATH_TAIL_RX.sub("", s)
    s = WORKOUT_PARENS_RX.sub("", s)
    s = re.sub(r"\((?:google meet|zoom|teams|tel\.?|ort)[^)]*\)", " ", s, flags=re.I)
    s = LEADING_SYNC_ICON_RX.sub("", s)
    s = (
        s.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")
    )
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    return s


def _title_headline(text: str) -> str:
    s = str(text or "")
    for sep in (" — ", " – ", " - ", " ("):
        if sep in s:
            head = s.split(sep, 1)[0].strip()
            if len(head) >= 4:
                return head
    return s


def title_similarity(a: str, b: str) -> float:
    """Deterministic similarity in [0.0, 1.0] with token overlap, German compound matching & char trigrams."""
    def _pair_score(sa: str, sb: str) -> float:
        na = _normalize_title_for_dedup(sa)
        nb = _normalize_title_for_dedup(sb)
        if not na or not nb:
            return 0.0
        if na == nb:
            return 1.0
        ta = {w for w in na.split() if len(w) >= 2 and w not in _DEDUP_STOPWORDS}
        tb = {w for w in nb.split() if len(w) >= 2 and w not in _DEDUP_STOPWORDS}
        if not ta or not tb:
            ta = set(na.split())
            tb = set(nb.split())
        exact_inter = len(ta & tb)
        # German compound/prefix matching (e.g. `zahnarzt` inside `zahnarzttermin`)
        comp_hits = 0
        for wa in ta:
            if wa in tb:
                continue
            if len(wa) >= 5 and any(len(wb) >= 5 and (wa in wb or wb in wa) for wb in tb):
                comp_hits += 1
        eff_inter = exact_inter + comp_hits
        union = max(len(ta | tb) - comp_hits, 1)
        jaccard = eff_inter / union
        contain = eff_inter / max(min(len(ta), len(tb)), 1)

        def _trigrams(s: str) -> set[str]:
            padded = f"  {s}  "
            return {padded[i:i + 3] for i in range(max(len(padded) - 2, 1))}

        ga, gb = _trigrams(na), _trigrams(nb)
        tri_dice = (2.0 * len(ga & gb)) / max(len(ga) + len(gb), 1)

        if eff_inter >= 2 and contain >= 0.85:
            return min(1.0, max(0.82, 0.55 * contain + 0.45 * tri_dice))
        if eff_inter >= 1 and (na in nb or nb in na) and min(len(na), len(nb)) >= 6:
            return min(1.0, max(0.80, 0.5 * contain + 0.5 * tri_dice))
        if comp_hits >= 1 and contain >= 0.85:
            return min(1.0, max(0.78, 0.55 * contain + 0.45 * tri_dice))
        return round(0.45 * jaccard + 0.25 * contain + 0.30 * tri_dice, 4)

    base = _pair_score(a, b)
    ha, hb = _title_headline(a), _title_headline(b)
    if ha != a or hb != b:
        return max(base, _pair_score(ha, hb))
    return base


def _time_to_min(hhmm: str) -> int | None:
    try:
        h, m = map(int, hhmm.split(":"))
        return h * 60 + m
    except Exception:
        return None


def _time_window_compatible(a_item: dict[str, Any], g_ev: dict[str, Any]) -> tuple[bool, bool]:
    """Return `(compatible, exact_slot_trigger)` for deduplication matching."""
    if a_item["start"] != g_ev["start"]:
        return False, False
    a_t = str(a_item.get("time") or "").strip()
    g_t = str(g_ev.get("time") or "").strip()
    if not a_t and not g_t:
        same_end = (a_item["end"] == g_ev["end"])
        is_exact_multiday = bool(same_end and a_item["end"] > a_item["start"])
        return same_end, is_exact_multiday
    if bool(a_t) != bool(g_t):
        # One timed, one all-day on same date -> still checkable if titles match very closely
        return True, False
    a_s = _time_to_min(a_t.split("-")[0])
    g_s = _time_to_min(g_t.split("-")[0])
    if a_s is None or g_s is None:
        return False, False
    diff = abs(a_s - g_s)
    if diff <= 15:
        return True, (diff == 0)
    # Check interval overlap
    a_e = _time_to_min(a_t.split("-")[1]) if "-" in a_t else (a_s + 60)
    g_e = _time_to_min(g_t.split("-")[1]) if "-" in g_t else (g_s + 60)
    if a_e is not None and g_e is not None:
        if max(a_s, g_s) < min(a_e, g_e):
            return True, False
    return False, False


# ---------------------------------------------------------------------------
# 4. Calendar Discovery & Layer Setup (`atlas.py cal discover`)
# ---------------------------------------------------------------------------

def _slugify_layer_name(name: str) -> str:
    s = str(name or "").lower()
    s = s.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s[:28] or "calendar"


def _ensure_layer_md_file(brain: str, layer: dict[str, Any]) -> str:
    """Create `brain/calendar/<file>` with declarative frontmatter if it does not exist yet."""
    fn = str(layer.get("file") or "README.md")
    full = os.path.join(brain, "calendar", fn)
    if os.path.isfile(full):
        return full
    os.makedirs(os.path.dirname(full), exist_ok=True)
    label = str(layer.get("label") or layer.get("id") or "Calendar")
    mode = str(layer.get("mode") or "bidirectional")
    color = str(layer.get("color") or "#38bdf8")
    lid = str(layer.get("id") or _slugify_layer_name(label))
    today_iso = dt.date.today().isoformat()
    note = (
        "Read-only mirror from external calendar (auto-managed by Atlas Calendar Sync — do not edit manually)."
        if mode == "read-only"
        else f"Bidirectional Atlas calendar layer for {label}."
    )
    content = (
        "---\n"
        f"summary: Calendar layer {label} ({mode})\n"
        "tags: [atlas, calendar]\n"
        f"cal_layer: {lid}\n"
        f"cal_label: {label}\n"
        f"cal_color: \"{color}\"\n"
        f"cal_mode: {mode}\n"
        "---\n"
        f"# Calendar: {label}\n\n"
        f"Stand: {today_iso}. {note}\n\n"
        "## Timeline\n"
    )
    cal_engine._atomic_write_text(full, content)
    return full


def discover_calendars(
    brain: str,
    provider: gcal_provider.CalendarProvider | None = None,
    execute: bool = False,
) -> dict[str, Any]:
    """Discover calendars across all connected accounts (Google + CalDAV/Apple), deduplicate, and build `sync.yaml`."""
    prov = provider or build_default_provider(brain)
    accounts = prov.list_accounts()
    connected = [a for a in accounts if a.get("connected")]
    if not connected:
        return {
            "ok": False,
            "error": (
                "No connected calendar accounts. Run `atlas.py cal auth --account personal` (Google) "
                "or `atlas.py cal auth --provider icloud --account icloud` (Apple iCloud / CalDAV) first."
            ),
            "accounts": accounts,
            "layers": [],
        }

    existing_cfg = load_sync_config(brain) or _deep_merge_defaults({})
    tz = existing_cfg.get("timezone") or gcal_provider.local_timezone_name()
    wko_cal_name = str(existing_cfg["routing"].get("workout_calendar_name") or "Atlas Coach")
    tasks_cal_name = str(existing_cfg["routing"].get("tasks_calendar_name") or "Atlas Tasks & Fristen")

    # 1. Fetch calendar lists from all connected accounts
    raw_by_acct: dict[str, list[dict[str, Any]]] = {}
    acct_provider_map: dict[str, str] = {}
    warnings: list[str] = []
    default_prov_name = getattr(prov, "provider_name", "gcal")
    if default_prov_name == "multi":
        default_prov_name = "gcal"
    for acct in connected:
        aid = acct["id"]
        acct_provider_map[aid] = str(acct.get("provider") or default_prov_name)
        raw_by_acct[aid] = prov.list_calendars(aid)

    # 2. Deduplicate shared calendarIds across accounts within the same provider (primary/owner wins)
    role_rank = {"owner": 3, "writer": 2, "reader": 1, "freeBusyReader": 0}
    best_owner: dict[tuple[str, str], tuple[int, str, str, dict[str, Any]]] = {}
    duplicates_skipped: list[dict[str, str]] = []

    for acct in connected:
        aid = acct["id"]
        aprov = acct_provider_map[aid]
        for cal in raw_by_acct.get(aid, []):
            cid = cal["id"]
            dedup_key = (aprov, cid)
            score = (10 if cal.get("primary") else 0) + role_rank.get(cal.get("access_role", ""), 0)
            prev = best_owner.get(dedup_key)
            if prev is None or score > prev[0]:
                if prev is not None:
                    duplicates_skipped.append({"calendar_id": cid, "skipped_account": prev[1], "owner_account": aid})
                best_owner[dedup_key] = (score, aid, aprov, cal)
            else:
                duplicates_skipped.append({"calendar_id": cid, "skipped_account": aid, "owner_account": prev[1]})

    # Choose default account: keep existing README.md owner if still connected, else first connected account
    existing_readme = next(
        (ly for ly in (existing_cfg.get("layers") or []) if isinstance(ly, dict) and ly.get("file") == "README.md"),
        None,
    )
    connected_ids = {a["id"] for a in connected}
    default_aid = (
        str(existing_readme["account"])
        if existing_readme and str(existing_readme.get("account") or "") in connected_ids
        else connected[0]["id"]
    )

    # Find or plan creation of `Atlas Coach` and `Atlas Tasks & Fristen` on default account
    wko_cal_id = ""
    tasks_cal_id = ""
    subcals_to_create: list[str] = []
    for cal in raw_by_acct.get(default_aid, []):
        low_s = cal["summary"].strip().lower()
        if low_s == wko_cal_name.strip().lower():
            wko_cal_id = cal["id"]
        elif low_s == tasks_cal_name.strip().lower():
            tasks_cal_id = cal["id"]

    if not wko_cal_id:
        if execute:
            try:
                created = prov.create_calendar(default_aid, wko_cal_name, time_zone=tz)
                wko_cal_id = created["id"]
            except gcal_provider.CalendarProviderError as exc:
                warnings.append(f"Could not create subcalendar '{wko_cal_name}' on {default_aid}: {exc}")
        else:
            subcals_to_create.append(wko_cal_name)
    if not tasks_cal_id:
        if execute:
            try:
                created = prov.create_calendar(default_aid, tasks_cal_name, time_zone=tz)
                tasks_cal_id = created["id"]
            except gcal_provider.CalendarProviderError as exc:
                warnings.append(f"Could not create subcalendar '{tasks_cal_name}' on {default_aid}: {exc}")
        else:
            subcals_to_create.append(tasks_cal_name)

    # 3. Build layers list (preserve user overrides from existing sync.yaml if already mapped)
    existing_by_cid = {
        str(ly.get("calendar_id") or ""): ly
        for ly in (existing_cfg.get("layers") or [])
        if isinstance(ly, dict) and ly.get("calendar_id")
    }
    layers: list[dict[str, Any]] = []
    skipped_freebusy_only: list[dict[str, str]] = []
    used_files: set[str] = {
        str(ly.get("file") or "")
        for ly in existing_by_cid.values()
        if ly.get("file")
    }
    color_idx = 0

    for (_aprov_k, cid), (_score, aid, aprov, cal) in best_owner.items():
        # Skip system Google calendars (Contacts/Birthdays & Public Holidays) by default
        if "#contacts@group.v.calendar.google.com" in cid or "#holiday@group.v.calendar.google.com" in cid:
            continue
        # Skip the Atlas-managed subcalendars as standalone layers (attached to default layer)
        if cid in (wko_cal_id, tasks_cal_id):
            continue
        # Never create a broken layer full of "(No Title)" for `freeBusyReader`-only shared calendars
        if cal.get("access_role") == "freeBusyReader":
            skipped_freebusy_only.append({
                "account": aid,
                "calendar_id": cid,
                "summary": str(cal.get("summary") or cid),
                "hint": "Connect the owning account via `atlas.py cal auth` or upgrade calendar sharing to reader/writer.",
            })
            continue

        prev_ly = existing_by_cid.get(cid)
        if prev_ly:
            ly = dict(prev_ly)
            ly.setdefault("provider", aprov)
            if aid == default_aid and cal.get("primary"):
                sub = dict(ly.get("subcalendars") or {})
                if wko_cal_id:
                    sub["workout"] = wko_cal_id
                if tasks_cal_id:
                    sub["tasks"] = tasks_cal_id
                ly["subcalendars"] = sub
            layers.append(ly)
            used_files.add(str(ly.get("file") or ""))
            continue

        is_default_primary = (aid == default_aid and cal.get("primary") and "README.md" not in used_files)
        cal_summary = str(cal.get("summary") or "")
        is_birthday_cal = bool(re.search(r"(?:geburtstag|birthday)", cal_summary, re.I))
        if is_default_primary:
            fn = "README.md"
            lid = aid
            label = "Personal" if aid in ("personal", "privat", "default") else (cal_summary or aid.capitalize())
            mode = "bidirectional"
        elif cal.get("primary"):
            lid = aid
            fn = f"{aid}.md"
            if fn in used_files:
                lid = f"{aprov}-{aid}"
                fn = f"{lid}.md"
            label = cal_summary or aid.capitalize()
            mode = "bidirectional" if cal.get("access_role") != "reader" else "read-only"
        else:
            lid = _slugify_layer_name(cal_summary or cid.split("@")[0])
            fn = f"{lid}.md"
            if fn in used_files:
                fn = f"{aid}-{lid}.md"
                lid = f"{aid}-{lid}"
            label = cal_summary or lid
            # Secondary calendars default to read-only (safe against external mirror loops); owner birthday calendars can be bidirectional
            mode = "bidirectional" if (is_birthday_cal and cal.get("access_role") == "owner") else "read-only"

        used_files.add(fn)
        color = cal.get("background_color") or _DEFAULT_LAYER_COLORS[color_idx % len(_DEFAULT_LAYER_COLORS)]
        color_idx += 1
        ly_entry: dict[str, Any] = {
            "id": lid,
            "file": fn,
            "label": label,
            "color": color,
            "provider": aprov,
            "account": aid,
            "calendar_id": cid,
            "mode": mode,
        }
        if is_birthday_cal:
            ly_entry["default_kind"] = "birthday"
            ly_entry["freebusy"] = False
        elif mode == "read-only" and FREEBUSY_EXCLUDE_NAME_RX.search(label):
            ly_entry["freebusy"] = False
        if is_default_primary:
            ly_entry["subcalendars"] = {
                "workout": wko_cal_id,
                "tasks": tasks_cal_id,
            }
        layers.append(ly_entry)

    # Sort so README.md is always first, then bidirectional, then read-only
    layers.sort(key=lambda x: (0 if x["file"] == "README.md" else (1 if x["mode"] == "bidirectional" else 2), x["file"]))
    existing_cfg["layers"] = layers
    existing_cfg["timezone"] = tz
    existing_cfg["enabled"] = True
    prov_set = {str(ly.get("provider") or "gcal") for ly in layers}
    if len(prov_set) == 1:
        existing_cfg["provider"] = next(iter(prov_set))
    elif len(prov_set) > 1:
        existing_cfg["provider"] = "multi"

    created_files: list[str] = []
    saved_cfg_path = ""
    if execute:
        saved_cfg_path = save_sync_config(brain, existing_cfg)
        for ly in layers:
            if ly.get("mode") != "ignore":
                target = os.path.join(brain, "calendar", ly["file"])
                existed = os.path.isfile(target)
                _ensure_layer_md_file(brain, ly)
                if not existed:
                    created_files.append(f"calendar/{ly['file']}")

    return {
        "ok": True,
        "applied": bool(execute),
        "config_path": saved_cfg_path or sync_config_path(brain),
        "accounts": [a["id"] for a in connected],
        "layers": layers,
        "subcalendars_created_or_planned": {
            "workout": wko_cal_id or f"(will create '{wko_cal_name}')",
            "tasks": tasks_cal_id or f"(will create '{tasks_cal_name}')",
        },
        "subcals_to_create": subcals_to_create,
        "duplicates_skipped": duplicates_skipped,
        "skipped_freebusy_only": skipped_freebusy_only,
        "warnings": warnings,
        "created_files": created_files,
    }


# ---------------------------------------------------------------------------
# 5. Routing & Decentralized `due:` Collector
# ---------------------------------------------------------------------------

def _should_sync_item(item: dict[str, Any], cfg: dict[str, Any], layer: dict[str, Any] | None = None) -> bool:
    if item.get("is_local"):
        return False
    if item.get("force_sync"):
        return True
    kind = str(item.get("kind") or "event").lower()
    if layer and str(layer.get("default_kind") or "").lower() == kind:
        return True
    routing = cfg.get("routing") or {}
    sync_kinds = {str(k).lower() for k in (routing.get("sync_kinds") or DEFAULT_CONFIG["routing"]["sync_kinds"])}
    local_only = {str(k).lower() for k in (routing.get("local_only_kinds") or ["birthday"])}
    if kind in local_only:
        return False
    return kind in sync_kinds


def _resolve_target_calendar(
    item: dict[str, Any],
    source_layer: dict[str, Any],
    all_layers: list[dict[str, Any]],
) -> tuple[str, str]:
    """Return `(account_id, calendar_id)` for pushing an Atlas item."""
    # Explicit `@layer` override in line
    ov = str(item.get("layer_override") or "").lower()
    if ov:
        for ly in all_layers:
            if str(ly.get("id") or "").lower() == ov and ly.get("mode") == "bidirectional":
                return str(ly["account"]), str(ly["calendar_id"])

    kind = str(item.get("kind") or "event").lower()
    subcals = source_layer.get("subcalendars") or {}
    if kind == "workout" and subcals.get("workout"):
        return str(source_layer["account"]), str(subcals["workout"])
    if kind in ("todo", "deadline") and subcals.get("tasks"):
        return str(source_layer["account"]), str(subcals["tasks"])
    return str(source_layer["account"]), str(source_layer["calendar_id"])


def _collect_decentralized_due_items(brain: str, today: dt.date) -> list[dict[str, Any]]:
    """Scan YAML frontmatter `due:` entries across `brain/`, deduplicating folder README vs detail page mirrors."""
    candidates: list[dict[str, Any]] = []
    skip_dirs = {".git", "_atlas", "_secrets", "_archive", "calendar", "journal", "inbox", "__pycache__"}
    for dp, dns, fns in os.walk(brain):
        dns[:] = sorted(d for d in dns if d not in skip_dirs and not d.startswith("."))
        for fn in sorted(fns):
            if not fn.lower().endswith(".md"):
                continue
            full = os.path.join(dp, fn)
            rel = os.path.relpath(full, brain).replace("\\", "/")
            try:
                with open(full, encoding="utf-8", errors="replace") as fh:
                    lines = fh.read(8192).splitlines()
            except OSError:
                continue
            for _idx, _pref, raw_val in cal_engine._find_frontmatter_due_lines(lines):
                parsed = parse_atlas_timeline_line(raw_val, rel, line_no=0, today=today)
                if not parsed:
                    continue
                if not KIND_PREFIX_RX.match(TIME_PREFIX_RX.sub(r"\2", raw_val.split(" ", 1)[-1] if " " in raw_val else "")):
                    if parsed["kind"] == "event":
                        parsed["kind"] = "deadline"
                parsed["sync_id"] = _due_uid(rel, raw_val)
                parsed["is_decentralized_due"] = True
                candidates.append(parsed)
    # Prefer specific detail pages over folder READMEs when deduplicating same-date `due:` entries
    candidates.sort(key=lambda it: (1 if it["rel"].endswith("/README.md") else 0, it["rel"], it["start"]))
    out: list[dict[str, Any]] = []
    for cand in candidates:
        dup = False
        for ex in out:
            if ex["start"] == cand["start"] and ex["end"] == cand["end"] and title_similarity(ex["what"], cand["what"]) >= 0.65:
                dup = True
                break
        if not dup:
            out.append(cand)
    return out


# ---------------------------------------------------------------------------
# 6. Core 3-Way Bidirectional Sync (`run_sync`)
# ---------------------------------------------------------------------------

def _in_window(date_start: str, date_end: str, today: dt.date, past_days: int, future_days: int) -> bool:
    try:
        sd = dt.date.fromisoformat(date_start)
        ed = dt.date.fromisoformat(date_end or date_start)
    except ValueError:
        return False
    min_d = today - dt.timedelta(days=past_days)
    max_d = today + dt.timedelta(days=future_days)
    return ed >= min_d and sd <= max_d


def _rewrite_readonly_layer_md(brain: str, layer: dict[str, Any], events: list[dict[str, Any]]) -> bool:
    """Deterministically rewrite `## Timeline` of a read-only calendar layer file."""
    fn = str(layer["file"])
    full = _ensure_layer_md_file(brain, layer)
    with open(full, encoding="utf-8", errors="replace") as fh:
        existing = fh.read()
    lines = existing.splitlines()
    tl_idx = None
    next_sec = len(lines)
    for i, ln in enumerate(lines):
        if ln.strip().lower().startswith("## timeline"):
            tl_idx = i
        elif tl_idx is not None and i > tl_idx and ln.startswith("## "):
            next_sec = i
            break

    rendered: list[str] = []
    for ev in sorted(events, key=lambda x: (x["start"], x["time"], x["title"])):
        if ev["status_remote"] == "cancelled":
            continue
        rendered.append(
            render_atlas_line(
                ev["start"],
                ev["end"],
                ev["time"],
                ev["kind"],
                ev["subkind"],
                ev["title"],
                status=ev["status"],
                atlas_uid=ev["atlas_uid"],
            )
        )

    if tl_idx is None:
        new_lines = lines + ["", "## Timeline", ""] + rendered
    else:
        new_lines = lines[:tl_idx + 1] + [""] + rendered + ([""] + lines[next_sec:] if next_sec < len(lines) else [])
    new_content = "\n".join(new_lines).rstrip() + "\n"
    if new_content == existing:
        return False
    err = cal_engine._atomic_write_text(full, new_content)
    if err:
        raise OSError(err)
    return True


def _apply_markdown_edits(
    brain: str,
    rel: str,
    replacements: dict[int, str],
    insertions: list[tuple[str, str]],
) -> bool:
    """Apply line replacements (1-based line_no -> new_line) and chronological insertions into `brain/<rel>`."""
    if not replacements and not insertions:
        return False
    full = os.path.join(brain, *rel.split("/"))
    if not os.path.isfile(full):
        return False
    with open(full, encoding="utf-8", errors="replace") as fh:
        existing = fh.read()
    lines = existing.splitlines()
    for ln_no, new_ln in replacements.items():
        if 1 <= ln_no <= len(lines):
            lines[ln_no - 1] = new_ln
    for target_date, entry_line in sorted(insertions, key=lambda x: (x[0], x[1])):
        cal_engine._insert_into_timeline_lines(lines, entry_line, target_date)
    new_content = "\n".join(lines).rstrip() + "\n"
    if new_content == existing:
        return False
    err = cal_engine._atomic_write_text(full, new_content)
    if err:
        raise OSError(err)
    return True


def _upsert_conflict(
    conn: sqlite3.Connection,
    kind: str,
    atlas_uid: str,
    layer_file: str,
    account_id: str,
    calendar_id: str,
    gcal_event_id: str,
    date_iso: str,
    summary: str,
    atlas_obj: dict[str, Any],
    gcal_obj: dict[str, Any],
    score: float = 0.0,
) -> str:
    # Avoid creating duplicate open conflicts for the same pair
    row = conn.execute(
        "SELECT id FROM conflicts WHERE status='open' AND kind=? AND layer_file=? AND gcal_event_id=? AND date_iso=?",
        (kind, layer_file, gcal_event_id, date_iso),
    ).fetchone()
    if row:
        return str(row["id"])
    cid = secrets.token_hex(4)
    conn.execute(
        """
        INSERT INTO conflicts
        (id, kind, atlas_uid, layer_file, account_id, calendar_id, gcal_event_id, date_iso, summary, atlas_json, gcal_json, score, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)
        """,
        (
            cid, kind, atlas_uid, layer_file, account_id, calendar_id, gcal_event_id,
            date_iso, summary, json.dumps(atlas_obj, ensure_ascii=False),
            json.dumps(gcal_obj, ensure_ascii=False), float(score), time.time(),
        ),
    )
    return cid


def run_sync(
    brain: str,
    provider: gcal_provider.CalendarProvider | None = None,
    execute: bool = False,
    pull_only: bool = False,
    push_only: bool = False,
    layer_filter: str = "",
    allow_bulk_delete: bool = False,
    reformat: bool = False,
    today: dt.date | None = None,
    timeout: float = 15.0,
) -> dict[str, Any]:
    """Execute a 3-way calendar sync (or dry-run) across all configured layers."""
    cfg = load_sync_config(brain)
    if not cfg or not cfg.get("enabled", True):
        return {"ok": False, "error": "Calendar sync is not configured or disabled (`brain/calendar/sync.yaml`)."}

    today = today or dt.date.today()
    prov = provider or build_default_provider(brain, timeout=timeout)
    tz = str(cfg.get("timezone") or gcal_provider.local_timezone_name())
    past_days = int(cfg.get("window_past_days") or 14)
    rec_days = int(cfg.get("window_recurring_days") or 90)
    single_days = int(cfg.get("window_single_days") or 365)
    bulk_limit = int(cfg.get("bulk_delete_limit") if cfg.get("bulk_delete_limit") is not None else 3)

    t_min_dt = dt.datetime.combine(today - dt.timedelta(days=past_days), dt.time.min).astimezone()
    t_max_dt = dt.datetime.combine(today + dt.timedelta(days=single_days), dt.time.max).astimezone()
    rec_t_max_dt = dt.datetime.combine(today + dt.timedelta(days=rec_days), dt.time.max).astimezone()
    time_min = gcal_provider.to_rfc3339(t_min_dt)
    time_max = gcal_provider.to_rfc3339(t_max_dt)
    rec_time_max = gcal_provider.to_rfc3339(rec_t_max_dt)

    layers = [
        ly for ly in (cfg.get("layers") or [])
        if isinstance(ly, dict) and ly.get("mode") != "ignore"
    ]
    if layer_filter:
        lf = layer_filter.strip().lower()
        layers = [ly for ly in layers if str(ly.get("id") or "").lower() == lf or str(ly.get("file") or "").lower() == lf]

    report: dict[str, Any] = {
        "ok": True,
        "applied": bool(execute),
        "pulled_new": 0,
        "pulled_updated": 0,
        "pulled_cancelled": 0,
        "pushed_new": 0,
        "pushed_updated": 0,
        "pushed_deleted": 0,
        "auto_linked": 0,
        "smart_merged": 0,
        "conflicts_added": 0,
        "open_conflicts": 0,
        "bulk_delete_blocked": False,
        "actions": [],
        "warnings": [],
        "auth_warnings": [],
        "files_changed": [],
    }

    with cal_engine.brain_write_lock(brain):
        conn = _open_db(brain)
        try:
            # Map of read-only remote events (for cross-layer dedup against bidirectional layers)
            readonly_events_index: list[dict[str, Any]] = []
            # Collect remote delete operations across all layers so Bulk-Delete Circuit Breaker checks globally
            pending_remote_deletes: list[dict[str, Any]] = []

            # Helper to pull one (account_id, calendar_id) and return `(pulled_events, is_full_sync, next_sync_token)`
            def _pull_cal(account_id: str, calendar_id: str, default_kind: str = "event") -> tuple[list[dict[str, Any]], bool, str] | None:
                cur_row = conn.execute(
                    "SELECT sync_token FROM calendar_cursors WHERE account_id=? AND calendar_id=?",
                    (account_id, calendar_id),
                ).fetchone()
                stoken = str(cur_row["sync_token"]) if cur_row else ""
                try:
                    res = prov.pull_events(
                        account_id,
                        calendar_id,
                        time_min=time_min,
                        time_max=time_max,
                        sync_token=stoken,
                        single_events=True,
                    )
                except gcal_provider.CalendarProviderError as exc:
                    if exc.code in ("refresh_expired", "unauthorized"):
                        if account_id not in report["auth_warnings"]:
                            report["auth_warnings"].append(account_id)
                    report["warnings"].append(f"{account_id}/{calendar_id}: {exc}")
                    return None
                is_full = bool(res.get("full_sync"))
                expanded_raw: list[dict[str, Any]] = []
                for raw_ev in res.get("events") or []:
                    st_low = str(raw_ev.get("status") or "").lower()
                    # Upstream fix: when incremental syncToken delta returns a recurring master event
                    # (`recurrence: [...]`, no `recurringEventId`), expand instances in the rolling window
                    # so `[x]`/`[-]` only ever patches individual occurrences instead of the series master!
                    if (
                        not is_full
                        and raw_ev.get("recurrence")
                        and not raw_ev.get("recurringEventId")
                        and st_low != "cancelled"
                    ):
                        inst_max = time_max if default_kind == "birthday" else rec_time_max
                        try:
                            insts = prov.list_event_instances(
                                account_id, calendar_id, str(raw_ev["id"]),
                                time_min=time_min, time_max=inst_max,
                            )
                        except gcal_provider.CalendarProviderError:
                            insts = []
                        if insts:
                            expanded_raw.extend(insts)
                            continue
                    # When a recurring master series is deleted (Google or CalDAV `.ics` 404),
                    # propagate cancellation to all active linked child instances in SQLite.
                    if st_low == "cancelled" and raw_ev.get("id"):
                        child_rows = conn.execute(
                            "SELECT gcal_event_id, date_start, date_end, last_gcal_snapshot FROM event_links "
                            "WHERE account_id=? AND calendar_id=? AND recurring_event_id=? AND status='active'",
                            (account_id, calendar_id, str(raw_ev["id"])),
                        ).fetchall()
                        for cr in child_rows:
                            snap_c = json.loads(cr["last_gcal_snapshot"] or "{}")
                            expanded_raw.append({
                                "id": str(cr["gcal_event_id"]),
                                "status": "cancelled",
                                "summary": snap_c.get("title", ""),
                                "recurringEventId": str(raw_ev["id"]),
                                "start": {"date": str(cr["date_start"])},
                                "end": {"date": str(cr["date_end"] or cr["date_start"])},
                            })
                    expanded_raw.append(raw_ev)

                parsed_evs: list[dict[str, Any]] = []
                for raw_ev in expanded_raw:
                    pe = parse_gcal_event(raw_ev, default_kind=default_kind)
                    # Enforce rolling window cap: recurring instances capped at +window_recurring_days (full +365d for birthday layers)
                    if pe["status_remote"] != "cancelled" and pe["start"]:
                        max_d = single_days if (default_kind == "birthday" or not pe["recurring_event_id"]) else rec_days
                        if not _in_window(pe["start"], pe["end"], today, past_days, max_d):
                            continue
                    parsed_evs.append(pe)
                return parsed_evs, is_full, str(res.get("next_sync_token") or "")

            def _save_cursor(account_id: str, calendar_id: str, next_tok: str, full_sync: bool) -> None:
                now = time.time()
                conn.execute(
                    """
                    INSERT INTO calendar_cursors(account_id, calendar_id, sync_token, last_full_sync_ts, last_pull_ts)
                    VALUES(?, ?, ?, ?, ?)
                    ON CONFLICT(account_id, calendar_id) DO UPDATE SET
                        sync_token=CASE WHEN excluded.sync_token != '' THEN excluded.sync_token ELSE calendar_cursors.sync_token END,
                        last_full_sync_ts=CASE WHEN ? THEN excluded.last_full_sync_ts ELSE calendar_cursors.last_full_sync_ts END,
                        last_pull_ts=excluded.last_pull_ts
                    """,
                    (account_id, calendar_id, next_tok, now if full_sync else 0.0, now, 1 if full_sync else 0),
                )

            # ---------------------------------------------------------------
            # PASS A: Read-Only Layers (strict pull-only mirror, e.g. OGCS work calendar)
            # ---------------------------------------------------------------
            for ly in layers:
                if ly.get("mode") != "read-only":
                    continue
                rel = f"calendar/{ly['file']}"
                aid = str(ly["account"])
                cid = str(ly["calendar_id"])

                if not push_only:
                    pulled = _pull_cal(aid, cid, default_kind=str(ly.get("default_kind") or "event"))
                    if pulled is None:
                        continue
                    evs, is_full, next_tok = pulled
                    seen_gids: set[str] = set()
                    for ev in evs:
                        gid = ev["gcal_event_id"]
                        seen_gids.add(gid)
                        row = conn.execute(
                            "SELECT atlas_uid, last_gcal_hash FROM event_links WHERE account_id=? AND calendar_id=? AND gcal_event_id=?",
                            (aid, cid, gid),
                        ).fetchone()
                        uid = str(row["atlas_uid"]) if row else (ev["atlas_uid"] or _new_atlas_uid())
                        ev["atlas_uid"] = uid
                        g_snap = _canonical_gcal_snapshot(ev)
                        g_hash = _hash_snapshot(g_snap)
                        meta = dict(ev["meta"])
                        meta["layer_label"] = str(ly.get("label") or ly.get("id") or "")
                        meta["layer_color"] = str(ly.get("color") or "#f59e0b")
                        meta["read_only"] = True

                        if ev["status_remote"] == "cancelled":
                            if row:
                                report["pulled_cancelled"] += 1
                                report["actions"].append(f"[RO cancel] {rel}: {ev['start']} {ev['title']}")
                                if execute:
                                    conn.execute(
                                        "UPDATE event_links SET status='deleted_remote', updated_at=? WHERE atlas_uid=?",
                                        (time.time(), uid),
                                    )
                            continue

                        if g_snap.get("start"):
                            readonly_events_index.append(g_snap)

                        if not row:
                            report["pulled_new"] += 1
                            report["actions"].append(f"[RO pull+] {rel}: {ev['start']} {ev['time']} {ev['title']}".strip())
                        elif row["last_gcal_hash"] != g_hash:
                            report["pulled_updated"] += 1
                            report["actions"].append(f"[RO pull~] {rel}: {ev['start']} {ev['time']} {ev['title']}".strip())

                        if execute:
                            conn.execute(
                                """
                                INSERT INTO event_links
                                (atlas_uid, layer_file, account_id, calendar_id, gcal_event_id, ical_uid, etag,
                                 recurring_event_id, date_start, date_end, last_atlas_hash, last_gcal_hash,
                                 last_atlas_snapshot, last_gcal_snapshot, meta_json, status, updated_at)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)
                                ON CONFLICT(atlas_uid) DO UPDATE SET
                                    etag=excluded.etag, date_start=excluded.date_start, date_end=excluded.date_end,
                                    last_gcal_hash=excluded.last_gcal_hash, last_gcal_snapshot=excluded.last_gcal_snapshot,
                                    meta_json=excluded.meta_json, status='active', updated_at=excluded.updated_at
                                """,
                                (
                                    uid, rel, aid, cid, gid, ev["ical_uid"], ev["etag"],
                                    ev["recurring_event_id"], ev["start"], ev["end"], "", g_hash,
                                    "{}", json.dumps(g_snap, ensure_ascii=False),
                                    json.dumps(meta, ensure_ascii=False), time.time(),
                                ),
                            )

                    if is_full and execute:
                        # Any previously active event absent from full-window pull was deleted remotely
                        active_rows = conn.execute(
                            "SELECT atlas_uid, gcal_event_id, date_start, date_end FROM event_links "
                            "WHERE account_id=? AND calendar_id=? AND status='active'",
                            (aid, cid),
                        ).fetchall()
                        for ar in active_rows:
                            if ar["gcal_event_id"] not in seen_gids and _in_window(ar["date_start"], ar["date_end"], today, past_days, single_days):
                                conn.execute("UPDATE event_links SET status='deleted_remote', updated_at=? WHERE atlas_uid=?", (time.time(), ar["atlas_uid"]))
                                report["pulled_cancelled"] += 1

                    if execute:
                        _save_cursor(aid, cid, next_tok, is_full)
                        # Materialize full current active set for read-only mirror file
                        all_active = conn.execute(
                            "SELECT atlas_uid, date_start, date_end, last_gcal_snapshot, meta_json, recurring_event_id "
                            "FROM event_links WHERE account_id=? AND calendar_id=? AND status='active'",
                            (aid, cid),
                        ).fetchall()
                        mirror_evs: list[dict[str, Any]] = []
                        for ar in all_active:
                            max_d = rec_days if ar["recurring_event_id"] else single_days
                            if not _in_window(ar["date_start"], ar["date_end"], today, past_days, max_d):
                                continue
                            snap = json.loads(ar["last_gcal_snapshot"] or "{}")
                            mirror_evs.append({
                                "atlas_uid": ar["atlas_uid"],
                                "status_remote": "confirmed",
                                "status": snap.get("status", "open"),
                                "start": snap.get("start") or ar["date_start"],
                                "end": snap.get("end") or ar["date_end"],
                                "time": snap.get("time", ""),
                                "kind": "blocked" if snap.get("start") != snap.get("end") else "event",
                                "subkind": "",
                                "title": snap.get("title", ""),
                            })
                        if _rewrite_readonly_layer_md(brain, ly, mirror_evs):
                            report["files_changed"].append(rel)

                # Index active read-only events for cross-layer dedup
                ro_rows = conn.execute(
                    "SELECT last_gcal_snapshot FROM event_links WHERE layer_file=? AND status='active'",
                    (rel,),
                ).fetchall()
                for rr in ro_rows:
                    try:
                        snap = json.loads(rr["last_gcal_snapshot"] or "{}")
                        if snap.get("start"):
                            readonly_events_index.append(snap)
                    except Exception:
                        pass

            # ---------------------------------------------------------------
            # PASS B: Bidirectional Layers (3-Way Diff + 3-Stage Dedup + Dual-Sync)
            # ---------------------------------------------------------------
            for ly in layers:
                if ly.get("mode") != "bidirectional":
                    continue
                rel = f"calendar/{ly['file']}"
                if execute:
                    _ensure_layer_md_file(brain, ly)
                _raw_lines, local_items = parse_calendar_md_items(brain, rel, today=today)
                local_by_uid: dict[str, dict[str, Any]] = {}
                unlinked_local: list[dict[str, Any]] = []
                for it in local_items:
                    if it["sync_id"]:
                        local_by_uid[it["sync_id"]] = it
                    else:
                        unlinked_local.append(it)

                # Calendars bound to this layer: primary + any attached subcalendars
                bound_cals: list[tuple[str, str, str]] = [(str(ly["account"]), str(ly["calendar_id"]), "primary")]
                subcals = ly.get("subcalendars") or {}
                if subcals.get("workout"):
                    bound_cals.append((str(ly["account"]), str(subcals["workout"]), "workout"))
                if subcals.get("tasks"):
                    bound_cals.append((str(ly["account"]), str(subcals["tasks"]), "tasks"))

                # Pull deltas from all bound calendars of this layer
                pulled_remote_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
                full_sync_cals: set[tuple[str, str]] = set()
                cursors_to_save: list[tuple[str, str, str, bool]] = []
                if not push_only:
                    ly_dk = str(ly.get("default_kind") or "event")
                    for aid, cid, role_c in bound_cals:
                        pulled = _pull_cal(aid, cid, default_kind=(ly_dk if role_c == "primary" else "event"))
                        if pulled is None:
                            continue
                        evs, is_full, next_tok = pulled
                        if is_full:
                            full_sync_cals.add((aid, cid))
                        cursors_to_save.append((aid, cid, next_tok, is_full))
                        for ev in evs:
                            pulled_remote_by_key[(aid, cid, ev["gcal_event_id"])] = ev

                # Load existing DB links for this layer
                db_rows = conn.execute(
                    "SELECT * FROM event_links WHERE layer_file=?",
                    (rel,),
                ).fetchall()
                links_by_uid = {str(r["atlas_uid"]): dict(r) for r in db_rows}
                links_by_remote = {
                    (str(r["account_id"]), str(r["calendar_id"]), str(r["gcal_event_id"])): dict(r)
                    for r in db_rows
                }

                # Load open & `keep-both` conflict decisions for this layer so unresolved duplicates are never pushed twice
                open_dup_local_sigs: set[tuple[str, str]] = set()
                open_dup_remote_ids: set[str] = set()
                keep_both_pairs: set[tuple[str, str, str]] = set()
                for crow in conn.execute(
                    "SELECT kind, status, date_iso, gcal_event_id, atlas_json FROM conflicts WHERE layer_file=? AND kind='duplicate_candidate'",
                    (rel,),
                ).fetchall():
                    try:
                        ca = json.loads(crow["atlas_json"] or "{}")
                    except Exception:
                        ca = {}
                    ctitle = str(ca.get("title") or "").strip().lower()
                    if crow["status"] == "open":
                        if ctitle:
                            open_dup_local_sigs.add((str(crow["date_iso"]), ctitle))
                        if crow["gcal_event_id"]:
                            open_dup_remote_ids.add(str(crow["gcal_event_id"]))
                    elif crow["status"] == "keep_both":
                        keep_both_pairs.add((str(crow["date_iso"]), ctitle, str(crow["gcal_event_id"])))

                md_replacements: dict[int, str] = {}
                md_insertions: list[tuple[str, str]] = []
                matched_remote_keys: set[tuple[str, str, str]] = set()
                matched_local_lines: set[int] = set()

                # Never echo-pull events already owned by another layer or by decentralized frontmatter `due:` files!
                for orow in conn.execute(
                    "SELECT account_id, calendar_id, gcal_event_id FROM event_links WHERE layer_file != ?",
                    (rel,),
                ).fetchall():
                    matched_remote_keys.add((str(orow["account_id"]), str(orow["calendar_id"]), str(orow["gcal_event_id"])))
                for rkey, ev in pulled_remote_by_key.items():
                    ev_owner_layer = str(ev.get("atlas_layer") or "")
                    if ev_owner_layer and ev_owner_layer != rel and not ev_owner_layer.startswith("calendar/"):
                        matched_remote_keys.add(rkey)

                # --- B1. Process existing linked events (3-Way Diff) ---
                for uid, link in list(links_by_uid.items()):
                    rkey = (link["account_id"], link["calendar_id"], link["gcal_event_id"])
                    l_item = local_by_uid.get(uid)
                    r_ev = pulled_remote_by_key.get(rkey)
                    if r_ev:
                        matched_remote_keys.add(rkey)

                    # Full-sync remote deletion detection
                    remote_deleted_on_full = (
                        not push_only
                        and r_ev is None
                        and (link["account_id"], link["calendar_id"]) in full_sync_cals
                        and link["status"] == "active"
                        and _in_window(link["date_start"], link["date_end"], today, past_days, single_days)
                    )
                    if remote_deleted_on_full:
                        r_ev = {
                            "gcal_event_id": link["gcal_event_id"],
                            "status_remote": "cancelled",
                            "status": "skipped",
                            "start": link["date_start"],
                            "end": link["date_end"],
                            "time": "",
                            "kind": "event",
                            "subkind": "",
                            "title": json.loads(link["last_gcal_snapshot"] or "{}").get("title", ""),
                            "meta": {},
                        }

                    # Case 1: Local line removed from Atlas while previously active inside sync window -> Remote Delete
                    if l_item is None:
                        if (
                            not pull_only
                            and link["status"] == "active"
                            and _in_window(link["date_start"], link["date_end"], today, past_days, single_days)
                        ):
                            pending_remote_deletes.append({
                                "atlas_uid": uid,
                                "layer_file": rel,
                                "account_id": link["account_id"],
                                "calendar_id": link["calendar_id"],
                                "gcal_event_id": link["gcal_event_id"],
                                "date": link["date_start"],
                                "title": json.loads(link["last_atlas_snapshot"] or "{}").get("title", uid),
                                "organizer_self": json.loads(link["meta_json"] or "{}").get("organizer_self", True),
                                "reason": "removed_in_atlas",
                            })
                        continue

                    matched_local_lines.add(l_item["line"])
                    a_snap = _canonical_atlas_snapshot(l_item)
                    a_hash = _hash_snapshot(a_snap)
                    atlas_changed = (a_hash != link["last_atlas_hash"])

                    g_changed = False
                    g_snap: dict[str, Any] = {}
                    g_hash = link["last_gcal_hash"]
                    if r_ev is not None:
                        if r_ev["status_remote"] == "cancelled":
                            g_changed = (link["status"] == "active")
                        else:
                            g_snap = _canonical_gcal_snapshot(r_ev)
                            g_hash = _hash_snapshot(g_snap)
                            g_changed = (g_hash != link["last_gcal_hash"])

                    # Case 2: User added `#local` to a previously synced item -> remove from Google, keep local
                    if l_item["is_local"] and link["status"] == "active" and not pull_only:
                        pending_remote_deletes.append({
                            "atlas_uid": uid,
                            "layer_file": rel,
                            "account_id": link["account_id"],
                            "calendar_id": link["calendar_id"],
                            "gcal_event_id": link["gcal_event_id"],
                            "date": l_item["start"],
                            "title": a_snap["title"],
                            "organizer_self": json.loads(link["meta_json"] or "{}").get("organizer_self", True),
                            "reason": "marked_local",
                        })
                        continue

                    # Case 3: Remote cancelled -> mark Atlas line `[-]` (never erase local text/notes!)
                    if r_ev is not None and r_ev["status_remote"] == "cancelled":
                        if l_item["status"] != "skipped":
                            new_ln = render_atlas_line(
                                l_item["start"], l_item["end"], l_item["time"],
                                l_item["kind"], l_item["subkind"], l_item["raw_what"],
                                status="skipped", atlas_uid=uid,
                            )
                            md_replacements[l_item["line"]] = new_ln
                            report["pulled_cancelled"] += 1
                            report["actions"].append(f"[pull cancel -> [-]] {rel}: {l_item['start']} {a_snap['title']}")
                            if execute:
                                a_snap_cancelled = dict(a_snap, status="skipped")
                                conn.execute(
                                    "UPDATE event_links SET status='deleted_remote', last_atlas_hash=?, last_atlas_snapshot=?, updated_at=? WHERE atlas_uid=?",
                                    (_hash_snapshot(a_snap_cancelled), json.dumps(a_snap_cancelled, ensure_ascii=False), time.time(), uid),
                                )
                        continue

                    # Case 4: Atlas marked `[-]` (skipped/cancelled) -> delete/decline in Google
                    if atlas_changed and l_item["status"] == "skipped" and link["status"] == "active" and not pull_only:
                        pending_remote_deletes.append({
                            "atlas_uid": uid,
                            "layer_file": rel,
                            "account_id": link["account_id"],
                            "calendar_id": link["calendar_id"],
                            "gcal_event_id": link["gcal_event_id"],
                            "date": l_item["start"],
                            "title": a_snap["title"],
                            "organizer_self": json.loads(link["meta_json"] or "{}").get("organizer_self", True),
                            "reason": "skipped_in_atlas",
                        })
                        continue

                    # Case 5: Only Atlas changed (or `--reformat` refresh) -> Push update to remote
                    prev_meta = json.loads(link["meta_json"] or "{}")
                    needs_reformat = False
                    if (
                        reformat
                        and not atlas_changed
                        and not g_changed
                        and not pull_only
                        and link["status"] == "active"
                        and l_item["status"] != "skipped"
                        and not link["recurring_event_id"]
                        and prev_meta.get("organizer_self", True)
                    ):
                        cand_body = format_atlas_for_gcal(
                            l_item, uid, timezone=tz,
                            existing_description=str(prev_meta.get("description") or ""),
                            existing_color_id=str(prev_meta.get("color_id") or ""),
                        )
                        cur_raw_sum = str(prev_meta.get("raw_summary") or "")
                        cur_col = str(prev_meta.get("color_id") or "")
                        want_col = str(cand_body.get("colorId") or "") if "colorId" in cand_body else cur_col
                        if cur_raw_sum != cand_body["summary"] or cur_col != want_col:
                            needs_reformat = True

                    if (atlas_changed or needs_reformat) and not g_changed and not pull_only and link["status"] == "active":
                        ev_body = format_atlas_for_gcal(
                            l_item, uid, timezone=tz,
                            existing_description=str(prev_meta.get("description") or ""),
                            existing_color_id=str(prev_meta.get("color_id") or ""),
                        )
                        report["pushed_updated"] += 1
                        tag_p = "[reformat~]" if (needs_reformat and not atlas_changed) else "[push~]"
                        report["actions"].append(f"{tag_p} {link['account_id']}: {l_item['start']} {l_item['time']} {ev_body['summary']}".strip())
                        if execute:
                            try:
                                updated_ev = prov.upsert_event(
                                    link["account_id"], link["calendar_id"], ev_body, event_id=link["gcal_event_id"]
                                )
                                pe = parse_gcal_event(updated_ev)
                                new_g_snap = _canonical_gcal_snapshot(pe)
                                new_meta = dict(pe["meta"])
                                new_meta["layer_label"] = str(ly.get("label") or ly.get("id") or "")
                                new_meta["layer_color"] = str(ly.get("color") or "#38bdf8")
                                conn.execute(
                                    """
                                    UPDATE event_links SET etag=?, date_start=?, date_end=?,
                                        last_atlas_hash=?, last_gcal_hash=?, last_atlas_snapshot=?,
                                        last_gcal_snapshot=?, meta_json=?, updated_at=?
                                    WHERE atlas_uid=?
                                    """,
                                    (
                                        pe["etag"], l_item["start"], l_item["end"],
                                        a_hash, _hash_snapshot(new_g_snap),
                                        json.dumps(a_snap, ensure_ascii=False),
                                        json.dumps(new_g_snap, ensure_ascii=False),
                                        json.dumps(new_meta, ensure_ascii=False),
                                        time.time(), uid,
                                    ),
                                )
                            except gcal_provider.CalendarProviderError as exc:
                                report["warnings"].append(f"push update {uid}: {exc}")
                        continue

                    # Case 6: Only Google changed -> Pull update into Atlas Markdown
                    if g_changed and not atlas_changed and not push_only and r_ev is not None:
                        new_ln = render_atlas_line(
                            r_ev["start"], r_ev["end"], r_ev["time"],
                            l_item["kind"] or r_ev["kind"], l_item["subkind"] or r_ev["subkind"],
                            r_ev["title"], status=r_ev["status"], atlas_uid=uid,
                            preserve_suffix_from=l_item["raw_what"],
                        )
                        md_replacements[l_item["line"]] = new_ln
                        report["pulled_updated"] += 1
                        report["actions"].append(f"[pull~] {rel}: {r_ev['start']} {r_ev['time']} {r_ev['title']}".strip())
                        if execute:
                            new_parsed = parse_atlas_timeline_line(new_ln, rel, line_no=l_item["line"], today=today)
                            new_a_snap = _canonical_atlas_snapshot(new_parsed) if new_parsed else a_snap
                            new_meta = dict(r_ev["meta"])
                            new_meta["layer_label"] = str(ly.get("label") or ly.get("id") or "")
                            new_meta["layer_color"] = str(ly.get("color") or "#38bdf8")
                            conn.execute(
                                """
                                UPDATE event_links SET etag=?, date_start=?, date_end=?,
                                    last_atlas_hash=?, last_gcal_hash=?, last_atlas_snapshot=?,
                                    last_gcal_snapshot=?, meta_json=?, updated_at=?
                                WHERE atlas_uid=?
                                """,
                                (
                                    r_ev["etag"], r_ev["start"], r_ev["end"],
                                    _hash_snapshot(new_a_snap), g_hash,
                                    json.dumps(new_a_snap, ensure_ascii=False),
                                    json.dumps(g_snap, ensure_ascii=False),
                                    json.dumps(new_meta, ensure_ascii=False),
                                    time.time(), uid,
                                ),
                            )
                        continue

                    # Case 7: Both sides changed -> Field-level Smart-Merge or Conflict Queue
                    if atlas_changed and g_changed and r_ev is not None:
                        base_a = json.loads(link["last_atlas_snapshot"] or "{}")
                        base_g = json.loads(link["last_gcal_snapshot"] or "{}")
                        a_time_chg = (a_snap["start"], a_snap["end"], a_snap["time"]) != (base_a.get("start"), base_a.get("end"), base_a.get("time"))
                        g_time_chg = (g_snap["start"], g_snap["end"], g_snap["time"]) != (base_g.get("start"), base_g.get("end"), base_g.get("time"))
                        a_title_chg = a_snap["title"] != base_a.get("title")
                        g_title_chg = g_snap["title"] != base_g.get("title")
                        direct_collision = (
                            (a_time_chg and g_time_chg and (a_snap["start"], a_snap["time"]) != (g_snap["start"], g_snap["time"]))
                            or (a_title_chg and g_title_chg and title_similarity(a_snap["title"], g_snap["title"]) < 0.9)
                        )
                        if direct_collision:
                            report["conflicts_added"] += 1
                            report["actions"].append(f"[conflict] {rel}: {l_item['start']} '{a_snap['title']}' vs Google '{g_snap['title']}'")
                            if execute:
                                _upsert_conflict(
                                    conn, "edit_conflict", uid, rel,
                                    link["account_id"], link["calendar_id"], link["gcal_event_id"],
                                    l_item["start"], f"Both changed: '{a_snap['title']}' vs '{g_snap['title']}'",
                                    a_snap, g_snap, score=title_similarity(a_snap["title"], g_snap["title"]),
                                )
                        else:
                            # Non-overlapping fields changed -> Smart-Merge!
                            # Google wins time/location; Atlas wins status/notes/title if only Atlas edited title
                            merged_start = g_snap["start"] if g_time_chg else a_snap["start"]
                            merged_end = g_snap["end"] if g_time_chg else a_snap["end"]
                            merged_time = g_snap["time"] if g_time_chg else a_snap["time"]
                            merged_title = g_snap["title"] if g_title_chg else a_snap["title"]
                            merged_status = a_snap["status"] if a_snap["status"] != base_a.get("status") else g_snap["status"]
                            new_ln = render_atlas_line(
                                merged_start, merged_end, merged_time,
                                l_item["kind"], l_item["subkind"], merged_title,
                                status=merged_status, atlas_uid=uid,
                                preserve_suffix_from=l_item["raw_what"],
                            )
                            md_replacements[l_item["line"]] = new_ln
                            report["smart_merged"] += 1
                            report["actions"].append(f"[smart-merge] {rel}: {merged_start} {merged_time} {merged_title}".strip())
                            if execute:
                                merged_item = parse_atlas_timeline_line(new_ln, rel, line_no=l_item["line"], today=today) or l_item
                                ev_body = format_atlas_for_gcal(
                                    merged_item, uid, timezone=tz,
                                    existing_description=str((r_ev.get("meta") or {}).get("description") or ""),
                                    existing_color_id=str((r_ev.get("meta") or {}).get("color_id") or ""),
                                )
                                try:
                                    updated_ev = prov.upsert_event(
                                        link["account_id"], link["calendar_id"], ev_body, event_id=link["gcal_event_id"]
                                    )
                                    pe = parse_gcal_event(updated_ev)
                                    new_a_snap = _canonical_atlas_snapshot(merged_item)
                                    new_g_snap = _canonical_gcal_snapshot(pe)
                                    new_meta = dict(pe["meta"])
                                    new_meta["layer_label"] = str(ly.get("label") or ly.get("id") or "")
                                    new_meta["layer_color"] = str(ly.get("color") or "#38bdf8")
                                    conn.execute(
                                        """
                                        UPDATE event_links SET etag=?, date_start=?, date_end=?,
                                            last_atlas_hash=?, last_gcal_hash=?, last_atlas_snapshot=?,
                                            last_gcal_snapshot=?, meta_json=?, updated_at=?
                                        WHERE atlas_uid=?
                                        """,
                                        (
                                            pe["etag"], merged_start, merged_end,
                                            _hash_snapshot(new_a_snap), _hash_snapshot(new_g_snap),
                                            json.dumps(new_a_snap, ensure_ascii=False),
                                            json.dumps(new_g_snap, ensure_ascii=False),
                                            json.dumps(new_meta, ensure_ascii=False),
                                            time.time(), uid,
                                        ),
                                    )
                                except gcal_provider.CalendarProviderError as exc:
                                    report["warnings"].append(f"smart-merge push {uid}: {exc}")

                # --- B1b. Smart Coalescing of auto-generated Gmail booking events (`Stay at ...`) onto same-span trip ---
                # If Google has both a user trip event AND a Gmail auto-booking (`Stay at ...`) on the exact same
                # multi-day span, enrich the trip event with the booking's location/description instead of duplicating.
                span_primary_remote: dict[tuple[str, str], tuple[tuple[str, str, str], dict[str, Any]]] = {}
                for rkey, ev in pulled_remote_by_key.items():
                    if ev["status_remote"] == "cancelled" or not (ev["start"] and ev["end"] > ev["start"]):
                        continue
                    if not AUTO_BOOKING_TITLE_RX.match(ev["title"]):
                        span_primary_remote.setdefault((ev["start"], ev["end"]), (rkey, ev))
                coalesced_booking_keys: set[tuple[str, str, str]] = set()
                for rkey, ev in list(pulled_remote_by_key.items()):
                    if ev["status_remote"] == "cancelled" or not (ev["start"] and ev["end"] > ev["start"]):
                        continue
                    if not AUTO_BOOKING_TITLE_RX.match(ev["title"]):
                        continue
                    span_k = (ev["start"], ev["end"])
                    # Check if a matching local multi-day trip OR a non-booking remote trip covers this exact span
                    has_local_trip = any(
                        it["start"] == ev["start"] and it["end"] == ev["end"]
                        for it in local_items
                    )
                    prim_pair = span_primary_remote.get(span_k)
                    if prim_pair or has_local_trip:
                        coalesced_booking_keys.add(rkey)
                        matched_remote_keys.add(rkey)
                        if prim_pair:
                            _pk, prim_ev = prim_pair
                            b_loc = ev["meta"].get("location") or ""
                            b_desc = ev["meta"].get("description") or ""
                            if b_loc and not prim_ev["meta"].get("location"):
                                prim_ev["meta"]["location"] = b_loc
                            extra_note = f"{ev['title']}" + (f" ({b_loc})" if b_loc else "") + (f"\n{b_desc}" if b_desc else "")
                            if extra_note and extra_note not in (prim_ev["meta"].get("description") or ""):
                                prim_ev["meta"]["description"] = (
                                    (prim_ev["meta"].get("description") or "").strip() + "\n\n" + extra_note
                                ).strip()
                        report["actions"].append(f"[coalesce-booking] {rel}: merged '{ev['title']}' ({ev['start']}..{ev['end']}) into trip metadata")

                # --- B2. 3-Stage Deduplication between Unlinked Local & Unlinked Remote ---
                unlinked_remote: list[tuple[tuple[str, str, str], dict[str, Any]]] = []
                for rkey, ev in pulled_remote_by_key.items():
                    if rkey in matched_remote_keys or rkey in links_by_remote:
                        continue
                    if ev["status_remote"] == "cancelled":
                        continue
                    # Stage 1: Exact `atlas_uid` already embedded in Google's extendedProperties
                    if ev["atlas_uid"] and ev["atlas_uid"] in local_by_uid:
                        l_item = local_by_uid[ev["atlas_uid"]]
                        matched_local_lines.add(l_item["line"])
                        matched_remote_keys.add(rkey)
                        report["auto_linked"] += 1
                        report["actions"].append(f"[link-id] {rel}: {ev['start']} {ev['title']}")
                        if execute:
                            a_snap = _canonical_atlas_snapshot(l_item)
                            g_snap = _canonical_gcal_snapshot(ev)
                            meta = dict(ev["meta"])
                            meta["layer_label"] = str(ly.get("label") or ly.get("id") or "")
                            meta["layer_color"] = str(ly.get("color") or "#38bdf8")
                            conn.execute(
                                """
                                INSERT OR REPLACE INTO event_links
                                (atlas_uid, layer_file, account_id, calendar_id, gcal_event_id, ical_uid, etag,
                                 recurring_event_id, date_start, date_end, last_atlas_hash, last_gcal_hash,
                                 last_atlas_snapshot, last_gcal_snapshot, meta_json, status, updated_at)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)
                                """,
                                (
                                    ev["atlas_uid"], rel, rkey[0], rkey[1], rkey[2], ev["ical_uid"], ev["etag"],
                                    ev["recurring_event_id"], ev["start"], ev["end"],
                                    _hash_snapshot(a_snap), _hash_snapshot(g_snap),
                                    json.dumps(a_snap, ensure_ascii=False), json.dumps(g_snap, ensure_ascii=False),
                                    json.dumps(meta, ensure_ascii=False), time.time(),
                                ),
                            )
                        continue
                    unlinked_remote.append((rkey, ev))

                still_unlinked_local: list[dict[str, Any]] = []
                consumed_remote_keys: set[tuple[str, str, str]] = set()
                ambiguous_remote_keys: set[tuple[str, str, str]] = set()

                for l_item in unlinked_local:
                    if l_item["line"] in matched_local_lines:
                        continue
                    if not _should_sync_item(l_item, cfg, layer=ly):
                        continue
                    l_snap_pre = _canonical_atlas_snapshot(l_item)
                    if (l_item["start"], l_snap_pre["title"].lower()) in open_dup_local_sigs:
                        matched_local_lines.add(l_item["line"])
                        continue
                    best_match: tuple[float, bool, tuple[str, str, str], dict[str, Any]] | None = None
                    for rkey, ev in unlinked_remote:
                        if rkey in consumed_remote_keys or rkey[2] in open_dup_remote_ids:
                            continue
                        if (l_item["start"], l_snap_pre["title"].lower(), rkey[2]) in keep_both_pairs:
                            continue
                        compat, exact_slot = _time_window_compatible(l_item, ev)
                        if not compat:
                            continue
                        sim = title_similarity(l_item["what"], ev["title"])
                        if best_match is None or sim > best_match[0]:
                            best_match = (sim, exact_slot, rkey, ev)

                    if best_match and best_match[0] >= 0.75:
                        # Stage 2: High-confidence auto-link (zero duplicates!)
                        sim, _ex, rkey, ev = best_match
                        uid = ev["atlas_uid"] or _new_atlas_uid()
                        consumed_remote_keys.add(rkey)
                        matched_local_lines.add(l_item["line"])
                        new_ln = f"{l_item['raw'].rstrip()} <!-- g:{uid} -->"
                        md_replacements[l_item["line"]] = new_ln
                        report["auto_linked"] += 1
                        report["actions"].append(f"[dedup-link {sim:.2f}] {rel}: '{l_item['what']}' <-> Google '{ev['title']}'")
                        if execute:
                            a_snap = _canonical_atlas_snapshot(l_item)
                            g_snap = _canonical_gcal_snapshot(ev)
                            meta = dict(ev["meta"])
                            meta["layer_label"] = str(ly.get("label") or ly.get("id") or "")
                            meta["layer_color"] = str(ly.get("color") or "#38bdf8")
                            conn.execute(
                                """
                                INSERT OR REPLACE INTO event_links
                                (atlas_uid, layer_file, account_id, calendar_id, gcal_event_id, ical_uid, etag,
                                 recurring_event_id, date_start, date_end, last_atlas_hash, last_gcal_hash,
                                 last_atlas_snapshot, last_gcal_snapshot, meta_json, status, updated_at)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)
                                """,
                                (
                                    uid, rel, rkey[0], rkey[1], rkey[2], ev["ical_uid"], ev["etag"],
                                    ev["recurring_event_id"], ev["start"], ev["end"],
                                    _hash_snapshot(a_snap), _hash_snapshot(g_snap),
                                    json.dumps(a_snap, ensure_ascii=False), json.dumps(g_snap, ensure_ascii=False),
                                    json.dumps(meta, ensure_ascii=False), time.time(),
                                ),
                            )
                            # Tag Google event with atlas_uid so future syncs are Stage-1 exact
                            try:
                                prov.upsert_event(
                                    rkey[0], rkey[1],
                                    {"extendedProperties": {"private": {"atlas_uid": uid, "atlas_layer": rel}}},
                                    event_id=rkey[2],
                                )
                            except gcal_provider.CalendarProviderError:
                                pass
                        continue

                    if best_match and (best_match[1] or best_match[0] >= 0.25):
                        # Stage 3: Ambiguous duplicate candidate -> Conflict Queue (do NOT blindly duplicate!)
                        sim, _ex, rkey, ev = best_match
                        ambiguous_remote_keys.add(rkey)
                        matched_local_lines.add(l_item["line"])
                        report["conflicts_added"] += 1
                        report["actions"].append(
                            f"[dedup-review {sim:.2f}] {rel}: {l_item['start']} '{l_item['what']}' vs Google '{ev['title']}'"
                        )
                        if execute:
                            _upsert_conflict(
                                conn, "duplicate_candidate", "", rel,
                                rkey[0], rkey[1], rkey[2], l_item["start"],
                                f"Possible duplicate: '{l_item['what']}' vs '{ev['title']}'",
                                _canonical_atlas_snapshot(l_item) | {"line": l_item["line"], "raw": l_item["raw"]},
                                _canonical_gcal_snapshot(ev) | {"gcal_event_id": rkey[2]},
                                score=sim,
                            )
                        continue

                    still_unlinked_local.append(l_item)

                # --- B3. Pull brand-new Google events into Atlas Markdown ---
                if not push_only:
                    for rkey, ev in unlinked_remote:
                        if rkey in consumed_remote_keys or rkey in ambiguous_remote_keys or rkey[2] in open_dup_remote_ids:
                            continue
                        uid = ev["atlas_uid"] or _new_atlas_uid()
                        # Check if event came from workout or tasks subcalendar -> preserve kind
                        ev_kind, ev_sub = ev["kind"], ev["subkind"]
                        if rkey[1] == subcals.get("workout") and ev_kind == "event":
                            ev_kind = "workout"
                        elif rkey[1] == subcals.get("tasks") and ev_kind == "event":
                            ev_kind = "todo"
                        new_ln = render_atlas_line(
                            ev["start"], ev["end"], ev["time"], ev_kind, ev_sub,
                            ev["title"], status=ev["status"], atlas_uid=uid,
                        )
                        md_insertions.append((ev["start"], new_ln))
                        report["pulled_new"] += 1
                        report["actions"].append(f"[pull+] {rel}: {ev['start']} {ev['time']} {ev['title']}".strip())
                        if execute:
                            new_parsed = parse_atlas_timeline_line(new_ln, rel, line_no=0, today=today)
                            a_snap = _canonical_atlas_snapshot(new_parsed) if new_parsed else {}
                            g_snap = _canonical_gcal_snapshot(ev)
                            meta = dict(ev["meta"])
                            meta["layer_label"] = str(ly.get("label") or ly.get("id") or "")
                            meta["layer_color"] = str(ly.get("color") or "#38bdf8")
                            conn.execute(
                                """
                                INSERT OR REPLACE INTO event_links
                                (atlas_uid, layer_file, account_id, calendar_id, gcal_event_id, ical_uid, etag,
                                 recurring_event_id, date_start, date_end, last_atlas_hash, last_gcal_hash,
                                 last_atlas_snapshot, last_gcal_snapshot, meta_json, status, updated_at)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)
                                """,
                                (
                                    uid, rel, rkey[0], rkey[1], rkey[2], ev["ical_uid"], ev["etag"],
                                    ev["recurring_event_id"], ev["start"], ev["end"],
                                    _hash_snapshot(a_snap), _hash_snapshot(g_snap),
                                    json.dumps(a_snap, ensure_ascii=False), json.dumps(g_snap, ensure_ascii=False),
                                    json.dumps(meta, ensure_ascii=False), time.time(),
                                ),
                            )

                # --- B4. Push brand-new local Atlas items to Google ---
                if not pull_only:
                    for l_item in still_unlinked_local:
                        if l_item["status"] == "skipped":
                            continue
                        if not _in_window(l_item["start"], l_item["end"], today, past_days, single_days):
                            continue
                        # Cross-layer dedup against read-only mirrors (same date OR overlapping multi-day span)
                        ro_dup = False
                        for ro_snap in readonly_events_index:
                            ro_s = str(ro_snap.get("start") or "")
                            ro_e = str(ro_snap.get("end") or ro_s)
                            if not ro_s:
                                continue
                            compat, _ex = _time_window_compatible(l_item, ro_snap)
                            overlaps_span = (l_item["start"] <= ro_e and ro_s <= l_item["end"])
                            sim_ro = title_similarity(l_item["what"], ro_snap.get("title", ""))
                            if (compat and sim_ro >= 0.75) or (overlaps_span and (l_item["end"] > l_item["start"] or ro_e > ro_s) and sim_ro >= 0.55):
                                ro_dup = True
                                break
                        if ro_dup:
                            report["actions"].append(f"[skip ro-dup] {rel}: {l_item['start']} {l_item['what']} (already in read-only layer)")
                            continue

                        target_aid, target_cid = _resolve_target_calendar(l_item, ly, layers)
                        uid = _new_atlas_uid()
                        ev_body = format_atlas_for_gcal(l_item, uid, timezone=tz)
                        report["pushed_new"] += 1
                        report["actions"].append(f"[push+] {target_aid}/{target_cid[:16]}: {l_item['start']} {l_item['time']} {ev_body['summary']}".strip())
                        if execute:
                            try:
                                created = prov.upsert_event(target_aid, target_cid, ev_body)
                                pe = parse_gcal_event(created)
                                new_ln = f"{l_item['raw'].rstrip()} <!-- g:{uid} -->"
                                md_replacements[l_item["line"]] = new_ln
                                a_snap = _canonical_atlas_snapshot(l_item)
                                g_snap = _canonical_gcal_snapshot(pe)
                                meta = dict(pe["meta"])
                                meta["layer_label"] = str(ly.get("label") or ly.get("id") or "")
                                meta["layer_color"] = str(ly.get("color") or "#38bdf8")
                                conn.execute(
                                    """
                                    INSERT OR REPLACE INTO event_links
                                    (atlas_uid, layer_file, account_id, calendar_id, gcal_event_id, ical_uid, etag,
                                     recurring_event_id, date_start, date_end, last_atlas_hash, last_gcal_hash,
                                     last_atlas_snapshot, last_gcal_snapshot, meta_json, status, updated_at)
                                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)
                                    """,
                                    (
                                        uid, rel, target_aid, target_cid, pe["gcal_event_id"], pe["ical_uid"], pe["etag"],
                                        pe["recurring_event_id"], l_item["start"], l_item["end"],
                                        _hash_snapshot(a_snap), _hash_snapshot(g_snap),
                                        json.dumps(a_snap, ensure_ascii=False), json.dumps(g_snap, ensure_ascii=False),
                                        json.dumps(meta, ensure_ascii=False), time.time(),
                                    ),
                                )
                            except gcal_provider.CalendarProviderError as exc:
                                report["warnings"].append(f"push create '{l_item['what']}': {exc}")

                if execute:
                    if _apply_markdown_edits(brain, rel, md_replacements, md_insertions):
                        report["files_changed"].append(rel)
                    for aid, cid, next_tok, is_full in cursors_to_save:
                        _save_cursor(aid, cid, next_tok, is_full)

            # ---------------------------------------------------------------
            # PASS C: Push-Only Decentralized `due:` Items (into Tasks subcalendar)
            # ---------------------------------------------------------------
            default_ly = next((ly for ly in layers if ly.get("file") == "README.md" and ly.get("mode") == "bidirectional"), None)
            if (
                not pull_only
                and not layer_filter
                and default_ly
                and (cfg.get("routing") or {}).get("sync_decentralized_due", True)
            ):
                due_items = _collect_decentralized_due_items(brain, today)
                for di in due_items:
                    if not _should_sync_item(di, cfg):
                        continue
                    if not _in_window(di["start"], di["end"], today, past_days, single_days):
                        continue
                    uid = di["sync_id"]
                    row = conn.execute("SELECT * FROM event_links WHERE atlas_uid=?", (uid,)).fetchone()
                    a_snap = _canonical_atlas_snapshot(di)
                    a_hash = _hash_snapshot(a_snap)
                    target_aid, target_cid = _resolve_target_calendar(di, default_ly, layers)
                    if row is None:
                        if di["status"] == "skipped":
                            continue
                        ev_body = format_atlas_for_gcal(di, uid, timezone=tz)
                        report["pushed_new"] += 1
                        report["actions"].append(f"[push+ due:] {di['rel']}: {di['start']} {ev_body['summary']}")
                        if execute:
                            try:
                                created = prov.upsert_event(target_aid, target_cid, ev_body)
                                pe = parse_gcal_event(created)
                                g_snap = _canonical_gcal_snapshot(pe)
                                conn.execute(
                                    """
                                    INSERT OR REPLACE INTO event_links
                                    (atlas_uid, layer_file, account_id, calendar_id, gcal_event_id, ical_uid, etag,
                                     recurring_event_id, date_start, date_end, last_atlas_hash, last_gcal_hash,
                                     last_atlas_snapshot, last_gcal_snapshot, meta_json, status, updated_at)
                                    VALUES (?, ?, ?, ?, ?, ?, ?, '', ?, ?, ?, ?, ?, ?, ?, 'active', ?)
                                    """,
                                    (
                                        uid, di["rel"], target_aid, target_cid, pe["gcal_event_id"], pe["ical_uid"], pe["etag"],
                                        di["start"], di["end"], a_hash, _hash_snapshot(g_snap),
                                        json.dumps(a_snap, ensure_ascii=False), json.dumps(g_snap, ensure_ascii=False),
                                        json.dumps(pe["meta"], ensure_ascii=False), time.time(),
                                    ),
                                )
                            except gcal_provider.CalendarProviderError as exc:
                                report["warnings"].append(f"push due {di['rel']}: {exc}")
                    elif row["status"] == "active" and row["last_atlas_hash"] != a_hash:
                        if di["status"] == "skipped":
                            pending_remote_deletes.append({
                                "atlas_uid": uid,
                                "layer_file": di["rel"],
                                "account_id": row["account_id"],
                                "calendar_id": row["calendar_id"],
                                "gcal_event_id": row["gcal_event_id"],
                                "date": di["start"],
                                "title": a_snap["title"],
                                "organizer_self": True,
                                "reason": "due_skipped",
                            })
                        else:
                            ev_body = format_atlas_for_gcal(di, uid, timezone=tz)
                            report["pushed_updated"] += 1
                            report["actions"].append(f"[push~ due:] {di['rel']}: {di['start']} {ev_body['summary']}")
                            if execute:
                                try:
                                    updated = prov.upsert_event(
                                        row["account_id"], row["calendar_id"], ev_body, event_id=row["gcal_event_id"]
                                    )
                                    pe = parse_gcal_event(updated)
                                    g_snap = _canonical_gcal_snapshot(pe)
                                    conn.execute(
                                        "UPDATE event_links SET etag=?, date_start=?, date_end=?, last_atlas_hash=?, last_gcal_hash=?, last_atlas_snapshot=?, last_gcal_snapshot=?, updated_at=? WHERE atlas_uid=?",
                                        (
                                            pe["etag"], di["start"], di["end"], a_hash, _hash_snapshot(g_snap),
                                            json.dumps(a_snap, ensure_ascii=False), json.dumps(g_snap, ensure_ascii=False),
                                            time.time(), uid,
                                        ),
                                    )
                                except gcal_provider.CalendarProviderError as exc:
                                    report["warnings"].append(f"push update due {di['rel']}: {exc}")

            # ---------------------------------------------------------------
            # PASS D: Bulk-Delete Circuit Breaker & Remote Deletions
            # ---------------------------------------------------------------
            if pending_remote_deletes:
                if len(pending_remote_deletes) > bulk_limit and not allow_bulk_delete:
                    report["bulk_delete_blocked"] = True
                    report["conflicts_added"] += 1
                    msg = (
                        f"Bulk-Delete Circuit Breaker: {len(pending_remote_deletes)} remote delete(s) exceed limit ({bulk_limit}). "
                        f"Inspect and re-run with `--execute --allow-bulk-delete` if intentional."
                    )
                    report["warnings"].append(msg)
                    report["actions"].append(f"[BLOCKED bulk-delete] {len(pending_remote_deletes)} event(s)")
                    if execute:
                        _upsert_conflict(
                            conn, "bulk_delete_blocked", "", "calendar/", "", "",
                            f"bulk-{int(time.time())}", today.isoformat(), msg,
                            {"deletes": pending_remote_deletes}, {}, score=0.0,
                        )
                else:
                    for item_del in pending_remote_deletes:
                        report["pushed_deleted"] += 1
                        action_tag = "rsvp-decline" if not item_del.get("organizer_self", True) else "push-del"
                        report["actions"].append(
                            f"[{action_tag}] {item_del['account_id']}: {item_del['date']} {item_del['title']} ({item_del['reason']})"
                        )
                        if execute:
                            try:
                                if item_del.get("organizer_self", True):
                                    prov.delete_event(item_del["account_id"], item_del["calendar_id"], item_del["gcal_event_id"])
                                else:
                                    prov.rsvp_event(item_del["account_id"], item_del["calendar_id"], item_del["gcal_event_id"], "declined")
                                new_st = "local_only" if item_del["reason"] == "marked_local" else "deleted_local"
                                conn.execute(
                                    "UPDATE event_links SET status=?, updated_at=? WHERE atlas_uid=?",
                                    (new_st, time.time(), item_del["atlas_uid"]),
                                )
                            except gcal_provider.CalendarProviderError as exc:
                                report["warnings"].append(f"delete {item_del['atlas_uid']}: {exc}")

            if execute:
                now_s = str(time.time())
                if not push_only:
                    _set_meta(conn, "last_pull_ts", now_s)
                if not pull_only:
                    _set_meta(conn, "last_push_ts", now_s)
                _set_meta(conn, "last_summary_json", json.dumps({
                    "pulled_new": report["pulled_new"],
                    "pulled_updated": report["pulled_updated"],
                    "pulled_cancelled": report["pulled_cancelled"],
                    "pushed_new": report["pushed_new"],
                    "pushed_updated": report["pushed_updated"],
                    "pushed_deleted": report["pushed_deleted"],
                    "auto_linked": report["auto_linked"],
                }))
                conn.commit()

            oc_row = conn.execute("SELECT COUNT(*) FROM conflicts WHERE status='open'").fetchone()
            report["open_conflicts"] = int(oc_row[0] or 0) if oc_row else 0
        finally:
            conn.close()

    return report


# ---------------------------------------------------------------------------
# 7. Conflict Resolution (`atlas.py cal conflicts`)
# ---------------------------------------------------------------------------

def list_conflicts(brain: str, include_resolved: bool = False) -> list[dict[str, Any]]:
    p = sync_db_path(brain)
    if not os.path.isfile(p):
        return []
    conn = _open_db(brain)
    try:
        sql = "SELECT * FROM conflicts" + ("" if include_resolved else " WHERE status='open'") + " ORDER BY created_at DESC"
        rows = conn.execute(sql).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            d["atlas"] = json.loads(d.pop("atlas_json", "{}") or "{}")
            d["gcal"] = json.loads(d.pop("gcal_json", "{}") or "{}")
            out.append(d)
        return out
    finally:
        conn.close()


def resolve_conflict(
    brain: str,
    conflict_id: str,
    action: str,
    provider: gcal_provider.CalendarProvider | None = None,
) -> dict[str, Any]:
    """Resolve an open conflict (`link`, `keep-both`, `use-atlas`, `use-remote`/`use-google`, `dismiss`)."""
    valid = {"link", "keep-both", "use-atlas", "use-google", "use-remote", "dismiss"}
    act = str(action or "").strip().lower()
    if act not in valid:
        return {"ok": False, "error": f"Invalid action '{action}'; choose from {sorted(valid)}"}
    eff_act = "use-google" if act == "use-remote" else act

    with cal_engine.brain_write_lock(brain):
        conn = _open_db(brain)
        try:
            row = conn.execute("SELECT * FROM conflicts WHERE id=?", (conflict_id,)).fetchone()
            if not row:
                return {"ok": False, "error": f"Conflict '{conflict_id}' not found."}
            c = dict(row)
            a_obj = json.loads(c.get("atlas_json") or "{}")
            g_obj = json.loads(c.get("gcal_json") or "{}")
            rel = c["layer_file"]

            if act == "dismiss":
                conn.execute("UPDATE conflicts SET status='resolved' WHERE id=?", (conflict_id,))
                conn.commit()
                return {"ok": True, "id": conflict_id, "action": act, "note": "Conflict marked resolved."}

            if act == "keep-both":
                conn.execute("UPDATE conflicts SET status='keep_both' WHERE id=?", (conflict_id,))
                conn.commit()
                return {"ok": True, "id": conflict_id, "action": act, "note": "Both events will be kept separately on next sync."}

            if act == "link" and c["kind"] == "duplicate_candidate":
                uid = c["atlas_uid"] or _new_atlas_uid()
                line_no = int(a_obj.get("line") or 0)
                raw_ln = str(a_obj.get("raw") or "")
                if line_no and raw_ln:
                    _apply_markdown_edits(brain, rel, {line_no: f"{raw_ln.rstrip()} <!-- g:{uid} -->"}, [])
                a_snap = {k: v for k, v in a_obj.items() if k not in ("line", "raw")}
                g_snap = {k: v for k, v in g_obj.items() if k != "gcal_event_id"}
                conn.execute(
                    """
                    INSERT OR REPLACE INTO event_links
                    (atlas_uid, layer_file, account_id, calendar_id, gcal_event_id, date_start, date_end,
                     last_atlas_hash, last_gcal_hash, last_atlas_snapshot, last_gcal_snapshot, status, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)
                    """,
                    (
                        uid, rel, c["account_id"], c["calendar_id"], c["gcal_event_id"],
                        c["date_iso"], a_snap.get("end") or c["date_iso"],
                        _hash_snapshot(a_snap), _hash_snapshot(g_snap),
                        json.dumps(a_snap, ensure_ascii=False), json.dumps(g_snap, ensure_ascii=False), time.time(),
                    ),
                )
                conn.execute("UPDATE conflicts SET status='resolved' WHERE id=?", (conflict_id,))
                conn.commit()
                return {"ok": True, "id": conflict_id, "action": "link", "atlas_uid": uid}

            if eff_act in ("use-atlas", "use-google") and c["atlas_uid"]:
                prov = provider or build_default_provider(brain)
                cfg = load_sync_config(brain) or _deep_merge_defaults({})
                tz = str(cfg.get("timezone") or gcal_provider.local_timezone_name())
                uid = c["atlas_uid"]
                _lines, items = parse_calendar_md_items(brain, rel)
                l_item = next((i for i in items if i["sync_id"] == uid), None)
                if eff_act == "use-atlas" and l_item:
                    ev_body = format_atlas_for_gcal(l_item, uid, timezone=tz)
                    updated = prov.upsert_event(c["account_id"], c["calendar_id"], ev_body, event_id=c["gcal_event_id"])
                    pe = parse_gcal_event(updated)
                    a_snap = _canonical_atlas_snapshot(l_item)
                    g_snap = _canonical_gcal_snapshot(pe)
                    conn.execute(
                        "UPDATE event_links SET last_atlas_hash=?, last_gcal_hash=?, last_atlas_snapshot=?, last_gcal_snapshot=?, updated_at=? WHERE atlas_uid=?",
                        (_hash_snapshot(a_snap), _hash_snapshot(g_snap), json.dumps(a_snap), json.dumps(g_snap), time.time(), uid),
                    )
                elif eff_act == "use-google" and l_item:
                    new_ln = render_atlas_line(
                        g_obj.get("start") or l_item["start"],
                        g_obj.get("end") or l_item["end"],
                        g_obj.get("time", ""),
                        l_item["kind"], l_item["subkind"],
                        g_obj.get("title") or l_item["what"],
                        status=g_obj.get("status", "open"), atlas_uid=uid,
                        preserve_suffix_from=l_item["raw_what"],
                    )
                    _apply_markdown_edits(brain, rel, {l_item["line"]: new_ln}, [])
                    new_p = parse_atlas_timeline_line(new_ln, rel, line_no=l_item["line"])
                    a_snap = _canonical_atlas_snapshot(new_p) if new_p else a_obj
                    conn.execute(
                        "UPDATE event_links SET last_atlas_hash=?, last_gcal_hash=?, last_atlas_snapshot=?, last_gcal_snapshot=?, updated_at=? WHERE atlas_uid=?",
                        (_hash_snapshot(a_snap), _hash_snapshot(g_obj), json.dumps(a_snap), json.dumps(g_obj), time.time(), uid),
                    )
                conn.execute("UPDATE conflicts SET status='resolved' WHERE id=?", (conflict_id,))
                conn.commit()
                return {"ok": True, "id": conflict_id, "action": act, "atlas_uid": uid}

            return {"ok": False, "error": f"Action '{act}' is not applicable to conflict kind '{c['kind']}'."}
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# 8. Agent High-Level Tools: Show, FreeBusy, Safety-Gated Create & RSVP
# ---------------------------------------------------------------------------

def find_event_details(brain: str, query: str) -> list[dict[str, Any]]:
    """Search calendar events across all `brain/calendar/*.md` layers enriched with cached Google metadata."""
    q = str(query or "").strip().lower()
    meta_map = load_event_metadata_map(brain)
    cal_dir = os.path.join(brain, "calendar")
    hits: list[dict[str, Any]] = []
    if not os.path.isdir(cal_dir):
        return hits
    for fn in sorted(os.listdir(cal_dir)):
        if not fn.lower().endswith(".md"):
            continue
        rel = f"calendar/{fn}"
        _lines, items = parse_calendar_md_items(brain, rel)
        for it in items:
            uid = it["sync_id"]
            meta = meta_map.get(uid, {})
            hay = " ".join([
                uid, it["start"], it["what"], rel,
                str(meta.get("location") or ""),
                str(meta.get("description") or ""),
            ]).lower()
            if not q or q == uid.lower() or q in hay:
                hits.append({
                    "atlas_uid": uid,
                    "rel": rel,
                    "line": it["line"],
                    "start": it["start"],
                    "end": it["end"],
                    "time": it["time"],
                    "kind": f"{it['kind']}:{it['subkind']}" if it["subkind"] else it["kind"],
                    "status": it["status"],
                    "what": it["what"],
                    "is_local": it["is_local"],
                    "account_id": meta.get("account_id", ""),
                    "calendar_id": meta.get("calendar_id", ""),
                    "gcal_event_id": meta.get("gcal_event_id", ""),
                    "location": meta.get("location", ""),
                    "meet_link": meta.get("meet_link", ""),
                    "description": meta.get("description", ""),
                    "attendees": meta.get("attendees", []),
                    "self_rsvp": meta.get("self_rsvp", ""),
                    "organizer": meta.get("organizer", ""),
                    "layer_label": meta.get("layer_label", ""),
                })
    return sorted(hits, key=lambda x: (x["start"], x["time"], x["what"]))


def compute_freebusy(
    brain: str,
    days: int = 7,
    start_date_iso: str = "",
    work_start_hour: int = 8,
    work_end_hour: int = 20,
    min_slot_minutes: int = 30,
    provider: gcal_provider.CalendarProvider | None = None,
    use_remote: bool = True,
) -> dict[str, Any]:
    """Compute consolidated busy intervals and free slots across ALL layers (Google FreeBusy + local Atlas)."""
    cfg = load_sync_config(brain)
    tz = str((cfg or {}).get("timezone") or gcal_provider.local_timezone_name())
    start_d = dt.date.fromisoformat(start_date_iso[:10]) if start_date_iso else dt.date.today()
    days = max(1, min(30, int(days)))
    end_d = start_d + dt.timedelta(days=days - 1)

    busy_by_day: dict[str, list[tuple[int, int, str]]] = {
        (start_d + dt.timedelta(days=i)).isoformat(): [] for i in range(days)
    }

    # 1. Local Atlas calendar items (all FreeBusy-enabled layers including read-only mirrors + workouts + blockers)
    excluded_fb_files: set[str] = set()
    if cfg:
        for ly in (cfg.get("layers") or []):
            if isinstance(ly, dict) and ly.get("file"):
                if ly.get("mode") == "ignore" or not _layer_freebusy_enabled(ly):
                    excluded_fb_files.add(str(ly["file"]))

    cal_dir = os.path.join(brain, "calendar")
    if os.path.isdir(cal_dir):
        for fn in sorted(os.listdir(cal_dir)):
            if not fn.lower().endswith(".md") or fn in excluded_fb_files:
                continue
            rel = f"calendar/{fn}"
            _lines, items = parse_calendar_md_items(brain, rel)
            for it in items:
                if it["status"] != "open":
                    continue
                if it["kind"] in ("todo", "deadline", "goal", "birthday") and not it["time"]:
                    continue
                s_d = dt.date.fromisoformat(it["start"])
                e_d = dt.date.fromisoformat(it["end"])
                if e_d < start_d or s_d > end_d:
                    continue
                if it["time"]:
                    ts, te, dur = _parse_time_window(it["start"], it["end"], it["time"], it["what"], it["kind"])
                    sm = _time_to_min(ts or "")
                    if sm is not None:
                        em = sm + max(dur, 15)
                        if it["start"] in busy_by_day:
                            busy_by_day[it["start"]].append((sm, min(em, 24 * 60), f"{it['what']} ({fn})"))
                elif it["kind"] == "blocked":
                    cur = max(s_d, start_d)
                    while cur <= min(e_d, end_d):
                        busy_by_day[cur.isoformat()].append((work_start_hour * 60, work_end_hour * 60, f"[blocked] {it['what']}"))
                        cur += dt.timedelta(days=1)

    # 2. Optional live remote FreeBusy query across Google & CalDAV/Apple (catches unpulled changes in real time)
    remote_checked: list[str] = []
    if use_remote and cfg and cfg.get("enabled", True):
        prov = provider or build_default_provider(brain, timeout=8.0)
        by_acct: dict[str, list[str]] = {}
        for ly in (cfg.get("layers") or []):
            if not isinstance(ly, dict) or ly.get("mode") == "ignore" or not _layer_freebusy_enabled(ly):
                continue
            aid = str(ly.get("account") or "")
            cid = str(ly.get("calendar_id") or "")
            if aid and cid:
                by_acct.setdefault(aid, []).append(cid)
                sub = ly.get("subcalendars") or {}
                if sub.get("workout"):
                    by_acct[aid].append(str(sub["workout"]))
        t_min = gcal_provider.to_rfc3339(dt.datetime.combine(start_d, dt.time.min).astimezone())
        t_max = gcal_provider.to_rfc3339(dt.datetime.combine(end_d, dt.time.max).astimezone())
        for aid, cids in by_acct.items():
            uniq_cids = list(dict.fromkeys(cids))
            try:
                fb = prov.freebusy(aid, uniq_cids, t_min, t_max, time_zone=tz)
                remote_checked.append(aid)
                for cid, spans in fb.items():
                    for sp in spans:
                        s_dt = gcal_provider.parse_rfc3339(sp["start"])
                        e_dt = gcal_provider.parse_rfc3339(sp["end"])
                        if not s_dt or not e_dt:
                            continue
                        s_loc = s_dt.astimezone()
                        e_loc = e_dt.astimezone()
                        d_iso = s_loc.date().isoformat()
                        if d_iso in busy_by_day:
                            sm = s_loc.hour * 60 + s_loc.minute
                            em = (e_loc.hour * 60 + e_loc.minute) if e_loc.date() == s_loc.date() else 24 * 60
                            if em > sm:
                                busy_by_day[d_iso].append((sm, em, f"remote:{aid}"))
            except Exception:
                pass

    # 3. Merge busy intervals and compute free windows
    days_out: list[dict[str, Any]] = []
    w_start = work_start_hour * 60
    w_end = work_end_hour * 60

    def _fmt_m(m: int) -> str:
        return f"{m // 60:02d}:{m % 60:02d}"

    for d_iso in sorted(busy_by_day):
        raw_b = sorted(busy_by_day[d_iso], key=lambda x: (x[0], x[1]))
        merged: list[tuple[int, int]] = []
        for s_m, e_m, _lbl in raw_b:
            cs, ce = max(s_m, w_start), min(e_m, w_end)
            if ce <= cs:
                continue
            if merged and cs <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], ce))
            else:
                merged.append((cs, ce))
        free_slots: list[dict[str, Any]] = []
        cursor = w_start
        for bs, be in merged:
            if bs - cursor >= min_slot_minutes:
                free_slots.append({"start": _fmt_m(cursor), "end": _fmt_m(bs), "minutes": bs - cursor})
            cursor = max(cursor, be)
        if w_end - cursor >= min_slot_minutes:
            free_slots.append({"start": _fmt_m(cursor), "end": _fmt_m(w_end), "minutes": w_end - cursor})
        days_out.append({
            "date": d_iso,
            "busy": [{"start": _fmt_m(s), "end": _fmt_m(e)} for s, e in merged],
            "free": free_slots,
        })

    return {
        "ok": True,
        "start": start_d.isoformat(),
        "end": end_d.isoformat(),
        "window": f"{work_start_hour:02d}:00-{work_end_hour:02d}:00",
        "min_slot_minutes": min_slot_minutes,
        "remote_accounts_checked": remote_checked,
        "days": days_out,
    }


def create_event_guarded(
    brain: str,
    title: str,
    date_iso: str,
    time_spec: str = "",
    end_date_iso: str = "",
    account_id: str = "",
    calendar_id: str = "",
    layer_id: str = "",
    location: str = "",
    description: str = "",
    attendees: list[str] | None = None,
    add_meet: bool = False,
    recurrence: list[str] | None = None,
    kind: str = "event",
    execute: bool = False,
    provider: gcal_provider.CalendarProvider | None = None,
) -> dict[str, Any]:
    """Create a calendar event with strict Safety-Gate whenever external attendees or Google Meet are involved."""
    cfg = load_sync_config(brain)
    if not cfg:
        return {"ok": False, "error": "Calendar sync not configured (`brain/calendar/sync.yaml`)."}

    clean_attendees = [a.strip() for a in (attendees or []) if a and "@" in a]
    norm_rrules: list[str] = []
    for rr in (recurrence or []):
        rs = str(rr or "").strip()
        if rs:
            norm_rrules.append(rs if rs.upper().startswith("RRULE:") else f"RRULE:{rs}")
    needs_gate = bool(clean_attendees or add_meet)

    # Resolve layer & target account/calendar (Google or CalDAV/Apple)
    layers = [ly for ly in (cfg.get("layers") or []) if isinstance(ly, dict) and ly.get("mode") == "bidirectional"]
    chosen_layer = None
    if layer_id:
        lf = layer_id.strip().lower()
        chosen_layer = next((ly for ly in layers if str(ly.get("id") or "").lower() == lf or str(ly.get("file") or "").lower() == lf), None)
    if not chosen_layer and account_id:
        af = gcal_provider.sanitize_account_id(account_id)
        chosen_layer = next((ly for ly in layers if str(ly.get("account") or "") == af), None)
    if not chosen_layer:
        chosen_layer = next((ly for ly in layers if ly.get("file") == "README.md"), layers[0] if layers else None)
    if not chosen_layer:
        return {"ok": False, "error": "No bidirectional calendar layer found in `sync.yaml`."}

    target_aid = gcal_provider.sanitize_account_id(account_id or chosen_layer["account"])
    target_cid = calendar_id or str(chosen_layer["calendar_id"])
    rel = f"calendar/{chosen_layer['file']}"
    tz = str(cfg.get("timezone") or gcal_provider.local_timezone_name())
    acct_info = next((a for a in list_all_configured_accounts(brain) if a["id"] == target_aid), {})
    sender_email = acct_info.get("email") or target_aid
    target_provider = str(chosen_layer.get("provider") or acct_info.get("provider") or "gcal").lower()

    if add_meet and target_provider == "caldav":
        return {
            "ok": False,
            "error": (
                f"Account '{target_aid}' is a CalDAV/Apple calendar and cannot auto-generate Google Meet links. "
                "Target a Google Calendar account (`--account ...`) or omit `--meet` and put a video URL in `--location`."
            ),
        }

    preview = {
        "account": target_aid,
        "provider": target_provider,
        "sender": sender_email,
        "calendar_id": target_cid,
        "layer_file": rel,
        "title": title.strip(),
        "date": date_iso,
        "end_date": end_date_iso or date_iso,
        "time": time_spec,
        "location": location,
        "description": description,
        "attendees": clean_attendees,
        "google_meet": bool(add_meet),
        "recurrence": norm_rrules,
        "send_updates": "all" if clean_attendees else "none",
    }

    if needs_gate and not execute:
        return {
            "ok": True,
            "dry_run": True,
            "requires_confirmation": True,
            "preview": preview,
            "message": "Safety-Gate: external invite / Google Meet creation requires `--yes` after user confirmation.",
        }

    uid = _new_atlas_uid()
    item_stub = {
        "rel": rel,
        "start": date_iso,
        "end": end_date_iso or date_iso,
        "time": time_spec,
        "kind": kind,
        "subkind": "",
        "status": "open",
        "what": title.strip(),
    }
    ev_body = format_atlas_for_gcal(item_stub, uid, timezone=tz, existing_description=description)
    if location:
        ev_body["location"] = location.strip()
    if clean_attendees:
        ev_body["attendees"] = [{"email": em} for em in clean_attendees]
    if norm_rrules:
        ev_body["recurrence"] = norm_rrules

    prov = provider or build_default_provider(brain)
    created = prov.upsert_event(
        target_aid,
        target_cid,
        ev_body,
        send_updates="all" if clean_attendees else "none",
        conference_data=bool(add_meet),
    )
    pe = parse_gcal_event(created)
    new_ln = render_atlas_line(
        pe["start"] or date_iso,
        pe["end"] or end_date_iso or date_iso,
        pe["time"] or time_spec,
        kind, "", title.strip(), status="open", atlas_uid=uid,
    )

    with cal_engine.brain_write_lock(brain):
        _ensure_layer_md_file(brain, chosen_layer)
        _apply_markdown_edits(brain, rel, {}, [(pe["start"] or date_iso, new_ln)])
        conn = _open_db(brain)
        try:
            a_snap = _canonical_atlas_snapshot(parse_atlas_timeline_line(new_ln, rel) or item_stub)
            g_snap = _canonical_gcal_snapshot(pe)
            meta = dict(pe["meta"])
            meta["layer_label"] = str(chosen_layer.get("label") or chosen_layer.get("id") or "")
            meta["layer_color"] = str(chosen_layer.get("color") or "#38bdf8")
            conn.execute(
                """
                INSERT OR REPLACE INTO event_links
                (atlas_uid, layer_file, account_id, calendar_id, gcal_event_id, ical_uid, etag,
                 recurring_event_id, date_start, date_end, last_atlas_hash, last_gcal_hash,
                 last_atlas_snapshot, last_gcal_snapshot, meta_json, status, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, '', ?, ?, ?, ?, ?, ?, ?, 'active', ?)
                """,
                (
                    uid, rel, target_aid, target_cid, pe["gcal_event_id"], pe["ical_uid"], pe["etag"],
                    pe["start"] or date_iso, pe["end"] or date_iso,
                    _hash_snapshot(a_snap), _hash_snapshot(g_snap),
                    json.dumps(a_snap, ensure_ascii=False), json.dumps(g_snap, ensure_ascii=False),
                    json.dumps(meta, ensure_ascii=False), time.time(),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    return {
        "ok": True,
        "applied": True,
        "atlas_uid": uid,
        "gcal_event_id": pe["gcal_event_id"],
        "meet_link": pe["meta"].get("meet_link", ""),
        "html_link": pe["meta"].get("html_link", ""),
        "layer_file": rel,
        "line": new_ln,
    }


def rsvp_event_guarded(
    brain: str,
    query: str,
    response_status: str,
    execute: bool = False,
    provider: gcal_provider.CalendarProvider | None = None,
) -> dict[str, Any]:
    """RSVP (`accepted`, `declined`, `tentative`) to an event with strict Safety-Gate preview (`--yes`)."""
    hits = [h for h in find_event_details(brain, query) if h.get("gcal_event_id")]
    if not hits:
        return {"ok": False, "error": f"No synced calendar event matching '{query}'."}
    ev = hits[0]
    preview = {
        "atlas_uid": ev["atlas_uid"],
        "account": ev["account_id"],
        "calendar_id": ev["calendar_id"],
        "title": ev["what"],
        "date": ev["start"],
        "time": ev["time"],
        "organizer": ev["organizer"],
        "current_rsvp": ev["self_rsvp"] or "needsAction",
        "new_rsvp": response_status,
    }
    if not execute:
        return {
            "ok": True,
            "dry_run": True,
            "requires_confirmation": True,
            "preview": preview,
            "message": "Safety-Gate: RSVP notifies the event organizer and requires `--yes` after confirmation.",
        }

    prov = provider or build_default_provider(brain)
    updated = prov.rsvp_event(ev["account_id"], ev["calendar_id"], ev["gcal_event_id"], response_status)
    pe = parse_gcal_event(updated)
    with cal_engine.brain_write_lock(brain):
        if response_status == "declined" and ev["line"] > 0:
            new_ln = render_atlas_line(
                ev["start"], ev["end"], ev["time"],
                ev["kind"].split(":")[0], ev["kind"].split(":")[1] if ":" in ev["kind"] else "",
                ev["what"], status="skipped", atlas_uid=ev["atlas_uid"],
            )
            _apply_markdown_edits(brain, ev["rel"], {ev["line"]: new_ln}, [])
        conn = _open_db(brain)
        try:
            conn.execute(
                "UPDATE event_links SET etag=?, meta_json=?, updated_at=? WHERE atlas_uid=?",
                (pe["etag"], json.dumps(pe["meta"], ensure_ascii=False), time.time(), ev["atlas_uid"]),
            )
            conn.commit()
        finally:
            conn.close()
    return {"ok": True, "applied": True, "atlas_uid": ev["atlas_uid"], "rsvp": response_status, "title": ev["what"]}


# ---------------------------------------------------------------------------
# 9. Zero-Token Hook Triggers (PreInvocation Pull & Stop-Hook Push)
# ---------------------------------------------------------------------------

def maybe_auto_pull(
    brain: str,
    urgent: bool = False,
    timeout: float = 2.0,
    provider: gcal_provider.CalendarProvider | None = None,
) -> dict[str, Any] | None:
    """TTL-gated incremental pull for `PreInvocation` hook (fail-open, 0 LLM tokens)."""
    cfg = load_sync_config(brain)
    if not cfg or not cfg.get("enabled", True):
        return None
    accounts = provider.list_accounts() if provider is not None else list_all_configured_accounts(brain)
    if not any(a.get("connected") for a in accounts):
        warns = [a["id"] for a in accounts if a.get("auth_error")]
        return {"ran": False, "auth_warnings": warns, "open_conflicts": 0} if warns else None

    auto_cfg = cfg.get("auto_sync") or {}
    ttl_s = float(auto_cfg.get("calendar_prompt_ttl_seconds") or 60) if urgent else float(auto_cfg.get("prompt_ttl_minutes") or 15) * 60.0

    summary = get_sync_ui_summary(brain)
    age = time.time() - float(summary.get("last_pull_ts") or 0)
    if age < ttl_s:
        if summary.get("open_conflicts") or summary.get("auth_warnings"):
            return {
                "ran": False,
                "pulled_new": 0,
                "pulled_updated": 0,
                "pulled_cancelled": 0,
                "open_conflicts": summary["open_conflicts"],
                "auth_warnings": summary["auth_warnings"],
            }
        return None

    res = run_sync(brain, provider=provider, execute=True, pull_only=True, timeout=timeout)
    return {
        "ran": True,
        "pulled_new": res.get("pulled_new", 0),
        "pulled_updated": res.get("pulled_updated", 0),
        "pulled_cancelled": res.get("pulled_cancelled", 0),
        "open_conflicts": res.get("open_conflicts", 0),
        "auth_warnings": res.get("auth_warnings", []),
        "files_changed": res.get("files_changed", []),
    }


def has_dirty_calendar_files(brain: str) -> bool:
    """Detect whether `brain/calendar/*.md` or files with `due:` changed since last push."""
    # 1. Check git dirty files first (fast & authoritative in Atlas brain repo)
    if os.path.isdir(os.path.join(brain, ".git")):
        try:
            res = subprocess.run(
                ["git", "-C", brain, "status", "--porcelain", "-uall"],
                capture_output=True, text=True, timeout=2,
            )
            if res.returncode == 0:
                for ln in res.stdout.splitlines():
                    if len(ln) < 4:
                        continue
                    rel = ln[3:].strip().strip('"').split(" -> ")[-1].replace("\\", "/")
                    if rel.startswith("calendar/") and rel.endswith(".md"):
                        return True
                    if rel.endswith(".md") and not rel.startswith(("_", "journal/")):
                        full = os.path.join(brain, *rel.split("/"))
                        if os.path.isfile(full):
                            try:
                                with open(full, encoding="utf-8", errors="replace") as fh:
                                    head = fh.read(2048)
                                if "\ndue:" in head or head.startswith("due:"):
                                    return True
                            except OSError:
                                pass
        except Exception:
            pass
    # 2. Fallback: mtime check on `brain/calendar/*.md` against `last_push_ts`
    last_push = float(get_sync_ui_summary(brain).get("last_push_ts") or 0)
    cal_dir = os.path.join(brain, "calendar")
    if os.path.isdir(cal_dir):
        for fn in os.listdir(cal_dir):
            if fn.endswith(".md"):
                try:
                    if os.path.getmtime(os.path.join(cal_dir, fn)) > last_push:
                        return True
                except OSError:
                    pass
    return False


def auto_push_on_stop(
    brain: str,
    timeout: float = 4.0,
    provider: gcal_provider.CalendarProvider | None = None,
) -> dict[str, Any] | None:
    """Push dirty Atlas calendar changes to Google during the Stop hook (0 LLM tokens, fail-open)."""
    cfg = load_sync_config(brain)
    if not cfg or not cfg.get("enabled", True):
        return None
    if not (cfg.get("auto_sync") or {}).get("push_on_stop", True):
        return None
    if not has_dirty_calendar_files(brain):
        return None
    return run_sync(brain, provider=provider, execute=True, push_only=True, timeout=timeout)
