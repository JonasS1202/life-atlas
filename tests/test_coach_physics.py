#!/usr/bin/env python3
"""Deterministic Biophysical Unit & Golden-Master Test Suite for Atlas Coach (`v1.4.7+`).

Compatible with both Python stdlib `unittest` (`python3 -I tests/test_coach_physics.py`)
and `pytest` (`pytest tests/test_coach_physics.py`). Zero external dependencies.

5 Verification Blocks:
  1. Golden-Master Archetypes (Kumpel A–E from the 7-Pillar PDF Matrix)
  2. 1-Hz Solver & Phenotype Invariants (Fast Myotype vs. 64 kg Diesel, 1:4 Shuttle, Compiler-Guard)
  3. Chorley Bi-Exponential & Substrate Conservation Laws
  4. Environment (Townsend / Heat / Indoor / Drivetrain), Spike Quarantine & Intensity-Weighted Durability
  5. Running Equivalence Bridge (CS/D', Minetti, Morin k_leg, RE), RED-S Guard & Readiness Overrides
"""
from __future__ import annotations

import datetime as dt
import math
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "core", "tools"))
sys.path.insert(0, os.path.join(REPO, "core", "hooks"))

from atlas_lib import coach_engine as ce  # noqa: E402
from atlas_lib import pillars_engine as pe  # noqa: E402


# ---------------------------------------------------------------------------
# Block 1: Golden-Master Tests (Kumpel A–E from 7-Pillar PDF Matrix, pp. 24–26)
# ---------------------------------------------------------------------------

