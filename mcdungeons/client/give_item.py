"""
give_item.py - reusable "give the player an item" building block for the
Archipelago client. Graduated from give_item_test.py once ClientAddItem
and the TMap-based EquipmentSlots reading were both confirmed working
against real, verified game state.

Layout of FInventoryItemData (0x78 = 120 bytes total) - confirmed via two
independent sources (Dungeons_structs.hpp and StructsInfo.json), every
offset matching exactly:
  0x00  ItemId (FSerializableItemId, 0x14 bytes)
          0x0C within THAT: SerializedId (FName - 4 byte ComparisonIndex + 4 byte Number)
  0x14  ItemPower (float)
  0x18  Enchantments (TArray, 0x10 bytes) - left empty/zeroed
  0x28  ArmorProperties (TArray, 0x10 bytes) - left empty/zeroed
  0x38  Rarity (byte enum: 0=Common, 1=Rare, 2=Unique)
  0x39-0x3F  bIsUpgraded/bIsGifted/bIsModified/timesModified - left false/0
  0x40  (0x20 bytes unreflected padding) - zeroed, safest default
  0x60  bHasNetherite (bool) - false
  0x64  NetheriteEnchantData (FEnchantmentData, 0x10 bytes) - zeroed
  0x74  (4 bytes final padding) - zeroed

EquipmentSlots TMap layout - confirmed against real data, six real
equipped items read back correctly:
  0x00  Data.Ptr (element array pointer)
  0x08  Data.Num (int32)
  0x0C  Data.Max (int32)
  each element (0x18 stride): Key @0x00 (EEquipmentSlot byte),
                               Value @0x08 (UInventoryItemSlot* pointer)

pip install pywin32 pymem
"""

import win32file
import struct
import csv
import io
import random
import time
import os
import pymem.exception
from dungeons_reader import attach, get_item_stash_component, read_inventory, OFFSETS, NAME_LOOKUP
from item_lookup import (ITEM_TABLE, ITEM_BY_NAME, categorize_item, items_by_category,
                          base_items_by_category, ITEM_TABLE_LOAD_ERROR)
try:
    from dungeons_reader import read_storage
except ImportError:
    read_storage = None  # not present in this version of dungeons_reader.py - storage just gets skipped below

def _pipe_name_for(pm):
    """Per-process pipe name matching dungeons_bridge.cpp's GetPipeName()
    and dungeons_reader.py's own _pipe_name_for - MUST agree exactly.
    give_item.py is normally run standalone (its own attach()), so it
    needs its own copy of this rather than importing dungeons_reader's
    (which would create a circular/duplicate-module situation given how
    this script is invoked) - see dungeons_reader.py's _pipe_name_for
    docstring for the full "two clients, one fixed pipe name" story."""
    return rf"\\.\pipe\dungeons_bridge_{pm.process_id}"
SUPER_STRUCT_OFFSET = 0x40  # Off::UStruct::SuperStruct
FINVENTORYITEMDATA_SIZE = 0x78
EQUIPMENT_ELEMENT_STRIDE = 0x18

# Confirmed empirically across 7 data points (power values 1, 5, 9.1, 10,
# 20, 50, 100), zero deviation: the raw ItemPower float we write is NOT
# the same number the game displays - the game shows displayed = 10*raw - 9.
POWER_DISPLAY_SCALE = 10
POWER_DISPLAY_OFFSET = -9


def raw_to_displayed_power(raw_power):
    return POWER_DISPLAY_SCALE * raw_power + POWER_DISPLAY_OFFSET


def displayed_to_raw_power(displayed_power):
    return (displayed_power - POWER_DISPLAY_OFFSET) / POWER_DISPLAY_SCALE


FENCHANTMENTDATA_SIZE = 0x10
MAX_ENCHANTMENT_SLOTS = 3  # confirmed game limit (3 visible slots)
CANDIDATES_PER_SLOT = 3  # confirmed via real dump: groups are (0,1,2), (3,4,5), (6,7,8) - not pairs
TOTAL_ENCHANTMENT_ARRAY_SLOTS = MAX_ENCHANTMENT_SLOTS * CANDIDATES_PER_SLOT  # 9

# EEnchantmentCategory - confirmed via SDK, bitflags. A weapon's own
# category is one of these; an enchant's category_raw (loaded from
# enchant_table.csv) may be a combination, e.g. Melee|Ranged.
CATEGORY_MELEE = 1
CATEGORY_RANGED = 2
CATEGORY_AOE = 4
CATEGORY_ARMOR = 8
CATEGORY_PERMANENT = 64


# Maps item_lookup's category strings to the matching enchant CATEGORY_*
# bit, so weapon_category can be derived automatically from an item name
# instead of set by hand each time.
ITEM_CATEGORY_TO_ENCHANT_CATEGORY = {
    "Melee": CATEGORY_MELEE,
    "Ranged": CATEGORY_RANGED,
    "Armor": CATEGORY_ARMOR,
}


def enchant_category_for_item(item_name_index):
    """Looks up an item's type via item_lookup and returns the matching
    CATEGORY_* constant. Raises for Artifact/Consumable items, which
    don't take weapon-style enchants."""
    item_category = categorize_item(item_name_index)
    if item_category not in ITEM_CATEGORY_TO_ENCHANT_CATEGORY:
        raise ValueError(f"item_name_index {item_name_index} is an {item_category} - no enchant category applies")
    return ITEM_CATEGORY_TO_ENCHANT_CATEGORY[item_category]


