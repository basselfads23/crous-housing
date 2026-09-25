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

Read-only-ish: writes to a temporary file, not the real
marseille_listings_data.jsonl. No network access.
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

failed = sum(1 for _, ok in results if not ok)
print()
print(f"RESULT: {len(results) - failed} passed, {failed} failed")
sys.exit(1 if failed else 0)
