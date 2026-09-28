"""
verify_sniper_tier_logic.py — run with: venv/bin/python3 verify_sniper_tier_logic.py

Verifies the price/surface-tiered auto-apply trigger in is_target_listing():
  - rent < 250e            -> snipe (individual), any surface
  - 250e <= rent <= 300e   -> snipe if surface >= 12 m2, NO upper bound (the
    19m2 cap was removed 2026-09-28 at the user's request: "we don't want to
    restrict the bot... what if it was a 20m2 room for 270? We don't want the
    bot to miss this one." -- only the floor still guards against "cheap
    because it's tiny")
  - rent > 300e            -> alert only, NEVER auto-applied (the third tier,
    300-350e/>19m2, existed briefly but was removed 2026-09-25 as too loose to
    trust unattended -- the manual Snipe button covers this band instead; this
    is unaffected by, and not a reinstatement of, the 2026-09-28 change above)
  - colocation             -> NEVER auto-applied, alert only, regardless of price
  - AUTO_APPLY_ENABLED=False -> never auto-applied regardless of price/surface
  - missing surface data at a price that needs a surface check -> fails safe
    (does NOT auto-apply; never guesses)

Includes real listings from Telegram alerts as anchor cases: Alice Chatenoud
(284.82e/19m2) and Gaston Berger (255e/12m2) should both trigger; Luminy
(350e/14m2) should not (never did, and now the whole 300-350e band doesn't
auto-apply at all regardless of surface). The 20m2/270e case is hypothetical
(motivated the 2026-09-28 change but hasn't occurred as a real listing yet).

Read-only / no network.
"""
import sys
import unittest
from unittest import mock

import crous_watcher


def occ(mode_type, rent_cents):
    return {"type": mode_type, "rent": {"min": rent_cents, "max": rent_cents}}


def make_item(price_eur, surface_m2, mode_type="alone", label="T1"):
    item = {
        "id": "test",
        "residence": {
            "label": "TEST",
            "address": "1 rue Test, 13006 Marseille",
            "location": {"lat": 43.29, "lon": 5.38},
        },
        "label": label,
        "occupationModes": [occ(mode_type, int(round(price_eur * 100)))],
    }
    if surface_m2 is not None:
        item["area"] = {"min": surface_m2}
    else:
        item["area"] = {}
    return item


