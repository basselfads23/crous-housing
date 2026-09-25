"""
verify_telegram_snipe_button.py — run with: venv/bin/python3 verify_telegram_snipe_button.py

Verifies the owner-only "Snipe" button feature (handle_telegram_callback()):
- rejects callbacks from any chat other than the owner's
- rejects malformed callback data without calling the sniper
- a valid tap triggers apply_for_accommodation() with the right args, and reports
  success (with screenshot) or failure appropriately
- a second tap on the SAME accommodation_id within the debounce window is blocked
  and does NOT trigger a second apply_for_accommodation() call -- this is the
  direct fix for the real 429 risk found in live testing 2026-09-25 (repeated
  rapid attempts on the same listing got rate-limited by CROUS)
- after the debounce window elapses, a new tap is allowed again
- poll_telegram_updates() actually routes callback_query updates to the handler
  (source-level check, since fully mocking the HTTP getUpdates round trip adds
  little over directly testing the handler itself)

Read-only / no network: mocks answer_telegram_callback, apply_for_accommodation,
send_telegram_message, send_telegram_photo.
"""
import sys
import time
import unittest
from unittest import mock

import crous_watcher


class TestBuildAlertMarkups(unittest.TestCase):
    def test_auto_sniped_listing_gets_no_button_for_anyone(self):
        owner_markup, viewer_markup = crous_watcher.build_alert_markups(
            "https://example.test/1", True, "47", "1234", "single"
        )
        owner_buttons = [b for row in owner_markup["inline_keyboard"] for b in row]
        viewer_buttons = [b for row in viewer_markup["inline_keyboard"] for b in row]
        self.assertEqual(len(owner_buttons), 1, "auto-sniped listing should not get a manual snipe button")
        self.assertEqual(len(viewer_buttons), 1)

    def test_non_auto_sniped_listing_gets_button_for_owner_only(self):
        owner_markup, viewer_markup = crous_watcher.build_alert_markups(
            "https://example.test/1", False, "47", "1234", "single"
        )
        owner_buttons = [b for row in owner_markup["inline_keyboard"] for b in row]
        viewer_buttons = [b for row in viewer_markup["inline_keyboard"] for b in row]
        self.assertEqual(len(owner_buttons), 2, "owner should get the open-link button AND the snipe button")
        self.assertEqual(len(viewer_buttons), 1, "viewer must never get the snipe button")
        self.assertTrue(any("callback_data" in b for b in owner_buttons))
        self.assertFalse(any("callback_data" in b for b in viewer_buttons))

    def test_snipe_button_callback_data_encodes_tool_and_listing_and_mode(self):
        owner_markup, _ = crous_watcher.build_alert_markups(
            "https://example.test/1", False, "47", "2769", "colocation"
        )
        snipe_button = owner_markup["inline_keyboard"][1][0]
        self.assertEqual(snipe_button["callback_data"], "snipe:47:2769:colocation")


