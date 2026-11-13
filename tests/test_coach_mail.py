"""Fast, pure-stdlib unit tests for `coach_engine.py` (Simple-to-Pro physics) and `atlas_mail.py` helpers."""

import datetime as dt
import os
import sys
import unittest

sys.dont_write_bytecode = True

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS_DIR = os.path.join(REPO, "core", "tools")
if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)

from atlas_lib import coach_engine as ce  # noqa: E402
import atlas_mail as am  # noqa: E402


class CoachMailUnit(unittest.TestCase):
    def test_cp_w_prime_and_cs_d_prime_quality_gating(self):
        # 1. Exact Monod-Scherrer synthetic curve: CP = 300 W, W' = 21,600 J (21.6 kJ)
        # P(t) = 300 + 21600 / t
        exact_mmp = {
            "5s": 1150,
            "15s": 920,
            "1m": 660,
            "3m": int(round(300 + 21600 / 180)),   # 420 W
            "5m": int(round(300 + 21600 / 300)),   # 372 W
            "10m": int(round(300 + 21600 / 600)),  # 336 W
            "20m": int(round(300 + 21600 / 1200)), # 318 W
        }
        cp_ok = ce.solve_cp_w_prime(exact_mmp)
        self.assertTrue(cp_ok["valid"], cp_ok)
        self.assertAlmostEqual(cp_ok["cp_w"], 300, delta=1)
        self.assertAlmostEqual(cp_ok["w_prime_kj"], 21.6, delta=0.2)
        self.assertGreaterEqual(cp_ok["r2"], 0.999)

        # 2. Missing <5m short anchor -> gated invalid even with good line
        missing_short = {"5m": 372, "10m": 336, "20m": 318}
        cp_no_short = ce.solve_cp_w_prime(missing_short)
        self.assertFalse(cp_no_short["valid"])
        self.assertIn("missing <5m", cp_no_short["quality_flag"])

        # 3. Missing >=12m long anchor -> gated invalid
        missing_long = {"3m": 420, "5m": 372, "10m": 336}
        cp_no_long = ce.solve_cp_w_prime(missing_long)
        self.assertFalse(cp_no_long["valid"])
        self.assertIn("missing >=12m", cp_no_long["quality_flag"])

        # 4. Noisy / non-monotonic Z2 ride curve (R^2 < 0.98) -> gated invalid
        noisy_mmp = {"3m": 310, "5m": 340, "10m": 220, "20m": 285}
        cp_noisy = ce.solve_cp_w_prime(noisy_mmp)
        self.assertFalse(cp_noisy["valid"])
        self.assertLess(cp_noisy["r2"], 0.98)

        # 5. Running CS/D' solver: CS = 4.20 m/s (3:58/km), D' = 180 m -> t(d) = (d - 180) / 4.20
        best_paces = {
            "1km": {"sec_per_km": (1000.0 - 180.0) / 4.20},           # 195.24 s/km
            "3km": {"sec_per_km": ((3000.0 - 180.0) / 4.20) / 3.0},   # 223.81 s/km
            "5km": {"sec_per_km": ((5000.0 - 180.0) / 4.20) / 5.0},   # 229.52 s/km
        }
        cs_ok = ce.solve_cs_d_prime(best_paces)
        self.assertTrue(cs_ok["valid"], cs_ok)
        self.assertAlmostEqual(cs_ok["cs_mps"], 4.20, places=2)
        self.assertAlmostEqual(cs_ok["d_prime_m"], 180, delta=2)
        self.assertGreaterEqual(cs_ok["r2"], 0.999)

    def test_skiba_w_bal_typology_torque_and_durability(self):
        # 1. 1Hz Skiba ODE: 60s @ 450W above CP=300W (burns 9,000 J -> 55% of 20kJ),
        #    300s @ 150W recovery (exponential reconstitution), then 80s @ 450W (burns 12,000 J)
        watts = [450.0] * 60 + [150.0] * 300 + [450.0] * 80 + [180.0] * 120
        skiba = ce.compute_skiba_w_bal(watts, cp_watts=300.0, w_prime_j=20000.0)
        self.assertLess(skiba["w_bal_min_pct"], 35.0)
        self.assertGreater(skiba["w_bal_min_pct"], 0.0)
        self.assertEqual(skiba["matches_burned"], 2)
        self.assertAlmostEqual(skiba["kj_above_cp"], 21.0, places=1)

        # 2. Muscle Fiber Typology: Fast sprinter vs Slow diesel
        fast_mmp = {"5s": 1320, "15s": 1050, "1m": 650, "5m": 380, "20m": 305}
        slow_mmp = {"5s": 760, "15s": 660, "1m": 460, "5m": 345, "20m": 310}
        t_fast = ce.estimate_fiber_typology(fast_mmp, cp_watts=300.0)
        t_slow = ce.estimate_fiber_typology(slow_mmp, cp_watts=300.0)
        self.assertEqual(t_fast["typology"], "Fast")
        self.assertEqual(t_slow["typology"], "Slow")
        self.assertGreater(t_fast["pdc_b"], t_slow["pdc_b"])

        # 3. Neuromuscular Torque & Q1–Q4 quadrants
        tq_watts = [380.0] * 60 + [360.0] * 60 + [180.0] * 60 + [200.0] * 60
        tq_cad = [98.0] * 60 + [65.0] * 60 + [72.0] * 60 + [96.0] * 60
        tq = ce.compute_torque_cadence_quadrants(tq_watts, tq_cad, cp_watts=280.0, ref_cadence=88.0)
        self.assertGreater(tq["peak_torque_nm"], 50.0)  # 360W @ 65 rpm -> ~52.9 Nm
        self.assertEqual(tq["f_opt_rpm"], 82)           # mean of >=90% Pmax (380W@98rpm & 360W@65rpm)
        q_sum = sum(tq["quadrants"].values())
        self.assertAlmostEqual(q_sum, 100.0, delta=0.2)

        # 4. Multi-Tier Durability (>15, >25, >35 kJ/kg + >5 kJ/kg above CP) for 60 kg athlete (CP=250W):
        #    35 kJ/kg = 2,100 kJ -> 7000s @ 300W = 2,100 kJ total AND 350 kJ (>5 kJ/kg = 300 kJ) above CP!
        dur_watts = [300.0] * 7000 + [340.0] * 320
        dur = ce.compute_multi_tier_durability(dur_watts, weight_kg=60.0, cp_watts=250.0)
        self.assertIn("15kj_kg", dur)
        self.assertIn("25kj_kg", dur)
        self.assertIn("35kj_kg", dur)
        self.assertIn("5kj_above_cp_kg", dur)
        self.assertEqual(dur["35kj_kg"]["5m"], 340)
        self.assertEqual(dur["5kj_above_cp_kg"]["5m"], 340)

        # 5. Steady-state Pw:Hr decoupling excludes coasting & supra-CP surges
        #    First steady half: 220W @ 140 bpm; insert 30s 450W surge; second steady half: 220W @ 150 bpm (~6.7% drift)
        ss_watts = [200.0] * 600 + [220.0] * 900 + [0.0] * 60 + [450.0] * 20 + [220.0] * 930
        ss_hr = [132.0] * 600 + [140.0] * 900 + [110.0] * 60 + [182.0] * 20 + [175.0] * 30 + [150.0] * 900
        dec = ce.compute_steady_state_decoupling(ss_watts, ss_hr, cp_watts=280.0)
        self.assertIsNotNone(dec)
        self.assertAlmostEqual(dec, 6.7, delta=0.5)

    def test_metabolic_fueling_environment_pmc_and_guardrails(self):
        # 1. Lab-free Metabolic Profile + Honest Glycolytic Index + Fueling + Altitude/Heat
        mmp = {"15s": 1020, "5m": 370, "20m": 300}
        met = ce.compute_metabolic_profile(
            mmp, cp_watts=280.0, w_prime_j=24000.0, weight_kg=67.0, age=30, sex="male",
            hr_max=192, rhr=46, best_paces={"5km": "3:42/km"},
        )
        self.assertGreater(met["vo2max_storer"], 60.0)
        self.assertGreater(met["vo2max_uth"], 60.0)
        self.assertGreater(met["running_vdot"], 50.0)
        self.assertEqual(met["glycolytic_band"], "High (Punchy/Glycolytic)")
        self.assertIsNotNone(met["fatmax_watts"])

        fuel = ce.compute_substrate_fueling(avg_watts=235.0, duration_s=10800, cp_watts=280.0, map_5m_w=370.0, if_val=0.84)
        self.assertGreaterEqual(fuel["fueling_g_h"], 90)
        self.assertIn("1:0.8", fuel["fueling_recommendation"])

        env = ce.compute_environment_adjustments([2000.0] * 100, [29.5] * 100, cp_watts=300.0)
        self.assertAlmostEqual(env["hypoxia_loss_pct"], 6.3, places=1)
        self.assertEqual(env["cp_alt_adjusted_w"], 281)
        self.assertTrue(env["thermal_drift"])

        # 2. Cross-Sport PMC (Cycling TSS + Running rTSS + HR hrTSS + Strength sRPE)
        today = dt.date(2026, 11, 13)
        acts = [
            {"date": (today - dt.timedelta(days=3)).isoformat(), "sport": "cycling", "duration_min": 90, "np_w": 260, "tss": 115},
            {"date": (today - dt.timedelta(days=2)).isoformat(), "sport": "running", "duration_min": 50, "distance_km": 11.0},
            {"date": (today - dt.timedelta(days=1)).isoformat(), "sport": "strength", "duration_min": 60, "rpe": 8},
            {"date": today.isoformat(), "sport": "hiking", "duration_min": 120, "avg_hr": 148},
        ]
        prof = {"ftp_w": 280, "hr_max": 190, "rhr": 48, "lthr": 172, "sex": "male"}
        pmc = ce.compute_pmc_metrics(acts, profile=prof, cs_mps=4.0, cp_watts=280.0, today=today)
        self.assertGreater(pmc["ctl"], 5.0)
        self.assertGreater(pmc["atl"], pmc["ctl"])
        self.assertLess(pmc["tsb"], 0.0)
        self.assertGreater(pmc["weekly_load_7d"], 250)

        # 3. Readiness RED gating + AMPK/mTORC1 interference + Bosquet 8–14d taper
        r_rows = [
            {"date": today.isoformat(), "rhr": 57, "sleep_h": 5.1, "soreness": 4},
            {"date": (today - dt.timedelta(days=3)).isoformat(), "rhr": 47, "sleep_h": 7.8, "soreness": 1},
            {"date": (today - dt.timedelta(days=5)).isoformat(), "rhr": 48, "sleep_h": 7.6, "soreness": 2},
        ]
        cal_items = [
            {"kind": "goal", "subkind": "A", "status": "open", "start": today + dt.timedelta(days=10), "end": today + dt.timedelta(days=10), "what": "Alpenbrevet"},
            {"kind": "workout", "subkind": "cycling", "status": "open", "start": today + dt.timedelta(days=1), "end": today + dt.timedelta(days=1), "time": "17:00", "what": "VO2max 5x4m@115%"},
            {"kind": "workout", "subkind": "strength", "status": "open", "start": today + dt.timedelta(days=1), "end": today + dt.timedelta(days=1), "time": "19:30", "what": "Heavy Squats"},
        ]
        rdy = ce.evaluate_readiness_and_guardrails(r_rows, prof, pmc, acts, cal_items=cal_items, today=today)
        self.assertEqual(rdy["status"], "RED")
        self.assertTrue(any("RHR +9" in r or "RHR +10" in r for r in rdy["reasons"]), rdy["reasons"])
        self.assertTrue(any("AMPK/mTORC1" in g for g in rdy["guardrails"]), rdy["guardrails"])
        self.assertIsNotNone(rdy["taper"])
        self.assertTrue(rdy["taper"]["active"])
        self.assertGreaterEqual(rdy["taper"]["volume_reduction_pct"], 41)

        # 4. Readiness Markdown round-trip with sleep_quality ("7.5 (poor)" / "6.8 (2/5)")
        md_rendered = ce.render_readiness_markdown(
            "",
            [{"date": today.isoformat(), "rhr": 49, "sleep_h": 6.8, "sleep_quality": "2/5", "soreness": 2, "status": "AMBER"}],
            today.isoformat(),
        )
        parsed_back = ce.parse_readiness_log(md_rendered)
        self.assertEqual(len(parsed_back), 1)
        self.assertAlmostEqual(parsed_back[0]["sleep_h"], 6.8, places=1)
        self.assertEqual(parsed_back[0]["sleep_quality"], "2/5")

    def test_adaptive_depth_simple_jargon_free_and_mail_helpers(self):
        today = dt.date(2026, 11, 13)
        casual_md = (
            "---\nsummary: Profile\ntags: [sport]\ndepth: auto\n---\n# Profile\n\n"
            "- **Primary Sport:** Running\n"
            "- **Weekly Budget:** Run 2x/week, stay healthy, nothing fancy\n"
            "- **Schedule:** Tue & Sat mornings\n"
        )
        p_casual = ce.parse_athlete_profile(casual_md)
        depth_casual = ce.resolve_coach_depth(profile_info=p_casual, activities=[], readiness_rows=[], strava_connected=False)
        self.assertEqual(depth_casual, "simple")

        # Physiology query promotes `auto` straight to `pro`
        depth_pro_q = ce.resolve_coach_depth(
            profile_info=p_casual, activities=[], readiness_rows=[], strava_connected=False,
            query_text="Wie hoch ist meine W'bal und mein Kohlenhydrat-Bedarf in g/h?",
        )
        self.assertEqual(depth_pro_q, "pro")

        # Verify SIMPLE context block contains ZERO technical jargon while surfacing [blocked] days
        cal_items = [
            {
                "kind": "blocked",
                "status": "open",
                "start": today + dt.timedelta(days=2),
                "end": today + dt.timedelta(days=3),
                "delta": 2,
                "time": "",
                "what": "Family Weekend Trip",
            }
        ]
        simple_blk = ce.format_simple_context_block("plan-14d", p_casual, [], cal_items, today=today)
        self.assertIn("COACH CONTEXT (SIMPLE", simple_blk)
        self.assertIn("Family Weekend Trip", simple_blk)
        for forbidden in ("FTP", "W'bal", "CTL", "TSB", "ACWR", "RHR", ".zwo", "/strava-connect", "VO2max"):
            self.assertNotIn(forbidden, simple_blk)

        # Mail helpers: ICS event extraction & server UID SEARCH criteria builder
        ics_bytes = (
            b"BEGIN:VCALENDAR\r\n"
            b"BEGIN:VEVENT\r\n"
            b"SUMMARY:Zahnarzt Kontrolle\r\n"
            b"DTSTART:20261120T093000Z\r\n"
            b"DTEND:20261120T101500Z\r\n"
            b"LOCATION:Praxis Mitte\r\n"
            b"END:VEVENT\r\n"
            b"END:VCALENDAR\r\n"
        )
        evs = am.parse_ics_events(ics_bytes)
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0]["summary"], "Zahnarzt Kontrolle")
        self.assertEqual(evs[0]["dtstart"], "20261120T093000Z")
        self.assertEqual(evs[0]["location"], "Praxis Mitte")

        crit_ascii = am._build_server_uid_search_criteria("Steuerbescheid", since="2026-11-01")
        self.assertIn("SINCE 01-Nov-2026", crit_ascii)
        self.assertIn('SUBJECT "Steuerbescheid"', crit_ascii)
        # Non-ASCII falls back safely to None (client-side Unicode filter)
        self.assertIsNone(am._build_server_uid_search_criteria("Rückfrage Gebühren"))


if __name__ == "__main__":
    unittest.main()
