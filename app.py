#!/usr/bin/env python3
"""
Local Flask deckbuilder for MTG Commander.

Usage:
    python3 app.py

Then open http://localhost:5000 in a browser.

Workflow:
1. Pick a commander.
2. The app filters all Commander-legal cards within the commander's color
   identity, scores each candidate's interaction strength with the current
   deck, and shows the top suggestions with images.
3. Click 'Add' on any suggestion to put it in the deck. The suggestion list
   re-ranks based on the new deck composition.
4. Click 'Remove' on any deck card to take it out.
5. Click 'Reset' to start over.

State is in-memory (single-user). Restart of the server clears the deck.
"""

import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path
from flask import Flask, jsonify, render_template_string, request, redirect, url_for

from interactions import score_pair, compute_tribe_sizes

# Resolve the directory containing the bundled data files (cards.jsonl,
# xmage_cards.txt, the seed pair_cache.pkl.gz, etc.).
#
# When run as a normal Python script, that's just `Path(__file__).parent`.
# When run as a PyInstaller-frozen executable, `__file__` points inside
# the unpacked _internal/ directory, NOT next to the .exe. The Electron
# build script stages the data files next to the executable itself, so
# we use `Path(sys.executable).parent` in frozen mode.
if getattr(sys, "frozen", False):
    ROOT = Path(sys.executable).parent
else:
    ROOT = Path(__file__).parent

# ---------- Data directory --------------------------------------------
# Read-only bundled assets (cards.jsonl, oracle-cards-*.json, xmage_cards.txt,
# etc.) live alongside this script. User-writable state — pair_cache.pkl.gz,
# personal xmage_excluded.txt overrides, etc. — goes in a separate
# directory so a packaged Electron app can store them in the per-user
# data location while keeping the bundle resources read-only.
#
# When DECKBUILDER_DATA_DIR is set (Electron sets it to app.getPath
# ('userData')), writable state goes there; otherwise everything stays
# next to the script for the standalone-Python use case.
_data_dir_env = os.environ.get("DECKBUILDER_DATA_DIR")
DATA_DIR = Path(_data_dir_env) if _data_dir_env else ROOT
DATA_DIR.mkdir(parents=True, exist_ok=True)

# ---------- Optional XMage card-pool intersection ---------------------
# When the user toggles XMage mode, we restrict the candidate pool to
# cards XMage actually implements. We discover those by scanning the
# Mage.Sets card-source tree for class names and converting them to a
# Scryfall-comparable form (strip punctuation/whitespace, case-fold).
# If the XMage source isn't reachable, we skip the toggle silently.
import os as _os, re as _re_xm

def _normalize_for_xmage(name):
    """Strip whitespace + punctuation so 'Heliod, Sun-Crowned' matches
    'HeliodSunCrowned'. For split cards / fuse / aftermath, XMage uses the
    concatenated form (e.g. Heaven // Earth → HeavenEarth.java), so we
    keep both faces. For transform / MDFC cards, XMage typically uses the
    front face's name (e.g. Delver of Secrets // Insectile Aberration →
    DelverOfSecrets.java), so we try the front face if the concatenated
    form misses, but the caller is responsible for that fallback."""
    if not name:
        return ""
    # Drop the // separator but keep both halves' letters.
    s = _re_xm.sub(r"[^A-Za-z0-9]", "", name)
    return s.lower()


def _xmage_match(name, xmage_set):
    """Check membership with both the concatenated form (split cards) and
    the front-face-only form (transform / MDFC). XMage uses one or the
    other depending on the card type, so we try both."""
    if name is None or xmage_set is None:
        return False
    full = _normalize_for_xmage(name)
    if full in xmage_set:
        return True
    if "//" in name:
        front = _re_xm.sub(r"[^A-Za-z0-9]", "", name.split("//")[0]).lower()
        if front and front in xmage_set:
            return True
    return False


def _read_normalized_name_file(path):
    """Read a one-name-per-line file, ignore '#' comments and blank lines,
    and return a set of normalized names (lowercase, alphanumerics only).
    Lines that already look normalized pass through unchanged; lines that
    look like display names ('Knowledge Seeker') get normalized on read so
    a user can paste either form into the file."""
    out = set()
    try:
        with open(path, encoding="utf-8") as f:
            for raw in f:
                line = raw.split("#", 1)[0].strip()
                if not line:
                    continue
                norm = _normalize_for_xmage(line)
                if norm:
                    out.add(norm)
    except OSError:
        pass
    return out


def _build_xmage_name_set():
    """Resolve the XMage card-name set in two ways, in priority order:

    1. A bundled `xmage_cards.txt` next to this script — one normalized
       name per line. This is what ships with the deckbuilder folder so
       XMage mode works out of the box on any machine.
    2. A live walk of `Mage.Sets/src/mage/cards/` if the user happens to
       have the XMage source tree locally — useful for developers who
       want fresh data ahead of the next bundled refresh.

    After loading the inclusion list, we subtract any name listed in
    `xmage_excluded.txt` (also next to this script). This is a personal
    delta-list — useful when the user's installed XMage build is older
    than the source tree, so cards from the newest sets are technically
    in the source but not yet in the binary they're playing on. The user
    edits this file to suppress cards their XMage rejects on import.

    Returns None if neither inclusion source is available.
    """
    # ROOT is PyInstaller-aware (resolves to Path(sys.executable).parent
    # in frozen mode), so the bundled xmage_cards.txt that the Electron
    # build script stages next to deckbuilder-server.exe is found here.
    here = str(ROOT)
    names = None

    bundled = _os.path.join(here, "xmage_cards.txt")
    if _os.path.isfile(bundled):
        try:
            with open(bundled, encoding="utf-8") as f:
                names = {line.strip().lower() for line in f if line.strip()}
        except OSError:
            names = None

    if not names:
        candidates = [
            _os.path.join(here, "..", "Mage.Sets", "src", "mage", "cards"),
            "/sessions/wizardly-funny-fermat/mnt/mage-master/Mage.Sets/src/mage/cards",
            _os.path.expanduser("~/Downloads/mage-master/mage-master/Mage.Sets/src/mage/cards"),
            _os.path.expanduser("~/mage-master/Mage.Sets/src/mage/cards"),
        ]
        root = next((p for p in candidates if _os.path.isdir(p)), None)
        if root is None:
            return None
        names = set()
        for sub in _os.listdir(root):
            d = _os.path.join(root, sub)
            if not _os.path.isdir(d):
                continue
            for fn in _os.listdir(d):
                if fn.endswith(".java"):
                    names.add(fn[:-5].lower())

    if not names:
        return None

    # Subtract personal exclusions (cards the user's XMage build rejects
    # on import even though the source tree has them).  The user-data
    # directory takes precedence so a packaged desktop app can let users
    # edit it without touching the read-only bundle resources.
    excluded_paths = []
    user_excl = DATA_DIR / "xmage_excluded.txt"
    bundle_excl = ROOT / "xmage_excluded.txt"
    if user_excl.exists():
        excluded_paths.append(str(user_excl))
    if bundle_excl.exists() and bundle_excl != user_excl:
        excluded_paths.append(str(bundle_excl))
    excluded = set()
    for p in excluded_paths:
        excluded |= _read_normalized_name_file(p)
    if excluded:
        before = len(names)
        names -= excluded
        removed = before - len(names)
        if removed:
            print(f"  XMage exclusion list applied — {removed} cards removed via xmage_excluded.txt.")

    return names

XMAGE_NAME_SET = _build_xmage_name_set()
if XMAGE_NAME_SET:
    print(f"  XMage mode available — {len(XMAGE_NAME_SET)} card classes loaded.")
else:
    print("  XMage source not found; XMage mode disabled.")


# ---------- Load card data once at startup ----------------------------
print("Loading cards...")
CARDS = {}            # oracle_id -> profile
NAME_TO_ID = {}       # lower-case name -> oracle_id
COMMANDER_OPTIONS = []  # list of {name, oracle_id, image_small, type_line, color_identity}
with (ROOT / "cards.jsonl").open(encoding="utf-8") as f:
    for line in f:
        c = json.loads(line)
        if c["oracle_id"] is None:
            continue
        CARDS[c["oracle_id"]] = c
        NAME_TO_ID[c["name"].lower()] = c["oracle_id"]
        if c["is_commander_eligible"] and c["commander_legal"]:
            COMMANDER_OPTIONS.append({
                "name": c["name"],
                "oracle_id": c["oracle_id"],
                "image_small": c["image_small"],
                "type_line": c["type_line"],
                "color_identity": c["color_identity"],
            })
COMMANDER_OPTIONS.sort(key=lambda c: c["name"])
TRIBE_SIZES = compute_tribe_sizes(CARDS)
print(f"  {len(CARDS)} cards, {len(COMMANDER_OPTIONS)} commander-eligible.")

# ---------- App state -------------------------------------------------
BASIC_LANDS = [
    "Plains", "Island", "Swamp", "Mountain", "Forest", "Wastes",
    "Snow-Covered Plains", "Snow-Covered Island", "Snow-Covered Swamp",
    "Snow-Covered Mountain", "Snow-Covered Forest", "Snow-Covered Wastes",
]
BASIC_COLOR = {
    "Plains": "W", "Island": "U", "Swamp": "B",
    "Mountain": "R", "Forest": "G", "Wastes": "C",
    "Snow-Covered Plains": "W", "Snow-Covered Island": "U",
    "Snow-Covered Swamp": "B", "Snow-Covered Mountain": "R",
    "Snow-Covered Forest": "G", "Snow-Covered Wastes": "C",
}
TYPE_FILTERS = ["All", "Ramp", "Creature", "Artifact", "Enchantment", "Instant",
                "Sorcery", "Planeswalker", "Land", "Battle"]

STATE = {
    "commander_id": None,
    "deck_ids": [],   # ordered list of oracle_ids (excluding commander)
    "basics": {b: 0 for b in BASIC_LANDS},
    "filter_type": "All",
    "max_bracket": 4,           # Open by default
    "weights": {
        "tribal": 1.0,
        "mechanic": 1.0,
        "combo": 1.0,
        "trigger_chain": 1.0,
    },
    "commander_weight": 2.0,
    "xmage_only": False,
}

BRACKETS = {
    1: "Casual",
    2: "Mid-power",
    3: "High-power",
    4: "Optimized",
}

DECK_TARGET_SIZE = 100  # commander + 99

# ---------- Auto-include staples ---------------------------------------
# When auto-build runs, these are added before greedy ranking starts —
# IF they're CI-legal for the chosen commander.  The list is split into
# "always-include" (mana rocks and colorless utility lands that fit any
# deck) and "color-fixing lands" (only useful when the commander has
# more than one color).  Frequencies are pulled from the user's own
# deck collection: Sol Ring 88%, Arcane Signet 57% in multi-color, etc.
#
# The lists are intentionally short — these are the cards that virtually
# every Commander deck wants regardless of strategy.  Strategy-specific
# cards still come from the synergy ranker.
STAPLE_MANA_ROCKS_ANY = [
    "Sol Ring",            # 88% of decks — uncontested auto-include
    "Mind Stone",          # cheap 2-mana + cantrip
    "Thought Vessel",      # cheap 2-mana + no max hand
]
STAPLE_MANA_ROCKS_MULTI = [
    "Arcane Signet",       # 57% — multi-color only
    "Fellwar Stone",       # 20% — cheap multi-color fixing
    "Commander's Sphere",  # 29% — CI-fixing, sac for draw
]
STAPLE_LANDS_COLORLESS = [
    "Reliquary Tower",     # 24% — no max hand size
    "Rogue's Passage",     # 24% — unblockable utility
]
# NOTE: Ash Barrens is NOT a staple. It's a colorless utility with a
# one-shot emergency cycle, not real fixing.
STAPLE_LANDS_MULTI = [
    "Command Tower",       # 59% — every multi-color deck wants this
    "Exotic Orchard",      # 39% — usually fine in multi-color metas
    "Reflecting Pool",     # universal in any deck with diverse mana
]
STAPLE_LANDS_3PLUS = [
    "Path of Ancestry",    # 35% — strong in 3C+ (any-tribe scry)
]

# CI-specific cycle staples — auto-build will pick the cycle members that
# match the commander's CI in addition to the generic staples above.
# Each entry maps a frozenset of color letters to a land name.