# Same reasoning as item_lookup.py's ALL_ITEMS_CSV: resolved relative to
# this file's own location, not the caller's cwd - dungeons_ap_client.py
# os.chdir()s to a writable per-user data directory before importing this
# module (via apply_item_reward.py), and a bare relative path here would
# silently look for enchant_table.csv in the wrong place afterward.
_ENCHANT_TABLE_DEFAULT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "enchant_table.csv")


def _read_data_file(path):
    """See item_lookup.py's _read_data_file - identical reasoning: works
    whether this module was loaded from a real extracted directory or
    live from inside a zipped .apworld via zipimport (which is how the
    Launcher actually runs client/*.py unless the world was manually
    extracted). A plain open() on a __file__-relative path resolves to a
    correct-LOOKING path even under zipimport, but still fails, since
    nothing about a zip archive's internals is a real directory."""
    try:
        return __loader__.get_data(path).decode("utf-8")
    except (NameError, AttributeError, OSError):
        with open(path, encoding="utf-8") as f:
            return f.read()


def load_enchant_table(path=_ENCHANT_TABLE_DEFAULT_PATH):
    """Loads the reference table generated by build_enchant_table.py -
    "name" is the real in-game display text (queried live via
    GetNameForEnchantmentType -> Conv_TextToString), "enum_name" is the
    raw C++ enum identifier kept for reference (these differ for several
    IDs - e.g. TypeID 18's enum name is CaveSpiderPoisonEnchantment but
    displays as "Cave Spider"). category_raw/rarity are also queried
    live, not guessed."""
    table = {}
    reader = csv.DictReader(io.StringIO(_read_data_file(path)))
    for row in reader:
        type_id = int(row["type_id"])
        table[type_id] = {
            "name": row["name"],
            "enum_name": row.get("enum_name", row["name"]),
            "category_raw": int(row["category_raw"]),
            "rarity": row["rarity"],
        }
    return table


ENCHANT_TABLE_LOAD_ERROR = None
try:
    ENCHANT_TABLE = load_enchant_table()
except OSError as e:
    # Same reasoning as item_lookup.py's ITEM_TABLE_LOAD_ERROR - don't
    # take the whole client down over a missing reference CSV that only
    # equipment rewards need.
    ENCHANT_TABLE = {}
    ENCHANT_TABLE_LOAD_ERROR = e

# Convenience: look up a TypeID by name (case-insensitive), e.g.
# ENCHANT_BY_NAME["sharpness"] -> 1
ENCHANT_BY_NAME = {info["name"].lower(): type_id for type_id, info in ENCHANT_TABLE.items()}


def validate_enchantment(type_id, weapon_category):
    """Raises if type_id isn't a real enchant, or doesn't support
    weapon_category (a single CATEGORY_* bit, e.g. CATEGORY_MELEE)."""
    info = ENCHANT_TABLE.get(type_id)
    if info is None:
        raise ValueError(f"TypeID {type_id} isn't in enchant_table.csv - not a real enchant")
    if not (info["category_raw"] & weapon_category):
        raise ValueError(
            f"{info['name']} (TypeID {type_id}) doesn't support category {weapon_category} "
            f"- valid categories: {info['category_raw']}"
        )


# TypeIDs confirmed broken/scrapped in-game (valid category per the table,
# but don't actually work) - excluded from random rolls. Add to this as
# more get found.
KNOWN_BROKEN_ENCHANT_IDS = {18, 11, 34, 42, 33, 13, 79, 49, 74, 85, 63, 59,
                             150, 153, 99, 130, 126, 154, 138, 139, 151, 118, 131, 41}
# Confirmed not working in-game (reported after the real-name fix, so
# these should be reliable - check enchant_table.csv's "name" column for
# what each one is actually called if needed). 41 = the real Shared Pain
# (not to be confused with the earlier TypeID 18 name mixup).


def enchant_pool_for_category(weapon_category):
    """All real TypeIDs whose category_raw supports weapon_category (a
    single CATEGORY_* bit), excluding KNOWN_BROKEN_ENCHANT_IDS."""
    return [type_id for type_id, info in ENCHANT_TABLE.items()
            if (info["category_raw"] & weapon_category) and type_id not in KNOWN_BROKEN_ENCHANT_IDS]


def random_slot_candidates(weapon_category, num_slots=MAX_ENCHANTMENT_SLOTS,
                             candidates_per_slot=CANDIDATES_PER_SLOT, rng=None):
    """Randomly rolls slot_candidates for build_item_data_with_choices /
    give_item_with_choices - num_slots groups, candidates_per_slot options
    each, all valid for weapon_category, no TypeID repeated anywhere on
    the item (matches how real drops never show the same enchant twice
    across slots or within one slot's choices)."""
    rng = rng or random
    pool = enchant_pool_for_category(weapon_category)
    needed = num_slots * candidates_per_slot
    if len(pool) < needed:
        raise ValueError(
            f"Only {len(pool)} enchants support this category, need {needed} "
            f"({num_slots} slots x {candidates_per_slot} candidates) with no repeats"
        )
    picked = rng.sample(pool, needed)
    return [picked[i:i + candidates_per_slot] for i in range(0, needed, candidates_per_slot)]


