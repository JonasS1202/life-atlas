"""Provider-agnostic Calendar interface & Google Calendar REST API v3 connector (Python 3 stdlib only).

Supports multi-account OAuth 2.0 (PKCE S256 + loopback callback or optional broker), incremental
delta polling via `syncToken`, `singleEvents` expansion, `FreeBusy` queries, Google Meet creation
(`conferenceData`), and RSVP updates.

Credentials live exclusively in `brain/_secrets/gcal.env` (`0o600`, git-ignored).
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import os
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Callable

GCAL_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GCAL_TOKEN_URL = "https://oauth2.googleapis.com/token"
GCAL_USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"
GCAL_API_BASE = "https://www.googleapis.com/calendar/v3"

GCAL_SCOPES = (
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/userinfo.email",
)

REQUEST_TIMEOUT = 15

# Type alias for injectable HTTP transport used in offline tests:
# `(method, url, headers, body_bytes, timeout) -> (status_code, response_dict)`
HttpTransport = Callable[[str, str, dict[str, str], bytes | None, float], tuple[int, dict[str, Any]]]


# ---------------------------------------------------------------------------
# 1. Secret `.env` Helpers (`brain/_secrets/gcal.env`, `chmod 600`)
# ---------------------------------------------------------------------------

def sanitize_account_id(account_id: str) -> str:
    """Normalize an account identifier to lowercase `[a-z0-9_]+`."""
    clean = re.sub(r"[^a-z0-9_]+", "_", str(account_id or "").strip().lower()).strip("_")
    return clean or "default"


def _acct_key(account_id: str, field: str) -> str:
    return f"GCAL_{sanitize_account_id(account_id).upper()}_{field.upper()}"


def gcal_secret_path(brain: str) -> str:
    return os.path.join(brain, "_secrets", "gcal.env")


def load_gcal_env(brain: str) -> dict[str, str]:
    p = gcal_secret_path(brain)
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


def save_gcal_env(brain: str, env_dict: dict[str, str]) -> str:
    sec_dir = os.path.join(brain, "_secrets")
    os.makedirs(sec_dir, exist_ok=True)
    p = gcal_secret_path(brain)
    lines = ["# Managed by Atlas Calendar Sync (Google Calendar) — local only, never committed"]
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


def list_configured_accounts(brain: str) -> list[dict[str, Any]]:
    """Return status dicts for all Google Calendar accounts stored in `brain/_secrets/gcal.env`."""
    env = load_gcal_env(brain)
    ids: list[str] = []
    raw_list = (env.get("GCAL_ACCOUNTS") or "").strip()
    if raw_list:
        for tok in raw_list.split(","):
            aid = sanitize_account_id(tok)
            if aid and aid not in ids:
                ids.append(aid)
    # Also discover any accounts referenced directly via GCAL_<ID>_REFRESH_TOKEN / ACCESS_TOKEN
    for k in sorted(env):
        m = re.match(r"^GCAL_([A-Z0-9_]+)_(?:REFRESH_TOKEN|ACCESS_TOKEN|EMAIL)$", k)
        if m:
            aid = sanitize_account_id(m.group(1))
            if aid and aid not in ("client", "broker", "redirect") and aid not in ids:
                ids.append(aid)

    now_ts = int(time.time())
    out: list[dict[str, Any]] = []
    for aid in ids:
        has_refresh = bool((env.get(_acct_key(aid, "REFRESH_TOKEN")) or "").strip())
        has_access = bool((env.get(_acct_key(aid, "ACCESS_TOKEN")) or "").strip())
        exp = int(env.get(_acct_key(aid, "EXPIRES_AT")) or 0)
        err = (env.get(_acct_key(aid, "AUTH_ERROR")) or "").strip()
        email = (env.get(_acct_key(aid, "EMAIL")) or "").strip()
        connected = (has_refresh or (has_access and exp > now_ts)) and err != "refresh_expired"
        out.append({
            "id": aid,
            "email": email,
            "connected": connected,
            "has_refresh_token": has_refresh,
            "expires_at": exp,
            "auth_error": err,
        })
    return out


def _register_account_in_env(env: dict[str, str], account_id: str) -> None:
    aid = sanitize_account_id(account_id)
    existing = [sanitize_account_id(x) for x in (env.get("GCAL_ACCOUNTS") or "").split(",") if x.strip()]
    if aid not in existing:
        existing.append(aid)
    env["GCAL_ACCOUNTS"] = ",".join(existing)


def import_client_json(brain: str, json_path: str, account_id: str = "") -> tuple[bool, str]:
    """Import Google Cloud OAuth Desktop/Web client JSON into `brain/_secrets/gcal.env`."""
    p = os.path.expanduser(str(json_path or "").strip())
    if not os.path.isfile(p):
        return False, f"OAuth client JSON not found: {p}"
    try:
        with open(p, encoding="utf-8") as fh:
            raw = json.load(fh)
    except Exception as exc:
        return False, f"Failed to parse client JSON: {exc}"
    block = raw.get("installed") or raw.get("web") or raw
    if not isinstance(block, dict):
        return False, "Invalid Google OAuth client JSON structure."
    cid = str(block.get("client_id") or "").strip()
    csec = str(block.get("client_secret") or "").strip()
    if not cid:
        return False, "Missing `client_id` in OAuth client JSON."
    env = load_gcal_env(brain)
    if account_id:
        env[_acct_key(account_id, "CLIENT_ID")] = cid
        if csec:
            env[_acct_key(account_id, "CLIENT_SECRET")] = csec
    else:
        env["GCAL_CLIENT_ID"] = cid
        if csec:
            env["GCAL_CLIENT_SECRET"] = csec
    saved = save_gcal_env(brain, env)
    return True, saved


def save_client_credentials(
    brain: str,
    client_id: str,
    client_secret: str = "",
    broker_url: str = "",
    account_id: str = "",
) -> str:
    env = load_gcal_env(brain)
    if account_id:
        if client_id:
            env[_acct_key(account_id, "CLIENT_ID")] = client_id.strip()
        if client_secret:
            env[_acct_key(account_id, "CLIENT_SECRET")] = client_secret.strip()
    else:
        if client_id:
            env["GCAL_CLIENT_ID"] = client_id.strip()
        if client_secret:
            env["GCAL_CLIENT_SECRET"] = client_secret.strip()
    if broker_url:
        env["GCAL_BROKER_URL"] = broker_url.strip()
    return save_gcal_env(brain, env)


# ---------------------------------------------------------------------------
# 2. OAuth 2.0 PKCE State & Token Management (Multi-Account)
# ---------------------------------------------------------------------------

def _oauth_state_store_path(brain: str) -> str:
    return os.path.join(brain, "_atlas", "cache", "gcal_oauth_states.json")


def _fallback_google_client_creds(brain: str) -> tuple[str, str]:
    """Reuse existing Google Cloud OAuth Desktop client from `google_drive.env` or `google_health.env` if present."""
    for fname, id_k, sec_k in (
        ("google_drive.env", "GOOGLE_DRIVE_CLIENT_ID", "GOOGLE_DRIVE_CLIENT_SECRET"),
        ("google_health.env", "GOOGLE_HEALTH_CLIENT_ID", "GOOGLE_HEALTH_CLIENT_SECRET"),
    ):
        p = os.path.join(brain, "_secrets", fname)
        if not os.path.isfile(p):
            continue
        vals: dict[str, str] = {}
        try:
            with open(p, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    s = line.strip()
                    if not s or s.startswith("#") or "=" not in s:
                        continue
                    k, v = s.split("=", 1)
                    vals[k.strip()] = v.strip().strip("\"'")
        except OSError:
            continue
        cid = vals.get(id_k, "").strip()
        csec = vals.get(sec_k, "").strip()
        if cid and csec:
            return cid, csec
    return "", ""


def gcal_oauth_config(brain: str, account_id: str = "personal", local_redirect_uri: str = "") -> dict[str, Any]:
    """Resolve OAuth client credentials (per-account override -> global `gcal.env` -> existing Google env -> env vars -> broker)."""
    aid = sanitize_account_id(account_id)
    env = load_gcal_env(brain)
    fb_cid, fb_csec = _fallback_google_client_creds(brain)
    cid = (
        env.get(_acct_key(aid, "CLIENT_ID"))
        or env.get("GCAL_CLIENT_ID")
        or os.environ.get("ATLAS_GCAL_CLIENT_ID")
        or fb_cid
        or ""
    ).strip()
    csec = (
        env.get(_acct_key(aid, "CLIENT_SECRET"))
        or env.get("GCAL_CLIENT_SECRET")
        or os.environ.get("ATLAS_GCAL_CLIENT_SECRET")
        or (fb_csec if cid == fb_cid else "")
        or ""
    ).strip()
    broker = (
        env.get("GCAL_BROKER_URL")
        or os.environ.get("ATLAS_GCAL_BROKER_URL")
        or ""
    ).strip()
    redir = (
        env.get("GCAL_REDIRECT_URI")
        or os.environ.get("ATLAS_GCAL_REDIRECT_URI")
        or local_redirect_uri
    ).strip()
    login_hint = (env.get(_acct_key(aid, "EMAIL")) or env.get(_acct_key(aid, "LOGIN_HINT")) or "").strip()
    return {
        "account_id": aid,
        "configured": bool((cid and csec) or (cid and broker) or cid),
        "use_broker": bool(broker and not csec),
        "client_id": cid,
        "client_secret": csec,
        "broker_url": broker,
        "redirect_uri": redir,
        "login_hint": login_hint,
    }


def mint_gcal_oauth_state(brain: str, account_id: str = "personal", port: int = 8765) -> tuple[str, str, str]:
    """Mint CSRF state + PKCE (`S256`) `code_verifier` & `code_challenge` with 30-min TTL (`0o600`)."""
    aid = sanitize_account_id(account_id)
    eff_port = port if 1024 <= int(port) <= 65535 else 8765
    state = f"atlas-gcal:{aid}:{eff_port}:{secrets.token_urlsafe(20)}"
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    p = _oauth_state_store_path(brain)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    now_ts = int(time.time())
    store: dict[str, dict[str, Any]] = {}
    if os.path.isfile(p):
        try:
            with open(p, encoding="utf-8") as fh:
                raw = json.load(fh)
            if isinstance(raw, dict):
                store = {
                    str(k): v for k, v in raw.items()
                    if isinstance(v, dict) and now_ts - int(v.get("ts") or 0) < 1800
                }
        except Exception:
            store = {}
    store[state] = {"ts": now_ts, "verifier": verifier, "port": eff_port, "account_id": aid}
    if len(store) > 16:
        store = dict(sorted(store.items(), key=lambda kv: int(kv[1].get("ts") or 0))[-16:])
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, json.dumps(store).encode("utf-8"))
    finally:
        os.close(fd)
    return state, verifier, challenge


def consume_gcal_oauth_state(brain: str, state: str | None) -> tuple[bool, str, str]:
    """Verify & consume a minted OAuth state; returns `(ok, account_id, code_verifier)`."""
    cand = str(state or "").strip()
    if not cand:
        return False, "", ""
    p = _oauth_state_store_path(brain)
    if not os.path.isfile(p):
        return False, "", ""
    now_ts = int(time.time())
    try:
        with open(p, encoding="utf-8") as fh:
            raw = json.load(fh)
        if not isinstance(raw, dict):
            return False, "", ""
    except Exception:
        return False, "", ""
    matched = False
    account_id = ""
    verifier = ""
    remaining: dict[str, dict[str, Any]] = {}
    for k, entry in raw.items():
        if not isinstance(entry, dict):
            continue
        if now_ts - int(entry.get("ts") or 0) >= 1800:
            continue
        if secrets.compare_digest(str(k), cand):
            matched = True
            account_id = sanitize_account_id(str(entry.get("account_id") or "personal"))
            verifier = str(entry.get("verifier") or "")
        else:
            remaining[str(k)] = entry
    if matched:
        try:
            fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.write(fd, json.dumps(remaining).encode("utf-8"))
            finally:
                os.close(fd)
        except OSError:
            pass
    return matched, account_id, verifier


def build_gcal_authorize_url(
    client_id: str,
    redirect_uri: str,
    state: str,
    code_challenge: str,
    login_hint: str = "",
) -> str:
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(GCAL_SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    if login_hint:
        params["login_hint"] = login_hint.strip()
    return f"{GCAL_AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"


def _default_http(
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes | None,
    timeout: float,
) -> tuple[int, dict[str, Any]]:
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            if not raw.strip():
                return resp.status, {}
            try:
                return resp.status, json.loads(raw)
            except json.JSONDecodeError:
                return resp.status, {"raw": raw}
    except urllib.error.HTTPError as he:
        err_raw = he.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(err_raw) if err_raw.strip() else {}
        except json.JSONDecodeError:
            parsed = {"error": err_raw[:400]}
        return he.code, parsed


def gcal_exchange_code(
    brain: str,
    account_id: str,
    code: str,
    redirect_uri: str,
    code_verifier: str = "",
    client_id: str | None = None,
    client_secret: str | None = None,
    http_fn: HttpTransport | None = None,
) -> tuple[bool, str]:
    """Exchange an authorization code for access + refresh tokens and persist them under `account_id`."""
    aid = sanitize_account_id(account_id)
    cfg = gcal_oauth_config(brain, account_id=aid, local_redirect_uri=redirect_uri)
    cid = (client_id or cfg["client_id"] or "").strip()
    csec = (client_secret or cfg["client_secret"] or "").strip()
    broker = (cfg["broker_url"] or "").strip()
    transport = http_fn or _default_http

    if cid and (csec or not broker):
        form: dict[str, str] = {
            "grant_type": "authorization_code",
            "code": code.strip(),
            "client_id": cid,
            "redirect_uri": redirect_uri,
        }
        if csec:
            form["client_secret"] = csec
        if code_verifier:
            form["code_verifier"] = code_verifier
        status, data = transport(
            "POST",
            GCAL_TOKEN_URL,
            {"Content-Type": "application/x-www-form-urlencoded"},
            urllib.parse.urlencode(form).encode("utf-8"),
            REQUEST_TIMEOUT,
        )
    elif broker:
        body: dict[str, Any] = {
            "grant_type": "authorization_code",
            "code": code.strip(),
            "redirect_uri": redirect_uri,
        }
        if code_verifier:
            body["code_verifier"] = code_verifier
        status, data = transport(
            "POST",
            broker,
            {"Content-Type": "application/json", "User-Agent": "AtlasCalSync/1.6"},
            json.dumps(body).encode("utf-8"),
            REQUEST_TIMEOUT,
        )
    else:
        return False, "Google Calendar OAuth credentials missing in `brain/_secrets/gcal.env`."

    if status >= 400 or not isinstance(data, dict):
        return False, f"Google Calendar OAuth token exchange failed (HTTP {status}): {str(data)[:300]}"
    access = str(data.get("access_token") or "").strip()
    if not access:
        return False, "Google Calendar OAuth response did not include `access_token`."

    # Resolve account email for clear UX & sender identification
    email = ""
    try:
        u_st, u_data = transport(
            "GET",
            GCAL_USERINFO_URL,
            {"Authorization": f"Bearer {access}"},
            None,
            10,
        )
        if u_st == 200 and isinstance(u_data, dict):
            email = str(u_data.get("email") or "").strip()
    except Exception:
        pass
    if not email:
        try:
            c_st, c_data = transport(
                "GET",
                f"{GCAL_API_BASE}/calendars/primary",
                {"Authorization": f"Bearer {access}"},
                None,
                10,
            )
            if c_st == 200 and isinstance(c_data, dict) and "@" in str(c_data.get("id") or ""):
                email = str(c_data["id"]).strip()
        except Exception:
            pass

    now_ts = int(time.time())
    env = load_gcal_env(brain)
    if cid and not env.get("GCAL_CLIENT_ID"):
        env["GCAL_CLIENT_ID"] = cid
    if csec and not env.get("GCAL_CLIENT_SECRET"):
        env["GCAL_CLIENT_SECRET"] = csec
    _register_account_in_env(env, aid)
    env[_acct_key(aid, "ACCESS_TOKEN")] = access
    if data.get("refresh_token"):
        env[_acct_key(aid, "REFRESH_TOKEN")] = str(data["refresh_token"]).strip()
        env[_acct_key(aid, "REFRESH_ISSUED_AT")] = str(now_ts)
    env[_acct_key(aid, "EXPIRES_AT")] = str(now_ts + int(data.get("expires_in") or 3600))
    if data.get("scope"):
        env[_acct_key(aid, "SCOPES")] = str(data["scope"]).strip()
    if email:
        env[_acct_key(aid, "EMAIL")] = email
    env.pop(_acct_key(aid, "AUTH_ERROR"), None)
    saved = save_gcal_env(brain, env)
    return True, f"{saved} ({email or aid})"


def gcal_refresh_if_needed(
    brain: str,
    account_id: str,
    timeout: float = REQUEST_TIMEOUT,
    http_fn: HttpTransport | None = None,
) -> tuple[str, str]:
    """Return `(access_token, auth_error)` — transparently refreshes expired tokens via stored refresh token."""
    aid = sanitize_account_id(account_id)
    env = load_gcal_env(brain)
    access = (env.get(_acct_key(aid, "ACCESS_TOKEN")) or "").strip()
    exp = int(env.get(_acct_key(aid, "EXPIRES_AT")) or 0)
    if access and exp > time.time() + 120:
        return access, ""
    rtok = (env.get(_acct_key(aid, "REFRESH_TOKEN")) or "").strip()
    if not rtok:
        return access, ("refresh_expired" if not access else "")

    cfg = gcal_oauth_config(brain, account_id=aid)
    cid = cfg["client_id"]
    csec = cfg["client_secret"]
    broker = cfg["broker_url"]
    transport = http_fn or _default_http

    try:
        if cid and (csec or not broker):
            form = {"grant_type": "refresh_token", "refresh_token": rtok, "client_id": cid}
            if csec:
                form["client_secret"] = csec
            status, data = transport(
                "POST",
                GCAL_TOKEN_URL,
                {"Content-Type": "application/x-www-form-urlencoded"},
                urllib.parse.urlencode(form).encode("utf-8"),
                timeout,
            )
        elif broker:
            status, data = transport(
                "POST",
                broker,
                {"Content-Type": "application/json", "User-Agent": "AtlasCalSync/1.6"},
                json.dumps({"grant_type": "refresh_token", "refresh_token": rtok}).encode("utf-8"),
                timeout,
            )
        else:
            return access, "missing_client_credentials"
    except Exception:
        return "", "network_error"

    if status < 400 and isinstance(data, dict) and data.get("access_token"):
        now_ts = int(time.time())
        env[_acct_key(aid, "ACCESS_TOKEN")] = str(data["access_token"]).strip()
        if data.get("refresh_token"):
            env[_acct_key(aid, "REFRESH_TOKEN")] = str(data["refresh_token"]).strip()
            env[_acct_key(aid, "REFRESH_ISSUED_AT")] = str(now_ts)
        env[_acct_key(aid, "EXPIRES_AT")] = str(now_ts + int(data.get("expires_in") or 3600))
        env.pop(_acct_key(aid, "AUTH_ERROR"), None)
        save_gcal_env(brain, env)
        return env[_acct_key(aid, "ACCESS_TOKEN")], ""

    err_txt = json.dumps(data).lower() if isinstance(data, dict) else str(data).lower()
    if status in (400, 401) and ("invalid_grant" in err_txt or "expired" in err_txt or "revoked" in err_txt):
        env[_acct_key(aid, "AUTH_ERROR")] = "refresh_expired"
        save_gcal_env(brain, env)
        return "", "refresh_expired"
    return "", f"http_{status}"


# ---------------------------------------------------------------------------
# 3. Provider-Agnostic Base Class & Google Calendar REST API v3 Client
# ---------------------------------------------------------------------------

class CalendarProviderError(RuntimeError):
    """Structured provider error carrying HTTP status and machine-readable code."""

    def __init__(self, message: str, status: int = 0, code: str = ""):
        super().__init__(message)
        self.status = status
        self.code = code


class CalendarProvider:
    """Abstract provider interface so Google Calendar, Apple/CalDAV, etc. share one sync engine."""

    provider_name = "base"

    def list_accounts(self) -> list[dict[str, Any]]:
        raise NotImplementedError

    def list_calendars(self, account_id: str) -> list[dict[str, Any]]:
        raise NotImplementedError

    def create_calendar(self, account_id: str, summary: str, time_zone: str = "UTC") -> dict[str, Any]:
        raise NotImplementedError

    def pull_events(
        self,
        account_id: str,
        calendar_id: str,
        time_min: str = "",
        time_max: str = "",
        sync_token: str = "",
        single_events: bool = True,
    ) -> dict[str, Any]:
        raise NotImplementedError

    def upsert_event(
        self,
        account_id: str,
        calendar_id: str,
        event_body: dict[str, Any],
        event_id: str | None = None,
        send_updates: str = "none",
        conference_data: bool = False,
    ) -> dict[str, Any]:
        raise NotImplementedError

    def delete_event(
        self,
        account_id: str,
        calendar_id: str,
        event_id: str,
        send_updates: str = "none",
    ) -> bool:
        raise NotImplementedError

    def rsvp_event(
        self,
        account_id: str,
        calendar_id: str,
        event_id: str,
        response_status: str,
        send_updates: str = "all",
    ) -> dict[str, Any]:
        raise NotImplementedError

    def freebusy(
        self,
        account_id: str,
        calendar_ids: list[str],
        time_min: str,
        time_max: str,
        time_zone: str = "UTC",
    ) -> dict[str, list[dict[str, str]]]:
        raise NotImplementedError


class GoogleCalendarProvider(CalendarProvider):
    """Pure-stdlib Google Calendar REST API v3 provider."""

    provider_name = "gcal"

    def __init__(
        self,
        brain: str,
        timeout: float = REQUEST_TIMEOUT,
        http_fn: HttpTransport | None = None,
    ):
        self.brain = brain
        self.timeout = float(timeout)
        self._http = http_fn or _default_http

    def list_accounts(self) -> list[dict[str, Any]]:
        return list_configured_accounts(self.brain)

    def _request(
        self,
        account_id: str,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        retry_auth: bool = True,
    ) -> tuple[int, dict[str, Any]]:
        token, err = gcal_refresh_if_needed(
            self.brain, account_id, timeout=self.timeout, http_fn=self._http
        )
        if not token:
            raise CalendarProviderError(
                f"Account '{account_id}' authentication unavailable: {err or 'not_connected'}",
                status=401,
                code=err or "not_connected",
            )
        url = f"{GCAL_API_BASE}{path}"
        if params:
            clean_p = [(k, str(v)) for k, v in params.items() if v is not None and v != ""]
            if clean_p:
                url = f"{url}?{urllib.parse.urlencode(clean_p)}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": "AtlasCalSync/1.6",
        }
        payload: bytes | None = None
        if body is not None:
            headers["Content-Type"] = "application/json; charset=utf-8"
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")

        try:
            status, data = self._http(method, url, headers, payload, self.timeout)
        except Exception as exc:
            raise CalendarProviderError(f"Network error calling Google Calendar API: {exc}", status=0, code="network_error") from exc

        if status == 401 and retry_auth:
            # Force refresh once on unexpected 401
            env = load_gcal_env(self.brain)
            env[_acct_key(account_id, "EXPIRES_AT")] = "0"
            save_gcal_env(self.brain, env)
            return self._request(account_id, method, path, params=params, body=body, retry_auth=False)

        return status, data

    def list_calendars(self, account_id: str) -> list[dict[str, Any]]:
        """List all visible calendars for `account_id` (paginated)."""
        items: list[dict[str, Any]] = []
        page_token = ""
        for _ in range(10):
            params: dict[str, Any] = {"minAccessRole": "freeBusyReader", "maxResults": 250}
            if page_token:
                params["pageToken"] = page_token
            status, data = self._request(account_id, "GET", "/users/me/calendarList", params=params)
            if status >= 400:
                raise CalendarProviderError(
                    f"list_calendars failed for '{account_id}' (HTTP {status}): {str(data)[:250]}",
                    status=status,
                    code="api_error",
                )
            for cal in data.get("items") or []:
                if not isinstance(cal, dict) or not cal.get("id"):
                    continue
                items.append({
                    "id": str(cal["id"]),
                    "summary": str(cal.get("summaryOverride") or cal.get("summary") or cal["id"]),
                    "primary": bool(cal.get("primary")),
                    "access_role": str(cal.get("accessRole") or "reader"),
                    "background_color": str(cal.get("backgroundColor") or ""),
                    "time_zone": str(cal.get("timeZone") or ""),
                    "selected": bool(cal.get("selected", True)),
                    "hidden": bool(cal.get("hidden", False)),
                })
            page_token = str(data.get("nextPageToken") or "")
            if not page_token:
                break
        return items

    def create_calendar(self, account_id: str, summary: str, time_zone: str = "UTC") -> dict[str, Any]:
        """Create a secondary Google Calendar (e.g. `Atlas Training`, `Atlas Tasks & Fristen`)."""
        body = {"summary": summary.strip(), "timeZone": time_zone or "UTC"}
        status, data = self._request(account_id, "POST", "/calendars", body=body)
        if status >= 400 or not data.get("id"):
            raise CalendarProviderError(
                f"create_calendar '{summary}' failed (HTTP {status}): {str(data)[:250]}",
                status=status,
                code="api_error",
            )
        return {
            "id": str(data["id"]),
            "summary": str(data.get("summary") or summary),
            "time_zone": str(data.get("timeZone") or time_zone),
            "access_role": "owner",
            "primary": False,
        }

    def pull_events(
        self,
        account_id: str,
        calendar_id: str,
        time_min: str = "",
        time_max: str = "",
        sync_token: str = "",
        single_events: bool = True,
    ) -> dict[str, Any]:
        """Fetch events via incremental `syncToken` (or full window fallback on first run / HTTP 410 Gone)."""
        cal_enc = urllib.parse.quote(calendar_id, safe="")
        used_sync_token = bool(sync_token)
        events: list[dict[str, Any]] = []
        next_sync_token = ""
        page_token = ""

        for _ in range(40):
            params: dict[str, Any] = {"showDeleted": "true", "maxResults": 250}
            if used_sync_token:
                params["syncToken"] = sync_token
            else:
                if single_events:
                    params["singleEvents"] = "true"
                if time_min:
                    params["timeMin"] = time_min
                if time_max:
                    params["timeMax"] = time_max
            if page_token:
                params["pageToken"] = page_token

            status, data = self._request(account_id, "GET", f"/calendars/{cal_enc}/events", params=params)
            if status == 410 and used_sync_token:
                # Sync token expired/invalidated by Google -> transparent full re-sync
                return self.pull_events(
                    account_id,
                    calendar_id,
                    time_min=time_min,
                    time_max=time_max,
                    sync_token="",
                    single_events=single_events,
                )
            if status >= 400:
                raise CalendarProviderError(
                    f"pull_events failed for {account_id}/{calendar_id} (HTTP {status}): {str(data)[:250]}",
                    status=status,
                    code="sync_token_gone" if status == 410 else "api_error",
                )
            for ev in data.get("items") or []:
                if isinstance(ev, dict) and ev.get("id"):
                    events.append(ev)
            page_token = str(data.get("nextPageToken") or "")
            if not page_token:
                next_sync_token = str(data.get("nextSyncToken") or "")
                break

        return {
            "events": events,
            "next_sync_token": next_sync_token,
            "full_sync": not used_sync_token,
        }

    def upsert_event(
        self,
        account_id: str,
        calendar_id: str,
        event_body: dict[str, Any],
        event_id: str | None = None,
        send_updates: str = "none",
        conference_data: bool = False,
    ) -> dict[str, Any]:
        """Create (`POST`) or update (`PATCH`) an event on Google Calendar."""
        cal_enc = urllib.parse.quote(calendar_id, safe="")
        params: dict[str, Any] = {"sendUpdates": send_updates}
        body = dict(event_body)
        if conference_data:
            params["conferenceDataVersion"] = "1"
            body.setdefault(
                "conferenceData",
                {
                    "createRequest": {
                        "requestId": f"atlas-{uuid.uuid4().hex[:16]}",
                        "conferenceSolutionKey": {"type": "hangoutsMeet"},
                    }
                },
            )
        if event_id:
            ev_enc = urllib.parse.quote(event_id, safe="")
            status, data = self._request(
                account_id,
                "PATCH",
                f"/calendars/{cal_enc}/events/{ev_enc}",
                params=params,
                body=body,
            )
        else:
            status, data = self._request(
                account_id,
                "POST",
                f"/calendars/{cal_enc}/events",
                params=params,
                body=body,
            )
        if status >= 400 or not isinstance(data, dict) or not data.get("id"):
            raise CalendarProviderError(
                f"upsert_event failed for {account_id}/{calendar_id} (HTTP {status}): {str(data)[:250]}",
                status=status,
                code="api_error",
            )
        return data

    def delete_event(
        self,
        account_id: str,
        calendar_id: str,
        event_id: str,
        send_updates: str = "none",
    ) -> bool:
        """Delete an event from Google Calendar (treats 404/410 as already deleted)."""
        cal_enc = urllib.parse.quote(calendar_id, safe="")
        ev_enc = urllib.parse.quote(event_id, safe="")
        status, data = self._request(
            account_id,
            "DELETE",
            f"/calendars/{cal_enc}/events/{ev_enc}",
            params={"sendUpdates": send_updates},
        )
        if status in (200, 204, 404, 410):
            return True
        raise CalendarProviderError(
            f"delete_event failed for {account_id}/{calendar_id}/{event_id} (HTTP {status}): {str(data)[:250]}",
            status=status,
            code="api_error",
        )

    def rsvp_event(
        self,
        account_id: str,
        calendar_id: str,
        event_id: str,
        response_status: str,
        send_updates: str = "all",
    ) -> dict[str, Any]:
        """Update the authenticated user's RSVP (`accepted`, `declined`, `tentative`) on an existing event."""
        valid = {"accepted", "declined", "tentative"}
        resp_s = str(response_status or "").strip().lower()
        if resp_s not in valid:
            raise ValueError(f"Invalid RSVP status '{response_status}'; expected one of {sorted(valid)}")
        cal_enc = urllib.parse.quote(calendar_id, safe="")
        ev_enc = urllib.parse.quote(event_id, safe="")
        status, ev = self._request(account_id, "GET", f"/calendars/{cal_enc}/events/{ev_enc}")
        if status >= 400 or not isinstance(ev, dict):
            raise CalendarProviderError(
                f"rsvp_event lookup failed (HTTP {status}): {str(ev)[:250]}",
                status=status,
                code="api_error",
            )
        acct_email = (load_gcal_env(self.brain).get(_acct_key(account_id, "EMAIL")) or "").strip().lower()
        attendees = list(ev.get("attendees") or [])
        updated = False
        for att in attendees:
            if not isinstance(att, dict):
                continue
            if att.get("self") or (acct_email and str(att.get("email") or "").strip().lower() == acct_email):
                att["responseStatus"] = resp_s
                updated = True
                break
        if not updated:
            if acct_email:
                attendees.append({"email": acct_email, "self": True, "responseStatus": resp_s})
            else:
                raise CalendarProviderError(
                    "Could not find authenticated user in event attendees list for RSVP.",
                    status=400,
                    code="self_attendee_missing",
                )
        return self.upsert_event(
            account_id,
            calendar_id,
            {"attendees": attendees},
            event_id=event_id,
            send_updates=send_updates,
        )

    def freebusy(
        self,
        account_id: str,
        calendar_ids: list[str],
        time_min: str,
        time_max: str,
        time_zone: str = "UTC",
    ) -> dict[str, list[dict[str, str]]]:
        """Query Google FreeBusy API for one or more calendars under `account_id`."""
        if not calendar_ids:
            return {}
        body = {
            "timeMin": time_min,
            "timeMax": time_max,
            "timeZone": time_zone or "UTC",
            "items": [{"id": cid} for cid in calendar_ids],
        }
        status, data = self._request(account_id, "POST", "/freeBusy", body=body)
        if status >= 400 or not isinstance(data, dict):
            raise CalendarProviderError(
                f"freebusy failed for '{account_id}' (HTTP {status}): {str(data)[:250]}",
                status=status,
                code="api_error",
            )
        out: dict[str, list[dict[str, str]]] = {}
        cals = data.get("calendars") or {}
        for cid, info in cals.items():
            if isinstance(info, dict):
                out[str(cid)] = [
                    {"start": str(b.get("start") or ""), "end": str(b.get("end") or "")}
                    for b in (info.get("busy") or [])
                    if isinstance(b, dict) and b.get("start") and b.get("end")
                ]
        return out


