"""
verify_status_capabilities.py — run with: venv/bin/python3 verify_status_capabilities.py

Verifies the "what the bot does / does NOT do" section of /status
(_format_bot_capabilities_text()):
- current real config (auto-apply off, keeper stopped, credentials present) says
  scouting/alerts/data collection are on, auto-apply and keeper logins are off, and
  warns that the Snipe button (LIVE) appears on every alert and sends a real request
- auto-apply on: shows mode + tier rules; DRY-RUN vs LIVE wording is right
- keeper active + auto-apply off -> reported idle, not "reconnects every 20 min"
- paused -> scouting reported as paused
- no credentials -> says the bot cannot log in, no manual-login warning
- every variant is valid Telegram legacy Markdown (balanced * and `, no bare _)
- /status actually includes the section (mocked send, no network, no systemd)
"""
import logging
# crous_watcher calls logging.basicConfig() at import, which attaches a FileHandler on the
# live watcher.log. basicConfig is a no-op when the root logger already has a handler, so
# attach one first to keep test noise out of the production log.
logging.getLogger().addHandler(logging.NullHandler())
import itertools
import re
import unittest
from unittest import mock

import crous_watcher as cw


def render(auto_apply=False, dry_run=False, paused=False, keeper="inactive", creds=True):
    with mock.patch.object(cw, "AUTO_APPLY_ENABLED", auto_apply), \
         mock.patch.object(cw, "AUTO_APPLY_DRY_RUN", dry_run):
        return cw._format_bot_capabilities_text(paused, keeper, creds)


def assert_valid_markdown(tc, text):
    outside_code = re.sub(r"`[^`]*`", "", text)
    tc.assertEqual(text.count("`") % 2, 0, "unbalanced backticks:\n" + text)
    tc.assertEqual(outside_code.count("*") % 2, 0, "unbalanced asterisks:\n" + text)
    tc.assertNotIn("_", outside_code, "bare underscore breaks Telegram Markdown:\n" + text)


class TestCapabilities(unittest.TestCase):
    def test_current_real_config(self):
        t = render(auto_apply=False, dry_run=False, keeper="inactive", creds=True)
        print("\n----- rendered for current config -----\n" + t + "\n---------------------------------------")
        self.assertIn("sans connexion à ton compte", t)
        self.assertIn("Collecte de données", t)
        self.assertIn("Auto-candidature : *désactivée*", t)
        self.assertIn("Session keeper : arrêté", t)
        self.assertIn("sur *chaque* alerte", t)
        self.assertIn("*VRAIE demande de réservation*", t)
        does, does_not = t.split("NE FAIT PAS")[0], t.split("NE FAIT PAS")[1]
        self.assertNotIn("Auto-candidature", does)
        self.assertIn("Auto-candidature", does_not.split("Seulement si")[0])

    def test_auto_apply_live_and_dry_run(self):
        live = render(auto_apply=True, dry_run=False, keeper="active")
        self.assertIn("*ACTIVÉE* — ⚡ LIVE", live)
        self.assertIn("Sniper", live)
        self.assertIn("toutes les ~20 min", live)
        self.assertIn("alertes non auto-snipées", live)
        dry = render(auto_apply=True, dry_run=True, keeper="active")
        self.assertIn("DRY-RUN", dry)
        self.assertIn("capture seulement (DRY-RUN)", dry)
        self.assertNotIn("VRAIE demande de réservation*", dry.split("Seulement si")[1])

    def test_keeper_running_but_auto_apply_off_is_idle(self):
        t = render(auto_apply=False, keeper="active")
        self.assertIn("en veille", t)
        self.assertNotIn("toutes les ~20 min", t)

    def test_keeper_unknown(self):
        self.assertIn("état inconnu", render(keeper="unknown"))

    def test_paused(self):
        t = render(paused=True)
        self.assertIn("*EN PAUSE*", t)
        self.assertNotIn("sans connexion à ton compte", t)

    def test_no_credentials(self):
        t = render(creds=False)
        self.assertIn("Aucun identifiant CROUS", t)
        self.assertNotIn("Bouton 🎯 Snipe", t)

    def test_all_variants_valid_markdown(self):
        for aa, dr, p, k, c in itertools.product([True, False], [True, False], [True, False],
                                                 ["active", "inactive", "unknown"], [True, False]):
            with self.subTest(aa=aa, dr=dr, p=p, k=k, c=c):
                assert_valid_markdown(self, render(aa, dr, p, k, c))


class TestStatusCommand(unittest.TestCase):
    def test_status_includes_section_and_is_valid_markdown(self):
        sent = []
        with mock.patch.object(cw, "send_telegram_message", side_effect=lambda t, *a, **k: sent.append(t)), \
             mock.patch.object(cw, "is_session_valid", return_value=(False, "expired")), \
             mock.patch.object(cw, "_session_keeper_state", return_value="inactive"):
            cw.handle_telegram_command("/status")
        self.assertEqual(len(sent), 1)
        self.assertIn("Ce que le bot FAIT", sent[0])
        self.assertIn("Ce que le bot NE FAIT PAS", sent[0])
        outside_code = re.sub(r"`[^`]*`", "", sent[0])
        self.assertEqual(sent[0].count("`") % 2, 0)
        self.assertEqual(outside_code.count("*") % 2, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
