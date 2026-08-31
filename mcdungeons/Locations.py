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

ZONE_CHEST_COUNTS: Dict[str, Tuple[int, int]] = {
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

ZONE_CHEST_BASE_ID = 0xDC4000  # separate block - clear of every other range above


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
for _zone_name in sorted(ZONE_CHEST_COUNTS):
    _chest_n, _supply_n = ZONE_CHEST_COUNTS[_zone_name]
    for _n in range(1, _chest_n + 1):
        ZONE_CHEST_TABLE[zone_chest_location_name(_zone_name, _n)] = _alloc_chest_id()
    for _n in range(1, _supply_n + 1):
        ZONE_CHEST_TABLE[zone_supply_chest_location_name(_zone_name, _n)] = _alloc_chest_id()


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
