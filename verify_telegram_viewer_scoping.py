"""
verify_telegram_viewer_scoping.py — run with: venv/bin/python3 verify_telegram_viewer_scoping.py

Verifies that only the actual new-listing alert reaches the viewer chat(s)
(TELEGRAM_VIEWER_CHAT_IDS) -- everything else (test messages, startup/restart
messages, errors, sniper attempts/results) goes only to the owner chat
(TELEGRAM_CHAT_ID). broadcast_telegram_message() is the only function that ever
touches get_viewer_chat_ids() / TELEGRAM_VIEWER_CHAT_IDS, so this comes down to:
exactly one call site of it should remain (the listing alert), everything else
that used to broadcast (test notification, startup message) must now use the
owner-only send_telegram_message().

Read-only / no network: mocks the Telegram send functions for the direct-call
test, and does a source-level check for call sites that are impractical to
exercise directly (main_loop blocks forever; the listing-alert path deep inside
check_and_notify needs a full live new-listing scenario to reach).
"""
import sys
import re
import unittest
from unittest import mock

import crous_watcher


class TestTelegramViewerScoping(unittest.TestCase):
    def test_test_notification_does_not_broadcast_to_viewers(self):
        with mock.patch.object(crous_watcher, "broadcast_telegram_message") as mock_broadcast, \
             mock.patch.object(crous_watcher, "send_telegram_message", return_value=True) as mock_send:
            crous_watcher.send_test_notification()
        mock_broadcast.assert_not_called()
        mock_send.assert_called_once()

    def test_only_the_listing_alert_still_calls_broadcast(self):
        source = open(crous_watcher.__file__, encoding="utf-8").read()
        call_lines = [
            (i + 1, line) for i, line in enumerate(source.splitlines())
            if re.search(r'(?<!def )broadcast_telegram_message\(', line)
        ]
        self.assertEqual(
            len(call_lines), 1,
            f"expected exactly one call site of broadcast_telegram_message (the listing alert), found: {call_lines}"
        )
        _, line = call_lines[0]
        self.assertIn("alert_text", line, "the one remaining broadcast call should be the new-listing alert")

    def test_startup_message_is_owner_only(self):
        source = open(crous_watcher.__file__, encoding="utf-8").read()
        self.assertIn("send_telegram_message(startup_msg)", source)
        self.assertNotIn("broadcast_telegram_message(startup_msg)", source)


if __name__ == "__main__":
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(unittest.TestLoader().loadTestsFromTestCase(TestTelegramViewerScoping))
    sys.exit(0 if result.wasSuccessful() else 1)
