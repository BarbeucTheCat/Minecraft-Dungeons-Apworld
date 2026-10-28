from typing import Dict, List, Optional, Tuple

from .ZoneData import ZONES, ANCIENT_HUNT_ZONES, ZONES_BY_NAME

BASE_LOCATION_ID = 0xDC1000  # arbitrary offset - replace with your registered AP game ID block

_next_id = [BASE_LOCATION_ID]


def _alloc_id() -> int:
    _next_id[0] += 1
    return _next_id[0]


# Placeholder: one "Mission Complete" location per zone. Swap this out for
# real per-chest / per-objective locations (e.g. from chest_catalog.json)
# once you're ready - the region wiring in Regions.py doesn't care how many
# locations live in a region.
LOCATION_TABLE: Dict[str, int] = {
    f"{z.display_name} - Mission Complete": _alloc_id()
    for z in (ZONES + ANCIENT_HUNT_ZONES)
}

LOCATIONS_BY_ZONE: Dict[str, str] = {
    z.internal_name: f"{z.display_name} - Mission Complete"
    for z in (ZONES + ANCIENT_HUNT_ZONES)
}


# ------------------------------------------------------------------
# Emerald milestone locations (dungeons_reader.py's currency watcher
# reports these to the server via ap_client.py's send_location_checks).
#
# EmeraldGoal (see Options.py) is a per-player Range option from 1000 up
# to 500000, so location_name_to_id at the World class level must contain
# every POSSIBLE milestone up front (the full range), even though any one
# player's actual goal only uses a prefix of it - AP resolves names to IDs
# at generation time before options are locked in per-slot, so the table
# can't be trimmed to just one player's goal.
# ------------------------------------------------------------------

EMERALD_MILESTONE_BASE_ID = 0xE4E4_0000  # must not collide with BASE_LOCATION_ID's range above
EMERALD_MILESTONE_MAX = 500000  # deliberate 10x headroom above EmeraldGoal's actual
                                 # ceiling (50000) - keeps this static ID table
                                 # stable even if that ceiling is ever raised later


def milestone_location_name(amount: int) -> str:
    return f"Emeralds: {amount}"


