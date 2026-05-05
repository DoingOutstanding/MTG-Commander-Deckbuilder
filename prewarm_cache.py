#!/usr/bin/env python3
"""Pre-warm the pair-score cache by running auto-build on popular commanders.

After this script runs, `pair_cache.pkl.gz` will contain pair scores for
all cards relevant to those commanders.  Ship that file alongside the
app so end-users get instant ranking for the same commanders out of the
box; the cache continues to grow organically as users explore other
commanders.

Run on a dev machine after every `profile.py` rebuild or `interactions.py`
change.  Takes a few minutes per commander on a fresh cache.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# Import app fresh so the auto-build route uses our patched rank.
import importlib.util
spec = importlib.util.spec_from_file_location("app", ROOT / "app.py")
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)

# Curated list of well-known commanders covering many archetypes.
# These are commanders in the user's `decks/` folder, plus a few extra
# popular tribal / combo commanders to broaden the cache.
PREWARM_COMMANDERS = [
    # Tribal commanders (5C / 4C / 3C / 2C / mono)
    "Sliver Hivelord",
    "Edgar Markov",
    "Krenko, Mob Boss",
    "Atraxa, Praetors' Voice",
    "Morophon, the Boundless",
    # Combo commanders
    "Heliod, Sun-Crowned",
    "Urza, Lord High Artificer",
    "Yuriko, the Tiger's Shadow",
    "Kinnan, Bonder Prodigy",
    "Niv-Mizzet, Parun",
    # Voltron / aggro
    "Karn, Legacy Reforged",
    "Bruvac the Grandiloquent",
    "Tiamat",
    "Jodah, Archmage Eternal",
    "Lord Xander, the Collector",
    # Tokens / aristocrats
    "Anhelo, the Painter",
    "Bhaal, Lord of Murder",
    "Prosper, Tome-Bound",
    "Selvala, Heart of the Wilds",
    "Oloro, Ageless Ascetic",
]


def main():
    NAME_TO_ID = app.NAME_TO_ID
    STATE = app.STATE

    print(f"Pre-warming with {len(PREWARM_COMMANDERS)} commanders.")
    print(f"Cache starts at {len(app._PAIR_CACHE):,} entries.")
    overall = time.time()
    for i, cname in enumerate(PREWARM_COMMANDERS, 1):
        oid = NAME_TO_ID.get(cname.lower())
        if not oid:
            print(f"  [{i}/{len(PREWARM_COMMANDERS)}] {cname!r} not found, skipping")
            continue
        STATE["commander_id"] = oid
        STATE["deck_ids"] = []
        STATE["basics"] = {b: 0 for b in app.BASIC_LANDS}
        before = len(app._PAIR_CACHE)
        t0 = time.time()
        with app.app.test_client() as client:
            client.post("/auto_build")
        elapsed = time.time() - t0
        added = len(app._PAIR_CACHE) - before
        print(f"  [{i}/{len(PREWARM_COMMANDERS)}] {cname:35s}  "
              f"{elapsed:6.1f}s  +{added:>7,} entries  "
              f"(cache now {len(app._PAIR_CACHE):,})")
    print(f"\nTotal: {time.time() - overall:.1f}s  cache: {len(app._PAIR_CACHE):,} entries")
    app._save_pair_cache(force=True)
    cache_path = app._CACHE_PATH
    if cache_path.exists():
        size_mb = cache_path.stat().st_size / 1024 / 1024
        print(f"Saved {cache_path.name}: {size_mb:.1f} MB on disk")


if __name__ == "__main__":
    main()
