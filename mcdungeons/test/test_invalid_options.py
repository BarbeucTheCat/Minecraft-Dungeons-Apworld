import unittest

from Options import OptionError
from test.general import setup_solo_multiworld
from .. import MCDungeonsWorld


def _world_with_options(options):
    """A solo MCDungeons world with default options plus the given overrides, with NO generation
    step run yet (so a test can call generate_early itself and inspect what it raises).
    setup_solo_multiworld's second argument is the gen steps (not the options), so the overrides
    are applied to world.options afterwards - this works on every supported Archipelago version."""
    multiworld = setup_solo_multiworld(MCDungeonsWorld, ())
    world = multiworld.worlds[1]
    for name, value in options.items():
        current = getattr(world.options, name)
        setattr(world.options, name, type(current).from_any(value))
    return world


class TestNoWinCondition(unittest.TestCase):
    """generate_early raises OptionError when Emerald Mode doesn't include the goal AND Goal Mission
    is 'none' - there would be nothing for the player to actually win on."""

    def test_neither_goal_nor_mission_raises(self):
        world = _world_with_options({
            "emerald_mode": "checks",
            "goal_mission": "none",
        })
        with self.assertRaises(OptionError):
            world.generate_early()

    def test_goal_zero_with_goal_mode_raises(self):
        world = _world_with_options({
            "emerald_mode": "goal",
            "emerald_goal": "disabled",
            "goal_mission": "none",
        })
        with self.assertRaises(OptionError):
            world.generate_early()

    def test_goal_mission_on_disabled_dlc_pack_raises(self):
        world = _world_with_options({
            "jungle_awakens_dlc": False,
            "goal_mission": "dingyjungle",  # belongs to jungle_awakens_dlc, disabled above
        })
        with self.assertRaises(OptionError):
            world.generate_early()