class TestBlock1GoldenMasterKumpelMatrix(unittest.TestCase):
    """Exact reproducibility of all 5 canonical PDF rider archetypes (Kumpel A through E)."""

    KUMPELS = [
        {
            "name": "Kumpel A: Reiner Diesel",
            "mass": 75.0, "height": 180.0, "kfa": 11.0,
            "mmp": {"5s": 903, "15s": 720, "3m": 369, "5m": 344, "12m": 322, "60m": 302},
            "c_opt": 111.0, "t_max_nm": 2.08 * 75.0,
            "exp_cp": 306.3, "exp_wp_kj": 11.3, "exp_vo2": 57.7, "exp_vla": 0.36,
            "exp_eta": 1.08, "exp_fatmax": 216, "exp_z_myo": -0.98, "exp_myo_cls": "Slow Myotype",
            "exp_flat": 1068, "exp_climb": 3.69, "exp_cs5m": 1578, "exp_omni_a": 11.0,
        },
        {
            "name": "Kumpel B: Spritziger Puncheur",
            "mass": 75.0, "height": 180.0, "kfa": 11.0,
            "mmp": {"5s": 1305, "15s": 960, "3m": 430, "5m": 374, "12m": 325, "60m": 276},
            "c_opt": 131.0, "t_max_nm": 2.56 * 75.0,
            "exp_cp": 290.0, "exp_wp_kj": 25.2, "exp_vo2": 57.3, "exp_vla": 0.77,
            "exp_eta": 1.23, "exp_fatmax": 198, "exp_z_myo": 1.58, "exp_myo_cls": "Fast Myotype",
            "exp_flat": 1011, "exp_climb": 3.49, "exp_cs5m": 1865, "exp_omni_a": 30.0,
        },
        {
            "name": "Kumpel C: Leichter Bergfahrer",
            "mass": 61.0, "height": 172.0, "kfa": 8.3,
            "mmp": {"5s": 815, "15s": 640, "3m": 355, "5m": 330, "12m": 308, "60m": 287},
            "c_opt": 117.0, "t_max_nm": 2.16 * 61.0,
            "exp_cp": 292.3, "exp_wp_kj": 11.3, "exp_vo2": 67.2, "exp_vla": 0.45,
            "exp_eta": 1.10, "exp_fatmax": 207, "exp_z_myo": -0.57, "exp_myo_cls": "Slow Myotype",
            "exp_flat": 1124, "exp_climb": 4.24, "exp_cs5m": 1785, "exp_omni_a": 12.0,
        },
        {
            "name": "Kumpel D: Schwerer Sprinter",
            "mass": 86.0, "height": 187.0, "kfa": 14.0,
            "mmp": {"5s": 1650, "15s": 1210, "3m": 452, "5m": 388, "12m": 334, "60m": 274},
            "c_opt": 138.0, "t_max_nm": 2.65 * 86.0,
            "exp_cp": 294.9, "exp_wp_kj": 28.1, "exp_vo2": 51.5, "exp_vla": 0.98,
            "exp_eta": 1.37, "exp_fatmax": 200, "exp_z_myo": 2.86, "exp_myo_cls": "Fast Myotype",
            "exp_flat": 957, "exp_climb": 3.14, "exp_cs5m": 1751, "exp_omni_a": 42.0,
        },
        {
            "name": "Kumpel E: Klassiker-Rouleur",
            "mass": 78.0, "height": 184.0, "kfa": 10.4,
            "mmp": {"5s": 1255, "15s": 970, "3m": 455, "5m": 412, "12m": 376, "60m": 348},
            "c_opt": 124.0, "t_max_nm": 2.46 * 78.0,
            "exp_cp": 349.8, "exp_wp_kj": 18.8, "exp_vo2": 63.9, "exp_vla": 0.71,
            "exp_eta": 1.21, "exp_fatmax": 245, "exp_z_myo": 0.71, "exp_myo_cls": "Fast Myotype",
            "exp_flat": 1187, "exp_climb": 4.07, "exp_cs5m": 2176, "exp_omni_a": 10.0,
        },
    ]

    def test_golden_master_all_five_archetypes(self):
        for k in self.KUMPELS:
            with self.subTest(archetype=k["name"]):
                m, h, kfa = k["mass"], k["height"], k["kfa"]
                ffm = m * (1.0 - kfa / 100.0)
                vol_rel = 0.5037 * (1.0 - kfa / 100.0)
                body = {
                    "mass_kg_7d": m, "kfa_pct": kfa, "kfa_calibrated": True,
                    "ffm_kg": round(ffm, 2), "vol_rel_ind": round(vol_rel, 4), "v_la_l": round(vol_rel * m, 2),
                }
                mmp = k["mmp"]
                cp_m = ce.solve_cp_w_prime(mmp)
                self.assertTrue(cp_m["valid"], f"{k['name']} CP invalid: {cp_m}")
                self.assertAlmostEqual(cp_m["cp_w"], k["exp_cp"], delta=0.6)
                self.assertAlmostEqual(cp_m["w_prime_kj"], k["exp_wp_kj"], delta=0.2)

                p1 = pe.compute_pillar1_oxidative(mmp["5m"], cp_m["w_prime_j"], m, ffm, True)
                self.assertAlmostEqual(p1["vo2max_rel_clean"], k["exp_vo2"], delta=0.2)

                curves = {"fresh": {dur: {"90d": val} for dur, val in mmp.items()}}
                p2 = pe.compute_pillar2_metabolic_mader(curves, cp_m["cp_w"], p1["vo2max_rel_clean"], body, t_alac_s=3.0)
                self.assertAlmostEqual(p2["vlamax_sprint_proxy"], k["exp_vla"], delta=0.02)
                self.assertAlmostEqual(p2["shuttle_index_eta"], k["exp_eta"], delta=0.05)
                # Unified Mader FatMax matches PDF matrix within <=2 W and equals >=95%-MFO plateau
                self.assertAlmostEqual(p2["p_fatmax_w"], k["exp_fatmax"], delta=2.0)
                self.assertEqual(p2["p_fatmax_w"], p2["mader_mfo_peak_w"])
                self.assertEqual(p2["fatmax_band_w"], p2["mader_fatmax_95_band_w"])

                p3 = pe.compute_cp_see_and_omnipd(mmp, cp_m)
                self.assertEqual(p3["omni_decay_source"], "empirical_60m")
                self.assertAlmostEqual(p3["omni_decay_a_w"], k["exp_omni_a"], delta=1.5)

                acts = [{"sport": "cycling", "device_watts": True, "t_max_nm": k["t_max_nm"], "c_opt_sprint_rpm": k["c_opt"]}]
                p4 = pe.compute_pillar4_myotype(acts, cp_m["cp_w"], p3["mpr_w"], p2["vlamax_sprint_proxy"], m)
                self.assertAlmostEqual(p4["myotype_z_score"], k["exp_z_myo"], delta=0.12)
                self.assertEqual(p4["myotype_class"], k["exp_myo_cls"])

                p6 = pe.compute_pillar6_locomotion_environment({"height_cm": h, "bike_weight_kg": 8.0}, body, [], curves, cp_m["cp_w"], cp_m["w_prime_kj"])
                self.assertAlmostEqual(p6["flat_index_w_m2"], k["exp_flat"], delta=5.0)
                self.assertAlmostEqual(p6["climb_index_w_kgsys"], k["exp_climb"], delta=0.03)
                self.assertAlmostEqual(p6["compound_score_5m"], k["exp_cs5m"], delta=2.0)

    def test_omnipd_myotype_wp_fallback_when_no_60m_anchor(self):
        """Without a >30m all-out anchor, OmniPD A uses Myotype/W'-coupled fallback (Sprinter >> Diesel)."""
        cp_diesel = ce.solve_cp_w_prime({"3m": 369, "5m": 344, "12m": 322})
        cp_sprint = ce.solve_cp_w_prime({"3m": 452, "5m": 388, "12m": 334})
        p3_d = pe.compute_cp_see_and_omnipd({"3m": 369, "5m": 344, "12m": 322}, cp_diesel, myotype_z=-0.98)
        p3_s = pe.compute_cp_see_and_omnipd({"3m": 452, "5m": 388, "12m": 334}, cp_sprint, myotype_z=2.86)
        self.assertEqual(p3_d["omni_decay_source"], "myotype_wp_fallback")
        self.assertEqual(p3_s["omni_decay_source"], "myotype_wp_fallback")
        self.assertIsNotNone(p3_d["omni_decay_a_w"])
        self.assertGreater(p3_s["omni_decay_a_w"], p3_d["omni_decay_a_w"] * 2.0)
        # 90m TT prediction strictly below CP
        p90_s = pe.predict_omnipd_power(5400.0, cp_sprint["cp_w"], cp_sprint["w_prime_j"], p3_s["omni_decay_a_w"])
        self.assertLess(p90_s, cp_sprint["cp_w"] * 0.93)


# ---------------------------------------------------------------------------
# Block 2: Solver & Phenotype Invariants
# ---------------------------------------------------------------------------

