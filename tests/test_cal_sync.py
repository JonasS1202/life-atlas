"""Offline unit & integration tests for Atlas Multi-Account Google Calendar 3-Way Sync.

Uses an in-memory `FakeGCalAPI` HTTP transport (0 real network calls) covering:
- Pure-stdlib YAML config roundtrip
- Multi-account OAuth2 PKCE, client JSON import, token refresh & 7-day expiry detection
- Multi-account calendar discovery, shared `calendarId` deduplication & subcalendar creation
- Smart-Formatter (clean titles, workout durations `(82m | ...)`, description details, transparency)
- 3-stage deduplication (exact ID, high-confidence auto-link, ambiguous conflict queue + persistence)
- Full 3-way sync (create, update, `[x]` -> `✓`, `[-]` <-> delete, remote cancel -> `[-]`,
  Smart-Merge, direct conflict resolution, read-only mirror protection, Bulk-Delete Circuit Breaker,
  HTTP 410 syncToken recovery, rolling window cap for recurring vs single events)
- Decentralized `due:` push-only sync
- Cross-layer FreeBusy, Safety-Gated Create/Meet & RSVP
- `atlas_hook.py` parsing/HTML enrichment & `cal_engine.toggle_todo_in_brain` compatibility
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
import urllib.parse

REPO_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
TOOLS_DIR = os.path.join(REPO_ROOT, "core", "tools")
HOOK_PATH = os.path.join(REPO_ROOT, "core", "hooks", "atlas_hook.py")
if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)

from atlas_lib import cal_engine, cal_sync, gcal_provider  # noqa: E402


def _write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


class FakeGCalAPI:
    """In-memory deterministic Google Calendar API v3 + OAuth2 fake HTTP server."""

    def __init__(self) -> None:
        self.tokens: dict[str, str] = {
            "tok_personal": "personal",
            "tok_work": "work",
        }
        self.emails: dict[str, str] = {
            "personal": "user@example.com",
            "work": "user@company.example",
        }
        self.calendars: dict[str, list[dict]] = {
            "personal": [
                {"id": "user@example.com", "summary": "Personal", "primary": True, "accessRole": "owner", "backgroundColor": "#38bdf8", "timeZone": "Europe/Berlin"},
                {"id": "work_ogcs@group.calendar.google.com", "summary": "Work Outlook Sync", "primary": False, "accessRole": "owner", "backgroundColor": "#f59e0b", "timeZone": "Europe/Berlin"},
                # Shared view of work primary calendar (must be deduplicated in favor of `work` account!)
                {"id": "user@company.example", "summary": "Company Calendar", "primary": False, "accessRole": "writer", "backgroundColor": "#a855f7", "timeZone": "Europe/Berlin"},
                # System calendars (must be ignored by default)
                {"id": "addressbook#contacts@group.v.calendar.google.com", "summary": "Birthdays", "primary": False, "accessRole": "reader"},
                {"id": "de.german#holiday@group.v.calendar.google.com", "summary": "Holidays", "primary": False, "accessRole": "reader"},
            ],
            "work": [
                {"id": "user@company.example", "summary": "Company Calendar", "primary": True, "accessRole": "owner", "backgroundColor": "#a855f7", "timeZone": "Europe/Berlin"},
            ],
        }
        # events[(account_id, calendar_id)][event_id] = event_dict
        self.events: dict[tuple[str, str], dict[str, dict]] = {}
        self.instances: dict[tuple[str, str, str], list[dict]] = {}
        self.deltas: dict[tuple[str, str], list[dict]] = {}
        self.sync_token_counter = 0
        self.expired_sync_tokens: set[str] = set()
        self.requests: list[tuple[str, str, dict | None]] = []
        self.next_id = 100
        self.fail_refresh_for: set[str] = set()

    def seed_event(self, account_id: str, calendar_id: str, ev: dict, as_delta: bool = False) -> dict:
        ev = json.loads(json.dumps(ev))
        ev.setdefault("id", f"gev_{self.next_id}")
        self.next_id += 1
        ev.setdefault("status", "confirmed")
        ev.setdefault("etag", f'"etag-{self.next_id}"')
        ev.setdefault("iCalUID", f"{ev['id']}@google.com")
        ev.setdefault("organizer", {"email": self.emails.get(account_id, ""), "self": True})
        self.events.setdefault((account_id, calendar_id), {})[ev["id"]] = ev
        if as_delta:
            self.deltas.setdefault((account_id, calendar_id), []).append(ev)
        return ev

    def __call__(self, method: str, url: str, headers: dict[str, str], body: bytes | None, timeout: float) -> tuple[int, dict]:
        parsed = urllib.parse.urlparse(url)
        qs = urllib.parse.parse_qs(parsed.query)
        req_json = None
        if body and headers.get("Content-Type", "").startswith("application/json"):
            req_json = json.loads(body.decode("utf-8"))
        self.requests.append((method, url, req_json))

        # OAuth token endpoint
        if url.startswith(gcal_provider.GCAL_TOKEN_URL):
            form = urllib.parse.parse_qs(body.decode("utf-8") if body else "")
            grant = (form.get("grant_type") or [""])[0]
            if grant == "authorization_code":
                code = (form.get("code") or [""])[0]
                acct = "work" if "work" in code else "personal"
                tok = f"tok_{acct}"
                self.tokens[tok] = acct
                return 200, {"access_token": tok, "refresh_token": f"ref_{acct}", "expires_in": 3600, "scope": " ".join(gcal_provider.GCAL_SCOPES)}
            if grant == "refresh_token":
                rtok = (form.get("refresh_token") or [""])[0]
                if rtok in self.fail_refresh_for:
                    return 400, {"error": "invalid_grant", "error_description": "Token has been expired or revoked."}
                acct = "work" if "work" in rtok else "personal"
                tok = f"tok_{acct}"
                self.tokens[tok] = acct
                return 200, {"access_token": tok, "expires_in": 3600}
            return 400, {"error": "unsupported_grant_type"}

        auth_hdr = headers.get("Authorization", "")
        token = auth_hdr.replace("Bearer ", "").strip()
        acct = self.tokens.get(token)
        if not acct:
            return 401, {"error": "unauthorized"}

        if url.startswith(gcal_provider.GCAL_USERINFO_URL):
            return 200, {"email": self.emails.get(acct, f"{acct}@example.com")}

        path = parsed.path
        if path.startswith("/calendar/v3"):
            path = path[len("/calendar/v3"):]

        # GET /users/me/calendarList
        if method == "GET" and path == "/users/me/calendarList":
            return 200, {"items": list(self.calendars.get(acct, []))}

        # POST /calendars (create secondary calendar)
        if method == "POST" and path == "/calendars":
            summary = (req_json or {}).get("summary", "Subcalendar")
            cid = f"sub_{summary.lower().replace(' ', '_')}@group.calendar.google.com"
            cal_obj = {
                "id": cid, "summary": summary, "primary": False,
                "accessRole": "owner", "timeZone": (req_json or {}).get("timeZone", "UTC"),
            }
            self.calendars.setdefault(acct, []).append(cal_obj)
            return 200, cal_obj

        # POST /freeBusy
        if method == "POST" and path == "/freeBusy":
            out_cals = {}
            for item in (req_json or {}).get("items") or []:
                cid = item["id"]
                busy = []
                for ev in self.events.get((acct, cid), {}).values():
                    if ev.get("status") == "cancelled" or ev.get("transparency") == "transparent":
                        continue
                    st = (ev.get("start") or {}).get("dateTime")
                    en = (ev.get("end") or {}).get("dateTime")
                    if st and en:
                        busy.append({"start": st, "end": en})
                out_cals[cid] = {"busy": busy}
            return 200, {"calendars": out_cals}

        # /calendars/{cid}/events[...]
        parts = [urllib.parse.unquote(p) for p in path.strip("/").split("/")]
        if len(parts) >= 3 and parts[0] == "calendars" and parts[2] == "events":
            cid = parts[1]
            # GET /calendars/{cid}/events
            if method == "GET" and len(parts) == 3:
                stoken = (qs.get("syncToken") or [""])[0]
                if stoken and stoken in self.expired_sync_tokens:
                    return 410, {"error": {"code": 410, "message": "Sync token is no longer valid."}}
                self.sync_token_counter += 1
                next_tok = f"st_{self.sync_token_counter}"
                if stoken:
                    items = list(self.deltas.pop((acct, cid), []))
                    return 200, {"items": items, "nextSyncToken": next_tok}
                items = list(self.events.get((acct, cid), {}).values())
                self.deltas.pop((acct, cid), None)
                return 200, {"items": items, "nextSyncToken": next_tok}

            # POST /calendars/{cid}/events
            if method == "POST" and len(parts) == 3:
                ev = json.loads(json.dumps(req_json or {}))
                ev["id"] = f"gev_{self.next_id}"
                self.next_id += 1
                ev["status"] = "confirmed"
                ev["etag"] = f'"etag-{self.next_id}"'
                ev["iCalUID"] = f"{ev['id']}@google.com"
                ev["htmlLink"] = f"https://calendar.google.com/event?eid={ev['id']}"
                ev["organizer"] = {"email": self.emails.get(acct, ""), "self": True}
                if qs.get("conferenceDataVersion") == ["1"] and ev.get("conferenceData"):
                    ev["hangoutLink"] = f"https://meet.google.com/abc-defg-{self.next_id}"
                self.events.setdefault((acct, cid), {})[ev["id"]] = ev
                return 200, ev

            # GET /calendars/{cid}/events/{ev_id}/instances
            if method == "GET" and len(parts) == 5 and parts[4] == "instances":
                ev_id = parts[3]
                return 200, {"items": list(self.instances.get((acct, cid, ev_id), []))}

            # GET / PATCH / DELETE /calendars/{cid}/events/{ev_id}
            if len(parts) == 4:
                ev_id = parts[3]
                store = self.events.setdefault((acct, cid), {})
                if method == "GET":
                    if ev_id not in store:
                        return 404, {"error": "not found"}
                    return 200, store[ev_id]
                if method == "PATCH":
                    if ev_id not in store:
                        return 404, {"error": "not found"}
                    cur = store[ev_id]
                    for k, v in (req_json or {}).items():
                        if k == "extendedProperties" and isinstance(v, dict):
                            cur.setdefault("extendedProperties", {}).setdefault("private", {}).update(v.get("private") or {})
                        elif v is None:
                            cur.pop(k, None)
                        else:
                            cur[k] = v
                    self.next_id += 1
                    cur["etag"] = f'"etag-{self.next_id}"'
                    return 200, cur
                if method == "DELETE":
                    if ev_id in store:
                        store[ev_id]["status"] = "cancelled"
                    return 204, {}

        return 404, {"error": f"unhandled {method} {path}"}


class CalSyncUnit(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="atlas-calsync-test-")
        self.brain = os.path.join(self.tmp, "brain")
        os.makedirs(os.path.join(self.brain, "calendar"), exist_ok=True)
        os.makedirs(os.path.join(self.brain, "_atlas", "cache"), exist_ok=True)
        os.makedirs(os.path.join(self.brain, "_secrets"), exist_ok=True)
        self.fake = FakeGCalAPI()
        self.provider = gcal_provider.GoogleCalendarProvider(self.brain, http_fn=self.fake)
        self.today = dt.date(2026, 11, 24)
        self._seed_oauth_accounts()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _seed_oauth_accounts(self) -> None:
        future_exp = str(int(time.time()) + 3600)
        gcal_provider.save_gcal_env(self.brain, {
            "GCAL_CLIENT_ID": "test-client-id.apps.googleusercontent.com",
            "GCAL_CLIENT_SECRET": "test-client-secret",
            "GCAL_ACCOUNTS": "personal,work",
            "GCAL_PERSONAL_EMAIL": "user@example.com",
            "GCAL_PERSONAL_ACCESS_TOKEN": "tok_personal",
            "GCAL_PERSONAL_REFRESH_TOKEN": "ref_personal",
            "GCAL_PERSONAL_EXPIRES_AT": future_exp,
            "GCAL_WORK_EMAIL": "user@company.example",
            "GCAL_WORK_ACCESS_TOKEN": "tok_work",
            "GCAL_WORK_REFRESH_TOKEN": "ref_work",
            "GCAL_WORK_EXPIRES_AT": future_exp,
        })

    def test_yaml_roundtrip_and_oauth_pkce_refresh(self) -> None:
        cfg = {
            "version": 1,
            "enabled": True,
            "timezone": "Europe/Berlin",
            "bulk_delete_limit": 3,
            "routing": {"sync_kinds": ["event", "workout"], "local_only_kinds": ["birthday"]},
            "layers": [
                {
                    "id": "personal", "file": "README.md", "label": "Personal", "color": "#38bdf8",
                    "account": "personal", "calendar_id": "user@example.com", "mode": "bidirectional",
                    "subcalendars": {"workout": "wko_id", "tasks": "tasks_id"},
                },
                {
                    "id": "work-ro", "file": "work-ro.md", "label": "Work Mirror", "color": "#f59e0b",
                    "account": "personal", "calendar_id": "ro@group.calendar.google.com", "mode": "read-only",
                },
            ],
        }
        cal_sync.save_sync_config(self.brain, cfg)
        loaded = cal_sync.load_sync_config(self.brain)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["timezone"], "Europe/Berlin")
        self.assertEqual(len(loaded["layers"]), 2)
        self.assertEqual(loaded["layers"][0]["subcalendars"]["workout"], "wko_id")
        self.assertEqual(loaded["layers"][1]["mode"], "read-only")

        # Client JSON import + PKCE state mint/consume + code exchange
        cj_path = os.path.join(self.tmp, "client_secret.json")
        _write(cj_path, json.dumps({"installed": {"client_id": "cid-from-json", "client_secret": "csec-from-json"}}))
        ok, _ = gcal_provider.import_client_json(self.brain, cj_path)
        self.assertTrue(ok)
        state, ver, chall = gcal_provider.mint_gcal_oauth_state(self.brain, account_id="work", port=8765)
        self.assertTrue(len(chall) > 20)
        s_ok, s_acct, s_ver = gcal_provider.consume_gcal_oauth_state(self.brain, state)
        self.assertTrue(s_ok)
        self.assertEqual(s_acct, "work")
        self.assertEqual(s_ver, ver)
        # Re-consuming same state must fail (replay protection)
        s_ok2, _, _ = gcal_provider.consume_gcal_oauth_state(self.brain, state)
        self.assertFalse(s_ok2)

        ex_ok, _ = gcal_provider.gcal_exchange_code(
            self.brain, "work", "auth_code_work", "http://127.0.0.1:8765/gcal-callback",
            code_verifier=ver, http_fn=self.fake,
        )
        self.assertTrue(ex_ok)

        # Force expired access token -> refresh failure flags `refresh_expired`
        env = gcal_provider.load_gcal_env(self.brain)
        env["GCAL_WORK_EXPIRES_AT"] = "0"
        gcal_provider.save_gcal_env(self.brain, env)
        self.fake.fail_refresh_for.add("ref_work")
        tok, err = gcal_provider.gcal_refresh_if_needed(self.brain, "work", http_fn=self.fake)
        self.assertEqual(tok, "")
        self.assertEqual(err, "refresh_expired")
        accts = {a["id"]: a for a in gcal_provider.list_configured_accounts(self.brain)}
        self.assertFalse(accts["work"]["connected"])
        self.assertEqual(accts["work"]["auth_error"], "refresh_expired")

    def test_multi_account_discovery_shared_calendar_dedup_and_subcalendars(self) -> None:
        dry = cal_sync.discover_calendars(self.brain, provider=self.provider, execute=False)
        self.assertTrue(dry["ok"])
        self.assertFalse(dry["applied"])
        # Shared `user@company.example` in `personal` must be deduplicated in favor of `work` owner
        self.assertEqual(len(dry["duplicates_skipped"]), 1)
        self.assertEqual(dry["duplicates_skipped"][0]["owner_account"], "work")
        # System calendars (#contacts / #holiday) excluded
        cids = [ly["calendar_id"] for ly in dry["layers"]]
        self.assertIn("user@example.com", cids)
        self.assertIn("user@company.example", cids)
        self.assertIn("work_ogcs@group.calendar.google.com", cids)
        self.assertFalse(any("#contacts" in c or "#holiday" in c for c in cids))

        # Apply discovery -> creates Atlas Training & Atlas Tasks subcalendars + sync.yaml + layer .md files
        applied = cal_sync.discover_calendars(self.brain, provider=self.provider, execute=True)
        self.assertTrue(applied["ok"])
        self.assertTrue(applied["applied"])
        self.assertTrue(os.path.isfile(cal_sync.sync_config_path(self.brain)))
        cfg = cal_sync.load_sync_config(self.brain)
        readme_ly = next(ly for ly in cfg["layers"] if ly["file"] == "README.md")
        work_ly = next(ly for ly in cfg["layers"] if ly["account"] == "work")
        ogcs_ly = next(ly for ly in cfg["layers"] if ly["calendar_id"] == "work_ogcs@group.calendar.google.com")
        self.assertEqual(readme_ly["mode"], "bidirectional")
        self.assertTrue(readme_ly["subcalendars"]["workout"].startswith("sub_atlas_coach"))
        self.assertTrue(readme_ly["subcalendars"]["tasks"].startswith("sub_atlas_tasks"))
        self.assertEqual(work_ly["mode"], "bidirectional")
        self.assertEqual(ogcs_ly["mode"], "read-only")
        self.assertTrue(os.path.isfile(os.path.join(self.brain, "calendar", ogcs_ly["file"])))

    def test_smart_formatter_routing_local_optout_and_decentralized_due(self) -> None:
        cal_sync.discover_calendars(self.brain, provider=self.provider, execute=True)
        cfg = cal_sync.load_sync_config(self.brain)
        readme_ly = next(ly for ly in cfg["layers"] if ly["file"] == "README.md")
        wko_cid = readme_ly["subcalendars"]["workout"]
        tasks_cid = readme_ly["subcalendars"]["tasks"]

        _write(
            os.path.join(self.brain, "calendar", "README.md"),
            "---\nsummary: Cal\ntags: [atlas, calendar]\n---\n# Calendar\n\n## Timeline\n\n"
            "- [ ] 2026-11-25 17:30 [workout:cycling] Spin-Ups & Cadence-Mix (82m | 15m WU 55%, 4x 30s@230W, 10m CD)\n"
            "- [ ] 2026-11-26 [todo] Cancel streaming subscription (areas/finance/subs.md)\n"
            "- [ ] 2026-11-27 10:00-11:00 [event] Private Secret Note #local\n"
            "- [ ] 2026-11-28 [birthday] Alex Birthday\n"
            "- [ ] 2026-11-29 [birthday] Best Friend Birthday #sync\n"
            "- [ ] 2027-05-23 [goal:A] Spring Classic Race\n",
        )
        _write(
            os.path.join(self.brain, "areas", "finance", "contract.md"),
            "---\nsummary: Contract\ndue:\n  - 2026-12-05 Renew insurance policy\n---\n# Contract\n",
        )

        res = cal_sync.run_sync(self.brain, provider=self.provider, execute=True, today=self.today)
        self.assertTrue(res["ok"])
        self.assertEqual(res["pushed_new"], 5)  # workout + todo + forced birthday + goal + decentralized due

        # 1. Workout landed in Atlas Training with clean summary, +82m duration (17:30 -> 18:52), intervals in description
        wko_evs = list(self.fake.events.get(("personal", wko_cid), {}).values())
        self.assertEqual(len(wko_evs), 1)
        self.assertEqual(wko_evs[0]["summary"], "🚴 Spin-Ups & Cadence-Mix")
        self.assertIn("17:30:00", wko_evs[0]["start"]["dateTime"])
        self.assertIn("18:52:00", wko_evs[0]["end"]["dateTime"])
        self.assertIn("4x 30s@230W", wko_evs[0]["description"])

        # 2. Dated [todo] AND decentralized `due:` landed in Atlas Tasks & Fristen with transparency=transparent
        task_evs = list(self.fake.events.get(("personal", tasks_cid), {}).values())
        self.assertEqual(len(task_evs), 2)
        summaries = {e["summary"] for e in task_evs}
        self.assertIn("☐ Cancel streaming subscription", summaries)
        self.assertIn("⏳ Renew insurance policy", summaries)
        for te in task_evs:
            self.assertEqual(te["transparency"], "transparent")
            self.assertIn("Atlas: areas/finance/", te["description"])

        # Regression: second sync receiving those exact `due:` events in tasks delta must NOT echo-pull them into README.md!
        for te in task_evs:
            self.fake.deltas.setdefault(("personal", tasks_cid), []).append(te)
        res_echo = cal_sync.run_sync(self.brain, provider=self.provider, execute=True, today=self.today)
        self.assertEqual(res_echo["pulled_new"], 0)
        with open(os.path.join(self.brain, "calendar", "README.md"), encoding="utf-8") as fh:
            self.assertNotIn("Renew insurance policy", fh.read())

        # Regression: long title with `: ` inside parentheses must NOT truncate inside open parens
        paren_ev = cal_sync.format_atlas_for_gcal({
            "rel": "areas/career/job.md", "start": "2026-11-24", "end": "2026-11-24", "time": "",
            "kind": "deadline", "subkind": "", "status": "open",
            "what": "November salary payment including bonus (arrives 24th-26th, target: 8,600 EUR gross / 5,023 EUR net, check slip)",
        }, "d999999", timezone="Europe/Berlin")
        self.assertEqual(paren_ev["summary"].count("("), paren_ev["summary"].count(")"))

        # 3. Primary personal calendar got forced `#sync` birthday + goal, NOT `#local` and NOT default birthday
        prim_evs = list(self.fake.events.get(("personal", "user@example.com"), {}).values())
        prim_sums = {e["summary"] for e in prim_evs}
        self.assertIn("🎂 Best Friend Birthday", prim_sums)
        self.assertIn("🏆 Spring Classic Race", prim_sums)
        self.assertFalse(any("Private Secret" in s for s in prim_sums))
        self.assertFalse(any("Alex Birthday" in s for s in prim_sums))

        # 4. Adding `#local` later to an already-synced item removes it from Google while preserving the local line
        readme_p = os.path.join(self.brain, "calendar", "README.md")
        with open(readme_p, encoding="utf-8") as fh:
            txt = fh.read()
        txt = txt.replace("Spring Classic Race", "Spring Classic Race #local")
        _write(readme_p, txt)
        res2 = cal_sync.run_sync(self.brain, provider=self.provider, execute=True, today=self.today)
        self.assertEqual(res2["pushed_deleted"], 1)
        goal_ev = next(e for e in self.fake.events[("personal", "user@example.com")].values() if "Spring Classic" in e["summary"])
        self.assertEqual(goal_ev["status"], "cancelled")
        with open(readme_p, encoding="utf-8") as fh:
            self.assertIn("Spring Classic Race #local", fh.read())

    def test_three_stage_dedup_and_readonly_ogcs_protection(self) -> None:
        cal_sync.discover_calendars(self.brain, provider=self.provider, execute=True)
        cfg = cal_sync.load_sync_config(self.brain)
        ogcs_ly = next(ly for ly in cfg["layers"] if ly["calendar_id"] == "work_ogcs@group.calendar.google.com")

        # Seed remote events in personal + read-only work mirror
        self.fake.seed_event("personal", "user@example.com", {
            "summary": "Project Update Alpha",
            "start": {"dateTime": "2026-11-26T10:00:00+01:00"},
            "end": {"dateTime": "2026-11-26T11:00:00+01:00"},
            "hangoutLink": "https://meet.google.com/aaa-bbbb-ccc",
            "location": "Remote",
        })
        # Ambiguous event at exact same slot 10:15 as local "Dentist appointment"
        amb_ev = self.fake.seed_event("personal", "user@example.com", {
            "summary": "Dr. Miller Checkup",
            "start": {"dateTime": "2026-11-27T10:15:00+01:00"},
            "end": {"dateTime": "2026-11-27T11:00:00+01:00"},
        })
        # Read-only OGCS work mirror event (also present locally in README.md -> must not duplicate to personal!)
        self.fake.seed_event("personal", "work_ogcs@group.calendar.google.com", {
            "summary": "Company Winter Offsite Hamburg",
            "start": {"date": "2026-11-28"},
            "end": {"date": "2026-11-30"},
        })
        # Recurring work standup far in future (+120d) -> must be filtered by +90d rolling window!
        self.fake.seed_event("personal", "work_ogcs@group.calendar.google.com", {
            "summary": "Daily Standup",
            "recurringEventId": "rec_daily_1",
            "start": {"dateTime": "2027-03-25T09:00:00+01:00"},
            "end": {"dateTime": "2027-03-25T09:15:00+01:00"},
        })

        _write(
            os.path.join(self.brain, "calendar", "README.md"),
            "---\nsummary: Cal\ntags: [atlas, calendar]\n---\n# Calendar\n\n## Timeline\n\n"
            "- [ ] 2026-11-26 10:00-11:00 [event] Project Update Alpha (Google Meet: Alice, Bob)\n"
            "- [ ] 2026-11-27 10:15 [event] Dentist appointment\n"
            "- [ ] 2026-11-28..2026-11-29 [blocked] Company Winter Offsite Hamburg\n",
        )

        # Initial dry-run must not write anything
        dry = cal_sync.run_sync(self.brain, provider=self.provider, execute=False, today=self.today)
        self.assertEqual(dry["auto_linked"], 1)
        self.assertEqual(dry["conflicts_added"], 1)
        self.assertEqual(dry["pushed_new"], 0)  # Offsite skipped as RO-dup; Dentist held for conflict review!

        # Execute initial sync
        res = cal_sync.run_sync(self.brain, provider=self.provider, execute=True, today=self.today)
        self.assertEqual(res["auto_linked"], 1)
        self.assertEqual(res["open_conflicts"], 1)
        self.assertEqual(res["pushed_new"], 0)

        # Verify ZERO write requests were ever sent to the read-only OGCS calendar!
        ro_writes = [
            r for r in self.fake.requests
            if r[0] in ("POST", "PATCH", "DELETE") and "work_ogcs" in r[1]
        ]
        self.assertEqual(ro_writes, [])

        # Verify read-only mirror file has Offsite and excluded the +120d recurring standup
        with open(os.path.join(self.brain, "calendar", ogcs_ly["file"]), encoding="utf-8") as fh:
            ro_txt = fh.read()
        self.assertIn("Company Winter Offsite Hamburg", ro_txt)
        self.assertNotIn("Daily Standup", ro_txt)

        # Critical regression test: second incremental sync while conflict is STILL open must NOT push Dentist!
        res2 = cal_sync.run_sync(self.brain, provider=self.provider, execute=True, today=self.today)
        self.assertEqual(res2["pushed_new"], 0)
        self.assertEqual(res2["pulled_new"], 0)

        # Resolve conflict via `link` -> links Dentist <-> Dr. Miller without duplicates
        confs = cal_sync.list_conflicts(self.brain)
        self.assertEqual(len(confs), 1)
        self.assertEqual(confs[0]["gcal_event_id"], amb_ev["id"])
        r_res = cal_sync.resolve_conflict(self.brain, confs[0]["id"], "link", provider=self.provider)
        self.assertTrue(r_res["ok"])
        self.assertEqual(len(cal_sync.list_conflicts(self.brain)), 0)

    def test_bidirectional_3way_updates_status_smart_merge_and_circuit_breaker(self) -> None:
        cal_sync.discover_calendars(self.brain, provider=self.provider, execute=True)
        cfg = cal_sync.load_sync_config(self.brain)
        wko_cid = next(ly for ly in cfg["layers"] if ly["file"] == "README.md")["subcalendars"]["workout"]

        _write(
            os.path.join(self.brain, "calendar", "README.md"),
            "---\nsummary: Cal\ntags: [atlas, calendar]\n---\n# Calendar\n\n## Timeline\n\n"
            "- [ ] 2026-11-25 17:30 [workout:cycling] Tempo Ride (75m | 15m WU, 3x 12m@245W, 10m CD)\n"
            "- [ ] 2026-11-26 14:00-15:00 [event] Client Sync\n"
            "- [ ] 2026-11-27 09:00-10:00 [event] Team Review\n"
            "- [ ] 2026-11-28 11:00-12:00 [event] Lunch Meetup\n"
            "- [ ] 2026-11-29 15:00-16:00 [event] Coffee Chat\n",
        )
        r1 = cal_sync.run_sync(self.brain, provider=self.provider, execute=True, today=self.today)
        self.assertEqual(r1["pushed_new"], 5)

        # 1. Remote moves workout to 18:00 -> Atlas pulls new time AND preserves `(75m | 15m WU, 3x 12m@245W, 10m CD)`!
        wko_ev = next(iter(self.fake.events[("personal", wko_cid)].values()))
        wko_ev["start"] = {"dateTime": "2026-11-25T18:00:00+01:00"}
        wko_ev["end"] = {"dateTime": "2026-11-25T19:15:00+01:00"}
        self.fake.seed_event("personal", wko_cid, wko_ev, as_delta=True)
        r2 = cal_sync.run_sync(self.brain, provider=self.provider, execute=True, today=self.today)
        self.assertEqual(r2["pulled_updated"], 1)
        readme_p = os.path.join(self.brain, "calendar", "README.md")
        with open(readme_p, encoding="utf-8") as fh:
            txt = fh.read()
        self.assertIn("2026-11-25 18:00-19:15 [workout:cycling] Tempo Ride (75m | 15m WU, 3x 12m@245W, 10m CD)", txt)

        # 2. Mark workout `[x]` in Atlas -> stays in Google with `✓ ` prefix (never deleted!)
        txt = txt.replace("- [ ] 2026-11-25 18:00-19:15", "- [x] 2026-11-25 18:00-19:15")
        _write(readme_p, txt)
        r3 = cal_sync.run_sync(self.brain, provider=self.provider, execute=True, today=self.today)
        self.assertEqual(r3["pushed_updated"], 1)
        self.assertEqual(r3["pushed_deleted"], 0)
        wko_after = next(iter(self.fake.events[("personal", wko_cid)].values()))
        self.assertTrue(wko_after["summary"].startswith("✓ "))

        # 3. Non-overlapping simultaneous edit on "Client Sync": Google moves time 14:00->16:00, Atlas checks `[x]` -> Smart-Merge!
        cs_ev = next(e for e in self.fake.events[("personal", "user@example.com")].values() if "Client Sync" in e["summary"])
        cs_ev["start"] = {"dateTime": "2026-11-26T16:00:00+01:00"}
        cs_ev["end"] = {"dateTime": "2026-11-26T17:00:00+01:00"}
        self.fake.seed_event("personal", "user@example.com", cs_ev, as_delta=True)
        txt = txt.replace("- [ ] 2026-11-26 14:00-15:00 [event] Client Sync", "- [x] 2026-11-26 14:00-15:00 [event] Client Sync")
        _write(readme_p, txt)
        r4 = cal_sync.run_sync(self.brain, provider=self.provider, execute=True, today=self.today)
        self.assertEqual(r4["smart_merged"], 1)
        with open(readme_p, encoding="utf-8") as fh:
            txt = fh.read()
        self.assertIn("- [x] 2026-11-26 16:00-17:00 [event] Client Sync", txt)

        # 4. Remote deletion of "Team Review" -> marked `[-]` in Atlas (no text loss!)
        tr_ev = next(e for e in self.fake.events[("personal", "user@example.com")].values() if "Team Review" in e["summary"])
        tr_ev["status"] = "cancelled"
        self.fake.seed_event("personal", "user@example.com", tr_ev, as_delta=True)
        r5 = cal_sync.run_sync(self.brain, provider=self.provider, execute=True, today=self.today)
        self.assertEqual(r5["pulled_cancelled"], 1)
        with open(readme_p, encoding="utf-8") as fh:
            txt = fh.read()
        self.assertIn("- [-] 2026-11-27 09:00-10:00 [event] Team Review", txt)

        # 5. Bulk-Delete Circuit Breaker: removing 4 active events (> limit 3) blocks ALL deletes!
        _write(readme_p, "---\nsummary: Cal\ntags: [atlas, calendar]\n---\n# Calendar\n\n## Timeline\n\n")
        r6 = cal_sync.run_sync(self.brain, provider=self.provider, execute=True, today=self.today)
        self.assertTrue(r6["bulk_delete_blocked"])
        self.assertEqual(r6["pushed_deleted"], 0)
        # Re-run with `--allow-bulk-delete` executes them
        r7 = cal_sync.run_sync(self.brain, provider=self.provider, execute=True, allow_bulk_delete=True, today=self.today)
        self.assertFalse(r7["bulk_delete_blocked"])
        self.assertEqual(r7["pushed_deleted"], 4)

    def test_sync_token_410_recovery_freebusy_safety_gate_and_hook_ui(self) -> None:
        cal_sync.discover_calendars(self.brain, provider=self.provider, execute=True)
        # Seed a work event + a personal event
        self.fake.seed_event("work", "user@company.example", {
            "summary": "Architecture Review",
            "start": {"dateTime": "2026-11-25T10:00:00+01:00"},
            "end": {"dateTime": "2026-11-25T11:30:00+01:00"},
            "location": "Room 4",
            "hangoutLink": "https://meet.google.com/xyz-1234-567",
            "attendees": [{"email": "user@company.example", "self": True, "responseStatus": "needsAction"}],
            "organizer": {"email": "lead@company.example", "self": False},
        })
        r1 = cal_sync.run_sync(self.brain, provider=self.provider, execute=True, today=self.today)
        self.assertEqual(r1["pulled_new"], 1)

        # Expire ALL current sync tokens -> HTTP 410 Gone must transparently recover with full sync!
        for i in range(1, self.fake.sync_token_counter + 1):
            self.fake.expired_sync_tokens.add(f"st_{i}")
        r_410 = cal_sync.run_sync(self.brain, provider=self.provider, execute=True, today=self.today)
        self.assertTrue(r_410["ok"])
        self.assertEqual(r_410["warnings"], [])

        # Safety-Gate on `create_event_guarded` with attendees + Google Meet
        prev = cal_sync.create_event_guarded(
            self.brain, title="Client Demo", date_iso="2026-11-25", time_spec="14:00-15:00",
            account_id="work", attendees=["client@external.example"], add_meet=True,
            execute=False, provider=self.provider,
        )
        self.assertTrue(prev["dry_run"])
        self.assertTrue(prev["requires_confirmation"])
        self.assertEqual(prev["preview"]["sender"], "user@company.example")

        created = cal_sync.create_event_guarded(
            self.brain, title="Client Demo", date_iso="2026-11-25", time_spec="14:00-15:00",
            account_id="work", attendees=["client@external.example"], add_meet=True,
            execute=True, provider=self.provider,
        )
        self.assertTrue(created["applied"])
        self.assertIn("https://meet.google.com/", created["meet_link"])

        # Safety-Gate on `rsvp_event_guarded` -> preview first, then `declined` marks Atlas `[-]`
        rsvp_prev = cal_sync.rsvp_event_guarded(self.brain, "Architecture Review", "declined", execute=False, provider=self.provider)
        self.assertTrue(rsvp_prev["dry_run"])
        rsvp_ok = cal_sync.rsvp_event_guarded(self.brain, "Architecture Review", "declined", execute=True, provider=self.provider)
        self.assertTrue(rsvp_ok["applied"])
        with open(os.path.join(self.brain, "calendar", "work.md"), encoding="utf-8") as fh:
            self.assertIn("- [-] 2026-11-25 10:00-11:30 [event] Architecture Review", fh.read())

        # Cross-layer FreeBusy merges local + remote busy windows (14:00-15:00 busy on 2026-11-25)
        fb = cal_sync.compute_freebusy(
            self.brain, days=1, start_date_iso="2026-11-25",
            work_start_hour=8, work_end_hour=18, min_slot_minutes=30,
            provider=self.provider, use_remote=True,
        )
        self.assertTrue(fb["ok"])
        busy_slots = fb["days"][0]["busy"]
        self.assertTrue(any(b["start"] == "14:00" and b["end"] == "15:00" for b in busy_slots))

        # Hook parsing strips `<!-- g:uid -->` and SQLite metadata cache stores Meet link & layer info
        spec = importlib.util.spec_from_file_location("_test_cal_sync_hook", HOOK_PATH)
        hk = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(hk)
        hk.BRAIN = self.brain
        hk.STATE = os.path.join(self.brain, "_atlas", "state.json")
        hk.CACHE = os.path.join(self.brain, "_atlas", "cache", "index.sqlite")
        hk.ERR_LOG = os.path.join(self.brain, "_atlas", "cache", "hook_errors.log")
        hk.CONFLICTS_FILE = os.path.join(self.brain, "_atlas", "conflicts.json")

        items = hk.calendar_items({}, days=365, past_days=30, include_done=True)
        demo_it = next(i for i in items if "Client Demo" in i["what"])
        self.assertNotIn("<!-- g:", demo_it["what"])
        self.assertEqual(demo_it["sync_id"], created["atlas_uid"])

        meta_map = cal_sync.load_event_metadata_map(self.brain)
        self.assertIn(created["atlas_uid"], meta_map)
        self.assertTrue(meta_map[created["atlas_uid"]].get("meet_link", "").startswith("https://meet.google.com/"))
        ui_sum = cal_sync.get_sync_ui_summary(self.brain)
        self.assertTrue(ui_sum.get("enabled"))

        # Toggle checkbox via `cal_engine.toggle_todo_in_brain` works cleanly on synced lines with `<!-- g:uid -->`
        tgl = cal_engine.toggle_todo_in_brain(self.brain, "calendar/work.md", line_no=demo_it["line"], what=demo_it["what"], target_done=True)
        self.assertTrue(tgl["ok"])
        with open(os.path.join(self.brain, "calendar", "work.md"), encoding="utf-8") as fh:
            self.assertIn(f"- [x] 2026-11-25 14:00-15:00 [event] Client Demo <!-- g:{created['atlas_uid']} -->", fh.read())

    def test_edit_conflict_keep_both_hooks_and_cli_smoke(self) -> None:
        import subprocess
        cal_sync.discover_calendars(self.brain, provider=self.provider, execute=True)
        readme_p = os.path.join(self.brain, "calendar", "README.md")
        _write(
            readme_p,
            "---\nsummary: Cal\ntags: [atlas, calendar]\n---\n# Calendar\n\n## Timeline\n\n"
            "- [ ] 2026-11-26 10:00-11:00 [event] Strategy Session\n",
        )
        cal_sync.run_sync(self.brain, provider=self.provider, execute=True, today=self.today)

        # Simultaneous conflicting title edits on both sides -> `edit_conflict` queued, resolved with `use-atlas`
        ev = next(iter(self.fake.events[("personal", "user@example.com")].values()))
        ev["summary"] = "Remote Budget Call"
        self.fake.seed_event("personal", "user@example.com", ev, as_delta=True)
        with open(readme_p, encoding="utf-8") as fh:
            txt = fh.read().replace("Strategy Session", "Product Roadmap Workshop")
        _write(readme_p, txt)
        r_conf = cal_sync.run_sync(self.brain, provider=self.provider, execute=True, today=self.today)
        self.assertEqual(r_conf["conflicts_added"], 1)
        confs = cal_sync.list_conflicts(self.brain)
        self.assertEqual(confs[0]["kind"], "edit_conflict")
        res_u = cal_sync.resolve_conflict(self.brain, confs[0]["id"], "use-atlas", provider=self.provider)
        self.assertTrue(res_u["ok"])
        self.assertEqual(self.fake.events[("personal", "user@example.com")][ev["id"]]["summary"], "Product Roadmap Workshop")

        # Hook auto-pull TTL gating: immediately after sync, `maybe_auto_pull(urgent=False)` does 0 network calls
        reqs_before = len(self.fake.requests)
        self.assertIsNone(cal_sync.maybe_auto_pull(self.brain, urgent=False, provider=self.provider))
        self.assertEqual(len(self.fake.requests), reqs_before)

        # CLI smoke tests (`cal status --json`, `cal list --json`, `cal freebusy --local-only --json`, `todo add --cal`)
        atlas_py = os.path.join(TOOLS_DIR, "atlas.py")
        p_st = subprocess.run(
            [sys.executable, "-I", atlas_py, "--root", self.tmp, "cal", "status", "--json"],
            capture_output=True, text=True, check=True,
        )
        st_data = json.loads(p_st.stdout)
        self.assertTrue(st_data["enabled"])
        self.assertEqual(len(st_data["accounts"]), 2)

        p_fb = subprocess.run(
            [sys.executable, "-I", atlas_py, "--root", self.tmp, "cal", "freebusy", "--days", "2", "--date", "2026-11-26", "--local-only", "--json"],
            capture_output=True, text=True, check=True,
        )
        fb_data = json.loads(p_fb.stdout)
        self.assertTrue(fb_data["ok"])
        self.assertTrue(any(b["start"] == "10:00" and b["end"] == "11:00" for b in fb_data["days"][0]["busy"]))

        p_add = subprocess.run(
            [sys.executable, "-I", atlas_py, "--root", self.tmp, "todo", "add", "Client Check-in", "--to", "2026-11-27", "--time", "11:00", "--kind", "event", "--cal", "work"],
            capture_output=True, text=True, check=True,
        )
        self.assertIn("calendar/work.md", p_add.stdout)
        with open(os.path.join(self.brain, "calendar", "work.md"), encoding="utf-8") as fh:
            self.assertIn("2026-11-27 11:00 [event] Client Check-in", fh.read())

    def test_real_world_hardening_compounds_cancelled_birthdays_and_freebusy_role(self) -> None:
        # 1. German compound + headline matching (`Zahnarzttermin — Praxis` <-> `Zahnarzt`)
        self.assertGreaterEqual(cal_sync.title_similarity("Zahnarzttermin — Zahnärzte in den Höfen", "Zahnarzt"), 0.78)

        # 2. Exact multi-day span triggers conflict-review (`exact_slot=True`)
        compat, exact = cal_sync._time_window_compatible(
            {"start": "2027-03-11", "end": "2027-03-21", "time": ""},
            {"start": "2027-03-11", "end": "2027-03-21", "time": ""},
        )
        self.assertTrue(compat and exact)

        # 3. Cancelled Outlook mirror title (`Abgesagt: ...`) parses as skipped `[-]`
        pe = cal_sync.parse_gcal_event({
            "id": "c1", "summary": "Abgesagt: DevOps Tickets Review", "status": "confirmed",
            "start": {"dateTime": "2026-11-27T13:00:00+01:00"}, "end": {"dateTime": "2026-11-27T14:00:00+01:00"},
        })
        self.assertEqual(pe["status"], "skipped")

        # 4. Workout `[feedback: ...]` moves to Google description & exact duration stays intact; long todo smart-splits
        w_ev = cal_sync.format_atlas_for_gcal({
            "rel": "calendar/README.md", "start": "2026-11-20", "end": "2026-11-20", "time": "17:30",
            "kind": "workout", "subkind": "cycling", "status": "done",
            "what": "Spin-Ups (82m | 12m WU, 4x 30s) [feedback: RPE 6/10 — great legs]",
        }, "u1", timezone="Europe/Berlin")
        self.assertEqual(w_ev["summary"], "✓ 🚴 Spin-Ups")
        self.assertIn("18:52:00", w_ev["end"]["dateTime"])
        self.assertIn("Feedback: RPE 6/10", w_ev["description"])

        long_todo = cal_sync.format_atlas_for_gcal({
            "rel": "calendar/README.md", "start": "2026-11-23", "end": "2026-11-23", "time": "",
            "kind": "todo", "subkind": "", "status": "open",
            "what": "Order and install new desktop GPU: check case clearance >= 315 mm, use two separate PCIe power cables, run DDU and verify 4K swapchain settings",
        }, "u2", timezone="Europe/Berlin")
        self.assertEqual(long_todo["summary"], "☐ Order and install new desktop GPU")
        self.assertIn("check case clearance", long_todo["description"])

        # 5. Dedicated owner Birthday calendar discovered as bidirectional `default_kind=birthday` + RRULE:FREQ=YEARLY on push;
        #    and `freeBusyReader`-only calendar skipped cleanly instead of creating `(Ohne Titel)` layer
        self.fake.calendars["personal"].append({
            "id": "bday_custom@group.calendar.google.com", "summary": "🎂 Birthdays",
            "primary": False, "accessRole": "owner", "timeZone": "Europe/Berlin",
        })
        self.fake.calendars["personal"].append({
            "id": "shared_fb_only@company.example", "summary": "External Shared",
            "primary": False, "accessRole": "freeBusyReader", "timeZone": "Europe/Berlin",
        })
        disc = cal_sync.discover_calendars(self.brain, provider=self.provider, execute=True)
        self.assertEqual(len(disc["skipped_freebusy_only"]), 1)
        self.assertEqual(disc["skipped_freebusy_only"][0]["calendar_id"], "shared_fb_only@company.example")
        bday_ly = next(ly for ly in disc["layers"] if ly["calendar_id"] == "bday_custom@group.calendar.google.com")
        self.assertEqual(bday_ly["mode"], "bidirectional")
        self.assertEqual(bday_ly.get("default_kind"), "birthday")

        b_ev = cal_sync.format_atlas_for_gcal({
            "rel": f"calendar/{bday_ly['file']}", "start": "2027-02-12", "end": "2027-02-12", "time": "",
            "kind": "birthday", "subkind": "", "status": "open", "what": "Alex Sample",
        }, "b1", timezone="Europe/Berlin")
        self.assertEqual(b_ev.get("recurrence"), ["RRULE:FREQ=YEARLY"])

        # 6. Smart Coalescing: Gmail auto-booking `Stay at Villa Sunny` on exact same span as trip `Spring Camp`
        #    merges location/details into `Spring Camp` instead of pulling a duplicate row!
        self.fake.seed_event("personal", "user@example.com", {
            "summary": "Spring Camp",
            "start": {"date": "2027-03-11"}, "end": {"date": "2027-03-22"},
        })
        self.fake.seed_event("personal", "user@example.com", {
            "summary": "Stay at Villa Sunny",
            "location": "Main Street 1, 07360 Island",
            "description": "Booking ref #999",
            "start": {"date": "2027-03-11"}, "end": {"date": "2027-03-22"},
        })
        res_coal = cal_sync.run_sync(self.brain, provider=self.provider, execute=True, today=self.today)
        self.assertTrue(any("[coalesce-booking]" in a for a in res_coal["actions"]))
        with open(os.path.join(self.brain, "calendar", "README.md"), encoding="utf-8") as fh:
            rm_txt = fh.read()
        self.assertIn("Spring Camp", rm_txt)
        self.assertNotIn("Stay at Villa Sunny", rm_txt)
        details = cal_sync.find_event_details(self.brain, "Spring Camp")
        self.assertEqual(details[0]["location"], "Main Street 1, 07360 Island")

    def test_v180_rrule_synctoken_expansion_and_layer_freebusy_exclude(self) -> None:
        # 1. Discover with a read-only waste calendar + birthday calendar -> both auto-get `freebusy: false`
        self.fake.calendars["personal"].append({
            "id": "waste_bins@group.calendar.google.com", "summary": "Abfallkalender",
            "primary": False, "accessRole": "reader", "timeZone": "Europe/Berlin",
        })
        self.fake.calendars["personal"].append({
            "id": "birthdays_custom@group.calendar.google.com", "summary": "Birthdays",
            "primary": False, "accessRole": "owner", "timeZone": "Europe/Berlin",
        })
        disc = cal_sync.discover_calendars(self.brain, provider=self.provider, execute=True)
        waste_ly = next(ly for ly in disc["layers"] if ly["calendar_id"] == "waste_bins@group.calendar.google.com")
        bday_ly = next(ly for ly in disc["layers"] if ly["calendar_id"] == "birthdays_custom@group.calendar.google.com")
        self.assertIs(waste_ly.get("freebusy"), False)
        self.assertIs(bday_ly.get("freebusy"), False)

        # Initial full sync to establish sync tokens
        cal_sync.run_sync(self.brain, provider=self.provider, execute=True, today=self.today)

        # 2. Incremental syncToken delta delivers a NEW recurring master event (with `recurrence`, NO `recurringEventId`)
        master_ev = self.fake.seed_event("personal", "user@example.com", {
            "id": "rec_master_1",
            "summary": "Take out bins",
            "recurrence": ["RRULE:FREQ=WEEKLY;BYDAY=TH"],
            "start": {"dateTime": "2026-11-26T18:00:00+01:00"},
            "end": {"dateTime": "2026-11-26T18:15:00+01:00"},
        }, as_delta=True)
        inst1 = self.fake.seed_event("personal", "user@example.com", {
            "id": "rec_master_1_20261126",
            "recurringEventId": master_ev["id"],
            "summary": "Take out bins",
            "start": {"dateTime": "2026-11-26T18:00:00+01:00"},
            "end": {"dateTime": "2026-11-26T18:15:00+01:00"},
        })
        inst2 = self.fake.seed_event("personal", "user@example.com", {
            "id": "rec_master_1_20261203",
            "recurringEventId": master_ev["id"],
            "summary": "Take out bins",
            "start": {"dateTime": "2026-12-03T18:00:00+01:00"},
            "end": {"dateTime": "2026-12-03T18:15:00+01:00"},
        })
        self.fake.instances[("personal", "user@example.com", master_ev["id"])] = [inst1, inst2]

        r_inc = cal_sync.run_sync(self.brain, provider=self.provider, execute=True, today=self.today)
        self.assertEqual(r_inc["pulled_new"], 2)  # Both expanded instances pulled, NOT the master!
        readme_p = os.path.join(self.brain, "calendar", "README.md")
        with open(readme_p, encoding="utf-8") as fh:
            txt = fh.read()
        self.assertIn("2026-11-26 18:00-18:15 [event] Take out bins", txt)
        self.assertIn("2026-12-03 18:00-18:15 [event] Take out bins", txt)

        # Marking only Nov 26 `[x]` patches ONLY `rec_master_1_20261126`, leaving `rec_master_1` untouched!
        txt_done = txt.replace("- [ ] 2026-11-26 18:00-18:15", "- [x] 2026-11-26 18:00-18:15")
        _write(readme_p, txt_done)
        cal_sync.run_sync(self.brain, provider=self.provider, execute=True, today=self.today)
        self.assertTrue(self.fake.events[("personal", "user@example.com")]["rec_master_1_20261126"]["summary"].startswith("✓ "))
        self.assertEqual(self.fake.events[("personal", "user@example.com")]["rec_master_1"]["summary"], "Take out bins")

        # Deleting master series in delta cascades cancellation to active instances!
        master_cancelled = dict(master_ev, status="cancelled")
        self.fake.deltas.setdefault(("personal", "user@example.com"), []).append(master_cancelled)
        r_can = cal_sync.run_sync(self.brain, provider=self.provider, execute=True, today=self.today)
        self.assertGreaterEqual(r_can["pulled_cancelled"], 1)
        with open(readme_p, encoding="utf-8") as fh:
            self.assertIn("- [-] 2026-12-03 18:00-18:15 [event] Take out bins", fh.read())

        # 3. `freebusy: false` prevents an opaque full-day event in Abfallkalender from blocking the whole workday!
        self.fake.seed_event("personal", "waste_bins@group.calendar.google.com", {
            "summary": "Organic Waste",
            "transparency": "opaque",
            "start": {"dateTime": "2026-11-27T00:00:00+01:00"},
            "end": {"dateTime": "2026-11-27T23:59:00+01:00"},
        })
        fb = cal_sync.compute_freebusy(
            self.brain, days=1, start_date_iso="2026-11-27",
            work_start_hour=8, work_end_hour=18, min_slot_minutes=30,
            provider=self.provider, use_remote=True,
        )
        self.assertEqual(fb["days"][0]["busy"], [])
        self.assertEqual(fb["days"][0]["free"][0]["start"], "08:00")
        self.assertEqual(fb["days"][0]["free"][0]["end"], "18:00")

        # 4. `create_event_guarded` with `recurrence` attaches RRULE cleanly
        c_rec = cal_sync.create_event_guarded(
            self.brain, title="Weekly Standup", date_iso="2026-11-30", time_spec="09:00-09:30",
            account_id="personal", recurrence=["FREQ=WEEKLY;BYDAY=MO"], execute=True, provider=self.provider,
        )
        self.assertTrue(c_rec["applied"])
        created_g = self.fake.events[("personal", "user@example.com")][c_rec["gcal_event_id"]]
        self.assertEqual(created_g.get("recurrence"), ["RRULE:FREQ=WEEKLY;BYDAY=MO"])

    def test_todo_visual_clarity_open_done_reopen_and_reformat(self) -> None:
        """Open `[todo]` uses empty `☐`, done `[x]` swaps to single `✅` + Graphite (`colorId=8`), reopen clears grey, and `--reformat` upgrades legacy `☑️`/`✓ ☑️` idempotently."""
        cal_sync.discover_calendars(self.brain, provider=self.provider, execute=True)
        cfg = cal_sync.load_sync_config(self.brain)
        pers_ly = next(ly for ly in cfg["layers"] if ly["id"] == "personal")
        tasks_cid = pers_ly["subcalendars"]["tasks"]
        readme_p = os.path.join(self.brain, "calendar", "README.md")

        _write(
            readme_p,
            "# Calendar\n\n## Timeline\n"
            "- [ ] 2026-11-26 [todo] Renew passport\n"
            "- [ ] 2026-11-27 [todo] File travel expense\n",
        )
        r1 = cal_sync.run_sync(self.brain, provider=self.provider, execute=True, today=self.today)
        self.assertEqual(r1["pushed_new"], 2)
        ev_pass = next(e for e in self.fake.events[("personal", tasks_cid)].values() if "Renew passport" in e["summary"])
        self.assertEqual(ev_pass["summary"], "☐ Renew passport")
        self.assertNotIn("colorId", ev_pass)

        # 1. Check `[x]` in Atlas -> replaces `☐` with single `✅` + muted Graphite `colorId="8"`
        with open(readme_p, encoding="utf-8") as fh:
            txt = fh.read()
        _write(readme_p, txt.replace("- [ ] 2026-11-26 [todo] Renew passport", "- [x] 2026-11-26 [todo] Renew passport"))
        r2 = cal_sync.run_sync(self.brain, provider=self.provider, execute=True, today=self.today)
        self.assertEqual(r2["pushed_updated"], 1)
        ev_pass = self.fake.events[("personal", tasks_cid)][ev_pass["id"]]
        self.assertEqual(ev_pass["summary"], "✅ Renew passport")
        self.assertNotIn("☑️", ev_pass["summary"])
        self.assertNotIn("☐", ev_pass["summary"])
        self.assertEqual(ev_pass.get("colorId"), cal_sync.DONE_TODO_COLOR_ID)

        # 2. Reopen `[ ]` in Atlas -> back to `☐ Renew passport` and Graphite `colorId` cleared!
        with open(readme_p, encoding="utf-8") as fh:
            txt = fh.read()
        _write(readme_p, txt.replace("- [x] 2026-11-26 [todo] Renew passport", "- [ ] 2026-11-26 [todo] Renew passport"))
        r3 = cal_sync.run_sync(self.brain, provider=self.provider, execute=True, today=self.today)
        self.assertEqual(r3["pushed_updated"], 1)
        ev_pass = self.fake.events[("personal", tasks_cid)][ev_pass["id"]]
        self.assertEqual(ev_pass["summary"], "☐ Renew passport")
        self.assertNotIn("colorId", ev_pass)

        # 3. Remote manual check (`✅ ...`) and uncheck (`☐ ...`) sync status cleanly both ways!
        ev_exp = next(e for e in self.fake.events[("personal", tasks_cid)].values() if "File travel expense" in e["summary"])
        ev_exp["summary"] = "✅ File travel expense"
        self.fake.seed_event("personal", tasks_cid, ev_exp, as_delta=True)
        r4 = cal_sync.run_sync(self.brain, provider=self.provider, execute=True, today=self.today)
        self.assertEqual(r4["pulled_updated"], 1)
        with open(readme_p, encoding="utf-8") as fh:
            self.assertIn("- [x] 2026-11-27 [todo] File travel expense", fh.read())

        ev_exp["summary"] = "☐ File travel expense"
        ev_exp.setdefault("extendedProperties", {}).setdefault("private", {})["atlas_status"] = "done"
        self.fake.seed_event("personal", tasks_cid, ev_exp, as_delta=True)
        r5 = cal_sync.run_sync(self.brain, provider=self.provider, execute=True, today=self.today)
        self.assertEqual(r5["pulled_updated"], 1)
        with open(readme_p, encoding="utf-8") as fh:
            self.assertIn("- [ ] 2026-11-27 [todo] File travel expense", fh.read())

        # 4. Simulate legacy pre-1.8.1 remote summaries (`☑️ ...` / `✓ ☑️ ...`) -> `--reformat` upgrades once & is idempotent!
        ev_pass["summary"] = "☑️ Renew passport"
        ev_exp["summary"] = "✓ ☑️ File travel expense"
        with open(readme_p, encoding="utf-8") as fh:
            txt = fh.read()
        _write(readme_p, txt.replace("- [ ] 2026-11-27 [todo] File travel expense", "- [x] 2026-11-27 [todo] File travel expense"))
        # Mark `File travel expense` done first, then simulate stale cached metadata from pre-1.8.1 DB
        cal_sync.run_sync(self.brain, provider=self.provider, execute=True, today=self.today)
        ev_pass["summary"] = "☑️ Renew passport"
        ev_exp["summary"] = "✓ ☑️ File travel expense"
        ev_exp.pop("colorId", None)
        conn = cal_sync._open_db(self.brain)
        for row in conn.execute("SELECT atlas_uid, meta_json FROM event_links").fetchall():
            m = json.loads(row["meta_json"] or "{}")
            m.pop("raw_summary", None)
            m["color_id"] = ""
            conn.execute("UPDATE event_links SET meta_json=? WHERE atlas_uid=?", (json.dumps(m), row["atlas_uid"]))
        conn.commit()
        conn.close()

        r_ref = cal_sync.run_sync(self.brain, provider=self.provider, execute=True, reformat=True, today=self.today)
        self.assertEqual(r_ref["pushed_updated"], 2)
        self.assertEqual(self.fake.events[("personal", tasks_cid)][ev_pass["id"]]["summary"], "☐ Renew passport")
        self.assertEqual(self.fake.events[("personal", tasks_cid)][ev_exp["id"]]["summary"], "✅ File travel expense")
        self.assertEqual(self.fake.events[("personal", tasks_cid)][ev_exp["id"]].get("colorId"), cal_sync.DONE_TODO_COLOR_ID)

        # Second `--reformat` run must be a 0-op (100 % idempotent!)
        r_ref2 = cal_sync.run_sync(self.brain, provider=self.provider, execute=True, reformat=True, today=self.today)
        self.assertEqual(r_ref2["pushed_updated"], 0)


if __name__ == "__main__":
    unittest.main()