def get_emerald_milestone_id(amount: int) -> int:
    """Given a specific milestone amount (must be a multiple of 500),
    returns its location ID directly - used by the client watcher to know
    which ID to send when a threshold is crossed, without needing the
    full table."""
    if amount % 500 != 0:
        raise ValueError(f"{amount} is not a multiple of 500")
    return EMERALD_MILESTONE_BASE_ID + (amount // 500)


def get_emerald_milestone_locations(goal: int) -> Dict[str, int]:
    """Returns {location_name: location_id} for every 500-emerald
    milestone from 500 up to (and including) goal. Used per-player in
    Regions.py to add only the milestones relevant to that player's
    emerald_goal option - NOT the full static range below."""
    if goal % 500 != 0:
        goal = (goal // 500) * 500  # defensive - EmeraldGoal.from_any already
                                     # rounds this, but don't trust callers blindly
    return {
        milestone_location_name(amount): get_emerald_milestone_id(amount)
        for amount in range(500, goal + 1, 500)
    }


# Full static range for location_name_to_id (every possible milestone,
# regardless of any one player's goal) - required because AP builds the
# name->id table once at the class level, before per-slot options exist.
EMERALD_MILESTONE_TABLE: Dict[str, int] = {
    milestone_location_name(amount): get_emerald_milestone_id(amount)
    for amount in range(500, EMERALD_MILESTONE_MAX + 1, 500)
}

# Merge everything the World class needs into one lookup table.
LOCATION_TABLE.update(EMERALD_MILESTONE_TABLE)


# ------------------------------------------------------------------
# "Secret Zone Found" locations: one per secret zone, only offered when the
# Secret Mission Access option is on. Finding the secret in-game (the
# USecretComponent::SecretFound trigger, which lives in the secret zone's
# PARENT mission - e.g. Creeper Woods for Creepy Crypt) no longer reveals
# the secret mission; the client blocks that and sends this check instead,
# and the mission itself is unlocked by its "<Zone> Secret Access" item.
# IDs come from their own block, indexed by the zone's position in ZONES
# (ZONES is append-only), so adding/removing other locations never shifts
# them.
# ------------------------------------------------------------------
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

# parent zone internal_name (where the secret trigger physically is) ->
# the secret zone it reveals. Derived from ZoneData's requires[]: the
# secret zone's single direct predecessor.
SECRET_PARENT_TO_ZONE: Dict[str, str] = {
    z.requires[0]: z.internal_name for z in ZONES if z.secret and z.requires
}

LOCATION_TABLE.update(SECRET_ZONE_FOUND_TABLE)


# ------------------------------------------------------------------
# Boss-kill locations: one "First Kill" check per boss, base-game
# Minecraft Dungeons only (the Arcade-exclusive bosses - Enderman, the
# two Evokers, Giant Cave Spider, Giant Royal Guard, Obsidian Monstrosity
# - don't apply to this world and aren't included). Gated behind the
# boss_kill_checks option (off by default) - see Options.py.
#
# BOSS_ZONE maps each boss to the zone (ZoneData.py internal_name) its
# check gets attached to in Regions.py - same access rule and DLC-pack
# gating as that zone's own Mission Complete check, so a boss whose DLC
# pack is disabled is correctly unreachable rather than silently offered
# anyway.
#
# All entries below have been verified against the Minecraft Dungeons
# wiki (fandom):
#   - Arch-Illager fights atop Obsidian Pinnacle - previously wrongly
#     mapped to Highblock Halls. Heart of Ender appears at the same
#     spot immediately after, but isn't included as its own entry
#     here: the client can't reliably detect its kill (no confirmed
#     EntityType - see BOSS_CLASS_LOOKUP in dungeons_reader.py, which
#     needs a one-time manual calibration pass and is usually empty),
#     so Obsidian Pinnacle gets a single boss-kill check via
#     Arch-Illager instead of two unreliable ones.
#   - Nameless One dwells in the Desert Temple - previously wrongly
#     mapped to Underhalls.
#   - Redstone Monstrosity is built within the Fiery Forge - previously
#     an unconfirmed guess pointing at Obsidian Pinnacle.
#   - Ancient Guardian resides in the Abyssal Monument - previously
#     wrongly mapped to Radiant Ravine.
#   - Jungle Abomination resides in the Overgrown Temple - previously
#     wrongly mapped to Panda Plateau (bamboobluff).
#   - Wretched Wraith dwells in the Lone Fortress - previously wrongly
#     mapped to Lost Settlement.
#   - Corrupted Cauldron (Soggy Swamp), Mooshroom Monstrosity
#     (Mooshroom Island), Tempest Golem (Gale Sanctum), Treetop
#     Whisperer (Treetop Tangle), and Vengeful Heart of Ender (Broken
#     Citadel) were already correct. Vengeful Heart of Ender is kept
#     despite the same EntityType caveat since it's the DLC final
#     boss and the only check on its zone either way - see
#     BOSS_ENTITY_TYPE_IDS comment in dungeons_reader.py.
# ------------------------------------------------------------------

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
    """Given a boss's display name (must match BOSS_NAMES exactly), returns
    its location ID directly - used by the client watcher to know which ID
    to send when that boss's first kill is detected."""
    return BOSS_KILL_TABLE[boss_kill_location_name(boss_name)]


def get_base_boss_names() -> List[str]:
    """Bosses whose zone is base-game (ZoneInfo.category == 'base') -
    gated by the BossKillChecks option, see Options.py. Computed from
    BOSS_ZONE + ZONES_BY_NAME rather than hardcoded, so it can't drift
    out of sync if BOSS_ZONE or a zone's category ever changes."""
    return [name for name, zone in BOSS_ZONE.items() if ZONES_BY_NAME[zone].category == "base"]


def get_dlc_boss_names() -> List[str]:
    """Bosses whose zone belongs to a DLC pack (ZoneInfo.category ==
    'dlc') - gated by the separate DlcBossKillChecks option, see
    Options.py."""
    return [name for name, zone in BOSS_ZONE.items() if ZONES_BY_NAME[zone].category == "dlc"]


LOCATION_TABLE.update(BOSS_KILL_TABLE)


# ------------------------------------------------------------------
# Mini boss kill locations: one "Mini Boss Kill" check per zone that has a mini boss (a "powerful mob",
# the ones the client's zone mini boss rule is about: client/miniboss.py ZONE_MINIBOSSES). The check is
# sent when a powerful mob is beaten in that zone. Gated behind mini_boss_kill_checks (base-game zones)
# and dlc_mini_boss_kill_checks (DLC zones) - both off by default, see Options.py - and attached to the
# zone's own Region in Regions.py, like the boss first-kills above.
#
# MINIBOSS_ZONES order decides the IDs: only ever APPEND to it. Mooshroom Island, The Stronghold, Squid
# Coast, Arch Haven, Underhalls and Nether Wastes have no mini boss, so no check. A test
# (test_client_data.py) keeps this list in sync with ZONE_MINIBOSSES.
# ------------------------------------------------------------------
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
    """Location ID of a zone's mini boss kill check (zone must be in MINIBOSS_ZONES) - used by the
    client watcher to know which ID to send when a powerful mob is beaten there."""
    return MINI_BOSS_KILL_TABLE[mini_boss_kill_location_name(zone_internal_name)]


def get_base_mini_boss_zones() -> List[str]:
    """Mini boss zones that are base-game - gated by the MiniBossKillChecks option."""
    return [z for z in MINIBOSS_ZONES if ZONES_BY_NAME[z].category == "base"]


def get_dlc_mini_boss_zones() -> List[str]:
    """Mini boss zones that belong to a DLC pack - gated by the DlcMiniBossKillChecks option."""
    return [z for z in MINIBOSS_ZONES if ZONES_BY_NAME[z].category == "dlc"]


LOCATION_TABLE.update(MINI_BOSS_KILL_TABLE)


# ------------------------------------------------------------------
# Mini boss checks - the two modes (options mini_boss_kill_checks / dlc_mini_boss_kill_checks, values:
# 0 none, 1 per_type, 2 per_zone, 3 both):
#   per_zone: the "<Zone> - Mini Boss Kill" checks above. The client FORCES a mini boss to appear in every zone
#             that has one (the zone mini boss rule, see client/miniboss.py), so these are always obtainable.
#   per_type: one "Mini Boss - <Type>" check per kind of mini boss, sent the first time one of that kind is beaten
#             anywhere. A given kind cannot be forced to appear, so these locations are EXCLUDED (they can only hold
#             filler, never progression items) - see Regions.py.
# MINIBOSS_ZONE_TYPE is the mini boss pinned to each zone (client/miniboss.py ZONE_MINIBOSSES - a test keeps them
# in sync); MINIBOSS_TYPE_ORDER decides the per-type IDs: only ever APPEND to it.
# ------------------------------------------------------------------
MINIBOSS_MODE_NONE, MINIBOSS_MODE_PER_TYPE, MINIBOSS_MODE_PER_ZONE, MINIBOSS_MODE_BOTH = 0, 1, 2, 3

MINIBOSS_TYPE_ORDER: List[str] = [
    "enderman", "skeletonhorseman", "evoker", "redstonegolem", "illusioner",
    "drownednecromancer", "elderguardian", "wildfire", "ghast", "endersent",
    # Mini bosses the game does not pin to a zone and the client cannot summon: they only count when they show up
    # and get beaten (per-type checks only - see MINIBOSS_EXTRA_TYPE_ZONES). Appended, so no ID above moves.
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
# Where the types without a pinned zone show up, in order of preference (the first one wanted + available holds the
# check, see wanted_mini_boss_type_homes). Raid Captains were seen in Mooshroom Island (base game), Abyssal Monument
# and Basalt Deltas (DLC); the Endersent variants belong to the Echoing Void zones.
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
    """{type: zone whose Region holds that type's check}. A type's check exists when at least one AVAILABLE zone
    (its DLC pack on) that uses it has its group set to per_type or both; the home is the first such zone in
    MINIBOSS_TYPE_ZONES order (the zones that pin the type first, i.e. the earliest mission), so the check is
    reachable as early as possible."""
    homes: Dict[str, str] = {}
    for type_key in MINIBOSS_TYPE_ORDER:
        for zone in MINIBOSS_TYPE_ZONES[type_key]:
            if zone in available_zones and mini_boss_mode_wants_type(_mini_boss_mode_of_zone(zone, base_mode, dlc_mode)):
                homes[type_key] = zone
                break
    return homes


LOCATION_TABLE.update(MINI_BOSS_TYPE_TABLE)


# ------------------------------------------------------------------
# Eye of Ender checks (option eye_of_ender_checks): one check per EEyeOfEnderType (Dungeons SDK enum:
# 1 DEEPWOOD_BROOK, 2 OLD_TOWN_HALL, 3 SUNKEN_WATCHTOWER, 4 THE_UNDERCROFT, 5 THE_TRIAL_OF_THE_NAMELESS,
# 6 HIGHBLOCK_HIDEAWAY - each Eye is named after the mission it opens at the Stronghold frame). A mini boss
# drops each Eye; the client sends the check when the Eye shows up in the level, reading its type.
# The sub-missions (Deepwood Brook, Old Town Hall...) are not zones here, so every check lives in the PARENT
# mission of its chain. IDs: own block, order = Eye type, only ever append.
# ------------------------------------------------------------------
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


# Villager checks (option villager_checks): freeing a captured villager in a mission is a check, and the camp merchant it
# would have unlocked arrives as an item instead (Items.py VILLAGER_ITEM_NAMES). Only the five captured camp merchants of
# the base game are known (zones from the wiki); the DLC ones are not yet - the client logs every merchant unlock it sees
# (dungeons_bridge_debug.log / /villager), so the missing ones can be added here once their class names are known.
VILLAGER_TYPES: List[Tuple[str, str, str, str]] = [
    # (key, lowercase fragment of the merchant definition class name, display name = item name, zone internal name)
    ("village", "villagemerchant", "Village Merchant", "creeperwoods"),
    ("mystery", "mysterymerchant", "Mystery Merchant", "pumpkinpastures"),
    ("luxury", "luxurymerchant", "Luxury Merchant", "cacticanyon"),
    ("giftwrapper", "giftwrappermerchant", "Gift Wrapper", "soggyswamp"),
    ("blacksmith", "blacksmithmerchant", "Blacksmith", "redstonemines"),
    # Added at the END so no villager location/item ID shifts. The fragments are guesses from the lobby actors
    # BP_LobbyVillagerEnchanterMerchant_C / BP_LobbyVillagerPiglinMerchant_C (matched as a substring of the lowercase
    # merchant definition class name, so "enchant" covers Enchanter/Enchantment/Enchantsmith).
    ("enchantsmith", "enchant", "Enchantsmith", "highblockhalls"),
    # The Piglin Merchant is not rescued: it is unlocked by completing at least one Ancient Hunt (so this check
    # exists only with the ancient_hunts option; its region is the first hunt, any hunt triggers it in game).
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


# ------------------------------------------------------------------
# Ancient Hunt boss-kill locations: one "Boss Kill" check per hunt
# (Woodland Mansion, Woodland Prison, Spider Cave), separate from that
# hunt's existing "Mission Complete" location. Gated by the
# ancient_hunt_boss_checks option (off by default, see Options.py) - only
# meaningful when ancient_hunts is also on, since these get added to the
# same Region as the hunt's own Mission Complete in Regions.py and are
# unreachable if that Region doesn't exist this generation.
#
# Not naming these after a specific creature (e.g. "Ancient Evoker") -
# unlike BOSS_ZONE above, what exactly you're hunting in each of these
# three missions isn't confirmed, so the location is just named after
# the hunt itself.
# ------------------------------------------------------------------

ANCIENT_HUNT_BOSS_KILL_BASE_ID = 0xDC3800  # separate block - clear of BOSS_KILL's and ZONE_CHEST's ranges


def ancient_hunt_boss_kill_location_name(hunt_display_name: str) -> str:
    return f"{hunt_display_name} - Boss Kill"


ANCIENT_HUNT_BOSS_KILL_TABLE: Dict[str, int] = {
    ancient_hunt_boss_kill_location_name(z.display_name): ANCIENT_HUNT_BOSS_KILL_BASE_ID + i
    for i, z in enumerate(ANCIENT_HUNT_ZONES)
}

LOCATION_TABLE.update(ANCIENT_HUNT_BOSS_KILL_TABLE)


# ------------------------------------------------------------------
# Per-zone, per-chest locations, for zones with a CONFIRMED fixed chest
# layout - verified by the player directly, zone by zone (not assumed
# for every zone in the game; other zones may have randomized chest
# placement/count and shouldn't be added here until checked). Each
# zone gets its own "<Zone> - Chest N" series (normal Wooden/Fancy/
# Deluxe chests) and, separately, "<Zone> - Supply Chest N" series
# (Supply Station) - two independent counters per zone, since they're
# detected via two different real class-name patterns (see
# dungeons_reader.py's classify_interactable_class) and the two counts
# don't have to match.
#
# ZONE_CHEST_COUNTS: internal_name -> (chest_count, supply_chest_count).
# Confirmed live, one zone at a time, via dungeons_bridge.dll's
# OnInteracted hook. Add a new zone here (and keep ap_world/locations.py's
# mirror in sync) once its layout is confirmed fixed too.
# ------------------------------------------------------------------

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

# ------------------------------------------------------------------
# DLC zones. To support the chests of a DLC mission, measure them in-game and add ONE line here:
#     "dingyjungle": (chest_count, supply_chest_count),
# The client helps: opening chests in a zone that is not listed yet logs a running tally
# ("... chest(s), ... supply chest(s) opened this run") and keeps the highest tally seen per zone in
# dlc_chest_counts_seen.json, so a full clear of the mission gives the numbers to enter below.
# The zone must be a DLC zone of ZoneData.py (its chests only exist when its DLC pack is enabled).
#
# Location IDs for DLC chests come from their own block (DLC_ZONE_CHEST_BASE_ID), with a FIXED
# range per zone derived from the zone's position in ZoneData.ZONES. Adding or changing one DLC
# zone therefore never shifts the IDs of any other zone, and the base-game IDs (block
# ZONE_CHEST_BASE_ID) never change either - old seeds stay valid. Keep a zone's total
# (chests + supply chests) under DLC_ZONE_CHEST_SLOTS_PER_ZONE.
# ------------------------------------------------------------------
DLC_ZONE_CHEST_COUNTS: Dict[str, Tuple[int, int]] = {
    # (chests, supply chests) as read off the in-game map of each mission. Secrets are NOT counted (they
    # send no chest check); a secret that turns out to be a chest still feeds the global bonus pool.
    # Nether pack: older measurements (minimum of the chest counter), not re-measured yet.
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

# Base-game zones measured AFTER the original base table was frozen. They are allocated after every
# zone of BASE_ZONE_CHEST_COUNTS, so no existing base-game ID shifts (see the loop below).
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


# Sorted for deterministic ID assignment across regeneration runs - dict
# iteration is insertion order in modern Python, but sorting explicitly
# removes any dependency on ZONE_CHEST_COUNTS's literal key order
# staying the same if it's ever reordered/edited later.
# Base game: sequential IDs in sorted zone order. NEVER add DLC zones to this loop - that would
# shift every base-game ID that sorts after them (they get their own block just below).
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
    """Given a zone and a 1-based chest number, returns its location ID -
    used by the client watcher to know which ID to send as each chest
    gets opened, in discovery order."""
    return ZONE_CHEST_TABLE[zone_chest_location_name(zone_internal_name, n)]


def get_zone_supply_chest_location_id(zone_internal_name: str, n: int) -> int:
    """Same as get_zone_chest_location_id, for the Supply Chest series."""
    return ZONE_CHEST_TABLE[zone_supply_chest_location_name(zone_internal_name, n)]


LOCATION_TABLE.update(ZONE_CHEST_TABLE)


# ------------------------------------------------------------------
# GLOBAL bonus chest locations - not tied to any specific zone. A
# zone's confirmed count (ZONE_CHEST_COUNTS above) is what's been
# directly verified so far, but any confirmed zone may turn out to have
# MORE chests than that in practice - a layout variant, a secret room,
# etc. Every EXTRA chest found, across ALL confirmed zones combined
# (beyond that zone's own confirmed baseline), sends the next globally-
# numbered "Bonus Chest N" check, 1:1 - discovery order across every
# zone at once, not per-zone.
#
# The full table below is a fixed 100-slot headroom, independent of any
# one player's BonusChestCount choice (Options.py, 0-100, default 20) -
# same reasoning as EMERALD_MILESTONE_TABLE: AP resolves the name->id
# table once at the class level, before per-slot options are locked in,
# so it can't be sized to just one player's chosen count.
# get_bonus_chest_locations() below is what Regions.py actually uses
# per-player, trimmed to that player's count.
#
# These are marked LocationProgressType.EXCLUDED in Regions.py, NOT
# just left as ordinary locations - reaching a high-numbered bonus
# chest requires finding a lot of extra chests across every zone
# combined, which the fill algorithm has no way to know is "hard" on
# its own; excluding them explicitly guarantees the fill algorithm
# never places a progression item there, so an early-game item can
# never end up stranded behind, say, Bonus Chest 100.
# ------------------------------------------------------------------

MAX_BONUS_CHESTS = 100  # full static headroom - matches BonusChestCount's range_end
BONUS_CHEST_BASE_ID = 0xDC5000  # separate block - clear of every other range above


def bonus_chest_location_name(n: int) -> str:
    return f"Bonus Chest {n}"


BONUS_CHEST_TABLE: Dict[str, int] = {
    bonus_chest_location_name(n): BONUS_CHEST_BASE_ID + n
    for n in range(1, MAX_BONUS_CHESTS + 1)
}


def get_bonus_chest_location_id(n: int) -> int:
    """Given a 1-based global bonus-slot number, returns its location ID
    directly - used by the client watcher each time an extra chest is
    found (in any confirmed zone, beyond that zone's own baseline)."""
    return BONUS_CHEST_TABLE[bonus_chest_location_name(n)]


def get_bonus_chest_locations(count: int) -> Dict[str, int]:
    """Returns {location_name: id} for however many bonus slots THIS
    player's BonusChestCount choice allows (0..MAX_BONUS_CHESTS) - NOT
    the full static headroom above. Used per-player in Regions.py."""
    count = max(0, min(count, MAX_BONUS_CHESTS))
    return {
        bonus_chest_location_name(n): get_bonus_chest_location_id(n)
        for n in range(1, count + 1)
    }


LOCATION_TABLE.update(BONUS_CHEST_TABLE)
