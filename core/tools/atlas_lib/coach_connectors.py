"""Hardware & Wellness Connectors for Atlas Coach (Hammerhead Karoo, Garmin Connect, Intervals.icu).

Python 3 standard library only (`base64`, `datetime`, `hashlib`, `json`, `os`, `re`,
`subprocess`, `sys`, `time`, `urllib`, `uuid`). Credentials live exclusively in
`brain/_secrets/{hammerhead,garmin,intervals}.env` (`0o600`, git-ignored).
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
from . import pillars_engine

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


def connector_status_summary(brain: str) -> dict[str, Any]:
    """Return connection flags for Strava, Hammerhead Karoo, Garmin Connect, and Intervals.icu."""
    strava = load_secret_env(brain, "strava")
    hh = load_secret_env(brain, "hammerhead")
    garmin = load_secret_env(brain, "garmin")
    icu = load_secret_env(brain, "intervals")
    return {
        "strava_connected": bool(strava.get("STRAVA_REFRESH_TOKEN") or strava.get("STRAVA_ACCESS_TOKEN")),
        "hammerhead_connected": bool(hh.get("HAMMERHEAD_REFRESH_TOKEN") or hh.get("HAMMERHEAD_ACCESS_TOKEN")),
        "hammerhead_configured": True,
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
        spec_sig = hashlib.sha1(
            json.dumps({"date": d_iso, "sport": sport, "what": it.get("what"), "ftp": ftp_watts}, sort_keys=True).encode("utf-8")
        ).hexdigest()[:12]

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
                )
                sim["date"] = d_iso
                sim["title"] = str(spec.get("title") or "Workout")
                if spec.get("unparsed_tokens"):
                    sim["unparsed_tokens"] = list(spec["unparsed_tokens"])
                sim_reports.append(sim)

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
        unchanged = prev.get("spec_sig") == spec_sig
        rec = dict(prev)
        rec.update({"date": d_iso, "sport": sport, "title": spec.get("title"), "spec_sig": spec_sig})

        if execute and hh_active and sport == "cycling" and zwo_xml and (not unchanged or not rec.get("hammerhead_id")):
            ok_hh, msg_hh, wid_hh = hammerhead_push_or_update(
                brain,
                zwo_xml=zwo_xml,
                filename=f"{d_iso}_{slug}.zwo",
                planned_date=d_iso,
                existing_workout_id=rec.get("hammerhead_id"),
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
