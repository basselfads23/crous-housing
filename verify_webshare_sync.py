"""
verify_webshare_sync.py — run with: venv/bin/python3 verify_webshare_sync.py

Covers the 2026-10-04 proxy-pool changes:
- escalating benches: the same proxy benched again without a success in between stays out
  1, 2, 4, then 7 days (cap); any success resets it to 1 day; bench_proxy() benches by hand
- load_proxies() = proxies.txt (hand-managed, Oxylabs) + proxies_webshare.txt (synced)
- sync_webshare_proxies(): writes only valid proxies; off unless WEBSHARE_SYNC_ENABLED;
  due every 12 h, never more than once an hour; a 407 requests an early sync; on ANY
  failure (API error, empty list, list shrinking below half) the current file is kept
- replay of the real 2026-09-28 event: Webshare replaces a proxy -> the old one leaves the
  pool and its quarantine entry is dropped, the replacement joins, and a proxy benched by
  hand for a CROUS block stays benched even though the sync lists it again
- crous_watcher wiring: Telegram only on a proxy's FIRST bench; a 407 requests a sync; the
  12 h sync reports changes and the 3rd failure in a row, and never breaks polling

No network, no real Telegram, no real state files: proxy_manager's paths point at a temp
dir and the Webshare API call is replaced by a fake.
"""
import logging
logging.getLogger().addHandler(logging.NullHandler())  # keep test noise out of the live watcher.log

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import proxy_manager as pm
import crous_watcher as cw

DAY = 86400
CRED = "pmuser:SECRETPW"


def url(i):
    return f"http://{CRED}@10.0.0.{i}:80{i:02d}"


def key(i):
    return f"10.0.0.{i}:80{i:02d}"


def api_row(i, valid=True):
    return {"proxy_address": f"10.0.0.{i}", "port": 8000 + i, "username": "pmuser",
            "password": "SECRETPW", "valid": valid}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._orig = {n: getattr(pm, n) for n in ("QUARANTINE_FILE", "PROXIES_FILE", "EXHAUSTED_GROUPS_FILE", "STATE_FILE")}
        pm.QUARANTINE_FILE = self.tmp / ".proxy_quarantine.json"
        pm.EXHAUSTED_GROUPS_FILE = self.tmp / ".exhausted_proxy_groups.json"
        pm.STATE_FILE = self.tmp / ".proxy_index"
        pm.PROXIES_FILE = self.tmp / "proxies.txt"
        pm.PROXIES_FILE.write_text("dc.oxylabs.io:8001:oxuser:OXPW\n")
        self.env = mock.patch.dict(os.environ, {"WEBSHARE_SYNC_ENABLED": "true", "WEBSHARE_API_KEY": "KEY",
                                                "WEBSHARE_DATACENTER_PLAN_ID": "14397141"})
        self.env.start()
        for var in ("CROUS_PROXY", "HTTPS_PROXY", "HTTP_PROXY"):
            os.environ.pop(var, None)
        self.t = 1_800_000_000.0
        self.clock = mock.patch.object(pm.time, "time", side_effect=lambda: self.t)
        self.clock.start()

    def tearDown(self):
        self.clock.stop()
        self.env.stop()
        for n, v in self._orig.items():
            setattr(pm, n, v)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_synced(self, ids):
        (self.tmp / "proxies_webshare.txt").write_text("".join(f"10.0.0.{i}:80{i:02d}:pmuser:SECRETPW\n" for i in ids))

    def strike(self, i, n=1, reason="timed out"):
        r = None
        for _ in range(n):
            r = pm.record_proxy_strike(url(i), reason)
        return r


