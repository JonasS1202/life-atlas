"""CalDAV (RFC 4791 / RFC 5545 / RFC 6578) Calendar Provider for Atlas (Python 3 stdlib only).

Supports Apple iCloud Calendar (iPhone, iPad, Mac, Apple Watch), Nextcloud, Fastmail,
Mailbox.org, Posteo, GMX, Web.de, and any standard RFC 4791 CalDAV server.

Implements the provider-agnostic `CalendarProvider` interface from `gcal_provider.py`
so `cal_sync.py` runs identical 3-way bidirectional sync, deduplication, Smart-Merge,
FreeBusy, Safety-Gated invites, and RSVP across Google Calendar and CalDAV/Apple side-by-side.

Credentials live exclusively in `brain/_secrets/caldav.env` (`0o600`, git-ignored).
"""
from __future__ import annotations

import base64
import calendar as pycal
import datetime as dt
import http.client
import os
import re
import ssl
import urllib.parse
import uuid
import xml.etree.ElementTree as ET
from typing import Any, Callable
from xml.sax.saxutils import escape as xml_escape

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover — Python 3.8 fallback
    ZoneInfo = None  # type: ignore[assignment]

from . import gcal_provider

DAV_NS = "DAV:"
CALDAV_NS = "urn:ietf:params:xml:ns:caldav"
CS_NS = "http://calendarserver.org/ns/"
APPLE_NS = "http://apple.com/ns/ical/"

REQUEST_TIMEOUT = 15.0
USER_AGENT = "AtlasCalSync/1.8 (CalDAV)"

# Injectable low-level HTTP transport for offline tests:
# `(method, url, headers, body_bytes, timeout) -> (status_code, response_headers_lower, response_bytes)`
CalDAVHttpTransport = Callable[
    [str, str, dict[str, str], bytes | None, float],
    tuple[int, dict[str, str], bytes],
]

# Built-in server presets (all generic; Apple iCloud uses App-Specific Passwords from account.apple.com)
CALDAV_PRESETS: dict[str, dict[str, str]] = {
    "icloud": {
        "label": "Apple iCloud Calendar",
        "url": "https://caldav.icloud.com/",
        "help": "Use your Apple ID email + a 16-character App-Specific Password from https://account.apple.com (Sign-In & Security -> App-Specific Passwords).",
    },
    "apple": {
        "label": "Apple iCloud Calendar",
        "url": "https://caldav.icloud.com/",
        "help": "Use your Apple ID email + a 16-character App-Specific Password from https://account.apple.com (Sign-In & Security -> App-Specific Passwords).",
    },
    "fastmail": {
        "label": "Fastmail",
        "url": "https://caldav.fastmail.com/.well-known/caldav",
        "help": "Use your Fastmail email + a Fastmail App Password with CalDAV access (Settings -> Privacy & Security -> Third-party apps).",
    },
    "nextcloud": {
        "label": "Nextcloud",
        "url": "",
        "path_suffix": "/remote.php/dav",
        "help": "Pass `--url https://cloud.example.com` + your username and a Nextcloud App Password (Settings -> Security -> Devices & sessions).",
    },
    "owncloud": {
        "label": "ownCloud",
        "url": "",
        "path_suffix": "/remote.php/dav",
        "help": "Pass `--url https://cloud.example.com` + your username and App Password.",
    },
    "mailbox": {
        "label": "Mailbox.org",
        "url": "https://dav.mailbox.org/caldav/",
        "help": "Use your Mailbox.org email address + password.",
    },
    "posteo": {
        "label": "Posteo",
        "url": "https://posteo.de:8443/calendars/{username}/",
        "help": "Use your Posteo email address + password.",
    },
    "gmx": {
        "label": "GMX",
        "url": "https://caldav.gmx.net/begenda/dav/{username}/calendar/",
        "help": "Use your GMX email address + password (enable CalDAV/CardDAV in GMX settings).",
    },
    "webde": {
        "label": "WEB.DE",
        "url": "https://caldav.web.de/begenda/dav/{username}/calendar/",
        "help": "Use your WEB.DE email address + password (enable CalDAV/CardDAV in WEB.DE settings).",
    },
    "caldav": {
        "label": "Generic CalDAV",
        "url": "",
        "help": "Pass `--url https://dav.example.com/...` + your CalDAV username and password.",
    },
}

_WINDOWS_TZ_MAP: dict[str, str] = {
    "W. Europe Standard Time": "Europe/Berlin",
    "Central Europe Standard Time": "Europe/Budapest",
    "Central European Standard Time": "Europe/Warsaw",
    "Romance Standard Time": "Europe/Paris",
    "GMT Standard Time": "Europe/London",
    "UTC": "UTC",
    "Coordinated Universal Time": "UTC",
    "Eastern Standard Time": "America/New_York",
    "Central Standard Time": "America/Chicago",
    "Mountain Standard Time": "America/Denver",
    "Pacific Standard Time": "America/Los_Angeles",
    "Tokyo Standard Time": "Asia/Tokyo",
    "AUS Eastern Standard Time": "Australia/Sydney",
    "India Standard Time": "Asia/Kolkata",
    "China Standard Time": "Asia/Shanghai",
    "FLE Standard Time": "Europe/Helsinki",
    "GTB Standard Time": "Europe/Bucharest",
    "Russian Standard Time": "Europe/Moscow",
}

_PARTSTAT_TO_GCAL = {
    "ACCEPTED": "accepted",
    "DECLINED": "declined",
    "TENTATIVE": "tentative",
    "NEEDS-ACTION": "needsAction",
    "DELEGATED": "needsAction",
}

_GCAL_TO_PARTSTAT = {
    "accepted": "ACCEPTED",
    "declined": "DECLINED",
    "tentative": "TENTATIVE",
    "needsaction": "NEEDS-ACTION",
}

_VIDEO_LINK_RX = re.compile(
    r"(https?://(?:meet\.google\.com|zoom\.us|[a-z0-9-]+\.zoom\.us|teams\.microsoft\.com|facetime\.apple\.com|meet\.jit\.si|whereby\.com)/[^\s<>\"')]+)",
    re.I,
)


# ---------------------------------------------------------------------------
# 1. Secret `.env` Helpers (`brain/_secrets/caldav.env`, `chmod 600`)
# ---------------------------------------------------------------------------

def caldav_secret_path(brain: str) -> str:
    return os.path.join(brain, "_secrets", "caldav.env")


def _acct_key(account_id: str, field: str) -> str:
    aid = gcal_provider.sanitize_account_id(account_id)
    return f"CALDAV_{aid.upper()}_{field.upper()}"


def load_caldav_env(brain: str) -> dict[str, str]:
    p = caldav_secret_path(brain)
    out: dict[str, str] = {}
    if not os.path.isfile(p):
        return out
    try:
        with open(p, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                s = line.strip()
                if not s or s.startswith("#") or "=" not in s:
                    continue
                k, v = s.split("=", 1)
                val = v.strip()
                if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
                    val = val[1:-1]
                out[k.strip()] = val
    except OSError:
        pass
    return out


def save_caldav_env(brain: str, env_dict: dict[str, str]) -> str:
    sec_dir = os.path.join(brain, "_secrets")
    os.makedirs(sec_dir, exist_ok=True)
    p = caldav_secret_path(brain)
    lines = ["# Managed by Atlas Calendar Sync (CalDAV / Apple iCloud) — local only, never committed"]
    for k in sorted(env_dict):
        v = str(env_dict[k] or "").replace("\n", " ").strip()
        if v:
            lines.append(f"{k}={v}")
    content = "\n".join(lines) + "\n"
    tmp = f"{p}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, content.encode("utf-8"))
    finally:
        os.close(fd)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, p)
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    return p


def _register_account_in_env(env: dict[str, str], account_id: str) -> None:
    aid = gcal_provider.sanitize_account_id(account_id)
    existing = [gcal_provider.sanitize_account_id(x) for x in (env.get("CALDAV_ACCOUNTS") or "").split(",") if x.strip()]
    if aid not in existing:
        existing.append(aid)
    env["CALDAV_ACCOUNTS"] = ",".join(existing)


def resolve_preset_url(preset: str, url: str = "", username: str = "") -> tuple[str, str]:
    """Normalize preset name and resolve the CalDAV entry URL."""
    pr = str(preset or "caldav").strip().lower()
    if pr == "apple":
        pr = "icloud"
    info = CALDAV_PRESETS.get(pr, CALDAV_PRESETS["caldav"])
    raw_u = str(url or "").strip()
    if raw_u:
        if not re.match(r"^https?://", raw_u, re.I):
            raw_u = "https://" + raw_u
        suffix = info.get("path_suffix", "")
        if suffix and suffix not in raw_u.lower():
            raw_u = raw_u.rstrip("/") + suffix
        return pr, raw_u
    tpl = info.get("url", "")
    if not tpl:
        return pr, ""
    if "{username}" in tpl:
        if not username:
            return pr, tpl
        return pr, tpl.replace("{username}", urllib.parse.quote(username.strip(), safe="@._+-"))
    return pr, tpl


def save_caldav_account(
    brain: str,
    account_id: str,
    preset: str = "icloud",
    url: str = "",
    username: str = "",
    password: str = "",
    email: str = "",
    principal_url: str = "",
    home_url: str = "",
) -> str:
    """Persist CalDAV account credentials securely in `brain/_secrets/caldav.env` (`chmod 600`)."""
    aid = gcal_provider.sanitize_account_id(account_id)
    norm_preset, norm_url = resolve_preset_url(preset, url=url, username=username)
    env = load_caldav_env(brain)
    _register_account_in_env(env, aid)
    env[_acct_key(aid, "PRESET")] = norm_preset
    if norm_url:
        env[_acct_key(aid, "URL")] = norm_url
    if username:
        env[_acct_key(aid, "USERNAME")] = username.strip()
    if password:
        env[_acct_key(aid, "PASSWORD")] = password.strip()
    eff_email = email.strip() or (username.strip() if "@" in username else env.get(_acct_key(aid, "EMAIL"), ""))
    if eff_email:
        env[_acct_key(aid, "EMAIL")] = eff_email
    if principal_url:
        env[_acct_key(aid, "PRINCIPAL_URL")] = principal_url.strip()
    if home_url:
        env[_acct_key(aid, "HOME_URL")] = home_url.strip()
    env.pop(_acct_key(aid, "AUTH_ERROR"), None)
    return save_caldav_env(brain, env)


def init_caldav_env_template(
    brain: str,
    account_id: str = "icloud",
    preset: str = "icloud",
    url: str = "",
    username: str = "",
) -> str:
    """Ensure `brain/_secrets/caldav.env` (`chmod 600`) exists with a clean commented template."""
    aid = gcal_provider.sanitize_account_id(account_id)
    norm_preset, norm_url = resolve_preset_url(preset, url=url, username=username or "you@icloud.com")
    p = caldav_secret_path(brain)
    if os.path.isfile(p):
        return p
    os.makedirs(os.path.dirname(p), exist_ok=True)
    up = aid.upper()
    tpl = (
        "# Atlas Calendar Sync — CalDAV / Apple iCloud Credentials (git-ignored, chmod 600)\n"
        "# Uncomment and fill in your credentials, then run:\n"
        f"#   atlas.py cal auth --provider {norm_preset} --account {aid} --verify\n"
        "#\n"
        f"# CALDAV_ACCOUNTS={aid}\n"
        f"# CALDAV_{up}_PRESET={norm_preset}\n"
        f"# CALDAV_{up}_URL={norm_url or 'https://caldav.icloud.com/'}\n"
        f"# CALDAV_{up}_USERNAME={username or 'you@icloud.com'}\n"
        f"# CALDAV_{up}_PASSWORD=xxxx-xxxx-xxxx-xxxx\n"
    )
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, tpl.encode("utf-8"))
    finally:
        os.close(fd)
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    return p