# Original duals (Tundra cycle) — top-tier untapped duals.
ORIGINAL_DUALS = {
    frozenset("WU"): "Tundra",
    frozenset("UB"): "Underground Sea",
    frozenset("BR"): "Badlands",
    frozenset("RG"): "Taiga",
    frozenset("GW"): "Savannah",
    frozenset("WB"): "Scrubland",
    frozenset("UR"): "Volcanic Island",
    frozenset("BG"): "Bayou",
    frozenset("RW"): "Plateau",
    frozenset("UG"): "Tropical Island",
}
# Shock lands (Ravnica) — pay 2 life to enter untapped.
SHOCK_LANDS = {
    frozenset("WU"): "Hallowed Fountain",
    frozenset("UB"): "Watery Grave",
    frozenset("BR"): "Blood Crypt",
    frozenset("RG"): "Stomping Ground",
    frozenset("GW"): "Temple Garden",
    frozenset("WB"): "Godless Shrine",
    frozenset("UR"): "Steam Vents",
    frozenset("BG"): "Overgrown Tomb",
    frozenset("RW"): "Sacred Foundry",
    frozenset("UG"): "Breeding Pool",
}
# Check lands (Innistrad/M10) — untapped if you control matching basic.
CHECK_LANDS = {
    frozenset("WU"): "Glacial Fortress",
    frozenset("UB"): "Drowned Catacomb",
    frozenset("BR"): "Dragonskull Summit",
    frozenset("RG"): "Rootbound Crag",
    frozenset("GW"): "Sunpetal Grove",
    frozenset("WB"): "Isolated Chapel",
    frozenset("UR"): "Sulfur Falls",
    frozenset("BG"): "Woodland Cemetery",
    frozenset("RW"): "Clifftop Retreat",
    frozenset("UG"): "Hinterland Harbor",
}
# Battlebond lands — untapped if you have ≥2 opponents (always true in EDH).
BATTLEBOND_LANDS = {
    frozenset("WU"): "Sea of Clouds",
    frozenset("UB"): "Morphic Pool",
    frozenset("BR"): "Luxury Suite",
    frozenset("RG"): "Spire Garden",
    frozenset("GW"): "Bountiful Promenade",
    frozenset("WB"): "Vault of Champions",
    frozenset("UR"): "Training Center",
    frozenset("BG"): "Undergrowth Stadium",
    frozenset("RW"): "Spectator Seating",
    frozenset("UG"): "Rejuvenating Springs",
}
# Modern triomes (Ikoria/SNC) — 3-color cycling tapped lands.
TRIOMES = {
    frozenset("GWB"): "Indatha Triome",
    frozenset("GUR"): "Ketria Triome",
    frozenset("URW"): "Raugrin Triome",
    frozenset("RWB"): "Savai Triome",
    frozenset("BGU"): "Zagoth Triome",
    frozenset("GWU"): "Spara's Headquarters",
    frozenset("WUB"): "Raffine's Tower",
    frozenset("UBR"): "Xander's Lounge",
    frozenset("BRG"): "Ziatora's Proving Ground",
    frozenset("RGW"): "Jetmir's Garden",
}
# Slow lands (Innistrad: Midnight Hunt / Crimson Vow) — untapped if you
# control 2+ other lands.  Bad turn 1, fine after.
SLOW_LANDS = {
    frozenset("WU"): "Deserted Beach",
    frozenset("UB"): "Shipwreck Marsh",
    frozenset("BR"): "Haunted Ridge",
    frozenset("RG"): "Rockfall Vale",
    frozenset("GW"): "Overgrown Farmland",
    frozenset("WB"): "Shattered Sanctum",
    frozenset("UR"): "Stormcarved Coast",
    frozenset("BG"): "Deathcap Glade",
    frozenset("RW"): "Sundown Pass",
    frozenset("UG"): "Dreamroot Cascade",
}
# Surveil lands (Murders at Karlov Manor) — always tapped, ETB surveil 1.
SURVEIL_LANDS = {
    frozenset("WU"): "Meticulous Archive",
    frozenset("UB"): "Undercity Sewers",
    frozenset("BR"): "Raucous Theater",
    frozenset("RG"): "Commercial District",
    frozenset("GW"): "Lush Portico",
    frozenset("WB"): "Shadowy Backstreet",
    frozenset("UR"): "Thundering Falls",
    frozenset("BG"): "Underground Mortuary",
    frozenset("RW"): "Elegant Parlor",
    frozenset("UG"): "Hedge Maze",
}
# Bounce lands (Ravnica) — ETB tapped, return a land to hand.
BOUNCE_LANDS = {
    frozenset("WU"): "Azorius Chancery",
    frozenset("UB"): "Dimir Aqueduct",
    frozenset("BR"): "Rakdos Carnarium",
    frozenset("RG"): "Gruul Turf",
    frozenset("GW"): "Selesnya Sanctuary",
    frozenset("WB"): "Orzhov Basilica",
    frozenset("UR"): "Izzet Boilerworks",
    frozenset("BG"): "Golgari Rot Farm",
    frozenset("RW"): "Boros Garrison",
    frozenset("UG"): "Simic Growth Chamber",
}
# Filter lands (Shadowmoor / Eventide) — produce filtered hybrid mana.
FILTER_LANDS = {
    frozenset("WU"): "Mystic Gate",
    frozenset("UB"): "Sunken Ruins",
    frozenset("BR"): "Graven Cairns",
    frozenset("RG"): "Fire-Lit Thicket",
    frozenset("GW"): "Wooded Bastion",
    frozenset("WB"): "Fetid Heath",
    frozenset("UR"): "Cascade Bluffs",
    frozenset("BG"): "Twilight Mire",
    frozenset("RW"): "Rugged Prairie",
    frozenset("UG"): "Flooded Grove",
}


def cycle_lands_for_ci(cmd_ci):
    """Return a list of CI-fitting cycle staples, weighted by power tier.

    Higher-quality cycles (originals, battlebonds, shocks) get more
    representatives than lower-quality ones (surveil, bounce, filter).
    For 5C this yields ≈7 originals + 5 battlebonds + 3 triomes + 5
    shocks + 3 checks + a few of the rest, which matches what dedicated
    EDH players actually run rather than 10-of-one-cycle.
    """
    cs = "".join(sorted(c for c in cmd_ci if c in "WUBRG"))
    if len(cs) < 2:
        return []
    pairs = []
    for i in range(len(cs)):
        for j in range(i + 1, len(cs)):
            pairs.append(frozenset(cs[i] + cs[j]))
    triples = []
    if len(cs) >= 3:
        from itertools import combinations
        for combo in combinations(cs, 3):
            triples.append(frozenset(combo))

    # Walk schedule: (cycle table, items to iterate, max picks from this cycle)
    # Items order matches the natural pair order, which is alphabetic.
    # Picks-per-cycle are sized so a 5C cap of 16 yields 4 originals +
    # 4 battlebonds + 3 triomes + 3 shocks + 2 checks (= 16) rather than
    # blowing the whole budget on the top tier.
    schedule = [
        (ORIGINAL_DUALS,   pairs,   4),  # q=1.00 — best
        (BATTLEBOND_LANDS, pairs,   4),  # q=0.95
        (TRIOMES,          triples, 3),  # q=0.70 but 3-color
        (SHOCK_LANDS,      pairs,   4),  # q=0.85 — universally untapped
        (CHECK_LANDS,      pairs,   3),  # q=0.90 — needs basic but cheap
        (SLOW_LANDS,       pairs,   2),  # q=0.80
        (FILTER_LANDS,     pairs,   2),  # q=0.85 (hybrid)
        (SURVEIL_LANDS,    pairs,   2),  # q=0.78
        (BOUNCE_LANDS,     pairs,   2),  # q=0.75 (good for landfall)
    ]

    out = []
    seen = set()
    for table, items, max_picks in schedule:
        added = 0
        for it in items:
            if added >= max_picks:
                break
            land = table.get(it)
            if land and land not in seen:
                out.append(land)
                seen.add(land)
                added += 1
    return out


def basic_total():
    return sum(STATE["basics"].values())


# Pretty labels for mechanic/trigger tags so the synergy display is readable.
HUMAN_LABELS = {
    "p1p1_counters":            "+1/+1 counters",
    "m1m1_counters":            "-1/-1 counters",
    "tokens_creature":          "Creature tokens",
    "tokens_treasure":          "Treasure tokens",
    "tokens_food":              "Food tokens",
    "tokens_clue":              "Clue tokens",
    "tokens_blood":             "Blood tokens",
    "sacrifice_creature":       "Creature sacrifice",
    "sacrifice_artifact":       "Artifact sacrifice",
    "dies_trigger_payoff":      "Death triggers",
    "etb_trigger":              "ETB triggers",
    "spellslinger":             "Spellslinger (instants/sorceries)",
    "graveyard_payoff":         "Graveyard recursion",
    "self_mill":                "Self-mill",
    "artifacts_matter":         "Artifacts matter",
    "enchantments_matter":      "Enchantments matter",
    "creatures_matter":         "Creatures matter",
    "planeswalkers_matter":     "Planeswalkers matter",
    "equipment_matters":        "Equipment payoff",
    "auras_matter":             "Aura payoff",
    "vehicles_matter":          "Vehicle payoff",
    "landfall":                 "Landfall",
    "lifegain":                 "Life gain",
    "lifeloss_opp":             "Opponent life loss",
    "damage_target":            "Direct damage",
    "counter_spells":           "Counterspells",
    "draw_cards":               "Card draw",
    "discard":                  "Discard",
    "blink_flicker":            "Blink / flicker",
    "equipment":                "Equipment",
    "auras":                    "Auras",
    "untap":                    "Untap effects",
    "ramp_mana":                "Mana ramp",
    "attack_trigger":           "Attack triggers",
    "voltron_attached":         "Voltron / aura/equipment buffs",
    "counters_in_general":      "Counters (any kind)",
    "untap_target_payoff":      "Untap-target abilities",
    "graveyard_size_scales":    "Scales with graveyard size",
    "cost_reduction_creatures": "Cost reduction (creatures)",
    "cost_reduction_artifacts": "Cost reduction (artifacts)",
    "scry_surveil_payoff":      "Scry / surveil triggers",
    "mana_value_matters":       "Mana value matters",
    "trigger_doubler":          "Triggered-ability doublers",
    "equipped_attacks_payoff":  "Equipped-attacks triggers",
    "artifacts_as_resource":    "Taps artifacts as a resource",
    "creatures_as_resource":    "Taps creatures as a resource",
    "lands_as_resource":        "Taps lands as a resource",
    "death_drain":              "Death drain (Blood Artist class)",
    "free_sac_outlet":          "Free sacrifice outlet",
    "protection":               "Protection (indestructible / hexproof / phase)",
    "mana_ritual":              "Mana ritual",
    # Triggers
    "etb_self":                 "Enters the battlefield",
    "creature_dies":            "Creature dies",
    "spell_cast_self":          "You cast a spell",
}


def humanize_tag(tag):
    return HUMAN_LABELS.get(tag, tag.replace("_", " ").capitalize())


def commander_synergies(cmd):
    """Build a structured summary of what axes this commander operates on."""
    if not cmd:
        return None

    def label_list(tags):
        return [humanize_tag(t) for t in tags] if tags else []

    flags_on = []
    if cmd.get("is_game_changer"): flags_on.append("⚠ Game Changer")
    if cmd.get("is_tutor"):        flags_on.append("Tutor")
    if cmd.get("is_mld"):          flags_on.append("Mass Land Destruction")
    if cmd.get("is_extra_turn"):   flags_on.append("Extra turn")
    if cmd.get("is_ramp"):         flags_on.append("Ramp")
    if cmd.get("is_amp_tokens"):   flags_on.append("Token doubler")
    if cmd.get("is_amp_counters"): flags_on.append("Counter doubler")
    if cmd.get("is_amp_etb"):      flags_on.append("ETB doubler")
    if cmd.get("is_amp_dies"):     flags_on.append("Death-trigger doubler")
    if cmd.get("is_amp_mana"):     flags_on.append("Mana doubler")
    if cmd.get("is_amp_damage"):   flags_on.append("Damage doubler")
    if cmd.get("is_amp_lifegain"): flags_on.append("Lifegain doubler")
    if cmd.get("is_amp_draw"):     flags_on.append("Draw doubler")
    if cmd.get("is_death_drain"):  flags_on.append("Death drain")
    if cmd.get("is_free_sac_outlet"): flags_on.append("Free sac outlet")
    if cmd.get("is_protection"):   flags_on.append("Protection")
    if cmd.get("is_mana_ritual"):  flags_on.append("Mana ritual")

    return {
        "tribes_is":       cmd.get("tribes_is") or [],
        "tribes_ref":      cmd.get("tribes_referenced") or [],
        "triggers":        label_list(cmd.get("triggers") or []),
        "produces":        label_list(cmd.get("mechanics_produces") or []),
        "cares":           label_list(cmd.get("mechanics_cares") or []),
        "flags":           flags_on,
        "bracket":         cmd.get("card_bracket", 2),
    }


def deck_stats():
    """Compute aggregate stats for the current deck (incl. commander + basics)."""
    cmd = commander_card()
    if not cmd:
        return None
    # Type categories — first matching wins so a creature artifact counts as Creature.
    categories = {
        "Lands": 0,
        "Creatures": 0,
        "Spells": 0,        # Instants + Sorceries
        "Art/Ench": 0,      # non-creature artifacts + non-creature enchantments
        "Planeswalkers": 0,
        "Battles": 0,
    }
    cmc_sum = 0.0
    cmc_count = 0
    cards = [cmd] + deck_cards()
    for c in cards:
        types = c["card_types"]
        if "LAND" in types:
            categories["Lands"] += 1
            continue
        # CMC is conventionally averaged across non-land cards only.
        cmc_sum += c.get("cmc") or 0
        cmc_count += 1
        if "CREATURE" in types:
            categories["Creatures"] += 1
        elif "PLANESWALKER" in types:
            categories["Planeswalkers"] += 1
        elif "INSTANT" in types or "SORCERY" in types:
            categories["Spells"] += 1
        elif "ARTIFACT" in types or "ENCHANTMENT" in types:
            categories["Art/Ench"] += 1
        elif "BATTLE" in types:
            categories["Battles"] += 1
    # Basics — all lands.
    categories["Lands"] += basic_total()

    return {
        "categories": categories,
        "avg_cmc": (cmc_sum / cmc_count) if cmc_count else 0.0,
        "total": sum(categories.values()),
        "non_land_count": cmc_count,
    }


# Pie-chart slice colors (kept consistent with deck-pane mana pip palette).
PIE_COLORS = {
    "Lands":         "#8a6",
    "Creatures":     "#c54",
    "Spells":        "#6cf",
    "Art/Ench":      "#cc5",
    "Planeswalkers": "#a6c",
    "Battles":       "#c63",
}


def build_pie_slices(categories):
    """Return [{label, count, color, path, mid_angle}] for an SVG pie chart."""
    import math
    total = sum(categories.values())
    if total == 0:
        return []
    cx, cy, r = 50, 50, 45
    slices = []
    angle = -math.pi / 2  # start at top
    for label, count in categories.items():
        if count == 0:
            continue
        pct = count / total
        sweep = pct * 2 * math.pi
        start_x = cx + r * math.cos(angle)
        start_y = cy + r * math.sin(angle)
        angle += sweep
        end_x = cx + r * math.cos(angle)
        end_y = cy + r * math.sin(angle)
        large = 1 if sweep > math.pi else 0
        if pct >= 0.999:
            # Single full slice — render as a circle path.
            path = f"M {cx-r} {cy} a {r} {r} 0 1 0 {2*r} 0 a {r} {r} 0 1 0 {-2*r} 0 Z"
        else:
            path = f"M {cx} {cy} L {start_x:.2f} {start_y:.2f} A {r} {r} 0 {large} 1 {end_x:.2f} {end_y:.2f} Z"
        slices.append({
            "label": label,
            "count": count,
            "color": PIE_COLORS.get(label, "#888"),
            "path": path,
            "pct": pct * 100,
        })
    return slices


def deck_size():
    return (1 if STATE["commander_id"] else 0) + len(STATE["deck_ids"]) + basic_total()


def deck_full():
    return deck_size() >= DECK_TARGET_SIZE


def commander_card():
    if not STATE["commander_id"]:
        return None
    return CARDS.get(STATE["commander_id"])


def deck_cards():
    return [CARDS[oid] for oid in STATE["deck_ids"] if oid in CARDS]


def color_identity_subset(cand_ci, cmd_ci):
    """Candidate's color identity must be a subset of the commander's."""
    return set(cand_ci).issubset(set(cmd_ci))


# Static-pool cache so we don't re-scan all 33k cards on every rank call
# during auto-build. The static slice depends only on the commander and
# the user's filter / bracket / XMage settings — *not* on deck contents,
# which change every iteration.
_POOL_CACHE = {"key": None, "pool": []}