def random_enchantments(weapon_category, num_slots=MAX_ENCHANTMENT_SLOTS, levels=1, rng=None):
    """Randomly rolls enchantments for build_item_data / give_item (the
    pre-applied, already-chosen version) - one random TypeID per slot, no
    repeats. levels: int applied to every slot, or a list of per-slot
    levels the same length as num_slots."""
    rng = rng or random
    pool = enchant_pool_for_category(weapon_category)
    if len(pool) < num_slots:
        raise ValueError(f"Only {len(pool)} enchants support this category, need {num_slots}")
    picked = rng.sample(pool, num_slots)
    level_list = levels if isinstance(levels, list) else [levels] * num_slots
    return list(zip(picked, level_list))


def build_item_data_with_choices(name_index, power, rarity_raw, slot_candidates, pm,
                                   weapon_category=None):
    """Like build_item_data, but instead of pre-applying a chosen enchant,
    rolls uninvested candidates the player picks and invests in themselves,
    same as a real drop. slot_candidates: list of up to 3 lists, each the
    candidate TypeIDs for one visible slot - 3 candidates/slot is confirmed
    via a real Rare item dump (groups (0,1,2), (3,4,5), (6,7,8), 9 total).
    Fewer candidates per slot (e.g. 2, or 1) also works structurally, just
    pass shorter inner lists - all slots must use the same count.

    Each candidate gets TypeID set, Level=0, InvestedPoints=0, Category=0,
    Source=0 - the exact "rolled but not chosen" pattern confirmed via
    dump_item.py on a real, not-yet-enchanted item."""
    if len(slot_candidates) > MAX_ENCHANTMENT_SLOTS:
        raise ValueError(f"Max {MAX_ENCHANTMENT_SLOTS} visible slots")

    candidates_per_slot = len(slot_candidates[0])
    if any(len(group) != candidates_per_slot for group in slot_candidates):
        raise ValueError("All slots must offer the same number of candidates")

    buf = bytearray(FINVENTORYITEMDATA_SIZE)
    struct.pack_into("<i", buf, 0x0C, name_index)
    struct.pack_into("<i", buf, 0x10, 0)
    struct.pack_into("<f", buf, 0x14, power)
    buf[0x38] = rarity_raw

    flat_type_ids = [type_id for group in slot_candidates for type_id in group]
    for type_id in flat_type_ids:
        if weapon_category is not None:
            validate_enchantment(type_id, weapon_category)
        elif type_id not in ENCHANT_TABLE:
            raise ValueError(f"TypeID {type_id} isn't in enchant_table.csv - not a real enchant")

    total_slots = len(flat_type_ids)
    ench_buf = bytearray(FENCHANTMENTDATA_SIZE * total_slots)
    for i, type_id in enumerate(flat_type_ids):
        offset = i * FENCHANTMENTDATA_SIZE
        ench_buf[offset + 0x00] = type_id
        # Level and InvestedPoints left at 0 - uninvested, player picks in-game.

    remote_addr = pm.allocate(len(ench_buf))
    pm.write_bytes(remote_addr, bytes(ench_buf), len(ench_buf))

    struct.pack_into("<Q", buf, 0x18, remote_addr)
    struct.pack_into("<i", buf, 0x20, total_slots)
    struct.pack_into("<i", buf, 0x24, total_slots)

    return bytes(buf)


def build_item_data(name_index, power, rarity_raw, enchantments=None, pm=None, weapon_category=None):
    """enchantments: optional list of up to 3 (type_id, level) tuples.
    Each type_id is validated against enchant_table.csv - both that it's
    a real enchant, and (if weapon_category is given) that it's actually
    valid for this weapon type. Unlike every other field in this struct,
    Enchantments is a TArray - not inline data, a pointer to separately
    allocated memory - so passing enchantments requires `pm` to allocate
    real backing memory in the game process for the array to point at."""
    buf = bytearray(FINVENTORYITEMDATA_SIZE)
    struct.pack_into("<i", buf, 0x0C, name_index)
    struct.pack_into("<i", buf, 0x10, 0)
    struct.pack_into("<f", buf, 0x14, power)
    buf[0x38] = rarity_raw

    if enchantments:
        if len(enchantments) > MAX_ENCHANTMENT_SLOTS:
            raise ValueError(f"Max {MAX_ENCHANTMENT_SLOTS} enchantment slots")
        if not pm:
            raise ValueError("enchantments requires pm (to allocate backing memory)")

        for type_id, level in enchantments:
            if weapon_category is not None:
                validate_enchantment(type_id, weapon_category)
            elif type_id not in ENCHANT_TABLE:
                raise ValueError(f"TypeID {type_id} isn't in enchant_table.csv - not a real enchant")

        ench_buf = bytearray(FENCHANTMENTDATA_SIZE * TOTAL_ENCHANTMENT_ARRAY_SLOTS)
        for i, (type_id, level) in enumerate(enchantments):
            # The 9-entry array is 3 groups of 3 - (0,1,2), (3,4,5), (6,7,8) -
            # one group per visible slot, each holding up to 3 rolled
            # candidates. Two occupants in the same group collide (confirmed:
            # placing 2 enchants in group (0,1,2) left only the first one
            # rendering). Each enchant gets its own group via index*3.
            # InvestedPoints=0 on the base pick - confirmed via a real,
            # freshly-chosen (not further upgraded) enchant: Level=1 but
            # InvestedPoints=0. Extra points beyond the base pick presumably
            # only accumulate in InvestedPoints if the player levels it up
            # further at the enchanting table.
            offset = (i * CANDIDATES_PER_SLOT) * FENCHANTMENTDATA_SIZE
            ench_buf[offset + 0x00] = type_id
            struct.pack_into("<i", ench_buf, offset + 0x04, level)
            # Category/Source left at 0 - confirmed via real dump: applied
            # enchantments never set these, despite what the enum names imply.
            # InvestedPoints left at 0 - see note above.

        remote_addr = pm.allocate(len(ench_buf))
        pm.write_bytes(remote_addr, bytes(ench_buf), len(ench_buf))

        struct.pack_into("<Q", buf, 0x18, remote_addr)
        struct.pack_into("<i", buf, 0x20, TOTAL_ENCHANTMENT_ARRAY_SLOTS)  # Num
        struct.pack_into("<i", buf, 0x24, TOTAL_ENCHANTMENT_ARRAY_SLOTS)  # Max

    return bytes(buf)


