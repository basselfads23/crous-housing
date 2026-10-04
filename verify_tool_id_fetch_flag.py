"""
verify_tool_id_fetch_flag.py — run with: venv/bin/python3 verify_tool_id_fetch_flag.py

Verifies discover_tool_ids_with_fetch_flag(): check_and_notify()'s "natural delay
between homepage check and search requests" used to fire every single cycle, even
on the vast majority of cycles where discover_tool_ids() is a pure cache hit (no
real homepage request happened, since discovery is cached for up to an hour). This
helper reports whether a real fetch just happened, so the caller can skip the delay
on cache hits instead of padding every cycle for nothing.

Read-only / no network: mocks get_crous_opener_and_proxy so discover_tool_ids()'s
underlying fetch never hits the real network.
"""
import sys
import time
import unittest
from unittest import mock

import crous_watcher


class FakeResp:
    def __init__(self, html):
        self._html = html.encode("utf-8")
    def read(self):
        return self._html
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


def make_opener(html='<a href="/tools/47/search">x</a>'):
    opener = mock.Mock()
    opener.open.return_value = FakeResp(html)
    return opener


class TestToolIdFetchFlag(unittest.TestCase):
    def setUp(self):
        crous_watcher._TOOL_IDS_CACHE["ids"] = None
        crous_watcher._TOOL_IDS_CACHE["checked_at"] = 0.0
        # activity_logger appends every attempt to the LIVE scouter.log -- keep fake
        # "proxyA" rows out of it (they leaked there on every run until 2026-10-04).
        self._al = mock.patch.object(crous_watcher, "activity_logger", mock.Mock())
        self._al.start()

    def tearDown(self):
        self._al.stop()
        crous_watcher._TOOL_IDS_CACHE["ids"] = None
        crous_watcher._TOOL_IDS_CACHE["checked_at"] = 0.0

    def test_cold_cache_reports_real_fetch(self):
        with mock.patch.object(crous_watcher, "get_crous_opener_and_proxy",
                                return_value=(make_opener(), "http://proxyA")):
            ids, did_fetch = crous_watcher.discover_tool_ids_with_fetch_flag()
        self.assertEqual(ids, ["47"])
        self.assertTrue(did_fetch, "first call (cold cache) must report a real fetch")

    def test_warm_cache_reports_no_real_fetch(self):
        with mock.patch.object(crous_watcher, "get_crous_opener_and_proxy",
                                return_value=(make_opener(), "http://proxyA")) as mock_opener_fn:
            ids1, did_fetch1 = crous_watcher.discover_tool_ids_with_fetch_flag()
            ids2, did_fetch2 = crous_watcher.discover_tool_ids_with_fetch_flag()

        self.assertTrue(did_fetch1)
        self.assertFalse(did_fetch2, "second call within the cache window must be a cache hit")
        self.assertEqual(ids1, ids2)
        self.assertEqual(mock_opener_fn.call_count, 1, "cache hit should not touch the network at all")

    def test_expired_cache_reports_real_fetch_again(self):
        with mock.patch.object(crous_watcher, "get_crous_opener_and_proxy",
                                return_value=(make_opener(), "http://proxyA")):
            crous_watcher.discover_tool_ids_with_fetch_flag()
            # Simulate the cache having expired an hour+ ago.
            crous_watcher._TOOL_IDS_CACHE["checked_at"] = time.time() - crous_watcher.TOOL_ID_REFRESH_INTERVAL_SEC - 1
            ids, did_fetch = crous_watcher.discover_tool_ids_with_fetch_flag()
        self.assertTrue(did_fetch, "an expired cache must be treated as a real fetch, not a hit")


if __name__ == "__main__":
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(unittest.TestLoader().loadTestsFromTestCase(TestToolIdFetchFlag))
    sys.exit(0 if result.wasSuccessful() else 1)