def candidate_pool():
    """All cards eligible for the current deck (filtered by type filter and excluding basics)."""
    cmd = commander_card()
    if not cmd:
        return []
    type_filter = STATE.get("filter_type", "All")
    max_bracket = STATE.get("max_bracket", 4)
    xmage_only = STATE.get("xmage_only", False) and XMAGE_NAME_SET is not None
    key = (cmd["oracle_id"], type_filter, max_bracket, xmage_only)
    if _POOL_CACHE["key"] != key:
        cmd_ci = set(cmd["color_identity"])
        static_pool = []
        for oid, c in CARDS.items():
            if not c["commander_legal"]:
                continue
            if not color_identity_subset(c["color_identity"], cmd_ci):
                continue
            if c["name"] in BASIC_LANDS:
                continue
            if c.get("card_bracket", 2) > max_bracket:
                continue
            if type_filter == "Ramp":
                if not c.get("is_ramp", False):
                    continue
            elif type_filter != "All":
                if type_filter.upper() not in c["card_types"]:
                    continue
            if xmage_only:
                if not _xmage_match(c["name"], XMAGE_NAME_SET):
                    continue
            static_pool.append(c)
        _POOL_CACHE["key"] = key
        _POOL_CACHE["pool"] = static_pool
    in_deck = set(STATE["deck_ids"]) | {cmd["oracle_id"]}
    return [c for c in _POOL_CACHE["pool"] if c["oracle_id"] not in in_deck]


# Cache of pair-score results keyed by (oracle_id_a, oracle_id_b) sorted.
# Pair scores are deterministic given the card profiles and tribe sizes,
# so we can memoise them across requests AND persist them to disk so the
# slow first-auto-build cost is paid once per pair across the entire
# lifetime of the app (and ideally across distribution — we ship a
# pre-warmed cache so users get instant ranking for popular commanders).
_PAIR_CACHE = {}
# User-writable cache lives in DATA_DIR; fall back to a bundle-shipped
# read-only seed in ROOT if present (so Electron can ship a pre-warmed
# cache that the user-data copy supersedes).
_CACHE_PATH = DATA_DIR / "pair_cache.pkl.gz"
_CACHE_SEED_PATH = ROOT / "pair_cache.pkl.gz"
_CACHE_DIRTY = False
_CACHE_DIRTY_THRESHOLD = 50000  # save after this many new entries
_CACHE_LAST_SAVED = 0


def _load_pair_cache():
    """Load the persistent pair cache from disk, if present.  Silent
    no-op if the file is missing or corrupt — the cache will repopulate
    organically as the app runs."""
    global _PAIR_CACHE
    # Prefer the user-data copy; if it's not there yet, seed from any
    # bundle-shipped pair_cache.pkl.gz next to app.py.
    candidates = [_CACHE_PATH]
    if _CACHE_SEED_PATH != _CACHE_PATH and _CACHE_SEED_PATH.exists():
        candidates.append(_CACHE_SEED_PATH)
    src = next((p for p in candidates if p.exists()), None)
    if src is None:
        return
    import gzip as _gzip, pickle as _pkl
    try:
        with _gzip.open(src, "rb") as f:
            data = _pkl.load(f)
        if isinstance(data, dict) and "scores" in data:
            _PAIR_CACHE = data["scores"]
            print(f"  pair-score cache loaded: {len(_PAIR_CACHE):,} entries from "
                  f"{src.stat().st_size / 1024 / 1024:.1f} MB on disk ({src.name}).")
        else:
            print(f"  warning: {src.name} unrecognised format; ignoring.")
    except (OSError, EOFError, _pkl.UnpicklingError) as e:
        print(f"  warning: couldn't load {src.name}: {e}")


def _save_pair_cache(force=False):
    """Persist the pair cache to disk so subsequent runs start hot."""
    global _CACHE_DIRTY, _CACHE_LAST_SAVED
    if not _CACHE_DIRTY and not force:
        return
    if not force and len(_PAIR_CACHE) - _CACHE_LAST_SAVED < _CACHE_DIRTY_THRESHOLD:
        return
    import gzip as _gzip, pickle as _pkl
    tmp = _CACHE_PATH.with_suffix(".pkl.gz.tmp")
    try:
        with _gzip.open(tmp, "wb") as f:
            _pkl.dump({"version": 1, "scores": _PAIR_CACHE},
                      f, protocol=_pkl.HIGHEST_PROTOCOL)
        tmp.replace(_CACHE_PATH)
        _CACHE_DIRTY = False
        _CACHE_LAST_SAVED = len(_PAIR_CACHE)
    except OSError as e:
        print(f"  warning: couldn't save cache: {e}")


def cached_score_pair(a, b):
    global _CACHE_DIRTY
    key = tuple(sorted((a["oracle_id"], b["oracle_id"])))
    cached = _PAIR_CACHE.get(key)
    if cached is None:
        cached = score_pair(a, b, TRIBE_SIZES)
        _PAIR_CACHE[key] = cached
        _CACHE_DIRTY = True
        # Maintain reverse adjacency so rank_suggestions can iterate
        # only candidates with potential edges to the current deck.
        if cached["total"] > 0:
            aid, bid = key
            _NEIGHBORS.setdefault(aid, set()).add(bid)
            _NEIGHBORS.setdefault(bid, set()).add(aid)
    return cached


# oracle_id -> set of oracle_ids with nonzero pair score against it.
# Built lazily as cached_score_pair runs and persisted in the cache.
_NEIGHBORS = {}


def _rebuild_neighbors_from_cache():
    """One-time build of _NEIGHBORS from any pre-loaded _PAIR_CACHE."""
    _NEIGHBORS.clear()
    for (aid, bid), result in _PAIR_CACHE.items():
        if result.get("total", 0) > 0:
            _NEIGHBORS.setdefault(aid, set()).add(bid)
            _NEIGHBORS.setdefault(bid, set()).add(aid)


_load_pair_cache()
_rebuild_neighbors_from_cache()
import atexit as _atexit
_atexit.register(lambda: _save_pair_cache(force=True))


def rank_suggestions(top_n=40):
    """Score every pool card against the current deck, return top N with edges."""
    cmd = commander_card()
    if not cmd:
        return []
    pool = candidate_pool()
    deck = [cmd] + deck_cards()

    # Fast path — restrict the candidate pool using the neighbor index.
    # Any card that doesn't appear in the cache as a neighbor of *any*
    # current deckmate will score 0 from synergy. Lands still need
    # consideration (color-fix score is independent of synergy edges),
    # so we keep them in the pool unconditionally.
    deck_oids = {d["oracle_id"] for d in deck}
    deck_neighbors = set()
    for oid in deck_oids:
        deck_neighbors |= _NEIGHBORS.get(oid, set())
    # Subtract deck itself (we don't suggest cards already in the deck;
    # candidate_pool already filters those out, but be defensive).
    deck_neighbors -= deck_oids

    if deck_neighbors or _NEIGHBORS:
        # Only filter when we have neighbor data; otherwise fall back to
        # the full pool (e.g. fresh install with empty cache).
        filtered = []
        for cand in pool:
            if cand["oracle_id"] in deck_neighbors or "LAND" in cand["card_types"]:
                filtered.append(cand)
        pool = filtered

    weights = STATE.get("weights", {"tribal": 1.0, "mechanic": 1.0, "combo": 1.0, "trigger_chain": 1.0})
    cmd_weight = STATE.get("commander_weight", 2.0)
    cmd_id = cmd["oracle_id"]
    # Build oid → card lookup so we can resolve a candidate's relevant
    # deckmates by oid intersection with cached neighbor set.
    deck_by_oid = {d["oracle_id"]: d for d in deck}
    results = []
    for cand in pool:
        total = 0.0
        edges_per_deckmate = []
        cand_oid = cand["oracle_id"]
        # Intersect this candidate's cached neighbors with the deck —
        # the only deckmates that can have nonzero edges with cand.
        # If we have no cache for cand at all, fall back to scanning
        # the whole deck so cold pairs still get computed.
        cand_neighbors = _NEIGHBORS.get(cand_oid)
        if cand_neighbors is not None:
            relevant = [deck_by_oid[o] for o in cand_neighbors & deck_by_oid.keys()]
        else:
            relevant = deck
        for d in relevant:
            s = cached_score_pair(cand, d)
            if s["total"] <= 0:
                continue
            # Apply category weights — each edge's contribution is scaled by
            # the user-set multiplier for its type family.
            weighted_total = 0.0
            for e in s["edges"]:
                t = e["type"]
                if t in ("tribal", "tribal_lord"):
                    mult = weights.get("tribal", 1.0)
                elif t == "combo":
                    mult = weights.get("combo", 1.0)
                elif t == "trigger_chain":
                    mult = weights.get("trigger_chain", 1.0)
                else:
                    mult = weights.get("mechanic", 1.0)
                weighted_total += e["weight"] * mult
            # Edges to the commander get an extra multiplier — the commander
            # defines the deck's strategy, so connections to it should weigh
            # more than connections between two random deckmates.
            if d["oracle_id"] == cmd_id:
                weighted_total *= cmd_weight
            if weighted_total > 0:
                total += weighted_total
                edges_per_deckmate.append({
                    "deckmate": d["name"],
                    "deckmate_id": d["oracle_id"],
                    "edges": s["edges"],
                    "score": weighted_total,
                })
        # Land scoring — independent of pairwise edges. Three components:
        #
        # 1. Color-fix bonus: each color-of-mana the land produces that
        #    matches the commander's color identity is worth 4 points,
        #    multiplied by the land's mana_quality (1.0 unconditional →
        #    0.85 shock/pain → 0.7 always-tapped → 0.5 tap-a-creature).
        #    Tundra in 2C: 2*4*1.0 = 8.  Indatha Triome in 3C: 3*4*0.7
        #    = 8.4.  Misty Rainforest in 2C: 2*4*0.85 = 6.8.
        # 2. Land base bonus: any land that produces SOME mana (even just
        #    {C}) gets +2 just for being a land, so utility lands like
        #    Reliquary Tower / Rogue's Passage / Buried Ruin aren't
        #    invisible to the ranker even when they have no synergy edges.
        # 3. Scaling-mana bonus: Cabal Coffers, Nykthos, Tron pieces,
        #    Gaea's Cradle produce *variable* amounts of mana that scale
        #    with permanents you control.  In their archetype these
        #    punch far above their colors_produced count.  In mono-color
        #    decks they get a +6 bonus on top of color-fix; in multi-color
        #    decks the bonus is halved (still useful, less archetype-defining).
        if "LAND" in cand["card_types"]:
            cmd_ci = set(cmd["color_identity"])
            cp = set(cand.get("colors_produced") or [])
            useful_colors = len(cp & cmd_ci) if cmd_ci else 0
            quality = cand.get("mana_quality", 1.0)
            color_bonus = useful_colors * 4 * quality
            base_bonus = 0.0
            scaling_bonus = 0.0
            # Land base bonus — any mana producer (incl. colorless-only).
            produces_any_mana = useful_colors > 0 or cand.get("produces_colorless")
            if produces_any_mana or cand.get("fetches_basic_types"):
                base_bonus = 2.0
            # Scaling-mana boost (Cabal Coffers, Nykthos, Tron, Cradle).
            if cand.get("mana_scaling"):
                scaling_bonus = 6.0 if len(cmd_ci) <= 1 else 3.0
            bonus = color_bonus + base_bonus + scaling_bonus
            if bonus > 0:
                total += bonus
                tag_parts = []
                if color_bonus:   tag_parts.append(f"color×{quality:.2f}")
                if base_bonus:    tag_parts.append("land_base")
                if scaling_bonus: tag_parts.append("scales")
                edges_per_deckmate.append({
                    "deckmate": "(deck color identity)",
                    "deckmate_id": None,
                    "edges": [{"type": "color_fix",
                               "tag": "+".join(tag_parts) if tag_parts else "land",
                               "weight": bonus,
                               "colors": sorted(cp & cmd_ci),
                               "quality": quality}],
                    "score": bonus,
                })
        if total <= 0:
            continue
        # No popularity tiebreaker. Tied scores break alphabetically — purely
        # mathematical signal, no EDHREC rank, no salt, no co-occurrence prior.
        results.append({
            "card": cand,
            "total": total,
            "edges": edges_per_deckmate,
        })
    results.sort(key=lambda r: (-r["total"], r["card"]["name"]))
    return results[:top_n]


# ---------- HTML templates ---------------------------------------------
INDEX_HTML = """
<!doctype html>
<html><head>
<title>MTG Commander Deckbuilder</title>
<meta charset="utf-8">
<style>
body { font-family: system-ui, sans-serif; max-width: 1100px; margin: 2em auto; padding: 0 1em; background: #1a1a1f; color: #ddd; }
h1, h2 { color: #fff; }
input[type=text] { width: 100%; padding: .6em; font-size: 1.1em; background: #2a2a2e; color: #ddd; border: 1px solid #444; border-radius: 4px; }
.cmd-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: 1em; margin-top: 1em; }
.cmd-card { background: #25252a; border: 1px solid #333; border-radius: 6px; padding: .5em; text-align: center; cursor: pointer; transition: transform .1s, border-color .1s; }
.cmd-card:hover { border-color: #6cf; transform: scale(1.03); }
.cmd-card img { width: 100%; height: auto; border-radius: 4px; }
.cmd-card .name { font-size: .9em; margin-top: .4em; color: #fff; }
.cmd-card .types { font-size: .75em; color: #999; }
.color-pip { display: inline-block; width: 14px; height: 14px; border-radius: 50%; margin: 0 1px; }
.pip-W { background: #f8f6d8; }
.pip-U { background: #6cf; }
.pip-B { background: #444; }
.pip-R { background: #f06; }
.pip-G { background: #6c6; }
.pip-C { background: #888; }
</style>
</head><body>
<h1>MTG Commander Deckbuilder</h1>
<p>Pick a commander. The app will then suggest cards that interact most strongly with your commander, color-identity filtered. Or <a href="/import" style="color:#6cf;">import an existing deck list</a> or <a href="/random_commander" style="color:#6cf;">pick a random one</a>.</p>
<input type="text" id="search" placeholder="Search by commander name (e.g. 'Krenko', 'Atraxa', 'Heliod')..." autofocus>
<div class="cmd-grid" id="results"></div>
<script>
const ALL_COMMANDERS = {{commanders|tojson}};
const search = document.getElementById('search');
const results = document.getElementById('results');
function render(list) {
    results.innerHTML = '';
    list.slice(0, 60).forEach(c => {
        const div = document.createElement('div');
        div.className = 'cmd-card';
        div.innerHTML = `
            <img src="${c.image_small}" alt="${c.name}" loading="lazy">
            <div class="name">${c.name}</div>
            <div class="types">${c.type_line}</div>
            <div>${c.color_identity.map(x => `<span class="color-pip pip-${x}"></span>`).join('')}</div>
        `;
        div.addEventListener('click', () => {
            window.location = '/set_commander/' + encodeURIComponent(c.oracle_id);
        });
        results.appendChild(div);
    });
}
search.addEventListener('input', () => {
    const q = search.value.toLowerCase().trim();
    if (!q) { render(ALL_COMMANDERS.slice(0, 60)); return; }
    const matched = ALL_COMMANDERS.filter(c => c.name.toLowerCase().includes(q));
    render(matched);
});
render(ALL_COMMANDERS.slice(0, 60));
</script>
</body></html>
"""

