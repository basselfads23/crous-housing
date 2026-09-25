"""
verify_sniper_proxy_index_fix.py — run with: venv/bin/python3 verify_sniper_proxy_index_fix.py

Verifies two things about the sniper's proxy selection:

1. The index-mismatch bug found in the original sniper proxy audit:
   get_sniper_proxy() and rotate_sniper_proxy() used to index into two DIFFERENT
   orderings of the proxy list while sharing the same stored index file
   (.sniper_proxy_index). After a 429 triggered rotate_sniper_proxy(), the next
   get_sniper_proxy() call could land on essentially an arbitrary proxy -- including
   possibly the one that had just been rate-limited -- wasting a request for
   nothing. Fixed via _sniper_candidate_list(), shared by both functions.

2. Since 2026-09-25 (Static Residential plan purchase), the sniper (and auth) source
   proxies from the residential pool (proxies_residential.txt / RESIDENTIAL_PROXIES_FILE),
   NOT the scouter's datacenter pool (proxies.txt / PROXIES_FILE) -- login and the
   multi-page apply flow need to look like a real human, which residential IPs do
   far better than datacenter ones. This test confirms get_sniper_proxy() reads
   from the residential file and leaves the datacenter file/scouter untouched.

Fully mocked / no live network calls: proxy lists are temp fixture files, and
get_sniper_proxy()'s live health-check request is mocked to always "succeed" so we
can test the index arithmetic in isolation, without spending any real bandwidth.
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
        fake_residential = self.tmpdir / "proxies_residential.txt"
        fake_residential.write_text(
            "res1.example.com:7000:resuser1:respass1\n"
            "res2.example.com:7001:resuser2:respass2\n"
            "res3.example.com:7002:resuser3:respass3\n"
        )
        # A separate datacenter fixture that must NOT be touched by sniper/auth.
        fake_datacenter = self.tmpdir / "proxies.txt"
        fake_datacenter.write_text("dc1.example.com:8000:dcuser1:dcpass1\n")

        self._orig_residential_file = proxy_manager.RESIDENTIAL_PROXIES_FILE
        self._orig_proxies_file = proxy_manager.PROXIES_FILE
        self._orig_sniper_state_file = proxy_manager.SNIPER_STATE_FILE
        proxy_manager.RESIDENTIAL_PROXIES_FILE = fake_residential
        proxy_manager.PROXIES_FILE = fake_datacenter
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
        proxy_manager.RESIDENTIAL_PROXIES_FILE = self._orig_residential_file
        proxy_manager.PROXIES_FILE = self._orig_proxies_file
        proxy_manager.SNIPER_STATE_FILE = self._orig_sniper_state_file
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _host(self, playwright_proxy):
        return playwright_proxy["server"].replace("http://", "")

    def test_sniper_sources_from_residential_pool_not_datacenter(self):
        candidates = proxy_manager._sniper_candidate_list()
        self.assertEqual(len(candidates), 3, "should be the 3 residential fixture proxies, not the 1 datacenter one")
        self.assertTrue(all("res" in c for c in candidates))
        self.assertTrue(all("dc1" not in c for c in candidates))

    def test_get_and_rotate_share_consistent_indexing(self):
        # First call: no stored index yet -> starts at candidate_list[0] (res1).
        p1 = proxy_manager.get_sniper_proxy()
        self.assertEqual(self._host(p1), "res1.example.com:7000")

        # Simulate a 429: rotate_sniper_proxy() should advance from index 1
        # (what get_sniper_proxy() just saved) to index 2 -> res3. Before the
        # index-mismatch fix, rotate used to index into a DIFFERENT ordering
        # than get_sniper_proxy(), which could land on the wrong proxy here.
        p2 = proxy_manager.rotate_sniper_proxy()
        self.assertEqual(self._host(p2), "res3.example.com:7002")

        # Next get_sniper_proxy() call must agree with what rotate just set --
        # proving both functions read/write the same index into the same list.
        p3 = proxy_manager.get_sniper_proxy()
        self.assertEqual(self._host(p3), "res3.example.com:7002")


if __name__ == "__main__":
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(unittest.TestLoader().loadTestsFromTestCase(TestSniperProxyIndexFix))
    sys.exit(0 if result.wasSuccessful() else 1)
