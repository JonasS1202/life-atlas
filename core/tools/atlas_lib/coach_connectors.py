"""Hardware & Wellness Connectors for Atlas Coach (Google Health API v4, Hammerhead Karoo, Garmin Connect, Intervals.icu).

Python 3 standard library only (`base64`, `datetime`, `hashlib`, `json`, `os`, `re`,
`subprocess`, `sys`, `time`, `urllib`, `uuid`). Credentials live exclusively in
`brain/_secrets/{google_health,hammerhead,garmin,intervals}.env` (`0o600`, git-ignored).
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any

from . import fit_writer
from . import health_engine
from . import pillars_engine

# ---------------------------------------------------------------------------
# Google Health API v4 (OAuth 2.0 + PKCE Loopback, Pure-Stdlib REST)
# ---------------------------------------------------------------------------
GOOGLE_HEALTH_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_HEALTH_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_HEALTH_API_BASE = "https://health.googleapis.com/v4/users/me"
GOOGLE_HEALTH_SCOPES = (
    "https://www.googleapis.com/auth/googlehealth.sleep.readonly",
    "https://www.googleapis.com/auth/googlehealth.health_metrics_and_measurements.readonly",
    "https://www.googleapis.com/auth/googlehealth.activity_and_fitness.readonly",
)
GOOGLE_HEALTH_TESTING_TOKEN_TTL_DAYS = 7.0
GOOGLE_HEALTH_RENEW_WARN_DAYS = 6.0

# ---------------------------------------------------------------------------
# Hammerhead Karoo OAuth 2.0 & Workout API Endpoints
# ---------------------------------------------------------------------------
HAMMERHEAD_AUTHORIZE_URL = "https://api.hammerhead.io/v1/auth/oauth/authorize"
HAMMERHEAD_TOKEN_URL = "https://api.hammerhead.io/v1/auth/oauth/token"
HAMMERHEAD_WORKOUTS_URL = "https://api.hammerhead.io/v1/api/workouts"
HAMMERHEAD_SCOPE = "workout:write"

# Default 1-Click OAuth App (stateless loopback relay + token broker; secret stays server-side)
DEFAULT_HAMMERHEAD_CLIENT_ID = "cddac053-588c-4e3f-90e1-30287f58b544"
DEFAULT_HAMMERHEAD_REDIRECT_URI = "https://bikecoa.ch/hammerhead-callback"
DEFAULT_HAMMERHEAD_BROKER_URL = "https://bikecoa.ch/api/atlas/hammerhead/token"

INTERVALS_API_BASE = "https://intervals.icu/api/v1"
REQUEST_TIMEOUT = 25


# ---------------------------------------------------------------------------
# 1. Secret `.env` File Helpers (`brain/_secrets/*.env`, `chmod 600`)
# ---------------------------------------------------------------------------

def secret_env_path(brain: str, service: str) -> str:
    return os.path.join(brain, "_secrets", f"{service}.env")


def load_secret_env(brain: str, service: str) -> dict[str, str]:
    p = secret_env_path(brain, service)
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


def save_secret_env(brain: str, service: str, env_dict: dict[str, str]) -> str:
    sec_dir = os.path.join(brain, "_secrets")
    os.makedirs(sec_dir, exist_ok=True)
    p = secret_env_path(brain, service)
    lines = [f"# Managed by Atlas Coach ({service}) — local only, never committed"]
    for k in sorted(env_dict):
        v = str(env_dict[k] or "").replace("\n", " ").strip()
        if v:
            lines.append(f"{k}={v}")
    content = "\n".join(lines) + "\n"
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, content.encode("utf-8"))
    finally:
        os.close(fd)
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    return p


def google_health_token_status(brain: str) -> dict[str, Any]:
    """Return Google Health OAuth configuration, connection, and 7-day Testing-Mode renew status."""
    gh = load_secret_env(brain, "google_health")
    cid = (gh.get("GOOGLE_HEALTH_CLIENT_ID") or os.environ.get("ATLAS_GOOGLE_HEALTH_CLIENT_ID") or "").strip()
    csec = (gh.get("GOOGLE_HEALTH_CLIENT_SECRET") or os.environ.get("ATLAS_GOOGLE_HEALTH_CLIENT_SECRET") or "").strip()
    broker = (gh.get("GOOGLE_HEALTH_BROKER_URL") or os.environ.get("ATLAS_GOOGLE_HEALTH_BROKER_URL") or "").strip()
    configured = bool((cid and csec) or (cid and broker))
    has_tok = bool(gh.get("GOOGLE_HEALTH_REFRESH_TOKEN") or gh.get("GOOGLE_HEALTH_ACCESS_TOKEN"))
    auth_err = (gh.get("GOOGLE_HEALTH_AUTH_ERROR") or "").strip()
    is_internal = str(gh.get("GOOGLE_HEALTH_WORKSPACE_INTERNAL") or "").strip().lower() in ("1", "true", "yes")
    issued_at = health_engine.safe_int(gh.get("GOOGLE_HEALTH_REFRESH_ISSUED_AT"))
    days_left: float | None = None
    needs_renew = False
    if has_tok:
        if auth_err == "refresh_expired":
            needs_renew = True
            days_left = 0.0
        elif issued_at and not is_internal:
            age_days = max(0.0, (time.time() - float(issued_at)) / 86400.0)
            days_left = round(max(0.0, GOOGLE_HEALTH_TESTING_TOKEN_TTL_DAYS - age_days), 1)
            if age_days >= GOOGLE_HEALTH_RENEW_WARN_DAYS:
                needs_renew = True
    return {
        "configured": configured,
        "connected": has_tok and auth_err != "refresh_expired",
        "has_tokens": has_tok,
        "needs_renew": needs_renew,
        "days_until_testing_expiry": days_left,
        "auth_error": auth_err,
        "login_hint": (gh.get("GOOGLE_HEALTH_LOGIN_HINT") or "").strip(),
    }


def connector_status_summary(brain: str) -> dict[str, Any]:
    """Return connection flags for Strava, Hammerhead Karoo, Google Health, Garmin Connect, and Intervals.icu."""
    strava = load_secret_env(brain, "strava")
    hh = load_secret_env(brain, "hammerhead")
    garmin = load_secret_env(brain, "garmin")
    icu = load_secret_env(brain, "intervals")
    gh_st = google_health_token_status(brain)
    return {
        "strava_connected": bool(strava.get("STRAVA_REFRESH_TOKEN") or strava.get("STRAVA_ACCESS_TOKEN")),
        "hammerhead_connected": bool(hh.get("HAMMERHEAD_REFRESH_TOKEN") or hh.get("HAMMERHEAD_ACCESS_TOKEN")),
        "hammerhead_configured": True,
        "google_health_configured": gh_st["configured"],
        "google_health_connected": gh_st["connected"],
        "google_health_needs_renew": gh_st["needs_renew"],
        "google_health_days_left": gh_st["days_until_testing_expiry"],
        "google_health_auth_error": gh_st["auth_error"],
        "garmin_connected": bool(garmin.get("GARMIN_TOKENS_B64") or garmin.get("GARMIN_ACCESS_TOKEN")),
        "intervals_connected": bool(icu.get("INTERVALS_API_KEY") and icu.get("INTERVALS_ATHLETE_ID")),
    }


# ---------------------------------------------------------------------------
# 2. Hammerhead Karoo Direct Integration (Pure-Stdlib 1-Click OAuth + Multipart)
# ---------------------------------------------------------------------------

def _hh_state_store_path(brain: str) -> str:
    return os.path.join(brain, "_atlas", "cache", "hh_oauth_states.json")


def mint_hammerhead_oauth_state(brain: str, port: int = 8765) -> str:
    """Mint a CSRF-safe loopback OAuth state (`atlas:<port>:<nonce>`) with 30-min TTL."""
    eff_port = port if 8760 <= int(port) <= 8799 else 8765
    state = f"atlas:{eff_port}:{secrets.token_urlsafe(20)}"
    p = _hh_state_store_path(brain)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    now_ts = int(time.time())
    store: dict[str, int] = {}
    if os.path.isfile(p):
        try:
            with open(p, encoding="utf-8") as fh:
                raw = json.load(fh)
            if isinstance(raw, dict):
                store = {str(k): int(v) for k, v in raw.items() if now_ts - int(v) < 1800}
        except Exception:
            store = {}
    store[state] = now_ts
    # Keep at most 12 recent nonces
    if len(store) > 12:
        store = dict(sorted(store.items(), key=lambda kv: kv[1])[-12:])
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, json.dumps(store).encode("utf-8"))
    finally:
        os.close(fd)
    return state


def verify_hammerhead_oauth_state(brain: str, state: str | None) -> bool:
    """Verify and consume a previously minted Hammerhead OAuth state nonce."""
    cand = str(state or "").strip()
    if not cand:
        return False
    p = _hh_state_store_path(brain)
    if not os.path.isfile(p):
        return False
    now_ts = int(time.time())
    try:
        with open(p, encoding="utf-8") as fh:
            raw = json.load(fh)
        if not isinstance(raw, dict):
            return False
    except Exception:
        return False
    matched = False
    remaining: dict[str, int] = {}
    for k, ts_val in raw.items():
        try:
            age = now_ts - int(ts_val)
        except Exception:
            continue
        if age >= 1800:
            continue
        if secrets.compare_digest(str(k), cand):
            matched = True
        else:
            remaining[str(k)] = int(ts_val)
    if matched:
        try:
            fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.write(fd, json.dumps(remaining).encode("utf-8"))
            finally:
                os.close(fd)
        except OSError:
            pass
    return matched


def hammerhead_oauth_config(brain: str, local_redirect_uri: str = "") -> dict[str, Any]:
    """Resolve effective Hammerhead OAuth configuration (1-Click default or custom developer override)."""
    env = load_secret_env(brain, "hammerhead")
    cid_custom = (env.get("HAMMERHEAD_CLIENT_ID") or os.environ.get("ATLAS_HAMMERHEAD_CLIENT_ID") or "").strip()
    csec_custom = (env.get("HAMMERHEAD_CLIENT_SECRET") or os.environ.get("ATLAS_HAMMERHEAD_CLIENT_SECRET") or "").strip()
    if cid_custom and csec_custom:
        return {
            "custom": True,
            "client_id": cid_custom,
            "client_secret": csec_custom,
            "redirect_uri": (env.get("HAMMERHEAD_REDIRECT_URI") or local_redirect_uri).strip(),
            "broker_url": "",
        }
    return {
        "custom": False,
        "client_id": DEFAULT_HAMMERHEAD_CLIENT_ID,
        "client_secret": "",
        "redirect_uri": os.environ.get("ATLAS_HAMMERHEAD_REDIRECT_URI") or DEFAULT_HAMMERHEAD_REDIRECT_URI,
        "broker_url": os.environ.get("ATLAS_HAMMERHEAD_BROKER_URL") or DEFAULT_HAMMERHEAD_BROKER_URL,
    }


def _encode_multipart_file(field_name: str, filename: str, file_bytes: bytes, content_type: str = "application/xml") -> tuple[bytes, str]:
    """Encode a single file as RFC 7578 `multipart/form-data` using Python stdlib only."""
    boundary = f"----AtlasCoachBoundary{uuid.uuid4().hex}"
    safe_fn = re.sub(r'["\\\r\n]', "_", filename or "Atlas_Workout.zwo")
    head = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{field_name}"; filename="{safe_fn}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode("utf-8")
    tail = f"\r\n--{boundary}--\r\n".encode("utf-8")
    return head + file_bytes + tail, f"multipart/form-data; boundary={boundary}"


def build_hammerhead_authorize_url(client_id: str, redirect_uri: str, state: str = "atlas-coach") -> str:
    return (
        f"{HAMMERHEAD_AUTHORIZE_URL}"
        f"?client_id={urllib.parse.quote(client_id, safe='')}"
        f"&response_type=code"
        f"&redirect_uri={urllib.parse.quote(redirect_uri, safe='')}"
        f"&scope={urllib.parse.quote(HAMMERHEAD_SCOPE, safe='')}"
        f"&state={urllib.parse.quote(state or 'atlas-coach', safe='')}"
    )


def hammerhead_exchange_code(
    brain: str,
    code: str,
    redirect_uri: str = "",
    client_id: str | None = None,
    client_secret: str | None = None,
) -> tuple[bool, str]:
    env = load_secret_env(brain, "hammerhead")
    cid = (client_id or env.get("HAMMERHEAD_CLIENT_ID") or os.environ.get("ATLAS_HAMMERHEAD_CLIENT_ID") or "").strip()
    csec = (client_secret or env.get("HAMMERHEAD_CLIENT_SECRET") or os.environ.get("ATLAS_HAMMERHEAD_CLIENT_SECRET") or "").strip()

    if cid and csec:
        eff_redir = redirect_uri or env.get("HAMMERHEAD_REDIRECT_URI") or "http://127.0.0.1:8765/hammerhead-callback"
        payload = urllib.parse.urlencode({
            "grant_type": "authorization_code",
            "code": code,
            "client_id": cid,
            "client_secret": csec,
            "redirect_uri": eff_redir,
        }).encode("utf-8")
        req = urllib.request.Request(
            HAMMERHEAD_TOKEN_URL,
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
    else:
        broker_url = os.environ.get("ATLAS_HAMMERHEAD_BROKER_URL") or DEFAULT_HAMMERHEAD_BROKER_URL
        payload = json.dumps({
            "grant_type": "authorization_code",
            "code": code,
        }).encode("utf-8")
        req = urllib.request.Request(
            broker_url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "AtlasCoach/1.4",
            },
            method="POST",
        )

    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception as exc:
        return False, f"Hammerhead OAuth token exchange failed: {exc}"
    access = str(data.get("access_token") or "").strip()
    if not access:
        return False, "Hammerhead OAuth response did not include access_token."
    if cid and csec:
        env["HAMMERHEAD_CLIENT_ID"] = cid
        env["HAMMERHEAD_CLIENT_SECRET"] = csec
    env["HAMMERHEAD_ACCESS_TOKEN"] = access
    if data.get("refresh_token"):
        env["HAMMERHEAD_REFRESH_TOKEN"] = str(data["refresh_token"]).strip()
    if data.get("expires_at"):
        env["HAMMERHEAD_EXPIRES_AT"] = str(int(data["expires_at"]))
    else:
        env["HAMMERHEAD_EXPIRES_AT"] = str(int(time.time()) + int(data.get("expires_in") or 3600))
    p = save_secret_env(brain, "hammerhead", env)
    return True, p


def hammerhead_refresh_if_needed(brain: str, env_dict: dict[str, str] | None = None) -> str:
    env = dict(env_dict) if env_dict is not None else load_secret_env(brain, "hammerhead")
    exp = int(env.get("HAMMERHEAD_EXPIRES_AT") or 0)
    if env.get("HAMMERHEAD_ACCESS_TOKEN") and (exp == 0 or exp > time.time() + 120):
        return env["HAMMERHEAD_ACCESS_TOKEN"]
    cid = (env.get("HAMMERHEAD_CLIENT_ID") or os.environ.get("ATLAS_HAMMERHEAD_CLIENT_ID") or "").strip()
    csec = (env.get("HAMMERHEAD_CLIENT_SECRET") or os.environ.get("ATLAS_HAMMERHEAD_CLIENT_SECRET") or "").strip()
    rtok = (env.get("HAMMERHEAD_REFRESH_TOKEN") or "").strip()
    if not rtok:
        return env.get("HAMMERHEAD_ACCESS_TOKEN", "")
    if cid and csec:
        payload = urllib.parse.urlencode({
            "grant_type": "refresh_token",
            "refresh_token": rtok,
            "client_id": cid,
            "client_secret": csec,
        }).encode("utf-8")
        req = urllib.request.Request(
            HAMMERHEAD_TOKEN_URL,
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
    else:
        broker_url = os.environ.get("ATLAS_HAMMERHEAD_BROKER_URL") or DEFAULT_HAMMERHEAD_BROKER_URL
        payload = json.dumps({
            "grant_type": "refresh_token",
            "refresh_token": rtok,
        }).encode("utf-8")
        req = urllib.request.Request(
            broker_url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "AtlasCoach/1.4",
            },
            method="POST",
        )
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        if data.get("access_token"):
            env["HAMMERHEAD_ACCESS_TOKEN"] = str(data["access_token"]).strip()
            if data.get("refresh_token"):
                env["HAMMERHEAD_REFRESH_TOKEN"] = str(data["refresh_token"]).strip()
            if data.get("expires_at"):
                env["HAMMERHEAD_EXPIRES_AT"] = str(int(data["expires_at"]))
            else:
                env["HAMMERHEAD_EXPIRES_AT"] = str(int(time.time()) + int(data.get("expires_in") or 3600))
            save_secret_env(brain, "hammerhead", env)
            return env["HAMMERHEAD_ACCESS_TOKEN"]
    except Exception:
        pass
    return env.get("HAMMERHEAD_ACCESS_TOKEN", "")


def hammerhead_push_or_update(
    brain: str,
    zwo_xml: str,
    filename: str,
    planned_date: str | None = None,
    existing_workout_id: str | None = None,
    mock_dir: str | None = None,
) -> tuple[bool, str, str | None]:
    """Create (`POST`) or update (`PUT`) a `.zwo` workout on Hammerhead Karoo."""
    if mock_dir:
        os.makedirs(mock_dir, exist_ok=True)
        wid = existing_workout_id or f"hh.mock.{hashlib.sha1(f'{planned_date}:{filename}'.encode('utf-8')).hexdigest()[:8]}"
        action = "PUT" if existing_workout_id else "POST"
        rec_path = os.path.join(mock_dir, "hammerhead_ops.jsonl")
        with open(rec_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"op": action, "id": wid, "date": planned_date, "filename": filename}) + "\n")
        with open(os.path.join(mock_dir, f"{wid}.zwo"), "w", encoding="utf-8") as fh:
            fh.write(zwo_xml)
        return True, f"Hammerhead mock {action} -> {wid}", wid

    token = hammerhead_refresh_if_needed(brain)
    if not token:
        return False, "Hammerhead not connected.", None

    body_bytes, content_type = _encode_multipart_file("file", filename, zwo_xml.encode("utf-8"), "application/xml")
    qs = f"?plannedDate={urllib.parse.quote(planned_date, safe='')}" if planned_date else ""

    def _do_req(method: str, url: str) -> tuple[int, dict[str, Any]]:
        req = urllib.request.Request(
            url,
            data=body_bytes,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": content_type,
            },
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                return resp.status, (json.loads(raw) if raw.strip().startswith("{") else {})
        except urllib.error.HTTPError as he:
            return he.code, {}
        except Exception:
            return 0, {}

    if existing_workout_id:
        put_url = f"{HAMMERHEAD_WORKOUTS_URL}/{urllib.parse.quote(existing_workout_id, safe='')}/file{qs}"
        st_code, payload = _do_req("PUT", put_url)
        if st_code in (200, 201, 204):
            wid = str(payload.get("id") or existing_workout_id)
            return True, f"Updated Hammerhead workout {wid}", wid
        if st_code not in (404, 410):
            return False, f"Hammerhead PUT failed (HTTP {st_code})", existing_workout_id

    post_url = f"{HAMMERHEAD_WORKOUTS_URL}/file{qs}"
    st_code, payload = _do_req("POST", post_url)
    if st_code in (200, 201):
        wid = str(payload.get("id") or payload.get("workoutId") or "")
        return True, f"Pushed Hammerhead workout {wid or filename}", (wid or None)
    return False, f"Hammerhead POST failed (HTTP {st_code})", None


def hammerhead_delete_workout(brain: str, workout_id: str, mock_dir: str | None = None) -> tuple[bool, str]:
    if not workout_id:
        return True, "No workout ID."
    if mock_dir:
        os.makedirs(mock_dir, exist_ok=True)
        rec_path = os.path.join(mock_dir, "hammerhead_ops.jsonl")
        with open(rec_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"op": "DELETE", "id": workout_id}) + "\n")
        z_p = os.path.join(mock_dir, f"{workout_id}.zwo")
        if os.path.isfile(z_p):
            os.remove(z_p)
        return True, f"Hammerhead mock DELETE -> {workout_id}"

    token = hammerhead_refresh_if_needed(brain)
    if not token:
        return False, "Hammerhead not connected."
    url = f"{HAMMERHEAD_WORKOUTS_URL}/{urllib.parse.quote(workout_id, safe='')}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"}, method="DELETE")
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT):
            return True, f"Deleted Hammerhead workout {workout_id}"
    except urllib.error.HTTPError as he:
        if he.code in (200, 204, 404):
            return True, f"Hammerhead workout {workout_id} already absent/deleted"
        return False, f"Hammerhead DELETE failed (HTTP {he.code})"
    except Exception as exc:
        return False, f"Hammerhead DELETE error: {exc}"


# ---------------------------------------------------------------------------
# 3. Garmin Connect Integration (Wellness Biometrics Pull + Structured Workouts)
# ---------------------------------------------------------------------------

def _load_garmin_library(brain: str, auto_install: bool = False) -> Any:
    """Import optional `garminconnect` from current Python or isolated `brain/_atlas/cache/pydeps`."""
    pydeps = os.path.join(brain, "_atlas", "cache", "pydeps")
    if os.path.isdir(pydeps) and pydeps not in sys.path:
        sys.path.insert(0, pydeps)
    try:
        import garminconnect  # type: ignore
        return garminconnect
    except ImportError:
        if not auto_install:
            return None
    try:
        os.makedirs(pydeps, exist_ok=True)
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "--quiet", "--target", pydeps, "garminconnect"],
            capture_output=True,
            text=True,
            timeout=90,
            check=True,
        )
        if pydeps not in sys.path:
            sys.path.insert(0, pydeps)
        import garminconnect  # type: ignore
        return garminconnect
    except Exception:
        return None


def garmin_authenticate(
    brain: str,
    email: str = "",
    password: str = "",
    mfa_code: str = "",
    tokens_json: str = "",
    install_helper: bool = True,
) -> tuple[bool, str]:
    """Authenticate Garmin Connect and persist session tokens ONLY (never plaintext password!) to `_secrets/garmin.env`."""
    env = load_secret_env(brain, "garmin")
    if tokens_json:
        env["GARMIN_TOKENS_B64"] = base64.b64encode(tokens_json.encode("utf-8")).decode("ascii")
        if email:
            env["GARMIN_EMAIL"] = email.strip()
        p = save_secret_env(brain, "garmin", env)
        return True, f"Saved Garmin session tokens to {p}"

    gc_mod = _load_garmin_library(brain, auto_install=install_helper)
    if gc_mod is None:
        return (
            False,
            "Optional `garminconnect` helper could not be loaded. Offline `.fit` workouts still work out-of-the-box, "
            "or connect via `intervals.icu` (`atlas.py coach auth-intervals`).",
        )
    if not (email and password):
        return False, "Provide `--email` and `--password` (discarded immediately after token exchange) or `--tokens-json`."

    state_file = os.path.join(brain, "_atlas", "cache", "garmin_mfa_state.pkl")
    try:
        client = gc_mod.Garmin(email=email.strip(), password=password, return_on_mfa=True)
        if mfa_code and os.path.isfile(state_file):
            import pickle
            with open(state_file, "rb") as fh:
                saved_state = pickle.load(fh)
            client.resume_login(saved_state, mfa_code.strip())
            try:
                os.remove(state_file)
            except OSError:
                pass
        else:
            status, mfa_state = client.login()
            if status == "needs_mfa":
                import pickle
                os.makedirs(os.path.dirname(state_file), exist_ok=True)
                with open(state_file, "wb") as fh:
                    pickle.dump(mfa_state, fh)
                return False, "MFA_REQUIRED: Re-run with `--mfa-code <6-digit-code>` to finish Garmin login."

        dumped = client.garth.dumps() if hasattr(client, "garth") else client.client.dumps()
        env["GARMIN_EMAIL"] = email.strip()
        env["GARMIN_TOKENS_B64"] = base64.b64encode(dumped.encode("utf-8")).decode("ascii")
        p = save_secret_env(brain, "garmin", env)
        return True, f"Connected Garmin Connect ({email.strip()}) -> {p}"
    except Exception as exc:
        return False, f"Garmin login failed: {exc}"


def _get_garmin_client(brain: str) -> Any:
    env = load_secret_env(brain, "garmin")
    b64 = env.get("GARMIN_TOKENS_B64")
    if not b64:
        return None
    gc_mod = _load_garmin_library(brain, auto_install=False)
    if gc_mod is None:
        return None
    try:
        tok_str = base64.b64decode(b64.encode("ascii")).decode("utf-8")
        client = gc_mod.Garmin()
        client.login(tok_str)
        return client
    except Exception:
        return None


def _sleep_score_to_quality(score: float | int | None) -> str | None:
    if score is None:
        return None
    sc = float(score)
    if sc >= 80:
        return "good"
    if sc >= 62:
        return "fair"
    if sc >= 45:
        return "poor"
    return "terrible"


def garmin_pull_wellness(
    brain: str,
    days: int = 7,
    mock_file: str | None = None,
    today: dt.date | None = None,
) -> list[dict[str, Any]]:
    """Pull daily RHR, sleep hours/score, overnight HRV (RMSSD ms), Body Battery, and weight from Garmin Connect."""
    ref_today = today or dt.date.today()
    rows: list[dict[str, Any]] = []

    if mock_file and os.path.isfile(mock_file):
        try:
            with open(mock_file, encoding="utf-8") as fh:
                raw = json.load(fh)
            items = raw if isinstance(raw, list) else (raw.get("wellness") or [])
            for it in items:
                if not isinstance(it, dict) or not it.get("date"):
                    continue
                sq = it.get("sleep_quality") or _sleep_score_to_quality(it.get("sleep_score"))
                rows.append({
                    "date": str(it["date"])[:10],
                    "rhr": int(it["rhr"]) if it.get("rhr") is not None else None,
                    "sleep_h": round(float(it["sleep_h"]), 2) if it.get("sleep_h") is not None else None,
                    "sleep_quality": sq,
                    "hrv_rmssd": round(float(it["hrv_rmssd"]), 1) if it.get("hrv_rmssd") is not None else None,
                    "body_battery": int(it["body_battery"]) if it.get("body_battery") is not None else None,
                    "weight_kg": round(float(it["weight_kg"]), 2) if it.get("weight_kg") is not None else None,
                    "source": "garmin",
                })
            return rows
        except Exception:
            return []

    client = _get_garmin_client(brain)
    if client is None:
        return []

    for offset in range(max(1, min(days, 28))):
        d_iso = (ref_today - dt.timedelta(days=offset)).isoformat()
        rec: dict[str, Any] = {"date": d_iso, "source": "garmin"}
        try:
            stats = client.get_stats(d_iso) or {}
            if stats.get("restingHeartRate"):
                rec["rhr"] = int(round(float(stats["restingHeartRate"])))
            if stats.get("bodyBatteryHighestValue"):
                rec["body_battery"] = int(round(float(stats["bodyBatteryHighestValue"])))
            if stats.get("weight"):
                w_g = float(stats["weight"])
                rec["weight_kg"] = round(w_g / 1000.0 if w_g > 300 else w_g, 2)
        except Exception:
            pass
        try:
            slp = client.get_sleep_data(d_iso) or {}
            dto = slp.get("dailySleepDTO") or {}
            secs = dto.get("sleepTimeSeconds")
            if secs and float(secs) > 0:
                rec["sleep_h"] = round(float(secs) / 3600.0, 2)
            sc_obj = (dto.get("sleepScores") or {}).get("overall") or {}
            sc_val = sc_obj.get("value") if isinstance(sc_obj, dict) else None
            if sc_val is not None:
                rec["sleep_quality"] = _sleep_score_to_quality(sc_val)
        except Exception:
            pass
        try:
            if hasattr(client, "get_hrv_data"):
                hrv = client.get_hrv_data(d_iso) or {}
                h_sum = hrv.get("hrvSummary") or {}
                rmssd = h_sum.get("lastNightAvg") or h_sum.get("weeklyAvg")
                if rmssd:
                    rec["hrv_rmssd"] = round(float(rmssd), 1)
        except Exception:
            pass
        if any(rec.get(k) is not None for k in ("rhr", "sleep_h", "hrv_rmssd", "weight_kg")):
            rows.append(rec)
    return rows


def build_garmin_workout_payload(
    title: str,
    sport: str,
    parsed_spec: dict[str, Any] | None,
    ftp_watts: int = 275,
    cs_mps: float | None = None,
    description: str = "Atlas Coach structured workout.",
) -> dict[str, Any]:
    """Build Garmin Connect `/workout-service/workout` JSON DTO for Cycling or Running."""
    sp_low = str(sport or "cycling").lower()
    is_run = sp_low in ("run", "running", "trailrun")
    sp_id = 1 if is_run else 2
    sp_key = "running" if is_run else "cycling"
    fit_steps = fit_writer._expand_parsed_blocks_to_fit_steps(
        parsed_spec=parsed_spec,
        sport=sp_low,
        ftp_watts=ftp_watts,
        cs_mps=cs_mps,
    )
    step_map = {
        fit_writer.FIT_INTENSITY_WARMUP: (1, "warmup"),
        fit_writer.FIT_INTENSITY_COOLDOWN: (2, "cooldown"),
        fit_writer.FIT_INTENSITY_ACTIVE: (3, "interval"),
        fit_writer.FIT_INTENSITY_RECOVERY: (4, "recovery"),
        fit_writer.FIT_INTENSITY_REST: (5, "rest"),
    }
    w_steps: list[dict[str, Any]] = []
    tot_secs = 0
    for idx, st in enumerate(fit_steps, start=1):
        dur_s = max(5, int(round(st["duration_ms"] / 1000.0)))
        tot_secs += dur_s
        st_id, st_key = step_map.get(st["intensity"], (3, "interval"))
        dto: dict[str, Any] = {
            "type": "ExecutableStepDTO",
            "stepOrder": idx,
            "stepType": {"stepTypeId": st_id, "stepTypeKey": st_key, "displayOrder": st_id},
            "endCondition": {"conditionTypeId": 2, "conditionTypeKey": "time", "displayOrder": 2, "displayable": True},
            "endConditionValue": float(dur_s),
            "description": st["name"],
        }
        if st["target_type"] == fit_writer.FIT_TGT_POWER and st["custom_low"] != fit_writer.UINT32_INVALID:
            dto["targetType"] = {"workoutTargetTypeId": 2, "workoutTargetTypeKey": "power.zone", "displayOrder": 2}
            dto["targetValueOne"] = float(st["custom_low"] - 1000)
            dto["targetValueTwo"] = float(st["custom_high"] - 1000)
        elif st["target_type"] == fit_writer.FIT_TGT_SPEED and st["custom_low"] != fit_writer.UINT32_INVALID:
            dto["targetType"] = {"workoutTargetTypeId": 6, "workoutTargetTypeKey": "pace.zone", "displayOrder": 6}
            dto["targetValueOne"] = round(st["custom_low"] / 1000.0, 3)
            dto["targetValueTwo"] = round(st["custom_high"] / 1000.0, 3)
        else:
            dto["targetType"] = {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target", "displayOrder": 1}
        w_steps.append(dto)

    return {
        "workoutName": (title or "Atlas Workout")[:75],
        "description": description,
        "sportType": {"sportTypeId": sp_id, "sportTypeKey": sp_key, "displayOrder": sp_id},
        "estimatedDurationInSecs": tot_secs,
        "workoutSegments": [{
            "segmentOrder": 1,
            "sportType": {"sportTypeId": sp_id, "sportTypeKey": sp_key, "displayOrder": sp_id},
            "workoutSteps": w_steps,
        }],
    }


def garmin_push_or_update(
    brain: str,
    payload: dict[str, Any],
    planned_date: str | None = None,
    existing_workout_id: str | None = None,
    mock_dir: str | None = None,
) -> tuple[bool, str, str | None]:
    if mock_dir:
        os.makedirs(mock_dir, exist_ok=True)
        if existing_workout_id:
            garmin_delete_workout(brain, existing_workout_id, mock_dir=mock_dir)
        w_name = payload.get("workoutName")
        wid = f"gc.mock.{hashlib.sha1(f'{planned_date}:{w_name}'.encode('utf-8')).hexdigest()[:8]}"
        with open(os.path.join(mock_dir, "garmin_ops.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"op": "PUSH", "id": wid, "date": planned_date, "name": payload.get("workoutName")}) + "\n")
        return True, f"Garmin mock PUSH -> {wid}", wid

    client = _get_garmin_client(brain)
    if client is None:
        return False, "Garmin Connect not connected.", None
    try:
        if existing_workout_id:
            garmin_delete_workout(brain, existing_workout_id)
        res = client.upload_workout(payload)
        wid = str((res or {}).get("workoutId") or (res or {}).get("workout_id") or "")
        if not wid:
            return False, "Garmin upload did not return workoutId.", None
        if planned_date and hasattr(client, "schedule_workout"):
            client.schedule_workout(wid, planned_date)
        return True, f"Scheduled Garmin workout {wid} on {planned_date}", wid
    except Exception as exc:
        return False, f"Garmin workout push failed: {exc}", None


def garmin_delete_workout(brain: str, workout_id: str, mock_dir: str | None = None) -> tuple[bool, str]:
    if not workout_id:
        return True, "No workout ID."
    if mock_dir:
        os.makedirs(mock_dir, exist_ok=True)
        with open(os.path.join(mock_dir, "garmin_ops.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"op": "DELETE", "id": workout_id}) + "\n")
        return True, f"Garmin mock DELETE -> {workout_id}"
    client = _get_garmin_client(brain)
    if client is None:
        return False, "Garmin Connect not connected."
    try:
        if hasattr(client, "delete_workout"):
            client.delete_workout(workout_id)
        return True, f"Deleted Garmin workout {workout_id}"
    except Exception as exc:
        return False, f"Garmin delete error: {exc}"


# ---------------------------------------------------------------------------
# 4. Intervals.icu Universal Pure-Stdlib REST Bridge (Wellness + Workouts)
# ---------------------------------------------------------------------------

def _intervals_auth_header(env: dict[str, str]) -> str | None:
    key = (env.get("INTERVALS_API_KEY") or "").strip()
    if not key:
        return None
    tok = base64.b64encode(f"API_KEY:{key}".encode("utf-8")).decode("ascii")
    return f"Basic {tok}"


def intervals_pull_wellness(
    brain: str,
    days: int = 7,
    mock_file: str | None = None,
    today: dt.date | None = None,
) -> list[dict[str, Any]]:
    """Pull wellness biometrics (`restingHR`, `hrv` RMSSD, `sleepSecs`, `weight`, `soreness`) from Intervals.icu."""
    ref_today = today or dt.date.today()
    if mock_file and os.path.isfile(mock_file):
        return garmin_pull_wellness(brain, days=days, mock_file=mock_file, today=ref_today)
    env = load_secret_env(brain, "intervals")
    aid = (env.get("INTERVALS_ATHLETE_ID") or "0").strip()
    hdr = _intervals_auth_header(env)
    if not hdr:
        return []
    oldest = (ref_today - dt.timedelta(days=max(1, days))).isoformat()
    newest = ref_today.isoformat()
    url = f"{INTERVALS_API_BASE}/athlete/{urllib.parse.quote(aid, safe='')}/wellness?oldest={oldest}&newest={newest}"
    req = urllib.request.Request(url, headers={"Authorization": hdr, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            items = json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    for it in (items if isinstance(items, list) else []):
        if not isinstance(it, dict) or not it.get("id"):
            continue
        slp_s = it.get("sleepSecs")
        out.append({
            "date": str(it["id"])[:10],
            "rhr": int(round(float(it["restingHR"]))) if it.get("restingHR") else None,
            "hrv_rmssd": round(float(it["hrv"]), 1) if it.get("hrv") else None,
            "sleep_h": round(float(slp_s) / 3600.0, 2) if slp_s else None,
            "sleep_quality": _sleep_score_to_quality(it.get("sleepScore")),
            "weight_kg": round(float(it["weight"]), 2) if it.get("weight") else None,
            "soreness": int(it["soreness"]) if it.get("soreness") else None,
            "source": "intervals",
        })
    return out


# ---------------------------------------------------------------------------
# 4B. Google Health API v4 (Dual-Mode PKCE OAuth + Live-Verified REST Puller)
# ---------------------------------------------------------------------------

def _gh_state_store_path(brain: str) -> str:
    return os.path.join(brain, "_atlas", "cache", "gh_oauth_states.json")


def import_google_health_client_json(brain: str, json_path: str, login_hint: str = "") -> tuple[bool, str]:
    """Import Google Cloud OAuth Client JSON (`installed` Desktop app or `web`) into `brain/_secrets/google_health.env`."""
    p_in = os.path.expanduser(str(json_path or "").strip())
    if not p_in or not os.path.isfile(p_in):
        return False, f"OAuth client JSON file not found: {p_in}"
    try:
        with open(p_in, encoding="utf-8", errors="replace") as fh:
            raw = json.load(fh)
        blk = raw.get("installed") or raw.get("web") or raw
        cid = str(blk.get("client_id") or "").strip()
        csec = str(blk.get("client_secret") or "").strip()
        if not (cid and csec):
            return False, "JSON did not contain `client_id` and `client_secret`."
    except Exception as exc:
        return False, f"Failed to parse client JSON: {exc}"
    env = load_secret_env(brain, "google_health")
    env["GOOGLE_HEALTH_CLIENT_ID"] = cid
    env["GOOGLE_HEALTH_CLIENT_SECRET"] = csec
    if login_hint:
        env["GOOGLE_HEALTH_LOGIN_HINT"] = login_hint.strip()
    saved = save_secret_env(brain, "google_health", env)
    return True, saved


def google_health_oauth_config(brain: str, local_redirect_uri: str = "") -> dict[str, Any]:
    """Resolve Google Health OAuth config (BYO Desktop/Web credentials or optional broker hook)."""
    env = load_secret_env(brain, "google_health")
    cid = (env.get("GOOGLE_HEALTH_CLIENT_ID") or os.environ.get("ATLAS_GOOGLE_HEALTH_CLIENT_ID") or "").strip()
    csec = (env.get("GOOGLE_HEALTH_CLIENT_SECRET") or os.environ.get("ATLAS_GOOGLE_HEALTH_CLIENT_SECRET") or "").strip()
    broker = (env.get("GOOGLE_HEALTH_BROKER_URL") or os.environ.get("ATLAS_GOOGLE_HEALTH_BROKER_URL") or "").strip()
    redir = (env.get("GOOGLE_HEALTH_REDIRECT_URI") or os.environ.get("ATLAS_GOOGLE_HEALTH_REDIRECT_URI") or local_redirect_uri).strip()
    return {
        "configured": bool((cid and csec) or (cid and broker)),
        "use_broker": bool(broker and not csec),
        "client_id": cid,
        "client_secret": csec,
        "broker_url": broker,
        "redirect_uri": redir,
        "login_hint": (env.get("GOOGLE_HEALTH_LOGIN_HINT") or "").strip(),
    }


def mint_google_health_oauth_state(brain: str, port: int = 8765) -> tuple[str, str, str]:
    """Mint CSRF state + PKCE (`S256`) `code_verifier` & `code_challenge` with 30-min TTL (`0o600`)."""
    eff_port = port if 1024 <= int(port) <= 65535 else 8765
    state = f"atlas:{eff_port}:{secrets.token_urlsafe(20)}"
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    p = _gh_state_store_path(brain)
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
    store[state] = {"ts": now_ts, "verifier": verifier, "port": eff_port}
    if len(store) > 12:
        store = dict(sorted(store.items(), key=lambda kv: int(kv[1].get("ts") or 0))[-12:])
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, json.dumps(store).encode("utf-8"))
    finally:
        os.close(fd)
    return state, verifier, challenge


def consume_google_health_oauth_state(brain: str, state: str | None) -> tuple[bool, str]:
    """Verify & consume a previously minted Google Health OAuth state; returns `(ok, code_verifier)`."""
    cand = str(state or "").strip()
    if not cand:
        return False, ""
    p = _gh_state_store_path(brain)
    if not os.path.isfile(p):
        return False, ""
    now_ts = int(time.time())
    try:
        with open(p, encoding="utf-8") as fh:
            raw = json.load(fh)
        if not isinstance(raw, dict):
            return False, ""
    except Exception:
        return False, ""
    matched = False
    verifier = ""
    remaining: dict[str, dict[str, Any]] = {}
    for k, entry in raw.items():
        if not isinstance(entry, dict):
            continue
        if now_ts - int(entry.get("ts") or 0) >= 1800:
            continue
        if secrets.compare_digest(str(k), cand):
            matched = True
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
    return matched, verifier


def build_google_health_authorize_url(
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
        "scope": " ".join(GOOGLE_HEALTH_SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    if login_hint:
        params["login_hint"] = login_hint.strip()
    return f"{GOOGLE_HEALTH_AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"


def google_health_exchange_code(
    brain: str,
    code: str,
    redirect_uri: str,
    code_verifier: str = "",
    client_id: str | None = None,
    client_secret: str | None = None,
) -> tuple[bool, str]:
    env = load_secret_env(brain, "google_health")
    cid = (client_id or env.get("GOOGLE_HEALTH_CLIENT_ID") or os.environ.get("ATLAS_GOOGLE_HEALTH_CLIENT_ID") or "").strip()
    csec = (client_secret or env.get("GOOGLE_HEALTH_CLIENT_SECRET") or os.environ.get("ATLAS_GOOGLE_HEALTH_CLIENT_SECRET") or "").strip()
    broker = (env.get("GOOGLE_HEALTH_BROKER_URL") or os.environ.get("ATLAS_GOOGLE_HEALTH_BROKER_URL") or "").strip()

    if cid and csec:
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": cid,
            "client_secret": csec,
            "redirect_uri": redirect_uri,
        }
        if code_verifier:
            form["code_verifier"] = code_verifier
        req = urllib.request.Request(
            GOOGLE_HEALTH_TOKEN_URL,
            data=urllib.parse.urlencode(form).encode("utf-8"),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
    elif broker:
        body = {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri}
        if code_verifier:
            body["code_verifier"] = code_verifier
        req = urllib.request.Request(
            broker,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": "AtlasCoach/1.5"},
            method="POST",
        )
    else:
        return False, "Google Health OAuth client credentials missing in `brain/_secrets/google_health.env`."

    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as he:
        err_t = he.read().decode("utf-8", errors="replace")[:300]
        return False, f"Google Health OAuth token exchange failed (HTTP {he.code}): {err_t}"
    except Exception as exc:
        return False, f"Google Health OAuth token exchange failed: {exc}"

    access = str(data.get("access_token") or "").strip()
    if not access:
        return False, "Google Health OAuth response did not include `access_token`."
    now_ts = int(time.time())
    if cid:
        env["GOOGLE_HEALTH_CLIENT_ID"] = cid
    if csec:
        env["GOOGLE_HEALTH_CLIENT_SECRET"] = csec
    env["GOOGLE_HEALTH_ACCESS_TOKEN"] = access
    if data.get("refresh_token"):
        env["GOOGLE_HEALTH_REFRESH_TOKEN"] = str(data["refresh_token"]).strip()
        env["GOOGLE_HEALTH_REFRESH_ISSUED_AT"] = str(now_ts)
    env["GOOGLE_HEALTH_EXPIRES_AT"] = str(now_ts + int(data.get("expires_in") or 3600))
    if data.get("scope"):
        env["GOOGLE_HEALTH_SCOPES"] = str(data["scope"]).strip()
    env.pop("GOOGLE_HEALTH_AUTH_ERROR", None)
    p = save_secret_env(brain, "google_health", env)
    return True, p


def google_health_refresh_if_needed(brain: str, env_dict: dict[str, str] | None = None) -> tuple[str, str]:
    """Return `(access_token, auth_error)` — auto-refreshes expired access tokens and flags Testing-Mode 7d expiry."""
    env = dict(env_dict) if env_dict is not None else load_secret_env(brain, "google_health")
    exp = int(env.get("GOOGLE_HEALTH_EXPIRES_AT") or 0)
    if env.get("GOOGLE_HEALTH_ACCESS_TOKEN") and exp > time.time() + 120:
        return env["GOOGLE_HEALTH_ACCESS_TOKEN"], ""
    rtok = (env.get("GOOGLE_HEALTH_REFRESH_TOKEN") or "").strip()
    if not rtok:
        return env.get("GOOGLE_HEALTH_ACCESS_TOKEN", ""), ("refresh_expired" if not env.get("GOOGLE_HEALTH_ACCESS_TOKEN") else "")
    cid = (env.get("GOOGLE_HEALTH_CLIENT_ID") or os.environ.get("ATLAS_GOOGLE_HEALTH_CLIENT_ID") or "").strip()
    csec = (env.get("GOOGLE_HEALTH_CLIENT_SECRET") or os.environ.get("ATLAS_GOOGLE_HEALTH_CLIENT_SECRET") or "").strip()
    broker = (env.get("GOOGLE_HEALTH_BROKER_URL") or os.environ.get("ATLAS_GOOGLE_HEALTH_BROKER_URL") or "").strip()
    if cid and csec:
        req = urllib.request.Request(
            GOOGLE_HEALTH_TOKEN_URL,
            data=urllib.parse.urlencode({
                "grant_type": "refresh_token",
                "refresh_token": rtok,
                "client_id": cid,
                "client_secret": csec,
            }).encode("utf-8"),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
    elif broker:
        req = urllib.request.Request(
            broker,
            data=json.dumps({"grant_type": "refresh_token", "refresh_token": rtok}).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": "AtlasCoach/1.5"},
            method="POST",
        )
    else:
        return env.get("GOOGLE_HEALTH_ACCESS_TOKEN", ""), "missing_client_credentials"

    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        if data.get("access_token"):
            now_ts = int(time.time())
            env["GOOGLE_HEALTH_ACCESS_TOKEN"] = str(data["access_token"]).strip()
            if data.get("refresh_token"):
                env["GOOGLE_HEALTH_REFRESH_TOKEN"] = str(data["refresh_token"]).strip()
                env["GOOGLE_HEALTH_REFRESH_ISSUED_AT"] = str(now_ts)
            env["GOOGLE_HEALTH_EXPIRES_AT"] = str(now_ts + int(data.get("expires_in") or 3600))
            env.pop("GOOGLE_HEALTH_AUTH_ERROR", None)
            save_secret_env(brain, "google_health", env)
            return env["GOOGLE_HEALTH_ACCESS_TOKEN"], ""
    except urllib.error.HTTPError as he:
        err_body = he.read().decode("utf-8", errors="replace").lower()
        if he.code in (400, 401) and ("invalid_grant" in err_body or "expired" in err_body or "revoked" in err_body):
            env["GOOGLE_HEALTH_AUTH_ERROR"] = "refresh_expired"
            save_secret_env(brain, "google_health", env)
            return "", "refresh_expired"
        return "", f"http_{he.code}"
    except Exception:
        return "", "network_error"
    return "", "refresh_failed"


def _local_tz_offset_seconds() -> int:
    try:
        off = dt.datetime.now().astimezone().utcoffset()
        return int(off.total_seconds()) if off is not None else 0
    except Exception:
        return 0


def _gh_api_get(token: str, url: str) -> tuple[int, dict[str, Any]]:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return resp.status, (json.loads(raw) if raw.strip().startswith("{") else {})
    except urllib.error.HTTPError as he:
        return he.code, {}
    except Exception:
        return 0, {}


def _gh_api_post(token: str, url: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return resp.status, (json.loads(raw) if raw.strip().startswith("{") else {})
    except urllib.error.HTTPError as he:
        return he.code, {}
    except Exception:
        return 0, {}


def _gh_reconcile_all(
    token: str,
    data_type: str,
    filter_expr: str = "",
    page_size: int = 200,
    max_pages: int = 20,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    page_token = ""
    for _ in range(max_pages):
        params = [f"pageSize={int(page_size)}"]
        if filter_expr:
            params.append("filter=" + urllib.parse.quote(filter_expr, safe=""))
        if page_token:
            params.append("pageToken=" + urllib.parse.quote(page_token, safe=""))
        url = f"{GOOGLE_HEALTH_API_BASE}/dataTypes/{data_type}/dataPoints:reconcile?" + "&".join(params)
        st, payload = _gh_api_get(token, url)
        if st != 200 or not isinstance(payload, dict):
            break
        dps = payload.get("dataPoints")
        if isinstance(dps, list):
            out.extend(x for x in dps if isinstance(x, dict))
        page_token = str(payload.get("nextPageToken") or "").strip()
        if not page_token:
            break
    return out


def _gh_rollup_all(
    token: str,
    data_type: str,
    method: str,
    body: dict[str, Any],
    max_pages: int = 20,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    page_token = ""
    for _ in range(max_pages):
        req_body = dict(body)
        if page_token:
            req_body["pageToken"] = page_token
        url = f"{GOOGLE_HEALTH_API_BASE}/dataTypes/{data_type}/dataPoints:{method}"
        st, payload = _gh_api_post(token, url, req_body)
        if st != 200 or not isinstance(payload, dict):
            break
        dps = payload.get("rollupDataPoints")
        if isinstance(dps, list):
            out.extend(x for x in dps if isinstance(x, dict))
        page_token = str(payload.get("nextPageToken") or "").strip()
        if not page_token:
            break
    return out


def parse_google_health_payloads(
    raw_bundle: dict[str, Any],
    intraday_cutoff_iso: str = "",
    default_utc_offset_s: int | None = None,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """Deterministically parse Google Health API v4 response bundles into canonical daily records + rolling intraday cache.

    Never imports `exercise` activities (Strava stays the single source of truth for workouts).
    """
    eff_off_default = _local_tz_offset_seconds() if default_utc_offset_s is None else int(default_utc_offset_s)
    days: dict[str, dict[str, Any]] = {}
    intraday: dict[str, dict[str, Any]] = {}
    day_offsets: dict[str, int] = {}

    def _day(d_iso: str) -> dict[str, Any]:
        rec = days.get(d_iso)
        if rec is None:
            rec = {
                "date": d_iso,
                "source": "google_health",
                "sleep": {},
                "autonomic": {},
                "body": {},
                "daytime_load": {},
            }
            days[d_iso] = rec
        return rec

    def _intra(d_iso: str) -> dict[str, Any]:
        rec = intraday.get(d_iso)
        if rec is None:
            rec = {"date": d_iso, "source": "google_health", "hr_5m": [], "hrv_samples": [], "sleep_stages": []}
            intraday[d_iso] = rec
        return rec

    # 1. Sleep sessions (`sleep`) — wake-up civil date attribution, mainSleep vs naps
    sleep_by_date: dict[str, list[dict[str, Any]]] = {}
    for dp in raw_bundle.get("sleep") or []:
        slp = dp.get("sleep") if isinstance(dp, dict) and isinstance(dp.get("sleep"), dict) else (dp if isinstance(dp, dict) else {})
        iv = slp.get("interval") if isinstance(slp.get("interval"), dict) else {}
        end_utc_s = iv.get("endTime")
        start_utc_s = iv.get("startTime")
        end_off = health_engine.parse_utc_offset_seconds(iv.get("endUtcOffset") or iv.get("startUtcOffset") or eff_off_default)
        start_off = health_engine.parse_utc_offset_seconds(iv.get("startUtcOffset") or end_off)
        end_local = health_engine.utc_to_local_iso(end_utc_s, end_off)
        start_local = health_engine.utc_to_local_iso(start_utc_s, start_off)
        if not end_local:
            continue
        wake_date = end_local[:10]
        day_offsets[wake_date] = end_off
        summ = slp.get("summary") if isinstance(slp.get("summary"), dict) else {}
        asleep_m = health_engine.safe_int(summ.get("minutesAsleep"))
        period_m = health_engine.safe_int(summ.get("minutesInSleepPeriod"))
        awake_m = health_engine.safe_int(summ.get("minutesAwake"))
        stage_mins: dict[str, int] = {}
        for st_item in (summ.get("stagesSummary") or []):
            if isinstance(st_item, dict) and st_item.get("type"):
                m_val = health_engine.safe_int(st_item.get("minutes"))
                if m_val is not None:
                    stage_mins[str(st_item["type"]).upper()] = m_val
        if asleep_m is None and stage_mins:
            asleep_m = sum(stage_mins.get(k, 0) for k in ("LIGHT", "DEEP", "REM"))
        if period_m is None and asleep_m is not None:
            period_m = asleep_m + (awake_m or stage_mins.get("AWAKE", 0))
        meta = slp.get("metadata") if isinstance(slp.get("metadata"), dict) else {}
        parsed_slp = {
            "main": bool(meta.get("mainSleep", True)),
            "start_utc": start_utc_s,
            "end_utc": end_utc_s,
            "start_local": start_local,
            "end_local": end_local,
            "utc_offset_s": end_off,
            "asleep_min": asleep_m,
            "period_min": period_m,
            "awake_min": awake_m if awake_m is not None else stage_mins.get("AWAKE"),
            "light_min": stage_mins.get("LIGHT"),
            "deep_min": stage_mins.get("DEEP"),
            "rem_min": stage_mins.get("REM"),
            "short_awakenings_count": len(slp.get("shortAwakenings") or []) if isinstance(slp.get("shortAwakenings"), list) else None,
            "stages": slp.get("stages") if isinstance(slp.get("stages"), list) else [],
        }
        sleep_by_date.setdefault(wake_date, []).append(parsed_slp)

    for d_iso, sess_list in sleep_by_date.items():
        sess_list.sort(key=lambda s: (1 if s["main"] else 0, int(s.get("asleep_min") or 0)), reverse=True)
        main_s = sess_list[0]
        rec_s = _day(d_iso)["sleep"]
        asleep_m = main_s.get("asleep_min")
        period_m = main_s.get("period_min")
        rec_s.update({
            "start_utc": main_s["start_utc"],
            "end_utc": main_s["end_utc"],
            "start_local": main_s["start_local"],
            "end_local": main_s["end_local"],
            "utc_offset_s": main_s["utc_offset_s"],
            "asleep_min": asleep_m,
            "period_min": period_m,
            "awake_min": main_s.get("awake_min"),
            "light_min": main_s.get("light_min"),
            "deep_min": main_s.get("deep_min"),
            "rem_min": main_s.get("rem_min"),
            "duration_h": round(asleep_m / 60.0, 2) if asleep_m else None,
            "efficiency_pct": round((asleep_m / period_m) * 100.0, 1) if (asleep_m and period_m and period_m > 0) else None,
            "short_awakenings_count": main_s.get("short_awakenings_count"),
        })
        rec_s["quality"] = health_engine.derive_sleep_quality(rec_s)
        naps = [
            {"start_local": x["start_local"], "end_local": x["end_local"], "asleep_min": x["asleep_min"]}
            for x in sess_list[1:] if x.get("asleep_min")
        ]
        if naps:
            rec_s["naps"] = naps
        if main_s.get("stages") and (not intraday_cutoff_iso or d_iso >= intraday_cutoff_iso):
            _intra(d_iso)["utc_offset_s"] = main_s["utc_offset_s"]
            _intra(d_iso)["sleep_stages"] = [
                {"start_utc": st_e.get("startTime"), "end_utc": st_e.get("endTime"), "type": st_e.get("type")}
                for st_e in main_s["stages"]
                if isinstance(st_e, dict) and st_e.get("startTime")
            ]

    # 2. Daily Resting Heart Rate (`daily-resting-heart-rate`)
    for dp in raw_bundle.get("daily_rhr") or []:
        obj = dp.get("dailyRestingHeartRate") if isinstance(dp, dict) and isinstance(dp.get("dailyRestingHeartRate"), dict) else {}
        d_iso = health_engine.civil_date_dict_to_iso(obj.get("date"))
        bpm = health_engine.safe_int(obj.get("beatsPerMinute"))
        if d_iso and bpm and 25 <= bpm <= 130:
            _day(d_iso)["autonomic"]["rhr_bpm"] = bpm

    # 3. Daily HRV (`daily-heart-rate-variability`)
    for dp in raw_bundle.get("daily_hrv") or []:
        obj = dp.get("dailyHeartRateVariability") if isinstance(dp, dict) and isinstance(dp.get("dailyHeartRateVariability"), dict) else {}
        d_iso = health_engine.civil_date_dict_to_iso(obj.get("date"))
        if not d_iso:
            continue
        aut = _day(d_iso)["autonomic"]
        avg_ms = health_engine.safe_float(obj.get("averageHeartRateVariabilityMilliseconds"))
        deep_ms = health_engine.safe_float(obj.get("deepSleepRootMeanSquareOfSuccessiveDifferencesMilliseconds"))
        nrem_hr = health_engine.safe_int(obj.get("nonRemHeartRateBeatsPerMinute"))
        ent = health_engine.safe_float(obj.get("entropy"))
        if avg_ms is not None and 5.0 <= avg_ms <= 300.0:
            aut["hrv_rmssd_ms"] = round(avg_ms, 2)
        if deep_ms is not None and 5.0 <= deep_ms <= 300.0:
            aut["hrv_deep_rmssd_ms"] = round(deep_ms, 2)
        if nrem_hr is not None and 25 <= nrem_hr <= 130:
            aut["hrv_non_rem_hr_bpm"] = nrem_hr
        if ent is not None:
            aut["hrv_entropy"] = round(ent, 3)

    # 4. Daily Respiratory Rate (`daily-respiratory-rate`)
    for dp in raw_bundle.get("daily_resp") or []:
        obj = dp.get("dailyRespiratoryRate") if isinstance(dp, dict) and isinstance(dp.get("dailyRespiratoryRate"), dict) else {}
        d_iso = health_engine.civil_date_dict_to_iso(obj.get("date"))
        brpm = health_engine.safe_float(obj.get("breathsPerMinute"))
        if d_iso and brpm is not None and 5.0 <= brpm <= 40.0:
            _day(d_iso)["autonomic"]["resp_rate_brpm"] = round(brpm, 1)

    # 5. Daily Sleep Temperature Derivations (`daily-sleep-temperature-derivations`)
    for dp in raw_bundle.get("daily_temp") or []:
        obj = dp.get("dailySleepTemperatureDerivations") if isinstance(dp, dict) and isinstance(dp.get("dailySleepTemperatureDerivations"), dict) else {}
        d_iso = health_engine.civil_date_dict_to_iso(obj.get("date"))
        if not d_iso:
            continue
        nightly = health_engine.safe_float(obj.get("nightlyTemperatureCelsius"))
        base_t = health_engine.safe_float(obj.get("baselineTemperatureCelsius"))
        sd_30 = health_engine.safe_float(obj.get("relativeNightlyStddev30dCelsius"))
        if nightly is not None and base_t is not None:
            _day(d_iso)["autonomic"]["skin_temp_delta_c"] = round(nightly - base_t, 2)
        elif health_engine.safe_float(obj.get("skinTemperatureVariation")) is not None:
            _day(d_iso)["autonomic"]["skin_temp_delta_c"] = round(float(obj["skinTemperatureVariation"]), 2)
        if sd_30 is not None:
            _day(d_iso)["autonomic"]["skin_temp_stddev_30d_c"] = round(sd_30, 2)

    # 6. Daily Oxygen Saturation (`daily-oxygen-saturation`)
    for dp in raw_bundle.get("daily_spo2") or []:
        obj = dp.get("dailyOxygenSaturation") if isinstance(dp, dict) and isinstance(dp.get("dailyOxygenSaturation"), dict) else {}
        d_iso = health_engine.civil_date_dict_to_iso(obj.get("date"))
        if not d_iso:
            continue
        avg_p = health_engine.safe_float(obj.get("averagePercentage"))
        lo_p = health_engine.safe_float(obj.get("lowerBoundPercentage"))
        hi_p = health_engine.safe_float(obj.get("upperBoundPercentage"))
        if avg_p is not None and 70.0 <= avg_p <= 100.0:
            _day(d_iso)["autonomic"]["spo2_avg_pct"] = round(avg_p, 1)
        if lo_p is not None and 70.0 <= lo_p <= 100.0:
            _day(d_iso)["autonomic"]["spo2_min_pct"] = round(lo_p, 1)
        if hi_p is not None and 70.0 <= hi_p <= 100.0:
            _day(d_iso)["autonomic"]["spo2_max_pct"] = round(hi_p, 1)

    # 7. Body Composition (`weight`, `body-fat`, `vo2-max`)
    def _sample_date(st_obj: Any) -> str | None:
        if not isinstance(st_obj, dict):
            return None
        ct = st_obj.get("civilTime")
        if isinstance(ct, dict):
            d_c = health_engine.civil_date_dict_to_iso(ct.get("date"))
            if d_c:
                return d_c
        off = health_engine.parse_utc_offset_seconds(st_obj.get("utcOffset") or eff_off_default)
        loc = health_engine.utc_to_local_iso(st_obj.get("physicalTime"), off)
        return loc[:10] if loc else None

    for dp in raw_bundle.get("weight") or []:
        obj = dp.get("weight") if isinstance(dp, dict) and isinstance(dp.get("weight"), dict) else {}
        d_iso = _sample_date(obj.get("sampleTime"))
        wg = health_engine.safe_float(obj.get("weightGrams"))
        if d_iso and wg and 30000.0 <= wg <= 220000.0:
            _day(d_iso)["body"].setdefault("weight_kg", round(wg / 1000.0, 2))

    for dp in raw_bundle.get("body_fat") or []:
        obj = dp.get("bodyFat") if isinstance(dp, dict) and isinstance(dp.get("bodyFat"), dict) else {}
        d_iso = _sample_date(obj.get("sampleTime"))
        pct = health_engine.safe_float(obj.get("percentage"))
        if d_iso and pct is not None and 3.0 <= pct <= 55.0:
            _day(d_iso)["body"].setdefault("body_fat_pct", round(pct, 1))

    for dp in raw_bundle.get("vo2_max") or []:
        obj = dp.get("vo2Max") if isinstance(dp, dict) and isinstance(dp.get("vo2Max"), dict) else {}
        d_iso = _sample_date(obj.get("sampleTime"))
        vm = health_engine.safe_float(obj.get("vo2Max"))
        if d_iso and vm is not None and 20.0 <= vm <= 100.0:
            _day(d_iso)["body"].setdefault("vo2max_watch", round(vm, 1))

    # 8. Daily Steps & Active Zone Minutes (`:dailyRollUp`)
    for dp in raw_bundle.get("steps_daily") or []:
        if not isinstance(dp, dict):
            continue
        cst = dp.get("civilStartTime") if isinstance(dp.get("civilStartTime"), dict) else {}
        d_iso = health_engine.civil_date_dict_to_iso(cst.get("date"))
        cnt = health_engine.safe_int((dp.get("steps") or {}).get("countSum"))
        if d_iso and cnt is not None and cnt >= 0:
            _day(d_iso)["daytime_load"]["steps_total"] = cnt

    for dp in raw_bundle.get("azm_daily") or []:
        if not isinstance(dp, dict):
            continue
        cst = dp.get("civilStartTime") if isinstance(dp.get("civilStartTime"), dict) else {}
        d_iso = health_engine.civil_date_dict_to_iso(cst.get("date"))
        azm = dp.get("activeZoneMinutes") if isinstance(dp.get("activeZoneMinutes"), dict) else {}
        if not d_iso or not azm:
            continue
        fb = health_engine.safe_int(azm.get("sumInFatBurnHeartZone")) or 0
        ca = health_engine.safe_int(azm.get("sumInCardioHeartZone")) or 0
        pk = health_engine.safe_int(azm.get("sumInPeakHeartZone")) or 0
        dl = _day(d_iso)["daytime_load"]
        dl["azm_fat_burn_min"] = fb
        dl["azm_cardio_min"] = ca
        dl["azm_peak_min"] = pk
        dl["azm_total_min"] = fb + ca + pk

    # 9. Hourly Steps (`steps:rollUp` windowSize=3600s) -> 24-int local `hourly_steps`
    hourly_buckets: dict[str, list[int]] = {}
    for dp in raw_bundle.get("steps_hourly") or []:
        if not isinstance(dp, dict):
            continue
        st_u = dp.get("startTime")
        cnt = health_engine.safe_int((dp.get("steps") or {}).get("countSum"))
        if not st_u or cnt is None:
            continue
        t_utc = health_engine.parse_iso_utc(st_u)
        if not t_utc:
            continue
        approx_d = t_utc.strftime("%Y-%m-%d")
        off_s = day_offsets.get(approx_d, eff_off_default)
        loc = health_engine.utc_to_local_iso(t_utc, off_s)
        if not loc:
            continue
        d_iso = loc[:10]
        hr_i = int(loc[11:13])
        arr = hourly_buckets.setdefault(d_iso, [0] * 24)
        arr[hr_i] += max(0, cnt)
    for d_iso, arr in hourly_buckets.items():
        if sum(arr) > 0:
            dl = _day(d_iso)["daytime_load"]
            dl["hourly_steps"] = arr
            if dl.get("steps_total") is None:
                dl["steps_total"] = sum(arr)

    # 10. Intraday 5-Min HR RollUp (`heart-rate:rollUp` windowSize=300s) & 5-Min HRV Samples
    for dp in raw_bundle.get("hr_5m") or []:
        if not isinstance(dp, dict):
            continue
        st_u = dp.get("startTime")
        en_u = dp.get("endTime")
        hr_obj = dp.get("heartRate") if isinstance(dp.get("heartRate"), dict) else {}
        avg_b = health_engine.safe_float(hr_obj.get("beatsPerMinuteAvg"))
        if not st_u or avg_b is None:
            continue
        t_utc = health_engine.parse_iso_utc(st_u)
        if not t_utc:
            continue
        approx_d = t_utc.strftime("%Y-%m-%d")
        off_s = day_offsets.get(approx_d, eff_off_default)
        loc = health_engine.utc_to_local_iso(t_utc, off_s)
        if not loc:
            continue
        d_iso = loc[:10]
        if intraday_cutoff_iso and d_iso < intraday_cutoff_iso:
            continue
        intr = _intra(d_iso)
        intr["utc_offset_s"] = off_s
        intr["hr_5m"].append({
            "start_utc": st_u,
            "end_utc": en_u,
            "avg": round(avg_b, 1),
            "min": health_engine.safe_int(hr_obj.get("beatsPerMinuteMin")),
            "max": health_engine.safe_int(hr_obj.get("beatsPerMinuteMax")),
        })

    for dp in raw_bundle.get("hrv_intraday") or []:
        obj = dp.get("heartRateVariability") if isinstance(dp, dict) and isinstance(dp.get("heartRateVariability"), dict) else {}
        st_obj = obj.get("sampleTime") if isinstance(obj.get("sampleTime"), dict) else {}
        rmssd = health_engine.safe_float(obj.get("rootMeanSquareOfSuccessiveDifferencesMilliseconds"))
        d_iso = _sample_date(st_obj)
        if not d_iso or rmssd is None:
            continue
        if intraday_cutoff_iso and d_iso < intraday_cutoff_iso:
            continue
        off_s = health_engine.parse_utc_offset_seconds(st_obj.get("utcOffset") or day_offsets.get(d_iso, eff_off_default))
        phys = st_obj.get("physicalTime")
        loc = health_engine.utc_to_local_iso(phys, off_s)
        meta = obj.get("metadata") if isinstance(obj.get("metadata"), dict) else {}
        _intra(d_iso)["hrv_samples"].append({
            "time_utc": phys,
            "local": loc,
            "rmssd": round(rmssd, 1),
            "hf": health_engine.safe_float(meta.get("highFrequencyPower")),
            "lf": health_engine.safe_float(meta.get("lowFrequencyPower")),
        })

    for intr in intraday.values():
        intr["hr_5m"].sort(key=lambda x: str(x.get("start_utc") or ""))
        intr["hrv_samples"].sort(key=lambda x: str(x.get("time_utc") or ""))

    out_days = sorted(days.values(), key=lambda x: x["date"], reverse=True)
    return out_days, intraday


def google_health_pull_biometrics(
    brain: str,
    days: int = 30,
    intraday_days: int = 30,
    mock_file: str | None = None,
    today: dt.date | None = None,
) -> dict[str, Any]:
    """Pull Sleep, Autonomics, Body & Daytime NEAT from Google Health API v4 (0 workouts/exercises!)."""
    ref_today = today or dt.date.today()
    eff_days = max(1, min(int(days or 30), 730))
    eff_intra = max(1, min(int(intraday_days or 30), eff_days, 30))
    start_date = ref_today - dt.timedelta(days=eff_days)
    start_iso = start_date.isoformat()
    intra_cutoff = (ref_today - dt.timedelta(days=eff_intra)).isoformat()

    if mock_file and os.path.isfile(mock_file):
        try:
            with open(mock_file, encoding="utf-8", errors="replace") as fh:
                raw_bundle = json.load(fh)
            parsed_days, parsed_intra = parse_google_health_payloads(
                raw_bundle if isinstance(raw_bundle, dict) else {},
                intraday_cutoff_iso=intra_cutoff,
                default_utc_offset_s=int((raw_bundle or {}).get("default_utc_offset_s", 3600)) if isinstance(raw_bundle, dict) else 3600,
            )
            return {"ok": True, "source": "google_health", "days": parsed_days, "intraday": parsed_intra, "auth_error": ""}
        except Exception as exc:
            return {"ok": False, "source": "google_health", "days": [], "intraday": {}, "auth_error": str(exc)}

    status = google_health_token_status(brain)
    if not status["has_tokens"]:
        return {"ok": False, "source": "google_health", "days": [], "intraday": {}, "auth_error": "not_connected"}

    token, auth_err = google_health_refresh_if_needed(brain)
    if not token:
        return {"ok": False, "source": "google_health", "days": [], "intraday": {}, "auth_error": auth_err or "refresh_expired"}

    # 1. Reconciled daily & session streams (fast, paginated cleanly via AIP-160 filters)
    max_p_daily = max(6, int(eff_days / 80) + 4)
    raw_bundle: dict[str, Any] = {
        "sleep": _gh_reconcile_all(token, "sleep", f'sleep.interval.end_time >= "{start_iso}T00:00:00Z"', page_size=100, max_pages=max_p_daily),
        "daily_rhr": _gh_reconcile_all(token, "daily-resting-heart-rate", f'daily_resting_heart_rate.date >= "{start_iso}"', page_size=200, max_pages=max_p_daily),
        "daily_hrv": _gh_reconcile_all(token, "daily-heart-rate-variability", f'daily_heart_rate_variability.date >= "{start_iso}"', page_size=200, max_pages=max_p_daily),
        "daily_resp": _gh_reconcile_all(token, "daily-respiratory-rate", f'daily_respiratory_rate.date >= "{start_iso}"', page_size=200, max_pages=max_p_daily),
        "daily_temp": _gh_reconcile_all(token, "daily-sleep-temperature-derivations", f'daily_sleep_temperature_derivations.date >= "{start_iso}"', page_size=200, max_pages=max_p_daily),
        "daily_spo2": _gh_reconcile_all(token, "daily-oxygen-saturation", f'daily_oxygen_saturation.date >= "{start_iso}"', page_size=200, max_pages=max_p_daily),
        "weight": _gh_reconcile_all(token, "weight", f'weight.sample_time.physical_time >= "{start_iso}T00:00:00Z"', page_size=200, max_pages=6),
        "body_fat": _gh_reconcile_all(token, "body-fat", f'body_fat.sample_time.physical_time >= "{start_iso}T00:00:00Z"', page_size=200, max_pages=6),
        "vo2_max": _gh_reconcile_all(token, "vo2-max", f'vo2_max.sample_time.physical_time >= "{start_iso}T00:00:00Z"', page_size=100, max_pages=4),
        "steps_daily": [],
        "azm_daily": [],
        "steps_hourly": [],
        "hr_5m": [],
        "hrv_intraday": [],
    }

    # 2. Chunked `:dailyRollUp` (max 90 days per request) for steps & active-zone-minutes
    end_excl = ref_today + dt.timedelta(days=1)
    cur_s = start_date
    while cur_s < end_excl:
        cur_e = min(end_excl, cur_s + dt.timedelta(days=85))
        body_d = {
            "range": {
                "start": {"date": {"year": cur_s.year, "month": cur_s.month, "day": cur_s.day}},
                "end": {"date": {"year": cur_e.year, "month": cur_e.month, "day": cur_e.day}},
            },
            "windowSizeDays": 1,
        }
        raw_bundle["steps_daily"].extend(_gh_rollup_all(token, "steps", "dailyRollUp", body_d, max_pages=5))
        raw_bundle["azm_daily"].extend(_gh_rollup_all(token, "active-zone-minutes", "dailyRollUp", body_d, max_pages=5))
        cur_s = cur_e

    # 3. Hourly steps `:rollUp` (windowSize=3600s, rolling window <= 30d) for permanent pre-workout NEAT
    now_utc = dt.datetime.now(dt.timezone.utc)
    intra_start_utc = dt.datetime.combine(ref_today - dt.timedelta(days=eff_intra), dt.time(0, 0), tzinfo=dt.timezone.utc)
    raw_bundle["steps_hourly"].extend(_gh_rollup_all(token, "steps", "rollUp", {
        "range": {
            "startTime": intra_start_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "endTime": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
        "windowSize": "3600s",
        "pageSize": 1000,
    }, max_pages=4))

    # 4. 5-Min HR `:rollUp` (max 14 days per request -> 2-3 chunks for 30 days)
    hr_cur = intra_start_utc
    while hr_cur < now_utc:
        hr_end = min(now_utc, hr_cur + dt.timedelta(days=13))
        raw_bundle["hr_5m"].extend(_gh_rollup_all(token, "heart-rate", "rollUp", {
            "range": {
                "startTime": hr_cur.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "endTime": hr_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
            "windowSize": "300s",
            "pageSize": 1000,
        }, max_pages=6))
        hr_cur = hr_end

    # 5. Intraday nocturnal HRV samples (capped to last min(eff_intra, 14) days to keep sync snappy)
    hrv_cut = (ref_today - dt.timedelta(days=min(eff_intra, 14))).isoformat()
    raw_bundle["hrv_intraday"] = _gh_reconcile_all(
        token,
        "heart-rate-variability",
        f'heart_rate_variability.sample_time.physical_time >= "{hrv_cut}T00:00:00Z"',
        page_size=200,
        max_pages=10,
    )

    parsed_days, parsed_intra = parse_google_health_payloads(raw_bundle, intraday_cutoff_iso=intra_cutoff)
    return {"ok": True, "source": "google_health", "days": parsed_days, "intraday": parsed_intra, "auth_error": ""}


def pull_all_wellness_into_biometrics(
    brain: str,
    store: dict[str, Any],
    days: int = 30,
    intraday_days: int = 30,
    google_health_mock: str | None = None,
    wellness_mock: str | None = None,
    execute: bool = True,
    today: dt.date | None = None,
) -> dict[str, Any]:
    """Unified multi-provider wellness sync: Google Health API v4 + Garmin Connect + Intervals.icu -> `biometrics.json` + 30d cache."""
    ref_today = today or dt.date.today()
    imported_days = 0
    providers_used: list[str] = []
    auth_warnings: list[str] = []
    intraday_written = 0

    # 1. Google Health API v4 (highest priority wearable provider)
    gh_status = google_health_token_status(brain)
    if google_health_mock or gh_status["has_tokens"]:
        gh_res = google_health_pull_biometrics(
            brain,
            days=days,
            intraday_days=intraday_days,
            mock_file=google_health_mock,
            today=ref_today,
        )
        if gh_res.get("auth_error") == "refresh_expired":
            auth_warnings.append("google_health_refresh_expired")
        if gh_res.get("days"):
            cnt = health_engine.merge_biometrics_days(store, gh_res["days"], source="google_health")
            imported_days += cnt
            providers_used.append("google_health")
        intraday_map = gh_res.get("intraday") or {}
        for d_iso, intr in intraday_map.items():
            if execute:
                health_engine.save_intraday_day(brain, d_iso, intr)
                intraday_written += 1
            day_rec = (store.get("days") or {}).get(d_iso)
            if isinstance(day_rec, dict):
                health_engine.extract_intraday_day_features(day_rec, intr)

    # 2. Garmin Connect (or legacy `--wellness-mock`)
    status = connector_status_summary(brain)
    if wellness_mock or status.get("garmin_connected"):
        g_rows = garmin_pull_wellness(brain, days=min(days, 28), mock_file=wellness_mock, today=ref_today)
        if g_rows:
            cnt = health_engine.merge_biometrics_days(store, g_rows, source="garmin")
            imported_days += cnt
            providers_used.append("garmin")

    # 3. Intervals.icu REST Bridge (runs alongside other providers via field-level merge, never exclusive fallback!)
    if status.get("intervals_connected") and not wellness_mock:
        i_rows = intervals_pull_wellness(brain, days=min(days, 90), today=ref_today)
        if i_rows:
            cnt = health_engine.merge_biometrics_days(store, i_rows, source="intervals")
            imported_days += cnt
            providers_used.append("intervals")

    pruned = health_engine.prune_intraday_cache(brain, keep_days=health_engine.DEFAULT_INTRADAY_RETENTION_DAYS, today=ref_today) if execute else []
    return {
        "imported_days": imported_days,
        "providers": providers_used,
        "intraday_written": intraday_written,
        "intraday_pruned": len(pruned),
        "auth_warnings": auth_warnings,
    }


# ---------------------------------------------------------------------------
# 5. Unified Idempotent Workout Compiler & Device Sync (`coach push`)
# ---------------------------------------------------------------------------

def sync_calendar_workouts_to_devices(
    brain: str,
    cal_items: list[dict[str, Any]],
    parse_workout_spec_fn: Any,
    compile_zwo_xml_fn: Any,
    ftp_watts: int = 275,
    cs_mps: float | None = None,
    days: int = 14,
    execute: bool = True,
    mock_dir: str | None = None,
    today: dt.date | None = None,
    w_prime_j: float | None = None,
    p_5min_w: float | None = None,
    mass_kg: float | None = None,
    creatine_flag: bool | None = None,
    base_tau_w_mult: float = 1.0,
    force_recreate: bool = False,
) -> dict[str, Any]:
    """Compile `.zwo` + `.fit` files, run 1-Hz W'bal/VO2 Compiler-Guard, archive superseded files, and sync to devices."""
    ref_today = today or dt.date.today()
    workouts_dir = os.path.join(brain, "coach", "workouts")
    cache_dir = os.path.join(brain, "_atlas", "cache")
    map_path = os.path.join(cache_dir, "device_sync_map.json")
    if execute:
        os.makedirs(workouts_dir, exist_ok=True)
        os.makedirs(cache_dir, exist_ok=True)

    sync_map: dict[str, dict[str, Any]] = {}
    if os.path.isfile(map_path):
        try:
            with open(map_path, encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                sync_map = loaded
        except Exception:
            sync_map = {}

    status = connector_status_summary(brain)
    hh_active = status["hammerhead_connected"] or bool(mock_dir)
    gc_active = status["garmin_connected"] or bool(mock_dir)

    active_keys: set[str] = set()
    active_filenames: set[str] = set()
    zwo_written: list[str] = []
    fit_written: list[str] = []
    device_actions: list[str] = []
    sim_reports: list[dict[str, Any]] = []
    superseded_archived: list[str] = []

    for it in cal_items or []:
        if it.get("kind") != "workout":
            continue
        st_d = it.get("start")
        if not isinstance(st_d, dt.date):
            continue
        delta = (st_d - ref_today).days
        if not (0 <= delta <= days):
            continue
        d_iso = st_d.isoformat()
        sport = str(it.get("subkind") or "cycling").lower()
        if sport not in ("cycling", "running"):
            continue
        spec = parse_workout_spec_fn(it.get("what") or "", subkind=sport, ftp_watts=ftp_watts)
        slug = re.sub(r"[^a-z0-9]+", "-", str(spec.get("title") or "workout").lower()).strip("-")[:40] or "workout"
        key = f"{d_iso}:{sport}:{slug}"

        # Skipped (`[-]`) workouts must be deleted from remote head units if previously pushed
        if it.get("status") != "open":
            continue

        active_keys.add(key)

        zwo_xml = ""
        if sport == "cycling" and (spec.get("segments") or spec.get("blocks")):
            zwo_xml = compile_zwo_xml_fn(spec, date_str=d_iso, ftp_watts=ftp_watts)
            zwo_name = f"{d_iso}_{slug}.zwo"
            zwo_p = os.path.join(workouts_dir, zwo_name)
            active_filenames.add(zwo_name)
            if execute:
                with open(zwo_p, "w", encoding="utf-8") as fh:
                    fh.write(zwo_xml)
            zwo_written.append(zwo_p)

            # Compiler-Guard: 1-Hz Chorley W'bal + VO2(t) kinetics audit on every compiled cycling workout!
            w_arr, wb_arr = pillars_engine.expand_spec_to_1hz_watts(spec, cp_watts=float(ftp_watts or 275))
            if w_arr:
                sim = pillars_engine.simulate_workout_stream(
                    watts=w_arr,
                    cp_watts=float(ftp_watts or 275),
                    w_prime_j=float(w_prime_j or 20000.0),
                    p_5min_w=p_5min_w,
                    mass_kg=float(mass_kg or 75.0),
                    title=str(spec.get("title") or it.get("what") or ""),
                    work_block_watts=wb_arr,
                    creatine_flag=creatine_flag,
                    base_tau_w_mult=base_tau_w_mult,
                )
                sim["date"] = d_iso
                sim["title"] = str(spec.get("title") or "Workout")
                if spec.get("unparsed_tokens"):
                    sim["unparsed_tokens"] = list(spec["unparsed_tokens"])
                sim_reports.append(sim)

        zwo_sha = hashlib.sha1(zwo_xml.encode("utf-8")).hexdigest()[:8] if zwo_xml else ""
        spec_sig = hashlib.sha1(
            json.dumps({"date": d_iso, "sport": sport, "what": it.get("what"), "ftp": ftp_watts, "zwo": zwo_sha}, sort_keys=True).encode("utf-8")
        ).hexdigest()[:12]

        if spec.get("segments") or spec.get("blocks") or sport in ("cycling", "running"):
            fit_bytes = fit_writer.compile_fit_workout_bytes(
                title=f"{d_iso} {spec.get('title', 'Workout')}",
                sport=sport,
                parsed_spec=spec,
                ftp_watts=ftp_watts,
                cs_mps=cs_mps,
                default_duration_min=int(spec.get("total_min") or 45),
            )
            fit_name = f"{d_iso}_{slug}.fit"
            fit_p = os.path.join(workouts_dir, fit_name)
            active_filenames.add(fit_name)
            if execute:
                with open(fit_p, "wb") as fh:
                    fh.write(fit_bytes)
            fit_written.append(fit_p)

        prev = sync_map.get(key) or {}
        unchanged = (prev.get("spec_sig") == spec_sig) and not force_recreate
        rec = dict(prev)
        rec.update({"date": d_iso, "sport": sport, "title": spec.get("title"), "spec_sig": spec_sig})

        if execute and hh_active and sport == "cycling" and zwo_xml and (not unchanged or not rec.get("hammerhead_id")):
            existing_hh_id = rec.get("hammerhead_id")
            if force_recreate and existing_hh_id:
                ok_del, msg_del = hammerhead_delete_workout(brain, existing_hh_id, mock_dir=mock_dir)
                device_actions.append(msg_del)
                if ok_del:
                    existing_hh_id = None
                    rec.pop("hammerhead_id", None)
            ok_hh, msg_hh, wid_hh = hammerhead_push_or_update(
                brain,
                zwo_xml=zwo_xml,
                filename=f"{d_iso}_{slug}.zwo",
                planned_date=d_iso,
                existing_workout_id=existing_hh_id,
                mock_dir=mock_dir,
            )
            device_actions.append(msg_hh)
            if ok_hh and wid_hh:
                rec["hammerhead_id"] = wid_hh

        if execute and gc_active and sport in ("cycling", "running") and (not unchanged or not rec.get("garmin_id")):
            g_payload = build_garmin_workout_payload(
                title=str(spec.get("title") or "Atlas Workout"),
                sport=sport,
                parsed_spec=spec,
                ftp_watts=ftp_watts,
                cs_mps=cs_mps,
                description=f"Atlas Coach ({d_iso}): {it.get('what', '')}",
            )
            ok_gc, msg_gc, wid_gc = garmin_push_or_update(
                brain,
                payload=g_payload,
                planned_date=d_iso,
                existing_workout_id=rec.get("garmin_id"),
                mock_dir=mock_dir,
            )
            device_actions.append(msg_gc)
            if ok_gc and wid_gc:
                rec["garmin_id"] = wid_gc

        sync_map[key] = rec

    # Archive superseded local .zwo/.fit files inside the active [ref_today..ref_today+days] horizon (never delete!)
    end_iso = (ref_today + dt.timedelta(days=days)).isoformat()
    start_iso = ref_today.isoformat()
    if execute and os.path.isdir(workouts_dir):
        sup_dir = os.path.join(workouts_dir, "_superseded")
        for fn in sorted(os.listdir(workouts_dir)):
            if not fn.endswith((".zwo", ".fit")):
                continue
            m_d = re.match(r"^(\d{4}-\d{2}-\d{2})_", fn)
            if not m_d:
                continue
            f_date = m_d.group(1)
            if start_iso <= f_date <= end_iso and fn not in active_filenames:
                os.makedirs(sup_dir, exist_ok=True)
                src_f = os.path.join(workouts_dir, fn)
                dst_f = os.path.join(sup_dir, fn)
                try:
                    os.replace(src_f, dst_f)
                    superseded_archived.append(fn)
                except OSError:
                    pass

    # Clean up future workouts that were skipped (`[-]`), moved, or removed from calendar
    stale_keys = [
        k for k, v in list(sync_map.items())
        if k not in active_keys and str(v.get("date") or "") >= ref_today.isoformat()
    ]
    for k in stale_keys:
        old = sync_map.pop(k, {})
        if execute and hh_active and old.get("hammerhead_id"):
            _, m_del = hammerhead_delete_workout(brain, str(old["hammerhead_id"]), mock_dir=mock_dir)
            device_actions.append(m_del)
        if execute and gc_active and old.get("garmin_id"):
            _, m_del = garmin_delete_workout(brain, str(old["garmin_id"]), mock_dir=mock_dir)
            device_actions.append(m_del)

    if execute:
        with open(map_path, "w", encoding="utf-8") as fh:
            json.dump(sync_map, fh, indent=2, ensure_ascii=False)

    return {
        "zwo_files": zwo_written,
        "fit_files": fit_written,
        "device_actions": device_actions,
        "active_synced": len(active_keys),
        "sim_reports": sim_reports,
        "superseded_archived": superseded_archived,
    }
