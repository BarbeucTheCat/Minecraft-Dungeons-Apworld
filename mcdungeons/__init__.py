"""
Minecraft Dungeons - Archipelago world.

How the files fit together:
    Options.py    - every yaml option: one DLC-pack Toggle each (default off),
                    EmeraldGoal + EmeraldMode (checks vs. goal, or both),
                    GoalMission (a dynamic Choice covering every base+DLC
                    mission - validated against which DLC packs are actually
                    enabled in generate_early below), BossKillChecks /
                    DlcBossKillChecks, BonusChestCount, and DeathLink
    ZoneData.py   - the mission map itself: which mission unlocks which,
                    and which ones are hidden behind a secret exit
    Items.py      - every item: each zone's Access item, Secret Access
                    items, and all the filler
    Locations.py  - every location: one "Mission Complete" per zone;
                    an emerald milestone every 500 up to EmeraldGoal's
                    50000 ceiling (the table itself goes up to 500000 -
                    10x headroom, so raising that ceiling later never
                    needs new IDs); one "First Kill" per boss (opt-in);
                    and, for zones where the real chest count has been
                    confirmed by watching the game live (not assumed),
                    a location per chest plus "Bonus Chest" headroom for
                    any extras BonusChestCount allows for
    Regions.py    - connects all of that into the actual region graph,
                    skipping anything from a DLC pack you didn't enable
    Rules.py      - the logic rules (what you need before you can reach
                    a given mission)

Emerald milestones, boss kills, and chest opens are all detected live
while you play, by dungeons_reader.py - that lives in client/, outside
this folder, since it talks to the running game rather than doing
anything at generation time. Equipment/location-specific item rewards
work the same way, via give_item.py and apply_item_reward.py alongside
it.

If a zone isn't in Locations.py's ZONE_CHEST_COUNTS yet, that just
means its chest layout hasn't been confirmed fixed - it only gets the
one placeholder "Mission Complete" location for now. To add a new one,
open every chest in that zone with dungeons_reader.py's
watch_chest_events running, and confirm the count stays the same
across repeat visits, before adding it here.
"""

from typing import Dict, List

from BaseClasses import Item, ItemClassification, Tutorial
from Options import OptionError, OptionGroup
from worlds.AutoWorld import World, WebWorld
from worlds.LauncherComponents import Component, Type, components, launch_subprocess

from .Options import (
    MCDungeonsOptions, GoalMission, GOAL_MISSION_ID_TO_ZONE,
    JungleAwakensDLC, CreepingWinterDLC, HowlingPeaksDLC, HiddenDepthsDLC,
    FlamesOfTheNetherDLC, EchoingVoidDLC, DlcMissionsOptional, DlcBossKillChecks,
)
from .Items import ITEM_TABLE, MISSION_ACCESS_ITEMS, SECRET_ACCESS_ITEMS, ANCIENT_HUNT_ACCESS_ITEM, FILLER_ITEMS, FILLER_ITEM_EMERALD_AMOUNTS, ITEM_REWARD_CATEGORIES, ITEM_REWARD_FILLERS, LOCATION_REWARD_ITEM_ZONE_CATEGORY, PROGRESSIVE_ENCHANT_SLOT_ITEM, PROGRESSIVE_ENCHANT_SLOT_COPIES, PROGRESSIVE_PICKUP_ITEM, PROGRESSIVE_PICKUP_COPIES, MCDungeonsItem
from .ZoneData import ZONES


