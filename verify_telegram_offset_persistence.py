"""
verify_telegram_offset_persistence.py — run with: venv/bin/python3 verify_telegram_offset_persistence.py

Verifies the fix for a real, serious bug found 2026-09-25: METRICS["telegram_update_offset"]
used to live in memory ONLY, reset to 0 on every process restart. Telegram's getUpdates
redelivers any update not yet confirmed by a HIGHER offset for up to 24 hours -- so every
restart could silently replay the entire backlog of commands and button taps since the
last confirmation, each one re-triggering a real action (a Snipe button tap re-firing a
genuine apply attempt against CROUS with nobody touching anything). This service was
restarted 15-20+ times in one session; very likely explains part of the "Telegram-triggered
sniper attempts fail more than direct CLI tests" pattern reported by the user.

Verifies:
- load with no file -> 0 (first ever run)
- save then load -> round-trips correctly
- corrupted file -> falls back to 0, doesn't crash
- poll_telegram_updates() actually calls save_telegram_update_offset() with the
  right value when it processes an update (both for a plain message and for a
  callback_query), and does so BEFORE handling it, not after -- so a restart
  mid-handling (e.g. during a long snipe) can never replay it

Read-only / no real network: mocks urllib.request.urlopen for the getUpdates call.
"""
import sys
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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


class TestTelegramOffsetPersistence(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self._orig_offset_file = crous_watcher.TELEGRAM_OFFSET_FILE
        crous_watcher.TELEGRAM_OFFSET_FILE = self.tmpdir / ".telegram_update_offset"
        self._orig_offset_metric = crous_watcher.METRICS["telegram_update_offset"]

    def tearDown(self):
        crous_watcher.TELEGRAM_OFFSET_FILE = self._orig_offset_file
        crous_watcher.METRICS["telegram_update_offset"] = self._orig_offset_metric
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_load_with_no_file_returns_zero(self):
        self.assertEqual(crous_watcher.load_telegram_update_offset(), 0)

    def test_save_then_load_round_trips(self):
        crous_watcher.save_telegram_update_offset(12345)
        self.assertEqual(crous_watcher.load_telegram_update_offset(), 12345)

    def test_corrupted_file_falls_back_to_zero(self):
        crous_watcher.TELEGRAM_OFFSET_FILE.write_text("not-a-number")
        self.assertEqual(crous_watcher.load_telegram_update_offset(), 0)

    def test_poll_persists_offset_for_a_plain_message(self):
        crous_watcher.METRICS["telegram_update_offset"] = 0
        fake_update = {
            "update_id": 999,
            "message": {"chat": {"id": crous_watcher.TELEGRAM_CHAT_ID}, "text": "/status"}
        }
        with mock.patch("urllib.request.urlopen", return_value=FakeResp({"ok": True, "result": [fake_update]})), \
             mock.patch.object(crous_watcher, "handle_telegram_command"):
            crous_watcher.poll_telegram_updates()
        self.assertEqual(crous_watcher.load_telegram_update_offset(), 1000)

    def test_poll_persists_offset_for_a_callback_query_before_handling(self):
        crous_watcher.METRICS["telegram_update_offset"] = 0
        fake_update = {
            "update_id": 555,
            "callback_query": {"id": "cbq1", "message": {"chat": {"id": "1"}}, "data": "snipe:47:6:single"}
        }

        def slow_handler(cb):
            # By the time the (simulated slow) handler runs, the offset must
            # already be saved -- proving persistence happens before handling,
            # not after, so a crash/restart mid-handling can't replay this.
            self.assertEqual(crous_watcher.load_telegram_update_offset(), 556)

        with mock.patch("urllib.request.urlopen", return_value=FakeResp({"ok": True, "result": [fake_update]})), \
             mock.patch.object(crous_watcher, "handle_telegram_callback", side_effect=slow_handler) as mock_cb:
            crous_watcher.poll_telegram_updates()
        mock_cb.assert_called_once()
        self.assertEqual(crous_watcher.load_telegram_update_offset(), 556)

    def test_metrics_initialized_from_persisted_offset_on_module_reload_equivalent(self):
        # Simulate what happens at import time: METRICS["telegram_update_offset"]
        # is set from load_telegram_update_offset() once, at module load. We can't
        # easily re-trigger module import, so this directly verifies the function
        # that initialization relies on returns the persisted value.
        crous_watcher.save_telegram_update_offset(77)
        self.assertEqual(crous_watcher.load_telegram_update_offset(), 77)


if __name__ == "__main__":
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(unittest.TestLoader().loadTestsFromTestCase(TestTelegramOffsetPersistence))
    sys.exit(0 if result.wasSuccessful() else 1)