class TestEscalatingBench(Base):
    def setUp(self):
        super().setUp()
        self.write_synced(range(1, 21))  # 20 + 1 oxylabs: cap = 5 benched

    def test_durations_escalate_and_cap(self):
        r = self.strike(1, 3)
        self.assertEqual((r["bench_count"], r["bench_seconds"]), (1, DAY))
        expected = [2 * DAY, 4 * DAY, 7 * DAY, 7 * DAY]
        for n, dur in enumerate(expected, start=2):
            prev = pm.bench_duration_seconds(n - 1)
            self.t += prev - 60
            self.assertIn(key(1), pm.get_quarantined_proxies(), f"still benched just before bench #{n-1} ends")
            self.t += 120
            self.assertNotIn(key(1), pm.get_quarantined_proxies(), f"released after bench #{n-1}")
            r = self.strike(1)  # probation: ONE failure re-benches
            self.assertTrue(r["quarantined_now"])
            self.assertEqual((r["bench_count"], r["bench_seconds"]), (n, dur))

    def test_success_resets_to_one_day(self):
        self.strike(1, 3)
        self.t += DAY + 1
        pm.get_quarantined_proxies()
        self.strike(1)                       # bench #2 (2 days)
        self.t += 2 * DAY + 1
        pm.get_quarantined_proxies()
        pm.record_proxy_success(url(1))      # it works again
        self.assertNotIn(key(1), json.loads(pm.QUARANTINE_FILE.read_text()))
        r = self.strike(1, 3)
        self.assertEqual((r["bench_count"], r["bench_seconds"]), (1, DAY), "a success wipes the history")

    def test_entries_written_before_escalation_count_as_first_bench(self):
        pm._write_quarantine_state({key(1): {"strikes": 3, "quarantined_at": self.t, "last_reason": "407"}})
        self.t += DAY - 60
        self.assertIn(key(1), pm.get_quarantined_proxies())
        self.t += 120
        self.assertNotIn(key(1), pm.get_quarantined_proxies())

    def test_manual_bench_seven_days_then_probation(self):
        pm.bench_proxy(key(2), "CROUS-side block", bench_count=4)
        self.assertIn(key(2), pm.get_quarantined_proxies())
        rotation = {pm.proxy_key(pm.get_current_proxy(rotate=True)) for _ in range(60)}
        self.assertNotIn(key(2), rotation, "a benched proxy is never handed out")
        self.t += 7 * DAY - 60
        self.assertIn(key(2), pm.get_quarantined_proxies())
        self.t += 120
        self.assertNotIn(key(2), pm.get_quarantined_proxies())
        r = self.strike(2)
        self.assertEqual((r["quarantined_now"], r["bench_seconds"]), (True, 7 * DAY))

    def test_quarantine_state_has_no_credentials(self):
        self.strike(1, 3)
        pm.bench_proxy(key(2), "x", 4)
        self.assertNotIn("SECRETPW", pm.QUARANTINE_FILE.read_text())


class TestPoolFiles(Base):
    def test_load_proxies_merges_both_files(self):
        self.write_synced([1, 2])
        keys = [pm.proxy_key(p) for p in pm.load_proxies()]
        self.assertEqual(keys, ["dc.oxylabs.io:8001", key(1), key(2)])

    def test_synced_file_is_next_to_proxies_file(self):
        self.assertEqual(pm._webshare_proxies_file(), self.tmp / "proxies_webshare.txt")


