import re
from dataclasses import dataclass
from typing import Dict

from Options import Toggle, Range, Choice, DeathLink, OptionSet, PerGameCommonOptions

from .ZoneData import ZONES


def _slug(internal_name: str) -> str:
    """Turn a ZoneData internal_name into a valid Options 'option_<name>'
    suffix. ZoneData's internal_name values are already lowercase/no-space
    for every zone, so this is mostly defensive against future additions."""
    return re.sub(r"[^a-z0-9]+", "_", internal_name.lower()).strip("_")


class JungleAwakensDLC(Toggle):
    """Include the Jungle Awakens DLC missions (Dingy Jungle, Overgrown Temple,
    Panda Plateau, Treetop Tangle) in this world."""
    display_name = "Jungle Awakens DLC"


class CreepingWinterDLC(Toggle):
    """Include the Creeping Winter DLC missions (Frosted Fjord, Lone Fortress,
    Lost Settlement) in this world."""
    display_name = "Creeping Winter DLC"


class HowlingPeaksDLC(Toggle):
    """Include the Howling Peaks DLC missions (Windswept Peaks, Gauntlet of
    Gales, Gale Sanctum, Colossal Rampart) in this world."""
    display_name = "Howling Peaks DLC"


class HiddenDepthsDLC(Toggle):
    """Include the Hidden Depths DLC missions (Coral Rise, Abyssal Monument,
    Radiant Ravine) in this world."""
    display_name = "Hidden Depths DLC"


class FlamesOfTheNetherDLC(Toggle):
    """Include the Flames of the Nether DLC missions (Nether Wastes, Warped
    Forest, Crimson Forest, Soul Sand Valley, Basalt Deltas, Nether Fortress)
    in this world."""
    display_name = "Flames of the Nether DLC"


class EchoingVoidDLC(Toggle):
    """Include the Echoing Void DLC missions (End Wilds, Broken Citadel) in
    this world."""
    display_name = "Echoing Void DLC"


class DlcMissionsOptional(Toggle):
    """Make DLC missions optional: if your goal is in the mainland, you don't have to complete any DLC mission to finish the game."""
    display_name = "DLC Missions Optional"
    default = Toggle.option_true


# DLC pack label (what the player writes in the YAML) -> the name of that pack's Toggle option.
DLC_PACK_BY_LABEL: Dict[str, str] = {
    "Jungle Awakens": "jungle_awakens_dlc",
    "Creeping Winter": "creeping_winter_dlc",
    "Howling Peaks": "howling_peaks_dlc",
    "Hidden Depths": "hidden_depths_dlc",
    "Flames of the Nether": "flames_of_the_nether_dlc",
    "Echoing Void": "echoing_void_dlc",
}


class RequiredDlcs(OptionSet):
    """Choose which DLC packs may hold important progression items. A selected DLC and its missions can contain
    items you need to progress, so you must play it. If a DLC is not selected, its checks only ever contain
    non-progression items (it stays playable, just never required).
    Leave empty to let "DLC Missions Optional" decide for all packs at once. A listed pack must also be enabled
    with its own DLC option. If your Goal Mission is in a DLC, that pack is always selected."""
    display_name = "Required DLC Packs"
    valid_keys = frozenset(DLC_PACK_BY_LABEL)


class SecretMissionsAccess(Toggle):
    """If enabled, a secret mission is unlocked by its own single "Secret Access" item (it has no separate "Access"
    item) before you can access it (Creepy Crypt, Arch Haven,
    Soggy Cave, Lower Temple, Underhalls, Panda Plateau, Lost Settlement,
    Colossal Rampart, Radiant Ravine, Crimson Forest, Soul Sand Valley,
    Nether Fortress)"""
    display_name = "Secret Mission Access"


class AncientHuntsEnabled(Toggle):
    """[NOT TESTED] Include the three Ancient Hunt missions (Woodland Mansion, Woodland
    Prison, Spider Cave) that you need to beat and find an item to unlock them"""
    display_name = "Ancient Hunts"


