"""Deterministic 7-Pillar Biophysical & Autonomy Engine for Atlas Coach v2.

Implements the complete Stage-1 mathematical pre-processing pipeline (Python 3 stdlib only,
0 LLM tokens):
  0. 1Hz Spike Filter (Mateo-March 2025), Body Composition (`FFM`, `Vol_rel,ind`), & Missing-Input Gate
  1. Pillar 1: `W'`-cleaned Oxidative Roof (`VO2max_rel_clean`, `VO2max_FFM`)
  2. Pillar 2: FFM-normalized Glycolytic Drive (`vLamax_sprint`), Mader-Heck-Hauser MLSS solver,
     Lactate-Shuttle Efficiency (`eta_shuttle`), Stoichiometric `CHO/Fat` curves, & Dunst Cadence
  3. Pillar 3: True `CP/W'` (with `SEE` quality gate), `MPR`, Puchowicz OmniPD `A`, & Chorley
     Dynamic Bi-Exponential `W'bal` (`tau_FC`, `tau_SC`)
  4. Pillar 4: Sprint Torque-Velocity (`T_max`, `C_max`, `C_opt` with ERG filter) & Functional
     Myotype Z-Score (`Z_Myo`)
  5. Pillar 5: Intensity-Weighted Durability (`w_sub` <= CP vs `w_sup` > CP), Cadence Fade,
     Domain Work Split (`W_<LT1`, `W_LT1-CP`, `W_>CP`), & Dual-TID
  6. Pillar 6: Morphometric `CdA` (with measured Brain override), `Compound Score`, Learned Indoor
     Offset, Townsend 2-Segment Altitude, Heat Acclimatization, Sex/RED-S Guard, & Minetti/Morin Run Bridge
  7. Pillar 7 & Stage 2: Karvonen `%HRR`, `ln RMSSD_7d` vs `SWC`, LSCT Detector, Sanders Subjective-Objective
     Ratios (`Q_Strain`, `Q_RPE:PO`), & 4-Stage Readiness (`GRÜN` | `GELB-A` | `GELB-B` | `ROT`)
  8. Plan-vs-Actual Interval Execution, Individual Dose-Response, Autonomy Audit & Socratic Question Radar
"""
from __future__ import annotations

import datetime as dt
import json
import math
import re
import statistics
from typing import Any


# ---------------------------------------------------------------------------
# Discovery Dimensions for `brain/coach/playbook.md` (Socratic Question Radar)
# ---------------------------------------------------------------------------
DISCOVERY_DIMENSIONS: tuple[tuple[str, str, str], ...] = (
    ("why_and_motivation", "Sportliche Motivation & innerer Antrieb",
     "Was treibt dich im Kern beim Training an (Wettkampf-Platzierung, Gruppen-Dominanz, Kopf frei kriegen, Gesundheit) – und wann verlierst du typischerweise die Lust?"),
    ("coaching_tone", "Gewünschter Coaching-Stil & Strenge",
     "Wie soll ich als dein Coach mit dir umgehen, wenn du Einheiten schwänzt oder Z2 überziehst – direkt & schonungslos (World-Tour-DS) oder ruhig-analytisch?"),
    ("skip_triggers", "Typische Alltags- & Job-Saboteure",
     "Was ist im Alltag der häufigste Grund, wenn ein geplantes Training ausfällt (Meetings am Nachmittag, mentale Müdigkeit, Wetter, späte Abende)?"),
    ("sleep_architecture", "Schlaf-Realität & Chronotyp",
     "Wie sieht dein echter Schlafrhythmus unter der Woche vs. am Wochenende aus, und was sind deine größten Schlafkiller?"),
    ("gut_and_fueling", "Magenverträglichkeit & Verpflegungs-Gewohnheit",
     "Wie viel Gramm Kohlenhydrate pro Stunde verträgt dein Magen aktuell problemlos (Gels, Malto/Fruktose im Bidon, Riegel), und isst du vor Früh-/Abendeinheiten?"),
    ("indoor_setup", "Indoor-Setup, Trainer-Modus & Kühlung",
     "Welchen Smart-Trainer und welche Kühlung (Ventilator/Raumtemperatur) nutzt du indoor, und fährst du Intervalle lieber im ERG- oder Slope/Level-Modus?"),
    ("power_sensors", "Powermeter-Quellen (Indoor vs. Outdoor)",
     "Misst du draußen und drinnen mit demselben Powermeter (z. B. Pedale/Kurbel) oder drinnen über den Smart-Trainer (wichtig für den Antriebsstrang-Offset)?"),
    ("injury_orthopedics", "Orthopädische Historie & Schmerzpunkte",
     "Gibt es Knie-, Rücken-, Sitz-, Achillessehnen- oder muskuläre Baustellen, die bei hohem Volumen, niedrigen Kadenzen oder beim Laufen zwicken?"),
    ("pacing_weakness", "Typisches Pacing- & Renn-Verhalten",
     "Wo verlierst du in harten Fahrten oder Rennen am ehesten den Anschluss – beim ersten Antritt, nach 2+ Stunden Vorarbeit oder durch zu schnelles Angehen?"),
    ("strength_equipment", "Krafttrainings-Zugang & Erfahrung",
     "Welches Equipment hast du für Krafttraining griffbereit (Langhantel/Gym für >=80% 1RM Beinkraft oder Kurzhanteln/Bodyweight zu Hause)?"),
)


# ---------------------------------------------------------------------------
# 0. 1Hz Spike Filter (Mateo-March 2025) & Body Composition (`FFM`, `Vol_rel`)
# ---------------------------------------------------------------------------

def filter_power_spikes(watts: list[float]) -> tuple[list[float], dict[str, Any]]:
    """Clean single-second non-progressive powermeter glitches (Mateo-March et al. 2025).

    Preserves legitimate sharp interval step changes (where `[i+1]` sustains the step) while
    clamping isolated 1-2s telemetry spikes (`>2500 W`, `<0 W`, or isolated `>35%` + `>180 W`
    jumps above both neighbors). Sessions with `>50` artifacts are locked out of record curves.
    """
    n = len(watts or [])
    if n == 0:
        return [], {"spike_count": 0, "locked_for_records": False}
    cleaned = [float(x or 0.0) for x in watts]
    spikes = 0
    for i in range(n):
        w = cleaned[i]
        prev_w = cleaned[i - 1] if i > 0 else (cleaned[1] if n > 1 else 0.0)
        next_w = cleaned[i + 1] if i + 1 < n else prev_w
        if w < 0.0 or w > 2500.0:
            spikes += 1
            cleaned[i] = max(0.0, min(2000.0, 0.5 * (max(0.0, prev_w) + max(0.0, next_w))))
            continue
        nbr_max = max(prev_w, next_w)
        # Isolated 1s spike: jumps >35% AND >180W above BOTH immediate neighbors
        if i > 0 and i + 1 < n and w > max(400.0, nbr_max * 1.35) and (w - nbr_max) > 180.0:
            # Check 2s spike case as well: only treat as artifact if i+2 also drops back
            next2_w = cleaned[i + 2] if i + 2 < n else next_w
            if next_w <= prev_w * 1.35 or next2_w <= prev_w * 1.35:
                spikes += 1
                cleaned[i] = round(0.5 * (prev_w + min(next_w, next2_w)), 1)
    return cleaned, {"spike_count": spikes, "locked_for_records": spikes > 50}


def compute_body_composition(
    profile: dict[str, Any],
    readiness_rows: list[dict[str, Any]] | None = None,
    today: dt.date | None = None,
) -> dict[str, Any]:
    """Compute 7d-smoothed mass `M`, `FFM`, active muscle mass `M_akt`, and `Vol_rel,ind`."""
    ref_today = today or dt.date.today()
    cutoff_7d = (ref_today - dt.timedelta(days=7)).isoformat()
    recent_weights = [
        float(r["weight_kg"])
        for r in (readiness_rows or [])
        if r.get("weight_kg") is not None and 30.0 <= float(r["weight_kg"]) <= 200.0 and str(r.get("date", "")) >= cutoff_7d
    ]
    explicit = set(profile.get("explicit_fields") or ())
    has_explicit_mass = bool(recent_weights) or ("weight_kg" in explicit and not profile.get("is_skeleton"))
    raw_mass = (
        round(sum(recent_weights) / len(recent_weights), 2)
        if recent_weights
        else (float(profile["weight_kg"]) if profile.get("weight_kg") else None)
    )

    kfa = profile.get("kfa_pct")
    has_kfa = kfa is not None and 3.0 <= float(kfa) <= 48.0 and ("kfa_pct" in explicit and not profile.get("is_skeleton"))

    if raw_mass and raw_mass > 25.0 and has_kfa:
        kfa_f = float(kfa)
        ffm = round(raw_mass * (1.0 - kfa_f / 100.0), 2)
        m_akt = round(0.30 * ffm, 2)
        v_la = round(0.5037 * ffm, 2)
        vol_rel = round(0.5037 * (1.0 - kfa_f / 100.0), 4)
        return {
            "mass_kg_7d": raw_mass,
            "mass_calibrated": has_explicit_mass,
            "kfa_pct": round(kfa_f, 1),
            "kfa_calibrated": True,
            "ffm_kg": ffm,
            "m_akt_kg": m_akt,
            "v_la_l": v_la,
            "vol_rel_ind": vol_rel,
        }

    # Provisional estimate when KFA is missing (flagged clearly so Missing-Data Gate asks!)
    if raw_mass and raw_mass > 25.0:
        sex = str(profile.get("sex") or "neutral")
        prov_kfa = 20.0 if sex == "female" else 14.0
        ffm_prov = round(raw_mass * (1.0 - prov_kfa / 100.0), 2)
        return {
            "mass_kg_7d": raw_mass,
            "mass_calibrated": has_explicit_mass,
            "kfa_pct": float(kfa) if kfa is not None else None,
            "kfa_calibrated": False,
            "ffm_kg": ffm_prov,
            "m_akt_kg": round(0.30 * ffm_prov, 2),
            "v_la_l": round(0.5037 * ffm_prov, 2),
            "vol_rel_ind": round(0.5037 * (1.0 - prov_kfa / 100.0), 4),
        }

    return {
        "mass_kg_7d": None,
        "mass_calibrated": False,
        "kfa_pct": None,
        "kfa_calibrated": False,
        "ffm_kg": None,
        "m_akt_kg": None,
        "v_la_l": None,
        "vol_rel_ind": 0.43,
    }


# ---------------------------------------------------------------------------
# 1. Pillar 1: `W'`-Cleaned Oxidative Roof (`VO2max_rel_clean`, `VO2max_FFM`)
# ---------------------------------------------------------------------------