DECK_HTML = """
<!doctype html>
<html><head>
<title>Deckbuilder — {{commander.name}}</title>
<meta charset="utf-8">
<style>
html, body { height: 100%; margin: 0; padding: 0; overflow: hidden; }
body { font-family: system-ui, sans-serif; background: #1a1a1f; color: #ddd; display: flex; flex-direction: column; }
.layout { flex: 1 1 auto; display: grid; grid-template-columns: 380px 1fr; gap: 1.5em; padding: 1em; min-height: 0; }
.deck-pane, .suggestions-pane { background: #20202a; border-radius: 8px; padding: 1em; overflow-y: auto; min-height: 0; }
.deck-pane { padding: 1em; }
.suggestions-pane { background: transparent; padding: 0 .5em; }
/* Basics strip — sits between top bar and main grid */
.basics-strip { background: #25252a; border-bottom: 1px solid #333; padding: .4em 1em; display: flex; flex-wrap: wrap; gap: 1em; align-items: center; flex: 0 0 auto; }
.basics-strip h3 { margin: 0 .5em 0 0; color: #888; font-size: .8em; font-weight: normal; text-transform: uppercase; letter-spacing: .04em; }
.basics-strip .basic-row { display: inline-flex; gap: .3em; align-items: center; padding: 0; border: none; }
.basics-strip .basic-row .color-pip { width: 14px; height: 14px; border-radius: 50%; }
.basics-strip .basic-row span { font-size: .85em; color: #ccc; }
.basics-strip input[type=number] { width: 48px; padding: .15em; background: #1a1a1f; color: #ddd; border: 1px solid #444; border-radius: 3px; font-size: .85em; }
.deck-pane h2 { margin-top: 0; color: #fff; }
.deck-card { display: flex; align-items: center; gap: .5em; padding: .25em; border-bottom: 1px solid #333; font-size: .9em; }
.deck-card img { width: 30px; height: 42px; border-radius: 2px; }
.deck-card.commander { background: #3a2a4a; padding: .5em; border-radius: 4px; margin-bottom: .5em; }
.deck-card .name { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.deck-card button { background: #553; color: #fcc; border: 1px solid #884; border-radius: 3px; padding: 0 .5em; cursor: pointer; }
.suggestions { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 1em; }
.sugg-card { background: #25252a; border: 1px solid #333; border-radius: 6px; padding: .5em; text-align: center; }
.sugg-card img { width: 100%; height: auto; border-radius: 4px; cursor: pointer; }
.sugg-card .name { font-size: .9em; margin: .3em 0; color: #fff; }
.sugg-card .score { font-size: .85em; color: #6cf; font-weight: bold; }
.sugg-card .why { font-size: .7em; color: #999; max-height: 4em; overflow: hidden; margin-top: .3em; text-align: left; }
.sugg-card button { width: 100%; padding: .5em; background: #283; color: #fff; border: none; border-radius: 3px; cursor: pointer; margin-top: .3em; font-weight: bold; }
.sugg-card button:hover { background: #3a4; }
.color-pip { display: inline-block; width: 12px; height: 12px; border-radius: 50%; margin: 0 1px; vertical-align: middle; }
.pip-W { background: #f8f6d8; } .pip-U { background: #6cf; } .pip-B { background: #444; }
.pip-R { background: #f06; } .pip-G { background: #6c6; } .pip-C { background: #888; }
.bar { background: #2a2a3a; padding: .5em 1em; display: flex; justify-content: space-between; align-items: center; }
.bar a, .bar form { display: inline; }
.bar button { background: #844; color: #fcc; border: 1px solid #c66; padding: .3em .8em; border-radius: 3px; cursor: pointer; }
h2 { color: #fff; margin: .3em 0 .8em 0; }
.size-counter { font-size: 1.1em; color: #6cf; }
.size-counter.full { color: #6f6; }
.tooltip-card { display: none; position: absolute; pointer-events: none; z-index: 1000; }
.tooltip-card img { width: 280px; height: auto; box-shadow: 0 4px 16px #000; border-radius: 8px; }
/* Basic-lands panel */
.basics { background: #25252a; border-radius: 6px; padding: .5em; margin-bottom: .8em; }
.basics h3 { margin: 0 0 .4em 0; color: #fff; font-size: .9em; font-weight: normal; }
.basic-row { display: grid; grid-template-columns: 18px 70px 60px; align-items: center; gap: .3em; padding: .15em 0; font-size: .85em; }
.basic-row.disabled { opacity: 0.35; }
.basic-row .color-pip { width: 14px; height: 14px; }
.basic-row input[type=number] { width: 50px; padding: .15em; background: #1a1a1f; color: #ddd; border: 1px solid #444; border-radius: 3px; font-size: .9em; }
/* Type filter chips */
.filter-bar { margin-bottom: .8em; display: flex; flex-wrap: wrap; gap: .3em; }
.filter-chip { background: #25252a; color: #ccc; border: 1px solid #444; border-radius: 16px; padding: .25em .8em; font-size: .85em; cursor: pointer; text-decoration: none; }
.filter-chip.active { background: #4a6; color: #fff; border-color: #4a6; }
.filter-chip:hover { border-color: #6cf; }
/* Export button in top bar */
.bar .export-btn { background: #364; color: #cfc; border: 1px solid #6a4; }
.bar .group { display: flex; gap: .5em; }
/* Stats panel */
.stats { background: #25252a; border-radius: 6px; padding: .6em; margin-bottom: .8em; }
.stats h3 { margin: 0 0 .4em 0; color: #fff; font-size: .9em; font-weight: normal; }
.stats-row { display: grid; grid-template-columns: 96px 1fr; gap: .6em; align-items: center; }
.pie-svg { width: 96px; height: 96px; }
.legend { font-size: .75em; }
.legend .row { display: flex; align-items: center; gap: .35em; padding: 1px 0; }
.legend .swatch { width: 9px; height: 9px; border-radius: 2px; }
.cmc-display { font-size: .85em; color: #ccc; margin-top: .4em; padding-top: .4em; border-top: 1px solid #333; }
.cmc-display b { color: #fff; font-size: 1.05em; }
/* Bracket + weight controls */
.controls-row { display: flex; flex-wrap: wrap; gap: 1.5em; align-items: center; margin-bottom: .8em; padding: .6em .8em; background: #25252a; border-radius: 6px; }
.bracket-group, .weight-group { display: flex; gap: .4em; align-items: center; font-size: .85em; color: #ccc; }
.bracket-group .chip { background: #2a2a2e; color: #ccc; border: 1px solid #444; border-radius: 14px; padding: .2em .7em; font-size: .8em; cursor: pointer; text-decoration: none; }
.bracket-group .chip.active { background: #c84; color: #fff; border-color: #c84; }
.weight-slider { display: flex; flex-direction: column; align-items: center; }
.weight-slider input { width: 70px; }
.weight-slider label { font-size: .75em; color: #aaa; }
.weight-slider .val { color: #6cf; font-weight: bold; min-width: 2em; text-align: center; }
/* Full-size commander display + synergies block */
.commander-display { background: #20202a; border-radius: 8px; padding: .8em; margin-bottom: .8em; }
.commander-display .big-card { width: 100%; max-width: 100%; height: auto; border-radius: 12px; box-shadow: 0 4px 16px rgba(0,0,0,.5); display: block; }
.commander-display .commander-name { font-size: 1.1em; font-weight: bold; color: #fff; margin: .8em 0 .2em 0; text-align: center; }
.commander-display .commander-typeline { font-size: .8em; color: #888; text-align: center; margin-bottom: .8em; }
.synergies { background: #1a1a22; border-radius: 6px; padding: .6em .8em; font-size: .82em; line-height: 1.5em; }
.synergies .synergy-title { color: #fff; font-weight: bold; margin: 0 0 .4em 0; font-size: .95em; }
.synergies .syn-row { padding: .15em 0; border-bottom: 1px dashed #2a2a35; }
.synergies .syn-row:last-child { border-bottom: none; }
.synergies .syn-label { color: #6cf; font-weight: 600; display: inline-block; min-width: 110px; }
.synergies .syn-value { color: #cfcfcf; }
.synergies .flag-badge { display: inline-block; background: #c84; color: #fff; padding: 1px 6px; border-radius: 3px; font-size: .8em; margin-right: .3em; }
.synergies .nothing { color: #666; font-style: italic; }
/* Manual search */
.search-block { background: #25252a; border-radius: 6px; padding: .5em .6em; margin-bottom: .8em; position: relative; }
.search-block input[type=text] { width: 100%; padding: .5em; background: #1a1a1f; color: #ddd; border: 1px solid #444; border-radius: 4px; box-sizing: border-box; font-size: .9em; }
.search-block input[type=text]:focus { outline: none; border-color: #6cf; }
.search-results { display: none; max-height: 300px; overflow-y: auto; margin-top: .4em; background: #1a1a22; border-radius: 4px; }
.search-results.open { display: block; }
.search-results .result { display: flex; gap: .5em; align-items: center; padding: .35em .5em; border-bottom: 1px solid #2a2a35; cursor: pointer; }
.search-results .result:hover { background: #2a2a3a; }
.search-results .result img { width: 28px; height: 40px; border-radius: 2px; }
.search-results .result .info { flex: 1; min-width: 0; }
.search-results .result .info .nm { color: #fff; font-size: .85em; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.search-results .result .info .tl { color: #888; font-size: .7em; }
.search-results .result button { background: #283; color: #fff; border: none; padding: .3em .6em; border-radius: 3px; cursor: pointer; font-weight: bold; }
.search-results .empty { padding: .5em; color: #888; font-style: italic; font-size: .85em; }
.deck-sort { display: flex; flex-wrap: wrap; gap: .25em; align-items: center; margin: .3em 0 .8em 0; }
.deck-sort .chip { background: #2a2a2e; color: #ccc; border: 1px solid #444; border-radius: 12px; padding: .15em .55em; font-size: .75em; cursor: pointer; text-decoration: none; }
.deck-sort .chip.active { background: #466; color: #fff; border-color: #466; }
/* Deck-card score badge */
.deck-card .score-badge { display: inline-block; background: #2a3548; color: #6cf; font-size: .75em; padding: .1em .4em; border-radius: 3px; margin-left: .3em; min-width: 2em; text-align: center; }
.deck-card .score-badge.weak { background: #3a2a2a; color: #c88; }
.deck-card.commander .score-badge { background: #4a3a5a; color: #cdf; }
</style>
</head><body>
<div class="bar">
  <span>Commander: <b>{{commander.name}}</b></span>
  <span class="size-counter {{'full' if deck_size >= 100 else ''}}">{{deck_size}}/100</span>
  <span class="group">
    <a href="/graph"><button class="export-btn">Graph</button></a>
    {% if xmage_available %}
    <a href="/toggle_xmage" title="Restrict to cards XMage implements"><button class="export-btn" style="background:{{ '#2a6f3a' if xmage_only else '#a44' }};color:#fff;">XMage{{ ' ✓' if xmage_only else '' }}</button></a>
    {% endif %}
    <form method="POST" action="/auto_build" style="display:inline" onsubmit="return confirm('Auto-build will reset the deck and pick: staple rocks → tribal core (if applicable) → synergy spell fill → cycle lands + basics proportional to actual pip distribution. Land target ≈ 36. Continue?')">
      <button class="export-btn" style="background:#646;">Auto-build</button>
    </form>
    <button class="export-btn" id="copy-btn">Copy to Clipboard</button>
    <a href="/export.txt" download="deck.txt"><button class="export-btn">Download TXT</button></a>
    <a href="/reset" onclick="return confirm('Start over?')"><button>Reset</button></a>
  </span>
</div>

<!-- Basic Lands strip — only shows colors in the commander's identity -->
<div class="basics-strip">
  <h3>Basic Lands</h3>
  {% for b in basics_info %}
  <div class="basic-row">
    <span class="color-pip pip-{{b.color}}"></span>
    <span>{{b.name}}</span>
    <input type="number" min="0" max="99" value="{{b.count}}" data-name="{{b.name}}">
  </div>
  {% endfor %}
</div>

<div class="layout">

<div class="deck-pane">
  <!-- Full-size commander display with synergy summary -->
  <div class="commander-display">
    <img class="big-card" src="{{commander.image_normal}}" alt="{{commander.name}}">
    <div class="commander-name">{{commander.name}}</div>
    <div class="commander-typeline">{{commander.type_line}}</div>

    <div class="synergies">
      <div class="synergy-title">Why this commander matches cards</div>

      {% if commander_synergies.flags %}
      <div class="syn-row">
        {% for f in commander_synergies.flags %}<span class="flag-badge">{{f}}</span>{% endfor %}
        <span class="syn-value">Bracket {{commander_synergies.bracket}}</span>
      </div>
      {% endif %}

      {% if commander_synergies.tribes_is %}
      <div class="syn-row"><span class="syn-label">Subtypes:</span>
        <span class="syn-value">{{ commander_synergies.tribes_is | join(', ') }}</span>
      </div>
      {% endif %}

      {% if commander_synergies.tribes_ref %}
      <div class="syn-row"><span class="syn-label">Tribes payoff:</span>
        <span class="syn-value">{{ commander_synergies.tribes_ref | join(', ') }}</span>
      </div>
      {% endif %}

      {% if commander_synergies.triggers %}
      <div class="syn-row"><span class="syn-label">Triggers on:</span>
        <span class="syn-value">{{ commander_synergies.triggers | join(', ') }}</span>
      </div>
      {% endif %}

      {% if commander_synergies.produces %}
      <div class="syn-row"><span class="syn-label">Produces:</span>
        <span class="syn-value">{{ commander_synergies.produces | join(', ') }}</span>
      </div>
      {% endif %}

      {% if commander_synergies.cares %}
      <div class="syn-row"><span class="syn-label">Cares about:</span>
        <span class="syn-value">{{ commander_synergies.cares | join(', ') }}</span>
      </div>
      {% endif %}

      {% if not (commander_synergies.tribes_ref or commander_synergies.triggers or commander_synergies.produces or commander_synergies.cares) %}
      <div class="syn-row nothing">No structural axes detected — this commander relies on stats / direct effects rather than synergy triggers.</div>
      {% endif %}
    </div>
  </div>

  <!-- Manual card search — for when you have a specific card or combo piece in mind -->
  <div class="search-block">
    <input type="text" id="card-search" placeholder="Search any card by name (e.g. 'Cyclonic Rift', 'Pestermite')..." autocomplete="off">
    <div class="search-results" id="search-results"></div>
  </div>

  <h2 style="margin-top:1em;">Deck</h2>
  <div class="deck-sort">
    <span style="color:#888;font-size:.8em;">Sort:</span>
    {% for opt, lbl in [('added','Order added'),('alpha','A–Z'),('score','Score'),('type','Type')] %}
    <a class="chip {{ 'active' if (sort_by == opt) else '' }}" href="?sort={{opt}}">{{lbl}}</a>
    {% endfor %}
  </div>

  <!-- Stats panel -->
  {% if stats and stats.total > 0 %}
  <div class="stats">
    <h3>Stats</h3>
    <div class="stats-row">
      <svg class="pie-svg" viewBox="0 0 100 100">
        {% for s in pie_slices %}
        <path d="{{ s.path }}" fill="{{ s.color }}" stroke="#1a1a1f" stroke-width="0.5">
          <title>{{ s.label }}: {{ s.count }} ({{ "%.0f"|format(s.pct) }}%)</title>
        </path>
        {% endfor %}
      </svg>
      <div class="legend">
        {% for s in pie_slices %}
        <div class="row">
          <span class="swatch" style="background: {{ s.color }};"></span>
          {{ s.label }}: <b>{{ s.count }}</b>
        </div>
        {% endfor %}
      </div>
    </div>
    <div class="cmc-display">
      Avg CMC (non-land): <b>{{ "%.2f"|format(stats.avg_cmc) }}</b>
      <span style="color:#888"> ({{ stats.non_land_count }} cards)</span>
    </div>
  </div>
  {% endif %}

  <!-- (Basic Lands moved to top strip below the bar) -->
  {% if commander_score is defined %}
  <div class="deck-card commander" style="margin-bottom:.5em;">
    <span style="font-size:.8em;color:#aaa;">Commander synergy with deck:</span>
    <span class="score-badge">{{ "%.0f"|format(commander_score) }}</span>
  </div>
  {% endif %}
  {% for entry in deck_with_scores %}
  {% set c = entry.card %}
  <div class="deck-card">
    <img src="{{c.image_small}}" alt="" data-image="{{c.image_normal}}" class="hover-card">
    <div class="name">{{c.name}}
      <span class="score-badge {{ 'weak' if entry.score < 5 else '' }}" title="Sum of interactions with the rest of the deck">
        {{ "%.0f"|format(entry.score) }}
      </span>
    </div>
    <form method="POST" action="/remove" style="display:inline">
      <input type="hidden" name="oracle_id" value="{{c.oracle_id}}">
      <button type="submit">×</button>
    </form>
  </div>
  {% endfor %}
</div>

<div class="suggestions-pane">
  <h2 style="margin-top:0;">Suggestions <small style="color:#888">— scored by interaction with current deck</small></h2>

  <div class="controls-row">
    <div class="bracket-group">
      <span style="color:#888;">Max bracket:</span>
      {% for level, name in brackets.items() %}
      <a class="chip {{ 'active' if level == active_bracket else '' }}" href="/set_bracket/{{level}}" title="Bracket {{level}}: {{name}}">{{level}} {{name}}</a>
      {% endfor %}
    </div>
    <div class="weight-group">
      <span style="color:#888;">Weights:</span>
      {% for key in ['tribal','mechanic','combo','trigger_chain'] %}
      <span class="weight-slider">
        <input type="range" min="0" max="3" step="0.1" value="{{ weights.get(key, 1.0) }}" data-key="{{key}}">
        <label>{{key}} <span class="val">{{ "%.1f"|format(weights.get(key, 1.0)) }}</span></label>
      </span>
      {% endfor %}
      <span class="weight-slider">
        <input type="range" min="0.5" max="5" step="0.25" value="{{ commander_weight }}" data-key="commander">
        <label>commander <span class="val">{{ "%.2f"|format(commander_weight) }}×</span></label>
      </span>
    </div>
  </div>

  <div class="filter-bar">
    {% for tf in type_filters %}
    <a class="filter-chip {{ 'active' if tf == active_filter else '' }}" href="/set_filter/{{tf}}">{{tf}}</a>
    {% endfor %}
  </div>
  {% if suggestions %}
  <div class="suggestions">
  {% for s in suggestions %}
    <div class="sugg-card">
      <a href="https://scryfall.com/search?q=!%22{{s.card.name|urlencode}}%22" target="_blank">
        <img src="{{s.card.image_small}}" alt="{{s.card.name}}" data-image="{{s.card.image_normal}}" class="hover-card" loading="lazy">
      </a>
      <div class="name">{{s.card.name}}</div>
      <div class="score">{{ "%.1f"|format(s.total) }} pts</div>
      <div class="why">{{ s.why }}</div>
      <form method="POST" action="/add">
        <input type="hidden" name="oracle_id" value="{{s.card.oracle_id}}">
        <button type="submit">+ Add</button>
      </form>
    </div>
  {% endfor %}
  </div>
  {% else %}
  <p>No suggestions found. The pool may be exhausted, or no cards in this color identity score above zero against your current deck.</p>
  {% endif %}
</div>

</div>

<div class="tooltip-card" id="tooltip"><img src="" id="tooltip-img"></div>
<script>
// Hover preview — show normal-resolution image when hovering small image.
const tooltip = document.getElementById('tooltip');
const tooltipImg = document.getElementById('tooltip-img');
document.querySelectorAll('.hover-card').forEach(img => {
    img.addEventListener('mouseenter', e => {
        tooltipImg.src = img.dataset.image || img.src;
        tooltip.style.display = 'block';
    });
    img.addEventListener('mousemove', e => {
        // Smart positioning — keep the tooltip on screen.
        const tw = tooltip.offsetWidth || 280;
        const th = tooltip.offsetHeight || 400;
        let x = e.clientX + 20;
        let y = e.clientY + 20;
        if (x + tw > window.innerWidth - 8)  x = e.clientX - tw - 20;
        if (y + th > window.innerHeight - 8) y = window.innerHeight - th - 8;
        if (y < 8) y = 8;
        if (x < 8) x = 8;
        tooltip.style.left = x + 'px';
        tooltip.style.top = y + 'px';
    });
    img.addEventListener('mouseleave', () => { tooltip.style.display = 'none'; });
});

// Copy-to-clipboard — fetch the export and write to navigator.clipboard.
const copyBtn = document.getElementById('copy-btn');
if (copyBtn) {
    copyBtn.addEventListener('click', async () => {
        try {
            const r = await fetch('/export.txt');
            const text = await r.text();
            await navigator.clipboard.writeText(text);
            const original = copyBtn.textContent;
            copyBtn.textContent = '✓ Copied!';
            setTimeout(() => { copyBtn.textContent = original; }, 1500);
        } catch (err) {
            // navigator.clipboard requires HTTPS in some browsers; fall back to a textarea trick.
            try {
                const r = await fetch('/export.txt');
                const text = await r.text();
                const ta = document.createElement('textarea');
                ta.value = text;
                document.body.appendChild(ta);
                ta.select();
                document.execCommand('copy');
                document.body.removeChild(ta);
                copyBtn.textContent = '✓ Copied!';
                setTimeout(() => { copyBtn.textContent = 'Copy to Clipboard'; }, 1500);
            } catch (e2) {
                alert('Copy failed: ' + e2);
            }
        }
    });
}

// Manual card search — live results, click to add.
(function () {
    const input = document.getElementById('card-search');
    const results = document.getElementById('search-results');
    if (!input || !results) return;
    let timer = null;
    let lastQuery = '';

    async function doSearch() {
        const q = input.value.trim();
        if (q.length < 2) {
            results.classList.remove('open');
            results.innerHTML = '';
            return;
        }
        if (q === lastQuery) return;
        lastQuery = q;
        const resp = await fetch('/api/search?q=' + encodeURIComponent(q));
        const data = await resp.json();
        if (!data.results.length) {
            results.classList.add('open');
            results.innerHTML = '<div class="empty">No matches in this color identity.</div>';
            return;
        }
        results.innerHTML = data.results.map(r => `
            <div class="result" data-oid="${r.oracle_id}">
                <img src="${r.image_small}" alt="">
                <div class="info">
                    <div class="nm">${r.name}</div>
                    <div class="tl">${r.type_line}</div>
                </div>
                <button>+ Add</button>
            </div>
        `).join('');
        results.classList.add('open');
        results.querySelectorAll('.result').forEach(el => {
            el.addEventListener('click', async (ev) => {
                ev.stopPropagation();
                if (el.classList.contains('busy')) return;
                el.classList.add('busy');
                const oid = el.dataset.oid;
                try {
                    const fd = new FormData();
                    fd.append('oracle_id', oid);
                    const r = await fetch('/add', { method: 'POST', body: fd });
                    if (!r.ok) {
                        const txt = await r.text();
                        el.querySelector('button').textContent = '✗ ' + (txt || ('HTTP ' + r.status));
                        el.classList.remove('busy');
                        return;
                    }
                    window.location.reload();
                } catch (err) {
                    el.querySelector('button').textContent = '✗ error';
                    el.classList.remove('busy');
                }
            });
        });
    }
    input.addEventListener('input', () => {
        clearTimeout(timer);
        timer = setTimeout(doSearch, 200);
    });
    document.addEventListener('click', (e) => {
        if (!input.contains(e.target) && !results.contains(e.target)) {
            results.classList.remove('open');
        }
    });
})();

// Weight sliders — fire on RELEASE only (the 'change' event), so the
// slider drags smoothly while the user is moving it and only reloads
// when they let go. The 'input' event just updates the displayed value
// for live feedback.
document.querySelectorAll('.weight-slider input[type=range]').forEach(input => {
    const valSpan = input.parentElement.querySelector('.val');
    input.addEventListener('input', () => {
        // Live label update, no network call.
        const v = parseFloat(input.value);
        valSpan.textContent = (input.dataset.key === 'commander') ? v.toFixed(2) + '×' : v.toFixed(1);
    });
    input.addEventListener('change', async () => {
        const fd = new FormData();
        document.querySelectorAll('.weight-slider input[type=range]').forEach(i => {
            fd.append(i.dataset.key, i.value);
        });
        await fetch('/set_weights', { method: 'POST', body: fd });
        window.location.reload();
    });
});

// Basic-land count inputs — POST on change, then refresh deck-size counter.
document.querySelectorAll('.basic-row input[type=number]').forEach(input => {
    input.addEventListener('change', async () => {
        const fd = new FormData();
        fd.append('name', input.dataset.name);
        fd.append('count', input.value);
        const r = await fetch('/set_basic', { method: 'POST', body: fd });
        const data = await r.json();
        // Update size counter
        const counter = document.querySelector('.size-counter');
        if (counter) {
            counter.textContent = data.deck_size + '/100';
            counter.classList.toggle('full', data.deck_size >= 100);
        }
    });
});
</script>
</body></html>
"""


