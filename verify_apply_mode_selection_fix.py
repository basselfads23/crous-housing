"""
verify_apply_mode_selection_fix.py — run with: venv/bin/python3 verify_apply_mode_selection_fix.py

Verifies the fix to crous_apply.py's occupation-mode selection logic.

The old code, when it couldn't find a radio button / dropdown option whose label
matched the requested mode, silently defaulted to the FIRST option in the list --
for a "colocation" target with no "coloc"-labeled option, that's very likely
"Individuel", meaning the sniper could submit a request for the wrong room type
on a listing that was specifically chosen because its colocation price qualified.

The fix (_pick_mode_index) never guesses for "single"/"colocation": it returns
None if nothing matches, so the caller fails loudly instead of misfiring.

Read-only / no network, no Playwright, no browser: this tests the pure matching
function directly, decoupled from the page-automation glue around it.
"""
import sys
import unittest

from crous_apply import _pick_mode_index, _prune_old_screenshots, SCREENSHOTS_DIR
import time


class TestPickModeIndex(unittest.TestCase):
    def test_single_mode_matches_individuel_label(self):
        labels = ["colocation", "individuel"]
        self.assertEqual(_pick_mode_index(labels, "single"), 1)

    def test_colocation_mode_matches_coloc_label(self):
        labels = ["individuel", "colocation"]
        self.assertEqual(_pick_mode_index(labels, "colocation"), 1)

    def test_single_mode_matches_seul_wording(self):
        labels = ["logement seul", "en colocation"]
        self.assertEqual(_pick_mode_index(labels, "single"), 0)

    def test_colocation_target_with_no_coloc_label_returns_none_not_index_zero(self):
        # This is the exact bug: old code defaulted to radios[0] here, which is
        # "individuel" -- wrong mode, silently submitted.
        labels = ["individuel", "partage"]  # CROUS phrased it differently than "coloc"
        self.assertIsNone(_pick_mode_index(labels, "colocation"))

    def test_single_target_with_no_matching_label_returns_none_not_index_zero(self):
        labels = ["option a", "option b"]
        self.assertIsNone(_pick_mode_index(labels, "single"))

    def test_empty_label_list_returns_none(self):
        self.assertIsNone(_pick_mode_index([], "single"))
        self.assertIsNone(_pick_mode_index([], "colocation"))

    def test_any_mode_prefers_single_label_if_present(self):
        labels = ["colocation", "individuel"]
        self.assertEqual(_pick_mode_index(labels, "any"), 1)

    def test_any_mode_falls_back_to_first_option_when_nothing_matches(self):
        labels = ["option a", "option b"]
        self.assertEqual(_pick_mode_index(labels, "any"), 0)

    def test_any_mode_with_no_options_returns_none(self):
        self.assertIsNone(_pick_mode_index([], "any"))


class TestPruneOldScreenshots(unittest.TestCase):
    def setUp(self):
        SCREENSHOTS_DIR.mkdir(exist_ok=True)
        self.old_file = SCREENSHOTS_DIR / "verify_test_old.png"
        self.new_file = SCREENSHOTS_DIR / "verify_test_new.png"
        self.old_file.write_bytes(b"x")
        self.new_file.write_bytes(b"x")
        old_time = time.time() - (20 * 86400)
        os_utime = __import__("os").utime
        os_utime(self.old_file, (old_time, old_time))

    def tearDown(self):
        for f in (self.old_file, self.new_file):
            try:
                f.unlink()
            except FileNotFoundError:
                pass

    def test_prunes_only_files_older_than_max_age(self):
        _prune_old_screenshots(max_age_days=14)
        self.assertFalse(self.old_file.exists(), "20-day-old screenshot should have been pruned")
        self.assertTrue(self.new_file.exists(), "fresh screenshot should NOT have been pruned")


if __name__ == "__main__":
    runner = unittest.TextTestRunner(verbosity=2)
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    suite.addTests(loader.loadTestsFromTestCase(TestPickModeIndex))
    suite.addTests(loader.loadTestsFromTestCase(TestPruneOldScreenshots))
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
