"""
verify_tool_id_discovery_fix.py — run with: venv/bin/python3 verify_tool_id_discovery_fix.py

Verifies the discover_tool_ids() hardening in crous_watcher.py:
- Caches the result so the homepage isn't re-fetched every single cycle.
- Retries with proxy rotation on failure (like fetch_all_crous_listings does),
  instead of a single unretried attempt.
- Falls back to the last known-good tool ID list on failure, only falling
  back to the hardcoded default (loudly, via Telegram) if no cached value
  exists yet.
- Still propagates AllProxyGroupsExhaustedError immediately (no retrying
  when there are genuinely no proxies left at all).

Read-only / network-free: monkeypatches crous_watcher's proxy/network/telegram
functions. Does not touch real state files or the network.
"""
import sys
import time
import unittest
from unittest import mock

import crous_watcher
from crous_watcher import AllProxyGroupsExhaustedError


class FakeResp:
    def __init__(self, html):
        self._html = html.encode("utf-8")

    def read(self):
        return self._html

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def make_opener(html):
    opener = mock.Mock()
    opener.open.return_value = FakeResp(html)
    return opener


class TestToolIdDiscoveryFix(unittest.TestCase):
    def setUp(self):
        crous_watcher._TOOL_IDS_CACHE["ids"] = None
        crous_watcher._TOOL_IDS_CACHE["checked_at"] = 0.0
        self.telegram_calls = []
        self._patches = [
            mock.patch.object(crous_watcher, "send_telegram_message",
                               side_effect=lambda *a, **kw: self.telegram_calls.append(a)),
            mock.patch.object(crous_watcher, "activity_logger", mock.Mock()),
            mock.patch.object(crous_watcher, "proxy_manager", mock.Mock()),
            mock.patch.object(crous_watcher, "get_next_run_estimate", return_value="50 seconds"),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        crous_watcher._TOOL_IDS_CACHE["ids"] = None
        crous_watcher._TOOL_IDS_CACHE["checked_at"] = 0.0

    def test_success_caches_and_skips_second_fetch(self):
        opener = make_opener('<a href="/tools/47/search">x</a><a href="/tools/22/search">y</a>')
        with mock.patch.object(crous_watcher, "get_crous_opener_and_proxy",
                                return_value=(opener, "http://proxyA")):
            first = crous_watcher.discover_tool_ids()
            second = crous_watcher.discover_tool_ids()
        self.assertEqual(first, ["22", "47"])
        self.assertEqual(second, ["22", "47"])
        self.assertEqual(opener.open.call_count, 1, "second call should use the cache, not refetch")

    def test_retries_and_rotates_before_success(self):
        good_opener = make_opener('<a href="/tools/47/search">x</a>')
        calls = {"n": 0}

        def fake_get_opener(rotate=False):
            calls["n"] += 1
            if calls["n"] < 3:
                raise ConnectionError("simulated network failure")
            return good_opener, "http://proxyC"

        with mock.patch.object(crous_watcher, "get_crous_opener_and_proxy", side_effect=fake_get_opener):
            result = crous_watcher.discover_tool_ids()

        self.assertEqual(result, ["47"])
        self.assertEqual(calls["n"], 3)
        self.assertEqual(crous_watcher.proxy_manager.rotate_proxy.call_count, 2)

    def test_all_retries_fail_falls_back_to_cached_value_no_alert(self):
        crous_watcher._TOOL_IDS_CACHE["ids"] = ["99"]
        crous_watcher._TOOL_IDS_CACHE["checked_at"] = time.time() - crous_watcher.TOOL_ID_REFRESH_INTERVAL_SEC - 1

        with mock.patch.object(crous_watcher, "get_crous_opener_and_proxy",
                                side_effect=ConnectionError("down")):
            result = crous_watcher.discover_tool_ids()

        self.assertEqual(result, ["99"], "should reuse last known-good list, not the hardcoded default")
        self.assertEqual(self.telegram_calls, [], "should not alert when a cached fallback exists")

    def test_all_retries_fail_no_cache_falls_back_to_default_and_alerts(self):
        with mock.patch.object(crous_watcher, "get_crous_opener_and_proxy",
                                side_effect=ConnectionError("down")):
            result = crous_watcher.discover_tool_ids()

        self.assertEqual(result, ["47"])
        self.assertEqual(len(self.telegram_calls), 1, "should alert loudly when falling back with no cache at all")

    def test_all_proxy_groups_exhausted_propagates_without_retrying(self):
        with mock.patch.object(crous_watcher, "get_crous_opener_and_proxy",
                                side_effect=AllProxyGroupsExhaustedError("none left")):
            with self.assertRaises(AllProxyGroupsExhaustedError):
                crous_watcher.discover_tool_ids()

        self.assertEqual(crous_watcher.proxy_manager.rotate_proxy.call_count, 0,
                          "should not retry/rotate when there are no proxies left at all")


if __name__ == "__main__":
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(unittest.TestLoader().loadTestsFromTestCase(TestToolIdDiscoveryFix))
    sys.exit(0 if result.wasSuccessful() else 1)