def explain_edges(suggestion):
    """Build a short human-readable 'why' string for a suggestion."""
    edges_by_type = Counter()
    edges_by_tag = Counter()
    edges_by_tribe = Counter()
    combo_subtypes = []
    for dm in suggestion["edges"]:
        for e in dm["edges"]:
            edges_by_type[e["type"]] += 1
            if "tag" in e:
                edges_by_tag[e["tag"]] += 1
            if "tribe" in e:
                edges_by_tribe[e["tribe"]] += 1
            if e["type"] == "combo":
                combo_subtypes.append(e.get("subtype", "?"))
    parts = []
    if combo_subtypes:
        parts.append("⚡ COMBO: " + ", ".join(set(combo_subtypes)))
    if edges_by_tribe:
        top_tribe = edges_by_tribe.most_common(1)[0]
        parts.append(f"{top_tribe[0]} tribal ({top_tribe[1]} hits)")
    if edges_by_tag:
        top_tags = [t for t, _ in edges_by_tag.most_common(3)]
        parts.append("synergy: " + ", ".join(top_tags))
    return "; ".join(parts) or "structural overlap"


# ---------- Routes ------------------------------------------------------
app = Flask(__name__)


@app.route("/")
def index():
    if STATE["commander_id"]:
        return redirect(url_for("deck"))
    return render_template_string(INDEX_HTML, commanders=COMMANDER_OPTIONS)


@app.route("/set_commander/<oracle_id>")
def set_commander(oracle_id):
    if oracle_id not in CARDS:
        return "Unknown card", 404
    if not CARDS[oracle_id]["is_commander_eligible"]:
        return "Not commander-eligible", 400
    STATE["commander_id"] = oracle_id
    STATE["deck_ids"] = []
    return redirect(url_for("deck"))


