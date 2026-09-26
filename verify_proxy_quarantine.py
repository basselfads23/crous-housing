"""
verify_proxy_quarantine.py — run with: python3 verify_proxy_quarantine.py

Covers the scouter's per-proxy quarantine (proxy_manager.record_proxy_strike & friends)
and its wiring in crous_watcher.fetch_all_crous_listings / _settle_proxy_health.

Safe to run while the service is live: it monkeypatches every proxy_manager state path
to a temp dir, replaces Telegram/activity_logger with mocks, fails loudly if anything
tries to open a real network connection, and keeps crous_watcher's logging out of the
live watcher.log. It does not touch real state files.
"""
import logging
# crous_watcher calls logging.basicConfig() at import, which attaches a FileHandler on the
# live watcher.log. basicConfig is a no-op when the root logger already has a handler, so
# attach one first to keep test noise out of the production log.
logging.getLogger().addHandler(logging.NullHandler())

import sys, os, json, shutil, tempfile, unittest
from unittest import mock
from pathlib import Path
from urllib.error import HTTPError, URLError

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import proxy_manager
import crous_watcher

REPO = Path(__file__).resolve().parent
SECRET = "SECRETPW"
NETWORK_ATTEMPTS = []


def _proxy_lines(n, account="acct", start=1):
    return [f"http://{account}:{SECRET}@10.0.0.{i}:80{i:02d}" for i in range(start, start + n)]


def key_of(i):
    return f"10.0.0.{i}:80{i:02d}"


class FakeResp:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode("utf-8")
    def read(self):
        return self._payload
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


GOOD_PAYLOAD = {"results": {"items": [{"id": "T1"}], "total": {"value": 1}}}


class QuarantineBase(unittest.TestCase):
    POOL_SIZE = 8

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self._orig = {
            name: getattr(proxy_manager, name)
            for name in ("QUARANTINE_FILE", "PROXIES_FILE", "EXHAUSTED_GROUPS_FILE", "STATE_FILE")
        }
        proxy_manager.QUARANTINE_FILE = self.tmpdir / ".proxy_quarantine.json"
        proxy_manager.EXHAUSTED_GROUPS_FILE = self.tmpdir / ".exhausted_proxy_groups.json"
        proxy_manager.STATE_FILE = self.tmpdir / ".proxy_index"
        proxy_manager.PROXIES_FILE = self.tmpdir / "proxies.txt"
        self.set_pool(_proxy_lines(self.POOL_SIZE))
        # Don't let the ambient env leak an extra proxy into load_proxies().
        self._env = mock.patch.dict(os.environ, {}, clear=False)
        self._env.start()
        for var in ("CROUS_PROXY", "HTTPS_PROXY", "HTTP_PROXY"):
            os.environ.pop(var, None)

    def tearDown(self):
        self._env.stop()
        for name, val in self._orig.items():
            setattr(proxy_manager, name, val)
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def set_pool(self, lines):
        proxy_manager.PROXIES_FILE.write_text("\n".join(lines) + "\n")

    def state(self):
        return json.loads(proxy_manager.QUARANTINE_FILE.read_text()) if proxy_manager.QUARANTINE_FILE.exists() else {}


