from ..Items import zone_access_item_name
from ..ZoneData import ZONES_BY_NAME, skip_level_requires_map
from . import MCDungeonsTestBase

SECRET_ZONE = "soggycave"


def _needed_items(zone: str, secret_option: bool):
    """Item names the generator's rule asks for, rebuilt from ZoneData: ONE item per zone."""
    requires = skip_level_requires_map()[zone]
    return [zone_access_item_name(ZONES_BY_NAME[z], secret_option) for z in [zone, *requires]]


class TestSecretZoneNeedsOnlyItsSecretAccess(MCDungeonsTestBase):
    """With secret_missions_require_secret ON, a secret mission is unlocked by its Secret Access item ALONE:
    its plain Access item is not enough, and is not needed."""
    options = {"secret_missions_require_secret": True}

    def test_plain_access_is_not_enough(self):
        requires = skip_level_requires_map()[SECRET_ZONE]
        names = [zone_access_item_name(ZONES_BY_NAME[z], True) for z in requires]
        names.append(f"{ZONES_BY_NAME[SECRET_ZONE].display_name} Access")
        self.collect_by_name(names)
        self.assertFalse(self.can_reach_region(ZONES_BY_NAME[SECRET_ZONE].display_name))

    def test_secret_access_alone_unlocks_the_zone(self):
        self.collect_by_name(_needed_items(SECRET_ZONE, True))
        self.assertTrue(self.can_reach_region(ZONES_BY_NAME[SECRET_ZONE].display_name))

    def test_secret_zone_has_a_single_access_item_in_the_pool(self):
        names = [item.name for item in self.multiworld.itempool]
        display = ZONES_BY_NAME[SECRET_ZONE].display_name
        self.assertIn(f"{display} Secret Access", names)
        self.assertNotIn(f"{display} Access", names)


class TestSecretOptionOff(MCDungeonsTestBase):
    """With the option OFF there are no Secret Access items: Access items must be enough."""
    options = {"secret_missions_require_secret": False}

    def test_access_items_are_enough(self):
        self.collect_by_name(_needed_items(SECRET_ZONE, False))
        self.assertTrue(self.can_reach_region(ZONES_BY_NAME[SECRET_ZONE].display_name))


class TestSlotDataVersion(MCDungeonsTestBase):
    """slot_data carries the apworld version so the client can warn about a mismatch."""
    options = {}

    def test_world_version_in_slot_data(self):
        version = self.world.fill_slot_data()["world_version"]
        self.assertIsInstance(version, str)
        self.assertNotEqual(version, "")