def list_configured_accounts(brain: str) -> list[dict[str, Any]]:
    """Return status dicts for all CalDAV / Apple iCloud accounts in `brain/_secrets/caldav.env`."""
    env = load_caldav_env(brain)
    ids: list[str] = []
    raw_list = (env.get("CALDAV_ACCOUNTS") or "").strip()
    if raw_list:
        for tok in raw_list.split(","):
            aid = gcal_provider.sanitize_account_id(tok)
            if aid and aid not in ids:
                ids.append(aid)
    for k in sorted(env):
        m = re.match(r"^CALDAV_([A-Z0-9_]+)_(?:URL|USERNAME|PASSWORD)$", k)
        if m:
            aid = gcal_provider.sanitize_account_id(m.group(1))
            if aid and aid not in ids:
                ids.append(aid)

    out: list[dict[str, Any]] = []
    for aid in ids:
        preset = (env.get(_acct_key(aid, "PRESET")) or "caldav").strip().lower()
        if preset == "apple":
            preset = "icloud"
        url = (env.get(_acct_key(aid, "URL")) or "").strip()
        user = (env.get(_acct_key(aid, "USERNAME")) or "").strip()
        pw = (env.get(_acct_key(aid, "PASSWORD")) or "").strip()
        if not url:
            preset, url = resolve_preset_url(preset, username=user)
        email = (env.get(_acct_key(aid, "EMAIL")) or (user if "@" in user else "")).strip()
        err = (env.get(_acct_key(aid, "AUTH_ERROR")) or "").strip()
        connected = bool(url and user and pw) and err not in ("auth_failed", "unauthorized")
        out.append({
            "id": aid,
            "provider": "caldav",
            "preset": preset,
            "url": url,
            "username": user,
            "email": email or user,
            "connected": connected,
            "has_refresh_token": False,
            "expires_at": 0,
            "auth_error": err,
        })
    return out


# ---------------------------------------------------------------------------
# 2. Pure-Stdlib Method-Preserving CalDAV HTTP Transport
# ---------------------------------------------------------------------------

def _same_credential_domain(old_host: str, new_host: str) -> bool:
    """Allow Authorization across redirects only on identical host or same trusted root domain."""
    oh = (old_host or "").strip().lower().rstrip(".")
    nh = (new_host or "").strip().lower().rstrip(".")
    if not oh or not nh:
        return False
    if oh == nh:
        return True
    # Allow Apple iCloud partition redirects (`caldav.icloud.com` -> `p42-caldav.icloud.com`)
    # and same-domain CalDAV clusters (`dav.example.com` -> `cal1.example.com`).
    op = oh.split(".")
    np = nh.split(".")
    if len(op) >= 2 and len(np) >= 2 and op[-2:] == np[-2:]:
        # Avoid broad public multi-label TLDs like co.uk
        if op[-2] not in ("co", "com", "org", "net", "gov", "ac", "edu"):
            return True
    return False


def _default_dav_http(
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes | None,
    timeout: float,
) -> tuple[int, dict[str, str], bytes]:
    """Execute a WebDAV/CalDAV HTTP request preserving method & body across redirects (stdlib only)."""
    cur_url = url
    cur_headers = dict(headers)
    cur_headers.setdefault("Accept-Encoding", "identity")
    ssl_ctx = ssl.create_default_context()

    for _hop in range(6):
        parsed = urllib.parse.urlparse(cur_url)
        scheme = (parsed.scheme or "https").lower()
        host = parsed.hostname or ""
        if not host:
            raise OSError(f"Invalid CalDAV URL: {cur_url}")
        port = parsed.port or (443 if scheme == "https" else 80)
        target = parsed.path or "/"
        if parsed.query:
            target = f"{target}?{parsed.query}"

        conn: http.client.HTTPConnection
        if scheme == "https":
            conn = http.client.HTTPSConnection(host, port, timeout=timeout, context=ssl_ctx)
        else:
            conn = http.client.HTTPConnection(host, port, timeout=timeout)
        try:
            conn.request(method, target, body=body, headers=cur_headers)
            resp = conn.getresponse()
            raw = resp.read()
            resp_hdrs = {k.lower(): v for k, v in resp.getheaders()}
            status = resp.status
        finally:
            conn.close()

        if status in (301, 302, 303, 307, 308) and resp_hdrs.get("location"):
            nxt = urllib.parse.urljoin(cur_url, resp_hdrs["location"].strip())
            nxt_p = urllib.parse.urlparse(nxt)
            nxt_scheme = (nxt_p.scheme or "").lower()
            nxt_host = nxt_p.hostname or ""
            # Never leak credentials across HTTPS->HTTP downgrade or foreign domain
            if (scheme == "https" and nxt_scheme != "https") or not _same_credential_domain(host, nxt_host):
                cur_headers = {k: v for k, v in cur_headers.items() if k.lower() != "authorization"}
            cur_url = nxt
            continue

        resp_hdrs["x-atlas-final-url"] = cur_url
        return status, resp_hdrs, raw

    raise OSError(f"Too many CalDAV redirects starting from {url}")


# ---------------------------------------------------------------------------
# 3. XML Parsing Helpers (Namespace-Agnostic WebDAV/CalDAV Multistatus)
# ---------------------------------------------------------------------------

def _local_name(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[1]
    return tag


def _xml_find(elem: ET.Element, local_name: str, ns: str | None = None) -> ET.Element | None:
    for child in elem:
        if _local_name(child.tag) == local_name:
            if ns is None or child.tag.startswith(f"{{{ns}}}"):
                return child
    return None


def _xml_findall(elem: ET.Element, local_name: str, ns: str | None = None) -> list[ET.Element]:
    out: list[ET.Element] = []
    for child in elem:
        if _local_name(child.tag) == local_name:
            if ns is None or child.tag.startswith(f"{{{ns}}}"):
                out.append(child)
    return out


def _xml_find_descendant(elem: ET.Element, local_name: str, ns: str | None = None) -> ET.Element | None:
    for node in elem.iter():
        if _local_name(node.tag) == local_name:
            if ns is None or node.tag.startswith(f"{{{ns}}}"):
                return node
    return None


def _parse_http_status_line(status_text: str) -> int:
    m = re.search(r"\b(\d{3})\b", str(status_text or ""))
    return int(m.group(1)) if m else 0


def _parse_multistatus(xml_bytes: bytes) -> tuple[list[dict[str, Any]], str]:
    """Parse a WebDAV `<multistatus>` payload into `(responses, root_sync_token)`."""
    if not xml_bytes or not xml_bytes.strip():
        return [], ""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise gcal_provider.CalendarProviderError(
            f"Invalid CalDAV XML response: {exc}", status=502, code="xml_error"
        ) from exc

    root_sync_el = _xml_find(root, "sync-token", DAV_NS)
    root_sync_token = (root_sync_el.text or "").strip() if root_sync_el is not None else ""

    responses: list[dict[str, Any]] = []
    for resp_el in _xml_findall(root, "response", DAV_NS):
        href_el = _xml_find(resp_el, "href", DAV_NS)
        href = (href_el.text or "").strip() if href_el is not None else ""
        top_status_el = _xml_find(resp_el, "status", DAV_NS)
        top_status = _parse_http_status_line(top_status_el.text if top_status_el is not None else "")

        ok_props: dict[str, ET.Element] = {}
        any_200 = False
        first_err_status = top_status
        for ps in _xml_findall(resp_el, "propstat", DAV_NS):
            st_el = _xml_find(ps, "status", DAV_NS)
            code = _parse_http_status_line(st_el.text if st_el is not None else "")
            if 200 <= code < 300:
                any_200 = True
                prop_el = _xml_find(ps, "prop", DAV_NS)
                if prop_el is not None:
                    for p_child in prop_el:
                        ok_props[_local_name(p_child.tag)] = p_child
            elif not first_err_status and code:
                first_err_status = code

        eff_status = 200 if any_200 else (top_status or first_err_status or 200)
        responses.append({
            "href": href,
            "status": eff_status,
            "props": ok_props,
        })
    return responses, root_sync_token


# ---------------------------------------------------------------------------
# 4. Pure-Stdlib RFC 5545 iCalendar Parser, RRULE Expander & Serializer
# ---------------------------------------------------------------------------

def _ics_unfold(text: str) -> list[str]:
    unfolded = re.sub(r"\r?\n[ \t]", "", str(text or ""))
    return unfolded.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def _ics_unescape(val: str) -> str:
    s = str(val or "")
    out: list[str] = []
    i = 0
    n = len(s)
    while i < n:
        if s[i] == "\\" and i + 1 < n:
            nxt = s[i + 1]
            if nxt in ("n", "N"):
                out.append("\n")
            elif nxt in (",", ";", "\\"):
                out.append(nxt)
            else:
                out.append(nxt)
            i += 2
        else:
            out.append(s[i])
            i += 1
    return "".join(out)


def _ics_escape(val: str) -> str:
    s = str(val or "").replace("\r\n", "\n").replace("\r", "\n")
    s = s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")
    return s


def _ics_fold_line(line: str) -> str:
    """Fold an iCalendar content line at 75 UTF-8 octets per RFC 5545 §3.1 without splitting codepoints."""
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line
    chunks: list[str] = []
    cur: list[str] = []
    cur_bytes = 0
    limit = 75
    for ch in line:
        cb = len(ch.encode("utf-8"))
        if cur_bytes + cb > limit and cur:
            chunks.append("".join(cur))
            cur = [ch]
            cur_bytes = cb
            limit = 74  # account for leading continuation space
        else:
            cur.append(ch)
            cur_bytes += cb
    if cur:
        chunks.append("".join(cur))
    return "\r\n ".join(chunks)


def _parse_ics_prop_line(line: str) -> tuple[str, dict[str, str], str] | None:
    """Parse `NAME;PARAM="VAL":VALUE` handling quoted colons/semicolons safely."""
    if not line or ":" not in line:
        return None
    in_q = False
    colon_idx = -1
    for i, ch in enumerate(line):
        if ch == '"':
            in_q = not in_q
        elif ch == ":" and not in_q:
            colon_idx = i
            break
    if colon_idx <= 0:
        return None
    lhs = line[:colon_idx]
    value = line[colon_idx + 1:]

    # Split lhs on `;` outside quotes
    parts: list[str] = []
    buf: list[str] = []
    in_q = False
    for ch in lhs:
        if ch == '"':
            in_q = not in_q
            buf.append(ch)
        elif ch == ";" and not in_q:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf))
    name = parts[0].strip().upper()
    params: dict[str, str] = {}
    for p in parts[1:]:
        if "=" in p:
            pk, pv = p.split("=", 1)
            params[pk.strip().upper()] = pv.strip().strip('"')
    return name, params, value