class TestSync(Base):
    def fake(self, rows):
        return lambda api_key, plan_id: rows

    def test_disabled_by_default(self):
        os.environ.pop("WEBSHARE_SYNC_ENABLED")
        self.assertFalse(pm.webshare_sync_due())

    def test_first_sync_writes_valid_proxies_only(self):
        self.assertTrue(pm.webshare_sync_due())
        res = pm.sync_webshare_proxies(fetch=self.fake([api_row(3), api_row(1), api_row(2, valid=False)]))
        self.assertTrue(res["ok"] and res["first_sync"])
        self.assertEqual(res["count"], 2)
        text = (self.tmp / "proxies_webshare.txt").read_text()
        self.assertTrue(text.startswith("# AUTO-GENERATED"))
        self.assertEqual([l for l in text.splitlines() if not l.startswith("#")],
                         ["10.0.0.1:8001:pmuser:SECRETPW", "10.0.0.3:8003:pmuser:SECRETPW"])
        self.assertNotIn("SECRETPW", pm._webshare_sync_state_file().read_text())

    def test_schedule_12h_and_min_gap(self):
        pm.sync_webshare_proxies(fetch=self.fake([api_row(1)]))
        self.t += 12 * 3600 - 60
        self.assertFalse(pm.webshare_sync_due())
        self.t += 120
        self.assertTrue(pm.webshare_sync_due())

    def test_407_request_triggers_early_sync_after_min_gap(self):
        pm.sync_webshare_proxies(fetch=self.fake([api_row(1)]))
        pm.request_webshare_sync("407 from x")
        self.t += 3600 - 60
        self.assertFalse(pm.webshare_sync_due(), "never more than one API call per hour")
        self.t += 120
        self.assertTrue(pm.webshare_sync_due())
        pm.sync_webshare_proxies(fetch=self.fake([api_row(1)]))
        self.t += 3600 + 1
        self.assertFalse(pm.webshare_sync_due(), "request cleared by the successful sync")

    def test_api_failure_keeps_file_and_retries_hourly(self):
        self.write_synced([1, 2])
        before = (self.tmp / "proxies_webshare.txt").read_text()
        def boom(api_key, plan_id):
            raise OSError("connection refused")
        res = pm.sync_webshare_proxies(fetch=boom)
        self.assertFalse(res["ok"])
        self.assertEqual(res["consecutive_failures"], 1)
        self.assertEqual((self.tmp / "proxies_webshare.txt").read_text(), before)
        self.t += 3600 + 1
        self.assertTrue(pm.webshare_sync_due(), "after a failure: retry after 1 h, not 12 h")
        self.assertEqual(pm.sync_webshare_proxies(fetch=boom)["consecutive_failures"], 2)

    def test_refuses_empty_or_shrunken_list(self):
        self.write_synced(range(1, 11))
        before = (self.tmp / "proxies_webshare.txt").read_text()
        for rows in ([], [api_row(i, valid=False) for i in range(1, 11)], [api_row(i) for i in range(1, 5)]):
            res = pm.sync_webshare_proxies(fetch=self.fake(rows))
            self.assertFalse(res["ok"], rows[:1])
            self.assertEqual((self.tmp / "proxies_webshare.txt").read_text(), before)
        self.assertTrue(pm.sync_webshare_proxies(fetch=self.fake([api_row(i) for i in range(1, 6)]))["ok"],
                        "exactly half is accepted")

    def test_missing_config_fails_without_raising(self):
        os.environ.pop("WEBSHARE_DATACENTER_PLAN_ID")
        res = pm.sync_webshare_proxies(fetch=self.fake([api_row(1)]))
        self.assertFalse(res["ok"])
        self.assertIn("missing", res["error"])

    def test_replay_of_the_2026_09_28_replacement(self):
        # before: 1..5 in the list; 3 is CROUS-blocked (benched by hand); Webshare replaces 5 with 6
        self.write_synced([1, 2, 3, 4, 5])
        pm.bench_proxy(key(3), "CROUS-side block", bench_count=4)
        pm._write_quarantine_state({**json.loads(pm.QUARANTINE_FILE.read_text()),
                                    key(5): {"strikes": 2, "quarantined_at": None, "last_reason": "407"}})
        res = pm.sync_webshare_proxies(fetch=self.fake([api_row(i) for i in (1, 2, 3, 4, 6)]))
        self.assertEqual((res["removed"], res["added"]), ([key(5)], [key(6)]))
        pool = {pm.proxy_key(p) for p in pm.load_proxies()}
        self.assertNotIn(key(5), pool)
        self.assertIn(key(6), pool)
        q = json.loads(pm.QUARANTINE_FILE.read_text())
        self.assertNotIn(key(5), q, "quarantine entry of a removed proxy is dropped")
        self.assertIn(key(3), pm.get_quarantined_proxies(), "CROUS-blocked proxy stays benched although listed")
        rotation = {pm.proxy_key(pm.get_current_proxy(rotate=True)) for _ in range(30)}
        self.assertEqual(rotation, {"dc.oxylabs.io:8001", key(1), key(2), key(4), key(6)})

    def test_fetch_follows_pagination_with_token_and_plan(self):
        seen = []
        pages = {
            "p1": {"results": [api_row(1)], "next": "https://proxy.webshare.io/api/v2/proxy/list/?page=2&plan_id=14397141"},
            "p2": {"results": [api_row(2)], "next": None},
        }
        class Resp:
            def __init__(self, d): self.d = json.dumps(d).encode()
            def read(self): return self.d
            def __enter__(self): return self
            def __exit__(self, *a): return False
        def fake_urlopen(req, timeout=None):
            seen.append((req.full_url, req.get_header("Authorization")))
            return Resp(pages["p1" if len(seen) == 1 else "p2"])
        with mock.patch.object(pm.urllib.request, "urlopen", side_effect=fake_urlopen):
            rows = pm._fetch_webshare_plan_proxies("KEY", "14397141")
        self.assertEqual([r["proxy_address"] for r in rows], ["10.0.0.1", "10.0.0.2"])
        self.assertIn("plan_id=14397141", seen[0][0])
        self.assertIn("mode=direct", seen[0][0])
        self.assertEqual({a for _, a in seen}, {"Token KEY"})