def compute_pillar1_oxidative(
    p_5min_w: float | None,
    w_prime_j: float | None,
    mass_kg: float | None,
    ffm_kg: float | None,
    kfa_calibrated: bool = False,
) -> dict[str, Any]:
    """Remove anaerobic `W'` contribution (`70%` over 300s) from `P_5min` (`CE_sev = 12.8`)."""
    if not p_5min_w or p_5min_w <= 0 or not mass_kg or mass_kg <= 25.0:
        return {
            "p_aero_5min_w": None,
            "vo2max_rel_clean": None,
            "vo2max_ffm": None,
            "vo2max_sitko_raw": None,
            "w_prime_deduction_w": None,
        }
    wp_j = float(w_prime_j) if (w_prime_j and w_prime_j > 0) else 0.0
    deduct_w = 0.70 * (wp_j / 300.0)
    p_aero = max(0.55 * float(p_5min_w), float(p_5min_w) - deduct_w)
    vo2_clean = (p_aero * 12.8) / float(mass_kg) + 3.5
    vo2_sitko = 16.6 + 8.87 * (float(p_5min_w) / float(mass_kg))
    vo2_ffm = ((vo2_clean * float(mass_kg)) / float(ffm_kg)) if (ffm_kg and ffm_kg > 20.0) else None
    return {
        "p_aero_5min_w": round(p_aero, 1),
        "vo2max_rel_clean": round(vo2_clean, 1),
        "vo2max_ffm": round(vo2_ffm, 1) if vo2_ffm else None,
        "vo2max_ffm_provisional": not kfa_calibrated,
        "vo2max_sitko_raw": round(vo2_sitko, 1),
        "w_prime_deduction_w": round(deduct_w, 1),
    }


# ---------------------------------------------------------------------------
# 2. Pillar 2: Mader-Heck-Hauser Glycolytic Drive, Shuttle `eta` & Fueling
# ---------------------------------------------------------------------------

def detect_sprint_t_alac(watts: list[float]) -> float:
    """Detect individual alactic phase `t_alac` (s) until first systematic `-3.5%` drop from `P_peak`."""
    if not watts or len(watts) < 8:
        return 3.0
    # Locate best 15s window
    best_i = 0
    best_sum = -1.0
    cur = sum(watts[: min(15, len(watts))])
    best_sum = cur
    for i in range(1, max(1, len(watts) - 14)):
        cur += watts[i + 14] - watts[i - 1]
        if cur > best_sum:
            best_sum = cur
            best_i = i
    seg = watts[best_i: best_i + 15]
    if not seg or max(seg) < 350.0:
        return 3.0
    p_pk = max(seg[:8])
    pk_idx = seg.index(p_pk)
    thresh = 0.965 * p_pk
    for j in range(pk_idx + 1, len(seg) - 1):
        if seg[j] <= thresh and seg[j + 1] <= thresh:
            return round(max(2.0, min(5.5, float(j))), 2)
    return 3.0


def _mader_vla_ss(p_w: float, mass_kg: float, vo2max_rel: float, vlamax: float) -> tuple[float, float]:
    """Return `(vo2_ss, vla_ss_mmol_l_min)` from Mader-Heck-Hauser equations."""
    vo2_ss = (p_w * 11.7) / mass_kg + 3.5
    if vo2_ss >= vo2max_rel - 0.05:
        return vo2_ss, 60.0 * vlamax
    ratio = (vo2max_rel - vo2_ss) / (0.0631 * max(1.0, vo2_ss))
    denom = 1.0 + 1.331 * (max(0.0, ratio) ** 1.5)
    return vo2_ss, (60.0 * vlamax) / denom


def _solve_mader_mlss_watts(
    mass_kg: float,
    vo2max_rel: float,
    vlamax: float,
    vol_rel_ind: float,
) -> float | None:
    """Solve `vLa_ss(P) == vLa_ox,max(P)` via bisection for `P_MLSS,Mader` (Watts)."""
    if mass_kg <= 25.0 or vo2max_rel <= 15.0 or vlamax <= 0.05 or vol_rel_ind <= 0.15:
        return None
    p_max_vo2 = ((vo2max_rel - 3.5) * mass_kg) / 11.7
    lo = 30.0
    hi = max(60.0, p_max_vo2 * 0.995)

    def _net_lac(pw: float) -> float:
        vo2_ss, vla_ss = _mader_vla_ss(pw, mass_kg, vo2max_rel, vlamax)
        vla_ox = (0.02049 / vol_rel_ind) * vo2_ss
        return vla_ss - vla_ox

    if _net_lac(lo) >= 0:
        return round(lo, 1)
    if _net_lac(hi) <= 0:
        return round(hi, 1)
    for _ in range(48):
        mid = 0.5 * (lo + hi)
        if _net_lac(mid) <= 0:
            lo = mid
        else:
            hi = mid
    return round(0.5 * (lo + hi), 1)


def compute_pillar2_metabolic_mader(
    curves: dict[str, Any],
    cp_watts: float | None,
    vo2max_rel_clean: float | None,
    body: dict[str, Any],
    t_alac_s: float = 3.0,
    creatine_flag: bool | None = None,
) -> dict[str, Any]:
    """Meixner `vLamax_sprint`, Mader MLSS, Shuttle Index `eta_shuttle`, and `CHO/Fat` stoichiometry."""
    fresh = (curves or {}).get("fresh") or {}
    p15_42 = float((fresh.get("15s") or {}).get("42d") or 0.0)
    p15_90 = float((fresh.get("15s") or {}).get("90d") or 0.0)
    p15_365 = float((fresh.get("15s") or {}).get("365d") or 0.0)

    # Refinement #1: Anchor Decay Guard — prevent Z2-only blocks from faking a low vLamax
    anchor_status = "missing"
    p15_eff = 0.0
    if p15_42 > 0 and (p15_90 <= 0 or p15_42 >= 0.85 * p15_90):
        p15_eff = p15_42
        anchor_status = "fresh_42d"
    elif p15_90 > 0 and (p15_365 <= 0 or p15_90 >= 0.85 * p15_365):
        p15_eff = p15_90
        anchor_status = "anchored_90d"
    elif p15_365 > 0:
        p15_eff = p15_365 * 0.97
        anchor_status = "anchored_365d"

    if cp_watts and p15_eff > 0 and (p15_eff / float(cp_watts)) < 1.95:
        anchor_status = "passive_lower_bound"

    mass = body.get("mass_kg_7d")
    ffm = body.get("ffm_kg")
    vol_rel = float(body.get("vol_rel_ind") or 0.43)
    v_la_l = float(body.get("v_la_l") or (0.43 * float(mass or 70.0)))

    if not mass or not ffm or p15_eff <= 0:
        return {
            "vlamax_sprint_proxy": None,
            "vlamax_confidence": "missing_sprint_or_ffm",
            "sprint_anchor_status": anchor_status,
            "p_mlss_mader_w": None,
            "shuttle_index_eta": None,
            "shuttle_class": None,
            "rcp_langley_w_kg": None,
            "p_fatmax_w": None,
            "mfo_g_min": None,
            "p_carbmax_90_w": None,
            "optimal_cadence_rpm": {"lt1": 66, "fatmax": 76, "mlss": 82, "vo2max": 85, "sprint": 135},
        }

    eff_t_alac = max(2.0, min(5.5, float(t_alac_s or 3.0)))
    delta_la = max(1.2, (15.0 * p15_eff + 551.0 - 123.0 * float(ffm)) / (11.0 * float(ffm)))
    vlamax_sprint = round(max(0.20, min(1.35, delta_la / (15.0 - eff_t_alac))), 2)

    conf = "high" if (body.get("kfa_calibrated") and anchor_status in ("fresh_42d", "anchored_90d")) else "low_provisional"
    if anchor_status == "passive_lower_bound":
        conf = "low_passive_submax"

    if not vo2max_rel_clean or vo2max_rel_clean <= 20.0:
        return {
            "vlamax_sprint_proxy": vlamax_sprint,
            "vlamax_confidence": conf,
            "sprint_anchor_status": anchor_status,
            "creatine_noted": creatine_flag is not None,
            "p_mlss_mader_w": None,
            "shuttle_index_eta": None,
            "shuttle_class": None,
            "rcp_langley_w_kg": None,
            "p_fatmax_w": None,
            "mfo_g_min": None,
            "p_carbmax_90_w": None,
            "optimal_cadence_rpm": {"lt1": 66, "fatmax": 76, "mlss": 82, "vo2max": 85, "sprint": 135},
        }

    p_mlss_mader = _solve_mader_mlss_watts(float(mass), float(vo2max_rel_clean), vlamax_sprint, vol_rel)
    rcp_w_kg = round(0.802 - 1.851 * vlamax_sprint + 0.070 * float(vo2max_rel_clean), 2)

    eta = None
    shuttle_cls = None
    eff_submax_vla = vlamax_sprint
    if cp_watts and cp_watts > 50 and p_mlss_mader and p_mlss_mader > 30:
        target_mlss = 0.95 * float(cp_watts)
        eta = round(target_mlss / p_mlss_mader, 2)
        if eta > 1.15:
            shuttle_cls = "High MCT1/Type-IIa Clearance (Allrounder)"
        elif eta <= 1.00:
            shuttle_cls = "Low Lactate Clearance (Aerobic Base Priority)"
        else:
            shuttle_cls = "Balanced Lactate Shuttle"

        # Calibrate effective submaximal vLamax so Mader MLSS matches empirical 0.95 * CP
        v_lo, v_hi = 0.15, 1.40
        for _ in range(36):
            v_mid = 0.5 * (v_lo + v_hi)
            pm = _solve_mader_mlss_watts(float(mass), float(vo2max_rel_clean), v_mid, vol_rel) or target_mlss
            if pm > target_mlss:
                v_lo = v_mid  # higher vLamax lowers MLSS
            else:
                v_hi = v_mid
        eff_submax_vla = 0.5 * (v_lo + v_hi)

    # Sweep power curve for exact stoichiometric FatMax, MFO (g/min), and CarbMax@90g/h
    best_fat_g_min = 0.0
    best_fat_w = None
    p_carb_90 = None
    upper_sweep = int(round((cp_watts or p_mlss_mader or 280.0) * 1.08))
    for pw in range(50, max(80, upper_sweep) + 1, 2):
        vo2_ss, vla_eff = _mader_vla_ss(float(pw), float(mass), float(vo2max_rel_clean), eff_submax_vla)
        c_cho_gh = vla_eff * v_la_l * 0.09008 * 60.0
        f_ox_gmin = 0.50 * (float(mass) / 1000.0) * max(0.0, (vo2_ss - 3.5) - (vla_eff * vol_rel) / 0.02049)
        if f_ox_gmin > best_fat_g_min:
            best_fat_g_min = f_ox_gmin
            best_fat_w = pw
        if p_carb_90 is None and c_cho_gh >= 90.0:
            p_carb_90 = pw

    return {
        "vlamax_sprint_proxy": vlamax_sprint,
        "vlamax_confidence": conf,
        "sprint_anchor_status": anchor_status,
        "t_alac_s": eff_t_alac,
        "creatine_noted": creatine_flag is not None,
        "p_mlss_mader_w": int(round(p_mlss_mader)) if p_mlss_mader else None,
        "shuttle_index_eta": eta,
        "shuttle_class": shuttle_cls,
        "rcp_langley_w_kg": rcp_w_kg,
        "p_fatmax_w": best_fat_w,
        "mfo_g_min": round(best_fat_g_min, 2) if best_fat_w else None,
        "p_carbmax_90_w": p_carb_90,
        "optimal_cadence_rpm": {"lt1": 66, "fatmax": 76, "mlss": 82, "vo2max": 85, "sprint": 135},
    }