class TestBlock2SolverAndPhenotypeInvariants(unittest.TestCase):
    """Jonas (294W / 31.4kJ) vs. 64 kg Diesel (310W / 10.5kJ), 1:4 Shuttle & Compiler-Guard."""

    def test_jonas_vs_64kg_diesel_safety_floor_survives_minus_2pct_daily_cp(self):
        body_j = {"mass_kg_7d": 75.0, "kfa_pct": 11.5, "kfa_calibrated": True, "ffm_kg": 66.38, "vol_rel_ind": 0.4458, "v_la_l": 33.43}
        p1_j = pe.compute_pillar1_oxidative(418.0, 31400.0, 75.0, 66.38, True)
        p2_j = pe.compute_pillar2_metabolic_mader({"fresh": {"15s": {"90d": 1094}}}, 294.0, p1_j["vo2max_rel_clean"], body_j, t_alac_s=2.0, creatine_flag=True)

        body_d = {"mass_kg_7d": 64.0, "kfa_pct": 10.0, "kfa_calibrated": True, "ffm_kg": 57.6, "vol_rel_ind": 0.4533, "v_la_l": 29.01}
        p1_d = pe.compute_pillar1_oxidative(345.0, 10500.0, 64.0, 57.6, True)
        p2_d = pe.compute_pillar2_metabolic_mader({"fresh": {"15s": {"90d": 680}}}, 310.0, p1_d["vo2max_rel_clean"], body_d)

        rx_j = pe.solve_individual_interval_prescriptions(294.0, 31400.0, 418.0, p2_j, {}, 75.0, creatine_flag=True)
        rx_d = pe.solve_individual_interval_prescriptions(310.0, 10500.0, 345.0, p2_d, {}, 64.0)

        # Diesel safety floor >= 3.0 kJ; Diesel 30/15 On-power strictly lower than Fast-Myotype Jonas
        self.assertGreaterEqual(rx_d["target_reserve_kj"], 3.0)
        self.assertGreaterEqual(rx_d["vo2max_float_30_15"]["w_bal_min_kj"], 2.9)
        self.assertLess(rx_d["vo2max_float_30_15"]["on_w"], rx_j["vo2max_float_30_15"]["on_w"])

        # Stress test: simulate the Diesel's solved 3x13 Float 30/15 on a -2% daily CP day -> MUST NOT hit 0 kJ!
        d_on = rx_d["vo2max_float_30_15"]["on_w"]
        d_off = rx_d["vo2max_float_30_15"]["off_w"]
        d_stream = [180.0] * 600
        for s in range(3):
            for _ in range(13):
                d_stream.extend([float(d_on)] * 30 + [float(d_off)] * 15)
            if s < 2:
                d_stream.extend([160.0] * 300)
        sim_bad_day = pe.simulate_workout_stream(d_stream, 310.0, 10500.0, 345.0, 64.0, form_pct=-2.0, title="VO2max Float 30/15")
        self.assertIsNone(sim_bad_day["blowup_s"], f"Diesel hit 0 kJ at -2% daily CP: {sim_bad_day}")
        self.assertGreater(sim_bad_day["w_bal_min_kj"], 0.5)

    def test_1_to_4_lactate_shuttle_corridor_and_iix_cap(self):
        body_j = {"mass_kg_7d": 75.0, "kfa_pct": 11.5, "kfa_calibrated": True, "ffm_kg": 66.38, "vol_rel_ind": 0.4458, "v_la_l": 33.43}
        p2_j = pe.compute_pillar2_metabolic_mader({"fresh": {"15s": {"90d": 1094}}}, 294.0, 62.3, body_j)
        rx = pe.solve_individual_interval_prescriptions(294.0, 31400.0, 418.0, p2_j, {}, 75.0)
        sh = rx["lactate_shuttle_1_4"]
        self.assertLessEqual(sh["over_w"], rx["iix_cap_w"])
        self.assertGreaterEqual(sh["under_pct"], 80)
        self.assertLessEqual(sh["under_pct"], 86)

    def test_compiler_guard_throws_stimulus_mismatch_on_anaerobic_exploit(self):
        """Exploit `4x (40s @ 705 W / 4m @ 155 W)` MUST deterministically emit `STIMULUS MISMATCH`."""
        import importlib.util
        spec_hk = importlib.util.spec_from_file_location("atlas_hook_phys", os.path.join(REPO, "core", "hooks", "atlas_hook.py"))
        hk_m = importlib.util.module_from_spec(spec_hk)
        spec_hk.loader.exec_module(hk_m)

        parsed = hk_m.parse_workout_spec("VO2max Exploit (45m | 10m WU 55-68%, 4x 40s@705W / 4m@155W, 10m CD 60-48%)", subkind="cycling", ftp_watts=294)
        self.assertEqual(parsed["unparsed_tokens"], [])
        w_arr, wb_arr = pe.expand_spec_to_1hz_watts(parsed, cp_watts=294.0)
        sim = pe.simulate_workout_stream(w_arr, 294.0, 31400.0, 418.0, 75.0, title=parsed["title"], work_block_watts=wb_arr)
        self.assertLess(sim["time_ge_90_vo2max_min"], 7.0)
        self.assertIn("STIMULUS MISMATCH", sim["stimulus_mismatch"])


# ---------------------------------------------------------------------------
# Block 3: Chorley & Substrate Conservation Laws
# ---------------------------------------------------------------------------

