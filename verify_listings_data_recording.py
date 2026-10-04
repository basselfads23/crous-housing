"""
verify_listings_data_recording.py — run with: venv/bin/python3 verify_listings_data_recording.py

Verifies:
1. is_target_listing() now exposes min_single_rent / min_coloc_rent / lat / lon
   in its parsed info dict (previously only the single "effective" price was
   exposed, losing data about listings that had both a single and a
   colocation price).
2. record_marseille_listing() appends one correct JSON line per call to the
   data file, without overwriting previous entries.
3. TARGET_CITY="all" (or "*"/"") still works after hoisting the lat/lon
   lookup out of the city-match if/else (it used to only be defined in the
   "else" branch, which would have been a NameError once the parsed dict
   started referencing lat/lon unconditionally).
4. record_nationwide_listing_raw() / _track_nationwide_availability()
   (added 2026-09-27, availability tracking since 2026-10-04): the FULL raw item (not
   a curated subset) is recorded each time an offer becomes available nationwide, any
   city -- including an offer (room type) already seen before, flagged
   offer_seen_before; the internal "_tool_id" bookkeeping key is stripped from the
   recorded item body; a duplicate id appearing twice in the SAME batch is only
   recorded once; every id seen is added to nationwide_seen.

Read-only-ish: writes to a temporary file, not the real
marseille_listings_data.jsonl / nationwide_listings_data.jsonl. No network access.
"""
import sys
import json
import tempfile
from pathlib import Path

import crous_watcher


def occ(mode_type, rent_cents):
    return {"type": mode_type, "rent": {"min": rent_cents, "max": rent_cents}}


results = []


def check(name, condition, detail=""):
    results.append((name, condition))
    print(("PASS" if condition else "FAIL") + f" | {name}" + (f" | {detail}" if detail and not condition else ""))


# --- 1. is_target_listing exposes the new fields ---

item_single = {
    "id": "9101",
    "residence": {
        "label": "TEST RESIDENCE",
        "address": "1 rue Test, 13006 Marseille",
        "location": {"lat": 43.29, "lon": 5.38},
    },
    "label": "T1",
    "area": {"min": 18.0},
    "occupationModes": [occ("alone", 28000)],
}
matches, info = crous_watcher.is_target_listing(item_single)
check("single-mode listing matches", matches)
check("min_single_rent exposed correctly", info.get("min_single_rent") == 280.0, str(info.get("min_single_rent")))
check("min_coloc_rent is None when no coloc mode offered", info.get("min_coloc_rent") is None)
check("lat exposed", info.get("lat") == 43.29)
check("lon exposed", info.get("lon") == 5.38)

item_both = {
    "id": "9102",
    "residence": {
        "label": "TEST RESIDENCE 2",
        "address": "2 rue Test, 13006 Marseille",
        "location": {"lat": 43.30, "lon": 5.39},
    },
    "label": "T5",
    "area": {"min": 80.0},
    "occupationModes": [occ("alone", 40000), occ("sharing", 30000)],
}
matches2, info2 = crous_watcher.is_target_listing(item_both)
check("listing with both modes matches", matches2)
check("min_single_rent captured even when coloc is chosen", info2.get("min_single_rent") == 400.0, str(info2.get("min_single_rent")))
check("min_coloc_rent captured", info2.get("min_coloc_rent") == 300.0, str(info2.get("min_coloc_rent")))

# --- 2. TARGET_CITY="all" doesn't crash now that lat/lon lookup is unconditional ---

orig_target_city = crous_watcher.TARGET_CITY
try:
    crous_watcher.TARGET_CITY = "all"
    matches3, info3 = crous_watcher.is_target_listing(item_single)
    check("TARGET_CITY='all' does not raise and still matches", matches3)
    check("TARGET_CITY='all' still exposes lat/lon", info3.get("lat") == 43.29)
except Exception as e:
    check("TARGET_CITY='all' does not raise and still matches", False, f"raised {type(e).__name__}: {e}")
finally:
    crous_watcher.TARGET_CITY = orig_target_city

# --- 3. record_marseille_listing appends correctly, doesn't overwrite ---