def _resolve_tzinfo(tzid: str, vtz_offsets: dict[str, dt.timezone] | None = None) -> dt.tzinfo:
    clean = str(tzid or "").strip().strip('"')
    if not clean:
        return dt.datetime.now().astimezone().tzinfo or dt.timezone.utc
    if clean.upper() in ("UTC", "Z", "GMT", "ETC/UTC"):
        return dt.timezone.utc
    mapped = _WINDOWS_TZ_MAP.get(clean, clean)
    if ZoneInfo is not None:
        try:
            return ZoneInfo(mapped)
        except Exception:
            if "/" in mapped:
                tail = mapped.split("/", 1)[-1]
                try:
                    return ZoneInfo(tail)
                except Exception:
                    pass
    if vtz_offsets and clean in vtz_offsets:
        return vtz_offsets[clean]
    return dt.datetime.now().astimezone().tzinfo or dt.timezone.utc


def _parse_ics_offset(raw: str) -> dt.timezone | None:
    m = re.match(r"^([+-])(\d{2})(\d{2})(?:\d{2})?$", str(raw or "").strip())
    if not m:
        return None
    sign = 1 if m.group(1) == "+" else -1
    delta = dt.timedelta(hours=int(m.group(2)), minutes=int(m.group(3))) * sign
    return dt.timezone(delta)


def _parse_ics_datetime(
    val: str,
    params: dict[str, str],
    vtz_offsets: dict[str, dt.timezone] | None = None,
) -> tuple[dt.datetime | dt.date | None, bool]:
    """Parse an iCalendar DATE or DATE-TIME value -> `(dt_or_date, is_all_day)`."""
    s = str(val or "").strip()
    if not s:
        return None, False
    val_type = (params.get("VALUE") or "").upper()
    if val_type == "DATE" or (len(s) == 8 and s.isdigit()):
        m = re.match(r"^(\d{4})(\d{2})(\d{2})$", s)
        if not m:
            return None, False
        try:
            return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3))), True
        except ValueError:
            return None, False

    m = re.match(r"^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})?(Z)?$", s)
    if not m:
        return None, False
    yr, mo, dy = int(m.group(1)), int(m.group(2)), int(m.group(3))
    hr, mi, sc = int(m.group(4)), int(m.group(5)), int(m.group(6) or 0)
    is_utc = bool(m.group(7))
    try:
        naive = dt.datetime(yr, mo, dy, hr, mi, sc)
    except ValueError:
        return None, False
    if is_utc:
        return naive.replace(tzinfo=dt.timezone.utc), False
    tzid = params.get("TZID", "")
    tz = _resolve_tzinfo(tzid, vtz_offsets=vtz_offsets)
    return naive.replace(tzinfo=tz), False


def _parse_ics_duration(raw: str) -> dt.timedelta | None:
    m = re.match(
        r"^([+-])?P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$",
        str(raw or "").strip().upper(),
    )
    if not m:
        return None
    sign = -1 if m.group(1) == "-" else 1
    weeks = int(m.group(2) or 0)
    days = int(m.group(3) or 0)
    hours = int(m.group(4) or 0)
    mins = int(m.group(5) or 0)
    secs = int(m.group(6) or 0)
    return dt.timedelta(weeks=weeks, days=days, hours=hours, minutes=mins, seconds=secs) * sign


def _to_aware_utc(val: dt.datetime | dt.date) -> dt.datetime:
    if isinstance(val, dt.datetime):
        if val.tzinfo is None:
            return val.astimezone().astimezone(dt.timezone.utc)
        return val.astimezone(dt.timezone.utc)
    return dt.datetime(val.year, val.month, val.day, tzinfo=dt.timezone.utc)


def _rec_id_key(val: dt.datetime | dt.date, is_all_day: bool) -> str:
    if is_all_day or not isinstance(val, dt.datetime):
        d = val.date() if isinstance(val, dt.datetime) else val
        return d.strftime("%Y%m%d")
    return _to_aware_utc(val).strftime("%Y%m%dT%H%M%SZ")


def _extract_mailto(raw: str) -> str:
    s = str(raw or "").strip()
    if s.lower().startswith("mailto:"):
        s = s[7:]
    return s.strip()


def parse_ics_vevents(ics_text: str) -> list[dict[str, Any]]:
    """Parse top-level `VEVENT` components from a `VCALENDAR` string (safely ignoring nested `VALARM` props)."""
    lines = _ics_unfold(ics_text)
    stack: list[str] = []
    vtz_offsets: dict[str, dt.timezone] = {}
    cur_tzid = ""
    cur_ev: dict[str, Any] | None = None
    events: list[dict[str, Any]] = []

    for line in lines:
        s = line.strip()
        if not s:
            continue
        up = s.upper()
        if up.startswith("BEGIN:"):
            comp = up.split(":", 1)[1].strip()
            stack.append(comp)
            if comp == "VTIMEZONE":
                cur_tzid = ""
            elif comp == "VEVENT":
                cur_ev = {"props": {}, "multi": {}}
            continue
        if up.startswith("END:"):
            comp = up.split(":", 1)[1].strip()
            if comp == "VEVENT" and cur_ev is not None:
                events.append(cur_ev)
                cur_ev = None
            if stack and stack[-1] == comp:
                stack.pop()
            continue

        parsed = _parse_ics_prop_line(line)
        if not parsed:
            continue
        name, params, val = parsed

        # Capture VTIMEZONE fallback offsets
        if "VTIMEZONE" in stack:
            if name == "TZID" and stack[-1] == "VTIMEZONE":
                cur_tzid = val.strip()
            elif name == "TZOFFSETTO" and cur_tzid and cur_tzid not in vtz_offsets:
                off = _parse_ics_offset(val)
                if off is not None:
                    vtz_offsets[cur_tzid] = off
            continue

        # Only record properties directly on VEVENT (skip nested VALARM!)
        if cur_ev is not None and stack and stack[-1] == "VEVENT":
            if name in ("ATTENDEE", "EXDATE", "RDATE", "RRULE"):
                cur_ev["multi"].setdefault(name, []).append((params, val))
            else:
                cur_ev["props"][name] = (params, val)

    for ev in events:
        ev["vtz_offsets"] = vtz_offsets
    return events


def _expand_rrule(
    dtstart: dt.datetime | dt.date,
    is_all_day: bool,
    rrule_str: str,
    win_start_utc: dt.datetime,
    win_end_utc: dt.datetime,
    max_occurrences: int = 2000,
) -> list[dt.datetime | dt.date]:
    """Pure-stdlib RFC 5545 RRULE expander (`DAILY`, `WEEKLY`, `MONTHLY`, `YEARLY`)."""
    parts: dict[str, str] = {}
    for seg in str(rrule_str or "").replace("RRULE:", "").split(";"):
        if "=" in seg:
            k, v = seg.split("=", 1)
            parts[k.strip().upper()] = v.strip().upper()
    freq = parts.get("FREQ", "")
    if freq not in ("DAILY", "WEEKLY", "MONTHLY", "YEARLY"):
        return [dtstart]
    try:
        interval = max(1, int(parts.get("INTERVAL") or "1"))
    except ValueError:
        interval = 1
    count: int | None = None
    if "COUNT" in parts:
        try:
            count = max(1, int(parts["COUNT"]))
        except ValueError:
            count = None
    until_utc: dt.datetime | None = None
    if "UNTIL" in parts:
        u_dt, u_all = _parse_ics_datetime(parts["UNTIL"], {})
        if u_dt is not None:
            if u_all and isinstance(u_dt, dt.date) and not isinstance(u_dt, dt.datetime):
                until_utc = dt.datetime(u_dt.year, u_dt.month, u_dt.day, 23, 59, 59, tzinfo=dt.timezone.utc)
            else:
                until_utc = _to_aware_utc(u_dt)

    wd_map = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}
    byday_raw = [x.strip() for x in parts.get("BYDAY", "").split(",") if x.strip()]
    bymonthday = [int(x) for x in parts.get("BYMONTHDAY", "").split(",") if re.match(r"^-?\d+$", x.strip())]
    bymonth = [int(x) for x in parts.get("BYMONTH", "").split(",") if x.strip().isdigit()]

    def _make_inst(d: dt.date) -> dt.datetime | dt.date:
        if is_all_day or not isinstance(dtstart, dt.datetime):
            return d
        return dt.datetime(d.year, d.month, d.day, dtstart.hour, dtstart.minute, dtstart.second, tzinfo=dtstart.tzinfo)

    start_d = dtstart.date() if isinstance(dtstart, dt.datetime) else dtstart
    hard_end_d = (min(until_utc, win_end_utc) if until_utc else win_end_utc).date() + dt.timedelta(days=2)
    emitted_total = 0
    results: list[dt.datetime | dt.date] = []

    def _accept(d: dt.date) -> bool:
        nonlocal emitted_total
        if d < start_d:
            return True
        inst = _make_inst(d)
        inst_utc = _to_aware_utc(inst)
        if until_utc is not None and inst_utc > until_utc:
            return False
        if d > hard_end_d:
            return False
        emitted_total += 1
        if inst_utc >= win_start_utc and inst_utc <= win_end_utc:
            results.append(inst)
        if count is not None and emitted_total >= count:
            return False
        if emitted_total >= max_occurrences:
            return False
        return True

    if freq == "DAILY":
        cur = start_d
        # Fast-forward only when COUNT is not specified (COUNT must count from DTSTART!)
        if count is None and (win_start_utc.date() - start_d).days > interval * 2:
            skip_steps = ((win_start_utc.date() - start_d).days - 2) // interval
            cur = start_d + dt.timedelta(days=skip_steps * interval)
        while cur <= hard_end_d:
            if not _accept(cur):
                break
            cur += dt.timedelta(days=interval)

    elif freq == "WEEKLY":
        target_wds = sorted({wd_map[x[-2:]] for x in byday_raw if x[-2:] in wd_map}) or [start_d.weekday()]
        week_mon = start_d - dt.timedelta(days=start_d.weekday())
        if count is None and (win_start_utc.date() - week_mon).days > 14 * interval:
            skip_w = ((win_start_utc.date() - week_mon).days - 14) // (7 * interval)
            week_mon = week_mon + dt.timedelta(weeks=skip_w * interval)
        while week_mon <= hard_end_d:
            stop = False
            for wd in target_wds:
                cand = week_mon + dt.timedelta(days=wd)
                if cand < start_d:
                    continue
                if not _accept(cand):
                    stop = True
                    break
            if stop:
                break
            week_mon += dt.timedelta(weeks=interval)

    elif freq == "MONTHLY":
        yr, mo = start_d.year, start_d.month
        while dt.date(yr, mo, 1) <= hard_end_d:
            days_in_mo = pycal.monthrange(yr, mo)[1]
            cands: list[dt.date] = []
            if byday_raw:
                for tok in byday_raw:
                    m_bd = re.match(r"^([+-]?\d+)?(MO|TU|WE|TH|FR|SA|SU)$", tok)
                    if not m_bd:
                        continue
                    ord_n = int(m_bd.group(1)) if m_bd.group(1) else 0
                    wd_i = wd_map[m_bd.group(2)]
                    matching = [dt.date(yr, mo, dy) for dy in range(1, days_in_mo + 1) if dt.date(yr, mo, dy).weekday() == wd_i]
                    if ord_n == 0:
                        cands.extend(matching)
                    elif ord_n > 0 and ord_n <= len(matching):
                        cands.append(matching[ord_n - 1])
                    elif ord_n < 0 and abs(ord_n) <= len(matching):
                        cands.append(matching[ord_n])
            else:
                target_days = bymonthday or [start_d.day]
                for td in target_days:
                    eff = td if td > 0 else (days_in_mo + 1 + td)
                    if 1 <= eff <= days_in_mo:
                        cands.append(dt.date(yr, mo, eff))
            stop = False
            for cand in sorted(set(cands)):
                if cand < start_d:
                    continue
                if not _accept(cand):
                    stop = True
                    break
            if stop:
                break
            total_mo = yr * 12 + (mo - 1) + interval
            yr, mo = total_mo // 12, (total_mo % 12) + 1

    elif freq == "YEARLY":
        yr = start_d.year
        target_mos = sorted(set(bymonth)) or [start_d.month]
        while dt.date(yr, 1, 1) <= hard_end_d:
            stop = False
            for mo in target_mos:
                days_in_mo = pycal.monthrange(yr, mo)[1]
                target_days = bymonthday or [start_d.day]
                for td in target_days:
                    eff = td if td > 0 else (days_in_mo + 1 + td)
                    if 1 <= eff <= days_in_mo:
                        cand = dt.date(yr, mo, eff)
                        if cand < start_d:
                            continue
                        if not _accept(cand):
                            stop = True
                            break
                if stop:
                    break
            if stop:
                break
            yr += interval

    return results


