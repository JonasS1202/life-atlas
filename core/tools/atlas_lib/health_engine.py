"""Provider-Agnostic Biometrics Store, 30-Day Intraday Cache & Strava Temporal Alignment Engine.

Stage 1 deterministic computation (0 LLM tokens, Python 3 standard library only):
- Canonical timeless daily biometrics store (`brain/coach/biometrics.json`, ~0.7 KB/day)
- Rolling git-ignored intraday raw cache (`brain/_atlas/cache/health/intraday/YYYY-MM-DD.json`, default 30 days)
- Multi-provider reconciliation (`manual` subjective wins; sensor priority `google_health > garmin > health_connect > intervals`)
- Strava <-> Biometrics temporal alignment (Pre-Workout NEAT/sleep, Finish-to-Bedtime, Post-Workout HR recovery, T->T+1 autonomic rebound)
- Multi-marker Illness Early-Warning (`>= 2` objective markers -> `ROT`), sleep architecture/debt & KFA drift protection
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import re
import statistics
from typing import Any

BIOMETRICS_SCHEMA_VERSION = 1
DEFAULT_INTRADAY_RETENTION_DAYS = 30

SOURCE_PRIORITY = {
    "google_health": 40,
    "garmin": 30,
    "health_connect": 25,
    "intervals": 20,
    "manual": 10,
}


# ---------------------------------------------------------------------------
# 1. Timestamp & Offset Helpers (UTC <-> Local Civil Time, Python stdlib only)
# ---------------------------------------------------------------------------

def parse_utc_offset_seconds(offset_val: Any) -> int:
    """Parse Google Health offset strings like `'3600s'`, `'-18000s'`, or numeric seconds into int seconds."""
    if offset_val is None or offset_val == "":
        return 0
    if isinstance(offset_val, (int, float)):
        return int(round(float(offset_val)))
    s = str(offset_val).strip()
    m = re.match(r"^([+-]?\d+(?:\.\d+)?)s?$", s)
    if m:
        return int(round(float(m.group(1))))
    m_hm = re.match(r"^([+-])(\d{2}):?(\d{2})$", s)
    if m_hm:
        sign = -1 if m_hm.group(1) == "-" else 1
        return sign * (int(m_hm.group(2)) * 3600 + int(m_hm.group(3)) * 60)
    return 0


def parse_iso_utc(ts_str: str | None) -> dt.datetime | None:
    """Parse RFC 3339 / ISO 8601 timestamp into timezone-aware UTC `datetime`."""
    if not ts_str:
        return None
    s = str(ts_str).strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(s)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(dt.timezone.utc)
    except ValueError:
        return None


def utc_to_local_iso(ts_utc: str | dt.datetime | None, utc_offset_s: int = 0) -> str | None:
    """Convert UTC timestamp + offset seconds to local civil ISO string `'YYYY-MM-DDTHH:MM:SS'`."""
    d_obj = ts_utc if isinstance(ts_utc, dt.datetime) else parse_iso_utc(ts_utc)
    if d_obj is None:
        return None
    local_dt = d_obj + dt.timedelta(seconds=int(utc_offset_s or 0))
    return local_dt.strftime("%Y-%m-%dT%H:%M:%S")


def parse_local_iso(ts_local: str | None) -> dt.datetime | None:
    """Parse local ISO string `'YYYY-MM-DDTHH:MM:SS'` (strips trailing Z/offset if present) to naive local datetime."""
    if not ts_local:
        return None
    s = str(ts_local).strip()
    m = re.match(r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})(?::(\d{2}))?", s)
    if not m:
        return None
    sec = m.group(3) or "00"
    try:
        return dt.datetime.fromisoformat(f"{m.group(1)}T{m.group(2)}:{sec}")
    except ValueError:
        return None


def civil_date_dict_to_iso(d_obj: Any) -> str | None:
    """Convert Google Health `{'year': 2026, 'month': 11, 'day': 20}` or ISO string into `'YYYY-MM-DD'`."""
    if isinstance(d_obj, str):
        m = re.match(r"^(\d{4}-\d{2}-\d{2})", d_obj.strip())
        return m.group(1) if m else None
    if isinstance(d_obj, dict):
        try:
            y = int(d_obj["year"])
            mo = int(d_obj["month"])
            da = int(d_obj["day"])
            return dt.date(y, mo, da).isoformat()
        except (KeyError, TypeError, ValueError):
            return None
    return None


def safe_float(val: Any) -> float | None:
    if val is None or val == "":
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def safe_int(val: Any) -> int | None:
    f = safe_float(val)
    return int(round(f)) if f is not None else None


# ---------------------------------------------------------------------------
# 2. Canonical Biometrics Store (`brain/coach/biometrics.json`) & Intraday Cache
# ---------------------------------------------------------------------------

def biometrics_path(brain: str) -> str:
    return os.path.join(brain, "coach", "biometrics.json")


def intraday_cache_dir(brain: str) -> str:
    return os.path.join(brain, "_atlas", "cache", "health", "intraday")


def empty_day_record(date_iso: str) -> dict[str, Any]:
    return {
        "date": str(date_iso)[:10],
        "sleep": {},
        "autonomic": {},
        "body": {},
        "daytime_load": {},
        "subjective": {},
        "pmc_snapshot": {},
        "workout_links": [],
        "sources": {},
    }


def load_biometrics_store(brain: str) -> dict[str, Any]:
    """Load canonical `brain/coach/biometrics.json` (returns empty v1 schema if absent/corrupt)."""
    p = biometrics_path(brain)
    if os.path.isfile(p):
        try:
            with open(p, encoding="utf-8", errors="replace") as fh:
                raw = json.load(fh)
            if isinstance(raw, dict) and isinstance(raw.get("days"), dict):
                return raw
        except (OSError, ValueError):
            pass
    return {
        "schema_version": BIOMETRICS_SCHEMA_VERSION,
        "updated": "",
        "days": {},
    }


def save_biometrics_store(brain: str, store: dict[str, Any]) -> str:
    """Persist `brain/coach/biometrics.json` cleanly sorted (newest day first) for diff-friendly Git commits."""
    p = biometrics_path(brain)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    days_raw = store.get("days") if isinstance(store.get("days"), dict) else {}
    sorted_days = {k: days_raw[k] for k in sorted(days_raw.keys(), reverse=True) if re.match(r"^\d{4}-\d{2}-\d{2}$", str(k))}
    out = {
        "schema_version": BIOMETRICS_SCHEMA_VERSION,
        "updated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "days": sorted_days,
    }
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, p)
    return p


def load_intraday_day(brain: str, date_iso: str) -> dict[str, Any] | None:
    """Load a single day's git-ignored intraday raw cache (`hr_5m`, `hrv_samples`, `sleep_stages`)."""
    p = os.path.join(intraday_cache_dir(brain), f"{str(date_iso)[:10]}.json")
    if not os.path.isfile(p):
        return None
    try:
        with open(p, encoding="utf-8", errors="replace") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def save_intraday_day(brain: str, date_iso: str, payload: dict[str, Any]) -> str:
    """Persist a single day's intraday raw series in git-ignored `_atlas/cache/health/intraday/`."""
    d_dir = intraday_cache_dir(brain)
    os.makedirs(d_dir, exist_ok=True)
    d_key = str(date_iso)[:10]
    p = os.path.join(d_dir, f"{d_key}.json")
    existing = load_intraday_day(brain, d_key) or {}
    merged = dict(existing)
    merged["date"] = d_key
    merged["updated"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for k in ("source", "hr_5m", "hrv_samples", "sleep_stages", "utc_offset_s"):
        if payload.get(k) is not None and payload.get(k) != []:
            merged[k] = payload[k]
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(merged, fh, separators=(",", ":"), ensure_ascii=False)
    return p


def prune_intraday_cache(
    brain: str,
    keep_days: int = DEFAULT_INTRADAY_RETENTION_DAYS,
    today: dt.date | None = None,
) -> list[str]:
    """Remove rolling intraday cache files strictly older than `keep_days` (never touches `brain/coach/`)."""
    d_dir = intraday_cache_dir(brain)
    if not os.path.isdir(d_dir):
        return []
    ref_today = today or dt.date.today()
    cutoff = (ref_today - dt.timedelta(days=max(1, int(keep_days)))).isoformat()
    removed: list[str] = []
    for fn in sorted(os.listdir(d_dir)):
        m = re.match(r"^(\d{4}-\d{2}-\d{2})\.json$", fn)
        if not m:
            continue
        if m.group(1) < cutoff:
            try:
                os.remove(os.path.join(d_dir, fn))
                removed.append(fn)
            except OSError:
                pass
    return removed


# ---------------------------------------------------------------------------
# 3. Multi-Provider Reconciliation & Bidirectional `readiness.md` Bridge
# ---------------------------------------------------------------------------

def _source_rank(src: str | None) -> int:
    return SOURCE_PRIORITY.get(str(src or "").lower(), 0)


def _should_replace_sensor_field(
    cur_val: Any,
    cur_src: str | None,
    new_val: Any,
    new_src: str,
    force_overwrite: bool = False,
) -> bool:
    if new_val is None or new_val == "" or new_val == []:
        return False
    if force_overwrite or cur_val is None or cur_val == "" or cur_val == []:
        return True
    if str(cur_src or "").lower() == str(new_src).lower():
        return True
    return _source_rank(new_src) >= _source_rank(cur_src)


def merge_biometrics_days(
    store: dict[str, Any],
    incoming_days: list[dict[str, Any]] | dict[str, dict[str, Any]],
    source: str = "google_health",
) -> int:
    """Merge provider daily biometrics into `store['days']` with field-level provenance and priority.

    - Subjective fields (`soreness`, `leg_pain`, `fatigue_7d`, `rpe`, `notes`): manual entries are NEVER overwritten by sensors.
    - Sensor fields (`sleep`, `autonomic`, `body`, `daytime_load`): merged by provider priority (`google_health > garmin > health_connect > intervals > manual`).
    """
    days_map = store.setdefault("days", {})
    items = list(incoming_days.values()) if isinstance(incoming_days, dict) else list(incoming_days or [])
    touched = 0

    for inc in items:
        if not isinstance(inc, dict) or not inc.get("date"):
            continue
        d_key = str(inc["date"])[:10]
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", d_key):
            continue
        inc_src = str(inc.get("source") or source).lower()
        cur = days_map.get(d_key)
        if not isinstance(cur, dict):
            cur = empty_day_record(d_key)
        else:
            for sec in ("sleep", "autonomic", "body", "daytime_load", "subjective", "pmc_snapshot", "sources"):
                if not isinstance(cur.get(sec), dict):
                    cur[sec] = {}
            if not isinstance(cur.get("workout_links"), list):
                cur["workout_links"] = []

        srcs = cur["sources"]
        changed = False

        # Sensor sections: sleep, autonomic, body, daytime_load
        for sec in ("sleep", "autonomic", "body", "daytime_load"):
            inc_sec = inc.get(sec)
            if not isinstance(inc_sec, dict):
                continue
            cur_sec = cur[sec]
            for fld, val in inc_sec.items():
                if val is None or val == "":
                    continue
                if fld == "hourly_steps":
                    if not isinstance(val, list) or len(val) != 24 or sum(int(x or 0) for x in val) == 0:
                        continue
                src_k = f"{sec}.{fld}"
                if _should_replace_sensor_field(cur_sec.get(fld), srcs.get(src_k), val, inc_src):
                    cur_sec[fld] = val
                    srcs[src_k] = inc_src
                    changed = True

        # Subjective section: manual always wins over sensor providers
        inc_subj = inc.get("subjective")
        if isinstance(inc_subj, dict):
            cur_subj = cur["subjective"]
            for fld, val in inc_subj.items():
                if val is None or val == "":
                    continue
                src_k = f"subjective.{fld}"
                existing_src = str(srcs.get(src_k) or "").lower()
                if inc_src == "manual":
                    cur_subj[fld] = val
                    srcs[src_k] = "manual"
                    changed = True
                elif cur_subj.get(fld) is None and existing_src != "manual":
                    cur_subj[fld] = val
                    srcs[src_k] = inc_src
                    changed = True

        # Optional flat legacy wellness keys (from Garmin/Intervals/manual adapters)
        flat_map = (
            ("rhr", "autonomic", "rhr_bpm"),
            ("hrv_rmssd", "autonomic", "hrv_rmssd_ms"),
            ("body_battery", "autonomic", "body_battery"),
            ("sleep_h", "sleep", "duration_h"),
            ("sleep_quality", "sleep", "quality"),
            ("weight_kg", "body", "weight_kg"),
            ("body_fat_pct", "body", "body_fat_pct"),
        )
        for flat_k, sec, fld in flat_map:
            val = inc.get(flat_k)
            if val is None or val == "":
                continue
            src_k = f"{sec}.{fld}"
            if _should_replace_sensor_field(cur[sec].get(fld), srcs.get(src_k), val, inc_src):
                cur[sec][fld] = val
                srcs[src_k] = inc_src
                changed = True

        for subj_k in ("rpe", "soreness", "leg_pain", "fatigue_7d", "notes"):
            val = inc.get(subj_k)
            if val is None or val == "":
                continue
            src_k = f"subjective.{subj_k}"
            if inc_src == "manual" or (cur["subjective"].get(subj_k) is None and srcs.get(src_k) != "manual"):
                cur["subjective"][subj_k] = val
                srcs[src_k] = inc_src
                changed = True

        if changed or d_key not in days_map:
            days_map[d_key] = cur
            touched += 1

    return touched


def merge_readiness_rows_into_biometrics(
    store: dict[str, Any],
    readiness_rows: list[dict[str, Any]],
) -> int:
    """Import manual / existing `readiness.md` rows into `biometrics.json` without overriding richer sensor data."""
    payloads: list[dict[str, Any]] = []
    for r in readiness_rows or []:
        if not isinstance(r, dict) or not r.get("date"):
            continue
        notes_clean = re.sub(
            r"\s*\[(?:hrv:[^\]]+|wt:[^\]]+|legs:\d|fat7d:\d|temp:[^\]]+|spo2:[^\]]+|steps:[^\]]+)\]",
            "",
            str(r.get("notes") or ""),
        ).strip()
        row_obj: dict[str, Any] = {
            "date": str(r["date"])[:10],
            "source": "manual",
            "rhr": r.get("rhr"),
            "hrv_rmssd": r.get("hrv_rmssd"),
            "sleep_h": r.get("sleep_h"),
            "sleep_quality": r.get("sleep_quality"),
            "weight_kg": r.get("weight_kg"),
            "rpe": r.get("rpe"),
            "soreness": r.get("soreness"),
            "leg_pain": r.get("leg_pain"),
            "fatigue_7d": r.get("fatigue_7d"),
            "notes": notes_clean if notes_clean and notes_clean != "—" else None,
        }
        payloads.append(row_obj)
    return merge_biometrics_days(store, payloads, source="manual")


def derive_sleep_quality(sleep_obj: dict[str, Any]) -> str | None:
    """Derive a human-readable quality label (`good` | `fair` | `poor` | `terrible`) from sleep architecture."""
    if not isinstance(sleep_obj, dict):
        return None
    if sleep_obj.get("quality"):
        return str(sleep_obj["quality"])
    dur_h = safe_float(sleep_obj.get("duration_h"))
    if dur_h is None:
        return None
    eff = safe_float(sleep_obj.get("efficiency_pct"))
    deep_m = safe_float(sleep_obj.get("deep_min"))
    rem_m = safe_float(sleep_obj.get("rem_min"))
    asleep_m = safe_float(sleep_obj.get("asleep_min")) or (dur_h * 60.0)
    score = 0.0
    # Duration component (max 55 pts)
    if dur_h >= 7.5:
        score += 55.0
    elif dur_h >= 6.8:
        score += 45.0
    elif dur_h >= 6.0:
        score += 34.0
    elif dur_h >= 5.2:
        score += 22.0
    else:
        score += 10.0
    # Efficiency component (max 20 pts)
    if eff is not None:
        score += 20.0 if eff >= 90.0 else (15.0 if eff >= 84.0 else (9.0 if eff >= 78.0 else 3.0))
    else:
        score += 14.0
    # Deep + REM restorative architecture (max 25 pts)
    if asleep_m and asleep_m > 0 and (deep_m is not None or rem_m is not None):
        rest_pct = (((deep_m or 0.0) + (rem_m or 0.0)) / asleep_m) * 100.0
        score += 25.0 if rest_pct >= 38.0 else (19.0 if rest_pct >= 30.0 else (11.0 if rest_pct >= 22.0 else 4.0))
    else:
        score += 16.0
    if score >= 80.0:
        return "good"
    if score >= 60.0:
        return "fair"
    if score >= 42.0:
        return "poor"
    return "terrible"


def biometrics_to_readiness_rows(
    store: dict[str, Any],
    fallback_rows: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Flatten `biometrics.json` (plus any unmerged `readiness.md` rows) into Stage-1-compatible readiness rows."""
    by_date: dict[str, dict[str, Any]] = {}
    for fr in fallback_rows or []:
        if isinstance(fr, dict) and fr.get("date"):
            by_date[str(fr["date"])[:10]] = dict(fr)

    days_map = store.get("days") if isinstance(store, dict) and isinstance(store.get("days"), dict) else {}
    for d_key, day in days_map.items():
        if not isinstance(day, dict):
            continue
        slp = day.get("sleep") if isinstance(day.get("sleep"), dict) else {}
        aut = day.get("autonomic") if isinstance(day.get("autonomic"), dict) else {}
        bod = day.get("body") if isinstance(day.get("body"), dict) else {}
        day_l = day.get("daytime_load") if isinstance(day.get("daytime_load"), dict) else {}
        sub = day.get("subjective") if isinstance(day.get("subjective"), dict) else {}
        pmc = day.get("pmc_snapshot") if isinstance(day.get("pmc_snapshot"), dict) else {}

        base = dict(by_date.get(d_key) or {"date": d_key})
        row: dict[str, Any] = {
            "date": d_key,
            "rhr": safe_int(aut.get("rhr_bpm")) if aut.get("rhr_bpm") is not None else base.get("rhr"),
            "sleep_h": round(float(slp["duration_h"]), 2) if slp.get("duration_h") is not None else base.get("sleep_h"),
            "sleep_quality": derive_sleep_quality(slp) or base.get("sleep_quality"),
            "rpe": safe_float(sub.get("rpe")) if sub.get("rpe") is not None else base.get("rpe"),
            "soreness": safe_int(sub.get("soreness")) if sub.get("soreness") is not None else base.get("soreness"),
            "leg_pain": safe_int(sub.get("leg_pain")) if sub.get("leg_pain") is not None else base.get("leg_pain"),
            "fatigue_7d": safe_int(sub.get("fatigue_7d")) if sub.get("fatigue_7d") is not None else base.get("fatigue_7d"),
            "hrv_rmssd": round(float(aut["hrv_rmssd_ms"]), 1) if aut.get("hrv_rmssd_ms") is not None else base.get("hrv_rmssd"),
            "hrv_deep_rmssd": round(float(aut["hrv_deep_rmssd_ms"]), 1) if aut.get("hrv_deep_rmssd_ms") is not None else None,
            "hrv_non_rem_hr": safe_int(aut.get("hrv_non_rem_hr_bpm")),
            "hrv_entropy": round(float(aut["hrv_entropy"]), 3) if aut.get("hrv_entropy") is not None else None,
            "weight_kg": round(float(bod["weight_kg"]), 2) if bod.get("weight_kg") is not None else base.get("weight_kg"),
            "body_fat_pct": round(float(bod["body_fat_pct"]), 1) if bod.get("body_fat_pct") is not None else None,
            "resp_rate_brpm": round(float(aut["resp_rate_brpm"]), 1) if aut.get("resp_rate_brpm") is not None else None,
            "skin_temp_delta_c": round(float(aut["skin_temp_delta_c"]), 2) if aut.get("skin_temp_delta_c") is not None else None,
            "skin_temp_stddev_30d_c": round(float(aut["skin_temp_stddev_30d_c"]), 2) if aut.get("skin_temp_stddev_30d_c") is not None else None,
            "spo2_avg_pct": round(float(aut["spo2_avg_pct"]), 1) if aut.get("spo2_avg_pct") is not None else None,
            "spo2_min_pct": round(float(aut["spo2_min_pct"]), 1) if aut.get("spo2_min_pct") is not None else None,
            "body_battery": safe_int(aut.get("body_battery")),
            "sleep_start_local": slp.get("start_local"),
            "sleep_end_local": slp.get("end_local"),
            "sleep_start_utc": slp.get("start_utc"),
            "sleep_end_utc": slp.get("end_utc"),
            "deep_min": safe_int(slp.get("deep_min")),
            "rem_min": safe_int(slp.get("rem_min")),
            "light_min": safe_int(slp.get("light_min")),
            "awake_min": safe_int(slp.get("awake_min")),
            "efficiency_pct": round(float(slp["efficiency_pct"]), 1) if slp.get("efficiency_pct") is not None else None,
            "hr_nadir_local": slp.get("hr_nadir_local"),
            "hr_nadir_pct": round(float(slp["hr_nadir_pct"]), 1) if slp.get("hr_nadir_pct") is not None else None,
            "hr_sleep_avg": round(float(slp["hr_sleep_avg"]), 1) if slp.get("hr_sleep_avg") is not None else None,
            "hr_sleep_min": safe_int(slp.get("hr_sleep_min")),
            "steps_total": safe_int(day_l.get("steps_total")),
            "hourly_steps": day_l.get("hourly_steps") if isinstance(day_l.get("hourly_steps"), list) else None,
            "azm_total_min": safe_int(day_l.get("azm_total_min")),
            "ctl": safe_float(pmc.get("ctl")) if pmc.get("ctl") is not None else base.get("ctl"),
            "atl": safe_float(pmc.get("atl")) if pmc.get("atl") is not None else base.get("atl"),
            "tsb": safe_float(pmc.get("tsb")) if pmc.get("tsb") is not None else base.get("tsb"),
            "acwr": safe_float(pmc.get("acwr")) if pmc.get("acwr") is not None else base.get("acwr"),
            "status": str(day.get("status") or base.get("status") or ""),
            "notes": str(sub.get("notes") if sub.get("notes") is not None else (base.get("notes") or "")),
            "workout_links": day.get("workout_links") if isinstance(day.get("workout_links"), list) else [],
            "sources": day.get("sources") if isinstance(day.get("sources"), dict) else {},
        }
        by_date[d_key] = row

    rows = list(by_date.values())
    rows.sort(key=lambda r: str(r.get("date") or ""), reverse=True)
    return rows


# ---------------------------------------------------------------------------
# 4. Intraday Feature Extraction (HR Nadir, Nocturnal HRV Stats, Post-Workout HR)
# ---------------------------------------------------------------------------

def extract_intraday_day_features(
    day_rec: dict[str, Any],
    intraday: dict[str, Any] | None,
) -> dict[str, Any]:
    """Extract permanent sleep-HR nadir and intraday HRV summary features from a rolling raw cache day."""
    if not isinstance(day_rec, dict) or not isinstance(intraday, dict):
        return day_rec
    slp = day_rec.setdefault("sleep", {})
    aut = day_rec.setdefault("autonomic", {})
    srcs = day_rec.setdefault("sources", {})
    src_name = str(intraday.get("source") or "google_health")

    # 1. Nocturnal HR Nadir & Sleep HR Avg/Min from 5-min HR rollups inside the main sleep window
    hr_5m = intraday.get("hr_5m") if isinstance(intraday.get("hr_5m"), list) else []
    s_start = parse_iso_utc(slp.get("start_utc"))
    s_end = parse_iso_utc(slp.get("end_utc"))
    utc_off = int(slp.get("utc_offset_s") or intraday.get("utc_offset_s") or 0)
    if hr_5m and s_start and s_end and s_end > s_start:
        total_span_s = (s_end - s_start).total_seconds()
        sleep_pts: list[tuple[dt.datetime, float, float]] = []
        for pt in hr_5m:
            if not isinstance(pt, dict):
                continue
            t0 = parse_iso_utc(pt.get("start_utc"))
            avg_bpm = safe_float(pt.get("avg"))
            min_bpm = safe_float(pt.get("min")) or avg_bpm
            if t0 and avg_bpm and avg_bpm > 25.0 and s_start <= t0 <= s_end:
                sleep_pts.append((t0, avg_bpm, min_bpm or avg_bpm))
        if len(sleep_pts) >= 3:
            sleep_pts.sort(key=lambda x: x[0])
            avg_all = sum(x[1] for x in sleep_pts) / len(sleep_pts)
            min_all = min(x[2] for x in sleep_pts)
            # Smooth nadir over a 15-min window (3 x 5m buckets) to avoid single-sample sensor artifacts
            best_win_val = 999.0
            best_win_t = sleep_pts[0][0]
            win_k = min(3, len(sleep_pts))
            for i in range(len(sleep_pts) - win_k + 1):
                w_avg = sum(sleep_pts[j][1] for j in range(i, i + win_k)) / win_k
                if w_avg < best_win_val:
                    best_win_val = w_avg
                    best_win_t = sleep_pts[i + win_k // 2][0]
            nadir_local = utc_to_local_iso(best_win_t, utc_off)
            nadir_pct = ((best_win_t - s_start).total_seconds() / total_span_s) * 100.0 if total_span_s > 0 else 50.0
            slp["hr_sleep_avg"] = round(avg_all, 1)
            slp["hr_sleep_min"] = int(round(min_all))
            slp["hr_nadir_local"] = nadir_local[11:16] if nadir_local else None
            slp["hr_nadir_pct"] = round(max(0.0, min(100.0, nadir_pct)), 1)
            srcs["sleep.hr_nadir_local"] = src_name

    # 2. Intraday HRV RMSSD min/max and LF/HF autonomic ratio
    hrv_s = intraday.get("hrv_samples") if isinstance(intraday.get("hrv_samples"), list) else []
    vals = [safe_float(x.get("rmssd")) for x in hrv_s if isinstance(x, dict) and safe_float(x.get("rmssd"))]
    vals_clean = [v for v in vals if v is not None and 5.0 <= v <= 300.0]
    if vals_clean:
        aut["hrv_min_ms"] = round(min(vals_clean), 1)
        aut["hrv_max_ms"] = round(max(vals_clean), 1)
        aut["hrv_samples_n"] = len(vals_clean)
        if aut.get("hrv_rmssd_ms") is None:
            aut["hrv_rmssd_ms"] = round(sum(vals_clean) / len(vals_clean), 1)
            srcs["autonomic.hrv_rmssd_ms"] = src_name
    ratios = []
    for x in hrv_s:
        if not isinstance(x, dict):
            continue
        lf = safe_float(x.get("lf"))
        hf = safe_float(x.get("hf"))
        if lf and hf and hf > 1.0:
            ratios.append(lf / hf)
    if ratios:
        aut["hrv_lf_hf_ratio"] = round(statistics.median(ratios), 2)

    return day_rec


def compute_post_workout_hr_recovery(
    end_utc: dt.datetime,
    hr_points: list[dict[str, Any]],
    baseline_rhr: float | None = None,
) -> dict[str, Any]:
    """Compute post-workout HR recovery (`0-30m`, `0-2h`, and minutes until return to resting band)."""
    if not hr_points:
        return {}
    pts: list[tuple[float, float]] = []
    for pt in hr_points:
        if not isinstance(pt, dict):
            continue
        t0 = parse_iso_utc(pt.get("start_utc"))
        avg_bpm = safe_float(pt.get("avg"))
        if not t0 or avg_bpm is None or avg_bpm < 30.0:
            continue
        delta_min = (t0 - end_utc).total_seconds() / 60.0
        if 0.0 <= delta_min <= 240.0:
            pts.append((delta_min, avg_bpm))
    if not pts:
        return {}
    pts.sort(key=lambda x: x[0])
    w30 = [b for m, b in pts if 0.0 <= m <= 30.0]
    w120 = [b for m, b in pts if 0.0 <= m <= 120.0]
    out: dict[str, Any] = {}
    if w30:
        out["post_hr_avg_0_30m"] = round(sum(w30) / len(w30), 1)
    if w120:
        out["post_hr_avg_0_2h"] = round(sum(w120) / len(w120), 1)
    # Time until 2 consecutive 5m buckets drop below `max(rhr + 15, rhr * 1.25)`
    rhr_ref = float(baseline_rhr) if (baseline_rhr and baseline_rhr >= 30.0) else 55.0
    thresh = max(rhr_ref + 15.0, rhr_ref * 1.25)
    for idx in range(len(pts)):
        if pts[idx][1] <= thresh:
            if idx + 1 >= len(pts) or pts[idx + 1][1] <= thresh + 3.0:
                out["post_hr_return_min"] = int(round(pts[idx][0]))
                break
    return out


# ---------------------------------------------------------------------------
# 5. Strava <-> Biometrics Temporal Alignment Engine
# ---------------------------------------------------------------------------

def _pre_workout_steps_from_hourly(hourly: list[Any] | None, start_local: dt.datetime | None) -> int | None:
    if not isinstance(hourly, list) or len(hourly) != 24 or start_local is None:
        return None
    hr_idx = max(0, min(23, start_local.hour))
    full_hours = sum(int(hourly[h] or 0) for h in range(hr_idx))
    partial = int(round(int(hourly[hr_idx] or 0) * (start_local.minute / 60.0)))
    return full_hours + partial


def align_activities_with_biometrics(
    activities: list[dict[str, Any]],
    store: dict[str, Any],
    brain: str | None = None,
    intraday_map: dict[str, dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Temporally align Strava activities with pre-workout, post-workout, and next-night biometrics.

    Updates `store['days'][date]['workout_links']` deterministically (preserving previously extracted
    post-workout HR recovery metrics even after the 30-day rolling raw intraday cache ages out!).
    Returns `{activity_id: link_dict}`.
    """
    days_map = store.get("days") if isinstance(store, dict) and isinstance(store.get("days"), dict) else {}
    if not days_map:
        return {}

    # Precompute 28d median RHR & HRV for delta calculation
    all_dates = sorted(days_map.keys())
    rhr_by_d = {d: safe_float((days_map[d].get("autonomic") or {}).get("rhr_bpm")) for d in all_dates}
    hrv_by_d = {d: safe_float((days_map[d].get("autonomic") or {}).get("hrv_rmssd_ms")) for d in all_dates}

    def _baseline_before(target_d: str, series_map: dict[str, float | None], win_days: int = 28) -> float | None:
        try:
            dt_t = dt.date.fromisoformat(target_d)
        except ValueError:
            return None
        lo = (dt_t - dt.timedelta(days=win_days)).isoformat()
        vals = [v for d, v in series_map.items() if v is not None and lo <= d < target_d]
        return statistics.median(vals) if len(vals) >= 3 else None

    def _get_intraday_hr(date_iso: str) -> list[dict[str, Any]]:
        if intraday_map is not None and date_iso in intraday_map:
            obj = intraday_map[date_iso]
        elif brain:
            obj = load_intraday_day(brain, date_iso)
        else:
            obj = None
        return list(obj.get("hr_5m") or []) if isinstance(obj, dict) else []

    # Preserve previously stored post_hr fields so 30d rolling cache pruning never loses them
    prev_links_by_id: dict[str, dict[str, Any]] = {}
    for day in days_map.values():
        if isinstance(day, dict):
            for wl in day.get("workout_links") or []:
                if isinstance(wl, dict) and wl.get("activity_id"):
                    prev_links_by_id[str(wl["activity_id"])] = dict(wl)
            day["workout_links"] = []

    links_by_act_id: dict[str, dict[str, Any]] = {}

    for act in activities or []:
        if not isinstance(act, dict) or not act.get("date"):
            continue
        aid = str(act.get("id") or "")
        d_iso = str(act["date"])[:10]
        if d_iso not in days_map:
            # Check if next day exists (for next-night recovery linkage)
            try:
                nxt_check = (dt.date.fromisoformat(d_iso) + dt.timedelta(days=1)).isoformat()
            except ValueError:
                continue
            if nxt_check not in days_map:
                continue
        day_rec = days_map.get(d_iso) or {}
        try:
            d_obj = dt.date.fromisoformat(d_iso)
            next_iso = (d_obj + dt.timedelta(days=1)).isoformat()
        except ValueError:
            continue
        next_rec = days_map.get(next_iso) or {}

        start_utc = parse_iso_utc(act.get("start_time_utc"))
        end_utc = parse_iso_utc(act.get("end_time_utc"))
        start_loc = parse_local_iso(act.get("start_time_local"))
        dur_s = int(act.get("elapsed_sec") or (int(act.get("duration_min") or 0) * 60))
        if start_utc and not end_utc and dur_s > 0:
            end_utc = start_utc + dt.timedelta(seconds=dur_s)

        prev_wl = prev_links_by_id.get(aid, {})
        link: dict[str, Any] = {
            "activity_id": aid,
            "date": d_iso,
            "name": str(act.get("name") or "Workout"),
            "sport": str(act.get("sport") or "cycling"),
            "start_local": act.get("start_time_local") or None,
            "end_local": utc_to_local_iso(end_utc, int(act.get("utc_offset_s") or 0)) if end_utc and act.get("utc_offset_s") is not None else None,
        }

        # Pre-Workout Sleep & Daytime NEAT
        slp_d = day_rec.get("sleep") if isinstance(day_rec.get("sleep"), dict) else {}
        dl_d = day_rec.get("daytime_load") if isinstance(day_rec.get("daytime_load"), dict) else {}
        if slp_d.get("duration_h") is not None:
            link["pre_sleep_h"] = slp_d["duration_h"]
        if slp_d.get("end_utc") and start_utc:
            slp_end_dt = parse_iso_utc(slp_d["end_utc"])
            if slp_end_dt:
                h_awake = (start_utc - slp_end_dt).total_seconds() / 3600.0
                if 0.0 <= h_awake <= 22.0:
                    link["hours_awake_before"] = round(h_awake, 1)
        pre_steps = _pre_workout_steps_from_hourly(dl_d.get("hourly_steps"), start_loc)
        if pre_steps is not None:
            link["pre_workout_steps"] = pre_steps

        # Post-Workout Finish -> Bedtime Gap
        slp_next = next_rec.get("sleep") if isinstance(next_rec.get("sleep"), dict) else {}
        if end_utc and slp_next.get("start_utc"):
            nxt_slp_start = parse_iso_utc(slp_next["start_utc"])
            if nxt_slp_start:
                gap_h = (nxt_slp_start - end_utc).total_seconds() / 3600.0
                if 0.0 <= gap_h <= 18.0:
                    link["finish_to_bedtime_h"] = round(gap_h, 2)

        # Post-Workout Intraday HR Recovery (compute live if cached, else preserve prior permanent extract)
        base_rhr = _baseline_before(d_iso, rhr_by_d) or safe_float((day_rec.get("autonomic") or {}).get("rhr_bpm"))
        post_hr_calc: dict[str, Any] = {}
        if end_utc:
            hr_pts = _get_intraday_hr(d_iso) + _get_intraday_hr(next_iso)
            if hr_pts:
                post_hr_calc = compute_post_workout_hr_recovery(end_utc, hr_pts, baseline_rhr=base_rhr)
        for pk in ("post_hr_avg_0_30m", "post_hr_avg_0_2h", "post_hr_return_min"):
            if pk in post_hr_calc:
                link[pk] = post_hr_calc[pk]
            elif prev_wl.get(pk) is not None:
                link[pk] = prev_wl[pk]

        # Next-Night Autonomic Recovery Response (T -> T+1 vs 28d baseline)
        aut_next = next_rec.get("autonomic") if isinstance(next_rec.get("autonomic"), dict) else {}
        base_hrv = _baseline_before(next_iso, hrv_by_d) or safe_float((day_rec.get("autonomic") or {}).get("hrv_rmssd_ms"))
        nxt_hrv = safe_float(aut_next.get("hrv_rmssd_ms"))
        if nxt_hrv is not None:
            link["next_night_hrv_ms"] = round(nxt_hrv, 1)
            if base_hrv and base_hrv > 5.0:
                link["next_night_delta_hrv_pct"] = round(((nxt_hrv - base_hrv) / base_hrv) * 100.0, 1)
        nxt_rhr = safe_int(aut_next.get("rhr_bpm"))
        if nxt_rhr is not None:
            link["next_night_rhr_bpm"] = nxt_rhr
            if base_rhr and base_rhr > 30.0:
                link["next_night_delta_rhr_bpm"] = round(nxt_rhr - base_rhr, 1)
        if slp_next.get("deep_min") is not None:
            link["next_night_deep_min"] = safe_int(slp_next["deep_min"])
        if slp_next.get("rem_min") is not None:
            link["next_night_rem_min"] = safe_int(slp_next["rem_min"])
        if slp_next.get("hr_nadir_pct") is not None:
            link["next_night_hr_nadir_pct"] = safe_float(slp_next["hr_nadir_pct"])
        if aut_next.get("skin_temp_delta_c") is not None:
            link["next_night_temp_delta_c"] = round(float(aut_next["skin_temp_delta_c"]), 2)

        links_by_act_id[aid] = link
        if d_iso in days_map:
            days_map[d_iso].setdefault("workout_links", []).append(link)

    return links_by_act_id


# ---------------------------------------------------------------------------
# 6. Multi-Marker Illness Early-Warning, Sleep Architecture & KFA Guard
# ---------------------------------------------------------------------------

def evaluate_biometric_signals(
    readiness_rows: list[dict[str, Any]],
    profile: dict[str, Any] | None = None,
    activities: list[dict[str, Any]] | None = None,
    today: dt.date | None = None,
) -> dict[str, Any]:
    """Evaluate 24/7 wearable signals deterministically for Stage 1 (Pillar 7 + Body KFA Guard):

    1. Multi-Marker Illness Early Warning (`>= 2` simultaneous objective anomalies -> `ROT` escalation).
    2. Sleep Architecture & 3-day Sleep Debt (`deep_pct`, `rem_pct`, `efficiency_pct`, `hr_nadir_pct`).
    3. Fast-Myotype 48–72h Post-HIIT Autonomic Rebound status.
    4. KFA Protection Guard (28d median `body_fat_pct` advisory; never silently overwrites calibrated profile KFA).
    """
    ref_today = today or dt.date.today()
    today_iso = ref_today.isoformat()
    cutoff_1d = (ref_today - dt.timedelta(days=1)).isoformat()
    cutoff_3d = (ref_today - dt.timedelta(days=2)).isoformat()  # inclusive 3 days: today, -1, -2
    cutoff_28d = (ref_today - dt.timedelta(days=28)).isoformat()

    rows = sorted(
        [r for r in (readiness_rows or []) if isinstance(r, dict) and r.get("date")],
        key=lambda x: str(x["date"]),
        reverse=True,
    )
    latest = next((r for r in rows if str(r["date"])[:10] >= cutoff_1d), None)
    latest_date = str(latest["date"])[:10] if latest else None

    # 28d historical baselines strictly prior to latest_date (or today)
    hist_cut = latest_date or today_iso
    hist_28d = [r for r in rows if cutoff_28d <= str(r["date"])[:10] < hist_cut]

    def _med(field: str, min_n: int = 3) -> float | None:
        vals = [safe_float(r.get(field)) for r in hist_28d]
        clean = [v for v in vals if v is not None]
        return statistics.median(clean) if len(clean) >= min_n else None

    base_rhr = _med("rhr") or (safe_float((profile or {}).get("rhr")))
    base_resp = _med("resp_rate_brpm")
    base_sleep_h = _med("sleep_h")
    ln_hrv_hist = [math.log(float(r["hrv_rmssd"])) for r in hist_28d if safe_float(r.get("hrv_rmssd")) and float(r["hrv_rmssd"]) > 5.0]
    ln_hrv_mean = (sum(ln_hrv_hist) / len(ln_hrv_hist)) if len(ln_hrv_hist) >= 4 else None
    ln_hrv_sd = statistics.pstdev(ln_hrv_hist) if len(ln_hrv_hist) >= 4 else None

    # 1. Multi-Marker Illness Early-Warning (>= 2 objective markers simultaneously)
    illness_markers: list[str] = []
    if latest:
        t_delta = safe_float(latest.get("skin_temp_delta_c"))
        t_sd = safe_float(latest.get("skin_temp_stddev_30d_c"))
        if t_delta is not None and (t_delta >= 0.50 or (t_sd and t_sd > 0.1 and t_delta >= max(0.40, 1.5 * t_sd))):
            illness_markers.append(f"Skin Temp {t_delta:+.2f}°C")
        resp_now = safe_float(latest.get("resp_rate_brpm"))
        if resp_now is not None and ((base_resp is not None and resp_now >= base_resp + 1.5) or (base_resp is None and resp_now >= 18.0)):
            ref_s = f" (+{resp_now - base_resp:.1f} vs 28d)" if base_resp is not None else ""
            illness_markers.append(f"Resp {resp_now:.1f}/min{ref_s}")
        rhr_now = safe_float(latest.get("rhr"))
        if rhr_now is not None and base_rhr is not None and (rhr_now - base_rhr) >= 5.0:
            illness_markers.append(f"RHR {int(round(rhr_now))} bpm (+{rhr_now - base_rhr:.0f} vs 28d)")
        hrv_now = safe_float(latest.get("hrv_rmssd"))
        if hrv_now is not None and hrv_now > 5.0 and ln_hrv_mean is not None and ln_hrv_sd is not None:
            drop_ln = ln_hrv_mean - math.log(hrv_now)
            if drop_ln >= max(0.10, 1.0 * ln_hrv_sd):
                pct_drop = (1.0 - math.exp(-drop_ln)) * 100.0
                illness_markers.append(f"HRV {hrv_now:.0f} ms (-{pct_drop:.0f}% vs 28d)")
        spo2_avg = safe_float(latest.get("spo2_avg_pct"))
        spo2_min = safe_float(latest.get("spo2_min_pct"))
        if (spo2_avg is not None and spo2_avg < 94.0) or (spo2_min is not None and spo2_min < 90.0):
            illness_markers.append(f"SpO2 {spo2_avg or spo2_min:.1f}%")

    illness_alert = len(illness_markers) >= 2

    # 2. Sleep Architecture & 3-Day Sleep Debt
    sleep_target_h = max(7.0, min(8.5, base_sleep_h)) if base_sleep_h else 7.5
    recent_3d = [r for r in rows if cutoff_3d <= str(r["date"])[:10] <= today_iso and safe_float(r.get("sleep_h")) is not None]
    sleep_debt_3d_h = round(sum(max(0.0, sleep_target_h - float(r["sleep_h"])) for r in recent_3d), 2) if recent_3d else None

    sleep_flags: list[str] = []
    deep_pct = None
    rem_pct = None
    if latest:
        slp_h = safe_float(latest.get("sleep_h"))
        asleep_m = (slp_h * 60.0) if slp_h and slp_h > 0 else None
        deep_m = safe_float(latest.get("deep_min"))
        rem_m = safe_float(latest.get("rem_min"))
        eff = safe_float(latest.get("efficiency_pct"))
        nadir_pct = safe_float(latest.get("hr_nadir_pct"))
        if asleep_m and deep_m is not None:
            deep_pct = round((deep_m / asleep_m) * 100.0, 1)
            if deep_pct < 12.0:
                sleep_flags.append(f"Low Deep sleep ({int(round(deep_m))}m / {deep_pct:.0f}%)")
        if asleep_m and rem_m is not None:
            rem_pct = round((rem_m / asleep_m) * 100.0, 1)
            if rem_pct < 15.0:
                sleep_flags.append(f"Low REM sleep ({int(round(rem_m))}m / {rem_pct:.0f}%)")
        if eff is not None and eff < 80.0:
            sleep_flags.append(f"Low sleep efficiency ({eff:.0f}%)")
        if nadir_pct is not None and nadir_pct > 68.0:
            sleep_flags.append(f"Late HR nadir ({latest.get('hr_nadir_local') or '?'}, {nadir_pct:.0f}% into night — sympathetic stress)")
    if sleep_debt_3d_h is not None and sleep_debt_3d_h >= 3.0:
        sleep_flags.append(f"3d sleep debt {sleep_debt_3d_h:.1f}h (target {sleep_target_h:.1f}h/night)")

    # 3. Post-HIIT / Heavy Load Autonomic Rebound Tracker (last 5 days)
    rebound_info: dict[str, Any] | None = None
    cutoff_5d = (ref_today - dt.timedelta(days=5)).isoformat()
    hard_act = None
    for a in activities or []:
        if not isinstance(a, dict) or not a.get("date"):
            continue
        ad = str(a["date"])[:10]
        if ad < cutoff_5d or ad > today_iso:
            continue
        is_hard = bool(
            float(a.get("kj_above_cp") or 0.0) >= 15.0
            or float(a.get("tss") or a.get("systemic_tss") or 0.0) >= 85.0
            or float(a.get("if") or 0.0) >= 0.88
        )
        if is_hard:
            hard_act = a
            break
    if hard_act and latest:
        try:
            days_since = max(0, (ref_today - dt.date.fromisoformat(str(hard_act["date"])[:10])).days)
        except ValueError:
            days_since = 0
        hrv_now = safe_float(latest.get("hrv_rmssd"))
        rhr_now = safe_float(latest.get("rhr"))
        hrv_ok = True
        if hrv_now and hrv_now > 5.0 and ln_hrv_mean is not None and ln_hrv_sd is not None:
            hrv_ok = math.log(hrv_now) >= (ln_hrv_mean - max(0.04, 0.5 * ln_hrv_sd))
        rhr_ok = True if (rhr_now is None or base_rhr is None) else ((rhr_now - base_rhr) <= 3.0)
        rebound_info = {
            "session_date": str(hard_act["date"])[:10],
            "session_name": str(hard_act.get("name") or "Hard Session"),
            "days_since": days_since,
            "autonomic_rebounded": bool(hrv_ok and rhr_ok),
            "status": "rebounded" if (hrv_ok and rhr_ok) else "recovering",
        }

    # 4. KFA Protection Guard (28d median body_fat_pct; never silently overwrites calibrated profile KFA)
    bf_28d = [
        float(r["body_fat_pct"])
        for r in rows
        if cutoff_28d <= str(r["date"])[:10] <= today_iso
        and safe_float(r.get("body_fat_pct")) is not None
        and 3.0 <= float(r["body_fat_pct"]) <= 50.0
    ]
    biometrics_kfa_28d = round(statistics.median(bf_28d), 1) if bf_28d else None
    kfa_drift_advisory = None
    prof_kfa = safe_float((profile or {}).get("kfa_pct"))
    if biometrics_kfa_28d is not None and len(bf_28d) >= 3 and prof_kfa is not None:
        if abs(biometrics_kfa_28d - prof_kfa) >= 1.5:
            kfa_drift_advisory = (
                f"Wearable/Scale 28d median KFA {biometrics_kfa_28d:.1f}% differs from profile.md ({prof_kfa:.1f}%) — "
                f"confirm before updating (`atlas.py coach set-profile --kfa {biometrics_kfa_28d:.1f} --execute`)"
            )

    return {
        "has_wearable_data": bool(
            latest and any(
                latest.get(k) is not None
                for k in ("hrv_rmssd", "deep_min", "resp_rate_brpm", "skin_temp_delta_c", "spo2_avg_pct", "steps_total")
            )
        ),
        "latest_date": latest_date,
        "illness_alert": illness_alert,
        "illness_markers": illness_markers,
        "sleep_debt_3d_h": sleep_debt_3d_h,
        "sleep_target_h": round(sleep_target_h, 2),
        "deep_pct": deep_pct,
        "rem_pct": rem_pct,
        "sleep_flags": sleep_flags,
        "post_hiit_rebound": rebound_info,
        "biometrics_kfa_28d": biometrics_kfa_28d,
        "kfa_drift_advisory": kfa_drift_advisory,
        "latest_summary": {
            "rhr": latest.get("rhr") if latest else None,
            "hrv_rmssd": latest.get("hrv_rmssd") if latest else None,
            "hrv_deep_rmssd": latest.get("hrv_deep_rmssd") if latest else None,
            "sleep_h": latest.get("sleep_h") if latest else None,
            "deep_min": latest.get("deep_min") if latest else None,
            "rem_min": latest.get("rem_min") if latest else None,
            "efficiency_pct": latest.get("efficiency_pct") if latest else None,
            "hr_nadir_local": latest.get("hr_nadir_local") if latest else None,
            "hr_nadir_pct": latest.get("hr_nadir_pct") if latest else None,
            "resp_rate_brpm": latest.get("resp_rate_brpm") if latest else None,
            "skin_temp_delta_c": latest.get("skin_temp_delta_c") if latest else None,
            "spo2_avg_pct": latest.get("spo2_avg_pct") if latest else None,
            "steps_total": latest.get("steps_total") if latest else None,
            "azm_total_min": latest.get("azm_total_min") if latest else None,
            "weight_kg": latest.get("weight_kg") if latest else None,
        },
    }


def compute_temporal_dose_response(
    activities: list[dict[str, Any]],
    readiness_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """Derive empirical temporal dose-response patterns from aligned workout_links + biometrics."""
    links: list[dict[str, Any]] = []
    act_by_id = {str(a.get("id") or ""): a for a in (activities or []) if isinstance(a, dict)}
    for r in readiness_rows or []:
        for wl in (r.get("workout_links") or []):
            if isinstance(wl, dict):
                links.append(wl)

    # 1. Late evening workout (<3.0h before sleep) vs earlier (>=4.0h) -> next-night Deep sleep & HRV delta
    late_hrv: list[float] = []
    early_hrv: list[float] = []
    late_deep: list[float] = []
    early_deep: list[float] = []
    for wl in links:
        gap = safe_float(wl.get("finish_to_bedtime_h"))
        if gap is None:
            continue
        d_hrv = safe_float(wl.get("next_night_delta_hrv_pct"))
        deep_m = safe_float(wl.get("next_night_deep_min"))
        if gap < 3.0:
            if d_hrv is not None:
                late_hrv.append(d_hrv)
            if deep_m is not None:
                late_deep.append(deep_m)
        elif gap >= 4.0:
            if d_hrv is not None:
                early_hrv.append(d_hrv)
            if deep_m is not None:
                early_deep.append(deep_m)

    late_workout_sig = None
    if len(late_hrv) >= 2 and len(early_hrv) >= 2:
        diff_hrv = (sum(late_hrv) / len(late_hrv)) - (sum(early_hrv) / len(early_hrv))
        deep_note = ""
        if len(late_deep) >= 2 and len(early_deep) >= 2:
            diff_deep = (sum(late_deep) / len(late_deep)) - (sum(early_deep) / len(early_deep))
            deep_note = f", Deep sleep {diff_deep:+.0f} min"
        if diff_hrv <= -4.0 or (deep_note and diff_deep <= -10.0):
            late_workout_sig = (
                f"Late finish (<3h before bed) shifts next-night HRV {diff_hrv:+.1f}%{deep_note} vs >=4h buffer"
            )

    # 2. Supra-CP (w_sup > CP) -> Next-night HRV response
    sup_hrv: list[float] = []
    for wl in links:
        act = act_by_id.get(str(wl.get("activity_id") or ""))
        if not act:
            continue
        if float(act.get("kj_above_cp") or 0.0) >= 15.0:
            d_hrv = safe_float(wl.get("next_night_delta_hrv_pct"))
            if d_hrv is not None:
                sup_hrv.append(d_hrv)
    supra_cp_hrv_sig = None
    if len(sup_hrv) >= 2:
        avg_sup = sum(sup_hrv) / len(sup_hrv)
        supra_cp_hrv_sig = f"After >=15 kJ >CP sessions, next-night HRV shifts {avg_sup:+.1f}% vs 28d baseline (n={len(sup_hrv)})"

    # 3. High Pre-Workout NEAT (>10,000 steps before ride) -> decoupling or RPE elevation
    high_neat_dec: list[float] = []
    low_neat_dec: list[float] = []
    for wl in links:
        steps = safe_int(wl.get("pre_workout_steps"))
        act = act_by_id.get(str(wl.get("activity_id") or ""))
        if steps is None or not act:
            continue
        dec = safe_float(act.get("steady_decoupling_pct") if act.get("steady_decoupling_pct") is not None else act.get("decoupling_pct"))
        if dec is None:
            continue
        if steps >= 10000:
            high_neat_dec.append(dec)
        elif steps <= 6000:
            low_neat_dec.append(dec)
    neat_sig = None
    if len(high_neat_dec) >= 2 and len(low_neat_dec) >= 2:
        d_dec = (sum(high_neat_dec) / len(high_neat_dec)) - (sum(low_neat_dec) / len(low_neat_dec))
        if d_dec >= 1.5:
            neat_sig = f"High pre-ride NEAT (>=10k steps) elevates Pw:Hr decoupling by +{d_dec:.1f}% vs <=6k steps"

    return {
        "aligned_workouts": len(links),
        "late_workout_sleep_impact": late_workout_sig,
        "supra_cp_hrv_response": supra_cp_hrv_sig,
        "pre_workout_neat_impact": neat_sig,
    }


# ---------------------------------------------------------------------------
# 7. Chat-First Day Timeline Formatter (`atlas.py coach inspect --date`)
# ---------------------------------------------------------------------------

def format_day_timeline_inspect(
    target_date: str,
    activities: list[dict[str, Any]],
    store: dict[str, Any],
) -> str:
    """Format a complete temporal story for `target_date`: Night (D) -> Pre-NEAT -> Strava Workout(s) -> Post-HR & Night (D+1)."""
    d_iso = str(target_date)[:10]
    days_map = store.get("days") if isinstance(store, dict) and isinstance(store.get("days"), dict) else {}
    day_rec = days_map.get(d_iso) or {}
    try:
        next_iso = (dt.date.fromisoformat(d_iso) + dt.timedelta(days=1)).isoformat()
    except ValueError:
        next_iso = ""
    next_rec = days_map.get(next_iso) or {}

    lines = [f"=== TEMPORAL BIOMETRICS & WORKOUT TIMELINE ({d_iso}) ==="]

    # 1. Morning / Prior Night (D)
    slp = day_rec.get("sleep") if isinstance(day_rec.get("sleep"), dict) else {}
    aut = day_rec.get("autonomic") if isinstance(day_rec.get("autonomic"), dict) else {}
    bod = day_rec.get("body") if isinstance(day_rec.get("body"), dict) else {}
    dl = day_rec.get("daytime_load") if isinstance(day_rec.get("daytime_load"), dict) else {}
    sub = day_rec.get("subjective") if isinstance(day_rec.get("subjective"), dict) else {}
    pmc = day_rec.get("pmc_snapshot") if isinstance(day_rec.get("pmc_snapshot"), dict) else {}

    if slp or aut or bod or dl or sub:
        slp_bits = []
        if slp.get("duration_h") is not None:
            win_s = ""
            if slp.get("start_local") and slp.get("end_local"):
                win_s = f" ({str(slp['start_local'])[11:16]}–{str(slp['end_local'])[11:16]})"
            slp_bits.append(f"Sleep {slp['duration_h']:.2f}h{win_s}")
        if slp.get("deep_min") is not None or slp.get("rem_min") is not None:
            slp_bits.append(
                f"Deep {slp.get('deep_min', '—')}m / REM {slp.get('rem_min', '—')}m / Light {slp.get('light_min', '—')}m / Awake {slp.get('awake_min', '—')}m"
            )
        if slp.get("efficiency_pct") is not None:
            slp_bits.append(f"Eff {slp['efficiency_pct']:.0f}%")
        if slp.get("hr_nadir_local"):
            slp_bits.append(f"HR-Nadir {slp['hr_nadir_local']} ({slp.get('hr_nadir_pct', '?')}%, min {slp.get('hr_sleep_min', '?')} bpm)")
        if slp_bits:
            lines.append("  [1. Night & Sleep] " + " | ".join(slp_bits))

        aut_bits = []
        if aut.get("rhr_bpm") is not None:
            aut_bits.append(f"RHR {aut['rhr_bpm']} bpm")
        if aut.get("hrv_rmssd_ms") is not None:
            deep_h = f" (deep {aut['hrv_deep_rmssd_ms']}ms)" if aut.get("hrv_deep_rmssd_ms") is not None else ""
            aut_bits.append(f"HRV {aut['hrv_rmssd_ms']} ms{deep_h}")
        if aut.get("resp_rate_brpm") is not None:
            aut_bits.append(f"Resp {aut['resp_rate_brpm']}/min")
        if aut.get("skin_temp_delta_c") is not None:
            aut_bits.append(f"SkinTemp {float(aut['skin_temp_delta_c']):+.2f}°C")
        if aut.get("spo2_avg_pct") is not None:
            aut_bits.append(f"SpO2 {aut['spo2_avg_pct']}% (min {aut.get('spo2_min_pct', '—')}%)")
        if bod.get("weight_kg") is not None:
            aut_bits.append(f"Weight {bod['weight_kg']} kg")
        if aut_bits:
            lines.append("  [2. Morning Autonomics] " + " | ".join(aut_bits))

        neat_bits = []
        if dl.get("steps_total") is not None:
            neat_bits.append(f"Steps {dl['steps_total']}")
        if dl.get("azm_total_min") is not None:
            neat_bits.append(
                f"AZM {dl['azm_total_min']}m (FatBurn {dl.get('azm_fat_burn_min', 0)} / Cardio {dl.get('azm_cardio_min', 0)} / Peak {dl.get('azm_peak_min', 0)})"
            )
        if pmc.get("ctl") is not None:
            neat_bits.append(f"PMC CTL {pmc['ctl']:.1f} / ATL {pmc['atl']:.1f} / TSB {pmc['tsb']:+.1f}")
        if sub.get("notes") or sub.get("soreness") is not None or sub.get("leg_pain") is not None:
            sub_s = " ".join(
                x for x in (
                    f"soreness={sub['soreness']}/5" if sub.get("soreness") is not None else "",
                    f"legs={sub['leg_pain']}/5" if sub.get("leg_pain") is not None else "",
                    f"notes='{sub['notes']}'" if sub.get("notes") else "",
                ) if x
            )
            neat_bits.append(sub_s)
        if neat_bits:
            lines.append("  [3. Daytime NEAT & PMC] " + " | ".join(neat_bits))
    else:
        lines.append("  [1-3. Biometrics] (no biometrics recorded for this date)")

    # 2. Workouts & Temporal Links
    links_by_id = {
        str(wl.get("activity_id") or ""): wl
        for wl in (day_rec.get("workout_links") or [])
        if isinstance(wl, dict)
    }
    if activities:
        for a in activities:
            aid = str(a.get("id") or "")
            wl = links_by_id.get(aid) or {}
            t_win = ""
            if a.get("start_time_local"):
                t_start = str(a["start_time_local"])[11:16]
                t_end = str(wl.get("end_local") or "")[11:16]
                t_win = f" @ {t_start}" + (f"–{t_end}" if t_end else "")
            pwr_s = f"NP {a['np_w']}W, IF {a.get('if', '—')}, {a.get('work_kj', 0)}kJ (>CP {a.get('kj_above_cp', 0)}kJ)" if a.get("np_w") else f"{a.get('distance_km', 0)}km"
            hr_s = f"HR {a.get('avg_hr', '—')}/{a.get('max_hr', '—')} bpm" if a.get("avg_hr") else ""
            lines.append(
                f"  [4. Workout{t_win}] [{a.get('sport', 'ride')}] '{a.get('name', 'Session')}' ({a.get('duration_min', 0)}m | {pwr_s}"
                + (f" | {hr_s}" if hr_s else "") + ")"
            )
            t_bits = []
            if wl.get("hours_awake_before") is not None:
                t_bits.append(f"awake {wl['hours_awake_before']}h before")
            if wl.get("pre_workout_steps") is not None:
                t_bits.append(f"pre-NEAT {wl['pre_workout_steps']} steps")
            if wl.get("finish_to_bedtime_h") is not None:
                t_bits.append(f"finish->bed {wl['finish_to_bedtime_h']:.1f}h")
            if wl.get("post_hr_avg_0_30m") is not None:
                t_bits.append(f"post-HR 0-30m {wl['post_hr_avg_0_30m']} / 0-2h {wl.get('post_hr_avg_0_2h', '—')} bpm")
            if wl.get("post_hr_return_min") is not None:
                t_bits.append(f"HR return {wl['post_hr_return_min']}m")
            if t_bits:
                lines.append("      Temporal Context: " + " | ".join(t_bits))

    # 3. Following Night (D+1 Recovery Response)
    if next_rec:
        n_slp = next_rec.get("sleep") if isinstance(next_rec.get("sleep"), dict) else {}
        n_aut = next_rec.get("autonomic") if isinstance(next_rec.get("autonomic"), dict) else {}
        nxt_bits = []
        if n_slp.get("duration_h") is not None:
            nxt_bits.append(f"Sleep {n_slp['duration_h']:.2f}h (Deep {n_slp.get('deep_min', '—')}m / REM {n_slp.get('rem_min', '—')}m)")
        if n_aut.get("rhr_bpm") is not None:
            nxt_bits.append(f"RHR {n_aut['rhr_bpm']} bpm")
        if n_aut.get("hrv_rmssd_ms") is not None:
            nxt_bits.append(f"HRV {n_aut['hrv_rmssd_ms']} ms")
        if n_aut.get("skin_temp_delta_c") is not None:
            nxt_bits.append(f"SkinTemp {float(n_aut['skin_temp_delta_c']):+.2f}°C")
        if n_slp.get("hr_nadir_local"):
            nxt_bits.append(f"HR-Nadir {n_slp['hr_nadir_local']} ({n_slp.get('hr_nadir_pct', '?')}%)")
        if nxt_bits:
            lines.append(f"  [5. Following Night Recovery ({next_iso})] " + " | ".join(nxt_bits))

    return "\n".join(lines)


def summarize_biometrics_epoch(
    store: dict[str, Any],
    date_lo: str,
    date_hi: str,
) -> dict[str, Any]:
    """Summarize biometrics across a date range for `coach history` and `coach compare`."""
    days_map = store.get("days") if isinstance(store, dict) and isinstance(store.get("days"), dict) else {}
    rhrs: list[float] = []
    hrvs: list[float] = []
    slps: list[float] = []
    deeps: list[float] = []
    rems: list[float] = []
    steps: list[float] = []
    wts: list[float] = []
    cnt = 0
    for d_key, day in days_map.items():
        if not (date_lo <= d_key <= date_hi) or not isinstance(day, dict):
            continue
        cnt += 1
        aut = day.get("autonomic") or {}
        slp = day.get("sleep") or {}
        dl = day.get("daytime_load") or {}
        bod = day.get("body") or {}
        if safe_float(aut.get("rhr_bpm")) is not None:
            rhrs.append(float(aut["rhr_bpm"]))
        if safe_float(aut.get("hrv_rmssd_ms")) is not None:
            hrvs.append(float(aut["hrv_rmssd_ms"]))
        if safe_float(slp.get("duration_h")) is not None:
            slps.append(float(slp["duration_h"]))
        if safe_float(slp.get("deep_min")) is not None:
            deeps.append(float(slp["deep_min"]))
        if safe_float(slp.get("rem_min")) is not None:
            rems.append(float(slp["rem_min"]))
        if safe_float(dl.get("steps_total")) is not None:
            steps.append(float(dl["steps_total"]))
        if safe_float(bod.get("weight_kg")) is not None:
            wts.append(float(bod["weight_kg"]))
    return {
        "days": cnt,
        "avg_rhr": round(sum(rhrs) / len(rhrs), 1) if rhrs else None,
        "avg_hrv": round(sum(hrvs) / len(hrvs), 1) if hrvs else None,
        "avg_sleep_h": round(sum(slps) / len(slps), 2) if slps else None,
        "avg_deep_min": int(round(sum(deeps) / len(deeps))) if deeps else None,
        "avg_rem_min": int(round(sum(rems) / len(rems))) if rems else None,
        "avg_steps": int(round(sum(steps) / len(steps))) if steps else None,
        "avg_weight_kg": round(sum(wts) / len(wts), 2) if wts else None,
    }