@app.route("/deck")
def deck():
    cmd = commander_card()
    if not cmd:
        return redirect(url_for("index"))
    suggestions = rank_suggestions(top_n=60)
    for s in suggestions:
        s["why"] = explain_edges(s)
    cmd_ci = set(cmd["color_identity"])
    basics_info = []
    for b in BASIC_LANDS:
        color = BASIC_COLOR[b]
        in_ci = (color == "C") or (color in cmd_ci)
        # Only render basics that are actually addable to this deck.
        # Wastes is colorless and always available.
        if not in_ci:
            continue
        basics_info.append({
            "name": b,
            "color": color,
            "count": STATE["basics"].get(b, 0),
            "in_ci": True,
        })
    stats = deck_stats()
    pie_slices = build_pie_slices(stats["categories"]) if stats else []

    # Per-deck-card score: how strongly each existing card interacts with the
    # rest of the deck. Helps the user spot weak links and re-evaluate slots.
    deck_card_list = deck_cards()
    full_deck = [cmd] + deck_card_list
    weights = STATE.get("weights", {"tribal": 1.0, "mechanic": 1.0, "combo": 1.0, "trigger_chain": 1.0})

    cmd_weight = STATE.get("commander_weight", 2.0)
    def _weighted_pair(a, b):
        s = cached_score_pair(a, b)
        if s["total"] <= 0:
            return 0.0
        total = 0.0
        for e in s["edges"]:
            t = e["type"]
            if t in ("tribal", "tribal_lord"): mult = weights.get("tribal", 1.0)
            elif t == "combo":               mult = weights.get("combo", 1.0)
            elif t == "trigger_chain":       mult = weights.get("trigger_chain", 1.0)
            else:                            mult = weights.get("mechanic", 1.0)
            total += e["weight"] * mult
        # If either side is the commander, apply commander weight.
        if a["oracle_id"] == cmd["oracle_id"] or b["oracle_id"] == cmd["oracle_id"]:
            total *= cmd_weight
        return total

    cmd_ci_set = set(cmd["color_identity"])
    def _land_color_bonus(c):
        if "LAND" not in c["card_types"]:
            return 0
        cp = set(c.get("colors_produced") or [])
        useful = len(cp & cmd_ci_set) if cmd_ci_set else 0
        return useful * 4 * c.get("mana_quality", 1.0)

    deck_with_scores = []
    for c in deck_card_list:
        s = sum(_weighted_pair(c, other) for other in full_deck if other["oracle_id"] != c["oracle_id"])
        s += _land_color_bonus(c)
        deck_with_scores.append({"card": c, "score": s})

    # Sort the deck list per request param. Default is insertion order.
    sort_by = request.args.get("sort", "added")
    TYPE_ORDER = {"CREATURE": 1, "PLANESWALKER": 2, "BATTLE": 3,
                  "ARTIFACT": 4, "ENCHANTMENT": 5,
                  "INSTANT": 6, "SORCERY": 7, "LAND": 8}
    if sort_by == "alpha":
        deck_with_scores.sort(key=lambda e: e["card"]["name"].lower())
    elif sort_by == "score":
        deck_with_scores.sort(key=lambda e: -e["score"])
    elif sort_by == "type":
        def primary_type(c):
            for t in ("CREATURE", "PLANESWALKER", "BATTLE", "ARTIFACT",
                      "ENCHANTMENT", "INSTANT", "SORCERY", "LAND"):
                if t in c["card_types"]:
                    return TYPE_ORDER[t]
            return 99
        deck_with_scores.sort(key=lambda e: (primary_type(e["card"]), e["card"]["name"].lower()))

    # Commander's own score for context (sum of interactions with the rest of the deck).
    commander_score = sum(_weighted_pair(cmd, other) for other in deck_card_list)

    return render_template_string(
        DECK_HTML,
        commander=cmd,
        commander_synergies=commander_synergies(cmd),
        commander_score=commander_score,
        deck=deck_card_list,
        deck_with_scores=deck_with_scores,
        suggestions=suggestions,
        deck_size=deck_size(),
        basics_info=basics_info,
        type_filters=TYPE_FILTERS,
        active_filter=STATE.get("filter_type", "All"),
        stats=stats,
        pie_slices=pie_slices,
        brackets=BRACKETS,
        active_bracket=STATE.get("max_bracket", 4),
        weights=STATE.get("weights", {}),
        commander_weight=STATE.get("commander_weight", 2.0),
        sort_by=sort_by,
        xmage_available=XMAGE_NAME_SET is not None,
        xmage_only=STATE.get("xmage_only", False),
    )


@app.route("/add", methods=["POST"])
def add_card():
    oid = request.form.get("oracle_id")
    if not oid or oid not in CARDS:
        return "Unknown card", 404
    if oid == STATE["commander_id"] or oid in STATE["deck_ids"]:
        return redirect(url_for("deck"))
    if deck_full():
        return redirect(url_for("deck"))
    cmd = commander_card()
    if not color_identity_subset(CARDS[oid]["color_identity"], set(cmd["color_identity"])):
        return "Color identity violation", 400
    if STATE.get("xmage_only", False) and XMAGE_NAME_SET is not None:
        if not _xmage_match(CARDS[oid]["name"], XMAGE_NAME_SET):
            return f"'{CARDS[oid]['name']}' is not in XMage's playable pool. Disable XMage mode to add anyway.", 400
    STATE["deck_ids"].append(oid)
    return redirect(url_for("deck"))


@app.route("/remove", methods=["POST"])
def remove_card():
    oid = request.form.get("oracle_id")
    if oid in STATE["deck_ids"]:
        STATE["deck_ids"].remove(oid)
    return redirect(url_for("deck"))


@app.route("/reset")
def reset():
    STATE["commander_id"] = None
    STATE["deck_ids"] = []
    STATE["basics"] = {b: 0 for b in BASIC_LANDS}
    STATE["filter_type"] = "All"
    return redirect(url_for("index"))


@app.route("/set_basic", methods=["POST"])
def set_basic():
    name = request.form.get("name", "")
    try:
        count = int(request.form.get("count", "0"))
    except ValueError:
        count = 0
    count = max(0, min(99, count))
    if name not in BASIC_LANDS:
        return "Unknown basic", 400
    # Enforce color identity for basics: outside-CI basics stay at 0.
    cmd = commander_card()
    if cmd:
        cmd_ci = set(cmd["color_identity"])
        color = BASIC_COLOR[name]
        if color != "C" and color not in cmd_ci:
            count = 0
    STATE["basics"][name] = count
    return jsonify({"basics": STATE["basics"], "deck_size": deck_size()})


@app.route("/set_filter/<filter_name>")
def set_filter(filter_name):
    if filter_name in TYPE_FILTERS:
        STATE["filter_type"] = filter_name
    return redirect(url_for("deck"))


@app.route("/set_bracket/<int:level>")
def set_bracket(level):
    if 1 <= level <= 4:
        STATE["max_bracket"] = level
    return redirect(url_for("deck"))


@app.route("/set_weights", methods=["POST"])
def set_weights():
    for key in ("tribal", "mechanic", "combo", "trigger_chain"):
        try:
            STATE["weights"][key] = float(request.form.get(key, STATE["weights"][key]))
        except ValueError:
            pass
    cmd_w = request.form.get("commander")
    if cmd_w is not None:
        try:
            STATE["commander_weight"] = float(cmd_w)
        except ValueError:
            pass
    return jsonify({"weights": STATE["weights"], "commander_weight": STATE["commander_weight"]})


@app.route("/toggle_xmage")
def toggle_xmage():
    if XMAGE_NAME_SET is None:
        return "XMage source not found; toggle unavailable", 400
    STATE["xmage_only"] = not STATE.get("xmage_only", False)
    # Invalidate the score cache because the pool changed.
    _PAIR_CACHE.clear()
    return redirect(url_for("deck"))


def _land_color_fix_score(land, cmd_ci):
    """Color-fix-only score for a land. Used to rank mana fixing without
    contamination from synergy edges. Higher = better fixing for the deck.

    Components:
      useful_colors × 4 × mana_quality   — the core color-fix value
      + scaling_bonus                    — Cabal Coffers / Nykthos / Tron
      + utility_bonus                    — small bump for produces_colorless
                                            or fetches_basic so true-utility
                                            lands aren't always 0
    """
    cp = set(land.get("colors_produced") or [])
    useful = len(cp & cmd_ci) if cmd_ci else 0
    quality = land.get("mana_quality", 1.0)
    color = useful * 4 * quality
    scaling = 0.0
    if land.get("mana_scaling"):
        scaling = 6.0 if len(cmd_ci) <= 1 else 2.0
    utility = 0.0
    # Lands that produce only colorless still have *some* value as a
    # land drop, but only ~0.5 — much less than a true fixing land.
    if useful == 0 and (land.get("produces_colorless") or land.get("fetches_basic_types")):
        utility = 0.5
    return color + scaling + utility


@app.route("/auto_build", methods=["POST"])
def auto_build():
    """Random/auto build — four phases.

    1. **Staples.** Add a short curated list of CI-legal mana rocks and
       utility lands that virtually every Commander deck wants: Sol Ring,
       Mind Stone, Thought Vessel, Reliquary Tower, Rogue's Passage, plus
       multi-color additions (Arcane Signet, Command Tower, Exotic
       Orchard, Path of Ancestry in 3C+).
    2. **Basics scaled to color count.** Target 36 total lands; basic share
       follows the user's empirical averages (1C≈28, 2C≈22, 3C≈17, 5C≈12).
    3. **Land budget fill, by COLOR-FIX score only.** This is the key
       difference from synergy ranking — lands compete on how much fixing
       they bring, NOT on whether they happen to pair with the commander.
       So a Grixis deck picks Xander's Lounge / Underground Sea / Watery
       Grave / Drowned Catacomb / Morphic Pool, NOT Phyrexia's Core or
       Miren-the-Moaning-Well that happen to have artifact-sac edges.
    4. **Synergy fill for non-land slots.** Greedy-pick the remaining slots
       from non-land suggestions ranked by synergy score.

    Respects the current XMage mode and bracket / weight settings."""
    cmd = commander_card()
    if not cmd:
        return redirect(url_for("index"))
    cmd_ci_list = [c for c in cmd["color_identity"] if c in "WUBRG"]
    cmd_ci = set(cmd_ci_list)
    n_colors = len(cmd_ci_list)

    # Reset deck and basics.
    STATE["deck_ids"] = []
    STATE["basics"] = {b: 0 for b in BASIC_LANDS}

    def is_legal(c):
        if not c.get("commander_legal"):
            return False
        if not set(c.get("color_identity") or []).issubset(cmd_ci | {"C"}):
            return False
        if STATE.get("xmage_only", False) and XMAGE_NAME_SET is not None:
            if not _xmage_match(c["name"], XMAGE_NAME_SET):
                return False
        return True

    def try_add(name):
        oid = NAME_TO_ID.get(name.lower())
        if not oid: return False
        c = CARDS[oid]
        if not is_legal(c):
            return False
        if oid == STATE["commander_id"] or oid in STATE["deck_ids"]:
            return False
        STATE["deck_ids"].append(oid)
        return True

    # ---- Phase 1: utility lands ONLY ---------------------------------
    # Mana rocks are deliberately deferred to Phase 4 (after synergy fill).
    # Including them up front makes the synergy ranker think the deck is
    # an artifact-mana strategy and biases later picks toward Doubling
    # Cube / Chromatic Lantern / Manaweft Sliver / X-spells / etc., which
    # have nothing to do with the commander's actual theme.  By keeping
    # only commander-neutral utility lands in the deck during synergy
    # ranking, the ranker focuses purely on what the commander cares
    # about.  Cycle lands (originals/shocks/triomes/etc.) are deferred
    # to Phase 5 so they can be matched to the deck's actual pip mix.
    rocks_to_add = list(STAPLE_MANA_ROCKS_ANY)
    if n_colors >= 2:
        rocks_to_add += STAPLE_MANA_ROCKS_MULTI

    for name in STAPLE_LANDS_COLORLESS:
        try_add(name)
    if n_colors >= 2:
        for name in STAPLE_LANDS_MULTI:
            try_add(name)
    if n_colors >= 3:
        for name in STAPLE_LANDS_3PLUS:
            try_add(name)

    # ---- Phase 2: tribal core (for tribal payoff commanders) ---------
    # If the commander references a tribe AND is itself of that tribe,
    # this is a tribal payoff commander (Sliver Hivelord, Krenko Mob
    # Boss, Edgar Markov, Marrow-Gnawer, etc.).  Add a core of tribe
    # members BEFORE the synergy ranker, because the ranker is dominated
    # by mana-rock / land mechanic_overlap edges from the staples and
    # won't favour tribe members until many are already in the deck.
    cmd_refs = set(cmd.get("tribes_referenced") or [])
    cmd_is = set(cmd.get("tribes_is") or [])
    primary_tribe = None
    for t in cmd_refs & cmd_is:
        if (TRIBE_SIZES.get(t, 0) >= 30 and
                t not in {"Human"}):  # Human is too generic to be tribal
            primary_tribe = t
            break
    if primary_tribe:
        members = []
        for o, c in CARDS.items():
            if not is_legal(c):
                continue
            if o == STATE["commander_id"] or o in STATE["deck_ids"]:
                continue
            if "CREATURE" not in (c.get("card_types") or []):
                continue
            if primary_tribe not in (c.get("tribes_is") or []):
                continue
            members.append((c.get("edhrec_rank") or 999999, c["name"], o))
        members.sort()
        for _, _, oid in members[:25]:
            STATE["deck_ids"].append(oid)

    # ---- Phase 3: synergy fill spell slots ---------------------------
    # Target enough non-land cards to leave room for the deferred mana
    # rocks (Phase 4) and still total 63 non-land slots.  Every CI-legal
    # rock counts toward the 63-spell target, so synergy fill stops
    # earlier when the rock count is higher (more multi-color decks add
    # more rocks).
    TOTAL_LANDS_TARGET = 36
    SPELL_TARGET = 100 - 1 - TOTAL_LANDS_TARGET  # 63
    rocks_eligible = sum(
        1 for n in rocks_to_add
        if NAME_TO_ID.get(n.lower()) and is_legal(CARDS[NAME_TO_ID[n.lower()]])
    )
    SYNERGY_TARGET = max(0, SPELL_TARGET - rocks_eligible)
    safety = 200
    while safety > 0:
        non_land_slots = sum(
            1 for o in STATE["deck_ids"]
            if "LAND" not in (CARDS[o].get("card_types") or [])
        )
        if non_land_slots >= SYNERGY_TARGET:
            break
        safety -= 1
        suggestions = rank_suggestions(top_n=10)
        if not suggestions:
            break
        pick = next((s for s in suggestions
                     if "LAND" not in (s["card"].get("card_types") or [])),
                    None)
        if pick is None:
            break
        STATE["deck_ids"].append(pick["card"]["oracle_id"])

    # ---- Phase 4: deferred staple mana rocks -------------------------
    # Sol Ring etc. are added now, AFTER synergy ranking has finished,
    # so their presence in the deck doesn't bias the ranker toward
    # artifact-mana / X-spell / "ramp_mana producer" themes.
    for name in rocks_to_add:
        try_add(name)

    # ---- Phase 5: pip-aware mana base --------------------------------
    # Now count the colored mana pips in every non-land card in the deck
    # (commander + spells) and build the mana base proportional to those
    # demands.  A WUBRG deck with 40 white pips and 10 of every other
    # color gets way more white sources than U/B/R/G sources.
    pips = _count_color_pips([STATE["commander_id"]] + STATE["deck_ids"])
    pip_total = sum(pips.values())
    nonbasic_lands_in_deck = sum(
        1 for o in STATE["deck_ids"]
        if "LAND" in (CARDS[o].get("card_types") or [])
    )

    # 4a: cycle picks proportional to pips
    cycle_picks = cycle_lands_for_ci_pip_weighted(cmd_ci, pips)
    cycle_cap = {2: 7, 3: 11, 4: 14, 5: 16}.get(n_colors, 0)
    cycle_added = 0
    for name in cycle_picks:
        if cycle_added >= cycle_cap:
            break
        if try_add(name):
            cycle_added += 1

    # 4b: phase 3a-equivalent — fill remaining nonbasic land slots by
    # color-fix score (Mana Confluence, Forbidden Orchard, fetches).
    BASICS_BY_COLORS = {0: 8, 1: 28, 2: 22, 3: 17, 4: 14, 5: 12}
    target_basics = BASICS_BY_COLORS.get(n_colors, 22)
    nonbasic_lands_in_deck = sum(
        1 for o in STATE["deck_ids"]
        if "LAND" in (CARDS[o].get("card_types") or [])
    )
    nonbasic_land_budget = max(0, TOTAL_LANDS_TARGET - target_basics - nonbasic_lands_in_deck)

    if nonbasic_land_budget > 0:
        candidate_lands = []
        for oid, c in CARDS.items():
            if "LAND" not in (c.get("card_types") or []):
                continue
            tl = c.get("type_line") or ""
            if "Basic" in tl:
                continue
            if oid == STATE["commander_id"] or oid in STATE["deck_ids"]:
                continue
            if not is_legal(c):
                continue
            score = _land_color_fix_score(c, cmd_ci)
            if score <= 0:
                continue
            candidate_lands.append((score, c["name"], oid))
        candidate_lands.sort(key=lambda x: (-x[0], x[1]))
        for score, name, oid in candidate_lands[:nonbasic_land_budget]:
            STATE["deck_ids"].append(oid)

    # 4c: basics distributed proportional to pips
    BASIC_FOR = {"W": "Plains", "U": "Island", "B": "Swamp",
                 "R": "Mountain", "G": "Forest"}
    if n_colors == 0:
        STATE["basics"]["Wastes"] = target_basics
    else:
        # Distribute target_basics across CI colors using the largest-
        # remainder (Hamilton) method, with a floor of 1 per CI color
        # so you can hit each color off a mulligan-keep even when its
        # pip count is small.
        ci_pips = {color: pips.get(color, 0) for color in cmd_ci_list}
        ci_total = sum(ci_pips.values()) or 1
        # Compute exact proportional shares.
        exact = {c: target_basics * ci_pips[c] / ci_total for c in cmd_ci_list}
        floors = {c: max(1, int(exact[c])) for c in cmd_ci_list}
        used = sum(floors.values())
        remainder = target_basics - used
        if remainder > 0:
            # Distribute leftovers to the colors with the largest fractional
            # parts (Hamilton's largest-remainder method).
            fracs = sorted(cmd_ci_list,
                           key=lambda c: -(exact[c] - int(exact[c])))
            for c in fracs:
                if remainder <= 0:
                    break
                floors[c] += 1
                remainder -= 1
        elif remainder < 0:
            # Over-allocated due to min-1 floor — trim from colors with
            # the smallest exact share that have >1 basic.
            trim_order = sorted(cmd_ci_list, key=lambda c: exact[c])
            for c in trim_order:
                if remainder >= 0:
                    break
                if floors[c] > 1:
                    floors[c] -= 1
                    remainder += 1
        for color, n in floors.items():
            STATE["basics"][BASIC_FOR[color]] = n

    # ---- Phase 5: top-up if we're under 100 cards --------------------
    # Rare edge case where ranker exhausted before SPELL_TARGET.
    safety = 100
    while deck_size() < 100 and safety > 0:
        safety -= 1
        suggestions = rank_suggestions(top_n=10)
        if not suggestions:
            break
        pick = next((s for s in suggestions
                     if "LAND" not in (s["card"].get("card_types") or [])),
                    None)
        if pick is None:
            break
        STATE["deck_ids"].append(pick["card"]["oracle_id"])

    # Persist any newly-cached pair scores so subsequent runs are fast.
    _save_pair_cache()
    return redirect(url_for("deck"))