def launch_client(*args: str) -> None:
    """Registered as this world's Launcher Component below - runs when
    the user clicks "Minecraft Dungeons Client" in the Archipelago
    Launcher. launch_subprocess runs dungeons_ap_client.py's launch()
    in its own process (same mechanism every other Launcher-integrated
    client uses), forwarding any args the Launcher itself received (e.g.
    from a webhost "Connect" link) straight through to it.

    Everything in this function runs in the LAUNCHER'S OWN process, not
    a subprocess - dungeons_ap_client.py's own try/except (which holds a
    window open on crash) can't help with anything that goes wrong here,
    since that code hasn't been reached yet. A GUI button-click handler
    swallowing an exception silently is exactly "nothing visibly
    happens" from the user's side, so this is wrapped defensively and
    written to a crash log file that will exist even if the Launcher
    itself shows nothing - check it first if the client won't open."""
    try:
        from .client.dungeons_ap_client import launch
        launch_subprocess(launch, name="Minecraft Dungeons Client", args=args)
    except Exception:
        import traceback
        from pathlib import Path
        crash_log = Path(__file__).resolve().parent / "client" / "launch_error.log"
        try:
            crash_log.write_text(traceback.format_exc())
        except OSError:
            pass
        raise


components.append(
    Component(
        "Minecraft Dungeons Client",
        func=launch_client,
        component_type=Type.CLIENT,
    )
)
from .Locations import (
    LOCATION_TABLE, BOSS_KILL_TABLE, BOSS_ZONE,
    get_base_boss_names, get_dlc_boss_names,
    ANCIENT_HUNT_BOSS_KILL_TABLE,
    ZONE_CHEST_COUNTS, get_bonus_chest_locations,
)
from .ZoneData import ZONES, ANCIENT_HUNT_ZONES, ZONES_BY_NAME, full_predecessor_chain
from .Regions import create_regions


class MCDungeonsWeb(WebWorld):
    tutorials = [
        Tutorial(
            "Multiworld Setup Guide",
            "A guide to setting up Minecraft Dungeons for MultiWorld play.",
            "English",
            "setup_en.md",
            "setup/en",
            ["FishTheCook"]  # matches archipelago.json's authors field -
                              # confirm this is the intended credit name/handle
        )
    ]
    theme = "stone"

    option_groups = [
        OptionGroup("DLC", [
            JungleAwakensDLC,
            CreepingWinterDLC,
            HowlingPeaksDLC,
            HiddenDepthsDLC,
            FlamesOfTheNetherDLC,
            EchoingVoidDLC,
            DlcMissionsOptional,
            DlcBossKillChecks,
        ]),
    ]


