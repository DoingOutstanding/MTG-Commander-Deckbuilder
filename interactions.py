#!/usr/bin/env python3
"""
Pairwise interaction scoring.

This module exposes one function: score_pair(a, b) -> {edges: [...], total: float}.
Each edge has a type, weight, and zone tags identifying where the interaction
takes place. The total is the sum of edge weights (with a tribal-class
penalty so the universe of Goblin-on-Goblin edges doesn't dwarf everything else).

Edge types:
  - tribal             A's tribe filter matches B's subtype
  - mechanic_overlap   Both cards work with the same mechanic (counters, tokens, ...)
  - resource_flow      A produces X, B consumes/cares about X
  - combo              A + B form a known game-ending combo
"""

import json
import re
from pathlib import Path
from collections import Counter, defaultdict

ROOT = Path(__file__).resolve().parent
CARDS_PATH = ROOT / "cards.jsonl"

# Mechanic-tag asymmetry — if a tag appears in A.mechanics_produces and
# B.mechanics_cares (or vice versa), that's a stronger edge than mere overlap.
# Some tags are bidirectional (e.g. "p1p1_counters" — anyone who places a
# counter cares about everyone who pays/uses counters).
BIDIRECTIONAL_TAGS = {
    # Resource accumulation tags — more producers in the deck genuinely
    # compound the value (more counters / tokens / mana / cards / etc.).
    "p1p1_counters", "m1m1_counters",
    "tokens_creature", "tokens_treasure", "tokens_food", "tokens_clue", "tokens_blood",
    "graveyard_payoff", "self_mill",
    "artifacts_matter", "enchantments_matter",
    "lifegain", "lifeloss_opp",
    "spellslinger", "draw_cards",
    "landfall",
    # NOTE: ramp_mana is deliberately *not* bidirectional. Two mana
    # producers genuinely don't compound 1:1 — there's a hard ceiling
    # on how much mana you can spend per turn, and a deck with 6 rocks
    # is barely better than one with 4. Bidirectional ramp_mana created
    # a clique where every rock paired with every other rock for free,
    # snowballing decks toward "lots of ramp" themes (Doubling Cube,
    # Mana Reflection, Joiner Adept, Manaweft Sliver) regardless of
    # what the commander actually wanted. Producer → carer (rock + an
    # X-spell that cares about mana) still works fine.
    "counters_in_general",
    "untap",
    "voltron_attached",
    "equipment", "auras",
    # Mutate cards genuinely compound: each new mutate card piled onto
    # an existing creature pays a (usually-cheap) mutate cost AND fires
    # every "Whenever this creature mutates" trigger on every card in
    # the pile. So three mutate cards on top of each other = three
    # triggers from each, not one shared trigger. Treat as bidirectional.
    "mutate",
    # NOTE: trigger-condition tags (etb_trigger, dies_trigger_payoff,
    # attack_trigger, blink_flicker, sacrifice_*) are deliberately NOT
    # in this set. Two creatures that both have ETB triggers don't
    # compound — each fires once when its own card enters. They should
    # not pair via mechanic_overlap; they each pair separately with
    # ETB-doublers / blink effects / token producers.
}

# Edge weight per matched mechanic.
MECH_EDGE_WEIGHT = 3.0
# Edge weight per matched tribe (penalised by tribe size — see compute below).
TRIBAL_EDGE_BASE = 8.0
# Edge weight when both cards are in the *same* tribe and at least one references that tribe (lord-style).
TRIBAL_LORD_BONUS = 12.0
# Combo edges
COMBO_WEIGHT = 100.0

# Zone tag inference. For each edge type, which (zone_a, zone_b) pairs are
# the interaction live in?
def zones_for_edge(edge_type, a, b):
    """Return list of (zone_a, zone_b) tuples where this edge fires."""
    a_zones = set(a.get("active_zones") or ["BF"])
    b_zones = set(b.get("active_zones") or ["BF"])
    pairs = []
    # Most interactions need both on BF. Triggers from cast can fire BF+STACK.
    if "BF" in a_zones and "BF" in b_zones:
        pairs.append(("BF", "BF"))
    if "STACK" in a_zones and "BF" in b_zones:
        pairs.append(("STACK", "BF"))
    if "BF" in a_zones and "STACK" in b_zones:
        pairs.append(("BF", "STACK"))
    if "GRAVE" in a_zones and "BF" in b_zones:
        pairs.append(("GRAVE", "BF"))
    if "BF" in a_zones and "GRAVE" in b_zones:
        pairs.append(("BF", "GRAVE"))
    return pairs


