import unittest

from ..Items import ITEM_TABLE, VILLAGER_ITEM_NAMES
from ..Locations import LOCATION_TABLE, VILLAGER_TABLE, VILLAGER_TYPES
from ..ZoneData import ZONES_BY_NAME, ANCIENT_HUNT_ZONES


class TestVillagers(unittest.TestCase):
    def test_items_match_the_villager_types(self):
        self.assertEqual([display for _k, _f, display, _z in VILLAGER_TYPES], VILLAGER_ITEM_NAMES)
        for name in VILLAGER_ITEM_NAMES:
            self.assertIn(name, ITEM_TABLE)

    def test_zones_exist_and_ids_are_unique(self):
        for _key, _fragment, _display, zone in VILLAGER_TYPES:
            self.assertTrue(zone in ZONES_BY_NAME or zone in {z.internal_name for z in ANCIENT_HUNT_ZONES})
        self.assertEqual(len(set(VILLAGER_TABLE.values())), len(VILLAGER_TYPES))
        for name, location_id in VILLAGER_TABLE.items():
            self.assertEqual(LOCATION_TABLE[name], location_id)

    def test_name_fragments_do_not_overlap(self):
        # the DLL and the client both match a merchant definition class name by fragment: one fragment must never
        # be contained in another, and they are lowercase (the DLL lowercases the class name before matching)
        fragments = [fragment for _k, fragment, _d, _z in VILLAGER_TYPES]
        for fragment in fragments:
            self.assertEqual(fragment, fragment.lower())
            self.assertEqual(1, sum(1 for other in fragments if fragment in other))
