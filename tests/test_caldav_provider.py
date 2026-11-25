"""Offline unit & integration tests for Atlas CalDAV Provider (Apple iCloud, Nextcloud, Fastmail)
and Side-by-Side Multi-Provider Sync (`CompositeCalendarProvider`: Google + Apple iCloud).

Uses an in-memory `FakeCalDAVServer` (0 real network calls) simulating Apple iCloud quirks:
- Partition redirect (`https://caldav.icloud.com/` -> `https://p42-caldav.icloud.com/...`) preserving method + auth
- 2-step `current-user-principal` -> `calendar-home-set` + `calendar-user-address-set` discovery
- `VTODO`-only Reminders collection filtering (never listed as an event calendar)
- Subscribed (`<subscribed/>`) & read-only privilege calendars mapped to `read-only` + `freebusy: false`
- `MKCALENDAR` subcalendar creation + graceful fallback when rejected (HTTP 403)
- RFC 5545 iCalendar parser/serializer: UTF-8 folding, quoted params, nested `VALARM` preservation,
  `X-APPLE-CONFERENCE` video link extraction, `X-ATLAS-*` identity & partial patch preservation
- `RRULE` expansion (`DAILY`/`WEEKLY`/`MONTHLY`/`YEARLY`), `EXDATE` exclusion, `RECURRENCE-ID` single-occurrence
  override & single-occurrence delete without destroying the master series
- CTag fast-path (0 REPORTs when unchanged), RFC 6578 `sync-collection` + `calendar-multiget` delta & 412 fallback
- Full 3-way bidirectional sync, `use-remote` conflict resolution, FreeBusy, Safety-Gated RSVP & invites
- Side-by-side Google Calendar + Apple iCloud discovery, sync, FreeBusy & CLI smoke tests
"""
from __future__ import annotations

import base64
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.parse
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape as xml_escape

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.normpath(os.path.join(TESTS_DIR, ".."))
TOOLS_DIR = os.path.join(REPO_ROOT, "core", "tools")
for _d in (TOOLS_DIR, TESTS_DIR):
    if _d not in sys.path:
        sys.path.insert(0, _d)

from atlas_lib import cal_sync, caldav_provider, gcal_provider  # noqa: E402
from test_cal_sync import FakeGCalAPI  # noqa: E402


def _write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


