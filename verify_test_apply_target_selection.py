"""
verify_test_apply_target_selection.py — run with: venv/bin/python3 verify_test_apply_target_selection.py

Verifies the /test_apply target-selection fix: it used to blindly take items[0]
from a full nationwide fetch -- an unpredictable, possibly already-unavailable
listing that changes every call, completely unlike the stable known-good target
used for direct CLI verification all session. This is a real, distinct bug from
the Telegram-offset one, and plausibly explains part of "the sniper fails when I
test it via Telegram but works when you test it directly" -- the two were never
testing the same thing.

Read-only, no network.
"""
import sys
import unittest

import crous_watcher


class TestPickTestApplyTarget(unittest.TestCase):
    def test_empty_list_returns_none(self):
        self.assertIsNone(crous_watcher.pick_test_apply_target([]))

    def test_prefers_first_available_item_even_if_not_first_in_list(self):
        items = [
            {"id": "111", "available": False},
            {"id": "222", "available": False},
            {"id": "333", "available": True},
            {"id": "444", "available": True},
        ]
        self.assertEqual(crous_watcher.pick_test_apply_target(items), "333")

    def test_falls_back_to_first_item_if_none_available(self):
        items = [
            {"id": "111", "available": False},
            {"id": "222", "available": False},
        ]
        self.assertEqual(crous_watcher.pick_test_apply_target(items), "111")

    def test_missing_available_key_treated_as_unavailable(self):
        items = [
            {"id": "111"},  # no "available" key at all
            {"id": "222", "available": True},
        ]
        self.assertEqual(crous_watcher.pick_test_apply_target(items), "222")

    def test_missing_id_key_falls_back_to_default_string(self):
        items = [{"available": True}]
        self.assertEqual(crous_watcher.pick_test_apply_target(items), "6")


if __name__ == "__main__":
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(unittest.TestLoader().loadTestsFromTestCase(TestPickTestApplyTarget))
    sys.exit(0 if result.wasSuccessful() else 1)