class TestBlock3ChorleyAndSubstrateConservation(unittest.TestCase):
    """Strict pool conservation `W'_FC + W'_SC in [0, W']`, exact Chorley taus & CHO monotonicity."""

    def test_chorley_pool_conservation_and_exact_50pct_taus(self):
        tau_fc, tau_sc = pe.chorley_recovery_taus(50.0)
        self.assertAlmostEqual(tau_fc, 22.3, places=1)
        self.assertAlmostEqual(tau_sc, 503.5, places=1)

        # After 10 min (600 s) easy spinning at 50% CP, slow pool is only ~69.6% recovered (NOT 100%!)
        rec_sc_10m = (1.0 - math.exp(-600.0 / tau_sc)) * 100.0
        self.assertAlmostEqual(rec_sc_10m, 69.6, delta=0.2)

        # Chaotic sprint + rest + all-out + cool-down stream preserves `0 <= W'_FC + W'_SC <= W'`
        cp, wp = 294.0, 31400.0
        stream = [150.0] * 120 + [1100.0] * 45 + [147.0] * 600 + [500.0] * 200 + [0.0] * 900
        res = pe.compute_chorley_w_bal(stream, cp_watts=cp, w_prime_j=wp, fc_fraction=0.43)
        self.assertGreaterEqual(res["w_bal_min_j"], 0)
        self.assertLessEqual(res["w_bal_max_j"], int(wp))
        self.assertGreaterEqual(res["fc_end_j"], 0)
        self.assertGreaterEqual(res["sc_end_j"], 0)
        self.assertLessEqual(res["fc_end_j"] + res["sc_end_j"], int(wp))
        self.assertAlmostEqual(res["tau_fc_50pct_s"], 22.3, places=1)
        self.assertAlmostEqual(res["tau_sc_50pct_s"], 503.5, places=1)

    def test_substrate_cho_intake_monotonicity(self):
        """Higher CHO intake (90 vs 60 vs 45 vs 30 g/h) yields strictly higher CP_eff & W'_eff after preload."""
        eff_30 = pe.compute_effective_cp_w_prime(294.0, 31400.0, mass_kg=75.0, prior_kj=1500.0, cho_g_h=30.0)
        eff_45 = pe.compute_effective_cp_w_prime(294.0, 31400.0, mass_kg=75.0, prior_kj=1500.0, cho_g_h=45.0)
        eff_60 = pe.compute_effective_cp_w_prime(294.0, 31400.0, mass_kg=75.0, prior_kj=1500.0, cho_g_h=60.0)
        eff_90 = pe.compute_effective_cp_w_prime(294.0, 31400.0, mass_kg=75.0, prior_kj=1500.0, cho_g_h=90.0)
        self.assertLess(eff_30["cp_eff_w"], eff_45["cp_eff_w"])
        self.assertLess(eff_45["cp_eff_w"], eff_60["cp_eff_w"])
        self.assertLess(eff_60["cp_eff_w"], eff_90["cp_eff_w"])
        self.assertLess(eff_45["wp_eff_j"], eff_90["wp_eff_j"])


# ---------------------------------------------------------------------------
# Block 4: Environment, Spike Quarantine & Intensity-Weighted Durability
# ---------------------------------------------------------------------------

class TestBlock4EnvironmentSpikesAndDurability(unittest.TestCase):
    """Townsend 2-segment altitude, heat/indoor/drivetrain, 65-spike quarantine & Mateo-March w_sup/sex."""

    def test_townsend_2_segment_altitude_and_tau_extension(self):
        alt_2500 = pe.compute_townsend_altitude(2500.0, cp_watts=294.0, w_prime_kj=31.4)
        self.assertEqual(alt_2500["w_prime_pct"], 100.0)
        self.assertAlmostEqual(alt_2500["w_prime_altitude_kj"], 31.4, places=1)
        self.assertAlmostEqual(alt_2500["cp_loss_pct"], 8.0, places=1)

        alt_3000 = pe.compute_townsend_altitude(3000.0, cp_watts=294.0, w_prime_kj=31.4)
        self.assertTrue(1.35 <= alt_3000["tau_w_altitude_mult"] <= 1.50, alt_3000)

        alt_3500 = pe.compute_townsend_altitude(3500.0, cp_watts=294.0, w_prime_kj=31.4)
        # Townsend > 2.76 km: 100 - 18.3 * (3.50 - 2.76) = 86.5%
        self.assertAlmostEqual(alt_3500["w_prime_pct"], 86.5, places=1)
        self.assertLess(alt_3500["w_prime_altitude_kj"], 31.4)

    def test_heat_acclimation_indoor_and_drivetrain_offsets(self):
        # 32°C unacclimatized -> -3.5% vs 14d acclimatized -> near 0%
        hot_unaccl = pe.compute_effective_cp_w_prime(294.0, 31400.0, temp_c=32.0, heat_accl_days=0)
        hot_accl = pe.compute_effective_cp_w_prime(294.0, 31400.0, temp_c=32.0, heat_accl_days=14)
        self.assertAlmostEqual(hot_unaccl["heat_cp_loss_pct"], 3.5, places=1)
        self.assertGreater(hot_accl["cp_eff_w"], hot_unaccl["cp_eff_w"] + 7.0)

        # Indoor (-19 W) + KICKR drivetrain (-2.4%) = (294 - 19) * 0.976 = 268.4 W
        eff_in = pe.compute_effective_cp_w_prime(294.0, 31400.0, indoor=True, indoor_cp_offset_w=-19.0, drivetrain_loss_pct=2.4)
        self.assertAlmostEqual(eff_in["cp_eff_w"], 268.4, delta=0.2)

    def test_mateo_march_spike_filter_locks_65_spike_session_and_preserves_intervals(self):
        # 20-min ride @ 348 W with 65 isolated +15% spikes (~400 W) -> cleaned & locked!
        watts = [348.0] * 1200
        for k in range(65):
            watts[10 + k * 15] = 402.0  # +15.5% isolated 1s glitch
        cleaned, meta = pe.filter_power_spikes(watts)
        self.assertEqual(meta["spike_count"], 65)
        self.assertTrue(meta["locked_for_records"])
        self.assertAlmostEqual(sum(cleaned) / len(cleaned), 348.0, delta=0.2)

        # Locked session is excluded from aggregate_epoch_curves()
        sm = ce.compute_stream_metrics({"watts": watts}, ftp_watts=294, weight_kg=75.0, sport="cycling", cp_watts=294.0, w_prime_j=31400.0)
        self.assertTrue(sm.get("locked_for_records"))
        today_iso = dt.date.today().isoformat()
        curves = ce.aggregate_epoch_curves([{"date": today_iso, "sport": "cycling", "device_watts": True, **sm}], today=dt.date.today())
        self.assertIsNone(curves["fresh"]["20m"].get("90d"))

        # Legitimate 30/15 intervals & progressive sprint acceleration produce ZERO false spikes
        legit = [200.0] * 30 + [400.0] * 30 + [200.0] * 15 + [350.0, 550.0, 800.0, 1050.0, 1100.0, 1080.0, 1020.0, 950.0]
        _, meta_legit = pe.filter_power_spikes(legit)
        self.assertEqual(meta_legit["spike_count"], 0)
        self.assertFalse(meta_legit["locked_for_records"])

    def test_mateo_march_intensity_weighted_durability_and_sex_paradox(self):
        # Exact Mateo-March 2024 male anchors: 2.5 / 5.0 / 7.5 kJ/kg > CP -> -2.2% / -6.1% / -16.2% CP
        m_25 = pe.compute_effective_cp_w_prime(300.0, 25000.0, mass_kg=75.0, prior_sup_kj=2.5 * 75.0, sex="M")
        m_50 = pe.compute_effective_cp_w_prime(300.0, 25000.0, mass_kg=75.0, prior_sup_kj=5.0 * 75.0, sex="M")
        m_75 = pe.compute_effective_cp_w_prime(300.0, 25000.0, mass_kg=75.0, prior_sup_kj=7.5 * 75.0, sex="M")
        self.assertAlmostEqual(m_25["cp_drop_pct"], 2.2, delta=0.2)
        self.assertAlmostEqual(m_50["cp_drop_pct"], 6.1, delta=0.2)
        self.assertAlmostEqual(m_75["cp_drop_pct"], 16.2, delta=0.2)

        # Same 7.5 kJ/kg BELOW CP drops CP far less than 7.5 kJ/kg ABOVE CP
        m_sub75 = pe.compute_effective_cp_w_prime(300.0, 25000.0, mass_kg=75.0, prior_kj=7.5 * 75.0, prior_sup_kj=0.0, sex="M")
        self.assertLess(m_75["cp_eff_w"], m_sub75["cp_eff_w"] - 40.0)

        # Sex paradox: at 5.0 kJ/kg > CP (300 kJ @ 60 kg), female loses -29.6% >> male -6.1%
        f_50 = pe.compute_effective_cp_w_prime(230.0, 12000.0, mass_kg=60.0, prior_sup_kj=300.0, sex="F")
        self.assertAlmostEqual(f_50["cp_drop_pct"], 29.6, delta=0.3)
        self.assertGreater(f_50["cp_drop_pct"], m_50["cp_drop_pct"] * 3.0)

        # Sub-CP paradox: on pure 25 kJ/kg sub-CP endurance, female loses LESS than male
        f_sub = pe.compute_effective_cp_w_prime(230.0, 12000.0, mass_kg=60.0, prior_kj=25.0 * 60.0, sex="F")
        m_sub = pe.compute_effective_cp_w_prime(230.0, 12000.0, mass_kg=60.0, prior_kj=25.0 * 60.0, sex="M")
        self.assertLess(f_sub["cp_drop_pct"], m_sub["cp_drop_pct"])