def _count_color_pips(card_ids):
    """Sum colored mana pips across a list of card oracle_ids.
    Hybrid pips ({W/U}) count half-half. Generic mana doesn't count."""
    pips = {"W": 0.0, "U": 0.0, "B": 0.0, "R": 0.0, "G": 0.0}
    for oid in card_ids:
        c = CARDS.get(oid)
        if not c:
            continue
        if "LAND" in (c.get("card_types") or []):
            continue
        cost = c.get("mana_cost", "") or ""
        # Plain mono-color pips
        for color in "WUBRG":
            pips[color] += cost.count("{" + color + "}")
        # Hybrid pips
        import re as _re
        for m in _re.finditer(r"\{([WUBRG])/([WUBRG])\}", cost):
            a, b = m.group(1), m.group(2)
            pips[a] += 0.5
            pips[b] += 0.5
        # Phyrexian pips ({W/P}) — count as full color since you usually pay it.
        for m in _re.finditer(r"\{([WUBRG])/P\}", cost):
            pips[m.group(1)] += 1.0
    return pips


def cycle_lands_for_ci_pip_weighted(cmd_ci, pips):
    """Like cycle_lands_for_ci, but ordering pairs and triples by combined
    pip weight so heavier color combinations get their cycle members
    picked first.  A deck with 40 W and 10 of each other color (5C) will
    favour Plains-related pairs (W+anything) before pairs that don't
    include W."""
    cs = "".join(sorted(c for c in cmd_ci if c in "WUBRG"))
    if len(cs) < 2:
        return []

    def pair_weight(p):
        return sum(pips.get(c, 0) for c in p)

    pairs = []
    for i in range(len(cs)):
        for j in range(i + 1, len(cs)):
            pairs.append(frozenset(cs[i] + cs[j]))
    pairs.sort(key=pair_weight, reverse=True)

    triples = []
    if len(cs) >= 3:
        from itertools import combinations
        for combo in combinations(cs, 3):
            triples.append(frozenset(combo))
        triples.sort(key=pair_weight, reverse=True)

    schedule = [
        (ORIGINAL_DUALS,   pairs,   4),
        (BATTLEBOND_LANDS, pairs,   4),
        (TRIOMES,          triples, 3),
        (SHOCK_LANDS,      pairs,   4),
        (CHECK_LANDS,      pairs,   3),
        (SLOW_LANDS,       pairs,   2),
        (FILTER_LANDS,     pairs,   2),
        (SURVEIL_LANDS,    pairs,   2),
        (BOUNCE_LANDS,     pairs,   2),
    ]

    out = []
    seen = set()
    for table, items, max_picks in schedule:
        added = 0
        for it in items:
            if added >= max_picks:
                break
            land = table.get(it)
            if land and land not in seen:
                out.append(land)
                seen.add(land)
                added += 1
    return out


@app.route("/random_commander")
def random_commander():
    import random
    pick = random.choice(COMMANDER_OPTIONS)
    STATE["commander_id"] = pick["oracle_id"]
    STATE["deck_ids"] = []
    STATE["basics"] = {b: 0 for b in BASIC_LANDS}
    STATE["filter_type"] = "All"
    return redirect(url_for("deck"))


@app.route("/export.txt")
def export_txt():
    """MTGA-style plain text deck export. Sorted alphabetically; basics aggregated."""
    from flask import Response
    cmd = commander_card()
    lines = []
    rows = []  # list of (name, count) pairs
    if cmd:
        rows.append((cmd["name"], 1))
    for c in deck_cards():
        rows.append((c["name"], 1))
    for b, n in STATE["basics"].items():
        if n > 0:
            rows.append((b, n))
    # Sort alphabetically by name (case-insensitive). Basics sit in their
    # alphabetical position naturally (e.g. "Plains" between "Pearl Medallion"
    # and "Ranger of Eos").
    rows.sort(key=lambda r: r[0].lower())
    body = "\n".join(f"{n} {name}" for name, n in rows) + "\n"
    return Response(body, mimetype="text/plain; charset=utf-8",
                    headers={"Content-Disposition": "inline; filename=deck.txt"})


IMPORT_HTML = """
<!doctype html>
<html><head>
<title>Import Deck</title>
<meta charset="utf-8">
<style>
body { font-family: system-ui, sans-serif; max-width: 900px; margin: 2em auto; padding: 0 1em; background: #1a1a1f; color: #ddd; }
h1, h2 { color: #fff; }
textarea { width: 100%; height: 250px; padding: .6em; font-family: monospace; font-size: .9em; background: #2a2a2e; color: #ddd; border: 1px solid #444; border-radius: 4px; box-sizing: border-box; }
input[type=text] { width: 100%; padding: .6em; font-size: 1.1em; background: #2a2a2e; color: #ddd; border: 1px solid #444; border-radius: 4px; box-sizing: border-box; }
.field { margin-bottom: 1em; }
label { display: block; margin-bottom: .3em; color: #ccc; font-size: .9em; }
button { background: #364; color: #cfc; border: 1px solid #6a4; padding: .6em 1.5em; border-radius: 4px; cursor: pointer; font-size: 1em; }
button:hover { background: #4a6; color: #fff; }
.errors { background: #422; color: #fcc; border: 1px solid #844; padding: .8em 1em; border-radius: 4px; margin-bottom: 1em; }
.errors ul { margin: .3em 0 0 0; padding-left: 1.5em; }
.help { color: #888; font-size: .85em; margin: .3em 0 0 0; }
a { color: #6cf; }
pre { background: #25252a; padding: .8em; border-radius: 4px; font-size: .85em; }
</style>
</head><body>
<h1>Import Deck</h1>
<p><a href="/">← Back</a></p>

{% if errors %}
<div class="errors">
  <b>Couldn't import:</b>
  <ul>{% for e in errors %}<li>{{ e }}</li>{% endfor %}</ul>
</div>
{% endif %}

<form method="POST" action="/import">
  <div class="field">
    <label for="commander">Commander name (must match a card from the list below)</label>
    <input type="text" id="commander" name="commander" value="{{ form_commander or '' }}" placeholder="e.g. Heliod, Sun-Crowned" required>
    <p class="help">The commander must be a legendary creature (or otherwise commander-eligible) and appear in the deck list below.</p>
  </div>
  <div class="field">
    <label for="decklist">Deck list (one card per line; format: <code>1 Card Name</code> or <code>35 Plains</code>)</label>
    <textarea id="decklist" name="decklist" placeholder="1 Heliod, Sun-Crowned&#10;1 Walking Ballista&#10;1 Sol Ring&#10;35 Plains" required>{{ form_decklist or '' }}</textarea>
  </div>
  <button type="submit">Import</button>
</form>

<h2>Format</h2>
<pre>1 Heliod, Sun-Crowned
1 Walking Ballista
1 Sol Ring
35 Plains</pre>
<p class="help">Lines starting with <code>//</code> or <code>#</code>, blank lines, and section headers (<code>Sideboard</code>, <code>Commander:</code>) are ignored. Quantities &gt; 1 are only respected for basic lands; non-basics are deduplicated to 1 (Commander format is singleton).</p>
</body></html>
"""


import re as _re
LINE_RE = _re.compile(r"^\s*(\d+)\s+(.+?)\s*$")


def parse_decklist(text):
    """Parse a TXT deck list. Returns (entries, parse_errors).

    entries is a list of (count, normalized_name).
    parse_errors is a list of human-readable warnings (skipped lines).
    """
    entries = []
    errors = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("//") or line.startswith("#"):
            continue
        # Skip common section headers
        if line.lower() in ("sideboard", "deck", "commander", "commander:", "main deck", "maybeboard"):
            continue
        # Strip set codes in brackets, e.g. "1 Lightning Bolt (M11) 134"
        cleaned = _re.sub(r"\s+\([A-Z0-9]{2,5}\).*$", "", line)
        cleaned = _re.sub(r"\s+\d+$", "", cleaned)  # trailing collector number
        m = LINE_RE.match(cleaned)
        if not m:
            errors.append(f"Could not parse line: {line!r}")
            continue
        count = int(m.group(1))
        name = m.group(2).strip()
        # Drop trailing flavor like "*F*" (foil) or "[CONFIDENT]"
        name = _re.sub(r"\s*\*[A-Z]+\*\s*$", "", name)
        # Normalize curly apostrophes to straight (Scryfall uses straight)
        name = name.replace("’", "'")
        entries.append((count, name))
    return entries, errors


def find_card_by_name(name):
    """Look up a card by name, with three fallback strategies:
       1. Exact match (case-insensitive).
       2. Match against the front face of a DFC ('Fable of the Mirror-Breaker'
          → 'Fable of the Mirror-Breaker // Reflection of Kiki-Jiki').
       3. Substring match — only return a hit if exactly one card matches,
          to avoid silent disambiguation errors.
    """
    if not name:
        return None
    nl = name.lower().strip()

    # 1. Exact
    if nl in NAME_TO_ID:
        return CARDS.get(NAME_TO_ID[nl])

    # 2. DFC front-face (the user pasted just the front name)
    for stored_lower, oid in NAME_TO_ID.items():
        if "//" in stored_lower:
            front = stored_lower.split("//")[0].strip()
            if front == nl:
                return CARDS.get(oid)

    # 3. Substring — only if unambiguous
    matches = [oid for nm, oid in NAME_TO_ID.items() if nl in nm]
    if len(matches) == 1:
        return CARDS.get(matches[0])
    return None


