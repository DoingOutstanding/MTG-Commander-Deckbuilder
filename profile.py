#!/usr/bin/env python3
"""
Deckbuilder card profiler.

Reads Scryfall oracle-cards JSON and produces an enriched profile per card with:
- Identity: oracle_id, name, mana_cost, cmc, color_identity, type_line
- Legality: commander_legal, is_legendary, is_commander_eligible
- Imagery: image_small, image_normal (Scryfall CDN URLs)
- Structural flags: from the combo detector
- Mechanics: produced (creates_tokens, places_p1p1_counters, ...) vs. cared_about
- Tribes: subtypes the card references in its oracle text
- Active zones: where the card actively interacts (BF, HAND, GRAVE, STACK)

Output: deckbuilder/cards.jsonl, keyed by oracle_id.
"""

import json
import re
import sys
from datetime import date
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent

TODAY_ISO = date.today().isoformat()

# WotC's official Commander "Game Changers" list (effective 2024-2025).
# Cards on this list count toward bracket level — e.g. bracket 1 disallows them
# entirely, bracket 2 allows a few. Hardcoded; update if WotC revises the list.
GAME_CHANGERS = {
    "Ad Nauseam", "Ancient Tomb", "Bolas's Citadel", "Chrome Mox",
    "Coalition Victory", "Consecrated Sphinx", "Cyclonic Rift",
    "Demonic Tutor", "Drannith Magistrate", "Enlightened Tutor",
    "Field of the Dead", "Fierce Guardianship", "Force of Will",
    "Gaea's Cradle", "Glacial Chasm", "Grand Arbiter Augustin IV",
    "Grim Monolith", "Humility", "Imperial Seal", "Jeska's Will",
    "Kinnan, Bonder Prodigy", "Lion's Eye Diamond", "Mana Drain",
    "Mana Vault", "Mishra's Workshop", "Mox Diamond", "Mystical Tutor",
    "Narset, Parter of Veils", "Necropotence", "Notion Thief",
    "Opposition Agent", "Orcish Bowmasters", "Panoptic Mirror",
    "Rhystic Study", "Serra's Sanctum", "Smothering Tithe",
    "Sway of the Stars", "Tainted Pact", "Thassa's Oracle",
    "The One Ring", "The Tabernacle at Pendrell Vale", "Trinisphere",
    "Underworld Breach", "Urza, Lord High Artificer", "Vampiric Tutor",
    "Vorinclex, Voice of Hunger", "Winota, Joiner of Forces",
    "Yuriko, the Tiger's Shadow",
}


def find_oracle_cards_json():
    """
    Locate the Scryfall oracle-cards-*.json bulk file.

    Search order:
      1. Command-line argument (sys.argv[1])
      2. Script directory: ./oracle-cards-*.json
      3. Parent directory:  ../oracle-cards-*.json
      4. ~/Downloads, ~/Desktop

    Download URL: https://scryfall.com/docs/api/bulk-data
    Pick "Oracle Cards" and save it next to this script.
    """
    if len(sys.argv) > 1:
        p = Path(sys.argv[1])
        if p.is_file():
            return p
        sys.exit(f"Argument {sys.argv[1]} is not a readable file.")

    candidates = []
    candidates.extend(sorted(SCRIPT_DIR.glob("oracle-cards-*.json")))
    candidates.extend(sorted(SCRIPT_DIR.parent.glob("oracle-cards-*.json")))
    home = Path.home()
    for sub in ("Downloads", "downloads", "Desktop"):
        d = home / sub
        if d.is_dir():
            candidates.extend(sorted(d.glob("oracle-cards-*.json")))

    # Filter out broken symlinks / inaccessible files (e.g. dangling Linux
    # symlinks left over from copying the workspace folder to Windows, or
    # OneDrive Files-On-Demand placeholders that haven't been hydrated).
    valid = []
    for p in candidates:
        try:
            stat = p.stat()
            if stat.st_size > 0:
                valid.append((p, stat.st_mtime))
        except OSError:
            # Inaccessible (broken symlink, permission denied, etc.) — skip.
            continue

    if valid:
        # Pick the most recently modified file if multiple match.
        return max(valid, key=lambda pair: pair[1])[0]

    sys.exit(
        "Could not find an oracle-cards-*.json file.\n"
        "Download one from https://scryfall.com/docs/api/bulk-data\n"
        "(pick the 'Oracle Cards' file) and save it next to this script,\n"
        "or pass the path explicitly:\n"
        f"    python {Path(__file__).name} <path-to-oracle-cards-XXXX.json>\n"
    )


INPUT = find_oracle_cards_json()
OUT = SCRIPT_DIR / "cards.jsonl"

# Curated list of common creature-type tribes used for tribal-synergy detection.
# Sourced empirically from Magic; doesn't have to be exhaustive — any tribe
# missed here just has weaker tribal-edge support.
TRIBES = {
    "Goblin", "Elf", "Wizard", "Knight", "Zombie", "Vampire", "Merfolk", "Human",
    "Soldier", "Warrior", "Beast", "Dragon", "Angel", "Demon", "Devil", "Faerie",
    "Sliver", "Eldrazi", "Spirit", "Cat", "Dog", "Snake", "Bird", "Insect",
    "Spider", "Wolf", "Werewolf", "Cleric", "Rogue", "Shaman", "Druid", "Monk",
    "Pirate", "Ninja", "Samurai", "Phoenix", "Hydra", "Ooze", "Treefolk",
    "Sphinx", "Construct", "Golem", "Horror", "Giant", "Dwarf", "Centaur",
    "Minotaur", "Troll", "Ogre", "Cyclops", "Beholder", "Slug", "Plant",
    "Shapeshifter", "Avatar", "Elemental", "Elder", "Citizen", "Scout",
    "Berserker", "Assassin", "Artificer", "Advisor", "Noble", "Mercenary",
    "Mystic", "Specter", "Vedalken", "Viashino", "Dryad", "Bear", "Boar",
    "Crocodile", "Frog", "Hippo", "Lizard", "Turtle", "Whale", "Fish",
    "Squid", "Crab", "Octopus", "Kraken", "Leviathan", "Serpent", "Hellion",
    "Cephalid", "Naga", "Nightmare", "Skeleton", "Lhurgoyf", "Wraith",
    "Egg", "Fungus", "Saproling", "Squirrel", "Rat", "Wurm", "Drake",
    # Tribes that were missing — World Tree, Cromat, etc. all care about Gods
    "God", "Demigod", "Avatar", "Phyrexian", "Kor", "Aetherborn", "Fox",
    "Otter", "Camel", "Rabbit", "Bat", "Ox", "Goat", "Sheep", "Mongoose",
    "Salamander", "Goblin", "Tiefling", "Dinosaur", "Crocodile", "Bringer",
    "Praetor", "Reflection", "Incarnation", "Egg", "Mole", "Mouse",
    "Nautilus", "Hippogriff", "Pegasus", "Unicorn", "Rhino", "Yeti",
    "Halfling", "Gnome", "Detective",
    # Non-creature subtypes that have tribal cards
    "Equipment", "Aura", "Vehicle", "Saga", "Background", "Class", "Role",
}

