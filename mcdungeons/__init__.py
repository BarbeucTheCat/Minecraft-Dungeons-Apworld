"""
Options.py    - all yaml options (DLC packs, EmeraldGoal/Mode, GoalMission, boss/mini-boss checks, BonusChestCount, DeathLink)
ZoneData.py   - mission unlock map (requires, secret exits)
Items.py      - Access items, Secret Access items, fillers
Locations.py  - Mission Complete per zone, emerald milestones, boss first-kills, confirmed chests + bonus headroom
Regions.py    - region graph, skips disabled DLC packs
Rules.py      - logic rules

Client-side (client/): dungeons_reader.py detects milestones/boss kills/chests live in-game;
give_item.py + apply_item_reward.py grant equipment rewards.

Zones not in ZONE_CHEST_COUNTS only get a Mission Complete location.
To add one: confirm the chest count is stable across repeat visits (watch_chest_events), then add it.   
"""

from typing import Dict, List

from BaseClasses import Item, ItemClassification, Tutorial
from Options import OptionError, OptionGroup
from worlds.AutoWorld import World, WebWorld
from worlds.LauncherComponents import Component, Type, components, launch_subprocess

from .Options import (
    MCDungeonsOptions, GoalMission, GOAL_MISSION_ID_TO_ZONE,
    JungleAwakensDLC, CreepingWinterDLC, HowlingPeaksDLC, HiddenDepthsDLC,
    FlamesOfTheNetherDLC, EchoingVoidDLC, DlcMissionsOptional, RequiredDlcs, DlcBossKillChecks, DlcMiniBossKillChecks,
)
from .Items import ITEM_TABLE, MISSION_ACCESS_ITEMS, SECRET_ACCESS_ITEMS, ANCIENT_HUNT_ACCESS_ITEM, FILLER_ITEMS, FILLER_ITEM_EMERALD_AMOUNTS, ITEM_REWARD_CATEGORIES, ITEM_REWARD_FILLERS, LOCATION_REWARD_ITEM_ZONE_CATEGORY, PROGRESSIVE_ENCHANT_SLOT_ITEM, PROGRESSIVE_ENCHANT_SLOT_COPIES, PROGRESSIVE_PICKUP_ITEM, PROGRESSIVE_PICKUP_COPIES, EYE_OF_ENDER_ITEM, EYE_OF_ENDER_COPIES, MCDungeonsItem, zone_access_item_name
from .ZoneData import ZONES, ANCIENT_HUNT_ZONES, ZONES_BY_NAME, full_predecessor_chain
from .Locations import (
    LOCATION_TABLE, SECRET_PARENT_TO_ZONE, BOSS_KILL_TABLE, BOSS_ZONE,
    get_base_boss_names, get_dlc_boss_names,
    ANCIENT_HUNT_BOSS_KILL_TABLE,
    wanted_mini_boss_zones, wanted_mini_boss_type_homes, EYE_OF_ENDER_TYPES, VILLAGER_TYPES,
    ZONE_CHEST_COUNTS, get_bonus_chest_locations,
)
from .Regions import create_regions

def launch_client(*args: str) -> None:
    """Runs dungeons_ap_client.py's launch() in a subprocess; forwards Launcher args through.
    Wrapped defensively with a crash log: this runs in the Launcher's own process, before the client's
    own error handling — check the log first if the client won't open."""   
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

class MCDungeonsWeb(WebWorld):
    tutorials = [
        Tutorial(
            "Multiworld Setup Guide",
            "A guide to setting up Minecraft Dungeons for MultiWorld play.",
            "English",
            "setup_en.md",
            "setup/en",
            ["FishTheCook"]
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
            RequiredDlcs,
            DlcBossKillChecks,
            DlcMiniBossKillChecks,
        ]),
    ]


