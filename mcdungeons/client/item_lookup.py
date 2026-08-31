"""
item_lookup.py - the complete item reference table (all 269 items), unlike
NAME_LOOKUP/list_all_items.py which only ever contains items your specific
save has personally encountered. Categorized by confirmed contiguous
ID ranges rather than name-pattern guessing:

    3089-3663  Melee    (81 items - swords, axes, glaives, etc.)
    3670-4167  Ranged   (71 items - bows, crossbows)
    4174-4678  Armor    (71 items - all armor sets)
    4685+      Artifact/Consumable (46 items - arrows, artifacts, potions)

Boundaries confirmed directly from the source list: 3663 is the last
melee item (Anchor_Unique1) before 3670 (Bow, first ranged item); 4167
is the last ranged item (VoidBow_Unique1) before 4174 (CowardsArmor,
first armor item); 4678 is the last armor item (NatureArmor_Unique1)
before 4685 (Arrow, first non-equipment item).

Usage, mirroring ENCHANT_TABLE/ENCHANT_BY_NAME in give_item.py:
    from item_lookup import ITEM_TABLE, ITEM_BY_NAME, items_by_category
    ITEM_BY_NAME["glaive"]          -> 3306
    ITEM_TABLE[3306]                -> {"name": "Glaive", "category": "Melee"}
    items_by_category("Ranged")     -> [(3670, "Bow"), (3677, "Bow_Spooky1"), ...]
"""

import csv
import io
import os

# Resolved relative to THIS file's own location, not the caller's current
# working directory - item_lookup.py gets imported (via give_item.py) from
# dungeons_ap_client.py, which os.chdir()s to a writable per-user data
# directory before that import chain runs (see its DATA_DIR comment) so
# dungeons_reader.py's own JSON state files land somewhere writable. A
# bare relative path here would silently break in that context, looking
# for all_items.csv in the wrong folder - this static reference table
# isn't meant to move with the caller's cwd regardless of whether it's
# invoked standalone (`python item_lookup.py`) or imported from a
# different working directory.
ALL_ITEMS_CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "all_items.csv")


def _read_data_file(path):
    """Reads a bundled data file's text content, working whether this
    module was loaded from a real extracted directory OR live from
    inside a zipped .apworld via zipimport - which is how the
    Archipelago Launcher actually runs client/*.py when a world isn't
    manually extracted (same situation _apworld_data.py's docstring
    describes for Locations.py/ZoneData.py/Items.py, one level up).

    Confirmed happening in practice: __file__-relative path math alone
    (ALL_ITEMS_CSV above) resolves to the CORRECT-looking path even when
    running from inside the .apworld, since zipimport gives modules a
    __file__ that looks like a normal nested filesystem path - but a
    plain open() on that path still fails, because nothing about the
    zip archive's internal structure is actually a real directory on
    disk. __loader__.get_data(path) is what actually knows how to pull
    a file out of the archive; a regular SourceFileLoader's get_data
    does the equivalent of open().read() for a real extracted file, so
    this works either way without needing to detect which situation
    it's in.

    Falls back to plain open() if __loader__ doesn't support get_data at
    all (e.g. running this file directly as a script - `python
    item_lookup.py` - rather than importing it as a module)."""
    try:
        return __loader__.get_data(path).decode("utf-8")
    except (NameError, AttributeError, OSError):
        with open(path, encoding="utf-8") as f:
            return f.read()


def categorize_item(item_name_index):
    if 3089 <= item_name_index <= 3663:
        return "Melee"
    if 3670 <= item_name_index <= 4167:
        return "Ranged"
    if 4174 <= item_name_index <= 4678:
        return "Armor"
    return "Artifact/Consumable"


def load_item_table(path=ALL_ITEMS_CSV):
    table = {}
    reader = csv.DictReader(io.StringIO(_read_data_file(path)))
    for row in reader:
        idx = int(row["item_name_index"])
        table[idx] = {"name": row["name"], "category": row["category"]}
    return table


ITEM_TABLE_LOAD_ERROR = None
try:
    ITEM_TABLE = load_item_table()
except OSError as e:
    # Missing/unreadable all_items.csv used to crash this entire module at
    # import time - which, since dungeons_ap_client.py imports this
    # transitively (via apply_item_reward -> give_item -> item_lookup),
    # took the WHOLE client down with it: missions, chests, emeralds,
    # DeathLink, everything, none of which have anything to do with
    # equipment rewards. Falls back to an empty table instead - every
    # equipment/location-reward grant attempt will then fail with a clear,
    # catchable error (see give_item.py's ITEM_TABLE_LOAD_ERROR check)
    # rather than the whole client refusing to even open a window.
    ITEM_TABLE = {}
    ITEM_TABLE_LOAD_ERROR = e
ITEM_BY_NAME = {info["name"].lower(): idx for idx, info in ITEM_TABLE.items()}


SEASONAL_MARKERS = ("_spooky", "_winter", "_year")


def is_seasonal(name):
    """True for event/DLC-exclusive items - confirmed by cross-referencing
    the wiki's "(Seasonal)" tags against our internal names: every
    _Spooky/_Winter/_Year-suffixed item corresponds to a wiki-listed
    seasonal exclusive. Regular _Unique variants are NOT seasonal - they're
    normal base-game uniques, always available."""
    return any(marker in name.lower() for marker in SEASONAL_MARKERS)


def items_by_category(category, include_seasonal=True):
    """category: 'Melee', 'Ranged', 'Armor', or 'Artifact/Consumable'.
    include_seasonal=False filters out _Spooky/_Winter/_Year DLC items -
    useful so an Archipelago game doesn't require every season pass to
    play, since a seasonal item could otherwise get randomly granted to
    someone who doesn't own it."""
    items = sorted((idx, info["name"]) for idx, info in ITEM_TABLE.items()
                    if info["category"] == category)
    if not include_seasonal:
        items = [(idx, name) for idx, name in items if not is_seasonal(name)]
    return items


def base_items_by_category(category, include_seasonal=True):
    """Same as items_by_category, but excludes _Unique variants - just the
    base, non-themed item for each weapon/armor line. Not perfect: a few
    items use "(Unique)" instead of "_Unique" in their name and will
    still slip through as "base" - check the full list if that matters."""
    variant_markers = ("_unique",)
    return [(idx, name) for idx, name in items_by_category(category, include_seasonal=include_seasonal)
            if not any(marker in name.lower() for marker in variant_markers)]


if __name__ == "__main__":
    from collections import Counter
    counts = Counter(info["category"] for info in ITEM_TABLE.values())
    print(f"Loaded {len(ITEM_TABLE)} items total:")
    for cat, count in counts.items():
        print(f"  {cat}: {count}")

    print("\nBase (non-variant) items per category:")
    for cat in ("Melee", "Ranged", "Armor", "Artifact/Consumable"):
        bases = base_items_by_category(cat)
        print(f"\n{cat} ({len(bases)}):")
        for idx, name in bases:
            print(f"  [{idx}] {name}")
