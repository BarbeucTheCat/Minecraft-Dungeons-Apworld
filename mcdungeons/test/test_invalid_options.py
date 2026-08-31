import unittest

from Options import OptionError
from test.bases import setup_solo_multiworld
from .. import MCDungeonsWorld


class TestNoWinCondition(unittest.TestCase):
    """generate_early raises OptionError when Emerald Mode doesn't
    include the goal AND Goal Mission is 'none' - there would be nothing
    for the player to actually win on. NOTE: setup_solo_multiworld's
    import path/signature above was written from documentation and
    reasoning about this codebase, not run against the real Archipelago
    test framework (not available in this environment) - if `pytest`
    reports an import error here, check test.bases for the current
    helper name/signature and adjust the import and call accordingly;
    the assertion itself (that OptionError is raised) is what matters."""

    def test_neither_goal_nor_mission_raises(self):
        with self.assertRaises(OptionError):
            setup_solo_multiworld(MCDungeonsWorld, {
                "emerald_mode": "checks",
                "goal_mission": "none",
            })

    def test_goal_zero_with_goal_mode_raises(self):
        with self.assertRaises(OptionError):
            setup_solo_multiworld(MCDungeonsWorld, {
                "emerald_mode": "goal",
                "emerald_goal": "disabled",
                "goal_mission": "none",
            })

    def test_goal_mission_on_disabled_dlc_pack_raises(self):
        with self.assertRaises(OptionError):
            setup_solo_multiworld(MCDungeonsWorld, {
                "jungle_awakens_dlc": False,
                "goal_mission": "dingyjungle",  # belongs to jungle_awakens_dlc, disabled above
            })
