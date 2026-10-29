from typing import Dict, List, Optional, Tuple

from .ZoneData import ZONES, ANCIENT_HUNT_ZONES, ZONES_BY_NAME

BASE_LOCATION_ID = 0xDC1000  # the offset that determines the location IDs; this value must not change


_next_id = [BASE_LOCATION_ID]


def _alloc_id() -> int:
    _next_id[0] += 1
    return _next_id[0]


# Placeholder: one "Mission Complete" per zone. Swap for real per-chest locations later —
# region wiring doesn't care how many locations live in a region.   
LOCATION_TABLE: Dict[str, int] = {
    f"{z.display_name} - Mission Complete": _alloc_id()
    for z in (ZONES + ANCIENT_HUNT_ZONES)
}

LOCATIONS_BY_ZONE: Dict[str, str] = {
    z.internal_name: f"{z.display_name} - Mission Complete"
    for z in (ZONES + ANCIENT_HUNT_ZONES)
}


# Full 500-step range in the table (not trimmed per player): AP resolves names→IDs before per-slot options exist.   
EMERALD_MILESTONE_BASE_ID = 0xE4E4_0000  # Base ID must not collide with BASE_LOCATION_ID's range.
EMERALD_MILESTONE_MAX = 500000  # 500000 is well above the current EmeraldGoal ceiling, so the table stays stable if it's raised later.

def milestone_location_name(amount: int) -> str:
    return f"Emeralds: {amount}"