def _vevent_to_provider_dict(
    vev: dict[str, Any],
    event_id: str,
    etag: str,
    account_email: str = "",
    override_start: dt.datetime | dt.date | None = None,
    override_end: dt.datetime | dt.date | None = None,
    recurring_event_id: str = "",
) -> dict[str, Any]:
    """Convert a parsed VEVENT dict into the canonical provider event format shared with `GoogleCalendarProvider`."""
    props = vev["props"]
    multi = vev["multi"]
    vtz = vev.get("vtz_offsets") or {}

    def _p(key: str) -> str:
        pair = props.get(key)
        return _ics_unescape(pair[1]) if pair else ""

    uid = _p("UID").strip()
    summary = _p("SUMMARY").strip() or "(No Title)"
    description = _p("DESCRIPTION").strip()
    location = _p("LOCATION").strip()
    url_prop = _p("URL").strip()
    status_raw = _p("STATUS").strip().upper()
    status = "cancelled" if status_raw == "CANCELLED" else ("tentative" if status_raw == "TENTATIVE" else "confirmed")
    transp_raw = _p("TRANSP").strip().upper()
    transparency = "transparent" if transp_raw == "TRANSPARENT" else "opaque"

    dtstart_pair = props.get("DTSTART")
    dtend_pair = props.get("DTEND")
    dur_pair = props.get("DURATION")
    if override_start is not None:
        s_val = override_start
        s_all = isinstance(s_val, dt.date) and not isinstance(s_val, dt.datetime)
        e_val = override_end
    else:
        s_val, s_all = _parse_ics_datetime(dtstart_pair[1], dtstart_pair[0], vtz) if dtstart_pair else (None, False)
        e_val = None
        if dtend_pair:
            e_val, _ = _parse_ics_datetime(dtend_pair[1], dtend_pair[0], vtz)
        elif dur_pair and s_val is not None:
            td = _parse_ics_duration(dur_pair[1])
            if td is not None:
                e_val = s_val + td

    start_obj: dict[str, str] = {}
    end_obj: dict[str, str] = {}
    if s_val is not None:
        if s_all or not isinstance(s_val, dt.datetime):
            sd = s_val.date() if isinstance(s_val, dt.datetime) else s_val
            ed = (e_val.date() if isinstance(e_val, dt.datetime) else e_val) if e_val is not None else (sd + dt.timedelta(days=1))
            start_obj = {"date": sd.isoformat()}
            end_obj = {"date": ed.isoformat()}
        else:
            start_obj = {"dateTime": gcal_provider.to_rfc3339(s_val)}
            if isinstance(e_val, dt.datetime):
                end_obj = {"dateTime": gcal_provider.to_rfc3339(e_val)}
            else:
                end_obj = {"dateTime": gcal_provider.to_rfc3339(s_val + dt.timedelta(hours=1))}

    # Organizer & attendees
    acct_low = (account_email or "").strip().lower()
    org_pair = props.get("ORGANIZER")
    organizer: dict[str, Any] = {"email": acct_low, "self": True}
    if org_pair:
        org_email = _extract_mailto(org_pair[1])
        organizer = {
            "email": org_email,
            "displayName": _ics_unescape(org_pair[0].get("CN", "")),
            "self": (not acct_low) or (org_email.lower() == acct_low),
        }

    attendees: list[dict[str, Any]] = []
    for a_params, a_val in multi.get("ATTENDEE") or []:
        a_email = _extract_mailto(a_val)
        if not a_email:
            continue
        pstat = _PARTSTAT_TO_GCAL.get((a_params.get("PARTSTAT") or "NEEDS-ACTION").upper(), "needsAction")
        attendees.append({
            "email": a_email,
            "displayName": _ics_unescape(a_params.get("CN", "")),
            "responseStatus": pstat,
            "self": bool(acct_low and a_email.lower() == acct_low),
        })

    # Video conference link detection (URL, X-APPLE-CONFERENCE, LOCATION, DESCRIPTION)
    meet_link = ""
    for cand in (_p("X-APPLE-CONFERENCE"), url_prop, location, description):
        m_vid = _VIDEO_LINK_RX.search(cand or "")
        if m_vid:
            meet_link = m_vid.group(1).rstrip(".,;)")
            break

    priv: dict[str, str] = {}
    for xk, pk in (
        ("X-ATLAS-UID", "atlas_uid"),
        ("X-ATLAS-KIND", "atlas_kind"),
        ("X-ATLAS-LAYER", "atlas_layer"),
        ("X-ATLAS-STATUS", "atlas_status"),
    ):
        xv = _p(xk).strip()
        if xv:
            priv[pk] = xv

    out: dict[str, Any] = {
        "id": event_id,
        "iCalUID": uid,
        "etag": etag,
        "status": status,
        "summary": summary,
        "description": description,
        "location": location,
        "start": start_obj,
        "end": end_obj,
        "transparency": transparency,
        "organizer": organizer,
        "updated": _p("LAST-MODIFIED") or _p("DTSTAMP"),
    }
    if meet_link:
        out["hangoutLink"] = meet_link
    if url_prop:
        out["htmlLink"] = url_prop
    if attendees:
        out["attendees"] = attendees
    if priv:
        out["extendedProperties"] = {"private": priv}
    if recurring_event_id:
        out["recurringEventId"] = recurring_event_id
    rrule_list = [f"RRULE:{rv.replace('RRULE:', '')}" for _rp, rv in (multi.get("RRULE") or []) if rv]
    if rrule_list and not recurring_event_id:
        out["recurrence"] = rrule_list
    return out


def expand_ics_to_provider_events(
    ics_text: str,
    item_id: str,
    etag: str,
    time_min_utc: dt.datetime,
    time_max_utc: dt.datetime,
    single_events: bool = True,
    account_email: str = "",
) -> list[dict[str, Any]]:
    """Parse one CalDAV `.ics` resource and expand any `RRULE` / `EXDATE` / `RECURRENCE-ID` cleanly."""
    vevents = parse_ics_vevents(ics_text)
    if not vevents:
        return []

    masters = [v for v in vevents if "RECURRENCE-ID" not in v["props"]]
    overrides_raw = [v for v in vevents if "RECURRENCE-ID" in v["props"]]

    # Index overrides by normalized RECURRENCE-ID token
    overrides: dict[str, dict[str, Any]] = {}
    for ov in overrides_raw:
        r_pair = ov["props"]["RECURRENCE-ID"]
        r_dt, r_all = _parse_ics_datetime(r_pair[1], r_pair[0], ov.get("vtz_offsets"))
        if r_dt is not None:
            overrides[_rec_id_key(r_dt, r_all)] = ov

    out: list[dict[str, Any]] = []
    used_overrides: set[str] = set()

    for master in masters:
        rrules = master["multi"].get("RRULE") or []
        if not rrules or not single_events:
            out.append(_vevent_to_provider_dict(master, item_id, etag, account_email=account_email))
            continue

        # Recurring series: expand occurrences within `[time_min_utc, time_max_utc]`
        vtz = master.get("vtz_offsets") or {}
        dtstart_pair = master["props"].get("DTSTART")
        if not dtstart_pair:
            continue
        s_val, s_all = _parse_ics_datetime(dtstart_pair[1], dtstart_pair[0], vtz)
        if s_val is None:
            continue

        # Compute single occurrence duration
        occ_delta: dt.timedelta
        dtend_pair = master["props"].get("DTEND")
        dur_pair = master["props"].get("DURATION")
        if dtend_pair:
            e_val, _ = _parse_ics_datetime(dtend_pair[1], dtend_pair[0], vtz)
            if e_val is not None:
                if isinstance(s_val, dt.datetime) and isinstance(e_val, dt.datetime):
                    occ_delta = e_val - s_val
                elif not isinstance(s_val, dt.datetime) and not isinstance(e_val, dt.datetime):
                    occ_delta = e_val - s_val
                else:
                    occ_delta = dt.timedelta(days=1 if s_all else 0, hours=0 if s_all else 1)
            else:
                occ_delta = dt.timedelta(days=1 if s_all else 0, hours=0 if s_all else 1)
        elif dur_pair:
            occ_delta = _parse_ics_duration(dur_pair[1]) or dt.timedelta(days=1 if s_all else 0, hours=0 if s_all else 1)
        else:
            occ_delta = dt.timedelta(days=1 if s_all else 0, hours=0 if s_all else 1)

        # Collect EXDATE keys
        exdate_keys: set[str] = set()
        for ex_params, ex_val in master["multi"].get("EXDATE") or []:
            for tok in ex_val.split(","):
                ex_dt, ex_all = _parse_ics_datetime(tok.strip(), ex_params, vtz)
                if ex_dt is not None:
                    exdate_keys.add(_rec_id_key(ex_dt, ex_all))

        # Expand all RRULEs + RDATEs
        occs: list[dt.datetime | dt.date] = []
        for _rp, rrule_val in rrules:
            occs.extend(_expand_rrule(s_val, s_all, rrule_val, time_min_utc, time_max_utc))
        for rd_params, rd_val in master["multi"].get("RDATE") or []:
            for tok in rd_val.split(","):
                rd_dt, _ = _parse_ics_datetime(tok.strip(), rd_params, vtz)
                if rd_dt is not None and time_min_utc <= _to_aware_utc(rd_dt) <= time_max_utc:
                    occs.append(rd_dt)

        seen_keys: set[str] = set()
        for occ in sorted(occs, key=_to_aware_utc):
            rk = _rec_id_key(occ, s_all)
            if rk in seen_keys or rk in exdate_keys:
                continue
            seen_keys.add(rk)
            inst_id = f"{item_id}#{rk}"
            if rk in overrides:
                used_overrides.add(rk)
                out.append(
                    _vevent_to_provider_dict(
                        overrides[rk], inst_id, etag,
                        account_email=account_email, recurring_event_id=item_id,
                    )
                )
            else:
                out.append(
                    _vevent_to_provider_dict(
                        master, inst_id, etag,
                        account_email=account_email,
                        override_start=occ,
                        override_end=occ + occ_delta,
                        recurring_event_id=item_id,
                    )
                )

    # Include any moved RECURRENCE-ID overrides whose actual DTSTART falls inside the window
    for rk, ov in overrides.items():
        if rk in used_overrides:
            continue
        inst_id = f"{item_id}#{rk}"
        ev_dict = _vevent_to_provider_dict(
            ov, inst_id, etag, account_email=account_email, recurring_event_id=item_id
        )
        out.append(ev_dict)

    return out


