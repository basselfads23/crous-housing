"""
verify_auth_proxy_residential_pool.py — run with: venv/bin/python3 verify_auth_proxy_residential_pool.py

Verifies get_auth_proxy() sources from the residential pool (proxies_residential.txt
/ RESIDENTIAL_PROXIES_FILE) rather than the scouter's datacenter pool (proxies.txt /
PROXIES_FILE), since the Static Residential plan purchase on 2026-09-25 -- login
(Altcha challenge, cookies) needs to look like a real human, which the scouter's
cheap datacenter pool doesn't need to.

Fully mocked / no live network calls.
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import proxy_manager


class FakeResp:
    def __init__(self, code=200, body=b"ok"):
        self._code = code
        self._body = body
    def read(self, n=None):
        return self._body if n is None else self._body[:n]
    def getcode(self):
        return self._code
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


class TestAuthProxyResidentialPool(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        fake_residential = self.tmpdir / "proxies_residential.txt"
        fake_residential.write_text("res1.example.com:7000:resuser1:respass1\n")
        fake_datacenter = self.tmpdir / "proxies.txt"
        fake_datacenter.write_text(
            "dc.oxylabs.io:8001:oxyuser:oxypass\n"
            "dc1.example.com:8000:dcuser1:dcpass1\n"
        )

        self._orig_residential_file = proxy_manager.RESIDENTIAL_PROXIES_FILE
        self._orig_proxies_file = proxy_manager.PROXIES_FILE
        self._orig_auth_state_file = proxy_manager.AUTH_STATE_FILE
        proxy_manager.RESIDENTIAL_PROXIES_FILE = fake_residential
        proxy_manager.PROXIES_FILE = fake_datacenter
        proxy_manager.AUTH_STATE_FILE = self.tmpdir / ".auth_proxy_index"

        self._opener_patch = mock.patch("urllib.request.build_opener")
        mock_build_opener = self._opener_patch.start()
        mock_opener = mock.Mock()
        mock_opener.open.return_value = FakeResp()
        mock_build_opener.return_value = mock_opener

    def tearDown(self):
        self._opener_patch.stop()
        proxy_manager.RESIDENTIAL_PROXIES_FILE = self._orig_residential_file
        proxy_manager.PROXIES_FILE = self._orig_proxies_file
        proxy_manager.AUTH_STATE_FILE = self._orig_auth_state_file
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_auth_proxy_comes_from_residential_pool(self):
        result = proxy_manager.get_auth_proxy()
        self.assertIsNotNone(result)
        self.assertIn("res1.example.com", result["server"])

    def test_auth_proxy_never_returns_a_datacenter_or_oxylabs_proxy(self):
        # Run several times (rotation) -- every result must come from the
        # residential fixture, never the datacenter/oxylabs fixture file.
        for _ in range(3):
            result = proxy_manager.get_auth_proxy()
            self.assertIn("res1.example.com", result["server"])
            self.assertNotIn("oxylabs", result["server"])
            self.assertNotIn("dc1.example.com", result["server"])


if __name__ == "__main__":
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(unittest.TestLoader().loadTestsFromTestCase(TestAuthProxyResidentialPool))
    sys.exit(0 if result.wasSuccessful() else 1)
