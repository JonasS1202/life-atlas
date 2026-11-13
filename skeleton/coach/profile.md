---
summary: Athlete profile, coaching depth (auto/simple/standard/pro), thresholds & zones, typology, weekly time budget, and bio-rules
tags: [sport, self]
aliases: [Athlete Profile, Trainingszonen, Leistungsdaten, Schwellenwerte, Rider Profile]
depth: auto
---
# Athlete Profile & Thresholds

Stand: {{DATE}}. Part of [Sport-Coach Hub](README.md). Read deterministically by `atlas.py coach` (`depth: auto | simple | standard | pro`).

## 1. Biometrics & Core Anchors

| Parameter | Value | Notes |
|---|---|---|
| **Height & Weight** | `186 cm` · `75.0 kg` | System weight (incl. bike & kit): `~85.0 kg` |
| **Primary & Secondary Sport** | `Cycling` (Primary) · `Running` & `Strength` (Secondary) | Polarized / Pyramidal endurance focus |
| **Weekly Time Budget** | `8.0 – 10.0 h / week` | Soft corridor across Cycling, Running & Strength |
| **FTP (Cycling)** | `300 W` (`4.00 W/kg`) | Functional Threshold Power |
| **CP & W' (Cycling)** | `CP: — W` · `W': — kJ` | 2-param Critical Power & anaerobic work capacity (`R² >= 0.98`) |
| **Fiber Typology & Glycolytic Index** | `Typology: —` · `W'/kg: —` · `CP/MAP: —` | Lab-free phenotype proxy (`Fast \| Balanced \| Slow`) |
| **VO2max (est.)** | `~65 ml/kg/min` | Storer MAP / Uth HR / Daniels VDOT ceiling |
| **Max HR, LTHR & Resting HR** | `HRmax: 192 bpm` · `LTHR: — bpm` · `RHR: ~48 bpm` | Autonomic & Banister TRIMP anchors |
| **Running Threshold (CS & D')** | `LT2 Pace: — /km` · `D': — m` | Critical Speed & anaerobic distance reserve |
| **Fueling Tolerance** | `60 – 90 g/h` | Max comfortable CHO intake (`1:0.8` maltodextrin:fructose above `60 g/h`) |
| **Smart-Trainer & Head-Unit** | `Hammerhead Karoo 3` · `.zwo` decimal power factors | Require exact decimal `Power="0.75"` in `.zwo` |

## 2. Cycling Power Zones (based on FTP / CP)

| Zone | Name | % FTP | Target Watts | Physiological Focus |
|---|---|---|---|---|
| **Z1** | Active Recovery | `< 55 %` | `< 165 W` | Clearance & blood flow |
| **Z2** | Endurance (LT1 / FatMax) | `56 – 75 %` | `168 – 225 W` | Mitochondrial density & fat oxidation |
| **Z3** | Tempo / SweetSpot | `76 – 94 %` | `228 – 282 W` | Muscular endurance & glycogen economy |
| **Z4** | Threshold (LT2 / CP) | `95 – 105 %` | `285 – 315 W` | Lactate clearance & sustainable ceiling |
| **Z5** | VO2max (MAP) | `106 – 120 %` | `318 – 360 W` | Maximal aerobic power & stroke volume |
| **Z6** | Anaerobic Capacity (W') | `> 120 %` | `> 360 W` | Glycolytic power & repeatability |

## 3. Weekly Rhythm & Training Windows

- **Weekday Window (Mon–Fri):** `17:00 – 20:00` (max `60 – 120 min` per session after work).
- **Weekend Window (Sat–Sun):** Flexible (`2.0 – 5.0 h` for long Zone 2 endurance rides, group rides, or key race-pace sessions).
- **Strength / Hypertrophy:** `1 – 3x / week` (`30 – 45 min` upper-body & core, `>=6h` separated from endurance and `>=24–48h` before key VO2max/Threshold workouts).

## 4. Bio-Rules & Personal Directives (Tier 0)

<!-- Validated biological patterns, fueling rules, and personal coaching constraints learned from ride feedback -->
- **Fueling:** `60 – 90 g/h` carbohydrates (maltodextrin/fructose) on intense or `>90 min` rides + electrolyte hydration.
- **Orthopedic Running Cap:** Scale running volume conservatively (`max +10 %/week`); avoid hard eccentric running intervals `<24–48h` before a key cycling threshold/VO2max workout.
