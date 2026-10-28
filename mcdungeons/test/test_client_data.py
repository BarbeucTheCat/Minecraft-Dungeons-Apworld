import ast
import json
import unittest
from pathlib import Path

from ..ZoneData import ZONES

_READER = Path(__file__).resolve().parents[1] / "client" / "dungeons_reader.py"


def _literal(name):
    """Read a module-level dict literal from dungeons_reader.py without importing it (the reader
    needs Windows-only modules)."""
    tree = ast.parse(_READER.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{name} not found in dungeons_reader.py")


class TestElevelNames(unittest.TestCase):
    def test_every_zone_resolves_to_a_game_level_value(self):
        elevelnames = _literal("ELEVELNAMES")
        remap = _literal("ZONE_NAME_TO_ELEVELNAME_KEY")
        missing = [z.internal_name for z in ZONES
                   if remap.get(z.internal_name, z.internal_name) not in elevelnames]
        self.assertEqual(missing, [])

    def test_values_are_unique_one_byte_enum_values(self):
        values = list(_literal("ELEVELNAMES").values())
        self.assertEqual(len(values), len(set(values)))
        self.assertTrue(all(0 < v < 256 for v in values))


class TestMiniBossKillChecks(unittest.TestCase):
    """Locations.MINIBOSS_ZONES (what the apworld generates) must match the zones that actually have a
    mini boss in client/miniboss.py ZONE_MINIBOSSES (what the client can detect)."""

    def test_zones_match_the_clients_mini_boss_table(self):
        from ..Locations import MINIBOSS_ZONES
        tree = ast.parse((_READER.parent / "miniboss.py").read_text(encoding="utf-8"))
        table = next(ast.literal_eval(n.value) for n in tree.body
                     if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "ZONE_MINIBOSSES"
                                                          for t in n.targets))
        with_mini_boss = {zone for zone, aliases in table.items() if aliases}
        self.assertEqual(set(MINIBOSS_ZONES), with_mini_boss)
        self.assertEqual(len(MINIBOSS_ZONES), len(set(MINIBOSS_ZONES)))

    def test_zone_types_match_the_clients_pinned_mini_bosses(self):
        """MINIBOSS_ZONE_TYPE (what the apworld uses for the per-type checks) = the mini boss pinned to each zone in
        client/miniboss.py, and every type has the entity name the powerful-mob tracker reports."""
        from ..Locations import MINIBOSS_ZONE_TYPE, MINIBOSS_TYPE_ORDER, MINIBOSS_TYPE_DISPLAY
        src = (_READER.parent / "miniboss.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        def literal(name):
            return next(ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign)
                        and any(isinstance(t, ast.Name) and t.id == name for t in n.targets))
        zone_table, types = literal("ZONE_MINIBOSSES"), literal("MINIBOSS_TYPES")
        from ..Locations import MINIBOSS_EXTRA_TYPE_ZONES, MINIBOSS_TYPE_ZONES
        from ..ZoneData import ZONES_BY_NAME
        self.assertEqual(MINIBOSS_ZONE_TYPE, {z: a[0] for z, a in zone_table.items() if a})
        # every type is either pinned to a zone or listed with the zones it shows up in
        self.assertEqual(set(MINIBOSS_TYPE_ORDER), set(MINIBOSS_ZONE_TYPE.values()) | set(MINIBOSS_EXTRA_TYPE_ZONES))
        self.assertFalse(set(MINIBOSS_ZONE_TYPE.values()) & set(MINIBOSS_EXTRA_TYPE_ZONES))
        for type_key, zones in MINIBOSS_TYPE_ZONES.items():
            self.assertTrue(zones, type_key)
            for zone in zones:
                self.assertIn(zone, ZONES_BY_NAME, zone)
        self.assertEqual(set(MINIBOSS_TYPE_ORDER), set(MINIBOSS_TYPE_DISPLAY))
        for key in MINIBOSS_TYPE_ORDER:
            self.assertIn(key, types)
            self.assertEqual(types[key][0], MINIBOSS_TYPE_DISPLAY[key])

    def test_the_four_modes(self):
        from ..Locations import (wanted_mini_boss_zones, wanted_mini_boss_type_homes, MINIBOSS_ZONES,
                                 get_base_mini_boss_zones, get_dlc_mini_boss_zones, MINIBOSS_TYPE_ORDER)
        from ..ZoneData import ZONES_BY_NAME
        every_zone = set(MINIBOSS_ZONES)            # zones that have a per-zone check
        every_available = set(ZONES_BY_NAME)        # every zone of the game (a per-type check can live in any of them)
        none, per_type, per_zone, both = 0, 1, 2, 3
        # none: nothing
        self.assertEqual(wanted_mini_boss_zones(none, none), set())
        self.assertEqual(wanted_mini_boss_type_homes(none, none, every_available), {})
        # per_zone: only the per-zone checks, split by group
        self.assertEqual(wanted_mini_boss_zones(per_zone, none), set(get_base_mini_boss_zones()))
        self.assertEqual(wanted_mini_boss_zones(none, per_zone), set(get_dlc_mini_boss_zones()))
        self.assertEqual(wanted_mini_boss_type_homes(per_zone, per_zone, every_available), {})
        # per_type: only the per-type checks; both: the two sets together
        self.assertEqual(wanted_mini_boss_zones(per_type, per_type), set())
        self.assertEqual(list(wanted_mini_boss_type_homes(per_type, per_type, every_available)), MINIBOSS_TYPE_ORDER)
        self.assertEqual(wanted_mini_boss_zones(both, both), every_zone)
        self.assertEqual(list(wanted_mini_boss_type_homes(both, both, every_available)), MINIBOSS_TYPE_ORDER)
        # base game only: DLC-only kinds (Ghast, Wildfire, Elder Guardian...) have no check
        base_only = set(wanted_mini_boss_type_homes(per_type, none, every_available))
        self.assertEqual(base_only, {"enderman", "skeletonhorseman", "evoker", "redstonegolem",
                                     "pillagerraidcaptain", "vindicatorraidcaptain"})   # Raid Captains: Mooshroom Island
        # a DLC pack that is off (its zones unavailable) drops its kinds
        no_nether = every_available - {"warpedforest", "crimsonforest", "soulsandvalley", "basaltdeltas", "netherfortress"}
        self.assertNotIn("ghast", wanted_mini_boss_type_homes(none, per_type, no_nether))
        self.assertIn("wildfire", wanted_mini_boss_type_homes(none, per_type, every_available))
        # Raid Captains also exist in DLC zones: with only the DLC group on they are held by Abyssal Monument
        self.assertEqual(wanted_mini_boss_type_homes(none, per_type, every_available)["pillagerraidcaptain"], "abyssalmonument")

    def test_location_ids_are_unique_and_in_their_own_block(self):
        from ..Locations import LOCATION_TABLE, MINI_BOSS_KILL_TABLE, MINIBOSS_KILL_BASE_ID
        self.assertEqual(sorted(MINI_BOSS_KILL_TABLE.values()),
                         list(range(MINIBOSS_KILL_BASE_ID, MINIBOSS_KILL_BASE_ID + len(MINI_BOSS_KILL_TABLE))))
        self.assertEqual(len(set(LOCATION_TABLE.values())), len(LOCATION_TABLE))