class AncientHuntBossChecks(Toggle):
    """If enabled, the bosses of the Ancient Hunts give checks (Woodland Mansion, Woodland Prison, Spider Cave)."""
    display_name = "Ancient Hunt Boss Checks"


class BossKillChecks(Toggle):
    """If enabled, bosses give checks (except DLC bosses and Ancient Hunt bosses)."""
    display_name = "Boss Kill Checks"


class DlcBossKillChecks(Toggle):
    """If enabled, DLC bosses give checks."""
    display_name = "DLC Boss Kill Checks"


class MiniBossKillChecks(Choice):
    """Checks for beating mini bosses (powerful mobs) in base-game missions.
    none: no mini boss checks.
    per_type: one check per kind of mini boss, the first time you beat one anywhere. A kind cannot be forced to
    appear, so these checks never hold progression items.
    per_zone: one check per mission that has a mini boss. A mini boss is forced to appear in every such mission
    (summoned when you open the mission's last chest check if none was beaten there yet).
    both: per_type and per_zone."""
    display_name = "Mini Boss Kill Checks"
    option_none = 0
    option_per_type = 1
    option_per_zone = 2
    option_both = 3
    alias_false = 0
    alias_off = 0
    alias_true = 2
    default = 0


class DlcMiniBossKillChecks(Choice):
    """Checks for beating mini bosses (powerful mobs) in DLC missions (only the packs that are enabled).
    none: no mini boss checks.
    per_type: one check per kind of mini boss, the first time you beat one anywhere. A kind cannot be forced to
    appear, so these checks never hold progression items.
    per_zone: one check per mission that has a mini boss. A mini boss is forced to appear in every such mission
    (summoned when you open the mission's last chest check if none was beaten there yet).
    both: per_type and per_zone."""
    display_name = "DLC Mini Boss Kill Checks"
    option_none = 0
    option_per_type = 1
    option_per_zone = 2
    option_both = 3
    alias_false = 0
    alias_off = 0
    alias_true = 2
    default = 0


class EyeOfEnderChecks(Toggle):
    """Makes the Eyes of Ender randomized in the multiworld. They become 6 checks (one per zone of appearance)
    that you need to collect, and also 6 items that you need to collect before you can go to the Stronghold."""
    display_name = "Eye of Ender Checks"


class VillagerChecks(Toggle):
    """Makes the captured villagers randomized in the multiworld. Freeing a captured camp merchant (Village Merchant,
    Mystery Merchant, Luxury Merchant, Gift Wrapper, Blacksmith) sends a check instead of unlocking that merchant in
    your camp, and the merchant is an item you receive from the multiworld."""
    display_name = "Villager Checks"


class ProgressivePickups(Toggle):
    """Make item pickups progressive (tier 0 can pick up arrows and tools (weapons, armor, ...))
    (tier 1 can pick up health items (bread, apples, ...))
    (tier 2 can pick up potions)
    (tier 3, the final tier, can pick up TNT)
    WARNING: this option was not tested in DLC missions; you may be able to pick up items that are not intended to be available."""
    display_name = "Progressive Pickups"


class BonusChestCount(Range):
    """[recommended] Each zone has a set number of chests you need to open.
    If you find more chests than that number, you can get non-progression items as a bonus."""
    display_name = "Bonus Chest Count"
    range_start = 0
    range_end = 100
    default = 20