# ---------------------------------------------------------------------------
# 4. Timezone & RFC3339 Helpers
# ---------------------------------------------------------------------------

def local_timezone_name() -> str:
    """Best-effort IANA timezone name detection (stdlib only)."""
    env_tz = (os.environ.get("TZ") or "").strip().lstrip(":")
    if env_tz and "/" in env_tz:
        return env_tz
    try:
        if os.path.islink("/etc/localtime"):
            target = os.path.realpath("/etc/localtime")
            if "/zoneinfo/" in target:
                return target.split("/zoneinfo/", 1)[1]
        if os.path.isfile("/etc/timezone"):
            with open("/etc/timezone", encoding="utf-8") as fh:
                tz = fh.read().strip()
                if tz and "/" in tz:
                    return tz
    except OSError:
        pass
    try:
        tzinfo = dt.datetime.now().astimezone().tzinfo
        key = getattr(tzinfo, "key", "")
        if key:
            return str(key)
    except Exception:
        pass
    return "UTC"


def to_rfc3339(d: dt.datetime) -> str:
    """Format aware datetime (or naive local datetime) as RFC3339 string."""
    if d.tzinfo is None:
        d = d.astimezone()
    return d.isoformat(timespec="seconds")


def parse_rfc3339(raw: str) -> dt.datetime | None:
    s = str(raw or "").strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(s)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed
    except ValueError:
        return None