def find_function_on_class(pm, pipe, class_addr, function_name, max_depth=10):
    """Finds a function by checking a class's own declared members
    (find_outer), walking up the inheritance chain via SuperStruct if not
    found there - GetDisplayNameText, GetOwnedEmeralds, and ClientAddItem
    all turned out to be declared on a parent class."""
    current_class = class_addr

    for depth in range(max_depth):
        if not current_class:
            break

        win32file.WriteFile(pipe, f"find_outer:{hex(current_class)[2:]}".encode())
        _, data = win32file.ReadFile(pipe, 32768)
        response = data.decode()

        if response.startswith("FOUND:"):
            header, _, rest = response.partition("|")
            count = int(header.split(":")[1])

            if count > 0:
                addrs = [entry.split(":")[0] for entry in rest.split(",")]
                for addr_hex in addrs:
                    obj_addr = int(addr_hex, 16)
                    try:
                        comparison_index = pm.read_int(obj_addr + 0x18)
                    except Exception:
                        continue
                    win32file.WriteFile(pipe, f"resolve_name:{comparison_index:x}".encode())
                    _, name_data = win32file.ReadFile(pipe, 8192)
                    name_response = name_data.decode()
                    if name_response.startswith("NAME:"):
                        resolved_name = name_response[5:].strip().rstrip("\x00")
                        if resolved_name == function_name:
                            return obj_addr

        current_class = pm.read_longlong(current_class + SUPER_STRUCT_OFFSET)

    raise RuntimeError(f"'{function_name}' not found within {max_depth} levels of the class hierarchy.")


def calldata(pipe, object_addr, function_addr, parms_bytes):
    parms_hex = parms_bytes.hex().upper()
    request = f"CALLDATA {object_addr:X} {function_addr:X} {parms_hex}"
    win32file.WriteFile(pipe, request.encode())
    _, data = win32file.ReadFile(pipe, 8192)
    response = data.decode()
    if not response.startswith("OK "):
        raise RuntimeError(f"Call failed: {response}")
    return bytes.fromhex(response[3:])


def call(pipe, object_addr, function_addr, parms_size):
    request = f"CALL {object_addr:X} {function_addr:X} {parms_size:X}"
    win32file.WriteFile(pipe, request.encode())
    _, data = win32file.ReadFile(pipe, 8192)
    response = data.decode()
    if not response.startswith("OK "):
        raise RuntimeError(f"Call failed: {response}")
    return bytes.fromhex(response[3:])


def read_equipment(pm, pipe, item_stash, item_stash_class):
    """Calls GetEquipmentSlots() for real and reads the actual equipped
    items using the confirmed TMap layout."""
    func_addr = find_function_on_class(pm, pipe, item_stash_class, "GetEquipmentSlots")
    header_bytes = call(pipe, item_stash, func_addr, 0x60)

    data_ptr = struct.unpack("<Q", header_bytes[0:8])[0]
    max_slots = struct.unpack("<i", header_bytes[12:16])[0]

    if not data_ptr or max_slots <= 0 or max_slots > 16:
        return []

    equipped = []
    for i in range(max_slots):
        elem_addr = data_ptr + i * EQUIPMENT_ELEMENT_STRIDE
        key = pm.read_uchar(elem_addr + 0x00)
        value_ptr = pm.read_longlong(elem_addr + 0x08)
        if not value_ptr:
            continue

        inventory_item = pm.read_longlong(value_ptr + OFFSETS["slot_item"])
        if not inventory_item:
            continue

        item_struct = inventory_item + OFFSETS["item_struct"]
        power = pm.read_float(item_struct + OFFSETS["item_power"])
        rarity_raw = pm.read_uchar(item_struct + OFFSETS["rarity"])
        name_index = pm.read_int(item_struct + OFFSETS["item_id_struct"] + OFFSETS["serialized_id"])
        name = NAME_LOOKUP.get(name_index, f"unknown (index {name_index})")
        equipped.append({"key": key, "name": name, "power": power, "rarity_raw": rarity_raw})

    return equipped


