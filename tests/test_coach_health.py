"""Unit tests for v1.5.0 Google Health API v4, 2-layer biometrics/intraday storage,
Strava temporal alignment, multi-marker illness guard, KFA guard, and multi-year history.

100 % synthetic/anonymized fixtures; Python standard library only.
"""
import datetime as dt
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.dont_write_bytecode = True

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS_DIR = os.path.join(REPO_ROOT, "core", "tools")
if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)

from atlas_lib import coach_connectors, coach_engine, health_engine, pillars_engine  # noqa: E402


def _synthetic_gh_bundle(ref_day: dt.date) -> dict:
    """Build an anonymized Google Health API v4 bundle matching live-verified schemas."""
    d0 = ref_day.isoformat()
    d1 = (ref_day - dt.timedelta(days=1)).isoformat()
    d2 = (ref_day - dt.timedelta(days=2)).isoformat()
    d0_obj = {"year": ref_day.year, "month": ref_day.month, "day": ref_day.day}
    d1_dt = ref_day - dt.timedelta(days=1)
    d1_obj = {"year": d1_dt.year, "month": d1_dt.month, "day": d1_dt.day}

    # Sleep on d0: 00:00Z..07:30Z (+3600s -> 01:00..08:30 local), 430m asleep / 450m period
    # Sleep on d1: 23:00Z(d2)..06:30Z(d1) (+3600s -> 00:00..07:30 local)
    hr_5m = []
    # Night d0 (00:00Z..07:30Z): early low nadir around 02:00Z (03:00 local, ~27% into night)
    for m_off in range(0, 450, 5):
        t0 = dt.datetime.combine(ref_day, dt.time(0, 0), tzinfo=dt.timezone.utc) + dt.timedelta(minutes=m_off)
        t1 = t0 + dt.timedelta(minutes=5)
        bpm = 46.0 if 100 <= m_off <= 140 else (53.0 if m_off < 100 else 51.0)
        hr_5m.append({
            "startTime": t0.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "endTime": t1.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "heartRate": {"beatsPerMinuteAvg": bpm, "beatsPerMinuteMin": int(bpm - 2), "beatsPerMinuteMax": int(bpm + 3)},
        })
    # Post-workout HR on d1 evening after 18:00Z (19:00 local) finish: drops from 92 -> 62 by +60m
    for m_off in range(0, 125, 5):
        t0 = dt.datetime.combine(d1_dt, dt.time(18, 0), tzinfo=dt.timezone.utc) + dt.timedelta(minutes=m_off)
        t1 = t0 + dt.timedelta(minutes=5)
        bpm = 90.0 if m_off < 25 else (74.0 if m_off < 55 else 59.0)
        hr_5m.append({
            "startTime": t0.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "endTime": t1.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "heartRate": {"beatsPerMinuteAvg": bpm, "beatsPerMinuteMin": int(bpm - 3), "beatsPerMinuteMax": int(bpm + 4)},
        })

    # Hourly steps on d1: 10500 steps before 17:00 local (16:00Z), 1500 after
    steps_hourly = []
    for hr_utc in range(7, 16):  # local 08..16 -> 9 hours * 1200 = 10800 steps
        t0 = dt.datetime.combine(d1_dt, dt.time(hr_utc, 0), tzinfo=dt.timezone.utc)
        t1 = t0 + dt.timedelta(hours=1)
        steps_hourly.append({
            "startTime": t0.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "endTime": t1.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "steps": {"countSum": "1200"},
        })
    for hr_utc in (18, 19):  # local 19, 20 -> 1500 steps post-ride
        t0 = dt.datetime.combine(d1_dt, dt.time(hr_utc, 0), tzinfo=dt.timezone.utc)
        t1 = t0 + dt.timedelta(hours=1)
        steps_hourly.append({
            "startTime": t0.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "endTime": t1.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "steps": {"countSum": "750"},
        })

    return {
        "default_utc_offset_s": 3600,
        "sleep": [
            {
                "sleep": {
                    "interval": {
                        "startTime": f"{d0}T00:00:00Z", "startUtcOffset": "3600s",
                        "endTime": f"{d0}T07:30:00Z", "endUtcOffset": "3600s",
                    },
                    "metadata": {"mainSleep": True, "processed": True},
                    "summary": {
                        "minutesInSleepPeriod": "450",
                        "minutesAsleep": "430",
                        "minutesAwake": "20",
                        "stagesSummary": [
                            {"type": "AWAKE", "minutes": "20", "count": "2"},
                            {"type": "LIGHT", "minutes": "220", "count": "7"},
                            {"type": "DEEP", "minutes": "95", "count": "3"},
                            {"type": "REM", "minutes": "115", "count": "4"},
                        ],
                    },
                    "stages": [
                        {"startTime": f"{d0}T00:00:00Z", "endTime": f"{d0}T01:35:00Z", "type": "DEEP"},
                        {"startTime": f"{d0}T01:35:00Z", "endTime": f"{d0}T07:30:00Z", "type": "LIGHT"},
                    ],
                    "shortAwakenings": [{"startTime": f"{d0}T03:10:00Z", "endTime": f"{d0}T03:12:00Z", "type": "LIGHT"}],
                }
            },
            {
                "sleep": {
                    "interval": {
                        "startTime": f"{d0}T13:00:00Z", "startUtcOffset": "3600s",
                        "endTime": f"{d0}T13:25:00Z", "endUtcOffset": "3600s",
                    },
                    "metadata": {"mainSleep": False, "processed": True},
                    "summary": {"minutesInSleepPeriod": "25", "minutesAsleep": "22", "minutesAwake": "3"},
                }
            },
            {
                "sleep": {
                    "interval": {
                        "startTime": f"{d2}T23:00:00Z", "startUtcOffset": "3600s",
                        "endTime": f"{d1}T06:30:00Z", "endUtcOffset": "3600s",
                    },
                    "metadata": {"mainSleep": True, "processed": True},
                    "summary": {
                        "minutesInSleepPeriod": "450",
                        "minutesAsleep": "425",
                        "minutesAwake": "25",
                        "stagesSummary": [
                            {"type": "AWAKE", "minutes": "25"},
                            {"type": "LIGHT", "minutes": "225"},
                            {"type": "DEEP", "minutes": "90"},
                            {"type": "REM", "minutes": "110"},
                        ],
                    },
                }
            },
        ],
        "daily_rhr": [
            {"dailyRestingHeartRate": {"date": d0_obj, "beatsPerMinute": "49"}},
            {"dailyRestingHeartRate": {"date": d1_obj, "beatsPerMinute": "48"}},
        ],
        "daily_hrv": [
            {
                "dailyHeartRateVariability": {
                    "date": d0_obj,
                    "averageHeartRateVariabilityMilliseconds": 78.4,
                    "deepSleepRootMeanSquareOfSuccessiveDifferencesMilliseconds": 82.1,
                    "nonRemHeartRateBeatsPerMinute": "47",
                    "entropy": 3.62,
                }
            },
            {
                "dailyHeartRateVariability": {
                    "date": d1_obj,
                    "averageHeartRateVariabilityMilliseconds": 84.0,
                    "deepSleepRootMeanSquareOfSuccessiveDifferencesMilliseconds": 88.0,
                    "nonRemHeartRateBeatsPerMinute": "46",
                    "entropy": 3.71,
                }
            },
        ],
        "daily_resp": [
            {"dailyRespiratoryRate": {"date": d0_obj, "breathsPerMinute": 13.6}},
            {"dailyRespiratoryRate": {"date": d1_obj, "breathsPerMinute": 13.4}},
        ],
        "daily_temp": [
            {
                "dailySleepTemperatureDerivations": {
                    "date": d0_obj,
                    "nightlyTemperatureCelsius": 36.42,
                    "baselineTemperatureCelsius": 36.50,
                    "relativeNightlyStddev30dCelsius": 0.31,
                }
            }
        ],
        "daily_spo2": [
            {
                "dailyOxygenSaturation": {
                    "date": d0_obj,
                    "averagePercentage": 96.4,
                    "lowerBoundPercentage": 94.1,
                    "upperBoundPercentage": 98.8,
                }
            }
        ],
        "weight": [
            {"weight": {"sampleTime": {"civilTime": {"date": d0_obj}}, "weightGrams": 71500}}
        ],
        "body_fat": [
            {"bodyFat": {"sampleTime": {"civilTime": {"date": d0_obj}}, "percentage": 12.6}}
        ],
        "steps_daily": [
            {"civilStartTime": {"date": d0_obj}, "steps": {"countSum": "6400"}},
            {"civilStartTime": {"date": d1_obj}, "steps": {"countSum": "12300"}},
        ],
        "azm_daily": [
            {
                "civilStartTime": {"date": d1_obj},
                "activeZoneMinutes": {"sumInFatBurnHeartZone": "18", "sumInCardioHeartZone": "6", "sumInPeakHeartZone": "2"},
            }
        ],
        "steps_hourly": steps_hourly,
        "hr_5m": hr_5m,
        "hrv_intraday": [
            {
                "heartRateVariability": {
                    "sampleTime": {"physicalTime": f"{d0}T03:00:00Z", "utcOffset": "3600s", "civilTime": {"date": d0_obj}},
                    "rootMeanSquareOfSuccessiveDifferencesMilliseconds": 72.0,
                    "metadata": {"highFrequencyPower": 8000.0, "lowFrequencyPower": 8800.0},
                }
            },
            {
                "heartRateVariability": {
                    "sampleTime": {"physicalTime": f"{d0}T04:00:00Z", "utcOffset": "3600s", "civilTime": {"date": d0_obj}},
                    "rootMeanSquareOfSuccessiveDifferencesMilliseconds": 95.0,
                    "metadata": {"highFrequencyPower": 9000.0, "lowFrequencyPower": 9000.0},
                }
            },
        ],
    }