class MCDungeonsWorld(World):
    """Minecraft Dungeons - defeat missions across the Mainland, with six
    optional DLC packs, to reach the end of your seed."""

    game = "Minecraft Dungeons"
    web = MCDungeonsWeb()
    options_dataclass = MCDungeonsOptions
    options: MCDungeonsOptions

    item_name_to_id = {name: info.code for name, info in ITEM_TABLE.items()}
    location_name_to_id = dict(LOCATION_TABLE)

    location_class = None  # set to your Location subclass if you add one; None uses the base class

    def generate_early(self) -> None:
        # Squid Coast unlocks everything else - every other mission
        # ultimately needs it. Its own Access item is a normal
        # randomized item like any other zone's, though, and nothing
        # else guarantees you'll receive it early. Without this, a
        # player could get stuck unable to start the game at all until
        # the multiworld happens to send it their way. Giving it to
        # everyone right from the start avoids that - matching the
        # base game, where Squid Coast was never locked behind
        # anything.
        self.multiworld.push_precollected(self.create_item("Squid Coast Access"))

        goal_mission_id = self.options.goal_mission.value

        if goal_mission_id != GoalMission.option_none:
            zone_name = GOAL_MISSION_ID_TO_ZONE[goal_mission_id]
            zone = ZONES_BY_NAME[zone_name]
            if zone.category == "dlc" and not bool(getattr(self.options, zone.dlc_pack)):
                raise OptionError(
                    f"{self.player_name}: Goal Mission is set to '{zone.display_name}', "
                    f"which belongs to the '{zone.dlc_pack}' DLC pack - that pack is "
                    f"disabled, so this mission can never be reached. Enable that DLC "
                    f"pack, or choose a different Goal Mission."
                )

            # The access rule for reaching your chosen goal zone is
            # already "early" (see Regions.py/Rules.py - it only checks
            # the zone's own Access item plus its skip-level
            # predecessor's), but that's a statement about LOGIC, not
            # about where the fill algorithm actually puts those Access
            # items. Left alone, "Soggy Swamp Access" is just another
            # progression item and can land anywhere reachable - including
            # deep in a late zone's chest, forcing you through most of
            # the mission tree to pick it up even though the rule itself
            # never asked for that. Pushing every Access item (and,
            # where SecretMissionsAccess is on, every Secret Access item)
            # on the FULL predecessor chain into early_items fixes this:
            # AP's fill weights early_items entries toward early spheres,
            # so your goal's whole critical path - not just its logic
            # rule - actually resolves early too.
            enabled_names = {
                z.internal_name for z in ZONES
                if z.category == "base" or bool(getattr(self.options, z.dlc_pack))
            }
            chain = full_predecessor_chain(zone_name, enabled_names) + [zone_name]
            require_secret_option = bool(self.options.secret_missions_require_secret)
            for ancestor_name in chain:
                ancestor = ZONES_BY_NAME[ancestor_name]
                access_item = f"{ancestor.display_name} Access"
                self.multiworld.early_items[self.player][access_item] = 1
                if ancestor.secret and require_secret_option:
                    secret_item = f"{ancestor.display_name} Secret Access"
                    self.multiworld.early_items[self.player][secret_item] = 1

        if not self.options.emerald_mode.goal_enabled and goal_mission_id == GoalMission.option_none:
            raise OptionError(
                f"{self.player_name}: no win condition is set - Emerald Mode is 'neither' "
                f"or 'checks' (doesn't include the goal) and Goal Mission is 'None'. Set "
                f"Emerald Mode to 'goal' or 'both', and/or pick a Goal Mission."
            )

        if self.options.emerald_mode.goal_enabled and self.options.emerald_goal.value < 500:
            raise OptionError(
                f"{self.player_name}: Emerald Mode includes the goal but Emerald "
                f"Goal is 0 - either raise Emerald Goal to at least 500, or set "
                f"Emerald Mode to 'neither'/'checks' (and make sure Goal Mission is set "
                f"instead)."
            )

    def create_regions(self) -> None:
        create_regions(self)

    def create_item(self, name: str) -> Item:
        info = ITEM_TABLE[name]
        return MCDungeonsItem(name, info.classification, info.code, self.player)

    def create_items(self) -> None:
        enabled_dlc_flags = {
            "jungle_awakens_dlc": bool(self.options.jungle_awakens_dlc),
            "creeping_winter_dlc": bool(self.options.creeping_winter_dlc),
            "howling_peaks_dlc": bool(self.options.howling_peaks_dlc),
            "hidden_depths_dlc": bool(self.options.hidden_depths_dlc),
            "flames_of_the_nether_dlc": bool(self.options.flames_of_the_nether_dlc),
            "echoing_void_dlc": bool(self.options.echoing_void_dlc),
        }

        def zone_enabled(zone) -> bool:
            if zone.category == "base":
                return True
            return enabled_dlc_flags.get(zone.dlc_pack, False)

        enabled_zones = [z for z in ZONES if zone_enabled(z)]

        pool: List[Item] = []

        for zone in enabled_zones:
            # Squid Coast Access is already given to everyone at the
            # start (see generate_early) - not added here too, or
            # there'd be two copies: one you already have, and one
            # placed somewhere random. That would put one more item in
            # the pool than there are locations to hold it.
            if zone.internal_name != "squidcoast":
                pool.append(self.create_item(f"{zone.display_name} Access"))
            if zone.secret and bool(self.options.secret_missions_require_secret):
                pool.append(self.create_item(f"{zone.display_name} Secret Access"))

        if self.options.ancient_hunts:
            pool.append(self.create_item(ANCIENT_HUNT_ACCESS_ITEM))

        # Progressive enchant slot capacity - 3 copies, each one raising
        # the player's granted-item enchant slot cap by one (0 received =
        # 0 slots, all 3 received = full 3 slots). See Items.py's comment
        # for the full design; the client (apply_item_reward.py /
        # watch_item_rewards) counts received copies and passes that as
        # num_slots on every give_random_item/give_location_reward call.
        for _ in range(PROGRESSIVE_ENCHANT_SLOT_COPIES):
            pool.append(self.create_item(PROGRESSIVE_ENCHANT_SLOT_ITEM))

        # Progressive pickup unlock - only if the option is actually on;
        # previously this option existed but placed no item at all, so
        # there was nothing to hint and no way to ever raise the tier
        # once enabled. See Items.py's comment for the full design.
        if self.options.progressive_pickups:
            for _ in range(PROGRESSIVE_PICKUP_COPIES):
                pool.append(self.create_item(PROGRESSIVE_PICKUP_ITEM))

        # Fill remaining slots with filler items so the pool matches the
        # location count: one location per created zone/hunt, plus one per
        # emerald milestone (500 up to this player's emerald_goal), plus
        # one per boss first-kill (always added - not DLC-gated, see
        # Locations.py's BOSS_KILL_TABLE comment).
        #
        # Filler composition is weighted, NOT a flat random.choice over
        # every filler name - a flat choice would badly skew toward
        # whichever "kind" happens to have the most distinct named
        # entries (there are 37 location-specific reward names vs only 3
        # emerald tiers, so a flat pick would make emeralds ~7% of filler
        # and location rewards ~82%, purely from how many entries each
        # kind happens to have - not an intentional design choice).
        #
        # Instead: emeralds are picked as a whole "kind" with priority
        # weight over item-type rewards as a whole. Within item-type
        # rewards, every EQUIPMENT CATEGORY (Melee/Ranged/Armor/Artifact/
        # Any) gets equal odds first, and only THEN does it pick uniformly
        # among that category's available names (its one generic "Random
        # X" filler plus however many location-specific variants exist for
        # it) - so having many Melee location-rewards doesn't make melee
        # overall more common than armor, it just adds variety within melee.
        # See Regions.py's emerald block: "goal"-only mode still adds
        # exactly one location (the top milestone), not the full range.
        goal = self.options.emerald_goal.value
        emerald_milestone_count = 0
        if goal >= 500:
            if self.options.emerald_mode.checks_enabled:
                emerald_milestone_count = goal // 500
            elif self.options.emerald_mode.goal_enabled:
                emerald_milestone_count = 1

        boss_kill_location_count = 0
        if self.options.boss_kill_checks or self.options.dlc_boss_kill_checks:
            enabled_zone_names = {z.internal_name for z in enabled_zones}
            wanted_bosses = set()
            if self.options.boss_kill_checks:
                wanted_bosses.update(get_base_boss_names())
            if self.options.dlc_boss_kill_checks:
                wanted_bosses.update(get_dlc_boss_names())
            for boss_name in wanted_bosses:
                zone_internal_name = BOSS_ZONE[boss_name]
                if zone_internal_name in enabled_zone_names:
                    boss_kill_location_count += 1

        ancient_hunt_boss_kill_count = (
            len(ANCIENT_HUNT_ZONES)
            if self.options.ancient_hunts and self.options.ancient_hunt_boss_checks
            else 0
        )

        # Per-zone chest/supply-chest locations (ZONE_CHEST_COUNTS) - only
        # for zones that are both confirmed-fixed AND enabled this
        # generation (DLC pack on), matching exactly what Regions.py adds.
        enabled_zone_names_for_chests = {z.internal_name for z in enabled_zones}
        zone_chest_location_count = 0
        for zone_name, (chest_n, supply_n) in ZONE_CHEST_COUNTS.items():
            if zone_name not in enabled_zone_names_for_chests:
                continue
            zone_chest_location_count += chest_n + supply_n

        # Global bonus chest locations - NOT per-zone, trimmed to this
        # player's BonusChestCount option (0-100). Always added to Menu
        # regardless of which zones are enabled (see Regions.py), so no
        # DLC-pack filtering here, unlike the per-zone count above.
        bonus_chest_location_count = len(get_bonus_chest_locations(self.options.bonus_chest_count.value))

        # Locations holding a locked "Victory" event item (see Regions.py's
        # _place_victory_event) never receive a pool-filled item - they're
        # already counted once above (as part of emerald_milestone_count /
        # enabled_zones' Mission Complete locations), so subtract them back
        # out here rather than double-handling them.
        victory_event_location_count = 0
        if self.options.emerald_mode.goal_enabled and goal >= 500:
            victory_event_location_count += 1
        if self.options.goal_mission.value != GoalMission.option_none:
            victory_event_location_count += 1

        location_count = (
            len(enabled_zones)
            + (len(ANCIENT_HUNT_ZONES) if self.options.ancient_hunts else 0)
            + ancient_hunt_boss_kill_count
            + emerald_milestone_count
            + boss_kill_location_count
            + zone_chest_location_count
            + bonus_chest_location_count
            - victory_event_location_count
        )

        FILLER_KIND_WEIGHTS = {"emerald": 2, "item": 1}  # emerald has priority

        category_to_names: Dict[str, List[str]] = {"Melee": [], "Ranged": [], "Armor": [], "Artifact": [], "Any": []}
        for name, cat in ITEM_REWARD_CATEGORIES.items():
            category_to_names["Any" if cat is None else cat].append(name)
        for name, (_zone, cat) in LOCATION_REWARD_ITEM_ZONE_CATEGORY.items():
            category_to_names[cat].append(name)

        def pick_filler_name() -> str:
            kind = self.random.choices(
                list(FILLER_KIND_WEIGHTS.keys()), weights=list(FILLER_KIND_WEIGHTS.values()), k=1,
            )[0]
            if kind == "emerald":
                return self.random.choice(list(FILLER_ITEM_EMERALD_AMOUNTS.keys()))
            category = self.random.choice(list(category_to_names.keys()))  # equal odds per category
            return self.random.choice(category_to_names[category])

        while len(pool) < location_count:
            pool.append(self.create_item(pick_filler_name()))

        self.multiworld.itempool += pool

    def fill_slot_data(self) -> Dict[str, object]:
        return {
            "jungle_awakens_dlc": bool(self.options.jungle_awakens_dlc),
            "creeping_winter_dlc": bool(self.options.creeping_winter_dlc),
            "howling_peaks_dlc": bool(self.options.howling_peaks_dlc),
            "hidden_depths_dlc": bool(self.options.hidden_depths_dlc),
            "flames_of_the_nether_dlc": bool(self.options.flames_of_the_nether_dlc),
            "echoing_void_dlc": bool(self.options.echoing_void_dlc),
            "secret_missions_require_secret": bool(self.options.secret_missions_require_secret),
            "ancient_hunts": bool(self.options.ancient_hunts),
            "ancient_hunt_boss_checks": bool(self.options.ancient_hunt_boss_checks),
            "emerald_goal": self.options.emerald_goal.value,
            "emerald_checks": self.options.emerald_mode.checks_enabled,
            "emerald_is_goal": self.options.emerald_mode.goal_enabled,
            "goal_mission": (
                GOAL_MISSION_ID_TO_ZONE[self.options.goal_mission.value]
                if self.options.goal_mission.value != GoalMission.option_none
                else None
            ),
            "boss_kill_checks": bool(self.options.boss_kill_checks),
            "dlc_boss_kill_checks": bool(self.options.dlc_boss_kill_checks),
            "progressive_pickups": bool(self.options.progressive_pickups),
            "bonus_chest_count": self.options.bonus_chest_count.value,
            "death_link": bool(self.options.death_link),
        }