class TestEyeOfEnderChecks(unittest.TestCase):
    """The six Eye of Ender checks: one per EEyeOfEnderType (1..6), in their own ID block, each in an
    existing base-game parent mission, matched by six progression items."""

    def test_one_check_per_eye_type_in_a_real_base_zone(self):
        from ..Locations import (EYE_OF_ENDER_TABLE, EYE_OF_ENDER_ZONE, EYE_OF_ENDER_BASE_ID, LOCATION_TABLE,
                                 get_eye_of_ender_location_id)
        from ..ZoneData import ZONES_BY_NAME
        self.assertEqual(sorted(EYE_OF_ENDER_ZONE), [1, 2, 3, 4, 5, 6])
        for eye_type, zone in EYE_OF_ENDER_ZONE.items():
            self.assertEqual(ZONES_BY_NAME[zone].category, "base", zone)
            self.assertEqual(get_eye_of_ender_location_id(eye_type), EYE_OF_ENDER_BASE_ID + eye_type)
        self.assertEqual(len(EYE_OF_ENDER_TABLE), 6)
        self.assertEqual(len(set(LOCATION_TABLE.values())), len(LOCATION_TABLE))

    def test_item_exists_with_six_copies(self):
        from ..Items import ITEM_TABLE, EYE_OF_ENDER_ITEM, EYE_OF_ENDER_COPIES
        self.assertIn(EYE_OF_ENDER_ITEM, ITEM_TABLE)
        self.assertEqual(EYE_OF_ENDER_COPIES, 6)


class TestZoneNameLookup(unittest.TestCase):
    """client/data_seeds/zone_name_lookup.json maps the game's live ELevelNames byte to a zone name."""

    def setUp(self):
        path = _READER.parent / "data_seeds" / "zone_name_lookup.json"
        self.lookup = json.loads(path.read_text(encoding="utf-8"))
        self.elevelnames = _literal("ELEVELNAMES")
        self.remap = _literal("ZONE_NAME_TO_ELEVELNAME_KEY")

    def test_every_entry_agrees_with_the_games_enum(self):
        zone_names = {z.internal_name for z in ZONES}
        wrong = [(key, name) for key, name in self.lookup.items()
                 if name in zone_names
                 and self.elevelnames.get(self.remap.get(name, name)) != int(key)]
        self.assertEqual(wrong, [])

    def test_every_dlc_zone_is_named(self):
        named = set(self.lookup.values())
        self.assertEqual([z.internal_name for z in ZONES if z.category == "dlc"
                          and z.internal_name not in named], [])
