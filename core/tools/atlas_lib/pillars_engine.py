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
    """Clean 1-2s non-progressive powermeter glitches (Mateo-March et al. 2025).

    Preserves legitimate sharp interval step changes and sprint accelerations (where neighbors
    sustain or progress the load) while clamping isolated 1-2s telemetry spikes (`>2500 W`, `<0 W`,
    or non-progressive `>10%` jumps >=35 W above both immediate neighbors). Sessions with `>50`
    artifacts are hard-locked out of record curves (`locked_for_records=True`).
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
        if i > 0 and i + 1 < n:
            nbr_max = max(prev_w, next_w)
            # Isolated 1s spike: non-progressive >10% jump AND >=35 W above BOTH immediate neighbors
            if nbr_max >= 80.0 and w > nbr_max * 1.10 and (w - nbr_max) >= 35.0:
                spikes += 1
                cleaned[i] = round(0.5 * (prev_w + next_w), 1)
                continue
            # Isolated 2s spike: i and i+1 both jump >10% & >=35 W above prev_w, then i+2 drops back
            if i + 2 < n and prev_w >= 80.0 and w > prev_w * 1.10 and (w - prev_w) >= 35.0:
                next2_w = cleaned[i + 2]
                if next_w > prev_w * 1.10 and next2_w <= prev_w * 1.10 and abs(next2_w - prev_w) <= max(35.0, 0.15 * prev_w):
                    spikes += 1
                    cleaned[i] = round(0.5 * (prev_w + next2_w), 1)
                    continue
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

    # Creatine supplementation (+~10-15% intramuscular PCr buffer) slightly extends alactic t_alac in Meixner 15s sprint calibration
    eff_t_alac = max(2.0, min(5.5, float(t_alac_s or 3.0) + (0.35 if creatine_flag is True else 0.0)))
    delta_la = max(1.2, (15.0 * p15_eff + 551.0 - 123.0 * float(ffm)) / (11.0 * float(ffm)))
    # Alactic/PCr-influenced 15s sprint proxy (reflects explosive W'_FC + glycolytic recruitment)
    vlamax_sprint = round(max(0.20, min(1.35, delta_la / (15.0 - eff_t_alac))), 2)

    conf = "high" if (body.get("kfa_calibrated") and anchor_status in ("fresh_42d", "anchored_90d")) else "low_provisional"
    if anchor_status == "passive_lower_bound":
        conf = "low_passive_submax"

    if not vo2max_rel_clean or vo2max_rel_clean <= 20.0:
        return {
            "vlamax_sprint_proxy": vlamax_sprint,
            "vlamax_met_mmol_l_s": None,
            "vlamax_confidence": conf,
            "sprint_anchor_status": anchor_status,
            "creatine_noted": creatine_flag is not None,
            "p_mlss_mader_w": None,
            "p_mlss_w": None,
            "p_lt1_w": None,
            "shuttle_index_eta": None,
            "shuttle_class": None,
            "rcp_langley_w_kg": None,
            "p_fatmax_w": None,
            "fatmax_band_w": None,
            "mfo_g_min": None,
            "p_carbmax_90_w": None,
            "optimal_cadence_rpm": {"lt1": 66, "fatmax": 76, "mlss": 82, "vo2max": 85, "sprint": 135},
        }

    p_mlss_mader = _solve_mader_mlss_watts(float(mass), float(vo2max_rel_clean), vlamax_sprint, vol_rel)

    eta = None
    shuttle_cls = None
    vlamax_met = round(min(0.85, max(0.25, vlamax_sprint * 0.72)), 2)
    if cp_watts and cp_watts > 50 and p_mlss_mader and p_mlss_mader > 30:
        # Inverse Mader equilibrium calibration: separate true metabolic vLamax_met from alactic/PCr sprint peak!
        # Solve iteratively for (vLamax_met, MLSS) because MLSS/CP itself depends on vLamax_met (`f(CP, vLamax_met)`).
        v_est = 0.55
        for _ in range(6):
            mlss_frac_iter = max(0.90, min(0.975, 0.985 - 0.065 * v_est))
            target_mlss = mlss_frac_iter * float(cp_watts)
            v_lo, v_hi = 0.20, 1.15
            for _ in range(36):
                v_mid = 0.5 * (v_lo + v_hi)
                pm = _solve_mader_mlss_watts(float(mass), float(vo2max_rel_clean), v_mid, vol_rel) or target_mlss
                if pm > target_mlss:
                    v_lo = v_mid  # higher vLamax lowers MLSS
                else:
                    v_hi = v_mid
            v_est = 0.5 * (v_lo + v_hi)
        # Clamp metabolic vLamax within physiological bounds and <= sprint proxy when sprint is calibrated
        if anchor_status != "passive_lower_bound":
            v_est = min(v_est, vlamax_sprint)
        vlamax_met = round(max(0.22, min(0.95, v_est)), 2)

        target_mlss_final = max(0.90, min(0.975, 0.985 - 0.065 * vlamax_met)) * float(cp_watts)
        eta = round(target_mlss_final / p_mlss_mader, 2)
        if eta > 1.15:
            shuttle_cls = "High MCT1/Type-IIa Clearance (Allrounder)"
        elif eta <= 1.00:
            shuttle_cls = "Low Lactate Clearance (Aerobic Base Priority)"
        else:
            shuttle_cls = "Balanced Lactate Shuttle"

    rcp_w_kg = round(0.802 - 1.851 * vlamax_met + 0.070 * float(vo2max_rel_clean), 2)

    # Stoichiometric MFO (g/min), unified >=95%-MFO FatMax plateau & CarbMax@90g/h using calibrated `vlamax_met`
    best_fat_g_min = 0.0
    best_fat_w = None
    p_carb_90 = None
    fat_sweep: list[tuple[int, float]] = []
    upper_sweep = int(round((cp_watts or p_mlss_mader or 280.0) * 1.08))
    for pw in range(50, max(80, upper_sweep) + 1):
        vo2_ss, vla_eff = _mader_vla_ss(float(pw), float(mass), float(vo2max_rel_clean), vlamax_met)
        c_cho_gh = vla_eff * v_la_l * 0.09008 * 60.0
        f_ox_gmin = 0.50 * (float(mass) / 1000.0) * max(0.0, (vo2_ss - 3.5) - (vla_eff * vol_rel) / 0.02049)
        fat_sweep.append((pw, f_ox_gmin))
        if f_ox_gmin > best_fat_g_min:
            best_fat_g_min = f_ox_gmin
            best_fat_w = pw
        if p_carb_90 is None and c_cho_gh >= 90.0:
            p_carb_90 = pw
    mader_95_pts = [pw for pw, fx in fat_sweep if best_fat_g_min > 0 and fx >= 0.95 * best_fat_g_min]
    mader_95_band = (min(mader_95_pts), max(mader_95_pts)) if mader_95_pts else None

    # Dynamic phenotype-scaled LT1 & MLSS anchors `f(CP, vLamax_met)`, while FatMax is strictly unified
    # to the recalibrated Mader `F_Ox(P)` >=95%-MFO plateau (no parallel `% CP` heuristic!).
    p_mlss_dyn = None
    p_lt1_dyn = None
    fatmax_band = mader_95_band
    p_fatmax_unified = best_fat_w
    if cp_watts and cp_watts > 50:
        cp_f = float(cp_watts)
        mlss_frac = max(0.90, min(0.975, 0.985 - 0.065 * vlamax_met))
        lt1_frac = max(0.68, min(0.84, 0.865 - 0.14 * vlamax_met))
        p_mlss_dyn = int(round(cp_f * mlss_frac))
        p_lt1_dyn = int(round(cp_f * lt1_frac))

    return {
        "vlamax_sprint_proxy": vlamax_sprint,
        "vlamax_met_mmol_l_s": vlamax_met,
        "vlamax_confidence": conf,
        "sprint_anchor_status": anchor_status,
        "t_alac_s": eff_t_alac,
        "creatine_noted": creatine_flag is not None,
        "p_mlss_mader_w": int(round(p_mlss_mader)) if p_mlss_mader else None,
        "p_mlss_w": p_mlss_dyn or (int(round(p_mlss_mader)) if p_mlss_mader else None),
        "p_lt1_w": p_lt1_dyn,
        "shuttle_index_eta": eta,
        "shuttle_class": shuttle_cls,
        "rcp_langley_w_kg": rcp_w_kg,
        "p_fatmax_w": p_fatmax_unified,
        "fatmax_band_w": fatmax_band,
        "mader_mfo_peak_w": best_fat_w,
        "mader_fatmax_95_band_w": mader_95_band,
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
    myotype_z: float | None = None,
) -> dict[str, Any]:
    """Compute OLS Standard Error (`SEE_CP %`, `SEE_W' %`), `MPR`, `APR`, and OmniPD decay `A`.

    When no true `>30 min` all-out anchor (`60m`) is present, derives a deterministic Myotype/`W'`-coupled
    fallback for `omni_decay_a_w` (`omni_decay_source='myotype_wp_fallback'`).
    """
    cp_w = cp_model.get("cp_w")
    wp_j = cp_model.get("w_prime_j")
    if not cp_w or not wp_j or cp_w <= 0 or wp_j <= 0:
        return {
            "see_cp_pct": None,
            "see_wp_pct": None,
            "mpr_w": None,
            "apr_w": None,
            "omni_decay_a_w": None,
            "omni_decay_source": None,
        }

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

    # OmniPD decay term A (W) for t > TCP_max (1800s): empirical 60m anchor when valid, else Myotype/W' fallback
    omni_a = None
    omni_src = None
    p60 = float(mmp_dict.get("60m") or 0.0)
    pred_3600 = float(cp_w) + (float(wp_j) / 3600.0)
    # Require realistic maximal 60m effort (>= 75% CP) to avoid passive Z2 60m inflating A
    if (0.75 * float(cp_w)) <= p60 < pred_3600:
        ln_term = math.log(3600.0 / 1800.0)
        if ln_term > 0:
            omni_a = round(max(0.0, (pred_3600 - p60) / ln_term), 1)
            omni_src = "empirical_60m"
    if omni_a is None:
        wp_s = float(wp_j) / float(cp_w)
        z_term = 0.012 * max(-1.5, min(3.0, float(myotype_z))) if myotype_z is not None else 0.0
        a_frac = max(0.032, min(0.15, 0.042 + 0.00085 * (wp_s - 45.0) + z_term))
        omni_a = round(float(cp_w) * a_frac, 1)
        omni_src = "myotype_wp_fallback"

    return {
        "see_cp_pct": see_cp_pct,
        "see_wp_pct": see_wp_pct,
        "mpr_w": mpr,
        "apr_w": apr,
        "omni_decay_a_w": omni_a,
        "omni_decay_source": omni_src,
    }


def predict_omnipd_power(
    t_sec: float,
    cp_watts: float,
    w_prime_j: float,
    omni_decay_a_w: float = 15.0,
    p_max_w: float | None = None,
    tcp_max_s: float = 1800.0,
) -> float:
    """Puchowicz (2020) OmniPD power-duration curve across sprint, severe, and >TCP_max durations."""
    t = max(1.0, float(t_sec))
    cp = max(50.0, float(cp_watts))
    wp = max(1000.0, float(w_prime_j))
    p_max = float(p_max_w) if (p_max_w and float(p_max_w) > cp) else (cp * 3.5)
    exp_term = 1.0 - math.exp(-t * (p_max - cp) / wp)
    p_base = (wp / t) * exp_term + cp
    if t > tcp_max_s and omni_decay_a_w > 0:
        p_base -= float(omni_decay_a_w) * math.log(t / float(tcp_max_s))
    return round(max(0.0, p_base), 1)


def chorley_recovery_taus(pct_d_cp: float, tau_mult: float = 1.0) -> tuple[float, float]:
    """Chorley et al. (2023) bi-exponential `W'` recovery time constants `(tau_FC_s, tau_SC_s)`."""
    d_pct = max(0.0, min(100.0, float(pct_d_cp)))
    mult = max(0.5, min(2.5, float(tau_mult or 1.0)))
    tau_fc = (45.0 * math.exp(-0.014 * d_pct) + 9999.0 * math.exp(-0.811 * d_pct)) * mult
    tau_sc = (9999.0 * math.exp(-0.098 * d_pct) + 429.0) * mult
    return tau_fc, tau_sc


def compute_chorley_w_bal(
    watts: list[float],
    cp_watts: float,
    w_prime_j: float = 20000.0,
    fc_fraction: float = 0.45,
    dt_s: float = 1.0,
    tau_mult: float = 1.0,
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
    max_tot = float(w_prime_j)
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
            fc_bal = max(0.0, min(fc_amp, fc_bal - take_fc))
            sc_bal = max(0.0, min(sc_amp, sc_bal - take_sc))
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
            tau_fc, tau_sc = chorley_recovery_taus(pct_d_cp, tau_mult=tau_mult)
            fc_bal = max(0.0, min(fc_amp, fc_amp - (fc_amp - fc_bal) * math.exp(-dt_s / max(15.0, tau_fc))))
            sc_bal = max(0.0, min(sc_amp, sc_amp - (sc_amp - sc_bal) * math.exp(-dt_s / max(60.0, tau_sc))))
            w_tot = fc_bal + sc_bal
            if w_tot > max_tot:
                max_tot = w_tot
            sub_streak += dt_s
            if sub_streak >= 10.0:
                bout_above_j = 0.0
                bout_counted = False
            if w_tot >= 0.65 * w_prime_j:
                deep_armed = True

    tau_fc_50, tau_sc_50 = chorley_recovery_taus(50.0, tau_mult=tau_mult)
    return {
        "w_bal_min_pct": round((min_tot / w_prime_j) * 100.0, 1),
        "w_bal_min_j": int(round(min_tot)),
        "w_bal_end_j": int(round(fc_bal + sc_bal)),
        "w_bal_max_j": int(round(max_tot)),
        "fc_end_j": int(round(fc_bal)),
        "sc_end_j": int(round(sc_bal)),
        "matches_burned": matches,
        "kj_above_cp": round(total_above_j / 1000.0, 1),
        "kj_sub_cp": round(total_sub_j / 1000.0, 1),
        "tau_fc_50pct_s": round(tau_fc_50, 1),
        "tau_sc_50pct_s": round(tau_sc_50, 1),
    }


def expand_spec_to_1hz_watts(
    parsed_spec: dict[str, Any] | None,
    cp_watts: float,
) -> tuple[list[float], list[float]]:
    """Expand parsed workout segments (`parse_workout_spec`) into `(all_watts_1hz, work_block_watts_1hz)`."""
    eff_cp = max(80.0, float(cp_watts or 275.0))
    segs = (parsed_spec or {}).get("segments") or (parsed_spec or {}).get("blocks") or []
    watts: list[float] = []
    work_watts: list[float] = []

    for blk in segs:
        kind = str(blk.get("type") or blk.get("kind") or "steady").lower()
        if kind == "intervals":
            reps = max(1, int(blk.get("repeat") or blk.get("repeats") or 1))
            on_s = max(1, int(blk.get("on_sec") or 60))
            off_s = max(0, int(blk.get("off_sec") or 0))
            on_w = float(blk.get("on_p") or blk.get("on_power") or 1.0) * eff_cp
            off_w = float(blk.get("off_p") or blk.get("off_power") or 0.55) * eff_cp
            for r_idx in range(reps):
                seg_on = [on_w] * on_s
                watts.extend(seg_on)
                work_watts.extend(seg_on)
                if off_s > 0:
                    seg_off = [off_w] * off_s
                    watts.extend(seg_off)
                    # Only include short micro-rests/floats (<=60s, e.g. 30/15 or 40/20) in work-block avg;
                    # exclude long passive recoveries (>60s, e.g. 4m@160W between 4x10m MLSS) so threshold work isn't misclassified as Z2.
                    if off_s <= 60:
                        work_watts.extend(seg_off)
        elif kind == "compound_intervals":
            # Multi-stage intervals such as Hard-Start `4x (45s@420W + 195s@350W) / 5m@147W`
            reps = max(1, int(blk.get("repeat") or 1))
            stages = blk.get("stages") or []
            off_s = max(0, int(blk.get("off_sec") or 0))
            off_w = float(blk.get("off_p") or 0.50) * eff_cp
            for r_idx in range(reps):
                for stg in stages:
                    s_sec = max(1, int(stg.get("sec") or 30))
                    s_w = float(stg.get("p") or 1.0) * eff_cp
                    seg_s = [s_w] * s_sec
                    watts.extend(seg_s)
                    work_watts.extend(seg_s)
                if off_s > 0 and r_idx < reps - 1:
                    watts.extend([off_w] * off_s)
        elif kind in ("warmup", "cooldown", "ramp"):
            dur_s = max(1, int(blk.get("sec") or blk.get("duration_sec") or 600))
            w_lo = float(blk.get("p_low") or blk.get("power_low") or 0.55) * eff_cp
            w_hi = float(blk.get("p_high") or blk.get("power_high") or 0.72) * eff_cp
            if dur_s == 1:
                arr = [0.5 * (w_lo + w_hi)]
            else:
                arr = [w_lo + (w_hi - w_lo) * (i / (dur_s - 1)) for i in range(dur_s)]
            watts.extend(arr)
            if kind == "ramp":
                work_watts.extend(arr)
        else:
            dur_s = max(1, int(blk.get("sec") or blk.get("duration_sec") or 600))
            pwr = float(blk.get("p") or blk.get("power") or 0.68) * eff_cp
            arr = [pwr] * dur_s
            watts.extend(arr)
            if pwr >= 0.75 * eff_cp:
                work_watts.extend(arr)

    return watts, work_watts


def _interp_mateo_march_sup_drop(w_sup_kj_kg: float, is_female: bool = False) -> float:
    """Mateo-March et al. (2024 / 2025 / 2026) intensity-weighted supra-CP CP-drop curve.

    Male anchors (`kJ/kg > CP -> CP drop`): `2.5 -> 2.2%`, `5.0 -> 6.1%`, `7.5 -> 16.2%`.
    Female anchors (sex-specific supra-CP paradox): steeper curve hitting `29.6%` at `5.0 kJ/kg > CP`.
    """
    x = max(0.0, float(w_sup_kj_kg))
    if x <= 0.0:
        return 0.0
    pts = (
        [(0.0, 0.0), (2.5, 0.090), (5.0, 0.296), (7.5, 0.380)]
        if is_female
        else [(0.0, 0.0), (2.5, 0.022), (5.0, 0.061), (7.5, 0.162)]
    )
    for idx in range(len(pts) - 1):
        x0, y0 = pts[idx]
        x1, y1 = pts[idx + 1]
        if x <= x1:
            t = (x - x0) / max(1e-6, x1 - x0)
            return y0 + t * (y1 - y0)
    last_x, last_y = pts[-1]
    slope = 0.015 if is_female else 0.022
    cap = 0.44 if is_female else 0.35
    return min(cap, last_y + (x - last_x) * slope)


def compute_effective_cp_w_prime(
    cp_watts: float,
    w_prime_j: float,
    mass_kg: float = 75.0,
    prior_kj: float = 0.0,
    cho_g_h: float = 60.0,
    verified_drop_15kj_pct: float | None = None,
    form_pct: float = 0.0,
    prior_sup_kj: float = 0.0,
    sex: str = "male",
    elev_m: float = 0.0,
    temp_c: float | None = None,
    heat_accl_days: int = 0,
    indoor: bool = False,
    indoor_cp_offset_w: float = -19.0,
    drivetrain_loss_pct: float = 0.0,
) -> dict[str, float]:
    """Apply intensity-weighted pre-fatigue (`w_sub` vs. `w_sup` + sex scaling), daily form (`form_pct`),
    Townsend altitude, heat/acclimatization, indoor physiology offset, and drivetrain loss to `(CP, W')`."""
    cp_fresh = max(80.0, float(cp_watts or 275.0))
    wp_fresh = max(4000.0, float(w_prime_j or 18000.0))
    bw = max(40.0, float(mass_kg or 75.0))
    is_female = str(sex or "male").strip().lower() in ("f", "female", "w", "weiblich")

    form_mult = max(0.80, min(1.10, 1.0 + float(form_pct or 0.0) / 100.0))
    cp_base = cp_fresh * form_mult
    wp_base = wp_fresh * form_mult

    # 1. Intensity-weighted pre-fatigue: separate `w_sub` (<=CP) from `w_sup` (>CP) without double-counting
    w_sup_kj = max(0.0, float(prior_sup_kj or 0.0))
    tot_kj = max(0.0, float(prior_kj or 0.0))
    w_sub_kj = max(0.0, tot_kj - w_sup_kj) if tot_kj > 0.0 else 0.0
    w_sub_kj_kg = w_sub_kj / bw
    w_sup_kj_kg = w_sup_kj / bw

    sub_cp_drop = 0.0
    sub_wp_drop = 0.0
    if w_sub_kj > 0.0:
        excess = max(0.0, w_sub_kj_kg - 12.0)
        # Fueling modulation: under-fueling (<60 g/h) accelerates glycogen/W' decay; 80-100 g/h dampens it
        fuel_mult = max(0.65, min(1.55, 1.0 + (60.0 - float(cho_g_h)) * 0.012))
        # Pastorio et al. (2025/2026): trained females preserve sub-CP thresholds better than males
        sub_sex_mult = 0.65 if is_female else 1.0
        base_cp_rate = 0.0020
        if verified_drop_15kj_pct is not None and verified_drop_15kj_pct > 0:
            base_cp_rate = max(0.0012, min(0.0040, (float(verified_drop_15kj_pct) / 100.0) * 0.25))
        sub_cp_drop = min(0.18, excess * base_cp_rate * fuel_mult * sub_sex_mult)
        sub_wp_drop = min(0.35, excess * 0.0055 * fuel_mult * sub_sex_mult)

    sup_cp_drop = 0.0
    sup_wp_drop = 0.0
    if w_sup_kj > 0.0:
        raw_sup_drop = _interp_mateo_march_sup_drop(w_sup_kj_kg, is_female=is_female)
        sup_fuel_mult = max(0.85, min(1.18, 1.0 + (60.0 - float(cho_g_h)) * 0.004))
        sup_cp_drop = min(0.45, raw_sup_drop * sup_fuel_mult)
        sup_wp_drop = min(0.55, sup_cp_drop * 1.65)

    cp_eff = cp_base * (1.0 - sub_cp_drop) * (1.0 - sup_cp_drop)
    wp_eff = wp_base * (1.0 - sub_wp_drop) * (1.0 - sup_wp_drop)

    # 2. Townsend 2-Segment Altitude (`CP` >1500m, `W'` 100% up to 2760m, `tau_W'` 1.35-1.50x at 3000m)
    tau_w_mult = 1.0
    alt_cp_loss_pct = 0.0
    alt_wp_pct = 100.0
    if elev_m and float(elev_m) > 1500.0:
        tw = compute_townsend_altitude(float(elev_m), cp_eff, wp_eff / 1000.0)
        alt_cp_loss_pct = float(tw.get("cp_loss_pct") or 0.0)
        alt_wp_pct = float(tw.get("w_prime_pct") or 100.0)
        tau_w_mult = float(tw.get("tau_w_altitude_mult") or 1.0)
        cp_eff *= (1.0 - alt_cp_loss_pct / 100.0)
        wp_eff *= (alt_wp_pct / 100.0)

    # 3. Heat Stress & 14-day Heat Acclimatization (`-0.5%/°C` >25°C unacclimatized)
    heat_cp_loss_pct = 0.0
    if temp_c is not None and float(temp_c) > 25.0:
        accl_days = max(0.0, min(14.0, float(heat_accl_days or 0)))
        accl_factor = max(0.10, 1.0 - (accl_days / 14.0) * 0.90)
        heat_drop = min(0.22, (float(temp_c) - 25.0) * 0.005 * accl_factor)
        heat_cp_loss_pct = round(heat_drop * 100.0, 2)
        cp_eff *= (1.0 - heat_drop)
        wp_eff *= (1.0 - 0.30 * heat_drop)

    # 4. Indoor physiology offset (Lipski 2022 default -19 W) & Drivetrain sensor loss (2.4% without PowerMatch)
    if indoor:
        cp_eff = max(80.0, cp_eff + float(indoor_cp_offset_w))
    dt_loss = max(0.0, min(10.0, float(drivetrain_loss_pct or 0.0)))
    if dt_loss > 0.0:
        cp_eff *= (1.0 - dt_loss / 100.0)
        wp_eff *= (1.0 - dt_loss / 100.0)

    cp_drop_pct = round(max(0.0, (1.0 - cp_eff / cp_fresh) * 100.0), 1)
    wp_drop_pct = round(max(0.0, (1.0 - wp_eff / wp_fresh) * 100.0), 1)
    return {
        "cp_eff_w": cp_eff,
        "wp_eff_j": wp_eff,
        "cp_drop_pct": cp_drop_pct,
        "wp_drop_pct": wp_drop_pct,
        "w_sub_kj_kg": round(w_sub_kj_kg, 2),
        "w_sup_kj_kg": round(w_sup_kj_kg, 2),
        "sup_cp_drop_pct": round(sup_cp_drop * 100.0, 1),
        "alt_cp_loss_pct": round(alt_cp_loss_pct, 1),
        "alt_wp_pct": round(alt_wp_pct, 1),
        "heat_cp_loss_pct": round(heat_cp_loss_pct, 1),
        "tau_w_mult": round(tau_w_mult, 2),
    }


def simulate_workout_stream(
    watts: list[float],
    cp_watts: float,
    w_prime_j: float,
    p_5min_w: float | None = None,
    mass_kg: float = 75.0,
    prior_kj: float = 0.0,
    cho_g_h: float = 60.0,
    verified_drop_15kj_pct: float | None = None,
    title: str = "",
    work_block_watts: list[float] | None = None,
    form_pct: float = 0.0,
    creatine_flag: bool | None = None,
    prior_sup_kj: float = 0.0,
    sex: str = "male",
    elev_m: float = 0.0,
    temp_c: float | None = None,
    heat_accl_days: int = 0,
    indoor: bool = False,
    indoor_cp_offset_w: float = -19.0,
    drivetrain_loss_pct: float = 0.0,
    tau_w_mult: float | None = None,
) -> dict[str, Any]:
    """1-Hz deterministic Chorley bi-exponential `W'bal` (`W'_FC` + `W'_SC`), 1st-order `VO2(t)` kinetics
    with severe-domain slow component, intensity-weighted pre-fatigue `(w_sub, w_sup, cho_g_h, sex)`,
    environment/hardware offsets, and physiological stimulus / mismatch guard."""
    cp_fresh = max(80.0, float(cp_watts or 275.0))
    wp_fresh = max(4000.0, float(w_prime_j or 18000.0))
    bw = max(40.0, float(mass_kg or 75.0))

    # Guardrail #5: Intensity-weighted pre-fatigue + environment/sensor/form scaling of (CP_eff, W'_eff)
    eff = compute_effective_cp_w_prime(
        cp_watts=cp_fresh,
        w_prime_j=wp_fresh,
        mass_kg=bw,
        prior_kj=prior_kj,
        cho_g_h=cho_g_h,
        verified_drop_15kj_pct=verified_drop_15kj_pct,
        form_pct=form_pct,
        prior_sup_kj=prior_sup_kj,
        sex=sex,
        elev_m=elev_m,
        temp_c=temp_c,
        heat_accl_days=heat_accl_days,
        indoor=indoor,
        indoor_cp_offset_w=indoor_cp_offset_w,
        drivetrain_loss_pct=drivetrain_loss_pct,
    )
    cp_eff = eff["cp_eff_w"]
    wp_eff = eff["wp_eff_j"]
    cp_drop_pct = eff["cp_drop_pct"]
    wp_drop_pct = eff["wp_drop_pct"]
    eff_tau_mult = float(tau_w_mult) if tau_w_mult is not None else float(eff.get("tau_w_mult") or 1.0)

    p5_eff = float(p_5min_w) if (p_5min_w and float(p_5min_w) > cp_fresh) else (cp_fresh + wp_fresh / 300.0)
    p5_deg = p5_eff * (cp_eff / cp_fresh)
    p_vo2max = max(cp_eff * 1.15, p5_deg * 0.92)

    # Chorley bi-exponential pools: fast PCr/myoglobin pool (`fc`, slightly elevated with creatine) + slow glycolytic pool (`sc`)
    fc_frac = 0.43 if creatine_flag is True else 0.40
    fc_amp = fc_frac * wp_eff
    sc_amp = (1.0 - fc_frac) * wp_eff
    fc_bal = fc_amp
    sc_bal = sc_amp
    vo2_frac = 0.15

    min_tot = wp_eff
    min_fc = fc_amp
    t_ge_90_s = 0
    peak_vo2 = 0.0
    blowup_s: int | None = None
    kj_above = 0.0
    total_kj = 0.0

    for idx, raw_p in enumerate(watts or []):
        p = max(0.0, float(raw_p or 0.0))
        total_kj += p / 1000.0
        if p > cp_eff:
            burn = p - cp_eff
            kj_above += burn / 1000.0
            take_fc = min(fc_bal, burn * 0.65)
            rem = burn - take_fc
            take_sc = min(sc_bal, rem)
            leftover = rem - take_sc
            if leftover > 0.0:
                take_fc = min(fc_bal, take_fc + leftover)
            fc_bal = max(0.0, min(fc_amp, fc_bal - take_fc))
            sc_bal = max(0.0, min(sc_amp, sc_bal - take_sc))
        else:
            pct_d_cp = max(0.0, min(100.0, ((cp_eff - p) / cp_eff) * 100.0))
            tau_fc, tau_sc = chorley_recovery_taus(pct_d_cp, tau_mult=eff_tau_mult)
            fc_bal = max(0.0, min(fc_amp, fc_amp - (fc_amp - fc_bal) * math.exp(-1.0 / max(15.0, tau_fc))))
            sc_bal = max(0.0, min(sc_amp, sc_amp - (sc_amp - sc_bal) * math.exp(-1.0 / max(60.0, tau_sc))))

        wtot = fc_bal + sc_bal
        if wtot < min_tot:
            min_tot = wtot
        if fc_bal < min_fc:
            min_fc = fc_bal
        if blowup_s is None and wtot <= 100.0:
            blowup_s = idx

        # Guardrail #4: 1st-order VO2(t) kinetics (tau_on ~ 22-30s, tau_off ~ 35s) + severe slow component
        u_prim = 0.08 + 0.92 * (p / max(100.0, p_vo2max))
        w_dep = max(0.0, 1.0 - (wtot / wp_eff))
        fc_dep = max(0.0, 1.0 - (fc_bal / fc_amp))
        slow_c = (0.14 * w_dep) if p > cp_eff else (0.04 * w_dep)
        u_tgt = min(1.0, max(0.08, u_prim + slow_c))
        tau_vo2 = (30.0 - 8.0 * fc_dep) if u_tgt >= vo2_frac else 35.0
        vo2_frac += (u_tgt - vo2_frac) / max(15.0, tau_vo2)
        if vo2_frac > peak_vo2:
            peak_vo2 = vo2_frac
        if vo2_frac >= 0.90:
            t_ge_90_s += 1

    n_s = len(watts or [])
    avg_w = round(sum(watts) / n_s, 1) if n_s else 0.0
    wb = work_block_watts if work_block_watts else watts
    wb_avg_w = round(sum(wb) / len(wb), 1) if wb else avg_w
    w_min_pct = round((min_tot / wp_eff) * 100.0, 1)
    w_min_kj = round(min_tot / 1000.0, 1)
    fc_min_pct = round((min_fc / fc_amp) * 100.0, 1)
    t90_min = round(t_ge_90_s / 60.0, 1)
    peak_vo2_pct = round(peak_vo2 * 100.0, 1)

    # Blowout guard floor: protect small-W' Diesels (~10 kJ) with >= 3.0 kJ floor (capped at 30% W'), large W' with >= 10% W'
    wp_eff_kj = wp_eff / 1000.0
    safety_floor_kj = round(min(0.30 * wp_eff_kj, max(0.10 * wp_eff_kj, 3.0)), 2)
    unsustainable = (blowup_s is not None) or (w_min_kj < safety_floor_kj and kj_above > 5.0)

    if unsustainable:
        stim_class = "UNSUSTAINABLE_BLOWOUT"
    elif t90_min >= 7.0 and w_min_pct <= 45.0:
        stim_class = "TRUE_VO2MAX_SEVERE"
    elif kj_above >= 10.0 and w_min_pct <= 45.0 and t90_min < 7.0:
        stim_class = "ANAEROBIC_CAPACITY_LOW_VO2"
    elif kj_above >= 4.0 or wb_avg_w >= 0.88 * cp_eff:
        stim_class = "THRESHOLD_OR_SHUTTLE_OPENER"
    elif avg_w >= 0.60 * cp_eff:
        stim_class = "AEROBIC_BASE_Z2"
    else:
        stim_class = "RECOVERY_Z1"

    # Compiler-Guard mismatch detection against workout title intent
    t_low = str(title or "").lower()
    claims_vo2 = any(k in t_low for k in ("vo2", "vo₂", "rønnestad", "ronnestad", "hard-start", "hard start", "map"))
    claims_opener = any(k in t_low for k in ("opener", "re-entry", "reentry", "threshold", "shuttle", "z1", "z2", "base", "recovery"))
    mismatch = ""
    if unsustainable:
        where_s = f" at t={blowup_s // 60}m{blowup_s % 60:02d}s" if blowup_s is not None else ""
        mismatch = (
            f"BLOWOUT WARNING: W'bal drops to {w_min_pct}% ({w_min_kj} kJ < {safety_floor_kj} kJ safety floor){where_s} "
            "— reduce On-power or add recovery!"
        )
    elif claims_vo2 and not claims_opener:
        if t90_min < 7.0:
            mismatch = (
                f"STIMULUS MISMATCH: Title claims VO2max, but Time >=90% VO2max is only {t90_min} min (< 7.0 min) "
                f"with W'bal_min={w_min_pct}% ({w_min_kj} kJ) -> actual stimulus is {stim_class}."
            )
        elif w_min_pct > 55.0:
            mismatch = (
                f"STIMULUS MISMATCH: Title claims VO2max, but W'bal_min stays at {w_min_pct}% (>55%) -> "
                "this is intermittent threshold, not severe VO2max."
            )

    tau_fc_50, tau_sc_50 = chorley_recovery_taus(50.0, tau_mult=eff_tau_mult)
    return {
        "duration_min": round(n_s / 60.0, 1),
        "cp_eff_w": round(cp_eff, 1),
        "wp_eff_kj": round(wp_eff / 1000.0, 1),
        "pre_fatigue_kj": round(float(prior_kj or 0.0), 0),
        "pre_fatigue_sup_kj": round(float(prior_sup_kj or 0.0), 0),
        "cho_g_h": round(float(cho_g_h or 60.0), 0),
        "cp_drop_pct": cp_drop_pct,
        "wp_drop_pct": wp_drop_pct,
        "tau_w_mult": round(eff_tau_mult, 2),
        "tau_fc_50pct_s": round(tau_fc_50, 1),
        "tau_sc_50pct_s": round(tau_sc_50, 1),
        "avg_w": avg_w,
        "work_block_avg_w": wb_avg_w,
        "total_kj": round(total_kj, 0),
        "kj_above_cp": round(kj_above, 1),
        "w_bal_min_pct": w_min_pct,
        "w_bal_min_kj": w_min_kj,
        "fc_pcr_min_pct": fc_min_pct,
        "time_ge_90_vo2max_min": t90_min,
        "peak_vo2_pct": peak_vo2_pct,
        "blowup_s": blowup_s,
        "stimulus_class": stim_class,
        "stimulus_mismatch": mismatch,
    }


def solve_individual_interval_prescriptions(
    cp_watts: float | None,
    w_prime_j: float | None,
    p_5min_w: float | None = None,
    p2_metabolic: dict[str, Any] | None = None,
    p4_myotype: dict[str, Any] | None = None,
    mass_kg: float | None = 75.0,
    prior_kj: float = 0.0,
    cho_g_h: float = 60.0,
    verified_drop_15kj_pct: float | None = None,
    form_pct: float = 0.0,
    creatine_flag: bool | None = None,
    prior_sup_kj: float = 0.0,
    sex: str = "male",
    elev_m: float = 0.0,
    temp_c: float | None = None,
    heat_accl_days: int = 0,
    indoor: bool = False,
    indoor_cp_offset_w: float = -19.0,
    drivetrain_loss_pct: float = 0.0,
) -> dict[str, Any]:
    """Constrained 1-Hz inverse `W'bal` + `VO2(t)` prescription solver individualized to `(CP, W', vLamax_met, Z_Myo)`
    with intensity-weighted pre-fatigue `(prior_kj, prior_sup_kj, cho_g_h, sex)`, environment/hardware offsets,
    and daily form scaling (`form_pct`).

    Enforces:
      - Diesel safety floor: `target_reserve >= max(0.20 * W', 3.0 kJ)`
      - Aerobic IIx-cap on `30/15s`: never pushes `P_on` above `min(0.98 * P_5m, CP + 0.35 * W'/100)` —
        automatically raises `P_off` (Float) instead of driving `P_on` into an anaerobic sprint trap!
      - 2-stage Hard-Start `4x4m`: Stage 1 targets `60-70%` fast PCr depletion in 45s without `W'_SC` flood;
        Stage 2 bisects `P_hold` (3:15m) to hit target reserve in Set 4.
      - Controlled `1:4` Lactate-Shuttle: anchors `4m Under` at `80-86% CP` (near LT1) and bounds `1m Over`
        to `15-20% W'` per bout (preventing `460-500W` IIx blasts).
    """
    if not cp_watts or float(cp_watts) < 80.0 or not w_prime_j or float(w_prime_j) < 4000.0:
        return {}

    cp_fresh = float(cp_watts)
    wp_fresh = float(w_prime_j)
    bw = max(40.0, float(mass_kg or 75.0))
    p5_fresh = float(p_5min_w) if (p_5min_w and float(p_5min_w) > cp_fresh) else (cp_fresh + wp_fresh / 300.0)
    p2 = p2_metabolic or {}

    eff = compute_effective_cp_w_prime(
        cp_watts=cp_fresh,
        w_prime_j=wp_fresh,
        mass_kg=bw,
        prior_kj=prior_kj,
        cho_g_h=cho_g_h,
        verified_drop_15kj_pct=verified_drop_15kj_pct,
        form_pct=form_pct,
        prior_sup_kj=prior_sup_kj,
        sex=sex,
        elev_m=elev_m,
        temp_c=temp_c,
        heat_accl_days=heat_accl_days,
        indoor=indoor,
        indoor_cp_offset_w=indoor_cp_offset_w,
        drivetrain_loss_pct=drivetrain_loss_pct,
    )
    cp = eff["cp_eff_w"]
    wp = eff["wp_eff_j"]
    tau_mult = float(eff.get("tau_w_mult") or 1.0)
    scale_ratio = cp / cp_fresh
    p5 = p5_fresh * scale_ratio

    # Guardrail #3b: Diesel safety floor (`max(0.20 * W', 3.0 kJ)`, capped at 0.45*W' only for ultra-tiny W')
    target_res_j = min(0.45 * wp, max(0.20 * wp, 3000.0))
    target_res_kj = round(target_res_j / 1000.0, 1)

    # Guardrail #3b: IIx-Cap for aerobic VO2max 30/15s
    p_on_iix_cap = min(p5 * 0.98, cp + 0.35 * (wp / 100.0))
    p_on_iix_cap = max(cp * 1.15, p_on_iix_cap)

    def _build_3015(p_on: float, p_off: float, sets: int = 3, reps: int = 13) -> tuple[list[float], list[float]]:
        w = [0.60 * cp] * 600
        wb: list[float] = []
        for s in range(sets):
            for r in range(reps):
                w.extend([p_on] * 30 + [p_off] * 15)
                wb.extend([p_on] * 30 + [p_off] * 15)
            if s < sets - 1:
                w.extend([0.52 * cp] * 300)
        return w, wb

    # 1. Re-Entry / Threshold-Opener 30/15 Float (`119% / 65% CP`, honestly labeled Threshold-Opener)
    op_on = int(round(cp * 1.19))
    op_off = int(round(cp * 0.65))
    op_w, op_wb = _build_3015(op_on, op_off, 3, 13)
    op_sim = simulate_workout_stream(op_w, cp, wp, p5, bw, work_block_watts=op_wb, creatine_flag=creatine_flag, tau_w_mult=tau_mult)

    # 2. VO2max Float 30/15 (`3x13`, `Off = 68-70% CP`, bisected to hit `target_res_j`, bounded by `p_on_iix_cap`)
    fl_off = int(round(cp * 0.70))
    lo, hi = cp * 1.03, p_on_iix_cap
    for _ in range(28):
        mid = 0.5 * (lo + hi)
        sw, swb = _build_3015(mid, fl_off, 3, 13)
        sm = simulate_workout_stream(sw, cp, wp, p5, bw, work_block_watts=swb, creatine_flag=creatine_flag, tau_w_mult=tau_mult)
        if sm["w_bal_min_kj"] * 1000.0 > target_res_j:
            lo = mid
        else:
            hi = mid
    fl_on = int(round(0.5 * (lo + hi)))
    fl_w, fl_wb = _build_3015(fl_on, fl_off, 3, 13)
    fl_sim = simulate_workout_stream(fl_w, cp, wp, p5, bw, work_block_watts=fl_wb, creatine_flag=creatine_flag, tau_w_mult=tau_mult)

    # 3. VO2max Classic 30/15 (`Off = 50% CP`) — if `P_on` hits `p_on_iix_cap`, auto-escalate `P_off` (Float)!
    cl_off = int(round(cp * 0.50))
    lo, hi = cp * 1.03, p_on_iix_cap * 1.15
    for _ in range(28):
        mid = 0.5 * (lo + hi)
        sw, swb = _build_3015(mid, cl_off, 3, 13)
        sm = simulate_workout_stream(sw, cp, wp, p5, bw, work_block_watts=swb, creatine_flag=creatine_flag, tau_w_mult=tau_mult)
        if sm["w_bal_min_kj"] * 1000.0 > target_res_j:
            lo = mid
        else:
            hi = mid
    cl_on_raw = 0.5 * (lo + hi)
    cl_capped = False
    if cl_on_raw > p_on_iix_cap:
        cl_capped = True
        cl_on = int(round(p_on_iix_cap))
        # Auto-raise P_off instead of breaching IIx-Cap!
        o_lo, o_hi = cp * 0.50, cp * 0.82
        for _ in range(24):
            omid = 0.5 * (o_lo + o_hi)
            sw, swb = _build_3015(cl_on, omid, 3, 13)
            sm = simulate_workout_stream(sw, cp, wp, p5, bw, work_block_watts=swb, creatine_flag=creatine_flag, tau_w_mult=tau_mult)
            if sm["w_bal_min_kj"] * 1000.0 > target_res_j:
                o_lo = omid
            else:
                o_hi = omid
        cl_off = int(round(0.5 * (o_lo + o_hi)))
    else:
        cl_on = int(round(cl_on_raw))
    cl_w, cl_wb = _build_3015(cl_on, cl_off, 3, 13)
    cl_sim = simulate_workout_stream(cl_w, cp, wp, p5, bw, work_block_watts=cl_wb, creatine_flag=creatine_flag, tau_w_mult=tau_mult)

    # 4. Guardrail #3c: 2-Stage Hard-Start 4x4m (`45s Start -> 3:15m Hold`, `5m @ 50% CP rest`)
    pcr_pool_j = min(0.22 * wp, 90.0 * bw)
    p_start_raw = cp + (0.62 * pcr_pool_j) / (45.0 * 0.65)
    hs_start = int(round(min(p5 * 1.03, max(cp * 1.15, p_start_raw))))

    def _build_hs4x4(p_s: float, p_h: float) -> tuple[list[float], list[float]]:
        w = [0.60 * cp] * 600
        wb: list[float] = []
        for s in range(4):
            w.extend([p_s] * 45 + [p_h] * 195)
            wb.extend([p_s] * 45 + [p_h] * 195)
            if s < 3:
                w.extend([0.50 * cp] * 300)
        return w, wb

    # Stage 2: Bisect `P_hold` (195s) so Set 4 lands cleanly on `target_res_j`
    h_lo, h_hi = cp * 1.02, min(float(hs_start) - 10.0, p5 * 0.98)
    for _ in range(28):
        hmid = 0.5 * (h_lo + h_hi)
        sw, swb = _build_hs4x4(hs_start, hmid)
        sm = simulate_workout_stream(sw, cp, wp, p5, bw, work_block_watts=swb, creatine_flag=creatine_flag, tau_w_mult=tau_mult)
        if sm["w_bal_min_kj"] * 1000.0 > target_res_j:
            h_lo = hmid
        else:
            h_hi = hmid
    hs_hold = int(round(0.5 * (h_lo + h_hi)))
    hs_w, hs_wb = _build_hs4x4(hs_start, hs_hold)
    hs_sim = simulate_workout_stream(hs_w, cp, wp, p5, bw, work_block_watts=hs_wb, creatine_flag=creatine_flag, tau_w_mult=tau_mult)

    # 5. Guardrail #3a: Controlled 1:4 Lactate-Shuttle (`6x [1m Over / 4m Under]`)
    lt1_base = p2.get("p_lt1_w")
    lt1_w = (float(lt1_base) * scale_ratio) if lt1_base else (cp * 0.78)
    sh_under = int(round(max(cp * 0.80, min(cp * 0.86, lt1_w * 1.05))))
    over_target_j = min(0.17 * wp, 0.28 * cp * 60.0)
    sh_over = int(round(min(p5 * 0.92, max(cp * 1.12, cp + over_target_j / 60.0))))
    sh_w = [0.60 * cp] * 600
    sh_wb: list[float] = []
    for _ in range(6):
        sh_w.extend([sh_over] * 60 + [sh_under] * 240)
        sh_wb.extend([sh_over] * 60 + [sh_under] * 240)
    sh_sim = simulate_workout_stream(sh_w, cp, wp, p5, bw, work_block_watts=sh_wb, creatine_flag=creatine_flag, tau_w_mult=tau_mult)

    # 6. Sub-Threshold / MLSS Builder (`4x10m @ MLSS`)
    mlss_base = p2.get("p_mlss_w")
    mlss_w = int(round(float(mlss_base) * scale_ratio)) if mlss_base else int(round(cp * 0.95))

    tau_fc_50, tau_sc_50 = chorley_recovery_taus(50.0, tau_mult=tau_mult)
    return {
        "cp_fresh_w": round(cp_fresh, 1),
        "wp_fresh_kj": round(wp_fresh / 1000.0, 1),
        "cp_eff_w": round(cp, 1),
        "wp_eff_kj": round(wp / 1000.0, 1),
        "cp_drop_pct": eff["cp_drop_pct"],
        "wp_drop_pct": eff["wp_drop_pct"],
        "prior_kj": round(float(prior_kj or 0.0), 0),
        "prior_sup_kj": round(float(prior_sup_kj or 0.0), 0),
        "cho_g_h": round(float(cho_g_h if cho_g_h is not None else 60.0), 1),
        "form_pct": round(float(form_pct or 0.0), 1),
        "tau_w_mult": round(tau_mult, 2),
        "tau_fc_50pct_s": round(tau_fc_50, 1),
        "tau_sc_50pct_s": round(tau_sc_50, 1),
        "target_reserve_kj": target_res_kj,
        "iix_cap_w": int(round(p_on_iix_cap)),
        "re_entry_opener": {
            "label": "Re-Entry Threshold-Opener 30/15 Float (intermittent CP, NOT VO2max)",
            "on_w": op_on, "on_pct": int(round(op_on / cp_fresh * 100)),
            "off_w": op_off, "off_pct": int(round(op_off / cp_fresh * 100)),
            "block_avg_w": op_sim["work_block_avg_w"],
            "w_bal_min_pct": op_sim["w_bal_min_pct"],
            "t90_min": op_sim["time_ge_90_vo2max_min"],
            "dsl": f"3x (13x 30s@{op_on}W / 15s@{op_off}W) / 5m@{int(round(cp * 0.52))}W",
        },
        "vo2max_float_30_15": {
            "label": "VO2max Float 30/15 (3x13, W'-scaled)",
            "on_w": fl_on, "on_pct": int(round(fl_on / cp_fresh * 100)),
            "off_w": fl_off, "off_pct": int(round(fl_off / cp_fresh * 100)),
            "block_avg_w": fl_sim["work_block_avg_w"],
            "w_bal_min_pct": fl_sim["w_bal_min_pct"],
            "w_bal_min_kj": fl_sim["w_bal_min_kj"],
            "t90_min": fl_sim["time_ge_90_vo2max_min"],
            "dsl": f"3x (13x 30s@{fl_on}W / 15s@{fl_off}W) / 5m@{int(round(cp * 0.52))}W",
        },
        "vo2max_classic_30_15": {
            "label": "VO2max Classic 30/15 (auto-float if IIx-capped)",
            "on_w": cl_on, "on_pct": int(round(cl_on / cp_fresh * 100)),
            "off_w": cl_off, "off_pct": int(round(cl_off / cp_fresh * 100)),
            "iix_capped": cl_capped,
            "block_avg_w": cl_sim["work_block_avg_w"],
            "w_bal_min_pct": cl_sim["w_bal_min_pct"],
            "w_bal_min_kj": cl_sim["w_bal_min_kj"],
            "t90_min": cl_sim["time_ge_90_vo2max_min"],
            "dsl": f"3x (13x 30s@{cl_on}W / 15s@{cl_off}W) / 5m@{int(round(cp * 0.52))}W",
        },
        "vo2max_hard_start_4x4": {
            "label": "VO2max Hard-Start 4x4m (2-Stage PCr+W' solved, 5m rest)",
            "start_w": hs_start, "start_pct": int(round(hs_start / cp_fresh * 100)),
            "hold_w": hs_hold, "hold_pct": int(round(hs_hold / cp_fresh * 100)),
            "block_avg_w": hs_sim["work_block_avg_w"],
            "w_bal_min_pct": hs_sim["w_bal_min_pct"],
            "w_bal_min_kj": hs_sim["w_bal_min_kj"],
            "fc_pcr_min_pct": hs_sim["fc_pcr_min_pct"],
            "t90_min": hs_sim["time_ge_90_vo2max_min"],
            "dsl": f"4x (45s@{hs_start}W + 3.25m@{hs_hold}W) / 5m@{int(round(cp * 0.50))}W",
        },
        "lactate_shuttle_1_4": {
            "label": "1:4 Lactate-Shuttle Over/Under (6x, Under@80-86% CP, Over=15-20% W' pulse)",
            "over_w": sh_over, "over_pct": int(round(sh_over / cp_fresh * 100)),
            "under_w": sh_under, "under_pct": int(round(sh_under / cp_fresh * 100)),
            "w_bal_min_pct": sh_sim["w_bal_min_pct"],
            "dsl": f"6x 1m@{sh_over}W / 4m@{sh_under}W",
        },
        "sub_threshold": {
            "label": "Sub-Threshold / MLSS Builder (4x10m)",
            "target_w": mlss_w, "target_pct": int(round(mlss_w / cp_fresh * 100)),
            "dsl": f"4x 10m@{mlss_w}W / 4m@{int(round(cp * 0.54))}W",
        },
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
            vo2_proto = "W'-scaled Float 30/15s, 2-Stage Hard-Start 4x4m, or 1:4 Lactate-Shuttle (see [3B_prescriptions] — NEVER static % CP!)"
            avoid_proto = "Uncapped 500W 30/15s, static % CP VO2 guesses, or back-to-back HIIT days"
        elif z_myo < -0.50:
            myo_cls = "Slow Myotype"
            rec_hiit_h = "24 h (fast ~20m neuromuscular rebound)"
            vo2_proto = "W'-scaled 2-Stage Hard-Start 4x4m / Bossi Alternating 5x5m (see [3B_prescriptions] — respect 3.0 kJ Diesel W' floor!)"
            avoid_proto = "Mild 30/30 intervals (fast tau_W' drops VO2 kinetics too far during 30s rests)"
        else:
            myo_cls = "Intermediate Myotype"
            rec_hiit_h = "36–48 h"
            vo2_proto = "W'-scaled Pyramidal mix: Sub-Threshold MLSS + Float 30/15s or Hard-Start 4x4m (see [3B_prescriptions])"
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
    hr_max: int | None = None,
    today: dt.date | None = None,
) -> dict[str, Any]:
    """Compute `w_sub` vs `w_sup` (`>CP`), Maximal-Effort-Gated `Delta_CP_30kJ_pct`, cadence fade, and Dual-TID."""
    ref_today = today or dt.date.today()
    cutoff_7d = (ref_today - dt.timedelta(days=7)).isoformat()
    cutoff_28d = (ref_today - dt.timedelta(days=28)).isoformat()
    cutoff_90d = (ref_today - dt.timedelta(days=90)).isoformat()
    bw = float(mass_kg) if (mass_kg and mass_kg > 25.0) else 70.0
    eff_cp = float(cp_watts) if (cp_watts and cp_watts > 50) else 275.0
    eff_hrmax = int(hr_max) if (hr_max and hr_max >= 130) else None

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

    # Maximal-Effort Validity Gate for Durability Tiers (prevents passive Z2 ride censoring bias!)
    # A 5-minute maximal effort (`MAP`) MUST sit strictly in the Severe Domain (`> 1.03 * CP`) and show
    # exhaustion (`W'bal <= 25%` or `HR >= 91% HRmax` or `>= 85% Fresh 5m`). Submaximal Z2/tempo rolls
    # on long endurance rides are classified `submax_untested` — NEVER as a physiological fatigue drop!
    dur_tiers = (curves or {}).get("durability_tiers") or {}
    fresh_5m = float(((curves or {}).get("fresh") or {}).get("5m", {}).get("90d") or 0.0)

    def _eval_tier_5m(tier_key: str) -> dict[str, Any]:
        curve_w = int(round(float(((dur_tiers.get(tier_key) or {}).get("5m") or {}).get("90d") or 0.0)))
        best_verified_w = 0
        best_any_w = curve_w
        for a in activities or []:
            if not isinstance(a, dict) or not a.get("date") or str(a["date"])[:10] < cutoff_90d:
                continue
            if str(a.get("sport") or "cycling").lower() != "cycling" or a.get("device_watts") is False or a.get("locked_for_records"):
                continue
            t_map = (a.get("peaks_durability") or {}).get(tier_key) or {}
            w5 = int(round(float(t_map.get("5m") or 0.0)))
            if w5 <= 0:
                continue
            best_any_w = max(best_any_w, w5)
            above_cp_gate = w5 > (eff_cp * 1.03)
            wbal = a.get("w_bal_min_pct")
            mhr = a.get("max_hr")
            exhausted = (
                (wbal is not None and float(wbal) <= 25.0)
                or (eff_hrmax is not None and mhr is not None and float(mhr) >= 0.91 * eff_hrmax)
                or (fresh_5m > 0 and w5 >= 0.85 * fresh_5m)
                or (wbal is None and mhr is None and w5 > eff_cp * 1.06)
            )
            if above_cp_gate and exhausted:
                best_verified_w = max(best_verified_w, w5)
        # Fallback if curve_w was populated from an external curve fixture without per-activity peaks_durability
        if best_verified_w == 0 and curve_w > eff_cp * 1.06 and not any((a.get("peaks_durability") or {}).get(tier_key) for a in (activities or []) if isinstance(a, dict)):
            best_verified_w = curve_w

        if best_verified_w > 0 and fresh_5m > 0:
            drop_p = round(max(0.0, ((fresh_5m - best_verified_w) / fresh_5m) * 100.0), 1)
            ret_p = round((best_verified_w / fresh_5m) * 100.0, 1)
            return {"watts": best_verified_w, "retention_pct": ret_p, "drop_pct": drop_p, "verified_max": True, "status": "verified_max"}
        if best_any_w > 0:
            return {"watts": best_any_w, "retention_pct": None, "drop_pct": None, "verified_max": False, "status": "submax_untested"}
        return {"watts": None, "retention_pct": None, "drop_pct": None, "verified_max": False, "status": "no_rides"}

    tier_validity = {
        tk: _eval_tier_5m(tk)
        for tk in ("15kj_kg", "25kj_kg", "35kj_kg", "5kj_above_cp_kg")
    }
    v25 = tier_validity["25kj_kg"]
    v35 = tier_validity["35kj_kg"]
    v15 = tier_validity["15kj_kg"]
    # `cp_drop_30kj_pct` ONLY reflects a verified maximal effort at >=25 kJ/kg (never a Z2 cruise!)
    cp_drop_pct = v25["drop_pct"] if v25["verified_max"] else (v35["drop_pct"] if v35["verified_max"] else None)
    verified_drop_15kj_pct = v15["drop_pct"] if v15["verified_max"] else None

    tot_sg = max(1, sg_low + sg_mod + sg_high)
    tot_dom = max(1.0, w_lt1_kj + w_heavy_kj + w_severe_kj)
    return {
        "cp_drop_30kj_pct": cp_drop_pct,
        "verified_drop_15kj_pct": verified_drop_15kj_pct,
        "durability_25kj_status": v25["status"],
        "tier_5m_validity": tier_validity,
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

def _fmt_run_pace(sec_per_km: float | None) -> str:
    if not sec_per_km or sec_per_km <= 0:
        return "—"
    total_s = int(round(float(sec_per_km)))
    return f"{total_s // 60}:{total_s % 60:02d}/km"


def solve_running_cs_d_prime(
    splits: list[tuple[float, float]] | dict[str, float | str],
) -> dict[str, Any]:
    """OLS Running Equivalence Bridge (`D(t) = CS * t + D'`) from `(dist_m, time_s)` pairs or split dict.

    Accepts either `[(1000, 200), (3000, 655), (5000, 1109)]` or `{"1km": "3:20", "3km": "10:55", "5km": "18:29"}`.
    Returns `cs_mps`, `cs_pace_fmt`, `d_prime_m`, `r2`, and derived `mlss_pace_fmt` (~95% CS) / `lt1_pace_range_fmt`.
    """
    dist_lookup = {
        "400m": 400.0, "800m": 800.0, "1k": 1000.0, "1km": 1000.0, "1000m": 1000.0,
        "1500m": 1500.0, "1.5k": 1500.0, "1600m": 1600.0, "mile": 1609.34,
        "2k": 2000.0, "2km": 2000.0, "3k": 3000.0, "3km": 3000.0, "3000m": 3000.0,
        "5k": 5000.0, "5km": 5000.0, "5000m": 5000.0, "10k": 10000.0, "10km": 10000.0, "10000m": 10000.0,
    }

    def _parse_time_s(raw: Any) -> float | None:
        if isinstance(raw, (int, float)):
            return float(raw) if float(raw) > 0 else None
        s = str(raw or "").strip()
        if not s:
            return None
        if ":" in s:
            parts = s.split(":")
            try:
                if len(parts) == 2:
                    return float(parts[0]) * 60.0 + float(parts[1].replace(",", "."))
                if len(parts) == 3:
                    return float(parts[0]) * 3600.0 + float(parts[1]) * 60.0 + float(parts[2].replace(",", "."))
            except ValueError:
                return None
        try:
            return float(s.replace(",", "."))
        except ValueError:
            return None

    pts: list[tuple[float, float]] = []
    if isinstance(splits, dict):
        for k, v in splits.items():
            kl = str(k).strip().lower()
            dist_m = dist_lookup.get(kl)
            if dist_m is None:
                m_num = re.match(r"^(\d+(?:[.,]\d+)?)\s*(km|k|m)$", kl)
                if m_num:
                    val = float(m_num.group(1).replace(",", "."))
                    dist_m = val * 1000.0 if m_num.group(2) in ("km", "k") else val
            t_s = _parse_time_s(v)
            if dist_m and dist_m > 0 and t_s and t_s > 0:
                pts.append((t_s, dist_m))
    else:
        for item in splits or []:
            if len(item) >= 2 and float(item[0]) > 0 and float(item[1]) > 0:
                pts.append((float(item[1]), float(item[0])))

    if len(pts) < 2:
        return {"valid": False, "cs_mps": None, "d_prime_m": None, "r2": 0.0, "cs_pace_fmt": "—"}

    pts.sort(key=lambda x: x[0])
    n = len(pts)
    mx = sum(t for t, _ in pts) / n
    my = sum(d for _, d in pts) / n
    sxx = sum((t - mx) ** 2 for t, _ in pts)
    sxy = sum((t - mx) * (d - my) for t, d in pts)
    syy = sum((d - my) ** 2 for _, d in pts)
    if sxx <= 0.0:
        return {"valid": False, "cs_mps": None, "d_prime_m": None, "r2": 0.0, "cs_pace_fmt": "—"}
    cs_mps = sxy / sxx
    d_prime_m = my - cs_mps * mx
    r2 = (sxy ** 2) / (sxx * syy) if syy > 0.0 else 0.0
    valid = bool(cs_mps > 1.5 and d_prime_m >= 15.0 and r2 >= 0.98)
    cs_spk = 1000.0 / cs_mps if cs_mps > 0.5 else None
    mlss_spk = 1000.0 / (0.95 * cs_mps) if cs_mps > 0.5 else None
    lt1_lo_spk = 1000.0 / (0.85 * cs_mps) if cs_mps > 0.5 else None
    lt1_hi_spk = 1000.0 / (0.80 * cs_mps) if cs_mps > 0.5 else None
    return {
        "valid": valid,
        "cs_mps": round(cs_mps, 2),
        "cs_mps_exact": round(cs_mps, 4),
        "cs_pace_spk": round(cs_spk, 1) if cs_spk else None,
        "cs_pace_fmt": _fmt_run_pace(cs_spk),
        "d_prime_m": int(round(d_prime_m)),
        "d_prime_m_exact": round(d_prime_m, 1),
        "r2": round(r2, 6),
        "mlss_pace_fmt": _fmt_run_pace(mlss_spk),
        "lt1_pace_range_fmt": f"{_fmt_run_pace(lt1_lo_spk)}–{_fmt_run_pace(lt1_hi_spk)}" if lt1_lo_spk else "—",
    }


def compute_running_effectiveness(vel_mps: float, power_w: float, mass_kg: float) -> float | None:
    """Coggan & Palladino Running Effectiveness `RE = v [m/s] / (P / M [W/kg])` in `kg/N`."""
    if not vel_mps or not power_w or not mass_kg or float(vel_mps) <= 0.5 or float(power_w) <= 30.0 or float(mass_kg) <= 25.0:
        return None
    w_kg = float(power_w) / float(mass_kg)
    return round(float(vel_mps) / w_kg, 3)


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
    """Townsend et al. (2017) 2-segment altitude adjustment (`CP` drops >1500m; `W'` 100% up to 2760m, -18.3%/km above; `tau_W'` 1.35-1.50x at 3000m)."""
    h_km = float(elev_m or 0.0) / 1000.0
    cp_loss_pct = round(min(30.0, max(0.0, (h_km - 1.50) * 8.0)), 1) if h_km > 1.50 else 0.0
    wp_pct = 100.0 if h_km <= 2.76 else max(50.0, round(100.0 - 18.3 * (h_km - 2.76), 1))
    tau_mult = 1.0 if h_km <= 1.50 else round(min(1.55, 1.0 + 0.30 * (h_km - 1.50)), 2)
    return {
        "elev_m": int(round(float(elev_m or 0.0))),
        "cp_loss_pct": cp_loss_pct,
        "w_prime_pct": wp_pct,
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

    # Deterministic RED-S Guard: rapid 28d weight loss (>3%) + suppressed `ln RMSSD_7d` + performance stagnation
    cutoff_7d = (ref_today - dt.timedelta(days=7)).isoformat()
    cutoff_28d = (ref_today - dt.timedelta(days=28)).isoformat()
    reds_warning_flag = False
    reds_warning = None
    weight_loss_28d_pct = None
    weights_28d = [
        (str(r["date"])[:10], float(r["weight_kg"]))
        for r in sorted((readiness_rows or []), key=lambda x: str(x.get("date") or ""))
        if r.get("weight_kg") is not None and str(r.get("date") or "")[:10] >= cutoff_28d
    ]
    if len(weights_28d) >= 2:
        k_win = 2 if len(weights_28d) >= 4 else 1
        w_early = sum(x[1] for x in weights_28d[:k_win]) / k_win
        w_late = sum(x[1] for x in weights_28d[-k_win:]) / k_win
        weight_loss_28d_pct = round(((w_early - w_late) / max(30.0, w_early)) * 100.0, 2)

        ln_7d = [
            math.log(float(r["hrv_rmssd"]))
            for r in (readiness_rows or [])
            if r.get("hrv_rmssd") and float(r["hrv_rmssd"]) > 5.0 and str(r.get("date") or "")[:10] >= cutoff_7d
        ]
        ln_28d = [
            math.log(float(r["hrv_rmssd"]))
            for r in (readiness_rows or [])
            if r.get("hrv_rmssd") and float(r["hrv_rmssd"]) > 5.0 and str(r.get("date") or "")[:10] >= cutoff_28d
        ]
        hrv_suppressed = False
        if ln_7d and len(ln_28d) >= 4:
            m7 = sum(ln_7d) / len(ln_7d)
            m28 = sum(ln_28d) / len(ln_28d)
            swc = 0.5 * statistics.pstdev(ln_28d)
            hrv_suppressed = bool((swc > 0.0 and m7 < (m28 - swc)) or (m28 - m7) >= 0.08)

        # Performance stagnation: 42d peak not exceeding 90d peak (or no 42d breakout)
        fresh_c = (curves or {}).get("fresh") or {}
        p5_42 = float((fresh_c.get("5m") or {}).get("42d") or 0.0)
        p5_90 = float((fresh_c.get("5m") or {}).get("90d") or 0.0)
        p20_42 = float((fresh_c.get("20m") or {}).get("42d") or 0.0)
        p20_90 = float((fresh_c.get("20m") or {}).get("90d") or 0.0)
        perf_stagnant = True
        if p5_42 > 0 and p5_90 > 0 and p5_42 > p5_90 * 1.01:
            perf_stagnant = False
        if p20_42 > 0 and p20_90 > 0 and p20_42 > p20_90 * 1.01:
            perf_stagnant = False

        if weight_loss_28d_pct > 3.0 and hrv_suppressed and perf_stagnant:
            reds_warning_flag = True
            reds_warning = (
                f"RED-S ALARM: Rapid 28d weight loss (-{weight_loss_28d_pct:.1f}% > 3.0%) + "
                "suppressed ln RMSSD_7d + performance stagnation — stop deficit, raise CHO/energy availability (>=45 kcal/kg FFM/d) & deload!"
            )

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
        "weight_loss_28d_pct": weight_loss_28d_pct,
        "reds_warning_flag": reds_warning_flag,
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
    run_k_leg_kn_m: float | None = None,
    run_k_leg_drop_pct: float | None = None,
) -> dict[str, Any]:
    """Compute `ln RMSSD_7d`, `CV`, `SWC`, Sanders `Q_Strain`/`Q_RPE:PO`, `HRmax` blunting,
    eccentric running `k_leg` collapse, & 4-Stage Readiness Flag (`GRÜN`/`GELB-A`/`GELB-B`/`ROT`)."""
    ref_today = today or dt.date.today()
    cutoff_2d = (ref_today - dt.timedelta(days=2)).isoformat()
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

    ln_raw_7d = (sum(ln_7d) / len(ln_7d)) if ln_7d else None
    ln_mean_7d = round(ln_raw_7d, 3) if ln_raw_7d is not None else None
    cv_ln_7d = round((statistics.pstdev(ln_7d) / ln_raw_7d) * 100.0, 2) if (len(ln_7d) >= 3 and ln_raw_7d) else None
    ln_mean_28d = (sum(ln_28d) / len(ln_28d)) if len(ln_28d) >= 5 else None
    swc_28d = (0.5 * statistics.pstdev(ln_28d)) if len(ln_28d) >= 5 else None
    hrv_below_swc = bool(ln_raw_7d is not None and ln_mean_28d is not None and swc_28d is not None and ln_raw_7d < (ln_mean_28d - max(0.01, swc_28d)))

    # 2. Sanders Subjective-Objective Ratios + HRmax ceiling blunting + eccentric k_leg drop detection
    eff_cp = float(cp_watts or profile.get("cp_w") or profile.get("ftp_w") or 275.0)
    hrmax = profile.get("hr_max")
    q_strains: list[float] = []
    q_rpe_pos: list[float] = []
    hrmax_blunted = False
    detected_k_leg_drop = float(run_k_leg_drop_pct) if run_k_leg_drop_pct is not None else None

    # Baseline k_leg from older runs if not explicitly passed
    run_k_vals = [
        float(a["run_k_leg_kn_m"])
        for a in (activities or [])
        if isinstance(a, dict) and str(a.get("sport") or "").lower() == "running" and a.get("run_k_leg_kn_m")
    ]
    k_leg_base = statistics.median(run_k_vals) if len(run_k_vals) >= 2 else None

    for a in (activities or [])[:10]:
        if not isinstance(a, dict):
            continue
        act_d = str(a.get("date") or "")[:10]
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

        # HRmax-Deckelung (Overreaching signature): hard/maximal session in last 7d with blunted peak HR (>=10 bpm / >=7% below HRmax)
        if hrmax and float(hrmax) >= 140.0 and act_d >= cutoff_7d:
            act_max_hr = float(a.get("max_hr") or 0.0)
            is_hard = bool(
                (rpe is not None and float(rpe) >= 8.0)
                or float(a.get("if") or 0.0) >= 0.90
                or float(a.get("kj_above_cp") or 0.0) >= 12.0
                or a.get("maximal_effort") is True
            )
            if is_hard and act_max_hr > 80.0 and act_max_hr <= min(float(hrmax) - 10.0, float(hrmax) * 0.93):
                hrmax_blunted = True

        # Eccentric running leg-spring stiffness collapse in last 48h
        if str(a.get("sport") or "").lower() == "running" and act_d >= cutoff_2d:
            if a.get("run_k_leg_drop_pct") is not None:
                drop_v = float(a["run_k_leg_drop_pct"])
                if detected_k_leg_drop is None or drop_v > detected_k_leg_drop:
                    detected_k_leg_drop = drop_v
            elif a.get("run_k_leg_kn_m") and k_leg_base and k_leg_base > 2.0:
                drop_v = ((k_leg_base - float(a["run_k_leg_kn_m"])) / k_leg_base) * 100.0
                if detected_k_leg_drop is None or drop_v > detected_k_leg_drop:
                    detected_k_leg_drop = round(drop_v, 1)

    latest_q_strain = q_strains[0] if q_strains else None
    latest_q_rpe_po = q_rpe_pos[0] if q_rpe_pos else None
    k_leg_collapsed = bool(detected_k_leg_drop is not None and detected_k_leg_drop >= 15.0)

    # 3. Resolve 4-Stage Readiness (`GRÜN` | `GELB-A` | `GELB-B` | `ROT`)
    today_row = next((r for r in (readiness_rows or []) if r["date"] == ref_today.isoformat()), None)
    leg_pain = int(today_row["leg_pain"]) if (today_row and today_row.get("leg_pain") is not None) else (
        int(today_row["soreness"]) if (today_row and today_row.get("soreness") is not None) else None
    )
    fatigue_7d = int(today_row["fatigue_7d"]) if (today_row and today_row.get("fatigue_7d") is not None) else None
    notes_low = str((today_row or {}).get("notes") or "").lower()
    heavy_legs_text = any(w in notes_low for w in ("beine wie beton", "schwere beine", "leere beine", "dead legs", "heavy legs"))

    base_st = base_readiness.get("status", "GREEN")
    if base_st == "RED" or (fatigue_7d is not None and fatigue_7d >= 5) or hrmax_blunted:
        flag_4 = "ROT"
        if hrmax_blunted:
            directive = "Overreaching / HRmax Ceiling Blunted (overrides green RHR): Immediate 3–5d deload + high CHO availability; cancel all HIIT."
        else:
            directive = "Overreaching / Red Guardrail: Immediate recovery or 3–5d deload + high CHO availability; cancel all HIIT."
    elif k_leg_collapsed or (leg_pain is not None and leg_pain >= 4) or heavy_legs_text or (latest_q_strain is not None and latest_q_strain >= 8.5):
        flag_4 = "GELB-B"
        myo_note = " (Fast-Myotype Type-II / intramyofibrillar glycogen lag)" if (myotype_z and myotype_z > 0.5) else ""
        if k_leg_collapsed:
            directive = (
                f"Peripheral Conflict — Eccentric Running Leg-Stiffness Drop (k_leg -{detected_k_leg_drop:.0f}%) "
                f"Overrides Green RHR/HRV{myo_note}: postpone HIIT 48h; spin easy Z1."
            )
        else:
            directive = f"Peripheral Conflict — Subjective Legs Override Autonomic HRV{myo_note}: postpone HIIT 24–48h; spin easy Z1."
    elif base_st == "AMBER" or hrv_below_swc:
        flag_4 = "GELB-A"
        directive = "Acute Stimulus Stress: swap today's HIIT for relaxed Zone 1/2 endurance; keep macro block intact."
    else:
        flag_4 = "GRÜN"
        directive = "All Systems Clear: execute planned key intervals or progression at full target."

    # Karvonen HRR anchor
    rhr = (today_row or {}).get("rhr") or base_readiness.get("baseline_rhr") or profile.get("rhr")
    hrr_span = int(round(float(hrmax) - float(rhr))) if (hrmax and rhr and float(hrmax) > float(rhr) + 20) else None

    return {
        "readiness_flag_4stage": flag_4,
        "coaching_directive": directive,
        "today_logged": today_row is not None,
        "ln_rmssd_7d": ln_mean_7d,
        "cv_ln_rmssd_pct": cv_ln_7d,
        "hrv_below_swc": hrv_below_swc,
        "hrmax_blunted": hrmax_blunted,
        "k_leg_drop_pct": detected_k_leg_drop,
        "k_leg_collapsed": k_leg_collapsed,
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
    pillar5: dict[str, Any] | None = None,
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
    if is_cyclist and pillar5 and pillar5.get("durability_25kj_status") == "submax_untested":
        v15_drop = pillar5.get("verified_drop_15kj_pct")
        v15_note = f" (verified >15 kJ/kg drop is only {v15_drop}%!)" if v15_drop is not None else ""
        calibration_gaps.append(
            f"Durability >25 kJ/kg is UNTESTED (athlete only rode submaximal Z2/tempo after 25 kJ/kg{v15_note} — "
            "NEVER interpret passive Z2 ride watts as a physiological fatigue drop; use Field-Test Unit 3 Preload to measure true late-ride 5m power)."
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


def format_seven_pillar_blocks(telemetry: dict[str, Any], include_actions: bool = True) -> str:
    """Render the compact, zero-hallucination 7-Pillar Vector + W'-Scaled Prescriptions + Missing Gate + Question Radar."""
    sp = telemetry.get("seven_pillars") or {}
    audit = telemetry.get("coach_audit") or {}
    resp_m = telemetry.get("response_model") or {}
    prof = telemetry.get("profile") or {}
    lines: list[str] = []

    if include_actions:
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
    p3_rx = sp.get("pillar3_prescriptions") or {}
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
        if p1.get("vo2max_rel_clean") or p2.get("vlamax_sprint_proxy") or p2.get("vlamax_met_mmol_l_s"):
            fm_band = p2.get("fatmax_band_w")
            fm_pk = p2.get("p_fatmax_w")
            fm_str = f"{fm_pk}W (95%-MFO: {fm_band[0]}–{fm_band[1]}W)" if (fm_band and fm_pk) else f"{fm_pk or '—'}W"
            lines.append(
                f"  [2_metabolic S1+S2] VO2max_clean={p1.get('vo2max_rel_clean') or '—'} mL/kg/min "
                f"(VO2max_FFM={p1.get('vo2max_ffm') or '—'}, W'-deduct={p1.get('w_prime_deduction_w') or 0}W) | "
                f"vLamax_met={p2.get('vlamax_met_mmol_l_s') or '—'} mmol/L/s (inverse Mader; alactic_15s_sprint_proxy={p2.get('vlamax_sprint_proxy') or '—'}) | "
                f"eta_shuttle={p2.get('shuttle_index_eta') or '—'} ({p2.get('shuttle_class') or 'uncalibrated'}) | "
                f"Mader FatMax={fm_str} ({p2.get('mfo_g_min') or '—'}g/min), LT1={p2.get('p_lt1_w') or '—'}W, MLSS={p2.get('p_mlss_w') or '—'}W | "
                f"CarbMax@90g/h={p2.get('p_carbmax_90_w') or '—'}W"
            )
        if cp_m.get("cp_w") or telemetry.get("effective_cp_w"):
            omni_tag = f" ({p3.get('omni_decay_source')})" if p3.get("omni_decay_source") else ""
            lines.append(
                f"  [3_kinetics S3] CP={cp_m.get('cp_w') or telemetry.get('effective_cp_w')}W (SEE={p3.get('see_cp_pct') or '—'}%) | "
                f"W'={cp_m.get('w_prime_kj') or telemetry.get('effective_w_prime_kj') or '—'}kJ | "
                f"MPR={p3.get('mpr_w') or '—'}W | OmniPD_A={p3.get('omni_decay_a_w') or '—'}W{omni_tag} | "
                f"Chorley tau_FC(50%CP)={p3.get('tau_fc_50pct_s') or 22.3}s, tau_SC={p3.get('tau_sc_50pct_s') or 503.5}s"
            )
        if p3_rx:
            op = p3_rx.get("re_entry_opener") or {}
            fl = p3_rx.get("vo2max_float_30_15") or {}
            cl = p3_rx.get("vo2max_classic_30_15") or {}
            hs = p3_rx.get("vo2max_hard_start_4x4") or {}
            sh = p3_rx.get("lactate_shuttle_1_4") or {}
            st = p3_rx.get("sub_threshold") or {}
            cl_note = " [IIx-capped -> auto-float]" if cl.get("iix_capped") else ""
            lines.append(
                f"  [3B_prescriptions W'-scaled (1-Hz Chorley+VO2 solved; floor={p3_rx.get('target_reserve_kj')}kJ, IIx_cap={p3_rx.get('iix_cap_w')}W — NEVER guess % CP!)]"
            )
            lines.append(
                f"    • Threshold-Opener 30/15 (Re-Entry, NOT VO2max): {op.get('on_w')}W ({op.get('on_pct')}%) / {op.get('off_w')}W ({op.get('off_pct')}%) "
                f"[avg {op.get('block_avg_w')}W, W'bal_min={op.get('w_bal_min_pct')}%, t>=90%VO2={op.get('t90_min')}m]"
            )
            lines.append(
                f"    • VO2max Float 30/15 (3x13): {fl.get('on_w')}W ({fl.get('on_pct')}%) / {fl.get('off_w')}W ({fl.get('off_pct')}%) "
                f"[avg {fl.get('block_avg_w')}W, W'bal_min={fl.get('w_bal_min_pct')}% ({fl.get('w_bal_min_kj')}kJ), t>=90%VO2={fl.get('t90_min')}m]"
            )
            lines.append(
                f"    • VO2max Classic 30/15 (3x13{cl_note}): {cl.get('on_w')}W ({cl.get('on_pct')}%) / {cl.get('off_w')}W ({cl.get('off_pct')}%) "
                f"[avg {cl.get('block_avg_w')}W, W'bal_min={cl.get('w_bal_min_pct')}%, t>=90%VO2={cl.get('t90_min')}m]"
            )
            lines.append(
                f"    • VO2max Hard-Start 4x4m (2-Stage, 5m rest): 45s@{hs.get('start_w')}W ({hs.get('start_pct')}%) -> 3:15m@{hs.get('hold_w')}W ({hs.get('hold_pct')}%) "
                f"[avg {hs.get('block_avg_w')}W, W'bal_min={hs.get('w_bal_min_pct')}% ({hs.get('w_bal_min_kj')}kJ), PCr_min={hs.get('fc_pcr_min_pct')}%, t>=90%VO2={hs.get('t90_min')}m]"
            )
            lines.append(
                f"    • 1:4 Lactate-Shuttle (6x): 1m@{sh.get('over_w')}W ({sh.get('over_pct')}%) / 4m@{sh.get('under_w')}W ({sh.get('under_pct')}%) "
                f"[W'bal_min={sh.get('w_bal_min_pct')}%] | Sub-Threshold 4x10m: {st.get('target_w')}W ({st.get('target_pct')}%)"
            )
        if p4.get("myotype_z_score") is not None or p4.get("t_max_nm_kg") is not None or p4.get("run_k_leg_kn_m") is not None:
            k_leg_s = f" | run_k_leg={p4.get('run_k_leg_kn_m')} kN/m" if p4.get("run_k_leg_kn_m") is not None else ""
            lines.append(
                f"  [4_neuromuscular S4] Z_Myo={p4.get('myotype_z_score') if p4.get('myotype_z_score') is not None else '—'} "
                f"({p4.get('myotype_class')}) | T_max={p4.get('t_max_nm_kg') or '—'} Nm/kg | C_opt={p4.get('c_opt_sprint_rpm') or '—'} rpm{k_leg_s} | "
                f"HIIT_Recovery={p4.get('hiit_recovery_window')} | Protocol: {p4.get('recommended_vo2_protocol')}"
            )
        tv = p5.get("tier_5m_validity") or {}
        v15 = tv.get("15kj_kg") or {}
        v25 = tv.get("25kj_kg") or {}
        dur_parts = []
        if v15.get("verified_max"):
            dur_parts.append(f">15kJ/kg=-{v15['drop_pct']}% ({v15['watts']}W VERIFIED)")
        elif v15.get("watts"):
            dur_parts.append(f">15kJ/kg=UNTESTED ({v15['watts']}W submax-only)")
        if v25.get("verified_max"):
            dur_parts.append(f">25kJ/kg=-{v25['drop_pct']}% ({v25['watts']}W VERIFIED)")
        elif v25.get("watts"):
            dur_parts.append(f">25kJ/kg=UNTESTED ({v25['watts']}W submax Z2/tempo only — NO all-out effort!)")
        dur_str = ", ".join(dur_parts) if dur_parts else "untested"
        lines.append(
            f"  [5_durability S5] 7d_Work: w_sub={p5.get('work_sub_cp_7d_kj_kg', 0)} kJ/kg (<=CP), "
            f"w_sup={p5.get('work_above_cp_7d_kj_kg', 0)} kJ/kg (>CP!) | "
            f"Durability_5m({dur_str}) | "
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
    if include_actions:
        pq = audit.get("priority_questions") or []
        dq = audit.get("discovery_questions") or []
        if pq or dq:
            lines.append("=== 1C. SOCRATIC QUESTION RADAR (ASK 1-2 SHARP QUESTIONS AT END OF REPLY!) ===")
            for q in pq:
                lines.append(f"  [Acute/Data] {q}")
            for q in dq:
                lines.append(f"  [Athlete Discovery -> file answer in brain/coach/playbook.md] {q}")

    return "\n".join(lines)