class FakeCalDAVServer:
    """In-memory RFC 4791 / RFC 6578 CalDAV server mimicking Apple iCloud partition routing."""

    def __init__(self, username: str = "user@icloud.com", password: str = "abcd-efgh-ijkl-mnop") -> None:
        self.username = username
        self.password = password
        self.partition_host = "https://p42-caldav.icloud.com"
        self.principal_path = "/10001/principal/"
        self.home_path = "/10001/calendars/"
        self.deny_mkcalendar = False
        self.fail_sync_token = False
        self.requests: list[tuple[str, str, str]] = []

        # calendars[cal_path] = metadata dict
        self.calendars: dict[str, dict] = {
            "/10001/calendars/home/": {
                "displayname": "Home",
                "color": "#1BADF8FF",
                "comps": ["VEVENT"],
                "writable": True,
                "subscribed": False,
                "ctag": "ctag-home-1",
                "sync_token": "https://p42-caldav.icloud.com/sync/home-1",
            },
            "/10001/calendars/family/": {
                "displayname": "Family",
                "color": "#A855F7FF",
                "comps": ["VEVENT"],
                "writable": True,
                "subscribed": False,
                "ctag": "ctag-fam-1",
                "sync_token": "https://p42-caldav.icloud.com/sync/fam-1",
            },
            "/10001/calendars/waste-bins/": {
                "displayname": "Abfall Collection",
                "color": "#10B981FF",
                "comps": ["VEVENT"],
                "writable": False,
                "subscribed": True,
                "ctag": "ctag-waste-1",
                "sync_token": "https://p42-caldav.icloud.com/sync/waste-1",
            },
            # Apple iCloud VTODO-only Reminders list (MUST be filtered out!)
            "/10001/calendars/reminders/": {
                "displayname": "Reminders",
                "color": "#F59E0BFF",
                "comps": ["VTODO"],
                "writable": True,
                "subscribed": False,
                "ctag": "ctag-rem-1",
                "sync_token": "https://p42-caldav.icloud.com/sync/rem-1",
            },
        }
        # items[cal_path][item_filename] = {"ics": str, "etag": str}
        self.items: dict[str, dict[str, dict]] = {k: {} for k in self.calendars}
        # sync_changes[cal_path] = list of (item_filename, status_code)
        self.sync_changes: dict[str, list[tuple[str, int]]] = {}
        self._seq = 10

    def _bump(self, cal_path: str) -> str:
        self._seq += 1
        cal = self.calendars[cal_path]
        cal["ctag"] = f"ctag-{self._seq}"
        cal["sync_token"] = f"{self.partition_host}/sync/{self._seq}"
        return f'"etag-{self._seq}"'

    def seed_ics(self, cal_path: str, filename: str, ics_text: str, record_delta: bool = False) -> str:
        etag = self._bump(cal_path)
        self.items.setdefault(cal_path, {})[filename] = {"ics": ics_text, "etag": etag}
        if record_delta:
            self.sync_changes.setdefault(cal_path, []).append((filename, 200))
        return etag

    def delete_remote_ics(self, cal_path: str, filename: str, record_delta: bool = True) -> None:
        self._bump(cal_path)
        self.items.setdefault(cal_path, {}).pop(filename, None)
        if record_delta:
            self.sync_changes.setdefault(cal_path, []).append((filename, 404))

    def _check_auth(self, headers: dict[str, str]) -> bool:
        auth = headers.get("Authorization", "")
        if not auth.startswith("Basic "):
            return False
        raw = base64.b64decode(auth[6:]).decode("utf-8", errors="replace")
        return raw == f"{self.username}:{self.password}"

    def __call__(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | None,
        timeout: float,
    ) -> tuple[int, dict[str, str], bytes]:
        body_txt = body.decode("utf-8", errors="replace") if body else ""
        self.requests.append((method, url, body_txt[:120]))

        if not self._check_auth(headers):
            return 401, {"www-authenticate": 'Basic realm="iCloud"'}, b"Unauthorized"

        parsed = urllib.parse.urlparse(url)
        # Simulate Apple iCloud partition redirect (`caldav.icloud.com` -> `p42-caldav.icloud.com`)
        final_url = url
        if parsed.hostname == "caldav.icloud.com":
            final_url = f"{self.partition_host}{parsed.path or '/'}"
        path = urllib.parse.urlparse(final_url).path or "/"
        depth = headers.get("Depth", "0")

        # 1. Entry root -> current-user-principal
        if method == "PROPFIND" and path == "/":
            xml = (
                '<?xml version="1.0" encoding="utf-8"?>'
                '<d:multistatus xmlns:d="DAV:">'
                "<d:response><d:href>/</d:href><d:propstat><d:prop>"
                f"<d:current-user-principal><d:href>{self.principal_path}</d:href></d:current-user-principal>"
                "</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
                "</d:multistatus>"
            ).encode("utf-8")
            return 207, {"content-type": "application/xml", "x-atlas-final-url": final_url}, xml

        # 2. Principal -> calendar-home-set + calendar-user-address-set
        if method == "PROPFIND" and path == self.principal_path:
            xml = (
                '<?xml version="1.0" encoding="utf-8"?>'
                '<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
                f"<d:response><d:href>{self.principal_path}</d:href><d:propstat><d:prop>"
                f"<c:calendar-home-set><d:href>{self.partition_host}{self.home_path}</d:href></c:calendar-home-set>"
                f"<c:calendar-user-address-set><d:href>mailto:{self.username}</d:href></c:calendar-user-address-set>"
                "</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
                "</d:multistatus>"
            ).encode("utf-8")
            return 207, {"content-type": "application/xml", "x-atlas-final-url": final_url}, xml

        # 3. Calendar home listing (`PROPFIND Depth: 1`)
        if method == "PROPFIND" and path == self.home_path and depth == "1":
            parts = [
                '<?xml version="1.0" encoding="utf-8"?>'
                '<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav" '
                'xmlns:cs="http://calendarserver.org/ns/" xmlns:a="http://apple.com/ns/ical/">',
                f"<d:response><d:href>{self.home_path}</d:href><d:propstat><d:prop>"
                "<d:resourcetype><d:collection/></d:resourcetype>"
                "</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>",
            ]
            for cpath, meta in self.calendars.items():
                rt_sub = "<cs:subscribed/>" if meta.get("subscribed") else ""
                comps_xml = "".join(f'<c:comp name="{c}"/>' for c in meta["comps"])
                priv_xml = (
                    "<d:privilege><d:read/></d:privilege><d:privilege><d:write/></d:privilege>"
                    if meta.get("writable")
                    else "<d:privilege><d:read/></d:privilege>"
                )
                parts.append(
                    f"<d:response><d:href>{cpath}</d:href><d:propstat><d:prop>"
                    f"<d:resourcetype><d:collection/><c:calendar/>{rt_sub}</d:resourcetype>"
                    f"<d:displayname>{xml_escape(meta['displayname'])}</d:displayname>"
                    f"<c:supported-calendar-component-set>{comps_xml}</c:supported-calendar-component-set>"
                    f"<a:calendar-color>{meta['color']}</a:calendar-color>"
                    f"<cs:getctag>{meta['ctag']}</cs:getctag>"
                    f"<d:sync-token>{meta['sync_token']}</d:sync-token>"
                    f"<d:current-user-privilege-set>{priv_xml}</d:current-user-privilege-set>"
                    "</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
                )
            parts.append("</d:multistatus>")
            return 207, {"content-type": "application/xml", "x-atlas-final-url": final_url}, "".join(parts).encode("utf-8")

        # 4. MKCALENDAR
        if method == "MKCALENDAR" and path.startswith(self.home_path):
            if self.deny_mkcalendar:
                return 403, {}, b"Forbidden"
            cpath = path if path.endswith("/") else path + "/"
            m_dn = re.search(r"<d:displayname>(.*?)</d:displayname>", body_txt)
            disp = m_dn.group(1) if m_dn else "Subcalendar"
            self._seq += 1
            self.calendars[cpath] = {
                "displayname": disp,
                "color": "#38BDF8FF",
                "comps": ["VEVENT"],
                "writable": True,
                "subscribed": False,
                "ctag": f"ctag-{self._seq}",
                "sync_token": f"{self.partition_host}/sync/{self._seq}",
            }
            self.items.setdefault(cpath, {})
            return 201, {"x-atlas-final-url": f"{self.partition_host}{cpath}"}, b""

        # Match calendar path
        cal_path = next((cp for cp in self.calendars if path == cp or path.startswith(cp)), None)
        if not cal_path:
            return 404, {}, b"Not Found"

        # 5. Collection PROPFIND Depth: 0 (sync-token & getctag)
        if method == "PROPFIND" and path == cal_path:
            meta = self.calendars[cal_path]
            xml = (
                '<?xml version="1.0" encoding="utf-8"?>'
                '<d:multistatus xmlns:d="DAV:" xmlns:cs="http://calendarserver.org/ns/">'
                f"<d:response><d:href>{cal_path}</d:href><d:propstat><d:prop>"
                f"<d:sync-token>{meta['sync_token']}</d:sync-token>"
                f"<cs:getctag>{meta['ctag']}</cs:getctag>"
                "</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
                "</d:multistatus>"
            ).encode("utf-8")
            return 207, {"content-type": "application/xml", "x-atlas-final-url": final_url}, xml

        # 6. REPORT (`calendar-query`, `sync-collection`, `calendar-multiget`)
        if method == "REPORT" and path == cal_path:
            if "sync-collection" in body_txt:
                if self.fail_sync_token:
                    return 412, {}, b"Invalid sync-token"
                changes = self.sync_changes.pop(cal_path, [])
                parts = ['<?xml version="1.0" encoding="utf-8"?><d:multistatus xmlns:d="DAV:">']
                for fname, st_code in changes:
                    href = f"{cal_path}{fname}"
                    if st_code == 404:
                        parts.append(f"<d:response><d:href>{href}</d:href><d:status>HTTP/1.1 404 Not Found</d:status></d:response>")
                    else:
                        etag = (self.items.get(cal_path, {}).get(fname) or {}).get("etag", '"e"')
                        parts.append(
                            f"<d:response><d:href>{href}</d:href><d:propstat><d:prop>"
                            f"<d:getetag>{xml_escape(etag)}</d:getetag>"
                            "</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
                        )
                parts.append(f"<d:sync-token>{self.calendars[cal_path]['sync_token']}</d:sync-token></d:multistatus>")
                return 207, {"content-type": "application/xml"}, "".join(parts).encode("utf-8")

            if "calendar-multiget" in body_txt:
                root = ET.fromstring(body_txt.encode("utf-8"))
                req_hrefs = [(el.text or "").strip() for el in root.iter() if el.tag.endswith("href")]
                parts = ['<?xml version="1.0" encoding="utf-8"?><d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">']
                for href in req_hrefs:
                    fname = href.rsplit("/", 1)[-1]
                    item = self.items.get(cal_path, {}).get(fname)
                    if item:
                        parts.append(
                            f"<d:response><d:href>{href}</d:href><d:propstat><d:prop>"
                            f"<d:getetag>{xml_escape(item['etag'])}</d:getetag>"
                            f"<c:calendar-data>{xml_escape(item['ics'])}</c:calendar-data>"
                            "</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
                        )
                parts.append("</d:multistatus>")
                return 207, {"content-type": "application/xml"}, "".join(parts).encode("utf-8")

            # Full calendar-query
            self.sync_changes.pop(cal_path, None)
            parts = ['<?xml version="1.0" encoding="utf-8"?><d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">']
            for fname, item in self.items.get(cal_path, {}).items():
                parts.append(
                    f"<d:response><d:href>{cal_path}{fname}</d:href><d:propstat><d:prop>"
                    f"<d:getetag>{xml_escape(item['etag'])}</d:getetag>"
                    f"<c:calendar-data>{xml_escape(item['ics'])}</c:calendar-data>"
                    "</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
                )
            parts.append("</d:multistatus>")
            return 207, {"content-type": "application/xml"}, "".join(parts).encode("utf-8")

        # 7. Item resource: GET / PUT / DELETE `.ics`
        fname = path[len(cal_path):].lstrip("/")
        if not fname:
            return 400, {}, b"Missing resource name"
        if method == "GET":
            item = self.items.get(cal_path, {}).get(fname)
            if not item:
                return 404, {}, b"Not Found"
            return 200, {"content-type": "text/calendar; charset=utf-8", "etag": item["etag"]}, item["ics"].encode("utf-8")
        if method == "PUT":
            etag = self._bump(cal_path)
            self.items.setdefault(cal_path, {})[fname] = {"ics": body_txt, "etag": etag}
            return 201, {"etag": etag}, b""
        if method == "DELETE":
            if fname in self.items.get(cal_path, {}):
                self._bump(cal_path)
                del self.items[cal_path][fname]
                return 204, {}, b""
            return 404, {}, b"Not Found"

        return 405, {}, b"Method Not Allowed"