# How long to wait between retries when a memory read fails during what's
# likely a zone transition/loading screen, and how long to give up after -
# a real loading screen shouldn't take anywhere near 2 minutes. Confirmed
# via real testing: reading inventory mid zone-change raises
# pymem.exception.MemoryReadError (WinAPIError 299 / ERROR_PARTIAL_COPY) -
# Unreal is tearing down/rebuilding UObjects during the transition, not a
# bug in our code.
LOADING_RETRY_INTERVAL = 2.0
LOADING_MAX_WAIT = 120.0


def _is_transient_memory_error(e):
    return isinstance(e, (pymem.exception.MemoryReadError, pymem.exception.WinAPIError))


def _wait_for_zone_transition(func, *args, **kwargs):
    """Calls func(*args, **kwargs), retrying on the memory-read failure
    pattern seen during zone transitions instead of letting it propagate
    as a hard failure. Re-raises anything else, or if LOADING_MAX_WAIT is
    exceeded (something other than a normal loading screen is wrong)."""
    waited = 0.0
    while True:
        try:
            return func(*args, **kwargs)
        except Exception as e:
            if _is_transient_memory_error(e) and waited < LOADING_MAX_WAIT:
                time.sleep(LOADING_RETRY_INTERVAL)
                waited += LOADING_RETRY_INTERVAL
                continue
            raise


# How many consecutive checks item_stash's address must stay identical
# before it's trusted enough to write to, and the delay between checks.
STABILITY_CHECKS = 3
STABILITY_CHECK_INTERVAL = 0.5


def wait_for_stable_item_stash(pm, base, max_wait=LOADING_MAX_WAIT):
    """Confirms item_stash is genuinely stable, not just currently
    readable, before returning it. Confirmed via real testing: items got
    silently wiped during a loading screen even with read-retry already
    in place - a component can be readable for one instant and then torn
    down right as a write lands on it. Being readable once isn't proof of
    stability; requiring the SAME address across several checks is a much
    stronger guarantee. Returns (item_stash, item_stash_class), or raises
    RuntimeError if it never stabilizes within max_wait."""
    from dungeons_reader import get_item_stash_component

    waited = 0.0
    while True:
        addresses = []
        for _ in range(STABILITY_CHECKS):
            item_stash, error = get_item_stash_component(pm, base)
            addresses.append(item_stash)
            time.sleep(STABILITY_CHECK_INTERVAL)

        if addresses[0] and all(a == addresses[0] for a in addresses):
            item_stash = addresses[0]
            item_stash_class = pm.read_longlong(item_stash + 0x10)
            return item_stash, item_stash_class

        if waited >= max_wait:
            raise RuntimeError(
                f"item_stash never stabilized after {max_wait}s - probably stuck loading, "
                f"or the ItemStashComponent genuinely isn't reachable right now."
            )
        waited += STABILITY_CHECKS * STABILITY_CHECK_INTERVAL


def get_max_inventory_count(pm, pipe, item_stash, item_stash_class):
    """Calls the real UItemStashComponent::GetMaxInventoryCount() - this
    is the actual cap the game enforces (see OnInventoryFull /
    GetSalvageInfo in the SDK). Confirmed via real testing: exceeding
    this silently auto-salvages EXISTING items (equipped, inventory, and
    storage all observed affected) to make room - invested enchant points
    get refunded (like a normal manual salvage) but emeralds don't,
    which is what tipped this off. Params struct is just a single int32
    ReturnValue at offset 0 - confirmed via Dungeons_parameters.hpp."""
    func_addr = find_function_on_class(pm, pipe, item_stash_class, "GetMaxInventoryCount")
    result = call(pipe, item_stash, func_addr, 0x04)
    return struct.unpack("<i", result[0:4])[0]


def check_inventory_room(pm, pipe, item_stash, item_stash_class, warn_only=False):
    """Compares current inventory+storage count against the real cap from
    get_max_inventory_count. Raises RuntimeError if full (or at/over cap)
    unless warn_only=True, in which case it just prints a warning and
    continues - use warn_only=True if you'd rather risk auto-salvage than
    block a send outright."""
    current_items = _wait_for_zone_transition(read_inventory, pm, item_stash)
    stored_items = _wait_for_zone_transition(read_storage, pm, item_stash) if read_storage is not None else []
    current_count = len(current_items) + len(stored_items)
    max_count = get_max_inventory_count(pm, pipe, item_stash, item_stash_class)

    if current_count >= max_count:
        message = (f"Inventory+storage is full ({current_count}/{max_count}) - granting another "
                    f"item now would likely trigger the game's auto-salvage and eat an existing item.")
        if warn_only:
            print(f"WARNING: {message}")
        else:
            raise RuntimeError(message + " Pass warn_only=True to give_item/give_item_with_choices/"
                                          "give_random_item to grant anyway.")


# Tracks the highest displayed power actually observed so far THIS
# PROCESS RUN. A real player's best power should essentially never drop
# during a session - if get_best_displayed_power suddenly reports lower
# than what we already saw, that's strong, immediate evidence something
# just went wrong (items wiped, or we're reading a different/empty
# container) rather than something to shrug off and keep sending into.
# Confirmed necessary via real testing: the stability check alone did not
# prevent item loss during a loading screen, so this exists as a hard
# stop to catch the SYMPTOM immediately instead of compounding the damage
# by continuing to send more items in the same run once something's wrong.
_highest_power_seen = None


class PowerDropDetected(RuntimeError):
    """Raised when best_displayed power just dropped from a previously
    observed higher value - treat this as "stop and investigate", not
    something to retry past. See _highest_power_seen above for why."""
    pass


