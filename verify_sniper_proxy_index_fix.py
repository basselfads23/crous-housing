"""
verify_sniper_proxy_index_fix.py — run with: venv/bin/python3 verify_sniper_proxy_index_fix.py

Verifies the fix for a real bug found in the sniper proxy audit: get_sniper_proxy()
and rotate_sniper_proxy() used to index into two DIFFERENT orderings of the proxy
list (get_sniper_proxy()'s Webshare-first candidate order vs. rotate_sniper_proxy()'s
raw load_proxies() file order) while sharing the same stored index file
(.sniper_proxy_index). After a 429 triggered rotate_sniper_proxy(), the next
get_sniper_proxy() call could land on essentially an arbitrary proxy -- including
possibly the one that had just been rate-limited -- wasting a request for nothing.

The fix (_sniper_candidate_list()) makes both functions share the exact same
ordering, so the same index always means the same proxy in both.

Fully mocked / no live network calls: the proxy list is a temp fixture file, and
get_sniper_proxy()'s live health-check request is mocked to always "succeed" so we
can test the index arithmetic in isolation, without spending any real bandwidth
against real proxies -- this matters given the Webshare 1GB bandwidth cap.
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import proxy_manager


class FakeResp:
    def __init__(self, body=b"ok"):
        self._body = body
    def read(self, n=None):
        return self._body if n is None else self._body[:n]
    def getcode(self):
        return 200
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


class TestSniperProxyIndexFix(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        # Deliberately interleaved file order: oxylabs, webshare, oxylabs, webshare.
        # This differs from the Webshare-first candidate order on purpose, so a
        # regression back to indexing into the raw file order would be caught.
        fake_proxies = self.tmpdir / "proxies.txt"
        fake_proxies.write_text(
            "oxy1.oxylabs.io:8001:oxyuser1:oxypass1\n"
            "ws1.example.com:6000:wsuser1:wspass1\n"
            "oxy2.oxylabs.io:8002:oxyuser2:oxypass2\n"
            "ws2.example.com:6001:wsuser2:wspass2\n"
        )
        self._orig_proxies_file = proxy_manager.PROXIES_FILE
        self._orig_sniper_state_file = proxy_manager.SNIPER_STATE_FILE
        proxy_manager.PROXIES_FILE = fake_proxies
        proxy_manager.SNIPER_STATE_FILE = self.tmpdir / ".sniper_proxy_index"

        self._env_patch = mock.patch.dict("os.environ", {}, clear=False)
        self._env_patch.start()
        import os
        os.environ.pop("SNIPER_PROXY", None)

        self._opener_patch = mock.patch("urllib.request.build_opener")
        mock_build_opener = self._opener_patch.start()
        mock_opener = mock.Mock()
        mock_opener.open.return_value = FakeResp()
        mock_build_opener.return_value = mock_opener

    def tearDown(self):
        self._opener_patch.stop()
        self._env_patch.stop()
        proxy_manager.PROXIES_FILE = self._orig_proxies_file
        proxy_manager.SNIPER_STATE_FILE = self._orig_sniper_state_file
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _host(self, playwright_proxy):
        return playwright_proxy["server"].replace("http://", "")

    def test_candidate_list_is_webshare_first(self):
        candidates = proxy_manager._sniper_candidate_list()
        hosts = [proxy_manager.get_proxy_group(p) or p for p in candidates]
        self.assertTrue("ws1.example.com:6000" in candidates[0])
        self.assertTrue("ws2.example.com:6001" in candidates[1])
        self.assertTrue("oxy1.oxylabs.io:8001" in candidates[2])
        self.assertTrue("oxy2.oxylabs.io:8002" in candidates[3])

    def test_get_and_rotate_share_consistent_indexing(self):
        # First call: no stored index yet -> starts at candidate_list[0] (ws1).
        p1 = proxy_manager.get_sniper_proxy()
        self.assertEqual(self._host(p1), "ws1.example.com:6000")

        # Simulate a 429: rotate_sniper_proxy() should advance from index 1
        # (what get_sniper_proxy() just saved) to index 2 in the SAME
        # Webshare-first ordering -> oxy1, not oxy2 (which is what the old
        # raw-file-order bug would have produced: proxies[(1+1)%4] = proxies[2]
        # = "oxy2" in file order [oxy1, ws1, oxy2, ws2]).
        p2 = proxy_manager.rotate_sniper_proxy()
        self.assertEqual(self._host(p2), "oxy1.oxylabs.io:8001")

        # Next get_sniper_proxy() call must agree with what rotate just set --
        # i.e. it should probe starting from index 2 (oxy1), not some other
        # position. Since the mock always "succeeds", it should immediately
        # return oxy1, proving get_sniper_proxy() and rotate_sniper_proxy()
        # are now reading/writing the same index into the same ordering.
        p3 = proxy_manager.get_sniper_proxy()
        self.assertEqual(self._host(p3), "oxy1.oxylabs.io:8001")


if __name__ == "__main__":
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(unittest.TestLoader().loadTestsFromTestCase(TestSniperProxyIndexFix))
    sys.exit(0 if result.wasSuccessful() else 1)
