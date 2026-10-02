"""
verify_listing_reappearance_alerts.py — run with: venv/bin/python3 verify_listing_reappearance_alerts.py

Verifies "one alert per appearance" (update_listing_visibility() + check_and_notify()):
- a listing alerts when it comes online, and NOT again while it stays online
- a listing that goes offline (missing from 2 complete checks in a row) and comes back
  alerts again, with the SAME alert text as a brand-new listing (no "seen before" label);
  the re-appearance and its appearance number are recorded in the data file only
- a one-check disappearance (the API returned incomplete results without error:
  53 -> 8 listings on 2026-09-30 00:00 UTC) does NOT cause a second alert
- a check where part of the fetch failed never counts as "missing"
- an ID seen before this feature existed (legacy seen_ids) alerts like any other
- duplicate items in one API response alert once
- each appearance and each disappearance is recorded in the Marseille data file
- regression for the stale-state bug: a failed cycle no longer erases IDs recorded
  since startup (_record_cycle_failure() re-reads the file), and main_loop() no
  longer saves a startup snapshot

No network, no real Telegram, no real state/data files: everything is mocked or
redirected to a temp dir. Uses two real Marseille listings from
nationwide_listings_data.jsonl as fixtures.
"""
import logging
# crous_watcher calls logging.basicConfig() at import, which attaches a FileHandler on the
# live watcher.log. basicConfig is a no-op when the root logger already has a handler, so
# attach one first to keep test noise out of the production log.
logging.getLogger().addHandler(logging.NullHandler())
import copy
import json
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import crous_watcher as cw

HERE = Path(__file__).resolve().parent


def _real_item(listing_id):
    with open(HERE / "nationwide_listings_data.jsonl", encoding="utf-8") as f:
        for line in f:
            row = json.loads(line)
            if str(row["item"]["id"]) == listing_id:
                return row["item"]
    raise LookupError(listing_id)


A = _real_item("1165")  # CITE LUMINY, 350 €, Marseille cedex 9
B = _real_item("2168")  # CITE ALICE CHATENOUD, 277 €, Marseille


class TestVisibilityTracker(unittest.TestCase):
    def run_checks(self, sequence, complete=None):
        active, alerts, gones = {}, [], []
        for i, ids in enumerate(sequence):
            ok = True if complete is None else complete[i]
            appeared, gone = cw.update_listing_visibility(active, {x: {} for x in ids}, ok, f"t{i}")
            alerts += [(i, x) for x in appeared]
            gones += [(i, x) for x, _ in gone]
        return alerts, gones, active

    def test_first_sighting_alerts_once_while_online(self):
        alerts, gones, _ = self.run_checks([["A"]] * 10)
        self.assertEqual(alerts, [(0, "A")])
        self.assertEqual(gones, [])

    def test_one_check_glitch_does_not_realert(self):
        # the real 2026-09-30 00:00 UTC pattern: present, missing for ONE check, present
        alerts, gones, _ = self.run_checks([["A"], [], ["A"], ["A"]])
        self.assertEqual(alerts, [(0, "A")])
        self.assertEqual(gones, [])

    def test_gone_after_two_misses_then_back_realerts(self):
        alerts, gones, _ = self.run_checks([["A"], [], [], ["A"]])
        self.assertEqual(alerts, [(0, "A"), (3, "A")])
        self.assertEqual(gones, [(2, "A")])

    def test_incomplete_fetch_never_counts_as_missing(self):
        alerts, gones, active = self.run_checks(
            [["A"], [], [], [], ["A"]], complete=[True, False, False, False, True])
        self.assertEqual(alerts, [(0, "A")])
        self.assertEqual(gones, [])
        self.assertEqual(active["A"]["missed"], 0)

    def test_miss_counter_resets_when_seen(self):
        alerts, gones, _ = self.run_checks([["A"], [], ["A"], [], ["A"], [], ["A"]])
        self.assertEqual(alerts, [(0, "A")], "alternating single misses never add up to 'gone'")

    def test_entry_tracks_first_last_and_checks(self):
        _, _, active = self.run_checks([["A"], ["A"], ["A"]])
        self.assertEqual((active["A"]["since"], active["A"]["last_seen"], active["A"]["checks"]), ("t0", "t2", 3))


