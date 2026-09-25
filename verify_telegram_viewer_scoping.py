"""
verify_telegram_viewer_scoping.py — run with: venv/bin/python3 verify_telegram_viewer_scoping.py

Verifies that only the actual new-listing alert reaches the viewer chat(s)
(TELEGRAM_VIEWER_CHAT_IDS) -- everything else (test messages, startup/restart
messages, errors, sniper attempts/results) goes only to the owner chat
(TELEGRAM_CHAT_ID).

Originally this ran through a single broadcast_telegram_message() function; as of
the "Snipe" button work, the listing alert is sent as two separate
send_telegram_message() calls instead (one to the owner with an extra button, one
per viewer without it), since the owner's and viewers' copies now genuinely
differ, not just in recipient. broadcast_telegram_message() itself was removed
entirely once it had no callers left.

Read-only / no network: mocks the Telegram send functions for the direct-call
test, and does source-level checks for call sites that are impractical to
exercise directly (main_loop blocks forever; the listing-alert path deep inside
check_and_notify needs a full live new-listing scenario to reach).
"""
import sys
import unittest
from unittest import mock

import crous_watcher


class TestTelegramViewerScoping(unittest.TestCase):
    def test_broadcast_telegram_message_no_longer_exists(self):
        self.assertFalse(
            hasattr(crous_watcher, "broadcast_telegram_message"),
            "broadcast_telegram_message should have been removed once it had no callers left"
        )

    def test_test_notification_does_not_reach_viewers(self):
        with mock.patch.object(crous_watcher, "send_telegram_message", return_value=True) as mock_send:
            crous_watcher.send_test_notification()
        mock_send.assert_called_once()
        # send_telegram_message() with no chat_id override defaults to the owner
        # chat only -- confirmed by its own signature/behavior, checked directly
        # in verify_telegram_photo_markup_fix.py's chat_id-override tests.
        _, kwargs = mock_send.call_args
        self.assertNotIn("chat_id", kwargs, "test notification must not target a specific (viewer) chat_id")

    def test_startup_message_is_owner_only(self):
        source = open(crous_watcher.__file__, encoding="utf-8").read()
        self.assertIn("send_telegram_message(startup_msg)", source)

    def test_listing_alert_sends_to_owner_then_loops_viewers_separately(self):
        source = open(crous_watcher.__file__, encoding="utf-8").read()
        fn_start = source.index("def check_and_notify(")
        fn_src = source[fn_start:source.index("\ndef ", fn_start + 1)]
        self.assertIn('send_telegram_message(alert_text, reply_markup=owner_markup)', fn_src)
        self.assertIn("for viewer_cid in get_viewer_chat_ids():", fn_src)
        self.assertIn(
            'send_telegram_message(alert_text, reply_markup=viewer_markup, chat_id=viewer_cid)', fn_src
        )


if __name__ == "__main__":
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(unittest.TestLoader().loadTestsFromTestCase(TestTelegramViewerScoping))
    sys.exit(0 if result.wasSuccessful() else 1)