class CoachHealthUnit(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="atlas-health-test-")
        self.brain = os.path.join(self.tmp, "brain")
        os.makedirs(os.path.join(self.brain, "coach"), exist_ok=True)
        os.makedirs(os.path.join(self.brain, "_atlas", "cache"), exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_google_health_v4_schema_parser_and_no_exercises(self):
        today = dt.date(2026, 11, 20)
        bundle = _synthetic_gh_bundle(today)
        days, intra = coach_connectors.parse_google_health_payloads(bundle, intraday_cutoff_iso="2026-10-21", default_utc_offset_s=3600)
        by_d = {d["date"]: d for d in days}
        self.assertIn("2026-11-20", by_d)
        self.assertIn("2026-11-19", by_d)

        d0 = by_d["2026-11-20"]
        self.assertAlmostEqual(d0["sleep"]["duration_h"], 7.17, places=2)
        self.assertEqual(d0["sleep"]["deep_min"], 95)
        self.assertEqual(d0["sleep"]["rem_min"], 115)
        self.assertEqual(d0["sleep"]["start_local"], "2026-11-20T01:00:00")
        self.assertEqual(d0["sleep"]["end_local"], "2026-11-20T08:30:00")
        self.assertEqual(len(d0["sleep"]["naps"]), 1)
        self.assertEqual(d0["autonomic"]["rhr_bpm"], 49)
        self.assertAlmostEqual(d0["autonomic"]["hrv_rmssd_ms"], 78.4, places=1)
        self.assertAlmostEqual(d0["autonomic"]["hrv_deep_rmssd_ms"], 82.1, places=1)
        self.assertEqual(d0["autonomic"]["hrv_non_rem_hr_bpm"], 47)
        self.assertAlmostEqual(d0["autonomic"]["skin_temp_delta_c"], -0.08, places=2)
        self.assertAlmostEqual(d0["autonomic"]["resp_rate_brpm"], 13.6, places=1)
        self.assertAlmostEqual(d0["autonomic"]["spo2_avg_pct"], 96.4, places=1)
        self.assertAlmostEqual(d0["body"]["weight_kg"], 71.5, places=2)
        self.assertAlmostEqual(d0["body"]["body_fat_pct"], 12.6, places=1)
        self.assertEqual(d0["daytime_load"]["steps_total"], 6400)

        d1 = by_d["2026-11-19"]
        self.assertEqual(d1["daytime_load"]["azm_total_min"], 26)
        self.assertEqual(len(d1["daytime_load"]["hourly_steps"]), 24)
        self.assertEqual(sum(d1["daytime_load"]["hourly_steps"][:17]), 10800)

        # Intraday features extracted: HR nadir + HRV min/max + LF/HF
        health_engine.extract_intraday_day_features(d0, intra["2026-11-20"])
        self.assertEqual(d0["sleep"]["hr_nadir_local"], "02:45")
        self.assertLess(d0["sleep"]["hr_nadir_pct"], 40.0)
        self.assertEqual(d0["autonomic"]["hrv_min_ms"], 72.0)
        self.assertEqual(d0["autonomic"]["hrv_max_ms"], 95.0)
        self.assertAlmostEqual(d0["autonomic"]["hrv_lf_hf_ratio"], 1.05, places=2)

    def test_oauth_pkce_state_client_json_and_7d_testing_token_status(self):
        client_json = os.path.join(self.tmp, "client_secret_test.json")
        with open(client_json, "w", encoding="utf-8") as fh:
            json.dump({"installed": {"client_id": "12345.apps.googleusercontent.com", "client_secret": "demo-client-secret"}}, fh)
        ok, saved = coach_connectors.import_google_health_client_json(self.brain, client_json, login_hint="athlete@example.com")
        self.assertTrue(ok)
        self.assertTrue(os.path.isfile(saved))

        state, verifier, challenge = coach_connectors.mint_google_health_oauth_state(self.brain, port=8765)
        self.assertTrue(state.startswith("atlas:8765:"))
        auth_url = coach_connectors.build_google_health_authorize_url(
            "12345.apps.googleusercontent.com", "http://127.0.0.1:8765/google-health-callback", state, challenge, login_hint="athlete@example.com"
        )
        self.assertIn("code_challenge_method=S256", auth_url)
        self.assertIn("login_hint=athlete%40example.com", auth_url)
        self.assertIn("googlehealth.sleep.readonly", auth_url)

        ok_s, got_v = coach_connectors.consume_google_health_oauth_state(self.brain, state)
        self.assertTrue(ok_s)
        self.assertEqual(got_v, verifier)
        # Single-use replay rejected
        ok_replay, _ = coach_connectors.consume_google_health_oauth_state(self.brain, state)
        self.assertFalse(ok_replay)

        # Testing mode 7-day token age warning
        env = coach_connectors.load_secret_env(self.brain, "google_health")
        env["GOOGLE_HEALTH_REFRESH_TOKEN"] = "rtok-demo"
        env["GOOGLE_HEALTH_REFRESH_ISSUED_AT"] = str(int(time.time() - 6.5 * 86400))
        coach_connectors.save_secret_env(self.brain, "google_health", env)
        st_warn = coach_connectors.google_health_token_status(self.brain)
        self.assertTrue(st_warn["needs_renew"])
        self.assertTrue(st_warn["connected"])

        env["GOOGLE_HEALTH_AUTH_ERROR"] = "refresh_expired"
        coach_connectors.save_secret_env(self.brain, "google_health", env)
        st_exp = coach_connectors.google_health_token_status(self.brain)
        self.assertTrue(st_exp["needs_renew"])
        self.assertFalse(st_exp["connected"])

    def test_two_layer_reconciliation_pruning_and_temporal_alignment(self):
        today = dt.date(2026, 11, 20)
        store = health_engine.load_biometrics_store(self.brain)

        # Manual subjective entry + intervals lower-priority RHR
        health_engine.merge_biometrics_days(store, [
            {"date": "2026-11-19", "rhr": 52, "soreness": 2, "leg_pain": 2, "notes": "Legs crisp"},
        ], source="manual")
        health_engine.merge_biometrics_days(store, [
            {"date": "2026-11-19", "rhr": 50, "soreness": 5, "notes": "Sensor overwrite attempt"},
        ], source="intervals")
        # Sensor updates RHR (rank 20 > manual 10), but NEVER overwrites manual subjective soreness/notes!
        self.assertEqual(store["days"]["2026-11-19"]["autonomic"]["rhr_bpm"], 50)
        self.assertEqual(store["days"]["2026-11-19"]["subjective"]["soreness"], 2)
        self.assertEqual(store["days"]["2026-11-19"]["subjective"]["notes"], "Legs crisp")

        # Pull Google Health mock (rank 40 > intervals 20) + save intraday + prune old cache
        mock_p = os.path.join(self.tmp, "gh_mock.json")
        with open(mock_p, "w", encoding="utf-8") as fh:
            json.dump(_synthetic_gh_bundle(today), fh)
        # Plant a 45-day-old intraday cache file to verify 30d rolling prune
        old_d = (today - dt.timedelta(days=45)).isoformat()
        health_engine.save_intraday_day(self.brain, old_d, {"source": "google_health", "hr_5m": [{"start_utc": f"{old_d}T01:00:00Z", "avg": 50}]})
        res = coach_connectors.pull_all_wellness_into_biometrics(
            self.brain, store, days=30, intraday_days=30, google_health_mock=mock_p, execute=True, today=today
        )
        self.assertEqual(res["providers"], ["google_health"])
        self.assertEqual(res["intraday_pruned"], 1)
        self.assertIsNone(health_engine.load_intraday_day(self.brain, old_d))
        self.assertEqual(store["days"]["2026-11-19"]["autonomic"]["rhr_bpm"], 48)
        self.assertEqual(store["days"]["2026-11-19"]["subjective"]["notes"], "Legs crisp")

        # Temporal alignment with Strava evening workout on 2026-11-19 (17:30-19:00 local -> 16:30Z-18:00Z)
        acts = [{
            "id": "9001",
            "date": "2026-11-19",
            "name": "Evening Threshold",
            "sport": "cycling",
            "device_watts": True,
            "duration_min": 90,
            "elapsed_sec": 5400,
            "start_time_utc": "2026-11-19T16:30:00Z",
            "start_time_local": "2026-11-19T17:30:00",
            "end_time_utc": "2026-11-19T18:00:00Z",
            "utc_offset_s": 3600,
            "np_w": 285,
            "if": 0.92,
            "work_kj": 1400,
            "kj_above_cp": 22.0,
        }]
        links = health_engine.align_activities_with_biometrics(acts, store, brain=self.brain)
        wl = links["9001"]
        self.assertEqual(wl["pre_workout_steps"], 10800)
        self.assertAlmostEqual(wl["hours_awake_before"], 10.0, places=1)
        self.assertAlmostEqual(wl["finish_to_bedtime_h"], 6.0, places=1)
        self.assertAlmostEqual(wl["post_hr_avg_0_30m"], 85.4, places=1)
        self.assertEqual(wl["post_hr_return_min"], 55)
        self.assertAlmostEqual(wl["next_night_hrv_ms"], 78.4, places=1)

        # Even after pruning/removing the 30d intraday file, permanent post_hr extract persists on re-align!
        os.remove(os.path.join(health_engine.intraday_cache_dir(self.brain), "2026-11-19.json"))
        links_after = health_engine.align_activities_with_biometrics(acts, store, brain=self.brain)
        self.assertEqual(links_after["9001"]["post_hr_return_min"], 55)

        tl = health_engine.format_day_timeline_inspect("2026-11-19", acts, store)
        self.assertIn("pre-NEAT 10800 steps", tl)
        self.assertIn("finish->bed 6.0h", tl)
        self.assertIn("HR return 55m", tl)
        self.assertIn("Following Night Recovery (2026-11-20)", tl)

    def test_illness_early_warning_multi_marker_and_kfa_guard(self):
        today = dt.date(2026, 11, 20)
        prof = {"rhr": 48, "hr_max": 195, "kfa_pct": 11.5, "weight_kg": 72.0}
        # 14 clean baseline days
        base_rows = [
            {
                "date": (today - dt.timedelta(days=k)).isoformat(),
                "rhr": 48, "hrv_rmssd": 80.0, "sleep_h": 7.8, "resp_rate_brpm": 13.5,
                "skin_temp_delta_c": 0.0, "spo2_avg_pct": 96.5, "body_fat_pct": 14.0,
            }
            for k in range(1, 15)
        ]

        # Single anomaly (only skin temp +0.55°C) must NOT trigger ILLNESS_ALERT ROT
        single_rows = [{"date": today.isoformat(), "rhr": 49, "hrv_rmssd": 78.0, "sleep_h": 7.6, "resp_rate_brpm": 13.7, "skin_temp_delta_c": 0.55, "spo2_avg_pct": 96.2, "body_fat_pct": 14.1}] + base_rows
        sig_single = health_engine.evaluate_biometric_signals(single_rows, profile=prof, today=today)
        self.assertFalse(sig_single["illness_alert"])
        self.assertEqual(len(sig_single["illness_markers"]), 1)
        # KFA drift advisory fires (14.0% vs 11.5% profile) without silently mutating profile
        self.assertIsNotNone(sig_single["kfa_drift_advisory"])
        self.assertEqual(prof["kfa_pct"], 11.5)

        # >= 2 objective anomalies (skin temp +0.62°C + resp +2.1/min + RHR +7) -> ILLNESS_ALERT ROT!
        multi_rows = [{"date": today.isoformat(), "rhr": 55, "hrv_rmssd": 54.0, "sleep_h": 7.5, "resp_rate_brpm": 15.6, "skin_temp_delta_c": 0.62, "spo2_avg_pct": 96.0}] + base_rows
        sig_multi = health_engine.evaluate_biometric_signals(multi_rows, profile=prof, today=today)
        self.assertTrue(sig_multi["illness_alert"])
        self.assertGreaterEqual(len(sig_multi["illness_markers"]), 3)
        p7 = pillars_engine.compute_pillar7_and_stage2_readiness(
            multi_rows, [], prof, {"status": "GREEN", "baseline_rhr": 48}, 290.0, today=today, biometric_signals=sig_multi
        )
        self.assertEqual(p7["readiness_flag_4stage"], "ROT")
        self.assertIn("Illness / Systemic Stress Early Warning", p7["coaching_directive"])

    def test_multi_year_all_time_curves_search_and_historical_pmc_map(self):
        today = dt.date(2026, 11, 20)
        acts = [
            {"id": "101", "date": "2024-06-15", "name": "Summer Gran Fondo 2024", "sport": "cycling", "device_watts": True, "duration_min": 180, "tss": 210, "work_kj": 2600, "peaks_fresh": {"5m": 440, "20m": 345}},
            {"id": "202", "date": "2025-10-03", "name": "Autumn Classic Race", "sport": "cycling", "device_watts": True, "duration_min": 150, "tss": 195, "work_kj": 2300, "peaks_fresh": {"5m": 425, "20m": 335}},
            {"id": "303", "date": "2026-11-10", "name": "Base Spin", "sport": "cycling", "device_watts": True, "duration_min": 75, "tss": 65, "work_kj": 920, "peaks_fresh": {"5m": 380, "20m": 300}},
        ]
        curves = coach_engine.aggregate_epoch_curves(acts, today=today)
        self.assertEqual(curves["fresh"]["5m"]["90d"], 380)
        self.assertEqual(curves["fresh"]["5m"]["all_time"], 440)
        self.assertIn("2024", curves["yearly"])
        self.assertIn("2025", curves["yearly"])
        self.assertIn("2026", curves["yearly"])

        # Search across all years works even without passing --months 36
        hist_search = pillars_engine.query_coach_history(acts, search_query="Gran Fondo", today=today)
        self.assertIn("Summer Gran Fondo 2024", hist_search)
        self.assertNotIn("Base Spin", hist_search)

        hist_year = pillars_engine.query_coach_history(acts, year=2025, today=today)
        self.assertIn("Autumn Classic Race", hist_year)
        self.assertNotIn("2024-06-15", hist_year)

        # Historical per-day PMC map distinguishes past peak day vs current quiet day
        pmc = coach_engine.compute_pmc_metrics(acts, today=today)
        dmap = pmc["daily_map"]
        self.assertIn("2024-06-15", dmap)
        self.assertIn("2026-11-20", dmap)
        self.assertNotEqual(dmap["2024-06-15"]["atl"], dmap["2026-11-20"]["atl"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