# ---------------------------------------------------------------------------
# 3. Pillar 3: `CP/W'` SEE, `MPR`, OmniPD `A` & Chorley Bi-Exponential `W'bal`
# ---------------------------------------------------------------------------

def compute_cp_see_and_omnipd(
    mmp_dict: dict[str, int | float],
    cp_model: dict[str, Any],
) -> dict[str, Any]:
    """Compute OLS Standard Error (`SEE_CP %`, `SEE_W' %`), `MPR`, `APR`, and OmniPD decay `A`."""
    cp_w = cp_model.get("cp_w")
    wp_j = cp_model.get("w_prime_j")
    if not cp_w or not wp_j or cp_w <= 0 or wp_j <= 0:
        return {"see_cp_pct": None, "see_wp_pct": None, "mpr_w": None, "apr_w": None, "omni_decay_a_w": None}

    dur_map = {"3m": 180, "5m": 300, "8m": 480, "10m": 600, "12m": 720, "20m": 1200}
    pts = [(float(sec), float(mmp_dict[k]) * sec) for k, sec in dur_map.items() if mmp_dict.get(k)]
    see_cp_pct = None
    see_wp_pct = None
    if len(pts) >= 3:
        n = len(pts)
        mx = sum(p[0] for p in pts) / n
        sxx = sum((p[0] - mx) ** 2 for p in pts)
        sse = sum((p[1] - (cp_w * p[0] + wp_j)) ** 2 for p in pts)
        s2 = sse / max(1, n - 2)
        if sxx > 0:
            se_slope = math.sqrt(s2 / sxx)
            se_inter = math.sqrt(s2 * (1.0 / n + (mx ** 2) / sxx))
            see_cp_pct = round((se_slope / cp_w) * 100.0, 2)
            see_wp_pct = round((se_inter / wp_j) * 100.0, 2)

    p5s = float(mmp_dict.get("5s") or 0.0)
    p5m = float(mmp_dict.get("5m") or 0.0)
    mpr = int(round(p5s - cp_w)) if p5s > cp_w else None
    apr = int(round(p5s - p5m)) if (p5s > 0 and p5m > 0 and p5s > p5m) else None

    # OmniPD decay term A (W) for t > TCP_max (1800s), evaluated on 60m (3600s) if available
    omni_a = None
    p60 = float(mmp_dict.get("60m") or 0.0)
    if 0 < p60 < cp_w + (wp_j / 3600.0):
        pred_3600 = float(cp_w) + (float(wp_j) / 3600.0)
        ln_term = math.log(3600.0 / 1800.0)
        if ln_term > 0:
            omni_a = round(max(0.0, (pred_3600 - p60) / ln_term), 1)

    return {
        "see_cp_pct": see_cp_pct,
        "see_wp_pct": see_wp_pct,
        "mpr_w": mpr,
        "apr_w": apr,
        "omni_decay_a_w": omni_a,
    }


def compute_chorley_w_bal(
    watts: list[float],
    cp_watts: float,
    w_prime_j: float = 20000.0,
    fc_fraction: float = 0.45,
    dt_s: float = 1.0,
) -> dict[str, Any]:
    """Dynamic Bi-Exponential `W'bal` ODE (Chorley et al. 2023) with fast PCr (`tau_FC`) & slow `H+` (`tau_SC`)."""
    if not watts or cp_watts <= 0 or w_prime_j <= 0:
        return {
            "w_bal_min_pct": None,
            "w_bal_min_j": None,
            "matches_burned": 0,
            "kj_above_cp": 0.0,
            "tau_fc_50pct_s": None,
            "tau_sc_50pct_s": None,
        }

    fc_amp = max(0.20, min(0.80, fc_fraction)) * float(w_prime_j)
    sc_amp = float(w_prime_j) - fc_amp
    fc_bal = fc_amp
    sc_bal = sc_amp
    min_tot = float(w_prime_j)
    total_above_j = 0.0
    total_sub_j = 0.0

    matches = 0
    bout_above_j = 0.0
    sub_streak = 0.0
    bout_counted = False
    deep_armed = True

    for raw_p in watts:
        p = max(0.0, float(raw_p or 0.0))
        if p > cp_watts:
            total_sub_j += cp_watts * dt_s
            burn = (p - cp_watts) * dt_s
            total_above_j += burn
            bout_above_j += burn
            sub_streak = 0.0
            # Fast phosphocreatine pool depletes first, remainder from slow glycolytic pool
            take_fc = min(fc_bal, burn * 0.65)
            rem = burn - take_fc
            take_sc = min(sc_bal, rem)
            leftover = rem - take_sc
            if leftover > 0:
                take_fc = min(fc_bal, take_fc + leftover)
            fc_bal = max(0.0, fc_bal - take_fc)
            sc_bal = max(0.0, sc_bal - take_sc)
            w_tot = fc_bal + sc_bal
            if w_tot < min_tot:
                min_tot = w_tot
            if not bout_counted and (bout_above_j >= 2000.0 or (deep_armed and w_tot < 0.50 * w_prime_j)):
                matches += 1
                bout_counted = True
                if w_tot < 0.50 * w_prime_j:
                    deep_armed = False
        else:
            total_sub_j += p * dt_s
            pct_d_cp = max(0.0, min(100.0, ((cp_watts - p) / cp_watts) * 100.0))
            tau_fc = 45.0 * math.exp(-0.014 * pct_d_cp) + 9999.0 * math.exp(-0.811 * pct_d_cp)
            tau_sc = 9999.0 * math.exp(-0.098 * pct_d_cp) + 429.0
            fc_bal = fc_amp - (fc_amp - fc_bal) * math.exp(-dt_s / max(15.0, tau_fc))
            sc_bal = sc_amp - (sc_amp - sc_bal) * math.exp(-dt_s / max(60.0, tau_sc))
            sub_streak += dt_s
            if sub_streak >= 10.0:
                bout_above_j = 0.0
                bout_counted = False
            if (fc_bal + sc_bal) >= 0.65 * w_prime_j:
                deep_armed = True

    tau_fc_50 = int(round(45.0 * math.exp(-0.014 * 50.0) + 9999.0 * math.exp(-0.811 * 50.0)))
    tau_sc_50 = int(round(9999.0 * math.exp(-0.098 * 50.0) + 429.0))
    return {
        "w_bal_min_pct": round((min_tot / w_prime_j) * 100.0, 1),
        "w_bal_min_j": int(round(min_tot)),
        "matches_burned": matches,
        "kj_above_cp": round(total_above_j / 1000.0, 1),
        "kj_sub_cp": round(total_sub_j / 1000.0, 1),
        "tau_fc_50pct_s": tau_fc_50,
        "tau_sc_50pct_s": tau_sc_50,
    }


# ---------------------------------------------------------------------------
# 4. Pillar 4: Sprint Torque-Velocity (with ERG Filter) & Myotype Z-Score
# ---------------------------------------------------------------------------

def extract_sprint_torque_velocity(watts: list[float], cadence: list[float]) -> dict[str, Any]:
    """Fit `T(C) = T_max + s_TC * C` (`s_TC < 0`) on non-ERG acceleration bursts."""
    n = min(len(watts or []), len(cadence or []))
    if n < 15:
        return {}

    # Refinement #2: Filter out ERG-mode constant-power blocks (low power CV despite cadence variance)
    burst_c: list[float] = []
    burst_t: list[float] = []
    p_max_stream = max((float(x or 0.0) for x in watts[:n]), default=0.0)
    if p_max_stream < 350.0:
        return {}

    min_burst_w = max(320.0, 0.55 * p_max_stream)
    for i in range(n):
        w = float(watts[i] or 0.0)
        c = float(cadence[i] or 0.0)
        if w < min_burst_w or c < 45.0 or c > 195.0:
            continue
        # Check 6s local window for ERG clamping (constant watts within +-2% while cadence shifts)
        lo = max(0, i - 3)
        hi = min(n, i + 3)
        win_w = [float(watts[k] or 0.0) for k in range(lo, hi)]
        win_c = [float(cadence[k] or 0.0) for k in range(lo, hi)]
        if len(win_w) >= 5:
            mw = sum(win_w) / len(win_w)
            sd_w = statistics.pstdev(win_w)
            sd_c = statistics.pstdev(win_c)
            if mw > 0 and (sd_w / mw) < 0.015 and sd_c > 2.5:
                continue  # ERG clamping artifact
        tk = (30.0 * w) / (math.pi * c)
        burst_c.append(c)
        burst_t.append(tk)

    if len(burst_c) < 6 or (max(burst_c) - min(burst_c)) < 15.0:
        return {}

    mc = sum(burst_c) / len(burst_c)
    mt = sum(burst_t) / len(burst_t)
    sxx = sum((c - mc) ** 2 for c in burst_c)
    if sxx <= 1e-6:
        return {}
    slope = sum((c - mc) * (t - mt) for c, t in zip(burst_c, burst_t)) / sxx
    t_max = mt - slope * mc
    if slope >= -0.08 or t_max <= 40.0 or t_max > 420.0:
        return {}
    c_max = -t_max / slope
    if not (150.0 <= c_max <= 310.0):
        return {}
    c_opt = 0.5 * c_max
    p_max_parabola = (math.pi / 60.0) * t_max * c_opt
    return {
        "t_max_nm": round(t_max, 1),
        "c_max_rpm": int(round(c_max)),
        "c_opt_sprint_rpm": round(c_opt, 1),
        "p_max_theoretical_w": int(round(p_max_parabola)),
    }