def get_best_displayed_power(pm, pipe, item_stash, item_stash_class, check_for_drop=True):
    """Highest DISPLAYED power (not raw) across inventory + storage +
    equipped gear. Returns None if the player has no items anywhere yet.
    Storage is skipped (with a one-time warning) if read_storage isn't
    available in the current dungeons_reader.py. Waits out zone
    transitions instead of failing - see _wait_for_zone_transition.

    check_for_drop: True (default) raises PowerDropDetected if the result
    is lower than the highest value seen so far this run - see
    _highest_power_seen above. Set False only if you specifically expect
    a legitimate drop (e.g. testing on a fresh/different save)."""
    global _highest_power_seen

    current_items = _wait_for_zone_transition(read_inventory, pm, item_stash)
    if read_storage is not None:
        stored_items = _wait_for_zone_transition(read_storage, pm, item_stash)
    else:
        stored_items = []
    equipped_items = _wait_for_zone_transition(read_equipment, pm, pipe, item_stash, item_stash_class)

    raw_powers = (
        [item["power"] for item in current_items]
        + [item["power"] for item in stored_items]
        + [item["power"] for item in equipped_items]
    )
    if not raw_powers:
        result = None
    else:
        result = raw_to_displayed_power(max(raw_powers))

    if check_for_drop and _highest_power_seen is not None:
        if result is None:
            # Worse than a partial drop - NOTHING was found anywhere
            # (inventory + storage + equipped all empty) despite having
            # confirmed real items earlier this run. Previously this fell
            # straight through the `if result is not None:` check below
            # and was silently treated as "fresh save, no items yet" -
            # give_item() would then grant at the bare minimum_power
            # floor into an already-wiped inventory instead of refusing.
            # A full wipe is strictly worse than a numeric drop, so it
            # must raise too, not slip past the check that exists
            # specifically to catch this.
            raise PowerDropDetected(
                f"Best displayed power was {_highest_power_seen}, but NO items were found anywhere "
                f"(inventory/storage/equipped all empty) - this almost certainly means the inventory "
                f"was just wiped (e.g. mid loading-screen/zone-transition instability). "
                f"Stopping here instead of granting into an empty container. "
                f"Check your inventory/storage/equipped gear before continuing."
            )
        if result < _highest_power_seen:
            raise PowerDropDetected(
                f"Best displayed power just dropped from {_highest_power_seen} to {result} - "
                f"this almost certainly means items were lost (e.g. mid loading-screen instability). "
                f"Stopping here instead of sending more items into a possibly-broken state. "
                f"Check your inventory/storage/equipped gear before continuing."
            )

    if result is not None:
        if _highest_power_seen is None or result > _highest_power_seen:
            _highest_power_seen = result

    return result


def give_item(pm, pipe, item_stash, item_stash_class, item_name_index,
               power_bonus=1, minimum_power=1, rarity_raw=0, enchantments=None,
               weapon_category=None, check_capacity=True):
    """Gives the player an item at (their current best DISPLAYED power) +
    power_bonus, or `minimum_power` as a floor if they have absolutely
    nothing yet (fresh save, or somehow lost everything). All power values
    here are DISPLAYED power (what shows in-game), not the raw stored
    float - both power_bonus and minimum_power are adjustable per call,
    pass whatever the check/reward logic decides.

    enchantments: optional list of up to 3 (type_id, level) tuples - look
    up type_id by name via ENCHANT_BY_NAME["sharpness"], or by hand in
    enchant_table.csv. Pass weapon_category (CATEGORY_MELEE etc, from
    the constants above) to validate each enchant actually supports this
    weapon type before spending a call on it - e.g. give_item(..., item_name_index=GLAIVE, weapon_category=CATEGORY_MELEE, enchantments=[(ENCHANT_BY_NAME["sharpness"], 1)]).

    check_capacity: True (default) raises RuntimeError instead of granting
    if inventory+storage is already at the game's real cap - confirmed via
    real testing that exceeding it silently auto-salvages EXISTING items
    to make room. Set False to skip the check (NOT recommended)."""
    if check_capacity:
        check_inventory_room(pm, pipe, item_stash, item_stash_class)

    best_displayed = get_best_displayed_power(pm, pipe, item_stash, item_stash_class)
    if best_displayed is None:
        target_displayed = minimum_power
    else:
        target_displayed = best_displayed + power_bonus
        target_displayed = max(target_displayed, minimum_power)  # never go below the floor

    target_raw = displayed_to_raw_power(target_displayed)

    func_addr = find_function_on_class(pm, pipe, item_stash_class, "ClientAddItem")
    item_data = build_item_data(item_name_index, target_raw, rarity_raw, enchantments=enchantments,
                                 pm=pm, weapon_category=weapon_category)
    calldata(pipe, item_stash, func_addr, item_data)

    return target_displayed