class TestTelegramSnipeButton(unittest.TestCase):
    def setUp(self):
        crous_watcher._recent_manual_snipes.clear()
        self._orig_chat_id = crous_watcher.TELEGRAM_CHAT_ID
        crous_watcher.TELEGRAM_CHAT_ID = "12345"
        self._answer_patch = mock.patch.object(crous_watcher, "answer_telegram_callback")
        self.mock_answer = self._answer_patch.start()
        self._send_msg_patch = mock.patch.object(crous_watcher, "send_telegram_message", return_value=True)
        self.mock_send_msg = self._send_msg_patch.start()
        self._send_photo_patch = mock.patch.object(crous_watcher, "send_telegram_photo", return_value=True)
        self.mock_send_photo = self._send_photo_patch.start()

    def tearDown(self):
        self._answer_patch.stop()
        self._send_msg_patch.stop()
        self._send_photo_patch.stop()
        crous_watcher.TELEGRAM_CHAT_ID = self._orig_chat_id
        crous_watcher._recent_manual_snipes.clear()

    def _callback(self, chat_id="12345", data="snipe:47:2769:single"):
        return {"id": "cbq1", "message": {"chat": {"id": chat_id}}, "data": data}

    def test_rejects_non_owner_chat(self):
        with mock.patch.object(crous_watcher, "apply_for_accommodation") as mock_apply:
            crous_watcher.handle_telegram_callback(self._callback(chat_id="99999"))
        mock_apply.assert_not_called()
        self.mock_answer.assert_called_once()
        self.assertIn("autorisé", self.mock_answer.call_args[0][1])

    def test_rejects_malformed_data(self):
        with mock.patch.object(crous_watcher, "apply_for_accommodation") as mock_apply:
            crous_watcher.handle_telegram_callback(self._callback(data="garbage"))
        mock_apply.assert_not_called()
        self.assertIn("invalide", self.mock_answer.call_args[0][1])

    def test_valid_tap_triggers_apply_with_correct_args(self):
        with mock.patch.object(crous_watcher, "apply_for_accommodation",
                                return_value={"success": True, "duration_seconds": 12.3,
                                               "screenshot_path": "/tmp/x.png"}) as mock_apply:
            crous_watcher.handle_telegram_callback(self._callback(data="snipe:47:2769:colocation"))
        mock_apply.assert_called_once_with(
            tool_id="47", accommodation_id="2769", target_mode="colocation",
            dry_run=crous_watcher.AUTO_APPLY_DRY_RUN
        )
        self.mock_send_photo.assert_called_once()

    def test_success_without_screenshot_sends_text_message(self):
        with mock.patch.object(crous_watcher, "apply_for_accommodation",
                                return_value={"success": True, "duration_seconds": 5.0}):
            crous_watcher.handle_telegram_callback(self._callback())
        self.mock_send_photo.assert_not_called()
        self.mock_send_msg.assert_called_once()

    def test_failure_reports_manual_link(self):
        with mock.patch.object(crous_watcher, "apply_for_accommodation",
                                return_value={"success": False, "error": "not_reservable"}):
            crous_watcher.handle_telegram_callback(self._callback())
        self.mock_send_msg.assert_called_once()
        self.assertIn("ÉCHEC", self.mock_send_msg.call_args[0][0])

    def test_second_tap_within_debounce_window_is_blocked(self):
        with mock.patch.object(crous_watcher, "apply_for_accommodation",
                                return_value={"success": True, "duration_seconds": 1.0}) as mock_apply:
            crous_watcher.handle_telegram_callback(self._callback())
            crous_watcher.handle_telegram_callback(self._callback())
        self.assertEqual(mock_apply.call_count, 1, "second tap on the same listing must not trigger a second snipe")
        self.assertIn("Déjà", self.mock_answer.call_args[0][1])

    def test_tap_after_debounce_window_expires_is_allowed(self):
        crous_watcher._recent_manual_snipes["2769"] = time.time() - crous_watcher.MANUAL_SNIPE_DEBOUNCE_SECONDS - 1
        with mock.patch.object(crous_watcher, "apply_for_accommodation",
                                return_value={"success": True, "duration_seconds": 1.0}) as mock_apply:
            crous_watcher.handle_telegram_callback(self._callback())
        mock_apply.assert_called_once()

    def test_different_listings_are_not_blocked_by_each_others_debounce(self):
        with mock.patch.object(crous_watcher, "apply_for_accommodation",
                                return_value={"success": True, "duration_seconds": 1.0}) as mock_apply:
            crous_watcher.handle_telegram_callback(self._callback(data="snipe:47:1111:single"))
            crous_watcher.handle_telegram_callback(self._callback(data="snipe:47:2222:single"))
        self.assertEqual(mock_apply.call_count, 2)

    def test_poll_telegram_updates_routes_callback_queries_to_handler(self):
        source = open(crous_watcher.__file__, encoding="utf-8").read()
        poll_fn_src = source[source.index("def poll_telegram_updates("):source.index("def handle_telegram_command(")]
        self.assertIn("callback_query", poll_fn_src)
        self.assertIn("handle_telegram_callback(callback_query)", poll_fn_src)


if __name__ == "__main__":
    runner = unittest.TextTestRunner(verbosity=2)
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    suite.addTests(loader.loadTestsFromTestCase(TestBuildAlertMarkups))
    suite.addTests(loader.loadTestsFromTestCase(TestTelegramSnipeButton))
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
