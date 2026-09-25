"""
verify_telegram_photo_markup_fix.py — run with: venv/bin/python3 verify_telegram_photo_markup_fix.py

Verifies the send_telegram_photo() bug fix: it used to not accept reply_markup at
all, while two real call sites in check_and_notify() (the automatic sniper's
success/failure result messages) passed reply_markup= anyway -- a guaranteed
TypeError the moment a real auto-snipe attempt with a screenshot occurred. Never
triggered before AUTO_APPLY_ENABLED was flipped true, since auto-apply was off for
the whole project until today.

Read-only / no network: mocks requests.post.
"""
import sys
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import crous_watcher


class TestTelegramPhotoMarkupFix(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.photo = self.tmpdir / "test.png"
        self.photo.write_bytes(b"fake png bytes")
        self._orig_token = crous_watcher.TELEGRAM_BOT_TOKEN
        self._orig_chat = crous_watcher.TELEGRAM_CHAT_ID
        crous_watcher.TELEGRAM_BOT_TOKEN = "fake-token"
        crous_watcher.TELEGRAM_CHAT_ID = "12345"

    def tearDown(self):
        crous_watcher.TELEGRAM_BOT_TOKEN = self._orig_token
        crous_watcher.TELEGRAM_CHAT_ID = self._orig_chat
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_accepts_reply_markup_without_raising(self):
        markup = {"inline_keyboard": [[{"text": "Open", "url": "https://example.test"}]]}
        with mock.patch("requests.post") as mock_post:
            mock_post.return_value.ok = True
            try:
                result = crous_watcher.send_telegram_photo(str(self.photo), "caption", reply_markup=markup)
            except TypeError as e:
                self.fail(f"send_telegram_photo raised TypeError on reply_markup: {e}")
        self.assertTrue(result)

    def test_reply_markup_is_actually_sent_as_json(self):
        markup = {"inline_keyboard": [[{"text": "Open", "url": "https://example.test"}]]}
        with mock.patch("requests.post") as mock_post:
            mock_post.return_value.ok = True
            crous_watcher.send_telegram_photo(str(self.photo), "caption", reply_markup=markup)
        _, kwargs = mock_post.call_args
        sent_markup = json.loads(kwargs["data"]["reply_markup"])
        self.assertEqual(sent_markup, markup)

    def test_no_reply_markup_key_when_none_given(self):
        with mock.patch("requests.post") as mock_post:
            mock_post.return_value.ok = True
            crous_watcher.send_telegram_photo(str(self.photo), "caption")
        _, kwargs = mock_post.call_args
        self.assertNotIn("reply_markup", kwargs["data"])

    def test_chat_id_override_still_works(self):
        with mock.patch("requests.post") as mock_post:
            mock_post.return_value.ok = True
            crous_watcher.send_telegram_photo(str(self.photo), "caption", chat_id="999")
        _, kwargs = mock_post.call_args
        self.assertEqual(kwargs["data"]["chat_id"], "999")


if __name__ == "__main__":
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(unittest.TestLoader().loadTestsFromTestCase(TestTelegramPhotoMarkupFix))
    sys.exit(0 if result.wasSuccessful() else 1)