with tempfile.TemporaryDirectory() as tmpdir:
    tmp_file = Path(tmpdir) / "test_listings.jsonl"
    orig_file = crous_watcher.LISTINGS_DATA_FILE
    crous_watcher.LISTINGS_DATA_FILE = tmp_file
    try:
        crous_watcher.record_marseille_listing(info, "47", "https://example.test/1")
        crous_watcher.record_marseille_listing(info2, "47", "https://example.test/2")

        lines = tmp_file.read_text(encoding="utf-8").strip().split("\n")
        check("two calls produce two lines (append, not overwrite)", len(lines) == 2, str(len(lines)))

        row1 = json.loads(lines[0])
        check("recorded row has correct id", row1.get("id") == "9101")
        check("recorded row has correct residence_name", row1.get("residence_name") == "TEST RESIDENCE")
        check("recorded row has correct url", row1.get("url") == "https://example.test/1")
        check("recorded row has seen_at timestamp", isinstance(row1.get("seen_at"), str) and len(row1.get("seen_at", "")) > 0)
        check("recorded row has min_single_rent", row1.get("min_single_rent") == 280.0)

        row2 = json.loads(lines[1])
        check("second recorded row has correct id", row2.get("id") == "9102")
        check("second recorded row has min_coloc_rent", row2.get("min_coloc_rent") == 300.0)
    finally:
        crous_watcher.LISTINGS_DATA_FILE = orig_file

# --- 4. nationwide raw recording: full item kept, dedup, "_tool_id" stripped ---

with tempfile.TemporaryDirectory() as tmpdir:
    tmp_file = Path(tmpdir) / "test_nationwide.jsonl"
    orig_file = crous_watcher.NATIONWIDE_LISTINGS_DATA_FILE
    crous_watcher.NATIONWIDE_LISTINGS_DATA_FILE = tmp_file
    try:
        rich_item = {
            "id": "5001",
            "label": "CHAMBRE SIMPLE",
            "equipments": [{"category": "Bâtiment", "label": "WC"}],
            "medias": [{"src": "x.jpg", "description": "coin cuisine"}],
            "_tool_id": "47",  # internal bookkeeping key injected by check_and_notify()
        }
        already_seen_item = {"id": "5002", "_tool_id": "47"}
        duplicate_in_batch = [
            {"id": "5003", "_tool_id": "22"},
            {"id": "5003", "_tool_id": "22"},  # same id twice in one fetch (paranoia case)
        ]
        no_id_item = {"label": "no id field, must be skipped"}

        batch = [rich_item, already_seen_item] + duplicate_in_batch + [no_id_item]
        seen = {"5002"}
        nat_state = {"active": {}, "history": {}, "tracking_started": "2026-10-04T00:00:00+00:00"}
        n = crous_watcher._track_nationwide_availability(batch, seen, nat_state, True, "2026-10-04T10:00:00+00:00")

        check("3 availabilities (5001 new, 5002 known room type available again, 5003 once; no-id skipped)",
              n == 3, str(n))
        check("every id seen is now in nationwide_seen", seen == {"5001", "5002", "5003"}, str(seen))

        lines = [json.loads(l) for l in tmp_file.read_text(encoding="utf-8").strip().split("\n")]
        check("exactly 3 lines recorded (not 4 -- the in-batch duplicate wasn't double-written)", len(lines) == 3, str(len(lines)))
        known_row = next(r for r in lines if r["item"]["id"] == "5002")
        check("known room type recorded again, flagged seen-before with unknown earlier count",
              (known_row["event"], known_row["offer_seen_before"], known_row["offer_availability_no"]) == ("appeared", True, None))

        rich_row = next(r for r in lines if r["item"]["id"] == "5001")
        check("full raw item preserved, including fields record_marseille_listing() would have dropped",
              rich_row["item"].get("equipments") == rich_item["equipments"]
              and rich_row["item"].get("medias") == rich_item["medias"])
        check("'_tool_id' bookkeeping key stripped from the recorded item body", "_tool_id" not in rich_row["item"])
        check("tool_id recorded in the envelope instead", rich_row.get("tool_id") == "47")
        check("seen_at timestamp present", isinstance(rich_row.get("seen_at"), str) and len(rich_row["seen_at"]) > 0)
        check("brand-new offer flagged as first availability",
              (rich_row["offer_seen_before"], rich_row["offer_availability_no"]) == (False, 1))

        dup_rows = [r for r in lines if r["item"]["id"] == "5003"]
        check("the in-batch duplicate id was recorded exactly once", len(dup_rows) == 1, str(len(dup_rows)))
    finally:
        crous_watcher.NATIONWIDE_LISTINGS_DATA_FILE = orig_file

failed = sum(1 for _, ok in results if not ok)
print()
print(f"RESULT: {len(results) - failed} passed, {failed} failed")
sys.exit(1 if failed else 0)