def compute_pillar4_myotype(
    activities: list[dict[str, Any]],
    cp_watts: float | None,
    mpr_watts: float | None,
    vlamax_sprint: float | None,
    mass_kg: float | None,
    run_k_leg_kn_m: float | None = None,
) -> dict[str, Any]:
    """Compute Functional Myotype Z-Score (`Z_Myo`) & personalized TID / Interval rules."""
    best_t_max = None
    best_c_opt = None
    for a in activities or []:
        if not isinstance(a, dict) or str(a.get("sport") or "cycling").lower() != "cycling" or a.get("device_watts") is False:
            continue
        tm = a.get("t_max_nm")
        co = a.get("c_opt_sprint_rpm")
        if tm and (best_t_max is None or float(tm) > best_t_max):
            best_t_max = float(tm)
        if co and (best_c_opt is None or float(co) > best_c_opt):
            best_c_opt = float(co)
        # Fallback from peak_torque_nm when raw sprint regression hasn't seen a wide cadence sweep yet
        if best_t_max is None and a.get("peak_torque_nm"):
            best_t_max = round(float(a["peak_torque_nm"]) * 1.35, 1)

    t_max_nm_kg = round(best_t_max / float(mass_kg), 2) if (best_t_max and mass_kg and mass_kg > 25.0) else None

    terms: list[tuple[float, float]] = []
    if best_c_opt is not None:
        terms.append((0.40, (best_c_opt - 120.0) / 8.5))
    if vlamax_sprint is not None:
        terms.append((0.35, (float(vlamax_sprint) - 0.52) / 0.16))
    if mpr_watts is not None and cp_watts and cp_watts > 50:
        terms.append((0.25, ((float(mpr_watts) / float(cp_watts)) - 2.40) / 0.55))

    z_myo = None
    myo_cls = "Unknown"
    rec_hiit_h = "24–48 h"
    vo2_proto = "4-5x 4m @ 106-112% CP"
    avoid_proto = "Unstructured junk miles in Zone 3"
    if terms:
        w_sum = sum(w for w, _ in terms)
        z_myo = round(sum(w * z for w, z in terms) / w_sum, 2)
        if z_myo > 0.50:
            myo_cls = "Fast Myotype"
            rec_hiit_h = "48–72 h (>5h neuromuscular SR recovery)"
            vo2_proto = "Rønnestad 30/15s (3x13, 'On' strictly anchored to CP, never P_5min) or 1:4 Lactate-Shuttle Over/Unders"
            avoid_proto = "Continuous 5x5m @ 120% FTP_20 & back-to-back HIIT days (triggers rapid acidosis & Overreaching)"
        elif z_myo < -0.50:
            myo_cls = "Slow Myotype"
            rec_hiit_h = "24 h (fast ~20m neuromuscular rebound)"
            vo2_proto = "Bossi Alternating 5x5m (30s@100% P_5m / 60s@77% P_5m) or 5x4m @ 108-115% CP (95-105 rpm)"
            avoid_proto = "Mild 30/30 intervals (fast tau_W' drops VO2 kinetics too far during 30s rests)"
        else:
            myo_cls = "Intermediate Myotype"
            rec_hiit_h = "36–48 h"
            vo2_proto = "Pyramidal mix: 4x8m @ 102-105% CP + 4x4m @ 110% CP"
            avoid_proto = "Chronic monotony without recovery days"

    return {
        "t_max_nm": best_t_max,
        "t_max_nm_kg": t_max_nm_kg,
        "c_opt_sprint_rpm": best_c_opt,
        "myotype_z_score": z_myo,
        "myotype_class": myo_cls,
        "myotype_confidence": "calibrated_3factor" if len(terms) == 3 else ("partial" if terms else "uncalibrated"),
        "hiit_recovery_window": rec_hiit_h,
        "recommended_vo2_protocol": vo2_proto,
        "avoid_protocol": avoid_proto,
        "run_k_leg_kn_m": run_k_leg_kn_m,
    }


# ---------------------------------------------------------------------------
# 5. Pillar 5: Intensity-Weighted Durability (`w_sub` vs `w_sup`) & Dual-TID
# ---------------------------------------------------------------------------

def compute_pillar5_durability_and_tid(
    activities: list[dict[str, Any]],
    curves: dict[str, Any],
    cp_watts: float | None,
    mass_kg: float | None,
    today: dt.date | None = None,
) -> dict[str, Any]:
    """Compute `w_sub` vs `w_sup` (`>CP`), `Delta_CP_30kJ_pct`, cadence fade, and Dual-TID."""
    ref_today = today or dt.date.today()
    cutoff_7d = (ref_today - dt.timedelta(days=7)).isoformat()
    cutoff_28d = (ref_today - dt.timedelta(days=28)).isoformat()
    bw = float(mass_kg) if (mass_kg and mass_kg > 25.0) else 70.0
    eff_cp = float(cp_watts) if (cp_watts and cp_watts > 50) else 275.0

    sub_7d_kj = 0.0
    sup_7d_kj = 0.0
    w_lt1_kj = 0.0
    w_heavy_kj = 0.0
    w_severe_kj = 0.0
    sg_low = sg_mod = sg_high = 0
    decouplings: list[float] = []
    cad_drops: list[float] = []

    for a in activities or []:
        if not isinstance(a, dict) or not a.get("date"):
            continue
        d_s = str(a["date"])[:10]
        valid_pwr = str(a.get("sport") or "cycling").lower() == "cycling" and a.get("device_watts") is not False
        if d_s >= cutoff_28d:
            # Session-Goal TID classification
            if_v = float(a.get("if") or 0.0) if valid_pwr else 0.0
            if (valid_pwr and if_v >= 0.92) or (a.get("matches_burned") or 0) >= 3:
                sg_high += 1
            elif valid_pwr and if_v >= 0.78:
                sg_mod += 1
            else:
                sg_low += 1

            if valid_pwr and a.get("decoupling_pct") is not None:
                decouplings.append(float(a["decoupling_pct"]))
            if valid_pwr and a.get("cadence_drop_fatigued_rpm") is not None:
                cad_drops.append(float(a["cadence_drop_fatigued_rpm"]))

        if d_s >= cutoff_7d and valid_pwr:
            tot_kj = float(a.get("work_kj") or 0.0)
            above_kj = float(a.get("kj_above_cp") or 0.0)
            sub_kj = max(0.0, tot_kj - above_kj)
            sub_7d_kj += sub_kj
            sup_7d_kj += above_kj
            w_severe_kj += above_kj
            if float(a.get("if") or 0.70) >= 0.78:
                w_heavy_kj += sub_kj * 0.45
                w_lt1_kj += sub_kj * 0.55
            else:
                w_lt1_kj += sub_kj * 0.88
                w_heavy_kj += sub_kj * 0.12

    # Fatigued 5m/20m drop after >25-35 kJ/kg
    dur_tiers = (curves or {}).get("durability_tiers") or {}
    fresh_5m = float(((curves or {}).get("fresh") or {}).get("5m", {}).get("90d") or 0.0)
    fat_5m_25 = float(((dur_tiers.get("25kj_kg") or {}).get("5m") or {}).get("90d") or 0.0)
    fat_5m_15 = float(((dur_tiers.get("15kj_kg") or {}).get("5m") or {}).get("90d") or 0.0)
    ref_fat_5m = fat_5m_25 or fat_5m_15
    cp_drop_pct = round(((fresh_5m - ref_fat_5m) / fresh_5m) * 100.0, 1) if (fresh_5m > 0 and ref_fat_5m > 0) else None

    tot_sg = max(1, sg_low + sg_mod + sg_high)
    tot_dom = max(1.0, w_lt1_kj + w_heavy_kj + w_severe_kj)
    return {
        "cp_drop_30kj_pct": cp_drop_pct,
        "significant_drop_above_sem": bool(cp_drop_pct is not None and cp_drop_pct > 3.0),
        "cadence_drop_fatigued_rpm": round(statistics.median(cad_drops), 1) if cad_drops else None,
        "z2_pwhr_decoupling_pct": round(statistics.median(decouplings), 1) if decouplings else None,
        "work_sub_cp_7d_kj_kg": round(sub_7d_kj / bw, 1),
        "work_above_cp_7d_kj_kg": round(sup_7d_kj / bw, 2),
        "domain_work_7d_kj": {
            "w_lt1_kj": int(round(w_lt1_kj)),
            "w_lt1_cp_kj": int(round(w_heavy_kj)),
            "w_above_cp_kj": int(round(w_severe_kj)),
        },
        "dual_tid_28d": {
            "session_goal_pct": f"{round(sg_low/tot_sg*100)}/{round(sg_mod/tot_sg*100)}/{round(sg_high/tot_sg*100)}",
            "work_domain_7d_pct": (
                f"{round(w_lt1_kj/tot_dom*100)}/{round(w_heavy_kj/tot_dom*100)}/{round(w_severe_kj/tot_dom*100)}"
                if (w_lt1_kj + w_heavy_kj + w_severe_kj) > 0 else "n/a"
            ),
        },
    }


# ---------------------------------------------------------------------------
# 6. Pillar 6: Morphometry (`CdA`), `Compound Score`, Environment, RED-S & Run
# ---------------------------------------------------------------------------

def minetti_running_cost_j_kg_m(grade_dec: float) -> float:
    """Minetti et al. (2002) 5th-degree metabolic cost polynomial (`-0.25 <= g <= +0.30`)."""
    g = max(-0.25, min(0.30, float(grade_dec)))
    return round(155.4 * (g ** 5) - 30.4 * (g ** 4) - 43.3 * (g ** 3) + 46.3 * (g ** 2) + 19.5 * g + 3.6, 3)


def compute_morin_leg_stiffness(
    mass_kg: float,
    height_cm: float,
    vel_mps: float,
    contact_time_s: float,
    flight_time_s: float,
) -> float | None:
    """Morin et al. (2005 / 2026) sine-wave leg spring stiffness `k_leg` in `kN/m`."""
    if mass_kg <= 25.0 or height_cm <= 120.0 or vel_mps <= 1.5 or contact_time_s <= 0.10 or flight_time_s < 0.0:
        return None
    g0 = 9.81
    leg_l = 0.53 * (height_cm / 100.0)
    f_max = mass_kg * g0 * (math.pi / 2.0) * ((flight_time_s / contact_time_s) + 1.0)
    dy_c = (f_max * (contact_time_s ** 2)) / (mass_kg * (math.pi ** 2)) - (g0 * (contact_time_s ** 2)) / 8.0
    half_step = (vel_mps * contact_time_s) / 2.0
    if half_step >= leg_l:
        return None
    dl = leg_l - math.sqrt((leg_l ** 2) - (half_step ** 2)) + dy_c
    if dl <= 0.005:
        return None
    return round((f_max / dl) / 1000.0, 2)


