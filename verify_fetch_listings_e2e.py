"""
verify_fetch_listings_e2e.py — run with: python3 verify_fetch_listings_e2e.py
Stop the service first: sudo systemctl stop crous-watcher.service
This monkeypatches proxy_manager's file paths (temp dir) and crous_watcher's
network/telegram functions. It does not touch real state files or the network.
Only restart the service after this reports ALL PASS.
"""
import sys, os, json, time, shutil, tempfile, unittest
from unittest import mock
from pathlib import Path
from urllib.error import HTTPError, URLError

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import proxy_manager
import crous_watcher


class FakeResp:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode("utf-8")
    def read(self):
        return self._payload
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


class TestFetchListingsE2E(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self._orig_exhausted = proxy_manager.EXHAUSTED_GROUPS_FILE
        self._orig_proxies_file = proxy_manager.PROXIES_FILE
        proxy_manager.EXHAUSTED_GROUPS_FILE = self.tmpdir / ".exhausted_proxy_groups.json"
        fake_proxies = self.tmpdir / "proxies.txt"
        fake_proxies.write_text(
            "http://webshare_acct:pw@1.1.1.1:1111\n"
            "http://webshare_acct:pw@2.2.2.2:2222\n"
            "http://oxylabs_acct:pw@dc.oxylabs.io:8001\n"
        )
        proxy_manager.PROXIES_FILE = fake_proxies
        if hasattr(proxy_manager, "STATE_FILE"):
            self._orig_state_file = proxy_manager.STATE_FILE
            proxy_manager.STATE_FILE = self.tmpdir / ".proxy_index"

        self.telegram_calls = []
        self._patches = [
            mock.patch.object(crous_watcher, "send_telegram_message",
                               side_effect=lambda msg, *a, **kw: self.telegram_calls.append(msg)),
            mock.patch.object(crous_watcher, "activity_logger", mock.Mock()),
            mock.patch.object(crous_watcher, "get_next_run_estimate", return_value="50 seconds"),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        proxy_manager.EXHAUSTED_GROUPS_FILE = self._orig_exhausted
        proxy_manager.PROXIES_FILE = self._orig_proxies_file
        if hasattr(self, "_orig_state_file"):
            proxy_manager.STATE_FILE = self._orig_state_file
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_402_failover_single_alert_and_success(self):
        def fake_get_opener(rotate=False):
            proxy_url = proxy_manager.get_current_proxy(rotate=rotate)
            group = proxy_manager.get_proxy_group(proxy_url)
            opener = mock.Mock()
            if group == "webshare_acct":
                def raise_402(req, timeout=None):
                    raise URLError("Tunnel connection failed: 402 Payment Required")
                opener.open.side_effect = raise_402
            else:
                payload = {"results": {"items": [{"id": "T1"}], "total": {"value": 1}}}
                opener.open.return_value = FakeResp(payload)
            return opener, proxy_url

        with mock.patch.object(crous_watcher, "get_crous_opener_and_proxy", side_effect=fake_get_opener):
            items = crous_watcher.fetch_all_crous_listings("47")

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["id"], "T1")
        self.assertEqual(len(self.telegram_calls), 1,
                          f"Expected exactly 1 telegram alert, got {len(self.telegram_calls)}: {self.telegram_calls}")
        self.assertIn("webshare_acct", self.telegram_calls[0])
        exhausted = proxy_manager.get_exhausted_groups()
        self.assertIn("webshare_acct", exhausted)
        self.assertNotIn("oxylabs_acct", exhausted)

    def test_all_groups_exhausted_raises_and_alerts_once_each(self):
        def fake_get_opener(rotate=False):
            proxy_url = proxy_manager.get_current_proxy(rotate=rotate)
            opener = mock.Mock()
            def raise_402(req, timeout=None):
                raise URLError("Tunnel connection failed: 402 Payment Required")
            opener.open.side_effect = raise_402
            return opener, proxy_url

        with mock.patch.object(crous_watcher, "get_crous_opener_and_proxy", side_effect=fake_get_opener):
            with self.assertRaises(crous_watcher.AllProxyGroupsExhaustedError):
                crous_watcher.fetch_all_crous_listings("47")

        self.assertEqual(len(self.telegram_calls), 2,
                          f"Expected exactly 2 group-exhaustion alerts (one per group), got {len(self.telegram_calls)}")
        exhausted = proxy_manager.get_exhausted_groups()
        self.assertIn("webshare_acct", exhausted)
        self.assertIn("oxylabs_acct", exhausted)

    def test_generic_405_logs_every_attempt_and_raises_after_retries(self):
        attempt_count = {"n": 0}

        def fake_get_opener(rotate=False):
            proxy_url = proxy_manager.get_current_proxy(rotate=rotate)
            opener = mock.Mock()
            def raise_405(req, timeout=None):
                attempt_count["n"] += 1
                raise HTTPError("http://x", 405, "Method Not Allowed", None, None)
            opener.open.side_effect = raise_405
            return opener, proxy_url

        with mock.patch.object(crous_watcher, "get_crous_opener_and_proxy", side_effect=fake_get_opener):
            with self.assertRaises(HTTPError):
                crous_watcher.fetch_all_crous_listings("47")

        self.assertEqual(attempt_count["n"], 3, "Expected exactly max_proxy_retries=3 attempts")
        calls = crous_watcher.activity_logger.log_scouter_attempt.call_args_list
        # Every attempt is still LOGGED (3 rows in scouter.log)...
        self.assertEqual(len(calls), 3, "every failed attempt must still be logged")
        # ...but as of 2026-09-26 only the FINAL attempt carries error_message (=> the
        # Telegram alert). Previously ("Fix A") every attempt did, which paged the owner for
        # failures that a retry on the next proxy fixed a second later. Recovered failures are
        # now counted as per-proxy strikes instead (see verify_proxy_quarantine.py).
        with_error = [c for c in calls if c.kwargs.get("error_message") is not None]
        self.assertEqual(len(with_error), 1, "exactly one alert per fully-failed request")
        self.assertIs(calls[-1], with_error[0], "the alert must ride on the final attempt")


if __name__ == "__main__":
    unittest.main(verbosity=2)