def score_pair(a, b, tribe_sizes=None):
    """Compute the typed-edge interaction profile of cards a and b."""
    edges = []

    # ---------- Tribal --------------------------------------------------
    # If A references a tribe T and B is of tribe T (or vice versa).
    a_refs = set(a.get("tribes_referenced") or [])
    b_refs = set(b.get("tribes_referenced") or [])
    a_is = set(a.get("tribes_is") or [])
    b_is = set(b.get("tribes_is") or [])

    # A is a tribal-payoff for tribe T, B is of tribe T
    for t in a_refs & b_is:
        size = (tribe_sizes or {}).get(t, 100)
        weight = TRIBAL_EDGE_BASE / max(1.0, (size / 100) ** 0.5)
        edges.append({"type": "tribal", "tribe": t, "direction": "a_pays_b", "weight": weight})

    for t in b_refs & a_is:
        size = (tribe_sizes or {}).get(t, 100)
        weight = TRIBAL_EDGE_BASE / max(1.0, (size / 100) ** 0.5)
        edges.append({"type": "tribal", "tribe": t, "direction": "b_pays_a", "weight": weight})

    # Both are tribe T and at least one references it — "lord effect"
    for t in a_is & b_is:
        if t in a_refs or t in b_refs:
            size = (tribe_sizes or {}).get(t, 100)
            weight = TRIBAL_LORD_BONUS / max(1.0, (size / 100) ** 0.5)
            edges.append({"type": "tribal_lord", "tribe": t, "weight": weight})

    # ---------- Mechanic overlap ----------------------------------------
    a_prod = set(a.get("mechanics_produces") or [])
    a_cares = set(a.get("mechanics_cares") or [])
    b_prod = set(b.get("mechanics_produces") or [])
    b_cares = set(b.get("mechanics_cares") or [])

    # Implicit type cares: a card that IS an artifact "cares" about
    # artifacts_matter even if its oracle text doesn't say so. This makes
    # Urza, Lord High Artificer (produces artifacts_matter via "artifact you
    # control" text) pair with Skullclamp (an artifact whose text doesn't
    # mention the word "artifact"). Niche interactions like Urza+Equipment
    # would otherwise be missed.
    TYPE_FLAG_CARES = {
        "artifacts_matter":          "is_artifact",
        "enchantments_matter":       "is_enchantment",
        "creatures_matter":          "is_creature",
        "planeswalkers_matter":      "is_planeswalker",
        # Concrete resource use — A taps this type for mana / damage / etc.
        "artifacts_as_resource":     "is_artifact",
        "creatures_as_resource":     "is_creature",
        # Cost reduction — A makes spells of this type cheaper.
        "cost_reduction_creatures":  "is_creature",
        "cost_reduction_artifacts":  "is_artifact",
    }
    SUBTYPE_CARES = {
        "equipment_matters":  "Equipment",
        "auras_matter":       "Aura",
        "vehicles_matter":    "Vehicle",
    }
    for tag, flag in TYPE_FLAG_CARES.items():
        if a.get(flag, False):
            a_cares.add(tag)
        if b.get(flag, False):
            b_cares.add(tag)
    for tag, sub in SUBTYPE_CARES.items():
        if sub in (a.get("subtypes") or []):
            a_cares.add(tag)
        if sub in (b.get("subtypes") or []):
            b_cares.add(tag)
    # Lands-as-resource is for anything the deck plays as a land.
    if "LAND" in (a.get("card_types") or []):
        a_cares.add("lands_as_resource")
    if "LAND" in (b.get("card_types") or []):
        b_cares.add("lands_as_resource")

    # Tighter ("concrete resource use") tags get a heavier weight because the
    # interaction is a real engine, not just abstract caring.
    HEAVY_TAGS = {
        "artifacts_as_resource", "creatures_as_resource", "lands_as_resource",
        "cost_reduction_artifacts", "cost_reduction_creatures",
        "untap_target_payoff", "trigger_doubler",
        "scry_surveil_payoff", "equipped_attacks_payoff",
    }

    def _edge_weight(tag):
        return MECH_EDGE_WEIGHT * 2 if tag in HEAVY_TAGS else MECH_EDGE_WEIGHT

    # Tags that represent "interchangeable tools" — having two of them
    # doesn't synergize, it's redundancy. Counterspells, removal, tutors,
    # protection are deck slots, not engine pieces. Skip resource_flow and
    # mechanic_overlap edges when both sides share one of these tags as
    # producer (two counterspells, two protection spells, etc.).
    REDUNDANT_TAGS = {
        "counter_spells",
        "discard",       # discard outlets are usually 1-of pieces
        "protection",    # protection effects don't stack
        "mana_ritual",   # ritual chains exist but each ritual is a discrete spell
        # Trigger-condition tags — each card's trigger fires when ITS card
        # enters/dies/attacks. Two cards with the same condition don't pair
        # with each other; they each pair with enablers (blinkers, doublers).
        "etb_trigger",
        "dies_trigger_payoff",
        "attack_trigger",
        "blink_flicker",
        "sacrifice_creature",
        "sacrifice_artifact",
        "damage_target",
    }

    # Cleanest signal: A produces X, B cares about X. But skip redundancy
    # tags when BOTH sides also produce the tag — that's two tools of the
    # same kind, not a producer/consumer relation. (e.g. two counterspells
    # both have "counter target" so the cares regex matches them both, but
    # they don't synergize with each other.)
    for tag in a_prod & b_cares:
        if tag in REDUNDANT_TAGS and tag in b_prod:
            continue
        edges.append({"type": "resource_flow", "tag": tag, "direction": "a_to_b", "weight": _edge_weight(tag)})
    for tag in b_prod & a_cares:
        if tag in REDUNDANT_TAGS and tag in a_prod:
            continue
        edges.append({"type": "resource_flow", "tag": tag, "direction": "b_to_a", "weight": _edge_weight(tag)})

    # Bidirectional overlap (both are in the same mechanic family even if no clear flow).
    overlap_both_prod = a_prod & b_prod & BIDIRECTIONAL_TAGS
    for tag in overlap_both_prod:
        # Don't double-count if already captured by resource_flow above
        if tag in a_prod and tag in b_cares:
            continue
        if tag in b_prod and tag in a_cares:
            continue
        edges.append({"type": "mechanic_overlap", "tag": tag, "weight": MECH_EDGE_WEIGHT * 0.6})

    # ---------- Amplifier edges — Doubling Season, Hardened Scales, Panharmonicon
    # Amplifiers double / multiply a resource the rest of the deck produces.
    # They're deck-defining — much more impactful than a commodity producer
    # — so the edge gets a heavy weight. Fires symmetrically.
    AMP_PAIRS = [
        # (amplifier_flag, partner_must_have_in_produces)
        ("is_amp_tokens",   "tokens_creature"),
        ("is_amp_counters", "p1p1_counters"),
        ("is_amp_etb",      "etb_trigger"),
        ("is_amp_dies",     "dies_trigger_payoff"),
        ("is_amp_dies",     "death_drain"),
        ("is_amp_dies",     "sacrifice_creature"),  # outlets cause deaths
        ("is_amp_dies",     "free_sac_outlet"),
        ("is_amp_mana",     "mana"),
        ("is_amp_mana",     "ramp_mana"),
        ("is_amp_damage",   "damage_target"),
        ("is_amp_damage",   "damage"),
        ("is_amp_lifegain", "lifegain"),
        ("is_amp_draw",     "draw_cards"),
    ]
    seen_amp = set()
    for amp_flag, partner_tag in AMP_PAIRS:
        # Avoid double-counting when the same amp flag matches multiple tags
        if a.get(amp_flag) and partner_tag in (b.get("mechanics_produces") or []):
            key = (amp_flag, "a")
            if key not in seen_amp:
                edges.append({"type": "amplifier", "tag": amp_flag,
                              "direction": "a_amplifies_b", "weight": 14.0})
                seen_amp.add(key)
        if b.get(amp_flag) and partner_tag in (a.get("mechanics_produces") or []):
            key = (amp_flag, "b")
            if key not in seen_amp:
                edges.append({"type": "amplifier", "tag": amp_flag,
                              "direction": "b_amplifies_a", "weight": 14.0})
                seen_amp.add(key)
    # NOTE: we used to also fire is_amp_mana edges between a mana doubler
    # and every individual land that produces colored mana. That created
    # absurd scores during auto-build — Mana Reflection got 14 points
    # against every single dual / shock / triome in the deck, totalling
    # 400+ from the mana base alone, which crowded out the commander's
    # actual theme. The edge is removed: a deck plays lands regardless,
    # so a doubler's "synergy" with every land is real on the table but
    # not a useful signal for "should this card be in the deck?".  The
    # AMP_PAIRS loop above still pairs is_amp_mana cards with explicit
    # ramp_mana/mana producers (rocks, dorks, treasure-makers, X-spell
    # payoffs), which is the legitimate use.

    # ---------- Self-restricted subtype amplifiers (Cloud / etc.) ------
    # A card that doubles triggered abilities of an attached subtype
    # (Equipment, Aura, Vehicle) pairs strongly with cards of that subtype
    # that have triggered abilities. Generic — applies to Cloud + Sword
    # cycle, Cloud + Hammer of Nazahn, any future "equip-trigger doubler"
    # commander, plus the Aura / Vehicle variants.
    SELF_AMP_PAIRS = [
        ("self_amp_equipment_triggers", "Equipment"),
        ("self_amp_aura_triggers",      "Aura"),
        ("self_amp_vehicle_triggers",   "Vehicle"),
    ]

    def _has_any_trigger(card):
        """Heuristic: does this card have any triggered ability at all?
        Cloud-style amps double EVERY triggered ability of the equipped piece
        — ETB triggers, attack triggers, damage triggers, dies triggers
        (Skullclamp), 'becomes attached' triggers, etc. So any 'When' or
        'Whenever' in oracle text counts."""
        txt = (card.get("oracle_text") or "").lower()
        return ("whenever" in txt) or bool(re.search(r"\bwhen\b", txt))

    for amp_flag, subtype in SELF_AMP_PAIRS:
        # Pair an A-side amp card with a B-side that's of the restricted
        # subtype AND has any triggered ability. Symmetric in both directions.
        # Weight 16 base — higher than universal amps (14) because the
        # restricted partner is exactly the right partner. Bonus +8 if the
        # partner has a *recurring combat trigger* (Sword cycle's "whenever
        # equipped creature deals combat damage" / Jitte's counter triggers
        # / etc.) because those fire each turn cycle, not just once at ETB.
        for amp_holder, partner in ((a, b), (b, a)):
            if not amp_holder.get(amp_flag):
                continue
            if subtype not in (partner.get("subtypes") or []):
                continue
            if not _has_any_trigger(partner):
                continue
            base = 16.0
            partner_prods = partner.get("mechanics_produces") or []
            # Recurring triggers — combat damage / attack triggers /
            # death triggers on the attached card. Cloud + Skullclamp
            # doubles the "draw 2 on equipped creature dying" so each
            # death = 4 cards.
            if any(t in partner_prods for t in (
                "equipped_attacks_payoff", "attack_trigger", "dies_trigger_payoff",
            )):
                base += 8.0
            edges.append({"type": "amplifier", "tag": amp_flag, "weight": base})

    # ---------- Archetype-specific edges (worth ~10pts each) ----------
    # Aristocrats — death-drain payoff + free sac outlet is the engine.
    # Drain pays off any death; outlet manufactures deaths on demand.
    if a.get("is_death_drain") and (b.get("is_free_sac_outlet") or b.get("flags", {}).get("has_persist") or b.get("flags", {}).get("has_undying")):
        edges.append({"type": "archetype", "tag": "aristocrats_drain_outlet", "weight": 10.0})
    if b.get("is_death_drain") and (a.get("is_free_sac_outlet") or a.get("flags", {}).get("has_persist") or a.get("flags", {}).get("has_undying")):
        edges.append({"type": "archetype", "tag": "aristocrats_drain_outlet", "weight": 10.0})

    # Free sac outlet + persist/undying = infinite if there's a counter
    # remover, but worth flagging for any persistent-creature strategy.
    if a.get("is_free_sac_outlet") and (b.get("flags", {}).get("has_persist") or b.get("flags", {}).get("has_undying")):
        edges.append({"type": "archetype", "tag": "sac_persist", "weight": 10.0})
    if b.get("is_free_sac_outlet") and (a.get("flags", {}).get("has_persist") or a.get("flags", {}).get("has_undying")):
        edges.append({"type": "archetype", "tag": "sac_persist", "weight": 10.0})

    # Protection — pairs with creature-focused commanders. The deck-side
    # heuristic is "this card is a creature" or "is_creature commander".
    if a.get("is_protection") and (b.get("is_creature") or b.get("is_planeswalker")):
        edges.append({"type": "archetype", "tag": "protection_for_creature", "weight": 6.0})
    if b.get("is_protection") and (a.get("is_creature") or a.get("is_planeswalker")):
        edges.append({"type": "archetype", "tag": "protection_for_creature", "weight": 6.0})

    # Mana ritual — pairs with X-cost spells, storm, expensive payoffs.
    # Heuristic: B has high cmc (>= 5) OR mentions storm/spellslinger.
    if a.get("is_mana_ritual") and (b.get("cmc", 0) >= 5 or "spellslinger" in (b.get("mechanics_produces") or [])):
        edges.append({"type": "archetype", "tag": "ritual_pays_off", "weight": 8.0})
    if b.get("is_mana_ritual") and (a.get("cmc", 0) >= 5 or "spellslinger" in (a.get("mechanics_produces") or [])):
        edges.append({"type": "archetype", "tag": "ritual_pays_off", "weight": 8.0})

    # ---------- Trigger chain — A triggers on event E, B causes E. ------
    # Stronger than mere mechanic_overlap because the engine effect closes
    # a loop. E.g. Niv-Mizzet (whenever you draw → ping) + Curiosity
    # (deal damage → draw a card). Each matched chain adds 8 points,
    # symmetric in both directions.
    TRIGGER_CHAIN_PAIRS = [
        # (a triggers on, b produces) -> tag
        ("draws_card",        "draw"),
        ("etb_self",          "token"),         # ETB-payoff + token-maker
        ("creature_dies",     "sac"),           # death-trigger payoff + sac outlet
        ("creature_dies",     "free_sac_outlet"),
        ("lifegain",          "lifegain"),      # any lifegain → lifegain trigger
        ("spell_cast_self",   "spellcast"),     # spellslinger trigger + spell castable
        ("attacks_self",      "extra_combat"),  # attack-trigger + extra combat
        ("attacks_self",      "goad"),          # attack-trigger + goad
        ("blocks_self",       "extra_combat"),  # block-trigger + extra combat
        ("deals_damage",      "extra_combat"),  # damage-trigger + extra combat
        ("taps",              "untap"),         # tap-trigger + untap effect
        ("sacrifices",        "free_sac_outlet"),
    ]
    a_triggers = set(a.get("triggers") or [])
    b_triggers = set(b.get("triggers") or [])
    a_prod_full = set(a.get("mechanics_produces") or []) | set(a.get("produces") or [])
    b_prod_full = set(b.get("mechanics_produces") or []) | set(b.get("produces") or [])
    # Slim-fit to the "draw_cards" tag from the profiler.
    if "draw_cards" in a.get("mechanics_cares") or []:
        a_triggers.add("draws_card")
    if "draw_cards" in b.get("mechanics_cares") or []:
        b_triggers.add("draws_card")
    if "draw_cards" in a.get("mechanics_produces") or []:
        a_prod_full.add("draw")
    if "draw_cards" in b.get("mechanics_produces") or []:
        b_prod_full.add("draw")

    chain_pts = 8.0
    for trig, prod in TRIGGER_CHAIN_PAIRS:
        if trig in a_triggers and prod in b_prod_full:
            edges.append({"type": "trigger_chain", "tag": f"{trig}<-{prod}",
                          "direction": "a_listens_b_emits", "weight": chain_pts})
        if trig in b_triggers and prod in a_prod_full:
            edges.append({"type": "trigger_chain", "tag": f"{trig}<-{prod}",
                          "direction": "b_listens_a_emits", "weight": chain_pts})

    # ---------- Subtype cost reduction (Slinza pattern) -----------------
    # If A reduces costs for tribe T, every member of tribe T in the deck
    # gets a real in-game discount. Worth a heavy edge per matched member
    # — distinct from generic tribal because cost reduction is concrete,
    # repeatable, and stacks across the deck.
    a_subtype_cr = set(a.get("subtype_cost_reduction") or [])
    b_subtype_cr = set(b.get("subtype_cost_reduction") or [])
    a_is = set(a.get("tribes_is") or [])
    b_is_set = set(b.get("tribes_is") or [])
    for tribe in a_subtype_cr & b_is_set:
        edges.append({"type": "subtype_cost_reduction", "tag": f"discount_{tribe}",
                      "tribe": tribe, "weight": 12.0})
    for tribe in b_subtype_cr & a_is:
        edges.append({"type": "subtype_cost_reduction", "tag": f"discount_{tribe}",
                      "tribe": tribe, "weight": 12.0})

    # ---------- Designed pairs (Partner / Meld) — massive edge ----------
    # When two cards are designed to be played together (Pir + Toothy
    # named partners, Bruna + Gisela meld pieces), they should appear at
    # the very top of each other's suggestion list. Worth 1000 points so
    # nothing else competes with them.
    a_designed_pairs = set((a.get("partner_pairs") or []) + (a.get("meld_pairs") or []))
    b_designed_pairs = set((b.get("partner_pairs") or []) + (b.get("meld_pairs") or []))
    if b.get("name") in a_designed_pairs or a.get("name") in b_designed_pairs:
        edges.append({"type": "designed_pair", "tag": "named_partner_or_meld", "weight": 1000.0})

    # ---------- Combo (very strong) — symmetric ------------------------
    # Each combo pattern has an a-role and b-role. We try both orderings so
    # that score_pair(X, Y) == score_pair(Y, X) for combos.
    def _combo_match(role_a, role_b):
        af, bf = role_a["flags"], role_b["flags"]
        out = []
        # Heliod + Ballista — needs lifelink from either side
        if af.get("lifegain_p1p1_target") and bf.get("p1p1_removal_damage"):
            if af.get("grants_lifelink") or bf.get("grants_lifelink"):
                out.append("lifelink_p1p1")
        if af.get("creates_copy_with_tap") and bf.get("untap_creature_on_etb"):
            out.append("kiki_twin")
        if af.get("cheap_self_blink") and bf.get("etb_produces_mana"):
            out.append("deadeye_drake")
        if af.get("wins_on_empty_library") and bf.get("mass_library_exile"):
            out.append("thoracle")
        if af.get("grants_undying") and bf.get("p1p1_removal_damage"):
            out.append("mikaeus_trike")
        if af.get("lifegain_drain_opp") and bf.get("lifeloss_opp_drain_self"):
            out.append("sanguine_exquisite")
        return out

    seen_subtypes = set()
    for sub in _combo_match(a, b) + _combo_match(b, a):
        if sub not in seen_subtypes:
            edges.append({"type": "combo", "subtype": sub, "weight": COMBO_WEIGHT})
            seen_subtypes.add(sub)

    # ---------- Zone tags ----------------------------------------------
    if edges:
        zones = zones_for_edge(None, a, b)
        for e in edges:
            e["zones"] = zones

    total = sum(e["weight"] for e in edges)
    return {"edges": edges, "total": total}