def compute_townsend_altitude(elev_m: float, cp_watts: float | None, w_prime_kj: float | None) -> dict[str, Any]:
    """Townsend et al. (2017) 2-segment altitude adjustment (`CP` drops >1500m; `W'` stable to 2760m)."""
    h_km = float(elev_m or 0.0) / 1000.0
    cp_loss_pct = round(min(30.0, max(0.0, (h_km - 1.50) * 8.0)), 1) if h_km > 1.50 else 0.0
    wp_pct = 100.0 if h_km <= 2.76 else max(50.0, round(100.0 - 18.3 * (h_km - 2.76), 1))
    tau_mult = 1.0 if h_km <= 1.50 else round(min(1.55, 1.0 + 0.28 * (h_km - 1.50)), 2)
    return {
        "cp_altitude_w": int(round(float(cp_watts) * (1.0 - cp_loss_pct / 100.0))) if cp_watts else None,
        "w_prime_altitude_kj": round(float(w_prime_kj) * (wp_pct / 100.0), 1) if w_prime_kj else None,
        "tau_w_altitude_mult": tau_mult,
    }


def compute_pillar6_locomotion_environment(
    profile: dict[str, Any],
    body: dict[str, Any],
    activities: list[dict[str, Any]],
    curves: dict[str, Any],
    cp_watts: float | None,
    w_prime_kj: float | None,
    readiness_rows: list[dict[str, Any]] | None = None,
    today: dt.date | None = None,
) -> dict[str, Any]:
    """Morphometric `CdA` (Bassett + measured override), `Compound Score`, Learned Indoor Offset & RED-S."""
    ref_today = today or dt.date.today()
    mass = body.get("mass_kg_7d")
    h_cm = profile.get("height_cm")
    m_bike = float(profile.get("bike_weight_kg") or 8.0)

    cda_drops = profile.get("cda_drops")
    cda_source = "measured_profile" if cda_drops else "bassett_est"
    if not cda_drops and mass and h_cm:
        h_m = float(h_cm) / 100.0
        ap_basis = 0.0293 * (h_m ** 0.725) * (float(mass) ** 0.425) + 0.0604
        cda_drops = round(0.84 * ap_basis, 3)

    p5m_90 = float(((curves or {}).get("fresh") or {}).get("5m", {}).get("90d") or 0.0)
    flat_idx = round(float(cp_watts) / float(cda_drops), 0) if (cp_watts and cda_drops and float(cda_drops) > 0.12) else None
    climb_idx = round(float(cp_watts) / (float(mass) + m_bike), 2) if (cp_watts and mass) else None
    comp_score = int(round((p5m_90 ** 2) / float(mass))) if (p5m_90 > 0 and mass and mass > 25.0) else None

    # Refinement #3: Learn personal indoor_cp_offset_w from indoor vs outdoor steady efficiency (fallback -19 W)
    indoor_offset = profile.get("indoor_cp_offset_w")
    if indoor_offset is None:
        in_efs: list[float] = []
        out_efs: list[float] = []
        for a in (activities or [])[:40]:
            if not isinstance(a, dict) or str(a.get("sport") or "").lower() != "cycling" or a.get("device_watts") is False:
                continue
            np_v = float(a.get("np_w") or a.get("avg_w") or 0.0)
            hr_v = float(a.get("avg_hr") or 0.0)
            if np_v > 120 and hr_v > 100:
                if a.get("indoor") or "zwift" in str(a.get("name") or "").lower() or "indoor" in str(a.get("name") or "").lower():
                    in_efs.append(np_v / hr_v)
                else:
                    out_efs.append(np_v / hr_v)
        if len(in_efs) >= 3 and len(out_efs) >= 3 and cp_watts:
            ratio = statistics.median(in_efs) / max(0.5, statistics.median(out_efs))
            indoor_offset = int(round(max(-35.0, min(5.0, float(cp_watts) * (ratio - 1.0)))))
        else:
            indoor_offset = -19

    # 14-day Heat Acclimatization tracker
    cutoff_14d = (ref_today - dt.timedelta(days=14)).isoformat()
    heat_exposures_14d = sum(
        1 for a in (activities or [])
        if isinstance(a, dict) and str(a.get("date", ""))[:10] >= cutoff_14d and (a.get("temp_c") or 0) >= 25.0
    )

    # RED-S Early Warning (falling weight trend + suppressed HRV + stagnant/dropping performance)
    reds_warning = None
    weights_sorted = [
        (r["date"], float(r["weight_kg"]))
        for r in sorted((readiness_rows or []), key=lambda x: x["date"])
        if r.get("weight_kg") is not None
    ]
    if len(weights_sorted) >= 4:
        w_early = sum(x[1] for x in weights_sorted[:2]) / 2.0
        w_late = sum(x[1] for x in weights_sorted[-2:]) / 2.0
        hrv_vals = [float(r["hrv_rmssd"]) for r in (readiness_rows or [])[:7] if r.get("hrv_rmssd")]
        if (w_late - w_early) / max(30.0, w_early) <= -0.025 and hrv_vals and len(hrv_vals) >= 3:
            reds_warning = "Weight down >=2.5% alongside tracked autonomic load — verify energy availability (RED-S guard)"

    sex = str(profile.get("sex") or "neutral")
    sex_dur_note = (
        "Female durability profile (Pastorio/Mateo-March 2025/2026): high sub-CP fat oxidation stability, "
        "but target supra-CP repeatability (>5 kJ/kg >CP) explicitly."
        if sex == "female" else None
    )

    return {
        "cda_drops_est": cda_drops,
        "cda_source": cda_source,
        "bike_weight_kg": m_bike,
        "flat_index_w_m2": int(flat_idx) if flat_idx else None,
        "climb_index_w_kgsys": climb_idx,
        "compound_score_5m": comp_score,
        "indoor_cp_offset_w": indoor_offset,
        "heat_exposures_14d": heat_exposures_14d,
        "heat_acclimatized": heat_exposures_14d >= 8,
        "sex_durability_note": sex_dur_note,
        "reds_warning": reds_warning,
    }


# ---------------------------------------------------------------------------
# 7. Pillar 7 & Stage 2: HRV (`ln RMSSD` / `SWC`), Sanders Ratios & 4-Stage Readiness
# ---------------------------------------------------------------------------

def compute_pillar7_and_stage2_readiness(
    readiness_rows: list[dict[str, Any]],
    activities: list[dict[str, Any]],
    profile: dict[str, Any],
    base_readiness: dict[str, Any],
    cp_watts: float | None,
    myotype_z: float | None = None,
    today: dt.date | None = None,
) -> dict[str, Any]:
    """Compute `ln RMSSD_7d`, `CV`, `SWC`, Sanders `Q_Strain`/`Q_RPE:PO`, & 4-Stage Readiness Flag."""
    ref_today = today or dt.date.today()
    cutoff_7d = (ref_today - dt.timedelta(days=7)).isoformat()
    cutoff_28d = (ref_today - dt.timedelta(days=28)).isoformat()

    # 1. Rolling HRV (ln RMSSD_7d, CV_lnRMSSD, 28d SWC)
    ln_7d: list[float] = []
    ln_28d: list[float] = []
    for r in readiness_rows or []:
        rmssd = r.get("hrv_rmssd")
        if rmssd and float(rmssd) > 5.0:
            ln_v = math.log(float(rmssd))
            if r["date"] >= cutoff_28d:
                ln_28d.append(ln_v)
            if r["date"] >= cutoff_7d:
                ln_7d.append(ln_v)

    ln_mean_7d = round(sum(ln_7d) / len(ln_7d), 3) if ln_7d else None
    cv_ln_7d = round((statistics.pstdev(ln_7d) / ln_mean_7d) * 100.0, 2) if (len(ln_7d) >= 3 and ln_mean_7d) else None
    ln_mean_28d = (sum(ln_28d) / len(ln_28d)) if len(ln_28d) >= 5 else None
    swc_28d = (0.5 * statistics.pstdev(ln_28d)) if len(ln_28d) >= 5 else None
    hrv_below_swc = bool(ln_mean_7d is not None and ln_mean_28d is not None and swc_28d is not None and ln_mean_7d < (ln_mean_28d - swc_28d))

    # 2. Sanders Subjective-Objective Ratios (sRPE / TSS_CP, RPE / (P_avg / CP))
    eff_cp = float(cp_watts or profile.get("cp_w") or profile.get("ftp_w") or 275.0)
    q_strains: list[float] = []
    q_rpe_pos: list[float] = []
    for a in (activities or [])[:10]:
        if not isinstance(a, dict):
            continue
        rpe = a.get("rpe")
        if rpe is None and a.get("feedback"):
            m_r = re.search(r"\brpe\s*[:=]?\s*(\d{1,2}(?:[.,]\d)?)", str(a["feedback"]), re.I)
            if m_r:
                rpe = float(m_r.group(1).replace(",", "."))
        dur_m = float(a.get("duration_min") or 0.0)
        if rpe and dur_m >= 15.0:
            srpe = float(rpe) * dur_m
            tss = float(a.get("tss") or 0.0)
            if tss > 10.0:
                q_strains.append(round(srpe / tss, 2))
            avg_w = float(a.get("avg_w") or a.get("np_w") or 0.0)
            if avg_w > 50.0 and eff_cp > 50.0:
                q_rpe_pos.append(round(float(rpe) / (avg_w / eff_cp), 2))

    latest_q_strain = q_strains[0] if q_strains else None
    latest_q_rpe_po = q_rpe_pos[0] if q_rpe_pos else None

    # 3. Resolve 4-Stage Readiness (`GRÜN` | `GELB-A` | `GELB-B` | `ROT`)
    today_row = next((r for r in (readiness_rows or []) if r["date"] == ref_today.isoformat()), None)
    leg_pain = int(today_row["leg_pain"]) if (today_row and today_row.get("leg_pain") is not None) else (
        int(today_row["soreness"]) if (today_row and today_row.get("soreness") is not None) else None
    )
    fatigue_7d = int(today_row["fatigue_7d"]) if (today_row and today_row.get("fatigue_7d") is not None) else None
    notes_low = str((today_row or {}).get("notes") or "").lower()
    heavy_legs_text = any(w in notes_low for w in ("beine wie beton", "schwere beine", "leere beine", "dead legs", "heavy legs"))

    base_st = base_readiness.get("status", "GREEN")
    if base_st == "RED" or (fatigue_7d is not None and fatigue_7d >= 5):
        flag_4 = "ROT"
        directive = "Overreaching / Red Guardrail: Immediate recovery or 3–5d deload + high CHO availability; cancel all HIIT."
    elif (leg_pain is not None and leg_pain >= 4) or heavy_legs_text or (latest_q_strain is not None and latest_q_strain >= 8.5):
        flag_4 = "GELB-B"
        myo_note = " (Fast-Myotype Type-II / intramyofibrillar glycogen lag)" if (myotype_z and myotype_z > 0.5) else ""
        directive = f"Peripheral Conflict — Subjective Legs Override Autonomic HRV{myo_note}: postpone HIIT 24–48h; spin easy Z1."
    elif base_st == "AMBER" or hrv_below_swc:
        flag_4 = "GELB-A"
        directive = "Acute Stimulus Stress: swap today's HIIT for relaxed Zone 1/2 endurance; keep macro block intact."
    else:
        flag_4 = "GRÜN"
        directive = "All Systems Clear: execute planned key intervals or progression at full target."

    # Karvonen HRR anchor
    hrmax = profile.get("hr_max")
    rhr = (today_row or {}).get("rhr") or base_readiness.get("baseline_rhr") or profile.get("rhr")
    hrr_span = int(round(float(hrmax) - float(rhr))) if (hrmax and rhr and float(hrmax) > float(rhr) + 20) else None

    return {
        "readiness_flag_4stage": flag_4,
        "coaching_directive": directive,
        "today_logged": today_row is not None,
        "ln_rmssd_7d": ln_mean_7d,
        "cv_ln_rmssd_pct": cv_ln_7d,
        "hrv_below_swc": hrv_below_swc,
        "hrr_karvonen_bpm": hrr_span,
        "q_strain_srpe_tss": latest_q_strain,
        "q_rpe_po": latest_q_rpe_po,
        "leg_pain_today": leg_pain,
        "fatigue_7d": fatigue_7d,
    }


