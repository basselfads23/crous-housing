"""
verify_posting_activity_tracking.py — run with: venv/bin/python3 verify_posting_activity_tracking.py

Verifies the new posting-activity tracker: record_posting_activity(),
load_nationwide_seen_ids() / save_nationwide_seen_ids(). Goal of the feature: record
the first/last time a NEW listing is seen each day, nationwide and Marseille
separately, to eventually replace the untested "CROUS is quiet at night/on Sundays"
assumption behind smart-cadence with real data.

Read-only-ish: writes to temporary files, never the real
posting_activity.json / nationwide_seen_ids.json. No network access.
"""
import sys
import json
import tempfile
import unittest
from pathlib import Path

import crous_watcher


class TestPostingActivityTracking(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self._orig_activity_file = crous_watcher.POSTING_ACTIVITY_FILE
        self._orig_seen_file = crous_watcher.NATIONWIDE_SEEN_FILE
        crous_watcher.POSTING_ACTIVITY_FILE = self.tmpdir / "posting_activity.json"
        crous_watcher.NATIONWIDE_SEEN_FILE = self.tmpdir / "nationwide_seen_ids.json"

    def tearDown(self):
        crous_watcher.POSTING_ACTIVITY_FILE = self._orig_activity_file
        crous_watcher.NATIONWIDE_SEEN_FILE = self._orig_seen_file
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _today_key(self):
        import datetime as dt
        return dt.datetime.now(crous_watcher.PARIS_TZ).strftime("%Y-%m-%d")

    def test_no_write_when_nothing_new(self):
        crous_watcher.record_posting_activity(0, 0)
        self.assertFalse(crous_watcher.POSTING_ACTIVITY_FILE.exists(), "should not create a file when nothing new was seen")

    def test_first_call_creates_todays_entry(self):
        crous_watcher.record_posting_activity(5, 2)
        data = json.loads(crous_watcher.POSTING_ACTIVITY_FILE.read_text())
        day = data[self._today_key()]
        self.assertEqual(day["nationwide_count"], 5)
        self.assertEqual(day["marseille_count"], 2)
        self.assertIn("nationwide_first_seen", day)
        self.assertIn("marseille_first_seen", day)
        self.assertEqual(day["nationwide_first_seen"], day["nationwide_last_seen"])

    def test_second_call_same_day_updates_last_seen_and_count_keeps_first_seen(self):
        crous_watcher.record_posting_activity(5, 2)
        data1 = json.loads(crous_watcher.POSTING_ACTIVITY_FILE.read_text())
        first_seen_1 = data1[self._today_key()]["nationwide_first_seen"]

        crous_watcher.record_posting_activity(3, 0)
        data2 = json.loads(crous_watcher.POSTING_ACTIVITY_FILE.read_text())
        day2 = data2[self._today_key()]
        self.assertEqual(day2["nationwide_first_seen"], first_seen_1, "first_seen must not change on later calls")
        self.assertEqual(day2["nationwide_count"], 8, "counts should accumulate (5 + 3)")
        self.assertEqual(day2["marseille_count"], 2, "marseille count unaffected by a 0-marseille call")

    def test_marseille_only_call_does_not_touch_nationwide_keys(self):
        crous_watcher.record_posting_activity(0, 4)
        data = json.loads(crous_watcher.POSTING_ACTIVITY_FILE.read_text())
        day = data[self._today_key()]
        self.assertNotIn("nationwide_first_seen", day)
        self.assertEqual(day["marseille_count"], 4)

    def test_yesterdays_entry_is_preserved_across_days(self):
        # Simulate a pre-existing entry from a previous day, then record today's --
        # both must coexist afterward, proving days don't clobber each other.
        crous_watcher.POSTING_ACTIVITY_FILE.write_text(json.dumps({
            "2020-01-01": {"nationwide_count": 99, "nationwide_first_seen": "x", "nationwide_last_seen": "y"}
        }))
        crous_watcher.record_posting_activity(1, 1)
        data = json.loads(crous_watcher.POSTING_ACTIVITY_FILE.read_text())
        self.assertIn("2020-01-01", data)
        self.assertEqual(data["2020-01-01"]["nationwide_count"], 99)
        self.assertIn(self._today_key(), data)

    def test_nationwide_seen_ids_roundtrip_and_dedup(self):
        ids = {"1", "2", "3"}
        crous_watcher.save_nationwide_seen_ids(ids)
        loaded = crous_watcher.load_nationwide_seen_ids()
        self.assertEqual(loaded, ids)

        loaded.add("4")
        crous_watcher.save_nationwide_seen_ids(loaded)
        self.assertEqual(crous_watcher.load_nationwide_seen_ids(), {"1", "2", "3", "4"})

    def test_load_nationwide_seen_ids_missing_file_returns_empty_set(self):
        self.assertEqual(crous_watcher.load_nationwide_seen_ids(), set())


if __name__ == "__main__":
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(unittest.TestLoader().loadTestsFromTestCase(TestPostingActivityTracking))
    sys.exit(0 if result.wasSuccessful() else 1)