class CalDAVProviderUnit(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="atlas-caldav-test-")
        self.brain = os.path.join(self.tmp, "brain")
        os.makedirs(os.path.join(self.brain, "calendar"), exist_ok=True)
        os.makedirs(os.path.join(self.brain, "_atlas", "cache"), exist_ok=True)
        os.makedirs(os.path.join(self.brain, "_secrets"), exist_ok=True)
        self.today = dt.date(2026, 11, 25)
        self.server = FakeCalDAVServer()
        caldav_provider.save_caldav_account(
            self.brain, "icloud", preset="icloud",
            username=self.server.username, password=self.server.password,
        )
        self.provider = caldav_provider.CalDAVProvider(self.brain, http_fn=self.server)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_icloud_discovery_reminders_filter_presets_and_redirect_security(self) -> None:
        # 1. Credential-domain redirect guard: allow iCloud partition, reject cross-domain!
        self.assertTrue(caldav_provider._same_credential_domain("caldav.icloud.com", "p42-caldav.icloud.com"))
        self.assertFalse(caldav_provider._same_credential_domain("caldav.icloud.com", "evil.example.org"))
        self.assertFalse(caldav_provider._same_credential_domain("dav.co.uk", "other.co.uk"))

        # 2. Preset URL resolution (Nextcloud appends /remote.php/dav once; apple alias -> icloud)
        pr1, u1 = caldav_provider.resolve_preset_url("apple")
        self.assertEqual((pr1, u1), ("icloud", "https://caldav.icloud.com/"))
        pr2, u2 = caldav_provider.resolve_preset_url("nextcloud", url="cloud.example.com")
        self.assertEqual((pr2, u2), ("nextcloud", "https://cloud.example.com/remote.php/dav"))

        # 3. Verify account discovers partition principal + calendar-home-set + email and filters VTODO Reminders!
        v = self.provider.verify_account("icloud")
        self.assertTrue(v["ok"])
        self.assertEqual(v["email"], "user@icloud.com")
        self.assertEqual(v["calendars_found"], 3)  # Home, Family, Abfall (NOT Reminders VTODO!)
        names = [c["summary"] for c in v["calendars"]]
        self.assertIn("Home", names)
        self.assertNotIn("Reminders", names)
        home_c = next(c for c in v["calendars"] if c["summary"] == "Home")
        waste_c = next(c for c in v["calendars"] if "Abfall" in c["summary"])
        self.assertTrue(home_c["primary"])
        self.assertEqual(home_c["background_color"], "#1BADF8")  # 8-digit Apple hex normalized to 6-digit
        self.assertEqual(waste_c["access_role"], "reader")  # subscribed calendar is read-only

        # Cached home URL saved into caldav.env with 0o600
        env = caldav_provider.load_caldav_env(self.brain)
        self.assertIn("p42-caldav.icloud.com/10001/calendars/", env["CALDAV_ICLOUD_HOME_URL"])

        # 4. `discover_calendars` handles MKCALENDAR 403 gracefully with warning (never crashes!)
        self.server.deny_mkcalendar = True
        d_res = cal_sync.discover_calendars(self.brain, provider=self.provider, execute=True)
        self.assertTrue(d_res["ok"])
        self.assertTrue(len(d_res["warnings"]) >= 1)
        # Waste read-only layer auto-gets `freebusy: false`
        waste_ly = next(ly for ly in d_res["layers"] if "abfall" in ly["label"].lower())
        self.assertEqual(waste_ly["mode"], "read-only")
        self.assertIs(waste_ly["freebusy"], False)
        self.assertEqual(waste_ly["provider"], "caldav")

    def test_ics_valarm_preservation_utf8_folding_rrule_exdate_and_overrides(self) -> None:
        # Complex Apple iCloud VEVENT with folded lines, nested VALARM DESCRIPTION, X-APPLE-CONFERENCE & RRULE + EXDATE + RECURRENCE-ID
        ics = (
            "BEGIN:VCALENDAR\r\n"
            "VERSION:2.0\r\n"
            "PRODID:-//Apple Inc.//macOS Calendar//EN\r\n"
            "BEGIN:VEVENT\r\n"
            "UID:weekly-sync-1@icloud.com\r\n"
            "DTSTART:20261126T090000Z\r\n"
            "DTEND:20261126T100000Z\r\n"
            "SUMMARY:Weekly Sync with Team\r\n"
            "DESCRIPTION:Agenda line 1\\nAgenda line 2\\, with comma\r\n"
            "X-APPLE-CONFERENCE:https://facetime.apple.com/join#v=1&p=demo\r\n"
            "RRULE:FREQ=WEEKLY;BYDAY=TH;COUNT=4\r\n"
            "EXDATE:20261203T090000Z\r\n"
            "BEGIN:VALARM\r\n"
            "ACTION:DISPLAY\r\n"
            "DESCRIPTION:This VALARM text MUST NOT overwrite event description!\r\n"
            "TRIGGER:-PT15M\r\n"
            "END:VALARM\r\n"
            "END:VEVENT\r\n"
            "BEGIN:VEVENT\r\n"
            "UID:weekly-sync-1@icloud.com\r\n"
            "RECURRENCE-ID:20261210T090000Z\r\n"
            "DTSTART:20261210T110000Z\r\n"
            "DTEND:20261210T120000Z\r\n"
            "SUMMARY:Moved Weekly Sync\r\n"
            "DESCRIPTION:Rescheduled occurrence\r\n"
            "END:VEVENT\r\n"
            "END:VCALENDAR\r\n"
        )
        t_min = dt.datetime(2026, 11, 20, 0, 0, tzinfo=dt.timezone.utc)
        t_max = dt.datetime(2026, 12, 31, 23, 59, tzinfo=dt.timezone.utc)
        evs = caldav_provider.expand_ics_to_provider_events(
            ics, "weekly-sync-1.ics", '"e1"', t_min, t_max, single_events=True, account_email="user@icloud.com"
        )
        # 4 occurrences minus 1 EXDATE (Dec 3) = 3 instances (Nov 26, Dec 10 overridden, Dec 17)
        self.assertEqual(len(evs), 3)
        self.assertEqual(evs[0]["id"], "weekly-sync-1.ics#20261126T090000Z")
        self.assertEqual(evs[0]["recurringEventId"], "weekly-sync-1.ics")
        self.assertIn("Agenda line 1\nAgenda line 2, with comma", evs[0]["description"])
        self.assertNotIn("VALARM", evs[0]["description"])
        self.assertIn("facetime.apple.com", evs[0]["hangoutLink"])
        # Dec 10 overridden to 11:00Z + new summary
        dec10 = next(e for e in evs if e["id"].endswith("#20261210T090000Z"))
        self.assertEqual(dec10["summary"], "Moved Weekly Sync")
        self.assertIn("2026-12-10T11:00:00", dec10["start"]["dateTime"])

        # Partial patch preserves VALARM & existing SUMMARY/DTSTART when only X-ATLAS-UID is injected!
        patched = caldav_provider.build_ics_payload(
            {"extendedProperties": {"private": {"atlas_uid": "a1b2c3d4", "atlas_layer": "calendar/README.md"}}},
            existing_ics=ics,
        )
        self.assertIn("X-ATLAS-UID:a1b2c3d4", patched)
        self.assertIn("BEGIN:VALARM", patched)
        self.assertIn("SUMMARY:Weekly Sync with Team", patched)

        # Single-occurrence override & EXDATE cancellation on server
        self.server.seed_ics("/10001/calendars/home/", "weekly-sync-1.ics", ics)
        self.provider.upsert_event(
            "icloud", "/10001/calendars/home/",
            {"summary": "✓ Weekly Sync with Team", "extendedProperties": {"private": {"atlas_status": "done"}}},
            event_id="weekly-sync-1.ics#20261126T090000Z",
        )
        # Master still has original title; Nov 26 instance is marked done!
        pulled = self.provider.pull_events(
            "icloud", "/10001/calendars/home/",
            time_min="2026-11-20T00:00:00Z", time_max="2026-12-31T23:59:59Z",
        )
        nov26 = next(e for e in pulled["events"] if e["id"].endswith("#20261126T090000Z"))
        dec17 = next(e for e in pulled["events"] if e["id"].endswith("#20261217T090000Z"))
        self.assertTrue(nov26["summary"].startswith("✓ "))
        self.assertEqual(dec17["summary"], "Weekly Sync with Team")

        # Deleting a single occurrence adds EXDATE instead of deleting the entire `.ics` file!
        self.provider.delete_event("icloud", "/10001/calendars/home/", "weekly-sync-1.ics#20261217T090000Z")
        self.assertIn("weekly-sync-1.ics", self.server.items["/10001/calendars/home/"])
        pulled2 = self.provider.pull_events(
            "icloud", "/10001/calendars/home/",
            time_min="2026-11-20T00:00:00Z", time_max="2026-12-31T23:59:59Z",
        )
        self.assertEqual(len(pulled2["events"]), 2)

        # UTF-8 folding preserves multi-byte characters cleanly across fold boundaries
        long_summary = "🚴 " + ("Übersicht-Änderung-Straße " * 6)
        folded_ics = caldav_provider.build_ics_payload({
            "summary": long_summary,
            "start": {"date": "2026-11-28"},
            "end": {"date": "2026-11-29"},
        })
        for raw_ln in folded_ics.split("\r\n"):
            self.assertLessEqual(len(raw_ln.encode("utf-8")), 75)
        reparsed = caldav_provider.parse_ics_vevents(folded_ics)
        self.assertEqual(caldav_provider._ics_unescape(reparsed[0]["props"]["SUMMARY"][1]), long_summary)

    def test_full_caldav_3way_sync_ctag_fastpath_delta_rsvp_and_freebusy(self) -> None:
        # Discover & auto-create `Atlas Coach` + `Atlas Tasks & Fristen` via MKCALENDAR
        disc = cal_sync.discover_calendars(self.brain, provider=self.provider, execute=True)
        self.assertTrue(disc["ok"])
        cfg = cal_sync.load_sync_config(self.brain)
        readme_ly = next(ly for ly in cfg["layers"] if ly["file"] == "README.md")
        wko_cid = readme_ly["subcalendars"]["workout"]
        tasks_cid = readme_ly["subcalendars"]["tasks"]
        self.assertTrue(wko_cid.startswith("/10001/calendars/"))
        self.assertTrue(tasks_cid.startswith("/10001/calendars/"))

        # Seed remote invite in Home + opaque all-day waste event in Abfall (must NOT block FreeBusy!)
        self.server.seed_ics(
            "/10001/calendars/home/", "invite-1.ics",
            "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\n"
            "UID:invite-1@external.example\r\n"
            "DTSTART:20261126T130000Z\r\nDTEND:20261126T140000Z\r\n"
            "SUMMARY:Design Review\r\n"
            "ORGANIZER:mailto:lead@external.example\r\n"
            "ATTENDEE;CN=\"Test User\";PARTSTAT=NEEDS-ACTION:mailto:user@icloud.com\r\n"
            "END:VEVENT\r\nEND:VCALENDAR\r\n",
        )
        self.server.seed_ics(
            "/10001/calendars/waste-bins/", "waste-1.ics",
            "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\n"
            "UID:waste-1@city.example\r\n"
            "DTSTART;VALUE=DATE:20261126\r\nDTEND;VALUE=DATE:20261127\r\n"
            "SUMMARY:Paper Bin Pickup\r\nTRANSP:OPAQUE\r\n"
            "END:VEVENT\r\nEND:VCALENDAR\r\n",
        )
        _write(
            os.path.join(self.brain, "calendar", "README.md"),
            "---\nsummary: Cal\ntags: [atlas, calendar]\n---\n# Calendar\n\n## Timeline\n\n"
            "- [ ] 2026-11-26 17:30 [workout:cycling] Sweet-Spot Ride (60m | 15m WU, 3x10m@260W, 15m CD)\n"
            "- [ ] 2026-11-27 [todo] Pay electricity bill\n",
        )

        r1 = cal_sync.run_sync(self.brain, provider=self.provider, execute=True, today=self.today)
        self.assertTrue(r1["ok"])
        self.assertEqual(r1["pulled_new"], 2)  # Design Review + Paper Bin (RO)
        self.assertEqual(r1["pushed_new"], 2)  # Sweet-Spot Ride -> Atlas Coach + Pay electricity bill -> Atlas Tasks

        # Verify workout landed in Atlas Coach with X-ATLAS-UID & todo in Atlas Tasks with TRANSP:TRANSPARENT
        wko_items = list(self.server.items[wko_cid].values())
        task_items = list(self.server.items[tasks_cid].values())
        self.assertEqual(len(wko_items), 1)
        self.assertIn("X-ATLAS-UID:", wko_items[0]["ics"])
        self.assertIn("Sweet-Spot Ride", wko_items[0]["ics"])
        self.assertEqual(len(task_items), 1)
        self.assertIn("TRANSP:TRANSPARENT", task_items[0]["ics"])

        # 2. CTag / sync-token fast path: after post-push cursor catch-up, unchanged server -> ZERO REPORT calls!
        cal_sync.run_sync(self.brain, provider=self.provider, execute=True, pull_only=True, today=self.today)
        req_count_before = len(self.server.requests)
        r_noop = cal_sync.run_sync(self.brain, provider=self.provider, execute=True, pull_only=True, today=self.today)
        self.assertEqual(r_noop["pulled_new"] + r_noop["pulled_updated"] + r_noop["pulled_cancelled"], 0)
        new_reqs = self.server.requests[req_count_before:]
        self.assertFalse(any(m == "REPORT" for m, _u, _b in new_reqs), f"Expected 0 REPORTs on unchanged CTag, got {new_reqs}")

        # 3. Incremental RFC 6578 `sync-collection` + `calendar-multiget` delta (remote edit + remote delete)
        self.server.seed_ics(
            "/10001/calendars/home/", "added-on-iphone.ics",
            "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\n"
            "UID:iphone-1@icloud.com\r\n"
            "DTSTART:20261128T100000Z\r\nDTEND:20261128T110000Z\r\n"
            "SUMMARY:Coffee with Sam\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n",
            record_delta=True,
        )
        r_delta = cal_sync.run_sync(self.brain, provider=self.provider, execute=True, today=self.today)
        self.assertEqual(r_delta["pulled_new"], 1)
        with open(os.path.join(self.brain, "calendar", "README.md"), encoding="utf-8") as fh:
            self.assertIn("Coffee with Sam", fh.read())

        # Remote delete via sync-collection 404 -> marks Atlas line `[-]`
        self.server.delete_remote_ics("/10001/calendars/home/", "added-on-iphone.ics", record_delta=True)
        r_del = cal_sync.run_sync(self.brain, provider=self.provider, execute=True, today=self.today)
        self.assertEqual(r_del["pulled_cancelled"], 1)
        with open(os.path.join(self.brain, "calendar", "README.md"), encoding="utf-8") as fh:
            self.assertIn("- [-] 2026-11-28", fh.read())

        # Expired sync-token (HTTP 412) transparently falls back to full `calendar-query`
        self.server.seed_ics(
            "/10001/calendars/home/", "after-412.ics",
            "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\n"
            "UID:after-412@icloud.com\r\nDTSTART:20261129T150000Z\r\nDTEND:20261129T160000Z\r\n"
            "SUMMARY:Recovered Event\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n",
            record_delta=False,
        )
        self.server.fail_sync_token = True
        r_412 = cal_sync.run_sync(self.brain, provider=self.provider, execute=True, today=self.today)
        self.server.fail_sync_token = False
        self.assertTrue(r_412["ok"])
        self.assertEqual(r_412["pulled_new"], 1)

        # 4. Safety-Gated RSVP on CalDAV invite (`declined` -> PARTSTAT=DECLINED + Atlas `[-]`)
        rsvp_prev = cal_sync.rsvp_event_guarded(self.brain, "Design Review", "declined", execute=False, provider=self.provider)
        self.assertTrue(rsvp_prev["dry_run"])
        rsvp_ok = cal_sync.rsvp_event_guarded(self.brain, "Design Review", "declined", execute=True, provider=self.provider)
        self.assertTrue(rsvp_ok["applied"])
        self.assertIn("PARTSTAT=DECLINED", self.server.items["/10001/calendars/home/"]["invite-1.ics"]["ics"])

        # 5. CalDAV `--meet` guard refuses Google Meet cleanly; `--rrule` creates recurring VEVENT!
        bad_meet = cal_sync.create_event_guarded(
            self.brain, title="Video Call", date_iso="2026-11-30", time_spec="10:00-11:00",
            account_id="icloud", add_meet=True, execute=False, provider=self.provider,
        )
        self.assertFalse(bad_meet["ok"])
        self.assertIn("CalDAV", bad_meet["error"])

        rec_ok = cal_sync.create_event_guarded(
            self.brain, title="Trash Out", date_iso="2026-11-30", time_spec="19:00-19:15",
            account_id="icloud", recurrence=["FREQ=WEEKLY;BYDAY=MO"], execute=True, provider=self.provider,
        )
        self.assertTrue(rec_ok["applied"])
        created_ics = self.server.items["/10001/calendars/home/"][rec_ok["gcal_event_id"]]["ics"]
        self.assertIn("RRULE:FREQ=WEEKLY;BYDAY=MO", created_ics)

        # 6. FreeBusy excludes `Abfall Collection` (`freebusy: false`), so 2026-11-26 is NOT fully booked!
        fb = cal_sync.compute_freebusy(
            self.brain, days=1, start_date_iso="2026-11-26",
            work_start_hour=8, work_end_hour=20, min_slot_minutes=30,
            provider=self.provider, use_remote=True,
        )
        self.assertTrue(fb["ok"])
        self.assertTrue(len(fb["days"][0]["free"]) >= 1)

    def test_side_by_side_google_plus_icloud_composite_and_cli_smoke(self) -> None:
        # Configure BOTH Google (`personal`) AND Apple iCloud (`icloud`) simultaneously!
        future_exp = str(int(time.time()) + 3600)
        gcal_provider.save_gcal_env(self.brain, {
            "GCAL_CLIENT_ID": "cid.apps.googleusercontent.com",
            "GCAL_CLIENT_SECRET": "csec",
            "GCAL_ACCOUNTS": "personal",
            "GCAL_PERSONAL_EMAIL": "user@example.com",
            "GCAL_PERSONAL_ACCESS_TOKEN": "tok_personal",
            "GCAL_PERSONAL_REFRESH_TOKEN": "ref_personal",
            "GCAL_PERSONAL_EXPIRES_AT": future_exp,
        })
        fake_gcal = FakeGCalAPI()
        g_prov = gcal_provider.GoogleCalendarProvider(self.brain, http_fn=fake_gcal)
        comp = cal_sync.CompositeCalendarProvider(self.brain, gcal=g_prov, caldav=self.provider)

        # Discover across BOTH providers in one pass
        disc = cal_sync.discover_calendars(self.brain, provider=comp, execute=True)
        self.assertTrue(disc["ok"])
        self.assertIn("personal", disc["accounts"])
        self.assertIn("icloud", disc["accounts"])
        cfg = cal_sync.load_sync_config(self.brain)
        self.assertEqual(cfg["provider"], "multi")
        g_ly = next(ly for ly in cfg["layers"] if ly["account"] == "personal" and ly["file"] == "README.md")
        i_ly = next(ly for ly in cfg["layers"] if ly["account"] == "icloud" and ly["mode"] == "bidirectional")
        self.assertEqual(g_ly["provider"], "gcal")
        self.assertEqual(i_ly["provider"], "caldav")
        self.assertNotEqual(i_ly["file"], "README.md")  # Never clobbers Google's README.md!

        # Seed 1 event in Google (09:00-10:00) and 1 event in Apple iCloud (14:00-15:00 local)
        fake_gcal.seed_event("personal", "user@example.com", {
            "summary": "Google Standup",
            "start": {"dateTime": "2026-11-26T09:00:00+01:00"},
            "end": {"dateTime": "2026-11-26T10:00:00+01:00"},
        })
        self.server.seed_ics(
            "/10001/calendars/home/", "apple-ev.ics",
            "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\n"
            "UID:apple-ev@icloud.com\r\n"
            "DTSTART:20261126T130000Z\r\nDTEND:20261126T140000Z\r\n"
            "SUMMARY:Apple Dentist\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n",
        )

        sync_res = cal_sync.run_sync(self.brain, provider=comp, execute=True, today=self.today)
        self.assertTrue(sync_res["ok"])
        self.assertEqual(sync_res["pulled_new"], 2)
        with open(os.path.join(self.brain, "calendar", "README.md"), encoding="utf-8") as fh:
            self.assertIn("Google Standup", fh.read())
        with open(os.path.join(self.brain, "calendar", i_ly["file"]), encoding="utf-8") as fh:
            self.assertIn("Apple Dentist", fh.read())

        # Consolidated FreeBusy checks BOTH Google AND Apple iCloud!
        fb = cal_sync.compute_freebusy(
            self.brain, days=1, start_date_iso="2026-11-26",
            work_start_hour=8, work_end_hour=18, min_slot_minutes=30,
            provider=comp, use_remote=True,
        )
        self.assertIn("personal", fb["remote_accounts_checked"])
        self.assertIn("icloud", fb["remote_accounts_checked"])
        self.assertGreaterEqual(len(fb["days"][0]["busy"]), 2)

        # CLI smoke test: `cal status --json` lists BOTH providers, `cal auth --provider fastmail --password-stdin --skip-verify` works securely!
        atlas_py = os.path.join(TOOLS_DIR, "atlas.py")
        p_st = subprocess.run(
            [sys.executable, "-I", atlas_py, "--root", self.tmp, "cal", "status", "--json"],
            capture_output=True, text=True, check=True,
        )
        st = json.loads(p_st.stdout)
        provs = {(a["id"], a["provider"]) for a in st["accounts"]}
        self.assertIn(("personal", "gcal"), provs)
        self.assertIn(("icloud", "caldav"), provs)

        p_fm = subprocess.run(
            [
                sys.executable, "-I", atlas_py, "--root", self.tmp, "cal", "auth",
                "--provider", "fastmail", "--account", "fastmail",
                "--username", "alice@fastmail.com", "--password-stdin", "--skip-verify",
            ],
            input="super-secret-app-pw\n",
            capture_output=True, text=True, check=True,
        )
        self.assertNotIn("super-secret-app-pw", p_fm.stdout)
        fm_env = caldav_provider.load_caldav_env(self.brain)
        self.assertEqual(fm_env["CALDAV_FASTMAIL_PASSWORD"], "super-secret-app-pw")


if __name__ == "__main__":
    unittest.main()
