from typing import TYPE_CHECKING, Dict, List

from BaseClasses import Region, Entrance, LocationProgressType

from .ZoneData import ZONES, ANCIENT_HUNT_ZONES, ZoneInfo, ZONES_BY_NAME, skip_level_requires_map
from .Locations import (
    SECRET_ZONE_FOUND_BY_ZONE, SECRET_PARENT_TO_ZONE,
    LOCATIONS_BY_ZONE, LOCATION_TABLE, get_emerald_milestone_locations,
    get_emerald_milestone_id, milestone_location_name,
    BOSS_KILL_TABLE, BOSS_ZONE, boss_kill_location_name,
    get_base_boss_names, get_dlc_boss_names,
    MINI_BOSS_KILL_TABLE, mini_boss_kill_location_name, wanted_mini_boss_zones, wanted_mini_boss_type_homes,
    MINI_BOSS_TYPE_TABLE, mini_boss_type_location_name,
    EYE_OF_ENDER_TYPES, EYE_OF_ENDER_TABLE, eye_of_ender_location_name,
    VILLAGER_TYPES, VILLAGER_TABLE, villager_location_name,
    ANCIENT_HUNT_BOSS_KILL_TABLE, ancient_hunt_boss_kill_location_name,
    ZONE_CHEST_COUNTS, ZONE_CHEST_TABLE, zone_chest_location_name, zone_supply_chest_location_name,
    get_bonus_chest_locations,
)
from .Rules import make_zone_rule, make_ancient_hunt_rule
from .Options import GoalMission, GOAL_MISSION_ID_TO_ZONE, DLC_PACK_BY_LABEL

#Victory event item names, see _place_victory_event's docstring below for
#why these are locked items rather than a can_reach_location rule.
EMERALD_GOAL_EVENT_NAME = "Victory - Emerald Goal"
GOAL_MISSION_EVENT_NAME = "Victory - Goal Mission"