# Mechanic taxonomy. Each tag has two facets: "produces X" (the card creates,
# places, or triggers X) and "cares about X" (the card has filters or triggers
# that respond to X). A tribal/mechanic edge fires when a producer card of
# mechanic M is paired with a card that cares about M.
MECHANICS = [
    # tag,        produces_pattern,                                                  cares_pattern
    ("p1p1_counters",
        r"\+1/\+1 counter",
        r"\+1/\+1 counter|whenever .*?counter is placed|counter on a creature you control"),
    ("m1m1_counters",
        r"\-1/\-1 counter",
        r"\-1/\-1 counter"),
    ("tokens_creature",
        r"create [^.]*?(\d+/\d+|creature) token",
        r"creature token|tokens? you control|whenever a (creature|token) (you control )?enters"),
    ("tokens_treasure",
        r"create [^.]*?treasure token",
        r"treasure token|sacrifice a treasure"),
    ("tokens_food",
        r"create [^.]*?food token",
        r"food token|sacrifice a food"),
    ("tokens_clue",
        r"create [^.]*?clue token",
        r"clue token|sacrifice a clue"),
    ("tokens_blood",
        r"create [^.]*?blood token",
        r"blood token|sacrifice a blood"),
    ("sacrifice_creature",
        r"sacrifice (a |another |that )?creature",
        r"whenever .*?(dies|sacrificed)|whenever a creature you control dies|when this creature dies"),
    ("sacrifice_artifact",
        r"sacrifice (a |an )?artifact",
        r"whenever .*?artifact .*?(dies|put into a graveyard)|whenever an artifact you control"),
    ("dies_trigger_payoff",
        r"whenever a creature (you control )?dies|whenever this creature dies",
        r"sacrifice|destroy|dies"),
    ("etb_trigger",
        r"when this creature enters|whenever a creature enters",
        r"create .*creature token|return .*?creature.*?to the battlefield|enters the battlefield|exile this creature, then return"),
    ("spellslinger",
        r"whenever you cast (a |an )?(noncreature )?(instant|sorcery)",
        r"\binstant\b|\bsorcery\b"),
    ("graveyard_payoff",
        r"return .*?from .*?graveyard|cards in your graveyard|whenever a creature card is put into your graveyard",
        r"mill|discard|cards in your graveyard|put .*?graveyard"),
    ("self_mill",
        r"mill|put the top .*?cards .*?graveyard|exile cards from .*?library",
        r"cards in your graveyard|graveyard payoff"),
    # X_matters producer regexes — note we deliberately do NOT match
    # "create [...] artifact token" or "create [...] creature token". A card
    # whose only artifact reference is making one artifact token isn't really
    # an artifact-payoff card; it's just a token-maker. The tokens_creature /
    # tokens_treasure tags handle that side. Real artifact-matters payoffs
    # have one of the explicit "artifact you control" / "whenever an artifact"
    # / "each artifact you control" / "tap an artifact" patterns.
    # Plural and alt-phrasing variants matter — Padeem says "Artifacts you
    # control have hexproof", which the singular-only regex missed.
    ("artifacts_matter",
        r"(?:whenever you cast an artifact|artifacts? you control|each artifact you control|tap (?:an? |another )?(?:untapped )?artifact|whenever an artifact (?:you control )?(?:enters|dies|attacks)|whenever another artifact|sacrifice (?:an? )?artifact[^.]*?: ?(?:add|deal|draw|return|create)|for each artifact you control|artifact (?:a player|target player|an opponent) controls|artifact card in[^.]*?graveyard)",
        r"\bartifact\b"),
    ("enchantments_matter",
        r"(?:whenever you cast an enchantment|enchantments? you control|each enchantment you control|whenever an enchantment (?:you control )?(?:enters|dies)|whenever another enchantment|for each enchantment you control)",
        r"\benchantment\b"),
    ("creatures_matter",
        r"(?:whenever a creature you control|each creature you control|creatures? you control|whenever another creature|whenever you cast a creature|for each creature you control)",
        r"\bcreature\b"),
    ("planeswalkers_matter",
        r"(?:planeswalker you control|each planeswalker you control|planeswalkers? you control)",
        r"\bplaneswalker\b"),
    # NOTE: producer regexes here must NOT match the subtype's own keyword
    # ("Equipped creature gets...", "Enchant creature...", "Crew N") — every
    # Equipment / Aura / Vehicle has those phrases in its own text, which
    # would tag every member as a payoff for its own type and create a
    # fully-connected clique. Producer phrasing must reference *another*
    # member of the type, e.g. "equipment you control", "aura you control",
    # "vehicle you control", "whenever you cast an aura".
    ("equipment_matters",
        r"(?:each equipment you control|equipment you control (?:gets?|have|has|deals?|enters?|attacks)|whenever you (?:equip|cast an equipment))",
        r"\bequipment\b"),
    ("auras_matter",
        r"(?:each aura you control|auras? you control (?:gets?|have|has|deals?|enters?|attacks?)|whenever (?:you cast |you control )?an aura|whenever an aura you control)",
        r"\baura\b"),
    ("vehicles_matter",
        r"(?:each vehicle you control|vehicles? you control (?:gets?|have|has|deals?|enters?|attacks?)|whenever a vehicle (?:you control )?(?:attacks|enters|deals))",
        r"\bvehicle\b"),
    ("landfall",
        r"landfall|whenever a land enters",
        r"land enters|landfall"),
    ("lifegain",
        r"you gain \d+ life|gain life|whenever you gain life",
        r"whenever you gain life|gained life this turn"),
    ("lifeloss_opp",
        r"opponent loses .*?life|each opponent loses",
        r"whenever an opponent loses life"),
    ("damage_target",
        r"deals \d+ damage|deals damage equal",
        r"whenever .*?deals damage"),
    ("counter_spells",
        r"counter target",
        r"\bcounter target\b"),
    ("draw_cards",
        r"draw \w+ cards?",
        r"whenever you draw|cards in your hand"),
    ("discard",
        r"discard a card|each player discards",
        r"whenever .*?discard|madness"),
    ("blink_flicker",
        r"exile (this|target) creature.*?return it to the battlefield",
        r"whenever .*?enters"),
    ("equipment",
        r"equip \{|attach",
        r"\bequipment\b"),
    ("auras",
        r"enchant creature",
        r"\bauras?\b|enchant"),
    ("untap",
        r"untap target|untap up to",
        r"\bwhenever .*?untaps?"),
    ("ramp_mana",
        r"add \{[wubrgcs0-9 /]+\}|add (one|two|three) mana",
        r"more mana|\{x\} costs"),
    ("attack_trigger",
        r"whenever this creature attacks|whenever a creature attacks|whenever you attack",
        r"\bcreature attacks?\b"),
    ("voltron_attached",
        r"creature gets|enchanted creature|equipped creature",
        r"creature you control with"),
    ("counters_in_general",
        r"counter on",
        r"counter on|with .*?counters?"),

    # ---- Niche axes that catch non-obvious interactions ----

    # Untap engines: A untaps target permanent, B has a tap-cost activated ability.
    # E.g. Pemmin's Aura + Gilded Lotus, Voltaic Construct + Urza, Quicksilver
    # Dagger + mana dorks. Anything with {T}: in oracle text counts as a payoff.
    ("untap_target_payoff",
        r"untap target (?:permanent|creature|artifact|land|nonland)",
        r"\{t\}\s*[,:]"),

    # Graveyard-size scaling: A's effect scales with cards in your graveyard.
    # Lhurgoyf, Splinterfright, Tasigur, Hooting Mandrills, Treasure Cruise.
    # Pair with anything that mills you or fills your grave.
    ("graveyard_size_scales",
        r"(?:equal to|for each) (?:the number of )?(?:creature )?cards? in (?:your |all )?graveyards?",
        r"(?:mill|surveil|cycle|delve|put .{0,20}?(?:graveyard|library)|exile .{0,20}?library|discard a card)"),

    # Cost-reduction-by-type: A reduces costs by N for cards of type X.
    # Animar (creature spells cost less), Emry (artifact spells cost less),
    # Goreclaw (creatures of CMC 4+ cost less), Urza's Incubator. Pair with
    # any card of the matching type — payoff is universal cost reduction.
    ("cost_reduction_creatures",
        r"creature spells (?:you cast )?cost \{?\d|creatures cost (?:\{?\d|less)",
        r""),  # consumed by all creatures via implicit cares
    ("cost_reduction_artifacts",
        r"artifact spells (?:you cast )?cost \{?\d|artifacts cost (?:\{?\d|less)",
        r""),

    # Scry/surveil triggers: A triggers on scry/surveil, B is a scry/surveil
    # source. Cosima God of the Voyage, Tatyova, Gleam of Authority.
    ("scry_surveil_payoff",
        r"whenever you (?:scry|surveil)",
        r"\bscry \d|\bsurveil \d|look at the top \w+ cards of your library"),

    # Mana-value-matters: A scales with the mana value of permanents/spells.
    # Doc Ock, Reduce to Memory, Yawgmoth's Bargain. Hard to match B-side
    # generically; we tag this so it surfaces in displays.
    ("mana_value_matters",
        r"(?:converted mana cost|mana value)[^.]*?(?:equal|less|greater|or less)",
        r"\bmana value\b|\bconverted mana cost\b"),

    # Triggered-ability doubling — Strionic Resonator, Lithoform Engine, etc.
    # Pairs with anything that has frequent triggered abilities.
    ("trigger_doubler",
        r"copy target triggered ability|copy that triggered ability",
        r"whenever |at the beginning of"),

    # Equipped-creature-attacks triggers: Sword of Fire and Ice, Sword of
    # Light and Shadow, Hammer of Nazahn. Pair with cheap evasive creatures
    # to ferry the equipment.
    ("equipped_attacks_payoff",
        r"whenever (?:equipped creature|this creature) (?:deals (?:combat )?damage|attacks)",
        r"\bflying\b|\bunblockable\b|\bmenace\b|\bshadow\b|\bcan't be blocked\b|\bequip\b"),

    # Mana-tap-payoff: A treats every X you control as a tappable resource.
    # Urza tapping artifacts for mana, Druid Class tapping creatures, etc.
    # Strong synergy with all members of type X — bigger weight than the
    # generic X_matters edge because the resource use is concrete.
    ("artifacts_as_resource",
        r"(?:\{t\}\s*,\s*)?tap (?:an? |another )?(?:untapped )?artifact you control[^.]*?:\s*(?:add|create|deal|draw|target)",
        r""),
    ("creatures_as_resource",
        r"(?:\{t\}\s*,\s*)?tap (?:an? |another )?(?:untapped )?creature you control[^.]*?:\s*(?:add|create|deal|draw|target)",
        r""),
    ("lands_as_resource",
        r"(?:\{t\}\s*,\s*)?tap (?:an? |another )?(?:untapped )?land you control[^.]*?:\s*(?:add|create|deal|draw|target)",
        r""),

    # Mutate (Ikoria) — mutating creatures stack onto each other and
    # re-trigger every "Whenever this creature mutates" ability in the
    # pile. Two mutate cards are always synergistic with each other:
    # the second one mutates onto the first, paying mutate cost, and
    # both trigger their on-mutate abilities. Pattern matches the
    # mutate keyword cost line itself (oracle text contains "Mutate {…}").
    ("mutate",
        r"\bmutate \{",
        r"\bmutate \{|whenever this creature mutates"),
]