# ---------------------------------------------------------------------------
# Block 5: Running Bridge, RED-S Guard, Readiness Overrides & CLI End-to-End
# ---------------------------------------------------------------------------

class TestBlock5RunningBridgeRedsAndReadinessOverrides(unittest.TestCase):
    """CS/D', Minetti, Morin k_leg, RE, RED-S 28d guard, HRmax/k_leg readiness overrides & CLI."""

    def test_running_cs_d_prime_minetti_re_and_morin_k_leg(self):
        # 1k=3:20 (200s), 3k=10:55 (655s), 5k=18:29 (1109s) -> CS = 4.40 m/s, D' = 119 m
        cs = pe.solve_running_cs_d_prime({"1km": "3:20", "3km": "10:55", "5km": "18:29"})
        self.assertTrue(cs["valid"])
        self.assertAlmostEqual(cs["cs_mps"], 4.40, places=2)
        self.assertEqual(cs["d_prime_m"], 119)
        self.assertGreaterEqual(cs["r2"], 0.9999)

        # Minetti 5th-order polynomial: flat=3.60, -12%=1.99 J/(kg·m)
        self.assertAlmostEqual(pe.minetti_running_cost_j_kg_m(0.0), 3.60, places=2)
        self.assertAlmostEqual(pe.minetti_running_cost_j_kg_m(-0.12), 1.99, places=2)

        # Running Effectiveness RE = v / (P/M)
        self.assertAlmostEqual(pe.compute_running_effectiveness(4.0, 300.0, 75.0), 1.000, places=3)

        # Morin leg-spring stiffness k_leg drops ~33% under eccentric downhill fatigue
        k_fresh = pe.compute_morin_leg_stiffness(75.0, 186.0, 4.0, 0.22, 0.14)
        k_fatig = pe.compute_morin_leg_stiffness(75.0, 186.0, 4.0, 0.26, 0.10)
        self.assertAlmostEqual(k_fresh, 11.26, delta=0.1)
        self.assertAlmostEqual(k_fatig, 7.56, delta=0.1)
        self.assertGreater(((k_fresh - k_fatig) / k_fresh) * 100.0, 30.0)

        # Stream-level automatic `run_k_leg_kn_m`, `run_k_leg_drop_pct`, `run_minetti_avg_j_kg_m`, `run_re_kg_n`
        streams = {
            "velocity_smooth": [4.0] * 120,
            "grade_smooth": [-12.0] * 120,
            "watts": [300.0] * 120,
            "contact_time_s": [0.22] * 60 + [0.26] * 60,
            "flight_time_s": [0.14] * 60 + [0.10] * 60,
            "height_cm": 186.0,
        }
        sm = ce.compute_stream_metrics(streams, ftp_watts=294, weight_kg=75.0, sport="running")
        self.assertAlmostEqual(sm["run_minetti_avg_j_kg_m"], 1.99, places=2)
        self.assertAlmostEqual(sm["run_re_kg_n"], 1.000, places=3)
        self.assertIsNotNone(sm.get("run_k_leg_kn_m"))
        self.assertGreater(sm.get("run_k_leg_drop_pct", 0.0), 25.0)

    def test_reds_warning_flag_triggers_only_on_rapid_28d_loss_plus_suppressed_hrv_plus_stagnation(self):
        today = dt.date(2026, 11, 19)
        body = {"mass_kg_7d": 57.5, "kfa_pct": 16.0, "kfa_calibrated": True}
        prof = {"height_cm": 168.0, "sex": "female", "bike_weight_kg": 7.5}
        # Partner scenario: 60.0 -> 57.5 kg (-4.2% in 28d), HRV collapsing 72 -> 44 ms, stagnant 5m/20m power
        rows_reds = [
            {"date": (today - dt.timedelta(days=26)).isoformat(), "weight_kg": 60.0, "hrv_rmssd": 72.0},
            {"date": (today - dt.timedelta(days=22)).isoformat(), "weight_kg": 59.8, "hrv_rmssd": 70.0},
            {"date": (today - dt.timedelta(days=15)).isoformat(), "weight_kg": 59.0, "hrv_rmssd": 65.0},
            {"date": (today - dt.timedelta(days=5)).isoformat(), "weight_kg": 57.8, "hrv_rmssd": 46.0},
            {"date": (today - dt.timedelta(days=2)).isoformat(), "weight_kg": 57.6, "hrv_rmssd": 45.0},
            {"date": today.isoformat(), "weight_kg": 57.5, "hrv_rmssd": 44.0},
        ]
        curves_stagnant = {"fresh": {"5m": {"42d": 270, "90d": 275}, "20m": {"42d": 240, "90d": 242}}}
        p6_reds = pe.compute_pillar6_locomotion_environment(prof, body, [], curves_stagnant, 230.0, 12.0, rows_reds, today=today)
        self.assertTrue(p6_reds["reds_warning_flag"], p6_reds)
        self.assertIn("RED-S", p6_reds["reds_warning"])

        # Healthy cut: mild -1.5% weight change + strong HRV + rising power -> NO false alarm!
        rows_ok = [
            {"date": (today - dt.timedelta(days=25)).isoformat(), "weight_kg": 60.0, "hrv_rmssd": 68.0},
            {"date": (today - dt.timedelta(days=18)).isoformat(), "weight_kg": 59.7, "hrv_rmssd": 70.0},
            {"date": (today - dt.timedelta(days=5)).isoformat(), "weight_kg": 59.2, "hrv_rmssd": 72.0},
            {"date": today.isoformat(), "weight_kg": 59.1, "hrv_rmssd": 73.0},
        ]
        curves_rising = {"fresh": {"5m": {"42d": 290, "90d": 275}, "20m": {"42d": 255, "90d": 242}}}
        p6_ok = pe.compute_pillar6_locomotion_environment(prof, body, [], curves_rising, 235.0, 12.0, rows_ok, today=today)
        self.assertFalse(p6_ok["reds_warning_flag"], p6_ok)

    def test_hrmax_blunting_and_k_leg_drop_override_green_rhr(self):
        today = dt.date(2026, 11, 19)
        green_rows = [
            {"date": (today - dt.timedelta(days=i)).isoformat(), "rhr": 45, "hrv_rmssd": 75.0, "sleep_h": 8.0, "leg_pain": 1}
            for i in range(7)
        ]
        green_base = {"status": "GREEN", "baseline_rhr": 45}
        prof = {"hr_max": 200, "rhr": 45, "cp_w": 294}

        # 1. Green baseline without anomalies -> GRÜN
        p7_ok = pe.compute_pillar7_and_stage2_readiness(green_rows, [], prof, green_base, 294.0, myotype_z=3.19, today=today)
        self.assertEqual(p7_ok["readiness_flag_4stage"], "GRÜN")

        # 2. Downhill run with -32% k_leg drop -> overrides green RHR/HRV to GELB-B!
        acts_kleg = [{"date": (today - dt.timedelta(days=1)).isoformat(), "sport": "running", "duration_min": 55, "run_k_leg_kn_m": 7.6, "run_k_leg_drop_pct": 32.0}]
        p7_kleg = pe.compute_pillar7_and_stage2_readiness(green_rows, acts_kleg, prof, green_base, 294.0, myotype_z=3.19, today=today)
        self.assertTrue(p7_kleg["k_leg_collapsed"])
        self.assertEqual(p7_kleg["readiness_flag_4stage"], "GELB-B")

        # 3. Blunted HRmax (max_hr=182 vs hr_max=200 on RPE 9 hard session) -> overrides green RHR/HRV to ROT!
        acts_hr = [{"date": (today - dt.timedelta(days=1)).isoformat(), "sport": "cycling", "duration_min": 70, "rpe": 9, "tss": 95, "max_hr": 182, "avg_w": 285}]
        p7_hr = pe.compute_pillar7_and_stage2_readiness(green_rows, acts_hr, prof, green_base, 294.0, myotype_z=3.19, today=today)
        self.assertTrue(p7_hr["hrmax_blunted"])
        self.assertEqual(p7_hr["readiness_flag_4stage"], "ROT")

    def test_cli_simulate_new_flags_end_to_end(self):
        """End-to-end CLI smoke test for `--preload-sup-kj`, `--sex`, `--alt-m`, `--temp-c`, `--indoor`, `--drivetrain-loss`, `--run-splits`, `--grade`, `--morin`."""
        tmp = tempfile.mkdtemp(prefix="atlas_phys_cli_")
        try:
            atlas_py = os.path.join(REPO, "core", "tools", "atlas.py")
            # Minimal installed Atlas workspace (same layout as tests/run.py)
            res_init = subprocess.run(
                [sys.executable, "-I", atlas_py, "--root", tmp, "init", "--source", REPO, "--name", "Test", "--language", "de", "--execute"],
                capture_output=True, text=True, check=True,
            )
            self.assertEqual(res_init.returncode, 0)
            inst_atlas = os.path.join(tmp, "brain", "_atlas", "tools", "atlas.py")
            subprocess.run(
                [sys.executable, "-I", inst_atlas, "coach", "context", "--init", "--execute"],
                capture_output=True, text=True, check=True,
            )
            # 1. Sierra Nevada altitude + heat + solve
            r1 = subprocess.run(
                [sys.executable, "-I", inst_atlas, "coach", "simulate",
                 "--solve", "float-30-15", "--cp", "294", "--w-prime", "31.4", "--mass-kg", "75",
                 "--alt-m", "2500", "--temp-c", "32", "--heat-accl-days", "0"],
                capture_output=True, text=True, check=True,
            )
            self.assertIn("alt=2500m", r1.stdout)
            self.assertIn("temp=32C(accl=0d)", r1.stdout)
            self.assertIn("tau_SC=", r1.stdout)

            # 2. Female supra-CP durability scenario (60 kg, CP 230 W, 300 kJ > CP)
            r2 = subprocess.run(
                [sys.executable, "-I", inst_atlas, "coach", "simulate",
                 "--cp", "230", "--w-prime", "12.0", "--mass-kg", "60",
                 "--sex", "F", "--preload-sup-kj", "300"],
                capture_output=True, text=True, check=True,
            )
            self.assertIn("-29.6%", r2.stdout)

            # 3. Indoor + KICKR drivetrain loss (-19 W + -2.4%)
            r3 = subprocess.run(
                [sys.executable, "-I", inst_atlas, "coach", "simulate",
                 "--cp", "294", "--w-prime", "31.4", "--indoor", "--drivetrain-loss"],
                capture_output=True, text=True, check=True,
            )
            self.assertIn("268.4W", r3.stdout)

            # 4. Running Equivalence Bridge (CS/D', Minetti -12%, Morin k_leg drop -> GELB-B)
            r4 = subprocess.run(
                [sys.executable, "-I", inst_atlas, "coach", "simulate",
                 "--mass-kg", "75", "--run-splits", "1k=3:20,3k=10:55,5k=18:29",
                 "--grade", "0,-0.12", "--morin", "v=4.0,tc=0.22,tf=0.14;v=4.0,tc=0.26,tf=0.10",
                 "--re", "v=4.0,p=300"],
                capture_output=True, text=True, check=True,
            )
            self.assertIn("CS=4.4 m/s", r4.stdout)
            self.assertIn("D'=119 m", r4.stdout)
            self.assertIn("1.99 J/(kg·m)", r4.stdout)
            self.assertIn("GELB-B PERIPHERAL OVERRIDE", r4.stdout)
            self.assertIn("1.0 kg/N", r4.stdout)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestBlock6TauReconstitutionAndMyotypeSplit(unittest.TestCase):
    """Block 6: Exact inverse `tau_w_mult` calibration, stream detection, seasonal re-solve & Fast-Myotype 3-day test split."""

    def test_fit_tau_w_mult_exact_roundtrip_and_guards(self):
        cp, wp = 300.0, 30000.0
        for true_mult in (0.65, 1.00, 1.45):
            # Forward-simulate exact Chorley ODE for 180s all-out + 120s @ 50% CP
            fc_amp = 0.43 * wp
            sc_amp = 0.57 * wp
            fc_bal, sc_bal = 0.0, 0.0  # fully depleted at end of 180s all-out
            t_fc, t_sc = pe.chorley_recovery_taus(50.0, tau_mult=true_mult)
            for _ in range(120):
                fc_bal = fc_amp - (fc_amp - fc_bal) * math.exp(-1.0 / max(15.0, t_fc))
                sc_bal = sc_amp - (sc_amp - sc_bal) * math.exp(-1.0 / max(60.0, t_sc))
            w_avail = fc_bal + sc_bal
            b2_avg = cp + w_avail / 180.0
            fit = pe.fit_tau_w_mult_from_bouts(
                b1_dur_s=180,
                b1_avg_w=cp + wp / 180.0,
                rec_dur_s=120,
                rec_avg_w=0.50 * cp,
                b2_dur_s=180,
                b2_avg_w=b2_avg,
                cp_watts=cp,
                w_prime_j=wp,
                creatine_flag=True,
            )
            self.assertTrue(fit["valid"], fit)
            self.assertAlmostEqual(fit["tau_w_mult"], true_mult, delta=0.02, msg=f"mult={true_mult}: {fit}")
            self.assertFalse(fit["clamped"])
            self.assertEqual(fit["confidence"], "high")

        # Submaximal Bout 1 (<70% W') must be rejected
        bad = pe.fit_tau_w_mult_from_bouts(180, 340.0, 120, 150.0, 180, 340.0, cp_watts=cp, w_prime_j=wp)
        self.assertFalse(bad["valid"])
        self.assertIn("bout1_submaximal", bad["reason"])

    def test_detect_stream_reconstitution_and_reject_multiset_intervals(self):
        cp, wp = 300.0, 28000.0
        # Genuine Unit 1 (Pro): 15m WU + 15s sprint + 20m Z1 + 3m All-Out + 2m Z1 + 3m All-Out + 10m CD
        b1_w = cp + wp / 180.0
        b2_w = cp + (0.60 * wp) / 180.0
        pro_stream = (
            [180.0] * 900
            + [950.0] * 15
            + [150.0] * 1200
            + [b1_w] * 180
            + [150.0] * 120
            + [b2_w] * 180
            + [150.0] * 600
        )
        det = pe.detect_w_reconstitution_test(pro_stream, cp_watts=cp, w_prime_j=wp, creatine_flag=True, title="Field-Test Unit 1 (Pro)")
        self.assertIsNotNone(det)
        self.assertEqual(det["b1_dur_s"], 180)
        self.assertEqual(det["rec_dur_s"], 120)
        self.assertEqual(det["b2_dur_s"], 180)
        self.assertLess(det["tau_w_mult_at_sync"], 0.90)

        # Normal 4x3m interval workout MUST NOT trigger false-positive reconstitution detection
        iv_stream = [180.0] * 600
        for _ in range(4):
            iv_stream += [cp + 0.78 * wp / 180.0] * 180 + [150.0] * 180
        iv_stream += [150.0] * 600
        self.assertIsNone(pe.detect_w_reconstitution_test(iv_stream, cp_watts=cp, w_prime_j=wp, title="4x3m VO2max"))

    def test_seasonal_resolve_staleness_and_altitude_multiplication(self):
        today = dt.date(2026, 12, 15)
        cp, wp = 300.0, 30000.0
        # Legacy profile without tau_w_mult defaults cleanly to 1.0
        def_res = pe.resolve_individual_tau_w_mult({}, [], cp_watts=cp, w_prime_j=wp, today=today)
        self.assertEqual(def_res["tau_w_mult"], 1.0)
        self.assertEqual(def_res["source"], "population_default")
        self.assertFalse(def_res["stale"])

        # Recent stream test dynamically re-solves against current CP/W' and beats stale profile
        acts = [{
            "date": "2026-12-01",
            "w_reconst_test": {
                "b1_dur_s": 180, "b1_avg_w": 467.0,
                "rec_dur_s": 120, "rec_avg_w": 150.0,
                "b2_dur_s": 180, "b2_avg_w": 405.0,
            },
        }]
        res_fresh = pe.resolve_individual_tau_w_mult(
            {"tau_w_mult": 1.30, "tau_w_date": "2026-08-01"},
            acts, cp_watts=cp, w_prime_j=wp, creatine_flag=True, today=today,
        )
        self.assertEqual(res_fresh["source"], "reconstitution_test")
        self.assertLess(res_fresh["tau_w_mult"], 0.85)
        self.assertFalse(res_fresh["stale"])

        # Test >90 days old is marked stale for seasonal re-calibration
        res_stale = pe.resolve_individual_tau_w_mult(
            {}, acts, cp_watts=cp, w_prime_j=wp, creatine_flag=True, today=dt.date(2027, 3, 20),
        )
        self.assertTrue(res_stale["stale"])
        self.assertGreater(res_stale["age_days"], 90)

        # Altitude multiplies base_tau_w_mult cleanly (1.20 * altitude_mult > 1.20)
        eff_alt = pe.compute_effective_cp_w_prime(300.0, 30000.0, elev_m=2500.0, base_tau_w_mult=1.20)
        self.assertAlmostEqual(eff_alt["tau_w_mult"], round(1.20 * 1.30, 2), places=2)

        # Individual tau_w_mult propagates into interval prescriptions (slower recovery -> lower Hard-Start hold watts)
        rx_fast = pe.solve_individual_interval_prescriptions(300.0, 30000.0, p_5min_w=420.0, base_tau_w_mult=0.70)
        rx_slow = pe.solve_individual_interval_prescriptions(300.0, 30000.0, p_5min_w=420.0, base_tau_w_mult=1.50)
        self.assertGreater(rx_fast["vo2max_hard_start_4x4"]["hold_w"], rx_slow["vo2max_hard_start_4x4"]["hold_w"])

    def test_fast_myotype_3day_split_vs_slow_unit2_gate(self):
        base_args = dict(
            profile={"primary_sport": "cycling", "sex": "male", "age": 26, "creatine_flag": True, "explicit_fields": ["weight_kg", "kfa_pct", "height_cm", "rhr", "hr_max"]},
            body={"mass_calibrated": True, "kfa_calibrated": True},
            pillar2={"sprint_anchor_status": "calibrated"},
            cp_model={"valid": False, "quality_flag": "insufficient anchors"},
            readiness_stage2={},
            playbook_info={"rules": [], "answered_keys": set(), "exists": True},
            activities=[],
            cal_items=[],
            macro_info=None,
            dna_snapshot=None,
            today=dt.date(2026, 11, 19),
        )
        fast_audit = pe.audit_missing_inputs_and_questions(
            **base_args, pillar4={"myotype_z_score": 2.4}, effective_w_prime_kj=30.0, depth="pro",
        )
        fast_txt = "\n".join(fast_audit["calibration_gaps"])
        self.assertIn("--unit 2a", fast_txt)
        self.assertIn("--unit 2b", fast_txt)
        self.assertIn("do NOT combine 5m + 12m", fast_txt)

        slow_audit = pe.audit_missing_inputs_and_questions(
            **base_args, pillar4={"myotype_z_score": -1.2}, effective_w_prime_kj=14.0, depth="standard",
        )
        slow_txt = "\n".join(slow_audit["calibration_gaps"])
        self.assertIn("--unit 2", slow_txt)
        self.assertNotIn("do NOT combine 5m + 12m", slow_txt)


if __name__ == "__main__":
    unittest.main(verbosity=2)
