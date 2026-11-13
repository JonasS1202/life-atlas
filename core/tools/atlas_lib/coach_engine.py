"""Atlas Adaptive Simple-to-Pro Sports-Science, Physics & Load Engine.

Python 3 standard library only (`math`, `statistics`, `datetime`, `re`, `json`, `os`).
Zero LLM tokens: computes deterministic physiology, biochemistry, biomechanics, and
cross-sport systemic load from 1Hz activity streams, athlete profile, readiness logs,
and calendar constraints.
"""
from __future__ import annotations

import datetime as dt
import math
import os
import re
import statistics
from typing import Any

COACH_DURATIONS: tuple[tuple[str, int], ...] = (
    ("5s", 5),
    ("15s", 15),
    ("30s", 30),
    ("1m", 60),
    ("2m", 120),
    ("3m", 180),
    ("5m", 300),
    ("8m", 480),
    ("10m", 600),
    ("12m", 720),
    ("20m", 1200),
    ("30m", 1800),
    ("60m", 3600),
)

DURATION_SECONDS: dict[str, int] = dict(COACH_DURATIONS)

RUN_SPLIT_DISTANCES: tuple[tuple[str, float], ...] = (
    ("1km", 1000.0),
    ("3km", 3000.0),
    ("5km", 5000.0),
    ("10km", 10000.0),
)

DURABILITY_TIERS: tuple[tuple[str, str], ...] = (
    ("15kj_kg", ">15 kJ/kg"),
    ("25kj_kg", ">25 kJ/kg"),
    ("35kj_kg", ">35 kJ/kg"),
    ("5kj_above_cp_kg", ">5 kJ/kg >CP"),
)

PRO_QUERY_RX = re.compile(
    r"(?i)\b(?:cp\b|w['’]|wbal|w['’]?bal|skiba|vlamax|glykoly\w*|vo2\s*max|vdot|fatmax|"
    r"ctl\b|atl\b|tsb\b|acwr\b|monoton\w*|trimp\b|rtss\b|hrtss\b|srpe\b|"
    r"typolog\w*|muskelfaser\w*|fiber\s+type|pdc\b|torque|drehmoment|quadrant\w*|"
    r"durability|erm[uü]dungswiderstand|pw\s*:\s*hr|decoupling|entkopplung|"
    r"ampk|mtor\w*|bosquet|peronnet|hypoxi\w*)\b"
)