def _fmt_utc_ical(d: dt.datetime) -> str:
    return _to_aware_utc(d).strftime("%Y%m%dT%H%M%SZ")


def _extract_vevent_blocks(ics_text: str) -> tuple[list[str], list[list[str]], list[str]]:
    """Split a VCALENDAR into `(header_lines_before_first_vevent, [vevent_line_lists], trailer_lines)` preserving VTIMEZONE."""
    lines = _ics_unfold(ics_text)
    header: list[str] = []
    vevents: list[list[str]] = []
    trailer: list[str] = []
    cur: list[str] | None = None
    seen_vevent = False

    for ln in lines:
        s = ln.strip()
        if not s:
            continue
        up = s.upper()
        if up == "BEGIN:VEVENT" and cur is None:
            seen_vevent = True
            cur = [ln]
            continue
        if cur is not None:
            cur.append(ln)
            if up == "END:VEVENT":
                vevents.append(cur)
                cur = None
            continue
        if up == "END:VCALENDAR":
            trailer.append(ln)
        elif not seen_vevent:
            header.append(ln)
        else:
            trailer.append(ln)

    if not header:
        header = [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "PRODID:-//life-atlas//CalDAV 1.8//EN",
            "CALSCALE:GREGORIAN",
        ]
    if not any(x.strip().upper() == "END:VCALENDAR" for x in trailer):
        trailer.append("END:VCALENDAR")
    return header, vevents, trailer


def _patch_vevent_lines(
    vev_lines: list[str],
    event_body: dict[str, Any],
    uid: str,
    account_email: str = "",
    recurrence_id: str = "",
) -> list[str]:
    """Patch (or build) a single `BEGIN:VEVENT ... END:VEVENT` block preserving VALARM & vendor `X-` props."""
    now_stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    existing_seq = 0
    preserved_top: list[str] = []
    nested_blocks: list[str] = []
    existing_uid = ""
    existing_created = ""

    # Determine which properties are being updated so untouched fields survive partial patches
    replace_keys: set[str] = {"DTSTAMP", "LAST-MODIFIED", "SEQUENCE"}
    if "summary" in event_body:
        replace_keys.add("SUMMARY")
    if "description" in event_body:
        replace_keys.add("DESCRIPTION")
    if "location" in event_body:
        replace_keys.add("LOCATION")
    if "start" in event_body:
        replace_keys.add("DTSTART")
    if "end" in event_body:
        replace_keys.update({"DTEND", "DURATION"})
    if "transparency" in event_body:
        replace_keys.add("TRANSP")
    if "status" in event_body:
        replace_keys.add("STATUS")
    if "attendees" in event_body:
        replace_keys.add("ATTENDEE")
    if "recurrence" in event_body:
        replace_keys.add("RRULE")
    priv = ((event_body.get("extendedProperties") or {}).get("private") or {})
    if isinstance(priv, dict):
        if "atlas_uid" in priv:
            replace_keys.add("X-ATLAS-UID")
        if "atlas_kind" in priv:
            replace_keys.add("X-ATLAS-KIND")
        if "atlas_layer" in priv:
            replace_keys.add("X-ATLAS-LAYER")
        if "atlas_status" in priv:
            replace_keys.add("X-ATLAS-STATUS")
    if recurrence_id:
        replace_keys.add("RECURRENCE-ID")
        replace_keys.add("RRULE")

    depth = 0
    for ln in vev_lines:
        up = ln.strip().upper()
        if up == "BEGIN:VEVENT" and depth == 0:
            depth = 1
            continue
        if up == "END:VEVENT" and depth == 1:
            depth = 0
            continue
        if up.startswith("BEGIN:"):
            depth += 1
            nested_blocks.append(ln)
            continue
        if up.startswith("END:"):
            nested_blocks.append(ln)
            depth = max(1, depth - 1)
            continue
        if depth > 1:
            nested_blocks.append(ln)
            continue

        parsed = _parse_ics_prop_line(ln)
        if not parsed:
            continue
        pname, _pparams, pval = parsed
        if pname == "UID":
            existing_uid = pval.strip()
            continue
        if pname == "CREATED":
            existing_created = ln
            continue
        if pname == "SEQUENCE":
            try:
                existing_seq = int(pval.strip())
            except ValueError:
                existing_seq = 0
            continue
        if pname in replace_keys:
            continue
        preserved_top.append(ln)

    final_uid = existing_uid or uid or f"atlas-{uuid.uuid4().hex[:12]}@life-atlas"
    new_props: list[str] = [
        "BEGIN:VEVENT",
        f"UID:{final_uid}",
        f"DTSTAMP:{now_stamp}",
        f"LAST-MODIFIED:{now_stamp}",
        existing_created or f"CREATED:{now_stamp}",
        f"SEQUENCE:{existing_seq + (1 if vev_lines else 0)}",
    ]

    if recurrence_id:
        if len(recurrence_id) == 8 and recurrence_id.isdigit():
            new_props.append(f"RECURRENCE-ID;VALUE=DATE:{recurrence_id}")
        else:
            new_props.append(f"RECURRENCE-ID:{recurrence_id}")

    if "start" in event_body and isinstance(event_body["start"], dict):
        st = event_body["start"]
        if st.get("date"):
            new_props.append(f"DTSTART;VALUE=DATE:{str(st['date'])[:10].replace('-', '')}")
        elif st.get("dateTime"):
            s_dt = gcal_provider.parse_rfc3339(str(st["dateTime"]))
            if s_dt:
                new_props.append(f"DTSTART:{_fmt_utc_ical(s_dt)}")

    if "end" in event_body and isinstance(event_body["end"], dict):
        en = event_body["end"]
        if en.get("date"):
            new_props.append(f"DTEND;VALUE=DATE:{str(en['date'])[:10].replace('-', '')}")
        elif en.get("dateTime"):
            e_dt = gcal_provider.parse_rfc3339(str(en["dateTime"]))
            if e_dt:
                new_props.append(f"DTEND:{_fmt_utc_ical(e_dt)}")

    if "summary" in event_body:
        new_props.append(f"SUMMARY:{_ics_escape(str(event_body['summary'] or ''))}")
    if "description" in event_body and event_body["description"] is not None:
        desc_s = str(event_body["description"])
        if desc_s:
            new_props.append(f"DESCRIPTION:{_ics_escape(desc_s)}")
    if "location" in event_body and event_body["location"]:
        new_props.append(f"LOCATION:{_ics_escape(str(event_body['location']))}")
    if "transparency" in event_body:
        tr = "TRANSPARENT" if str(event_body["transparency"]).lower() == "transparent" else "OPAQUE"
        new_props.append(f"TRANSP:{tr}")
    if "status" in event_body:
        st_m = {"cancelled": "CANCELLED", "tentative": "TENTATIVE"}.get(str(event_body["status"]).lower(), "CONFIRMED")
        new_props.append(f"STATUS:{st_m}")
    elif not vev_lines:
        new_props.append("STATUS:CONFIRMED")

    if "recurrence" in event_body and not recurrence_id:
        for rr in (event_body.get("recurrence") or []):
            rr_s = str(rr or "").strip()
            if rr_s:
                new_props.append(rr_s if rr_s.upper().startswith("RRULE:") else f"RRULE:{rr_s}")

    if "attendees" in event_body:
        has_org = any(ln.upper().startswith("ORGANIZER") for ln in preserved_top)
        if not has_org and account_email:
            new_props.append(f"ORGANIZER:mailto:{account_email}")
        for att in (event_body.get("attendees") or []):
            if not isinstance(att, dict) or not att.get("email"):
                continue
            a_em = str(att["email"]).strip()
            pstat = _GCAL_TO_PARTSTAT.get(str(att.get("responseStatus") or "needsAction").lower(), "NEEDS-ACTION")
            cn_bit = f';CN="{str(att["displayName"]).replace(chr(34), "")}"' if att.get("displayName") else ""
            new_props.append(f"ATTENDEE;PARTSTAT={pstat};RSVP=TRUE{cn_bit}:mailto:{a_em}")

    if isinstance(priv, dict):
        for pk, xk in (
            ("atlas_uid", "X-ATLAS-UID"),
            ("atlas_kind", "X-ATLAS-KIND"),
            ("atlas_layer", "X-ATLAS-LAYER"),
            ("atlas_status", "X-ATLAS-STATUS"),
        ):
            if priv.get(pk):
                new_props.append(f"{xk}:{_ics_escape(str(priv[pk]))}")

    new_props.extend(preserved_top)
    new_props.extend(nested_blocks)
    new_props.append("END:VEVENT")
    return new_props


def build_ics_payload(
    event_body: dict[str, Any],
    uid: str = "",
    existing_ics: str = "",
    recurrence_id: str = "",
    account_email: str = "",
) -> str:
    """Build or patch a complete RFC 5545 `VCALENDAR` document ready for CalDAV `PUT`."""
    if not existing_ics.strip():
        vev_lines = _patch_vevent_lines([], event_body, uid=uid, account_email=account_email, recurrence_id=recurrence_id)
        all_lines = [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "PRODID:-//life-atlas//CalDAV 1.8//EN",
            "CALSCALE:GREGORIAN",
            *vev_lines,
            "END:VCALENDAR",
        ]
        return "\r\n".join(_ics_fold_line(ln) for ln in all_lines) + "\r\n"

    header, vevents, trailer = _extract_vevent_blocks(existing_ics)
    if not recurrence_id:
        # Patch master VEVENT (first VEVENT without RECURRENCE-ID)
        patched = False
        new_vevents: list[list[str]] = []
        for blk in vevents:
            has_rid = any(ln.upper().startswith("RECURRENCE-ID") for ln in blk)
            if not has_rid and not patched:
                new_vevents.append(_patch_vevent_lines(blk, event_body, uid=uid, account_email=account_email))
                patched = True
            else:
                new_vevents.append(blk)
        if not patched:
            new_vevents.insert(0, _patch_vevent_lines([], event_body, uid=uid, account_email=account_email))
    else:
        # Patch or insert single-instance RECURRENCE-ID override without touching the master series
        master_blk = next((b for b in vevents if not any(ln.upper().startswith("RECURRENCE-ID") for ln in b)), [])
        master_uid = uid
        for ln in master_blk:
            p = _parse_ics_prop_line(ln)
            if p and p[0] == "UID":
                master_uid = p[2].strip()
                break
        matched_ov = False
        new_vevents = []
        for blk in vevents:
            rid_ln = next((ln for ln in blk if ln.upper().startswith("RECURRENCE-ID")), "")
            if rid_ln:
                p = _parse_ics_prop_line(rid_ln)
                if p:
                    r_dt, r_all = _parse_ics_datetime(p[2], p[1])
                    if r_dt is not None and _rec_id_key(r_dt, r_all) == recurrence_id:
                        new_vevents.append(
                            _patch_vevent_lines(blk, event_body, uid=master_uid, account_email=account_email, recurrence_id=recurrence_id)
                        )
                        matched_ov = True
                        continue
            new_vevents.append(blk)
        if not matched_ov:
            # Seed override from master fields + new overrides
            seed_blk = [ln for ln in master_blk if not ln.upper().startswith(("RRULE", "EXDATE", "RDATE"))]
            new_vevents.append(
                _patch_vevent_lines(seed_blk, event_body, uid=master_uid, account_email=account_email, recurrence_id=recurrence_id)
            )

    flat: list[str] = [*header]
    for blk in new_vevents:
        flat.extend(blk)
    flat.extend(trailer)
    return "\r\n".join(_ics_fold_line(ln) for ln in flat) + "\r\n"


