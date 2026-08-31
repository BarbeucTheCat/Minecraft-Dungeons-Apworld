from typing import TYPE_CHECKING, Dict, List

from BaseClasses import Region, Entrance, LocationProgressType

from .ZoneData import ZONES, ANCIENT_HUNT_ZONES, ZoneInfo, ZONES_BY_NAME, skip_level_requires_map
from .Locations import (
    LOCATIONS_BY_ZONE, LOCATION_TABLE, get_emerald_milestone_locations,
    get_emerald_milestone_id, milestone_location_name,
    BOSS_KILL_TABLE, BOSS_ZONE, boss_kill_location_name,
    get_base_boss_names, get_dlc_boss_names,
    ANCIENT_HUNT_BOSS_KILL_TABLE, ancient_hunt_boss_kill_location_name,
    ZONE_CHEST_COUNTS, ZONE_CHEST_TABLE, zone_chest_location_name, zone_supply_chest_location_name,
    get_bonus_chest_locations,
)
from .Rules import make_zone_rule, make_ancient_hunt_rule
from .Options import GoalMission, GOAL_MISSION_ID_TO_ZONE

#Victory event item names, see _place_victory_event's docstring below for
#why these are locked items rather than a can_reach_location rule.
EMERALD_GOAL_EVENT_NAME = "Victory - Emerald Goal"
GOAL_MISSION_EVENT_NAME = "Victory - Goal Mission"


def _place_victory_event(world: "MCDungeonsWorld", region: Region, location_name: str, event_name: str) -> None:
    """Attach a Victory item to an existing real location, the top
    emerald milestone or a mission's own Mission Complete check, both
    reported by dungeons_reader.py when used.

    Not modeled as a state.can_reach_location(...) rule: these locations
    have no access_rule of their own, so that would be trivially True from
    the start of generation, it couldn't distinguish "goal reached" from
    "game just started". A locked item only enters state.has(...) once the
    location is actually checked, which only happens when the running game
    reports it, exactly the gate we want.

    Uses world.create_item (a real, allocated item), NOT a code=None event
    item: AP core's serialization asserts a location with a real address
    must hold an item with a real code too, see Items.py's "Victory - ..."
    entries for the two items this uses."""
    for loc in region.locations:
        if loc.name == location_name:
            loc.place_locked_item(world.create_item(event_name))
            return
    raise RuntimeError(f"Expected location '{location_name}' to exist before attaching a Victory event to it")

if TYPE_CHECKING:
    from . import MCDungeonsWorld


def _dlc_enabled(world: "MCDungeonsWorld", zone: ZoneInfo) -> bool:
    """Base zones are always enabled. DLC zones check the pack's option."""
    if zone.category == "base":
        return True
    return bool(getattr(world.options, zone.dlc_pack))