class TestWatcherWiring(Base):
    def setUp(self):
        super().setUp()
        self.write_synced(range(1, 21))
        self.sent = []
        self.p = mock.patch.object(cw, "send_telegram_message", side_effect=lambda t, *a, **k: self.sent.append(t))
        self.p.start()

    def tearDown(self):
        self.p.stop()
        super().tearDown()

    def fail_then_succeed(self, i, reason="timed out"):
        cw._settle_proxy_health(url(19), [(url(i), reason)])

    def test_only_first_bench_alerts(self):
        for _ in range(3):
            self.fail_then_succeed(1)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("pendant 24h", self.sent[0])
        self.t += DAY + 1
        pm.get_quarantined_proxies()
        with self.assertLogs("crous_watcher", level="WARNING") as logs:
            self.fail_then_succeed(1)  # probation failure -> bench #2, 2 days
        self.assertEqual(len(self.sent), 1, "re-bench is logged, not sent")
        self.assertTrue(any("2 jours" in l and "bench #2" in l for l in logs.output), logs.output)

    def test_407_requests_a_sync(self):
        self.fail_then_succeed(5, "<urlopen error Tunnel connection failed: 407 Proxy Authentication Required>")
        self.assertTrue(pm.get_webshare_sync_state().get("requested_at"))
        self.fail_then_succeed(6, "timed out")
        self.assertIn("407 from", pm.get_webshare_sync_state()["requested_reason"])

    def test_sync_wrapper_messages(self):
        rows = [api_row(i) for i in range(1, 21)]
        with mock.patch.object(pm, "_fetch_webshare_plan_proxies", side_effect=lambda k, p: rows):
            (self.tmp / "proxies_webshare.txt").unlink()
            cw._maybe_sync_webshare_proxies()                      # first sync
            self.assertEqual(len(self.sent), 1)
            self.assertIn("Première synchronisation", self.sent[0])
            self.t += 12 * 3600 + 1
            cw._maybe_sync_webshare_proxies()                      # unchanged -> silent
            self.assertEqual(len(self.sent), 1)
            rows = [api_row(i) for i in list(range(1, 20)) + [30]]
            self.t += 12 * 3600 + 1
            cw._maybe_sync_webshare_proxies()                      # Webshare replaced 20 with 30
            self.assertEqual(len(self.sent), 2)
            self.assertIn(key(20), self.sent[1])
            self.assertIn(key(30), self.sent[1])
        def boom(k, p):
            raise OSError("down")
        self.t += 12 * 3600 - 3600  # next scheduled sync is due 12 h after the last success
        with mock.patch.object(pm, "_fetch_webshare_plan_proxies", side_effect=boom):
            for n in range(1, 5):
                self.t += 3600 + 1
                cw._maybe_sync_webshare_proxies()
                self.assertEqual(len(self.sent), 2 + (1 if n >= 3 else 0), f"failure #{n}")
        self.assertIn("3 fois de suite", self.sent[-1])

    def test_sync_wrapper_never_raises(self):
        with mock.patch.object(pm, "sync_webshare_proxies", side_effect=RuntimeError("disk full")):
            cw._maybe_sync_webshare_proxies()  # must not raise

    def test_status_line(self):
        pm.sync_webshare_proxies(fetch=lambda k, p: [api_row(i) for i in range(1, 21)])
        pm.bench_proxy(key(3), "x", 4)
        self.t += 2 * 3600
        with mock.patch.object(cw.time, "time", return_value=self.t):
            line = cw._format_proxy_maintenance_line()
        self.assertIn("synchronisée il y a 2h", line)
        self.assertIn("1 proxy(s) en quarantaine", line)
        self.assertEqual(line.count("*") % 2, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
