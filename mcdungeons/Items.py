from typing import Dict, List, NamedTuple, Optional, Tuple
try:
    from BaseClasses import Item, ItemClassification
except ImportError:
    # Client-only environments (no full Archipelago install) can't import
    # BaseClasses - same situation _apworld_data.py's docstring describes
    # for Locations.py/ZoneData.py. Nothing here actually needs the real
    # Item class (nothing instantiates one - this module only ever builds
    # ItemInfo tuples), and ItemClassification's specific bit VALUES don't
    # matter client-side either - the client only ever reads ItemInfo.code
    # to resolve a received item ID back to a name, never .classification.
    # This is a client-only fallback, never a generation-time one: real
    # generation always has BaseClasses available, so this branch is
    # unreachable there and can never silently produce a wrong seed.
    # Nothing here actually instantiates a real Item (this module only
    # ever builds ItemInfo tuples for its own tables) - MCDungeonsItem
    # below still needs SOMETHING subclassable though, hence a plain
    # placeholder rather than None.
    class Item:
        pass

    class ItemClassification:
        progression = 0
        useful = 0
        filler = 0
        trap = 0
        skip_balancing = 0

from .ZoneData import ZONES, ANCIENT_HUNT_ZONES, ZONES_BY_NAME


class ItemInfo(NamedTuple):
    code: Optional[int]
    classification: ItemClassification


BASE_ITEM_ID = 0xDC0000  # arbitrary offset - replace with your registered AP game ID block

_next_id = [BASE_ITEM_ID]


def _alloc_id() -> int:
    _next_id[0] += 1
    return _next_id[0]


# One "Mission Access" progression item per zone (base + DLC). Whether a
# given zone's item actually gets placed in the pool is decided in __init__.py
# based on which DLC options are enabled.
MISSION_ACCESS_ITEMS: Dict[str, ItemInfo] = {
    f"{z.display_name} Access": ItemInfo(_alloc_id(), ItemClassification.progression)
    for z in ZONES
}

# Secret-exit access, only meaningful when secret_missions_require_secret is on.
SECRET_ACCESS_ITEMS: Dict[str, ItemInfo] = {
    f"{z.display_name} Secret Access": ItemInfo(_alloc_id(), ItemClassification.progression)
    for z in ZONES if z.secret
}

# Single item gating all three Ancient Hunts at once.
ANCIENT_HUNT_ACCESS_ITEM = "Ancient Hunt Access"

# Progressive enchant slot capacity. give_item.py's give_random_item /
# give_location_reward now take a num_slots argument (0-3) controlling
# how many enchant slots a granted item actually gets, instead of always
# granting the max 3. This item is placed in the pool 3 TIMES - a
# player's current tier is however many copies they've received so far
# (0 copies received = 0 slots, all the way up to 3). The client (see
# apply_item_reward.py / watch_item_rewards) is responsible for counting
# how many of these it's received and passing that count as num_slots on
# every subsequent give_random_item/give_location_reward call.
PROGRESSIVE_ENCHANT_SLOT_ITEM = "Progressive Enchant Slot"
PROGRESSIVE_ENCHANT_SLOT_COPIES = 3  # 0/1/2/3 slots - one copy per tier above 0

# Progressive pickup unlock. Options.py's ProgressivePickups describes 4
# tiers (0: arrows/tools/weapons/armor, 1: adds health items, 2: adds
# potions, 3: adds TNT - the final tier), so 3 copies are needed to climb
# from the starting tier 0 up to the final tier 3, same one-copy-per-step
# pattern as PROGRESSIVE_ENCHANT_SLOT_ITEM above. Marked `progression` (not
# `filler`) specifically so it's used in logic/reachability and so
# `!hint`/auto-hinting treat it like any other required item, not junk -
# without a real item backing this option, there was nothing to hint at
# all and the option's tiers could never actually progress once toggled
# on. Only added to the pool when the option is enabled (see __init__.py).
PROGRESSIVE_PICKUP_ITEM = "Progressive Pickup"
PROGRESSIVE_PICKUP_COPIES = 3  # tier 0 (start) -> 1 -> 2 -> 3 (final tier)

# Tiered emerald filler rewards. The amount each one grants is looked up
# by dungeons_reader.py (via FILLER_ITEM_EMERALD_AMOUNTS below) when the
# corresponding item is received over the network - it's a straight
# addition to the player's current emerald total, not a new item.
FILLER_ITEM_EMERALD_AMOUNTS: Dict[str, int] = {
    "100 Emeralds": 100,
    "300 Emeralds": 300,
    "500 Emeralds": 500,
}

FILLER_ITEMS: Dict[str, ItemInfo] = {
    name: ItemInfo(_alloc_id(), ItemClassification.filler)
    for name in FILLER_ITEM_EMERALD_AMOUNTS
}

# Item-equipment filler rewards - actual random weapons/armor granted
# in-game via give_item.py's give_random_item() when received (see
# apply_item_reward.py, which lives alongside give_item.py outside this
# world folder, same reasoning as ap_client.py/dungeons_reader.py: these
# act against a live game process, not at generation time). Distinct
# from FILLER_ITEM_EMERALD_AMOUNTS above - those add emeralds directly;
# these grant a real item with random matching enchants, same as
# give_random_item(category=...) already tested standalone.
ITEM_REWARD_CATEGORIES: Dict[str, Optional[str]] = {
    "Random Melee Weapon": "Melee",
    "Random Ranged Weapon": "Ranged",
    "Random Armor": "Armor",
    "Random Artifact": "Artifact",  # no enchant slots - give_random_item skips enchant logic entirely for these
    "Random Item": None,  # any of the three EQUIPMENT categories above (not Artifact - ask for that explicitly)
}