def give_item_with_choices(pm, pipe, item_stash, item_stash_class, item_name_index,
                             slot_candidates, power_bonus=1, minimum_power=1, rarity_raw=0,
                             weapon_category=None, check_capacity=True):
    """Same as give_item, but grants uninvested enchant candidates for the
    player to pick and spend points on in-game, like a real drop, instead
    of a pre-applied enchant. See build_item_data_with_choices for the
    slot_candidates format.

    check_capacity: True (default) raises RuntimeError instead of granting
    if inventory+storage is already at the game's real cap - see give_item's
    docstring for why this matters."""
    if check_capacity:
        check_inventory_room(pm, pipe, item_stash, item_stash_class)

    best_displayed = get_best_displayed_power(pm, pipe, item_stash, item_stash_class)
    if best_displayed is None:
        target_displayed = minimum_power
    else:
        target_displayed = best_displayed + power_bonus
        target_displayed = max(target_displayed, minimum_power)

    target_raw = displayed_to_raw_power(target_displayed)

    func_addr = find_function_on_class(pm, pipe, item_stash_class, "ClientAddItem")
    item_data = build_item_data_with_choices(item_name_index, target_raw, rarity_raw,
                                              slot_candidates, pm, weapon_category=weapon_category)
    calldata(pipe, item_stash, func_addr, item_data)

    return target_displayed


def _grant_item_with_random_enchants(pm, pipe, item_stash, item_stash_class, item_name_index, item_name,
                                       power_bonus, minimum_power, rarity_raw, with_choices, rng,
                                       check_capacity, num_slots):
    """Shared by give_random_item and give_location_reward: grants one
    specific already-chosen item, with random matching enchants (or none
    at all for artifacts, which have no enchant slots). Not meant to be
    called directly - it assumes item_name_index/item_name are already
    validated real items.

    num_slots: how many enchant slots to actually roll/apply (0-3) - this
    is what makes enchant capacity a progression mechanic: a player who
    hasn't received any "Progressive Enchant Slot" items yet gets 0, so
    granted items come out with no enchants at all until they've received
    some. 0 is valid and means "no enchants, same as an artifact" for
    equipment too, not just artifacts."""
    if categorize_item(item_name_index) == "Artifact/Consumable" or num_slots <= 0:
        power = give_item(
            pm, pipe, item_stash, item_stash_class, item_name_index,
            power_bonus=power_bonus, minimum_power=minimum_power, rarity_raw=rarity_raw,
            enchantments=None, weapon_category=None, check_capacity=check_capacity,
        )
        return item_name_index, item_name, power

    weapon_category = enchant_category_for_item(item_name_index)

    if with_choices:
        slot_candidates = random_slot_candidates(weapon_category, num_slots=num_slots, rng=rng)
        power = give_item_with_choices(
            pm, pipe, item_stash, item_stash_class, item_name_index, slot_candidates,
            power_bonus=power_bonus, minimum_power=minimum_power, rarity_raw=rarity_raw,
            weapon_category=weapon_category, check_capacity=check_capacity,
        )
    else:
        enchantments = random_enchantments(weapon_category, num_slots=num_slots, rng=rng)
        power = give_item(
            pm, pipe, item_stash, item_stash_class, item_name_index,
            power_bonus=power_bonus, minimum_power=minimum_power, rarity_raw=rarity_raw,
            enchantments=enchantments, weapon_category=weapon_category, check_capacity=check_capacity,
        )

    return item_name_index, item_name, power


def give_random_item(pm, pipe, item_stash, item_stash_class, category=None,
                       include_uniques=True, include_dlc=False,
                       power_bonus=1, minimum_power=1, rarity_raw=0,
                       with_choices=True, rng=None, check_capacity=True,
                       num_slots=MAX_ENCHANTMENT_SLOTS):
    """Grants a random item, with random enchants matching its type.

    category: "Melee", "Ranged", "Armor", "Artifact", or None for any of
    the three equipment types (Melee/Ranged/Armor - "Artifact" is NOT
    included in that default, since it's a different kind of reward; ask
    for it explicitly). Artifacts have no enchant slots at all - confirmed
    directly - so this skips all enchant logic for them and grants
    plainly, regardless of with_choices.
    include_uniques: True (default) includes named _Unique variants in the
    pool alongside base items; False restricts to base items only.
    include_dlc: False (default) excludes seasonal/event-exclusive items
    (_Spooky/_Winter/_Year suffixed) so a random grant never requires DLC
    or a season pass the player might not own; True allows them.
    with_choices: True (default) grants uninvested candidates to pick
    in-game, like give_item_with_choices; False pre-applies chosen
    enchants instead, like give_item. Ignored for artifacts (see above).
    check_capacity: True (default) raises RuntimeError instead of granting
    if inventory+storage is already at the game's real cap - see give_item's
    docstring for why this matters.
    num_slots: how many enchant slots to actually use (0-3, default 3) -
    pass the player's current progression tier here (see "Progressive
    Enchant Slot" in the AP world's Items.py) rather than always granting
    the max. 0 grants the item with no enchants at all, same as an artifact.

    Returns (item_name_index, item_name, displayed_power)."""
    if ITEM_TABLE_LOAD_ERROR is not None:
        raise RuntimeError(f"item_lookup.py's ITEM_TABLE failed to load ({ITEM_TABLE_LOAD_ERROR}) - "
                            f"all_items.csv is missing or unreadable, equipment rewards are unavailable "
                            f"until it's restored")
    rng = rng or random
    categories = [category] if category else ["Melee", "Ranged", "Armor"]
    lookup_categories = ["Artifact/Consumable" if c == "Artifact" else c for c in categories]

    pool = []
    for cat in lookup_categories:
        cat_items = (items_by_category(cat, include_seasonal=include_dlc) if include_uniques
                     else base_items_by_category(cat, include_seasonal=include_dlc))
        pool.extend(cat_items)
    if not pool:
        raise ValueError(
            f"No items found for category={category!r} "
            f"(include_uniques={include_uniques}, include_dlc={include_dlc})"
        )

    item_name_index, item_name = rng.choice(pool)
    return _grant_item_with_random_enchants(
        pm, pipe, item_stash, item_stash_class, item_name_index, item_name,
        power_bonus, minimum_power, rarity_raw, with_choices, rng, check_capacity, num_slots,
    )


