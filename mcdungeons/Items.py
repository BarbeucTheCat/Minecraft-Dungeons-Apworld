from typing import Dict, List, NamedTuple, Optional, Tuple
try:
    from BaseClasses import Item, ItemClassification
except ImportError:
    # Client can't import BaseClasses; these placeholders stand in for Item and ItemClassification.
    # Only builds ItemInfo tuples. Never runs at generation time.     
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


BASE_ITEM_ID = 0xDC0000  # the offset that determines the item IDs; this value must not change

_next_id = [BASE_ITEM_ID]


def _alloc_id() -> int:
    _next_id[0] += 1
    return _next_id[0]


# One "Mission Access" progression item per zone (base + DLC).
MISSION_ACCESS_ITEMS: Dict[str, ItemInfo] = {
    f"{z.display_name} Access": ItemInfo(_alloc_id(), ItemClassification.progression)
    for z in ZONES
}

# Plain and secret access items both stay in the table so IDs never shift.   
SECRET_ACCESS_ITEMS: Dict[str, ItemInfo] = {
    f"{z.display_name} Secret Access": ItemInfo(_alloc_id(), ItemClassification.progression)
    for z in ZONES if z.secret
}


def zone_access_item_name(zone, secret_option: bool) -> str:
    """The one item that unlocks `zone` and counts as Access for later zones.
    Secret zones with the option on use "<Zone> Secret Access"."""
    if secret_option and zone.secret:
        return f"{zone.display_name} Secret Access"
    return f"{zone.display_name} Access"

# Single item gating all three Ancient Hunts at once.
ANCIENT_HUNT_ACCESS_ITEM = "Ancient Hunt Access"

# Three copies are in the pool; each one the player finds grants an enchant slot.
PROGRESSIVE_ENCHANT_SLOT_ITEM = "Progressive Enchant Slot"
PROGRESSIVE_ENCHANT_SLOT_COPIES = 3  # 0/1/2/3 slots - one copy per tier above 0

# Each copy unlocks a new tier. Always in logic (marked as progression); only in the pool when the option is on.
PROGRESSIVE_PICKUP_ITEM = "Progressive Pickup"
PROGRESSIVE_PICKUP_COPIES = 3  # 0/1/2/3 Tiers

# Six copies, only in the pool when the option is on. The Stronghold needs all six.
# The real Eyes from mini bosses become checks instead.
EYE_OF_ENDER_ITEM = "Eye of Ender"
EYE_OF_ENDER_COPIES = 6

# One per camp merchant, received instead of freeing the villager; unlocks that merchant.
# Only in the pool when the option is on. Names must match VILLAGER_TYPES display names (enforced by a test).   
VILLAGER_ITEM_NAMES = ["Village Merchant", "Mystery Merchant", "Luxury Merchant", "Gift Wrapper", "Blacksmith",
                       "Enchantsmith", "Piglin Merchant"]

# Each one adds emeralds to the player's total (no in-game object).   
FILLER_ITEM_EMERALD_AMOUNTS: Dict[str, int] = {
    "100 Emeralds": 100,
    "300 Emeralds": 300,
    "500 Emeralds": 500,
}

FILLER_ITEMS: Dict[str, ItemInfo] = {
    name: ItemInfo(_alloc_id(), ItemClassification.filler)
    for name in FILLER_ITEM_EMERALD_AMOUNTS
}

# Grants random gear in the named category; specific piece rolled at receive time (enchants for non-artifacts).
ITEM_REWARD_CATEGORIES: Dict[str, Optional[str]] = {
    "Random Melee Weapon": "Melee",
    "Random Ranged Weapon": "Ranged",
    "Random Armor": "Armor",
    "Random Artifact": "Artifact",  # no enchant slots
    "Random Item": None,   # no fixed item; one is rolled at receive time; Cannot include artifacts
}
# Pool is fixed by the item at generation, not by the player's location at receive time.
# Keep in sync with location_reward_pools.py by hand.

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

    # On a real location, not a virtual event — AP core rejects code=None there, so these items need real codes.
    # New items go at the end of the table so no existing ID shifts.   
    "Victory - Emerald Goal": ItemInfo(_alloc_id(), ItemClassification.progression),
    "Victory - Goal Mission": ItemInfo(_alloc_id(), ItemClassification.progression),
    EYE_OF_ENDER_ITEM: ItemInfo(_alloc_id(), ItemClassification.progression),
    **{name: ItemInfo(_alloc_id(), ItemClassification.useful) for name in VILLAGER_ITEM_NAMES},
}


class MCDungeonsItem(Item):
    game = "Minecraft Dungeons"
