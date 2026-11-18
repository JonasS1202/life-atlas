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

    def test_readiness_deadline_separation_and_training_window_overlap(self):
        today = dt.date(2026, 11, 13)
        prof_md = (
            "---\nsummary: Runner Profile\ntags: [self, sport]\ndepth: pro\n---\n# Profile\n\n"
            "- **Primary Sport:** Running\n"
            "- **Training Window:** 16:00–19:30\n"
        )
        prof = ce.parse_athlete_profile(prof_md)
        self.assertIn("16:00", prof["schedule"])

        r_rows = [
            {"date": today.isoformat(), "rhr": 48, "sleep_h": 7.8, "sleep_quality": "good", "soreness": 1},
            {"date": (today - dt.timedelta(days=2)).isoformat(), "rhr": 48, "sleep_h": 7.7, "soreness": 1},
            {"date": (today - dt.timedelta(days=4)).isoformat(), "rhr": 49, "sleep_h": 7.6, "soreness": 2},
        ]
        pmc_ok = {"ctl": 42.0, "atl": 44.0, "tsb": -2.0, "acwr": 1.05}

        # 1. Morning deadline ("10:00 Werkstatt Angebot einholen: Motorölwechsel Touran") + untimed deadline
        #    MUST keep Readiness GREEN with zero reasons and zero schedule conflicts!
        cal_morning = [
            {
                "kind": "deadline", "status": "open",
                "start": today, "end": today, "time": "10:00",
                "what": "Werkstatt Angebot einholen: Motorölwechsel Touran",
            },
            {
                "kind": "deadline", "status": "open",
                "start": today + dt.timedelta(days=1), "end": today + dt.timedelta(days=1), "time": "",
                "what": "Steuerunterlagen sortieren",
            },
        ]
        rdy_morning = ce.compute_readiness_and_guardrails(r_rows, prof, pmc_ok, [], cal_items=cal_morning, today=today)
        self.assertEqual(rdy_morning["status"], "GREEN")
        self.assertEqual(rdy_morning["reasons"], [])
        self.assertEqual(rdy_morning["schedule_conflicts"], [])
        self.assertFalse(any("Touran" in g for g in rdy_morning["guardrails"]))

        # 2. Timed event inside 16:00–19:30 training window today adds `schedule_conflicts` WITHOUT turning AMBER!
        cal_overlap = cal_morning + [
            {
                "kind": "event", "status": "open",
                "start": today, "end": today, "time": "17:00-18:00",
                "what": "Elternabend Schule",
            },
        ]
        rdy_overlap = ce.evaluate_readiness_and_guardrails(r_rows, prof, pmc_ok, [], cal_items=cal_overlap, today=today)
        self.assertEqual(rdy_overlap["status"], "GREEN")
        self.assertEqual(rdy_overlap["reasons"], [])
        self.assertEqual(len(rdy_overlap["schedule_conflicts"]), 1)
        self.assertIn("🕒 Training-window overlap today: 17:00-18:00 Elternabend Schule", rdy_overlap["schedule_conflicts"][0])
        self.assertIn(rdy_overlap["schedule_conflicts"][0], rdy_overlap["guardrails"])

        # 3. True `[blocked]` days still drive RED (today) and AMBER (within 3d)
        rdy_blk_soon = ce.evaluate_readiness_and_guardrails(
            r_rows, prof, pmc_ok, [],
            cal_items=[{"kind": "blocked", "status": "open", "start": today + dt.timedelta(days=2), "end": today + dt.timedelta(days=2), "time": "", "what": "Dienstreise"}],
            today=today,
        )
        self.assertEqual(rdy_blk_soon["status"], "AMBER")
        rdy_blk_today = ce.evaluate_readiness_and_guardrails(
            r_rows, prof, pmc_ok, [],
            cal_items=[{"kind": "blocked", "status": "open", "start": today, "end": today, "time": "", "what": "Krank"}],
            today=today,
        )
        self.assertEqual(rdy_blk_today["status"], "RED")

    def test_sport_and_power_sensor_separation_runner_wrist_watts_and_virtual_ride(self):
        today = dt.date(2026, 11, 13)
        # 1. Helper truth table (backward-compatible for legacy cached cycling without device_watts)
        self.assertFalse(ce.is_valid_cycling_power_activity({"sport": "running", "device_watts": True}))
        self.assertFalse(ce.is_valid_cycling_power_activity({"sport": "strength", "device_watts": True}))
        self.assertFalse(ce.is_valid_cycling_power_activity({"sport": "cycling", "device_watts": False}))
        self.assertTrue(ce.is_valid_cycling_power_activity({"sport": "cycling", "device_watts": True}))
        self.assertTrue(ce.is_valid_cycling_power_activity({"sport": "cycling"}))

        # 2. Stream metrics on 390W Garmin wrist-watt Run + virtual-watt Ride (`device_watts=False`)
        run_metrics = ce.compute_stream_metrics(
            {"watts": [390.0] * 1500, "heartrate": [164.0] * 1500, "velocity_smooth": [4.35] * 1500},
            ftp_watts=300,
            weight_kg=72.0,
            sport="running",
            cp_watts=300.0,
            w_prime_j=20000.0,
            device_watts=True,
        )
        for forbidden_key in ("peaks_fresh", "peaks_fatigued", "peaks_durability", "np_w", "tss", "w_bal_min_pct", "cho_burn_g"):
            self.assertNotIn(forbidden_key, run_metrics)
        self.assertIn("best_paces", run_metrics)
        self.assertIn("5km", run_metrics["best_paces"])

        virt_ride_metrics = ce.compute_stream_metrics(
            {"watts": [255.0] * 2400, "heartrate": [148.0] * 2400},
            ftp_watts=300,
            weight_kg=72.0,
            sport="cycling",
            cp_watts=300.0,
            w_prime_j=20000.0,
            device_watts=False,
        )
        for forbidden_key in ("peaks_fresh", "peaks_fatigued", "peaks_durability", "np_w", "tss", "w_bal_min_pct", "cho_burn_g"):
            self.assertNotIn(forbidden_key, virt_ride_metrics)

        # 3. Cached `activities.json` with pre-existing wrist-watt pollution on runs + virtual ride + fallback_ftp=300:
        #    MUST produce ZERO cycling CP/W'/FatMax/Storer pollution while computing CS/D' & Daniels VDOT VO2max!
        polluted_cached_acts = [
            {
                "id": 101,
                "date": (today - dt.timedelta(days=4)).isoformat(),
                "sport": "running",
                "duration_min": 42,
                "distance_km": 10.5,
                "avg_hr": 166,
                "np_w": 392,
                "tss": 114.0,
                "peaks_fresh": {"5s": 602, "15s": 540, "1m": 455, "3m": 420, "5m": 402, "10m": 388, "20m": 380},
                "best_paces": {
                    "5km": {"sec_per_km": 230.0, "pace": "3:50/km"},
                    "10km": {"sec_per_km": 238.0, "pace": "3:58/km"},
                },
            },
            {
                "id": 102,
                "date": (today - dt.timedelta(days=2)).isoformat(),
                "sport": "cycling",
                "device_watts": False,
                "duration_min": 60,
                "distance_km": 28.0,
                "avg_hr": 138,
                "np_w": 240,
                "peaks_fresh": {"5s": 480, "3m": 320, "5m": 295, "10m": 275, "20m": 260},
            },
        ]
        runner_prof_md = (
            "---\nsummary: Pure Runner\ntags: [self, sport]\ndepth: pro\n---\n# Profile\n\n"
            "- **Primary Sport:** Running\n"
            "- **Weight:** 72 kg\n"
            "- **Training Window:** 16:00-19:30\n"
        )
        r_md = ce.render_readiness_markdown(
            "",
            [{"date": today.isoformat(), "rhr": 47, "sleep_h": 8.0, "soreness": 1, "status": "GREEN"}],
            today.isoformat(),
        )
        tel = ce.build_coach_telemetry(
            runner_prof_md,
            polluted_cached_acts,
            readiness_text=r_md,
            cal_items=[{
                "kind": "deadline", "status": "open",
                "start": today, "end": today, "time": "10:00",
                "what": "Werkstatt Angebot einholen: Motorölwechsel Touran",
            }],
            fallback_ftp=300,
            today=today,
        )
        self.assertFalse(tel["cp_model"]["valid"])
        self.assertEqual(tel["curves"]["fresh"]["5m"], {})
        self.assertEqual(tel["curves"]["fresh"]["20m"], {})
        self.assertIsNone(tel["metabolic"]["vo2max_storer"])
        self.assertIsNone(tel["metabolic"]["fatmax_watts"])
        self.assertTrue(tel["cs_model"]["valid"], tel["cs_model"])
        self.assertIsNotNone(tel["metabolic"]["vo2max_vdot"])
        self.assertGreater(tel["metabolic"]["vo2max_vdot"], 50.0)
        self.assertEqual(tel["metabolic"]["vo2max_est"], tel["metabolic"]["vo2max_vdot"])
        self.assertEqual(tel["readiness"]["status"], "GREEN")

        pro_blk = ce.format_pro_telemetry_block(tel)
        self.assertIn("Readiness: GREEN", pro_blk)
        self.assertIn("CS ", pro_blk)
        self.assertIn("Daniels VDOT", pro_blk)
        self.assertIn("VO2max (est.)", pro_blk)
        self.assertNotIn("FatMax", pro_blk)
        self.assertNotIn("Storer", pro_blk)
        self.assertNotIn("Touran", pro_blk)

    def test_rider_dna_enriched_peaks_quarterly_and_mode_detection(self):
        # 1. Cycling enrich_peak: 10m lead-in @ 200W (=120 kJ), then 5m negative split (310W -> 330W)
        watts = [200.0] * 600 + [310.0] * 150 + [330.0] * 150
        hr = [140.0] * 600 + [168.0] * 150 + [178.0] * 150
        c_metrics = ce.compute_stream_metrics(
            {"watts": watts, "heartrate": hr},
            ftp_watts=280,
            weight_kg=70.0,
            sport="cycling",
            device_watts=True,
        )
        ep = c_metrics.get("enriched_peaks") or {}
        self.assertIn("5m", ep)
        self.assertIn("3m", ep)
        self.assertNotIn("20m", ep)
        p5 = ep["5m"]
        self.assertEqual(p5["avg_w"], 320)
        self.assertEqual(p5["start_s"], 600)
        self.assertEqual(p5["kj_before"], 120)
        self.assertEqual(p5["lead_in_w"], 200)
        self.assertGreater(p5["drift_w"], 0)
        self.assertEqual(p5["avg_hr"], 173)
        self.assertEqual(p5["max_hr"], 178)

        # 2. Running enrich_peak: 10m @ 3.0 m/s (1.8 km), then 1km fast split (4.0 m/s -> 4.5 m/s)
        vel = [3.0] * 600 + [4.0] * 125 + [4.5] * 112
        r_hr = [142.0] * 600 + [170.0] * 125 + [182.0] * 112
        r_metrics = ce.compute_stream_metrics(
            {"velocity_smooth": vel, "heartrate": r_hr},
            sport="running",
        )
        erp = r_metrics.get("enriched_run_peaks") or {}
        self.assertIn("1km", erp)
        r1k = erp["1km"]
        self.assertGreater(r1k["km_before"], 1.7)
        self.assertIsNotNone(r1k["lead_in_pace"])
        self.assertLess(r1k["split_delta_s"], 0)  # faster 2nd half = negative split
        self.assertGreater(r1k["max_hr"], 175)

        # 3. Quarterly curves + DNA context + DNA snapshot parsing
        today = dt.date(2026, 11, 14)
        acts = [
            {
                "id": "1",
                "date": "2025-05-10",
                "sport": "cycling",
                "device_watts": True,
                "name": "Spring Climb",
                "duration_min": 120,
                "peaks_fresh": {"5s": 900, "5m": 340, "20m": 290},
                "enriched_peaks": {"5m": p5},
            },
            {
                "id": "2",
                "date": "2026-11-01",
                "sport": "cycling",
                "device_watts": True,
                "name": "Autumn TT",
                "duration_min": 90,
                "np_w": 270,
                "work_kj": 1400,
                "peaks_fresh": {"5s": 950, "5m": 360, "20m": 310},
                "enriched_peaks": {"5m": p5},
            },
        ]
        dna_md = (
            "---\n"
            "updated: 2026-11-10\n"
            "archetype: Endurance Diesel\n"
            "primary_limiter: VO2max Ceiling\n"
            "confidence: medium\n"
            "---\n"
            "```json\n"
            '{"archetype": "Endurance Diesel", "primary_limiter": "VO2max Ceiling", '
            '"scores": {"peak_shape": 84, "freshness": 72, "durability": 88, "trajectory": 90}}\n'
            "```\n"
        )
        tel = ce.build_coach_telemetry(
            profile_text="depth: pro\nweight_kg: 70\n",
            activities=acts,
            today=today,
            dna_text=dna_md,
        )
        q = tel["curves"]["quarterly"]
        self.assertIn("2025-Q2", q)
        self.assertIn("2026-Q4", q)
        self.assertEqual(q["2025-Q2"]["cycling"]["20m"], 290)
        self.assertEqual(tel["dna_snapshot"]["archetype"], "Endurance Diesel")
        self.assertIn("DNA Snapshot (2026-11-10): Archetype=Endurance Diesel", ce.format_pro_telemetry_block(tel))

        dna_ctx = ce.build_dna_context_block(acts, tel, dna_snapshot_text=dna_md, today=today)
        self.assertIn("Quarterly Trajectory:", dna_ctx)
        self.assertIn("All-Time Defining Cycling Efforts (Enriched):", dna_ctx)
        self.assertIn("began 600s / 120kJ in", dna_ctx)

        # 4. Hook mode detection for Rider DNA
        hooks_dir = os.path.join(REPO, "core", "hooks")
        if hooks_dir not in sys.path:
            sys.path.insert(0, hooks_dir)
        import atlas_hook
        mode, why, _ = atlas_hook.detect_coach_mode("Analysiere meine Rider DNA und was ist mein Limiter?")
        self.assertEqual(mode, "dna")
        self.assertIn("DNA", why)

    def test_seven_pillars_diesel_vs_puncheur_ttl_gelbb_and_missing_gate(self):
        today = dt.date(2026, 11, 18)

        # 1. Spike filter clamps 1s telemetry glitch and locks >50-spike sessions from record curves
        raw_w = [250.0] * 20 + [3400.0] + [250.0] * 20
        cleaned_w, meta_ok = ce.filter_power_spikes(raw_w)
        self.assertEqual(meta_ok["spike_count"], 1)
        self.assertFalse(meta_ok["locked_for_records"])
        self.assertEqual(cleaned_w[20], 250.0)
        corrupt_w = [200.0, 2900.0] * 55
        _, meta_bad = ce.filter_power_spikes(corrupt_w)
        self.assertTrue(meta_bad["locked_for_records"])

        # 2. Identical 20m power (~316W -> naive FTP_20 = 300W) at 75 kg, 11.0% KFA:
        #    Athlete A (Diesel) vs Athlete B (Puncheur) must separate cleanly across Pillars 1-4!
        prof_full = (
            "---\nsummary: Profile\ntags: [self, sport]\ndepth: pro\nsex: male\nage: 28\ncreatine: no\n---\n# Profile\n\n"
            "| Metric | Value |\n|---|---|\n"
            "| **Biometrie** | 180 cm · 75.0 kg · 11.0 % KFA |\n"
            "| **Max HR / Rest HR** | 195 bpm / 47 bpm |\n"
            "| **Radgewicht** | 8.1 kg |\n"
        )
        diesel_acts = [{
            "id": "A1", "date": (today - dt.timedelta(days=5)).isoformat(), "sport": "cycling", "device_watts": True,
            "duration_min": 90, "np_w": 280, "work_kj": 1500, "kj_above_cp": 25.0,
            "peaks_fresh": {"5s": 760, "15s": 720, "3m": 369, "5m": 344, "12m": 322, "20m": 316, "60m": 302},
            "c_opt_sprint_rpm": 111.0, "t_max_nm": 156.0,
        }]
        punch_acts = [{
            "id": "B1", "date": (today - dt.timedelta(days=5)).isoformat(), "sport": "cycling", "device_watts": True,
            "duration_min": 90, "np_w": 280, "work_kj": 1500, "kj_above_cp": 120.0,
            "peaks_fresh": {"5s": 1120, "15s": 960, "3m": 430, "5m": 374, "12m": 325, "20m": 311, "60m": 276},
            "c_opt_sprint_rpm": 131.0, "t_max_nm": 192.0,
        }]
        tel_diesel = ce.build_coach_telemetry(prof_full, diesel_acts, today=today)
        tel_punch = ce.build_coach_telemetry(prof_full, punch_acts, today=today)

        sp_d = tel_diesel["seven_pillars"]
        sp_p = tel_punch["seven_pillars"]
        self.assertTrue(sp_d["body"]["kfa_calibrated"])
        self.assertAlmostEqual(sp_d["body"]["ffm_kg"], 66.75, places=1)
        self.assertEqual(tel_diesel["coach_audit"]["missing_critical"], [])

        # True CP separates: Diesel CP > 304W, Puncheur CP <= 293W despite identical 316W 20m!
        self.assertGreater(tel_diesel["cp_model"]["cp_w"], 304)
        self.assertLessEqual(tel_punch["cp_model"]["cp_w"], 293)
        # vLamax & Z_Myo separate Slow vs Fast Myotype
        self.assertLess(sp_d["pillar2_metabolic"]["vlamax_sprint_proxy"], 0.45)
        self.assertGreater(sp_p["pillar2_metabolic"]["vlamax_sprint_proxy"], 0.65)
        self.assertEqual(sp_d["pillar4_myotype"]["myotype_class"], "Slow Myotype")
        self.assertEqual(sp_p["pillar4_myotype"]["myotype_class"], "Fast Myotype")
        self.assertIn("30/15", sp_p["pillar4_myotype"]["recommended_vo2_protocol"])
        self.assertGreater(sp_p["pillar3_kinetics"]["omni_decay_a_w"], sp_d["pillar3_kinetics"]["omni_decay_a_w"])

        # 3. Readiness TTL (3-day-old RED sleep expires!) + GELB-B (Peripheral leg pain overrides Green HRV!)
        stale_rdy = ce.render_readiness_markdown(
            "",
            [{"date": (today - dt.timedelta(days=3)).isoformat(), "rhr": 52, "sleep_h": 4.0, "sleep_quality": "poor", "soreness": 4, "status": "RED"}],
            today.isoformat(),
        )
        tel_ttl = ce.build_coach_telemetry(prof_full, punch_acts, readiness_text=stale_rdy, today=today)
        self.assertEqual(tel_ttl["readiness"]["status"], "GREEN")
        self.assertFalse(tel_ttl["readiness"]["today_logged"])

        gelbb_rdy = ce.render_readiness_markdown(
            "",
            [
                {"date": today.isoformat(), "rhr": 47, "hrv_rmssd": 74.0, "sleep_h": 7.8, "soreness": 2, "leg_pain": 4, "notes": "Beine wie Beton"},
                {"date": (today - dt.timedelta(days=1)).isoformat(), "rhr": 47, "hrv_rmssd": 72.0, "sleep_h": 7.6, "soreness": 1},
                {"date": (today - dt.timedelta(days=2)).isoformat(), "rhr": 48, "hrv_rmssd": 73.0, "sleep_h": 7.7, "soreness": 1},
            ],
            today.isoformat(),
        )
        tel_gelbb = ce.build_coach_telemetry(prof_full, punch_acts, readiness_text=gelbb_rdy, today=today)
        self.assertEqual(tel_gelbb["readiness"]["status"], "GREEN")  # Autonomic green
        self.assertEqual(tel_gelbb["readiness"]["flag_4stage"], "GELB-B")  # Peripheral override!

        # 4. Missing-Data Gate flags unprovided KFA, RHR, creatine without silent precision
        prof_incomplete = "---\ndepth: pro\n---\n# Profile\n| Metric | Value |\n|---|---|\n| **Biometrie** | 75 kg |\n"
        tel_miss = ce.build_coach_telemetry(prof_incomplete, punch_acts, today=today)
        miss_fields = {m["field"] for m in tel_miss["coach_audit"]["missing_critical"]}
        self.assertIn("kfa_pct", miss_fields)
        self.assertIn("rhr", miss_fields)
        self.assertIn("creatine_flag", miss_fields)

        # 5. Minetti cost & Morin leg spring stiffness
        self.assertAlmostEqual(ce.minetti_running_cost_j_kg_m(0.0), 3.6, places=2)
        k_leg = ce.compute_morin_leg_stiffness(mass_kg=75.0, height_cm=186.0, vel_mps=4.2, contact_time_s=0.22, flight_time_s=0.11)
        self.assertIsNotNone(k_leg)
        self.assertGreater(k_leg, 8.0)

    def test_hammerhead_one_click_oauth_state_and_broker(self):
        """Hammerhead Karoo works with 1-Click out-of-the-box (no manual Client ID/Secret required)."""
        import json
        import subprocess
        import tempfile
        from unittest.mock import patch, MagicMock
        from atlas_lib import coach_connectors as cc

        with tempfile.TemporaryDirectory() as r:
            env_sub = dict(os.environ, ATLAS_NO_CAL_SERVER="1")
            atlas_py = os.path.join(TOOLS_DIR, "atlas.py")
            subprocess.run(
                [sys.executable, "-I", atlas_py, "--root", r, "init", "--source", REPO, "--name", "Test", "--modules", "coach", "--execute"],
                check=True, capture_output=True, text=True, env=env_sub,
            )
            brain = os.path.join(r, "brain")

            # 1. Default 1-Click config works with zero secrets configured
            cfg = cc.hammerhead_oauth_config(brain, local_redirect_uri="http://127.0.0.1:8765/hammerhead-callback")
            self.assertFalse(cfg["custom"])
            self.assertEqual(cfg["client_id"], cc.DEFAULT_HAMMERHEAD_CLIENT_ID)
            self.assertEqual(cfg["redirect_uri"], cc.DEFAULT_HAMMERHEAD_REDIRECT_URI)

            # 2. CSRF state mint + single-use verification
            st = cc.mint_hammerhead_oauth_state(brain, port=8765)
            self.assertTrue(st.startswith("atlas:8765:"))
            self.assertFalse(cc.verify_hammerhead_oauth_state(brain, "atlas:8765:forged-state"))
            self.assertTrue(cc.verify_hammerhead_oauth_state(brain, st))
            self.assertFalse(cc.verify_hammerhead_oauth_state(brain, st))  # Consumed once!

            # 3. `atlas.py coach auth-hammerhead` outputs 1-Click link without asking for API keys
            out = subprocess.run(
                [sys.executable, "-I", atlas_py, "--root", r, "coach", "auth-hammerhead"],
                check=True, capture_output=True, text=True, env=env_sub,
            ).stdout
            self.assertIn("1-Click Connect (Ready)", out)
            self.assertIn("/hammerhead-connect", out)
            self.assertIn("no API keys", out)

            # 4. Stateless broker code exchange & token refresh save local tokens (`chmod 600`)
            fake_resp = MagicMock()
            fake_resp.read.return_value = json.dumps({
                "access_token": "hh-1click-acc",
                "refresh_token": "hh-1click-ref",
                "expires_at": int(dt.datetime.now(dt.timezone.utc).timestamp()) - 10,  # Force refresh test next
            }).encode("utf-8")
            fake_resp.__enter__.return_value = fake_resp
            with patch("urllib.request.urlopen", return_value=fake_resp) as mock_url:
                ok, msg = cc.hammerhead_exchange_code(brain, code="oauth-code-xyz")
                self.assertTrue(ok, msg)
                req_sent = mock_url.call_args[0][0]
                self.assertEqual(req_sent.full_url, cc.DEFAULT_HAMMERHEAD_BROKER_URL)

            fake_ref = MagicMock()
            fake_ref.read.return_value = json.dumps({
                "access_token": "hh-1click-acc-fresh",
                "refresh_token": "hh-1click-ref-fresh",
                "expires_at": int(dt.datetime.now(dt.timezone.utc).timestamp()) + 3600,
            }).encode("utf-8")
            fake_ref.__enter__.return_value = fake_ref
            with patch("urllib.request.urlopen", return_value=fake_ref):
                tok = cc.hammerhead_refresh_if_needed(brain)
                self.assertEqual(tok, "hh-1click-acc-fresh")
            self.assertTrue(cc.connector_status_summary(brain)["hammerhead_connected"])


if __name__ == "__main__":
    unittest.main()
