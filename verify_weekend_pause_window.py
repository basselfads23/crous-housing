"""
verify_weekend_pause_window.py — run with: venv/bin/python3 verify_weekend_pause_window.py

Verifies in_weekend_pause_window(): a one-time, time-boxed full stop of automatic
polling, agreed 2026-09-25 to conserve proxy bandwidth over the weekend --
- paused right away (Friday) through Saturday 06:00 Paris time
- active Saturday 06:00 - 14:00 Paris time
- paused again Saturday 14:00 through Monday 06:00 Paris time
- back to normal (no special rule) from Monday 06:00 onward

Read-only, no network.
"""
import sys
import unittest
from datetime import datetime

import crous_watcher
from crous_watcher import in_weekend_pause_window, PARIS_TZ


def paris(y, m, d, h, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=PARIS_TZ)


class TestWeekendPauseWindow(unittest.TestCase):
    def test_friday_evening_is_paused(self):
        self.assertTrue(in_weekend_pause_window(paris(2026, 9, 25, 21, 34)))

    def test_saturday_just_before_6am_is_paused(self):
        self.assertTrue(in_weekend_pause_window(paris(2026, 9, 26, 5, 59)))

    def test_saturday_6am_is_active(self):
        self.assertFalse(in_weekend_pause_window(paris(2026, 9, 26, 6, 0)))

    def test_saturday_midday_is_active(self):
        self.assertFalse(in_weekend_pause_window(paris(2026, 9, 26, 10, 0)))

    def test_saturday_just_before_2pm_is_active(self):
        self.assertFalse(in_weekend_pause_window(paris(2026, 9, 26, 13, 59)))

    def test_saturday_2pm_is_paused(self):
        self.assertTrue(in_weekend_pause_window(paris(2026, 9, 26, 14, 0)))

    def test_saturday_evening_is_paused(self):
        self.assertTrue(in_weekend_pause_window(paris(2026, 9, 26, 20, 0)))

    def test_sunday_is_paused(self):
        self.assertTrue(in_weekend_pause_window(paris(2026, 9, 27, 12, 0)))

    def test_monday_just_before_6am_is_paused(self):
        self.assertTrue(in_weekend_pause_window(paris(2026, 9, 28, 5, 59)))

    def test_monday_6am_is_active(self):
        self.assertFalse(in_weekend_pause_window(paris(2026, 9, 28, 6, 0)))

    def test_monday_midday_is_active_no_special_rule(self):
        self.assertFalse(in_weekend_pause_window(paris(2026, 9, 28, 12, 0)))

    def test_following_tuesday_unaffected(self):
        self.assertFalse(in_weekend_pause_window(paris(2026, 9, 29, 12, 0)))


if __name__ == "__main__":
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(unittest.TestLoader().loadTestsFromTestCase(TestWeekendPauseWindow))
    sys.exit(0 if result.wasSuccessful() else 1)