class TestSniperTierLogic(unittest.TestCase):
    def setUp(self):
        # The live .env currently has AUTO_APPLY_ENABLED=false (deliberately, pending
        # a manual decision to go live) -- patch it True here so these tests exercise
        # the tier logic itself, independent of that unrelated master switch.
        self._enabled_patch = mock.patch.object(crous_watcher, "AUTO_APPLY_ENABLED", True)
        self._enabled_patch.start()

    def tearDown(self):
        self._enabled_patch.stop()

    def test_below_250_snipes_regardless_of_surface_small(self):
        _, info = crous_watcher.is_target_listing(make_item(200.0, 5.0))
        self.assertTrue(info["should_auto_apply"])

    def test_below_250_snipes_regardless_of_surface_large(self):
        _, info = crous_watcher.is_target_listing(make_item(249.99, 50.0))
        self.assertTrue(info["should_auto_apply"])

    def test_below_250_snipes_even_with_missing_surface(self):
        _, info = crous_watcher.is_target_listing(make_item(240.0, None))
        self.assertTrue(info["should_auto_apply"])

    def test_mid_band_250_to_300_snipes_within_12_to_19(self):
        _, info = crous_watcher.is_target_listing(make_item(275.0, 15.0))
        self.assertTrue(info["should_auto_apply"])

    def test_mid_band_boundary_12_inclusive(self):
        _, info = crous_watcher.is_target_listing(make_item(280.0, 12.0))
        self.assertTrue(info["should_auto_apply"])

    def test_mid_band_boundary_19_inclusive(self):
        _, info = crous_watcher.is_target_listing(make_item(280.0, 19.0))
        self.assertTrue(info["should_auto_apply"])

    def test_mid_band_price_boundary_250_inclusive(self):
        _, info = crous_watcher.is_target_listing(make_item(250.0, 15.0))
        self.assertTrue(info["should_auto_apply"])

    def test_mid_band_price_boundary_300_inclusive(self):
        _, info = crous_watcher.is_target_listing(make_item(300.0, 15.0))
        self.assertTrue(info["should_auto_apply"])

    def test_mid_band_rejects_surface_below_12(self):
        _, info = crous_watcher.is_target_listing(make_item(280.0, 11.9))
        self.assertFalse(info["should_auto_apply"])

    def test_mid_band_no_longer_rejects_surface_above_19(self):
        # Was assertFalse before 2026-09-28 (the old 19m2 cap); the cap is gone now.
        _, info = crous_watcher.is_target_listing(make_item(280.0, 19.1))
        self.assertTrue(info["should_auto_apply"])

    def test_mid_band_snipes_a_much_larger_surface_no_cap_at_all(self):
        _, info = crous_watcher.is_target_listing(make_item(280.0, 100.0))
        self.assertTrue(info["should_auto_apply"])

    def test_mid_band_missing_surface_fails_safe_no_snipe(self):
        _, info = crous_watcher.is_target_listing(make_item(280.0, None))
        self.assertFalse(info["should_auto_apply"])

    def test_above_300_never_snipes_even_with_large_surface(self):
        # The third tier (300-350e, >19m2) was removed 2026-09-25 -- this band is
        # alert-only now regardless of surface, no matter how good the deal looks.
        _, info = crous_watcher.is_target_listing(make_item(320.0, 25.0))
        self.assertFalse(info["should_auto_apply"])

    def test_above_300_never_snipes_at_350_with_large_surface(self):
        _, info = crous_watcher.is_target_listing(make_item(350.0, 30.0))
        self.assertFalse(info["should_auto_apply"])

    def test_above_300_never_snipes_small_surface_either(self):
        _, info = crous_watcher.is_target_listing(make_item(320.0, 19.0))
        self.assertFalse(info["should_auto_apply"])

    def test_above_300_never_snipes_missing_surface(self):
        _, info = crous_watcher.is_target_listing(make_item(320.0, None))
        self.assertFalse(info["should_auto_apply"])

    def test_380_never_snipes_but_still_alerts(self):
        # Within alert range (<= MAX_PRICE, 400e) but well above the auto-snipe
        # ceiling (300e) -- should still match (so a manual Snipe button is
        # offered) but never auto-apply.
        matches, info = crous_watcher.is_target_listing(make_item(380.0, 100.0))
        self.assertTrue(matches)
        self.assertFalse(info["should_auto_apply"])

    def test_above_400_is_discarded_entirely(self):
        matches, info = crous_watcher.is_target_listing(make_item(410.0, 100.0))
        self.assertFalse(matches)
        self.assertEqual(info, {})

    def test_colocation_never_auto_applies_even_when_cheap(self):
        _, info = crous_watcher.is_target_listing(make_item(200.0, 15.0, mode_type="house_sharing", label="T5 Colocation"))
        self.assertFalse(info["should_auto_apply"])
        self.assertIsNone(info["min_single_rent"])

    def test_auto_apply_disabled_never_snipes(self):
        with mock.patch.object(crous_watcher, "AUTO_APPLY_ENABLED", False):
            _, info = crous_watcher.is_target_listing(make_item(200.0, 15.0))
        self.assertFalse(info["should_auto_apply"])

    # --- Real anchor cases from this week's Telegram alerts ---

    def test_real_alice_chatenoud_284_82_at_19m2_snipes(self):
        _, info = crous_watcher.is_target_listing(make_item(284.82, 19.0, label="T1"))
        self.assertTrue(info["should_auto_apply"])

    def test_real_gaston_berger_255_at_12m2_snipes(self):
        _, info = crous_watcher.is_target_listing(make_item(255.0, 12.0, label="CHAMBRE SIMPLE"))
        self.assertTrue(info["should_auto_apply"])

    def test_real_luminy_350_at_14m2_does_not_snipe(self):
        _, info = crous_watcher.is_target_listing(make_item(350.0, 14.0, label="CHAMBRE CONFORT D"))
        self.assertFalse(info["should_auto_apply"])

    def test_hypothetical_20m2_at_270_snipes(self):
        # The exact motivating example for the 2026-09-28 change: previously this
        # would NOT have auto-sniped (surface > the old 19m2 cap) even though 270e
        # for 20m2 is a strictly better deal than the same price at 12m2.
        _, info = crous_watcher.is_target_listing(make_item(270.0, 20.0))
        self.assertTrue(info["should_auto_apply"])


if __name__ == "__main__":
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(unittest.TestLoader().loadTestsFromTestCase(TestSniperTierLogic))
    sys.exit(0 if result.wasSuccessful() else 1)
