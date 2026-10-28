import hashlib
import json
import unittest

from ..Locations import (
    BASE_ZONE_CHEST_COUNTS, DLC_ZONE_CHEST_BASE_ID, LATE_BASE_ZONE_CHEST_COUNTS, DLC_ZONE_CHEST_COUNTS, DLC_ZONE_CHEST_SLOTS_PER_ZONE,
    LOCATION_TABLE, ZONE_CHEST_COUNTS, ZONE_CHEST_TABLE, zone_chest_location_name,
    zone_supply_chest_location_name,
)
from ..ZoneData import ZONES_BY_NAME

# sha1 of the sorted (name, id) pairs of every base-game zone chest, recorded before DLC chest
# support was added. If this changes, base-game location IDs shifted and seeds generated earlier
# no longer match the client's IDs - see the comment above DLC_ZONE_CHEST_COUNTS in Locations.py.
BASE_CHEST_IDS_SHA1 = "e3387e207c83a4c71a38e1977196cb073bc3bd48"


def _zone_ids(zone):
    chests, supply = ZONE_CHEST_COUNTS[zone]
    names = [zone_chest_location_name(zone, n) for n in range(1, chests + 1)]
    names += [zone_supply_chest_location_name(zone, n) for n in range(1, supply + 1)]
    return [ZONE_CHEST_TABLE[name] for name in names]


class TestChestIds(unittest.TestCase):
    def test_base_game_chest_ids_never_change(self):
        base = {}
        for zone in BASE_ZONE_CHEST_COUNTS:
            chests, supply = BASE_ZONE_CHEST_COUNTS[zone]
            for n in range(1, chests + 1):
                name = zone_chest_location_name(zone, n)
                base[name] = ZONE_CHEST_TABLE[name]
            for n in range(1, supply + 1):
                name = zone_supply_chest_location_name(zone, n)
                base[name] = ZONE_CHEST_TABLE[name]
        digest = hashlib.sha1(json.dumps(sorted(base.items())).encode()).hexdigest()
        self.assertEqual(digest, BASE_CHEST_IDS_SHA1)

    def test_dlc_chest_zones_are_dlc_zones(self):
        for zone in DLC_ZONE_CHEST_COUNTS:
            self.assertEqual(ZONES_BY_NAME[zone].category, "dlc", zone)
        for zone in (*BASE_ZONE_CHEST_COUNTS, *LATE_BASE_ZONE_CHEST_COUNTS):
            self.assertEqual(ZONES_BY_NAME[zone].category, "base", zone)

    def test_every_chest_zone_is_in_the_merged_table(self):
        self.assertEqual(set(ZONE_CHEST_COUNTS), set(BASE_ZONE_CHEST_COUNTS) | set(LATE_BASE_ZONE_CHEST_COUNTS) | set(DLC_ZONE_CHEST_COUNTS))

    def test_dlc_chest_ids_stay_in_their_own_zone_range(self):
        zone_order = list(ZONES_BY_NAME)
        for zone in DLC_ZONE_CHEST_COUNTS:
            low = DLC_ZONE_CHEST_BASE_ID + zone_order.index(zone) * DLC_ZONE_CHEST_SLOTS_PER_ZONE
            for location_id in _zone_ids(zone):
                self.assertTrue(low < location_id <= low + DLC_ZONE_CHEST_SLOTS_PER_ZONE, zone)

    def test_all_location_ids_are_unique(self):
        ids = list(LOCATION_TABLE.values())
        self.assertEqual(len(ids), len(set(ids)))
