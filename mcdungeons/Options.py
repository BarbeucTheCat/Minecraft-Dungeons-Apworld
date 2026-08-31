import re
from dataclasses import dataclass
from typing import Dict

from Options import Toggle, Range, Choice, DeathLink, PerGameCommonOptions

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
    """Make DLC mission optionnal so if you're goal is in the mainland you don't have to do DLC mission to completed it"""
    display_name = "DLC Missions Optional"
    default = Toggle.option_true


class SecretMissionsAccess(Toggle):
    """If enabled, you need to unlock the secret mission before accessing it (Creepy Crypt, Arch Haven,
    Soggy Cave, Lower Temple, Underhalls, Panda Plateau, Lost Settlement,
    Colossal Rampart, Radiant Ravine, Crimson Forest, Soul Sand Valley,
    Nether Fortress)"""
    display_name = "Secret Mission Acess"


class AncientHuntsEnabled(Toggle):
    """[NOT TESTED] Include the three Ancient Hunt missions (Woodland Mansion, Woodland
    Prison, Spider Cave) that you need to beat and find an item to unlock them"""
    display_name = "Ancient Hunts"


class AncientHuntBossChecks(Toggle):
    """If enabled, make boss in Ancient Hunts Check (Woodland Mansion, Woodland Prison, Spider Cave)."""
    display_name = "Ancient Hunt Boss Checks"


class BossKillChecks(Toggle):
    """If enabled, Make Boss Check if they are not in a DLC and Ancient Hunts."""
    display_name = "Boss Kill Checks"


class DlcBossKillChecks(Toggle):
    """If enabled, Make DLC boss check"""
    display_name = "DLC Boss Kill Checks"


class ProgressivePickups(Toggle):
    """Make pick up item progressive (tier 0 can pick up arrow and tools (weapon, armor,...))
    (tier 1 can pick up health item (bread, apple, ...))
    (tier 2 can pick up potion)
    (tier 3 and final tier can pick up TNT)
    WARNING this option was not tested in dlc mission you may be able to pickup item not intended to be"""
    display_name = "Progressive Pickups"


class BonusChestCount(Range):
    """ [recommended] chest have set amount per zone you need to open
    If you find more chest than the set amount you can get non progressive item as a bonus"""
    display_name = "Bonus Chest Count"
    range_start = 0
    range_end = 100
    default = 20


class EmeraldGoal(Choice):
    """
    Choose the amount of emerald you need to have to finish you're game if you choose goal
    and every 500 emerald is a check if you have choose check or both
    can be ignore if choose none
    """
    display_name = "Emerald Goal"
    default = 20000

    # option_<amount> = <amount> for every 500-multiple from 500 to
    # 50000, plus option_disabled = 0. Built with a loop rather than
    # typed out by hand - 100 entries is too many to hand-maintain, and
    # this keeps it impossible for the option list and the 500-step
    # milestone logic elsewhere (Locations.py/Regions.py/__init__.py) to
    # quietly drift apart from each other.
    option_disabled = 0
    locals().update({f"option_{amount}": amount for amount in range(500, 50000 + 1, 500)})

    @classmethod
    def from_any(cls, value):
        if isinstance(value, str):
            return super().from_any(value)
        # numeric input (including a raw int typed directly into a YAML,
        # which - unlike picking from the option list - isn't guaranteed
        # to already be a valid 500-multiple): round down to the nearest
        # valid one, same behavior the old Range.from_any had.
        rounded = (int(value) // 500) * 500
        rounded = max(0, min(rounded, 50000))
        return cls(rounded)


class EmeraldMode(Choice):
    """Choose if you need emerald for you're goal, only for check or for both
    but if you don't whant to collact emerald choose "none" """
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
        "Choose a mission you have to beat to finish you're game "
        "Can be combine with emerald goal and can be ignore by choosing none"
    ),
    "option_none": 0,
    "default": 0,
}
_goal_mission_namespace.update({f"option_{_slug(z.internal_name)}": i for i, z in enumerate(ZONES, start=1)})
GoalMission = type("GoalMission", (Choice,), _goal_mission_namespace)


@dataclass
class MCDungeonsOptions(PerGameCommonOptions):
    jungle_awakens_dlc: JungleAwakensDLC
    creeping_winter_dlc: CreepingWinterDLC
    howling_peaks_dlc: HowlingPeaksDLC
    hidden_depths_dlc: HiddenDepthsDLC
    flames_of_the_nether_dlc: FlamesOfTheNetherDLC
    echoing_void_dlc: EchoingVoidDLC
    dlc_missions_optional: DlcMissionsOptional
    secret_missions_require_secret: SecretMissionsAccess
    ancient_hunts: AncientHuntsEnabled
    ancient_hunt_boss_checks: AncientHuntBossChecks
    emerald_goal: EmeraldGoal
    emerald_mode: EmeraldMode
    goal_mission: GoalMission
    boss_kill_checks: BossKillChecks
    dlc_boss_kill_checks: DlcBossKillChecks
    progressive_pickups: ProgressivePickups
    bonus_chest_count: BonusChestCount
    death_link: DeathLink