class EmeraldGoal(Choice):
    """
    Choose the amount of emeralds you need to have to finish your game if you choose goal.
    Every 500 emeralds is a check if you chose checks or both.
    Ignored if you choose none.
    """
    display_name = "Emerald Goal"
    default = 10000

    # option_<amount> = <amount> for every 500-multiple from 500 to
    # 50000, plus option_disabled = 0. Built with a loop rather than
    # typed out by hand - 100 entries is too many to hand-maintain, and
    # this keeps it impossible for the option list and the 500-step
    # milestone logic elsewhere (Locations.py/Regions.py/__init__.py) to
    # quietly drift apart from each other.
    option_disabled = 0
    locals().update({f"option_{amount}": amount for amount in range(500, 40000 + 1, 500)})

    @classmethod
    def from_any(cls, value):
        if isinstance(value, str):
            return super().from_any(value)
        # numeric input (including a raw int typed directly into a YAML,
        # which - unlike picking from the option list - isn't guaranteed
        # to already be a valid 500-multiple): round down to the nearest
        # valid one, same behavior the old Range.from_any had.
        rounded = (int(value) // 500) * 500
        rounded = max(0, min(rounded, 40000))
        return cls(rounded)


class EmeraldMode(Choice):
    """Choose whether emeralds are needed for your goal, only for checks, or for both.
    If you don't want to collect emeralds, choose "none"."""
    display_name = "Emerald Mode"
    option_none = 0
    option_checks = 1
    option_goal = 2
    option_both = 3
    default = option_both

    @property
    def checks_enabled(self) -> bool:
        return self.value in (self.option_checks, self.option_both)

    @property
    def goal_enabled(self) -> bool:
        return self.value in (self.option_goal, self.option_both)


class GameStore(Choice):
    """Where the game came from"""
    display_name = "Game Store"
    option_steam = 0
    option_minecraft_launcher = 1
    option_microsoft_store = 2
    default = option_steam

    @property
    def store_key(self) -> str:
        """Matches dungeons_reader.STORE_OFFSETS keys."""
        return {0: "steam", 1: "minecraft_launcher", 2: "microsoft_store"}[self.value]


# value -> zone internal_name, in the same order the option_<slug>
# attributes below get generated. generate_early's DLC validation and
# Regions.py's completion-condition wiring both need this reverse lookup.
GOAL_MISSION_ID_TO_ZONE: Dict[int, str] = {i: z.internal_name for i, z in enumerate(ZONES, start=1)}

# GoalMission's valid choices depend on the full static zone list (base +
# every DLC pack's missions), not on any one player's enabled DLC packs -
# same reasoning as EMERALD_MILESTONE_TABLE in Locations.py: the option's
# name->id table is fixed at class-definition time, before per-slot
# options exist. Whether a DLC mission's pack is actually enabled for a
# given player is validated separately, in generate_early (__init__.py).
_goal_mission_namespace = {
    "display_name": "Goal Mission",
    "__doc__": (
        "Choose a mission you have to beat to finish your game. "
        "It can be combined with the emerald goal, and is ignored if you choose none."
    ),
    "option_none": 0,
    "default": 0,
}
_goal_mission_namespace.update({f"option_{_slug(z.internal_name)}": i for i, z in enumerate(ZONES, start=1)})
GoalMission = type("GoalMission", (Choice,), _goal_mission_namespace)


@dataclass
class MCDungeonsOptions(PerGameCommonOptions):
    game_store: GameStore
    jungle_awakens_dlc: JungleAwakensDLC
    creeping_winter_dlc: CreepingWinterDLC
    howling_peaks_dlc: HowlingPeaksDLC
    hidden_depths_dlc: HiddenDepthsDLC
    flames_of_the_nether_dlc: FlamesOfTheNetherDLC
    echoing_void_dlc: EchoingVoidDLC
    dlc_missions_optional: DlcMissionsOptional
    required_dlcs: RequiredDlcs
    secret_missions_require_secret: SecretMissionsAccess
    ancient_hunts: AncientHuntsEnabled
    ancient_hunt_boss_checks: AncientHuntBossChecks
    emerald_goal: EmeraldGoal
    emerald_mode: EmeraldMode
    goal_mission: GoalMission
    boss_kill_checks: BossKillChecks
    dlc_boss_kill_checks: DlcBossKillChecks
    mini_boss_kill_checks: MiniBossKillChecks
    eye_of_ender_checks: EyeOfEnderChecks
    villager_checks: VillagerChecks
    dlc_mini_boss_kill_checks: DlcMiniBossKillChecks
    progressive_pickups: ProgressivePickups
    bonus_chest_count: BonusChestCount
    death_link: DeathLink