class TestQuarantineUnit(QuarantineBase):
    def test_benched_at_limit_and_skipped_by_rotation(self):
        bad = _proxy_lines(1)[0]
        r1 = proxy_manager.record_proxy_strike(bad, "502")
        r2 = proxy_manager.record_proxy_strike(bad, "502")
        self.assertEqual((r1["strikes"], r1["quarantined_now"]), (1, False))
        self.assertEqual((r2["strikes"], r2["quarantined_now"]), (2, False))
        self.assertEqual(proxy_manager.get_quarantined_proxies(), {}, "2 strikes must not bench")
        r3 = proxy_manager.record_proxy_strike(bad, "502")
        self.assertTrue(r3["quarantined_now"])
        self.assertEqual(r3["benched_count"], 1)
        self.assertIn(key_of(1), proxy_manager.get_quarantined_proxies())
        seen = set()
        for _ in range(3 * self.POOL_SIZE):
            seen.add(proxy_manager.proxy_key(proxy_manager.rotate_proxy()))
        self.assertNotIn(key_of(1), seen)
        self.assertEqual(len(seen), self.POOL_SIZE - 1)

    def test_state_file_has_no_credentials(self):
        bad = _proxy_lines(1)[0]
        for _ in range(3):
            proxy_manager.record_proxy_strike(bad, "Tunnel connection failed: 502 Bad Gateway")
        raw = proxy_manager.QUARANTINE_FILE.read_text()
        self.assertNotIn(SECRET, raw)
        self.assertNotIn("acct", raw)
        self.assertIn(key_of(1), json.loads(raw))
        self.assertFalse(list(self.tmpdir.glob("*.tmp")), "temp file left behind after atomic write")

    def test_success_resets_strikes_so_only_consecutive_failures_count(self):
        bad = _proxy_lines(1)[0]
        proxy_manager.record_proxy_strike(bad, "x")
        proxy_manager.record_proxy_strike(bad, "x")
        proxy_manager.record_proxy_success(bad)
        self.assertNotIn(key_of(1), self.state())
        proxy_manager.record_proxy_strike(bad, "x")
        r = proxy_manager.record_proxy_strike(bad, "x")
        self.assertEqual(r["strikes"], 2)
        self.assertFalse(r["quarantined_now"])
        self.assertEqual(proxy_manager.get_quarantined_proxies(), {})

    def test_strikes_persist_across_a_restart(self):
        bad = _proxy_lines(1)[0]
        proxy_manager.record_proxy_strike(bad, "x")
        proxy_manager.record_proxy_strike(bad, "x")
        # "Restart": nothing is cached in memory -- a fresh read must see 2 strikes on disk.
        self.assertEqual(self.state()[key_of(1)]["strikes"], 2)
        r = proxy_manager.record_proxy_strike(bad, "x")
        self.assertTrue(r["quarantined_now"])

    def test_bench_expires_into_probation_then_one_strike_rebenches(self):
        bad = _proxy_lines(1)[0]
        t0 = 1_000_000.0
        with mock.patch.object(proxy_manager.time, "time", return_value=t0):
            for _ in range(3):
                proxy_manager.record_proxy_strike(bad, "x")
            self.assertIn(key_of(1), proxy_manager.get_quarantined_proxies())
        with mock.patch.object(proxy_manager.time, "time", return_value=t0 + proxy_manager.QUARANTINE_TTL_SECONDS - 1):
            self.assertIn(key_of(1), proxy_manager.get_quarantined_proxies(), "still benched just before 24h")
        with mock.patch.object(proxy_manager.time, "time", return_value=t0 + proxy_manager.QUARANTINE_TTL_SECONDS + 1):
            self.assertNotIn(key_of(1), proxy_manager.get_quarantined_proxies())
            self.assertEqual(self.state()[key_of(1)]["strikes"], proxy_manager.PROXY_STRIKE_LIMIT - 1)
            picked = {proxy_manager.proxy_key(proxy_manager.rotate_proxy()) for _ in range(self.POOL_SIZE)}
            self.assertIn(key_of(1), picked, "proxy is usable again after the bench expires")
            r = proxy_manager.record_proxy_strike(bad, "x")
            self.assertTrue(r["quarantined_now"], "ONE failure on probation must re-bench immediately")

    def test_success_on_probation_clears_it_fully(self):
        bad = _proxy_lines(1)[0]
        t0 = 1_000_000.0
        with mock.patch.object(proxy_manager.time, "time", return_value=t0):
            for _ in range(3):
                proxy_manager.record_proxy_strike(bad, "x")
        with mock.patch.object(proxy_manager.time, "time", return_value=t0 + proxy_manager.QUARANTINE_TTL_SECONDS + 5):
            proxy_manager.get_quarantined_proxies()
            proxy_manager.record_proxy_success(bad)
            self.assertNotIn(key_of(1), self.state())

    def test_cap_prevents_benching_more_than_a_quarter_of_the_pool(self):
        self.set_pool(_proxy_lines(20))  # cap = int(20 * 0.25) = 5
        results = []
        for i in range(1, 8):
            p = _proxy_lines(1, start=i)[0]
            for _ in range(3):
                r = proxy_manager.record_proxy_strike(p, "x")
            results.append(r)
        self.assertEqual(len(proxy_manager.get_quarantined_proxies()), 5)
        self.assertTrue(all(r["quarantined_now"] for r in results[:5]))
        for r in results[5:]:
            self.assertTrue(r["blocked_by_floor"])
            self.assertFalse(r["quarantined_now"])
        # Rotation still has plenty of proxies.
        seen = {proxy_manager.proxy_key(proxy_manager.rotate_proxy()) for _ in range(60)}
        self.assertEqual(len(seen), 15)

    def test_tiny_pool_is_inert(self):
        self.set_pool(_proxy_lines(3))  # cap = int(0.75) = 0
        p = _proxy_lines(1)[0]
        for _ in range(5):
            r = proxy_manager.record_proxy_strike(p, "x")
        self.assertTrue(r["blocked_by_floor"])
        self.assertEqual(proxy_manager.get_quarantined_proxies(), {})

    def test_corrupt_state_fails_open(self):
        for garbage in ("{not json", "[1,2,3]", "", '{"10.0.0.1:8001": "oops"}'):
            proxy_manager.QUARANTINE_FILE.write_text(garbage)
            self.assertEqual(proxy_manager.get_quarantined_proxies(), {}, garbage)
            self.assertIsNotNone(proxy_manager.get_current_proxy())
        proxy_manager.QUARANTINE_FILE.write_text("{not json")
        r = proxy_manager.record_proxy_strike(_proxy_lines(1)[0], "x")  # must not raise
        self.assertEqual(r["strikes"], 1)

    def test_works_alongside_group_exhaustion_and_skip_flag(self):
        self.set_pool(_proxy_lines(4, "acctA") + _proxy_lines(4, "acctB", start=5))
        for _ in range(3):
            proxy_manager.record_proxy_strike(_proxy_lines(1, "acctB", start=5)[0], "x")
        proxy_manager.mark_group_exhausted("acctA")
        seen = {proxy_manager.proxy_key(proxy_manager.rotate_proxy()) for _ in range(40)}
        self.assertEqual(seen, {key_of(6), key_of(7), key_of(8)})
        seen_all = {proxy_manager.proxy_key(proxy_manager.get_current_proxy(rotate=True, skip_exhausted=False)) for _ in range(40)}
        self.assertEqual(len(seen_all), 8, "skip_exhausted=False must still see everything")

    def test_other_pools_and_load_proxies_are_untouched(self):
        res_file = self.tmpdir / "proxies_residential.txt"
        res_file.write_text("\n".join(_proxy_lines(3, "res", start=90)) + "\n")
        orig_res = proxy_manager.RESIDENTIAL_PROXIES_FILE
        proxy_manager.RESIDENTIAL_PROXIES_FILE = res_file
        try:
            before_all = proxy_manager.load_proxies()
            before_res = proxy_manager.load_residential_proxies()
            before_sniper = proxy_manager._sniper_candidate_list()
            for _ in range(3):
                proxy_manager.record_proxy_strike(_proxy_lines(1)[0], "x")
            self.assertEqual(proxy_manager.load_proxies(), before_all, "load_proxies() must not change")
            self.assertEqual(proxy_manager.load_residential_proxies(), before_res)
            self.assertEqual(proxy_manager._sniper_candidate_list(), before_sniper)
        finally:
            proxy_manager.RESIDENTIAL_PROXIES_FILE = orig_res