# ------------------------------------------------------------------
# Location-specific reward items: the reward's pool is fixed by which
# ITEM this is (decided here, at generation time), not by the player's
# current in-game zone or unlock progress at receive time - e.g. "Squid
# Coast Artifact Reward" always draws from Squid Coast's curated pool
# (Fireworks Arrow / Fishing Rod - see location_reward_pools.py) no
# matter where the player is or whether they've even reached Squid
# Coast yet. Selection WITHIN a pool is still random; which pool applies
# is not.
#
# This list only needs the (zone, category) PAIRS that have a pool
# defined - the actual pool CONTENTS live in location_reward_pools.py,
# which is a client-side file (alongside give_item.py), not imported
# here, since world generation and the game client may not share an
# environment. Keep this list in sync with location_reward_pools.py's
# keys by hand when you add a new zone's pool - this only decides which
# distinct reward ITEM TYPES exist and get IDs; apply_item_reward.py
# (also client-side) is what actually resolves a pool's contents at
# grant time.
# ------------------------------------------------------------------

LOCATION_REWARD_TYPES: List[Tuple[str, str]] = [
    ("squidcoast", "Artifact"),
    ("creeperwoods", "Melee"),
    ("creeperwoods", "Ranged"),
    ("creeperwoods", "Armor"),
    ("creeperwoods", "Artifact"),
    ("soggyswamp", "Melee"),
    ("soggyswamp", "Ranged"),
    ("soggyswamp", "Armor"),
    ("soggyswamp", "Artifact"),
    ("pumpkinpastures", "Melee"),
    ("pumpkinpastures", "Ranged"),
    ("pumpkinpastures", "Armor"),
    ("pumpkinpastures", "Artifact"),
    ("cacticanyon", "Melee"),
    ("cacticanyon", "Ranged"),
    ("cacticanyon", "Armor"),
    ("cacticanyon", "Artifact"),
    ("redstonemines", "Melee"),
    ("redstonemines", "Ranged"),
    ("redstonemines", "Armor"),
    ("redstonemines", "Artifact"),
    ("deserttemple", "Melee"),
    ("deserttemple", "Ranged"),
    ("deserttemple", "Armor"),
    ("deserttemple", "Artifact"),
    ("fieryforge", "Melee"),
    ("fieryforge", "Ranged"),
    ("fieryforge", "Armor"),
    ("fieryforge", "Artifact"),
    ("highblockhalls", "Melee"),
    ("highblockhalls", "Ranged"),
    ("highblockhalls", "Armor"),
    ("highblockhalls", "Artifact"),
    ("obsidianpinnacle", "Melee"),
    ("obsidianpinnacle", "Ranged"),
    ("obsidianpinnacle", "Armor"),
    ("obsidianpinnacle", "Artifact"),
]


def location_reward_item_name(zone_internal_name: str, category: str) -> str:
    zone = ZONES_BY_NAME[zone_internal_name]
    return f"{zone.display_name} {category} Reward"


# name -> (zone_internal_name, category), for apply_item_reward.py's dispatch.
LOCATION_REWARD_ITEM_ZONE_CATEGORY: Dict[str, tuple] = {
    location_reward_item_name(zone, cat): (zone, cat)
    for zone, cat in LOCATION_REWARD_TYPES
}

LOCATION_REWARD_ITEMS: Dict[str, ItemInfo] = {
    name: ItemInfo(_alloc_id(), ItemClassification.filler)
    for name in LOCATION_REWARD_ITEM_ZONE_CATEGORY
}

ITEM_REWARD_FILLERS: Dict[str, ItemInfo] = {
    name: ItemInfo(_alloc_id(), ItemClassification.filler)
    for name in ITEM_REWARD_CATEGORIES
}

ITEM_TABLE: Dict[str, ItemInfo] = {
    **MISSION_ACCESS_ITEMS,
    **SECRET_ACCESS_ITEMS,
    ANCIENT_HUNT_ACCESS_ITEM: ItemInfo(_alloc_id(), ItemClassification.progression),
    PROGRESSIVE_ENCHANT_SLOT_ITEM: ItemInfo(_alloc_id(), ItemClassification.progression),
    PROGRESSIVE_PICKUP_ITEM: ItemInfo(_alloc_id(), ItemClassification.progression),
    **FILLER_ITEMS,
    **ITEM_REWARD_FILLERS,
    **LOCATION_REWARD_ITEMS,

    # Victory item carriers (see Regions.py's _place_victory_event) - NOT
    # purely virtual AP "events" (those need address=None, and the whole
    # point here is these stay on a REAL, network-checkable location -
    # dungeons_reader.py reports them like any other check). A real
    # location holding an item with code=None is invalid (AP core's
    # Main.py asserts on it: "item code None should be event, location
    # .address should then also be None") - these two need real,
    # allocated codes instead. Added at the very end, AFTER every other
    # _alloc_id() call in this file/ITEM_TABLE, so no existing ID shifts.
    "Victory - Emerald Goal": ItemInfo(_alloc_id(), ItemClassification.progression),
    "Victory - Goal Mission": ItemInfo(_alloc_id(), ItemClassification.progression),
}


class MCDungeonsItem(Item):
    game = "Minecraft Dungeons"