@app.route("/import", methods=["GET", "POST"])
def import_deck():
    if request.method == "GET":
        return render_template_string(IMPORT_HTML, errors=None,
                                      form_commander="", form_decklist="")

    cmd_name = (request.form.get("commander") or "").strip()
    decklist = request.form.get("decklist") or ""

    errors = []
    entries, parse_errs = parse_decklist(decklist)
    errors.extend(parse_errs)

    if not cmd_name:
        errors.append("Please provide a commander name.")
    cmd_card = find_card_by_name(cmd_name) if cmd_name else None
    if cmd_name and not cmd_card:
        errors.append(f"Commander '{cmd_name}' not found in card pool.")
    if cmd_card and not cmd_card["is_commander_eligible"]:
        errors.append(f"'{cmd_card['name']}' isn't commander-eligible (must be a legendary creature, planeswalker that can be your commander, or background).")

    if errors:
        return render_template_string(IMPORT_HTML, errors=errors,
                                      form_commander=cmd_name,
                                      form_decklist=decklist)

    cmd_ci = set(cmd_card["color_identity"])
    new_deck_ids = []
    new_basics = {b: 0 for b in BASIC_LANDS}
    seen_oracle = {cmd_card["oracle_id"]}
    skipped = []

    for count, name in entries:
        if name in BASIC_LANDS:
            color = BASIC_COLOR[name]
            if color != "C" and color not in cmd_ci:
                skipped.append(f"Skipped basic '{name}' (outside commander's color identity)")
                continue
            new_basics[name] = max(0, min(99, count))
            continue

        # Skip duplicate of the commander (already counted)
        if name.lower() == cmd_card["name"].lower():
            continue

        card = find_card_by_name(name)
        if card is None:
            skipped.append(f"Card not found: '{name}'")
            continue
        if not card["commander_legal"]:
            skipped.append(f"Skipped '{card['name']}' (not Commander-legal)")
            continue
        if not color_identity_subset(card["color_identity"], cmd_ci):
            skipped.append(f"Skipped '{card['name']}' (color identity violation)")
            continue
        if card["oracle_id"] in seen_oracle:
            continue  # singleton; ignore second copy
        seen_oracle.add(card["oracle_id"])
        new_deck_ids.append(card["oracle_id"])

    # Commit state
    STATE["commander_id"] = cmd_card["oracle_id"]
    STATE["deck_ids"] = new_deck_ids
    STATE["basics"] = new_basics
    STATE["filter_type"] = "All"

    # If there were any per-entry warnings, render an info banner with the
    # successful import + the warnings list. The deck redirect happens
    # after a brief confirmation page.
    if skipped:
        warnings_html = "".join(f"<li>{w}</li>" for w in skipped)
        return f"""
        <html><body style="font-family:system-ui;background:#1a1a1f;color:#ddd;max-width:900px;margin:2em auto;padding:0 1em;">
        <h2>Imported with {len(skipped)} warning{'s' if len(skipped)!=1 else ''}</h2>
        <p><b>Commander:</b> {cmd_card['name']}<br>
        <b>Cards added:</b> {len(new_deck_ids)}<br>
        <b>Basic lands:</b> {sum(new_basics.values())}</p>
        <h3>Warnings</h3>
        <ul style="color:#fc8;">{warnings_html}</ul>
        <p><a href="/deck" style="color:#6cf;">Continue to deck →</a></p>
        </body></html>
        """
    return redirect(url_for("deck"))


GRAPH_HTML = """
<!doctype html>
<html><head>
<title>Deck Interaction Graph — {{commander.name}}</title>
<meta charset="utf-8">
<style>
html, body { margin: 0; padding: 0; background: #0f0f14; color: #ddd; font-family: system-ui, sans-serif; height: 100%; overflow: hidden; }
.bar { background: #2a2a3a; padding: .5em 1em; display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #333; }
.bar a { color: #6cf; text-decoration: none; }
.controls { display: flex; gap: 1em; align-items: center; font-size: .85em; color: #ccc; }
.controls input[type=range] { width: 140px; }
svg { width: 100vw; height: calc(100vh - 50px); display: block; cursor: grab; background: #0f0f14; }
svg:active { cursor: grabbing; }
.link { stroke-opacity: 0.55; }
.link.combo { stroke: #f6c; stroke-width: 3; }
.link.tribal { stroke: #6c6; }
.link.tribal_lord { stroke: #6c6; stroke-width: 2; }
.link.resource_flow { stroke: #6cf; }
.link.mechanic_overlap { stroke: #888; }
.node { cursor: pointer; }
.node text { fill: #fff; font-size: 9px; pointer-events: none; text-shadow: 0 0 3px #000, 0 0 3px #000; }
.tooltip { position: absolute; background: #25252a; border: 1px solid #555; border-radius: 6px; padding: .6em; font-size: .85em; pointer-events: none; max-width: 280px; box-shadow: 0 4px 16px rgba(0,0,0,.6); }
.tooltip img { width: 200px; display: block; margin-bottom: .4em; border-radius: 4px; }
.tooltip .edges-list { font-size: .75em; color: #aaa; margin-top: .3em; }
.legend-bar { padding: .4em 1em; background: #1a1a22; font-size: .75em; color: #aaa; display: flex; gap: 1.5em; }
.legend-bar span { display: inline-flex; align-items: center; gap: .35em; }
.legend-bar .swatch { display: inline-block; width: 18px; height: 3px; border-radius: 1px; }
</style>
</head><body>
<div class="bar">
  <span><a href="/deck">← Back to deck</a></span>
  <span><b>{{commander.name}}</b> — interaction graph ({{node_count}} nodes / {{edge_count}} edges)</span>
  <div class="controls">
    <label>Min edge weight <input type="range" id="threshold" min="1" max="50" value="3" step="1">
      <span id="threshold-val">3</span></label>
    <button id="release-all" style="background:#444;color:#ddd;border:1px solid #666;padding:.2em .6em;border-radius:3px;cursor:pointer;">Release all</button>
    <label style="cursor:pointer;">
      <input type="checkbox" id="show-lands" {% if show_lands %}checked{% endif %}> Show pure-fixing lands
    </label>
  </div>
</div>
<div class="legend-bar">
  <span><span class="swatch" style="background:#f6c;"></span> Combo</span>
  <span><span class="swatch" style="background:#6c6;"></span> Tribal</span>
  <span><span class="swatch" style="background:#6cf;"></span> Resource flow</span>
  <span><span class="swatch" style="background:#888;"></span> Mechanic overlap</span>
  <span style="color:#666;">Drag nodes to reposition · scroll to zoom · stroke width ∝ edge weight</span>
</div>
<svg id="graph"></svg>
<div class="tooltip" id="tooltip" style="display:none"></div>

<script src="https://cdn.jsdelivr.net/npm/d3@7"></script>
<script>
const NODES = {{nodes_json|safe}};
const LINKS = {{links_json|safe}};

const svg = d3.select('#graph');
const w = window.innerWidth;
const h = window.innerHeight - 80;
svg.attr('viewBox', `0 0 ${w} ${h}`);

const g = svg.append('g');
const zoom = d3.zoom().scaleExtent([0.2, 4]).on('zoom', (event) => {
    g.attr('transform', event.transform);
});
svg.call(zoom);

const sim = d3.forceSimulation(NODES)
    .force('charge', d3.forceManyBody().strength(-180))
    .force('center', d3.forceCenter(w / 2, h / 2))
    .force('collision', d3.forceCollide().radius(28))
    .force('link', d3.forceLink(LINKS).id(d => d.id).distance(d => 90 - Math.min(50, d.weight)));

const linkGroup = g.append('g').attr('class', 'links');
const nodeGroup = g.append('g').attr('class', 'nodes');

let linkSel = linkGroup.selectAll('line').data(LINKS).enter().append('line')
    .attr('class', d => `link ${d.primary_type}`)
    .attr('stroke-width', d => Math.max(1, Math.min(8, d.weight / 6)));

const nodeSel = nodeGroup.selectAll('g').data(NODES).enter().append('g')
    .attr('class', 'node')
    .call(d3.drag()
        .on('start', (event, d) => {
            if (!event.active) sim.alphaTarget(0.3).restart();
            d.fx = d.x; d.fy = d.y;
        })
        .on('drag', (event, d) => { d.fx = event.x; d.fy = event.y; })
        .on('end', (event, d) => {
            if (!event.active) sim.alphaTarget(0);
            // Leave fx/fy set — node stays where the user dropped it.
            // Double-click any node to release that node back to physics.
        }))
    .on('dblclick', (event, d) => {
        // Release this node back to the simulation.
        d.fx = null; d.fy = null;
        sim.alpha(0.3).restart();
    });

nodeSel.append('image')
    .attr('href', d => d.image_small)
    .attr('x', -20).attr('y', -28)
    .attr('width', 40).attr('height', 56);

nodeSel.append('rect')
    .attr('x', -22).attr('y', -30).attr('width', 44).attr('height', 60)
    .attr('fill', 'none').attr('stroke', d => d.is_commander ? '#fc6' : 'transparent')
    .attr('stroke-width', 2).attr('rx', 3);

nodeSel.append('text').attr('y', 38).attr('text-anchor', 'middle').text(d => d.name);

// Tooltip
const tooltip = d3.select('#tooltip');
nodeSel.on('mouseover', (event, d) => {
    const incidentEdges = LINKS.filter(l => l.source.id === d.id || l.target.id === d.id);
    const edgesList = incidentEdges
        .sort((a,b) => b.weight - a.weight)
        .slice(0, 8)
        .map(e => {
            const other = e.source.id === d.id ? e.target : e.source;
            return `<div>${other.name} <span style="color:#999">(${e.primary_type}, ${e.weight.toFixed(1)})</span></div>`;
        }).join('');
    tooltip.style('display', 'block')
        .html(`<img src="${d.image_normal || d.image_small}" alt=""><b>${d.name}</b><br><small style="color:#aaa">${d.type_line}</small><div class="edges-list">${edgesList}</div>`);
}).on('mousemove', (event) => {
    tooltip.style('left', (event.clientX + 18) + 'px').style('top', (event.clientY + 18) + 'px');
}).on('mouseout', () => { tooltip.style('display', 'none'); });

sim.on('tick', () => {
    linkSel
        .attr('x1', d => d.source.x).attr('y1', d => d.source.y)
        .attr('x2', d => d.target.x).attr('y2', d => d.target.y);
    nodeSel.attr('transform', d => `translate(${d.x},${d.y})`);
});

// Release all — unpin every node.
document.getElementById('release-all').addEventListener('click', () => {
    NODES.forEach(n => { n.fx = null; n.fy = null; });
    sim.alpha(0.6).restart();
});
// Show-lands toggle — re-fetches the graph with the lands flag.
document.getElementById('show-lands').addEventListener('change', (e) => {
    window.location = '/graph' + (e.target.checked ? '?lands=1' : '');
});

// Threshold slider — hide low-weight edges client-side.
const slider = document.getElementById('threshold');
const valSpan = document.getElementById('threshold-val');
slider.addEventListener('input', () => {
    const t = +slider.value;
    valSpan.textContent = t;
    linkSel.style('display', d => d.weight >= t ? null : 'none');
});
</script>
</body></html>
"""


@app.route("/graph")
def graph():
    cmd = commander_card()
    if not cmd:
        return redirect(url_for("index"))
    show_lands = request.args.get("lands") == "1"
    deck_only = [cmd] + deck_cards()
    if len(deck_only) < 2:
        return ("<html><body style='background:#1a1a1f;color:#ddd;font-family:system-ui;padding:2em;'>"
                "<p>Add at least one card before viewing the graph.</p>"
                "<p><a href='/deck' style='color:#6cf;'>← Back to deck</a></p>"
                "</body></html>")

    if not show_lands:
        # Hide lands whose only role is to add mana (no special abilities).
        # A land is "interesting" if it produces non-mana mechanics (mill,
        # damage, draw, token, etc.) or has triggered abilities.
        def is_meaningful_land(c):
            if "LAND" not in c["card_types"]:
                return True  # not a land — keep
            if c["oracle_id"] == cmd["oracle_id"]:
                return True  # commander always shown
            # Check if any non-color mechanic is produced
            interesting = set(c.get("mechanics_produces") or []) - {"ramp_mana"}
            if interesting:
                return True
            # Check for triggered abilities or activated abilities beyond mana
            txt = c.get("oracle_text", "").lower()
            if "whenever" in txt or "when " in txt:
                return True
            return False
        deck_only = [c for c in deck_only if is_meaningful_land(c)]

    # Compute pair scores for everything in the deck
    nodes = []
    for c in deck_only:
        nodes.append({
            "id": c["oracle_id"],
            "name": c["name"],
            "type_line": c["type_line"],
            "image_small": c["image_small"],
            "image_normal": c["image_normal"],
            "is_commander": c["oracle_id"] == cmd["oracle_id"],
        })

    links = []
    EDGE_TYPE_PRIORITY = ["combo", "tribal_lord", "tribal", "resource_flow", "mechanic_overlap"]
    for i in range(len(deck_only)):
        for j in range(i + 1, len(deck_only)):
            r = score_pair(deck_only[i], deck_only[j], TRIBE_SIZES)
            if r["total"] <= 0:
                continue
            # Pick the highest-priority edge type for display.
            primary = "mechanic_overlap"
            for cand_type in EDGE_TYPE_PRIORITY:
                if any(e["type"] == cand_type for e in r["edges"]):
                    primary = cand_type
                    break
            links.append({
                "source": deck_only[i]["oracle_id"],
                "target": deck_only[j]["oracle_id"],
                "weight": r["total"],
                "primary_type": primary,
            })

    return render_template_string(
        GRAPH_HTML,
        commander=cmd,
        nodes_json=json.dumps(nodes),
        links_json=json.dumps(links),
        node_count=len(nodes),
        edge_count=len(links),
        show_lands=show_lands,
    )


@app.route("/api/search")
def api_search():
    """Substring-match against the addable card pool. Used by the manual search
    box for users who already have a specific card or combo piece in mind."""
    q = (request.args.get("q") or "").strip().lower()
    if len(q) < 2:
        return jsonify({"results": []})
    cmd = commander_card()
    if not cmd:
        return jsonify({"results": []})
    cmd_ci = set(cmd["color_identity"])
    in_deck = set(STATE["deck_ids"]) | {cmd["oracle_id"]}
    xmage_only = STATE.get("xmage_only", False) and XMAGE_NAME_SET is not None
    results = []
    for c in CARDS.values():
        if c["oracle_id"] in in_deck:
            continue
        if not c["commander_legal"]:
            continue
        if c["name"] in BASIC_LANDS:
            continue
        if not color_identity_subset(c["color_identity"], cmd_ci):
            continue
        # Honor XMage mode here too, so the user can't search-add a card
        # that's not in the XMage pool when the filter is active.
        if xmage_only and not _xmage_match(c["name"], XMAGE_NAME_SET):
            continue
        if q not in c["name"].lower():
            continue
        results.append({
            "oracle_id": c["oracle_id"],
            "name": c["name"],
            "image_small": c["image_small"],
            "type_line": c["type_line"],
        })
        if len(results) >= 25:
            break
    # Prefer matches where the query is at the start of the name.
    results.sort(key=lambda r: (not r["name"].lower().startswith(q), r["name"]))
    return jsonify({"results": results})


@app.route("/api/state")
def api_state():
    return jsonify({
        "commander_id": STATE["commander_id"],
        "deck_ids": STATE["deck_ids"],
        "deck_size": deck_size(),
        "basics": STATE["basics"],
        "filter_type": STATE.get("filter_type", "All"),
    })


if __name__ == "__main__":
    # Host / port are env-overridable so an Electron wrapper can bind
    # somewhere harmless and pass the URL into the BrowserWindow.
    host = os.environ.get("DECKBUILDER_HOST", "127.0.0.1")
    try:
        port = int(os.environ.get("DECKBUILDER_PORT", "5000"))
    except ValueError:
        port = 5000
    print(f"\nServer starting at http://{host}:{port}\n", flush=True)
    app.run(host=host, port=port, debug=False)