class TestFetchIntegration(QuarantineBase):
    POOL_SIZE = 8

    def setUp(self):
        super().setUp()
        self.telegram_calls = []
        self.attempts = {}          # host:port -> attempts
        self.behaviour = {}         # host:port -> callable raising / None for success
        NETWORK_ATTEMPTS.clear()
        self._patches = [
            mock.patch.object(crous_watcher, "send_telegram_message",
                              side_effect=lambda msg, *a, **kw: self.telegram_calls.append(msg)),
            mock.patch.object(crous_watcher, "activity_logger", mock.Mock()),
            mock.patch.object(crous_watcher, "get_next_run_estimate", return_value="50 seconds"),
            mock.patch("urllib.request.urlopen", side_effect=lambda *a, **kw: (NETWORK_ATTEMPTS.append(a), (_ for _ in ()).throw(AssertionError("real network call attempted")))[1]),
            mock.patch.object(crous_watcher, "get_crous_opener_and_proxy", side_effect=self._fake_get_opener),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self.assertEqual(NETWORK_ATTEMPTS, [], "a test tried to open a real network connection")
        super().tearDown()

    def _fake_get_opener(self, rotate=False):
        proxy_url = proxy_manager.get_current_proxy(rotate=rotate)
        key = proxy_manager.proxy_key(proxy_url)
        opener = mock.Mock()

        def do_open(req, timeout=None):
            self.attempts[key] = self.attempts.get(key, 0) + 1
            fail = self.behaviour.get(key)
            if fail is not None:
                raise fail()
            return FakeResp(GOOD_PAYLOAD)
        opener.open.side_effect = do_open
        return opener, proxy_url

    @staticmethod
    def bad_502():
        return URLError("Tunnel connection failed: 502 Bad Gateway")

    def alerts_with_error(self):
        return [c for c in crous_watcher.activity_logger.log_scouter_attempt.call_args_list
                if c.kwargs.get("error_message") is not None]

    def point_wheel_at(self, i):
        """Simulate the daily lap coming back round to proxy #i (1-based, unbenched pool order)."""
        proxy_manager._save_stored_index(i - 1)

    def test_recovered_502_is_silent_but_counts_a_strike_against_the_failing_proxy_only(self):
        self.behaviour[key_of(1)] = self.bad_502
        self.point_wheel_at(1)
        items = crous_watcher.fetch_all_crous_listings("47")
        self.assertEqual(items, GOOD_PAYLOAD["results"]["items"])
        self.assertEqual(self.alerts_with_error(), [], "a retry-recovered failure must not page the owner")
        self.assertEqual(self.telegram_calls, [])
        # Every attempt is still logged to scouter.log (failed one + success).
        calls = crous_watcher.activity_logger.log_scouter_attempt.call_args_list
        self.assertEqual([c.kwargs.get("success") for c in calls], [False, True])
        self.assertEqual(calls[0].kwargs["next_run"], "Retrying with next proxy...")
        st = self.state()
        self.assertEqual(st[key_of(1)]["strikes"], 1)
        self.assertEqual(list(st), [key_of(1)], "only the failing proxy is blamed")

    def test_three_laps_bench_it_with_exactly_one_alert_and_it_is_never_tried_again(self):
        self.behaviour[key_of(1)] = self.bad_502
        for lap in range(3):
            self.point_wheel_at(1)
            crous_watcher.fetch_all_crous_listings("47")
        self.assertEqual(self.attempts[key_of(1)], 3)
        self.assertIn(key_of(1), proxy_manager.get_quarantined_proxies())
        self.assertEqual(len(self.telegram_calls), 1)
        msg = self.telegram_calls[0]
        self.assertIn(key_of(1), msg)
        self.assertIn("7/8", msg)  # pool of 8, 1 benched
        self.assertNotIn(SECRET, msg)
        self.assertEqual(msg.count("`") % 2, 0, "unbalanced backticks would break Telegram Markdown")
        # More laps: proxy #1 must never be attempted again.
        for _ in range(3 * self.POOL_SIZE):
            proxy_manager.rotate_proxy()
            crous_watcher.fetch_all_crous_listings("47")
        self.assertEqual(self.attempts[key_of(1)], 3)
        self.assertEqual(len(self.telegram_calls), 1, "no further alerts once benched")

    def test_when_every_attempt_fails_nobody_is_blamed_and_the_final_attempt_alerts_once(self):
        for i in range(1, self.POOL_SIZE + 1):
            self.behaviour[key_of(i)] = self.bad_502
        self.point_wheel_at(1)
        with self.assertRaises(URLError):
            crous_watcher.fetch_all_crous_listings("47")
        self.assertEqual(sum(self.attempts.values()), 3)
        self.assertEqual(self.state(), {}, "systemic failure must not create any strikes")
        errs = self.alerts_with_error()
        self.assertEqual(len(errs), 1)
        self.assertIn("502 Bad Gateway", errs[0].kwargs["error_message"])
        calls = crous_watcher.activity_logger.log_scouter_attempt.call_args_list
        self.assertEqual(len(calls), 3, "all three failed attempts still logged")
        self.assertIs(calls[-1], errs[0], "the alert rides on the LAST attempt")

    def test_all_attempts_failing_with_http_5xx_also_blames_nobody(self):
        # HTTPError (unlike a tunnel URLError) doesn't re-raise inside the attempt loop for
        # non-403/429 codes: control falls out of the loop with data=None. That path must not
        # reach the health bookkeeping either.
        for i in range(1, self.POOL_SIZE + 1):
            self.behaviour[key_of(i)] = lambda: HTTPError("http://x", 500, "Internal Server Error", None, None)
        self.point_wheel_at(1)
        with self.assertRaises(HTTPError):
            crous_watcher.fetch_all_crous_listings("47")
        self.assertEqual(sum(self.attempts.values()), 3)
        self.assertEqual(self.state(), {}, "systemic failure must not create strikes or clear any")
        errs = self.alerts_with_error()
        self.assertEqual(len(errs), 1)
        self.assertIn("HTTP 500", errs[0].kwargs["error_message"])

    def test_http_403_recovered_by_next_proxy_is_silent_but_struck(self):
        self.behaviour[key_of(1)] = lambda: HTTPError("http://x", 403, "Forbidden", None, None)
        self.point_wheel_at(1)
        crous_watcher.fetch_all_crous_listings("47")
        self.assertEqual(self.alerts_with_error(), [])
        self.assertEqual(self.state()[key_of(1)]["strikes"], 1)

    def test_402_is_not_a_proxy_strike(self):
        self.set_pool(_proxy_lines(4, "acctA") + _proxy_lines(4, "acctB", start=5))
        self.behaviour = {key_of(i): (lambda: URLError("Tunnel connection failed: 402 Payment Required")) for i in range(1, 5)}
        self.point_wheel_at(1)
        items = crous_watcher.fetch_all_crous_listings("47")
        self.assertEqual(items, GOOD_PAYLOAD["results"]["items"])
        self.assertEqual(self.state(), {}, "402 is handled per account, never as a per-proxy strike")
        self.assertIn("acctA", proxy_manager.get_exhausted_groups())
        self.assertEqual(len(self.telegram_calls), 1, "existing single group-exhaustion alert only")

    def test_same_proxy_failing_then_succeeding_is_not_blamed(self):
        # Pool of ONE proxy: rotation lands on the same proxy for the retry. It fails once,
        # then works -> that's a blip, not evidence the proxy is bad.
        self.set_pool(_proxy_lines(1))
        opens = {"n": 0}

        def fake_get_opener(rotate=False):
            proxy_url = proxy_manager.get_current_proxy(rotate=rotate)
            opener = mock.Mock()

            def do_open(req, timeout=None):
                opens["n"] += 1
                if opens["n"] == 1:
                    raise self.bad_502()
                return FakeResp(GOOD_PAYLOAD)
            opener.open.side_effect = do_open
            return opener, proxy_url

        with mock.patch.object(crous_watcher, "get_crous_opener_and_proxy", side_effect=fake_get_opener):
            items = crous_watcher.fetch_all_crous_listings("47")
        self.assertEqual(items, GOOD_PAYLOAD["results"]["items"])
        self.assertEqual(opens["n"], 2, "test premise: first open failed, retry on the same proxy worked")
        self.assertEqual(self.state(), {}, "the working proxy is the same one that failed -> no proxy-specific blame")

    def test_bookkeeping_errors_never_break_polling(self):
        self.behaviour[key_of(1)] = self.bad_502
        self.point_wheel_at(1)
        with mock.patch.object(proxy_manager, "record_proxy_strike", side_effect=RuntimeError("disk full")), \
             mock.patch.object(proxy_manager, "record_proxy_success", side_effect=RuntimeError("disk full")):
            items = crous_watcher.fetch_all_crous_listings("47")
        self.assertEqual(items, GOOD_PAYLOAD["results"]["items"])

    def test_cap_reached_alerts_via_notify_general_error_and_still_polls(self):
        self.set_pool(_proxy_lines(8))  # cap = int(8 * .25) = 2
        for i in (2, 3):
            for _ in range(3):
                proxy_manager.record_proxy_strike(_proxy_lines(1, start=i)[0], "x")
        self.assertEqual(len(proxy_manager.get_quarantined_proxies()), 2)
        self.behaviour[key_of(1)] = self.bad_502
        for _ in range(3):
            # candidates now [1,4,5,6,7,8]; wheel at 0 -> proxy #1
            self.point_wheel_at(1)
            items = crous_watcher.fetch_all_crous_listings("47")
            self.assertEqual(items, GOOD_PAYLOAD["results"]["items"])
        self.assertNotIn(key_of(1), proxy_manager.get_quarantined_proxies(), "cap must stop the 3rd bench")
        crous_watcher.activity_logger.notify_general_error.assert_called_once()
        self.assertEqual(self.telegram_calls, [], "no 'benched' alert when the cap blocked the bench")


if __name__ == "__main__":
    unittest.main(verbosity=2)