class TestCheckAndNotifyReappearance(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.sent = []
        self.responses = []  # one entry per check: {tool_id: list-of-items or Exception}
        files = {
            "STATE_FILE": "listings_seen.json",
            "LISTINGS_DATA_FILE": "marseille.jsonl",
            "NATIONWIDE_LISTINGS_DATA_FILE": "nationwide.jsonl",
            "NATIONWIDE_SEEN_FILE": "nationwide_seen.json",
            "POSTING_ACTIVITY_FILE": "posting.json",
            "HISTORY_LOG_FILE": "history.log",
        }
        self.patches = [mock.patch.object(cw, k, self.tmp / v) for k, v in files.items()]
        self.patches += [
            mock.patch.object(cw, "send_telegram_message",
                              side_effect=lambda text, reply_markup=None, chat_id=None: self.sent.append((chat_id, text)) or True),
            mock.patch.object(cw, "get_viewer_chat_ids", return_value=["viewer"]),
            mock.patch.object(cw, "proxy_manager", None),
            mock.patch.object(cw, "AUTO_APPLY_ENABLED", False),
            mock.patch.object(cw, "discover_tool_ids_with_fetch_flag", side_effect=self._tools),
            mock.patch.object(cw, "fetch_all_crous_listings", side_effect=self._fetch),
            mock.patch.object(cw.time, "sleep"),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.tmp)

    def _tools(self):
        return list(self.responses[0].keys()), False

    def _fetch(self, tool_id):
        r = self.responses[0][tool_id]
        if isinstance(r, Exception):
            raise r
        return [copy.deepcopy(x) for x in r]

    def check(self, *items, failing_tool=False):
        resp = {"47": list(items)}
        if failing_tool:
            resp["99"] = RuntimeError("simulated tool failure")
        self.responses = [resp]
        before = len(self.sent)
        cw.check_and_notify()
        return [t for _, t in self.sent[before:]]

    def data_lines(self):
        p = self.tmp / "marseille.jsonl"
        return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []

    def test_full_lifecycle(self):
        # 1. comes online -> one alert, to owner AND viewer
        out = self.check(A)
        self.assertEqual(len(out), 2)
        self.assertIn("NOUVELLE OFFRE", out[0])
        self.assertEqual([c for c, _ in self.sent], [None, "viewer"])
        first_alert = out[0]
        # 2. stays online, including a duplicated item in one response -> nothing
        self.assertEqual(self.check(A), [])
        self.assertEqual(self.check(A, A), [])
        # 3. API glitch: missing from ONE complete check, then back -> nothing
        self.assertEqual(self.check(), [])
        self.assertEqual(self.check(A), [])
        # 4. genuinely goes offline (2 complete checks) -> nothing sent, "gone" recorded
        self.assertEqual(self.check(), [])
        self.assertEqual(self.check(), [])
        gone = [l for l in self.data_lines() if l.get("event") == "gone"]
        self.assertEqual(len(gone), 1)
        self.assertEqual(gone[0]["id"], "1165")
        self.assertEqual(gone[0]["checks_seen"], 4)
        # 5. comes back -> alerted again, exactly like a new listing
        out = self.check(A)
        self.assertEqual(len(out), 2)
        print("\n----- re-appearance alert as sent -----\n" + out[0] + "\n---------------------------------------")
        self.assertEqual(out[0], first_alert, "re-appearance alert must be identical to the first one")
        self.assertNotIn("Déjà vue", out[0])
        self.assertEqual(out[0], out[1], "viewer gets the same text")
        # 6. and again not repeated while it stays up
        self.assertEqual(self.check(A), [])

        appeared = [l for l in self.data_lines() if l.get("event") == "appeared"]
        self.assertEqual([(l["id"], l["appearance_no"], l["reappearance"]) for l in appeared],
                         [("1165", 1, False), ("1165", 2, True)])
        self.assertEqual(cw._count_marseille_appearances(), 2, "/status counts appearances, not gone lines")
        state = json.loads((self.tmp / "listings_seen.json").read_text())
        self.assertEqual(state["history"]["1165"]["appearances"], 2)
        self.assertIn("1165", state["active"])

    def test_partial_fetch_never_marks_listing_gone(self):
        self.check(A)
        for _ in range(4):
            self.assertEqual(self.check(failing_tool=True), [])
        self.assertEqual(self.check(A), [], "absent only in checks where a tool failed -> still the same appearance")
        self.assertEqual([l for l in self.data_lines() if l.get("event") == "gone"], [])

    def test_legacy_seen_id_alerts_like_any_other(self):
        # B was alerted before this feature existed: in seen_ids, no history
        (self.tmp / "listings_seen.json").write_text(json.dumps({"seen_ids": ["2168"], "consecutive_failures": 0}))
        out = self.check(B)
        self.assertEqual(len(out), 2, "previously silenced forever -- must alert now")
        self.assertIn("NOUVELLE OFFRE", out[0])
        self.assertNotIn("Déjà vue", out[0])
        last = self.data_lines()[-1]
        self.assertEqual((last["reappearance"], last["appearance_no"]), (True, None),
                         "data says: seen before, earlier count unknown")
        # next re-appearance has a known (lower-bound) count -- in the data, not the alert
        self.check(); self.check()
        out = self.check(B)
        self.assertNotIn("Déjà vue", out[0])
        last = self.data_lines()[-1]
        self.assertEqual((last["reappearance"], last["appearance_no"]), (True, 3))

    def test_two_listings_independent(self):
        out = self.check(A, B)
        self.assertEqual(len(out), 4)
        self.check(A); self.check(A)  # B gone, A stays
        out = self.check(A, B)
        self.assertEqual(len(out), 2)
        self.assertIn("CITE ALICE CHATENOUD", out[0])

    def test_alert_markdown_balanced(self):
        (self.tmp / "listings_seen.json").write_text(json.dumps({"seen_ids": ["2168"]}))
        for text in self.check(A, B):
            outside = re.sub(r"`[^`]*`", "", text)
            self.assertEqual(outside.count("*") % 2, 0, text)


class TestStaleStateRegression(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.p = mock.patch.object(cw, "STATE_FILE", self.tmp / "listings_seen.json")
        self.p.start()

    def tearDown(self):
        self.p.stop()
        shutil.rmtree(self.tmp)

    def test_failure_after_new_alert_keeps_it(self):
        # replay of #2156: startup state -> a check records the ID -> a cycle fails
        cw.save_state({"seen_ids": ["1"], "consecutive_failures": 0})
        st = cw.load_state()
        st["seen_ids"].append("2156")
        st["active"]["2156"] = {"since": "x", "last_seen": "x", "checks": 1, "missed": 0}
        st["history"]["2156"] = {"appearances": 1}
        cw.save_state(st)
        self.assertEqual(cw._record_cycle_failure(), 1)
        self.assertEqual(cw._record_cycle_failure(), 2)
        after = cw.load_state()
        self.assertIn("2156", after["seen_ids"])
        self.assertIn("2156", after["active"])
        self.assertEqual(after["history"]["2156"], {"appearances": 1})
        self.assertEqual(after["consecutive_failures"], 2)

    def test_load_state_keeps_unknown_keys_and_old_formats(self):
        cw.save_state({"seen_ids": ["1"], "future_key": 42})
        self.assertEqual(cw.load_state()["future_key"], 42)
        (self.tmp / "listings_seen.json").write_text('["7", "8"]')
        st = cw.load_state()
        self.assertEqual((st["seen_ids"], st["active"], st["history"]), (["7", "8"], {}, {}))

    def test_main_loop_no_longer_saves_a_startup_snapshot(self):
        src = Path(cw.__file__).read_text(encoding="utf-8")
        fn = src[src.index("def main_loop("):]
        self.assertNotIn("load_state()", fn)
        self.assertNotIn("save_state(state)", fn)
        self.assertEqual(fn.count("_record_cycle_failure()"), 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