def create_regions(world: "MCDungeonsWorld") -> None:
    player = world.player
    multiworld = world.multiworld

    menu = Region("Menu", player, multiworld)
    multiworld.regions.append(menu)

    all_zones: List[ZoneInfo] = list(ZONES)
    enabled_zones = [z for z in all_zones if _dlc_enabled(world, z)]
    enabled_names = {z.internal_name for z in enabled_zones}

    region_by_name: Dict[str, Region] = {}

    #create a Region per enabled zone, with its "Mission Complete" location
    dlc_missions_optional = bool(world.options.dlc_missions_optional)
    goal_mission_id = world.options.goal_mission.value
    goal_mission_zone = (GOAL_MISSION_ID_TO_ZONE[goal_mission_id]
                          if goal_mission_id != GoalMission.option_none else None)
    for zone in enabled_zones:
        region = Region(zone.display_name, player, multiworld)
        location_name = LOCATIONS_BY_ZONE[zone.internal_name]
        region.add_locations({location_name: LOCATION_TABLE[location_name]}, world.location_class)
        multiworld.regions.append(region)
        region_by_name[zone.internal_name] = region

    #DlcMissionsOptional (default on): excludes all DLC zone's 
    #check the same way as Bonus Chests below, so the fill never
    #put a required item behind a check in a DLC mission. Only the
    #Mission Complete location is excluded, Access item, chests, and
    #boss kills stay untouched, so DLC content is still explorable.

    #Exception: if the player chose this zone as their GoalMission, it's
    #their real win condition (see _place_victory_event below) - an
    #explicit goal always wins over the "optional" default.
        if (dlc_missions_optional and zone.category == "dlc"
                and zone.internal_name != goal_mission_zone):
            for loc in region.locations:
                if loc.name == location_name:
                    loc.progress_type = LocationProgressType.EXCLUDED
                    break

        #The Stronghold's real in-game unlock isn't "beat the previous
        #mission" at all - it needs Eyes of Ender collected across 6
        #OTHER specific missions, a cross-mission requirement this
        #logic doesn't model yet (planned as a real check later). Its
        #requires[] above (Obsidian Pinnacle) is a placeholder standing
        #in for "somewhere reasonably late," not a claim that beating
        #Obsidian Pinnacle actually unlocks it in-game. Excluding both
        #this location and its boss kill (added further below) the
        #same way DLC-optional missions are just above guarantees the
        #fill algorithm never strands a required item behind a mission
        #that can't reliably be completed on the timeline the logic
        #assumes.
        if zone.internal_name == "thestronghold":
            for loc in region.locations:
                if loc.name == location_name:
                    loc.progress_type = LocationProgressType.EXCLUDED
                    break

        #Mooshroom Island ("??? (Mooshroom Island)") is a secret,
        #post-game-only zone - only reachable after everything else is
        #already done. Its Access item was already changed from
        #progression to useful for the same reason (marking it
        #progression pushed the fill algorithm to treat unlocking it as
        #something to prioritize early, which makes no sense for
        #content you can only reach at the very end). This is the other
        #half of that: its own Mission Complete check (and its boss
        #kill check, added further below) are excluded the same way
        #DLC-optional missions are just above, so the fill algorithm
        #only ever places filler there - nothing anyone (this player or
        #any other) actually needs to progress can end up locked behind
        #finishing a hidden bonus zone. Same goal_mission_zone
        #exception as the DLC-optional block above: it's technically
        #selectable as a GoalMission (GOAL_MISSION_ID_TO_ZONE covers
        #every zone), and if a player explicitly chose it as their real
        #win condition, excluding it would directly contradict that
        #choice.
        if zone.internal_name == "mooshroomisland" and zone.internal_name != goal_mission_zone:
            for loc in region.locations:
                if loc.name == location_name:
                    loc.progress_type = LocationProgressType.EXCLUDED
                    break

    #Per-zone confirmed-fixed chest locations (see Locations.py's
    #ZONE_CHEST_COUNTS comment) - each zone's own Chest/Supply Chest
    #locations attach to that zone's own region, alongside its
    #Mission Complete check. Only zones already in ZONE_CHEST_COUNTS
    #get this (verified fixed layout so far); a zone whose DLC pack
    #is disabled this generation is simply skipped, same as every
    #other optional location group below.
    for zone_name, (chest_n, supply_n) in ZONE_CHEST_COUNTS.items():
        region = region_by_name.get(zone_name)
        if not region:
            continue  # that zone's DLC pack is off this generation
        zone_chest_locations: Dict[str, int] = {}
        for n in range(1, chest_n + 1):
            name = zone_chest_location_name(zone_name, n)
            zone_chest_locations[name] = ZONE_CHEST_TABLE[name]
        for n in range(1, supply_n + 1):
            name = zone_supply_chest_location_name(zone_name, n)
            zone_chest_locations[name] = ZONE_CHEST_TABLE[name]
        region.add_locations(zone_chest_locations, world.location_class)

    #Global bonus chest locations: extra chests found across ALL
    #confirmed zones combined, beyond each zone's own baseline (see
    #Locations.py's get_bonus_chest_locations comment) - NOT tied to
    #any specific zone's region, unlike the base chest locations
    #above. Attached to Menu (always reachable) the same way
    #emerald milestones are below, trimmed to THIS player's
    #BonusChestCount option, not the full static headroom.

    #Marked EXCLUDED, not left as ordinary locations: reaching a
    #high-numbered bonus chest requires finding a lot of extra
    #chests across every zone combined, which isn't guaranteed
    #early in a run (or at all) - excluding them explicitly
    #guarantees the fill algorithm never strands a progression
    #item behind one.
    bonus_count = world.options.bonus_chest_count.value
    bonus_locations = get_bonus_chest_locations(bonus_count)
    menu.add_locations(bonus_locations, world.location_class)
    for loc in menu.locations:
        if loc.name in bonus_locations:
            loc.progress_type = LocationProgressType.EXCLUDED

    #wire entrances according to requires[], skipping any requirement
    #that points at a zone whose DLC pack ended up disabled
    skip_level_map = skip_level_requires_map(enabled_names)
    for zone in enabled_zones:
        target = region_by_name[zone.internal_name]

        if not zone.requires:
            #No predecessor so reachable straight from Menu.
            menu.connect(target, f"Menu -> {zone.display_name}")
            continue

        #skip_level_requires_map replaces "needs your direct predecessor's
        #Access item" with "needs whichever zone(s) unlocked THAT
        #predecessor" - see its own docstring for the full reasoning.
        #Falls back to zone.requires itself for the cases it deliberately
        #leaves untouched (root-adjacent zones, and Mooshroom Island's
        #postgame everything-gate).
        valid_requires = [r for r in skip_level_map.get(zone.internal_name, zone.requires) if r in enabled_names]
        if not valid_requires:
            #Every real predecessor got disabled (their DLC pack is off) -
            #fall back to Menu so the zone isn't stranded/unreachable.
            menu.connect(target, f"Menu -> {zone.display_name}")
            continue

        #AND logic across all valid predecessors: connect the LAST
        #predecessor's region -> target, with a rule that also checks
        #every earlier predecessor is reachable. This keeps a single
        #Entrance per zone regardless of how many prerequisites it has.
        source = region_by_name[valid_requires[-1]]
        rule = make_zone_rule(world, zone, valid_requires)
        entrance = source.connect(target, f"{valid_requires[-1]} -> {zone.display_name}")
        entrance.access_rule = rule

    #Ancient Hunts: separate, no predecessor chain, gated by one item ---
    if world.options.ancient_hunts:
        for hunt in ANCIENT_HUNT_ZONES:
            region = Region(hunt.display_name, player, multiworld)
            location_name = LOCATIONS_BY_ZONE[hunt.internal_name]
            region.add_locations({location_name: LOCATION_TABLE[location_name]}, world.location_class)
            if world.options.ancient_hunt_boss_checks:
                boss_loc_name = ancient_hunt_boss_kill_location_name(hunt.display_name)
                region.add_locations({boss_loc_name: ANCIENT_HUNT_BOSS_KILL_TABLE[boss_loc_name]}, world.location_class)
            multiworld.regions.append(region)
            entrance = menu.connect(region, f"Menu -> {hunt.display_name}")
            entrance.access_rule = make_ancient_hunt_rule(world)

    #Emerald milestones: no region of their own, just currency checks ---
    #against a running total (dungeons_reader.py reports these via ---
    #ap_client.py). Only the player's actual goal's worth of ---
    #milestones gets added, even though the full 500..500000 range ---
    #exists in LOCATION_TABLE for id-table purposes.

    # EmeraldMode (see Options.py) decouples "do the intermediate
    # milestone checks exist" from "does reaching the goal amount matter
    # for victory": "both" (the default) does both, and every milestone
    # up to the goal gets added. If EmeraldMode is "goal" only (checks
    # off), none of the intermediate checks are added, but the single
    # TOP milestone still needs to exist as a real Location so the
    # completion condition below has something to check reachability
    # against - it's added on its own in that case.
    goal = world.options.emerald_goal.value
    emerald_checks_on = world.options.emerald_mode.checks_enabled
    emerald_is_goal = world.options.emerald_mode.goal_enabled

    if goal >= 500:
        if emerald_checks_on:
            milestone_locations = get_emerald_milestone_locations(goal)
            menu.add_locations(milestone_locations, world.location_class)
            # EXCLUDED for the same reason Bonus Chests are, just above:
            # there's no real logic modeling how much playtime it takes to
            # earn a given emerald total (see _place_victory_event's own
            # comment on this same gap for the goal condition). Without
            # this, the fill algorithm could freely strand a genuinely
            # required progression item behind the TOP milestone - a goal
            # of 45000 emeralds, say - forcing an enormous, unguaranteed
            # grind before that item is even reachable. Harmless if this
            # milestone also ends up hosting the locked Victory event
            # below (place_locked_item bypasses progress_type entirely,
            # same as it does for Bonus Chests).
            for loc in menu.locations:
                if loc.name in milestone_locations:
                    loc.progress_type = LocationProgressType.EXCLUDED
        elif emerald_is_goal:
            top_amount = (goal // 500) * 500
            top_name = milestone_location_name(top_amount)
            menu.add_locations({top_name: get_emerald_milestone_id(top_amount)}, world.location_class)
            for loc in menu.locations:
                if loc.name == top_name:
                    loc.progress_type = LocationProgressType.EXCLUDED

    #Boss first-kills: opt-in, and split into two independent
    #toggles, BossKillChecks for base-game bosses, DlcBossKillChecks
    #for DLC bosses (Options.py), so you can enable checks for one
    #group without the other. Each check is attached to its own
    #zone's Region rather than Menu, same access rule + DLC-pack
    #gating as that zone's Mission Complete check. A boss whose zone
    #didn't make it into region_by_name (DLC pack disabled) is simply
    #skipped, its check isn't offered, matching that zone being
    #unreachable this generation. See Locations.py's BOSS_ZONE
    #comment for which two boss->zone mappings are still unconfirmed.
    wanted_bosses = set()
    if world.options.boss_kill_checks:
        wanted_bosses.update(get_base_boss_names())
    if world.options.dlc_boss_kill_checks:
        wanted_bosses.update(get_dlc_boss_names())

    for boss_name in wanted_bosses:
        zone_internal_name = BOSS_ZONE[boss_name]
        region = region_by_name.get(zone_internal_name)
        if not region:
            continue  # that zone's DLC pack is off this generation
        loc_name = boss_kill_location_name(boss_name)
        region.add_locations({loc_name: BOSS_KILL_TABLE[loc_name]}, world.location_class)
        if zone_internal_name == "thestronghold":
            # Same reasoning as the Mission Complete exclusion above
            for loc in region.locations:
                if loc.name == loc_name:
                    loc.progress_type = LocationProgressType.EXCLUDED
                    break
        if zone_internal_name == "mooshroomisland" and zone_internal_name != goal_mission_zone:
            # Same reasoning and same goal_mission_zone exception as
            # mooshroomisland's Mission Complete exclusion above
            # filler only, unless it's the player's own chosen goal.
            for loc in region.locations:
                if loc.name == loc_name:
                    loc.progress_type = LocationProgressType.EXCLUDED
                    break

    #Win condition: Emerald Mode (reaching your Emerald Goal) and/or
    #GoalMission (reaching a specific mission), and together when
    #both are active (state.has_all requires every event name
    #present). generate_early (__init__.py) already guarantees at
    #least one of the two is active, so victory_event_names is never
    #empty by the time we get here. See _place_victory_event above
    #for why these are locked events, not a can_reach_location rule.
    victory_event_names: List[str] = []

    if emerald_is_goal and goal >= 500:
        top_name = milestone_location_name((goal // 500) * 500)
        _place_victory_event(world, menu, top_name, EMERALD_GOAL_EVENT_NAME)
        victory_event_names.append(EMERALD_GOAL_EVENT_NAME)

    goal_mission_id = world.options.goal_mission.value
    if goal_mission_id != GoalMission.option_none:
        goal_zone_name = GOAL_MISSION_ID_TO_ZONE[goal_mission_id]
        goal_loc_name = LOCATIONS_BY_ZONE[goal_zone_name]
        goal_region = region_by_name[goal_zone_name]
        _place_victory_event(world, goal_region, goal_loc_name, GOAL_MISSION_EVENT_NAME)
        victory_event_names.append(GOAL_MISSION_EVENT_NAME)

    multiworld.completion_condition[player] = lambda state: state.has_all(victory_event_names, player)
