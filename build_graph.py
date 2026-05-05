#!/usr/bin/env python3
"""Pre-compute the pairwise card-interaction graph.

Reads `cards.jsonl` and writes `pair_edges.pkl.gz`, a compact sparse
representation of every pair of cards that has a non-zero interaction
score under `interactions.score_pair`.

The output is a dict mapping `(int_a, int_b)` to the float total score,
where `int_a < int_b` are stable card indexes in the alphabetical
oracle-id ordering of `cards.jsonl`.  An accompanying list of oracle
ids in that order is saved alongside so the app can map between the
oracle ids it knows about and the int ids used as cache keys.

Run after every `profile.py` rebuild or any change to
`interactions.py`.  Takes a few minutes — the result is shipped with
the deckbuilder so end-users get instant ranking out of the box.
"""

from __future__ import annotations

import gzip
import json
import pickle
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# Importing score_pair directly so the cached scores match exactly what
# the live app would compute.  The graph is the source of truth for the
# rest of the system; if score_pair changes, rebuild the graph.
import sys
sys.path.insert(0, str(ROOT))
from interactions import score_pair, compute_tribe_sizes  # noqa: E402


# Combo-detector / amplifier flag names that score_pair checks. Cards
# carrying any of these flags are scored against many partners, so we
# include "flagged_card × every other card" in the candidate set rather
# than trying to mirror the exact partner-tag logic here.
COMBO_AND_AMP_FLAGS = [
    "is_amp_etb", "is_amp_dies", "is_amp_tokens", "is_amp_mana",
    "is_amp_lifegain", "is_amp_draw", "is_amp_damage",
    "is_death_drain", "is_free_sac_outlet", "is_protection",
    "is_mana_ritual", "self_amp_equipment_triggers",
    "self_amp_aura_triggers", "self_amp_vehicle_triggers",
    "has_generic_partner", "subtype_cost_reduction",
    # combo-detector flags
    "lifelink_p1p1", "kiki_twin", "deadeye_drake", "thoracle",
    "mikaeus_trike", "sanguine_exquisite",
]


def load_cards():
    cards = {}
    with open(ROOT / "cards.jsonl", encoding="utf-8") as f:
        for line in f:
            c = json.loads(line)
            if c.get("oracle_id"):
                cards[c["oracle_id"]] = c
    return cards


def build_indexes(cards):
    by_tribe_is = defaultdict(set)
    by_tribe_ref = defaultdict(set)
    by_mech_prod = defaultdict(set)
    by_mech_cares = defaultdict(set)
    flagged = set()
    has_partner = set()

    for oid, c in cards.items():
        for t in c.get("tribes_is") or []:
            by_tribe_is[t].add(oid)
        for t in c.get("tribes_referenced") or []:
            by_tribe_ref[t].add(oid)
        for tag in c.get("mechanics_produces") or []:
            by_mech_prod[tag].add(oid)
        for tag in c.get("mechanics_cares") or []:
            by_mech_cares[tag].add(oid)
        for f in COMBO_AND_AMP_FLAGS:
            if c.get(f):
                flagged.add(oid)
                break
        if c.get("partner_pairs") or c.get("meld_pairs"):
            has_partner.add(oid)
    return {
        "by_tribe_is": by_tribe_is,
        "by_tribe_ref": by_tribe_ref,
        "by_mech_prod": by_mech_prod,
        "by_mech_cares": by_mech_cares,
        "flagged": flagged,
        "has_partner": has_partner,
    }


def find_candidate_pairs(cards, idx):
    """Return a set of (a_oid, b_oid) tuples where a < b that are
    *potentially* nonzero. We then call score_pair on each to filter
    to the actual nonzero set."""
    pairs = set()

    def add_clique(oids):
        ol = list(oids)
        for i in range(len(ol)):
            ai = ol[i]
            for j in range(i + 1, len(ol)):
                bj = ol[j]
                pairs.add((ai, bj) if ai < bj else (bj, ai))

    # Tribal: members ∪ referencers form a clique of candidates
    for tribe in set(idx["by_tribe_is"]) | set(idx["by_tribe_ref"]):
        related = idx["by_tribe_is"].get(tribe, set()) | idx["by_tribe_ref"].get(tribe, set())
        add_clique(related)

    # Mechanic: producers ∪ carers form a candidate clique
    for tag in set(idx["by_mech_prod"]) | set(idx["by_mech_cares"]):
        related = idx["by_mech_prod"].get(tag, set()) | idx["by_mech_cares"].get(tag, set())
        add_clique(related)

    # Flag-bearing cards (amplifiers, combos, archetype roles, sub-cost-reduction):
    # these can pair with many things so cross with everything.
    all_oids = list(cards.keys())
    for a in idx["flagged"] | idx["has_partner"]:
        for b in all_oids:
            if a == b:
                continue
            pairs.add((a, b) if a < b else (b, a))

    return pairs


def main():
    print("Loading cards.jsonl...")
    t0 = time.time()
    cards = load_cards()
    print(f"  {len(cards):,} cards in {time.time() - t0:.1f}s")

    print("Computing tribe sizes...")
    tribe_sizes = compute_tribe_sizes(cards)

    print("Building inverted indexes...")
    t0 = time.time()
    idx = build_indexes(cards)
    print(f"  done in {time.time() - t0:.1f}s")
    print(f"  {len(idx['by_tribe_is'])} tribes, {len(idx['by_mech_prod'])} producer tags,")
    print(f"  {len(idx['flagged']):,} flagged cards, {len(idx['has_partner']):,} partner cards")

    print("Finding candidate pairs...")
    t0 = time.time()
    pairs = find_candidate_pairs(cards, idx)
    print(f"  {len(pairs):,} candidate pairs in {time.time() - t0:.1f}s")

    # Stable int IDs in alphabetical oracle-id order so the app can
    # mmap the file consistently.
    sorted_oids = sorted(cards.keys())
    oid_to_int = {o: i for i, o in enumerate(sorted_oids)}
    print(f"  card index: {len(sorted_oids):,} entries")

    print("Scoring each candidate pair (this is the slow part)...")
    t0 = time.time()
    nonzero = {}
    for i, (a, b) in enumerate(pairs):
        if i and i % 1_000_000 == 0:
            elapsed = time.time() - t0
            rate = i / max(elapsed, 1e-3)
            eta = (len(pairs) - i) / max(rate, 1e-3)
            print(f"  {i:,}/{len(pairs):,} ({i*100//len(pairs)}%) — "
                  f"{elapsed:.0f}s elapsed, ETA {eta:.0f}s")
        result = score_pair(cards[a], cards[b], tribe_sizes)
        total = result["total"]
        if total > 0:
            nonzero[(oid_to_int[a], oid_to_int[b])] = round(total, 3)
    print(f"  {len(nonzero):,} nonzero pairs ({len(nonzero)*100//max(1,len(pairs))}% of candidates) "
          f"in {time.time() - t0:.0f}s")

    print("Saving pair_edges.pkl.gz...")
    t0 = time.time()
    out_path = ROOT / "pair_edges.pkl.gz"
    with gzip.open(out_path, "wb") as f:
        pickle.dump({
            "version": 1,
            "card_oids": sorted_oids,   # int -> oracle_id
            "scores": nonzero,          # (int_a, int_b) -> float
        }, f, protocol=pickle.HIGHEST_PROTOCOL)
    size_mb = out_path.stat().st_size / 1024 / 1024
    print(f"  saved {size_mb:.1f} MB in {time.time() - t0:.1f}s")
    print(f"  output: {out_path}")


if __name__ == "__main__":
    main()