def get_emerald_milestone_id(amount: int) -> int:
    """Converts a milestone amount (multiple of 500) to its location ID directly, so the client watcher doesn't need the full table."""
    if amount % 500 != 0:
        raise ValueError(f"{amount} is not a multiple of 500")
    return EMERALD_MILESTONE_BASE_ID + (amount // 500)


def get_emerald_milestone_locations(goal: int) -> Dict[str, int]:
    """Returns {name: id} for milestones 500 through goal (inclusive) — per-player subset, not the full static table."""
    if goal % 500 != 0:
        goal = (goal // 500) * 500  # defensive - EmeraldGoal.from_any already
                                     # rounds this, but don't trust callers blindly
    return {
        milestone_location_name(amount): get_emerald_milestone_id(amount)
        for amount in range(500, goal + 1, 500)
    }


# Full static range (not trimmed per player): AP builds the name→id table at class level, before per-slot options exist.
EMERALD_MILESTONE_TABLE: Dict[str, int] = {
    milestone_location_name(amount): get_emerald_milestone_id(amount)
    for amount in range(500, EMERALD_MILESTONE_MAX + 1, 500)
}

# Merge everything the World class needs into one lookup table.
LOCATION_TABLE.update(EMERALD_MILESTONE_TABLE)


# One per secret zone, only with the option on. Attaches to the parent's region (the trigger is there).
# IDs in their own block, indexed by ZONES position (append-only) — never shift.
SECRET_ZONE_FOUND_BASE_ID = 0xDC6000  # separate block - clear of every other range above


def secret_zone_found_location_name(zone_display_name: str) -> str:
    return f"{zone_display_name} - Secret Zone Found"


SECRET_ZONE_FOUND_TABLE: Dict[str, int] = {
    secret_zone_found_location_name(z.display_name): SECRET_ZONE_FOUND_BASE_ID + i
    for i, z in enumerate(ZONES) if z.secret
}

# secret zone internal_name -> its "Secret Zone Found" location name
SECRET_ZONE_FOUND_BY_ZONE: Dict[str, str] = {
    z.internal_name: secret_zone_found_location_name(z.display_name)
    for z in ZONES if z.secret
}

# Maps parent zone internal_name → the secret zone it reveals (derived from the secret zone's requires[]).
SECRET_PARENT_TO_ZONE: Dict[str, str] = {
    z.requires[0]: z.internal_name for z in ZONES if z.secret and z.requires
}

LOCATION_TABLE.update(SECRET_ZONE_FOUND_TABLE)


# One "First Kill" check per base-game boss.
# Gated behind boss_kill_checks; BOSS_ZONE maps each boss to the zone whose region the check attaches to
# (same access rule and DLC gating as that zone's Mission Complete check).

# Heart of Ender excluded: client can't reliably detect its kill (no confirmed EntityType).
# Vengeful Heart of Ender kept despite the same caveat — it's the only check on its zone.
BOSS_ZONE: Dict[str, str] = {
    "Arch-Illager": "obsidianpinnacle",
    "Corrupted Cauldron": "soggyswamp",
    "Mooshroom Monstrosity": "mooshroomisland",
    "Tempest Golem": "galesanctum",
    "Treetop Whisperer": "treetoptangle",
    "Vengeful Heart of Ender": "endcitadel_blightedcitadel",
    "Ancient Guardian": "abyssalmonument",
    "Jungle Abomination": "overgrowntemple",
    "Nameless One": "deserttemple",
    "Redstone Monstrosity": "fieryforge",
    "Wretched Wraith": "lonefortress",
}

BOSS_NAMES = list(BOSS_ZONE.keys())

BOSS_KILL_BASE_ID = 0xDC3000  # separate block - clear of BASE_LOCATION_ID's and EMERALD's ranges above


def boss_kill_location_name(boss_name: str) -> str:
    return f"{boss_name} - First Kill"


BOSS_KILL_TABLE: Dict[str, int] = {
    boss_kill_location_name(name): BOSS_KILL_BASE_ID + i
    for i, name in enumerate(BOSS_NAMES)
}


def get_boss_kill_location_id(boss_name: str) -> int:
    """Converts a boss display name to its location ID, so the client watcher knows which ID to send on first kill."""
    return BOSS_KILL_TABLE[boss_kill_location_name(boss_name)]


def get_base_boss_names() -> List[str]:
    """Base-game bosses (zone category == 'base'), gated by BossKillChecks.
    Derived from BOSS_ZONE + ZONES_BY_NAME so it can't drift if either changes."""
    return [name for name, zone in BOSS_ZONE.items() if ZONES_BY_NAME[zone].category == "base"]


def get_dlc_boss_names() -> List[str]:
    """DLC bosses (zone category == 'dlc'), gated by DlcBossKillChecks."""
    return [name for name, zone in BOSS_ZONE.items() if ZONES_BY_NAME[zone].category == "dlc"]


LOCATION_TABLE.update(BOSS_KILL_TABLE)


# One "Mini Boss Kill" check per zone that has a mini boss (see ZONE_MINIBOSSES in client/miniboss.py).
# Gated behind mini_boss_kill_checks / dlc_mini_boss_kill_checks (both off by default).
# MINIBOSS_ZONES order decides IDs — append-only. A test keeps it in sync with ZONE_MINIBOSSES.
MINIBOSS_ZONES: List[str] = [
    # base game
    "creeperwoods", "creepycrypt", "pumpkinpastures", "soggyswamp", "soggycave", "redstonemines",
    "fieryforge", "cacticanyon", "deserttemple", "lowertemple", "highblockhalls", "obsidianpinnacle",
    # Jungle Awakens
    "dingyjungle", "overgrowntemple", "bamboobluff", "treetoptangle",
    # Creeping Winter
    "frozenfjord", "lonefortress", "lostsettlement",
    # Howling Peaks
    "windsweptpeaks", "gauntletofgales", "galesanctum", "endlessrampart",
    # Hidden Depths
    "coralrise", "abyssalmonument", "radiantravine",
    # Flames of the Nether
    "warpedforest", "crimsonforest", "soulsandvalley", "basaltdeltas", "netherfortress",
    # Echoing Void
    "enderwilds", "endcitadel_blightedcitadel",
]

MINIBOSS_KILL_BASE_ID = 0xDC3C00  # separate block - clear of BOSS_KILL's (0xDC3000), ANCIENT_HUNT's (0xDC3800) and ZONE_CHEST's (0xDC4000)


def mini_boss_kill_location_name(zone_internal_name: str) -> str:
    return f"{ZONES_BY_NAME[zone_internal_name].display_name} - Mini Boss Kill"


MINI_BOSS_KILL_TABLE: Dict[str, int] = {
    mini_boss_kill_location_name(zone): MINIBOSS_KILL_BASE_ID + i
    for i, zone in enumerate(MINIBOSS_ZONES)
}


def get_mini_boss_kill_location_id(zone_internal_name: str) -> int:
    """Location ID of a zone's mini boss kill check (zone must be in MINIBOSS_ZONES), so the client watcher knows which ID to send."""
    return MINI_BOSS_KILL_TABLE[mini_boss_kill_location_name(zone_internal_name)]


def get_base_mini_boss_zones() -> List[str]:
    """Mini boss zones that are base-game - gated by the MiniBossKillChecks option."""
    return [z for z in MINIBOSS_ZONES if ZONES_BY_NAME[z].category == "base"]


def get_dlc_mini_boss_zones() -> List[str]:
    """Mini boss zones that belong to a DLC pack - gated by the DlcMiniBossKillChecks option."""
    return [z for z in MINIBOSS_ZONES if ZONES_BY_NAME[z].category == "dlc"]


LOCATION_TABLE.update(MINI_BOSS_KILL_TABLE)


# Mini boss checks, two modes (per_zone / per_type, or both):
#   per_zone: forced to appear in every zone that has one — always obtainable.
#   per_type: one per boss kind, first kill anywhere; can't be forced, so excluded from progression.
# MINIBOSS_ZONE_TYPE maps each zone to its pinned boss type (synced with client via test).
# MINIBOSS_TYPE_ORDER decides per-type IDs — append-only.
MINIBOSS_MODE_NONE, MINIBOSS_MODE_PER_TYPE, MINIBOSS_MODE_PER_ZONE, MINIBOSS_MODE_BOTH = 0, 1, 2, 3

MINIBOSS_TYPE_ORDER: List[str] = [
    "enderman", "skeletonhorseman", "evoker", "redstonegolem", "illusioner",
    "drownednecromancer", "elderguardian", "wildfire", "ghast", "endersent",
    # Mini bosses not pinned to a zone and not summonable by the client — only count if they happen to appear.
    # Per-type checks only (see MINIBOSS_EXTRA_TYPE_ZONES). Appended, so no existing ID shifts.   
    "pillagerraidcaptain", "vindicatorraidcaptain",
    "endersentthornblight", "endersentvoidstrike", "endersentwritherot",
    "endersenteverfire", "endersentsurgefiend", "endersentdeadeye",
]
MINIBOSS_TYPE_DISPLAY: Dict[str, str] = {
    "enderman": "Enderman", "skeletonhorseman": "Skeleton Horseman", "evoker": "Evoker",
    "redstonegolem": "Redstone Golem", "illusioner": "Illusioner", "drownednecromancer": "Drowned Necromancer",
    "elderguardian": "Elder Guardian", "wildfire": "Wildfire", "ghast": "Ghast", "endersent": "Endersent",
    "pillagerraidcaptain": "Pillager Raid Captain", "vindicatorraidcaptain": "Vindicator Raid Captain",
    "endersentthornblight": "Endersent Thornblight", "endersentvoidstrike": "Endersent Voidstrike",
    "endersentwritherot": "Endersent Writherot", "endersenteverfire": "Endersent Everfire",
    "endersentsurgefiend": "Endersent Surgefiend", "endersentdeadeye": "Endersent Deadeye",
}
MINIBOSS_ZONE_TYPE: Dict[str, str] = {
    "creeperwoods": "enderman", "creepycrypt": "skeletonhorseman", "pumpkinpastures": "evoker",
    "soggyswamp": "enderman", "soggycave": "skeletonhorseman", "redstonemines": "redstonegolem",
    "fieryforge": "redstonegolem", "cacticanyon": "skeletonhorseman", "deserttemple": "enderman",
    "lowertemple": "skeletonhorseman", "highblockhalls": "evoker", "obsidianpinnacle": "redstonegolem",
    "dingyjungle": "enderman", "overgrowntemple": "enderman", "bamboobluff": "skeletonhorseman",
    "treetoptangle": "enderman",
    "frozenfjord": "illusioner", "lonefortress": "illusioner", "lostsettlement": "illusioner",
    "windsweptpeaks": "evoker", "gauntletofgales": "illusioner", "galesanctum": "evoker",
    "endlessrampart": "illusioner",
    "coralrise": "drownednecromancer", "abyssalmonument": "elderguardian", "radiantravine": "drownednecromancer",
    "warpedforest": "enderman", "crimsonforest": "wildfire", "soulsandvalley": "wildfire",
    "basaltdeltas": "ghast", "netherfortress": "wildfire",
    "enderwilds": "endersent", "endcitadel_blightedcitadel": "endersent",
}
# Zones where unpinned types can appear, in order of preference (first available wins).
# Raid Captains: Mooshroom Island, Abyssal Monument, Basalt Deltas. Endersents: Echoing Void zones.
MINIBOSS_EXTRA_TYPE_ZONES: Dict[str, List[str]] = {
    "pillagerraidcaptain": ["mooshroomisland", "abyssalmonument", "basaltdeltas"],
    "vindicatorraidcaptain": ["mooshroomisland", "abyssalmonument", "basaltdeltas"],
    "endersentthornblight": ["enderwilds", "endcitadel_blightedcitadel"],
    "endersentvoidstrike": ["enderwilds", "endcitadel_blightedcitadel"],
    "endersentwritherot": ["enderwilds", "endcitadel_blightedcitadel"],
    "endersenteverfire": ["enderwilds", "endcitadel_blightedcitadel"],
    "endersentsurgefiend": ["enderwilds", "endcitadel_blightedcitadel"],
    "endersentdeadeye": ["enderwilds", "endcitadel_blightedcitadel"],
}
# every zone a type can show up in: the zones that pin it (MINIBOSS_ZONES order) + the extra zones above
MINIBOSS_TYPE_ZONES: Dict[str, List[str]] = {
    t: [z for z in MINIBOSS_ZONES if MINIBOSS_ZONE_TYPE[z] == t] + MINIBOSS_EXTRA_TYPE_ZONES.get(t, [])
    for t in MINIBOSS_TYPE_ORDER
}
MINIBOSS_TYPE_BASE_ID = 0xDC3D00  # own block - between MINIBOSS (0xDC3C00) and EYE_OF_ENDER (0xDC3E00)


def mini_boss_type_location_name(type_key: str) -> str:
    return f"Mini Boss - {MINIBOSS_TYPE_DISPLAY[type_key]}"


MINI_BOSS_TYPE_TABLE: Dict[str, int] = {
    mini_boss_type_location_name(t): MINIBOSS_TYPE_BASE_ID + i for i, t in enumerate(MINIBOSS_TYPE_ORDER)
}


def get_mini_boss_type_location_id(type_key: str) -> int:
    return MINI_BOSS_TYPE_TABLE[mini_boss_type_location_name(type_key)]


def mini_boss_mode_wants_zone(mode: int) -> bool:
    return mode in (MINIBOSS_MODE_PER_ZONE, MINIBOSS_MODE_BOTH)


def mini_boss_mode_wants_type(mode: int) -> bool:
    return mode in (MINIBOSS_MODE_PER_TYPE, MINIBOSS_MODE_BOTH)


def _mini_boss_mode_of_zone(zone: str, base_mode: int, dlc_mode: int) -> int:
    return base_mode if ZONES_BY_NAME[zone].category == "base" else dlc_mode


def wanted_mini_boss_zones(base_mode: int, dlc_mode: int) -> set:
    """Zones whose per-zone check exists: the zone's group (base game / DLC) is set to per_zone or both."""
    return {z for z in MINIBOSS_ZONES if mini_boss_mode_wants_zone(_mini_boss_mode_of_zone(z, base_mode, dlc_mode))}


def wanted_mini_boss_type_homes(base_mode: int, dlc_mode: int, available_zones) -> Dict[str, str]:
    """Maps a mini boss type to the zone whose Region holds its check.
    Only when at least one available zone (DLC pack on) has per_type or both.
    Picks the earliest such zone in MINIBOSS_TYPE_ZONES order for maximum reachability."""   
    homes: Dict[str, str] = {}
    for type_key in MINIBOSS_TYPE_ORDER:
        for zone in MINIBOSS_TYPE_ZONES[type_key]:
            if zone in available_zones and mini_boss_mode_wants_type(_mini_boss_mode_of_zone(zone, base_mode, dlc_mode)):
                homes[type_key] = zone
                break
    return homes


LOCATION_TABLE.update(MINI_BOSS_TYPE_TABLE)


# One check per Eye of Ender type (6), only with the option on. A mini boss drops each Eye; the client
# sends the check when it appears in the level. Sub-missions aren't zones here, so checks live in the parent.
# IDs in their own block, append-only.   
EYE_OF_ENDER_TYPES: List[Tuple[int, str, str]] = [
    # (EEyeOfEnderType value, mission the Eye is named after, parent zone of that chain)
    (1, "Deepwood Brook", "creeperwoods"),
    (2, "Old Town Hall", "pumpkinpastures"),
    (3, "Sunken Watchtower", "soggyswamp"),
    (4, "The Undercroft", "cacticanyon"),
    (5, "The Trial of the Nameless", "deserttemple"),
    (6, "Highblock Hideaway", "highblockhalls"),
]
EYE_OF_ENDER_BASE_ID = 0xDC3E00  # clear of MINIBOSS (0xDC3C00) and ZONE_CHEST (0xDC4000)


def eye_of_ender_location_name(eye_type: int) -> str:
    for value, mission, _zone in EYE_OF_ENDER_TYPES:
        if value == eye_type:
            return f"Eye of Ender - {mission}"
    raise KeyError(eye_type)


EYE_OF_ENDER_TABLE: Dict[str, int] = {
    eye_of_ender_location_name(value): EYE_OF_ENDER_BASE_ID + value
    for value, _mission, _zone in EYE_OF_ENDER_TYPES
}
EYE_OF_ENDER_ZONE: Dict[int, str] = {value: zone for value, _mission, zone in EYE_OF_ENDER_TYPES}


def get_eye_of_ender_location_id(eye_type: int) -> int:
    """Location ID of the check for a dropped Eye of that EEyeOfEnderType (1..6)."""
    return EYE_OF_ENDER_TABLE[eye_of_ender_location_name(eye_type)]


LOCATION_TABLE.update(EYE_OF_ENDER_TABLE)


# Villager checks (option villager_checks): freeing a captured villager sends a check; the merchant arrives as an item.
# Base-game merchants confirmed; DLC ones pending (client logs them to dungeons_bridge_debug.log for identification).
# Tuple: (key, class name fragment, display name = item name, zone internal name).
VILLAGER_TYPES: List[Tuple[str, str, str, str]] = [
    ("village", "villagemerchant", "Village Merchant", "creeperwoods"),
    ("mystery", "mysterymerchant", "Mystery Merchant", "pumpkinpastures"),
    ("luxury", "luxurymerchant", "Luxury Merchant", "cacticanyon"),
    ("giftwrapper", "giftwrappermerchant", "Gift Wrapper", "soggyswamp"),
    ("blacksmith", "blacksmithmerchant", "Blacksmith", "redstonemines"),
    ("enchantsmith", "enchant", "Enchantsmith", "highblockhalls"),
    # Piglin Merchant is not rescued — unlocked by completing an Ancient Hunt (check only with ancient_hunts option).
    ("piglin", "piglin", "Piglin Merchant", "hm_woodlandmansion"),
]
VILLAGER_BASE_ID = 0xDC3F00  # clear of EYE_OF_ENDER (0xDC3E00) and ZONE_CHEST (0xDC4000)


def villager_location_name(key: str) -> str:
    """e.g. "Redstone Mines - Rescued the Blacksmith" (the zone where that villager is held, then the merchant)."""
    for k, _fragment, display, zone in VILLAGER_TYPES:
        if k == key:
            if k == "piglin":
                return f"Ancient Hunts - Unlocked the {display}"
            return f"{ZONES_BY_NAME[zone].display_name} - Rescued the {display}"
    raise KeyError(key)


VILLAGER_TABLE: Dict[str, int] = {
    villager_location_name(key): VILLAGER_BASE_ID + index
    for index, (key, _fragment, _display, _zone) in enumerate(VILLAGER_TYPES)
}
VILLAGER_ZONE: Dict[str, str] = {key: zone for key, _fragment, _display, zone in VILLAGER_TYPES}


def get_villager_location_id(key: str) -> int:
    """Location ID of the check for the captured villager `key` (see VILLAGER_TYPES)."""
    return VILLAGER_TABLE[villager_location_name(key)]


LOCATION_TABLE.update(VILLAGER_TABLE)


# One "Boss Kill" check per Ancient Hunt (3), separate from Mission Complete.
# Gated by ancient_hunt_boss_checks (off by default); only meaningful when ancient_hunts is on.
# Named after the hunt, not a specific creature — the exact target isn't confirmed.
ANCIENT_HUNT_BOSS_KILL_BASE_ID = 0xDC3800  # separate block - clear of BOSS_KILL's and ZONE_CHEST's ranges


def ancient_hunt_boss_kill_location_name(hunt_display_name: str) -> str:
    return f"{hunt_display_name} - Boss Kill"


ANCIENT_HUNT_BOSS_KILL_TABLE: Dict[str, int] = {
    ancient_hunt_boss_kill_location_name(z.display_name): ANCIENT_HUNT_BOSS_KILL_BASE_ID + i
    for i, z in enumerate(ANCIENT_HUNT_ZONES)
}

LOCATION_TABLE.update(ANCIENT_HUNT_BOSS_KILL_TABLE)


# Per-zone chest locations, only for zones with a CONFIRMED fixed layout (verified live, zone by zone).
# Two independent counters per zone: regular chests and supply chests (different detection patterns).
# ZONE_CHEST_COUNTS: internal_name -> (chest_count, supply_chest_count).
# Add a new zone once its layout is confirmed; keep the mirror in locations.py in sync.
BASE_ZONE_CHEST_COUNTS: Dict[str, Tuple[int, int]] = {
    "squidcoast": (4, 1),
    "soggyswamp": (5, 1),
    "creeperwoods": (6, 1),
    "creepycrypt": (10, 0),
    "soggycave": (1, 0),
    "redstonemines": (5, 2),
    "cacticanyon": (2, 1),
    "pumpkinpastures": (6, 2),
    "fieryforge": (5, 2),
    "deserttemple": (5, 1),
    "lowertemple": (7, 2),
    "archhaven": (3, 0),
    "thestronghold": (3, 0),
    "highblockhalls": (5, 2),
    "obsidianpinnacle": (4, 2),
    "underhalls": (3, 0),
}

# DLC zone chests: add one line per zone once measured in-game (client logs tallies to dlc_chest_counts_seen.json).
# Zone must be a DLC zone in ZoneData.py.
# IDs in their own block (DLC_ZONE_CHEST_BASE_ID), fixed range per zone — adding one never shifts others.
# Keep total (chests + supply) under DLC_ZONE_CHEST_SLOTS_PER_ZONE.
DLC_ZONE_CHEST_COUNTS: Dict[str, Tuple[int, int]] = {
    # (chests, supply chests) read from the in-game map. Secrets not counted (no chest check); if a secret is a chest, it feeds the global bonus pool.
    # Nether pack: older measurements (minimum of the counter), not re-measured.
    "netherwastes": (1, 0),
    "warpedforest": (5, 0),
    "crimsonforest": (1, 0),
    "basaltdeltas": (4, 0),
    "netherfortress": (3, 0),
    "soulsandvalley": (1, 0),
    # Hidden Depths
    "coralrise": (7, 1),
    "radiantravine": (4, 0),
    "abyssalmonument": (5, 1),
    # Jungle Awakens
    "dingyjungle": (4, 1),
    "overgrowntemple": (5, 1),
    "treetoptangle": (8, 1),
    "bamboobluff": (3, 0),         # Panda Plateau
    # Creeping Winter
    "frozenfjord": (7, 1),         # Frosted Fjord
    "lonefortress": (3, 1),
    "lostsettlement": (3, 0),
    # Howling Peaks
    "galesanctum": (6, 1),
    "gauntletofgales": (6, 1),
    "windsweptpeaks": (8, 1),
    "endlessrampart": (5, 0),      # Colossal Rampart
}

# Base-game zones measured after the original table was frozen; allocated after BASE_ZONE_CHEST_COUNTS so no existing ID shifts.
LATE_BASE_ZONE_CHEST_COUNTS: Dict[str, Tuple[int, int]] = {
    "mooshroomisland": (1, 0),     # the "???" mission: map counter 0/1 chests, 0/0 secrets
}

# Everything below (generation, client, map tab) reads this merged view.
ZONE_CHEST_COUNTS: Dict[str, Tuple[int, int]] = {**BASE_ZONE_CHEST_COUNTS, **LATE_BASE_ZONE_CHEST_COUNTS,
                                                 **DLC_ZONE_CHEST_COUNTS}

ZONE_CHEST_BASE_ID = 0xDC4000  # separate block - clear of every other range above
DLC_ZONE_CHEST_BASE_ID = 0xDC7000  # DLC zones' chests - separate block, see DLC_ZONE_CHEST_COUNTS
DLC_ZONE_CHEST_SLOTS_PER_ZONE = 32


def zone_chest_location_name(zone_internal_name: str, n: int) -> str:
    zone = ZONES_BY_NAME[zone_internal_name]
    return f"{zone.display_name} - Chest {n}"


def zone_supply_chest_location_name(zone_internal_name: str, n: int) -> str:
    zone = ZONES_BY_NAME[zone_internal_name]
    return f"{zone.display_name} - Supply Chest {n}"


ZONE_CHEST_TABLE: Dict[str, int] = {}
_chest_id = [ZONE_CHEST_BASE_ID]


def _alloc_chest_id() -> int:
    _chest_id[0] += 1
    return _chest_id[0]


# Sorted for deterministic IDs (independent of ZONE_CHEST_COUNTS key order).
# Base game only — DLC zones have their own block below; adding them here would shift base IDs.
for _zone_name in sorted(BASE_ZONE_CHEST_COUNTS):
    _chest_n, _supply_n = BASE_ZONE_CHEST_COUNTS[_zone_name]
    for _n in range(1, _chest_n + 1):
        ZONE_CHEST_TABLE[zone_chest_location_name(_zone_name, _n)] = _alloc_chest_id()
    for _n in range(1, _supply_n + 1):
        ZONE_CHEST_TABLE[zone_supply_chest_location_name(_zone_name, _n)] = _alloc_chest_id()

# Late base zones: continue the same sequence, after every original base zone (IDs of those never move).
for _zone_name in sorted(LATE_BASE_ZONE_CHEST_COUNTS):
    if _zone_name not in ZONES_BY_NAME or ZONES_BY_NAME[_zone_name].category != "base":
        raise ValueError(f"LATE_BASE_ZONE_CHEST_COUNTS: {_zone_name!r} is not a base zone of ZoneData.py")
    _chest_n, _supply_n = LATE_BASE_ZONE_CHEST_COUNTS[_zone_name]
    for _n in range(1, _chest_n + 1):
        ZONE_CHEST_TABLE[zone_chest_location_name(_zone_name, _n)] = _alloc_chest_id()
    for _n in range(1, _supply_n + 1):
        ZONE_CHEST_TABLE[zone_supply_chest_location_name(_zone_name, _n)] = _alloc_chest_id()

# DLC zones: a fixed ID range per zone (position in ZoneData.ZONES * slots per zone).
_ZONE_ORDER = list(ZONES_BY_NAME)
for _zone_name, (_chest_n, _supply_n) in DLC_ZONE_CHEST_COUNTS.items():
    if _zone_name not in ZONES_BY_NAME or ZONES_BY_NAME[_zone_name].category != "dlc":
        raise ValueError(f"DLC_ZONE_CHEST_COUNTS: {_zone_name!r} is not a DLC zone of ZoneData.py")
    if _chest_n + _supply_n > DLC_ZONE_CHEST_SLOTS_PER_ZONE:
        raise ValueError(f"DLC_ZONE_CHEST_COUNTS: {_zone_name!r} has more than "
                         f"{DLC_ZONE_CHEST_SLOTS_PER_ZONE} chests in total")
    _next_id = DLC_ZONE_CHEST_BASE_ID + _ZONE_ORDER.index(_zone_name) * DLC_ZONE_CHEST_SLOTS_PER_ZONE
    for _n in range(1, _chest_n + 1):
        _next_id += 1
        ZONE_CHEST_TABLE[zone_chest_location_name(_zone_name, _n)] = _next_id
    for _n in range(1, _supply_n + 1):
        _next_id += 1
        ZONE_CHEST_TABLE[zone_supply_chest_location_name(_zone_name, _n)] = _next_id


def get_zone_chest_location_id(zone_internal_name: str, n: int) -> int:
    """Returns the location ID for a zone's Nth chest (1-based), so the client watcher knows which ID to send as chests are opened."""
    return ZONE_CHEST_TABLE[zone_chest_location_name(zone_internal_name, n)]


def get_zone_supply_chest_location_id(zone_internal_name: str, n: int) -> int:
    """Same as get_zone_chest_location_id, for the Supply Chest series."""
    return ZONE_CHEST_TABLE[zone_supply_chest_location_name(zone_internal_name, n)]


LOCATION_TABLE.update(ZONE_CHEST_TABLE)


# Global bonus chest locations — not tied to a zone. Every extra chest beyond a zone's confirmed baseline
# sends the next "Bonus Chest N" (discovery order across all zones combined).
# Full 100-slot table is static (AP resolves name→id at class level, before per-slot options);
# get_bonus_chest_locations() trims per-player in Regions.py.
# Marked EXCLUDED: reaching a high-numbered one isn't guaranteed early, so fill must never strand
# a progression item there.

MAX_BONUS_CHESTS = 100  # full static headroom - matches BonusChestCount's range_end
BONUS_CHEST_BASE_ID = 0xDC5000  # separate block - clear of every other range above


def bonus_chest_location_name(n: int) -> str:
    return f"Bonus Chest {n}"


BONUS_CHEST_TABLE: Dict[str, int] = {
    bonus_chest_location_name(n): BONUS_CHEST_BASE_ID + n
    for n in range(1, MAX_BONUS_CHESTS + 1)
}


def get_bonus_chest_location_id(n: int) -> int:
    """Returns the location ID for a global bonus chest slot (1-based), so the client watcher knows which ID to send when an extra chest is found."""
    return BONUS_CHEST_TABLE[bonus_chest_location_name(n)]


def get_bonus_chest_locations(count: int) -> Dict[str, int]:
    """Returns {name: id} for this player's bonus chest slots (0..BonusChestCount) — per-player subset, not the full static table."""
    count = max(0, min(count, MAX_BONUS_CHESTS))
    return {
        bonus_chest_location_name(n): get_bonus_chest_location_id(n)
        for n in range(1, count + 1)
    }


LOCATION_TABLE.update(BONUS_CHEST_TABLE)