def give_location_reward(pm, pipe, item_stash, item_stash_class, zone_internal_name, category,
                           fallback_to_full_pool=False, power_bonus=1, minimum_power=1, rarity_raw=0,
                           with_choices=True, rng=None, check_capacity=True,
                           num_slots=MAX_ENCHANTMENT_SLOTS):
    """Like give_random_item, but restricts the random pool to
    location_reward_pools.py's curated list of items for this specific
    zone+category, instead of every item of that category in the game -
    e.g. Squid Coast's "Artifact" pool might only be Fireworks Arrow and
    Fishing Rod, not all 46 artifacts, matching how the real game
    restricts loot per zone rather than pulling from everything.

    zone_internal_name: matches ZoneData.py's internal_name / dungeons_
    reader.py's ZONE_NAME_LOOKUP (e.g. "squidcoast"), NOT the display name.
    category: "Melee", "Ranged", "Armor", or "Artifact" - required (unlike
    give_random_item, there's no "any equipment type" default here, since
    a location pool is inherently category-specific).
    fallback_to_full_pool: False (default) raises ValueError if no pool is
    defined for this zone+category in location_reward_pools.py. Set True
    to fall back to give_random_item's normal full-category pool instead
    of failing - useful while you're still filling in pools zone by zone.
    num_slots: how many enchant slots to actually use (0-3, default 3) -
    see give_random_item's docstring for why this exists.

    Returns (item_name_index, item_name, displayed_power)."""
    if ITEM_TABLE_LOAD_ERROR is not None:
        raise RuntimeError(f"item_lookup.py's ITEM_TABLE failed to load ({ITEM_TABLE_LOAD_ERROR}) - "
                            f"all_items.csv is missing or unreadable, equipment rewards are unavailable "
                            f"until it's restored")
    from location_reward_pools import get_location_pool

    names = get_location_pool(zone_internal_name, category)
    rng = rng or random

    if names is None:
        if not fallback_to_full_pool:
            raise ValueError(
                f"No reward pool defined for zone={zone_internal_name!r} category={category!r} "
                f"in location_reward_pools.py - pass fallback_to_full_pool=True to use the full "
                f"category pool instead, or add an entry for this zone."
            )
        return give_random_item(
            pm, pipe, item_stash, item_stash_class, category=category,
            power_bonus=power_bonus, minimum_power=minimum_power, rarity_raw=rarity_raw,
            with_choices=with_choices, rng=rng, check_capacity=check_capacity, num_slots=num_slots,
        )

    if not names:
        raise ValueError(f"Reward pool for zone={zone_internal_name!r} category={category!r} is empty")

    candidates = []
    for name in names:
        idx = ITEM_BY_NAME.get(name.lower())
        if idx is None:
            raise ValueError(
                f"{name!r} (in location_reward_pools.py for {zone_internal_name!r}) isn't a "
                f"recognized item name - check item_lookup.py's ITEM_BY_NAME / all_items.csv"
            )
        candidates.append((idx, ITEM_TABLE[idx]["name"]))

    item_name_index, item_name = rng.choice(candidates)
    return _grant_item_with_random_enchants(
        pm, pipe, item_stash, item_stash_class, item_name_index, item_name,
        power_bonus, minimum_power, rarity_raw, with_choices, rng, check_capacity, num_slots,
    )


if __name__ == "__main__":
    pm, base = attach()
    item_stash, error = get_item_stash_component(pm, base)
    if not item_stash:
        print(f"Could not reach ItemStashComponent: {error}")
        raise SystemExit
    item_stash_class = pm.read_longlong(item_stash + 0x10)

    pipe = win32file.CreateFile(
        _pipe_name_for(pm), win32file.GENERIC_READ | win32file.GENERIC_WRITE,
        0, None, win32file.OPEN_EXISTING, 0, None
    )

    POWER_BONUS = 1     # <-- adjustable: how much better (in DISPLAYED power) than your current best
    MINIMUM_POWER = 1   # <-- adjustable: floor (DISPLAYED power) used if you have nothing at all yet

    # give_random_item picks a random base item from a category (or any of
    # Melee/Ranged/Armor if category=None) and rolls random matching
    # enchant candidates for it automatically.
    #   category="Melee"   -> random melee weapon (this example)
    #   category="Ranged"  -> random bow/crossbow
    #   category="Armor"   -> random armor set
    #   category=None      -> random item of ANY of the three types
    item_name_index, item_name, power_given = give_random_item(
        pm, pipe, item_stash, item_stash_class,
        category="Melee",
        power_bonus=POWER_BONUS, minimum_power=MINIMUM_POWER,
    )
    print(f"Gave {item_name} (index {item_name_index}) at displayed power {power_given}")
    print("Check your in-game inventory - it should show 3 slots, each with 3 real candidates to pick from.")

    win32file.CloseHandle(pipe)