def add_exdate_to_ics(existing_ics: str, recurrence_id: str) -> str:
    """Cancel a single occurrence of a recurring VEVENT by adding `EXDATE` and dropping matching `RECURRENCE-ID`."""
    header, vevents, trailer = _extract_vevent_blocks(existing_ics)
    ex_line = f"EXDATE;VALUE=DATE:{recurrence_id}" if (len(recurrence_id) == 8 and recurrence_id.isdigit()) else f"EXDATE:{recurrence_id}"
    new_vevents: list[list[str]] = []
    for blk in vevents:
        rid_ln = next((ln for ln in blk if ln.upper().startswith("RECURRENCE-ID")), "")
        if rid_ln:
            p = _parse_ics_prop_line(rid_ln)
            if p:
                r_dt, r_all = _parse_ics_datetime(p[2], p[1])
                if r_dt is not None and _rec_id_key(r_dt, r_all) == recurrence_id:
                    continue
            new_vevents.append(blk)
            continue
        # Insert EXDATE right before END:VEVENT on master
        if ex_line not in blk:
            blk = blk[:-1] + [ex_line, blk[-1]]
        new_vevents.append(blk)
    flat: list[str] = [*header]
    for blk in new_vevents:
        flat.extend(blk)
    flat.extend(trailer)
    return "\r\n".join(_ics_fold_line(ln) for ln in flat) + "\r\n"


# ---------------------------------------------------------------------------
# 5. CalDAVProvider (Apple iCloud, Nextcloud, Fastmail, Mailbox.org, Generic)
# ---------------------------------------------------------------------------

