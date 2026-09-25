"""
verify_city_match_fix.py — run with: venv/bin/python3 verify_city_match_fix.py

Verifies the coordinate-based Marseille matching fix in is_target_listing()
(crous_watcher.py). Read-only: does not touch the network, proxies, or any
state file.

Test data sources:
- The 4 real listings the bot alerted on this week (pasted by the user from
  Telegram), with coordinates pulled live from each listing's own detail
  page via the proxy pool (2026-09-25).
- One synthetic false-positive case reproducing the exact bug class the old
  address-text regex was vulnerable to (a street literally named after
  Marseille, in a town that is not Marseille).
- One real far-away listing (Saint-Brieuc, from a live search API sample)
  as a sanity-check negative.
"""
import sys
from crous_watcher import is_target_listing

results = []


def check(name, item, expect_match):
    matches, info = is_target_listing(item)
    ok = matches == expect_match
    results.append((name, ok, matches, expect_match, info))
    status = "PASS" if ok else "FAIL"
    print(f"{status} | {name} | got match={matches} want={expect_match}"
          + (f" | price={info.get('price')} coloc={info.get('is_coloc')}" if matches else ""))


def occ(mode_type, rent_cents):
    return {"type": mode_type, "rent": {"min": rent_cents, "max": rent_cents}}


# --- Real listings from this week's Telegram alerts (coords pulled live from
# each listing's own detail page, 2026-09-25) ---

check(
    "REAL: CITE LUMINY (350.00e, 9.2km from center)",
    {
        "id": "1165",
        "residence": {
            "label": "CITE LUMINY",
            "address": "171 avenue de Luminy - 13288 Marseille cedex 9",
            "location": {"lat": 43.231, "lon": 5.44},
        },
        "label": "CHAMBRE CONFORT D",
        "area": {"min": 14.0},
        "occupationModes": [occ("alone", 35000)],
    },
    True,
)

check(
    "REAL: RESIDENCE LES DOUANES (328.00e coloc, T5)",
    {
        "id": "2769",
        "residence": {
            "label": "RESIDENCE LES DOUANES",
            "address": "5 rue Pierre Leca, 13003 Marseille",
            "location": {"lat": 43.308, "lon": 5.377},
        },
        "label": "T5 Colocation",
        "area": {"min": 81.8},
        "occupationModes": [occ("sharing", 32800)],
    },
    True,
)

check(
    "REAL: RESIDENCE ALICE CHATENOUD (284.82e, CEDEX-style postal 13388)",
    {
        "id": "2162",
        "residence": {
            "label": "RESIDENCE ALICE CHATENOUD",
            "address": "10 rue Henri Poincaré -13388 Marseille",
            "location": {"lat": 43.336, "lon": 5.409},
        },
        "label": "T1",
        "area": {"min": 19.0},
        "occupationModes": [occ("alone", 28482)],
    },
    True,
)

check(
    "REAL: CITE GASTON BERGER (255.00e, CEDEX-style postal 13331)",
    {
        "id": "2156",
        "residence": {
            "label": "CITE GASTON BERGER",
            "address": "43, rue du 141eme RIA - 13331 Marseille cedex 3",
            "location": {"lat": 43.305, "lon": 5.379},
        },
        "label": "CHAMBRE SIMPLE",
        "area": {"min": 12.0},
        "occupationModes": [occ("alone", 25500)],
    },
    True,
)

# --- The exact false-positive class the old regex-on-address-text logic was
# vulnerable to: a street literally named after Marseille, in a town that is
# NOT Marseille (~29km away, real Aix-en-Provence coordinates). The OLD code
# would have matched this (the word "marseille" appears in the address).
# The NEW code must reject it because the coordinates aren't close enough. ---

check(
    "SYNTHETIC: 'Route de Marseille' street address, but actually in Aix-en-Provence (~29km away)",
    {
        "id": "9001",
        "residence": {
            "label": "FAKE RESIDENCE AIX",
            "address": "12 Route de Marseille - 13090 Aix-en-Provence",
            "location": {"lat": 43.5297, "lon": 5.4474},
        },
        "label": "T1",
        "area": {"min": 18.0},
        "occupationModes": [occ("alone", 30000)],
    },
    False,
)

# --- Real far-away listing (Saint-Brieuc / Brittany), pulled live from the
# search API this session, as a negative sanity check. ---

check(
    "REAL: CU Villes Dorees, Saint-Brieuc (~750km away, no 'marseille' in address either)",
    {
        "id": "d72a6495",
        "residence": {
            "label": "CU Villes Dorees",
            "address": "56A rue Lafayette 22000 Saint-Brieuc ",
            "location": {"lat": 48.51, "lon": -2.746},
        },
        "label": "Studio",
        "area": {"min": 17.76},
        "occupationModes": [occ("alone", 36593)],
    },
    False,
)

# --- Fallback path: no residence.location at all (older/incomplete data).
# Must still fall back to the old address-text logic so we don't regress
# coverage when coordinates are genuinely unavailable. ---

check(
    "FALLBACK: no location field, but address text says Marseille with a standard postal code",
    {
        "id": "9002",
        "residence": {
            "label": "FAKE RESIDENCE NO COORDS",
            "address": "1 rue Exemple, 13006 Marseille",
        },
        "label": "T1",
        "area": {"min": 15.0},
        "occupationModes": [occ("alone", 30000)],
    },
    True,
)

failed = sum(1 for r in results if not r[1])
print()
print(f"RESULT: {len(results) - failed} passed, {failed} failed")
sys.exit(1 if failed else 0)