def format_pace(sec_per_km: float) -> str:
    """Format seconds per km as `M:SS/km`."""
    if not sec_per_km or sec_per_km <= 0 or math.isnan(sec_per_km) or math.isinf(sec_per_km):
        return "—"
    mm = int(sec_per_km // 60)
    ss = int(round(sec_per_km % 60))
    if ss == 60:
        mm += 1
        ss = 0
    return f"{mm}:{ss:02d}/km"


def parse_pace_to_spk(text: str) -> float | None:
    """Parse `4:15/km` or `4:15 min/km` into seconds per kilometer."""
    if not text:
        return None
    m = re.search(r"\b(\d{1,2}):(\d{2})\s*(?:/km|min/km)?\b", str(text), re.I)
    if not m:
        return None
    spk = int(m.group(1)) * 60 + int(m.group(2))
    return float(spk) if 120 <= spk <= 900 else None


# ---------------------------------------------------------------------------
# 1. Profile Parsing & Adaptive Depth Tiering (`auto | simple | standard | pro`)
# ---------------------------------------------------------------------------

def is_untouched_skeleton_profile(text: str) -> bool:
    """Return True only if profile.md still matches the unedited default template."""
    if not text or not text.strip():
        return True
    # Skeleton from skeleton/coach/profile.md
    if "186 cm" in text and "75.0 kg" in text and "300 W" in text and "Hammerhead Karoo 3" in text:
        return True
    # Unharvested COACH_DEFAULT_PROFILE fallback
    if "| **Biometrie** | — cm · — kg |" in text and "| **FTP (Cycling)** | 275 W |" in text:
        return True
    return False


def parse_athlete_profile(profile_text: str, fallback_ftp: int | None = None, fallback_weight: float | None = None) -> dict[str, Any]:
    """Parse `brain/coach/profile.md` frontmatter and biometrics/thresholds table."""
    info: dict[str, Any] = {
        "depth": "auto",
        "is_skeleton": is_untouched_skeleton_profile(profile_text),
        "has_casual_intent": bool(
            profile_text
            and re.search(
                r"(?i)(?:nothing\s+fancy|casual|just\s+(?:let\s+me\s+)?(?:go\s+)?run|ohne\s+(?:leistungsdruck|schnickschnack)|einfach\s+\d+\s*x)",
                profile_text,
            )
        ),
        "weight_kg": fallback_weight,
        "height_cm": None,
        "age": None,
        "sex": "neutral",
        "ftp_w": fallback_ftp,
        "has_explicit_ftp": False,
        "cp_w": None,
        "w_prime_kj": None,
        "cs_mps": None,
        "d_prime_m": None,
        "lt2_pace_spk": None,
        "hr_max": None,
        "rhr": None,
        "lthr": None,
        "vo2max": None,
        "fueling_g_h": None,
        "primary_sport": "",
        "weekly_budget": "",
        "schedule": "",
        "bio_rules": [],
    }
    if not profile_text:
        if info["weight_kg"] is None:
            info["weight_kg"] = 67.0
        return info

    # Parse YAML frontmatter
    if profile_text.startswith("---"):
        end = profile_text.find("\n---", 3)
        if end > 0:
            fm_block = profile_text[3:end]
            m_dep = re.search(r"(?im)^depth\s*:\s*([a-z]+)\b", fm_block)
            if m_dep and m_dep.group(1).lower() in ("auto", "simple", "standard", "pro"):
                info["depth"] = m_dep.group(1).lower()
            m_sex = re.search(r"(?im)^(?:sex|gender|geschlecht)\s*:\s*([a-zäöü]+)\b", fm_block)
            if m_sex:
                sx = m_sex.group(1).lower()
                if sx in ("m", "male", "mann", "männlich"):
                    info["sex"] = "male"
                elif sx in ("f", "w", "female", "frau", "weiblich"):
                    info["sex"] = "female"
            m_age = re.search(r"(?im)^(?:age|alter)\s*:\s*(\d{2})\b", fm_block)
            if m_age:
                info["age"] = int(m_age.group(1))

    in_rules = False
    for raw_line in profile_text.splitlines():
        line = raw_line.strip()
        low = line.lower()

        if line.startswith("## "):
            in_rules = any(k in low for k in ("bio-rules", "bio-feedback", "learned", "regeln", "personal"))
            continue
        if in_rules and line.startswith(("- ", "* ")) and "<!--" not in line:
            info["bio_rules"].append(line[2:].strip())

        if "weekday window" in low or "mo–fr" in low or "mo-fr" in low or "werktags" in low:
            m_sch = re.search(r"(\d{1,2}:\d{2}\s*[–-]\s*\d{1,2}:\d{2})", line)
            if m_sch and not info["schedule"]:
                info["schedule"] = m_sch.group(1)

        if "|" not in line:
            # Also detect casual free-text sport cues when user wrote prose instead of a table
            if not info["primary_sport"]:
                if any(w in low for w in ("laufen", "running", "joggen")) and not any(w in low for w in ("cycling", "rennrad", "rad")):
                    info["primary_sport"] = "running"
            continue

        cells = [c.strip().strip("`*") for c in line.strip("|").split("|")]
        if len(cells) < 2:
            continue
        k_low = cells[0].lower()
        v_raw = cells[1]
        v_low = v_raw.lower()

        if any(w in k_low for w in ("biometrie", "height", "weight", "körpermaße", "gewicht")):
            m_cm = re.search(r"(\d{3})\s*cm\b", v_low)
            if m_cm:
                info["height_cm"] = int(m_cm.group(1))
            m_kg = re.search(r"(\d{2,3}(?:[.,]\d+)?)\s*kg\b", v_low)
            if m_kg:
                info["weight_kg"] = float(m_kg.group(1).replace(",", "."))
            m_age = re.search(r"(\d{2})\s*(?:y|yo|jahre|j\.)\b", v_low)
            if m_age and not info["age"]:
                info["age"] = int(m_age.group(1))
            if any(w in v_low for w in ("male", "männlich")) and "female" not in v_low:
                info["sex"] = "male"
            elif any(w in v_low for w in ("female", "weiblich")):
                info["sex"] = "female"

        elif "primary" in k_low or "hauptsport" in k_low or "sportart" in k_low:
            info["primary_sport"] = re.sub(r"[`*]", "", v_raw).strip()

        elif "weekly time" in k_low or "volumen" in k_low or "umfang" in k_low or "zeitbudget" in k_low:
            info["weekly_budget"] = re.sub(r"[`*]", "", v_raw).strip()

        elif "ftp" in k_low and "cp" not in k_low:
            m_w = re.search(r"(\d{2,3})\s*w\b", v_low)
            if m_w:
                info["ftp_w"] = int(m_w.group(1))
                if not info["is_skeleton"]:
                    info["has_explicit_ftp"] = True

        elif "cp" in k_low and ("w'" in k_low or "critical power" in k_low or "critical" in v_low):
            m_cp = re.search(r"(?:cp\s*[:=]?\s*)?(\d{2,3})\s*w\b", v_low)
            if m_cp:
                info["cp_w"] = int(m_cp.group(1))
            m_wp = re.search(r"(\d{1,2}(?:[.,]\d+)?)\s*kj\b", v_low)
            if m_wp:
                info["w_prime_kj"] = float(m_wp.group(1).replace(",", "."))

        elif "cs" in k_low or "running threshold" in k_low or "laufschwelle" in k_low:
            spk = parse_pace_to_spk(v_raw)
            if spk:
                info["lt2_pace_spk"] = spk
                info["cs_mps"] = round(1000.0 / spk, 3)
            m_dp = re.search(r"(\d{2,3}(?:[.,]\d+)?)\s*m\b", v_low)
            if m_dp and ("d'" in k_low or "d'" in v_low or "d:" in v_low):
                info["d_prime_m"] = float(m_dp.group(1).replace(",", "."))
            m_lthr = re.search(r"lthr\s*[:=]?\s*(\d{2,3})\s*bpm", v_low)
            if m_lthr:
                info["lthr"] = int(m_lthr.group(1))

        elif "max hr" in k_low or "herzfrequenz" in k_low or "ruhepuls" in k_low or "resting hr" in k_low:
            m_max = re.search(r"(?:hrmax|max\s*hr|max)\s*[:=]?\s*~?(\d{3})\s*bpm", v_low)
            if not m_max:
                m_max = re.search(r"\b(1[5-9]\d|2[0-2]\d)\s*bpm\b", v_low)
            if m_max:
                info["hr_max"] = int(m_max.group(1))
            m_rhr = re.search(r"(?:rhr|rest(?:ing)?\s*hr|ruhepuls)\s*[:=]?\s*~?(\d{2})\s*bpm", v_low)
            if m_rhr:
                info["rhr"] = int(m_rhr.group(1))
            m_lthr = re.search(r"lthr\s*[:=]?\s*~?(\d{2,3})\s*bpm", v_low)
            if m_lthr:
                info["lthr"] = int(m_lthr.group(1))

        elif "lthr" in k_low:
            m_lthr = re.search(r"(\d{2,3})\s*bpm", v_low)
            if m_lthr:
                info["lthr"] = int(m_lthr.group(1))

        elif "vo2max" in k_low or "vo₂max" in k_low:
            m_vo2 = re.search(r"(\d{2}(?:[.,]\d+)?)\s*ml", v_low)
            if m_vo2 and not info["is_skeleton"]:
                info["vo2max"] = float(m_vo2.group(1).replace(",", "."))

        elif "fueling" in k_low or "kohlenhydrat" in k_low or "verpflegung" in k_low:
            m_f = re.search(r"(\d{2,3})\s*g\s*/\s*h", v_low)
            if m_f:
                info["fueling_g_h"] = int(m_f.group(1))

    if info["weight_kg"] is None:
        info["weight_kg"] = 67.0
    return info


def resolve_coach_depth(
    explicit_depth: str | None = None,
    profile_info: dict[str, Any] | None = None,
    activities: list[dict[str, Any]] | None = None,
    readiness_rows: list[dict[str, Any]] | None = None,
    strava_connected: bool = False,
    query_text: str = "",
) -> str:
    """Resolve active coaching tier: `simple | standard | pro`."""
    if explicit_depth and explicit_depth.lower() in ("simple", "standard", "pro"):
        return explicit_depth.lower()
    prof = profile_info or {}
    p_depth = str(prof.get("depth") or "auto").lower()
    if p_depth in ("simple", "standard", "pro"):
        return p_depth

    # `auto` heuristic:
    if query_text and PRO_QUERY_RX.search(query_text):
        return "pro"

    acts = activities or []
    has_power_streams = any(
        isinstance(a, dict) and (a.get("peaks_fresh") or a.get("np_w") or a.get("w_bal_min_pct") is not None)
        for a in acts
    )
    has_pace_streams = sum(1 for a in acts if isinstance(a, dict) and a.get("best_paces")) >= 1
    has_readiness = bool(readiness_rows)

    if has_power_streams or prof.get("cp_w") or prof.get("w_prime_kj") or has_readiness:
        return "pro"
    if prof.get("has_explicit_ftp") and (acts or strava_connected):
        return "pro"
    if prof.get("has_casual_intent") and not prof.get("has_explicit_ftp") and not prof.get("lt2_pace_spk"):
        return "simple"
    if has_pace_streams and (prof.get("lt2_pace_spk") or prof.get("hr_max") or len(acts) >= 4):
        return "pro"

    # Check if user has structured thresholds/Strava basics or untouched coach module onboarding -> `standard`
    if prof.get("has_explicit_ftp") or prof.get("lt2_pace_spk") or prof.get("lthr") or strava_connected:
        return "standard"
    if prof.get("is_skeleton") and not prof.get("has_casual_intent"):
        return "standard"

    # Default for casual fitness / no thresholds / "just run 2x/week" -> `simple`
    return "simple"


# ---------------------------------------------------------------------------
# 2. Power-Duration (`CP/W'`), Running (`CS/D'`), Skiba `W'bal`, Typology & Torque
# ---------------------------------------------------------------------------

def mmp_from_watts(watts: list[float], durations: tuple[tuple[str, int], ...] = COACH_DURATIONS) -> dict[str, int]:
    """Compute Mean Maximal Power dict in O(N) using prefix sums."""
    n = len(watts)
    if not n:
        return {}
    pref = [0.0] * (n + 1)
    for i, w in enumerate(watts):
        pref[i + 1] = pref[i] + max(0.0, float(w or 0.0))
    peaks: dict[str, int] = {}
    for label, sec in durations:
        if n < sec:
            continue
        best = 0.0
        for i in range(n - sec + 1):
            avg = (pref[i + sec] - pref[i]) / sec
            if avg > best:
                best = avg
        if best > 0:
            peaks[label] = int(round(best))
    return peaks


def _ols_linear(xs: list[float], ys: list[float]) -> tuple[float, float, float]:
    """Return `(slope, intercept, r2)` for ordinary least squares `y = slope * x + intercept`."""
    n = len(xs)
    if n < 2:
        return 0.0, 0.0, 0.0
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 1e-12:
        return 0.0, my, 0.0
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    slope = sxy / sxx
    intercept = my - slope * mx
    sst = sum((y - my) ** 2 for y in ys)
    if sst <= 1e-12:
        return slope, intercept, 0.0
    sse = sum((y - (slope * x + intercept)) ** 2 for x, y in zip(xs, ys))
    r2 = max(0.0, min(1.0, 1.0 - (sse / sst)))
    return slope, intercept, r2


def solve_cp_w_prime(mmp_dict: dict[str, int | float]) -> dict[str, Any]:
    """2-parameter Monod & Scherrer linear Work-Time `CP/W'` solver (`W(t) = CP * t + W'`) over 3m–20m MMPs.

    Quality gating:
      - >= 1 short anchor in [180s, 300s) (i.e. < 5m, such as 3m)
      - >= 1 long anchor in [720s, 1200s] (i.e. >= 12m, such as 12m or 20m)
      - R^2 >= 0.98
      - Physiological monotonicity: P_short > P_long * 1.05 and W' >= 2.0 kJ
    """
    points: list[tuple[int, float, str]] = []
    for lbl, val in (mmp_dict or {}).items():
        if not val or val <= 0:
            continue
        sec = DURATION_SECONDS.get(str(lbl))
        if sec is None and str(lbl).isdigit():
            sec = int(lbl)
        if sec is not None and 180 <= sec <= 1200:
            points.append((sec, float(val), str(lbl)))
    points.sort(key=lambda x: x[0])

    has_short = any(180 <= s < 300 for s, _, _ in points)
    has_long = any(720 <= s <= 1200 for s, _, _ in points)
    if len(points) < 2:
        return {
            "valid": False,
            "cp_w": None,
            "w_prime_j": None,
            "w_prime_kj": None,
            "r2": 0.0,
            "anchors": [p[2] for p in points],
            "quality_flag": "insufficient anchors in 3m–20m window (need >=1 <5m and >=1 >=12m)",
        }

    ts = [float(p[0]) for p in points]
    works = [float(p[0]) * p[1] for p in points]
    cp, w_prime, r2 = _ols_linear(ts, works)

    p_short = points[0][1]
    p_long = points[-1][1]
    reasons: list[str] = []
    if not has_short:
        reasons.append("missing <5m maximal anchor (e.g. 3m)")
    if not has_long:
        reasons.append("missing >=12m maximal anchor (e.g. 12m/20m)")
    if r2 < 0.98:
        reasons.append(f"R^2={r2:.3f} < 0.98 (uneven effort across durations)")
    if cp <= 50 or w_prime < 2000.0 or p_short <= p_long * 1.05:
        reasons.append("non-maximal flat curve (need true all-out short & long efforts)")

    valid = len(reasons) == 0
    return {
        "valid": valid,
        "cp_w": int(round(cp)) if cp > 0 else None,
        "w_prime_j": int(round(w_prime)) if w_prime > 0 else None,
        "w_prime_kj": round(w_prime / 1000.0, 1) if w_prime > 0 else None,
        "r2": round(r2, 4),
        "anchors": [p[2] for p in points],
        "quality_flag": "OK" if valid else "; ".join(reasons),
    }


def solve_cs_d_prime(best_paces: dict[str, Any]) -> dict[str, Any]:
    """2-parameter Running Critical Speed (`d(t) = CS * t + D'`) solver from split records (1km–10km)."""
    dist_map = dict(RUN_SPLIT_DISTANCES)
    points: list[tuple[float, float, str]] = []
    for lbl, val in (best_paces or {}).items():
        dist_m = dist_map.get(lbl)
        if not dist_m:
            continue
        spk = None
        if isinstance(val, dict) and val.get("sec_per_km"):
            spk = float(val["sec_per_km"])
        elif isinstance(val, (int, float)):
            spk = float(val)
        elif isinstance(val, str):
            spk = parse_pace_to_spk(val)
        if spk and spk > 0:
            t_sec = spk * (dist_m / 1000.0)
            if 130.0 <= t_sec <= 3000.0:
                points.append((t_sec, dist_m, lbl))

    points.sort(key=lambda x: x[0])
    # Prefer anchors in 2.5m..35m (1km, 3km, 5km; include 10km only if <3 points or <=2400s)
    core_pts = [p for p in points if p[0] <= 1800.0]
    if len(core_pts) >= 2:
        points = core_pts

    if len(points) < 2:
        return {
            "valid": False,
            "cs_mps": None,
            "cs_pace_spk": None,
            "cs_pace_fmt": "—",
            "d_prime_m": None,
            "r2": 0.0,
            "anchors": [p[2] for p in points],
            "quality_flag": "need >=2 running split anchors (e.g. 1km/3km + 5km/10km)",
        }

    has_short = any(t < 600.0 for t, _, _ in points)
    has_long = any(t >= 720.0 for t, _, _ in points)
    ts = [p[0] for p in points]
    ds = [p[1] for p in points]
    cs_mps, d_prime_m, r2 = _ols_linear(ts, ds)

    v_short = points[0][1] / points[0][0]
    v_long = points[-1][1] / points[-1][0]
    reasons: list[str] = []
    if not has_short:
        reasons.append("missing short <10m running anchor (1km/3km)")
    if not has_long:
        reasons.append("missing >=12m running anchor (5km/10km)")
    if r2 < 0.98:
        reasons.append(f"R^2={r2:.3f} < 0.98")
    if cs_mps <= 1.5 or d_prime_m < 20.0 or v_short <= v_long * 1.02:
        reasons.append("non-maximal pace progression across distances")

    valid = len(reasons) == 0
    spk_cs = (1000.0 / cs_mps) if cs_mps > 0.5 else None
    return {
        "valid": valid,
        "cs_mps": round(cs_mps, 3) if cs_mps > 0 else None,
        "cs_pace_spk": round(spk_cs, 1) if spk_cs else None,
        "cs_pace_fmt": format_pace(spk_cs) if spk_cs else "—",
        "d_prime_m": int(round(d_prime_m)) if d_prime_m > 0 else None,
        "r2": round(r2, 4),
        "anchors": [p[2] for p in points],
        "quality_flag": "OK" if valid else "; ".join(reasons),
    }


def compute_skiba_w_bal(
    watts: list[float],
    cp_watts: float,
    w_prime_j: float = 20000.0,
    dt_s: float = 1.0,
) -> dict[str, Any]:
    """1Hz Skiba differential `W'bal` ODE (Skiba et al. 2012, 2015).

    For P_t > CP:  W'_bal(t) = max(0, W'_bal(t-1) - (P_t - CP) * dt)
    For P_t <= CP: tau_W' = 546 * exp(-0.01 * (CP - P_t)) + 316
                   W'_bal(t) = W' - (W' - W'_bal(t-1)) * exp(-dt / tau_W')
    """
    if not watts or cp_watts <= 0 or w_prime_j <= 0:
        return {"w_bal_min_pct": None, "w_bal_min_j": None, "matches_burned": 0, "kj_above_cp": 0.0}

    w_bal = float(w_prime_j)
    min_w_bal = w_bal
    total_above_j = 0.0

    # Match detection: a match is counted once per supra-CP bout / deep depletion cycle
    # when either continuous supra-CP work >= 2000 J (>2 kJ) OR W'bal drops < 50% W'.
    matches = 0
    bout_above_j = 0.0
    sub_cp_streak_s = 0.0
    bout_counted = False
    deep_armed = True

    for raw_p in watts:
        p = max(0.0, float(raw_p or 0.0))
        if p > cp_watts:
            burn = (p - cp_watts) * dt_s
            total_above_j += burn
            bout_above_j += burn
            sub_cp_streak_s = 0.0
            w_bal = max(0.0, w_bal - burn)
            if w_bal < min_w_bal:
                min_w_bal = w_bal

            if not bout_counted and (bout_above_j >= 2000.0 or (deep_armed and w_bal < 0.50 * w_prime_j)):
                matches += 1
                bout_counted = True
                if w_bal < 0.50 * w_prime_j:
                    deep_armed = False
        else:
            d_cp = cp_watts - p
            tau = 546.0 * math.exp(-0.01 * d_cp) + 316.0
            w_bal = w_prime_j - (w_prime_j - w_bal) * math.exp(-dt_s / tau)
            sub_cp_streak_s += dt_s
            # End continuous supra-CP bout after >10s below CP
            if sub_cp_streak_s >= 10.0:
                bout_above_j = 0.0
                bout_counted = False
            if w_bal >= 0.65 * w_prime_j:
                deep_armed = True

    return {
        "w_bal_min_pct": round((min_w_bal / w_prime_j) * 100.0, 1),
        "w_bal_min_j": int(round(min_w_bal)),
        "matches_burned": matches,
        "kj_above_cp": round(total_above_j / 1000.0, 1),
    }


def estimate_fiber_typology(mmp_dict: dict[str, int | float], cp_watts: float) -> dict[str, Any]:
    """Muscle Fiber Typology proxy from `P5s/CP`, `P15s/CP`, and log-log PDC decay exponent `b` (`P(t) = a * t^-b`)."""
    if not mmp_dict or not cp_watts or cp_watts <= 0:
        return {"typology": "Unknown", "p5s_cp": None, "p15s_cp": None, "pdc_b": None, "hint": ""}

    p5 = float(mmp_dict.get("5s") or 0.0)
    p15 = float(mmp_dict.get("15s") or 0.0)
    p5s_cp = round(p5 / cp_watts, 2) if p5 > 0 else None
    p15s_cp = round(p15 / cp_watts, 2) if p15 > 0 else None

    # Log-log regression across 15s..20m: ln(P) = ln(a) - b * ln(t)
    xs_ln: list[float] = []
    ys_ln: list[float] = []
    for lbl, sec in COACH_DURATIONS:
        if 15 <= sec <= 1200 and mmp_dict.get(lbl) and float(mmp_dict[lbl]) > 0:
            xs_ln.append(math.log(float(sec)))
            ys_ln.append(math.log(float(mmp_dict[lbl])))

    pdc_b = None
    if len(xs_ln) >= 3:
        slope, _inter, _r2 = _ols_linear(xs_ln, ys_ln)
        pdc_b = round(max(0.0, -slope), 3)

    if p5s_cp is None and p15s_cp is None and pdc_b is None:
        return {"typology": "Unknown", "p5s_cp": None, "p15s_cp": None, "pdc_b": None, "hint": ""}

    # Score typology signals (-1 Slow, 0 Balanced, +1 Fast)
    fast_votes = 0
    slow_votes = 0
    if p5s_cp is not None:
        if p5s_cp >= 3.60:
            fast_votes += 2
        elif p5s_cp <= 2.85:
            slow_votes += 2
    if p15s_cp is not None:
        if p15s_cp >= 3.10:
            fast_votes += 1
        elif p15s_cp <= 2.45:
            slow_votes += 1
    if pdc_b is not None:
        if pdc_b >= 0.16:
            fast_votes += 2
        elif pdc_b <= 0.10:
            slow_votes += 2

    if fast_votes > slow_votes and fast_votes >= 2:
        typ = "Fast"
        hint = "Type II bias: high anaerobic reserve; allow >=48h recovery after HIIT, moderate volume, higher F_opt cadence."
    elif slow_votes > fast_votes and slow_votes >= 2:
        typ = "Slow"
        hint = "Type I diesel bias: high fractional utilization & fatigue resistance; responds well to threshold density & volume."
    else:
        typ = "Balanced"
        hint = "Balanced Type I/IIa profile: responds well to polarized/pyramidal distribution and standard 24–48h HIIT spacing."

    return {
        "typology": typ,
        "p5s_cp": p5s_cp,
        "p15s_cp": p15s_cp,
        "pdc_b": pdc_b,
        "hint": hint,
    }


def compute_torque_cadence_quadrants(
    watts: list[float],
    cadence: list[float],
    cp_watts: float,
    ref_cadence: float = 88.0,
) -> dict[str, Any]:
    """Neuromuscular Torque-Cadence (`tau_t = P_t / omega_t`) and Coggan Q1–Q4 distribution."""
    n = min(len(watts or []), len(cadence or []))
    if n == 0:
        return {}

    ped_cads: list[float] = []
    ped_torques: list[float] = []
    ped_watts: list[float] = []
    peak_torque = 0.0

    omega_ref = 2.0 * math.pi * (ref_cadence / 60.0)
    tau_ref = (cp_watts / omega_ref) if cp_watts and cp_watts > 0 else 30.0
    q1 = q2 = q3 = q4 = 0

    for i in range(n):
        w = float(watts[i] or 0.0)
        c = float(cadence[i] or 0.0)
        if w <= 10.0 or c < 30.0 or c > 210.0:
            continue
        omega = 2.0 * math.pi * (c / 60.0)
        tau = w / omega
        ped_cads.append(c)
        ped_torques.append(tau)
        ped_watts.append(w)
        if c >= 40.0 and tau > peak_torque:
            peak_torque = tau

        if tau >= tau_ref and c >= ref_cadence:
            q1 += 1
        elif tau >= tau_ref and c < ref_cadence:
            q2 += 1
        elif tau < tau_ref and c < ref_cadence:
            q3 += 1
        else:
            q4 += 1

    total_ped = len(ped_cads)
    if total_ped == 0:
        return {}

    p_max = max(ped_watts)
    high_p_cads = [c for w, c in zip(ped_watts, ped_cads) if w >= 0.90 * p_max and c >= 50.0]
    f_opt = int(round(sum(high_p_cads) / len(high_p_cads))) if high_p_cads else int(round(sum(ped_cads) / total_ped))

    return {
        "avg_cadence": int(round(sum(ped_cads) / total_ped)),
        "avg_torque_nm": round(sum(ped_torques) / total_ped, 1),
        "peak_torque_nm": round(peak_torque, 1),
        "f_opt_rpm": f_opt,
        "quadrants": {
            "q1_pct": round((q1 / total_ped) * 100.0, 1),
            "q2_pct": round((q2 / total_ped) * 100.0, 1),
            "q3_pct": round((q3 / total_ped) * 100.0, 1),
            "q4_pct": round((q4 / total_ped) * 100.0, 1),
        },
    }


# ---------------------------------------------------------------------------
# 3. Metabolic Profiling, Honest Glycolytic Index, FatMax & Substrate Fueling
# ---------------------------------------------------------------------------

def compute_daniels_vdot(dist_m: float, time_s: float) -> float | None:
    """Daniels & Gilbert (1979) VDOT estimation from running race/split distance (m) and time (s)."""
    if dist_m < 1000.0 or time_s <= 0:
        return None
    t_min = time_s / 60.0
    v_m_min = dist_m / t_min
    vo2 = -4.60 + 0.182258 * v_m_min + 0.000104 * (v_m_min ** 2)
    pct_max = 0.8 + 0.1894393 * math.exp(-0.012778 * t_min) + 0.2989558 * math.exp(-0.1932605 * t_min)
    if pct_max <= 0.2 or vo2 <= 10.0:
        return None
    return round(vo2 / pct_max, 1)


def compute_metabolic_profile(
    mmp_dict: dict[str, int | float],
    cp_watts: float | None,
    w_prime_j: float | None,
    weight_kg: float,
    age: int | None = None,
    sex: str = "neutral",
    hr_max: int | None = None,
    rhr: int | None = None,
    best_paces: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Lab-free VO2max (Storer 1990 + Uth 2004 + Daniels VDOT), Honest Glycolytic Index, and FatMax window."""
    bw = weight_kg if weight_kg and weight_kg > 25.0 else 70.0
    eff_age = age if age and 14 <= age <= 90 else 35

    map_5m = float((mmp_dict or {}).get("5m") or 0.0)
    map_estimated = False
    if map_5m <= 0 and cp_watts and cp_watts > 0:
        map_5m = cp_watts / 0.83
        map_estimated = True

    vo2_storer = None
    if map_5m > 0:
        vo2_m = (10.51 * map_5m + 6.35 * bw - 10.49 * eff_age + 519.3) / bw
        vo2_f = (9.39 * map_5m + 7.70 * bw - 5.88 * eff_age + 136.7) / bw
        if sex == "male":
            vo2_storer = round(vo2_m, 1)
        elif sex == "female":
            vo2_storer = round(vo2_f, 1)
        else:
            vo2_storer = round((vo2_m + vo2_f) / 2.0, 1)

    vo2_uth = None
    if hr_max and rhr and hr_max > rhr >= 30:
        vo2_uth = round(15.3 * (float(hr_max) / float(rhr)), 1)

    vdot = None
    for d_lbl, dist_m in (("5km", 5000.0), ("10km", 10000.0), ("3km", 3000.0)):
        pinfo = (best_paces or {}).get(d_lbl)
        spk = None
        if isinstance(pinfo, dict) and pinfo.get("sec_per_km"):
            spk = float(pinfo["sec_per_km"])
        elif isinstance(pinfo, str):
            spk = parse_pace_to_spk(pinfo)
        if spk and spk > 0:
            cand = compute_daniels_vdot(dist_m, spk * (dist_m / 1000.0))
            if cand and (vdot is None or cand > vdot):
                vdot = cand

    # Honest Glycolytic Index (never fabricate mmol/L/s VLamax without blood lactate)
    wp_kj_kg = round((w_prime_j / 1000.0) / bw, 2) if w_prime_j and w_prime_j > 0 else None
    p15 = float((mmp_dict or {}).get("15s") or 0.0)
    p5m_raw = float((mmp_dict or {}).get("5m") or 0.0)
    p15s_p5m = round(p15 / p5m_raw, 2) if p15 > 0 and p5m_raw > 0 else None
    cp_map = round(float(cp_watts) / p5m_raw, 2) if cp_watts and cp_watts > 0 and p5m_raw > 0 else None

    gly_band = "Unknown"
    if cp_map is not None or wp_kj_kg is not None or p15s_p5m is not None:
        high_g = 0
        low_g = 0
        if cp_map is not None:
            if cp_map < 0.78:
                high_g += 2
            elif cp_map > 0.85:
                low_g += 2
        if wp_kj_kg is not None:
            if wp_kj_kg >= 0.32:
                high_g += 1
            elif wp_kj_kg <= 0.22:
                low_g += 1
        if p15s_p5m is not None:
            if p15s_p5m >= 2.65:
                high_g += 1
            elif p15s_p5m <= 2.10:
                low_g += 1
        if high_g > low_g and high_g >= 2:
            gly_band = "High (Punchy/Glycolytic)"
        elif low_g > high_g and low_g >= 2:
            gly_band = "Low (Diesel/Economical)"
        else:
            gly_band = "Moderate"

    fatmax_w = None
    if map_5m > 0:
        fatmax_w = (int(round(0.55 * map_5m)), int(round(0.72 * map_5m)))

    return {
        "vo2max_storer": vo2_storer,
        "vo2max_map_estimated": map_estimated,
        "vo2max_uth": vo2_uth,
        "running_vdot": vdot,
        "w_prime_kj_kg": wp_kj_kg,
        "p15s_p5m": p15s_p5m,
        "cp_map_ratio": cp_map,
        "glycolytic_band": gly_band,
        "fatmax_watts": fatmax_w,
    }


def _fat_energy_fraction(rel_vo2: float) -> float:
    """Piecewise Achten & Jeukendrup (2002) fat energy share vs fraction of VO2max (`u = P / MAP_5m`)."""
    u = max(0.0, rel_vo2)
    if u <= 0.35:
        return 0.65
    if u <= 0.62:
        # FatMax plateau ~55-70% of metabolic energy
        return 0.62
    if u < 0.88:
        # Crossover decline toward 0% fat at >=88% VO2max (RER >= 1.00)
        return max(0.0, 0.62 * (0.88 - u) / (0.88 - 0.62))
    return 0.0


def compute_substrate_fueling(
    watts: list[float] | None = None,
    avg_watts: float = 0.0,
    duration_s: float = 0.0,
    cp_watts: float = 275.0,
    map_5m_w: float | None = None,
    if_val: float | None = None,
) -> dict[str, Any]:
    """Gross-efficiency (~24%) CHO/Fat oxidation (`g`) and glycogen-sparing fueling target (`g/h`)."""
    eff_cp = cp_watts if cp_watts and cp_watts > 0 else 250.0
    eff_map = map_5m_w if map_5m_w and map_5m_w > 0 else (eff_cp / 0.83)
    ge = 0.24  # ~24% gross mechanical efficiency

    cho_kcal = 0.0
    fat_kcal = 0.0
    dur = int(len(watts)) if watts else int(round(duration_s))

    if watts:
        for raw_p in watts:
            p = max(0.0, float(raw_p or 0.0))
            if p <= 0.0:
                continue
            kcal_s = (p / ge) / 4184.0
            f_share = _fat_energy_fraction(p / eff_map)
            fat_kcal += kcal_s * f_share
            cho_kcal += kcal_s * (1.0 - f_share)
    elif avg_watts > 0 and dur > 0:
        total_kcal_est = ((avg_watts * dur) / ge) / 4184.0
        f_share = _fat_energy_fraction(avg_watts / eff_map)
        fat_kcal = total_kcal_est * f_share
        cho_kcal = total_kcal_est * (1.0 - f_share)

    total_kcal = int(round(cho_kcal + fat_kcal))
    cho_g = int(round(cho_kcal / 4.0))
    fat_g = int(round(fat_kcal / 9.0))
    dur_min = dur / 60.0
    eff_if = if_val if if_val is not None else ((avg_watts / eff_cp) if avg_watts > 0 else 0.70)

    if dur_min < 60 and cho_g <= 120:
        target_gh = 0
        rec = "0–30 g/h (water/electrolytes; endogenous glycogen sufficient)"
    elif dur_min <= 90 and cho_g <= 250:
        target_gh = 45
        rec = "30–60 g/h CHO (single-source glucose/maltodextrin)"
    elif cho_g > 350 or (dur_min > 150 and eff_if >= 0.78):
        target_gh = 100
        rec = "90–120 g/h CHO (1:0.8 maltodextrin:fructose dual-transporter to protect ~2000 kcal glycogen store)"
    elif eff_if >= 0.78 or dur_min > 150 or cho_g > 300:
        target_gh = 90
        rec = "80–90 g/h CHO (1:0.8 maltodextrin:fructose recommended)"
    else:
        target_gh = 60
        rec = "60 g/h CHO + electrolytes"

    return {
        "total_kcal": total_kcal,
        "cho_burn_g": cho_g,
        "fat_burn_g": fat_g,
        "fueling_g_h": target_gh,
        "fueling_recommendation": rec,
    }


# ---------------------------------------------------------------------------
# 4. Multi-Tier Durability, Steady-State `Pw:Hr` & Environmental Adjustments
# ---------------------------------------------------------------------------

def compute_multi_tier_durability(
    watts: list[float],
    weight_kg: float,
    cp_watts: float,
) -> dict[str, dict[str, int]]:
    """Body-weight-normalized fatigued MMP tiers (`>15`, `>25`, `>35 kJ/kg` + `>5 kJ/kg above CP`)."""
    n = len(watts)
    if not n:
        return {}
    bw = weight_kg if weight_kg and weight_kg > 25.0 else 70.0
    eff_cp = cp_watts if cp_watts and cp_watts > 0 else 250.0

    thresholds_total = {
        "15kj_kg": 15_000.0 * bw,
        "25kj_kg": 25_000.0 * bw,
        "35kj_kg": 35_000.0 * bw,
    }
    thresh_above_cp = 5_000.0 * bw

    idx_map: dict[str, int | None] = {k: None for k in ("15kj_kg", "25kj_kg", "35kj_kg", "5kj_above_cp_kg")}
    cum_j = 0.0
    cum_above_j = 0.0

    for i, raw_w in enumerate(watts):
        w = max(0.0, float(raw_w or 0.0))
        cum_j += w
        if w > eff_cp:
            cum_above_j += (w - eff_cp)
        for k, th in thresholds_total.items():
            if idx_map[k] is None and cum_j >= th:
                idx_map[k] = i + 1
        if idx_map["5kj_above_cp_kg"] is None and cum_above_j >= thresh_above_cp:
            idx_map["5kj_above_cp_kg"] = i + 1

    dur_subset = (("5s", 5), ("15s", 15), ("1m", 60), ("5m", 300), ("10m", 600), ("20m", 1200), ("60m", 3600))
    out: dict[str, dict[str, int]] = {}
    for k, start_idx in idx_map.items():
        if start_idx is not None and start_idx < n:
            peaks = mmp_from_watts(watts[start_idx:], durations=dur_subset)
            if peaks:
                out[k] = peaks
    return out


def compute_steady_state_decoupling(
    watts: list[float],
    hr: list[float],
    cp_watts: float,
) -> float | None:
    """Compute `Pw:Hr` aerobic decoupling (%) strictly on steady sub-CP segments (excluding coasting & surges)."""
    n = min(len(watts or []), len(hr or []))
    if n < 1200:
        return None
    eff_cp = cp_watts if cp_watts and cp_watts > 0 else 250.0
    start_i = 600 if n >= 2400 else 180
    low_w = max(50.0, 0.40 * eff_cp)
    high_w = 1.02 * eff_cp

    steady_pairs: list[tuple[float, float]] = []
    post_surge_cooldown = 0
    for i in range(start_i, n):
        w = float(watts[i] or 0.0)
        h = float(hr[i] or 0.0)
        if w > high_w:
            post_surge_cooldown = 30  # exclude 30s elevated HR tail after supra-CP surge
            continue
        if post_surge_cooldown > 0:
            post_surge_cooldown -= 1
            continue
        if low_w <= w <= high_w and 75.0 <= h <= 220.0:
            steady_pairs.append((w, h))

    if len(steady_pairs) < 600:
        return None
    mid = len(steady_pairs) // 2
    p1, p2 = steady_pairs[:mid], steady_pairs[mid:]
    w1 = sum(x[0] for x in p1) / len(p1)
    h1 = sum(x[1] for x in p1) / len(p1)
    w2 = sum(x[0] for x in p2) / len(p2)
    h2 = sum(x[1] for x in p2) / len(p2)
    if h1 <= 75.0 or h2 <= 75.0 or w1 <= 40.0:
        return None
    ef1 = w1 / h1
    ef2 = w2 / h2
    return round(((ef1 - ef2) / ef1) * 100.0, 1)


def compute_environment_adjustments(
    altitude: list[float] | None,
    temp: list[float] | None,
    cp_watts: float | None = None,
) -> dict[str, Any]:
    """Peronnet et al. (1991) hypoxia decay (~-6.3% per 1000m > 1000m) and >25°C thermal drift detection."""
    out: dict[str, Any] = {}
    if altitude:
        valid_alt = [float(a) for a in altitude if a is not None and -200.0 <= float(a) <= 6000.0]
        if valid_alt:
            avg_m = sum(valid_alt) / len(valid_alt)
            max_m = max(valid_alt)
            out["elev_avg_m"] = int(round(avg_m))
            out["elev_max_m"] = int(round(max_m))
            if avg_m > 1000.0:
                loss_frac = min(0.35, 0.063 * ((avg_m - 1000.0) / 1000.0))
                out["hypoxia_loss_pct"] = round(loss_frac * 100.0, 1)
                if cp_watts and cp_watts > 0:
                    out["cp_alt_adjusted_w"] = int(round(cp_watts * (1.0 - loss_frac)))
            else:
                out["hypoxia_loss_pct"] = 0.0

    if temp:
        valid_t = [float(t) for t in temp if t is not None and -30.0 <= float(t) <= 60.0]
        if valid_t:
            avg_c = round(sum(valid_t) / len(valid_t), 1)
            max_c = round(max(valid_t), 1)
            out["temp_c"] = avg_c
            out["temp_max_c"] = max_c
            out["thermal_drift"] = bool(avg_c > 25.0 or max_c >= 28.0)

    return out


def compute_stream_metrics(
    streams: dict[str, Any],
    ftp_watts: int = 275,
    weight_kg: float = 67.0,
    sport: str = "cycling",
    cp_watts: float | None = None,
    w_prime_j: float | None = None,
) -> dict[str, Any]:
    """Full 0-token 1Hz stream analyzer for cycling & running."""
    watts = [float(x or 0.0) for x in (streams.get("watts") or [])]
    hr = [float(x or 0.0) for x in (streams.get("heartrate") or [])]
    cad = [float(x or 0.0) for x in (streams.get("cadence") or [])]
    vel = [float(x or 0.0) for x in (streams.get("velocity_smooth") or [])]
    alt = streams.get("altitude") or []
    tmp = streams.get("temp") or []

    eff_cp = float(cp_watts or ftp_watts or 275)
    eff_wp = float(w_prime_j or 20000.0)
    out: dict[str, Any] = {}

    if watts:
        n = len(watts)
        joules = sum(max(0.0, w) for w in watts)
        out["work_kj"] = int(round(joules / 1000.0))
        out["work_kj_kg"] = round((joules / 1000.0) / max(30.0, weight_kg), 1)
        out["avg_w"] = int(round(joules / n))

        if n >= 30:
            w_sum = sum(watts[:30])
            fourth_sum = (max(0.0, w_sum) / 30.0) ** 4
            cnt = 1
            for i in range(30, n):
                w_sum += watts[i] - watts[i - 30]
                fourth_sum += (max(0.0, w_sum) / 30.0) ** 4
                cnt += 1
            np_w = int(round((fourth_sum / cnt) ** 0.25))
        else:
            np_w = out["avg_w"]
        out["np_w"] = np_w

        ref_thresh = float(ftp_watts or eff_cp)
        if ref_thresh > 0:
            if_val = round(np_w / ref_thresh, 2)
            out["if"] = if_val
            out["tss"] = int(round((n * np_w * if_val) / (ref_thresh * 3600.0) * 100.0))

        out["peaks_fresh"] = mmp_from_watts(watts)

        # Legacy >1500 kJ fatigued MMP (preserved for backward compatibility)
        cum_j = 0.0
        idx_1500 = None
        for i, w in enumerate(watts):
            cum_j += max(0.0, w)
            if cum_j >= 1_500_000.0:
                idx_1500 = i + 1
                break
        out["peaks_fatigued"] = mmp_from_watts(watts[idx_1500:]) if (idx_1500 is not None and idx_1500 < n) else {}

        # Multi-tier kJ/kg + >CP durability
        dur_tiers = compute_multi_tier_durability(watts, weight_kg=weight_kg, cp_watts=eff_cp)
        if dur_tiers:
            out["peaks_durability"] = dur_tiers

        # Skiba W'bal 1Hz ODE
        wbal = compute_skiba_w_bal(watts, cp_watts=eff_cp, w_prime_j=eff_wp)
        if wbal.get("w_bal_min_pct") is not None:
            out["w_bal_min_pct"] = wbal["w_bal_min_pct"]
            out["matches_burned"] = wbal["matches_burned"]
            out["kj_above_cp"] = wbal["kj_above_cp"]

        # Steady-state filtered Pw:Hr decoupling
        if len(hr) == n:
            ss_dec = compute_steady_state_decoupling(watts, hr, cp_watts=eff_cp)
            if ss_dec is not None:
                out["decoupling_pct"] = ss_dec
                out["steady_decoupling_pct"] = ss_dec

        # Neuromuscular Torque-Cadence & Q1–Q4
        if cad:
            tc = compute_torque_cadence_quadrants(watts, cad, cp_watts=eff_cp)
            out.update(tc)

        # Substrate oxidation & fueling recommendation
        map_5m = float((out["peaks_fresh"] or {}).get("5m") or (eff_cp / 0.83))
        sub = compute_substrate_fueling(watts=watts, cp_watts=eff_cp, map_5m_w=map_5m, if_val=out.get("if"))
        out["cho_burn_g"] = sub["cho_burn_g"]
        out["fat_burn_g"] = sub["fat_burn_g"]
        out["fueling_g_h"] = sub["fueling_g_h"]

    if hr:
        valid_hr = [x for x in hr if x > 30]
        if valid_hr:
            out["avg_hr"] = int(round(sum(valid_hr) / len(valid_hr)))
            out["max_hr"] = int(round(max(valid_hr)))

    env = compute_environment_adjustments(alt, tmp, cp_watts=eff_cp)
    out.update(env)

    if sport == "running" and vel:
        n_v = len(vel)
        pref_d = [0.0] * (n_v + 1)
        for i, v in enumerate(vel):
            pref_d[i + 1] = pref_d[i] + max(0.0, v)
        best_paces: dict[str, dict[str, Any]] = {}
        for d_lbl, dist_m in RUN_SPLIT_DISTANCES:
            if pref_d[-1] < dist_m:
                continue
            best_sec = None
            j = 0
            for i in range(n_v):
                while j < n_v and (pref_d[j] - pref_d[i]) < dist_m:
                    j += 1
                if (pref_d[j] - pref_d[i]) >= dist_m:
                    sec = j - i
                    if best_sec is None or sec < best_sec:
                        best_sec = sec
            if best_sec and best_sec > 0:
                sec_per_km = best_sec / (dist_m / 1000.0)
                best_paces[d_lbl] = {"sec_per_km": round(sec_per_km, 1), "fmt": format_pace(sec_per_km)}
        if best_paces:
            out["best_paces"] = best_paces

    return out


# ---------------------------------------------------------------------------
# 5. Cross-Sport Systemic Load (`TSS`, `rTSS`, `TRIMP/hrTSS`, `sRPE`) & PMC
# ---------------------------------------------------------------------------

def _extract_rpe_from_activity(act: dict[str, Any]) -> float | None:
    if act.get("rpe") is not None:
        try:
            v = float(act["rpe"])
            if 1.0 <= v <= 10.0:
                return v
        except (TypeError, ValueError):
            pass
    fb = str(act.get("feedback") or "")
    m = re.search(r"\brpe\s*[:=]?\s*(\d{1,2}(?:[.,]\d)?)\s*(?:/\s*10)?\b", fb, re.I)
    if m:
        v = float(m.group(1).replace(",", "."))
        if 1.0 <= v <= 10.0:
            return v
    return None


def compute_banister_hrtss(
    duration_min: float,
    avg_hr: float,
    hr_rest: float = 52.0,
    hr_max: float = 190.0,
    lthr: float | None = None,
    sex: str = "neutral",
) -> float:
    """Banister exponential TRIMP normalized to 1 hour at LTHR = 100 hrTSS."""
    if duration_min <= 0 or avg_hr <= hr_rest or hr_max <= hr_rest + 10:
        return 0.0
    eff_lthr = lthr if (lthr and hr_rest < lthr < hr_max) else (hr_rest + 0.85 * (hr_max - hr_rest))
    hrr = max(0.0, min(1.0, (avg_hr - hr_rest) / (hr_max - hr_rest)))
    hrr_thr = max(0.2, min(0.98, (eff_lthr - hr_rest) / (hr_max - hr_rest)))

    def _y(r: float) -> float:
        ym = 0.64 * math.exp(1.92 * r)
        yf = 0.86 * math.exp(1.67 * r)
        if sex == "male":
            return ym
        if sex == "female":
            return yf
        return 0.5 * (ym + yf)

    trimp = duration_min * hrr * _y(hrr)
    trimp_1h_thr = 60.0 * hrr_thr * _y(hrr_thr)
    if trimp_1h_thr <= 1e-6:
        return 0.0
    return round((trimp / trimp_1h_thr) * 100.0, 1)


def compute_activity_load(
    act: dict[str, Any],
    profile: dict[str, Any] | None = None,
    cs_mps: float | None = None,
    cp_watts: float | None = None,
) -> tuple[float, str]:
    """Return `(systemic_tss, method)` across cycling, running, HR-only, and strength."""
    prof = profile or {}
    dur_min = float(act.get("duration_min") or 0.0)
    if dur_min <= 0:
        return 0.0, "none"
    dur_h = dur_min / 60.0
    sport = str(act.get("sport") or "cycling").lower()

    # 1. Power-based Coggan TSS
    if act.get("tss") is not None and float(act["tss"]) > 0:
        return round(float(act["tss"]), 1), "power_tss"
    np_w = float(act.get("np_w") or act.get("avg_w") or 0.0)
    thresh_w = float(cp_watts or prof.get("cp_w") or prof.get("ftp_w") or 275.0)
    if np_w > 0 and thresh_w > 0 and sport == "cycling":
        if_v = np_w / thresh_w
        return round(dur_h * (if_v ** 2) * 100.0, 1), "power_tss"

    # 2. Running Pace-based rTSS (with elevation grade equivalent)
    dist_km = float(act.get("distance_km") or 0.0)
    eff_cs = cs_mps or prof.get("cs_mps")
    if sport == "running" and dist_km > 0.2 and eff_cs and eff_cs > 1.5:
        elev_m = float(act.get("elevation_m") or 0.0)
        ngp_dist_m = dist_km * 1000.0 + 4.0 * max(0.0, elev_m)
        vel_mps = ngp_dist_m / (dur_min * 60.0)
        if_run = min(1.5, max(0.3, vel_mps / float(eff_cs)))
        return round(dur_h * (if_run ** 2) * 100.0, 1), "rtss"

    # 3. Strength / Gym Foster sRPE load -> systemic TSS (~45-80 TSS/h)
    rpe = _extract_rpe_from_activity(act)
    if sport == "strength":
        eff_rpe = rpe if rpe is not None else 6.0
        return round(dur_h * (15.0 + 7.5 * eff_rpe), 1), "srpe_tss"

    # 4. HR-based Banister TRIMP -> hrTSS
    avg_hr = float(act.get("avg_hr") or 0.0)
    if avg_hr >= 80.0:
        hr_rest = float(prof.get("rhr") or 52.0)
        hr_max = float(prof.get("hr_max") or 190.0)
        hrtss = compute_banister_hrtss(
            dur_min, avg_hr, hr_rest=hr_rest, hr_max=hr_max,
            lthr=prof.get("lthr"), sex=str(prof.get("sex") or "neutral"),
        )
        if hrtss > 0:
            return hrtss, "hrtss"

    # 5. Running fallback when CS not yet calibrated (pace heuristic) or sRPE fallback
    if sport == "running" and dist_km > 0.2:
        vel_mps = (dist_km * 1000.0) / (dur_min * 60.0)
        if_est = min(1.35, max(0.45, vel_mps / 3.8))
        return round(dur_h * (if_est ** 2) * 100.0, 1), "rtss_est"

    eff_rpe = rpe if rpe is not None else 5.0
    return round(dur_h * (12.0 + 7.0 * eff_rpe), 1), "srpe_tss"


def compute_pmc_metrics(
    activities: list[dict[str, Any]],
    profile: dict[str, Any] | None = None,
    cs_mps: float | None = None,
    cp_watts: float | None = None,
    today: dt.date | None = None,
) -> dict[str, Any]:
    """Compute longitudinal daily `CTL` (42d), `ATL` (7d), `TSB`, `Ramp Rate`, `ACWR`, `Monotony`, and `Strain`."""
    ref_today = today or dt.date.today()
    daily_load: dict[dt.date, float] = {}
    earliest = ref_today - dt.timedelta(days=120)

    for a in activities or []:
        if not isinstance(a, dict) or not a.get("date"):
            continue
        try:
            d = dt.date.fromisoformat(str(a["date"])[:10])
        except ValueError:
            continue
        if d > ref_today:
            continue
        if d < earliest:
            earliest = d
        tss_val, _method = compute_activity_load(a, profile=profile, cs_mps=cs_mps, cp_watts=cp_watts)
        daily_load[d] = daily_load.get(d, 0.0) + tss_val

    ctl = 0.0
    atl = 0.0
    ctl_hist: dict[dt.date, float] = {}
    series_42d: list[dict[str, Any]] = []

    span_days = (ref_today - earliest).days
    for step in range(span_days + 1):
        cur_d = earliest + dt.timedelta(days=step)
        ld = daily_load.get(cur_d, 0.0)
        ctl = ctl + (ld - ctl) / 42.0
        atl = atl + (ld - atl) / 7.0
        tsb = ctl - atl
        ctl_hist[cur_d] = ctl
        if (ref_today - cur_d).days < 42:
            series_42d.append({
                "date": cur_d.isoformat(),
                "load": round(ld, 1),
                "ctl": round(ctl, 1),
                "atl": round(atl, 1),
                "tsb": round(tsb, 1),
            })

    tsb_now = ctl - atl
    d_7ago = ref_today - dt.timedelta(days=7)
    ctl_7ago = ctl_hist.get(d_7ago, 0.0)
    ramp_7d = ctl - ctl_7ago
    acwr = round(atl / ctl, 2) if ctl >= 5.0 else None

    loads_7d = [daily_load.get(ref_today - dt.timedelta(days=k), 0.0) for k in range(7)]
    sum_7d = sum(loads_7d)
    mean_7d = sum_7d / 7.0
    sd_7d = statistics.pstdev(loads_7d)
    if sum_7d <= 0.0:
        monotony = 0.0
    elif sd_7d <= 1e-6:
        monotony = 10.0
    else:
        monotony = round(mean_7d / sd_7d, 2)
    strain = int(round(sum_7d * monotony))

    warnings: list[str] = []
    if ramp_7d > 7.0:
        warnings.append(f"High 7d CTL ramp (+{ramp_7d:.1f}/wk > +7 guardrail)")
    elif ramp_7d > 5.0:
        warnings.append(f"Aggressive 7d CTL ramp (+{ramp_7d:.1f}/wk)")
    if acwr is not None and ctl >= 15.0 and acwr > 1.5:
        warnings.append(f"ACWR {acwr:.2f} > 1.5 injury/overreach zone")
    if monotony > 2.0 and sum_7d >= 150.0:
        warnings.append(f"High Foster Monotony ({monotony:.2f} > 2.0) — add polarization/rest day")

    return {
        "ctl": round(ctl, 1),
        "atl": round(atl, 1),
        "tsb": round(tsb_now, 1),
        "ramp_7d": round(ramp_7d, 1),
        "acwr": acwr,
        "weekly_load_7d": int(round(sum_7d)),
        "monotony": monotony,
        "strain": strain,
        "warnings": warnings,
        "series_42d": series_42d,
    }


# ---------------------------------------------------------------------------
# 6. Readiness Log (`readiness.md`), Interference (`AMPK/mTORC1`) & Bosquet Taper
# ---------------------------------------------------------------------------

READINESS_HEADER = (
    "| Date | RHR (bpm) | Sleep (h) | RPE (1-10) | Soreness (1-5) | CTL | ATL | TSB | ACWR | Readiness | Notes |\n"
    "|---|---|---|---|---|---|---|---|---|---|---|"
)


def parse_readiness_log(text: str) -> list[dict[str, Any]]:
    """Parse `brain/coach/readiness.md` Markdown table rows (newest first)."""
    rows: list[dict[str, Any]] = []
    if not text:
        return rows
    for raw in text.splitlines():
        s = raw.strip()
        if not s.startswith("|") or s.count("|") < 6:
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", cells[0]):
            continue

        def _num(idx: int, is_int: bool = False) -> float | int | None:
            if idx >= len(cells):
                return None
            v = re.sub(r"[^0-9.\-+]", "", cells[idx].replace(",", "."))
            if not v or v in ("-", "+", "."):
                return None
            try:
                return int(round(float(v))) if is_int else float(v)
            except ValueError:
                return None

        slp_cell = cells[2] if len(cells) > 2 else ""
        m_slp = re.match(r"^\s*(\d+(?:[.,]\d+)?)", slp_cell)
        slp_val = float(m_slp.group(1).replace(",", ".")) if m_slp else None
        m_sq = re.search(r"\(([^)]+)\)", slp_cell)
        sq_val = m_sq.group(1).strip() if m_sq else None

        rows.append({
            "date": cells[0],
            "rhr": _num(1, is_int=True),
            "sleep_h": slp_val,
            "sleep_quality": sq_val,
            "rpe": _num(3),
            "soreness": _num(4, is_int=True),
            "ctl": _num(5),
            "atl": _num(6),
            "tsb": _num(7),
            "acwr": _num(8),
            "status": cells[9].strip() if len(cells) > 9 and cells[9].strip() not in ("—", "-") else "",
            "notes": cells[10].strip() if len(cells) > 10 and cells[10].strip() not in ("—", "-") else "",
        })
    rows.sort(key=lambda r: r["date"], reverse=True)
    return rows


def render_readiness_markdown(
    existing_text: str,
    rows: list[dict[str, Any]],
    today_iso: str,
) -> str:
    """Render `brain/coach/readiness.md` preserving custom header/prose outside the table."""
    sorted_rows = sorted(rows, key=lambda r: r["date"], reverse=True)
    table_lines = [READINESS_HEADER]
    for r in sorted_rows[:180]:
        rhr_s = f"{r['rhr']}" if r.get("rhr") is not None else "—"
        slp_base = f"{r['sleep_h']:.1f}" if r.get("sleep_h") is not None else "—"
        sq_raw = str(r.get("sleep_quality") or "").replace("|", "/").replace("(", "").replace(")", "").strip()
        slp_s = f"{slp_base} ({sq_raw})" if sq_raw else slp_base
        rpe_s = f"{r['rpe']:g}" if r.get("rpe") is not None else "—"
        sor_s = f"{r['soreness']}" if r.get("soreness") is not None else "—"
        ctl_s = f"{r['ctl']:.1f}" if r.get("ctl") is not None else "—"
        atl_s = f"{r['atl']:.1f}" if r.get("atl") is not None else "—"
        tsb_s = f"{r['tsb']:+.1f}" if r.get("tsb") is not None else "—"
        acw_s = f"{r['acwr']:.2f}" if r.get("acwr") is not None else "—"
        st_s = r.get("status") or "—"
        nt_s = str(r.get("notes") or "—").replace("|", "/").replace("\n", " ").strip() or "—"
        table_lines.append(
            f"| {r['date']} | {rhr_s} | {slp_s} | {rpe_s} | {sor_s} | {ctl_s} | {atl_s} | {tsb_s} | {acw_s} | {st_s} | {nt_s} |"
        )

    if not existing_text or not existing_text.strip():
        return (
            "---\n"
            "summary: Daily athlete readiness log (morning RHR, sleep, soreness, RPE) and longitudinal CTL/ATL/TSB/ACWR state\n"
            "tags: [sport, log]\n"
            "aliases: [Readiness, Erholung, Ruhepuls, Schlaf, Recovery, Tagesform]\n"
            f"updated: {today_iso}\n"
            "---\n"
            "# Athlete Readiness & Systemic Load Log\n\n"
            f"Stand: {today_iso}. Part of [Sport-Coach Hub](README.md). Updated via "
            "`atlas.py coach note --rhr <bpm> --sleep <h> --soreness <1-5> [--rpe <1-10>] [--text \"...\"]`.\n\n"
            "## Daily Readiness & PMC State\n\n"
            + "\n".join(table_lines)
            + "\n"
        )

    # Update `updated:` in frontmatter and replace existing Markdown table cleanly
    updated_txt = re.sub(r"(?m)^updated:\s*\S+", f"updated: {today_iso}", existing_text, count=1)
    lines = updated_txt.splitlines()
    t_start = None
    t_end = None
    for i, ln in enumerate(lines):
        if ln.strip().startswith("|") and "RHR" in ln and "Sleep" in ln:
            t_start = i
            t_end = i + 1
            while t_end < len(lines) and lines[t_end].strip().startswith("|"):
                t_end += 1
            break
    if t_start is not None and t_end is not None:
        new_lines = lines[:t_start] + table_lines + lines[t_end:]
        return "\n".join(new_lines).rstrip() + "\n"
    return updated_txt.rstrip() + "\n\n## Daily Readiness & PMC State\n\n" + "\n".join(table_lines) + "\n"


def evaluate_readiness_and_guardrails(
    readiness_rows: list[dict[str, Any]],
    profile: dict[str, Any],
    pmc: dict[str, Any],
    activities: list[dict[str, Any]],
    cal_items: list[dict[str, Any]] | None = None,
    today: dt.date | None = None,
) -> dict[str, Any]:
    """Compute `GREEN | AMBER | RED` readiness, `AMPK/mTORC1` interference flags, and Bosquet taper status."""
    ref_today = today or dt.date.today()
    today_iso = ref_today.isoformat()
    cutoff_30d = (ref_today - dt.timedelta(days=30)).isoformat()
    cutoff_2d = (ref_today - dt.timedelta(days=2)).isoformat()

    # Latest readiness check-in (today or yesterday)
    recent_row = next((r for r in (readiness_rows or []) if r["date"] >= cutoff_2d), None)
    rhr_hist = [
        float(r["rhr"])
        for r in (readiness_rows or [])
        if r.get("rhr") is not None and cutoff_30d <= r["date"] < today_iso
    ]
    base_rhr = statistics.median(rhr_hist) if rhr_hist else (float(profile["rhr"]) if profile.get("rhr") else None)

    red_reasons: list[str] = []
    amber_reasons: list[str] = []

    if recent_row:
        if recent_row.get("rhr") is not None and base_rhr is not None:
            delta_rhr = float(recent_row["rhr"]) - base_rhr
            if delta_rhr >= 7.0:
                red_reasons.append(f"RHR +{delta_rhr:.0f} bpm vs 30d baseline ({base_rhr:.0f} bpm)")
            elif delta_rhr >= 4.0:
                amber_reasons.append(f"RHR +{delta_rhr:.0f} bpm vs 30d ({base_rhr:.0f} bpm)")
        if recent_row.get("sleep_h") is not None:
            slp = float(recent_row["sleep_h"])
            if slp < 5.5:
                red_reasons.append(f"Sleep {slp:.1f}h (<5.5h)")
            elif slp < 6.5:
                amber_reasons.append(f"Sleep {slp:.1f}h")
        if recent_row.get("sleep_quality"):
            sq_low = str(recent_row["sleep_quality"]).strip().lower()
            if sq_low in ("terrible", "awful", "1", "1/5"):
                red_reasons.append(f"Poor sleep quality ({recent_row['sleep_quality']})")
            elif sq_low in ("poor", "bad", "restless", "schlecht", "unruhig", "2", "2/5"):
                amber_reasons.append(f"Sleep quality {recent_row['sleep_quality']}")
        if recent_row.get("soreness") is not None:
            sor = int(recent_row["soreness"])
            if sor >= 5:
                red_reasons.append(f"Severe soreness ({sor}/5)")
            elif sor >= 4:
                amber_reasons.append(f"Elevated soreness ({sor}/5)")

    tsb = float(pmc.get("tsb") or 0.0)
    if tsb < -30.0:
        red_reasons.append(f"Deep fatigue TSB {tsb:.1f}")
    elif tsb < -20.0:
        amber_reasons.append(f"Accumulated load TSB {tsb:.1f}")

    # Calendar [blocked], deadlines, AMPK/mTORC1 interference & Bosquet taper
    items = cal_items or []
    blocked_today: list[str] = []
    blocked_soon: list[str] = []
    deadlines_soon: list[str] = []
    goal_a_Nearest: dict[str, Any] | None = None
    planned_workouts: list[dict[str, Any]] = []

    for it in items:
        if it.get("status") not in (None, "open", "done"):
            continue
        st_d = it.get("start")
        end_d = it.get("end") or st_d
        if not isinstance(st_d, dt.date):
            continue
        delta = (st_d - ref_today).days
        kind = it.get("kind")
        what = str(it.get("what") or "").strip()

        if kind == "blocked" and it.get("status", "open") == "open":
            if st_d <= ref_today <= end_d:
                blocked_today.append(what)
            elif 0 < delta <= 5:
                blocked_soon.append(f"{st_d.isoformat()} {what}")
        elif kind == "deadline" and it.get("status", "open") == "open" and 0 <= delta <= 2:
            deadlines_soon.append(f"{st_d.isoformat()} {what}")
        elif kind == "goal" and it.get("status", "open") == "open" and (it.get("subkind") or "A").upper() == "A" and delta >= 0:
            if goal_a_Nearest is None or delta < goal_a_Nearest["days"]:
                goal_a_Nearest = {"date": st_d.isoformat(), "days": delta, "what": what}
        elif kind == "workout" and it.get("status", "open") == "open" and 0 <= delta <= 7:
            planned_workouts.append(it)

    if blocked_today:
        red_reasons.append(f"Calendar [blocked] today ({blocked_today[0]})")
    elif blocked_soon:
        amber_reasons.append(f"Upcoming [blocked]: {blocked_soon[0]}")
    if deadlines_soon and not blocked_today:
        amber_reasons.append(f"High cognitive/work deadline: {deadlines_soon[0]}")

    if red_reasons or len(amber_reasons) >= 3:
        status = "RED"
    elif amber_reasons:
        status = "AMBER"
    else:
        status = "GREEN"

    # Concurrent Training Interference (AMPK <-> mTORC1 & eccentric lag)
    guardrails: list[str] = []
    by_day_planned: dict[dt.date, list[dict[str, Any]]] = {}
    for w in planned_workouts:
        by_day_planned.setdefault(w["start"], []).append(w)

    def _parse_hhmm(t_str: str) -> float | None:
        m = re.match(r"^(\d{1,2}):(\d{2})", str(t_str or "").strip())
        return (int(m.group(1)) + int(m.group(2)) / 60.0) if m else None

    def _is_key_hiit(what_s: str) -> bool:
        low_w = what_s.lower()
        return any(k in low_w for k in ("vo2", "threshold", "schwelle", "z4", "z5", "sweetspot", "sweet spot", "102%", "105%", "110%", "120%", "map"))

    for d_plan, w_list in sorted(by_day_planned.items()):
        sports_day = {(w.get("subkind") or "cycling").lower(): w for w in w_list}
        if "strength" in sports_day and ("cycling" in sports_day or "running" in sports_day):
            end_w = sports_day.get("cycling") or sports_day.get("running")
            t_str = _parse_hhmm(sports_day["strength"].get("time", ""))
            t_end = _parse_hhmm(end_w.get("time", "")) if end_w else None
            if t_str is not None and t_end is not None and abs(t_str - t_end) < 6.0:
                guardrails.append(
                    f"AMPK/mTORC1 interference on {d_plan.isoformat()}: separate endurance & strength by >=6h"
                )
            elif t_str is None or t_end is None:
                guardrails.append(
                    f"Concurrent day {d_plan.isoformat()}: keep >=6h gap between endurance & hypertrophy/strength"
                )
        # Check eccentric/CNS lag 24h before key cycling VO2max/Threshold
        for w in w_list:
            if (w.get("subkind") or "cycling").lower() == "cycling" and _is_key_hiit(w.get("what", "")):
                prev_d = d_plan - dt.timedelta(days=1)
                prev_planned = by_day_planned.get(prev_d, [])
                if any((pw.get("subkind") or "").lower() in ("strength", "running") for pw in prev_planned):
                    guardrails.append(
                        f"Protect {d_plan.isoformat()} key cycling session: avoid heavy lower-body strength / eccentric run <24–48h prior"
                    )

    # Also check yesterday/today completed strength/run before today/tomorrow key cycling
    for a in (activities or [])[:5]:
        if not isinstance(a, dict) or not a.get("date"):
            continue
        try:
            ad = dt.date.fromisoformat(str(a["date"])[:10])
        except ValueError:
            continue
        if (ref_today - ad).days in (0, 1) and str(a.get("sport", "")).lower() in ("strength", "running"):
            for check_d in (ad + dt.timedelta(days=1),):
                for w in by_day_planned.get(check_d, []):
                    if (w.get("subkind") or "cycling").lower() == "cycling" and _is_key_hiit(w.get("what", "")):
                        guardrails.append(
                            f"Recent {a.get('sport')} ({ad.isoformat()}) within 24h of {check_d.isoformat()} key cycling — verify leg freshness"
                        )

    # Bosquet et al. (2007) Exponential Taper Physics
    taper: dict[str, Any] | None = None
    if goal_a_Nearest is not None:
        d_rem = goal_a_Nearest["days"]
        if 0 <= d_rem <= 14:
            # Exponential decay from -41% to -60% volume reduction as race approaches, preserving intensity & frequency
            progress = 1.0 - (d_rem / 14.0)
            vol_red_pct = int(round(41.0 + 19.0 * (progress ** 1.3)))
            taper = {
                "active": True,
                "days_to_goal": d_rem,
                "goal": goal_a_Nearest["what"],
                "date": goal_a_Nearest["date"],
                "volume_reduction_pct": vol_red_pct,
                "target_tsb": "+10 to +25",
            }
            guardrails.append(
                f"Bosquet Taper ACTIVE ({d_rem}d -> [goal:A] {goal_a_Nearest['what']}): reduce volume ~{vol_red_pct}%, preserve intensity & frequency, target TSB +10..+25"
            )
        elif 15 <= d_rem <= 21:
            taper = {
                "active": False,
                "days_to_goal": d_rem,
                "taper_starts_in_days": d_rem - 14,
                "goal": goal_a_Nearest["what"],
                "date": goal_a_Nearest["date"],
            }
            guardrails.append(
                f"Taper starts in {d_rem - 14}d -> [goal:A] {goal_a_Nearest['what']} ({goal_a_Nearest['date']})"
            )

    reasons = red_reasons + amber_reasons
    return {
        "status": status,
        "reasons": reasons,
        "baseline_rhr": round(base_rhr, 1) if base_rhr is not None else None,
        "latest": recent_row,
        "guardrails": guardrails,
        "taper": taper,
    }


# ---------------------------------------------------------------------------
# 7. End-to-End Telemetry Builder & Context Formatters (`Simple` vs `Pro`)
# ---------------------------------------------------------------------------

def aggregate_epoch_curves(
    activities: list[dict[str, Any]],
    today: dt.date | None = None,
) -> dict[str, Any]:
    """Compute 3-epoch (`42d`, `90d`, `365d`) Fresh MMP, Legacy Fatigued MMP, Multi-Tier Durability, and Run Splits."""
    ref_today = today or dt.date.today()
    epochs = (("42d", 42), ("90d", 90), ("365d", 365))
    fresh_ep: dict[str, dict[str, int]] = {lbl: {} for lbl, _ in COACH_DURATIONS}
    fatig_ep: dict[str, dict[str, int]] = {lbl: {} for lbl, _ in COACH_DURATIONS}
    dur_ep: dict[str, dict[str, dict[str, int]]] = {
        tier_k: {lbl: {} for lbl in ("5s", "15s", "1m", "5m", "10m", "20m", "60m")}
        for tier_k, _ in DURABILITY_TIERS
    }
    run_ep: dict[str, dict[str, tuple[float, str]]] = {d: {} for d, _ in RUN_SPLIT_DISTANCES}

    for a in activities or []:
        if not isinstance(a, dict) or not a.get("date"):
            continue
        try:
            adate = dt.date.fromisoformat(str(a["date"])[:10])
        except ValueError:
            continue
        age = (ref_today - adate).days
        if age < 0 or age > 365:
            continue

        pf = a.get("peaks_fresh") or {}
        p_fat = a.get("peaks_fatigued") or {}
        p_dur = a.get("peaks_durability") or {}

        for dur_lbl, _sec in COACH_DURATIONS:
            val = pf.get(dur_lbl)
            if val:
                for ep_name, ep_days in epochs:
                    if age <= ep_days and int(val) > fresh_ep[dur_lbl].get(ep_name, 0):
                        fresh_ep[dur_lbl][ep_name] = int(val)
            fval = p_fat.get(dur_lbl)
            if fval:
                for ep_name, ep_days in epochs:
                    if age <= ep_days and int(fval) > fatig_ep[dur_lbl].get(ep_name, 0):
                        fatig_ep[dur_lbl][ep_name] = int(fval)

        for tier_k in dur_ep:
            t_peaks = p_dur.get(tier_k) or {}
            for dur_lbl in dur_ep[tier_k]:
                t_val = t_peaks.get(dur_lbl)
                if t_val:
                    for ep_name, ep_days in epochs:
                        if age <= ep_days and int(t_val) > dur_ep[tier_k][dur_lbl].get(ep_name, 0):
                            dur_ep[tier_k][dur_lbl][ep_name] = int(t_val)

        bp = a.get("best_paces") or {}
        for d_lbl, _dist in RUN_SPLIT_DISTANCES:
            pinfo = bp.get(d_lbl)
            if isinstance(pinfo, dict) and pinfo.get("sec_per_km"):
                spk = float(pinfo["sec_per_km"])
                for ep_name, ep_days in epochs:
                    cur_best = run_ep[d_lbl].get(ep_name)
                    if age <= ep_days and (cur_best is None or spk < cur_best[0]):
                        run_ep[d_lbl][ep_name] = (spk, pinfo.get("fmt") or format_pace(spk))

    return {
        "fresh": fresh_ep,
        "fatigued_1500kj": fatig_ep,
        "durability_tiers": dur_ep,
        "run_splits": run_ep,
    }


def build_coach_telemetry(
    profile_text: str,
    activities: list[dict[str, Any]],
    readiness_text: str = "",
    cal_items: list[dict[str, Any]] | None = None,
    strava_connected: bool = False,
    explicit_depth: str | None = None,
    query_text: str = "",
    fallback_ftp: int | None = None,
    fallback_weight: float | None = None,
    today: dt.date | None = None,
) -> dict[str, Any]:
    """Build unified Simple/Standard/Pro telemetry snapshot in < 2 ms (0 LLM tokens)."""
    ref_today = today or dt.date.today()
    prof = parse_athlete_profile(profile_text, fallback_ftp=fallback_ftp, fallback_weight=fallback_weight)
    readiness_rows = parse_readiness_log(readiness_text)
    depth = resolve_coach_depth(
        explicit_depth=explicit_depth,
        profile_info=prof,
        activities=activities,
        readiness_rows=readiness_rows,
        strava_connected=strava_connected,
        query_text=query_text,
    )

    curves = aggregate_epoch_curves(activities, today=ref_today)
    mmp_90d = {lbl: curves["fresh"][lbl]["90d"] for lbl, _ in COACH_DURATIONS if curves["fresh"][lbl].get("90d")}
    run_90d = {
        d_lbl: {"sec_per_km": curves["run_splits"][d_lbl]["90d"][0], "fmt": curves["run_splits"][d_lbl]["90d"][1]}
        for d_lbl, _ in RUN_SPLIT_DISTANCES
        if curves["run_splits"][d_lbl].get("90d")
    }

    cp_model = solve_cp_w_prime(mmp_90d)
    cs_model = solve_cs_d_prime(run_90d)

    eff_cp = (
        cp_model["cp_w"]
        if cp_model["valid"] and cp_model["cp_w"]
        else (prof.get("cp_w") or prof.get("ftp_w") or 275)
    )
    eff_wp_j = (
        cp_model["w_prime_j"]
        if cp_model["valid"] and cp_model["w_prime_j"]
        else ((prof["w_prime_kj"] * 1000.0) if prof.get("w_prime_kj") else None)
    )
    eff_cs = (
        cs_model["cs_mps"]
        if cs_model["valid"] and cs_model["cs_mps"]
        else prof.get("cs_mps")
    )

    typology = estimate_fiber_typology(mmp_90d, cp_watts=float(eff_cp))
    metabolic = compute_metabolic_profile(
        mmp_90d,
        cp_watts=float(eff_cp) if (cp_model["valid"] or prof.get("cp_w") or prof.get("ftp_w")) else None,
        w_prime_j=eff_wp_j,
        weight_kg=float(prof.get("weight_kg") or 70.0),
        age=prof.get("age"),
        sex=str(prof.get("sex") or "neutral"),
        hr_max=prof.get("hr_max"),
        rhr=prof.get("rhr"),
        best_paces=run_90d,
    )
    pmc = compute_pmc_metrics(activities, profile=prof, cs_mps=eff_cs, cp_watts=float(eff_cp), today=ref_today)
    readiness = evaluate_readiness_and_guardrails(
        readiness_rows, prof, pmc, activities, cal_items=cal_items, today=ref_today
    )

    # Summarize durability retention across 90d tiers on 5m MMP
    dur_summary: dict[str, str] = {}
    base_5m = mmp_90d.get("5m")
    if base_5m:
        for tier_k, tier_label in DURABILITY_TIERS:
            f5 = curves["durability_tiers"][tier_k]["5m"].get("90d")
            if f5:
                dur_summary[tier_label] = f"{f5}W ({(f5 / base_5m) * 100.0:.0f}%)"

    sorted_acts = sorted(
        [a for a in (activities or []) if isinstance(a, dict) and a.get("date")],
        key=lambda x: (str(x.get("date", "")), str(x.get("id", ""))),
        reverse=True,
    )
    last_act = sorted_acts[0] if sorted_acts else None

    return {
        "depth": depth,
        "profile": prof,
        "cp_model": cp_model,
        "cs_model": cs_model,
        "effective_cp_w": eff_cp,
        "effective_w_prime_kj": round(eff_wp_j / 1000.0, 1) if eff_wp_j else None,
        "effective_cs_mps": eff_cs,
        "typology": typology,
        "metabolic": metabolic,
        "pmc": pmc,
        "readiness": readiness,
        "durability_5m_90d": dur_summary,
        "last_activity": last_act,
        "curves": curves,
    }


def format_pro_telemetry_block(telemetry: dict[str, Any]) -> str:
    """Format compact 0-token `=== PHYSIOLOGICAL TELEMETRY & LOAD ===` block for `atlas.py coach context`."""
    pmc = telemetry["pmc"]
    rdy = telemetry["readiness"]
    cp_m = telemetry["cp_model"]
    cs_m = telemetry["cs_model"]
    typ = telemetry["typology"]
    met = telemetry["metabolic"]
    bw = float(telemetry["profile"].get("weight_kg") or 70.0)

    acwr_s = f"{pmc['acwr']:.2f}" if pmc.get("acwr") is not None else "n/a"
    lines = [
        "=== PHYSIOLOGICAL TELEMETRY & LOAD ===",
        (
            f"Status: CTL {pmc['ctl']:.1f} | ATL {pmc['atl']:.1f} | TSB {pmc['tsb']:+.1f} | "
            f"Ramp {pmc['ramp_7d']:+.1f}/wk | ACWR {acwr_s} | Monotony {pmc['monotony']:.2f} (Strain {pmc['strain']})"
        ),
    ]

    rdy_detail = ", ".join(rdy["reasons"]) if rdy["reasons"] else "all recovery & calendar markers clear"
    lines.append(f"Readiness: {rdy['status']} ({rdy_detail})")

    # Model line (CP/W', CS/D', Typology)
    model_parts: list[str] = []
    if cp_m["valid"] and cp_m["cp_w"]:
        wp_kg = (cp_m["w_prime_kj"] / bw) if cp_m["w_prime_kj"] and bw > 0 else 0.0
        model_parts.append(f"CP {cp_m['cp_w']} W | W' {cp_m['w_prime_kj']} kJ ({wp_kg:.2f} kJ/kg, R²={cp_m['r2']:.3f})")
    else:
        eff_cp = telemetry.get("effective_cp_w")
        model_parts.append(f"Anchor {eff_cp} W (CP/W' quality gate: {cp_m['quality_flag']})")
    if cs_m["valid"] and cs_m["cs_mps"]:
        model_parts.append(f"CS {cs_m['cs_pace_fmt']} ({cs_m['cs_mps']:.2f} m/s) | D' {cs_m['d_prime_m']} m")
    if typ.get("typology") and typ["typology"] != "Unknown":
        t_bits = [typ["typology"]]
        if typ.get("p5s_cp"):
            t_bits.append(f"P5s/CP {typ['p5s_cp']}")
        if typ.get("pdc_b") is not None:
            t_bits.append(f"b={typ['pdc_b']}")
        model_parts.append("Typology: " + " (".join([t_bits[0], ", ".join(t_bits[1:]) + ")"]) if len(t_bits) > 1 else f"Typology: {t_bits[0]}")
    lines.append("Model (90d): " + " | ".join(model_parts))

    # Metabolic & Durability line
    met_parts: list[str] = []
    if met.get("vo2max_storer"):
        est_tag = " est" if met.get("vo2max_map_estimated") else ""
        met_parts.append(f"VO2max ~{met['vo2max_storer']} ml/kg/min (Storer{est_tag})")
    if met.get("vo2max_uth"):
        met_parts.append(f"Uth HR ~{met['vo2max_uth']}")
    if met.get("running_vdot"):
        met_parts.append(f"VDOT {met['running_vdot']}")
    if met.get("glycolytic_band") and met["glycolytic_band"] != "Unknown":
        g_sub = []
        if met.get("cp_map_ratio"):
            g_sub.append(f"CP/MAP {met['cp_map_ratio']}")
        if met.get("p15s_p5m"):
            g_sub.append(f"P15s/P5m {met['p15s_p5m']}")
        met_parts.append(f"Glycolytic Index: {met['glycolytic_band']}" + (f" ({', '.join(g_sub)})" if g_sub else ""))
    if met.get("fatmax_watts"):
        fw = met["fatmax_watts"]
        met_parts.append(f"FatMax {fw[0]}–{fw[1]} W")
    if telemetry.get("durability_5m_90d"):
        d_str = ", ".join(f"{k}: {v}" for k, v in telemetry["durability_5m_90d"].items())
        met_parts.append(f"5m Durability [{d_str}]")
    if met_parts:
        lines.append("Metabolic & Durability: " + " | ".join(met_parts))

    # Last Activity telemetry
    la = telemetry.get("last_activity")
    if la:
        tss_v, tss_m = compute_activity_load(
            la, profile=telemetry["profile"],
            cs_mps=telemetry.get("effective_cs_mps"), cp_watts=float(telemetry.get("effective_cp_w") or 275),
        )
        la_bits = [f"{la.get('date')} '{la.get('name', 'Session')}'", f"{la.get('duration_min', 0)}m", f"{tss_v:.0f} TSS ({tss_m})"]
        if la.get("w_bal_min_pct") is not None:
            la_bits.append(f"W'bal min {la['w_bal_min_pct']}% ({la.get('matches_burned', 0)} matches)")
        if la.get("cho_burn_g") is not None:
            la_bits.append(f"CHO ~{la['cho_burn_g']}g (target {la.get('fueling_g_h', 0)}g/h)")
        if la.get("avg_torque_nm") is not None:
            la_bits.append(f"Torque {la['avg_torque_nm']} Nm (peak {la.get('peak_torque_nm', 0)} Nm, F_opt {la.get('f_opt_rpm', 0)} rpm)")
        if la.get("decoupling_pct") is not None:
            t_note = f" @ {la['temp_c']}C" + (" [HEAT DRIFT]" if la.get("thermal_drift") else "") if la.get("temp_c") is not None else ""
            la_bits.append(f"Steady Pw:Hr {la['decoupling_pct']:+.1f}%{t_note}")
        elif la.get("thermal_drift"):
            la_bits.append(f"Temp {la.get('temp_c')}C [HEAT DRIFT]")
        if la.get("hypoxia_loss_pct"):
            la_bits.append(f"Alt {la.get('elev_avg_m')}m (-{la['hypoxia_loss_pct']}% Peronnet hypoxia)")
        lines.append("Last Session: " + " | ".join(la_bits))

    all_guards = list(pmc.get("warnings") or []) + list(rdy.get("guardrails") or [])
    if all_guards:
        lines.append("Guardrails: " + "; ".join(all_guards))
    return "\n".join(lines)


def format_simple_context_block(
    mode: str,
    profile_info: dict[str, Any],
    activities: list[dict[str, Any]],
    cal_items: list[dict[str, Any]],
    today: dt.date | None = None,
) -> str:
    """Calm, 100% jargon-free context block for `depth: simple` (casual fitness / habits)."""
    ref_today = today or dt.date.today()
    cutoff_7d = (ref_today - dt.timedelta(days=7)).isoformat()
    cutoff_28d = (ref_today - dt.timedelta(days=28)).isoformat()

    acts_7d = [a for a in (activities or []) if isinstance(a, dict) and str(a.get("date", ""))[:10] >= cutoff_7d]
    acts_28d = [a for a in (activities or []) if isinstance(a, dict) and str(a.get("date", ""))[:10] >= cutoff_28d]

    items = cal_items or []
    done_cal_7d = [
        i for i in items
        if i.get("kind") == "workout" and i.get("status") == "done" and -7 <= i.get("delta", -99) <= 0
    ]
    sessions_7d = max(len(acts_7d), len(done_cal_7d))
    mins_7d = sum(int(a.get("duration_min") or 0) for a in acts_7d)

    lines = [
        f"=== COACH CONTEXT (SIMPLE · mode: {mode}) ===",
        "Style Directive: SIMPLE MODE active. Keep guidance warm, clear, and completely free of sports-science jargon.",
        "  Do NOT mention or ask for power meters, watt zones, threshold numbers, morning pulse, or trainer files.",
        "  Describe sessions simply by duration and natural feel (easy conversational pace, moderate, or brisk).",
        "",
        "1. Routine & Weekly Preferences:",
        f"  - Focus: {profile_info.get('primary_sport') or 'General Fitness & Movement'}",
        f"  - Weekly Target: {profile_info.get('weekly_budget') or '2–3 sessions / week'}",
        f"  - Preferred Window: {profile_info.get('schedule') or 'Flexible'}",
    ]
    for rule in profile_info.get("bio_rules") or []:
        # Strip any technical acronyms if present in template defaults
        if not any(tok in rule.lower() for tok in ("maltodextrin", "vo2max", "threshold", "ftp", "cp", "w'", "ctl", "tsb", "rhr", ".zwo")):
            lines.append(f"  - Note: {rule}")

    lines += [
        "",
        "2. Recent Consistency:",
        f"  - Last 7 days: {sessions_7d} session(s)" + (f" ({mins_7d} min total)" if mins_7d else ""),
        f"  - Last 4 weeks: {len(acts_28d)} logged session(s)",
    ]
    for a in sorted(acts_28d, key=lambda x: str(x.get("date", "")), reverse=True)[:5]:
        fb = a.get("feedback") or ""
        fb_note = f" — feel: {fb}" if fb and fb != "pending" else (" — ask briefly how it felt" if fb == "pending" else "")
        lines.append(f"    * {a.get('date')} · {a.get('sport', 'session')} · {a.get('name', 'Workout')} ({a.get('duration_min', 0)} min){fb_note}")

    goals = [i for i in items if i.get("kind") == "goal" and i.get("delta", -1) >= 0 and i.get("status") == "open"]
    blocked = [
        i for i in items
        if 0 <= i.get("delta", -1) <= 21 and i.get("status") == "open"
        and (i.get("kind") == "blocked" or (i.get("kind") == "event" and i.get("time")))
    ]
    workouts = [i for i in items if -7 <= i.get("delta", -99) <= 14 and i.get("kind") == "workout"]

    lines.append("")
    lines.append("3. Calendar Schedule & Blocked Days (strictly respect unavailable days):")
    if goals:
        for g in goals[:5]:
            lines.append(f"  - Goal: {g['start'].isoformat()} (in {g['delta']}d) — {g['what']}")
    if blocked:
        for b in blocked[:10]:
            span = b["start"].isoformat() if b["start"] == b["end"] else f"{b['start'].isoformat()}..{b['end'].isoformat()}"
            t_s = f" {b['time']}" if b.get("time") else ""
            lines.append(f"  - Unavailable/Busy: {span}{t_s} [{b['kind']}] {b['what']}")
    else:
        lines.append("  - Unavailable/Busy: (none in next 21 days)")

    box_map = {"open": "[ ]", "done": "[x]", "skipped": "[-]"}
    if workouts:
        lines.append("  - Planned / Recent Sessions (-7d .. +14d):")
        for w in workouts:
            t_s = f" {w['time']}" if w.get("time") else ""
            lines.append(f"    {box_map.get(w['status'], '[ ]')} {w['start'].isoformat()}{t_s} [workout:{w.get('subkind') or 'running'}] {w['what']}")
    else:
        lines.append("  - Planned Sessions: (none scheduled yet)")

    return "\n".join(lines)
