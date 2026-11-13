#!/usr/bin/env python3
"""
Atlas Multi-Account IMAP Engine (atlas_mail.py).
Reads credentials strictly from <ROOT>/brain/_secrets/mail.env.

Architecture & Safety Principles:
1. All read/inspection commands (accounts, folders, list, read, search, audit, and
   inspect-only flags/labels) strictly select mailboxes with readonly=True and fetch
   bodies via BODY.PEEK[], so read/unread states (\\Seen) are NEVER mutated accidentally.
2. Write commands (move, copy, trash, delete, flags, labels, mkdir) enforce a multi-stage
   Safety Gate:
   - Stage 1: Account Write-Guard (checks MAIL_<ID>_ALLOW_WRITE in mail.env; default allowed
     unless explicitly set to false/0).
   - Stage 2: Pre-Flight Preview (fetches affected UIDs in readonly=True first and prints
     exact Subject, From, Date, current FLAGS/LABELS, and target_folder / planned action).
   - Stage 3: Execution Gate (--dry-run stops after preview; without --yes / -y, interactive
     TTY prompts for confirmation, and non-interactive agent calls abort with instructions
     to ask the user first).
3. Standards Compliance:
   - RFC 3501 (IMAP4rev1: FLAGS, COPY, STORE, EXPUNGE, Modified UTF-7 folder names)
   - RFC 4315 (IMAP UIDPLUS: UID EXPUNGE for surgical single-UID expunging)
   - RFC 6154 (IMAP LIST Extension for Special-Use Mailboxes: \\Trash, \\Sent, \\All, \\Archive)
   - RFC 6851 (IMAP MOVE Extension: atomic UID MOVE with safe COPY + STORE + EXPUNGE fallback)
   - Gmail IMAP Extensions (X-GM-EXT-1: X-GM-LABELS management)
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import email
import email.header
import email.message
import email.utils
from html.parser import HTMLParser
import imaplib
import json
import os
from pathlib import Path
import re
import shlex
import ssl
import stat
import sys
from typing import Any

sys.dont_write_bytecode = True


def resolve_default_secrets_path(explicit_root: str | None = None, explicit_env: str | None = None) -> Path:
    """Dynamically locate <ROOT>/brain/_secrets/mail.env across any OS or installation path."""
    if explicit_env:
        return Path(explicit_env).expanduser().resolve()
    root_env = explicit_root or os.environ.get("ATLAS_ROOT")
    if root_env:
        root_path = Path(root_env).expanduser().resolve()
        if (root_path / "_secrets").is_dir() or root_path.name == "brain":
            return root_path / "_secrets" / "mail.env"
        return root_path / "brain" / "_secrets" / "mail.env"

    here = Path(__file__).resolve()
    for parent in here.parents:
        if parent.name == "brain" and ((parent / "_atlas").exists() or (parent / "INDEX.md").exists()):
            return parent / "_secrets" / "mail.env"
        if (parent / "brain" / "_atlas").exists():
            return parent / "brain" / "_secrets" / "mail.env"

    default_root = Path(r"C:\Atlas") if os.name == "nt" else (Path.home() / "Atlas")
    return default_root / "brain" / "_secrets" / "mail.env"


SECRETS_PATH = resolve_default_secrets_path()

DEFAULT_HOSTS = {
    "gmail.com": "imap.gmail.com",
    "googlemail.com": "imap.gmail.com",
    "icloud.com": "imap.mail.me.com",
    "me.com": "imap.mail.me.com",
    "mac.com": "imap.mail.me.com",
    "outlook.com": "outlook.office365.com",
    "hotmail.com": "outlook.office365.com",
    "live.com": "outlook.office365.com",
    "office365.com": "outlook.office365.com",
    "t-online.de": "secureimap.t-online.de",
    "gmx.de": "imap.gmx.net",
    "gmx.net": "imap.gmx.net",
    "web.de": "imap.web.de",
    "posteo.de": "posteo.de",
    "mailbox.org": "imap.mailbox.org",
    "fastmail.com": "imap.fastmail.com",
    "yahoo.com": "imap.mail.yahoo.com",
    "zoho.com": "imappro.zoho.com",
    "zohomail.eu": "imappro.zoho.eu",
    "strato.de": "imap.strato.de",
    "ionos.de": "imap.ionos.de",
    "1und1.de": "imap.1und1.de",
}

STANDARD_FLAG_ALIASES = {
    "seen": "\\Seen",
    "read": "\\Seen",
    "flagged": "\\Flagged",
    "star": "\\Flagged",
    "starred": "\\Flagged",
    "answered": "\\Answered",
    "replied": "\\Answered",
    "draft": "\\Draft",
    "deleted": "\\Deleted",
}

MAIL_ENV_TEMPLATE = """# Atlas Mail Credentials (brain/_secrets/mail.env)
# Git-ignored & private (chmod 600). You can configure as many accounts as you like
# by choosing any uppercase identifier <ID> (e.g. PERSONAL, WORK, GMAIL, ICLOUD):
#
# MAIL_<ID>_LABEL="My Work Mail"
# MAIL_<ID>_HOST="imap.gmail.com"         # e.g. imap.gmail.com, outlook.office365.com, imap.mail.me.com, imap.strato.de
# MAIL_<ID>_PORT="993"                    # 993 (IMAPS SSL/TLS)
# MAIL_<ID>_USER="you@example.com"
# MAIL_<ID>_PASSWORD="your-app-password"  # For Gmail/Google Workspace/iCloud use a 16-char App Password
# MAIL_<ID>_ALLOW_WRITE="true"            # Optional: set to "false" to strictly block all write operations on this account
# MAIL_<ID>_TRASH_FOLDER=""               # Optional: custom Trash folder name (auto-detected via RFC 6154 \\Trash by default)
"""


class _HTMLStripper(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("style", "script", "head"):
            self._skip = True
        elif tag in ("p", "br", "div", "tr", "li", "h1", "h2", "h3", "h4"):
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("style", "script", "head"):
            self._skip = False

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)

    def get_text(self) -> str:
        raw = "".join(self.parts)
        lines = [re.sub(r"[ \t]+", " ", line).strip() for line in raw.splitlines()]
        cleaned = "\n".join(lines)
        return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


def html_to_text(html_content: str) -> str:
    stripper = _HTMLStripper()
    try:
        stripper.feed(html_content)
        return stripper.get_text()
    except Exception:
        return re.sub(r"<[^>]+>", " ", html_content).strip()


def decode_imap_utf7(s: str) -> str:
    """Decode modified UTF-7 used by IMAP folder names and Gmail labels (RFC 3501 §5.1.3)."""
    res: list[str] = []
    i = 0
    n = len(s)
    while i < n:
        if s[i] == "&":
            end = s.find("-", i + 1)
            if end == -1:
                res.append(s[i:])
                break
            if end == i + 1:
                res.append("&")
            else:
                b64 = s[i + 1 : end].replace(",", "/")
                pad = (-len(b64)) % 4
                b64 += "=" * pad
                try:
                    res.append(base64.b64decode(b64).decode("utf-16-be"))
                except Exception:
                    res.append(s[i : end + 1])
            i = end + 1
        else:
            res.append(s[i])
            i += 1
    return "".join(res)


def encode_imap_utf7(s: str) -> str:
    """Encode string into modified UTF-7 for IMAP folder names and labels (RFC 3501 §5.1.3)."""
    res: list[str] = []
    non_ascii_buf: list[str] = []

    def flush_buf() -> None:
        if not non_ascii_buf:
            return
        utf16_bytes = "".join(non_ascii_buf).encode("utf-16-be")
        b64 = base64.b64encode(utf16_bytes).decode("ascii").rstrip("=").replace("/", ",")
        res.append(f"&{b64}-")
        non_ascii_buf.clear()

    for ch in s:
        code = ord(ch)
        if 0x20 <= code <= 0x7E:
            flush_buf()
            if ch == "&":
                res.append("&-")
            else:
                res.append(ch)
        else:
            non_ascii_buf.append(ch)
    flush_buf()
    return "".join(res)


def decode_mime_header(value: str | None) -> str:
    if not value:
        return ""
    parts = email.header.decode_header(value)
    decoded: list[str] = []
    for content, charset in parts:
        if isinstance(content, bytes):
            enc = charset or "utf-8"
            try:
                decoded.append(content.decode(enc, errors="replace"))
            except LookupError:
                decoded.append(content.decode("utf-8", errors="replace"))
        else:
            decoded.append(str(content))
    return "".join(decoded).strip()


def load_accounts(env_path: Path | None = None) -> dict[str, dict[str, Any]]:
    target_env = env_path or SECRETS_PATH
    if not target_env.exists():
        return {}
    raw_env: dict[str, str] = {}
    for line in target_env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip()
        if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
            v = v[1:-1]
        raw_env[k] = v

    accounts: dict[str, dict[str, Any]] = {}
    pattern = re.compile(
        r"^MAIL_([A-Z0-9_]+)_(HOST|PORT|USER|PASSWORD|PASS|LABEL|DEFAULT_FOLDER|ALLOW_WRITE|TRASH_FOLDER|SSL)$"
    )
    for k, v in raw_env.items():
        m = pattern.match(k)
        if not m:
            continue
        acc_id = m.group(1).lower()
        field = m.group(2).lower()
        if field == "pass":
            field = "password"
        accounts.setdefault(acc_id, {"id": acc_id})[field] = v

    for acc_id, acc in list(accounts.items()):
        user = acc.get("user", "").strip()
        if not user or user.startswith("DEIN_") or acc.get("password", "").startswith("DEIN_"):
            acc["configured"] = False
        else:
            acc["configured"] = bool(acc.get("password"))
        if not acc.get("host") and "@" in user:
            domain = user.split("@", 1)[1].lower()
            acc["host"] = DEFAULT_HOSTS.get(domain, f"imap.{domain}")
        acc["port"] = int(acc.get("port", 993))
        acc.setdefault("label", acc_id)
        allow_write_raw = str(acc.get("allow_write", "true")).strip().lower()
        acc["allow_write"] = allow_write_raw not in ("false", "0", "no", "off", "readonly")
        ssl_raw = str(acc.get("ssl", "true")).strip().lower()
        acc["ssl"] = ssl_raw not in ("false", "0", "no", "off", "plain")
    return accounts


def connect_imap(acc: dict[str, Any]) -> imaplib.IMAP4:
    if not acc.get("configured"):
        raise RuntimeError(
            f"Account '{acc['id']}' ist in {SECRETS_PATH} noch nicht vollständig hinterlegt (USER/PASSWORD prüfen)."
        )
    host = acc["host"]
    port = acc["port"]
    user = acc["user"]
    password = acc["password"]
    # Strip spaces if 16-char App Password was pasted in 4x4 blocks
    if len(password.replace(" ", "")) == 16 and " " in password:
        password = password.replace(" ", "")

    if acc.get("ssl", True):
        ctx = ssl.create_default_context()
        conn: imaplib.IMAP4 = imaplib.IMAP4_SSL(host, port, ssl_context=ctx, timeout=20)
    else:
        conn = imaplib.IMAP4(host, port, timeout=20)
    conn.login(user, password)
    return conn


def get_server_capabilities(conn: imaplib.IMAP4) -> set[str]:
    """Return uppercase IMAP capability tokens after login (e.g. MOVE, UIDPLUS, X-GM-EXT-1)."""
    caps: set[str] = {
        (c.decode("ascii", errors="replace") if isinstance(c, bytes) else str(c)).upper()
        for c in getattr(conn, "capabilities", ())
    }
    try:
        typ, data = conn.capability()
        if typ == "OK" and data:
            for item in data:
                if item:
                    text = item.decode("ascii", errors="replace") if isinstance(item, bytes) else str(item)
                    for token in text.split():
                        caps.add(token.upper())
    except Exception:
        pass
    return caps


def list_imap_folders(conn: imaplib.IMAP4) -> list[dict[str, str]]:
    typ, data = conn.list()
    if typ != "OK" or not data:
        return []
    folders: list[dict[str, str]] = []
    pattern = re.compile(r'^\((?P<flags>[^)]*)\)\s+"?(?P<delim>[^"]+)"?\s+(?P<name>.+)$')
    for item in data:
        if not item:
            continue
        line = item.decode("utf-8", errors="replace") if isinstance(item, bytes) else str(item)
        m = pattern.match(line)
        if not m:
            continue
        flags = m.group("flags")
        raw_name = m.group("name").strip()
        if raw_name.startswith('"') and raw_name.endswith('"'):
            raw_name = raw_name[1:-1]
        folders.append(
            {
                "raw": raw_name,
                "name": decode_imap_utf7(raw_name),
                "flags": flags,
            }
        )
    return folders


def quote_folder(folder: str) -> str:
    """Quote an IMAP mailbox name if needed for imaplib commands."""
    f = folder.strip()
    if f.startswith('"') and f.endswith('"'):
        return f
    if any(ch in f for ch in (' ', '"', '\\', '(', ')', '[', ']', '{', '}', '&')):
        escaped = f.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return f


def resolve_folder_name(
    conn: imaplib.IMAP4,
    requested_folder: str,
    folders_cache: list[dict[str, str]] | None = None,
    must_exist: bool = True,
) -> tuple[str, str]:
    """
    Resolve a user-supplied folder name (decoded UTF-8 or raw modified UTF-7)
    into (raw_imap_name, decoded_display_name).
    """
    req = requested_folder.strip()
    if req.upper() == "INBOX":
        return "INBOX", "INBOX"

    folders = folders_cache if folders_cache is not None else list_imap_folders(conn)
    # 1. Exact match on raw or decoded name
    for f in folders:
        if req == f["raw"] or req == f["name"]:
            return f["raw"], f["name"]
    # 2. Case-insensitive match on raw or decoded name
    req_lower = req.lower()
    for f in folders:
        if req_lower == f["raw"].lower() or req_lower == f["name"].lower():
            return f["raw"], f["name"]

    if must_exist:
        available = ", ".join(f["name"] for f in folders)
        raise RuntimeError(
            f"IMAP-Ordner '{requested_folder}' existiert nicht. Verfügbare Ordner: {available}"
        )

    raw_encoded = encode_imap_utf7(req)
    return raw_encoded, decode_imap_utf7(raw_encoded)


def detect_trash_folder(conn: imaplib.IMAP4, acc: dict[str, Any]) -> tuple[str, str]:
    """Detect the Trash / Papierkorb folder via RFC 6154 \\Trash flag or common names."""
    folders = list_imap_folders(conn)
    if acc.get("trash_folder"):
        return resolve_folder_name(conn, acc["trash_folder"], folders_cache=folders, must_exist=True)

    # 1. RFC 6154 Special-Use \Trash flag
    for f in folders:
        if "\\trash" in f["flags"].lower():
            return f["raw"], f["name"]

    # 2. Well-known Trash names
    trash_candidates = (
        "[gmail]/papierkorb",
        "[gmail]/trash",
        "[google mail]/papierkorb",
        "[google mail]/trash",
        "trash",
        "papierkorb",
        "deleted items",
        "deleted messages",
        "gelöschte elemente",
        "geloeschte elemente",
        "inbox.trash",
        "inbox/trash",
    )
    for cand in trash_candidates:
        for f in folders:
            if f["name"].lower() == cand or f["raw"].lower() == cand:
                return f["raw"], f["name"]

    available = ", ".join(f"{f['name']} ({f['flags']})" for f in folders)
    raise RuntimeError(
        f"Konnte keinen Papierkorb-Ordner (\\Trash) automatisch erkennen. "
        f"Bitte gib ihn explizit mit --trash-folder an. Verfügbare Ordner: {available}"
    )


def parse_imap_flags_and_labels(meta_text: str) -> tuple[list[str], list[str]]:
    """Extract FLAGS and X-GM-LABELS lists from an IMAP FETCH response line."""
    flags: list[str] = []
    labels: list[str] = []

    m_flags = re.search(r"FLAGS\s*\(([^)]*)\)", meta_text, flags=re.IGNORECASE)
    if m_flags:
        raw_flags = m_flags.group(1).strip()
        if raw_flags:
            flags = [tok.strip() for tok in raw_flags.split() if tok.strip()]

    m_labels = re.search(r"X-GM-LABELS\s*\(([^)]*)\)", meta_text, flags=re.IGNORECASE)
    if m_labels:
        raw_labels = m_labels.group(1).strip()
        if raw_labels:
            try:
                lex = shlex.shlex(raw_labels, posix=True)
                lex.whitespace_split = True
                tokens = list(lex)
            except Exception:
                tokens = raw_labels.split()
            labels = [decode_imap_utf7(t) for t in tokens if t]

    return flags, labels


def resolve_brain_dir(explicit_root: str | None = None, secrets_path: Path | None = None) -> Path:
    """Locate <ROOT>/brain directory for state.json and inbox/ provenance stubs."""
    if explicit_root:
        rp = Path(explicit_root).expanduser().resolve()
        if (rp / "_atlas").exists() or rp.name == "brain":
            return rp
        return rp / "brain"
    sec = secrets_path or SECRETS_PATH
    if sec.parent.name == "_secrets":
        return sec.parent.parent
    root_env = os.environ.get("ATLAS_ROOT")
    if root_env:
        rp = Path(root_env).expanduser().resolve()
        return rp if (rp / "_atlas").exists() else (rp / "brain")
    default_root = Path(r"C:\Atlas") if os.name == "nt" else (Path.home() / "Atlas")
    return default_root / "brain"


def parse_ics_events(ics_bytes: bytes) -> list[dict[str, str]]:
    """Parse RFC 5545 VEVENT summaries (SUMMARY, DTSTART, DTEND, LOCATION, UID) in 0 tokens."""
    text = ics_bytes.decode("utf-8", errors="replace")
    unfolded = re.sub(r"\r?\n[ \t]", "", text)
    events: list[dict[str, str]] = []
    cur: dict[str, str] | None = None
    for raw_line in unfolded.splitlines():
        line = raw_line.strip()
        up = line.upper()
        if up == "BEGIN:VEVENT":
            cur = {}
            continue
        if up == "END:VEVENT":
            if cur:
                events.append(cur)
            cur = None
            continue
        if cur is None or ":" not in line:
            continue
        key_part, val = line.split(":", 1)
        prop = key_part.split(";", 1)[0].upper().strip()
        if prop in ("SUMMARY", "DTSTART", "DTEND", "LOCATION", "UID"):
            clean_v = val.replace("\\n", " ").replace("\\,", ",").replace("\\;", ";").strip()
            cur[prop.lower()] = clean_v
    return events


def extract_body_and_attachments(
    msg: email.message.Message,
    save_dir: Path | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    plain_parts: list[str] = []
    html_parts: list[str] = []
    attachments: list[dict[str, Any]] = []

    if msg.is_multipart():
        for part in msg.walk():
            if part.is_multipart():
                continue
            ctype = part.get_content_type().lower()
            cdisp = str(part.get("Content-Disposition") or "")
            filename = part.get_filename()
            if filename:
                filename = decode_mime_header(filename)

            is_inline_cal = ctype in ("text/calendar", "application/ics")
            if filename or "attachment" in cdisp.lower() or is_inline_cal:
                payload = part.get_payload(decode=True) or b""
                eff_name = filename or ("invite.ics" if is_inline_cal else "unnamed_attachment")
                att_info: dict[str, Any] = {
                    "filename": eff_name,
                    "content_type": ctype,
                    "size_bytes": len(payload),
                }
                if save_dir and payload:
                    save_dir.mkdir(parents=True, exist_ok=True)
                    safe_name = re.sub(r"[^A-Za-z0-9._ -]", "_", eff_name)
                    target = save_dir / safe_name
                    target.write_bytes(payload)
                    att_info["saved_to"] = str(target)
                if is_inline_cal or eff_name.lower().endswith((".ics", ".vcs")):
                    att_info["ics_events"] = parse_ics_events(payload)
                attachments.append(att_info)
                continue

            if ctype == "text/plain":
                payload = part.get_payload(decode=True)
                if payload is not None:
                    charset = part.get_content_charset() or "utf-8"
                    try:
                        plain_parts.append(payload.decode(charset, errors="replace"))
                    except LookupError:
                        plain_parts.append(payload.decode("utf-8", errors="replace"))
            elif ctype == "text/html":
                payload = part.get_payload(decode=True)
                if payload is not None:
                    charset = part.get_content_charset() or "utf-8"
                    try:
                        html_parts.append(payload.decode(charset, errors="replace"))
                    except LookupError:
                        html_parts.append(payload.decode("utf-8", errors="replace"))
    else:
        ctype_single = msg.get_content_type().lower()
        payload = msg.get_payload(decode=True)
        if payload is not None:
            if ctype_single in ("text/calendar", "application/ics"):
                eff_name = decode_mime_header(msg.get_filename()) or "invite.ics"
                att_info = {
                    "filename": eff_name,
                    "content_type": ctype_single,
                    "size_bytes": len(payload),
                    "ics_events": parse_ics_events(payload),
                }
                if save_dir and payload:
                    save_dir.mkdir(parents=True, exist_ok=True)
                    safe_name = re.sub(r"[^A-Za-z0-9._ -]", "_", eff_name)
                    target = save_dir / safe_name
                    target.write_bytes(payload)
                    att_info["saved_to"] = str(target)
                attachments.append(att_info)
            else:
                charset = msg.get_content_charset() or "utf-8"
                try:
                    text = payload.decode(charset, errors="replace")
                except LookupError:
                    text = payload.decode("utf-8", errors="replace")
                if ctype_single == "text/html":
                    html_parts.append(text)
                else:
                    plain_parts.append(text)

    body = "\n".join(plain_parts).strip()
    if not body and html_parts:
        body = html_to_text("\n".join(html_parts))
    return body, attachments


def parse_message_bytes(
    uid: str,
    folder: str,
    raw_bytes: bytes,
    save_dir: Path | None = None,
    flags: list[str] | None = None,
    labels: list[str] | None = None,
) -> dict[str, Any]:
    msg = email.message_from_bytes(raw_bytes)
    subject = decode_mime_header(msg.get("Subject"))
    from_hdr = decode_mime_header(msg.get("From"))
    to_hdr = decode_mime_header(msg.get("To"))
    cc_hdr = decode_mime_header(msg.get("Cc"))
    date_hdr = decode_mime_header(msg.get("Date"))
    msg_id = decode_mime_header(msg.get("Message-ID"))

    parsed_dt: dt.datetime | None = None
    if date_hdr:
        try:
            parsed_dt = email.utils.parsedate_to_datetime(date_hdr)
        except Exception:
            parsed_dt = None

    iso_date = parsed_dt.isoformat() if parsed_dt else date_hdr
    body, attachments = extract_body_and_attachments(msg, save_dir=save_dir)
    snippet = re.sub(r"\s+", " ", body)[:280]

    return {
        "uid": uid,
        "folder": decode_imap_utf7(folder),
        "message_id": msg_id,
        "date": iso_date,
        "from": from_hdr,
        "to": to_hdr,
        "cc": cc_hdr,
        "subject": subject,
        "flags": flags if flags is not None else [],
        "labels": labels if labels is not None else [],
        "attachments": attachments,
        "snippet": snippet,
        "body": body,
    }


def _extract_fetch_parts(msg_data: list[Any]) -> tuple[bytes | None, list[str], list[str]]:
    """Extract raw message bytes, FLAGS, and X-GM-LABELS from imaplib fetch response."""
    raw_bytes: bytes | None = None
    meta_chunks: list[str] = []
    for item in msg_data:
        if isinstance(item, tuple):
            if len(item) >= 1 and isinstance(item[0], bytes):
                meta_chunks.append(item[0].decode("utf-8", errors="replace"))
            if len(item) >= 2 and isinstance(item[1], bytes):
                raw_bytes = item[1]
        elif isinstance(item, bytes):
            meta_chunks.append(item.decode("utf-8", errors="replace"))
    flags, labels = parse_imap_flags_and_labels(" ".join(meta_chunks))
    return raw_bytes, flags, labels


HEADER_PEEK_FIELDS = "FROM TO CC SUBJECT DATE MESSAGE-ID CONTENT-TYPE"


def _format_imap_since(since_iso: str) -> str:
    d = dt.date.fromisoformat(since_iso[:10])
    months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    return f"{d.day:02d}-{months[d.month - 1]}-{d.year}"


def _get_mailbox_uidvalidity(conn: imaplib.IMAP4, raw_folder: str) -> str:
    """Return current mailbox UIDVALIDITY string (RFC 3501) after SELECT/EXAMINE."""
    try:
        _code, uv_list = conn.response("UIDVALIDITY")
        if uv_list and uv_list[-1]:
            val = uv_list[-1].decode("ascii", errors="replace") if isinstance(uv_list[-1], bytes) else str(uv_list[-1])
            m = re.search(r"(\d+)", val)
            if m:
                return m.group(1)
    except Exception:
        pass
    try:
        typ, st_data = conn.status(quote_folder(raw_folder), "(UIDVALIDITY)")
        if typ == "OK" and st_data and st_data[0]:
            s = st_data[0].decode("ascii", errors="replace") if isinstance(st_data[0], bytes) else str(st_data[0])
            m = re.search(r"UIDVALIDITY\s+(\d+)", s, re.I)
            if m:
                return m.group(1)
    except Exception:
        pass
    return "1"


def fetch_messages_in_folder(
    conn: imaplib.IMAP4,
    folder: str,
    limit: int = 50,
    since: str | None = None,
    unread_only: bool = False,
    flagged_only: bool = False,
    caps: set[str] | None = None,
    headers_only: bool = False,
) -> list[dict[str, Any]]:
    raw_folder, _ = resolve_folder_name(conn, folder, must_exist=False)
    typ, _ = conn.select(quote_folder(raw_folder), readonly=True)
    if typ != "OK":
        return []

    if caps is None:
        caps = get_server_capabilities(conn)
    has_gm_labels = "X-GM-EXT-1" in caps

    criteria: list[str] = []
    if unread_only:
        criteria.append("UNSEEN")
    if flagged_only:
        criteria.append("FLAGGED")
    if since:
        criteria.append(f"SINCE {_format_imap_since(since)}")
    search_str = "(" + " ".join(criteria) + ")" if criteria else "ALL"

    typ, data = conn.uid("search", None, search_str)
    if typ != "OK" or not data or not data[0]:
        return []

    uids = [u.decode("ascii") for u in data[0].split()]
    if limit > 0:
        uids = uids[-limit:]

    if headers_only:
        body_spec = f"BODY.PEEK[HEADER.FIELDS ({HEADER_PEEK_FIELDS})]"
    else:
        body_spec = "BODY.PEEK[]"
    fetch_spec = f"(FLAGS X-GM-LABELS {body_spec})" if has_gm_labels else f"(FLAGS {body_spec})"

    results: list[dict[str, Any]] = []
    # Fetch in reverse chronological order (newest UID first)
    for uid in reversed(uids):
        typ, msg_data = conn.uid("fetch", uid, fetch_spec)
        if typ != "OK" or not msg_data:
            continue
        raw_bytes, flags, labels = _extract_fetch_parts(msg_data)
        if raw_bytes is not None:
            parsed = parse_message_bytes(uid, raw_folder, raw_bytes, flags=flags, labels=labels)
            if headers_only:
                msg_hdr = email.message_from_bytes(raw_bytes)
                parsed["content_type"] = msg_hdr.get_content_type().lower()
            results.append(parsed)
    return results


def fetch_uid_previews(
    conn: imaplib.IMAP4,
    raw_folder: str,
    uids: list[str],
    caps: set[str] | None = None,
) -> list[dict[str, Any]]:
    """
    Fetch lightweight headers + FLAGS + X-GM-LABELS in readonly=True mode
    to display a safe Pre-Flight Preview before any write operation.
    """
    typ, _ = conn.select(quote_folder(raw_folder), readonly=True)
    if typ != "OK":
        raise RuntimeError(f"Ordner '{decode_imap_utf7(raw_folder)}' konnte nicht im Lesemodus geöffnet werden.")

    if caps is None:
        caps = get_server_capabilities(conn)
    has_gm_labels = "X-GM-EXT-1" in caps
    fetch_spec = (
        "(FLAGS X-GM-LABELS BODY.PEEK[HEADER.FIELDS (SUBJECT FROM TO DATE MESSAGE-ID)])"
        if has_gm_labels
        else "(FLAGS BODY.PEEK[HEADER.FIELDS (SUBJECT FROM TO DATE MESSAGE-ID)])"
    )

    previews: list[dict[str, Any]] = []
    for uid in uids:
        typ, msg_data = conn.uid("fetch", str(uid), fetch_spec)
        if typ != "OK" or not msg_data or msg_data == [None]:
            raise RuntimeError(
                f"Nachricht mit UID {uid} wurde im Ordner '{decode_imap_utf7(raw_folder)}' nicht gefunden."
            )
        raw_bytes, flags, labels = _extract_fetch_parts(msg_data)
        if raw_bytes is None:
            raise RuntimeError(
                f"Nachricht mit UID {uid} existiert im Ordner '{decode_imap_utf7(raw_folder)}' nicht."
            )
        msg = email.message_from_bytes(raw_bytes)
        subject = decode_mime_header(msg.get("Subject")) or "(Kein Betreff)"
        from_hdr = decode_mime_header(msg.get("From")) or "(Unbekannter Absender)"
        to_hdr = decode_mime_header(msg.get("To"))
        date_hdr = decode_mime_header(msg.get("Date"))
        msg_id = decode_mime_header(msg.get("Message-ID"))
        previews.append(
            {
                "uid": str(uid),
                "folder": decode_imap_utf7(raw_folder),
                "date": date_hdr,
                "from": from_hdr,
                "to": to_hdr,
                "subject": subject,
                "message_id": msg_id,
                "flags": flags,
                "labels": labels,
            }
        )
    return previews


def normalize_uids(raw_uids: list[str]) -> list[str]:
    """Parse space- or comma-separated UID arguments and validate they are positive integers."""
    result: list[str] = []
    for token in raw_uids:
        for part in re.split(r"[,\s]+", str(token).strip()):
            if not part:
                continue
            if not part.isdigit() or int(part) <= 0:
                raise ValueError(f"Ungültige Nachrichten-UID: '{part}' (muss eine positive Zahl sein).")
            if part not in result:
                result.append(part)
    if not result:
        raise ValueError("Mindestens eine gültige UID muss angegeben werden.")
    return result


def normalize_flag_list(raw_items: list[str] | str | None) -> list[str]:
    """Normalize user-specified IMAP flags (resolving aliases like 'seen' -> '\\Seen')."""
    if not raw_items:
        return []
    items = [raw_items] if isinstance(raw_items, str) else list(raw_items)
    flags: list[str] = []
    for item in items:
        for part in re.split(r"[,\s]+", item.strip()):
            if not part:
                continue
            low = part.lower().lstrip("\\")
            if low in STANDARD_FLAG_ALIASES:
                canon = STANDARD_FLAG_ALIASES[low]
            else:
                canon = part
            if canon not in flags:
                flags.append(canon)
    return flags


def normalize_label_list(raw_items: list[str] | str | None) -> list[str]:
    """Normalize user-specified Gmail/IMAP labels."""
    if not raw_items:
        return []
    items = [raw_items] if isinstance(raw_items, str) else list(raw_items)
    labels: list[str] = []
    for item in items:
        for part in item.split(","):
            clean = part.strip()
            if clean and clean not in labels:
                labels.append(clean)
    return labels


def format_imap_label_list(labels: list[str]) -> str:
    """Format a list of labels into an IMAP parenthesized list with modified UTF-7 encoding."""
    encoded_parts: list[str] = []
    for lbl in labels:
        enc = encode_imap_utf7(lbl)
        if any(ch in enc for ch in (' ', '"', '\\', '(', ')')):
            escaped = enc.replace("\\", "\\\\").replace('"', '\\"')
            encoded_parts.append(f'"{escaped}"')
        else:
            encoded_parts.append(enc)
    return "(" + " ".join(encoded_parts) + ")"


def enforce_write_safety_gate(
    acc: dict[str, Any],
    action_title: str,
    action_details: str,
    previews: list[dict[str, Any]],
    dry_run: bool = False,
    assume_yes: bool = False,
    json_mode: bool = False,
    interactive: bool = False,
) -> bool:
    """
    Multi-stage Safety Gate for all write operations:
    1. Checks account-level write permission (MAIL_<ID>_ALLOW_WRITE).
    2. Prints a detailed Pre-Flight Preview of all affected messages.
    3. Stops if --dry-run is active.
    4. Requires explicit confirmation (--yes flag after user approval in chat, or -i/--interactive prompt).
    Returns True if execution is approved, False otherwise.
    """
    if not acc.get("allow_write", True):
        raise RuntimeError(
            f"SAFETY GATE BLOCK: Schreibzugriffe sind für Konto [{acc['id']}] ({acc.get('user')}) "
            f"per Konfiguration (MAIL_{acc['id'].upper()}_ALLOW_WRITE=false) gesperrt."
        )

    if not json_mode:
        print("=" * 78)
        print(f"🛡️  ATLAS MAIL SAFETY GATE — {action_title}")
        print("=" * 78)
        print(f"Konto:   [{acc['id']}] {acc.get('label')} ({acc.get('user')})")
        print(f"Aktion:  {action_details}")
        if previews:
            print(f"Betroffene E-Mails ({len(previews)}):")
            for p in previews:
                flags_str = f" | FLAGS: {' '.join(p['flags'])}" if p.get("flags") else ""
                labels_str = f" | LABELS: {', '.join(p['labels'])}" if p.get("labels") else ""
                print(f"  • UID {p['uid']} ({p['folder']}) | {p['date']}")
                print(f"    Von:     {p['from']}")
                print(f"    Betreff: {p['subject']}{flags_str}{labels_str}")
        print("=" * 78)

    if dry_run:
        if json_mode:
            print(
                json.dumps(
                    {
                        "status": "DRY_RUN",
                        "account": acc["id"],
                        "action": action_title,
                        "details": action_details,
                        "affected_messages": previews,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            print("ℹ️  [DRY-RUN] Vorschau beendet. Es wurden KEINE Änderungen am Postfach vorgenommen.")
        return False

    if assume_yes:
        return True

    if interactive and sys.stdin.isatty() and not json_mode:
        try:
            ans = input("❓ Soll diese Schreiboperation jetzt ausgeführt werden? [y/N]: ").strip().lower()
        except EOFError:
            ans = ""
        if ans in ("y", "yes", "j", "ja"):
            return True
        print("🚫 Abgebrochen durch Benutzer. Keine Änderungen vorgenommen.")
        return False

    msg = (
        "SAFETY GATE: Schreiboperation wurde noch NICHT ausgeführt (Vorschau-Modus). "
        "Zeige dem Nutzer zuerst die oben gelisteten E-Mails (UID, Betreff, Absender, target_folder/Flags) "
        "und führe den Befehl erst nach seiner expliziten Bestätigung mit '--yes' aus."
    )
    if json_mode:
        print(
            json.dumps(
                {
                    "status": "CONFIRMATION_REQUIRED",
                    "message": msg,
                    "account": acc["id"],
                    "action": action_title,
                    "details": action_details,
                    "affected_messages": previews,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(f"⚠️  {msg}", file=sys.stderr)
    return False


def execute_imap_move(
    conn: imaplib.IMAP4,
    raw_source_folder: str,
    raw_target_folder: str,
    uids: list[str],
    caps: set[str] | None = None,
) -> dict[str, Any]:
    """
    Execute an atomic IMAP MOVE (RFC 6851) if supported by the server,
    or safely fall back to COPY + STORE +FLAGS (\\Deleted) + UID EXPUNGE (RFC 4315 / RFC 3501).
    """
    if caps is None:
        caps = get_server_capabilities(conn)

    typ, _ = conn.select(quote_folder(raw_source_folder), readonly=False)
    if typ != "OK":
        raise RuntimeError(
            f"Quellordner '{decode_imap_utf7(raw_source_folder)}' konnte nicht im Schreibmodus geöffnet werden."
        )

    uid_set = ",".join(uids)
    quoted_target = quote_folder(raw_target_folder)
    method_used = "RFC6851_MOVE"

    if "MOVE" in caps:
        typ, data = conn.uid("MOVE", uid_set, quoted_target)
        if typ == "OK":
            return {
                "method": method_used,
                "uids": uids,
                "source_folder": decode_imap_utf7(raw_source_folder),
                "target_folder": decode_imap_utf7(raw_target_folder),
                "raw_target_folder": raw_target_folder,
            }

    # Fallback: RFC 3501 COPY + STORE (\Deleted) + EXPUNGE
    method_used = "FALLBACK_COPY_STORE_EXPUNGE"
    typ, copy_data = conn.uid("COPY", uid_set, quoted_target)
    if typ != "OK":
        raise RuntimeError(
            f"IMAP COPY nach '{decode_imap_utf7(raw_target_folder)}' fehlgeschlagen: {copy_data}"
        )

    typ, store_data = conn.uid("STORE", uid_set, "+FLAGS.SILENT", "(\\Deleted)")
    if typ != "OK":
        raise RuntimeError(
            f"IMAP STORE +FLAGS (\\Deleted) nach erfolgreichem COPY fehlgeschlagen: {store_data}"
        )

    if "UIDPLUS" in caps:
        conn.uid("EXPUNGE", uid_set)
        method_used = "FALLBACK_COPY_STORE_UID_EXPUNGE"
    else:
        conn.expunge()

    return {
        "method": method_used,
        "uids": uids,
        "source_folder": decode_imap_utf7(raw_source_folder),
        "target_folder": decode_imap_utf7(raw_target_folder),
        "raw_target_folder": raw_target_folder,
    }


def resolve_account(accounts: dict[str, dict[str, Any]], requested: str | None) -> dict[str, Any]:
    if not accounts:
        raise RuntimeError(
            f"Keine Accounts in {SECRETS_PATH} gefunden. Führe 'atlas_mail.py init' aus, um die Vorlage anzulegen."
        )
    if requested:
        key = requested.lower()
        if key in accounts:
            return accounts[key]
        for acc in accounts.values():
            if acc.get("user", "").lower() == key:
                return acc
        raise RuntimeError(f"Account '{requested}' nicht gefunden. Verfügbar: {', '.join(accounts.keys())}")
    for acc in accounts.values():
        if acc.get("configured"):
            return acc
    return next(iter(accounts.values()))


def cmd_init(args: argparse.Namespace) -> int:
    """Create or extend <ROOT>/brain/_secrets/mail.env with chmod 600."""
    SECRETS_PATH.parent.mkdir(parents=True, exist_ok=True)
    acc_id = re.sub(r"[^A-Z0-9_]", "_", (args.account or "MAIN").upper()).strip("_") or "MAIN"
    block = (
        f"\nMAIL_{acc_id}_LABEL=\"{args.label or acc_id.title()}\"\n"
        f"MAIL_{acc_id}_HOST=\"{args.host or ''}\"\n"
        f"MAIL_{acc_id}_PORT=\"{args.port or 993}\"\n"
        f"MAIL_{acc_id}_USER=\"{args.user or 'DEIN_LOGIN@example.com'}\"\n"
        f"MAIL_{acc_id}_PASSWORD=\"{args.password or 'DEIN_APP_PASSWORT'}\"\n"
        f"MAIL_{acc_id}_ALLOW_WRITE=\"true\"\n"
    )
    if not SECRETS_PATH.exists():
        SECRETS_PATH.write_text(MAIL_ENV_TEMPLATE + block, encoding="utf-8")
        try:
            os.chmod(SECRETS_PATH, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
        print(f"✅ {SECRETS_PATH} angelegt (chmod 600) mit Account-Block MAIL_{acc_id}_*.")
        return 0

    existing = SECRETS_PATH.read_text(encoding="utf-8")
    if f"MAIL_{acc_id}_USER" in existing and not getattr(args, "force", False):
        print(f"ℹ️  {SECRETS_PATH} enthält bereits MAIL_{acc_id}_*. Nutze --account <NEUE_ID> für ein weiteres Konto.")
        return 0

    SECRETS_PATH.write_text(existing.rstrip() + "\n" + block, encoding="utf-8")
    try:
        os.chmod(SECRETS_PATH, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    print(f"✅ Account-Block MAIL_{acc_id}_* zu {SECRETS_PATH} hinzugefügt.")
    return 0


def cmd_accounts(args: argparse.Namespace) -> int:
    accounts = load_accounts()
    if not accounts:
        print(f"Keine Accounts in {SECRETS_PATH} definiert. Nutze 'init' zum Anlegen einer Vorlage.")
        return 1
    for acc_id, acc in accounts.items():
        status = "NOT_CONFIGURED"
        inbox_count = "-"
        feature_str = "-"
        if acc.get("configured"):
            try:
                conn = connect_imap(acc)
                caps = get_server_capabilities(conn)
                features = [f for f in ("MOVE", "UIDPLUS", "X-GM-EXT-1") if f in caps]
                feature_str = ",".join(features) if features else "IMAP4rev1"
                typ, data = conn.select("INBOX", readonly=True)
                if typ == "OK" and data and data[0]:
                    inbox_count = data[0].decode("ascii", errors="replace")
                status = "OK"
                conn.logout()
            except Exception as e:
                status = f"ERROR: {e}"
        write_mode = "WRITE_ENABLED (gated)" if acc.get("allow_write", True) else "STRICT_READONLY"
        print(
            f"[{acc_id}] {acc.get('label')} | {acc.get('user')} | {acc.get('host')}:{acc.get('port')} | "
            f"Status: {status} | INBOX: {inbox_count} | IMAP: {feature_str} | Mode: {write_mode}"
        )
    return 0


def cmd_folders(args: argparse.Namespace) -> int:
    accounts = load_accounts()
    acc = resolve_account(accounts, args.account)
    conn = connect_imap(acc)
    try:
        folders = list_imap_folders(conn)
        if getattr(args, "json", False):
            print(json.dumps(folders, ensure_ascii=False, indent=2))
            return 0
        for f in folders:
            count = "-"
            if "\\Noselect" not in f["flags"]:
                typ, data = conn.select(quote_folder(f["raw"]), readonly=True)
                if typ == "OK" and data and data[0]:
                    count = data[0].decode("ascii", errors="replace")
            print(f"{f['name']:<40} (raw: {f['raw']:<35}) | msgs: {count:<6} | flags: {f['flags']}")
    finally:
        conn.logout()
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    accounts = load_accounts()
    acc = resolve_account(accounts, args.account)
    conn = connect_imap(acc)
    try:
        msgs = fetch_messages_in_folder(
            conn,
            folder=args.folder,
            limit=args.limit,
            since=args.since,
            unread_only=args.unread,
            flagged_only=getattr(args, "flagged", False),
            headers_only=not getattr(args, "with_body", False),
        )
        if args.json:
            for m in msgs:
                m.pop("body", None)
            print(json.dumps(msgs, ensure_ascii=False, indent=2))
            return 0
        print(f"=== Account: [{acc['id']}] {acc['user']} | Folder: {args.folder} ({len(msgs)} Treffer) ===")
        for m in msgs:
            att_str = (
                f" [📎 {len(m['attachments'])}: {', '.join(a['filename'] for a in m['attachments'])}]"
                if m["attachments"]
                else (" [📎 multipart]" if str(m.get("content_type", "")).startswith("multipart/mixed") else "")
            )
            flag_badges: list[str] = []
            if "\\Seen" not in m.get("flags", []):
                flag_badges.append("UNSEEN")
            if "\\Flagged" in m.get("flags", []):
                flag_badges.append("★")
            if "\\Answered" in m.get("flags", []):
                flag_badges.append("↩")
            badge_str = f" [{' '.join(flag_badges)}]" if flag_badges else ""
            lbl_str = f" [🏷️ {', '.join(m['labels'])}]" if m.get("labels") else ""
            print(f"- UID {m['uid']}{badge_str} | {m['date']} | Von: {m['from']} -> An: {m['to']}")
            print(f"  Betreff: {m['subject']}{att_str}{lbl_str}")
            if m["snippet"]:
                print(f"  Auszug:  {m['snippet']}")
    finally:
        conn.logout()
    return 0


def cmd_read(args: argparse.Namespace) -> int:
    accounts = load_accounts()
    acc = resolve_account(accounts, args.account)
    conn = connect_imap(acc)
    try:
        caps = get_server_capabilities(conn)
        raw_folder, disp_folder = resolve_folder_name(conn, args.folder, must_exist=True)
        typ, _ = conn.select(quote_folder(raw_folder), readonly=True)
        if typ != "OK":
            raise RuntimeError(f"Ordner '{disp_folder}' konnte nicht geöffnet werden.")
        fetch_spec = "(FLAGS X-GM-LABELS BODY.PEEK[])" if "X-GM-EXT-1" in caps else "(FLAGS BODY.PEEK[])"
        typ, msg_data = conn.uid("fetch", str(args.uid), fetch_spec)
        if typ != "OK" or not msg_data:
            raise RuntimeError(f"Nachricht UID {args.uid} nicht gefunden.")
        save_dir = Path(args.save_attachments) if args.save_attachments else None
        raw_bytes, flags, labels = _extract_fetch_parts(msg_data)
        if raw_bytes is not None:
            parsed = parse_message_bytes(
                str(args.uid),
                raw_folder,
                raw_bytes,
                save_dir=save_dir,
                flags=flags,
                labels=labels,
            )
            if args.json:
                print(json.dumps(parsed, ensure_ascii=False, indent=2))
            else:
                print(f"UID:        {parsed['uid']} ({parsed['folder']})")
                print(f"Message-ID: {parsed['message_id']}")
                print(f"Datum:      {parsed['date']}")
                print(f"Von:        {parsed['from']}")
                print(f"An:         {parsed['to']}")
                if parsed["cc"]:
                    print(f"Cc:         {parsed['cc']}")
                print(f"Betreff:    {parsed['subject']}")
                print(f"FLAGS:      {' '.join(parsed['flags']) if parsed['flags'] else '(keine)'}")
                if parsed["labels"]:
                    print(f"LABELS:     {', '.join(parsed['labels'])}")
                if parsed["attachments"]:
                    print("Anhänge:")
                    for a in parsed["attachments"]:
                        saved_note = f" -> gespeichert unter {a['saved_to']}" if a.get("saved_to") else ""
                        print(f"  - {a['filename']} ({a['content_type']}, {a['size_bytes']} Bytes){saved_note}")
                print("-" * 72)
                print(parsed["body"])
            return 0
        raise RuntimeError(f"Keine RFC822-Daten für UID {args.uid} erhalten.")
    finally:
        conn.logout()


def _build_server_uid_search_criteria(query: str, since: str | None = None) -> str | None:
    """Build safe RFC 3501 ASCII UID SEARCH criteria, or return None if client-side fallback is safer."""
    q_clean = query.strip()
    if not q_clean:
        return None
    # Server-side un-charsetted IMAP UID SEARCH is reliable for printable ASCII without control chars
    if any(ord(ch) < 0x20 or ord(ch) > 0x7E for ch in q_clean):
        return None
    esc = q_clean.replace("\\", "\\\\").replace('"', '\\"')
    or_expr = f'OR OR SUBJECT "{esc}" FROM "{esc}" OR TO "{esc}" TEXT "{esc}"'
    if since:
        return f"(SINCE {_format_imap_since(since)} {or_expr})"
    return f"({or_expr})"


def _message_matches_query(m: dict[str, Any], q_low: str) -> bool:
    return (
        q_low in m["subject"].lower()
        or q_low in m["from"].lower()
        or q_low in m["to"].lower()
        or q_low in m["body"].lower()
        or any(q_low in a["filename"].lower() for a in m["attachments"])
        or any(q_low in lbl.lower() for lbl in m.get("labels", []))
    )


def cmd_search(args: argparse.Namespace) -> int:
    accounts = load_accounts()
    acc = resolve_account(accounts, args.account)
    conn = connect_imap(acc)
    q = args.query.lower()
    since_arg = getattr(args, "since", None)
    try:
        caps = get_server_capabilities(conn)
        has_gm_labels = "X-GM-EXT-1" in caps
        raw_folder, _ = resolve_folder_name(conn, args.folder, must_exist=False)

        msgs: list[dict[str, Any]] | None = None
        server_crit = _build_server_uid_search_criteria(args.query, since=since_arg)
        if server_crit is not None:
            try:
                typ_sel, _ = conn.select(quote_folder(raw_folder), readonly=True)
                if typ_sel == "OK":
                    typ_s, s_data = conn.uid("search", None, server_crit)
                    if typ_s == "OK" and s_data is not None:
                        cand_uids = [u.decode("ascii") for u in (s_data[0] or b"").split()] if s_data else []
                        if args.scan_limit > 0:
                            cand_uids = cand_uids[-args.scan_limit:]
                        fetch_spec = "(FLAGS X-GM-LABELS BODY.PEEK[])" if has_gm_labels else "(FLAGS BODY.PEEK[])"
                        fetched: list[dict[str, Any]] = []
                        for uid in reversed(cand_uids):
                            t_f, m_data = conn.uid("fetch", uid, fetch_spec)
                            if t_f != "OK" or not m_data:
                                continue
                            raw_b, flgs, lbls = _extract_fetch_parts(m_data)
                            if raw_b is not None:
                                fetched.append(parse_message_bytes(uid, raw_folder, raw_b, flags=flgs, labels=lbls))
                        # If UID SEARCH returned 0 hits, check if user might be matching a label or attachment filename
                        # on a small mailbox; only keep empty result directly unless X-GM-LABELS / filename fallback applies.
                        msgs = fetched
            except Exception:
                msgs = None

        if msgs is None:
            msgs = fetch_messages_in_folder(
                conn, folder=args.folder, limit=args.scan_limit, since=since_arg, caps=caps, headers_only=False
            )

        matched = [m for m in msgs if _message_matches_query(m, q)]
        # Fallback if server-side TEXT/SUBJECT/FROM/TO missed an attachment filename or Gmail label
        if not matched and server_crit is not None and (has_gm_labels or "." in q):
            fallback_msgs = fetch_messages_in_folder(
                conn, folder=args.folder, limit=min(args.scan_limit, 100), since=since_arg, caps=caps, headers_only=False
            )
            matched = [m for m in fallback_msgs if _message_matches_query(m, q)]

        if args.limit > 0:
            matched = matched[: args.limit]
        if args.json:
            print(json.dumps(matched, ensure_ascii=False, indent=2))
            return 0
        print(f"=== Suche '{args.query}' in [{acc['id']}]/{args.folder}: {len(matched)} Treffer ===")
        for m in matched:
            att_str = f" [📎 {', '.join(a['filename'] for a in m['attachments'])}]" if m["attachments"] else ""
            lbl_str = f" [🏷️ {', '.join(m['labels'])}]" if m.get("labels") else ""
            print(f"- UID {m['uid']} | {m['date']} | Von: {m['from']} | Betreff: {m['subject']}{att_str}{lbl_str}")
            print(f"  Auszug: {m['snippet']}")
    finally:
        conn.logout()
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    """Scan all relevant folders (All Mail or INBOX + Sent) for complete context extraction."""
    accounts = load_accounts()
    acc = resolve_account(accounts, args.account)
    conn = connect_imap(acc)
    try:
        caps = get_server_capabilities(conn)
        folders = list_imap_folders(conn)
        target_folders: list[str] = []
        all_mail = next(
            (
                f["raw"]
                for f in folders
                if "\\All" in f["flags"]
                or "alle nachrichten" in f["name"].lower()
                or "all mail" in f["name"].lower()
            ),
            None,
        )
        if all_mail:
            target_folders.append(all_mail)
        else:
            for f in folders:
                if "\\Noselect" in f["flags"] or "\\Trash" in f["flags"] or "\\Junk" in f["flags"]:
                    continue
                fname = f["name"].lower()
                if any(t in fname for t in ("trash", "papierkorb", "spam", "junk", "draft", "entwürfe")):
                    continue
                target_folders.append(f["raw"])

        seen_msg_ids: set[str] = set()
        all_messages: list[dict[str, Any]] = []
        for raw_folder in target_folders:
            msgs = fetch_messages_in_folder(
                conn, folder=raw_folder, limit=args.limit, since=args.since, caps=caps
            )
            for m in msgs:
                key = m["message_id"] or f"{m['folder']}:{m['uid']}"
                if key in seen_msg_ids:
                    continue
                seen_msg_ids.add(key)
                all_messages.append(m)

        all_messages.sort(key=lambda x: x.get("date") or "", reverse=True)
        if args.out:
            out_path = Path(args.out)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(json.dumps(all_messages, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"{len(all_messages)} deduplizierte E-Mails nach {out_path} geschrieben.")
        elif args.json:
            print(json.dumps(all_messages, ensure_ascii=False, indent=2))
        else:
            print(
                f"=== Voll-Audit [{acc['id']}] {acc['user']} | Ordner: {', '.join(target_folders)} | "
                f"{len(all_messages)} E-Mails ==="
            )
            for m in all_messages:
                att_str = f" [📎 {', '.join(a['filename'] for a in m['attachments'])}]" if m["attachments"] else ""
                print(f"\n--- UID {m['uid']} ({m['folder']}) | {m['date']} ---")
                print(f"Von:     {m['from']}")
                print(f"An:      {m['to']}")
                print(f"Betreff: {m['subject']}{att_str}")
                print(f"Auszug:  {m['snippet']}")
    finally:
        conn.logout()
    return 0


INGEST_EXTS = (".ics", ".vcs", ".pdf")
INGEST_CTYPES = ("text/calendar", "application/ics", "application/pdf")


def _slugify_stub(text: str, max_len: int = 44) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return (s[:max_len].rstrip("-") or "message")


def _is_ingest_attachment(filename: str, ctype: str) -> bool:
    fn_low = (filename or "").lower().strip()
    ct_low = (ctype or "").lower().strip()
    return fn_low.endswith(INGEST_EXTS) or ct_low in INGEST_CTYPES


def _extract_ingest_payloads(msg: email.message.Message) -> tuple[str, list[dict[str, Any]]]:
    """Extract clean plain-text body and raw bytes for .ics / .pdf attachments."""
    plain_parts: list[str] = []
    html_parts: list[str] = []
    matched_atts: list[dict[str, Any]] = []

    parts = list(msg.walk()) if msg.is_multipart() else [msg]
    for part in parts:
        if part.is_multipart():
            continue
        ctype = part.get_content_type().lower()
        cdisp = str(part.get("Content-Disposition") or "")
        raw_fn = part.get_filename()
        filename = decode_mime_header(raw_fn) if raw_fn else ""
        is_cal = ctype in ("text/calendar", "application/ics")
        eff_name = filename or ("invite.ics" if is_cal else ("document.pdf" if ctype == "application/pdf" else ""))

        if eff_name and (_is_ingest_attachment(eff_name, ctype) or ("attachment" in cdisp.lower() and _is_ingest_attachment(eff_name, ctype))):
            payload = part.get_payload(decode=True) or b""
            if payload:
                entry: dict[str, Any] = {
                    "filename": eff_name,
                    "content_type": ctype,
                    "size_bytes": len(payload),
                    "bytes": payload,
                }
                if is_cal or eff_name.lower().endswith((".ics", ".vcs")):
                    entry["ics_events"] = parse_ics_events(payload)
                matched_atts.append(entry)
            if filename or "attachment" in cdisp.lower() or is_cal:
                continue

        if ctype == "text/plain" and "attachment" not in cdisp.lower():
            payload = part.get_payload(decode=True)
            if payload is not None:
                ch = part.get_content_charset() or "utf-8"
                try:
                    plain_parts.append(payload.decode(ch, errors="replace"))
                except LookupError:
                    plain_parts.append(payload.decode("utf-8", errors="replace"))
        elif ctype == "text/html" and "attachment" not in cdisp.lower():
            payload = part.get_payload(decode=True)
            if payload is not None:
                ch = part.get_content_charset() or "utf-8"
                try:
                    html_parts.append(payload.decode(ch, errors="replace"))
                except LookupError:
                    html_parts.append(payload.decode("utf-8", errors="replace"))

    body = "\n".join(plain_parts).strip()
    if not body and html_parts:
        body = html_to_text("\n".join(html_parts))
    return body, matched_atts


def _yaml_quote(val: str) -> str:
    clean = re.sub(r"[\r\n\t]+", " ", str(val or "")).strip()
    escaped = clean.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def cmd_ingest(args: argparse.Namespace) -> int:
    """Deterministic 0-token read-only bridge from IMAP into brain/inbox/ with UIDVALIDITY cursor."""
    accounts = load_accounts()
    if getattr(args, "all_accounts", False):
        target_accs = [a for a in accounts.values() if a.get("configured")]
        if not target_accs:
            raise RuntimeError(f"Keine konfigurierten Konten in {SECRETS_PATH} gefunden.")
    else:
        target_accs = [resolve_account(accounts, args.account)]

    brain_dir = resolve_brain_dir(explicit_root=getattr(args, "root", None), secrets_path=SECRETS_PATH)
    inbox_dir = brain_dir / "inbox"
    state_path = brain_dir / "_atlas" / "state.json"

    state_data: dict[str, Any] = {}
    if state_path.is_file():
        try:
            state_data = json.loads(state_path.read_text(encoding="utf-8"))
            if not isinstance(state_data, dict):
                state_data = {}
        except Exception:
            state_data = {}

    ingest_cursors: dict[str, Any] = dict(state_data.get("mail_ingest") or {})
    execute = bool(getattr(args, "execute", False))
    include_all = bool(getattr(args, "include_all", False))
    today_iso = dt.date.today().isoformat()
    ingested_items: list[dict[str, Any]] = []
    scanned_total = 0

    for acc in target_accs:
        conn = connect_imap(acc)
        try:
            caps = get_server_capabilities(conn)
            has_gm_labels = "X-GM-EXT-1" in caps
            raw_folder, disp_folder = resolve_folder_name(conn, args.folder, must_exist=True)
            typ, _ = conn.select(quote_folder(raw_folder), readonly=True)
            if typ != "OK":
                raise RuntimeError(f"Ordner '{disp_folder}' konnte nicht im Lesemodus geöffnet werden.")

            cur_uv = _get_mailbox_uidvalidity(conn, raw_folder)
            cursor_key = f"{acc['id']}:{disp_folder}"
            prev_cursor = dict(ingest_cursors.get(cursor_key) or {})
            prev_uv = str(prev_cursor.get("uidvalidity") or "")
            last_uid = int(prev_cursor.get("last_uid") or 0) if prev_uv == cur_uv else 0
            seen_ids: list[str] = list(prev_cursor.get("seen_message_ids") or [])
            seen_set = set(seen_ids)

            crit_parts: list[str] = []
            if getattr(args, "since", None):
                crit_parts.append(f"SINCE {_format_imap_since(args.since)}")
            search_str = "(" + " ".join(crit_parts) + ")" if crit_parts else "ALL"
            typ_s, s_data = conn.uid("search", None, search_str)
            all_uids_int = sorted(
                int(u.decode("ascii"))
                for u in ((s_data[0] or b"").split() if (typ_s == "OK" and s_data) else [])
                if u.decode("ascii", errors="ignore").isdigit()
            )
            new_uids = [u for u in all_uids_int if u > last_uid]
            if args.limit > 0:
                new_uids = new_uids[: args.limit]

            hdr_spec = (
                f"(FLAGS X-GM-LABELS BODY.PEEK[HEADER.FIELDS ({HEADER_PEEK_FIELDS})])"
                if has_gm_labels
                else f"(FLAGS BODY.PEEK[HEADER.FIELDS ({HEADER_PEEK_FIELDS})])"
            )
            full_spec = "(FLAGS X-GM-LABELS BODY.PEEK[])" if has_gm_labels else "(FLAGS BODY.PEEK[])"

            max_processed_uid = last_uid
            for uid_num in new_uids:
                uid_str = str(uid_num)
                scanned_total += 1
                t_h, h_data = conn.uid("fetch", uid_str, hdr_spec)
                if t_h != "OK" or not h_data:
                    continue
                hdr_bytes, _flgs, _lbls = _extract_fetch_parts(h_data)
                if hdr_bytes is None:
                    continue
                hdr_msg = email.message_from_bytes(hdr_bytes)
                msg_id = decode_mime_header(hdr_msg.get("Message-ID")).strip()
                ctype_top = hdr_msg.get_content_type().lower()

                if msg_id and msg_id in seen_set:
                    max_processed_uid = max(max_processed_uid, uid_num)
                    continue

                # Fast header pre-filter: plain/html non-multipart messages cannot hold .ics/.pdf attachments
                can_have_att = (
                    ctype_top.startswith("multipart/")
                    or ctype_top in INGEST_CTYPES
                    or include_all
                )
                if not can_have_att:
                    max_processed_uid = max(max_processed_uid, uid_num)
                    continue

                t_b, b_data = conn.uid("fetch", uid_str, full_spec)
                if t_b != "OK" or not b_data:
                    continue
                full_bytes, _f2, lbls = _extract_fetch_parts(b_data)
                if full_bytes is None:
                    continue

                full_msg = email.message_from_bytes(full_bytes)
                subject = decode_mime_header(full_msg.get("Subject")) or "(No Subject)"
                from_hdr = decode_mime_header(full_msg.get("From")) or "(Unknown Sender)"
                to_hdr = decode_mime_header(full_msg.get("To"))
                date_hdr = decode_mime_header(full_msg.get("Date"))
                parsed_dt: dt.datetime | None = None
                if date_hdr:
                    try:
                        parsed_dt = email.utils.parsedate_to_datetime(date_hdr)
                    except Exception:
                        parsed_dt = None
                msg_date_iso = parsed_dt.date().isoformat() if parsed_dt else today_iso
                full_iso_dt = parsed_dt.isoformat() if parsed_dt else date_hdr

                body_txt, atts = _extract_ingest_payloads(full_msg)
                if not atts and not include_all:
                    max_processed_uid = max(max_processed_uid, uid_num)
                    continue

                slug = _slugify_stub(subject)
                stub_name = f"{msg_date_iso}_mail_{acc['id']}_{uid_str}_{slug}.md"
                stub_rel = f"inbox/{stub_name}"
                saved_files_rel: list[str] = []
                saved_att_Links: list[str] = []
                ics_summaries: list[str] = []

                if execute:
                    inbox_dir.mkdir(parents=True, exist_ok=True)

                for idx_a, att in enumerate(atts):
                    safe_fn = re.sub(r"[^A-Za-z0-9._-]", "_", att["filename"]).strip("_") or f"att_{idx_a + 1}"
                    out_fn = f"{msg_date_iso}_{acc['id']}_{uid_str}_{safe_fn}"
                    out_rel = f"inbox/{out_fn}"
                    saved_files_rel.append(out_rel)
                    saved_att_Links.append(f"- [{out_fn}]({out_fn}) (`{att['content_type']}`, {att['size_bytes']} bytes)")
                    if execute:
                        (inbox_dir / out_fn).write_bytes(att["bytes"])
                    for ev in att.get("ics_events") or []:
                        ev_sum = ev.get("summary") or "Calendar Event"
                        ev_dt = ev.get("dtstart") or ""
                        ev_end = f" -> {ev['dtend']}" if ev.get("dtend") else ""
                        ev_loc = f" @ {ev['location']}" if ev.get("location") else ""
                        ics_summaries.append(f"- **Event:** {ev_sum} (`{ev_dt}{ev_end}`{ev_loc})")

                from_short = re.sub(r"<[^>]+>", "", from_hdr).strip().strip('"') or from_hdr
                sum_raw = f"Mail ({from_short[:30]}): {subject}"[:115]
                imap_uri = f"imap://{acc['id']}/{disp_folder};uidvalidity={cur_uv};uid={uid_str}"
                sources_list = [imap_uri]
                if msg_id:
                    sources_list.append(msg_id if msg_id.startswith("<") else f"msgid:{msg_id}")
                sources_list.extend(saved_files_rel)

                fm_lines = [
                    "---",
                    f"summary: {_yaml_quote(sum_raw)}",
                    "tags: [mail]",
                    f"verified: {today_iso}",
                    "sources:",
                ]
                for s_item in sources_list:
                    fm_lines.append(f"  - {_yaml_quote(s_item)}")
                fm_lines.append("---")

                snippet_clean = re.sub(r"\s+", " ", body_txt).strip()[:600]
                md_lines = fm_lines + [
                    f"# Mail Ingest: {subject}",
                    "",
                    f"Stand: {today_iso}. Deterministically ingested via `atlas.py mail ingest` (read-only `BODY.PEEK`, 0 LLM tokens).",
                    "",
                    "## Provenance",
                    "",
                    f"- **Account / Folder:** `{acc['id']}` / `{disp_folder}` (`UID {uid_str}`, `UIDVALIDITY {cur_uv}`)",
                    f"- **From:** {from_hdr}",
                    f"- **To:** {to_hdr or '—'}",
                    f"- **Date:** {full_iso_dt or '—'}",
                    f"- **Message-ID:** `{msg_id or '—'}`",
                ]
                if lbls:
                    md_lines.append(f"- **Labels:** {', '.join(lbls)}")
                if saved_att_Links:
                    md_lines += ["", "## Saved Attachments", ""] + saved_att_Links
                if ics_summaries:
                    md_lines += ["", "## Calendar Invite (`.ics`) Summary", ""] + ics_summaries
                if snippet_clean:
                    md_lines += ["", "## Body Excerpt", "", snippet_clean]

                if execute:
                    (inbox_dir / stub_name).write_text("\n".join(md_lines) + "\n", encoding="utf-8")
                    if msg_id:
                        seen_ids.append(msg_id)
                        seen_set.add(msg_id)

                max_processed_uid = max(max_processed_uid, uid_num)
                ingested_items.append({
                    "account": acc["id"],
                    "folder": disp_folder,
                    "uid": uid_str,
                    "uidvalidity": cur_uv,
                    "message_id": msg_id,
                    "subject": subject,
                    "from": from_hdr,
                    "date": msg_date_iso,
                    "stub": stub_rel,
                    "attachments": saved_files_rel,
                    "ics_events_count": len(ics_summaries),
                })

            if execute and (max_processed_uid != last_uid or prev_uv != cur_uv):
                ingest_cursors[cursor_key] = {
                    "uidvalidity": cur_uv,
                    "last_uid": max_processed_uid,
                    "seen_message_ids": seen_ids[-500:],
                    "updated": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
                }
        finally:
            conn.logout()

    if execute and ingest_cursors:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_data["mail_ingest"] = ingest_cursors
        tmp_st = state_path.with_name(f"state.json.{os.getpid()}.tmp")
        tmp_st.write_text(json.dumps(state_data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp_st, state_path)

    if getattr(args, "json", False):
        print(json.dumps({
            "status": "APPLIED" if execute else "DRY_RUN",
            "scanned_new_uids": scanned_total,
            "ingested_count": len(ingested_items),
            "items": ingested_items,
        }, ensure_ascii=False, indent=2))
        return 0

    mode_lbl = "applied" if execute else "dry-run"
    print(f"mail ingest ({mode_lbl}): scanned {scanned_total} new UID(s), {len(ingested_items)} item(s) {'saved to' if execute else 'ready for'} brain/inbox/.")
    for it in ingested_items:
        atts_s = f" + {len(it['attachments'])} attachment(s) ({', '.join(Path(x).name for x in it['attachments'])})" if it["attachments"] else ""
        print(f"  - [{it['account']}] UID {it['uid']} '{it['subject']}' -> {it['stub']}{atts_s}")
    if not execute and ingested_items:
        print("Dry run. Add --execute to save files into brain/inbox/ and advance state.json cursor.")
    return 0


def cmd_move(args: argparse.Namespace) -> int:
    """Move message(s) by UID to target_folder via RFC 6851 MOVE or safe fallback."""
    accounts = load_accounts()
    acc = resolve_account(accounts, args.account)
    uids = normalize_uids(args.uids)
    conn = connect_imap(acc)
    try:
        caps = get_server_capabilities(conn)
        folders = list_imap_folders(conn)
        raw_source, disp_source = resolve_folder_name(conn, args.folder, folders_cache=folders, must_exist=True)
        raw_target, disp_target = resolve_folder_name(
            conn,
            args.target_folder,
            folders_cache=folders,
            must_exist=not args.create_folder,
        )
        if raw_source == raw_target:
            raise RuntimeError(f"Quellordner und target_folder ('{disp_target}') sind identisch.")

        previews = fetch_uid_previews(conn, raw_source, uids, caps=caps)
        move_proto = "RFC 6851 UID MOVE" if "MOVE" in caps else "RFC 3501 COPY + STORE (\\Deleted) + EXPUNGE"
        details = f"Verschiebe {len(uids)} E-Mail(s) von '{disp_source}' -> target_folder '{disp_target}' ({move_proto})"

        if not enforce_write_safety_gate(
            acc,
            action_title="MOVE (E-Mails umsortieren / verschieben)",
            action_details=details,
            previews=previews,
            dry_run=args.dry_run,
            assume_yes=args.yes,
            json_mode=args.json,
        ):
            return 0 if args.dry_run else 2

        if args.create_folder and not any(f["raw"] == raw_target for f in folders):
            typ, create_data = conn.create(quote_folder(raw_target))
            if typ != "OK":
                raise RuntimeError(f"Konnte target_folder '{disp_target}' nicht anlegen: {create_data}")

        res = execute_imap_move(conn, raw_source, raw_target, uids, caps=caps)
        res["status"] = "MOVED"
        res["affected_messages"] = previews
        if args.json:
            print(json.dumps(res, ensure_ascii=False, indent=2))
        else:
            print(
                f"✅ {len(uids)} E-Mail(s) erfolgreich von '{disp_source}' nach '{disp_target}' "
                f"verschoben ({res['method']})."
            )
        return 0
    finally:
        conn.logout()


def cmd_copy(args: argparse.Namespace) -> int:
    """Copy message(s) by UID to target_folder via RFC 3501 UID COPY."""
    accounts = load_accounts()
    acc = resolve_account(accounts, args.account)
    uids = normalize_uids(args.uids)
    conn = connect_imap(acc)
    try:
        caps = get_server_capabilities(conn)
        folders = list_imap_folders(conn)
        raw_source, disp_source = resolve_folder_name(conn, args.folder, folders_cache=folders, must_exist=True)
        raw_target, disp_target = resolve_folder_name(conn, args.target_folder, folders_cache=folders, must_exist=True)

        previews = fetch_uid_previews(conn, raw_source, uids, caps=caps)
        details = f"Kopiere {len(uids)} E-Mail(s) von '{disp_source}' -> target_folder '{disp_target}' (RFC 3501 UID COPY)"

        if not enforce_write_safety_gate(
            acc,
            action_title="COPY (E-Mails kopieren)",
            action_details=details,
            previews=previews,
            dry_run=args.dry_run,
            assume_yes=args.yes,
            json_mode=args.json,
        ):
            return 0 if args.dry_run else 2

        typ, _ = conn.select(quote_folder(raw_source), readonly=False)
        if typ != "OK":
            raise RuntimeError(f"Ordner '{disp_source}' konnte nicht geöffnet werden.")

        uid_set = ",".join(uids)
        typ, copy_data = conn.uid("COPY", uid_set, quote_folder(raw_target))
        if typ != "OK":
            raise RuntimeError(f"IMAP COPY nach '{disp_target}' fehlgeschlagen: {copy_data}")

        out = {
            "status": "COPIED",
            "uids": uids,
            "source_folder": disp_source,
            "target_folder": disp_target,
            "affected_messages": previews,
        }
        if args.json:
            print(json.dumps(out, ensure_ascii=False, indent=2))
        else:
            print(f"✅ {len(uids)} E-Mail(s) erfolgreich von '{disp_source}' nach '{disp_target}' kopiert.")
        return 0
    finally:
        conn.logout()


def cmd_trash(args: argparse.Namespace) -> int:
    """Soft-delete message(s) by moving them into the Trash / Papierkorb folder."""
    accounts = load_accounts()
    acc = resolve_account(accounts, args.account)
    uids = normalize_uids(args.uids)
    conn = connect_imap(acc)
    try:
        caps = get_server_capabilities(conn)
        folders = list_imap_folders(conn)
        raw_source, disp_source = resolve_folder_name(conn, args.folder, folders_cache=folders, must_exist=True)
        if args.trash_folder:
            raw_trash, disp_trash = resolve_folder_name(
                conn, args.trash_folder, folders_cache=folders, must_exist=True
            )
        else:
            raw_trash, disp_trash = detect_trash_folder(conn, acc)

        if raw_source == raw_trash:
            raise RuntimeError(
                f"Die E-Mail(s) befinden sich bereits im Papierkorb ('{disp_trash}'). "
                f"Ein erneutes Verschieben in den Papierkorb ist nicht nötig."
            )

        previews = fetch_uid_previews(conn, raw_source, uids, caps=caps)
        details = (
            f"Soft-Delete: Verschiebe {len(uids)} E-Mail(s) aus '{disp_source}' "
            f"in den Papierkorb '{disp_trash}' (wiederherstellbar)"
        )

        if not enforce_write_safety_gate(
            acc,
            action_title="TRASH (Soft-Delete in Papierkorb)",
            action_details=details,
            previews=previews,
            dry_run=args.dry_run,
            assume_yes=args.yes,
            json_mode=args.json,
        ):
            return 0 if args.dry_run else 2

        res = execute_imap_move(conn, raw_source, raw_trash, uids, caps=caps)
        res["status"] = "TRASHED"
        res["affected_messages"] = previews
        if args.json:
            print(json.dumps(res, ensure_ascii=False, indent=2))
        else:
            print(
                f"🗑️  {len(uids)} E-Mail(s) aus '{disp_source}' in den Papierkorb '{disp_trash}' "
                f"verschoben ({res['method']})."
            )
        return 0
    finally:
        conn.logout()


def cmd_delete(args: argparse.Namespace) -> int:
    """
    Hard-delete message(s) via IMAP STORE +FLAGS (\\Deleted) + EXPUNGE.
    Requires explicit --hard flag in addition to the safety gate.
    """
    if not args.hard:
        raise RuntimeError(
            "SAFETY GATE: 'delete' führt eine permanente IMAP-Löschung (\\Deleted + EXPUNGE) durch! "
            "Nutze stattdessen 'trash' (Papierkorb) oder übergib explizit '--hard', wenn wirklich "
            "endgültig gelöscht werden soll."
        )

    accounts = load_accounts()
    acc = resolve_account(accounts, args.account)
    uids = normalize_uids(args.uids)
    conn = connect_imap(acc)
    try:
        caps = get_server_capabilities(conn)
        raw_source, disp_source = resolve_folder_name(conn, args.folder, must_exist=True)
        previews = fetch_uid_previews(conn, raw_source, uids, caps=caps)
        expunge_proto = "RFC 4315 UID EXPUNGE" if "UIDPLUS" in caps else "RFC 3501 EXPUNGE"
        details = (
            f"⚠️  PERMANENTE LÖSCHUNG (Hard-Delete): Setze \\Deleted Flag und führe {expunge_proto} "
            f"für {len(uids)} E-Mail(s) in '{disp_source}' aus (NICHT WIEDERHERSTELLBAR)"
        )

        if not enforce_write_safety_gate(
            acc,
            action_title="DELETE (Permanente Hard-Delete Löschung)",
            action_details=details,
            previews=previews,
            dry_run=args.dry_run,
            assume_yes=args.yes,
            json_mode=args.json,
        ):
            return 0 if args.dry_run else 2

        typ, _ = conn.select(quote_folder(raw_source), readonly=False)
        if typ != "OK":
            raise RuntimeError(f"Ordner '{disp_source}' konnte nicht im Schreibmodus geöffnet werden.")

        uid_set = ",".join(uids)
        typ, store_data = conn.uid("STORE", uid_set, "+FLAGS.SILENT", "(\\Deleted)")
        if typ != "OK":
            raise RuntimeError(f"Konnte \\Deleted Flag nicht setzen: {store_data}")

        if "UIDPLUS" in caps:
            conn.uid("EXPUNGE", uid_set)
            method = "UID_EXPUNGE"
        else:
            conn.expunge()
            method = "EXPUNGE"

        out = {
            "status": "HARD_DELETED",
            "method": method,
            "uids": uids,
            "folder": disp_source,
            "affected_messages": previews,
        }
        if args.json:
            print(json.dumps(out, ensure_ascii=False, indent=2))
        else:
            print(f"✅ {len(uids)} E-Mail(s) in '{disp_source}' permanent gelöscht ({method}).")
        return 0
    finally:
        conn.logout()


def cmd_flags(args: argparse.Namespace) -> int:
    """Inspect or modify standard IMAP FLAGS (RFC 3501) on one or more UIDs."""
    accounts = load_accounts()
    acc = resolve_account(accounts, args.account)
    uids = normalize_uids(args.uids)

    add_flags = normalize_flag_list(args.add_flags)
    remove_flags = normalize_flag_list(args.remove_flags)
    set_flags = normalize_flag_list(args.set_flags) if args.set_flags is not None else None

    if args.read:
        if "\\Seen" not in add_flags:
            add_flags.append("\\Seen")
    if args.unread:
        if "\\Seen" not in remove_flags:
            remove_flags.append("\\Seen")
    if args.star:
        if "\\Flagged" not in add_flags:
            add_flags.append("\\Flagged")
    if args.unstar:
        if "\\Flagged" not in remove_flags:
            remove_flags.append("\\Flagged")
    if args.answered:
        if "\\Answered" not in add_flags:
            add_flags.append("\\Answered")
    if args.unanswered:
        if "\\Answered" not in remove_flags:
            remove_flags.append("\\Answered")

    is_inspect_only = not add_flags and not remove_flags and set_flags is None

    conn = connect_imap(acc)
    try:
        caps = get_server_capabilities(conn)
        raw_folder, disp_folder = resolve_folder_name(conn, args.folder, must_exist=True)
        previews = fetch_uid_previews(conn, raw_folder, uids, caps=caps)

        if is_inspect_only:
            if args.json:
                print(json.dumps(previews, ensure_ascii=False, indent=2))
            else:
                print(f"=== IMAP FLAGS (Read-Only Inspektion) in [{acc['id']}]/{disp_folder} ===")
                for p in previews:
                    f_str = " ".join(p["flags"]) if p["flags"] else "(keine Flags)"
                    l_str = f" | LABELS: {', '.join(p['labels'])}" if p["labels"] else ""
                    print(f"- UID {p['uid']} | FLAGS: {f_str}{l_str} | Betreff: {p['subject']}")
            return 0

        ops_desc: list[str] = []
        if set_flags is not None:
            ops_desc.append(f"SET FLAGS = ({' '.join(set_flags)})")
        if add_flags:
            ops_desc.append(f"+FLAGS ({' '.join(add_flags)})")
        if remove_flags:
            ops_desc.append(f"-FLAGS ({' '.join(remove_flags)})")
        details = f"Ändere IMAP FLAGS in '{disp_folder}': {', '.join(ops_desc)}"

        if not enforce_write_safety_gate(
            acc,
            action_title="FLAGS (IMAP Status-Flags ändern)",
            action_details=details,
            previews=previews,
            dry_run=args.dry_run,
            assume_yes=args.yes,
            json_mode=args.json,
        ):
            return 0 if args.dry_run else 2

        typ, _ = conn.select(quote_folder(raw_folder), readonly=False)
        if typ != "OK":
            raise RuntimeError(f"Ordner '{disp_folder}' konnte nicht im Schreibmodus geöffnet werden.")

        uid_set = ",".join(uids)
        if set_flags is not None:
            flag_str = "(" + " ".join(set_flags) + ")"
            typ, data = conn.uid("STORE", uid_set, "FLAGS", flag_str)
            if typ != "OK":
                raise RuntimeError(f"IMAP STORE FLAGS fehlgeschlagen: {data}")
        if add_flags:
            flag_str = "(" + " ".join(add_flags) + ")"
            typ, data = conn.uid("STORE", uid_set, "+FLAGS", flag_str)
            if typ != "OK":
                raise RuntimeError(f"IMAP STORE +FLAGS fehlgeschlagen: {data}")
        if remove_flags:
            flag_str = "(" + " ".join(remove_flags) + ")"
            typ, data = conn.uid("STORE", uid_set, "-FLAGS", flag_str)
            if typ != "OK":
                raise RuntimeError(f"IMAP STORE -FLAGS fehlgeschlagen: {data}")

        updated = fetch_uid_previews(conn, raw_folder, uids, caps=caps)
        if args.json:
            print(
                json.dumps(
                    {
                        "status": "FLAGS_UPDATED",
                        "folder": disp_folder,
                        "operations": ops_desc,
                        "messages": updated,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            print(f"✅ IMAP FLAGS für {len(uids)} E-Mail(s) in '{disp_folder}' aktualisiert:")
            for u in updated:
                print(f"  • UID {u['uid']}: FLAGS = ({' '.join(u['flags'])}) | {u['subject']}")
        return 0
    finally:
        conn.logout()


def cmd_labels(args: argparse.Namespace) -> int:
    """Inspect or modify Gmail X-GM-LABELS (or IMAP keyword flags as fallback) on UIDs."""
    accounts = load_accounts()
    acc = resolve_account(accounts, args.account)
    uids = normalize_uids(args.uids)

    add_labels = normalize_label_list(args.add)
    remove_labels = normalize_label_list(args.remove)
    set_labels = normalize_label_list(args.set) if args.set is not None else None
    is_inspect_only = not add_labels and not remove_labels and set_labels is None

    conn = connect_imap(acc)
    try:
        caps = get_server_capabilities(conn)
        has_gm_labels = "X-GM-EXT-1" in caps
        raw_folder, disp_folder = resolve_folder_name(conn, args.folder, must_exist=True)
        previews = fetch_uid_previews(conn, raw_folder, uids, caps=caps)

        if is_inspect_only:
            if args.json:
                print(json.dumps(previews, ensure_ascii=False, indent=2))
            else:
                proto = "X-GM-LABELS (Gmail Extension)" if has_gm_labels else "Standard IMAP Keywords (FLAGS)"
                print(f"=== LABELS ({proto}) in [{acc['id']}]/{disp_folder} ===")
                for p in previews:
                    l_str = ", ".join(p["labels"]) if p["labels"] else "(keine Labels)"
                    f_str = " ".join(p["flags"]) if p["flags"] else "(keine Flags)"
                    print(f"- UID {p['uid']} | LABELS: {l_str} | FLAGS: {f_str} | Betreff: {p['subject']}")
            return 0

        if not has_gm_labels and not args.use_keywords:
            raise RuntimeError(
                f"Der IMAP-Server für Konto [{acc['id']}] ({acc['host']}) unterstützt keine Gmail "
                f"X-GM-LABELS (Capability X-GM-EXT-1 fehlt). Nutze 'flags' für IMAP-Keywords, "
                f"'move' zum Umsortieren in Ordner oder übergib '--use-keywords'."
            )

        store_attr = "X-GM-LABELS" if has_gm_labels else "FLAGS"
        ops_desc: list[str] = []
        if set_labels is not None:
            ops_desc.append(f"SET {store_attr} = ({', '.join(set_labels)})")
        if add_labels:
            ops_desc.append(f"+{store_attr} ({', '.join(add_labels)})")
        if remove_labels:
            ops_desc.append(f"-{store_attr} ({', '.join(remove_labels)})")
        details = f"Ändere {store_attr} in '{disp_folder}': {', '.join(ops_desc)}"

        if not enforce_write_safety_gate(
            acc,
            action_title="LABELS (E-Mail-Labels / Tags verwalten)",
            action_details=details,
            previews=previews,
            dry_run=args.dry_run,
            assume_yes=args.yes,
            json_mode=args.json,
        ):
            return 0 if args.dry_run else 2

        typ, _ = conn.select(quote_folder(raw_folder), readonly=False)
        if typ != "OK":
            raise RuntimeError(f"Ordner '{disp_folder}' konnte nicht im Schreibmodus geöffnet werden.")

        uid_set = ",".join(uids)
        if set_labels is not None:
            typ, data = conn.uid("STORE", uid_set, store_attr, format_imap_label_list(set_labels))
            if typ != "OK":
                raise RuntimeError(f"IMAP STORE {store_attr} fehlgeschlagen: {data}")
        if add_labels:
            typ, data = conn.uid("STORE", uid_set, f"+{store_attr}", format_imap_label_list(add_labels))
            if typ != "OK":
                raise RuntimeError(f"IMAP STORE +{store_attr} fehlgeschlagen: {data}")
        if remove_labels:
            typ, data = conn.uid("STORE", uid_set, f"-{store_attr}", format_imap_label_list(remove_labels))
            if typ != "OK":
                raise RuntimeError(f"IMAP STORE -{store_attr} fehlgeschlagen: {data}")

        updated = fetch_uid_previews(conn, raw_folder, uids, caps=caps)
        if args.json:
            print(
                json.dumps(
                    {
                        "status": "LABELS_UPDATED",
                        "folder": disp_folder,
                        "operations": ops_desc,
                        "messages": updated,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            print(f"✅ LABELS für {len(uids)} E-Mail(s) in '{disp_folder}' aktualisiert:")
            for u in updated:
                print(
                    f"  • UID {u['uid']}: LABELS = [{', '.join(u['labels'])}] | "
                    f"FLAGS = ({' '.join(u['flags'])}) | {u['subject']}"
                )
        return 0
    finally:
        conn.logout()


def cmd_mkdir(args: argparse.Namespace) -> int:
    """Create a new IMAP folder (target_folder)."""
    accounts = load_accounts()
    acc = resolve_account(accounts, args.account)
    conn = connect_imap(acc)
    try:
        folders = list_imap_folders(conn)
        raw_target, disp_target = resolve_folder_name(
            conn, args.target_folder, folders_cache=folders, must_exist=False
        )
        if any(f["raw"] == raw_target for f in folders):
            print(f"ℹ️  IMAP-Ordner '{disp_target}' (raw: {raw_target}) existiert bereits.")
            return 0

        details = f"Erstelle neuen IMAP-Ordner target_folder '{disp_target}' (raw UTF-7: {raw_target})"
        if not enforce_write_safety_gate(
            acc,
            action_title="MKDIR (Neuen IMAP-Ordner erstellen)",
            action_details=details,
            previews=[],
            dry_run=args.dry_run,
            assume_yes=args.yes,
            json_mode=args.json,
        ):
            return 0 if args.dry_run else 2

        typ, data = conn.create(quote_folder(raw_target))
        if typ != "OK":
            raise RuntimeError(f"IMAP CREATE für '{disp_target}' fehlgeschlagen: {data}")

        if args.json:
            print(
                json.dumps(
                    {"status": "FOLDER_CREATED", "target_folder": disp_target, "raw_folder": raw_target},
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            print(f"✅ IMAP-Ordner '{disp_target}' (raw: {raw_target}) erfolgreich erstellt.")
        return 0
    finally:
        conn.logout()


def _add_write_safety_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Nur Vorschau der betroffenen E-Mails anzeigen, ohne Änderungen am Postfach vorzunehmen",
    )
    parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Sicherheitsabfrage bestätigen (durch Agenten NUR nach expliziter Freigabe durch den Nutzer zu setzen!)",
    )
    parser.add_argument("--json", action="store_true", help="Ausgabe als JSON")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Atlas Multi-Account IMAP Tool (Read-Only by default + Safety-Gated Write Operations: "
            "MOVE, COPY, TRASH, DELETE, FLAGS, LABELS, MKDIR)"
        )
    )
    p.add_argument("--root", help="Atlas ROOT folder (default: auto-detected from script path or $ATLAS_ROOT)")
    p.add_argument("--env", help="Explicit path to mail.env (default: <ROOT>/brain/_secrets/mail.env)")
    sub = p.add_subparsers(dest="cmd", required=True)

    # --- ONBOARDING / INIT ---
    p_init = sub.add_parser("init", help="Vorlage für brain/_secrets/mail.env (chmod 600) anlegen oder Konto ergänzen")
    p_init.add_argument("-a", "--account", default="MAIN", help="Account-ID (z. B. WORK, PERSONAL, GMAIL)")
    p_init.add_argument("--label", help="Anzeigename des Kontos")
    p_init.add_argument("--host", help="IMAP-Host (z. B. imap.gmail.com)")
    p_init.add_argument("--port", type=int, default=993, help="IMAP-Port (Default: 993)")
    p_init.add_argument("--user", help="E-Mail-Adresse / Login")
    p_init.add_argument("--password", help="App-Passwort / Passwort")
    p_init.add_argument("--force", action="store_true", help="Bestehenden Block überschreiben/ergänzen")
    p_init.set_defaults(func=cmd_init)

    # --- READ-ONLY COMMANDS ---
    p_acc = sub.add_parser("accounts", help="Alle konfigurierten E-Mail-Konten, IMAP-Capabilities & Status prüfen")
    p_acc.set_defaults(func=cmd_accounts)

    p_fld = sub.add_parser("folders", help="Alle IMAP-Ordner eines Kontos auflisten")
    p_fld.add_argument("-a", "--account", help="Account-ID oder E-Mail-Adresse")
    p_fld.add_argument("--json", action="store_true", help="Ausgabe als JSON")
    p_fld.set_defaults(func=cmd_folders)

    p_lst = sub.add_parser("list", help="Nachrichten in einem Ordner auflisten (Read-Only, fast HEADER.PEEK)")
    p_lst.add_argument("-a", "--account", help="Account-ID oder E-Mail-Adresse")
    p_lst.add_argument("-f", "--folder", default="INBOX", help="IMAP-Ordner (Default: INBOX)")
    p_lst.add_argument("-n", "--limit", type=int, default=50, help="Maximale Anzahl (Default: 50)")
    p_lst.add_argument("--since", help="Nur Nachrichten seit YYYY-MM-DD")
    p_lst.add_argument("--unread", action="store_true", help="Nur ungelesene Nachrichten (UNSEEN)")
    p_lst.add_argument("--flagged", action="store_true", help="Nur markierte Nachrichten (\\Flagged)")
    p_lst.add_argument("--with-body", action="store_true", help="Volltext-Snippets laden statt schnellem HEADER.PEEK")
    p_lst.add_argument("--json", action="store_true", help="Ausgabe als JSON")
    p_lst.set_defaults(func=cmd_list)

    p_rd = sub.add_parser("read", help="Einzelne E-Mail inkl. Volltext, FLAGS, LABELS & Anhängen lesen (Read-Only)")
    p_rd.add_argument("uid", help="Nachrichten-UID")
    p_rd.add_argument("-a", "--account", help="Account-ID oder E-Mail-Adresse")
    p_rd.add_argument("-f", "--folder", default="INBOX", help="IMAP-Ordner (Default: INBOX)")
    p_rd.add_argument("--save-attachments", help="Zielordner zum Speichern von Anhängen")
    p_rd.add_argument("--json", action="store_true", help="Ausgabe als JSON")
    p_rd.set_defaults(func=cmd_read)

    p_srch = sub.add_parser("search", help="Volltext-, Header-, Anhang- & Label-Suche via UID SEARCH + Fallback (Read-Only)")
    p_srch.add_argument("query", help="Suchbegriff (Betreff, Absender, Empfänger, Text, Anhang, Label)")
    p_srch.add_argument("-a", "--account", help="Account-ID oder E-Mail-Adresse")
    p_srch.add_argument("-f", "--folder", default="INBOX", help="IMAP-Ordner (Default: INBOX)")
    p_srch.add_argument("-n", "--limit", type=int, default=50, help="Max. Treffer")
    p_srch.add_argument("--scan-limit", type=int, default=300, help="Max. zu durchsuchende Mails")
    p_srch.add_argument("--since", help="Optional: nur Nachrichten seit YYYY-MM-DD")
    p_srch.add_argument("--json", action="store_true", help="Ausgabe als JSON")
    p_srch.set_defaults(func=cmd_search)

    p_ing = sub.add_parser(
        "ingest",
        help="Read-Only 0-Token Bridge: speichert .ics/.pdf Anhänge + Provenance-Stubs in brain/inbox/ mit UIDVALIDITY-Cursor",
    )
    p_ing.add_argument("-a", "--account", help="Account-ID oder E-Mail-Adresse (Default: aktives Hauptkonto)")
    p_ing.add_argument("--all-accounts", action="store_true", help="Alle konfigurierten Konten scannen")
    p_ing.add_argument("-f", "--folder", default="INBOX", help="IMAP-Ordner (Default: INBOX)")
    p_ing.add_argument("-n", "--limit", type=int, default=50, help="Max. neue UIDs pro Lauf (Default: 50)")
    p_ing.add_argument("--since", help="Optional: nur Nachrichten seit YYYY-MM-DD")
    p_ing.add_argument("--include-all", action="store_true", help="Auch reine Text-Mails ohne .ics/.pdf als Stub ablegen")
    p_ing.add_argument("--execute", action="store_true", help="Dateien in brain/inbox/ & Cursor in state.json schreiben")
    p_ing.add_argument("--json", action="store_true", help="Ausgabe als JSON")
    p_ing.set_defaults(func=cmd_ingest)

    p_aud = sub.add_parser("audit", help="Komplett-Scan über Posteingang & Gesendet / Alle Nachrichten (Read-Only)")
    p_aud.add_argument("-a", "--account", help="Account-ID oder E-Mail-Adresse")
    p_aud.add_argument("-n", "--limit", type=int, default=200, help="Max. Mails pro Ordner (0 = alle)")
    p_aud.add_argument("--since", help="Optional: nur seit YYYY-MM-DD")
    p_aud.add_argument("--out", help="Optional: Pfad für vollständigen JSON-Export")
    p_aud.add_argument("--json", action="store_true", help="Ausgabe als JSON")
    p_aud.set_defaults(func=cmd_audit)

    # --- SAFETY-GATED WRITE COMMANDS ---
    p_mv = sub.add_parser(
        "move",
        help="E-Mail(s) per RFC 6851 MOVE (oder Fallback) in einen target_folder verschieben (Safety-Gated)",
    )
    p_mv.add_argument("uids", nargs="+", help="Eine oder mehrere Nachrichten-UIDs (z. B. 101 102)")
    p_mv.add_argument("-t", "--target-folder", required=True, dest="target_folder", help="Zielordner (target_folder)")
    p_mv.add_argument("-a", "--account", help="Account-ID oder E-Mail-Adresse")
    p_mv.add_argument("-f", "--folder", default="INBOX", help="Quellordner (Default: INBOX)")
    p_mv.add_argument(
        "--create-folder",
        action="store_true",
        help="target_folder automatisch anlegen, falls er noch nicht existiert",
    )
    _add_write_safety_args(p_mv)
    p_mv.set_defaults(func=cmd_move)

    p_cp = sub.add_parser("copy", help="E-Mail(s) per RFC 3501 COPY in einen target_folder kopieren (Safety-Gated)")
    p_cp.add_argument("uids", nargs="+", help="Eine oder mehrere Nachrichten-UIDs")
    p_cp.add_argument("-t", "--target-folder", required=True, dest="target_folder", help="Zielordner (target_folder)")
    p_cp.add_argument("-a", "--account", help="Account-ID oder E-Mail-Adresse")
    p_cp.add_argument("-f", "--folder", default="INBOX", help="Quellordner (Default: INBOX)")
    _add_write_safety_args(p_cp)
    p_cp.set_defaults(func=cmd_copy)

    p_tr = sub.add_parser(
        "trash",
        help="Soft-Delete: E-Mail(s) sicher in den Papierkorb (\\Trash) verschieben (Safety-Gated)",
    )
    p_tr.add_argument("uids", nargs="+", help="Eine oder mehrere Nachrichten-UIDs")
    p_tr.add_argument("-a", "--account", help="Account-ID oder E-Mail-Adresse")
    p_tr.add_argument("-f", "--folder", default="INBOX", help="Quellordner (Default: INBOX)")
    p_tr.add_argument("--trash-folder", help="Optional: Expliziter Name des Papierkorb-Ordners")
    _add_write_safety_args(p_tr)
    p_tr.set_defaults(func=cmd_trash)

    p_del = sub.add_parser(
        "delete",
        help="Hard-Delete: E-Mail(s) per \\Deleted + EXPUNGE permanent löschen (Erfordert --hard & Safety-Gate)",
    )
    p_del.add_argument("uids", nargs="+", help="Eine oder mehrere Nachrichten-UIDs")
    p_del.add_argument("-a", "--account", help="Account-ID oder E-Mail-Adresse")
    p_del.add_argument("-f", "--folder", default="INBOX", help="Quellordner (Default: INBOX)")
    p_del.add_argument(
        "--hard",
        action="store_true",
        help="Pflicht-Flag zur Bestätigung einer unwiderruflichen Löschung (statt 'trash')",
    )
    _add_write_safety_args(p_del)
    p_del.set_defaults(func=cmd_delete)

    p_flg = sub.add_parser(
        "flags",
        help="IMAP FLAGS (\\Seen, \\Flagged, \\Answered etc.) inspizieren oder ändern (Safety-Gated)",
    )
    p_flg.add_argument("uids", nargs="+", help="Eine oder mehrere Nachrichten-UIDs")
    p_flg.add_argument("-a", "--account", help="Account-ID oder E-Mail-Adresse")
    p_flg.add_argument("-f", "--folder", default="INBOX", help="IMAP-Ordner (Default: INBOX)")
    p_flg.add_argument("--read", action="store_true", help="Als gelesen markieren (+FLAGS \\Seen)")
    p_flg.add_argument("--unread", action="store_true", help="Als ungelesen markieren (-FLAGS \\Seen)")
    p_flg.add_argument("--star", "--flag", dest="star", action="store_true", help="Markieren / Stern setzen (+FLAGS \\Flagged)")
    p_flg.add_argument("--unstar", "--unflag", dest="unstar", action="store_true", help="Markierung entfernen (-FLAGS \\Flagged)")
    p_flg.add_argument("--answered", action="store_true", help="Als beantwortet markieren (+FLAGS \\Answered)")
    p_flg.add_argument("--unanswered", action="store_true", help="Beantwortet-Flag entfernen (-FLAGS \\Answered)")
    p_flg.add_argument("--add-flags", nargs="+", help="Beliebige IMAP FLAGS hinzufügen (z. B. '\\Seen,\\Flagged')")
    p_flg.add_argument("--remove-flags", nargs="+", help="Beliebige IMAP FLAGS entfernen")
    p_flg.add_argument("--set-flags", nargs="*", help="IMAP FLAGS komplett überschreiben")
    _add_write_safety_args(p_flg)
    p_flg.set_defaults(func=cmd_flags)

    p_lbl = sub.add_parser(
        "labels",
        help="Gmail X-GM-LABELS (oder IMAP Keyword-Labels) inspizieren oder ändern (Safety-Gated)",
    )
    p_lbl.add_argument("uids", nargs="+", help="Eine oder mehrere Nachrichten-UIDs")
    p_lbl.add_argument("-a", "--account", help="Account-ID oder E-Mail-Adresse")
    p_lbl.add_argument("-f", "--folder", default="INBOX", help="IMAP-Ordner (Default: INBOX)")
    p_lbl.add_argument("--add", nargs="+", help="Labels hinzufügen (z. B. --add 'Steuern' 'Wichtig')")
    p_lbl.add_argument("--remove", nargs="+", help="Labels entfernen")
    p_lbl.add_argument("--set", nargs="*", help="Labels komplett ersetzen")
    p_lbl.add_argument(
        "--use-keywords",
        action="store_true",
        help="Auf Nicht-Gmail-Servern Standard-IMAP-FLAGS als Keyword-Fallback verwenden",
    )
    _add_write_safety_args(p_lbl)
    p_lbl.set_defaults(func=cmd_labels)

    p_mk = sub.add_parser(
        "mkdir",
        aliases=["create-folder"],
        help="Neuen IMAP-Ordner (target_folder) anlegen (Safety-Gated)",
    )
    p_mk.add_argument("target_folder", help="Name des anzulegenden IMAP-Ordners (target_folder)")
    p_mk.add_argument("-a", "--account", help="Account-ID oder E-Mail-Adresse")
    _add_write_safety_args(p_mk)
    p_mk.set_defaults(func=cmd_mkdir)

    return p


def main(argv: list[str] | None = None) -> int:
    global SECRETS_PATH
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "root", None) or getattr(args, "env", None):
        SECRETS_PATH = resolve_default_secrets_path(explicit_root=args.root, explicit_env=args.env)
    try:
        return args.func(args)
    except Exception as exc:
        print(f"Fehler: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