def load_cards():
    cards = {}
    with CARDS_PATH.open(encoding="utf-8") as f:
        for line in f:
            c = json.loads(line)
            cards[c["oracle_id"]] = c
    return cards


def compute_tribe_sizes(cards):
    """Count how many creatures share each tribe (for scaling tribal edges)."""
    sizes = Counter()
    for c in cards.values():
        for t in c.get("tribes_is") or []:
            sizes[t] += 1
    return sizes


# Quick self-test ------------------------------------------------------
if __name__ == "__main__":
    cards = load_cards()
    tribe_sizes = compute_tribe_sizes(cards)
    print(f"Loaded {len(cards)} cards. Tribe sizes (top 12):")
    for t, n in tribe_sizes.most_common(12):
        print(f"  {t:15s} {n}")

    name_to_id = {c["name"]: oid for oid, c in cards.items()}

    # Test pairs
    test_pairs = [
        ("Krenko, Mob Boss", "Goblin King"),
        ("Krenko, Mob Boss", "Skirk Prospector"),
        ("Krenko, Mob Boss", "Lightning Bolt"),
        ("Krenko, Mob Boss", "Sol Ring"),
        ("Krenko, Mob Boss", "Counterspell"),  # color identity violation, edge still computed
        ("Atraxa, Praetors' Voice", "Doubling Season"),
        ("Heliod, Sun-Crowned", "Walking Ballista"),
        ("Splinter Twin", "Pestermite"),
    ]
    print("\nSample pair scores:")
    for an, bn in test_pairs:
        a_id = name_to_id.get(an)
        b_id = name_to_id.get(bn)
        if not a_id or not b_id:
            print(f"  ?? {an} or {bn} not found")
            continue
        result = score_pair(cards[a_id], cards[b_id], tribe_sizes)
        print(f"  {an:32s} + {bn:30s} = {result['total']:6.1f}  edges={len(result['edges'])}")
        for e in result["edges"][:4]:
            print(f"      {e}")