def normalize(s: str) -> str:
    return s.replace("’", "'") if s else s


def parse_card(card: dict):
    name = normalize(card.get("name") or "")
    if not name:
        return None
    # Skip Arena Alchemy "A-" variants — these are digital-only rebalanced
    # versions and aren't legal in paper Commander or available in XMage.
    # The non-rebalanced original card is registered separately under the
    # plain name, so dropping the A- variant doesn't lose anything.
    if name.startswith("A-") and len(name) > 2 and name[2].isupper():
        return None
    layout = card.get("layout")
    if layout in ("token", "double_faced_token", "emblem", "art_series",
                  "vanguard", "scheme", "planar"):
        return None

    # Skip unreleased cards (preview / future-set leaks). released_at is ISO YYYY-MM-DD.
    released = card.get("released_at") or ""
    if released and released > TODAY_ISO:
        return None
    type_line = card.get("type_line") or ""
    if "Card //" in type_line:  # Adventure-half / Split second-half artifacts
        pass

    # For transform / meld / flip cards (Ajani Nacatl Pariah, Delver of
    # Secrets, Westvale Abbey), the back face is a future state — the deck
    # is built around the front. Use only the front face's oracle text so
    # mechanic extraction doesn't pull in tags from the back side.
    # Modal DFCs (Hagra Mauling // Hagra Broodpit) and Adventures (Brazen
    # Borrower // Petty Theft) have both sides genuinely playable from the
    # same card, so keep using combined text for those.
    layout = card.get("layout") or ""
    text_raw = card.get("oracle_text") or ""
    if layout in ("transform", "meld", "flip") and card.get("card_faces"):
        # Front face only.
        text_raw = card["card_faces"][0].get("oracle_text") or text_raw
    text = normalize(text_raw)
    txt = text.lower()

    # Some cards (split, modal_dfc, adventure) have card_faces and an empty
    # top-level oracle_text. Concatenate both faces in that case.
    if not text and card.get("card_faces"):
        face_texts = []
        for face in card["card_faces"]:
            if face.get("oracle_text"):
                face_texts.append(face["oracle_text"])
        text = normalize("\n".join(face_texts))
        txt = text.lower()

    # Card types — use ONLY the front face for DFCs / MDFCs. Concatenating
    # both faces (e.g. "Sorcery // Land") would falsely pass type filters
    # like "Land" for cards whose front face isn't a land.
    front_face = type_line.split("//")[0].strip() if type_line else ""
    types_token = front_face.split("—")[0].lower()
    card_types = []
    for t in ("Creature", "Artifact", "Enchantment", "Planeswalker", "Land",
              "Instant", "Sorcery", "Battle", "Tribal"):
        if t.lower() in types_token:
            card_types.append(t.upper())

    subtypes = []
    if "—" in front_face:
        sub_part = front_face.split("—", 1)[1].strip()
        subtypes = [s for s in re.split(r"\s+", sub_part) if s]

    is_legendary = "Legendary" in type_line
    is_creature = "CREATURE" in card_types
    is_planeswalker = "PLANESWALKER" in card_types
    is_artifact = "ARTIFACT" in card_types

    # Commander eligibility — legendary creature, or planeswalker with
    # "can be your commander", or background, or partner.
    is_commander_eligible = (
        (is_legendary and is_creature)
        or (is_planeswalker and "can be your commander" in txt)
        or "Background" in subtypes
    )

    legalities = card.get("legalities") or {}
    commander_legal = legalities.get("commander") == "legal"

    image_small = ""
    image_normal = ""
    iu = card.get("image_uris") or {}
    if iu:
        image_small = iu.get("small") or ""
        image_normal = iu.get("normal") or ""
    elif card.get("card_faces"):
        for face in card["card_faces"]:
            fu = face.get("image_uris") or {}
            if fu:
                image_small = fu.get("small") or ""
                image_normal = fu.get("normal") or ""
                break

    # ---------- Partner / meld pairs --------------------------------
    # Scryfall tags related cards in card.all_parts. We extract the named
    # partners (combo_piece) and named meld pieces (meld_part) so the
    # interaction scorer can give massive edges to designed pairings:
    # Pir + Toothy, Bruna + Gisela, etc.
    partner_pairs = []   # named-partner cards (Pir/Toothy class)
    meld_pairs = []      # named meld cards (Bruna/Gisela class)
    for part in (card.get("all_parts") or []):
        comp = part.get("component")
        pname = normalize(part.get("name") or "")
        if not pname or pname == name:
            continue
        if comp == "meld_part":
            meld_pairs.append(pname)
        elif comp == "combo_piece":
            # combo_piece is broad — partners, story-related cards, etc.
            # Filter to actual partners by also checking "Partner with" text.
            if re.search(rf"partner with {re.escape(pname)}", text, re.IGNORECASE):
                partner_pairs.append(pname)
    # Generic "Partner" (without specifying a name) from oracle text — just
    # the bare keyword. Doesn't pair with anyone specifically.
    has_generic_partner = bool(re.search(r"^partner\b|\npartner\b", text, re.IGNORECASE)) and not partner_pairs

    # Tribes referenced — subtype names appearing in oracle text.
    # Use case-sensitive match because tribe names are TitleCase in oracle text.
    tribes_referenced = []
    for tribe in TRIBES:
        # Allow singular or plural form ("Goblin" or "Goblins")
        if re.search(rf"\b{re.escape(tribe)}s?\b", text):
            tribes_referenced.append(tribe)

    # Tribes the card IS (its subtypes intersected with TRIBES).
    tribes_is = [s for s in subtypes if s in TRIBES]

    # Mechanics produced and cared-about.
    # An empty pattern means "this side has no oracle-text cares — match only
    # via type-flag implicit cares in interactions.py". re.search('', txt)
    # ALWAYS returns a match, which silently tagged every card with every
    # empty-pattern axis, so we explicitly skip empties.
    mechanics_produces = []
    mechanics_cares = []
    for tag, prod_pat, cares_pat in MECHANICS:
        if prod_pat and re.search(prod_pat, txt):
            mechanics_produces.append(tag)
        if cares_pat and re.search(cares_pat, txt):
            mechanics_cares.append(tag)

    # Amplifier / replacement effects — cards that double a resource the
    # rest of the deck produces. They get a dedicated flag so the interaction
    # scorer can give them a much heavier edge weight (deck-defining, not
    # commodity producers).
    #
    # Patterns are intentionally broad — we want to catch alternative
    # phrasings ("creates twice", "are created instead", "if ... would be
    # put on ... twice that many ... are put on it instead", etc.).

    is_amp_tokens = bool(
        re.search(r"creates? twice that many|create that many plus|that many tokens? plus", txt)
        or re.search(r"create one or more.*?tokens?[^.]*?creates? twice", txt)
        or re.search(r"if (?:an effect|a |one or more |a player )would create (?:one or more )?tokens?", txt)
        # Mondrak / Adrix and Nev / Annie Joins Up — "are created instead"
        or re.search(r"if (?:one or more |a |an )tokens? would be created[^.]*?(?:twice|that many plus|three times)", txt)
        or re.search(r"would (?:create|put into play) (?:a |an |one or more )?tokens?[^.]*?(?:twice|that many plus)", txt)
        # Ojer Taq style — "create three times that many"
        or re.search(r"create[s]? three times that many", txt)
    )
    if is_amp_tokens:
        mechanics_produces.append("tokens_creature")
        mechanics_cares.append("tokens_creature")

    is_amp_counters = bool(
        re.search(r"puts? twice that many.*?counter|that many plus one .*?counter|put one or more counters[^.]*?puts? twice", txt)
        or re.search(r"if an effect would put one or more counters", txt)
        or re.search(r"would (?:put|place) (?:a |an additional )?\+1/\+1 counter[s]?[^.]*?(?:put|place) (?:that many plus|an additional)", txt)
        # Branching Evolution / Innkeeper's Talent — "are put on it instead"
        or re.search(r"if (?:one or more )?\+?1/\+?1 counters? would be put[^.]*?(?:twice|that many plus|are put on)", txt)
        or re.search(r"if (?:a |an )?counters? would be (?:put|placed)[^.]*?(?:twice|that many plus)", txt)
    )
    if is_amp_counters:
        mechanics_produces.append("p1p1_counters")
        mechanics_cares.append("p1p1_counters")
        mechanics_produces.append("counters_in_general")
        mechanics_cares.append("counters_in_general")
    if "proliferate" in txt:
        # Proliferate adds to existing counters of any kind.
        mechanics_produces.append("p1p1_counters")
        mechanics_produces.append("counters_in_general")
        mechanics_cares.append("counters_in_general")

    # is_amp_etb / is_amp_dies fire only when the doubler clause specifies a
    # BROAD subject ("a creature entering", "a permanent entering", "a creature
    # dying"). Cards like Cloud, Midgar Mercenary use the same "that ability
    # triggers an additional time" wording but with a SELF-RESTRICTED subject
    # ("a triggered ability of Cloud or an Equipment attached to it") — those
    # only double their own card's triggers and shouldn't be flagged as
    # universal amplifiers.
    is_amp_etb = bool(
        # "If a permanent entering [the battlefield] causes a triggered ability..."
        # Scryfall's modernized oracle text often drops "the battlefield" so
        # we make that part optional.
        re.search(r"if (?:a |an )?(?:creature|artifact|permanent|noncreature) (?:you control )?entering(?: the battlefield)? causes (?:a |an )?triggered ability", txt)
        or "panharmonicon" in name.lower()
    )
    is_amp_dies = bool(
        re.search(r"if (?:a |an )?creature (?:you control )?dying causes (?:a |an )?triggered ability", txt)
        or "teysa karlov" in name.lower()
    )

    # Self / subtype-restricted amplifiers — cards like Cloud, Midgar
    # Mercenary that double triggered abilities of themselves OR an
    # attached Equipment / Aura. These are NOT universal amps (so they
    # correctly don't set is_amp_etb), but they create a strong synergy
    # with cards of the restricted subtype that have triggered abilities
    # (Sword of X cycle's "whenever equipped creature deals damage..."
    # triggers fire twice with Cloud equipped).
    self_amp_equipment_triggers = bool(
        re.search(r"triggered abilit[^.]*?equipment[^.]*?(?:additional time|twice|once more)", txt)
    )
    self_amp_aura_triggers = bool(
        re.search(r"triggered abilit[^.]*?aura[^.]*?(?:additional time|twice|once more)", txt)
    )
    self_amp_vehicle_triggers = bool(
        re.search(r"triggered abilit[^.]*?vehicle[^.]*?(?:additional time|twice|once more)", txt)
    )

    # Mana doublers — Mana Reflection, Caged Sun, Vorinclex Voice of Hunger,
    # Mirari's Wake, Heartbeat of Spring, Doubling Cube, Nyxbloom Ancient,
    # Zhur-Taa Ancient. Pairs with anything that produces mana.
    is_amp_mana = bool(
        re.search(r"add(?:s)? that mana plus", txt)
        or re.search(r"twice that much mana|double the (?:amount|number) of[^.]*?mana", txt)
        or re.search(r"adds (?:an additional|that mana plus|twice|three times|one additional)", txt)
        # Mana Reflection / Vorinclex: "If you tap a permanent for mana, add one mana of any type that permanent produced."
        or re.search(r"if (?:you|that player|a player|an opponent)[^.]*?tap[s]?\b[^.]*?for mana[^.]*?(?:add|produce)", txt)
        # Mirari's Wake / Heartbeat / Zhur-Taa: "Whenever (you|a player) tap[s] a land for mana, ... add ... that land's type / that mana"
        or re.search(r"whenever (?:a player |you |an opponent )?tap[s]?[^.]*?(?:land|permanent) for mana[^.]*?(?:add|that mana|that land)", txt)
        # Nyxbloom Ancient: "produces three times that much mana"
        or re.search(r"produce[s]? (?:three times|twice) that much mana", txt)
        or re.search(r"would produce[^.]*?mana[^.]*?(?:twice|three times|that much plus|that much instead)", txt)
        # Mirari's Wake: "Lands you control have '{T}: Add one mana of any color this land could produce.'"
        or re.search(r"lands? you control have[^.]*?add[^.]*?(?:any color|that land could produce)", txt)
    )

    # Damage doublers — Furnace of Rath, Fiery Emancipation, Gisela Blade of
    # Goldnight (one-sided). Pairs with anything that deals damage.
    is_amp_damage = bool(
        re.search(r"if a source[^.]*?would deal damage[^.]*?(?:double|triple|twice) that damage", txt)
        or re.search(r"(?:double|triple) all damage", txt)
        or re.search(r"if[^.]*?source you control[^.]*?would deal damage[^.]*?deals (?:double|twice|three times)", txt)
        or re.search(r"deals (?:twice|three times) that much damage", txt)
        or re.search(r"that damage is (?:doubled|tripled)", txt)
    )

    # Lifegain doublers — Boon Reflection, Rhox Faithmender, Alhammarret's
    # Archive, Cradle of Vitality (kind of). Pairs with anything that gains life.
    is_amp_lifegain = bool(
        re.search(r"if you would gain life[^.]*?(?:gain twice|gain double|gain three times)", txt)
        or re.search(r"double the (?:amount of )?life you (?:would )?gain", txt)
    )

    # Card-draw doublers — Alhammarret's Archive, Sylvan Library (sort of),
    # Howling Mine (extra card on opponent's draw), Forced Fruition.
    # Pairs with anything that draws cards.
    is_amp_draw = bool(
        re.search(r"if you would draw[^.]*?draw (?:two|three) cards? instead", txt)
        or re.search(r"if (?:a |an )?(?:player|opponent) would draw[^.]*?draws? (?:two|three) cards? instead", txt)
        or re.search(r"draws? an additional card", txt)
    )

    # ---------- Archetype patterns drawn from competitive deck analysis ----
    # Each is a structural signature that surfaces a class of cards; we tag
    # them as flags AND add to the producer/cares table so the existing
    # scoring loop picks them up.

    # Blood-Artist-class death drains: "Whenever a creature dies / [you/opp]
    # loses life / drain". Aristocrats payoff that turns any death into
    # incremental damage. Examples: Blood Artist, Zulaport Cutthroat, Cruel
    # Celebrant, Bastion of Remembrance, Falkenrath Noble, Vindictive Vampire.
    is_death_drain = bool(
        re.search(r"whenever (?:a |another |this )?creature[^.]*?dies[^.]*?(?:loses?\s+\d+ life|deals?\s+\d+ damage)", txt)
        or re.search(r"whenever (?:a |another |this )?creature you control dies[^.]*?(?:loses?|gain)\s+\d+ life", txt)
    )
    if is_death_drain:
        mechanics_produces.append("death_drain")
        mechanics_cares.append("dies_trigger_payoff")  # cares about deaths happening

    # Free sacrifice outlet — repeatable, no-mana-cost ability that turns
    # creatures into value. Phyrexian Altar, Ashnod's Altar, Carrion Feeder,
    # Viscera Seer, Goblin Bombardment, Greater Gargadon, Altar of Dementia.
    # Heuristic: an activated ability whose cost is "Sacrifice [creature/
    # artifact]" with no mana symbol before the colon.
    is_free_sac_outlet = bool(
        re.search(r"sacrifice (?:a |an |another |target )(?:creature|artifact|permanent)\s*:", txt)
    )
    if is_free_sac_outlet:
        mechanics_produces.append("free_sac_outlet")
        mechanics_cares.append("sacrifice_creature")  # cares about creatures to sac

    # Protection effects — cards that grant indestructibility, hexproof,
    # protection, or phasing. Heroic Intervention, Teferi's Protection,
    # Boros Charm, Akroma's Will, Selfless Spirit, Mother of Runes,
    # Gods Willing, Veil of Summer.
    is_protection = bool(
        re.search(r"(?:creatures? you control|target creature|all creatures|other creatures|permanents you control)[^.]*?(?:gain|gains|have|has)[^.]*?(?:indestructible|hexproof|protection from|shroud)", txt)
        or "phase out" in txt or "phases out" in txt
        or re.search(r"prevent all (?:combat )?damage that would be dealt", txt)
    )
    if is_protection:
        mechanics_produces.append("protection")
        mechanics_cares.append("creatures_matter")  # protection helps creature decks

    # Mana ritual — cheap instant/sorcery that adds more mana than it costs.
    # Dark Ritual, Cabal Ritual, Songs of the Damned, Pyretic Ritual,
    # Desperate Ritual, Seething Song, Manamorphose. Pairs with combo decks
    # (storm cards, X-cost spells, etc.).
    # A spell counts as a ritual if it's instant/sorcery AND adds two-or-more
    # mana, OR adds a variable amount (X / for each / etc.). Manamorphose
    # ("Add two mana in any combination of colors") qualifies even though
    # it's net-zero, because it cycles a slot and is a storm-count piece.
    is_mana_ritual = (
        ("INSTANT" in card_types or "SORCERY" in card_types)
        and bool(
            re.search(r"add\s+\{[wubrgcs]\}\{[wubrgcs]\}", txt)  # 2+ colored
            or re.search(r"add (?:two|three|four|five|six|seven) mana", txt)
            or re.search(r"add\s+\{[2-9]\}", txt)
            or re.search(r"add\s+x mana", txt)
            or re.search(r"add\s+\{[wubrgcs]\}\s+for each", txt)  # Songs of the Damned
        )
    )
    if is_mana_ritual:
        mechanics_produces.append("mana_ritual")
        # Ritual decks care about cheap spells / storm / X-cost wincons
        mechanics_cares.append("spellslinger")
    if "panharmonicon" in name.lower() or re.search(r"if (a |an )?(creature|artifact|permanent|noncreature) (you control )?entering the battlefield causes a triggered ability", txt):
        mechanics_produces.append("etb_trigger")
        mechanics_cares.append("etb_trigger")
    if "strionic resonator" in name.lower() or re.search(r"copy target triggered ability", txt):
        # Trigger doubler — useful with anything that has triggers, very broad.
        mechanics_produces.append("etb_trigger")
        mechanics_produces.append("dies_trigger_payoff")

    # Active zones — where the card "does something" relevant for interaction.
    active_zones = set()
    if any(t in card_types for t in ("CREATURE", "ARTIFACT", "ENCHANTMENT", "PLANESWALKER", "LAND", "BATTLE")):
        active_zones.add("BF")
    if any(t in card_types for t in ("INSTANT", "SORCERY")):
        active_zones.add("STACK")  # cast triggers on stack
        active_zones.add("HAND")   # holdable in hand
    if "flashback" in txt or "jump-start" in txt or "escape" in txt or "aftermath" in txt or "embalm" in txt or "eternalize" in txt:
        active_zones.add("GRAVE")
    if "from your graveyard" in txt or "in your graveyard" in txt or "card in your graveyard" in txt:
        active_zones.add("GRAVE")
    if "cycling" in txt or "channel" in txt or "from your hand" in txt:
        active_zones.add("HAND")
    if "from exile" in txt or "foretell" in txt or "suspend" in txt or "cast .*?from exile" in txt:
        active_zones.add("EXILE")

    # Structural flags reused from the combo detector ----------------------
    flags = {}
    flags["lifegain_p1p1_target"] = bool(re.search(
        r"whenever you gain life,?\s+put (?:a |an additional )?\+1/\+1 counter[s]?\s+on target", txt))
    # Grants lifelink to other creatures (for Heliod-Ballista combo)
    flags["grants_lifelink"] = bool(
        re.search(r"target creature gains? lifelink", txt)
        or re.search(r"creatures? you control (have|gain|has) lifelink", txt)
    )
    flags["p1p1_removal_damage"] = bool(re.search(
        r"remove a \+1/\+1 counter from (?:this|~)[^:]*?:\s*(?:it|this creature|~) deals", txt))
    flags["cheap_self_blink"] = "exile this creature, then return it to the battlefield" in txt
    flags["untap_creature_on_etb"] = bool(
        re.search(r"when this creature enters[^.]*?(?:tap or )?untap target (?:permanent|creature)", txt)
        or re.search(r"when this creature enters[^.]*?target permanent[^.]*?\.\s*untap that permanent", txt))
    flags["untap_lands_on_etb"] = bool(re.search(
        r"when this creature enters[^.]*?untap up to [a-z]+ lands?", txt))
    flags["etb_produces_mana"] = bool(re.search(
        r"when this creature enters[^.]*?(?:add\s+\{|treasure token|untap up to[^.]*?lands?)", txt))
    flags["creates_copy_with_tap"] = bool(
        re.search(r"\{t\}\s*:\s*create a token that's a copy of[^.]*?(?:haste|except it has haste)", txt)
        or re.search(r"has\s+\"\{t\}\s*:\s*create a token that's a copy of this creature[^.]*?haste", txt))
    flags["wins_on_empty_library"] = name in {"Thassa's Oracle", "Laboratory Maniac", "Jace, Wielder of Mysteries"}
    flags["mass_library_exile"] = name in {"Demonic Consultation", "Tainted Pact"}
    flags["grants_undying"] = bool(re.search(r"creatures? you control[^.]*?have\s+undying", txt))
    flags["lifegain_drain_opp"] = bool(re.search(
        r"whenever you gain life,?\s+(?:each\s+opponent|target opponent|opponent)\s+loses\s+that much life", txt))
    flags["lifeloss_opp_drain_self"] = bool(re.search(
        r"whenever an opponent loses life,?\s+you gain that much life", txt))

    # Production / consumption / triggers (broad strokes for scoring)
    triggers = []
    if "etb_trigger" in mechanics_cares and is_creature:
        triggers.append("etb_self")
    if "lifegain" in mechanics_cares:
        triggers.append("lifegain")
    if "spellslinger" in mechanics_cares:
        triggers.append("spell_cast_self")
    if "dies_trigger_payoff" in mechanics_cares:
        triggers.append("creature_dies")

    # ---------- Multi-trigger detection ------------------------------
    # Compound trigger conditions ("Whenever Aang and Katara enter or
    # attack", "Whenever a creature dies or is exiled") were missed by
    # the single-action regexes above because they only matched canonical
    # phrasings ("Whenever this creature attacks"). This pass scans the
    # CONDITION half of every "When/Whenever ... ," sentence and tags
    # every action that appears, so OR-compound triggers fire all the
    # appropriate tags and name-as-subject triggers (using the card's
    # actual name) are still picked up.
    TRIGGER_ACTION_TAGS = [
        (r"\benter[s]?\b",                          "etb_self"),
        (r"\battack[s]?\b",                         "attacks_self"),
        (r"\bblock[s]?\b",                          "blocks_self"),
        (r"\bdie[s]?\b",                            "creature_dies"),
        (r"\bis exiled\b",                          "exiled_self"),
        (r"\bgain[s]? life\b",                      "lifegain"),
        (r"\blose[s]? life\b",                      "lifeloss_self"),
        (r"\bcast[s]?\b",                           "spell_cast_self"),
        (r"\bdraw[s]? a card|\bdraw[s]? cards?\b",  "draws_card"),
        (r"\bdiscard[s]?\b",                        "discards"),
        (r"\bsacrifice[s]?\b",                      "sacrifices"),
        (r"\bdeal[s]? (?:combat )?damage\b",        "deals_damage"),
        (r"\btap[s]?\b",                            "taps"),
        (r"\buntap[s]?\b",                          "untaps"),
    ]
    for m in re.finditer(r"when(?:ever)?\s+([^,.]+)[,.]", txt):
        condition = m.group(1)
        # Skip clauses that are pre-condition modifiers ("if", "as long as")
        # rather than trigger conditions.
        for action_pat, tag in TRIGGER_ACTION_TAGS:
            if re.search(action_pat, condition) and tag not in triggers:
                triggers.append(tag)

    # ---------- Combat-trigger payoffs -------------------------------
    # Cards that grant additional combat phases pair with attack-trigger
    # cards because each extra combat re-fires their triggers. Aurelia,
    # the Warleader; Combat Celebrant; Aggravated Assault; Hellkite
    # Charger; World at War; Relentless Assault; Savage Beating.
    has_extra_combat = bool(
        re.search(r"additional combat phase", txt)
        or re.search(r"after this (?:phase|main phase)[^.]*?(?:another|additional) combat", txt)
    )
    if has_extra_combat:
        mechanics_produces.append("extra_combat")
        # Extra combat producers care about creatures with attack triggers
        # and creatures with haste/evasion that benefit from re-attacks.
        mechanics_cares.append("attacks_self")

    # Goad effects — force opponents to attack, useful for political /
    # control decks and as anti-synergy with attack-trigger commanders.
    if re.search(r"\bgoad[s]?\b|attacks (?:each|every) (?:turn|combat) if able", txt):
        mechanics_produces.append("goad")

    # ---------- Land color production --------------------------------
    # For lands (and lands-on-the-back DFCs), figure out which mana colors
    # they can tap for. Used by the suggestion ranker to give triomes /
    # Command Tower / dual lands a color-fixing bonus over mono-color lands.
    colors_produced = set()
    produces_colorless = False
    fetches_basic_types = set()  # set of basic land types this fetches (for fetchlands)
    mana_scaling = False  # Cabal Coffers / Nykthos / Tron — produces variable amounts
    mana_quality = 1.0  # 1.0 = unconditional; lower = more conditional
    if "LAND" in card_types:
        # "Add one mana of any color / any type / any one color" -> all five.
        # Covers City of Brass, Mana Confluence, Command Tower, Forbidden
        # Orchard ("any color"), Reflecting Pool ("any type"), and similar.
        if re.search(r"add (?:one |two )?(?:mana )?of any (?:color|type|one color)", txt):
            colors_produced.update("WUBRG")
        # Look at every "Add ..." clause and extract the colored symbols.
        for clause in re.findall(r"add\s+([^.;]+)", txt):
            for color in "wubrg":
                if "{" + color + "}" in clause:
                    colors_produced.add(color.upper())
            if "{c}" in clause:
                produces_colorless = True

        # Fetchland detection. A typed fetch ("Search your library for a
        # Forest or Island card...") credits the colors of the basic land
        # types it can find. A generic fetch (Evolving Wilds, Terramorphic
        # Expanse, Ash Barrens cycling, Prismatic Vista) finds *any* basic,
        # so credit it as universally CI-fixing — score in app.py treats
        # that case the same as a Command Tower for color-fix purposes,
        # discounted by the life cost / tempo loss in mana_quality below.
        BASIC_TO_COLOR = {"plains": "W", "island": "U", "swamp": "B",
                          "mountain": "R", "forest": "G"}
        is_typed_fetch = bool(re.search(
            r"search your library for an? .*?(plains|island|swamp|mountain|forest)[^.]*?card[^.]*?put (?:it|that card) onto the battlefield",
            txt))
        is_generic_fetch = bool(re.search(
            r"search your library for an? basic land card[^.]*?put (?:it|that card) onto the battlefield",
            txt))
        if is_typed_fetch:
            for basic, color in BASIC_TO_COLOR.items():
                # Match patterns like "Forest or Island", "Forest, Island, or Plains".
                if re.search(rf"\b{basic}\b", txt):
                    fetches_basic_types.add(color)
                    colors_produced.add(color)
        elif is_generic_fetch:
            # Treat as wildcard — produces any of the five.
            colors_produced.update("WUBRG")
            fetches_basic_types.update("WUBRG")
        # Note: we deliberately do NOT credit "basic landcycling" with
        # 5-color fetching. Ash Barrens taps for {C}; the cycling ability
        # costs {1} AND removes the land from your battlefield in exchange
        # for a *tapped* basic. That's a one-shot emergency fix, not real
        # color production. Ash Barrens should rank as a colorless utility
        # land (~0.5 base bonus), not anywhere near a real fetchland.

        # Scaling-mana detection. Lands that can produce *more* than one
        # mana per activation, scaling with permanents you control. These
        # punch far above their weight in their archetype (mono-G Cradle,
        # mono-B Coffers, devotion decks for Nykthos, Tron pieces with
        # all three online). Flag for app-level boost.
        # Pattern A: "add an amount of mana ... equal to / for each"
        #   (Nykthos: "Add an amount of mana ... equal to your devotion")
        # Pattern B: "add {X} for each [permanent] you control"
        #   (Cabal Coffers: "Add {B} for each Swamp you control",
        #    Gaea's Cradle: "Add {G} for each creature you control",
        #    Serra's Sanctum: "Add {W} for each enchantment you control")
        # Pattern C: "add N mana ... if you control / for each"
        #   (rarely-needed catch-all)
        if (
            re.search(r"add an amount of (?:colorless |black |green |any )?mana[^.]*?(?:equal to|for each)", txt)
            or re.search(r"add\s+\{[wubrgcs]\}\s+for each\b", txt)
            or re.search(r"add (?:two|three|four|five) (?:mana|\{[wubrgcs]\})[^.]*?(?:if you control|for each)", txt)
        ):
            mana_scaling = True
        # Specifically Tron: "If you control an Urza's X and Urza's Y, add {C}{C} instead."
        if re.search(r"if you control an urza", txt):
            mana_scaling = True

        # Quality multiplier — penalises conditional mana production. The
        # multipliers stack so a land with multiple drawbacks compounds them.
        # Tundra (true dual) = 1.0; Sacred Foundry (shock) ≈ 0.85;
        # Indatha Triome (always tapped) = 0.7; City of Brass (damage) = 0.7;
        # Mana Confluence (pay life) = 0.7; Scene of the Crime (tap a
        # creature) = 0.5; Mirrex / Vivid lands (counters) ≈ 0.6.

        # Land-cycle detection. We assign cycle-specific quality multipliers
        # so the color-fix score reflects EDH-relevant power, not the raw
        # "is this tapped" check. Several cycles enter tapped only in
        # narrow conditions that almost never bite in Commander.
        #
        # Cycles in approximate descending power tier (and the multiplier
        # applied below):
        #   Original duals (Tundra, Underground Sea, ...)        1.00
        #   Battlebond duals (Bountiful Promenade, ...)          0.95  (untapped if ≥2 opponents — always true in EDH)
        #   Shock lands (Sacred Foundry, ...)                    0.85
        #   Check lands (Dragonskull Summit, ...)                0.90
        #   Pain lands (Battlefield Forge, ...)                  0.85
        #   Slow lands (Dreamroot Cascade, ...)                  0.80  (tapped unless you control 2+ other lands)
        #   Surveil lands (Elegant Parlor, ...)                  0.78  (always tapped, but ETB surveil is real value)
        #   Filter lands (Cascade Bluffs, ...)                   0.85
        #   Bounce lands (Boros Garrison, ...)                   0.75  (tapped + bounce, but +1 land drop)
        #   Triomes (Indatha, Xander's Lounge, ...)              0.70
        #   Vanilla guildgates / always-tapped duals             0.70
        is_shock = bool(re.search(
            r"as .{0,40}? enters,? you may pay [12] life", txt))
        is_battlebond = bool(re.search(
            r"unless you have two or more opponents|"
            r"unless an opponent controls more lands than you",
            txt))
        is_slow_land = bool(re.search(
            r"unless you control two or more other lands", txt))
        is_check_land = bool(re.search(
            r"unless you control an? (?:plains|island|swamp|mountain|forest)",
            txt))
        # "as ~ enters, surveil 1" + "this enters tapped" combo
        is_surveil_land = bool(re.search(
            r"surveil 1[^.]*?\.[^.]*?(?:this land|enters tapped)|enters tapped[^.]*?\.[^.]*?surveil 1",
            txt))
        is_bounce_land = bool(re.search(
            r"when (?:this|\w+) enters[^.]*?return (?:a |an |target )?land you control",
            txt))
        is_filter_land = bool(re.search(r"\{[wubrg]/[wubrg]\}", txt))
        # Triome detection: 3 colors produced + cycling.
        is_triome = (len(colors_produced & set("WUBRG")) == 3
                     and bool(re.search(r"cycling \{", txt)))
        # Original dual: produces 2 colors AND has no drawback at all.
        # The simplest way to tell is "no enters-tapped, no pay life, no
        # damage, no extra cost". Easiest: very short oracle text + 2 colors.
        is_original_dual = (
            len(colors_produced & set("WUBRG")) == 2
            and not is_shock and not is_battlebond and not is_slow_land
            and not is_check_land and not is_surveil_land and not is_filter_land
            and not bool(re.search(r"\benters tapped\b|enters the battlefield tapped|deals?\s+\d+ damage|pay\s+\d+ life", txt))
        )

        # Generic "enters tapped" without a more specific cycle = guildgate.
        generic_etb_tapped = bool(re.search(r"\benters tapped\b|enters the battlefield tapped", txt))

        # Apply the multiplier. ORDER MATTERS — most-specific cycles first.
        if is_original_dual:
            pass  # 1.0 — no penalty
        elif is_battlebond:
            mana_quality *= 0.95
        elif is_shock:
            mana_quality *= 0.85
        elif is_check_land:
            mana_quality *= 0.90
        elif is_filter_land:
            mana_quality *= 0.85
        elif is_slow_land:
            mana_quality *= 0.80
        elif is_surveil_land:
            mana_quality *= 0.78
        elif is_bounce_land:
            mana_quality *= 0.75
        elif is_triome:
            mana_quality *= 0.70
        elif generic_etb_tapped:
            mana_quality *= 0.70
        # Backstop: keep the legacy "check/pain" detection so anything we
        # didn't pattern-match above still gets some discount.
        elif re.search(r"unless you control (?:a |an |two )", txt):
            mana_quality *= 0.9

        # Per-mana recurring costs (skip if already counted as shock).
        # Mana Confluence / City of Brass are 5-color untapped — better
        # than always-tapped guildgates despite the per-tap cost — so
        # the multiplier is gentler than the always-tapped 0.7. Pain land
        # cycle (Caves of Koilos: 1 damage per colored tap) gets the same
        # treatment.
        # Fetchlands also have a "pay 1 life" but the cost is paid once
        # per-fetch, not per-mana — handled in the fetchland branch above
        # (we record fetches_basic_types) so we skip the generic
        # "{T}, pay X life" multiplier if the land is a fetchland.
        is_fetch = bool(fetches_basic_types) and not is_generic_fetch and not is_typed_fetch
        # Actually: is_fetch should be true any time we recorded a
        # fetchland. Re-derive from the regex flags directly.
        is_fetch = is_typed_fetch or is_generic_fetch
        if not is_shock:
            if not is_fetch and re.search(r"\{t\}\s*,\s*pay\s+\d+ life", txt):
                mana_quality *= 0.85  # Mana Confluence-style (was 0.7)
            if re.search(r"deals?\s+\d+ damage to (?:you|its controller|that player)", txt):
                mana_quality *= 0.85  # City of Brass / pain lands (was 0.7)
            if is_fetch:
                mana_quality *= 0.85  # fetchland life cost + tempo loss

        if re.search(r"\{t\}\s*,\s*tap (?:an? |another )?(?:untapped )?(?:creature|artifact|permanent) you control", txt):
            mana_quality *= 0.5
        # "Spend this mana only to cast a [type] spell" — Cavern of Souls,
        # Ancient Ziggurat, Ally Encampment, Unclaimed Territory, A-Base
        # Camp, etc. The mana is real but type-restricted, so the card is
        # only valuable in matching tribal/type decks.  Halve the score.
        if re.search(r"spend this mana only to (?:cast|activate)", txt):
            mana_quality *= 0.5
        # Energy / charge-counter-gated any-color (Aether Hub).
        if re.search(r"pay \{e\}.*?add one mana of any color|"
                     r"add \{[wubrg]\}\.[^.]*?activate.*?energy", txt):
            mana_quality *= 0.6
        # Conditional "if you're in a city that starts with..." flavor lands.
        if re.search(r"if you're in a city that starts with", txt):
            mana_quality *= 0.4
        # Activated-cost any-color (Abstergo Entertainment: "{1}, {T}: Add
        # one mana of any color"). Has a per-tap mana cost on top of the
        # tap, which is much worse than free mana production.
        if re.search(r"\{[1-9]\},?\s*\{t\}[:,].{0,40}?add one mana of any color", txt):
            mana_quality *= 0.5
        # Self-sacrifices on a condition (Glimmervoid, Crosis's Catacombs,
        # Lotus Vale-style "sacrifice if X").
        if re.search(r"sacrifice (?:this|it)[^.]*?unless|"
                     r"if you control no [^.]*?, sacrifice (?:this|it)|"
                     r"if there are no [^.]*?counters[^.]*?sacrifice (?:this|it)|"
                     r"this land doesn't untap",
                     txt):
            mana_quality *= 0.5
        # Counter-fade lands (Vivid cycle, Gemstone Mine).  These have
        # finite charges before they fall off — already partially covered
        # by the "remove a counter" rule above, but Gemstone Mine uses
        # "mining counters" which slips past.
        if re.search(r"remove a mining counter|enters? with (?:three|four|five) [a-z]+ counters?", txt):
            mana_quality *= 0.6
        # Drawback: gives an opponent a creature/token when tapped
        # (Forbidden Orchard).  Symmetric drawback in a multiplayer game
        # is mild but real.
        if re.search(r"target opponent creates? a", txt):
            mana_quality *= 0.85
        # Sac-itself-for-mana-effect (Archaeological Dig, Tarnished
        # Citadel-style cost).  These produce mana exactly once.
        if re.search(r"\{t\},?\s*sacrifice (?:this|it)[^.]*?:\s*add", txt):
            mana_quality *= 0.5
        # Conditional auto-include "command tower / command mine / command
        # power plant" cycle from MH3 — relies on having all three of an
        # uncommon-named cycle, much harder than tron.  De-rate.
        if re.search(r"if you control a command (?:tower|mine|power plant) and",
                     txt):
            mana_quality *= 0.5
        # Channelstorm / "storm but on a land" Blustering Barnyard etc.
        # Most of these are joke / Un-set lands.  Treat as low quality.
        if re.search(r"\bchannelstorm\b|storm \(pretend it works on a land\)", txt):
            mana_quality *= 0.4
        # "When this land is turned face up" — Disguise/Manifest lands
        # (Branch of Vitu-Ghazi). Mana is conditional on flipping; until
        # then it taps for {C} only.
        if re.search(r"when this (?:land|permanent) is turned face up,?\s*add",
                     txt):
            mana_quality *= 0.6
        # "Instead add one mana of any color" — the "any color" credit is
        # conditional on a flag/counter that usually isn't there (Gemstone
        # Caverns: only with luck counter; certain transform/flip lands).
        # Heavy penalty.
        if re.search(r"instead,? add (?:one |two |three )?mana", txt) or \
           re.search(r"instead,? add \{", txt) or \
           re.search(r"instead,? add one mana of any color", txt):
            mana_quality *= 0.5
        # Activated-cost per-color mana ({1}, {T}: Add {B}; {2}, {T}: Add
        # {U} or {R}).  Castle Sengir, Crypt of the Eternals, Tarnished
        # Citadel, Pillar of the Paruns-style.  The {T} alone gives {C}
        # but colored mana costs additional generic.
        if re.search(r"\{[1-9]\},?\s*\{t\}[:,].{0,30}?add\s+\{[wubrg]\}", txt):
            mana_quality *= 0.6
        # Sacrifice-a-permanent-as-cost any-color (Lazotep Quarry).
        if re.search(r"sacrifice an? (?:creature|artifact|permanent)[^.]*?:\s*add", txt):
            mana_quality *= 0.6
        # Self-bounce after use (Undiscovered Paradise — returns to hand).
        if re.search(r"return this (?:land|permanent) to its owner['s ]+hand", txt):
            mana_quality *= 0.5
        # Opponent gains control (Rainbow Vale — gives away each turn).
        if re.search(r"(?:an? )?opponent gains? control of (?:this|it)", txt):
            mana_quality *= 0.4
        # Gate-tribal-only any-color (Gond Gate, Plaza of Harmony) — only
        # works when you have other Gates, otherwise just colorless.
        if re.search(r"any (?:color|type) (?:that )?an? gate you control could produce", txt):
            mana_quality *= 0.3
        # Reflecting Pool / Exotic Orchard variant: "any type a land you
        # control could produce" — universal in multi-color decks, very
        # high quality. Bump it.  (Already detected as 5-color via the
        # "any color/type/one color" regex; this just keeps quality at 1.0
        # since there's no real drawback.)
        # Conspiracy "draft this card" lands (Paliano, The Grey Havens
        # legendary mode, etc.) — relies on draft-time choices that
        # aren't available in Constructed/Commander.
        if re.search(r"reveal this card as you draft it|"
                     r"any color chosen as you drafted|"
                     r"any color among (?:legendary )?(?:creature )?cards in (?:your )?graveyard", txt):
            mana_quality *= 0.3
        # "Say the secret word" / Un-set joke triggers.
        if re.search(r"say the secret word|spend this mana only to pay un-costs|"
                     r"any color that appears on your top|"
                     r"mark one of [a-z'\- ]+'s? unmarked nodes", txt):
            mana_quality *= 0.3
        if re.search(r"remove a[n]? (?:charge|verse|fade|brick|storage|loot|oil|fuse|study) counter", txt):
            mana_quality *= 0.6
        if re.search(r"\bchosen type\b|chosen creature type|creature type that (?:was )?chosen", txt):
            mana_quality *= 0.7
        if re.search(r"only if you control|only on your turn", txt):
            mana_quality *= 0.6
        if re.search(r"reveal a card from your hand", txt):
            mana_quality *= 0.7
        # "Activate only if this land entered this turn" — Mirrex pattern.
        # Effectively a one-shot color fix over the card's lifetime; heavy
        # penalty so these don't outrank true duals or triomes.
        if re.search(r"activate only if this (?:land|permanent|creature) (?:has |)?entered (?:this|the battlefield this) turn", txt):
            mana_quality *= 0.3
        mana_quality = max(0.25, mana_quality)

    # ---------- Ramp tagging -----------------------------------------
    # A card counts as "ramp" if it accelerates mana or fetches lands.
    # Pure lands themselves are excluded — they're "lands", not "ramp".
    is_ramp = False
    if "LAND" not in card_types:
        if (
            # Mana rocks / mana dorks: {T}: Add ...
            re.search(r"\{t\}\s*[,:]?\s*[^.]{0,40}?\badd\s+(?:\{|one |two |three |four |x )", txt)
            # Tap-other-artifact for mana (Urza-style, Citanul Hierophants)
            or re.search(r"\{t\}\s*,\s*tap (?:an? |another )?(?:untapped )?(?:artifact|creature) you control[^.]*?:\s*add", txt)
            # Land tutors
            or re.search(r"search your library for [^.]*?(?:basic\s+land|land\s+card|forest|island|mountain|plains|swamp|gate|triome|shock)", txt)
            # Land cheats from hand/grave/library to battlefield
            or re.search(r"put (?:a |an |target |that )?land card[^.]*?onto the battlefield", txt)
            or re.search(r"play an additional land", txt)
            # Treasure / Gold / Powerstone token producers
            or re.search(r"create .{0,40}?(?:treasure|gold|powerstone) token", txt)
            # Mana doublers / replacement mana effects
            or re.search(r"would (?:produce|add)[^.]*?produces?[^.]*?(?:twice|additional|plus)", txt)
            or re.search(r"adds an additional|add that mana plus", txt)
            # ETB-untap-lands (mana-positive on blink)
            or re.search(r"when this creature enters[^.]*?untap up to[^.]*?lands?", txt)
            # Free spells / cost reduction common ramp pieces
            or re.search(r"reduce[^.]*?(?:cost|mana cost) of", txt) and "land" in txt
        ):
            is_ramp = True

    # ---------- Subtype cost reduction (Slinza-style) -------------------
    # "Beast spells you cast cost {2} less to cast" — restricted to a
    # specific creature subtype. This is stronger than a regular tribal
    # reference because EVERY beast spell becomes cheaper, which is a
    # direct in-game advantage on every cast. Tag the card with the
    # subtype it discounts so we can pair it with cards of that subtype.
    subtype_cost_reduction = []
    for tribe in TRIBES:
        # Match "Beast spells", "Goblin creature spells", "Dragon spells you cast cost"
        plural = tribe.lower() + "s?"  # crude plural-or-singular
        pat = rf"\b{plural}\s+(?:creature\s+)?spells\s+(?:you cast\s+)?cost\s+(?:\{{|\d|less)"
        if re.search(pat, txt):
            subtype_cost_reduction.append(tribe)

    # ---------- Bracket tagging --------------------------------------
    is_game_changer = name in GAME_CHANGERS

    # Tutors: "Search your library for a card" with broad scope. Narrow tutors
    # (search for a basic land, search for a specific creature type) don't
    # count toward the bracket-1 tutor restriction.
    is_tutor = bool(
        re.search(r"search your library for a card", txt)
        or re.search(r"search your library for an instant or sorcery", txt)
        or re.search(r"search your library for a creature card", txt)
        or re.search(r"search your library for a nonland", txt)
        or re.search(r"search your library for an? \w+ card[^.]*?put", txt)
    )
    # Skip narrow basic-land tutors
    if is_tutor and re.search(r"search your library for (a |an )?(basic |snow )?land", txt):
        # Probably just a land tutor; not a "bracket-restricted" tutor unless
        # also searches for a card (e.g. "for a land or a card").
        if not re.search(r"search your library for a card", txt):
            is_tutor = False

    is_mld = bool(
        re.search(r"destroy all lands", txt)
        or re.search(r"destroy each land", txt)
        or re.search(r"each player sacrifices all lands", txt)
        or name in {"Armageddon", "Ravages of War", "Catastrophe", "Wildfire",
                    "Jokulhaups", "Obliterate", "Worldslayer"}
    )

    is_extra_turn = bool(
        re.search(r"take an extra turn", txt)
        or re.search(r"an additional turn", txt)
    )

    # Highest implied bracket level for this individual card. The deck's
    # overall bracket is the max of its cards' brackets.
    if is_game_changer:
        card_bracket = 4
    elif is_tutor or is_mld or is_extra_turn:
        card_bracket = 3
    else:
        card_bracket = 2

    return {
        "oracle_id": card.get("oracle_id"),
        "name": name,
        "mana_cost": card.get("mana_cost") or "",
        "cmc": card.get("cmc") or 0,
        "color_identity": card.get("color_identity") or [],
        "colors": card.get("colors") or [],
        "type_line": type_line,
        "card_types": card_types,
        "subtypes": subtypes,
        "is_legendary": is_legendary,
        "is_creature": is_creature,
        "is_artifact": is_artifact,
        "is_planeswalker": is_planeswalker,
        "is_commander_eligible": bool(is_commander_eligible),
        "commander_legal": commander_legal,
        "image_small": image_small,
        "image_normal": image_normal,
        "tribes_is": tribes_is,
        "tribes_referenced": tribes_referenced,
        "mechanics_produces": mechanics_produces,
        "mechanics_cares": mechanics_cares,
        "active_zones": sorted(active_zones),
        "triggers": triggers,
        "flags": flags,
        "oracle_text": text,
        "released_at": released,
        "is_game_changer": is_game_changer,
        "is_tutor": is_tutor,
        "is_mld": is_mld,
        "is_extra_turn": is_extra_turn,
        "is_ramp": is_ramp,
        "is_enchantment": "ENCHANTMENT" in card_types,
        "is_battle": "BATTLE" in card_types,
        "is_amp_tokens": is_amp_tokens,
        "is_amp_counters": is_amp_counters,
        "is_amp_etb": is_amp_etb,
        "is_amp_dies": is_amp_dies,
        "partner_pairs": partner_pairs,
        "meld_pairs": meld_pairs,
        "has_generic_partner": has_generic_partner,
        "subtype_cost_reduction": subtype_cost_reduction,
        "self_amp_equipment_triggers": self_amp_equipment_triggers,
        "self_amp_aura_triggers": self_amp_aura_triggers,
        "self_amp_vehicle_triggers": self_amp_vehicle_triggers,
        "is_amp_mana": is_amp_mana,
        "is_amp_damage": is_amp_damage,
        "is_amp_lifegain": is_amp_lifegain,
        "is_amp_draw": is_amp_draw,
        "is_death_drain": is_death_drain,
        "is_free_sac_outlet": is_free_sac_outlet,
        "is_protection": is_protection,
        "is_mana_ritual": is_mana_ritual,
        "colors_produced": sorted(colors_produced),
        "produces_colorless": produces_colorless,
        "fetches_basic_types": sorted(fetches_basic_types),
        "mana_scaling": mana_scaling,
        "mana_quality": round(mana_quality, 3),
        "card_bracket": card_bracket,
        "edhrec_rank": card.get("edhrec_rank") or 999999,
    }


def main():
    sys.stderr.write(f"Loading {INPUT.name}...\n")
    data = json.loads(INPUT.read_text(encoding="utf-8"))
    sys.stderr.write(f"  {len(data)} cards in source\n")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    seen = set()
    with OUT.open("w", encoding="utf-8") as out:
        for card in data:
            prof = parse_card(card)
            if prof is None or prof["oracle_id"] is None:
                continue
            if prof["oracle_id"] in seen:
                continue
            seen.add(prof["oracle_id"])
            out.write(json.dumps(prof, ensure_ascii=False) + "\n")
            written += 1
    sys.stderr.write(f"Wrote {written} card profiles to {OUT}\n")


if __name__ == "__main__":
    main()
