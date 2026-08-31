from . import MCDungeonsTestBase


class TestDefaultOptions(MCDungeonsTestBase):
    """Default options for every setting - the combination most players
    will actually generate with."""
    options = {}


class TestAllDlcAndAncientHunts(MCDungeonsTestBase):
    """Every DLC pack plus Ancient Hunts and their boss checks enabled -
    the largest possible region/location graph this world can produce,
    stressing full-state reachability the hardest."""
    options = {
        "jungle_awakens_dlc": True,
        "creeping_winter_dlc": True,
        "howling_peaks_dlc": True,
        "hidden_depths_dlc": True,
        "flames_of_the_nether_dlc": True,
        "echoing_void_dlc": True,
        "dlc_missions_optional": True,
        "ancient_hunts": True,
        "ancient_hunt_boss_checks": True,
        "boss_kill_checks": True,
        "dlc_boss_kill_checks": True,
        "secret_missions_require_secret": True,
        "progressive_pickups": True,
    }


class TestNoDlc(MCDungeonsTestBase):
    """Every DLC pack off - the smallest possible region/location graph,
    to catch anything that implicitly assumes a DLC zone exists."""
    options = {
        "jungle_awakens_dlc": False,
        "creeping_winter_dlc": False,
        "howling_peaks_dlc": False,
        "hidden_depths_dlc": False,
        "flames_of_the_nether_dlc": False,
        "echoing_void_dlc": False,
        "ancient_hunts": False,
    }


class TestEmeraldGoalOnly(MCDungeonsTestBase):
    """Win condition is emeralds alone - Emerald Mode 'goal' (not
    'both'), so emeralds award no location checks, and no Goal Mission is
    set. Exercises Regions.py's completion_condition wiring for the
    emerald-only path."""
    options = {
        "emerald_mode": "goal",
        "emerald_goal": 5000,
        "goal_mission": "none",
    }


class TestGoalMissionOnly(MCDungeonsTestBase):
    """Win condition is a specific mission alone - Emerald Mode 'checks'
    (emeralds award checks but aren't part of the win condition) with a
    real Goal Mission set. Exercises generate_early's early_items
    push for the goal's full predecessor chain."""
    options = {
        "emerald_mode": "checks",
        "goal_mission": "creeperwoods",
    }


class TestGoalMissionPlusEmeraldGoal(MCDungeonsTestBase):
    """Both a Goal Mission AND the emerald goal must be reached - Emerald
    Mode 'both', combined with a Goal Mission on a DLC zone whose pack is
    enabled (the valid case of the DLC-pack cross-check in
    generate_early)."""
    options = {
        "jungle_awakens_dlc": True,
        "emerald_mode": "both",
        "emerald_goal": 2000,
        "goal_mission": "creeperwoods",
    }


class TestBonusChestCountExtremes(MCDungeonsTestBase):
    """Bonus Chest Count at its minimum - makes sure the location table
    doesn't assume a nonzero bonus-chest pool anywhere."""
    options = {
        "bonus_chest_count": 0,
    }