def _place_victory_event(world: "MCDungeonsWorld", region: Region, location_name: str, event_name: str) -> None:
    """Attached to an existing real location (emerald milestone or Mission Complete check).
No access_rule: the item only enters state.has() once the running game reports the location checked.
Real codes, not events — AP core rejects code=None on a real-address location."""
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
    
    # Which packs are optional (locations never hold progression items):
    #   RequiredDlcs non-empty → only those are required, rest are optional.
    #   RequiredDlcs empty → DlcMissionsOptional decides for all.
    # The Goal Mission's pack is always required.   
    enabled_dlc_packs = {z.dlc_pack for z in enabled_zones if z.category == "dlc"}
    chosen_packs = {DLC_PACK_BY_LABEL[label] for label in world.options.required_dlcs.value}
    if chosen_packs:
        required_packs = chosen_packs & enabled_dlc_packs
    else:
        required_packs = set() if dlc_missions_optional else set(enabled_dlc_packs)
    if goal_mission_zone is not None and ZONES_BY_NAME[goal_mission_zone].category == "dlc":
        required_packs.add(ZONES_BY_NAME[goal_mission_zone].dlc_pack)
    optional_dlc_packs = enabled_dlc_packs - required_packs
    world.optional_dlc_packs = sorted(optional_dlc_packs)   # read by fill_slot_data (client map colours)
    for zone in enabled_zones:
        region = Region(zone.display_name, player, multiworld)
        location_name = LOCATIONS_BY_ZONE[zone.internal_name]
        region.add_locations({location_name: LOCATION_TABLE[location_name]}, world.location_class)
        multiworld.regions.append(region)
        region_by_name[zone.internal_name] = region

    # DlcMissionsOptional (default on): excludes DLC Mission Complete checks from progression fill
    # (same as Bonus Chests). Access items, chests, and boss kills stay — DLC is still explorable.
    # Exception: the Goal Mission zone is never treated as optional.   
        if zone.category == "dlc" and zone.dlc_pack in optional_dlc_packs:
            for loc in region.locations:
                if loc.name == location_name:
                    loc.progress_type = LocationProgressType.EXCLUDED
                    break

    # requires[] (Obsidian Pinnacle) is a placeholder for "reasonably late" — the real in-game unlock
    # (Eyes of Ender from 6 other missions) is modeled separately in Rules.py (needs_eyes).
    # Location and boss kill excluded from progression fill so the fill never strands a required item here.   
        if zone.internal_name == "thestronghold" and not world.options.eye_of_ender_checks:
            for loc in region.locations:
                if loc.name == location_name:
                    loc.progress_type = LocationProgressType.EXCLUDED
                    break

    # Post-game-only secret zone. Checks excluded from progression fill (same as DLC-optional) so only
    # filler lands here — nothing needed to progress is locked behind a hidden bonus zone.
    # Exception: if chosen as GoalMission, never excluded.   
        if zone.internal_name == "mooshroomisland" and zone.internal_name != goal_mission_zone:
            for loc in region.locations:
                if loc.name == location_name:
                    loc.progress_type = LocationProgressType.EXCLUDED
                    break

    # Fixed chest locations per zone (verified layout, see ZONE_CHEST_COUNTS), attached to the zone's own region.
    # Only zones in ZONE_CHEST_COUNTS get these; disabled DLC packs are skipped like other optional groups.   
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

    # Only with the Secret Mission Access option on. Attaches to the parent's region (the trigger is there).
    # Unlocks the secret zone via its Secret Access item, not by finding the secret in-game.
    # Skipped if parent or secret zone is disabled; DLC parents get the optional-DLC exclusion.   
    if world.options.secret_missions_require_secret:
        for parent_name, secret_name in SECRET_PARENT_TO_ZONE.items():
            parent_region = region_by_name.get(parent_name)
            if not parent_region or secret_name not in enabled_names:
                continue
            found_name = SECRET_ZONE_FOUND_BY_ZONE[secret_name]
            parent_region.add_locations({found_name: LOCATION_TABLE[found_name]}, world.location_class)
            parent_zone = next(z for z in enabled_zones if z.internal_name == parent_name)
            if parent_zone.category == "dlc" and parent_zone.dlc_pack in optional_dlc_packs:
                for loc in parent_region.locations:
                    if loc.name == found_name:
                        loc.progress_type = LocationProgressType.EXCLUDED
                        break

    # Global bonus chests (not tied to a zone), attached to Menu, trimmed to this player's BonusChestCount.
    # Excluded from progression fill: reaching a high-numbered one isn't guaranteed early (or at all).   
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

        # Skips one level in the requires chain: uses the predecessor's own requires instead of the predecessor itself.
        # Falls back to zone.requires for root-adjacent zones and Mooshroom Island.   
        valid_requires = [r for r in skip_level_map.get(zone.internal_name, zone.requires) if r in enabled_names]
        if not valid_requires:
            #Every real predecessor got disabled (their DLC pack is off) -
            #fall back to Menu so the zone isn't stranded/unreachable.
            menu.connect(target, f"Menu -> {zone.display_name}")
            continue

        # Single entrance per zone: connects the last predecessor's region to the target, with a rule requiring all earlier predecessors to be reachable.   
        source = region_by_name[valid_requires[-1]]
        rule = make_zone_rule(world, zone, valid_requires)
        entrance = source.connect(target, f"{valid_requires[-1]} -> {zone.display_name}")
        entrance.access_rule = rule

    #Ancient Hunts: separate, no predecessor chain, gated by one item ---
    hunt_region_by_name: Dict[str, Region] = {}
    if world.options.ancient_hunts:
        for hunt in ANCIENT_HUNT_ZONES:
            region = Region(hunt.display_name, player, multiworld)
            hunt_region_by_name[hunt.internal_name] = region
            location_name = LOCATIONS_BY_ZONE[hunt.internal_name]
            region.add_locations({location_name: LOCATION_TABLE[location_name]}, world.location_class)
            if world.options.ancient_hunt_boss_checks:
                boss_loc_name = ancient_hunt_boss_kill_location_name(hunt.display_name)
                region.add_locations({boss_loc_name: ANCIENT_HUNT_BOSS_KILL_TABLE[boss_loc_name]}, world.location_class)
            multiworld.regions.append(region)
            entrance = menu.connect(region, f"Menu -> {hunt.display_name}")
            entrance.access_rule = make_ancient_hunt_rule(world)

    # Currency checks against a running total, attached to Menu (no region of their own).
    # EmeraldMode "both" adds all milestones up to the goal; "goal" adds only the top one (still needed for the completion check).   
    goal = world.options.emerald_goal.value
    emerald_checks_on = world.options.emerald_mode.checks_enabled
    emerald_is_goal = world.options.emerald_mode.goal_enabled

    if goal >= 500:
        if emerald_checks_on:
            milestone_locations = get_emerald_milestone_locations(goal)
            menu.add_locations(milestone_locations, world.location_class)
            # Excluded from progression fill (same as Bonus Chests): no logic models emerald grind time, so a required item
            # could be stranded behind a high milestone. Harmless if it hosts the Victory event (place_locked_item bypasses progress_type).   
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

    # Boss first-kill checks, two independent toggles (base / DLC).
    # Attached to the zone's own region, same gating as its Mission Complete check; skipped if the zone's DLC pack is off.
    # Two boss→zone mappings still unconfirmed (see BOSS_ZONE in Locations.py).   
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
        if zone_internal_name == "thestronghold" and not world.options.eye_of_ender_checks:
            # Same reasoning as the Mission Complete exclusion above (lifted once the Eyes are modelled)
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

    # Mini boss kill checks, two independent toggles (base / DLC).
    # Attached to the zone's own region; skipped if the zone's DLC pack is off.   
    mini_boss_base_mode = world.options.mini_boss_kill_checks.value
    mini_boss_dlc_mode = world.options.dlc_mini_boss_kill_checks.value

    # per_zone / both: one check per zone, in the zone's own Region (always obtainable: the client forces a mini boss there)
    for zone_internal_name in sorted(wanted_mini_boss_zones(mini_boss_base_mode, mini_boss_dlc_mode)):
        region = region_by_name.get(zone_internal_name)
        if not region:
            continue  # that zone's DLC pack is off this generation
        loc_name = mini_boss_kill_location_name(zone_internal_name)
        region.add_locations({loc_name: MINI_BOSS_KILL_TABLE[loc_name]}, world.location_class)

    # per_type / both: one check per kind of mini boss. A given kind cannot be forced to appear, so these are
    # EXCLUDED (filler only, never a progression item), in the earliest zone that uses that kind.
    for type_key, home_zone in wanted_mini_boss_type_homes(
            mini_boss_base_mode, mini_boss_dlc_mode, set(region_by_name)).items():
        region = region_by_name[home_zone]
        type_loc_name = mini_boss_type_location_name(type_key)
        region.add_locations({type_loc_name: MINI_BOSS_TYPE_TABLE[type_loc_name]}, world.location_class)
        for loc in region.locations:
            if loc.name == type_loc_name:
                loc.progress_type = LocationProgressType.EXCLUDED

    # Eye of Ender checks: one per Eye type, only with eye_of_ender_checks on. Placed in the parent mission
    # of its chain. The six matching "Eye of Ender" items gate The Stronghold in Rules.py.
    if world.options.eye_of_ender_checks:
        for eye_type, _mission, eye_zone in EYE_OF_ENDER_TYPES:
            region = region_by_name.get(eye_zone)
            if not region:
                continue
            eye_loc_name = eye_of_ender_location_name(eye_type)
            region.add_locations({eye_loc_name: EYE_OF_ENDER_TABLE[eye_loc_name]}, world.location_class)

    #Villager checks: one per captured camp merchant, placed in the mission where the villager is held. The matching
    #merchant items are plain "useful" items (no logic depends on them).
    if world.options.villager_checks:
        for villager_key, _fragment, _display, villager_zone in VILLAGER_TYPES:
            region = region_by_name.get(villager_zone) or hunt_region_by_name.get(villager_zone)
            if not region:
                continue
            villager_loc_name = villager_location_name(villager_key)
            region.add_locations({villager_loc_name: VILLAGER_TABLE[villager_loc_name]}, world.location_class)

    # Pack-wide exclusion for optional DLC packs: no progression items in any of their locations (chests, boss/mini boss kills, secrets, etc.).
    # Separate from the Mission Complete exclusion above, which applies to all zones.   
    for zone in enabled_zones:
        if zone.category == "dlc" and zone.dlc_pack in optional_dlc_packs:
            for loc in region_by_name[zone.internal_name].locations:
                loc.progress_type = LocationProgressType.EXCLUDED

    # Win condition: Emerald Goal and/or GoalMission (both when both are active).
    # generate_early guarantees at least one is active, so victory_event_names is never empty.
    # Locked events, not a can_reach rule — see _place_victory_event.   
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