# ---------------------------------------------------------------------------
# 8. Plan-vs-Actual Interval Execution, Dose-Response, Missing Gate & Autonomy
# ---------------------------------------------------------------------------

def evaluate_workout_execution(
    act: dict[str, Any],
    planned_item: dict[str, Any] | None,
    parsed_spec: dict[str, Any] | None,
    ftp_watts: int = 275,
    watts_stream: list[float] | None = None,
) -> dict[str, Any]:
    """Compare completed Strava activity against planned calendar workout (interval hit-rate & Z2 discipline)."""
    if not planned_item or not parsed_spec:
        return {}
    valid_pwr = str(act.get("sport") or "cycling").lower() == "cycling" and act.get("device_watts") is not False
    planned_min = int(parsed_spec.get("total_min") or 0)
    actual_min = int(act.get("duration_min") or 0)
    dur_ratio = round(actual_min / planned_min, 2) if planned_min > 0 else 1.0

    blocks = parsed_spec.get("segments") or parsed_spec.get("blocks") or []
    has_hiit_plan = any(
        ((b.get("type") or b.get("kind")) == "intervals" and float(b.get("on_p") or b.get("on_power") or 0) >= 0.88)
        or ((b.get("type") or b.get("kind")) not in ("warmup", "cooldown", "intervals", "freeride") and float(b.get("p") or b.get("power") or 0) >= 0.88)
        for b in blocks
    )
    is_z2_plan = not has_hiit_plan and planned_min >= 35

    breaches: list[str] = []
    praise: list[str] = []
    score = 100

    if planned_min >= 30:
        if dur_ratio < 0.70:
            score -= 30
            breaches.append(f"Cut short ({actual_min}m vs planned {planned_min}m)")
        elif dur_ratio >= 0.92:
            praise.append(f"Completed full duration ({actual_min}m/{planned_min}m)")

    if valid_pwr:
        if_actual = float(act.get("if") or 0.0)
        matches = int(act.get("matches_burned") or 0)
        if is_z2_plan and (if_actual >= 0.82 or matches >= 2):
            score -= 28
            breaches.append(
                f"Z2 Intensity Discipline Breach: rode IF {if_actual:.2f} with {matches} supra-CP surge(s) on an easy base day"
            )
        elif is_z2_plan and 0.55 <= if_actual <= 0.76 and matches == 0:
            praise.append(f"Clean Zone 2 discipline (IF {if_actual:.2f}, 0 surges)")

        # Check interval block hit-rate when 1Hz watts stream is available
        if has_hiit_plan and watts_stream:
            for b in blocks:
                if (b.get("type") or b.get("kind")) == "intervals":
                    reps = max(1, int(b.get("repeat") or b.get("repeats") or 1))
                    on_s = max(15, int(b.get("on_sec") or 60))
                    target_w = float(b.get("on_p") or b.get("on_power") or 1.0) * float(ftp_watts or 275)
                    # Find top non-overlapping `reps` windows of length `on_s`
                    w_len = len(watts_stream)
                    if w_len >= on_s:
                        pref = [0.0] * (w_len + 1)
                        for idx_w, vw in enumerate(watts_stream):
                            pref[idx_w + 1] = pref[idx_w] + max(0.0, float(vw or 0.0))
                        cands = [((pref[k + on_s] - pref[k]) / on_s, k) for k in range(0, w_len - on_s + 1, max(5, on_s // 4))]
                        cands.sort(reverse=True)
                        chosen: list[tuple[int, float]] = []
                        for avg_seg, start_k in cands:
                            if all(abs(start_k - prev_k) >= int(on_s * 0.85) for prev_k, _ in chosen):
                                chosen.append((start_k, avg_seg))
                                if len(chosen) == reps:
                                    break
                        chosen.sort(key=lambda x: x[0])
                        if chosen:
                            hit_cnt = sum(1 for _, aw in chosen if aw >= 0.94 * target_w)
                            rep_str = ", ".join(f"#{i+1}:{int(round(aw))}W" for i, (_, aw) in enumerate(chosen))
                            if hit_cnt < reps:
                                score -= int(round(25 * (1.0 - hit_cnt / reps)))
                                breaches.append(f"Interval fade/shortfall ({hit_cnt}/{reps} hit >=94% of {int(round(target_w))}W: {rep_str})")
                            else:
                                praise.append(f"Nailed all {reps}/{reps} intervals (~{int(round(target_w))}W target: {rep_str})")

    score = max(0, min(100, score))
    return {
        "planned_title": parsed_spec.get("title"),
        "execution_score": score,
        "breaches": breaches,
        "praise": praise,
    }


def compute_individual_response_model(
    activities: list[dict[str, Any]],
    readiness_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """Correlate athlete's own history (`N >= 4`) to derive empirical individual dose-response signatures."""
    r_by_date = {r["date"]: r for r in (readiness_rows or []) if r.get("date")}
    low_sleep_soreness: list[float] = []
    good_sleep_soreness: list[float] = []
    for r in readiness_rows or []:
        if r.get("sleep_h") is not None and r.get("soreness") is not None:
            if float(r["sleep_h"]) < 6.2:
                low_sleep_soreness.append(float(r["soreness"]))
            elif float(r["sleep_h"]) >= 7.2:
                good_sleep_soreness.append(float(r["soreness"]))

    sleep_sig = None
    if low_sleep_soreness and good_sleep_soreness:
        d_sor = (sum(low_sleep_soreness) / len(low_sleep_soreness)) - (sum(good_sleep_soreness) / len(good_sleep_soreness))
        if d_sor >= 0.8:
            sleep_sig = f"High sleep sensitivity: <6.2h sleep elevates next-day soreness/fatigue by +{d_sor:.1f} pts"

    # Next-day RHR response after heavy >20 kJ/kg rides
    post_heavy_rhr: list[float] = []
    for a in activities or []:
        if not isinstance(a, dict) or not a.get("date"):
            continue
        if float(a.get("work_kj_kg") or 0.0) >= 20.0:
            try:
                nxt = (dt.date.fromisoformat(str(a["date"])[:10]) + dt.timedelta(days=1)).isoformat()
                if nxt in r_by_date and r_by_date[nxt].get("rhr") is not None:
                    post_heavy_rhr.append(float(r_by_date[nxt]["rhr"]))
            except ValueError:
                pass

    all_rhr = [float(r["rhr"]) for r in (readiness_rows or []) if r.get("rhr") is not None]
    heavy_rhr_sig = None
    if len(post_heavy_rhr) >= 2 and len(all_rhr) >= 4:
        med_rhr = statistics.median(all_rhr)
        diff = (sum(post_heavy_rhr) / len(post_heavy_rhr)) - med_rhr
        heavy_rhr_sig = f"After >=20 kJ/kg sessions, next-day RHR shifts {diff:+.1f} bpm vs baseline ({med_rhr:.0f} bpm)"

    return {
        "calibrated_pairs": len(r_by_date),
        "sleep_sensitivity": sleep_sig,
        "heavy_load_recovery": heavy_rhr_sig,
        "status": "calibrated" if len(r_by_date) >= 5 else f"calibrating ({len(r_by_date)}/5 readiness days logged)",
    }


def parse_playbook(playbook_text: str) -> dict[str, Any]:
    """Parse `brain/coach/playbook.md` for distilled empirical rules and answered discovery dimensions."""
    rules: list[str] = []
    answered_keys: set[str] = set()
    if not playbook_text:
        return {"rules": [], "answered_keys": set(), "exists": False}

    low_all = playbook_text.lower()
    in_rules = False
    for raw in playbook_text.splitlines():
        s = raw.strip()
        if s.startswith("## "):
            in_rules = any(k in s.lower() for k in ("empirical", "learned", "regeln", "muster", "feelings"))
            continue
        if in_rules and s.startswith(("- ", "* ")) and "<!--" not in s and "_(noch offen" not in s.lower():
            rules.append(s[2:].strip())

    # Check which discovery dimensions already have substantive text
    key_markers = {
        "why_and_motivation": ("motivation", "antrieb", "warum"),
        "coaching_tone": ("coaching-stil", "tonfall", "world-tour", "strenge", "accountability"),
        "skip_triggers": ("saboteur", "ausfall", "alltag", "zeitfresser"),
        "sleep_architecture": ("chronotyp", "schlaf", "uhr"),
        "gut_and_fueling": ("magen", "g/h", "maltodextrin", "fruktose", "gels"),
        "indoor_setup": ("smart-trainer", "ventilator", "indoor", "erg", "direto", "kickr"),
        "power_sensors": ("powermeter", "pedal", "kurbel", "assioma", "quarq"),
        "injury_orthopedics": ("orthopädie", "knie", "rücken", "schmerz", "beschwerdefrei"),
        "pacing_weakness": ("pacing", "rennen", "limiter", "antritt"),
        "strength_equipment": ("hantel", "gym", "bodyweight", "klimmzug", "1rm"),
    }
    for dim_k, tokens in key_markers.items():
        for ln in playbook_text.splitlines():
            s = ln.strip()
            if not s or s.startswith("#") or "<!--" in s or "_(open" in s.lower() or "_(noch offen" in s.lower():
                continue
            if any(t in s.lower() for t in tokens) and len(s) >= 25:
                answered_keys.add(dim_k)
                break
    return {"rules": rules, "answered_keys": answered_keys, "exists": bool(low_all.strip())}


def audit_missing_inputs_and_questions(
    profile: dict[str, Any],
    body: dict[str, Any],
    pillar2: dict[str, Any],
    cp_model: dict[str, Any],
    readiness_stage2: dict[str, Any],
    playbook_info: dict[str, Any],
    activities: list[dict[str, Any]],
    cal_items: list[dict[str, Any]],
    macro_info: dict[str, Any] | None,
    dna_snapshot: dict[str, Any] | None,
    today: dt.date | None = None,
) -> dict[str, Any]:
    """Build Missing-Data Gate (`0_missing`), Autonomy Actions (`1_autonomy`), Accountability & Question Radar."""
    ref_today = today or dt.date.today()
    explicit = set(profile.get("explicit_fields") or ())
    is_skel = bool(profile.get("is_skeleton"))
    sport_focus = str(profile.get("primary_sport") or "cycling").lower()
    is_cyclist = "run" not in sport_focus or "cycl" in sport_focus or "rad" in sport_focus or not sport_focus

    missing_critical: list[dict[str, str]] = []
    if not body.get("mass_calibrated"):
        missing_critical.append({
            "field": "weight_kg",
            "blocks": "S1 (VO2max), S2 (vLamax/Mader), S6 (W/kg & Compound Score)",
            "ask": "Mir fehlt noch dein aktuelles Körpergewicht (morgens nüchtern in kg) – wie viel wiegst du aktuell?",
        })
    if is_cyclist and not body.get("kfa_calibrated"):
        missing_critical.append({
            "field": "kfa_pct",
            "blocks": "S2 (Fettfreie Masse FFM & Laktatverteilungsraum Vol_rel,ind für vLamax/MLSS) & S1 (VO2max_FFM)",
            "ask": "Mir fehlt dein Körperfettanteil (gemessen per Waage/Kaliper oder ehrlich geschätzt in %) für deine fettfreie Masse (FFM) – wo liegst du ca.?",
        })
    if "height_cm" not in explicit or is_skel:
        missing_critical.append({
            "field": "height_cm",
            "blocks": "S6 (Stirnfläche/CdA) & Lauf-Beinfedersteifigkeit k_leg",
            "ask": "Wie groß bist du (in cm)?",
        })
    if ("rhr" not in explicit or is_skel) and not readiness_stage2.get("hrr_karvonen_bpm"):
        missing_critical.append({
            "field": "rhr",
            "blocks": "S7 (Karvonen %HRR & autonome Ruhe-Baseline)",
            "ask": "Mir fehlt noch dein typischer morgendlicher Ruhepuls (in bpm) – kennst du deinen Schnitt oder misst deine Uhr ihn?",
        })
    if "hr_max" not in explicit or is_skel:
        missing_critical.append({
            "field": "hr_max",
            "blocks": "S7 (Karvonen %HRR & LSCT 60/80/90% Stufen)",
            "ask": "Was ist dein höchster real gemessener Maximalpuls (in bpm)?",
        })
    if profile.get("age") is None or str(profile.get("sex") or "neutral") == "neutral":
        missing_critical.append({
            "field": "age_and_sex",
            "blocks": "S1/S6 (Geschlechtsspezifische Durability-Kurven, RED-S-Schutz & Metabolik)",
            "ask": "Bitte bestätige kurz Alter/Geburtsjahr und biologisches Geschlecht fürs physiologische Modell.",
        })
    if is_cyclist and profile.get("creatine_flag") is None:
        missing_critical.append({
            "field": "creatine_flag",
            "blocks": "S2 (Meixner 2025 15s-Sprint- & FFM-Wasser-Kalibrierung)",
            "ask": "Supplementierst du aktuell Kreatin (ja/nein)?",
        })

    # Field-test anchor calibration checks
    calibration_gaps: list[str] = []
    if is_cyclist and not cp_model.get("valid"):
        calibration_gaps.append(
            f"CP/W' uncalibrated ({cp_model.get('quality_flag')}): schedule Field-Test Unit 2 (5m + 12m All-Out) or Unit 1 (15s + 3m)."
        )
    if is_cyclist and pillar2.get("sprint_anchor_status") in ("missing", "passive_lower_bound"):
        calibration_gaps.append(
            "No true 15s maximal seated sprint anchor in recent data -> schedule Field-Test Unit 1 (6s C_max + 15s All-Out Sprint)."
        )

    # Autonomous file actions
    autonomy_actions: list[str] = []
    if macro_info and (macro_info.get("stale") or str(macro_info.get("active_phase") or "").lower() in ("", "uninitialized")):
        autonomy_actions.append(
            f"AUTO_UPDATE_MACRO: `brain/coach/macro-plan.md` is stale/uninitialized ({macro_info.get('stale_reason') or 'empty'}) -> "
            f"build or update it autonomously NOW (set `updated: {ref_today.isoformat()}` & `goals_hash: {macro_info.get('current_goals_hash', '')}`)."
        )

    dna_snap = dna_snapshot or {}
    dna_upd = str(dna_snap.get("updated") or "")
    dna_age = 999
    if re.match(r"^\d{4}-\d{2}-\d{2}$", dna_upd):
        try:
            dna_age = (ref_today - dt.date.fromisoformat(dna_upd)).days
        except ValueError:
            dna_age = 999
    if activities and (not dna_snap.get("archetype") or dna_age > 21):
        autonomy_actions.append(
            f"AUTO_UPDATE_DNA: `brain/coach/dna.md` is {'missing' if dna_age == 999 else f'{dna_age}d old (>21d)'} -> "
            "write/refresh the Athlete DNA Scouting Report + statcard autonomously when appropriate."
        )

    future_7d_wo = [
        it for it in (cal_items or [])
        if it.get("kind") == "workout" and it.get("status") == "open"
        and isinstance(it.get("start"), dt.date) and 0 <= (it["start"] - ref_today).days <= 7
    ]
    if len(future_7d_wo) == 0:
        autonomy_actions.append(
            "AUTO_PLAN_NEXT_7D: Zero open workouts scheduled in the next 7 days in `brain/calendar/README.md` -> "
            "propose/write concrete upcoming workouts aligned with the macro phase and run `atlas.py coach push --execute`."
        )

    # Accountability checks: unexcused past `[ ]` skipped workouts (-7d .. -1d)
    accountability_flags: list[str] = []
    past_uncompleted = [
        it for it in (cal_items or [])
        if it.get("kind") == "workout" and it.get("status") == "open"
        and isinstance(it.get("start"), dt.date) and -7 <= (it["start"] - ref_today).days < 0
    ]
    for sk in past_uncompleted[:3]:
        accountability_flags.append(
            f"UNEXCUSED_SKIP: Planned workout on {sk['start'].isoformat()} ('{sk.get('what', 'Workout')}') is still unmarked `[ ]` "
            "with no matching activity — call it out directly, ask why it didn't happen, mark `[-]` or `[x]`, and adapt the week!"
        )

    for a in (activities or [])[:4]:
        ex = a.get("execution") or {}
        for br in (ex.get("breaches") or []):
            accountability_flags.append(f"EXECUTION_CALLOUT ({a.get('date')} '{a.get('name')}'): {br}")
        for pr in (ex.get("praise") or []):
            accountability_flags.append(f"EXECUTION_PRAISE ({a.get('date')} '{a.get('name')}'): {pr}")

    # Socratic Question Radar (prioritized: 1. Missing inputs / Today readiness / Pending ride, 2. Deep discovery gaps)
    questions: list[str] = []
    if not readiness_stage2.get("today_logged"):
        questions.append(
            "Tagesform-Check: Wie hast du heute Nacht geschlafen und wie fühlen sich Beine (1–5) & Kopf heute an?"
        )
    for m_in in missing_critical[:2]:
        questions.append(f"Daten-Lücke ({m_in['field']}): {m_in['ask']}")

    answered = set(playbook_info.get("answered_keys") or ())
    # Also mark answered if already documented in profile bio_rules
    bio_blob = " ".join(profile.get("bio_rules") or []).lower()
    if "schlaf" in bio_blob:
        answered.add("sleep_architecture")
    if "torque" in bio_blob or "cadence" in bio_blob:
        answered.add("pacing_weakness")

    discovery_prompts: list[str] = []
    for dim_key, dim_title, dim_q in DISCOVERY_DIMENSIONS:
        if dim_key not in answered:
            discovery_prompts.append(f"[{dim_title}] {dim_q}")
        if len(discovery_prompts) >= 2:
            break

    return {
        "missing_critical": missing_critical,
        "calibration_gaps": calibration_gaps,
        "autonomy_actions": autonomy_actions,
        "accountability_flags": accountability_flags,
        "priority_questions": questions[:3],
        "discovery_questions": discovery_prompts[:2],
    }


# ---------------------------------------------------------------------------
# 9. On-Demand Progressive Pull Helpers (`history`, `compare`, `inspect`)
# ---------------------------------------------------------------------------

def query_coach_history(
    activities: list[dict[str, Any]],
    date_from: str | None = None,
    date_to: str | None = None,
    months: int = 6,
    sport_filter: str | None = None,
    today: dt.date | None = None,
) -> str:
    """On-demand multi-month historical query (`atlas.py coach history`) so daily context stays compact."""
    ref_today = today or dt.date.today()
    d_end = date_to or ref_today.isoformat()
    d_start = date_from or (ref_today - dt.timedelta(days=max(7, int(months * 30.5)))).isoformat()

    matched = []
    for a in activities or []:
        if not isinstance(a, dict) or not a.get("date"):
            continue
        ds = str(a["date"])[:10]
        if not (d_start <= ds <= d_end):
            continue
        if sport_filter and str(a.get("sport") or "").lower() != sport_filter.lower():
            continue
        matched.append(a)
    matched.sort(key=lambda x: str(x.get("date", "")), reverse=True)

    by_month: dict[str, dict[str, Any]] = {}
    for a in matched:
        ym = str(a["date"])[:7]
        b = by_month.setdefault(ym, {"count": 0, "min": 0, "kj": 0, "above_cp_kj": 0.0, "best_5m": 0, "best_20m": 0})
        b["count"] += 1
        b["min"] += int(a.get("duration_min") or 0)
        if str(a.get("sport") or "").lower() == "cycling" and a.get("device_watts") is not False:
            b["kj"] += int(a.get("work_kj") or 0)
            b["above_cp_kj"] += float(a.get("kj_above_cp") or 0.0)
            pf = a.get("peaks_fresh") or {}
            b["best_5m"] = max(b["best_5m"], int(pf.get("5m") or 0))
            b["best_20m"] = max(b["best_20m"], int(pf.get("20m") or 0))

    lines = [
        f"=== COACH ON-DEMAND HISTORY ({d_start} .. {d_end} | {len(matched)} sessions) ===",
        "Monthly Summary:",
    ]
    for ym, b in sorted(by_month.items(), reverse=True):
        lines.append(
            f"  - {ym}: {b['count']} sessions | {b['min']/60.0:.1f} h | {b['kj']} kJ total ({b['above_cp_kj']:.0f} kJ >CP) | "
            f"Best 5m={b['best_5m'] or '—'}W, 20m={b['best_20m'] or '—'}W"
        )
    lines.append("\nMatching Sessions (up to 40):")
    for a in matched[:40]:
        pwr = f"NP {a['np_w']}W (IF {a.get('if', '—')}, {a.get('work_kj', 0)}kJ)" if a.get("np_w") else f"{a.get('distance_km', 0):.1f}km"
        fb = f" | feel: {a['feedback']}" if a.get("feedback") and a.get("feedback") != "pending" else ""
        lines.append(f"  - {a['date']} [{a.get('sport', 'ride')}] '{a.get('name', 'Session')}' ({a.get('duration_min', 0)}m | {pwr}){fb}")
    return "\n".join(lines)


def detect_lsct_warmup(
    watts: list[float],
    hr: list[float],
    hr_max: int | float | None,
) -> dict[str, Any] | None:
    """Detect Lamberts Submaximal Cycle Test (`6m@60% -> 6m@80% -> 3m@90% HRmax + 60s sit-up HRR_60s`)."""
    n = min(len(watts or []), len(hr or []))
    if not hr_max or float(hr_max) < 140 or n < 960:
        return None
    hm = float(hr_max)
    # Check first ~20 minutes for progressive 360s + 360s + 180s stages followed by >=60s zero-watt recovery
    for offset in range(0, min(300, n - 960), 30):
        s1_hr = sum(hr[offset + 120: offset + 360]) / 240.0
        s2_hr = sum(hr[offset + 480: offset + 720]) / 240.0
        s3_hr = sum(hr[offset + 780: offset + 900]) / 120.0
        post_w = sum(watts[offset + 905: offset + 960]) / 55.0
        if (0.54 * hm <= s1_hr <= 0.67 * hm
                and 0.74 * hm <= s2_hr <= 0.86 * hm
                and 0.85 * hm <= s3_hr <= 0.95 * hm
                and post_w < 25.0):
            s2_w = int(round(sum(watts[offset + 420: offset + 720]) / 300.0))
            s3_w = int(round(sum(watts[offset + 750: offset + 900]) / 150.0))
            hr_peak_end = max(hr[offset + 885: offset + 905])
            hr_at_60s = sum(hr[offset + 955: offset + 965]) / max(1, len(hr[offset + 955: offset + 965]))
            hrr_60s = int(round(max(0.0, hr_peak_end - hr_at_60s)))
            return {
                "stage2_80pct_w": s2_w,
                "stage3_90pct_w": s3_w,
                "hrr_60s_bpm": hrr_60s,
            }
    return None


def format_seven_pillar_blocks(telemetry: dict[str, Any]) -> str:
    """Render the compact, zero-hallucination 7-Pillar Vector + Missing Gate + Autonomy + Question Radar."""
    sp = telemetry.get("seven_pillars") or {}
    audit = telemetry.get("coach_audit") or {}
    resp_m = telemetry.get("response_model") or {}
    prof = telemetry.get("profile") or {}
    lines: list[str] = []

    # Block 0: Missing-Data & Calibration Gate
    miss = audit.get("missing_critical") or []
    cal_gaps = audit.get("calibration_gaps") or []
    if miss or cal_gaps:
        lines.append("=== 0. MISSING-DATA & CALIBRATION GATE (NO SILENT FALLBACKS!) ===")
        for m in miss:
            lines.append(f"  [MISSING: {m['field']}] Affects {m['blocks']} -> ASK ATHLETE: \"{m['ask']}\"")
        for cg in cal_gaps:
            lines.append(f"  [CALIBRATION] {cg}")

    # Block 1: Autonomy & Accountability Directives
    auto_acts = audit.get("autonomy_actions") or []
    acc_flags = audit.get("accountability_flags") or []
    persona = prof.get("coach_style") or "direct-demanding"
    acc_lvl = prof.get("accountability") or "strict"
    lines.append(f"=== 1A. AUTONOMOUS AGENT ACTIONS & COACH ACCOUNTABILITY (style: {persona}, accountability: {acc_lvl}) ===")
    if auto_acts:
        for act in auto_acts:
            lines.append(f"  -> {act}")
    else:
        lines.append("  -> Files up-to-date (`macro-plan.md`, `dna.md`, 7d workout horizon).")
    for af in acc_flags:
        lines.append(f"  [!] {af}")

    # Block 2: The 7-Pillar Biophysical Vector
    b = sp.get("body") or {}
    p1 = sp.get("pillar1_oxidative") or {}
    p2 = sp.get("pillar2_metabolic") or {}
    p3 = sp.get("pillar3_kinetics") or {}
    p4 = sp.get("pillar4_myotype") or {}
    p5 = sp.get("pillar5_durability") or {}
    p6 = sp.get("pillar6_locomotion") or {}
    p7 = sp.get("pillar7_readiness") or {}
    cp_m = telemetry.get("cp_model") or {}

    if sp:
        kfa_tag = f"{b.get('kfa_pct')}%" if b.get("kfa_calibrated") else "MISSING (provisional FFM)"
        lines.append("=== 1B. 7-PILLAR BIOPHYSICAL STATE VECTOR ===")
        lines.append(
            f"  [1_constraints] mass_7d={b.get('mass_kg_7d') or 'MISSING'}kg | KFA={kfa_tag} | "
            f"FFM={b.get('ffm_kg') or '—'}kg | Vol_rel={b.get('vol_rel_ind')}L/kg | "
            f"sex={prof.get('sex')} | age={prof.get('age') or '—'} | creatine={prof.get('creatine_flag')}"
        )
        if p1.get("vo2max_rel_clean") or p2.get("vlamax_sprint_proxy"):
            lines.append(
                f"  [2_metabolic S1+S2] VO2max_clean={p1.get('vo2max_rel_clean') or '—'} mL/kg/min "
                f"(VO2max_FFM={p1.get('vo2max_ffm') or '—'}, W'-deduct={p1.get('w_prime_deduction_w') or 0}W) | "
                f"vLamax_sprint={p2.get('vlamax_sprint_proxy') or '—'} mmol/L/s ({p2.get('vlamax_confidence')}) | "
                f"eta_shuttle={p2.get('shuttle_index_eta') or '—'} ({p2.get('shuttle_class') or 'uncalibrated'}) | "
                f"FatMax={p2.get('p_fatmax_w') or '—'}W ({p2.get('mfo_g_min') or '—'}g/min) | CarbMax@90g/h={p2.get('p_carbmax_90_w') or '—'}W"
            )
        if cp_m.get("cp_w") or telemetry.get("effective_cp_w"):
            lines.append(
                f"  [3_kinetics S3] CP={cp_m.get('cp_w') or telemetry.get('effective_cp_w')}W (SEE={p3.get('see_cp_pct') or '—'}%) | "
                f"W'={cp_m.get('w_prime_kj') or telemetry.get('effective_w_prime_kj') or '—'}kJ | "
                f"MPR={p3.get('mpr_w') or '—'}W | OmniPD_A={p3.get('omni_decay_a_w') or '—'}W | "
                f"Chorley tau_FC(50%CP)={p3.get('tau_fc_50pct_s') or 22}s, tau_SC={p3.get('tau_sc_50pct_s') or 503}s"
            )
        if p4.get("myotype_z_score") is not None or p4.get("t_max_nm_kg") is not None:
            lines.append(
                f"  [4_neuromuscular S4] Z_Myo={p4.get('myotype_z_score') if p4.get('myotype_z_score') is not None else '—'} "
                f"({p4.get('myotype_class')}) | T_max={p4.get('t_max_nm_kg') or '—'} Nm/kg | C_opt={p4.get('c_opt_sprint_rpm') or '—'} rpm | "
                f"HIIT_Recovery={p4.get('hiit_recovery_window')} | Protocol: {p4.get('recommended_vo2_protocol')}"
            )
        lines.append(
            f"  [5_durability S5] 7d_Work: w_sub={p5.get('work_sub_cp_7d_kj_kg', 0)} kJ/kg (<=CP), "
            f"w_sup={p5.get('work_above_cp_7d_kj_kg', 0)} kJ/kg (>CP!) | "
            f"Drop@25-30kJ/kg={p5.get('cp_drop_30kj_pct') if p5.get('cp_drop_30kj_pct') is not None else '—'}% | "
            f"Steady_Pw:Hr={p5.get('z2_pwhr_decoupling_pct') if p5.get('z2_pwhr_decoupling_pct') is not None else '—'}% | "
            f"Dual_TID(SG vs Domain)={p5.get('dual_tid_28d', {}).get('session_goal_pct')} vs {p5.get('dual_tid_28d', {}).get('work_domain_7d_pct')}"
        )
        if p6.get("compound_score_5m") or p6.get("cda_drops_est"):
            lines.append(
                f"  [6_locomotion S6] CdA_drops={p6.get('cda_drops_est') or '—'}m² ({p6.get('cda_source')}) | "
                f"Flat={p6.get('flat_index_w_m2') or '—'}W/m² | Climb={p6.get('climb_index_w_kgsys') or '—'}W/kg_sys (bike {p6.get('bike_weight_kg')}kg) | "
                f"CompoundScore_5m={p6.get('compound_score_5m') or '—'} W²/kg | IndoorOffset={p6.get('indoor_cp_offset_w')}W | "
                f"Heat14d={p6.get('heat_exposures_14d')} sessions"
            )
        if p6.get("reds_warning"):
            lines.append(f"  [!] RED-S WARNING: {p6['reds_warning']}")
        lines.append(
            f"  [7_readiness_feelings S7] 4-Stage={p7.get('readiness_flag_4stage')} (today_logged={p7.get('today_logged')}) | "
            f"lnRMSSD_7d={p7.get('ln_rmssd_7d') or '—'} (CV={p7.get('cv_ln_rmssd_pct') or '—'}%) | "
            f"Q_Strain(sRPE/TSS)={p7.get('q_strain_srpe_tss') or '—'} | Q_RPE:PO={p7.get('q_rpe_po') or '—'} | "
            f"Directive: {p7.get('coaching_directive')}"
        )

    # Individual Dose-Response Model
    if resp_m:
        sigs = [x for x in (resp_m.get("sleep_sensitivity"), resp_m.get("heavy_load_recovery")) if x]
        lines.append(
            f"  [dose_response] {resp_m.get('status')}" + (f" -> {'; '.join(sigs)}" if sigs else "")
        )

    # Socratic Question Radar
    pq = audit.get("priority_questions") or []
    dq = audit.get("discovery_questions") or []
    if pq or dq:
        lines.append("=== 1C. SOCRATIC QUESTION RADAR (ASK 1-2 SHARP QUESTIONS AT END OF REPLY!) ===")
        for q in pq:
            lines.append(f"  [Acute/Data] {q}")
        for q in dq:
            lines.append(f"  [Athlete Discovery -> file answer in brain/coach/playbook.md] {q}")

    return "\n".join(lines)