class MCDungeonsWorld(World):
    """Minecraft Dungeons — clear missions across the Mainland (plus six optional DLC packs) to complete your seed."""

    game = "Minecraft Dungeons"
    web = MCDungeonsWeb()
    options_dataclass = MCDungeonsOptions
    options: MCDungeonsOptions

    item_name_to_id = {name: info.code for name, info in ITEM_TABLE.items()}
    location_name_to_id = dict(LOCATION_TABLE)

    location_class = None  # set to your Location subclass if you add one; None uses the base class

    def generate_early(self) -> None:
        # Squid Coast is the root of the mission tree (every other zone needs it). Its Access item is given
        # to everyone at start (not placed in the pool) to avoid a soft-lock — matches the base game.
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

            # The goal zone's access rule is already "early," but that only controls logic, not where fill places the Access items.
            # Pushing every Access item on the full predecessor chain into early_items makes fill weight them toward early spheres.   
            enabled_names = {
                z.internal_name for z in ZONES
                if z.category == "base" or bool(getattr(self.options, z.dlc_pack))
            }
            chain = full_predecessor_chain(zone_name, enabled_names) + [zone_name]
            require_secret_option = bool(self.options.secret_missions_require_secret)
            for ancestor_name in chain:
                ancestor = ZONES_BY_NAME[ancestor_name]
                # ONE item per zone (a secret zone's is its Secret Access, see Items.zone_access_item_name)
                access_item = zone_access_item_name(ancestor, require_secret_option)
                self.multiworld.early_items[self.player][access_item] = 1

        if not self.options.emerald_mode.goal_enabled and goal_mission_id == GoalMission.option_none:
            raise OptionError(
                f"{self.player_name}: no win condition is set - Emerald Mode is 'none' "
                f"or 'checks' (doesn't include the goal) and Goal Mission is 'None'. Set "
                f"Emerald Mode to 'goal' or 'both', and/or pick a Goal Mission."
            )

        if self.options.emerald_mode.goal_enabled and self.options.emerald_goal.value < 500:
            raise OptionError(
                f"{self.player_name}: Emerald Mode includes the goal but Emerald "
                f"Goal is 0 - either raise Emerald Goal to at least 500, or set "
                f"Emerald Mode to 'none'/'checks' (and make sure Goal Mission is set "
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
            # Squid Coast Access is given at start (generate_early); adding it here too would create a duplicate
            # (more items than locations to hold them).
            # One access item per zone: Secret Access for secret zones with the option on, plain Access otherwise.   
            if zone.internal_name != "squidcoast":
                pool.append(self.create_item(
                    zone_access_item_name(zone, bool(self.options.secret_missions_require_secret))))

        if self.options.ancient_hunts:
            pool.append(self.create_item(ANCIENT_HUNT_ACCESS_ITEM))

        # Three copies; each one the player finds raises the enchant slot cap on granted items by one.
        for _ in range(PROGRESSIVE_ENCHANT_SLOT_COPIES):
            pool.append(self.create_item(PROGRESSIVE_ENCHANT_SLOT_ITEM))

        # Six copies (one per Eye type), only in the pool when the option is on. The Stronghold needs all six.
        # One location per Eye type in Regions.py.   
        if self.options.eye_of_ender_checks:
            for _ in range(EYE_OF_ENDER_COPIES):
                pool.append(self.create_item(EYE_OF_ENDER_ITEM))

        # One per camp merchant per captured villager, only for enabled zones (locations in Regions.py; counts must agree).   
        if self.options.villager_checks:
            enabled_zone_names_for_villagers = {z.internal_name for z in enabled_zones}
            if self.options.ancient_hunts:      # the Piglin Merchant check lives in any Ancient Hunt
                enabled_zone_names_for_villagers |= {z.internal_name for z in ANCIENT_HUNT_ZONES}
            for _key, _fragment, villager_display, villager_zone in VILLAGER_TYPES:
                if villager_zone in enabled_zone_names_for_villagers:
                    pool.append(self.create_item(villager_display))

        # Only in the pool when the option is on.   
        if self.options.progressive_pickups:
            for _ in range(PROGRESSIVE_PICKUP_COPIES):
                pool.append(self.create_item(PROGRESSIVE_PICKUP_ITEM))

        # Fills remaining slots so pool size matches location count.
        # Weighted by kind (emeralds vs item-type), then by equipment category within item-type — not a flat pick over all names,
        # which would skew toward whichever kind has the most entries.   
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

        mini_boss_kill_location_count = 0
        if self.options.mini_boss_kill_checks.value or self.options.dlc_mini_boss_kill_checks.value:
            enabled_zone_names_for_mini_bosses = {z.internal_name for z in enabled_zones}
            mb_base, mb_dlc = self.options.mini_boss_kill_checks.value, self.options.dlc_mini_boss_kill_checks.value
            mini_boss_kill_location_count = (
                len(wanted_mini_boss_zones(mb_base, mb_dlc) & enabled_zone_names_for_mini_bosses)   # per_zone checks
                + len(wanted_mini_boss_type_homes(mb_base, mb_dlc, enabled_zone_names_for_mini_bosses))   # per_type checks
            )

        eye_of_ender_location_count = 0
        if self.options.eye_of_ender_checks:
            enabled_zone_names_for_eyes = {z.internal_name for z in enabled_zones}
            eye_of_ender_location_count = sum(1 for _t, _m, zone_name in EYE_OF_ENDER_TYPES
                                              if zone_name in enabled_zone_names_for_eyes)

        villager_location_count = 0
        if self.options.villager_checks:
            enabled_zone_names_for_villager_checks = {z.internal_name for z in enabled_zones}
            if self.options.ancient_hunts:
                enabled_zone_names_for_villager_checks |= {z.internal_name for z in ANCIENT_HUNT_ZONES}
            villager_location_count = sum(1 for _k, _f, _d, zone_name in VILLAGER_TYPES
                                          if zone_name in enabled_zone_names_for_villager_checks)

        ancient_hunt_boss_kill_count = (
            len(ANCIENT_HUNT_ZONES)
            if self.options.ancient_hunts and self.options.ancient_hunt_boss_checks
            else 0
        )

        # Per-zone chest locations from ZONE_CHEST_COUNTS; only for confirmed-fixed, enabled zones (matches Regions.py).   
        enabled_zone_names_for_chests = {z.internal_name for z in enabled_zones}
        zone_chest_location_count = 0
        for zone_name, (chest_n, supply_n) in ZONE_CHEST_COUNTS.items():
            if zone_name not in enabled_zone_names_for_chests:
                continue
            zone_chest_location_count += chest_n + supply_n

        # Global bonus chests (not per-zone), trimmed to BonusChestCount, always attached to Menu — no DLC filtering.   
        bonus_chest_location_count = len(get_bonus_chest_locations(self.options.bonus_chest_count.value))

        # Victory event locations already hold a locked item, so they're subtracted from the filler count
        # (they were included in the location total above).   
        victory_event_location_count = 0
        if self.options.emerald_mode.goal_enabled and goal >= 500:
            victory_event_location_count += 1
        if self.options.goal_mission.value != GoalMission.option_none:
            victory_event_location_count += 1

        # "Secret Zone Found" locations (Regions.py): one per secret zone whose
        # parent AND itself are enabled, only with Secret Mission Access on.
        secret_zone_found_location_count = 0
        if self.options.secret_missions_require_secret:
            for parent_name, secret_name in SECRET_PARENT_TO_ZONE.items():
                if parent_name in enabled_zone_names_for_chests and secret_name in enabled_zone_names_for_chests:
                    secret_zone_found_location_count += 1

        location_count = (
            len(enabled_zones)
            + secret_zone_found_location_count
            + (len(ANCIENT_HUNT_ZONES) if self.options.ancient_hunts else 0)
            + ancient_hunt_boss_kill_count
            + emerald_milestone_count
            + boss_kill_location_count
            + mini_boss_kill_location_count
            + eye_of_ender_location_count
            + villager_location_count
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

    @classmethod
    def _apworld_version(cls) -> str:
        """Version from archipelago.json; read defensively (World.world_version/manifest only exist in AP 0.6.8+).   """
        version = getattr(cls, "world_version", None)
        if version is not None and tuple(version) != (0, 0, 0):
            return ".".join(str(part) for part in version)
        manifest = getattr(cls, "manifest", None) or {}
        return str(manifest.get("world_version", "unknown"))

    def fill_slot_data(self) -> Dict[str, object]:
        return {
            # Lets the client warn when the seed was generated with a different apworld version.
            "world_version": self._apworld_version(),
            "game_store": self.options.game_store.store_key,
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
            "mini_boss_kill_checks": self.options.mini_boss_kill_checks.value,          # 0 none, 1 per_type, 2 per_zone, 3 both
            "eye_of_ender_checks": bool(self.options.eye_of_ender_checks),
            "villager_checks": bool(self.options.villager_checks),
            "dlc_mini_boss_kill_checks": self.options.dlc_mini_boss_kill_checks.value,  # same values, DLC zones
            "progressive_pickups": bool(self.options.progressive_pickups),
            "bonus_chest_count": self.options.bonus_chest_count.value,
            "death_link": bool(self.options.death_link),
            # DLC packs whose checks never hold progression items
            "optional_dlc_packs": list(getattr(self, "optional_dlc_packs", [])),
        }