class CalDAVProvider(gcal_provider.CalendarProvider):
    """Pure-stdlib RFC 4791 / RFC 5545 / RFC 6578 CalDAV calendar provider."""

    provider_name = "caldav"

    def __init__(
        self,
        brain: str,
        timeout: float = REQUEST_TIMEOUT,
        http_fn: CalDAVHttpTransport | None = None,
    ):
        self.brain = brain
        self.timeout = float(timeout)
        self._http: CalDAVHttpTransport = http_fn or _default_dav_http

    def list_accounts(self) -> list[dict[str, Any]]:
        return list_configured_accounts(self.brain)

    def _account_creds(self, account_id: str) -> dict[str, str]:
        aid = gcal_provider.sanitize_account_id(account_id)
        env = load_caldav_env(self.brain)
        preset = (env.get(_acct_key(aid, "PRESET")) or "caldav").strip().lower()
        user = (env.get(_acct_key(aid, "USERNAME")) or "").strip()
        pw = (env.get(_acct_key(aid, "PASSWORD")) or "").strip()
        url = (env.get(_acct_key(aid, "URL")) or "").strip()
        if not url:
            preset, url = resolve_preset_url(preset, username=user)
        if not url or not user or not pw:
            raise gcal_provider.CalendarProviderError(
                f"CalDAV account '{aid}' credentials missing in `brain/_secrets/caldav.env`.",
                status=401,
                code="not_connected",
            )
        return {
            "id": aid,
            "preset": preset,
            "url": url,
            "username": user,
            "password": pw,
            "email": (env.get(_acct_key(aid, "EMAIL")) or (user if "@" in user else "")).strip(),
            "principal_url": (env.get(_acct_key(aid, "PRINCIPAL_URL")) or "").strip(),
            "home_url": (env.get(_acct_key(aid, "HOME_URL")) or "").strip(),
        }

    def _dav_request(
        self,
        account_id: str,
        method: str,
        url: str,
        body: str | bytes | None = None,
        content_type: str = "",
        depth: str | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        creds = self._account_creds(account_id)
        token_b64 = base64.b64encode(f"{creds['username']}:{creds['password']}".encode("utf-8")).decode("ascii")
        headers: dict[str, str] = {
            "Authorization": f"Basic {token_b64}",
            "User-Agent": USER_AGENT,
        }
        if content_type:
            headers["Content-Type"] = content_type
        if depth is not None:
            headers["Depth"] = str(depth)
        if extra_headers:
            headers.update(extra_headers)
        payload = body.encode("utf-8") if isinstance(body, str) else body

        try:
            status, resp_hdrs, raw = self._http(method, url, headers, payload, self.timeout)
        except Exception as exc:
            raise gcal_provider.CalendarProviderError(
                f"Network error calling CalDAV server ({creds['preset']}): {exc}",
                status=0,
                code="network_error",
            ) from exc

        if status in (401, 403):
            env = load_caldav_env(self.brain)
            if status == 401:
                env[_acct_key(creds["id"], "AUTH_ERROR")] = "unauthorized"
                save_caldav_env(self.brain, env)
            raise gcal_provider.CalendarProviderError(
                f"CalDAV authentication failed for '{creds['id']}' (HTTP {status}). "
                "Verify username and App-Specific Password in `brain/_secrets/caldav.env`.",
                status=status,
                code="unauthorized" if status == 401 else "forbidden",
            )
        return status, resp_hdrs, raw

    def discover_home_url(self, account_id: str, force: bool = False) -> str:
        """Discover `current-user-principal` -> `calendar-home-set` (cached in `caldav.env`)."""
        creds = self._account_creds(account_id)
        if creds["home_url"] and not force:
            return creds["home_url"]

        entry_url = creds["url"]
        principal_url = ""
        home_url = ""
        discovered_email = creds["email"]

        # Step 1: PROPFIND entry URL for current-user-principal (or direct calendar-home-set)
        q1 = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
            "<d:prop>"
            "<d:current-user-principal/>"
            "<c:calendar-home-set/>"
            "<c:calendar-user-address-set/>"
            "</d:prop>"
            "</d:propfind>"
        )
        st1, hdr1, raw1 = self._dav_request(
            account_id, "PROPFIND", entry_url, body=q1,
            content_type="application/xml; charset=utf-8", depth="0",
        )
        if st1 >= 400:
            raise gcal_provider.CalendarProviderError(
                f"CalDAV discovery failed at {entry_url} (HTTP {st1})", status=st1, code="discovery_error"
            )
        eff1 = hdr1.get("x-atlas-final-url") or entry_url
        resps1, _ = _parse_multistatus(raw1)
        for r in resps1:
            props = r["props"]
            if "calendar-home-set" in props:
                h_el = _xml_find_descendant(props["calendar-home-set"], "href", DAV_NS)
                if h_el is not None and (h_el.text or "").strip():
                    home_url = urllib.parse.urljoin(eff1, h_el.text.strip())
            if "current-user-principal" in props:
                p_el = _xml_find_descendant(props["current-user-principal"], "href", DAV_NS)
                if p_el is not None and (p_el.text or "").strip():
                    principal_url = urllib.parse.urljoin(eff1, p_el.text.strip())
            if "calendar-user-address-set" in props:
                for addr_el in props["calendar-user-address-set"].iter():
                    if _local_name(addr_el.tag) == "href" and (addr_el.text or "").lower().startswith("mailto:"):
                        discovered_email = _extract_mailto(addr_el.text or "")
                        break

        # Step 2: PROPFIND principal URL for calendar-home-set & email if not yet found
        if not home_url and principal_url:
            q2 = (
                '<?xml version="1.0" encoding="utf-8"?>'
                '<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
                "<d:prop>"
                "<c:calendar-home-set/>"
                "<c:calendar-user-address-set/>"
                "</d:prop>"
                "</d:propfind>"
            )
            st2, hdr2, raw2 = self._dav_request(
                account_id, "PROPFIND", principal_url, body=q2,
                content_type="application/xml; charset=utf-8", depth="0",
            )
            if st2 < 400:
                eff2 = hdr2.get("x-atlas-final-url") or principal_url
                resps2, _ = _parse_multistatus(raw2)
                for r in resps2:
                    props = r["props"]
                    if "calendar-home-set" in props:
                        h_el = _xml_find_descendant(props["calendar-home-set"], "href", DAV_NS)
                        if h_el is not None and (h_el.text or "").strip():
                            home_url = urllib.parse.urljoin(eff2, h_el.text.strip())
                    if "calendar-user-address-set" in props:
                        for addr_el in props["calendar-user-address-set"].iter():
                            if _local_name(addr_el.tag) == "href" and (addr_el.text or "").lower().startswith("mailto:"):
                                discovered_email = _extract_mailto(addr_el.text or "")
                                break

        if not home_url:
            # Fallback: entry URL itself may already be the calendar home
            home_url = eff1
        if not home_url.endswith("/"):
            home_url += "/"

        # Cache discovered URLs & email in caldav.env (clears any stale auth error)
        env = load_caldav_env(self.brain)
        aid = creds["id"]
        env[_acct_key(aid, "HOME_URL")] = home_url
        if principal_url:
            env[_acct_key(aid, "PRINCIPAL_URL")] = principal_url
        if discovered_email:
            env[_acct_key(aid, "EMAIL")] = discovered_email
        env.pop(_acct_key(aid, "AUTH_ERROR"), None)
        save_caldav_env(self.brain, env)
        return home_url

    def _resolve_cal_url(self, account_id: str, calendar_id: str) -> str:
        cid = str(calendar_id or "").strip()
        if re.match(r"^https?://", cid, re.I):
            return cid if cid.endswith("/") else cid + "/"
        home = self.discover_home_url(account_id)
        resolved = urllib.parse.urljoin(home, cid)
        return resolved if resolved.endswith("/") else resolved + "/"

    def _normalize_cal_id(self, cal_url: str, home_url: str) -> str:
        """Store stable path-based calendar ID when on same host as `home_url` (resilient to iCloud partition shifts)."""
        cp = urllib.parse.urlparse(cal_url)
        hp = urllib.parse.urlparse(home_url)
        if cp.netloc.lower() == hp.netloc.lower():
            path = cp.path or "/"
            return path if path.endswith("/") else path + "/"
        return cal_url if cal_url.endswith("/") else cal_url + "/"

    def verify_account(self, account_id: str) -> dict[str, Any]:
        """Verify CalDAV login, force-discover home URL, and return account + calendar count."""
        home = self.discover_home_url(account_id, force=True)
        cals = self.list_calendars(account_id)
        creds = self._account_creds(account_id)
        return {
            "ok": True,
            "account_id": creds["id"],
            "preset": creds["preset"],
            "email": creds["email"] or creds["username"],
            "home_url": home,
            "calendars_found": len(cals),
            "calendars": cals,
        }

    def list_calendars(self, account_id: str) -> list[dict[str, Any]]:
        """List all VEVENT-capable calendars under the account's `calendar-home-set`."""
        home_url = self.discover_home_url(account_id)
        q = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav" '
            'xmlns:cs="http://calendarserver.org/ns/" xmlns:a="http://apple.com/ns/ical/">'
            "<d:prop>"
            "<d:resourcetype/>"
            "<d:displayname/>"
            "<c:supported-calendar-component-set/>"
            "<a:calendar-color/>"
            "<cs:getctag/>"
            "<d:sync-token/>"
            "<d:current-user-privilege-set/>"
            "</d:prop>"
            "</d:propfind>"
        )
        status, hdrs, raw = self._dav_request(
            account_id, "PROPFIND", home_url, body=q,
            content_type="application/xml; charset=utf-8", depth="1",
        )
        if status == 404:
            # Cached partition URL may have shifted -> re-discover once
            home_url = self.discover_home_url(account_id, force=True)
            status, hdrs, raw = self._dav_request(
                account_id, "PROPFIND", home_url, body=q,
                content_type="application/xml; charset=utf-8", depth="1",
            )
        if status >= 400:
            raise gcal_provider.CalendarProviderError(
                f"CalDAV list_calendars failed for '{account_id}' (HTTP {status})",
                status=status, code="api_error",
            )

        eff_home = hdrs.get("x-atlas-final-url") or home_url
        home_path = urllib.parse.urlparse(eff_home).path.rstrip("/")
        resps, _ = _parse_multistatus(raw)

        items: list[dict[str, Any]] = []
        for r in resps:
            if r["status"] >= 400 or not r["href"]:
                continue
            full_u = urllib.parse.urljoin(eff_home, r["href"])
            r_path = urllib.parse.urlparse(full_u).path.rstrip("/")
            if r_path == home_path:
                continue
            props = r["props"]
            rt = props.get("resourcetype")
            if rt is None:
                continue
            rt_names = {_local_name(ch.tag) for ch in rt}
            if "calendar" not in rt_names:
                continue
            if rt_names & {"schedule-inbox", "schedule-outbox", "principal", "notification"}:
                continue

            # Critical Apple iCloud filter: skip VTODO-only Reminders lists!
            comp_el = props.get("supported-calendar-component-set")
            if comp_el is not None:
                comps = {
                    (ch.attrib.get("name") or "").upper()
                    for ch in comp_el
                    if _local_name(ch.tag) == "comp"
                }
                if comps and "VEVENT" not in comps:
                    continue

            disp = (props["displayname"].text or "").strip() if "displayname" in props else ""
            if not disp:
                disp = urllib.parse.unquote(r_path.rsplit("/", 1)[-1] or "Calendar")

            # Color normalization (`#1BADF8FF` -> `#1BADF8`)
            color_raw = (props["calendar-color"].text or "").strip() if "calendar-color" in props else ""
            color = color_raw[:7] if re.match(r"^#[0-9a-fA-F]{8}$", color_raw) else (color_raw if re.match(r"^#[0-9a-fA-F]{6}$", color_raw) else "")

            # Determine access_role from resourcetype (`subscribed` -> reader) and current-user-privilege-set
            role = "reader" if "subscribed" in rt_names else "owner"
            priv_el = props.get("current-user-privilege-set")
            if priv_el is not None and role != "reader":
                priv_names = {_local_name(n.tag) for n in priv_el.iter()}
                if not (priv_names & {"all", "write", "write-content", "bind"}):
                    role = "reader"

            cid = self._normalize_cal_id(full_u, eff_home)
            items.append({
                "id": cid,
                "summary": disp,
                "primary": False,
                "access_role": role,
                "background_color": color,
                "time_zone": "",
                "selected": True,
                "hidden": False,
                "ctag": (props["getctag"].text or "").strip() if "getctag" in props else "",
                "sync_token": (props["sync-token"].text or "").strip() if "sync-token" in props else "",
            })

        # Designate one primary writable non-birthday calendar for clean Atlas discovery
        if items:
            preferred_names = ("home", "privat", "personal", "calendar", "kalender", "default")
            prim = next(
                (
                    c for c in items
                    if c["access_role"] != "reader"
                    and c["summary"].strip().lower() in preferred_names
                ),
                None,
            )
            if prim is None:
                prim = next(
                    (
                        c for c in items
                        if c["access_role"] != "reader"
                        and not re.search(r"(?:geburtstag|birthday|feiertag|holiday|atlas\s)", c["summary"], re.I)
                    ),
                    items[0],
                )
            prim["primary"] = True
        return items

    def create_calendar(self, account_id: str, summary: str, time_zone: str = "UTC") -> dict[str, Any]:
        """Create a secondary CalDAV calendar collection via `MKCALENDAR` (RFC 4791 §5.3.1)."""
        home_url = self.discover_home_url(account_id)
        cal_slug = str(uuid.uuid4()).upper()
        target_url = urllib.parse.urljoin(home_url, f"{cal_slug}/")
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<c:mkcalendar xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
            "<d:set><d:prop>"
            f"<d:displayname>{xml_escape(summary.strip())}</d:displayname>"
            '<c:supported-calendar-component-set><c:comp name="VEVENT"/></c:supported-calendar-component-set>'
            "</d:prop></d:set>"
            "</c:mkcalendar>"
        )
        status, hdrs, _raw = self._dav_request(
            account_id, "MKCALENDAR", target_url, body=body,
            content_type="application/xml; charset=utf-8",
        )
        if status >= 400:
            raise gcal_provider.CalendarProviderError(
                f"CalDAV MKCALENDAR '{summary}' failed for '{account_id}' (HTTP {status})",
                status=status, code="mkcalendar_failed",
            )
        eff_u = hdrs.get("x-atlas-final-url") or target_url
        cid = self._normalize_cal_id(eff_u, home_url)
        return {
            "id": cid,
            "summary": summary.strip(),
            "time_zone": time_zone,
            "access_role": "owner",
            "primary": False,
        }

    def _fetch_collection_tokens(self, account_id: str, cal_url: str) -> tuple[str, str]:
        """Lightweight Depth:0 PROPFIND returning `(sync_token, ctag)`."""
        q = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<d:propfind xmlns:d="DAV:" xmlns:cs="http://calendarserver.org/ns/">'
            "<d:prop><d:sync-token/><cs:getctag/></d:prop>"
            "</d:propfind>"
        )
        try:
            st, _hdrs, raw = self._dav_request(
                account_id, "PROPFIND", cal_url, body=q,
                content_type="application/xml; charset=utf-8", depth="0",
            )
        except gcal_provider.CalendarProviderError as exc:
            if exc.status in (401, 403):
                raise
            return "", ""
        if st >= 400:
            return "", ""
        resps, root_st = _parse_multistatus(raw)
        sync_tok = root_st
        ctag = ""
        for r in resps:
            if "sync-token" in r["props"] and (r["props"]["sync-token"].text or "").strip():
                sync_tok = r["props"]["sync-token"].text.strip()
            if "getctag" in r["props"] and (r["props"]["getctag"].text or "").strip():
                ctag = r["props"]["getctag"].text.strip()
        return sync_tok, ctag

    @staticmethod
    def _encode_cursor(sync_tok: str, ctag: str) -> str:
        if sync_tok:
            return f"st:{sync_tok}"
        if ctag:
            return f"ctag:{ctag}"
        return ""

    @staticmethod
    def _item_id_from_href(href: str, cal_url: str) -> str:
        cal_path = urllib.parse.urlparse(cal_url).path
        if not cal_path.endswith("/"):
            cal_path += "/"
        item_path = urllib.parse.urlparse(urllib.parse.urljoin(cal_url, href)).path
        if item_path.startswith(cal_path):
            rel = item_path[len(cal_path):].lstrip("/")
            if rel:
                return rel
        return item_path.rsplit("/", 1)[-1] or href

    def _multiget(
        self,
        account_id: str,
        cal_url: str,
        hrefs: list[str],
    ) -> list[tuple[str, str, str]]:
        """Batch-fetch `(item_id, etag, ics_text)` for changed hrefs via RFC 4791 `calendar-multiget`."""
        if not hrefs:
            return []
        href_xml = "".join(f"<d:href>{xml_escape(h)}</d:href>" for h in hrefs)
        q = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<c:calendar-multiget xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
            "<d:prop><d:getetag/><c:calendar-data/></d:prop>"
            f"{href_xml}"
            "</c:calendar-multiget>"
        )
        st, _hdrs, raw = self._dav_request(
            account_id, "REPORT", cal_url, body=q,
            content_type="application/xml; charset=utf-8", depth="1",
        )
        if st >= 400:
            raise gcal_provider.CalendarProviderError(
                f"CalDAV calendar-multiget failed (HTTP {st})", status=st, code="api_error"
            )
        resps, _ = _parse_multistatus(raw)
        out: list[tuple[str, str, str]] = []
        for r in resps:
            if r["status"] >= 400:
                continue
            ics_el = r["props"].get("calendar-data")
            if ics_el is None or not (ics_el.text or "").strip():
                continue
            etag = (r["props"]["getetag"].text or "").strip() if "getetag" in r["props"] else ""
            out.append((self._item_id_from_href(r["href"], cal_url), etag, ics_el.text))
        return out

    def pull_events(
        self,
        account_id: str,
        calendar_id: str,
        time_min: str = "",
        time_max: str = "",
        sync_token: str = "",
        single_events: bool = True,
    ) -> dict[str, Any]:
        """Pull events via CTag fast-path, RFC 6578 `sync-collection` delta, or RFC 4791 `calendar-query`."""
        creds = self._account_creds(account_id)
        cal_url = self._resolve_cal_url(account_id, calendar_id)
        now_utc = dt.datetime.now(dt.timezone.utc)
        t_min_dt = gcal_provider.parse_rfc3339(time_min) or (now_utc - dt.timedelta(days=14))
        t_max_dt = gcal_provider.parse_rfc3339(time_max) or (now_utc + dt.timedelta(days=365))
        t_min_utc = _to_aware_utc(t_min_dt)
        t_max_utc = _to_aware_utc(t_max_dt)

        cur_st, cur_ctag = self._fetch_collection_tokens(account_id, cal_url)
        cur_cursor = self._encode_cursor(cur_st, cur_ctag)

        # 1. Zero-change fast path: unchanged sync-token or ctag -> 0 events, 1 tiny roundtrip!
        if sync_token:
            if sync_token.startswith("st:") and cur_st and sync_token[3:] == cur_st:
                return {"events": [], "next_sync_token": cur_cursor, "full_sync": False}
            if sync_token.startswith("ctag:") and cur_ctag and sync_token[5:] == cur_ctag:
                return {"events": [], "next_sync_token": cur_cursor, "full_sync": False}

        # 2. RFC 6578 incremental sync-collection delta when a `st:` token is stored
        if sync_token.startswith("st:"):
            raw_tok = sync_token[3:]
            q_sync = (
                '<?xml version="1.0" encoding="utf-8"?>'
                '<d:sync-collection xmlns:d="DAV:">'
                f"<d:sync-token>{xml_escape(raw_tok)}</d:sync-token>"
                "<d:sync-level>1</d:sync-level>"
                "<d:prop><d:getetag/></d:prop>"
                "</d:sync-collection>"
            )
            st, _hdrs, raw = self._dav_request(
                account_id, "REPORT", cal_url, body=q_sync,
                content_type="application/xml; charset=utf-8", depth="0",
            )
            if 200 <= st < 300:
                resps, new_st = _parse_multistatus(raw)
                changed_hrefs: list[str] = []
                events: list[dict[str, Any]] = []
                for r in resps:
                    if not r["href"]:
                        continue
                    item_id = self._item_id_from_href(r["href"], cal_url)
                    if r["status"] in (404, 410):
                        events.append({"id": item_id, "status": "cancelled"})
                    elif r["status"] < 400 and item_id.endswith(".ics"):
                        changed_hrefs.append(r["href"])
                    elif r["status"] < 400 and item_id and not item_id.endswith("/"):
                        changed_hrefs.append(r["href"])
                for item_id, etag, ics_txt in self._multiget(account_id, cal_url, changed_hrefs):
                    events.extend(
                        expand_ics_to_provider_events(
                            ics_txt, item_id, etag, t_min_utc, t_max_utc,
                            single_events=single_events, account_email=creds["email"],
                        )
                    )
                next_cursor = self._encode_cursor(new_st or cur_st, cur_ctag)
                return {"events": events, "next_sync_token": next_cursor, "full_sync": False}
            # On 400/403/410/412/501 (expired or unsupported sync-token): transparently fall back to full query!

        # 3. Full window pull via RFC 4791 `calendar-query` (time-range filter)
        tr_start = t_min_utc.strftime("%Y%m%dT%H%M%SZ")
        tr_end = t_max_utc.strftime("%Y%m%dT%H%M%SZ")
        q_full = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
            "<d:prop><d:getetag/><c:calendar-data/></d:prop>"
            "<c:filter>"
            '<c:comp-filter name="VCALENDAR">'
            '<c:comp-filter name="VEVENT">'
            f'<c:time-range start="{tr_start}" end="{tr_end}"/>'
            "</c:comp-filter>"
            "</c:comp-filter>"
            "</c:filter>"
            "</c:calendar-query>"
        )
        st, _hdrs, raw = self._dav_request(
            account_id, "REPORT", cal_url, body=q_full,
            content_type="application/xml; charset=utf-8", depth="1",
        )
        if st >= 400:
            raise gcal_provider.CalendarProviderError(
                f"CalDAV pull_events failed for {account_id}/{calendar_id} (HTTP {st})",
                status=st, code="api_error",
            )
        resps, _ = _parse_multistatus(raw)
        events = []
        for r in resps:
            if r["status"] >= 400:
                continue
            ics_el = r["props"].get("calendar-data")
            if ics_el is None or not (ics_el.text or "").strip():
                continue
            item_id = self._item_id_from_href(r["href"], cal_url)
            etag = (r["props"]["getetag"].text or "").strip() if "getetag" in r["props"] else ""
            events.extend(
                expand_ics_to_provider_events(
                    ics_el.text, item_id, etag, t_min_utc, t_max_utc,
                    single_events=single_events, account_email=creds["email"],
                )
            )
        return {
            "events": events,
            "next_sync_token": cur_cursor,
            "full_sync": True,
        }

    def _split_event_id(self, event_id: str) -> tuple[str, str]:
        if "#" in event_id:
            base, rid = event_id.split("#", 1)
            return base, rid
        return event_id, ""

    def upsert_event(
        self,
        account_id: str,
        calendar_id: str,
        event_body: dict[str, Any],
        event_id: str | None = None,
        send_updates: str = "none",
        conference_data: bool = False,
    ) -> dict[str, Any]:
        """Create or update an `.ics` resource via HTTP `PUT` (supports partial patches & single recurrence overrides)."""
        creds = self._account_creds(account_id)
        cal_url = self._resolve_cal_url(account_id, calendar_id)
        priv = ((event_body.get("extendedProperties") or {}).get("private") or {})
        atlas_uid = str(priv.get("atlas_uid") or "").strip() if isinstance(priv, dict) else ""

        existing_ics = ""
        recurrence_id = ""
        if event_id:
            item_id, recurrence_id = self._split_event_id(event_id)
            item_url = urllib.parse.urljoin(cal_url, item_id)
            st_get, _hdr_get, raw_get = self._dav_request(account_id, "GET", item_url)
            if 200 <= st_get < 300 and raw_get:
                existing_ics = raw_get.decode("utf-8", errors="replace")
            uid = ""
        else:
            slug = f"atlas-{atlas_uid}" if atlas_uid else f"atlas-{uuid.uuid4().hex[:12]}"
            item_id = f"{slug}.ics"
            item_url = urllib.parse.urljoin(cal_url, item_id)
            uid = f"{slug}@life-atlas"

        ics_payload = build_ics_payload(
            event_body,
            uid=uid,
            existing_ics=existing_ics,
            recurrence_id=recurrence_id,
            account_email=creds["email"],
        )
        st, hdrs, _raw = self._dav_request(
            account_id, "PUT", item_url, body=ics_payload,
            content_type="text/calendar; charset=utf-8",
        )
        if st >= 400:
            raise gcal_provider.CalendarProviderError(
                f"CalDAV PUT failed for {account_id}/{calendar_id}/{item_id} (HTTP {st})",
                status=st, code="api_error",
            )
        new_etag = hdrs.get("etag", "")
        vevs = parse_ics_vevents(ics_payload)
        target_vev = vevs[0] if vevs else None
        if recurrence_id and vevs:
            for v in vevs:
                r_pair = v["props"].get("RECURRENCE-ID")
                if r_pair:
                    r_dt, r_all = _parse_ics_datetime(r_pair[1], r_pair[0], v.get("vtz_offsets"))
                    if r_dt is not None and _rec_id_key(r_dt, r_all) == recurrence_id:
                        target_vev = v
                        break
        out_id = f"{item_id}#{recurrence_id}" if recurrence_id else item_id
        if target_vev is None:
            return {"id": out_id, "etag": new_etag, "status": "confirmed", **event_body}
        return _vevent_to_provider_dict(
            target_vev, out_id, new_etag,
            account_email=creds["email"],
            recurring_event_id=(item_id if recurrence_id else ""),
        )

    def delete_event(
        self,
        account_id: str,
        calendar_id: str,
        event_id: str,
        send_updates: str = "none",
    ) -> bool:
        """Delete an `.ics` resource (or add `EXDATE` when deleting a single recurring occurrence)."""
        cal_url = self._resolve_cal_url(account_id, calendar_id)
        item_id, recurrence_id = self._split_event_id(event_id)
        item_url = urllib.parse.urljoin(cal_url, item_id)

        if recurrence_id:
            st_get, _h, raw_get = self._dav_request(account_id, "GET", item_url)
            if st_get in (404, 410):
                return True
            if st_get >= 400:
                raise gcal_provider.CalendarProviderError(
                    f"CalDAV GET for recurrence delete failed (HTTP {st_get})", status=st_get, code="api_error"
                )
            patched = add_exdate_to_ics(raw_get.decode("utf-8", errors="replace"), recurrence_id)
            st_put, _hp, _rp = self._dav_request(
                account_id, "PUT", item_url, body=patched, content_type="text/calendar; charset=utf-8"
            )
            if st_put < 400:
                return True
            raise gcal_provider.CalendarProviderError(
                f"CalDAV PUT EXDATE failed (HTTP {st_put})", status=st_put, code="api_error"
            )

        st, _hdrs, _raw = self._dav_request(account_id, "DELETE", item_url)
        if st in (200, 204, 404, 410):
            return True
        raise gcal_provider.CalendarProviderError(
            f"CalDAV DELETE failed for {account_id}/{calendar_id}/{item_id} (HTTP {st})",
            status=st, code="api_error",
        )

    def rsvp_event(
        self,
        account_id: str,
        calendar_id: str,
        event_id: str,
        response_status: str,
        send_updates: str = "all",
    ) -> dict[str, Any]:
        """Update the user's `PARTSTAT` (`ACCEPTED`, `DECLINED`, `TENTATIVE`) on a CalDAV invite and `PUT` back."""
        valid = {"accepted", "declined", "tentative"}
        resp_s = str(response_status or "").strip().lower()
        if resp_s not in valid:
            raise ValueError(f"Invalid RSVP status '{response_status}'; expected one of {sorted(valid)}")

        creds = self._account_creds(account_id)
        acct_email = (creds["email"] or creds["username"]).strip().lower()
        cal_url = self._resolve_cal_url(account_id, calendar_id)
        item_id, recurrence_id = self._split_event_id(event_id)
        item_url = urllib.parse.urljoin(cal_url, item_id)

        st_get, hdr_get, raw_get = self._dav_request(account_id, "GET", item_url)
        if st_get >= 400 or not raw_get:
            raise gcal_provider.CalendarProviderError(
                f"CalDAV rsvp_event lookup failed (HTTP {st_get})", status=st_get, code="api_error"
            )
        ics_txt = raw_get.decode("utf-8", errors="replace")
        vevs = parse_ics_vevents(ics_txt)
        if not vevs:
            raise gcal_provider.CalendarProviderError("CalDAV RSVP target has no VEVENT.", status=400, code="invalid_ics")

        parsed_ev = _vevent_to_provider_dict(vevs[0], event_id, hdr_get.get("etag", ""), account_email=acct_email)
        attendees = list(parsed_ev.get("attendees") or [])
        updated = False
        for att in attendees:
            if att.get("self") or (acct_email and str(att.get("email") or "").strip().lower() == acct_email):
                att["responseStatus"] = resp_s
                updated = True
                break
        if not updated:
            if acct_email:
                attendees.append({"email": acct_email, "self": True, "responseStatus": resp_s})
            else:
                raise gcal_provider.CalendarProviderError(
                    "Could not identify user email for CalDAV RSVP.", status=400, code="self_attendee_missing"
                )
        return self.upsert_event(
            account_id, calendar_id, {"attendees": attendees},
            event_id=event_id, send_updates=send_updates,
        )

    def freebusy(
        self,
        account_id: str,
        calendar_ids: list[str],
        time_min: str,
        time_max: str,
        time_zone: str = "UTC",
    ) -> dict[str, list[dict[str, str]]]:
        """Compute busy spans across one or more CalDAV calendars via window `calendar-query` (universal RFC 4791)."""
        out: dict[str, list[dict[str, str]]] = {}
        for cid in calendar_ids:
            res = self.pull_events(
                account_id, cid, time_min=time_min, time_max=time_max,
                sync_token="", single_events=True,
            )
            spans: list[dict[str, str]] = []
            for ev in res.get("events") or []:
                if ev.get("status") == "cancelled" or ev.get("transparency") == "transparent":
                    continue
                st = (ev.get("start") or {}).get("dateTime")
                en = (ev.get("end") or {}).get("dateTime")
                if st and en:
                    spans.append({"start": st, "end": en})
            out[str(cid)] = spans
        return out
