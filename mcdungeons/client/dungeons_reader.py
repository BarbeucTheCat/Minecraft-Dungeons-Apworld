"""
Minecraft Dungeons - Inventory Reader (fully standalone)
============================================================

No UUU, no Cheat Engine, no dumps needed to run this - ever, for this
game build. Every offset below was derived once using Dumper-7 and
UUU's object dumps, then independently confirmed stable across three
separate game restarts.

pip install pymem

------------------------------------------------------------
THE FULL CHAIN
------------------------------------------------------------
module base
  -> +0x47540B0 -> UWorld
  -> +0x30      -> PersistentLevel
  -> +0x160     -> GameInstance
  -> +0x38      -> LocalPlayers[0]
  -> +0x30      -> PlayerController
  -> +0x360     -> Pawn (you)
  -> +0xEC0     -> ItemStashComponent
  -> +0x300     -> InventorySlots (TArray of InventoryItemSlot*)
       each slot -> +0x30  -> InventoryItem*
                       -> +0x28 -> Item struct (InventoryItemData)
                            -> +0x14 -> ItemPower (float)
                            -> +0x38 -> Rarity (byte enum)
                            -> +0x0  -> ItemId struct (SerializableItemId)
                                 -> +0xC -> SerializedId FName index

If Dungeons ever gets updated, these could shift and need re-deriving -
same techniques used to find them the first time still apply.

------------------------------------------------------------
IDENTIFYING NEW ITEMS / CHESTS / ENEMIES / ZONES
------------------------------------------------------------
Item names are resolved from item_lookup.py's ITEM_TABLE - the complete,
static 269-item reference table (all_items.csv) - so there's nothing to
label or persist for items any more. Other lookup tables (pickup classes,
chest classes, enemy classes, zone names) still live in their own JSON
file next to this script (pickup_class_lookup.json, etc.), created
automatically on first run. Whenever a watch mode ends (Ctrl+C) or the
default mode finishes, it offers to label anything unrecognised seen that
run for those - answer once, it saves immediately, and it's permanent
from then on.
------------------------------------------------------------
GIVING ITEMS (Archipelago check rewards)
------------------------------------------------------------
This writes to game memory - a different risk category than everything
above, which only ever reads. Read the caveats before using it:

- It REPLACES an existing, already-registered inventory item rather than
  creating a new one. Conjuring a brand-new object (or duplicating one
  into an empty slot) isn't safe to do from outside the engine - it has
  no entry in the engine's own object-tracking/garbage-collection system,
  which risks a crash or corruption later, disconnected from the write
  that caused it. Overwriting an item that's already sitting in a slot
  sidesteps that entirely: nothing new gets allocated, nothing new needs
  bookkeeping.
- Because of that, it needs a real item to sacrifice. By default it picks
  your lowest-power Common item (falling back to lowest-power overall if
  you have no Common items) - or pass slot_index to choose exactly.
- It won't trigger the game's own "item gained" popup/effects, since it
  bypasses that code path entirely. The item just appears in the slot.
- No save file is touched and nothing is dumped - this is a live memory
  write while the game is running, gone the moment the process closes
  (until the next check reward writes again).
"""

try:
    import pymem
    import pymem.process
    PYMEM_IMPORT_ERROR = None
except ImportError as _e:
    # Deliberately NOT fatal at import time: this module gets imported
    # the instant dungeons_ap_client.py's `launch` is resolved (see its
    # top-of-file comment) - including in the Archipelago Launcher's own
    # process, just to look the function up, BEFORE any subprocess is
    # spawned. If pymem being missing crashed on import, clicking the
    # client in the Launcher could fail silently right there, which is
    # indistinguishable from "the client won't open" - exactly the bug
    # this guard exists to avoid. The real failure only happens when
    # attach() is actually called (see its own check below), by which
    # point main()'s try/except can print something the user can see.
    pymem = None
    PYMEM_IMPORT_ERROR = _e
import json
import os
import struct
import sys
import time

# See dungeons_ap_client.py's matching comment - needed so the plain
# `from _apworld_data import ...` below still resolves when this module
# is reached via a dotted import (mcdungeons.client.dungeons_reader),
# not just when run/imported directly out of this folder.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _apworld_data import Locations as _apw_locations, ZoneData as _apw_zonedata
from item_lookup import ITEM_TABLE

PROCESS_NAME = "Dungeons.exe"  # adjust if needed (or Dungeons-Win64-Shipping.exe)


def load_lookup(filename, defaults=None):
    """Loads a {int: str} lookup table from a JSON file next to this script.
    First run (file doesn't exist yet) seeds it from `defaults` and creates
    the file - after that, every confirmed entry persists automatically,
    no editing this script or getting a new copy required."""
    if os.path.exists(filename):
        with open(filename) as f:
            content = f.read().strip()
        if not content:
            raw = {}
        else:
            try:
                raw = json.loads(content)
            except json.JSONDecodeError:
                print(f"Warning: {filename} exists but isn't valid JSON (empty or corrupted) - starting fresh.")
                raw = {}
        return {int(k): v for k, v in raw.items()}
    data = dict(defaults) if defaults else {}
    save_lookup(filename, data)
    return data


def save_lookup(filename, data):
    with open(filename, "w") as f:
        json.dump({str(k): v for k, v in data.items()}, f, indent=2, sort_keys=True)


def interactive_review(unknowns, lookup, lookup_filename, kind_label, candidates_fn=None, reference_list=None):
    """After a watch session ends (or the default inventory read finishes),
    offers to label anything unrecognised seen this run. Saves after EVERY
    answer, so stopping partway through keeps whatever you already did.
    Never blocks during actual gameplay - only called once a loop exits.

    reference_list: an optional full authoritative name list (e.g. every
    real zone name from the game's own ELevelNames enum) printed once
    before labeling starts - useful when we don't have a per-index formula
    but do have the complete, real set of valid answers to pick from."""
    unlabeled = sorted(i for i in unknowns if i not in lookup)
    if not unlabeled:
        return
    print(f"\n{len(unlabeled)} unidentified {kind_label} index(es) this session.")
    if input("Label any now? (y/n): ").strip().lower() != "y":
        return
    if reference_list:
        print(f"\nReference - every real {kind_label} name from the game's own data:")
        for i in range(0, len(reference_list), 6):
            print("  " + ", ".join(reference_list[i:i + 6]))
        print()
    for idx in unlabeled:
        hint = ""
        if candidates_fn:
            candidates = candidates_fn(idx)
            if candidates:
                hint = f"  (maybe: {', '.join(candidates)})"
        label = input(f"  index {idx}{hint} - what is it? (blank to skip): ").strip()
        if label:
            lookup[idx] = label
            save_lookup(lookup_filename, lookup)
            print("    saved.")
    print("Done - saved entries are permanent, no need to re-identify.\n")

OFFSETS = {
    "gworld": 0x47540B0,
    "gobjects": 0x46556C8,  # FUObjectArray struct itself (from Dumper-7's OffsetsInfo.json
                             # OFFSET_GOBJECTS=73750216) - confirmed same build/base scheme
                             # as gworld above (Dumper-7's OFFSET_GWORLD matched exactly).
    "persistent_level": 0x30,
    "game_instance": 0x160,
    "local_players": 0x38,
    "player_controller": 0x30,
    "pawn": 0x360,
    "item_stash": 0xEC0,
    "inventory_slots": 0x300,
    "slot_item": 0x30,
    "item_struct": 0x28,
    "item_power": 0x14,
    "rarity": 0x38,
    "item_id_struct": 0x0,
    "serialized_id": 0xC,
    # Found via scan_identity_field(): a SECOND 4-byte index living at the
    # very start of item_id_struct (item_struct+0x0, same base address as
    # item_id_struct itself) - matches serialized_id for every naturally
    # created item, but write_item() never touched it. Naturally-spawned
    # items have both copies agreeing; items we'd overwritten via give_reward
    # were the one group excluded from scan_identity_field's own output,
    # because their two copies had gone out of sync - strong evidence this
    # is what the UI actually reads for icon/name, separate from whatever
    # reads serialized_id.
    "display_id": 0x0,
}

# ------------------------------------------------------------
# FNamePool - external, read-only resolution of real name STRINGS
# from a class_name_index, instead of re-guessing numeric indices
# fresh every session (which is what broke watch_chests after a game
# restart). This is the same technique every public UE4.23+ SDK dumper
# uses internally, including Dumper-7 itself - done here purely via
# ReadProcessMemory through pymem. No DLL, no injection, nothing runs
# inside the game process.
#
# WHY THIS IS NEEDED: OffsetsInfo.json shows OFFSET_GNAMES: 0 - Dumper-7
# couldn't auto-locate the name pool for this build (a known gap on some
# UE4.25/4.26 minor versions). The pool still exists in memory; we just
# have to find its base address ourselves instead of trusting a dumped
# offset - see find_gnames_candidates / verify_gnames below.
# ------------------------------------------------------------

FNAME_BLOCK_OFFSET_BITS = 16   # standard since UE4.23
FNAME_ENTRY_STRIDE = 2         # FNameEntry alignment - 2-byte aligned since UE4.23
FNAME_MAX_BLOCKS = 8192        # capacity of FNamePool::Blocks[]

# Set this once you've found and verified the real address (see
# verify_gnames mode). Persists in gnames_address.json so you don't have
# to re-find it by hand every session unless it actually moves (ASLR
# means the raw address WILL differ per launch even if the technique to
# re-derive it stays the same - see the note in verify_gnames).
_gnames_cfg = load_lookup("gnames_address.json", {})
OFFSETS["gnames"] = _gnames_cfg.get(0)  # stored as {"0": <address>} - single value, reusing load_lookup's {int: X} shape


def resolve_fname(pm, comparison_index, pool_base=None, max_len=1024):
    """Reads a real name string (e.g. "ChestActor") straight out of the
    engine's own FNamePool for a given comparison_index (the same value
    used everywhere else in this script as class_name_index). Returns
    None on any failure - most commonly because pool_base is wrong, or
    hasn't been found/verified yet (see verify_gnames)."""
    pool_base = pool_base if pool_base is not None else OFFSETS.get("gnames")
    if not pool_base:
        return None
    try:
        block_index = comparison_index >> FNAME_BLOCK_OFFSET_BITS
        offset_in_block = (comparison_index & ((1 << FNAME_BLOCK_OFFSET_BITS) - 1)) * FNAME_ENTRY_STRIDE
        if not (0 <= block_index < FNAME_MAX_BLOCKS):
            return None

        # FNamePool layout: Lock(8 bytes, FRWLock) + CurrentBlock(4) +
        # CurrentByteCursor(4) + Blocks[FNAME_MAX_BLOCKS] (uint8_t* each)
        blocks_array_addr = pool_base + 0x10
        block_ptr = pm.read_longlong(blocks_array_addr + block_index * 8)
        if not block_ptr:
            return None

        entry_addr = block_ptr + offset_in_block
        header = pm.read_ushort(entry_addr)
        is_wide = bool(header & 1)
        length = header >> 6  # standard FNameEntryHeader layout (6 bits flags, 10 bits length) -
                               # if resolved strings come back garbled, this bit split is the
                               # first thing to double check against this exact engine minor version

        if not (0 < length <= max_len):
            return None

        raw = pm.read_bytes(entry_addr + 2, length * (2 if is_wide else 1))
        return raw.decode("utf-16-le" if is_wide else "latin-1", errors="replace")
    except Exception:
        return None

# Confirmed via the find_gamebp diagnostic: class_name_index 182042 is a
# per-level singleton actor (exactly 1 instance per zone, like AGameBP)
# whose byte at +0x6B8 gave 1/Squid Coast, 2/Creeper Woods, 4/Soggy Swamp
# across three different missions in the same session - small, distinct,
# never-repeating values, matching an ELevelNames enum ordinal. This
# replaces the earlier (wrong) attempt to read the mission field off
# GameState - that field actually lives on this separate per-level actor,
# not GameState itself.
#
# CAVEAT: class_name_index values come from this game build/session's
# FName table ordering. If a game update or a future session ever shows
# this index resolving to something that doesn't look like a real mission
# (e.g. every zone reports the same value again, or wildly large numbers),
# re-run find_gamebp in 2-3 fresh missions and update this constant.
OFFSETS["gamebp_class_index"] = 182042
OFFSETS["mission_field"] = 0x6B8   # byte offset on that actor

# Confirmed directly from Dungeons_classes.hpp (Dumper-7 SDK dump) - real
# offsets, not guessed/scanned. APlayerCharacter::WalletComponent, then
# UWalletComponent::mCurrencySlots (TArray<UItemSlot*>), then
# UItemSlot::Count (int32) - this is the actual emerald/gold balance path.
OFFSETS["wallet_component"] = 0xE78   # APlayerCharacter -> UWalletComponent*
OFFSETS["currency_slots"] = 0x130     # UWalletComponent -> TArray<UItemSlot*> mCurrencySlots
OFFSETS["slot_count"] = 0x1FC         # UItemSlot -> int32 Count

# Confirmed from Dungeons_classes.hpp: AChestActor (StaticName "ChestActor")
# has these fields directly on the actor itself - once you've identified
# which class_name_index is ChestActor (via survey), this lets you read
# each individual chest's real opened/discovered state instead of just
# counting instances.
OFFSETS["chest_type"] = 0x330        # EChestType (uint8)

# Confirmed from ClassesInfo.json: AActor::RootComponent (a USceneComponent*)
# and USceneComponent::RelativeLocation (FVector, 3 floats) - lets us tell
# two instances of the same class apart by where they actually are.
OFFSETS["root_component"] = 344   # AActor -> USceneComponent*
OFFSETS["relative_location"] = 356  # USceneComponent -> FVector (X,Y,Z)
OFFSETS["entity_type"] = 0x0BA0   # AMobCharacter::EntityType (uint32) - confirmed from Dumper-7 SDK dump.
                                   # Direct, unambiguous mob/boss identity - no FName resolution or
                                   # empirical labeling needed. Only present on AMobCharacter (enemies),
                                   # not on the player's APlayerCharacter.


def get_actor_location(pm, actor_address):
    """Returns (x, y, z) for any actor, or None if it has no usable
    RootComponent right now. Technically RelativeLocation is relative to
    AttachParent, not guaranteed world-space, but for telling two
    instances of the same class apart (which is all this is for) that
    distinction doesn't matter - different chests will have different
    values here regardless."""
    try:
        root = pm.read_longlong(actor_address + OFFSETS["root_component"])
        if not root:
            return None
        x = pm.read_float(root + OFFSETS["relative_location"])
        y = pm.read_float(root + OFFSETS["relative_location"] + 4)
        z = pm.read_float(root + OFFSETS["relative_location"] + 8)
        return (x, y, z)
    except Exception:
        return None

OFFSETS["chest_opened"] = 0x331      # bool
OFFSETS["chest_discovered"] = 0x332  # bool

# AInstantTravelActor.openDoor (confirmed via ClassesInfo.json: offset 848
# decimal = 0x350). This is a BASE class field shared by every travel-door
# subclass (ABP_TravelDoor_Base_C, ABP_GenericTravelDoor_C,
# ABP_TravelDoor_Cave_C, and any other AInstantTravelActor-derived actor,
# including whichever variant is placed as a mission's end/exit door) -
# Blueprint subclasses only APPEND fields, so this offset is the same
# across all of them. Same flip-on-interact pattern as chest_opened above,
# just for doors instead.
OFFSETS["door_open"] = 0x350  # bool

# UUMG_MissionEndWidget_C.Victory (confirmed via UMG_MissionEndWidget_classes.hpp:
# offset 0x035C, right after SpawnTime at 0x0358). This is THE definitive signal:
# the same widget class is used for both a real mission completion and a
# failure screen, distinguished only by this bool - True on victory, False
# on failure/quit. No need to separately test "does this class only appear
# on a win" - just check this field once the widget's class is confirmed.
OFFSETS["mission_end_victory"] = 0x35C  # bool

# Extra cross-validation fields on the same UUMG_MissionEndWidget_C, from the
# same SDK dump: SpawnTime (float, right before Victory) and WaitDuration
# (float, after Mission_end_mix/SubtitlesWidget pointers). A random unrelated
# class landing on Victory==False by coincidence (a lone zero byte is common)
# is much less likely to ALSO show sane small positive floats at both of
# these offsets at the same time - three independent coincidences instead of
# one, which is what actually narrows 27 candidates down to the real one.
OFFSETS["mission_end_spawn_time"] = 0x358      # float
OFFSETS["mission_end_wait_duration"] = 0x370   # float

# UUMG_YouDiedTotemLives_C.UMG_TotemLivesCounter (offset 592 = 0x250) - a
# pointer to a sub-widget. No bool/float to cross-validate against on this
# class (all its fields are sub-widget pointers), so the check here is
# weaker: just "does this look like a real pointer" rather than a value
# range check like mission_end's SpawnTime/WaitDuration.
OFFSETS["totem_lives_counter_ptr"] = 0x250  # pointer

# ABP_PlayerCharacter_C.HealthComponent -> UHealthComponent.Health, confirmed
# via ClassesInfo.json. Writing 0.0 to Health lets the game's own damage/
# death/totem-consumption logic run naturally (same path a real fatal hit
# takes), rather than trying to fake a "lose a totem" call directly - which
# would need calling a UFunction via ProcessEvent, a much bigger feature
# this script doesn't have anywhere else. This is used ONLY for DeathLink
# (see watch_deathlink) - it's a real memory WRITE, unlike everything else
# in this script so far, which only reads.
OFFSETS["player_health_component"] = 0x11E8  # ABP_PlayerCharacter_C.HealthComponent (pointer)
OFFSETS["health_component_health"] = 0xFC    # UHealthComponent.Health (float)
OFFSETS["health_component_max_health"] = 0x100    # UHealthComponent.MaxHealth (float)
OFFSETS["health_component_barrier"] = 0x104        # UHealthComponent.Barrier (float) - absorbs
                                                    # damage before Health does, if present
OFFSETS["health_component_resist_death"] = 0x114   # UHealthComponent.ResistDeath (bool)
OFFSETS["health_component_invincible"] = 0x115     # UHealthComponent.Invincible (bool) - if true,
                                                    # damage won't reduce Health at all

# UUMG_YouDiedHUD_C - the real "GAME OVER" HUD widget (Dumper-7 SDK dump,
# UMG_YouDiedHud package/UMG_YouDiedHud_classes.hpp). Two fixed, compiled-in
# offsets used to FIND and then WATCH this widget WITHOUT ever needing a
# GNames-pool class-name lookup or a manually-identified/saved
# class_name_index (that index is an FName pool ordinal that's per-session
# and requires either GNames verification or a one-time interactive
# identify_* step - see the now-removed TOTEM_LOST_WIDGET_CLASS approach).
# Instead this widget is found by STRUCTURAL fingerprint: scan newly-
# appeared GObjects pointers (same cheap diff already used for mission-end
# detection) and check whether the candidate's own PlayerCharacter field
# equals the pawn we already know from get_pawn() - a real, if incidental,
# unique-enough signature with zero extra setup. Once found, its address
# is cached in-memory for the session (see find_you_died_hud /
# YOU_DIED_HUD_PLAYERCHARACTER_OFFSET/YOU_DIED_HUD_GAMEOVER_OFFSET usage in
# dungeons_ap_client.py) - nothing is persisted to disk, since a widget
# address is only valid for the current level/attach anyway and would need
# rediscovering next session regardless.
OFFSETS["you_died_hud_playercharacter"] = 0x310   # UUMG_YouDiedHUD_C.PlayerCharacter (pointer)
OFFSETS["you_died_hud_gameover"] = 0x320           # UUMG_YouDiedHUD_C.gameover (bool) - flips
                                                    # True the instant the death screen shows;
                                                    # this IS the real death signal the game
                                                    # itself uses (see SetGameOver/
                                                    # OnGameOverChanged in the SDK dump), not a
                                                    # heuristic - so this replaces the health-dip
                                                    # heuristic as DeathLink's primary trigger.
OFFSETS["you_died_hud_lives"] = 0x32C              # UUMG_YouDiedHUD_C.Lives (int32) - logging only

# GAS (GameplayAbilitySystem) health chain - the likely REAL live health for
# the player, since UHealthComponent.Health above stayed frozen at
# CDO-looking defaults (500/500) even while visibly damaged in-game.
# Confirmed via ClassesInfo.json:
#   ABaseCharacter.AbilitySystem -> UDungeonsAbilitySystemComponent*
#   UAbilitySystemComponent.SpawnedAttributes -> TArray<UAttributeSet*>
#   UHealthAttributeSet.Health / MaxHealth (float) - one entry in that array
OFFSETS["ability_system_component"] = 0x8B0   # ABaseCharacter.AbilitySystem (pointer)
OFFSETS["spawned_attributes_array"] = 0x188   # UAbilitySystemComponent.SpawnedAttributes (TArray)
OFFSETS["health_attr_health"] = 0x34          # UHealthAttributeSet.Health (float)
OFFSETS["health_attr_max_health"] = 0x38      # UHealthAttributeSet.MaxHealth (float)

# AMissionProgressHandler - a real AInfo (AActor) placed in the mission
# level itself, one per active mission, existing for the WHOLE mission
# (unlike the transient reward-screen widgets, which turned out unreliable
# across sessions due to GObjects slot reuse / FName index instability).
# Confirmed via Dungeons_classes.hpp. Since it's a normal level actor,
# it's found the same reliable way as chests/doors (scan_full_zone), not
# by diffing GObjects.
OFFSETS["mission_progress_is_visible"] = 0x3A0   # bool
OFFSETS["mission_progress_count"] = 0x42C        # int32

HEALTH_ATTRIBUTE_SET_LOOKUP_FILE = "health_attribute_set_class.json"


def _load_health_attribute_set_class():
    if os.path.exists(HEALTH_ATTRIBUTE_SET_LOOKUP_FILE):
        with open(HEALTH_ATTRIBUTE_SET_LOOKUP_FILE) as f:
            content = f.read().strip()
        if content:
            try:
                return json.loads(content).get("class_name_index")
            except json.JSONDecodeError:
                print(f"Warning: {HEALTH_ATTRIBUTE_SET_LOOKUP_FILE} isn't valid JSON - starting fresh.")
    return None


def _save_health_attribute_set_class(class_name_index):
    with open(HEALTH_ATTRIBUTE_SET_LOOKUP_FILE, "w") as f:
        json.dump({"class_name_index": class_name_index}, f, indent=2)


HEALTH_ATTRIBUTE_SET_CLASS = _load_health_attribute_set_class()


# ------------------------------------------------------------
# UObject reflection layout - confirmed via CoreUObject_classes.hpp
# (Dumper-7's own SDK core, not game-specific - these are standard UE4.22
# engine offsets). This is what class_name_index reads (+0x10, +0x18) have
# relied on all along, now with a name behind every number:
#   UObject:  VTable+0x00  Flags+0x08  Index+0x0C  Class+0x10  Name+0x18  Outer+0x20
#   UField  (: UObject):   Next+0x28
#   UStruct (: UField):    SuperStruct+0x40  Children+0x48  Size+0x50
#   UFunction (: UStruct): FunctionFlags+0x98  ExecFunction(native fn ptr)+0xC0
# EFunctionFlags bits used below: Final=0x1, Native=0x400, Public=0x20000
# (confirmed via Basic.hpp's EFunctionFlags enum).
# ------------------------------------------------------------
OFFSETS["uobject_class"] = 0x10
OFFSETS["uobject_name"] = 0x18
OFFSETS["uobject_outer"] = 0x20
OFFSETS["ufield_next"] = 0x28
OFFSETS["ustruct_super"] = 0x40
OFFSETS["ustruct_children"] = 0x48
OFFSETS["ustruct_size"] = 0x50
OFFSETS["ufunction_flags"] = 0x98
OFFSETS["ufunction_exec"] = 0xC0

FUNC_FINAL = 0x00000001
FUNC_NATIVE = 0x00000400
FUNC_PUBLIC = 0x00020000
FUNC_CONST = 0x40000000  # confirmed real bit, not just a C++ notation - Kill()'s
                          # own comment "(Final, Native, Public, Const)" means all
                          # four are actually set on its FunctionFlags


def walk_super_chain(pm, class_ptr, max_depth=32):
    """Yields class_ptr, then each UStruct.SuperStruct up the inheritance
    chain, until null or max_depth (safety cap against a corrupt chain
    looping forever)."""
    seen = set()
    current = class_ptr
    depth = 0
    while current and depth < max_depth and current not in seen:
        seen.add(current)
        yield current
        try:
            current = pm.read_longlong(current + OFFSETS["ustruct_super"])
        except Exception:
            break
        depth += 1


def walk_children(pm, struct_ptr, max_count=512):
    """Yields every UField* declared directly on this UStruct (its OWN
    Children linked list via UField.Next) - does NOT include inherited
    members from SuperStruct, that's why callers combine this with
    walk_super_chain to search a whole hierarchy."""
    try:
        current = pm.read_longlong(struct_ptr + OFFSETS["ustruct_children"])
    except Exception:
        return
    count = 0
    seen = set()
    while current and count < max_count and current not in seen:
        seen.add(current)
        yield current
        try:
            current = pm.read_longlong(current + OFFSETS["ufield_next"])
        except Exception:
            break
        count += 1


def find_zero_param_native_final_functions(pm, class_ptr):
    """Searches the WHOLE super chain starting at class_ptr for UFunctions
    matching: Size==0 (no parameters/return value at all - Kill() takes
    none) AND FunctionFlags has Final|Native|Public|Const all set (checked via a
    bitmask, not exact equality - other stray bits may be present), matching
    ABaseCharacter::Kill()'s documented flags "(Final, Native, Public,
    Const)". Size==0 already rules out the vast majority of functions
    (most take or return something), so this is a small, well-justified
    candidate set even without name resolution - but still needs a live
    test to confirm which (if more than one) is really Kill.
    Returns [(function_ptr, owning_class_ptr, super_chain_depth), ...]."""
    candidates = []
    for depth, level_class in enumerate(walk_super_chain(pm, class_ptr)):
        for field in walk_children(pm, level_class):
            try:
                size = pm.read_int(field + OFFSETS["ustruct_size"])
                flags = pm.read_uint(field + OFFSETS["ufunction_flags"])
            except Exception:
                continue
            required_bits = FUNC_FINAL | FUNC_NATIVE | FUNC_PUBLIC | FUNC_CONST
            if size == 0 and (flags & required_bits) == required_bits:
                candidates.append((field, level_class, depth))
    return candidates


def get_spawned_attributes(pm, pawn):
    """Returns [(attr_set_ptr, class_name_index), ...] from the pawn's
    AbilitySystemComponent.SpawnedAttributes array. Small array (one entry
    per attribute set type the character uses), safe to fully read every
    call - no need for the bulk-chunk-read approach GObjects needed."""
    try:
        asc = pm.read_longlong(pawn + OFFSETS["ability_system_component"])
        if not asc:
            return []
        array_header = asc + OFFSETS["spawned_attributes_array"]
        data_ptr = pm.read_longlong(array_header)
        count = pm.read_int(array_header + 0x8)
        if not data_ptr or count <= 0 or count > 64:  # sanity cap
            return []
        results = []
        for i in range(count):
            attr_set = pm.read_longlong(data_ptr + i * 8)
            if not attr_set:
                continue
            cls = get_uobject_class_name_index(pm, attr_set)
            results.append((attr_set, cls))
        return results
    except Exception:
        return []


def read_door_state(pm, actor_address):
    """Reads AInstantTravelActor's own openDoor field directly - openDoor
    is just a lone bool with no validating enum alongside it, so a byte
    of 0 or 1 here is weaker evidence of 'this really is a door' than a
    richer field would be. Use the confirm-by-diff workflow
    (confirm_end_door) rather than trusting any single read in
    isolation."""
    try:
        open_door = pm.read_uchar(actor_address + OFFSETS["door_open"])
        return {"open": bool(open_door)}
    except Exception as e:
        return {"error": str(e)}

# name_lookup.json / NAME_LOOKUP removed - item_lookup.py's ITEM_TABLE is the
# complete, static reference table for all 269 items (see that file's own
# docstring), so there's no need to build up a separate "items we've
# personally seen" table at runtime any more. NAME_LOOKUP is kept as a thin
# alias (name_index -> name string) so the rest of this file didn't need to
# change its lookup calls.
NAME_LOOKUP = {idx: info["name"] for idx, info in ITEM_TABLE.items()}

# Empirical rule: if the datamined id string for an item contains "Unique",
# the item is always Unique rarity - confirmed by observation, not derived
# from an offset. Doesn't change how NAME_LOOKUP/PREDICTED work, but worth
# knowing when eyeballing suggest_candidates() output - if every candidate
# for an index has "_Unique" in it, the true rarity is Unique regardless of
# what the rarity byte happens to say.

RARITY_NAMES = {0: "Common", 1: "Rare", 2: "Unique"}

# ------------------------------------------------------------
# Candidate suggestion for unrecognised indices - not a lookup, a guess.
# Derived from a datamined item list (item_id_order.json) plus a formula
# fitted against confirmed indices: real item names are registered into
# the game's internal name table in roughly the same order they appear in
# this data table, so an item's position predicts its index fairly closely
# (usually within a few, sometimes exact). Verified against all 11 known
# items so far - correct answer landed in the top 3 every single time.
# ------------------------------------------------------------
SLOPE, INTERCEPT = 7.123, 3085.54
try:
    import json
    with open("item_id_order.json") as f:
        _ID_ORDER = json.load(f)
    PREDICTED = {sid: SLOPE * pos + INTERCEPT for pos, sid in enumerate(_ID_ORDER)}
except FileNotFoundError:
    PREDICTED = {}


def suggest_candidates(unknown_index, top_n=3):
    if not PREDICTED:
        return []
    ranked = sorted(PREDICTED.items(), key=lambda kv: abs(kv[1] - unknown_index))
    return [sid for sid, _ in ranked[:top_n]]


# ------------------------------------------------------------
# World drop detection (chests, mobs, shops)
# ------------------------------------------------------------
# GNames is still unresolved for this build, so we can't turn a live
# actor's class into a readable string the way the offline GObjects dump
# could. What we CAN do without that: read each actor's class object's own
# FName index (same UObject::Name offset used everywhere else, just
# applied to the class object instead of an item), and use a before/after
# snapshot diff. Trigger a drop (open the chest / kill the mob / buy from
# the shop) between two snapshots, and whatever actor shows up in "after"
# but not "before" is almost certainly the dropped item.
#
# Caveat: this catches ANY newly spawned actor, not just pickups - a death
# effect, a projectile, etc. could also show up. Best used standing still,
# not mid-fight, right at the moment of the drop.
#
# Once you confirm what a class_name_index actually is (do the diff, then
# check the item's name/rarity in-game), add it to PICKUP_CLASS_LOOKUP -
# same workflow as NAME_LOOKUP above.

OFFSETS["level_actors"] = 0x98  # ULevel::Actors, from Dumper-7: Off::InSDK::ULevel::Actors

# ------------------------------------------------------------
# Level streaming - persistent level alone is NOT the whole zone
# ------------------------------------------------------------
# Confirmed from real data: Creeper Woods showed only ~50 actors via
# scan_world_actors() (which only walks the persistent level) - far too
# few for an actual zone (should be hundreds: chests, mobs, foliage,
# props). That's the signature of level streaming - UWorld keeps a
# StreamingLevels array of ULevelStreaming objects, each wrapping its own
# already-loaded ULevel (with its own separate Actors TArray) for a piece
# of the map. The persistent level only holds always-loaded actors
# (player start, lighting, etc.) - the real zone content lives in these
# sub-levels instead.
#
# STILL TBD - get these two from your Dumper-7 SDK dump:
#   - UWorld::StreamingLevels offset      (search "class UWorld" in
#     Engine_classes.hpp - it's a TArray<ULevelStreaming*>)
#   - ULevelStreaming's loaded-level ptr   (search "class ULevelStreaming"
#     - look for a ULevel* member, commonly named LoadedLevel)
# Until both are filled in, scan_full_zone() falls back to persistent-level
# actors only (same as scan_world_actors) - nothing breaks, it just won't
# see the full zone yet.

OFFSETS["streaming_levels"] = None  # TArray<ULevelStreaming*> on UWorld - TBD
OFFSETS["loaded_level"] = None      # ULevel* on ULevelStreaming - TBD


def get_streaming_sublevels(pm, world):
    """Returns a list of ULevel addresses for every currently-loaded
    streaming sub-level. Empty list until the two TBD offsets above are
    filled in."""
    if OFFSETS["streaming_levels"] is None or OFFSETS["loaded_level"] is None:
        return []

    tarray_header = world + OFFSETS["streaming_levels"]
    try:
        data_ptr = pm.read_longlong(tarray_header)
        count = pm.read_int(tarray_header + 0x8)
    except Exception:
        return []
    if not data_ptr or count <= 0:
        return []

    sublevels = []
    for i in range(count):
        try:
            streaming_obj = pm.read_longlong(data_ptr + i * 8)
            if not streaming_obj:
                continue
            loaded_level = pm.read_longlong(streaming_obj + OFFSETS["loaded_level"])
            if loaded_level:
                sublevels.append(loaded_level)
        except Exception:
            continue
    return sublevels


def scan_level_actors(pm, level, max_actors=4000):
    """Same TArray walk as scan_world_actors, but takes a ULevel address
    directly - reused for both the persistent level and each streaming
    sub-level."""
    tarray_header = level + OFFSETS["level_actors"]
    try:
        data_ptr = pm.read_longlong(tarray_header)
        count = pm.read_int(tarray_header + 0x8)
    except Exception:
        return {}
    if not data_ptr or count <= 0:
        return {}

    snapshot = {}
    for i in range(min(count, max_actors)):
        try:
            actor = pm.read_longlong(data_ptr + i * 8)
            if not actor:
                continue
            actor_class = pm.read_longlong(actor + 0x10)
            if not actor_class:
                continue
            class_name_index = pm.read_int(actor_class + 0x18)
            snapshot[actor] = class_name_index
        except Exception:
            continue
    return snapshot


# ------------------------------------------------------------
# GObjects (FUObjectArray) - the engine's global table of EVERY live
# UObject, not just AActor instances in the current level. scan_full_zone
# below only walks PersistentLevel->Actors, which is why it can never see
# UMG widgets (popups, HUD elements, menu screens) - those live here
# instead. UE4.22 layout (confirmed via OffsetsInfo.json's OFFSET_GWORLD
# matching OFFSETS["gworld"] exactly, so this is the same build/base
# scheme): base+OFFSETS["gobjects"] points DIRECTLY at ObjObjects
# (FChunkedFixedUObjectArray) - confirmed empirically via debug_gobjects
# (NumElements read back as a sane ~735887; MaxElements=2162688 exactly
# equals MaxChunks(33)*0x10000, confirming the field boundaries below):
#   +0x00  FUObjectItem** Objects        (chunk pointer array)
#   +0x08  FUObjectItem*  PreAllocatedObjects
#   +0x10  int32 MaxElements
#   +0x14  int32 NumElements
#   +0x18  int32 MaxChunks
#   +0x1C  int32 NumChunks
# Each chunk holds up to 0x10000 (65536) FUObjectItem entries, each 0x18
# bytes (UObject* Object; int32 Flags; int32 ClusterRootIndex;
# int32 SerialNumber; padded to 24). Object pointer is the item's first
# 8 bytes, so item_addr itself can be read directly as that pointer.
_GOBJECTS_CHUNK_SIZE = 0x10000
_GOBJECTS_ITEM_SIZE = 0x18


def get_gobjects_pointer_snapshot(pm, base):
    """Bulk-reads EVERY live object pointer (index -> UObject* or 0) in one
    pass, chunk by chunk, instead of one read per index - needed because
    transient UObjects (like a popup widget) very often reuse a freed slot
    SOMEWHERE in the array rather than appending past the current
    NumElements, so comparing NumElements alone (or only scanning the tail)
    misses most of them. A full snapshot diff (see identify_mission_end_widget)
    catches slot reuse too, not just array growth.
    Returns {index: obj_ptr} for every index up to NumElements. Uses one
    read_bytes() per chunk (up to ~1.5MB) instead of thousands of tiny
    reads, so this stays fast even across ~650k+ entries."""
    import struct
    snapshot = {}
    try:
        chunks_ptr = pm.read_longlong(base + OFFSETS["gobjects"] + 0x00)
        num_elements = pm.read_int(base + OFFSETS["gobjects"] + 0x14)
        num_chunks = pm.read_int(base + OFFSETS["gobjects"] + 0x1C)
        if not chunks_ptr or not num_elements or not num_chunks:
            return snapshot
        for chunk_i in range(num_chunks):
            base_index = chunk_i * _GOBJECTS_CHUNK_SIZE
            if base_index >= num_elements:
                break
            items_in_chunk = min(_GOBJECTS_CHUNK_SIZE, num_elements - base_index)
            try:
                chunk_ptr = pm.read_longlong(chunks_ptr + chunk_i * 8)
            except Exception:
                continue
            if not chunk_ptr:
                continue
            try:
                raw = pm.read_bytes(chunk_ptr, items_in_chunk * _GOBJECTS_ITEM_SIZE)
            except Exception:
                continue
            for j in range(items_in_chunk):
                off = j * _GOBJECTS_ITEM_SIZE
                obj_ptr = struct.unpack_from("<Q", raw, off)[0]
                snapshot[base_index + j] = obj_ptr
    except Exception:
        pass
    return snapshot


def get_uobject_class_name_index(pm, obj):
    """Same UObject header layout already relied on elsewhere in this
    script (actor+0x10 = ClassPrivate, class+0x18 = Name.ComparisonIndex)
    - applies to ANY UObject, not just AActor instances, since it's the
    universal UObject header, not something AActor adds."""
    try:
        obj_class = pm.read_longlong(obj + 0x10)
        if not obj_class:
            return None
        return pm.read_int(obj_class + 0x18)
    except Exception:
        return None


MISSION_END_WIDGET_LOOKUP_FILE = "mission_end_widget_class.json"


def _load_mission_end_widget_class():
    """Single global class_name_index (not per-zone like END_CHEST_CLASS_LOOKUP) -
    UUMG_MissionEndWidget_C is the same class everywhere, only its
    instances are per-mission and transient."""
    if os.path.exists(MISSION_END_WIDGET_LOOKUP_FILE):
        with open(MISSION_END_WIDGET_LOOKUP_FILE) as f:
            content = f.read().strip()
        if content:
            try:
                return json.loads(content).get("class_name_index")
            except json.JSONDecodeError:
                print(f"Warning: {MISSION_END_WIDGET_LOOKUP_FILE} isn't valid JSON - starting fresh.")
    return None


def _save_mission_end_widget_class(class_name_index):
    with open(MISSION_END_WIDGET_LOOKUP_FILE, "w") as f:
        json.dump({"class_name_index": class_name_index}, f, indent=2)


MISSION_END_WIDGET_CLASS = _load_mission_end_widget_class()

# Location IDs for the "<Mission> - Mission Complete" locations, read
# straight from the real Locations.py (via _apworld_data) instead of a
# hand-maintained literal table - can never drift out of sync with the
# generator, and (unlike the old base-game-only hardcoded version) covers
# every zone Locations.py knows about: base game, every DLC pack, and the
# three Ancient Hunts.
MISSION_LOCATION_IDS = {
    zone_name: _apw_locations.LOCATION_TABLE[location_name]
    for zone_name, location_name in _apw_locations.LOCATIONS_BY_ZONE.items()
}

# Item IDs for the "<Mission> Access" progression items. Items.py itself
# can't be loaded client-side (it imports BaseClasses, which needs a full
# Archipelago install) - so this mirrors its _alloc_id() allocation
# formula (BASE_ITEM_ID=0xDC0000, +1 per zone, MISSION_ACCESS_ITEMS built
# first from ZONES in order - see Items.py) rather than importing it
# directly. This is the one part of the reader that's still fragile to
# Items.py's internals: if Items.py's allocation order ever changes (a
# new item category inserted before MISSION_ACCESS_ITEMS, or ZONES
# reordered), this must be updated to match. It's sourced from the real,
# live ZoneData.py though, so at least the ZONE LIST itself (which zones
# exist, in what order) can never drift - only the *formula* is
# duplicated, not any data.
_MISSION_ACCESS_BASE_ITEM_ID = 0xDC0000
MISSION_ACCESS_ITEM_IDS = {
    z.internal_name: _MISSION_ACCESS_BASE_ITEM_ID + i + 1
    for i, z in enumerate(_apw_zonedata.ZONES)
}
ITEM_ID_TO_ZONE = {v: k for k, v in MISSION_ACCESS_ITEM_IDS.items()}

# Emerald filler item IDs ("100/300 500 Emeralds") - same fragile-mirror
# situation as MISSION_ACCESS_ITEM_IDS just above (Items.py needs
# BaseClasses, can't be loaded client-side), and allocated right after
# it in Items.py: MISSION_ACCESS_ITEMS (one per zone), then
# SECRET_ACCESS_ITEMS (one per zone where z.secret is True), THEN
# FILLER_ITEMS ("100 Emeralds", "300 Emeralds", "500 Emeralds" in that
# order). The zone count and secret-zone count both come from the live
# ZoneData.py (same as above), so only the FORMULA/ORDER is duplicated
# here, not any data - update this dict's KEYS/ORDER by hand if Items.py
# ever reorders what comes before FILLER_ITEMS, same caveat as above.
_EMERALD_FILLER_BASE_OFFSET = len(_apw_zonedata.ZONES) + sum(1 for z in _apw_zonedata.ZONES if z.secret)
EMERALD_FILLER_ITEM_IDS = {
    "100 Emeralds": _MISSION_ACCESS_BASE_ITEM_ID + _EMERALD_FILLER_BASE_OFFSET + 1,
    "300 Emeralds": _MISSION_ACCESS_BASE_ITEM_ID + _EMERALD_FILLER_BASE_OFFSET + 2,
    "500 Emeralds": _MISSION_ACCESS_BASE_ITEM_ID + _EMERALD_FILLER_BASE_OFFSET + 3,
}
ITEM_ID_TO_EMERALD_AMOUNT = {
    EMERALD_FILLER_ITEM_IDS["100 Emeralds"]: 100,
    EMERALD_FILLER_ITEM_IDS["300 Emeralds"]: 300,
    EMERALD_FILLER_ITEM_IDS["500 Emeralds"]: 500,
}

UNLOCKED_ZONES_FILE = "unlocked_zones.json"


def _load_unlocked_zones():
    """Persisted set of zones (internal_name) whose Access item has been
    received so far. Starts with just squidcoast unlocked by default (the
    hub/tutorial zone) even before any items arrive, so the player isn't
    locked out of the game entirely at the very start - adjust here if
    your world's starting inventory/logic differs."""
    if os.path.exists(UNLOCKED_ZONES_FILE):
        with open(UNLOCKED_ZONES_FILE) as f:
            content = f.read().strip()
        if content:
            try:
                return set(json.loads(content))
            except json.JSONDecodeError:
                pass
    return {"squidcoast"}


def _save_unlocked_zones(unlocked_set):
    with open(UNLOCKED_ZONES_FILE, "w") as f:
        json.dump(sorted(unlocked_set), f, indent=2)

AP_GOAL_FILE = "ap_goal_zone.json"


def _load_ap_goal_zone():
    """Which zone (internal_name) counts as the AP goal, if any - set via
    `set_ap_goal <zone>` or the --goal flag on watch_mission_end. None
    means no goal is configured; watch_mission_end will still send
    LocationChecks normally, just never send_goal_complete()."""
    if os.path.exists(AP_GOAL_FILE):
        with open(AP_GOAL_FILE) as f:
            content = f.read().strip()
        if content:
            try:
                return json.loads(content).get("goal_zone")
            except json.JSONDecodeError:
                return None
    return None


def _save_ap_goal_zone(zone_name):
    with open(AP_GOAL_FILE, "w") as f:
        json.dump({"goal_zone": zone_name}, f, indent=2)

def scan_full_zone(pm, world, max_actors=4000):
    """Persistent level + every loaded streaming sub-level, merged into
    one snapshot. This is what actually captures a whole zone's chests
    and enemies - use it instead of scan_world_actors() for anything
    zone-wide (chest/enemy surveys especially). Falls back to persistent-
    level-only if the streaming offsets aren't filled in yet, so nothing
    breaks in the meantime."""
    level = get_persistent_level(pm, world)
    if not level:
        return {}

    combined = scan_level_actors(pm, level, max_actors)
    for sublevel in get_streaming_sublevels(pm, world):
        combined.update(scan_level_actors(pm, sublevel, max_actors))
    return combined



# Found via scan_stash_diff() after a real shop purchase - UNTESTED as an
# actual UI-refresh trigger yet, just the strongest candidates from the
# diff. 0x1a0/0x1a8 both went 2->3 (paired +1 increments - classic
# version/dirty-counter shape). 0x2ac went 0->256 (a single bit turning
# on - classic needs-refresh flag shape). Everything else in that scan
# (0x40/0x44, 0x108-0x128, 0x198) looked more like elapsed-time drift or
# shop-purchase-specific transient data, so not included here.
OFFSETS["dirty_counter_a"] = 0x1A0
OFFSETS["dirty_counter_b"] = 0x1A8
OFFSETS["dirty_flag"] = 0x2AC
DIRTY_FLAG_BIT = 0x100  # the bit that turned on (0 -> 256)


def bump_ui_refresh_hint(pm, item_stash_address, mode="counters"):
    """Nudges the candidate refresh-trigger fields found by scan_dirty, to
    test whether writing them ourselves makes the UI rebuild without
    needing a storage visit or restart. mode: "counters" (increment
    0x1a0/0x1a8 by 1, our top guess), "flag" (OR the 0x100 bit into
    0x2ac), "both", or "none" (skip - useful for a clean A/B comparison).
    Returns (ok: bool, detail: str) - this is exploratory, so failures are
    reported rather than raised.
    """
    if mode == "none":
        return True, "skipped (mode=none)"

    details = []
    try:
        if mode in ("counters", "both"):
            for key in ("dirty_counter_a", "dirty_counter_b"):
                addr = item_stash_address + OFFSETS[key]
                val = pm.read_int(addr)
                pm.write_int(addr, val + 1)
                details.append(f"{key} {val} -> {val + 1}")
        if mode in ("flag", "both"):
            addr = item_stash_address + OFFSETS["dirty_flag"]
            val = pm.read_int(addr)
            pm.write_int(addr, val | DIRTY_FLAG_BIT)
            details.append(f"dirty_flag {val} -> {val | DIRTY_FLAG_BIT}")
        return True, "; ".join(details) if details else "nothing written (unrecognized mode)"
    except Exception as e:
        return False, f"refresh-hint write failed: {e}"



# Confirmed empirically. Persists in pickup_class_lookup.json.
PICKUP_CLASS_LOOKUP = load_lookup("pickup_class_lookup.json")


def get_persistent_level(pm, world):
    return pm.read_longlong(world + OFFSETS["persistent_level"])


def scan_world_actors(pm, world, max_actors=4000):
    """Snapshot every actor currently in the level.
    Returns {actor_address: class_name_index}.
    """
    level = get_persistent_level(pm, world)
    if not level:
        return {}

    tarray_header = level + OFFSETS["level_actors"]
    data_ptr = pm.read_longlong(tarray_header)
    count = pm.read_int(tarray_header + 0x8)
    if not data_ptr or count <= 0:
        return {}

    snapshot = {}
    for i in range(min(count, max_actors)):
        try:
            actor = pm.read_longlong(data_ptr + i * 8)
            if not actor:
                continue
            actor_class = pm.read_longlong(actor + 0x10)  # UObject::Class
            if not actor_class:
                continue
            class_name_index = pm.read_int(actor_class + 0x18)  # UObject::Name
            snapshot[actor] = class_name_index
        except Exception:
            continue  # actor got destroyed mid-scan, or a bad pointer - just skip it

    return snapshot


def diff_scans(before, after):
    """Actors present in `after` but not `before` - newly spawned since
    the first snapshot. This is the actual drop-detection mechanism."""
    return {addr: cls for addr, cls in after.items() if addr not in before}


def diff_scans_both_ways(before, after):
    """Like diff_scans, but also reports actors that DISAPPEARED (present
    before, gone after) - for things whose interaction destroys the actor
    entirely rather than flipping a bool (e.g. if the supply chest isn't
    AChestActor and doesn't have an 'opened' field at all, this is what
    would actually catch it)."""
    added = {addr: cls for addr, cls in after.items() if addr not in before}
    removed = {addr: cls for addr, cls in before.items() if addr not in after}
    return added, removed


def watch_for_drops(pm, world, poll_interval=1.0):
    """Continuously polls the actor list and prints any actor that appears
    since the previous poll - no manual before/after button-pressing, it
    just reports new actors as they spawn while you play. Ctrl+C to stop.

    Note on class_name_index values: these live in a completely different
    numeric range than the item NAME_LOOKUP indices (tens of thousands vs.
    low thousands) - that's expected, not a bug. Item id strings and
    blueprint class names are different FNames registered at different
    points, so there's no reason to expect them to land in the same range
    or line up with item_id_order.json. Two actors sharing the same
    class_name_index just means they're the same class (e.g. two copies
    of the same pickup blueprint).
    """
    import time

    print("Watching for new actors - play normally, open chests, kill mobs, browse shops. Ctrl+C to stop.")
    baseline = scan_world_actors(pm, world)
    print(f"Baseline: {len(baseline)} actors.\n")

    seen_unknown = set()
    try:
        while True:
            time.sleep(poll_interval)
            try:
                current = scan_world_actors(pm, world)
            except Exception:
                continue  # transient read failure - just retry next tick

            new_actors = diff_scans(baseline, current)
            for addr, class_name_index in new_actors.items():
                label = PICKUP_CLASS_LOOKUP.get(class_name_index, "unknown - go check this one in-game")
                if class_name_index not in PICKUP_CLASS_LOOKUP:
                    seen_unknown.add(class_name_index)
                print(f"  NEW actor @ {hex(addr)}  class_name_index={class_name_index}  ({label})")
            baseline = current
    except KeyboardInterrupt:
        print("\nStopped.")
        interactive_review(seen_unknown, PICKUP_CLASS_LOOKUP, "pickup_class_lookup.json", "pickup class")


# ------------------------------------------------------------
# Enemy tracking + player-death detection
# ------------------------------------------------------------
# Chest detection via memory-scan/class-lookup heuristics (CHEST_CLASS_LOOKUP,
# END_CHEST_CLASS_LOOKUP, SUPPLY_CHEST_CLASS_LOOKUP, read_chest_state, etc.)
# was removed - real chest-open detection now goes exclusively through
# dungeons_bridge.dll's ProcessEvent hook (see get_chest_open_events /
# classify_interactable_class), which reports the actor's real class name
# directly instead of guessing from per-actor byte offsets.
#
# Enemies are still just a count of actors whose class_name_index matches a
# known enemy class, confirmed the same way as before: classes with counts
# matching what you see on the enemy-count HUD element go in
# ENEMY_CLASS_LOOKUP.
ENEMY_CLASS_LOOKUP = load_lookup("enemy_class_lookup.json")

# --- Player death detection: placeholder until offset is confirmed ---
DEATH_DETECTION_READY = False
PAWN_HEALTH_OFFSET = None  # fill in once found, e.g. 0x1A8
DEATH_HEALTH_THRESHOLD = 0.0


def classify_actors(snapshot):
    """Group an actor snapshot ({addr: class_name_index}) by class, for
    figuring out which class_name_index values are enemies vs scenery.
    Print this once at level start to eyeball candidates."""
    from collections import Counter
    counts = Counter(snapshot.values())
    return counts.most_common()


def count_enemies(snapshot):
    counts = {}
    for addr, cls in snapshot.items():
        if cls in ENEMY_CLASS_LOOKUP:
            label = ENEMY_CLASS_LOOKUP[cls]
            counts[label] = counts.get(label, 0) + 1
    return counts


def get_pawn_health(pm, pawn_address):
    """Returns current player health, or None if the offset isn't
    confirmed yet or the pawn pointer is invalid."""
    if not DEATH_DETECTION_READY or PAWN_HEALTH_OFFSET is None:
        return None
    try:
        return pm.read_float(pawn_address + PAWN_HEALTH_OFFSET)
    except Exception:
        return None


def watch_level(pm, base, poll_interval=1.0):
    """All-in-one live monitor for a level (e.g. Creeper Woods):
    - enemy count by type (from ENEMY_CLASS_LOOKUP)
    - item pickups (existing NAME_LOOKUP / suggest_candidates)
    - player death, if DEATH_DETECTION_READY is set

    Enemy lookup starts empty - run this once first just to see
    classify_actors() output and fill the lookup table in, same as any
    other empirical offset in this file. Chest tracking was removed here -
    handled exclusively via the DLL's ProcessEvent hook now (see
    get_chest_open_events).
    """
    import time

    world = pm.read_longlong(base + OFFSETS["gworld"])
    if not world:
        print("No UWorld - are you in a level?")
        return

    baseline = scan_full_zone(pm, world)
    print(f"Baseline: {len(baseline)} actors.")

    if not ENEMY_CLASS_LOOKUP:
        print("\nENEMY_CLASS_LOOKUP is empty.")
        top_classes = classify_actors(baseline)[:20]
        print("Top classes by actor count:")
        for cls, n in top_classes:
            print(f"  class_name_index={cls:<8} count={n}")
        if input("\nLabel any of these now (enemy/skip)? (y/n): ").strip().lower() == "y":
            for cls, n in top_classes:
                answer = input(f"  class_name_index={cls} (count={n}) - enemy, or blank to skip: ").strip().lower()
                if answer.startswith("e"):
                    label = input("    enemy label (e.g. 'Zombie'): ").strip()
                    if label:
                        ENEMY_CLASS_LOOKUP[cls] = label
                        save_lookup("enemy_class_lookup.json", ENEMY_CLASS_LOOKUP)
            print()

    enemies = count_enemies(baseline)
    if enemies:
        print("Enemies detected:", enemies)

    print("\nWatching - play normally. Ctrl+C to stop.\n")
    was_dead = False
    pawn_address = None  # wire this up once PAWN_HEALTH_OFFSET is confirmed

    seen_unknown_pickups = set()

    try:
        while True:
            time.sleep(poll_interval)
            try:
                current = scan_full_zone(pm, world)
            except Exception:
                continue

            new_actors = diff_scans(baseline, current)
            for addr, class_name_index in new_actors.items():
                label = PICKUP_CLASS_LOOKUP.get(class_name_index)
                if label:
                    print(f"  PICKUP: {label}")
                elif class_name_index not in ENEMY_CLASS_LOOKUP:
                    seen_unknown_pickups.add(class_name_index)

            new_enemies = count_enemies(new_actors)
            if new_enemies:
                print(f"  NEW enemy spawn(s): {new_enemies}")

            if DEATH_DETECTION_READY and pawn_address:
                health = get_pawn_health(pm, pawn_address)
                if health is not None:
                    is_dead = health <= DEATH_HEALTH_THRESHOLD
                    if is_dead and not was_dead:
                        print("  *** PLAYER DIED ***")
                    was_dead = is_dead

            baseline = current
    except KeyboardInterrupt:
        print("\nStopped.")
        interactive_review(seen_unknown_pickups, PICKUP_CLASS_LOOKUP, "pickup_class_lookup.json", "pickup class")


# ------------------------------------------------------------
# Zone / level detection
# ------------------------------------------------------------
# The UWorld object has the same UObject::Name field every other object
# in this file already uses (offset 0x18 - same one read off actor
# classes for class_name_index, and off items elsewhere). So a level's
# identity is just another unresolved name index - it slots into the
# exact same "confirm once in-game, keep forever" lookup pattern as
# NAME_LOOKUP, PICKUP_CLASS_LOOKUP, ENEMY_CLASS_LOOKUP, etc.
#
# WORKFLOW:
# 1. Stand in the lobby, note the printed "unknown zone (index N)".
# 2. Travel to Creeper Woods, note the new index that appears.
# 3. Add both to ZONE_NAME_LOOKUP below - permanent from then on, no
#    need to re-identify unless the game updates.
#
# Caveat: if UWorld gets fully destroyed/recreated on a zone change
# (rather than reused), OFFSETS["gworld"] itself might momentarily read
# 0 during the loading screen - handled below by just skipping that poll
# and retrying, same as any other transient read failure in this file.

OFFSETS["world_name"] = 0x18  # UObject::Name, same field as elsewhere
OFFSETS["object_outer"] = 0x20  # UObject::Outer, confirmed from Dumper-7 console log

# Confirmed empirically. Persists in zone_name_lookup.json.
ZONE_NAME_LOOKUP = load_lookup("zone_name_lookup.json")

# The game's own complete, authoritative list of every real zone name -
# straight from the ELevelNames enum in EnumsInfo.json (Dumper-7's SDK
# dump), not scanned or guessed. We still can't map a live class_name_index
# directly to one of these (GNames is unresolved, and this enum's own
# integer values are a completely different numbering from the FName
# indices we read live), but it's the definitive reference for what to
# type when labeling an unknown zone - see interactive_review's
# reference_list param below.
try:
    with open("zone_id_order.json") as f:
        ZONE_ID_ORDER = json.load(f)
except FileNotFoundError:
    ZONE_ID_ORDER = []


def zone_naming_progress():
    """Checklist view against the full ELevelNames reference - confirmed
    vs still-unlabeled zones, so naming can be done systematically instead
    of ad-hoc as you happen to wander into each one.

    Caveat worth knowing: many entries are daily/weekly/seasonal variants
    of the same base mission (e.g. creepycrypt/creepycryptdaily/
    creepycryptweekly/creepycryptseasonal) - these may share the same
    underlying zone index as their base mission rather than being truly
    distinct, so 159/159 may not be a meaningful target; the base mission
    names and hub-type entries are the ones that matter most."""
    if not ZONE_ID_ORDER:
        print("zone_id_order.json not found - can't show a checklist without it.")
        return
    confirmed_names = set(ZONE_NAME_LOOKUP.values())
    done = [z for z in ZONE_ID_ORDER if z in confirmed_names]
    missing = [z for z in ZONE_ID_ORDER if z not in confirmed_names]

    print(f"{len(done)}/{len(ZONE_ID_ORDER)} zones confirmed so far.\n")
    if done:
        print("Confirmed:")
        for i in range(0, len(done), 8):
            print("  " + ", ".join(done[i:i + 8]))
    if missing:
        print(f"\nStill unlabeled ({len(missing)}):")
        for i in range(0, len(missing), 8):
            print("  " + ", ".join(missing[i:i + 8]))
    print("\nNote: daily/weekly/seasonal variants of a mission may share the same zone index "
          "as the base mission rather than being separately reachable - focus on base mission "
          "names and hub-type entries first, those are the ones likely to matter.")

# Same idea, for enemies - real internal mob names from community-sourced
# datamining (not scanned/guessed either). Same caveat as zones: no direct
# index mapping, just a reference to pick the right name from when
# labeling an enemy candidate.
try:
    with open("mob_id_order.json") as f:
        MOB_ID_ORDER = json.load(f)
except FileNotFoundError:
    MOB_ID_ORDER = []


_zone_index_cache = {"value": None, "checked_at": 0.0}


def get_zone_name_index(pm, world, min_interval=2.0):
    """Reads the real per-mission ELevelNames byte off the per-level
    singleton actor identified via the find_gamebp diagnostic (see the
    OFFSETS["gamebp_class_index"] comment) - NOT off GameState (that
    field doesn't live there) and NOT off the level package name (shared/
    generic across many missions in this game). Requires a full zone scan
    since this actor is identified by class_name_index, not a fixed
    pointer chain from UWorld - so results are cached for min_interval
    seconds, since callers in watch-mode loops call this every tick and a
    full actor scan every tick would be wasteful. Pass min_interval=0 to
    force a fresh read (e.g. right after detecting a zone change)."""
    now = time.time()
    if min_interval > 0 and (now - _zone_index_cache["checked_at"]) < min_interval:
        return _zone_index_cache["value"]
    from collections import Counter
    try:
        snapshot = scan_full_zone(pm, world)
        addrs = [a for a, cls in snapshot.items() if cls == OFFSETS["gamebp_class_index"]]
        if not addrs:
            value = None
        else:
            # The 3-mission sample that identified this class only ever
            # saw 1 instance, but bigger levels with streaming sublevels
            # may load more than one copy. Reading all of them and taking
            # the most common byte is safer than grabbing an arbitrary
            # one (which was flipping between unrelated values mid-level).
            bytes_read = []
            for a in addrs:
                try:
                    bytes_read.append(pm.read_uchar(a + OFFSETS["mission_field"]))
                except Exception:
                    pass
            if len(set(bytes_read)) > 1:
                print(f"Warning: {len(addrs)} instances of the mission-tracking class found, "
                      f"disagreeing values {sorted(set(bytes_read))} - using the most common. "
                      f"This class may not be reliable for this level; consider re-running "
                      f"find_gamebp here to double check.")
            value = Counter(bytes_read).most_common(1)[0][0] if bytes_read else None
    except Exception:
        value = None
    _zone_index_cache["value"] = value
    _zone_index_cache["checked_at"] = now
    return value


def zone_label(zone_index):
    if zone_index is None:
        return "(loading...)"
    return ZONE_NAME_LOOKUP.get(zone_index, f"unknown zone (index {zone_index})")


def zone_label_with_raw(zone_index):
    """Same as zone_label, but always shows the raw index alongside the
    name (even once labeled) - needed to diagnose whether a shown label
    is genuinely tied to the current raw index or a stale/incorrect one."""
    if zone_index is None:
        return "(loading...)"
    name = ZONE_NAME_LOOKUP.get(zone_index)
    if name:
        return f"{name} (raw index={zone_index})"
    return f"unknown zone (index {zone_index})"


def report_zone_snapshot(pm, baseline):
    """Print the enemy breakdown for a freshly-entered zone, plus an
    interactive labeling prompt if the lookup table is still empty.
    Chest reporting was removed - handled exclusively via the DLL's
    ProcessEvent hook now (see get_chest_open_events)."""
    if not ENEMY_CLASS_LOOKUP:
        top_classes = classify_actors(baseline)[:20]

        print("  (Top classes seen this zone - could be enemies or scenery:)")
        for cls, n in top_classes:
            print(f"    class_name_index={cls:<8} count={n}")
        if input("  Label any of these (enemy/skip)? (y/n): ").strip().lower() == "y":
            for cls, n in top_classes:
                answer = input(f"    class_name_index={cls} (count={n}) - enemy, or blank to skip: ").strip().lower()
                if answer.startswith("e"):
                    label = input("      enemy label: ").strip()
                    if label:
                        ENEMY_CLASS_LOOKUP[cls] = label
                        save_lookup("enemy_class_lookup.json", ENEMY_CLASS_LOOKUP)
        return

    enemies = count_enemies(baseline)
    print(f"  Enemies: {enemies if enemies else 'none identified yet'}")


# ------------------------------------------------------------
# Zone survey - accumulates enemy findings per zone over time
# ------------------------------------------------------------
# Enemies get killed over time, so a single snapshot undercounts - we
# track the PEAK count seen during a zone visit instead, which is a much
# better proxy for "how many enemies this zone actually spawns with".
# Saved continuously to zone_survey.json (keyed by zone name once known,
# falling back to "zone index N" if not yet labeled) - safe to Ctrl+C
# any time, whatever's been seen so far is already on disk.

SURVEY_FILE = "zone_survey.json"


def load_survey():
    if os.path.exists(SURVEY_FILE):
        try:
            with open(SURVEY_FILE) as f:
                content = f.read().strip()
                if not content:
                    return {}
                return json.loads(content)
        except json.JSONDecodeError:
            print(f"Warning: {SURVEY_FILE} exists but isn't valid JSON (empty or corrupted) - starting fresh.")
            return {}
    return {}


def save_survey(survey):
    with open(SURVEY_FILE, "w") as f:
        json.dump(survey, f, indent=2, sort_keys=True)


def update_survey_entry(survey, zone_key, total_actors, enemies, total_kills_estimate=None):
    """Merges new counts into the running peak for this zone - enemy
    counts track the highest simultaneous count seen, which is what
    tells you the zone's real enemy population.

    total_kills_estimate is separate from the per-species breakdown - it's
    the sum of ALL population drops seen (labeled or not), so the mission's
    real kill total is available even when you haven't (and don't need to)
    name every single enemy variant."""
    entry = survey.setdefault(zone_key, {"total_actors_seen": 0, "enemies": {}, "enemies_total_killed_estimate": 0})
    entry.setdefault("enemies_total_killed_estimate", 0)
    entry["total_actors_seen"] = max(entry["total_actors_seen"], total_actors)
    for label, count in enemies.items():
        entry["enemies"][label] = max(entry["enemies"].get(label, 0), count)
    if total_kills_estimate is not None:
        entry["enemies_total_killed_estimate"] = max(entry["enemies_total_killed_estimate"], total_kills_estimate)
    return entry


def survey_zones(pm, base, poll_interval=1.0):
    """Dedicated survey mode: travel between zones freely, and this
    continuously builds up zone_survey.json with peak enemy counts per
    zone, using the full-zone scan (persistent level + all loaded
    streaming sub-levels) rather than the persistent-level-only scan.

    Enemy detection here is behavior-based, not guessed from static
    fields or raw counts (which produced false positives before - e.g.
    scenery classes that happen to have plausible-looking field values):
      - ENEMY candidates: classes whose total instance count drops during
        play - real, since killed enemies get destroyed and removed from
        the actor array. Static scenery never does this.
    This signal only fires from something actually changing while you
    play - a class that's constant everywhere (like the false positives
    from the old signature check) never accumulates any evidence at all.

    Chest tracking was removed from this survey - handled exclusively via
    the DLL's ProcessEvent hook now (see get_chest_open_events).

    Ctrl+C to stop - already-saved data is never lost.
    """
    import time
    from collections import Counter, defaultdict

    survey = load_survey()
    print(f"Loaded existing survey with {len(survey)} zone(s) already recorded.\n" if survey
          else "Starting a fresh survey.\n")

    current_zone_index = "not_set_yet"
    seen_unknown_zones = set()
    seen_unknown_pickups = set()

    # Session-wide (across zones) - accumulated evidence a class is real
    class_decrease_evidence = defaultdict(lambda: {"times_decreased": 0, "total_decrease": 0, "max_count_seen": 0})

    # Per-zone (reset on zone change - different zones have unrelated actors)
    class_count_history = {}
    zone_kill_accumulator = 0

    print("Surveying - travel between zones freely, explore each fully "
          "(let enemies show themselves before moving on). "
          "Ctrl+C to stop.\n")

    try:
        while True:
            time.sleep(poll_interval)
            try:
                world = pm.read_longlong(base + OFFSETS["gworld"])
            except Exception:
                continue
            if not world:
                current_zone_index = None
                continue

            zone_index = get_zone_name_index(pm, world)
            if zone_index != current_zone_index:
                if zone_index is not None and zone_index not in ZONE_NAME_LOOKUP:
                    seen_unknown_zones.add(zone_index)
                print(f"\n=== Entered zone: {zone_label_with_raw(zone_index)} ===")
                current_zone_index = zone_index
                class_count_history.clear()
                zone_kill_accumulator = 0

            try:
                snapshot = scan_full_zone(pm, world)
            except Exception:
                continue
            if not snapshot:
                continue

            # Enemy signal: population drops
            counts = Counter(snapshot.values())
            for cls, new_count in counts.items():
                old_count = class_count_history.get(cls)
                ev = class_decrease_evidence[cls]
                if old_count is not None and new_count < old_count:
                    ev["times_decreased"] += 1
                    ev["total_decrease"] += (old_count - new_count)
                    zone_kill_accumulator += (old_count - new_count)
                ev["max_count_seen"] = max(ev["max_count_seen"], new_count)
                class_count_history[cls] = new_count

            if not ENEMY_CLASS_LOOKUP:
                # same first-run labeling prompt as watch_level/report_zone_snapshot
                report_zone_snapshot(pm, snapshot)
                continue

            enemies = count_enemies(snapshot)
            zone_key = zone_label(zone_index) if zone_index is not None else "(loading)"
            if zone_key != "(loading)":
                entry = update_survey_entry(survey, zone_key, len(snapshot), enemies, total_kills_estimate=zone_kill_accumulator)
                save_survey(survey)
    except KeyboardInterrupt:
        print("\nStopped. Survey saved to zone_survey.json:\n")
        for zone_key, entry in survey.items():
            print(f"  {zone_key}: {entry['total_actors_seen']} actors seen")
            for label, count in entry["enemies"].items():
                print(f"    enemy - {label}: {count} (peak simultaneous)")
            total_killed = entry.get("enemies_total_killed_estimate", 0)
            if total_killed:
                print(f"    enemies killed (ALL types, including unlabeled): {total_killed}")

        enemy_candidates = sorted(
            ((cls, ev) for cls, ev in class_decrease_evidence.items() if ev["times_decreased"] > 0 and cls not in ENEMY_CLASS_LOOKUP),
            key=lambda kv: -kv[1]["total_decrease"],
        )

        if enemy_candidates:
            print(f"\n{len(enemy_candidates)} enemy candidate(s) - population dropped during play:")
            for cls, ev in enemy_candidates[:20]:
                print(f"  class_name_index={cls:<8} dropped {ev['total_decrease']} total across "
                      f"{ev['times_decreased']} event(s), peak {ev['max_count_seen']} seen at once")
            print("(You don't need to name all of these - the total kill count above already "
                  "includes unlabeled ones. Naming just the big, common ones is enough; type "
                  "'stop' at any prompt to leave the rest unlabeled.)")
            if input("Label any as enemies now? (y/n): ").strip().lower() == "y":
                if MOB_ID_ORDER:
                    print("\nReference - real internal mob names (community-sourced datamining):")
                    for i in range(0, len(MOB_ID_ORDER), 8):
                        print("  " + ", ".join(MOB_ID_ORDER[i:i + 8]))
                    print()
                for cls, ev in enemy_candidates[:20]:
                    label = input(f"  class_name_index={cls} - enemy name (blank to skip, 'stop' for rest): ").strip()
                    if label.lower() == "stop":
                        break
                    if label:
                        ENEMY_CLASS_LOOKUP[cls] = label
                        save_lookup("enemy_class_lookup.json", ENEMY_CLASS_LOOKUP)

        interactive_review(seen_unknown_zones, ZONE_NAME_LOOKUP, "zone_name_lookup.json", "zone", reference_list=ZONE_ID_ORDER)
        interactive_review(seen_unknown_pickups, PICKUP_CLASS_LOOKUP, "pickup_class_lookup.json", "pickup class")


def watch_session(pm, base, poll_interval=1.0):
    """Top-level monitor: tracks zone changes (lobby -> Creeper Woods ->
    next zone, etc.) automatically, announcing each one by name and
    printing that zone's enemy breakdown - then continues the existing
    pickup/enemy/death polling from watch_level() within that zone until
    the next transition. Ctrl+C to stop.
    """
    import time

    current_zone_index = "not_set_yet"  # sentinel, distinct from None (loading)
    baseline = {}
    was_dead = False
    pawn_address = None  # wire up once PAWN_HEALTH_OFFSET is confirmed

    print("Watching session - travel between zones freely. Ctrl+C to stop.\n")

    seen_unknown_zones = set()
    seen_unknown_pickups = set()

    try:
        while True:
            time.sleep(poll_interval)
            try:
                world = pm.read_longlong(base + OFFSETS["gworld"])
            except Exception:
                continue

            if not world:
                if current_zone_index is not None:
                    print(f"\n[{zone_label(None)}]")
                current_zone_index = None
                continue

            zone_index = get_zone_name_index(pm, world)

            if zone_index != current_zone_index:
                # Zone transition (including the very first zone entered)
                if zone_index is not None and zone_index not in ZONE_NAME_LOOKUP:
                    seen_unknown_zones.add(zone_index)
                print(f"\n=== Entered zone: {zone_label_with_raw(zone_index)} ===")
                current_zone_index = zone_index
                try:
                    baseline = scan_full_zone(pm, world)
                except Exception:
                    baseline = {}
                print(f"  {len(baseline)} actors present.")
                report_zone_snapshot(pm, baseline)
                print()
                continue  # skip diffing on the same poll we just reset baseline

            try:
                current = scan_full_zone(pm, world)
            except Exception:
                continue

            new_actors = diff_scans(baseline, current)
            for addr, class_name_index in new_actors.items():
                label = PICKUP_CLASS_LOOKUP.get(class_name_index)
                if label:
                    print(f"  PICKUP: {label}")
                elif class_name_index not in ENEMY_CLASS_LOOKUP:
                    seen_unknown_pickups.add(class_name_index)

            new_enemies = count_enemies(new_actors)
            if new_enemies:
                print(f"  NEW enemy spawn(s): {new_enemies}")

            if DEATH_DETECTION_READY and pawn_address:
                health = get_pawn_health(pm, pawn_address)
                if health is not None:
                    is_dead = health <= DEATH_HEALTH_THRESHOLD
                    if is_dead and not was_dead:
                        print("  *** PLAYER DIED ***")
                    was_dead = is_dead

            baseline = current
    except KeyboardInterrupt:
        print("\nStopped.")
        interactive_review(seen_unknown_zones, ZONE_NAME_LOOKUP, "zone_name_lookup.json", "zone", reference_list=ZONE_ID_ORDER)
        interactive_review(seen_unknown_pickups, PICKUP_CLASS_LOOKUP, "pickup_class_lookup.json", "pickup class")


def attach():
    if pymem is None:
        raise RuntimeError(
            "pymem isn't installed in this Python environment "
            f"({PYMEM_IMPORT_ERROR}). Run: pip install pymem pywin32"
        )
    # A bare pymem.Pymem(PROCESS_NAME) (like attach_module_size below still
    # does) just grabs the FIRST Dungeons.exe found by name - harmless with
    # one game running, but with two, every client process launched kept
    # attaching to that same first process, silently leaving the second
    # game instance with no client (and no injected DLL) at all - no
    # error, just quietly nothing happening for player 2. pick_target_pid
    # (auto_inject.py) is the one place that actually notices more than
    # one match and warns about it, picking a DIFFERENT, not-yet-injected
    # instance each time instead.
    import auto_inject
    pid = auto_inject.pick_target_pid(PROCESS_NAME, auto_inject.DLL_NAME, warn=print)
    if not pid:
        raise RuntimeError(f"Could not find a running {PROCESS_NAME} process.")
    pm = pymem.Pymem()
    pm.open_process_from_id(pid)
    module = pymem.process.module_from_name(pm.process_handle, PROCESS_NAME)
    return pm, module.lpBaseOfDll


def attach_module_size():
    pm = pymem.Pymem(PROCESS_NAME)
    module = pymem.process.module_from_name(pm.process_handle, PROCESS_NAME)
    return module.SizeOfImage


# ------------------------------------------------------------
# External, injection-free GNames scanner. Two passes, both plain
# ReadProcessMemory:
#   1. Search the heap for the raw bytes of the index-0 "None" entry -
#      every FNamePool has one, and its bytes are a fixed, searchable
#      signature (2-byte header + ASCII "None", no null terminator).
#   2. Search the main module's own memory (where the static FNamePool
#      object lives) for a pointer TO that address - that pointer's
#      location is FNamePool::Blocks[0], and pool_base is a fixed 0x10
#      back from there.
# No disassembly, no Cheat Engine, no code running inside the game.
# ------------------------------------------------------------

import ctypes
from ctypes import wintypes

MEM_COMMIT = 0x1000
MEM_PRIVATE = 0x20000
PAGE_NOACCESS = 0x01
PAGE_GUARD = 0x100


class _MEMORY_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", wintypes.DWORD),
        ("RegionSize", ctypes.c_size_t),
        ("State", wintypes.DWORD),
        ("Protect", wintypes.DWORD),
        ("Type", wintypes.DWORD),
    ]


def _enum_regions(pm, only_private=False):
    """Yields (base_addr, size) for committed, readable regions in the
    target process - the same information Cheat Engine's memory scanner
    is built on, just via raw VirtualQueryEx through ctypes."""
    VirtualQueryEx = ctypes.windll.kernel32.VirtualQueryEx
    mbi = _MEMORY_BASIC_INFORMATION()
    address = 0
    max_address = 0x7FFFFFFFFFFF
    while address < max_address:
        result = VirtualQueryEx(pm.process_handle, ctypes.c_void_p(address), ctypes.byref(mbi), ctypes.sizeof(mbi))
        if result == 0:
            break
        region_base = mbi.BaseAddress or address
        region_size = mbi.RegionSize or 0x1000
        if (mbi.State == MEM_COMMIT and not (mbi.Protect & PAGE_NOACCESS)
                and not (mbi.Protect & PAGE_GUARD)):
            if not only_private or mbi.Type == MEM_PRIVATE:
                yield region_base, region_size
        address = region_base + region_size


def _search_bytes_in_regions(pm, needle, regions, max_hits=50, chunk_size=4 * 1024 * 1024):
    """Reads each region in overlapping chunks (so a match spanning a
    chunk boundary isn't missed) and returns every absolute address
    where `needle` occurs, up to max_hits."""
    hits = []
    overlap = len(needle) - 1
    for region_base, region_size in regions:
        offset = 0
        while offset < region_size:
            read_len = min(chunk_size, region_size - offset)
            try:
                data = pm.read_bytes(region_base + offset, read_len)
            except Exception:
                offset += read_len
                continue
            start = 0
            while True:
                idx = data.find(needle, start)
                if idx == -1:
                    break
                hits.append(region_base + offset + idx)
                if len(hits) >= max_hits:
                    return hits
                start = idx + 1
            offset += read_len - overlap if read_len > overlap else read_len
    return hits


def get_item_stash_component(pm, base):
    world = pm.read_longlong(base + OFFSETS["gworld"])
    if not world:
        return None, "No UWorld - are you in a level?"

    game_instance = pm.read_longlong(world + OFFSETS["game_instance"])
    if not game_instance:
        return None, "No GameInstance"

    local_players_tarray = game_instance + OFFSETS["local_players"]
    local_players_data = pm.read_longlong(local_players_tarray)
    local_players_count = pm.read_int(local_players_tarray + 0x8)
    if local_players_count < 1 or not local_players_data:
        return None, "No local players"

    local_player = pm.read_longlong(local_players_data)
    player_controller = pm.read_longlong(local_player + OFFSETS["player_controller"])
    if not player_controller:
        return None, "No PlayerController"

    pawn = pm.read_longlong(player_controller + OFFSETS["pawn"])
    if not pawn:
        return None, "No Pawn - character not spawned yet?"

    item_stash = pm.read_longlong(pawn + OFFSETS["item_stash"])
    if not item_stash:
        return None, "No ItemStashComponent"

    return item_stash, None


# ------------------------------------------------------------
# Currency reading via the confirmed-working native-getter-hook
# technique (see the "currency" CLI command for full context).
# Requires dungeons_bridge.dll injected with the currency-getter hook
# (added in the v4 update alongside the Present hook).
# ------------------------------------------------------------

def _pipe_name_for(pm):
    """Per-process pipe name matching dungeons_bridge.cpp's GetPipeName()
    - MUST agree exactly, since this is how Python finds the right DLL
    instance's pipe when more than one Dungeons.exe is running at once
    (two clients open at the same time). A single fixed pipe name only
    ever works for the FIRST injected process - CreateNamedPipeW there
    uses nMaxInstances=1, so a second process's DLL trying to create a
    pipe of the exact same name just fails and retries forever, never
    becoming reachable (looked like "the dll doesn't work" for whichever
    client lost the race, when really its dll just could never open a
    channel Python could connect to)."""
    return rf"\\.\pipe\dungeons_bridge_{pm.process_id}"

# Confirmed from the same Cheat Engine table dungeons_bridge.cpp's
# currency-getter hook is based on (Dungeons_Master_Table_v3_70.CT) - see
# that file's own comment above hkCurrencyGetter for the source. Named as
# separate constants (not just dict literal values) specifically so a
# future edit can't silently swap two entries the way "gold"/"eyes_of_ender"
# got swapped before (0x20/0x14 reversed) - each offset now carries its own
# name right next to the hex value instead of living as a bare value in a
# dict where a copy-paste/reorder mistake is easy to make and easy to miss
# in review.
#
# CORRECTION (confirmed live, once the g_currencyPtr staleness bug was
# fixed and writes were finally landing on the real, live currency
# object): writing to the offset previously labeled EMERALDS visibly
# incremented the player's EYES OF ENDER count in-game instead - i.e.
# the CT-sourced 0x08/0x20 pairing above had Emeralds and Eyes of Ender
# swapped, the same class of transcription mistake as the earlier
# confirmed Gold/Eyes of Ender swap, just not caught until writes were
# actually reaching the right OBJECT for the first time. Gold (0x14) is
# unconfirmed either way by this - it was never involved in either
# swap and there's no live evidence against it - but treat it as
# unverified until cross-checked too. Use the currency-widget hook
# (get_currency_value_events/get_currency_type_events, and the
# mismatch-logging cross-check wired into the emerald poll in
# dungeons_ap_client.py) with the HUD open to positively confirm each
# of the three going forward, rather than trusting any CT-sourced
# offset blind.
# CORRECTION #2 (confirmed live via /dump_currency read directly against
# player-reported real HUD values - Emeralds=158, Gold=0, Eyes of
# Ender=401 - taken at the same instant): CORRECTION #1 above swapped
# the wrong pair. A direct three-way read showed offset 0x08 (labeled
# eyes_of_ender by correction #1) actually holds 158 - the real
# EMERALDS value - and offset 0x14 (labeled gold) holds 401 - the real
# EYES OF ENDER value. 0x08 was right all along, all the way back to
# before correction #1 - the live "writes hit eyes of ender" test that
# prompted correction #1 was misleading (likely caught mid a still-
# settling pointer/object state right after the g_currencyPtr fix, or
# some other confound), not evidence the offset itself was wrong.
#
# Gold remains UNCONFIRMED - 0x20 read -2107078688 (garbage) against a
# real Gold of 0, which rules 0x20 OUT (that's not what a real zero
# balance reads as) but doesn't tell us what the RIGHT offset is, since
# a real 0 can't be distinguished from wrong-address garbage that
# happens to differ. Left at 0x20 as a placeholder, not a confirmed
# value - re-verify with /dump_currency once gold is actually nonzero
# in a real save, the same way emeralds and eyes of ender were just
# pinned down here.
CURRENCY_OFFSET_EMERALDS = 0x08
CURRENCY_OFFSET_GOLD = 0x20  # UNCONFIRMED - see CORRECTION #2 above
CURRENCY_OFFSET_EYES_OF_ENDER = 0x14

CURRENCY_OFFSETS = {
    "emeralds": CURRENCY_OFFSET_EMERALDS,
    "gold": CURRENCY_OFFSET_GOLD,
    "eyes_of_ender": CURRENCY_OFFSET_EYES_OF_ENDER,
}


def get_currency_pointer(pm):
    """Asks dungeons_bridge.dll for the captured currency pointer (the
    'this' of the native getter, hooked via the same technique confirmed
    working in Dungeons_Master_Table_v3_70.CT). Returns (pointer, error) -
    pointer is 0 if the hook hasn't captured anything yet (nothing's
    called the getter this session - open your inventory/HUD once)."""
    try:
        import win32file
    except ImportError:
        return None, "pywin32 not installed - pip install pywin32"

    try:
        pipe = win32file.CreateFile(
            _pipe_name_for(pm), win32file.GENERIC_READ | win32file.GENERIC_WRITE,
            0, None, win32file.OPEN_EXISTING, 0, None
        )
    except Exception as e:
        return None, f"couldn't connect to dungeons_bridge.dll: {e}"

    try:
        win32file.WriteFile(pipe, b"get_currency_ptr")
        _, data = win32file.ReadFile(pipe, 256)
    finally:
        win32file.CloseHandle(pipe)

    response = data.decode()
    if not response.startswith("PTR:"):
        return None, f"unexpected response: {response}"

    return int(response[4:], 16), None


def read_currency_values(pm, currency_ptr):
    """Reads Emeralds/Gold/Eyes of Ender directly from the captured
    currency pointer - confirmed offsets from the same CT table."""
    values = {}
    for name, offset in CURRENCY_OFFSETS.items():
        try:
            values[name] = pm.read_int(currency_ptr + offset)
        except Exception:
            values[name] = None
    return values


def scan_currency_offsets(pm, currency_ptr, real_emeralds, real_gold, real_eyes_of_ender,
                           window=0x200):
    """Targeted alternative to CURRENCY_OFFSETS guessing/CT-table trust,
    which has now been directly disproven twice - a live cross-check
    (real HUD: Emeralds=258, Gold=0, Eyes of Ender=801 vs a /dump_currency
    using the "confirmed" offsets: emeralds=801 (that's really Eyes of
    Ender), gold=672 (matches NONE of the three real values), eyes_of_
    ender=-1624226688 (garbage)) showed the whole CURRENCY_OFFSETS table
    is wrong, not just swapped. Rather than guess a THIRD time, this reads
    every 4-byte-aligned int32 in [currency_ptr, currency_ptr+window) and
    reports every offset whose value exactly matches one of the three
    real numbers the player just read off their own HUD - direct ground
    truth, immune to widget/address mixups. real_gold=0 is deliberately
    NOT matched (0 is far too common a false positive across an object
    this size to mean anything) unless it's the only nonzero-distinct
    value among the three, so re-run this with real_gold nonzero once the
    player is actually carrying some for a trustworthy Gold offset.
    Returns a dict of {label: [offsets]} for whichever of the three had a
    nonzero/distinctive value to search for."""
    targets = {}
    if real_emeralds:
        targets.setdefault(real_emeralds, []).append("emeralds")
    if real_gold:
        targets.setdefault(real_gold, []).append("gold")
    if real_eyes_of_ender:
        targets.setdefault(real_eyes_of_ender, []).append("eyes_of_ender")

    hits = {"emeralds": [], "gold": [], "eyes_of_ender": []}
    for off in range(0, window, 4):
        try:
            val = pm.read_int(currency_ptr + off)
        except Exception:
            continue
        if val in targets:
            for label in targets[val]:
                hits[label].append(off)
    return hits


def get_chest_open_events(pm):
    """Drains dungeons_bridge.dll's ProcessEvent-hook-captured interact
    events (see dungeons_bridge.cpp's hkProcessEvent/OnInteracted hook).
    Returns (list_of_events, error). Each event is a dict:
    {actor_addr, class_name, world_ptr}. Fires for EVERY interactable
    object in the game (chests, doors, NPCs, pickups, buttons...), not
    just chests - confirmed real class names (from live testing):
    'BP_FancyChest_C', 'BP_SupplyStation_C', 'BP_ArrowStorable_C' (a
    pickup, not a chest). Filtering for which classes actually count as
    chests happens here, in KNOWN_CHEST_CLASSES below, specifically so
    that list can be extended (once Wooden/Deluxe's real names are
    confirmed) without ever needing to recompile/reinject the DLL again.
    Requires the updated dungeons_bridge.dll (with the OnInteracted
    hook) injected - older DLL versions won't understand this request.

    Wire format per entry is "addrHex,className,worldPtrHex" (three
    comma-separated fields - InteractEvent grew a worldPtr field after
    this parser was first written, and this parser was never updated to
    match: it was still doing entry.split(",", 1), which silently folded
    ",worldPtrHex" onto the end of class_name instead of raising - class
    names containing "supply"/"chest" as a substring still happened to
    classify correctly by luck, but world_ptr was always missing,
    silently forcing every caller onto the "older DLL" fallback path
    that resolves the zone from whatever zone is CURRENTLY polled rather
    than the zone the interact actually happened in."""
    try:
        import win32file
    except ImportError:
        return None, "pywin32 not installed - pip install pywin32"

    try:
        pipe = _connect_bridge_pipe(pm)
    except Exception as e:
        return None, f"couldn't connect to dungeons_bridge.dll: {e}"

    try:
        win32file.WriteFile(pipe, b"get_chest_events")
        _, data = win32file.ReadFile(pipe, 65536)
    finally:
        win32file.CloseHandle(pipe)

    response = data.decode()
    if not response.startswith("EVENTS:"):
        return None, f"unexpected response: {response}"

    body = response[len("EVENTS:"):]
    parts = body.split("|")
    try:
        count = int(parts[0])
    except ValueError:
        return None, f"malformed count in response: {response}"

    events = []
    for entry in parts[1:1 + count]:
        # className itself can't contain a comma (UE identifiers are
        # alphanumeric/underscore only), so a plain split(",", 2) safely
        # yields exactly [addr_hex, class_name, world_ptr_hex] against
        # the bridge's current 3-field format.
        if "," not in entry:
            continue
        fields = entry.split(",", 2)
        addr_hex, class_name = fields[0], fields[1]
        world_ptr = None
        if len(fields) == 3 and fields[2]:
            try:
                world_ptr = int(fields[2], 16)
            except ValueError:
                world_ptr = None  # malformed - fall back to the caller's current-zone path
        events.append({
            "actor_addr": int(addr_hex, 16),
            "class_name": class_name,
            "world_ptr": world_ptr,
        })
    return events, None


def get_mission_outcome_events(pm):
    """Drains dungeons_bridge.dll's mission-outcome trigger queue (see
    dungeons_bridge.cpp's IsMissionOutcomeFunction/CaptureMissionOutcomeEvent
    - classifies ProcessEvent calls to MulticastMissionFinished,
    OnShowMissionVictory, or MulticastGameOver). Returns (list_of_events,
    error). Each event is a dict: {actor_addr, trigger_name}.

    This is NOT a source of truth by itself - a totem-loss failure can
    fire the exact same trigger as a real win. It only means "a mission
    run just concluded somehow" - the caller's job is to treat this as a
    wake-up signal for exactly one authoritative call_is_mission_completed()
    call, which is the only thing allowed to decide completed-or-not (see
    dungeons_ap_client.py's game_watcher, "mission completion" section).

    Wire format per entry is "addrHex,triggerName" (two comma-separated
    fields, same style as get_chest_open_events but without a worldPtr
    field - the trigger's own actor address isn't currently used for
    anything, only its presence and which name fired, so there was
    nothing to add a third field for here)."""
    try:
        import win32file
    except ImportError:
        return None, "pywin32 not installed - pip install pywin32"

    try:
        pipe = _connect_bridge_pipe(pm)
    except Exception as e:
        return None, f"couldn't connect to dungeons_bridge.dll: {e}"

    try:
        win32file.WriteFile(pipe, b"get_mission_outcome_events")
        _, data = win32file.ReadFile(pipe, 65536)
    finally:
        win32file.CloseHandle(pipe)

    response = data.decode()
    if not response.startswith("EVENTS:"):
        return None, f"unexpected response: {response}"

    body = response[len("EVENTS:"):]
    parts = body.split("|")
    try:
        count = int(parts[0])
    except ValueError:
        return None, f"malformed count in response: {response}"

    events = []
    for entry in parts[1:1 + count]:
        if "," not in entry:
            continue
        addr_hex, _, trigger_name = entry.partition(",")
        events.append({
            "actor_addr": int(addr_hex, 16),
            "trigger_name": trigger_name,
        })
    return events, None


def get_currency_value_events(pm):
    """Drains dungeons_bridge.dll's currency-widget OnValueChanged queue
    (see dungeons_bridge.cpp's IsCurrencyValueChangedFunction/
    CaptureCurrencyValueEvent - fires when a UMG *Counter* widget
    receives the value it's about to display). Returns
    (list_of_events, error). Each event is a dict:
    {widget_addr, new_value, previous_value}.

    This is an offset-free CROSS-CHECK against the p_currency-based
    CURRENCY_OFFSETS read/write path, not a replacement for it - it only
    fires while the relevant HUD widget actually exists on-screen, so it
    can't be polled continuously the way read_currency_values can. See
    get_currency_type_events for pairing a widget_addr up with WHICH
    currency it belongs to.

    Wire format per entry is "widgetAddrHex,newValue,previousValue"
    (three comma-separated fields, same style as get_chest_open_events).
    Requires a dungeons_bridge.dll build with the currency-widget hook -
    older DLLs don't understand this request and this will just report
    an unexpected-response error (nothing crashes; the caller should
    treat that the same as "not available this session")."""
    try:
        import win32file
    except ImportError:
        return None, "pywin32 not installed - pip install pywin32"

    try:
        pipe = _connect_bridge_pipe(pm)
    except Exception as e:
        return None, f"couldn't connect to dungeons_bridge.dll: {e}"

    try:
        win32file.WriteFile(pipe, b"get_currency_value_events")
        _, data = win32file.ReadFile(pipe, 65536)
    finally:
        win32file.CloseHandle(pipe)

    response = data.decode()
    if not response.startswith("EVENTS:"):
        return None, f"unexpected response: {response}"

    body = response[len("EVENTS:"):]
    parts = body.split("|")
    try:
        count = int(parts[0])
    except ValueError:
        return None, f"malformed count in response: {response}"

    events = []
    for entry in parts[1:1 + count]:
        fields = entry.split(",")
        if len(fields) != 3:
            continue
        addr_hex, new_hex, prev_hex = fields
        try:
            events.append({
                "widget_addr": int(addr_hex, 16),
                "new_value": int(new_hex),
                "previous_value": int(prev_hex),
            })
        except ValueError:
            continue  # malformed entry - drop it, not worth failing the whole batch over
    return events, None


def get_currency_type_events(pm):
    """Drains dungeons_bridge.dll's currency-widget OnCurrencyTypeChanged
    queue (see dungeons_bridge.cpp's IsCurrencyTypeChangedFunction/
    CaptureCurrencyTypeEvent). Returns (list_of_events, error). Each
    event is a dict: {widget_addr, currency_name}.

    currency_name is resolved from the raw FName ComparisonIndex the DLL
    reports via resolve_fname (same FNamePool machinery used everywhere
    else in this file for class_name_index) - None if resolution fails
    (gnames offset not verified yet, or a bad read), in which case the
    caller has the raw index available separately if it wants to retry
    resolution later rather than dropping the event outright.

    Pair this up with get_currency_value_events by widget_addr: a
    counter widget fires OnCurrencyTypeChanged once (when it's told
    which currency it displays, typically once at widget construction)
    and OnValueChanged repeatedly (every time that currency's amount
    changes) - so caching {widget_addr: currency_name} from this
    function's output, keyed for the lifetime of the session, is enough
    to label every OnValueChanged event against the right currency
    without needing both event types every single poll.

    Wire format per entry is "widgetAddrHex,serializedIdIndex" (two
    comma-separated fields, same style as get_mission_outcome_events)."""
    try:
        import win32file
    except ImportError:
        return None, "pywin32 not installed - pip install pywin32"

    try:
        pipe = _connect_bridge_pipe(pm)
    except Exception as e:
        return None, f"couldn't connect to dungeons_bridge.dll: {e}"

    try:
        win32file.WriteFile(pipe, b"get_currency_type_events")
        _, data = win32file.ReadFile(pipe, 65536)
    finally:
        win32file.CloseHandle(pipe)

    response = data.decode()
    if not response.startswith("EVENTS:"):
        return None, f"unexpected response: {response}"

    body = response[len("EVENTS:"):]
    parts = body.split("|")
    try:
        count = int(parts[0])
    except ValueError:
        return None, f"malformed count in response: {response}"

    events = []
    for entry in parts[1:1 + count]:
        if "," not in entry:
            continue
        addr_hex, _, index_hex = entry.partition(",")
        try:
            widget_addr = int(addr_hex, 16)
            serialized_id_index = int(index_hex)
        except ValueError:
            continue
        currency_name = resolve_fname(pm, serialized_id_index)
        events.append({
            "widget_addr": widget_addr,
            "serialized_id_index": serialized_id_index,
            "currency_name": currency_name,
        })
    return events, None


PICKUP_TIER_NAMES = {0: "nothing gated pickable yet", 1: "Health items", 2: "Health items + Potions",
                      3: "Health items + Potions + TNT"}


def _connect_bridge_pipe(pm, retries=10, delay=0.05):
    """Opens a connection to dungeons_bridge.dll's named pipe, retrying
    briefly on failure. PipeServerThread (dungeons_bridge.cpp) recreates
    its named pipe instance fresh after every single client disconnects
    - a connection attempt made immediately after a PREVIOUS one just
    closed can land in the brief window before the server calls
    CreateNamedPipeW again, failing with ERROR_FILE_NOT_FOUND even
    though the DLL is alive and working correctly. This matters here
    specifically because pickup_tier's CLI command opens two connections
    back-to-back (set, then get to verify) - a single connection per
    script run was less likely to ever hit this race. Requires pm now
    (not just retries/delay) since the pipe name is per-process - see
    _pipe_name_for."""
    import win32file
    last_error = None
    for _ in range(retries):
        try:
            return win32file.CreateFile(
                _pipe_name_for(pm), win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                0, None, win32file.OPEN_EXISTING, 0, None
            )
        except Exception as e:
            last_error = e
            time.sleep(delay)
    raise last_error


def set_pickup_tier(pm, tier):
    """Tells dungeons_bridge.dll's hkProcessEvent hook which progressive-
    pickup tier is currently unlocked (0-3, see PICKUP_TIER_NAMES) -
    tier 1 = Health items pickable, 2 = + Potions, 3 = + TNT. Weapons,
    armor, artifacts, tokens, eye of ender, and arrows are ALWAYS
    pickable regardless of tier - the DLL never gates them at all (see
    ClassifyPickupTier in dungeons_bridge.cpp; tier 3 intentionally
    gates only TNT, not the broad "everything else" it used to).
    Anything above the unlocked tier simply does nothing when interacted
    with in-game (no inventory add, no destroy, no sound - the
    underlying Blueprint logic never runs). Requires the updated
    dungeons_bridge.dll (with the pickup-tier gating hook) already
    injected."""
    try:
        import win32file
    except ImportError:
        return False, "pywin32 not installed - pip install pywin32"

    try:
        pipe = _connect_bridge_pipe(pm)
    except Exception as e:
        return False, f"couldn't connect to dungeons_bridge.dll: {e}"

    try:
        request = f"set_pickup_tier {tier}"
        win32file.WriteFile(pipe, request.encode())
        _, data = win32file.ReadFile(pipe, 65536)
    finally:
        win32file.CloseHandle(pipe)

    if data.decode() != "OK":
        return False, f"unexpected response: {data.decode()}"
    return True, None


def get_pickup_tier(pm):
    """Returns (tier_int, error) - whatever the DLL currently has
    unlocked, for verifying set_pickup_tier actually took effect."""
    try:
        import win32file
    except ImportError:
        return None, "pywin32 not installed - pip install pywin32"

    try:
        pipe = _connect_bridge_pipe(pm)
    except Exception as e:
        return None, f"couldn't connect to dungeons_bridge.dll: {e}"

    try:
        win32file.WriteFile(pipe, b"get_pickup_tier")
        _, data = win32file.ReadFile(pipe, 65536)
    finally:
        win32file.CloseHandle(pipe)

    response = data.decode()
    if not response.startswith("TIER:"):
        return None, f"unexpected response: {response}"

    try:
        return int(response[len("TIER:"):]), None
    except ValueError:
        return None, f"malformed tier in response: {response}"


def classify_interactable_class(class_name):
    """Classifies a real UE class name (from watch_chest_events) into
    'chest', 'supply', or None (not chest-related). Generic by NAME
    PATTERN rather than an exact-match list - confirmed real class names
    include BP_FancyChest_C, BP_WoodenChest_C, and even
    BP_WoodenChest_Hidden_C (a variant that would never have been
    guessed via a fixed list), all containing "Chest"; Supply Chest is
    internally BP_SupplyStation_C, not named "chest" at all, hence the
    separate check. This means any new chest tier/variant added to the
    game in the future is picked up automatically, with zero code
    changes needed - no more per-class-name maintenance."""
    lower = class_name.lower()
    if "supply" in lower:
        return "supply"
    if "chest" in lower:
        return "chest"
    return None


# ------------------------------------------------------------
# Emerald milestone watcher - Archipelago integration
# ------------------------------------------------------------
# Every 500 emeralds from 500 up to the player's emerald_goal option
# (see ap_world/options.py) is a location check. Claimed milestones are
# persisted to disk so a check earned in a previous session isn't lost,
# and so we never send the same LocationChecks packet twice even across
# restarts (the AP client itself also dedups, but no reason to rely on
# that alone).

MILESTONES_FILE = "emerald_milestones_claimed.json"


def load_claimed_milestones():
    if os.path.exists(MILESTONES_FILE):
        with open(MILESTONES_FILE) as f:
            return set(json.load(f))
    return set()


def save_claimed_milestones(claimed):
    with open(MILESTONES_FILE, "w") as f:
        json.dump(sorted(claimed), f)


EMERALD_EARNED_STATE_FILE = "emerald_total_earned.json"


def load_emerald_earned_state():
    """{"total_earned": N, "last_seen_balance": M} - see
    update_emerald_earned_total's docstring for why this exists instead
    of just polling the live currency balance directly."""
    if os.path.exists(EMERALD_EARNED_STATE_FILE):
        with open(EMERALD_EARNED_STATE_FILE) as f:
            try:
                data = json.load(f)
                total_earned = data.get("total_earned", 0)
                if not isinstance(total_earned, (int, float)) or total_earned > 100_000_000:
                    # A pre-fix client could have already written a
                    # corrupted value here (confirmed happening: a single
                    # bad read once pushed this over 1.8 BILLION) -
                    # update_emerald_earned_total no longer lets new
                    # corruption in, but a file written before that fix
                    # would otherwise be trusted forever, since this
                    # value only ever goes up. 100 million is nowhere
                    # near reachable by real play - anything past it is
                    # unambiguously bad data, not a legitimately dedicated
                    # player. Resets to 0 rather than trying to guess
                    # what the real total should have been - the milestones
                    # already (wrongly) claimed stay claimed either way,
                    # since that's tracked separately in
                    # emerald_milestones_claimed.json, but at least this
                    # stops making things worse going forward.
                    return {"total_earned": 0, "last_seen_balance": data.get("last_seen_balance")}
                return {"total_earned": total_earned,
                        "last_seen_balance": data.get("last_seen_balance")}
            except json.JSONDecodeError:
                pass
    return {"total_earned": 0, "last_seen_balance": None}


def save_emerald_earned_state(state):
    with open(EMERALD_EARNED_STATE_FILE, "w") as f:
        json.dump(state, f)


MAX_PLAUSIBLE_EMERALD_DELTA = 50000  # matches EmeraldGoal's own max ceiling - a
                                      # single poll gaining more than the ENTIRE
                                      # possible goal range at once is a bad read,
                                      # never a real haul


def update_emerald_earned_total(state, current_balance):
    """Feeds one freshly-polled currency balance into `state`
    ({"total_earned", "last_seen_balance"}), updating it in place.
    Returns whether it changed, so the caller knows whether to persist it.

    Emerald milestones used to be checked against the LIVE spendable
    balance - which meant spending emeralds (the gambling llama, mostly)
    could permanently cost a milestone the player had already genuinely
    earned. A mission only grants on the order of 500 emeralds; a goal
    in the thousands was never realistically reachable by holding that
    much at once while also never spending anything the whole run,
    which isn't how the currency is meant to be used.

    Fix: track CUMULATIVE emeralds ever earned instead of the live
    balance, by summing only the POSITIVE deltas between consecutive
    polls and ignoring drops (a drop is spending, not un-earning).
    500 -> spend to 100 -> earn back to 500 sums to +500 total, not
    +900 - the regrowth from 100 to 500 IS newly-earned emeralds, it's
    only the numbers passing back over old ground that makes it look
    like a re-count. This is exact as long as a single poll interval
    never contains BOTH an earn and a spend (only the net of the two
    would be visible then) - an acceptable approximation at a
    multi-second poll interval for a milestone tracker.

    last_seen_balance starts as None (fresh install, or first poll after
    a restart) - that first observation only establishes the baseline
    and adds nothing to the total, so restarting the client never
    phantom-credits whatever the player happens to be currently holding
    as if it were newly earned.

    A single bad memory read (e.g. during a zone-transition window - the
    same class of transient issue documented elsewhere in this file for
    item_stash/inventory reads) can hand this an absurd current_balance.
    Confirmed happening live: a garbage read once pushed total_earned to
    over 1.8 BILLION in a single poll from a real balance of 6 - which,
    since total_earned only ever grows and gets persisted to disk,
    doesn't just misfire once, it permanently corrupts the file and
    instantly claims every remaining milestone forever after. Any single
    poll's delta larger than MAX_PLAUSIBLE_EMERALD_DELTA (comfortably
    above anything a real mission/chest haul could produce between two
    polls, but nowhere near "instantly wins the whole track") is treated
    as a bad read and dropped entirely - state is left completely
    untouched (not even last_seen_balance moves), so the next good
    reading is compared against the last KNOWN GOOD balance rather than
    against garbage.
    """
    if current_balance < 0 or current_balance > MAX_PLAUSIBLE_EMERALD_DELTA * 10:
        # Not just an implausible DELTA - an implausible BALANCE outright
        # (10x the delta bound is well beyond any real emerald count the
        # game can actually hold). Reject outright, same reasoning as the
        # delta check below, before it ever gets a chance to become one.
        return False

    changed = False
    if state["last_seen_balance"] is None:
        state["last_seen_balance"] = current_balance
        changed = True
    elif current_balance > state["last_seen_balance"]:
        delta = current_balance - state["last_seen_balance"]
        if delta > MAX_PLAUSIBLE_EMERALD_DELTA:
            return False
        state["total_earned"] += delta
        state["last_seen_balance"] = current_balance
        changed = True
    elif current_balance < state["last_seen_balance"]:
        state["last_seen_balance"] = current_balance
        changed = True
    return changed


APPLIED_REWARDS_FILE = "applied_item_rewards.json"


def load_applied_reward_indices():
    """Absolute ReceivedItems indices (see ap_client.py's module docstring)
    already applied in-game by watch_item_rewards - persisted so a restart
    doesn't re-grant the same reward twice."""
    if os.path.exists(APPLIED_REWARDS_FILE):
        with open(APPLIED_REWARDS_FILE) as f:
            return set(json.load(f))
    return set()


def save_applied_reward_indices(applied):
    with open(APPLIED_REWARDS_FILE, "w") as f:
        json.dump(sorted(applied), f)


ENCHANT_SLOT_TIER_FILE = "enchant_slot_tier.json"


def load_enchant_slot_tier():
    """How many "Progressive Enchant Slot" items have been received so
    far this game (0-3, capped - see apply_item_reward.py's
    MAX_ENCHANT_SLOT_TIER), persisted so a client restart doesn't forget
    progression already earned. Tracked separately from
    APPLIED_REWARDS_FILE - that file marks WHICH absolute indices have
    been processed (shared across every reward type); this file is just
    the resulting count, the one value give_random_item's num_slots
    argument actually needs on every subsequent equipment grant."""
    if os.path.exists(ENCHANT_SLOT_TIER_FILE):
        with open(ENCHANT_SLOT_TIER_FILE) as f:
            try:
                return int(json.load(f))
            except (json.JSONDecodeError, TypeError, ValueError):
                pass
    return 0


def save_enchant_slot_tier(tier):
    with open(ENCHANT_SLOT_TIER_FILE, "w") as f:
        json.dump(tier, f)


PICKUP_TIER_FILE = "pickup_tier.json"


def load_pickup_tier():
    """How many "Progressive Pickup" items have been received so far this
    game (0-3, capped), persisted the same way and for the same reason as
    load_enchant_slot_tier above - this is the value actually pushed into
    the game via set_pickup_tier() on every attach/reconnect, not just a
    display number."""
    if os.path.exists(PICKUP_TIER_FILE):
        with open(PICKUP_TIER_FILE) as f:
            try:
                return int(json.load(f))
            except (json.JSONDecodeError, TypeError, ValueError):
                pass
    return 0


def save_pickup_tier(tier):
    with open(PICKUP_TIER_FILE, "w") as f:
        json.dump(tier, f)


def milestones_up_to(amount, goal):
    """Every 500-multiple milestone reached by `amount`, capped at `goal`
    (matches ap_world/locations.py's get_emerald_milestone_locations)."""
    capped = min(amount, goal)
    highest = (capped // 500) * 500
    return list(range(500, highest + 1, 500))


def watch_emeralds(pm, goal, host, port, slot_name, game_name, password="", poll_interval=2.0):
    """Polls the currency pointer, and any time the emerald count crosses
    a new 500-multiple milestone (up to goal), sends the corresponding
    Archipelago LocationChecks packet. Persists claimed milestones to
    MILESTONES_FILE so nothing is lost/resent across restarts.
    """
    from ap_client import ArchipelagoClient
    from _apworld_data import Locations as _apw
    get_emerald_milestone_id, milestone_location_name = (
        _apw.get_emerald_milestone_id, _apw.milestone_location_name
    )

    claimed = load_claimed_milestones()
    print(f"{len(claimed)} milestone(s) already claimed (from {MILESTONES_FILE}).")

    print(f"Connecting to Archipelago server at {host}:{port} as '{slot_name}' ({game_name})...")
    client = ArchipelagoClient(host, port, slot_name, game_name, password)
    connected_packet = client.connect()
    print(f"Connected. Slot data: team={connected_packet.get('team')}, "
          f"slot={connected_packet.get('slot')}\n")

    # Catch up immediately in case milestones were reached while this
    # watcher wasn't running (e.g. currency read externally, or the
    # watcher was closed and reopened mid-session).
    currency_ptr, error = get_currency_pointer(pm)
    if currency_ptr:
        values = read_currency_values(pm, currency_ptr)
        current_emeralds = values.get("emeralds") or 0
        reached = milestones_up_to(current_emeralds, goal)
        new_ones = [m for m in reached if m not in claimed]
        if new_ones:
            print(f"Catching up on {len(new_ones)} milestone(s) already reached: {new_ones}")
            ids = [get_emerald_milestone_id(m) for m in new_ones]
            client.send_location_checks(ids)
            claimed.update(new_ones)
            save_claimed_milestones(claimed)

    print(f"Watching emeralds - goal is {goal} ({goal // 500} total milestones). "
          f"Ctrl+C to stop.\n")

    try:
        while True:
            time.sleep(poll_interval)

            currency_ptr, error = get_currency_pointer(pm)
            if not currency_ptr:
                continue  # not captured yet, or DLL not reachable this poll - just retry

            values = read_currency_values(pm, currency_ptr)
            current_emeralds = values.get("emeralds")
            if current_emeralds is None:
                continue

            reached = milestones_up_to(current_emeralds, goal)
            new_ones = [m for m in reached if m not in claimed]
            if not new_ones:
                continue

            print(f"New milestone(s) reached (currently at {current_emeralds} emeralds): {new_ones}")
            ids = [get_emerald_milestone_id(m) for m in new_ones]
            client.send_location_checks(ids)
            claimed.update(new_ones)
            save_claimed_milestones(claimed)

            for m in new_ones:
                print(f"  -> sent check: {milestone_location_name(m)}")

            if current_emeralds >= goal and goal in claimed:
                print(f"\nGoal of {goal} emeralds reached - all milestone checks sent!")

    except KeyboardInterrupt:
        print("\nStopped. Progress saved - safe to resume later.")
    finally:
        client.close()


ZONE_CHESTS_FILE = "zone_chests_claimed.json"


def load_claimed_zone_chests():
    """Now only ever holds "global:extra_count" - a cosmetic-only log
    counter with no server equivalent. The real per-zone/supply/bonus
    claimed counts are derived live from ctx.checked_locations instead
    (see fire_chest_open's docstring) - server-authoritative, so they
    can't drift and don't need local storage. Old save files may still
    have leftover "zonename:chest"/"global:bonus" keys from before this
    change; they're simply never read anymore, harmless to leave in
    place."""
    if os.path.exists(ZONE_CHESTS_FILE):
        with open(ZONE_CHESTS_FILE) as f:
            return json.load(f)
    return {}


def save_claimed_zone_chests(claimed):
    with open(ZONE_CHESTS_FILE, "w") as f:
        json.dump(claimed, f, indent=2, sort_keys=True)


def watch_zone_chests(pm, base, host, port, slot_name, game_name, password=""):
    """Watches for real chest-open events (via dungeons_bridge.dll's
    OnInteracted hook - see classify_interactable_class) across EVERY
    zone with a confirmed fixed chest layout (Locations.py's
    ZONE_CHEST_COUNTS / ap_world/locations.py's mirror), and sends the
    next unclaimed '<Zone> - Chest N' or '<Zone> - Supply Chest N'
    Archipelago check for each one, in discovery order, tracked
    separately per zone AND per kind (chest vs supply - two independent
    counters, since a zone's chest count and supply count don't have to
    match).

    Once a zone/kind's confirmed count is exhausted, further opens in
    THAT zone (chest or supply) count as "extra beyond baseline" and
    feed a SINGLE GLOBAL extra-chest counter - shared across every zone,
    not per-zone. Every single extra chest found, anywhere, sends the
    next globally-numbered 'Bonus Chest N' check 1:1, up to the player's
    BonusChestCount option (read from the server's slot data, 0-100).
    These bonus locations are marked EXCLUDED server-side (see
    Regions.py) specifically so an early-game item can never end up
    placed behind a high bonus-chest number that requires a lot of
    extra-chest farming to reach.

    Zone-gated the same debounced way every other zone-aware watcher in
    this file is (a transient misread must never be treated as "left
    the zone" and silently swallow a real chest open) - a chest opened
    in a zone that ISN'T in ZONE_CHEST_COUNTS at all (or isn't
    confidently identified yet) is simply logged and ignored, never
    claims any slot."""
    from ap_client import ArchipelagoClient
    from _apworld_data import Locations as _apw
    (ZONE_CHEST_COUNTS, get_zone_chest_location_id, get_zone_supply_chest_location_id,
     zone_chest_location_name, zone_supply_chest_location_name,
     get_bonus_chest_location_id, bonus_chest_location_name, MAX_BONUS_CHESTS) = (
        _apw.ZONE_CHEST_COUNTS, _apw.get_zone_chest_location_id, _apw.get_zone_supply_chest_location_id,
        _apw.zone_chest_location_name, _apw.zone_supply_chest_location_name,
        _apw.get_bonus_chest_location_id, _apw.bonus_chest_location_name, _apw.MAX_BONUS_CHESTS,
    )

    claimed = load_claimed_zone_chests()
    print(f"Progress loaded from {ZONE_CHESTS_FILE} for {len(claimed)} zone/kind pair(s).")
    for zone_name, (chest_n, supply_n) in sorted(ZONE_CHEST_COUNTS.items()):
        c = claimed.get(f"{zone_name}:chest", 0)
        s = claimed.get(f"{zone_name}:supply", 0)
        print(f"  {zone_name}: chest {c}/{chest_n}, supply {s}/{supply_n}")
    global_bonus_sent = claimed.get("global:bonus", 0)
    global_extra_count = claimed.get("global:extra_count", 0)
    print(f"  global bonus: {global_bonus_sent} sent ({global_extra_count} extra chests found so far)")

    print(f"\nConnecting to Archipelago server at {host}:{port} as '{slot_name}' ({game_name})...")
    client = ArchipelagoClient(host, port, slot_name, game_name, password)
    connected_packet = client.connect()
    slot_data = connected_packet.get("slot_data", {}) or {}
    bonus_count_limit = slot_data.get("bonus_chest_count", 20)  # 20 matches Options.py's BonusChestCount default, in case an older server/slot doesn't send it
    bonus_count_limit = max(0, min(bonus_count_limit, MAX_BONUS_CHESTS))
    print(f"Connected. Slot data: team={connected_packet.get('team')}, "
          f"slot={connected_packet.get('slot')}, bonus_chest_count={bonus_count_limit}\n")

    current_zone_index = "__unset__"
    pending_zone_index = None
    pending_count = 0
    current_zone_name = None

    def send_next(zone_name, kind):
        """Sends the next unclaimed check for zone_name/kind. Once the
        confirmed base count for that zone/kind is exhausted, further
        opens count as "extra beyond baseline" and feed the ONE GLOBAL
        bonus counter (shared across every zone/kind) - every single
        extra chest sends the next bonus check, 1:1, up to
        bonus_count_limit. Returns a short status string for logging."""
        key = f"{zone_name}:{kind}"
        total = ZONE_CHEST_COUNTS[zone_name][0 if kind == "chest" else 1]
        already = claimed.get(key, 0)

        if already < total:
            next_num = already + 1
            location_id = (get_zone_chest_location_id(zone_name, next_num) if kind == "chest"
                            else get_zone_supply_chest_location_id(zone_name, next_num))
            location_name = (zone_chest_location_name(zone_name, next_num) if kind == "chest"
                              else zone_supply_chest_location_name(zone_name, next_num))
            client.send_location_checks([location_id])
            claimed[key] = next_num
            save_claimed_zone_chests(claimed)
            print(f"  -> sent check: {location_name}")
            if next_num == total:
                print(f"     ({zone_name} {kind} chests fully claimed: {total}/{total})")
            return "sent"

        # Base count already claimed - this is an extra chest, beyond
        # what was confirmed for this zone. Feeds the single GLOBAL
        # bonus counter, shared across every zone and kind - not
        # per-zone. Every extra chest sends a bonus check 1:1.
        extra_count = claimed.get("global:extra_count", 0) + 1
        claimed["global:extra_count"] = extra_count
        save_claimed_zone_chests(claimed)

        bonus_already = claimed.get("global:bonus", 0)
        if bonus_already >= bonus_count_limit:
            print(f"  (extra {zone_name} {kind} chest beyond baseline - all "
                  f"{bonus_count_limit} bonus slots already claimed, not sending)")
            return "bonus_exhausted"

        bonus_num = bonus_already + 1
        location_id = get_bonus_chest_location_id(bonus_num)
        location_name = bonus_chest_location_name(bonus_num)
        client.send_location_checks([location_id])
        claimed["global:bonus"] = bonus_num
        save_claimed_zone_chests(claimed)
        print(f"  -> sent BONUS check: {location_name} "
              f"(extra chest #{extra_count} globally, found in {zone_name})")
        return "bonus_sent"

    print("Watching for chest opens across all confirmed zones (Ctrl+C to stop)...\n")
    try:
        while True:
            time.sleep(0.3)

            try:
                world = pm.read_longlong(base + OFFSETS["gworld"])
            except Exception:
                continue
            if not world:
                continue

            zone_index = get_zone_name_index(pm, world)
            if zone_index is None:
                pending_zone_index = None
                pending_count = 0
            elif zone_index != current_zone_index:
                if zone_index == pending_zone_index:
                    pending_count += 1
                else:
                    pending_zone_index = zone_index
                    pending_count = 1
                if pending_count >= 2:
                    current_zone_index = zone_index
                    pending_zone_index = None
                    pending_count = 0
                    current_zone_name = ZONE_NAME_LOOKUP.get(zone_index, f"unknown_zone_{zone_index}")
                    print(f"=== Entered zone: {current_zone_name} ===")
            else:
                pending_zone_index = None
                pending_count = 0

            events, error = get_chest_open_events(pm)
            if error:
                continue
            for evt in events:
                kind = classify_interactable_class(evt["class_name"])
                if not kind:
                    continue  # not chest-related at all

                if current_zone_name not in ZONE_CHEST_COUNTS:
                    print(f"  (chest opened in '{current_zone_name}' - not a confirmed-fixed zone, "
                          f"not sending: {evt['class_name']})")
                    continue

                send_next(current_zone_name, kind)

    except KeyboardInterrupt:
        print("\nStopped. Progress saved - safe to resume later.")
    finally:
        client.close()


def get_pawn(pm, base):
    """Same chain as get_item_stash_component, but stops at the Pawn
    itself - needed as the anchor for WalletComponent (and anything else
    hanging directly off APlayerCharacter)."""
    world = pm.read_longlong(base + OFFSETS["gworld"])
    if not world:
        return None, "No UWorld - are you in a level?"

    game_instance = pm.read_longlong(world + OFFSETS["game_instance"])
    if not game_instance:
        return None, "No GameInstance"

    local_players_tarray = game_instance + OFFSETS["local_players"]
    local_players_data = pm.read_longlong(local_players_tarray)
    local_players_count = pm.read_int(local_players_tarray + 0x8)
    if local_players_count < 1 or not local_players_data:
        return None, "No local players"

    local_player = pm.read_longlong(local_players_data)
    player_controller = pm.read_longlong(local_player + OFFSETS["player_controller"])
    if not player_controller:
        return None, "No PlayerController"

    pawn = pm.read_longlong(player_controller + OFFSETS["pawn"])
    if not pawn:
        return None, "No Pawn - character not spawned yet?"

    return pawn, None


# ------------------------------------------------------------
# Remote UFunction calling via ProcessEvent, using the vtable-slot trick
# (INDEX_PROCESSEVENT=64, confirmed via OffsetsInfo.json) instead of a
# fixed ProcessEvent address - reads it live off each object's own vtable,
# so it doesn't depend on hardcoding an address that could shift between
# builds. Executes via a tiny hand-written x64 shellcode stub run in a
# genuine remote thread (CreateRemoteThread through pymem's start_thread,
# which blocks until the thread finishes) - this actually EXECUTES CODE
# inside the game process, unlike everything else in this script, which
# only reads/writes memory passively.
#
# DANGER: a wrong function address, a wrongly-sized/laid-out params
# buffer, or calling a function that isn't safe to invoke this way, CAN
# CRASH THE GAME. There is no dry-run - double check offsets before use.
# ------------------------------------------------------------

INDEX_PROCESSEVENT = 64  # vtable slot, from OffsetsInfo.json's INDEX_PROCESSEVENT

# Reads a 4-qword parameter block passed as the thread's lpParameter
# (arrives in RCX per the Windows x64 calling convention for thread entry
# points), sets up the three real ProcessEvent(UFunction*, void*) args in
# RCX/RDX/R8 (RCX=this/Object, RDX=Function, R8=Params), and calls it.
# Parameter block layout (8 bytes each, little-endian):
#   [0]=Object  [8]=Function  [16]=Params  [24]=ProcessEvent address
#
#   mov rax, rcx          mov rcx, [rax]         mov rdx, [rax+8]
#   mov r8, [rax+16]       mov r9, [rax+24]        sub rsp, 0x28
#   call r9                 add rsp, 0x28            xor eax, eax   ret
_PROCESS_EVENT_SHELLCODE = bytes([
    0x48, 0x89, 0xC8,
    0x48, 0x8B, 0x08,
    0x48, 0x8B, 0x50, 0x08,
    0x4C, 0x8B, 0x40, 0x10,
    0x4C, 0x8B, 0x48, 0x18,
    0x48, 0x83, 0xEC, 0x28,
    0x41, 0xFF, 0xD1,
    0x48, 0x83, 0xC4, 0x28,
    0x31, 0xC0,
    0xC3,
])


def call_ufunction_no_params(pm, object_addr, function_addr):
    """Calls a zero-parameter UFunction via ProcessEvent, replicating
    EXACTLY the idiom Dumper-7's own generated wrappers use for native
    functions (see ABaseCharacter::Kill()'s wrapper in Dungeons_functions.cpp):
    temporarily OR in FUNC_NATIVE on the function's own FunctionFlags,
    call ProcessEvent(function, nullptr), then restore the original flags.
    Returns (success, error_message)."""
    try:
        original_flags = pm.read_uint(function_addr + OFFSETS["ufunction_flags"])
    except Exception as e:
        return False, f"Could not read FunctionFlags: {e}"

    try:
        pm.write_uint(function_addr + OFFSETS["ufunction_flags"], original_flags | FUNC_NATIVE)
    except Exception as e:
        return False, f"Could not write FunctionFlags: {e}"

    success, error, _ = call_process_event(pm, object_addr, function_addr, b"")

    try:
        pm.write_uint(function_addr + OFFSETS["ufunction_flags"], original_flags)
    except Exception:
        pass  # best-effort restore - don't mask the call's own result over this

    return success, error


def call_process_event(pm, object_addr, function_addr, params_bytes, force_native=False):
    """Calls object_addr->ProcessEvent(function_addr, &params) for real,
    inside the game process, via a remote thread. params_bytes must match
    the UFunction's parameter struct layout exactly (see its
    _parameters.hpp) - written to remote memory before the call, then
    read back after so OutParm/ReturnParm fields can be recovered.

    force_native: temporarily ORs FUNC_NATIVE (0x400) into the target
    UFunction's own FunctionFlags right before the call, then restores
    the original flags afterward (success or failure) - mirrors EXACTLY
    what the real game code itself does, confirmed straight from
    Dungeons_functions.cpp's own decompiled wrapper for
    WalletComponent::ClientAdd:
        auto Flgs = Func->FunctionFlags;
        Func->FunctionFlags |= 0x400;
        UObject::ProcessEvent(Func, &Parms);
        Func->FunctionFlags = Flgs;
    Needed for functions flagged Net/NetClient/NetReliable (like
    ClientAdd) - without forcing FUNC_NATIVE for the duration of the
    call, ProcessEvent would likely route the call through the network
    RPC dispatch path instead of actually running the native
    implementation locally, silently doing nothing useful despite
    ProcessEvent itself reporting success. Not needed (and left off by
    default) for plain Native functions that already carry FUNC_NATIVE
    permanently, like IsMissionCompleted.
    Returns (success, error_message, params_bytes_after)."""
    original_flags = None
    try:
        vtable = pm.read_longlong(object_addr)
        if not vtable:
            return False, "Object has a null vtable pointer - not a valid UObject address", None
        process_event_addr = pm.read_longlong(vtable + INDEX_PROCESSEVENT * 8)
        if not process_event_addr:
            return False, "Null ProcessEvent address read from vtable", None

        if force_native:
            original_flags = pm.read_uint(function_addr + OFFSETS["ufunction_flags"])
            pm.write_uint(function_addr + OFFSETS["ufunction_flags"], original_flags | FUNC_NATIVE)

        params_size = max(len(params_bytes), 8)  # avoid a zero-size allocation
        remote_params = pm.allocate(params_size)
        pm.write_bytes(remote_params, params_bytes, len(params_bytes))

        remote_block = pm.allocate(32)  # Object, Function, Params, ProcessEvent addr
        pm.write_longlong(remote_block + 0, object_addr)
        pm.write_longlong(remote_block + 8, function_addr)
        pm.write_longlong(remote_block + 16, remote_params)
        pm.write_longlong(remote_block + 24, process_event_addr)

        remote_shellcode = pm.allocate(len(_PROCESS_EVENT_SHELLCODE))
        pm.write_bytes(remote_shellcode, _PROCESS_EVENT_SHELLCODE, len(_PROCESS_EVENT_SHELLCODE))

        pm.start_thread(remote_shellcode, remote_block)  # blocks until the call returns

        params_after = pm.read_bytes(remote_params, params_size)
        return True, None, params_after
    except Exception as e:
        return False, str(e), None
    finally:
        if original_flags is not None:
            try:
                pm.write_uint(function_addr + OFFSETS["ufunction_flags"], original_flags)
            except Exception:
                pass


KILL_FUNCTION_DEPTH_FILE = "kill_function_depth.json"


def _load_kill_function_depth():
    """The confirmed super_chain_depth where ABaseCharacter::Kill() lives
    (found via find_kill_function) - NOT a raw address, since UFunction
    addresses aren't guaranteed stable across game restarts, but the
    class hierarchy depth is structural and doesn't change between
    sessions for the same build."""
    if os.path.exists(KILL_FUNCTION_DEPTH_FILE):
        with open(KILL_FUNCTION_DEPTH_FILE) as f:
            content = f.read().strip()
        if content:
            try:
                return json.loads(content).get("depth")
            except json.JSONDecodeError:
                return None
    return None


def _save_kill_function_depth(depth):
    with open(KILL_FUNCTION_DEPTH_FILE, "w") as f:
        json.dump({"depth": depth}, f, indent=2)


KILL_FUNCTION_DEPTH = _load_kill_function_depth()


def find_confirmed_kill_function(pm, pawn):
    """Re-runs the structural search fresh (cheap - small candidate list)
    and returns the function pointer at the confirmed super_chain_depth,
    or None if not found/not confirmed yet. Re-searching each time avoids
    ever caching a raw address across a restart."""
    if KILL_FUNCTION_DEPTH is None:
        return None
    pawn_class = pm.read_longlong(pawn + OFFSETS["uobject_class"])
    if not pawn_class:
        return None
    candidates = find_zero_param_native_final_functions(pm, pawn_class)
    matches = [f for f, cls, depth in candidates if depth == KILL_FUNCTION_DEPTH]
    return matches[0] if matches else None


def kill_local_player(pm, base):
    """Kills the local player for real. Prefers calling the confirmed
    ABaseCharacter::Kill() UFunction (see find_kill_function /
    KILL_FUNCTION_DEPTH) - the actual, correct way the game itself kills a
    character, found via structural search (Size==0, Final|Native|Public|Const)
    and confirmed live (Health dropped to 0 AND the character actually died
    in-game). Falls back to directly writing the GAS HealthAttributeSet's
    Health to 0 if Kill() hasn't been confirmed yet - that fallback is
    known to just zero the value without reliably triggering death, since
    death is checked inside the damage-application call chain, not by
    polling Health.
    Returns (success, error_message, diagnostics)."""
    pawn, error = get_pawn(pm, base)
    if not pawn:
        return False, error, {}

    if KILL_FUNCTION_DEPTH is not None:
        func_addr = find_confirmed_kill_function(pm, pawn)
        if func_addr is None:
            return False, (f"Confirmed kill function not found at depth "
                            f"{KILL_FUNCTION_DEPTH} right now - class hierarchy "
                            f"may differ for this pawn."), {"pawn": hex(pawn)}
        success, call_error = call_ufunction_no_params(pm, pawn, func_addr)
        diagnostics = {"pawn": hex(pawn), "function": hex(func_addr), "chain": "Kill()"}
        return success, call_error, diagnostics

    # --- fallback: direct Health write (old behavior, kept for when
    # Kill() hasn't been confirmed via find_kill_function yet) ---
    if HEALTH_ATTRIBUTE_SET_CLASS is not None:
        entries = get_spawned_attributes(pm, pawn)
        attr_set = next((ptr for ptr, cls in entries if cls == HEALTH_ATTRIBUTE_SET_CLASS), None)
        if attr_set is None:
            return False, (f"Confirmed HealthAttributeSet class {HEALTH_ATTRIBUTE_SET_CLASS} not "
                            f"found in this pawn's SpawnedAttributes right now"), {"pawn": hex(pawn)}
        health_addr = attr_set + OFFSETS["health_attr_health"]
        chain_desc = {"pawn": hex(pawn), "attribute_set": hex(attr_set), "chain": "GAS (fallback - no Kill() confirmed)"}
    else:
        health_component = pm.read_longlong(pawn + OFFSETS["player_health_component"])
        if not health_component:
            return False, "No HealthComponent on Pawn (wrong pawn class, or offset stale)", {"pawn": hex(pawn)}
        health_addr = health_component + OFFSETS["health_component_health"]
        chain_desc = {"pawn": hex(pawn), "health_component": hex(health_component), "chain": "UHealthComponent (fallback)"}

    try:
        health_before = pm.read_float(health_addr)
    except Exception:
        health_before = None

    try:
        pm.write_float(health_addr, 0.0)
    except Exception as e:
        return False, str(e), chain_desc

    try:
        health_after = pm.read_float(health_addr)
    except Exception:
        health_after = None

    diagnostics = {**chain_desc, "health_before": health_before, "health_after": health_after}
    return True, None, diagnostics


def read_inventory(pm, item_stash_address, max_items=30):
    tarray_header = item_stash_address + OFFSETS["inventory_slots"]
    data_ptr = pm.read_longlong(tarray_header)
    count = pm.read_int(tarray_header + 0x8)

    items = []
    for i in range(min(count, max_items)):
        slot = pm.read_longlong(data_ptr + i * 8)
        if not slot:
            continue
        inventory_item = pm.read_longlong(slot + OFFSETS["slot_item"])
        if not inventory_item:
            continue

        item_struct = inventory_item + OFFSETS["item_struct"]
        power = pm.read_float(item_struct + OFFSETS["item_power"])
        rarity_raw = pm.read_uchar(item_struct + OFFSETS["rarity"])
        id_struct = item_struct + OFFSETS["item_id_struct"]
        name_index = pm.read_int(id_struct + OFFSETS["serialized_id"])

        if name_index in NAME_LOOKUP:
            name = NAME_LOOKUP[name_index]
        else:
            candidates = suggest_candidates(name_index)
            name = f"unknown (index {name_index}, maybe: {', '.join(candidates)})"
        rarity = RARITY_NAMES.get(rarity_raw, f"rarity {rarity_raw}")
        items.append({
            "slot": i,
            "name": name,
            "power": power,
            "rarity": rarity,
            "name_index": name_index,
            "address": item_struct,  # needed to write back to this exact item
        })

    return items


# ------------------------------------------------------------
# Item rewards (Archipelago checks) - WRITES to game memory
# ------------------------------------------------------------
# See the "GIVING ITEMS" section at the top of this file for the safety
# reasoning. In short: this overwrites an existing, already-registered
# item's data in place - it never allocates anything new.

RARITY_BYTES = {v: k for k, v in RARITY_NAMES.items()}
REVERSE_NAME_LOOKUP = {v: k for k, v in NAME_LOOKUP.items()}


def write_item(pm, item_struct_address, name_index, rarity_byte, power):
    """Low-level write: overwrites one item's id/rarity/power in place.
    Same offsets used everywhere else in this file for reading, just in
    reverse. Returns (ok: bool, error: str | None) - the write call itself
    can fail (e.g. no write access to that page), which is different from
    the write succeeding but the game reverting it afterward - that's what
    verify_write() below is for.

    Writes name_index to BOTH display_id and serialized_id - scan_identity_field
    found these are two separate copies that only agree for naturally-created
    items. Writing just one (what earlier versions of this did) left them
    out of sync, which is almost certainly why the in-game icon/name never
    updated even though power/rarity/serialized_id demonstrably did.
    """
    try:
        id_struct = item_struct_address + OFFSETS["item_id_struct"]
        pm.write_int(id_struct + OFFSETS["display_id"], name_index)
        pm.write_int(id_struct + OFFSETS["serialized_id"], name_index)
        pm.write_uchar(item_struct_address + OFFSETS["rarity"], rarity_byte)
        pm.write_float(item_struct_address + OFFSETS["item_power"], power)
        return True, None
    except Exception as e:
        return False, str(e)


def verify_write(pm, item_struct_address, expected_name_index, expected_rarity_byte):
    """Re-reads the exact addresses just written to, after a short delay -
    confirms the write actually stuck rather than trusting the pre-write
    read. If the game validates rarity against an item's expected rarity
    (or otherwise rejects the change on its next tick), this is what
    catches it instead of silently reporting success."""
    try:
        id_struct = item_struct_address + OFFSETS["item_id_struct"]
        actual_display_id = pm.read_int(id_struct + OFFSETS["display_id"])
        actual_index = pm.read_int(id_struct + OFFSETS["serialized_id"])
        actual_rarity = pm.read_uchar(item_struct_address + OFFSETS["rarity"])
        actual_power = pm.read_float(item_struct_address + OFFSETS["item_power"])
    except Exception as e:
        return False, f"couldn't re-read that address afterward ({e}) - the item may have been destroyed or reallocated"

    if actual_display_id != expected_name_index:
        return False, f"display_id reverted to {actual_display_id} (wrote {expected_name_index}) - something is rejecting or overwriting the change"
    if actual_index != expected_name_index:
        return False, f"serialized_id reverted to {actual_index} (wrote {expected_name_index}) - something is rejecting or overwriting the change"
    if actual_rarity != expected_rarity_byte:
        return False, (
            f"ids stuck (display_id={actual_display_id}, serialized_id={actual_index}) but rarity reverted to "
            f"{RARITY_NAMES.get(actual_rarity, actual_rarity)} - possibly a rarity/item mismatch the game enforces"
        )
    return True, (
        f"confirmed: display_id={actual_display_id} serialized_id={actual_index} "
        f"rarity={RARITY_NAMES.get(actual_rarity, actual_rarity)} power={actual_power:.2f}"
    )


def pick_latest_slot(items):
    """Choose the most-recently-added item to overwrite for a reward.

    InventorySlots is a TArray whose length IS the number of real items -
    there's no such thing as an empty slot sitting in it waiting to be
    filled; every pointer in the array is already a live, allocated
    InventoryItem*. So "use an empty slot" isn't something this structure
    supports - the array only ever holds real items, and new pickups
    append to the end of it. That makes the highest slot index the
    closest available proxy for "the item you most recently picked up",
    which is what we fall back to instead.
    """
    if not items:
        return None
    return max(items, key=lambda it: it["slot"])


def pick_sacrifice_slot(items):
    """Alternate strategy, kept for cases where overwriting the newest
    item isn't what you want: lowest-power Common item, falling back to
    lowest-power overall if you have no Common items."""
    if not items:
        return None
    common_items = [it for it in items if it["rarity"] == "Common"]
    pool = common_items if common_items else items
    return min(pool, key=lambda it: it["power"])


def parse_give_args(args):
    """Robustly splits `give` CLI args without requiring quotes around
    multi-word item names (e.g. Mercenary Armor). Scans every token for a
    recognizable rarity (Common/Rare/Unique), a number (power), a known
    strategy (latest/lowest_power), or a known refresh mode
    (counters/flag/both/none) - whatever's left, in order, is joined back
    together as the item name. This is what caused the earlier crash:
    `give Mercenary Armor Rare 1.5` unquoted split into 4 separate tokens,
    shifting rarity/power over by one until "Rare" landed where a float
    was expected.

    refresh_mode defaults to "none" - "counters" caused a real game crash
    when tested (see give_reward's docstring). Don't pass "counters" again
    without confirming what 0x1a0/0x1a8 actually are first.
    """
    rarity = "Common"
    power = 1.0
    strategy = "latest"
    refresh_mode = "none"
    name_parts = []
    valid_rarities = {"common", "rare", "unique"}
    valid_strategies = {"latest", "lowest_power"}
    valid_refresh_modes = {"counters", "flag", "both", "none"}

    for token in args:
        low = token.lower()
        if low in valid_rarities:
            rarity = token.capitalize()
            continue
        if low in valid_strategies:
            strategy = low
            continue
        if low in valid_refresh_modes:
            refresh_mode = low
            continue
        try:
            power = float(token)
            continue
        except ValueError:
            pass
        name_parts.append(token)

    return " ".join(name_parts), rarity, power, strategy, refresh_mode


def give_reward(pm, item_stash_address, item_name, rarity="Common", power=1.0, slot_index=None,
                 strategy="latest", refresh_mode="none"):
    """Grants an item as an Archipelago check reward by overwriting an
    existing inventory slot. Returns (success: bool, message: str).

    item_name must already be a value in NAME_LOOKUP (i.e. item_lookup.py's
    ITEM_TABLE, the static 269-item reference table) - this writes the FName
    index, so it needs a name that maps to a real index. rarity is
    "Common"/"Rare"/"Unique".

    slot_index picks a specific slot to overwrite, overriding strategy.
    strategy picks which existing item to sacrifice when slot_index isn't
    given: "latest" (default - highest slot index, i.e. most recently
    added) or "lowest_power" (previous default - lowest-power Common item).

    refresh_mode: which candidate UI-refresh-trigger field(s) to nudge
    after writing, from scan_dirty's results. DEFAULT IS NOW "none" -
    testing "counters" (bumping 0x1a0/0x1a8) caused a real game crash on
    opening the inventory. That means those fields are very likely NOT a
    simple dirty flag - more likely something like an array capacity,
    bound, or reference count that other code trusts to match real data,
    and incrementing it without a matching real change left something
    reading/dereferencing an entry that doesn't exist. Do NOT default this
    back to "counters" without confirming what 0x1a0/0x1a8 actually are
    first (e.g. checking what else reads them, or whether they're really
    counters at all) - re-testing it risks another crash and potential
    save corruption. "flag" (the 0x2ac bit) hasn't been tested yet and is
    a separate, untested hypothesis - back up your save before trying it.
    "both" combines both hypotheses and inherits the same risk as
    "counters". "none" (default) skips this entirely - use a real storage
    visit as the safe workaround for now.
    """
    if item_name not in REVERSE_NAME_LOOKUP:
        return False, f"'{item_name}' isn't a recognized item name - check item_lookup.py's ITEM_TABLE/all_items.csv"

    rarity_byte = RARITY_BYTES.get(rarity)
    if rarity_byte is None:
        return False, f"Unknown rarity '{rarity}' - use Common, Rare, or Unique"

    items = read_inventory(pm, item_stash_address)
    if not items:
        return False, "No items in inventory to overwrite"

    if slot_index is not None:
        target = next((it for it in items if it["slot"] == slot_index), None)
        if target is None:
            return False, f"No item in slot {slot_index}"
    elif strategy == "lowest_power":
        target = pick_sacrifice_slot(items)
    else:
        target = pick_latest_slot(items)

    name_index = REVERSE_NAME_LOOKUP[item_name]
    write_ok, write_err = write_item(pm, target["address"], name_index, rarity_byte, power)
    if not write_ok:
        return False, f"Write call failed outright: {write_err}"

    import time
    time.sleep(0.3)  # give the game a tick before checking whether it stuck
    verified, detail = verify_write(pm, target["address"], name_index, rarity_byte)

    refresh_ok, refresh_detail = bump_ui_refresh_hint(pm, item_stash_address, mode=refresh_mode)
    detail += f" | refresh hint ({refresh_mode}): {refresh_detail}"

    base_message = (
        f"Slot {target['slot']} ({target['name']}, {target['rarity']}) "
        f"-> attempted {item_name} ({rarity}, power {power})"
    )
    return verified, f"{base_message}. {detail}"


# ------------------------------------------------------------
# Identity field scan - finding what actually drives the icon/name
# ------------------------------------------------------------
# We now know SerializedId is a genuine READ-time identity signal (it's
# how item_lookup.py's ITEM_TABLE was cross-checked against real reads),
# but writing it doesn't change the in-game icon/name - only power updates
# live when we write it. That means the UI is very likely reading a
# separate, cached reference set once at item-creation time (an
# ItemDefinition pointer, icon asset reference, or similar) rather than
# re-deriving display info from SerializedId every frame.
#
# METHOD: grouped memory diff, same principle as a Cheat Engine "unknown
# initial value, compare next scan" - but across slots instead of across
# time. Take several inventory slots whose displayed name we already
# trust (from ITEM_TABLE), group their InventoryItem addresses by name,
# and look for a byte offset that's IDENTICAL within every group (every
# Crossbow agrees) but DIFFERENT between groups (Crossbow != Glaive).
# That offset is a strong identity-field candidate, because SerializedId
# already told us we can trust which items are "the same" for grouping.
#
# Needs at least 2 different confirmed item types in your inventory (more
# instances per type = stronger signal). Run it as: python dungeons_reader.py scan_identity

# ------------------------------------------------------------
# Stash-level diff scan - finding what makes the UI actually rebuild
# ------------------------------------------------------------
# Confirmed: display_id + serialized_id writes are correct in memory
# (verify_write proves this), but the in-game icon/name sometimes only
# updates after a full restart. That means something ELSE - most likely
# a "dirty"/"version"/count field living on ItemStashComponent itself,
# not on the item - is what the UI actually checks to decide whether to
# rebuild the inventory list at all. A real pickup changes that field;
# our external write never does, since we only ever touch the item.
#
# METHOD: simple before/after diff (not grouped - we don't need to
# compare item types here, just "what changed when one real pickup
# happened"). Snapshot bytes around item_stash_address, wait for you to
# pick up any real item in-game, snapshot again, and report every offset
# whose value changed. A small integer that increments by exactly 1 is
# the strongest candidate for a "version"/"dirty" counter.
#
# Run as: python dungeons_reader.py scan_dirty

# ------------------------------------------------------------
# Read-only field monitor - observe without writing, after the crash
# ------------------------------------------------------------
# Writing to 0x1a0/0x1a8 caused a real game crash on inventory-open, so
# those fields are not a confirmed-safe "dirty counter" - they may be an
# array bound, capacity, or reference count something else trusts to
# match real data. Before writing to them again, watch how they behave
# naturally: does the value only ever go up by exactly 1? Does it ever
# jump by more, or reset? Does it change on every inventory-affecting
# event (pickup, shop purchase, storage visit, leveling up) or only some
# of them? That tells us whether it's really a simple counter at all.
#
# This function only reads - it never writes anything, so there's no
# crash risk from running it. Play normally while it's running and watch
# for printed changes.
#
# Run as: python dungeons_reader.py monitor_counters

WATCHED_FIELDS = {
    "dirty_counter_a": OFFSETS["dirty_counter_a"],  # 0x1a0 - caused the crash when written to
    "dirty_counter_b": OFFSETS["dirty_counter_b"],  # 0x1a8 - caused the crash when written to
    "dirty_flag": OFFSETS["dirty_flag"],            # 0x2ac - untested, not yet written to
    "inventory_count": OFFSETS["inventory_slots"] + 0x8,  # 0x308 - known TArray count, for context
}


def monitor_counters(pm, base, poll_interval=0.5):
    """READ-ONLY. Re-resolves ItemStashComponent every poll (instead of
    using one fixed address for the whole session) - the first version of
    this function held a single address across the entire monitoring run,
    which produced physically-impossible jumps (e.g. inventory_count
    18 -> -4194304) that are the classic signature of reading stale/freed
    memory after the component got destroyed and recreated (zone change,
    respawn, certain menu transitions all can do this). Re-deriving the
    pointer each poll - the same approach watch_session already uses for
    `world` - avoids that entirely, and also lets us directly see if/when
    the address itself changes.
    """
    import time

    print("Read-only monitor - no writes will happen. Play normally (pickups, shop, "
          "storage, leveling up, etc). Ctrl+C to stop.\n")

    item_stash, error = get_item_stash_component(pm, base)
    if not item_stash:
        print(f"Could not reach ItemStashComponent yet: {error}. Will keep retrying.")

    last_values = {}
    if item_stash:
        for name, offset in WATCHED_FIELDS.items():
            try:
                last_values[name] = pm.read_int(item_stash + offset)
            except Exception:
                last_values[name] = None
        print(f"Stash address: {hex(item_stash)}")
        print("Starting values:", last_values, "\n")

    try:
        while True:
            time.sleep(poll_interval)

            current_stash, error = get_item_stash_component(pm, base)
            if not current_stash:
                if item_stash is not None:
                    print(f"  [ItemStashComponent unreachable: {error} - probably loading/menu]")
                item_stash = None
                continue

            if current_stash != item_stash:
                print(f"\n  *** Stash address changed: {hex(item_stash) if item_stash else 'None'} "
                      f"-> {hex(current_stash)} - component was recreated, resetting baseline ***")
                item_stash = current_stash
                last_values = {}
                for name, offset in WATCHED_FIELDS.items():
                    try:
                        last_values[name] = pm.read_int(item_stash + offset)
                    except Exception:
                        last_values[name] = None
                print("  New starting values:", last_values, "\n")
                continue

            for name, offset in WATCHED_FIELDS.items():
                try:
                    current = pm.read_int(item_stash + offset)
                except Exception:
                    continue
                previous = last_values.get(name)
                if previous is not None and current != previous:
                    delta = current - previous
                    sign = "+" if delta > 0 else ""
                    print(f"  {name} ({hex(offset)}): {previous} -> {current}  ({sign}{delta})")
                last_values[name] = current
    except KeyboardInterrupt:
        print("\nStopped. Final values:", last_values)


def scan_stash_diff(pm, item_stash_address, window=0x400):
    try:
        before = pm.read_bytes(item_stash_address, window)
    except Exception as e:
        print(f"Couldn't read stash memory: {e}")
        return []

    input("Snapshot taken. Now go pick up ANY real item in-game, then press Enter here...")

    try:
        after = pm.read_bytes(item_stash_address, window)
    except Exception as e:
        print(f"Couldn't re-read stash memory: {e}")
        return []

    changed = []
    for offset in range(0, window - 4, 4):  # 4-byte aligned scan - counters/flags are almost always aligned
        old_val = int.from_bytes(before[offset:offset + 4], "little")
        new_val = int.from_bytes(after[offset:offset + 4], "little")
        if old_val != new_val:
            changed.append((offset, old_val, new_val))

    if not changed:
        print("No 4-byte-aligned changes detected in this window. Try a larger --window, "
              "or the field may live on the Pawn/PlayerController instead of the stash - "
              "let me know and we'll widen the scan target.")
        return []

    # increments-by-exactly-1 are the strongest "dirty counter" signal
    def rank(entry):
        _, old_val, new_val = entry
        return 0 if new_val == old_val + 1 else 1

    changed.sort(key=rank)

    print(f"\n{len(changed)} changed offset(s) on ItemStashComponent (relative to its base address):")
    for offset, old_val, new_val in changed[:20]:
        note = "  <-- incremented by 1, likely the version/dirty counter" if new_val == old_val + 1 else ""
        print(f"  offset {hex(offset)}: {old_val} -> {new_val}{note}")

    return changed


def scan_identity_field(pm, item_stash_address, window_before=0x20, window_after=0x300):
    """Grouped memory diff - looks for a byte offset that's identical
    within every slot sharing the same displayed name, but differs
    between names. Restored here after it got orphaned as dead code in a
    previous edit - this is the tool that originally found display_id."""
    items = read_inventory(pm, item_stash_address)

    groups = {}
    for it in items:
        if it["name"] not in NAME_LOOKUP.values():
            continue  # skip unrecognised items - we need trusted grouping
        inventory_item_addr = it["address"] - OFFSETS["item_struct"]
        groups.setdefault(it["name"], []).append(inventory_item_addr)

    if len(groups) < 2:
        print("Need at least 2 different confirmed item types in your inventory to compare. "
              "Pick up/keep a couple more known item types and try again.")
        return []

    print("Groups being compared:")
    for name, addrs in groups.items():
        print(f"  {name}: {len(addrs)} instance(s)")

    span = window_before + window_after
    raw = {}
    for name, addrs in groups.items():
        blobs = []
        for addr in addrs:
            try:
                blobs.append(pm.read_bytes(addr - window_before, span))
            except Exception:
                continue
        if blobs:
            raw[name] = blobs

    if len(raw) < 2:
        print("Couldn't read enough live instances to compare - try again in-game.")
        return []

    candidates = []
    for offset in range(0, span - 8):
        per_group_value = {}
        consistent = True
        for name, blobs in raw.items():
            values = {int.from_bytes(b[offset:offset + 8], "little") for b in blobs}
            if len(values) != 1:
                consistent = False
                break
            per_group_value[name] = next(iter(values))
        if not consistent:
            continue
        if len(set(per_group_value.values())) == len(per_group_value):
            # every group internally agrees, AND every group disagrees with every other group
            real_offset = offset - window_before
            multi_instance_confidence = sum(1 for name in per_group_value if len(raw[name]) >= 2)
            candidates.append((real_offset, per_group_value, multi_instance_confidence))

    # prefer candidates backed by multi-instance groups (stronger signal),
    # then aligned offsets (real pointers/enums are 4- or 8-byte aligned -
    # unaligned hits are usually just a byte-shifted overlap of an aligned
    # one right next to it), then proximity to the item_struct we know
    def alignment_rank(offset):
        if offset % 8 == 0:
            return 0
        if offset % 4 == 0:
            return 1
        return 2

    candidates.sort(key=lambda c: (-c[2], alignment_rank(c[0]), abs(c[0])))

    print(f"\n{len(candidates)} candidate offset(s), relative to the InventoryItem base "
          f"(item_struct is at +{hex(OFFSETS['item_struct'])} from here). Aligned offsets "
          f"ranked first - unaligned neighbors of a real hit are usually just overlap noise:")
    for real_offset, values, confidence in candidates[:15]:
        print(f"  offset {hex(real_offset)}  (backed by {confidence} multi-instance group(s)):")
        for name, val in values.items():
            print(f"      {name:20s} = {hex(val)}")

    return candidates


# ============================================================
# Boss-kill detection (merged from the bosskill_ branch)
# ============================================================

BOSS_ENTITY_TYPE_IDS = {
    2630: "Redstone Monstrosity",
    2643: "Corrupted Cauldron",
    2645: "Arch-Illager",
    2691: "Mooshroom Monstrosity",
    284: "Treetop Whisperer",
    210: "Jungle Abomination",
    244: "Tempest Golem",
    316: "Ancient Guardian",
    1116899: "Wretched Wraith",
    1116748: "Nameless One",
}
# Confirmed directly from the game's own Dungeons.EntityType enum (Dumper-7
# SDK dump) - fixed, unambiguous integer IDs, not something that needs
# empirical labeling like class_name_index does. 10 of 12 bosses covered;
# Heart of Ender and Vengeful Heart of Ender still fall back to
# BOSS_CLASS_LOOKUP/document_bosses below.

BOSS_CLASS_LOOKUP = load_lookup("boss_class_lookup.json")

BOSS_MOB_ID_HINTS = {
    "Arch-Illager": "archillager",
    "Corrupted Cauldron": "cauldronboss",
    "Mooshroom Monstrosity": "mooshroommonstrosity",
    "Tempest Golem": "tempestgolem",
    "Treetop Whisperer": "whisperer",
    "Ancient Guardian": "Ancient_guardian",
    "Jungle Abomination": "jungleabomination",
    "Nameless One": "namelessking",
    "Redstone Monstrosity": "redstonemonstrosity",
    "Wretched Wraith": "wickedwraith",
}
# Best-guess internal mob id (mob_id_order.json) hint for labeling via
# document_bosses/prompt_boss_label - not confirmed class_name_index values.

BOSS_KILLS_FILE = "boss_kills_claimed.json"


def get_actor_class_name_index(pm, actor_addr):
    """Same actor -> UClass -> Name chain scan_level_actors/scan_full_zone
    use internally, exposed standalone for when you only have a bare
    actor address (e.g. from a death event) rather than a full scan."""
    try:
        actor_class = pm.read_longlong(actor_addr + 0x10)
        if not actor_class:
            return None
        return pm.read_int(actor_class + 0x18)
    except Exception:
        return None

def get_actor_entity_type(pm, actor_addr):
    """Reads AMobCharacter::EntityType directly - only meaningful for
    enemies (AMobCharacter), not the player's own pawn (APlayerCharacter
    doesn't have this field). Returns None on a failed/out-of-bounds read
    rather than raising, same convention as get_actor_class_name_index."""
    try:
        return pm.read_int(actor_addr + OFFSETS["entity_type"])
    except Exception:
        return None

def resolve_boss_for_actor(pm, actor_addr):
    """Best-effort boss identification for one actor address. Tries the
    confirmed EntityType field first (BOSS_ENTITY_TYPE_IDS - reliable,
    zero labeling needed, covers 10/12 bosses); falls back to the older
    class_name_index + BOSS_CLASS_LOOKUP path (still needed for Heart of
    Ender / Vengeful Heart of Ender until their EntityType is found).
    Returns the boss's display name, or None if neither resolves."""
    entity_type = get_actor_entity_type(pm, actor_addr)
    if entity_type in BOSS_ENTITY_TYPE_IDS:
        return BOSS_ENTITY_TYPE_IDS[entity_type]
    cls = get_actor_class_name_index(pm, actor_addr)
    if cls in BOSS_CLASS_LOOKUP:
        return BOSS_CLASS_LOOKUP[cls]
    return None

def resolve_boss_name(typed):
    """Matches free-typed input against BOSS_NAMES: exact match (case-
    insensitive) preferred, then a unique substring match, then a fuzzy
    typo-tolerant match (difflib) as a last resort - "restone monstosity"
    should still find "Redstone Monstrosity" instead of silently failing
    and getting the class marked as skipped/ignored. Returns the
    canonical name or None if nothing resolves uniquely."""
    from _apworld_data import Locations as _apw
    BOSS_NAMES = _apw.BOSS_NAMES
    typed_lower = typed.strip().lower()
    if len(typed_lower) < 3:
        # too short to mean anything - avoids e.g. "b" alone substring-
        # matching "Jungle Abomination" by accident
        return None
    for name in BOSS_NAMES:
        if name.lower() == typed_lower:
            return name
    matches = [name for name in BOSS_NAMES if typed_lower in name.lower()]
    if len(matches) == 1:
        return matches[0]
    import difflib
    close = difflib.get_close_matches(typed_lower, [n.lower() for n in BOSS_NAMES], n=1, cutoff=0.6)
    if close:
        return next(name for name in BOSS_NAMES if name.lower() == close[0])
    return None

def save_boss_label(cls, resolved_name):
    """Saves cls -> resolved_name in BOSS_CLASS_LOOKUP. resolved_name must
    already be a canonical BOSS_NAMES entry (run it through
    resolve_boss_name first) - this function doesn't validate it again."""
    hint = BOSS_MOB_ID_HINTS.get(resolved_name)
    if hint:
        print(f"      (mob_id_order.json hint for {resolved_name}: '{hint}' - not confirmed, just a pointer)")
    BOSS_CLASS_LOOKUP[cls] = resolved_name
    save_lookup("boss_class_lookup.json", BOSS_CLASS_LOOKUP)
    print(f"      Saved class_name_index={cls} as boss '{resolved_name}'.")

def prompt_boss_label(cls):
    """Interactive labeling for one class_name_index as a boss - shows the
    full BOSS_NAMES list, then asks for the name and saves it via
    save_boss_label if it resolves to exactly one canonical boss name.
    Returns True if it saved a label, False otherwise (blank input or no
    unique match) - callers use this to know whether to treat the class
    as still unresolved."""
    from _apworld_data import Locations as _apw
    BOSS_NAMES = _apw.BOSS_NAMES
    print("      Bosses: " + ", ".join(BOSS_NAMES))
    typed = input("      boss name (must match one of the above): ").strip()
    if not typed:
        return False
    resolved = resolve_boss_name(typed)
    if not resolved:
        print(f"      '{typed}' didn't match exactly one boss name - skipped.")
        return False
    save_boss_label(cls, resolved)
    return True

def get_death_events(pm):
    """Drains dungeons_bridge.dll's OnCharacterDeath event queue - one
    entry per character (player, mob, OR boss - they're all
    ABaseCharacter) that has died since the last call. Returns a list of
    actor addresses (ints), or None on connection failure (caller decides
    how to handle that - e.g. fall back to the disappearance heuristic).
    This is the reliable replacement for watch_boss_kills' old
    'the actor vanished from a scan' guess: the game itself tells us the
    exact moment and exact actor, via a real reflected UFunction call -
    no despawn/streaming ambiguity, no proximity heuristic needed."""
    try:
        import win32file
    except ImportError:
        return None

    try:
        pipe = win32file.CreateFile(
            _pipe_name_for(pm), win32file.GENERIC_READ | win32file.GENERIC_WRITE,
            0, None, win32file.OPEN_EXISTING, 0, None
        )
    except Exception:
        return None

    try:
        win32file.WriteFile(pipe, b"get_death_events")
        _, data = win32file.ReadFile(pipe, 65536)
    finally:
        win32file.CloseHandle(pipe)

    response = data.decode()
    if not response.startswith("EVENTS:"):
        return None

    body = response[len("EVENTS:"):]
    count_str, _, rest = body.partition("|")
    if count_str == "0" or not rest:
        return []
    return [int(addr_hex, 16) for addr_hex in rest.split("|") if addr_hex]

def get_wallet_component(pm, base):
    """Resolves the live UWalletComponent* for the local player, via
    Pawn -> +OFFSETS['wallet_component'] (0xE78, confirmed straight from
    Dungeons_classes.hpp). Returns (wallet_ptr, error)."""
    pawn, err = get_pawn(pm, base)
    if not pawn:
        return None, err or "No Pawn - character not spawned yet?"
    try:
        wallet = pm.read_longlong(pawn + OFFSETS["wallet_component"])
    except Exception as e:
        return None, f"couldn't read wallet_component: {e}"
    if not wallet:
        return None, "wallet_component is null - HUD/inventory may not have initialized yet"
    return wallet, None


def read_currency_slots(pm, wallet_ptr, max_slots=16):
    """Reads UWalletComponent::mCurrencySlots (TArray<UItemSlot*> at
    +OFFSETS['currency_slots'], confirmed via Dumper-7), same TArray
    layout as read_inventory (data ptr at +0x0, count at +0x8). Each
    UItemSlot's balance lives at +OFFSETS['slot_count'] (confirmed
    UItemSlot::Count, +0x1FC). Returns a list of
    {"slot": i, "address": item_slot_addr, "count": int}. Which slot
    index is Emeralds/Gold/EyesOfEnder is NOT yet confirmed - use
    /dump_currency_slots in-game, compare each printed count against
    your on-screen HUD numbers, and set EMERALD_SLOT_INDEX accordingly
    (see below)."""
    slots, _diag = read_currency_slots_diag(pm, wallet_ptr, max_slots)
    return slots


def read_currency_slots_diag(pm, wallet_ptr, max_slots=16):
    """Same as read_currency_slots but also returns a diagnostic dict
    (data_ptr, count, any exception) so an empty result can be told
    apart from 'count genuinely reads as 0' vs 'the read itself
    failed' vs 'currency_slots offset is pointing at the wrong place
    entirely' - a bare empty list can't distinguish those, which
    matters when currency_slots (+0x130) hasn't been cross-checked
    against a live TArray the way inventory_slots/item_stash has."""
    tarray_header = wallet_ptr + OFFSETS["currency_slots"]
    diag = {"tarray_header": tarray_header, "data_ptr": None, "count": None, "error": None}
    try:
        diag["data_ptr"] = pm.read_longlong(tarray_header)
        diag["count"] = pm.read_int(tarray_header + 0x8)
    except Exception as e:
        diag["error"] = str(e)
        return [], diag

    data_ptr, count = diag["data_ptr"], diag["count"]
    slots = []
    if data_ptr and 0 < count <= 10000:
        for i in range(min(count, max_slots)):
            try:
                item_slot = pm.read_longlong(data_ptr + i * 8)
                if not item_slot:
                    continue
                value = pm.read_int(item_slot + OFFSETS["slot_count"])
            except Exception:
                continue
            slots.append({"slot": i, "address": item_slot, "count": value})
    return slots, diag


def scan_for_currency_slots_array(pm, wallet_ptr, window=0x400, max_count=10):
    """Fallback for when currency_slots (+0x130) reads as empty/wrong:
    scans every 8-byte-aligned field on the wallet object itself for
    something that LOOKS like a TArray<UItemSlot*> header - a plausible
    heap pointer immediately followed (at +0x8) by a small positive
    count (1..max_count), where every entry it points to is itself a
    plausible pointer whose +OFFSETS['slot_count'] reads as a sane
    int32. Same brute-force-a-known-shape idiom used throughout this
    file's offset-discovery tooling, for when the Dumper-7 offset
    (+0x130) doesn't match what's actually live (build drift, wrong
    struct picked, or the field simply isn't populated at that address
    for this save). Returns a list of candidate offsets with what
    they'd decode to, for manual comparison against the real HUD."""
    try:
        data = pm.read_bytes(wallet_ptr, window)
    except Exception as e:
        return [{"error": str(e)}]

    candidates = []
    for local_offset in range(0, window - 16, 8):
        ptr = int.from_bytes(data[local_offset:local_offset + 8], "little")
        cnt = int.from_bytes(data[local_offset + 8:local_offset + 12], "little", signed=True)
        if not (0x10000 < ptr < 0x7FFFFFFFFFFF) or not (0 < cnt <= max_count):
            continue
        decoded = []
        ok = True
        try:
            for i in range(cnt):
                item_slot = pm.read_longlong(ptr + i * 8)
                if not item_slot:
                    ok = False
                    break
                value = pm.read_int(item_slot + OFFSETS["slot_count"])
                decoded.append({"slot": i, "address": item_slot, "count": value})
        except Exception:
            ok = False
        if ok and decoded:
            candidates.append({"offset": local_offset, "data_ptr": ptr, "count": cnt, "slots": decoded})
    return candidates


def get_anchors(pm, base):
    """Resolves every known stable structure we can currently reach -
    world, game_instance, local_player, player_controller, pawn,
    item_stash, wallet - so a not-yet-placed address/value can be
    compared against each (see find_currency_array below) to figure out
    which object it actually lives on."""
    anchors = {}
    try:
        world = pm.read_longlong(base + OFFSETS["gworld"])
        anchors["world"] = world
        if world:
            game_instance = pm.read_longlong(world + OFFSETS["game_instance"])
            anchors["game_instance"] = game_instance
            if game_instance:
                local_players_data = pm.read_longlong(game_instance + OFFSETS["local_players"])
                if local_players_data:
                    local_player = pm.read_longlong(local_players_data)
                    anchors["local_player"] = local_player
                    if local_player:
                        player_controller = pm.read_longlong(local_player + OFFSETS["player_controller"])
                        anchors["player_controller"] = player_controller
                        if player_controller:
                            pawn = pm.read_longlong(player_controller + OFFSETS["pawn"])
                            anchors["pawn"] = pawn
                            if pawn:
                                item_stash = pm.read_longlong(pawn + OFFSETS["item_stash"])
                                anchors["item_stash"] = item_stash
                                wallet = pm.read_longlong(pawn + OFFSETS["wallet_component"])
                                anchors["wallet"] = wallet
    except Exception:
        pass
    return {k: v for k, v in anchors.items() if v}


def scan_object_for_currency_array(pm, obj_addr, real_values, window=0x800, max_count=10):
    """Like scan_for_currency_slots_array, but scans an arbitrary object
    (not just a resolved 'wallet') and only returns candidates whose
    decoded slot counts include at least one of `real_values` (the
    actual HUD numbers you just read - Emeralds/other-currency/Eyes of
    Ender) - filters out address-shaped noise that happens to look like
    a small TArray but decodes to nothing meaningful."""
    try:
        data = pm.read_bytes(obj_addr, window)
    except Exception as e:
        return [{"error": str(e)}]

    real_set = set(real_values)
    candidates = []
    for local_offset in range(0, window - 16, 8):
        ptr = int.from_bytes(data[local_offset:local_offset + 8], "little")
        cnt = int.from_bytes(data[local_offset + 8:local_offset + 12], "little", signed=True)
        if not (0x10000 < ptr < 0x7FFFFFFFFFFF) or not (0 < cnt <= max_count):
            continue
        decoded = []
        ok = True
        try:
            for i in range(cnt):
                item_slot = pm.read_longlong(ptr + i * 8)
                if not item_slot:
                    ok = False
                    break
                value = pm.read_int(item_slot + OFFSETS["slot_count"])
                decoded.append({"slot": i, "address": item_slot, "count": value})
        except Exception:
            ok = False
        if ok and decoded and (real_set & {d["count"] for d in decoded}):
            candidates.append({"offset": local_offset, "data_ptr": ptr, "count": cnt, "slots": decoded})
    return candidates


def find_currency_array(pm, base, real_values, window=0x800, max_count=10):
    """Broader fallback for when neither the confirmed currency_slots
    offset (+0x130 off wallet_component) nor a raw scan right at the
    wallet address turn up anything real: scans every resolvable anchor
    object (pawn, wallet_component, item_stash, player_controller,
    local_player, game_instance - via get_anchors) for a TArray-shaped
    region whose decoded counts include at least one of your real,
    just-read HUD numbers. Confirms not just "looks like a TArray" but
    "actually holds a number you can see on screen right now" - the
    strongest signal available without the Dumper-7 offset panning out
    live. Returns a list of {"object": anchor_name, "offset":, ...}."""
    anchors = get_anchors(pm, base)
    results = []
    for name, addr in anchors.items():
        for c in scan_object_for_currency_array(pm, addr, real_values, window, max_count):
            if "error" not in c:
                results.append({"object": name, "object_address": addr, **c})
    return results


# Which index into mCurrencySlots is Emeralds. NOT hardcoded/guessed -
# set this only after confirming it live with /dump_currency_slots
# (compare each slot's printed count to the real on-screen Emeralds
# HUD number - whichever slot's count matches IS EMERALD_SLOT_INDEX).
# None means "not confirmed yet" - apply_emerald_reward refuses to
# guess and returns False rather than silently writing to the wrong
# currency (e.g. Gold or Eyes of Ender) if this is left unset.
EMERALD_SLOT_INDEX = None


def read_widget_wallet_info(pm, widget_addr):
    """Reads a UMG_CurrencyCounterBase_C widget's own cached fields
    (from the Dumper-7-confirmed layout found earlier: mWallet at
    +0x0248, CurrencyItemId (FSerializableItemId) at +0x0250) directly
    off widget_addr - the widget addresses already come for free from
    get_currency_value_events/get_currency_type_events, which are
    confirmed live and correctly reporting real HUD values (274/801 in
    a real session) even though OnCurrencyTypeChanged itself doesn't
    reliably (re)fire. This sidesteps the Pawn -> +0xE78 wallet_component
    chain entirely - useful when that chain resolves to a non-null
    pointer whose mCurrencySlots still reads empty, to tell apart
    "wrong wallet object" from "right wallet, wrong currency_slots
    offset". Returns {"wallet_ptr": int|None, "currency_item_id_raw": int|None}."""
    result = {"wallet_ptr": None, "currency_item_id_raw": None}
    try:
        result["wallet_ptr"] = pm.read_longlong(widget_addr + 0x0248) or None
    except Exception:
        pass
    try:
        result["currency_item_id_raw"] = pm.read_longlong(widget_addr + 0x0250)
    except Exception:
        pass
    return result


def find_currency_slot_by_value(pm, wallet_ptr, real_value, tolerance=0):
    """Diagnostic helper: reads every currency slot and returns the
    slot index/address whose count matches real_value (the number you
    read off the HUD right before calling this) within `tolerance`.
    Use this once, in-game, to pin down EMERALD_SLOT_INDEX - same
    'known real value narrows an unknown slot/offset' idiom used
    elsewhere in this file (scan_currency_offsets)."""
    hits = []
    for s in read_currency_slots(pm, wallet_ptr):
        if abs(s["count"] - real_value) <= tolerance:
            hits.append(s)
    return hits


# CurrencyItemId values (FName ComparisonIndex for FSerializableItemId::
# SerializedId) - NOT guessed, empirically matched live: the currency-
# widget hook (dungeons_bridge.cpp's OnCurrencyTypeChanged capture) reads
# this exact same ComparisonIndex off each HUD counter widget, and each
# one was matched against the real on-screen HUD value at the same
# instant (Emeralds: widget showing 274 came from ItemId 5294; Eyes of
# Ender: widget showing 801 came from ItemId 5105; Gold: confirmed
# directly by the player). Number (FName's other int32 half) is 0 for
# all three - never observed otherwise.
EMERALD_CURRENCY_ITEM_ID = 5294
EYES_OF_ENDER_CURRENCY_ITEM_ID = 5105
GOLD_CURRENCY_ITEM_ID = 5301


def _pack_serializable_item_id(comparison_index):
    """Builds the raw 0x14-byte FSerializableItemId parameter blob -
    Pad_0[0xC] (12 bytes, irrelevant/unused padding per Dumper-7) +
    FName SerializedId (int32 ComparisonIndex, int32 Number) - exact
    layout confirmed from the real Dungeons_structs.hpp dump, matching
    what dungeons_bridge.cpp's currency-widget hook already assumed for
    reading the SAME struct (that assumption is what let the
    EMERALD_CURRENCY_ITEM_ID constants above get confirmed live in the
    first place)."""
    return b"\x00" * 12 + struct.pack("<ii", comparison_index, 0)


def find_wallet_add_function(pm, wallet_class):
    """Locates UWalletComponent::ClientAdd's live UFunction address on
    wallet_class, STRUCTURALLY (by exact parameter-struct size), not by
    name resolution - FNamePool-based name lookup was confirmed
    unreliable this session (the currency-widget hook's
    OnCurrencyTypeChanged classification never resolved a name), so this
    mirrors call_is_mission_completed's same structural approach.

    WalletComponent_ClientAdd's param struct is 0x1C (28) bytes -
    confirmed from Dumper-7's Dungeons_parameters.hpp - and is the ONLY
    one of WalletComponent's three currency functions at that exact
    size (Deduct and Balance are both 0x18/24 bytes, indistinguishable
    from each other by size alone - deliberately not needed here, since
    only ClientAdd is required to fix reward granting). An exact-size
    filter should isolate ClientAdd on its own, without needing a
    persisted/calibrated index the way IS_MISSION_COMPLETED_INDEX needs
    one (there, multiple same-sized candidates existed on that class).

    require_const=False because ClientAdd is not const (only Balance()
    is). require_final=False because ClientAdd's real flags - confirmed
    from Dungeons_functions.cpp's own comment above its implementation -
    are (Net, NetReliable, Native, Event, Public, NetClient,
    BlueprintCallable), NOT Final - unlike Deduct/Balance, which both
    are Final. Requiring Final here (the default) silently excluded the
    only real candidate outright.

    Returns (func_addr, error) - func_addr is None with error set if the
    candidate count isn't exactly 1, since a wrong count means the
    size-uniqueness assumption doesn't hold on this build and calling
    the wrong function blind would be worse than refusing outright."""
    candidates = find_functions_on_class(pm, wallet_class, min_size=28, max_size=28,
                                          require_const=False, require_final=False)
    if len(candidates) != 1:
        return None, (f"expected exactly 1 candidate 28-byte-param function on "
                       f"WalletComponent (ClientAdd) but found {len(candidates)} - "
                       f"refusing to guess which one")
    return candidates[0][0], None


def apply_emerald_reward(pm, base, amount):
    """Adds `amount` to the player's current emerald total by calling
    the REAL UWalletComponent::ClientAdd(Type, Amount, reason) UFUNCTION
    via ProcessEvent (same remote-call technique already used for
    call_is_mission_completed) - NOT a raw memory write.

    This replaces TWO earlier, both-disproven approaches in order:
      1. currency_ptr/CURRENCY_OFFSETS (dungeons_bridge.dll hook +
         guessed fixed offsets into what turned out to not even be one
         shared object) - disproven live: a grant's baseline read came
         back as a real EYES OF ENDER balance, not emeralds, and the
         write landed on Eyes of Ender instead.
      2. mCurrencySlots[EMERALD_SLOT_INDEX]::Count raw write (the
         wallet object itself WAS confirmed correct this time) -
         disproven live too: mCurrencySlots read back as a genuinely
         empty array (data_ptr=0, count=0) on the real, correct wallet
         object - the real data isn't reachable as a plain TArray read
         at all.

    ClientAdd is what the game's OWN pickup code calls - it's marked
    NetClient/NetReliable in the real UFunction flags, meaning calling
    it client-side triggers the actual replicated pickup flow, the same
    as walking over a real emerald pile. That makes it far more likely
    to be correctly observed by the HUD, by shops, and by the server's
    own replication, than any direct memory write ever could be -
    exactly the mismatch (grants landing somewhere our own reads could
    verify, but never visible in the actual game) that motivated
    ditching memory-write approaches for this entirely.

    Returns True on success. Returns False (caller should NOT mark the
    reward applied, so it retries later) if:
      - the wallet component isn't resolvable yet (character not
        spawned, HUD/inventory not opened this session)
      - ClientAdd's UFunction can't be uniquely located this call (see
        find_wallet_add_function's own docstring)
      - the remote ProcessEvent call itself fails/throws

    Deliberately does NOT try to read back and verify the new balance
    here (unlike the old memory-write version) - there's no cheap,
    reliable read path left to check against (Balance() shares its
    param size with Deduct(), so it can't be uniquely located the same
    structural way without a calibrated index, which is out of scope
    for fixing the write path specifically). The EXISTING pending-grant
    stability-check machinery in dungeons_ap_client.py already re-reads
    the live balance a few seconds later via its own independent path
    and re-grants if it doesn't hold - that external verification layer
    covers this without apply_emerald_reward needing its own.

    Returns (success, error) - error is a short human-readable string
    naming which step failed whenever success is False, instead of a
    bare bool a caller can't do anything with beyond "it didn't work" -
    every one of this function's several early-return points used to
    return that same uninformative False, which is exactly why a whole
    session of silent failures here produced nothing in the log at
    all."""
    wallet_ptr, err = get_wallet_component(pm, base)
    if not wallet_ptr:
        return False, f"no wallet component: {err}"
    try:
        wallet_class = pm.read_longlong(wallet_ptr + OFFSETS["uobject_class"])
    except Exception as e:
        return False, f"couldn't read wallet's uobject_class: {e}"
    if not wallet_class:
        return False, "wallet's uobject_class read as null"

    func_addr, err = find_wallet_add_function(pm, wallet_class)
    if not func_addr:
        return False, f"couldn't locate ClientAdd: {err}"

    params_bytes = (
        _pack_serializable_item_id(EMERALD_CURRENCY_ITEM_ID)
        + struct.pack("<i", amount)
        + bytes([2])          # ECurrencyObtainReason::Pickup = 2 - mimics a normal
                               # in-game pickup, confirmed from the real enum values
        + b"\x00" * 3          # Pad_19[0x3] - struct-size padding, per
                               # WalletComponent_ClientAdd's real layout
    )
    success, err, _params_after = call_process_event(pm, wallet_ptr, func_addr, params_bytes,
                                                        force_native=True)
    if not success:
        return False, f"call_process_event failed: {err}"
    return True, None


def find_wallet_balance_function(pm, wallet_class):
    """Locates UWalletComponent::Balance's live UFunction address,
    structurally, mirroring find_wallet_add_function - same 0x18-byte
    param-struct size as Deduct(), but Balance is the only one of the
    two that's Const (confirmed from Dungeons_functions.cpp's own flag
    comments: both are Final|Native|Public, but only Balance carries
    Const) - find_functions_on_class's require_const=True (its default)
    isolates it on its own without needing a calibrated index. Returns
    (func_addr, error)."""
    candidates = find_functions_on_class(pm, wallet_class, min_size=24, max_size=24,
                                          require_const=True, require_final=True)
    if len(candidates) != 1:
        return None, (f"expected exactly 1 candidate 24-byte-param Const function on "
                       f"WalletComponent (Balance) but found {len(candidates)} - "
                       f"refusing to guess which one")
    return candidates[0][0], None


def read_current_emeralds(pm, base):
    """Reads the player's real current emerald total via the REAL
    UWalletComponent::Balance(Type) UFUNCTION (ProcessEvent, same
    technique as apply_emerald_reward/call_is_mission_completed) -
    replacing the old currency_ptr/CURRENCY_OFFSETS polling (which read
    the wrong object entirely - a real Eyes of Ender balance came back
    labeled 'emeralds' - and needed a 'plausibility jump' filter to
    paper over it skipping/spamming every time it did). Balance() is
    ground truth: no filtering needed, and none is applied here.

    Returns (balance_or_None, error). None means unavailable this tick
    (character not spawned, Balance() not uniquely locatable, or the
    remote call itself failed) - caller should skip this poll, not
    treat None as zero."""
    wallet_ptr, err = get_wallet_component(pm, base)
    if not wallet_ptr:
        return None, f"no wallet component: {err}"
    try:
        wallet_class = pm.read_longlong(wallet_ptr + OFFSETS["uobject_class"])
    except Exception as e:
        return None, f"couldn't read wallet's uobject_class: {e}"
    if not wallet_class:
        return None, "wallet's uobject_class read as null"

    func_addr, err = find_wallet_balance_function(pm, wallet_class)
    if not func_addr:
        return None, f"couldn't locate Balance: {err}"

    params_bytes = _pack_serializable_item_id(EMERALD_CURRENCY_ITEM_ID) + b"\x00" * 4  # ReturnValue slot
    success, err, params_after = call_process_event(pm, wallet_ptr, func_addr, params_bytes,
                                                       force_native=True)
    if not success:
        return None, f"call_process_event failed: {err}"
    balance = int.from_bytes(params_after[0x14:0x18], "little", signed=True)
    return balance, None


def load_claimed_boss_kills():
    if os.path.exists(BOSS_KILLS_FILE):
        with open(BOSS_KILLS_FILE) as f:
            return set(json.load(f))
    return set()

def save_claimed_boss_kills(claimed):
    with open(BOSS_KILLS_FILE, "w") as f:
        json.dump(sorted(claimed), f)

def document_bosses(pm, base, poll_interval=2.0, min_persistence=15):
    """Dedicated workflow for filling in BOSS_CLASS_LOOKUP for every name
    in BOSS_NAMES - no AP connection, no location checks, just labeling.

    Best run once you're actually inside the boss's arena/room, not
    during general mission exploration - a normal mission throws up
    dozens of distinct enemy *variants* (pillager/zombie/piglin variant0/
    1/2, ancient versions, etc. - see mob_id_order.json), each of which
    can legitimately be the only one of its kind alive for a while. None
    of those are the boss, but without narrowing to count==1 candidates
    they used to flood this tool exactly like the actual garbage/VFX
    noise did. Two things fixed that, after two real runs both flooded:

      1. Classes with count >= 2 are never prompted at all - silently
         absorbed into seen_classes. A boss is always exactly one
         instance; anything appearing more than once, even briefly,
         cannot be a boss and isn't worth interrupting you for here (use
         watch_level's general prompts if you also want those labeled).
      2. A count==1 class only becomes a candidate after persisting
         `min_persistence` consecutive polls (default 15 - at the default
         2s poll_interval that's 30 seconds continuously alive and alone,
         well past both a VFX/projectile flicker AND a regular unique
         enemy's usual lifespan, but well within a boss fight's). Raise
         this further if a tough regular enemy still slips through;
         lower it if you're missing a genuinely fast boss kill.

    Implausible class_name_index reads (negative, or absurdly large -
    definitely not a real FName table index) are also dropped silently
    regardless of count.

    If PAWN_HEALTH_OFFSET is already confirmed (see get_pawn_health), each
    prompt also shows the candidate's current HP - a boss's HP dwarfs a
    regular enemy's, so this is usually a much faster/more certain tell
    than waiting out the persistence timer. Shows nothing if the offset
    isn't confirmed yet or the read fails, same as anywhere else it's used.

    Shows overall progress up front, skips anything already in
    CHEST_/ENEMY_/BOSS_CLASS_LOOKUP. Run this once per mission (same
    pattern as watch_level/watch_boss_kills - restart it when you move to
    the next mission) until every name in BOSS_NAMES is confirmed.
    """
    import time
    from _apworld_data import Locations as _apw
    BOSS_NAMES = _apw.BOSS_NAMES

    world = pm.read_longlong(base + OFFSETS["gworld"])
    if not world:
        print("No UWorld - are you in a level?")
        return

    def print_progress():
        confirmed = set(BOSS_CLASS_LOOKUP.values()) & set(BOSS_NAMES)
        missing = [b for b in BOSS_NAMES if b not in confirmed]
        print(f"Boss labeling progress: {len(confirmed)}/{len(BOSS_NAMES)} confirmed.")
        if missing:
            print(f"Still missing: {', '.join(missing)}")
        else:
            print("All bosses confirmed!")

    def is_plausible(cls):
        # A real class_name_index is a small non-negative FName table
        # index - Skeleton/Zombie's confirmed values are in the low
        # hundred-thousands. Anything negative or wildly large is a bad
        # read (dereferenced a bogus/transient pointer), not a real class.
        return isinstance(cls, int) and 0 <= cls <= 10_000_000

    print_progress()
    print()

    seen_classes = set(ENEMY_CLASS_LOOKUP) | set(BOSS_CLASS_LOOKUP)
    skipped = load_lookup("boss_labeling_skipped.json")  # {class_name_index: True}
    seen_classes |= set(skipped)
    if skipped:
        print(f"({len(skipped)} previously-ignored class(es) loaded from boss_labeling_skipped.json - won't re-ask)")
    candidate_first_seen = {}  # class_name_index -> poll index it first appeared
    poll_index = 0
    garbage_filtered = 0
    crowd_absorbed = 0  # count>=2 classes silently marked seen, for the progress line

    print(f"Watching for a solo boss-like actor (count==1, persisting "
          f"{min_persistence}+ polls in a row = "
          f"~{int(min_persistence * poll_interval)}s alone) - best done inside the "
          "boss's own arena/room. Anything appearing more than once is ignored "
          "automatically. Ctrl+C to stop - nothing is lost, everything saves as "
          "you go.\n")

    try:
        while True:
            time.sleep(poll_interval)
            poll_index += 1
            try:
                snapshot = scan_full_zone(pm, world)
            except Exception:
                continue

            counts = {}
            for cls in snapshot.values():
                if not is_plausible(cls):
                    garbage_filtered += 1
                    continue
                counts[cls] = counts.get(cls, 0) + 1

            # Never prompt for anything that isn't a lone instance - can't
            # be a boss. Absorb it into seen_classes immediately so it
            # doesn't keep occupying candidate-tracking every poll.
            for cls, n in counts.items():
                if n >= 2 and cls not in seen_classes:
                    seen_classes.add(cls)
                    crowd_absorbed += 1

            present_now = {cls for cls, n in counts.items() if n == 1}

            # Stop tracking anything that vanished (or stopped being
            # solo) before reaching the persistence threshold.
            for cls in list(candidate_first_seen):
                if cls not in present_now:
                    del candidate_first_seen[cls]

            ready_to_prompt = []
            for cls in present_now:
                if cls in seen_classes:
                    continue
                if cls not in candidate_first_seen:
                    candidate_first_seen[cls] = poll_index
                elapsed = poll_index - candidate_first_seen[cls] + 1
                if elapsed >= min_persistence:
                    ready_to_prompt.append(cls)

            if not ready_to_prompt:
                continue

            # address of each candidate class in *this* snapshot - used
            # below to peek at HP, since a boss's HP dwarfs a regular
            # enemy's and is a much faster/more certain tell than timing.
            addr_by_class = {cls: addr for addr, cls in snapshot.items()}

            for cls in ready_to_prompt:
                hp = get_pawn_health(pm, addr_by_class[cls]) if cls in addr_by_class else None
                hp_str = f", HP={hp:.0f}" if hp is not None else ""
                answer = input(f"New class_name_index={cls}{hp_str} - solo actor, alive "
                                f"~{int(min_persistence * poll_interval)}s+ - enemy, "
                                f"boss (or type the boss name directly), or blank to ignore: ").strip()
                labeled = False
                answer_lower = answer.lower()
                # exact shorthand ("e"/"b") always falls through to the
                # letter-based branches below - resolve_boss_name would
                # otherwise treat bare "b" as a unique substring match for
                # "Jungle Abomination" and silently mislabel it.
                direct_boss_match = None
                if answer and answer_lower not in ("e", "b"):
                    direct_boss_match = resolve_boss_name(answer)
                if direct_boss_match:
                    # typed a full/partial boss name straight away (e.g. "Corrupted
                    # Cauldron") instead of "b" first - save it directly rather than
                    # falling through to the letter-based shorthand below, since
                    # several boss names also happen to start with "e".
                    save_boss_label(cls, direct_boss_match)
                    labeled = True
                elif answer_lower.startswith("b"):
                    labeled = prompt_boss_label(cls)
                elif answer_lower.startswith("e"):
                    label = input("  enemy label: ").strip()
                    if label:
                        ENEMY_CLASS_LOOKUP[cls] = label
                        save_lookup("enemy_class_lookup.json", ENEMY_CLASS_LOOKUP)
                        labeled = True
                if not labeled:
                    # blank / typo'd boss name / declined mid-prompt - persist
                    # the skip so a restart doesn't ask about this one again
                    skipped[cls] = True
                    save_lookup("boss_labeling_skipped.json", skipped)
                seen_classes.add(cls)  # don't ask again this session either way
                candidate_first_seen.pop(cls, None)

            print()
            print_progress()
            extras = []
            if garbage_filtered:
                extras.append(f"{garbage_filtered} implausible/garbage reads")
            if crowd_absorbed:
                extras.append(f"{crowd_absorbed} non-solo classes")
            if extras:
                print(f"(also silently filtered so far: {', '.join(extras)})")
            print()

    except KeyboardInterrupt:
        print("\nStopped. All labels saved - resume anytime, in this or a different mission.")

def watch_deaths(pm, poll_interval=1.0):
    """Diagnostic tool, no AP connection: live-prints every
    OnCharacterDeath event from dungeons_bridge.dll as it happens, with
    its resolved EntityType and class_name_index - unfiltered, not just
    boss candidates. Meant to be run WHILE fighting a boss still missing
    from BOSS_ENTITY_TYPE_IDS (currently just Heart of Ender / Vengeful
    Heart of Ender): since this is driven by the game's own real death
    events (not a presence/absence guess), the boss's true identity is
    simply whichever event prints right before the victory screen - no
    ambiguity, no persistence timers, no typing needed mid-fight.
    Requires dungeons_bridge.dll injected. Ctrl+C to stop.
    """
    import time

    probe = get_death_events(pm)
    if probe is None:
        print("dungeons_bridge.dll not reachable - inject it first (see auto_inject.py).")
        return

    print("Watching every character death (unfiltered) - fight normally, including the "
          "boss. Ctrl+C to stop once you see the boss's death print.\n")

    try:
        while True:
            time.sleep(poll_interval)
            events = get_death_events(pm)
            if not events:
                continue
            for addr in events:
                entity_type = get_actor_entity_type(pm, addr)
                cls = get_actor_class_name_index(pm, addr)
                if entity_type in BOSS_ENTITY_TYPE_IDS:
                    tag = f" <- CONFIRMED BOSS: {BOSS_ENTITY_TYPE_IDS[entity_type]}"
                elif cls in BOSS_CLASS_LOOKUP:
                    tag = f" <- already known boss: {BOSS_CLASS_LOOKUP[cls]}"
                elif cls in ENEMY_CLASS_LOOKUP:
                    tag = f" (known enemy: {ENEMY_CLASS_LOOKUP[cls]})"
                else:
                    tag = ""
                print(f"Death: addr={hex(addr)}  EntityType={entity_type}  class_name_index={cls}{tag}")
    except KeyboardInterrupt:
        print("\nStopped.")


def watch_boss_kills(pm, base, host, port, slot_name, game_name, password="",
                      poll_interval=2.0):
    """Per-level watcher (same usage pattern as watch_level - run it once
    per mission, restart when you move to the next one): sends the
    corresponding "<Boss> - First Kill" Archipelago LocationCheck the
    first time ever a boss-labeled character dies (subsequent kills of
    the same boss, e.g. on a replay, are not re-sent). Claimed kills
    persist to BOSS_KILLS_FILE.

    Requires dungeons_bridge.dll injected (see auto_inject.py) - there is
    no memory-scan fallback. dungeons_bridge.dll hooks ProcessEvent for
    ABaseCharacter::OnCharacterDeath, a genuine reflected UFunction the
    game calls on ANY character's death (player, mob, or boss - they're
    all ABaseCharacter), handing us the exact dying actor directly: no
    polling a snapshot for absence, no "did it despawn or get killed"
    ambiguity, no stale-UWorld risk (it fires on the game's own
    ProcessEvent calls, independent of whatever level pointer we think
    we're scanning). Only the address is enough to resolve its class and
    check it against a known boss - if that read fails (the actor's
    memory may already be torn down by the time we poll), the event is
    skipped rather than guessed at.

    BOSS_CLASS_LOOKUP starts empty like CHEST_/ENEMY_CLASS_LOOKUP - run
    document_bosses first (or answer the labeling prompt here) to fill it
    in for any boss not already covered by BOSS_ENTITY_TYPE_IDS (10/12
    bosses need zero labeling at all - see resolve_boss_for_actor).
    """
    import time
    from ap_client import ArchipelagoClient
    from _apworld_data import Locations as _apw
    get_boss_kill_location_id, boss_kill_location_name = (
        _apw.get_boss_kill_location_id, _apw.boss_kill_location_name
    )

    world = pm.read_longlong(base + OFFSETS["gworld"])
    if not world:
        print("No UWorld - are you in a level?")
        return

    claimed = load_claimed_boss_kills()
    print(f"{len(claimed)} boss kill(s) already claimed (from {BOSS_KILLS_FILE}).")

    baseline = scan_full_zone(pm, world)
    print(f"Baseline: {len(baseline)} actors.")

    dll_events = get_death_events(pm)
    if dll_events is None:
        print("dungeons_bridge.dll not reachable - inject it first (see auto_inject.py). "
              "watch_boss_kills requires the DLL, there is no fallback.")
        return

    if not BOSS_CLASS_LOOKUP:
        # Not fatal - BOSS_ENTITY_TYPE_IDS already covers 10/12 bosses
        # with zero labeling. This only matters for the remaining two
        # (Heart of Ender / Vengeful Heart of Ender) - use watch_deaths
        # to identify those live instead of labeling blind here.
        print("BOSS_CLASS_LOOKUP is empty - fine for the 10 bosses covered by "
              "BOSS_ENTITY_TYPE_IDS, but Heart of Ender / Vengeful Heart of Ender "
              "need document_bosses or watch_deaths first.\n")

    print(f"\nConnecting to Archipelago server at {host}:{port} as '{slot_name}' ({game_name})...")
    client = ArchipelagoClient(host, port, slot_name, game_name, password)
    client.connect()
    print("Connected.\n")

    pending_checks = []  # boss names detected but not yet successfully sent - retried every call

    def reconnect():
        nonlocal client
        try:
            client.close()
        except Exception:
            pass
        print("  Reconnecting to Archipelago server...")
        client = ArchipelagoClient(host, port, slot_name, game_name, password)
        client.connect()
        print("  Reconnected.")

    def send_kill_check(boss_name, how):
        if boss_name in claimed:
            return
        print(f"Boss kill detected: {boss_name} ({how})")
        if boss_name not in pending_checks:
            pending_checks.append(boss_name)
        flush_pending_checks()

    def flush_pending_checks():
        """Tries to send everything still pending. A boss only moves to
        `claimed` (and gets persisted) once send_location_checks actually
        succeeds - a dropped connection mid-fight (idle timeout, network
        hiccup) no longer means a real kill silently goes unrecorded: it
        just stays queued and gets retried here, including on the very
        next detected event, without losing anything."""
        nonlocal client
        if not pending_checks:
            return
        still_pending = []
        for boss_name in pending_checks:
            try:
                client.send_location_checks([get_boss_kill_location_id(boss_name)])
                claimed.add(boss_name)
                save_claimed_boss_kills(claimed)
                print(f"  -> sent check: {boss_kill_location_name(boss_name)}")
            except Exception as e:
                print(f"  Couldn't send check for {boss_name} ({e}) - will retry.")
                still_pending.append(boss_name)
        pending_checks[:] = still_pending
        if still_pending:
            try:
                reconnect()
            except Exception as e:
                print(f"  Reconnect failed too ({e}) - will keep retrying on the next poll/event.")

    print("Watching for boss kills via dungeons_bridge.dll's OnCharacterDeath events "
          "- play normally. Ctrl+C to stop.\n")

    try:
        while True:
            time.sleep(poll_interval)
            if pending_checks:
                flush_pending_checks()
            events = get_death_events(pm)
            if not events:
                continue
            for addr in events:
                boss_name = resolve_boss_for_actor(pm, addr)
                if boss_name is None:
                    continue  # not a boss (or memory already torn down) - not our concern
                send_kill_check(boss_name, "OnCharacterDeath event")
    except KeyboardInterrupt:
        print("\nStopped. Progress saved - safe to resume later.")
    finally:
        client.close()



# ============================================================
# Mission-completion / zone-unlock detection (merged from the idk_ branch)
# ============================================================

# ELevelNames enum values, confirmed via Dungeons_structs.hpp (uint8 enum).
# Only the entries relevant to MISSION_LOCATION_IDS are listed - extend if
# DLC support is added later. NOTE: keyed by the game's own internal enum
# names, which is "mooncorecaverns" for Redstone Mines (not "redstonemines"
# as used elsewhere in this script for a friendlier zone key) - so a
# lookup here needs that exact spelling, not the MISSION_LOCATION_IDS key.
ELEVELNAMES = {
    "squidcoast": 1, "creeperwoods": 2, "pumpkinpastures": 3, "soggyswamp": 4,
    "mooncorecaverns": 5, "fieryforge": 6, "deserttemple": 7, "slimysewers": 8,
    "highblockhalls": 9, "obsidianpinnacle": 10, "cacticanyon": 11,
    "creepycrypt": 42, "soggycave": 43, "underhalls": 44, "archhaven": 45,
    "lowertemple": 46, "mooshroomisland": 47, "woodlandmansion": 48,
    "spidercave": 49,
}

# Map from THIS script's own zone-name spelling (used throughout
# ZONE_NAME_LOOKUP / MISSION_LOCATION_IDS / last_mission_zone) to the
# matching ELevelNames key above, for the couple of zones where they differ.
ZONE_NAME_TO_ELEVELNAME_KEY = {
    "redstonemines": "mooncorecaverns",
    "hm_woodlandmansion": "woodlandmansion",
    "hm_spidercave": "spidercave",
}

# zone_id_order.json (loaded way above, before this remap table even
# existed yet) is sourced from the game's own ELevelNames enum, so it
# uses the game's raw internal names ("mooncorecaverns", "woodlandmansion",
# "spidercave") - NOT this script's friendlier zone-name spelling
# ("redstonemines", "hm_woodlandmansion", "hm_spidercave") used
# everywhere else (ZONE_NAME_LOOKUP, MISSION_LOCATION_IDS, zone_name
# variables throughout dungeons_ap_client.py). Confirmed bug: this
# mismatch meant `zone_name in ZONE_ID_ORDER` silently evaluated False
# for Redstone Mines specifically (and would for the two hm_ zones too)
# every single time, since "redstonemines" is never actually IN that
# list under that spelling - only "mooncorecaverns" is. In
# dungeons_ap_client.py that check gates whether last_mission_zone gets
# set on zone entry, so completing Redstone Mines could never be
# attributed to anything and its "Mission Complete" check would never
# fire. Normalizing ZONE_ID_ORDER to the friendly spelling here, once,
# right after the remap table above is available, fixes every call site
# that checks `zone_name in ZONE_ID_ORDER` at once instead of needing
# each one patched individually (and stays correct if more such
# call sites get added later).
_ELEVELNAME_TO_ZONE_NAME_KEY = {v: k for k, v in ZONE_NAME_TO_ELEVELNAME_KEY.items()}
ZONE_ID_ORDER[:] = [_ELEVELNAME_TO_ZONE_NAME_KEY.get(z, z) for z in ZONE_ID_ORDER]

def get_elevelname_value(zone_name):
    """Resolves this script's zone_name (whatever ZONE_NAME_LOOKUP
    produced) to its ELevelNames enum value, or None if not mapped."""
    key = ZONE_NAME_TO_ELEVELNAME_KEY.get(zone_name, zone_name)
    return ELEVELNAMES.get(key)

IS_MISSION_COMPLETED_INDEX_FILE = "is_mission_completed_index.json"

def _load_is_mission_completed_index():
    """Which candidate index (from find_functions_on_class's deterministic,
    reflection-order-based results on MissionProgressComponent's class) is
    the confirmed real IsMissionCompleted - an INDEX, not a raw address,
    since Children order is stable across sessions (same reasoning as
    KILL_FUNCTION_DEPTH) but addresses aren't."""
    if os.path.exists(IS_MISSION_COMPLETED_INDEX_FILE):
        with open(IS_MISSION_COMPLETED_INDEX_FILE) as f:
            content = f.read().strip()
        if content:
            try:
                return json.loads(content).get("index")
            except json.JSONDecodeError:
                return None
    return None

def _save_is_mission_completed_index(index):
    with open(IS_MISSION_COMPLETED_INDEX_FILE, "w") as f:
        json.dump({"index": index}, f, indent=2)

IS_MISSION_COMPLETED_INDEX = _load_is_mission_completed_index()

MISSION_PROGRESS_BASELINE_FILE = "mission_progress_baseline.json"

def _load_mission_progress_baseline():
    """Zones considered 'already completed before this Archipelago seed
    started' - IsMissionCompleted is a per-hero lifetime save flag, so an
    experienced hero starting a brand new seed would otherwise have their
    OLD completions auto-granted as checks for a seed they haven't
    actually played yet. Empty/missing file means no baseline is active -
    everything auto-grants as before (the right default for a fresh hero,
    where there's nothing to exclude anyway)."""
    if os.path.exists(MISSION_PROGRESS_BASELINE_FILE):
        with open(MISSION_PROGRESS_BASELINE_FILE) as f:
            content = f.read().strip()
        if content:
            try:
                return set(json.loads(content))
            except json.JSONDecodeError:
                return set()
    return set()

def _save_mission_progress_baseline(zone_set):
    with open(MISSION_PROGRESS_BASELINE_FILE, "w") as f:
        json.dump(sorted(zone_set), f, indent=2)

def call_is_mission_completed(pm, base, zone_name, debug_log=None):
    """Calls the confirmed UMissionProgressComponent::IsMissionCompleted
    for zone_name, for real. Returns True/False, or None if anything
    along the chain isn't available (no pawn, no component, index not
    confirmed yet, zone not in ELEVELNAMES, etc) - callers should treat
    None as "unknown", not as False.

    debug_log: optional callable (e.g. game_logger.info) - if given, it's
    called with exactly which step returned None/False, instead of the
    caller only ever seeing an opaque None with no way to tell which of
    the several early-return points it came from. None by default so
    this stays silent for callers (e.g. dungeons_reader.py's own CLI
    tools) that don't want the extra noise."""
    def _log(msg):
        if debug_log:
            debug_log(f"call_is_mission_completed({zone_name}): {msg}")

    if IS_MISSION_COMPLETED_INDEX is None:
        _log("IS_MISSION_COMPLETED_INDEX not confirmed (is_mission_completed_index.json missing/empty)")
        return None
    elevelname_value = get_elevelname_value(zone_name)
    if elevelname_value is None:
        _log(f"get_elevelname_value returned None - {zone_name!r} not recognized as an ELevelNames value")
        return None
    pawn, pawn_err = get_pawn(pm, base)
    if not pawn:
        _log(f"get_pawn failed: {pawn_err}")
        return None
    try:
        component = pm.read_longlong(pawn + OFFSETS["mission_progress_component"])
        if not component:
            _log("mission_progress_component read as null off the current pawn")
            return None
        component_class = pm.read_longlong(component + OFFSETS["uobject_class"])
        candidates = find_functions_on_class(pm, component_class, min_size=1, max_size=16)
        if IS_MISSION_COMPLETED_INDEX >= len(candidates):
            _log(f"IS_MISSION_COMPLETED_INDEX ({IS_MISSION_COMPLETED_INDEX}) out of range - "
                 f"only found {len(candidates)} candidate function(s) on this class right now")
            return None
        func_addr, size = candidates[IS_MISSION_COMPLETED_INDEX]
        params_bytes = bytes([elevelname_value]) + bytes(max(size, 8) - 1)
        success, _, params_after = call_process_event(pm, component, func_addr, params_bytes)
        if not success or len(params_after) < 2:
            _log(f"call_process_event failed or returned too few bytes (success={success}, "
                 f"len={len(params_after) if params_after else 'N/A'})")
            return None
        result = bool(params_after[1])
        _log(f"resolved cleanly -> {result}")
        return result
    except Exception as e:
        _log(f"raised {type(e).__name__}: {e}")
        return None

def find_functions_on_class(pm, class_ptr, min_size=0, max_size=None, require_const=True, require_final=True):
    """Searches ONLY class_ptr's own Children (not the super chain) for
    Native|Public (+Final, +Const, unless disabled) UFunctions within a
    parameter-struct size range. Used for functions declared directly on
    a specific component's class (e.g. UMissionProgressComponent::
    IsMissionCompleted, UWalletComponent::ClientAdd/Balance/Deduct)
    where we don't need to walk a whole inheritance chain - narrows the
    candidate set a lot compared to find_zero_param_native_final_functions.

    require_const/require_final both default to True (IsMissionCompleted's
    original, still-default behavior) but must be set False as needed -
    confirmed straight from Dumper-7's own Dungeons_functions.cpp flag
    comments that UWalletComponent's three currency functions do NOT
    share the same flag set: Deduct and Balance are both (Final, Native,
    Public, ...), but ClientAdd is (Net, NetReliable, Native, Event,
    Public, NetClient, BlueprintCallable) - NOT Final. Also only Balance
    is const. Requiring a flag a real function doesn't carry silently
    returns zero candidates, not a wrong answer exactly, but an
    unhelpfully empty one that looks identical to 'this function isn't
    on this class at all'.
    Returns [(function_ptr, size), ...]."""
    results = []
    required_bits = FUNC_NATIVE | FUNC_PUBLIC
    if require_const:
        required_bits |= FUNC_CONST
    if require_final:
        required_bits |= FUNC_FINAL
    for field in walk_children(pm, class_ptr):
        try:
            size = pm.read_int(field + OFFSETS["ustruct_size"])
            flags = pm.read_uint(field + OFFSETS["ufunction_flags"])
        except Exception:
            continue
        if (flags & required_bits) != required_bits:
            continue
        if size < min_size:
            continue
        if max_size is not None and size > max_size:
            continue
        results.append((field, size))
    return results


# Predecessor chain for base-game zones, mirroring the AP world's own
# Rules.py access logic exactly (AND across every entry - Highblock Halls
# needs BOTH deserttemple AND fieryforge, for example). Items arrive in a
# completely random order in Archipelago (scattered across the whole
# multiworld, not sequentially) - a player can easily receive
# "Highblock Halls Access" before ever getting "Desert Temple Access", so
# checking only a zone's own item (as the first version of watch_level_lock
# did) isn't enough: it would let the player into a mission whose
# prerequisites per the world's own logic graph haven't arrived yet.
MISSION_REQUIRES = _apw_zonedata.skip_level_requires_map()
# Computed from the real ZoneData graph via skip_level_requires_map (same
# function Regions.py's generation-side logic rules use) rather than
# hand-duplicated here - a hardcoded copy of this exact table drifting out
# of sync with ZoneData.py is exactly the kind of bug this project already
# hit once (the gauntletgales/gauntletofgales zone-name mismatch). Note
# this reflects the "skip a generation" progression relaxation - a zone's
# entry here is its effective grandparent(s), not its raw direct
# predecessor(s) from ZoneData.py's own requires[] field.

def is_zone_truly_unlocked(zone_name, received_zone_items, _seen=None):
    """A zone counts as unlocked only if its OWN Access item has been
    received AND every zone in its predecessor chain is ALSO unlocked,
    recursively - matching Rules.py's make_zone_rule exactly (AND across
    the zone's own item plus every predecessor's item). received_zone_items
    is a set of zone_names whose Access item has arrived (not necessarily
    "truly unlocked" yet - that's what this function determines).
    _seen tracks the current root-to-node path (for real-cycle detection
    only) and is NEVER mutated in place - each recursive call gets its own
    frozenset via _seen | {zone_name}. Passing the same mutable set object
    down every branch (as an earlier version did) incorrectly marked a
    shared ancestor as "visited" after the FIRST sibling branch reached it,
    so a second, unrelated branch reaching that same ancestor via a
    different path (a normal diamond-shaped dependency graph - e.g. both
    cacticanyon and creeperwoods requiring squidcoast) hit a false
    cycle-guard and returned False even though every item was genuinely
    present. Confirmed via a real Launcher log: /unlocked showed
    'cacticanyon' and 'redstonemines' both present, yet entering either
    zone still logged 'Locked mission ... missing: [that same zone]' and
    killed the player - reproduced exactly by this shared-_seen bug and
    fixed by switching to frozenset union per branch."""
    if _seen is None:
        _seen = frozenset()
    if zone_name in _seen:
        return False  # genuine cycle guard - shouldn't happen with real data
    if zone_name not in received_zone_items:
        return False
    new_seen = _seen | {zone_name}
    for predecessor in MISSION_REQUIRES.get(zone_name, []):
        if not is_zone_truly_unlocked(predecessor, received_zone_items, new_seen):
            return False
    return True



if __name__ == "__main__":
    import sys

    pm, base = attach()

    if len(sys.argv) > 1 and sys.argv[1] == "watch":
        # python dungeons_reader.py watch
        world = pm.read_longlong(base + OFFSETS["gworld"])
        if not world:
            print("No UWorld - are you in a level?")
        else:
            watch_for_drops(pm, world)
    elif len(sys.argv) > 1 and sys.argv[1] == "survey":
        # python dungeons_reader.py survey
        # Dedicated chest/enemy survey across every zone you visit - uses
        # the full-zone scan (persistent level + all loaded streaming
        # sub-levels), so it actually sees a whole map's worth of content
        # instead of just the ~50 persistent-level actors. Builds up
        # zone_survey.json continuously (peak enemy count per zone, chest
        # counts by type) - safe to Ctrl+C any time.
        survey_zones(pm, base)
    elif len(sys.argv) > 1 and sys.argv[1] == "level":
        # python dungeons_reader.py level
        # Combined enemy/pickup/death monitor for the current level
        # (e.g. Creeper Woods). Run this once with empty ENEMY_CLASS_LOOKUP
        # first - it'll print the actor-class breakdown so you can identify
        # which class_name_index values are enemies.
        watch_level(pm, base)
    elif len(sys.argv) > 1 and sys.argv[1] == "session":
        # python dungeons_reader.py session
        # Full run monitor: auto-announces each zone by name as you travel
        # (lobby -> Creeper Woods -> next zone...), printing that zone's
        # chest/enemy breakdown, then tracks pickups/spawns/death within it.
        watch_session(pm, base)
    elif len(sys.argv) > 1 and sys.argv[1] == "give":
        # python dungeons_reader.py give <item name...> [rarity] [power] [strategy] [refresh_mode]
        # e.g. python dungeons_reader.py give Crossbow Rare 1.5
        # e.g. python dungeons_reader.py give Mercenary Armor Rare 1.5
        # e.g. python dungeons_reader.py give Crossbow Rare 1.5 lowest_power
        # No quotes needed for multi-word item names - rarity/power/strategy/
        # refresh_mode tokens are detected wherever they appear, everything
        # else becomes the item name.
        #
        # refresh_mode defaults to "none" - visit lobby storage to force the
        # UI to show a correct icon/name for now, that's the confirmed-safe
        # workaround. Do NOT pass "counters" (bumping 0x1a0/0x1a8) - this
        # caused a real game crash on opening the inventory when tested, so
        # those fields are very likely something other than a simple dirty
        # flag (e.g. an array bound/reference count something else trusts).
        # "flag" (the 0x2ac bit) is untested - back up your save first if
        # you try it. "both" inherits the "counters" crash risk.
        if len(sys.argv) < 3:
            print('Usage: python dungeons_reader.py give <item name...> [rarity] [power] [strategy] [refresh_mode]')
        else:
            item_name, rarity, power, strategy, refresh_mode = parse_give_args(sys.argv[2:])
            item_stash, error = get_item_stash_component(pm, base)
            if not item_stash:
                print(f"Could not reach ItemStashComponent: {error}")
            else:
                ok, message = give_reward(pm, item_stash, item_name, rarity, power,
                                           strategy=strategy, refresh_mode=refresh_mode)
                print(message)
                print("\nFull inventory, re-read fresh right now:")
                for it in read_inventory(pm, item_stash):
                    print(f"  [{it['slot']}] {it['name']:20s} power={it['power']:.1f}  {it['rarity']}")
    elif len(sys.argv) > 1 and sys.argv[1] == "give_safe":
        # python dungeons_reader.py give_safe <item name...> [rarity] [power_bonus]
        # e.g. python dungeons_reader.py give_safe Crossbow Rare 1.5
        #
        # SAFE alternative to "give" above - "give" overwrites an existing
        # slot's raw bytes directly (write_item) with none of the checks
        # give_item.py's real grant path has. This command instead goes
        # through the EXACT same safety stack as a real Archipelago reward
        # (see _do_grant in dungeons_ap_client.py):
        #   1. get_pawn() - refuses if there's no Pawn at all (main menu,
        #      character select, mid loading screen). This is the fix for
        #      the "inventory cleared on connect/menu" bug - a menu/loading
        #      screen never gets a write attempted against it in the first
        #      place.
        #   2. wait_for_stable_item_stash() - requires the SAME
        #      ItemStashComponent address across 3 checks 0.5s apart before
        #      trusting it enough to write to.
        #   3. get_best_displayed_power(check_for_drop=True) - compares
        #      against the highest power ever seen this run. If it just
        #      DROPPED (the "power drop to a much lower value" signal that
        #      means the inventory was just wiped/corrupted by something),
        #      this raises PowerDropDetected and NOTHING is granted -
        #      that's the "if power drops, we've lost [the inventory],
        #      stop" protection.
        #   4. give_item.py's real give_item() - calls the actual
        #      ClientAddItem UFunction (a real, in-game item grant) rather
        #      than overwriting an existing slot's bytes directly, and
        #      itself checks inventory/storage capacity first so a full
        #      inventory can't trigger the game's silent auto-salvage.
        #
        # Uses ClientAddItem (adds a NEW item) rather than "give"'s
        # overwrite-an-existing-slot approach, so nothing existing gets
        # sacrificed - this needs free inventory/storage space instead.
        if len(sys.argv) < 3:
            print('Usage: python dungeons_reader.py give_safe <item name...> [rarity] [power_bonus]')
        else:
            from give_item import (wait_for_stable_item_stash, get_best_displayed_power,
                                    give_item as safe_give_item, PowerDropDetected)

            item_name, rarity, power_bonus, _strategy, _refresh_mode = parse_give_args(sys.argv[2:])
            if item_name not in REVERSE_NAME_LOOKUP:
                print(f"'{item_name}' isn't a recognized item name - check item_lookup.py's ITEM_TABLE/all_items.csv")
            else:
                rarity_raw = RARITY_BYTES.get(rarity)
                if rarity_raw is None:
                    print(f"Unknown rarity '{rarity}' - use Common, Rare, or Unique")
                else:
                    pawn, pawn_error = get_pawn(pm, base)
                    if pawn_error:
                        print(f"Refusing to grant: player isn't in a level right now ({pawn_error}). "
                              f"This is exactly the menu/loading-screen state that used to cause the "
                              f"inventory-clear bug - try again once you're actually in a mission or the hub.")
                    else:
                        try:
                            item_stash, item_stash_class = wait_for_stable_item_stash(pm, base)
                        except RuntimeError as e:
                            print(f"Refusing to grant: {e}")
                        else:
                            import win32file
                            pipe = _connect_bridge_pipe(pm)
                            try:
                                try:
                                    get_best_displayed_power(pm, pipe, item_stash, item_stash_class,
                                                              check_for_drop=True)
                                except PowerDropDetected as e:
                                    print(f"ABORTED - {e}")
                                else:
                                    name_index = REVERSE_NAME_LOOKUP[item_name]
                                    granted_power = safe_give_item(
                                        pm, pipe, item_stash, item_stash_class, name_index,
                                        power_bonus=power_bonus, rarity_raw=rarity_raw,
                                    )
                                    print(f"Granted {item_name} ({rarity}) at power={granted_power:.1f}")
                                    print("\nFull inventory, re-read fresh right now:")
                                    for it in read_inventory(pm, item_stash):
                                        print(f"  [{it['slot']}] {it['name']:20s} power={it['power']:.1f}  {it['rarity']}")
                            finally:
                                win32file.CloseHandle(pipe)
    elif len(sys.argv) > 1 and sys.argv[1] == "monitor_counters":
        # python dungeons_reader.py monitor_counters
        # READ-ONLY - watches dirty_counter_a/b, dirty_flag, and the
        # inventory count over time, no writes at all. Re-resolves the
        # ItemStashComponent pointer every poll (rather than once), so it
        # won't chase a stale pointer if the component gets recreated
        # mid-session. Use this to figure out what 0x1a0/0x1a8 actually
        # are before ever writing to them again.
        monitor_counters(pm, base)
    elif len(sys.argv) > 1 and sys.argv[1] == "scan_dirty":
        # python dungeons_reader.py scan_dirty
        # Before/after diff on ItemStashComponent itself - looks for the
        # "dirty"/"version" counter the UI checks to decide whether to
        # rebuild the inventory list, since that's the likely reason a
        # correct memory write sometimes needs a full restart to show up.
        item_stash, error = get_item_stash_component(pm, base)
        if not item_stash:
            print(f"Could not reach ItemStashComponent: {error}")
        else:
            scan_stash_diff(pm, item_stash)
    elif len(sys.argv) > 1 and sys.argv[1] == "watch_emeralds":
        # python dungeons_reader.py watch_emeralds <goal> <host> <port> <slot_name> <game_name> [password]
        # e.g. python dungeons_reader.py watch_emeralds 50000 localhost 38281 MyName "Minecraft Dungeons"
        #
        # Watches your emerald count and sends an Archipelago check every
        # time you cross a new 500-emerald milestone (up to <goal>).
        # Claimed milestones persist to emerald_milestones_claimed.json,
        # so nothing's lost or resent across restarts.
        #
        # pip install websocket-client   (needed for ap_client.py)
        if len(sys.argv) < 7:
            print("Usage: python dungeons_reader.py watch_emeralds <goal> <host> <port> "
                  "<slot_name> <game_name> [password]")
        else:
            goal = int(sys.argv[2])
            host = sys.argv[3]
            port = int(sys.argv[4])
            slot_name = sys.argv[5]
            game_name = sys.argv[6]
            password = sys.argv[7] if len(sys.argv) > 7 else ""
            watch_emeralds(pm, goal, host, port, slot_name, game_name, password)

    elif len(sys.argv) > 1 and sys.argv[1] == "watch_zone_chests":
        # python dungeons_reader.py watch_zone_chests <host> <port> <slot_name> <game_name> [password]
        # e.g. python dungeons_reader.py watch_zone_chests localhost 38281 MyName "Minecraft Dungeons"
        #
        # Sends a real Archipelago check for each confirmed-fixed zone's
        # chests as you open them (in discovery order - see Locations.py's
        # ZONE_CHEST_COUNTS comment for why exact per-chest identity isn't
        # needed), tracked across EVERY zone in ZONE_CHEST_COUNTS at once -
        # no need to specify a zone, it follows wherever you currently
        # are. Requires the updated dungeons_bridge.dll (OnInteracted
        # hook) already injected. Claimed progress persists to
        # zone_chests_claimed.json.
        #
        # pip install websocket-client   (needed for ap_client.py)
        if len(sys.argv) < 6:
            print("Usage: python dungeons_reader.py watch_zone_chests <host> <port> "
                  "<slot_name> <game_name> [password]")
        else:
            host = sys.argv[2]
            port = int(sys.argv[3])
            slot_name = sys.argv[4]
            game_name = sys.argv[5]
            password = sys.argv[6] if len(sys.argv) > 6 else ""
            watch_zone_chests(pm, base, host, port, slot_name, game_name, password)

    elif len(sys.argv) > 1 and sys.argv[1] == "watch_currency_jumps":
        # python dungeons_reader.py watch_currency_jumps
        # Wooden/Supply chests showed no reproducible pickup-actor signature
        # in calibrate_loot_class (different random class each time, count
        # 1) - likely because they just grant currency directly with no
        # discrete item actor at all, unlike Fancy's item drops. Tests that
        # directly: poll the already-proven-working currency pointer and
        # print every time Emeralds/Gold changes, so we can see whether it
        # correlates with opening a Wooden/Supply chest.
        currency_ptr, error = get_currency_pointer(pm)
        if currency_ptr is None:
            print(f"Could not reach dungeons_bridge.dll: {error}")
        elif currency_ptr == 0:
            print("Currency pointer not captured yet - open your inventory or anything "
                  "showing currency in-game once, then try again.")
        else:
            last_values = read_currency_values(pm, currency_ptr)
            print(f"Starting values: {last_values}")
            print("Watching for any change (Ctrl+C to stop) - open a Wooden or Supply chest now...\n")
            try:
                while True:
                    time.sleep(0.3)
                    currency_ptr, error = get_currency_pointer(pm)
                    if not currency_ptr:
                        continue
                    values = read_currency_values(pm, currency_ptr)
                    for name in values:
                        if values[name] is not None and values[name] != last_values.get(name):
                            print(f"  [{name} CHANGED] {last_values.get(name)} -> {values[name]} "
                                  f"(delta {values[name] - (last_values.get(name) or 0)})")
                    last_values = values
            except KeyboardInterrupt:
                print("\nStopped watching.")
    elif len(sys.argv) > 1 and sys.argv[1] == "pickup_tier":
        # python dungeons_reader.py pickup_tier <0-3>
        # Manual test harness for set_pickup_tier - 0 = nothing gated
        # pickable, 1 = + Health items, 2 = + Potions, 3 = + TNT.
        # Weapons/armor/artifacts/tokens/eye of ender/arrows are never
        # gated at any tier. Requires the updated dungeons_bridge.dll
        # (with the pickup-tier gating hook) already injected.
        if len(sys.argv) < 3:
            print("Usage: python dungeons_reader.py pickup_tier <0-3>")
            for t, desc in PICKUP_TIER_NAMES.items():
                print(f"  {t}: {desc}")
        else:
            try:
                tier = int(sys.argv[2])
            except ValueError:
                print("Tier must be an integer 0-3.")
            else:
                ok, error = set_pickup_tier(pm, tier)
                if not ok:
                    print(f"Failed: {error}")
                else:
                    current, error = get_pickup_tier(pm)
                    if error:
                        print(f"Set OK, but couldn't verify: {error}")
                    else:
                        print(f"Now at tier {current}: {PICKUP_TIER_NAMES.get(current, 'unknown')}")
    elif len(sys.argv) > 1 and sys.argv[1] == "watch_chest_events":
        # python dungeons_reader.py watch_chest_events
        # The real, confirmed-working fix: requires the updated
        # dungeons_bridge.dll (with the OnInteracted-pattern ProcessEvent
        # hook) rebuilt and injected. Live-tested against real opens -
        # fires with the real actor class name straight from the game's
        # own reflection data, no memory-offset guessing involved at all.
        #
        # This fires for EVERY interactable in the game (doors, NPCs,
        # pickups, buttons...), not just chests - classify_interactable_class
        # filters generically by name pattern ("chest" / "supply"), so
        # every chest tier/variant (including ones never explicitly
        # tested, like the BP_WoodenChest_Hidden_C variant discovered
        # live) is caught automatically, with zero maintenance.
        print("Watching for interact events via dungeons_bridge.dll (Ctrl+C to stop)...")
        print("Requires the updated DLL (with the OnInteracted hook) already injected.\n")
        check_counts = {"chest": 0, "supply": 0}
        try:
            while True:
                time.sleep(0.3)
                events, error = get_chest_open_events(pm)
                if error:
                    print(f"  (bridge unreachable: {error})")
                    time.sleep(2.0)
                    continue
                for evt in events:
                    cls = evt["class_name"]
                    kind = classify_interactable_class(cls)
                    if kind:
                        check_counts[kind] += 1
                        label = "CHEST" if kind == "chest" else "SUPPLY CHEST"
                        print(f"  [{label} OPENED] actor_addr={hex(evt['actor_addr'])} "
                              f"class={cls} - check #{sum(check_counts.values())} "
                              f"({check_counts['chest']} chest, {check_counts['supply']} supply)")
                        # TODO: send an Archipelago LocationCheck here once
                        # the location-id mapping is connected.
                    else:
                        print(f"  (ignored, not chest-related: {cls} @ {hex(evt['actor_addr'])})")
        except KeyboardInterrupt:
            print("\nStopped watching.")
    elif len(sys.argv) > 1 and sys.argv[1] == "currency":
        # python dungeons_reader.py currency
        # NEW: uses the confirmed-working technique from a real Cheat
        # Engine table (Dungeons_Master_Table_v3_70.CT) - the real
        # Emeralds/Gold/Eyes of Ender getter is a tiny NATIVE (non-
        # reflected) function, never a UFUNCTION, which is why every
        # WalletComponent/GObjects-based attempt before this failed to
        # find it. dungeons_bridge.dll (v4+) hooks that getter and keeps
        # its captured "this" pointer updated on every call the game
        # itself makes (not just the first) so it self-heals if the
        # underlying currency object is ever destroyed/recreated - this
        # just asks the DLL for that pointer, then reads directly from
        # it via CURRENCY_OFFSETS (confirmed against real HUD values:
        # Emeralds +0x08, Eyes of Ender +0x14; Gold +0x20 unconfirmed).
        #
        # If this comes back with "not captured yet", open your
        # inventory/HUD/anything that displays currency once in-game
        # (that's what triggers the hooked function to run for the
        # first time), then try again.
        currency_ptr, error = get_currency_pointer(pm)
        if currency_ptr is None:
            print(f"Could not reach dungeons_bridge.dll: {error}")
        elif currency_ptr == 0:
            print("Currency pointer not captured yet - open your inventory or anything "
                  "showing currency in-game once, then try again.")
        else:
            values = read_currency_values(pm, currency_ptr)
            print(f"Currency pointer: {hex(currency_ptr)}")
            print(f"  Emeralds:      {values['emeralds']}")
            print(f"  Gold:          {values['gold']}")
            print(f"  Eyes of Ender: {values['eyes_of_ender']}")

    elif len(sys.argv) > 1 and sys.argv[1] == "scan_currency":
        # python dungeons_reader.py scan_currency <real_emeralds> <real_gold> <real_eyes_of_ender>
        # Ground-truth targeted scan around the live p_currency pointer -
        # see scan_currency_offsets' docstring for why this replaces
        # trusting the CT-table CURRENCY_OFFSETS, which a direct
        # side-by-side check just proved wrong (not merely swapped).
        # Pass the THREE real numbers currently shown on your in-game HUD,
        # in that order, right before running this.
        if len(sys.argv) < 5:
            print("Usage: python dungeons_reader.py scan_currency <real_emeralds> <real_gold> <real_eyes_of_ender>")
        else:
            real_emeralds, real_gold, real_eyes = int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4])
            currency_ptr, error = get_currency_pointer(pm)
            if not currency_ptr:
                print(f"Could not reach currency pointer: {error or 'not captured yet - open your inventory/HUD once'}")
            else:
                print(f"Scanning p_currency @ {hex(currency_ptr)} for "
                      f"emeralds={real_emeralds}, gold={real_gold}, eyes_of_ender={real_eyes}...")
                hits = scan_currency_offsets(pm, currency_ptr, real_emeralds, real_gold, real_eyes)
                for label, offsets in hits.items():
                    if offsets:
                        print(f"  {label}: {[hex(o) for o in offsets]}"
                              + ("  <-- single match, high confidence" if len(offsets) == 1 else
                                 "  (multiple matches - re-run after the value changes to narrow down)"))
                    else:
                        print(f"  {label}: no match in [0x0, 0x200) - try a wider window or a "
                              f"more distinctive current value for this one")

    elif len(sys.argv) > 1 and sys.argv[1] == "zone_progress":
        # python dungeons_reader.py zone_progress
        # Checklist of confirmed vs unlabeled zones against ELevelNames.
        zone_naming_progress()
    elif len(sys.argv) > 1 and sys.argv[1] == "diff_zone":
        # python dungeons_reader.py diff_zone
        # Manual before/after snapshot - catches BOTH new actors and
        # actors that disappeared entirely, for things whose interaction
        # destroys the actor rather than just flipping a bool (e.g. if
        # the supply chest isn't AChestActor at all).
        world = pm.read_longlong(base + OFFSETS["gworld"])
        if not world:
            print("No UWorld - are you in a level?")
        else:
            input("Stand right next to (but don't interact with) the thing you want to identify, then press Enter for the BEFORE snapshot...")
            before = scan_full_zone(pm, world)
            print(f"Captured {len(before)} actors.")
            input("Now interact with ONLY that one thing, then press Enter for the AFTER snapshot...")
            after = scan_full_zone(pm, world)
            print(f"Captured {len(after)} actors.")

            added, removed = diff_scans_both_ways(before, after)
            if added:
                print(f"\n{len(added)} actor(s) newly appeared:")
                for addr, cls in added.items():
                    print(f"  class_name_index={cls}  (addr={hex(addr)})")
            if removed:
                print(f"\n{len(removed)} actor(s) disappeared:")
                for addr, cls in removed.items():
                    print(f"  class_name_index={cls}  (addr={hex(addr)})")
            if not added and not removed:
                print("\nNo change detected at all - the interaction may not have registered as "
                      "a world-actor change, or it happened too fast between snapshots.")
    elif len(sys.argv) > 1 and sys.argv[1] == "nearest_actors":
        # python dungeons_reader.py nearest_actors
        # Like nearest_chest, but scans EVERY actor regardless of known
        # class - for finding something that isn't AChestActor at all
        # (e.g. the supply chest, confirmed via nearest_chest to not be
        # a known chest class). Stand right next to the thing you want
        # identified and run this.
        pawn, error = get_pawn(pm, base)
        if not pawn:
            print(f"Could not reach Pawn: {error}")
        else:
            player_loc = get_actor_location(pm, pawn)
            if not player_loc:
                print("Could not read player position.")
            else:
                print(f"Player position: ({player_loc[0]:.0f}, {player_loc[1]:.0f}, {player_loc[2]:.0f})")
                world = pm.read_longlong(base + OFFSETS["gworld"])
                snapshot = scan_full_zone(pm, world)
                distances = []
                for addr, cls in snapshot.items():
                    if addr == pawn:
                        continue
                    loc = get_actor_location(pm, addr)
                    if not loc or loc == (0.0, 0.0, 0.0):
                        continue
                    dist = ((loc[0] - player_loc[0]) ** 2 + (loc[1] - player_loc[1]) ** 2 + (loc[2] - player_loc[2]) ** 2) ** 0.5
                    distances.append((dist, addr, cls, loc))
                distances.sort(key=lambda d: d[0])
                print(f"\nClosest {min(10, len(distances))} actor(s) (any class):")
                for dist, addr, cls, loc in distances[:10]:
                    known = ""
                    if cls in ENEMY_CLASS_LOOKUP:
                        known = f"  <-- known enemy: {ENEMY_CLASS_LOOKUP[cls]}"
                    print(f"  distance={dist:.0f}  class_name_index={cls:<8} "
                          f"@ ({loc[0]:.0f}, {loc[1]:.0f}, {loc[2]:.0f})  (addr={hex(addr)}){known}")
    elif len(sys.argv) > 1 and sys.argv[1] == "nearest_actors_raw":
        # python dungeons_reader.py nearest_actors_raw [count=15]
        # Ground-truth diagnostic - reads every actor in the zone with a
        # valid position and shows the closest N to you, raw
        # class_name_index and all. Stand right next to any object and
        # this tells you exactly what class it really is, no trust/warm-up
        # period required - the most direct way to confirm what a
        # specific in-game object's class_name_index is this session.
        pawn, error = get_pawn(pm, base)
        if not pawn:
            print(f"Could not reach Pawn: {error}")
        else:
            player_loc = get_actor_location(pm, pawn)
            if not player_loc:
                print("Could not read player position.")
            else:
                count = int(sys.argv[2]) if len(sys.argv) > 2 else 15
                print(f"Player position: ({player_loc[0]:.0f}, {player_loc[1]:.0f}, {player_loc[2]:.0f})\n")
                world = pm.read_longlong(base + OFFSETS["gworld"])
                snapshot = scan_full_zone(pm, world)
                rows = []
                for addr, cls in snapshot.items():
                    loc = get_actor_location(pm, addr)
                    if not loc or loc == (0.0, 0.0, 0.0):
                        continue
                    dist = ((loc[0] - player_loc[0]) ** 2 + (loc[1] - player_loc[1]) ** 2 + (loc[2] - player_loc[2]) ** 2) ** 0.5
                    rows.append((dist, addr, cls, loc))
                rows.sort(key=lambda r: r[0])
                print(f"Closest {min(count, len(rows))} actor(s) (any class, no filtering):")
                for dist, addr, cls, loc in rows[:count]:
                    print(f"  dist={dist:.0f}  class={cls}  @ ({loc[0]:.0f}, {loc[1]:.0f}, {loc[2]:.0f})")
                print("\nStand right next to the object you want and look for its class in this list - "
                      "whatever's closest to dist=0 IS the object you're looking at.")
    elif len(sys.argv) > 1 and sys.argv[1] == "find_gnames":
        # python dungeons_reader.py find_gnames
        # No Cheat Engine, no injection - locates the FNamePool base via
        # the None-entry signature technique (see the block comment above
        # attach_module_size). Can take a minute or two, since pass 1
        # searches heap memory; only needs to be run once (result is
        # saved to gnames_address.json and reused automatically after).
        NONE_ENTRY = bytes([0x00, 0x01]) + b"None"  # header (len=4,ansi,no flags) + ASCII text, no null terminator

        print("Pass 1/2: searching heap memory for the 'None' name entry signature...")
        heap_regions = list(_enum_regions(pm, only_private=True))
        print(f"  ({len(heap_regions)} private memory region(s) to search)")
        entry_candidates = _search_bytes_in_regions(pm, NONE_ENTRY, heap_regions, max_hits=200)
        print(f"  found {len(entry_candidates)} candidate entry address(es)")

        if not entry_candidates:
            print("No candidates found - the header byte layout may differ for this engine "
                  "build (see the comment on FNameEntryHeader in resolve_fname). Manual "
                  "reverse-engineering (Ghidra/IDA on a static copy of the exe) would be the "
                  "next step, outside what this script can automate.")
        else:
            entry_addr = entry_candidates[0]
            found = False

            # Attempt A: FNamePool embedded directly as a static struct in
            # the module - Blocks[0] (module memory) holds entry_addr
            # itself. This is what we tried first; kept here for other
            # engine builds where it does work.
            print("\nPass 2/2, attempt A: module holds a direct pointer to the entry itself...")
            module_size = attach_module_size()
            needle = entry_addr.to_bytes(8, "little")
            for hit_addr in _search_bytes_in_regions(pm, needle, [(base, module_size)], max_hits=10):
                pool_base_candidate = hit_addr - 0x10
                if resolve_fname(pm, 0, pool_base=pool_base_candidate) == "None":
                    print(f"\nMATCH - pool_base=0x{pool_base_candidate:X} (module+0x{pool_base_candidate - base:X})")
                    save_lookup("gnames_address.json", {0: pool_base_candidate})
                    OFFSETS["gnames"] = pool_base_candidate
                    print("Saved to gnames_address.json. Try 'identify_classes' now.")
                    found = True
                    break

            # Attempt B: FNamePool is heap-allocated; the module only
            # holds a pointer to pool_base itself (not to the entry). So
            # instead of searching for a specific byte pattern, treat
            # every plausible pointer-sized value found in the module as
            # a CANDIDATE pool_base, and check whether ITS Blocks[0]
            # (candidate+0x10) matches entry_addr.
            if not found:
                print("Pass 2/2, attempt B: module holds a pointer to the pool struct itself "
                      "(heap-allocated pool) - checking every plausible pointer value found...")
                try:
                    module_bytes = pm.read_bytes(base, module_size)
                except Exception as e:
                    module_bytes = b""
                    print(f"  couldn't read module memory in one shot ({e}); trying smaller chunks...")
                    chunk = 16 * 1024 * 1024
                    parts = []
                    off = 0
                    while off < module_size:
                        ln = min(chunk, module_size - off)
                        try:
                            parts.append(pm.read_bytes(base + off, ln))
                        except Exception:
                            parts.append(b"\x00" * ln)
                        off += ln
                    module_bytes = b"".join(parts)

                # Bound candidates to addresses that fall inside a region
                # we already know is committed (the heap regions from
                # pass 1), to avoid firing a remote read at garbage.
                import bisect
                region_ranges = sorted((r_base, r_base + r_size) for r_base, r_size in heap_regions)
                region_starts = [r[0] for r in region_ranges]  # built ONCE, not per-candidate

                def _in_known_region(addr):
                    i = bisect.bisect_right(region_starts, addr) - 1
                    if i < 0:
                        return False
                    r_start, r_end = region_ranges[i]
                    return r_start <= addr < r_end

                # Pass B is two stages: first a fast, purely-local scan of
                # the module bytes we already have (no remote calls) to
                # collect plausible pointer values, THEN one remote read
                # per surviving candidate - keeps the expensive part
                # (network/IPC round trips) down to a small, printable
                # number instead of hiding inside a 25M-iteration loop.
                print("  scanning module bytes locally for plausible pointers...")
                # Exclude the known GObjects array region - it's a huge
                # legitimate array of heap pointers (one per UObject) that
                # was drowning out any real FNamePool signal underneath it.
                gobjects_off = 0x46556C8
                gobjects_exclude_range = (gobjects_off - 0x1000, gobjects_off + 0x200000)  # generous margin around the array itself
                candidates = []
                total_slots = (len(module_bytes) - 8) // 8
                for i, off in enumerate(range(0, len(module_bytes) - 8, 8)):
                    if i % 2_000_000 == 0 and i > 0:
                        print(f"    ...{i}/{total_slots} slots scanned, {len(candidates)} candidate(s) so far")
                    if gobjects_exclude_range[0] <= off <= gobjects_exclude_range[1]:
                        continue
                    v = int.from_bytes(module_bytes[off:off + 8], "little")
                    if v < 0x10000 or v > 0x7FFFFFFFFFFF:
                        continue
                    if _in_known_region(v):
                        candidates.append((off, v))

                print(f"  {len(candidates)} candidate(s) survived the local filter - checking each remotely...")
                checked = 0
                for off, v in candidates:
                    checked += 1
                    try:
                        candidate_blocks0 = pm.read_longlong(v + 0x10)
                    except Exception:
                        continue
                    if candidate_blocks0 == entry_addr:
                        if resolve_fname(pm, 0, pool_base=v) == "None":
                            print(f"\nMATCH - pool_base=0x{v:X} (module+0x{off:X} held a pointer to it)")
                            save_lookup("gnames_address.json", {0: v})
                            OFFSETS["gnames"] = v
                            print("Saved to gnames_address.json. Try 'identify_classes' now.")
                            found = True
                            break
                print(f"  checked {checked} candidate(s) remotely")

            if not found:
                print("\nNo verified pool_base found via either layout. The FNamePool may be "
                      "reached through one more layer of indirection than either attempt "
                      "covers (e.g. module -> pointer -> pointer -> pool), which would need "
                      "manual reverse-engineering to pin down precisely.")
    elif len(sys.argv) > 1 and sys.argv[1] == "verify_gnames":
        # python dungeons_reader.py verify_gnames <hex_address>
        # Tests a candidate FNamePool base address. Index 0 is reserved
        # for "None" in EVERY UE4.23+ build - if this candidate resolves
        # index 0 to "None", it's very likely correct.
        #
        # HOW TO FIND A CANDIDATE ADDRESS: this script can't safely
        # locate it via a live disassembly/AOB scan on its own (that's
        # genuinely a manual reverse-engineering step) - but you
        # mentioned already having a Cheat Engine table for this game,
        # which is the easiest path: open the CT, find any symbol/pointer
        # already resolving names (or do a fresh AOB/pointer scan for a
        # known string like "ChestActor" in CE's own tools), and pass
        # whatever static/module-relative address you find here as
        # <module_base>+<offset> in hex.
        if len(sys.argv) < 3:
            print("Usage: verify_gnames <hex_address>  (e.g. verify_gnames 7FF6A0000000)")
        else:
            try:
                candidate = int(sys.argv[2], 16)
            except ValueError:
                print("Couldn't parse that as a hex address.")
                candidate = None
            if candidate:
                result = resolve_fname(pm, 0, pool_base=candidate)
                if result == "None":
                    print(f"MATCH - 0x{candidate:X} resolved index 0 to 'None'. This is very likely the real GNames base.")
                    print("Saving to gnames_address.json...")
                    save_lookup("gnames_address.json", {0: candidate})
                    OFFSETS["gnames"] = candidate
                    print("Saved. Try 'identify_classes' now to auto-resolve real names for everything in the current zone.")
                else:
                    print(f"No match - index 0 resolved to {result!r} (expected 'None'). "
                          f"Try a different candidate address.")
    elif len(sys.argv) > 1 and sys.argv[1] == "identify_classes":
        # python dungeons_reader.py identify_classes
        # Requires gnames verified first (see verify_gnames). Scans the
        # current zone and prints the REAL class name string for every
        # class_name_index present - replacing the old workflow of
        # guessing from instance counts and manually labeling. This is
        # what finally makes chest/enemy/zone identification stable
        # across game restarts, since it reads the true name instead of
        # a session-local numeric index.
        if not OFFSETS.get("gnames"):
            print("No verified GNames address yet - run 'verify_gnames <hex_address>' first.")
        else:
            world = pm.read_longlong(base + OFFSETS["gworld"])
            if not world:
                print("No UWorld - are you in a level?")
            else:
                snapshot = scan_full_zone(pm, world)
                from collections import Counter
                counts = Counter(snapshot.values())
                print(f"{len(counts)} distinct class(es) in this zone:\n")
                for cls, n in counts.most_common():
                    real_name = resolve_fname(pm, cls)
                    flag = ""
                    if real_name and "chest" in real_name.lower():
                        flag = "  <-- CHEST"
                    print(f"  class_name_index={cls:<8} count={n:<4} name={real_name!r}{flag}")
                print("\nLook for the real 'ChestActor' (or similar) line above and note its")
                print("class_name_index - that's the stable way to update confirmed_chest_classes.json")
                print("going forward, and you can re-run this after any restart instead of guessing.")
    elif len(sys.argv) > 1 and sys.argv[1] == "diff_actor_bytes":
        # python dungeons_reader.py diff_actor_bytes <hex_addr> [scan_size]
        # For actors like SupplyChest that have no name or struct in any
        # static dump (confirmed via ClassesInfo.json - AChestActor has 0
        # listed subclasses, since Blueprint-only classes like the real
        # chest variants only exist as runtime objects, never in a
        # header). Instead of guessing offsets, this watches raw bytes at
        # a given address and reports which ones CHANGE between polls -
        # open the chest while this is running and whichever byte flips
        # is almost certainly bOpened (or close to it), the same way
        # AChestActor's real offsets were originally found, just done
        # empirically instead of from a header.
        #
        # Get an address from list_chests (the addr= column) for an
        # instance whose class_name_index ISN'T in the auto-calibrated
        # set - i.e. exactly the kind of actor that gets excluded today.
        if len(sys.argv) < 3:
            print("Usage: diff_actor_bytes <hex_addr> [scan_size=0x400]")
        else:
            try:
                target_addr = int(sys.argv[2], 16)
            except ValueError:
                print("Couldn't parse that as a hex address.")
                target_addr = None
            scan_size = int(sys.argv[3], 16) if len(sys.argv) > 3 else 0x400
            if target_addr:
                try:
                    baseline = pm.read_bytes(target_addr, scan_size)
                except Exception as e:
                    baseline = None
                    print(f"Couldn't read that address: {e}")
                if baseline is not None:
                    print(f"Watching 0x{scan_size:X} bytes at 0x{target_addr:X} - baseline captured.")
                    print("Now go open the chest in-game. Polling every 1s (Ctrl+C to stop)...\n")
                    try:
                        while True:
                            time.sleep(1.0)
                            try:
                                current = pm.read_bytes(target_addr, scan_size)
                            except Exception:
                                continue
                            if current != baseline:
                                changed = [i for i in range(len(baseline)) if baseline[i] != current[i]]
                                # Group consecutive changed offsets together - a single bool flip
                                # shows as one offset, but multi-byte fields (timers, pointers) show
                                # as a run of several - useful to see them as one field, not N lines.
                                groups = []
                                for i in changed:
                                    if groups and i == groups[-1][-1] + 1:
                                        groups[-1].append(i)
                                    else:
                                        groups.append([i])
                                print(f"CHANGE DETECTED at {time.strftime('%H:%M:%S')}:")
                                for g in groups:
                                    off_start = g[0]
                                    old_bytes = baseline[off_start:g[-1] + 1]
                                    new_bytes = current[off_start:g[-1] + 1]
                                    print(f"  offset +0x{off_start:X} (len {len(g)}): "
                                          f"{old_bytes.hex()} -> {new_bytes.hex()}")
                                baseline = current
                                print("  (baseline updated - keep watching for more changes, or Ctrl+C when done)\n")
                    except KeyboardInterrupt:
                        print("\nStopped. Whatever offset(s) got printed above are your real candidates "
                              "for this class's bOpened/state fields.")
    elif len(sys.argv) > 1 and sys.argv[1] == "find_gamebp":
        # python dungeons_reader.py find_gamebp
        # DIAGNOSTIC - run this in 2-3 different missions (not the lobby).
        # AGameBP is a per-level singleton (exactly one instance loaded at
        # a time), unlike chests/enemies which have many. This prints
        # every class_name_index with EXACTLY 1 instance in the current
        # zone, plus what byte sits at +0x6B8 on that instance - the real
        # mission's ELevelNames value if this is the right class. Compare
        # the singleton list across missions: the index that's (a) present
        # every time and (b) shows a DIFFERENT +0x6B8 byte per mission is
        # AGameBP. Report back the printed lines and I'll wire it in.
        world = pm.read_longlong(base + OFFSETS["gworld"])
        if not world:
            print("No UWorld - are you in a level?")
        else:
            snapshot = scan_full_zone(pm, world)
            counts = classify_actors(snapshot)
            singletons = [cls for cls, n in counts if n == 1]
            print(f"{len(singletons)} class(es) with exactly 1 instance this zone:\n")
            for cls in singletons:
                addr = next(a for a, c in snapshot.items() if c == cls)
                try:
                    probe_byte = pm.read_uchar(addr + 0x6B8)
                except Exception:
                    probe_byte = "?"
                label = ENEMY_CLASS_LOOKUP.get(cls) or NAME_LOOKUP.get(cls) or ""
                print(f"  class_name_index={cls:<8} addr=0x{addr:X}  byte@+0x6B8={probe_byte}  {label}")
    elif len(sys.argv) > 1 and sys.argv[1] == "resolve_class":
        # python dungeons_reader.py resolve_class <class_name_index>
        if len(sys.argv) < 3:
            print("Usage: python dungeons_reader.py resolve_class <class_name_index>")
        else:
            try:
                idx = int(sys.argv[2])
            except ValueError:
                print("class_name_index must be an integer.")
            else:
                name = resolve_fname(pm, idx)
                print(f"class_name_index={idx} -> {name}")
    elif len(sys.argv) > 1 and sys.argv[1] == "watch_player_pos":
        # python dungeons_reader.py watch_player_pos
        # Sanity check on get_actor_location() itself, against known ground
        # truth: does the printed position actually track real movement?
        # Walk in a straight line (e.g. forward) for the whole 15s. If the
        # numbers below don't change smoothly and substantially in that
        # time, get_actor_location (root_component/relative_location
        # offsets) is unreliable - which would explain every contradictory
        # proximity/distance result across every chest test so far, since
        # ALL of them depend on this same function.
        pawn, error = get_pawn(pm, base)
        if not pawn:
            print(f"Could not reach Pawn: {error}")
        else:
            print("Printing player position every 0.5s for 15s - WALK IN A STRAIGHT LINE now...")
            t_start = time.time()
            last = None
            while time.time() - t_start < 15:
                time.sleep(0.5)
                loc = get_actor_location(pm, pawn)
                if not loc:
                    print(f"  [t={time.time()-t_start:.1f}s] no position readable")
                    continue
                moved = ""
                if last:
                    d = ((loc[0]-last[0])**2 + (loc[1]-last[1])**2 + (loc[2]-last[2])**2) ** 0.5
                    moved = f"  (moved {d:.1f} units since last print)"
                print(f"  [t={time.time()-t_start:.1f}s] ({loc[0]:.1f}, {loc[1]:.1f}, {loc[2]:.1f}){moved}")
                last = loc
    elif len(sys.argv) > 1 and sys.argv[1] == "raw_diff_by_position":
        # python dungeons_reader.py raw_diff_by_position <x> <y> <z> [radius]
        # Both the class_type filter (too many always-zero-at-0x330 false
        # positives, e.g. class_name_index=25881 which doesn't even resolve
        # to a real class name) and distance-to-player targeting (unreliable
        # RelativeLocation math) have produced bad candidates so far. This
        # instead targets by a WORLD POSITION you already know is a real
        # chest (confirmed across multiple earlier sessions) - matched by
        # proximity to that fixed point, not to the player. Also polls for
        # 20s AND does a full byte diff in the same run, so a delayed write
        # can't be missed either.
        if len(sys.argv) < 5:
            print("Usage: python dungeons_reader.py raw_diff_by_position <x> <y> <z> [radius=100]")
        else:
            target = (float(sys.argv[2]), float(sys.argv[3]), float(sys.argv[4]))
            radius = float(sys.argv[5]) if len(sys.argv) > 5 else 100.0
            world = pm.read_longlong(base + OFFSETS["gworld"])
            if not world:
                print("No UWorld - are you in a level?")
            else:
                snapshot = scan_full_zone(pm, world)
                best = None
                best_dist = None
                for addr, cls in snapshot.items():
                    loc = get_actor_location(pm, addr)
                    if not loc or loc == (0.0, 0.0, 0.0):
                        continue
                    dist = ((loc[0] - target[0]) ** 2 + (loc[1] - target[1]) ** 2 + (loc[2] - target[2]) ** 2) ** 0.5
                    if dist <= radius and (best_dist is None or dist < best_dist):
                        best, best_dist = (addr, cls, loc), dist

                if not best:
                    print(f"No actor found within {radius} units of {target}.")
                else:
                    addr, cls, loc = best
                    print(f"Targeting addr={hex(addr)} cls={cls} at ({loc[0]:.1f},{loc[1]:.1f},{loc[2]:.1f}), "
                          f"{best_dist:.1f} units from requested position.")
                    WINDOW = 0x800
                    try:
                        before_bytes = pm.read_bytes(addr, WINDOW)
                    except Exception as e:
                        print(f"Could not read memory: {e}")
                        before_bytes = None

                    if before_bytes is not None:
                        input(f"\nDumped {WINDOW} bytes. Press Enter, THEN open THAT chest within 20s...")
                        t_start = time.time()
                        flagged = False
                        while time.time() - t_start < 20:
                            time.sleep(0.5)
                            try:
                                cur = pm.read_bytes(addr, WINDOW)
                            except Exception as e:
                                print(f"[t={time.time()-t_start:.1f}s] no longer readable ({e})")
                                flagged = True
                                break
                            if cur != before_bytes:
                                elapsed = time.time() - t_start
                                diffs = [(i, before_bytes[i], cur[i]) for i in range(WINDOW) if before_bytes[i] != cur[i]]
                                print(f"\n[t={elapsed:.1f}s] {len(diffs)} byte(s) changed:")
                                for off, b, a in diffs[:40]:
                                    print(f"    +{hex(off):<6} : {b:3d} (0x{b:02X}) -> {a:3d} (0x{a:02X})")
                                if len(diffs) > 40:
                                    print(f"    ...and {len(diffs) - 40} more byte(s)")
                                flagged = True
                                break
                        if not flagged:
                            print("\nNo change detected across the full 20s window.")
    elif len(sys.argv) > 1 and sys.argv[1] == "debug_gobjects":
        # python dungeons_reader.py debug_gobjects
        # Diagnostic: dumps raw bytes at base+OFFSETS["gobjects"] so we can
        # visually verify the FUObjectArray layout assumption instead of
        # guessing blindly. What to look for in the output:
        #   - the +0x10 qword should look like a real pointer (starts with
        #     0x1 or 0x2 typically, matches the same address range as
        #     other pointers this script already reads successfully, e.g.
        #     compare against a `gworld` value from `python dungeons_reader.py`
        #     with no args, or any actor address from `nearest_chest`)
        #   - somewhere in the 0x10-0x30 range there should be a small
        #     int (4 bytes) roughly matching total live UObject count -
        #     order of magnitude ~600000 (0x927xx-ish in hex, little-endian
        #     so e.g. bytes matching 90 27 09 00 for ~615056), NOT -1/0xFFFFFFFF.
        addr = base + OFFSETS["gobjects"]
        try:
            raw = pm.read_bytes(addr, 0x40)
            print(f"Raw bytes at base+0x{OFFSETS['gobjects']:X} (base={hex(base)}, addr={hex(addr)}):\n")
            for row in range(0, len(raw), 16):
                chunk = raw[row:row+16]
                hex_str = " ".join(f"{b:02x}" for b in chunk)
                print(f"  +0x{row:02X}: {hex_str}")

            print("\nInterpreted as qwords (8-byte, little-endian) at each offset:")
            for off in range(0, 0x40, 8):
                try:
                    val = pm.read_longlong(addr + off)
                    print(f"  +0x{off:02X}: {hex(val)}  ({val})")
                except Exception as e:
                    print(f"  +0x{off:02X}: <read failed: {e}>")

            print("\nInterpreted as int32s at each offset (for spotting NumElements):")
            for off in range(0, 0x40, 4):
                try:
                    val = pm.read_int(addr + off)
                    print(f"  +0x{off:02X}: {val}")
                except Exception as e:
                    print(f"  +0x{off:02X}: <read failed: {e}>")
        except Exception as e:
            print(f"Could not read at {hex(addr)}: {e}")
            print("If this fails entirely, OFFSETS['gobjects'] itself is likely wrong "
                  "(wrong module base, or the offset needs an extra dereference).")
    elif len(sys.argv) > 1 and sys.argv[1] == "find_mission_progress_component":
        # python dungeons_reader.py find_mission_progress_component
        # call_is_mission_completed needs a pointer, directly on the Pawn,
        # to its UMissionProgressComponent - OFFSETS["mission_progress_component"]
        # was never actually filled in (IS_MISSION_COMPLETED_INDEX got
        # confirmed via confirm_is_mission_completed, but this pointer
        # offset never did - a real gap), which is why mission completion
        # currently never gets detected/reported at all, every time,
        # regardless of zone. Scans the Pawn's own field range for a
        # pointer that resolves to a class whose name contains
        # "MissionProgress". Run this from INSIDE an active mission (not
        # the hub/menu) - the component most likely doesn't exist outside
        # one, the same way MissionProgressHandler doesn't (see
        # identify_mission_progress_handler above).
        pawn, error = get_pawn(pm, base)
        if not pawn:
            print(f"Could not reach Pawn: {error}")
        else:
            print("Scanning Pawn's fields for a MissionProgressComponent "
                  "pointer (a few seconds)...\n")
            found = []
            for offset in range(0, 0x2000, 8):
                try:
                    ptr = pm.read_longlong(pawn + offset)
                except Exception:
                    continue
                if ptr < 0x10000 or ptr > 0x7FFFFFFFFFFF:
                    continue  # not a plausible heap pointer
                try:
                    obj_class = pm.read_longlong(ptr + OFFSETS["uobject_class"])
                    if not obj_class:
                        continue
                    class_name_index = pm.read_int(obj_class + 0x18)
                    name = resolve_fname(pm, class_name_index)
                except Exception:
                    continue
                if name and "missionprogress" in name.lower():
                    found.append((offset, ptr, name))
            if not found:
                print("No matching pointer found. Make sure you're inside an "
                      "active mission (not the hub/menu) when running this.")
            else:
                print(f"{len(found)} candidate(s):\n")
                for offset, ptr, name in found:
                    print(f"  +{hex(offset)}  ->  {hex(ptr)}  ({name})")
                print("\nAdd the matching one to dungeons_reader.py, near the other "
                      "pawn-relative OFFSETS entries, as:\n"
                      "  OFFSETS[\"mission_progress_component\"] = <offset>")
    elif len(sys.argv) > 1 and sys.argv[1] == "identify_mission_end_widget":
        # python dungeons_reader.py identify_mission_end_widget
        # Run this, THEN go finish a mission for real (or fail one, to
        # test the negative case) and press Enter the instant the "LEVEL
        # COMPLETE" screen appears. Diffs the FULL GObjects pointer array
        # (not just new indices past the old NumElements) - transient
        # UObjects very often reuse a freed slot somewhere in the middle
        # of the array rather than appending at the tail, so a tail-only
        # diff misses most of them (this is why the tail version found
        # nothing). A full snapshot diff is done with bulk chunk reads
        # (see get_gobjects_pointer_snapshot), so it's still fast despite
        # covering ~650k+ entries.
        if MISSION_END_WIDGET_CLASS is not None:
            print(f"Already confirmed: class_name_index={MISSION_END_WIDGET_CLASS} in "
                  f"{MISSION_END_WIDGET_LOOKUP_FILE}. Delete that file first if you need to redo this.")
        else:
            print("Taking baseline snapshot of GObjects (may take a moment)...")
            snapshot_before = get_gobjects_pointer_snapshot(pm, base)
            if not snapshot_before:
                print("Could not read GObjects - check OFFSETS['gobjects'] / game is running.")
            else:
                print(f"Baseline: {len(snapshot_before)} live slots captured.")
                input("Now finish (or fail) a mission for real, and press Enter the INSTANT "
                      "the 'LEVEL COMPLETE' (or mission failed) screen appears...")
                snapshot_after = get_gobjects_pointer_snapshot(pm, base)
                if not snapshot_after:
                    print("Could not re-read GObjects.")
                else:
                    # An index counts as "changed" if its pointer differs from
                    # before (covers: was 0/empty and now populated - the
                    # classic append case - AND was some other now-destroyed
                    # object and got reused for a new one - the common case
                    # this fixes). Indices only in the after-snapshot (array
                    # grew) count too.
                    changed = []
                    all_indices = set(snapshot_before) | set(snapshot_after)
                    for idx in all_indices:
                        before_ptr = snapshot_before.get(idx, 0)
                        after_ptr = snapshot_after.get(idx, 0)
                        if after_ptr and after_ptr != before_ptr:
                            changed.append(after_ptr)

                    if not changed:
                        print("No changed/new object pointers detected - try pressing Enter "
                              "earlier, right as the screen first appears.")
                    else:
                        print(f"{len(changed)} new/changed object(s) - checking their classes "
                              f"(this part IS one read per object, but only for the small "
                              f"changed set, not all ~650k)...")
                        class_counts = {}
                        class_sample_obj = {}
                        for obj in changed:
                            cls = get_uobject_class_name_index(pm, obj)
                            if cls is not None:
                                class_counts[cls] = class_counts.get(cls, 0) + 1
                                class_sample_obj.setdefault(cls, obj)

                        print(f"\n{len(class_counts)} distinct new class(es):")
                        results = []
                        for cls, cnt in class_counts.items():
                            sample = class_sample_obj[cls]
                            try:
                                victory_byte = pm.read_uchar(sample + OFFSETS["mission_end_victory"])
                                victory_str = str(bool(victory_byte)) if victory_byte in (0, 1) else f"garbage({victory_byte})"
                            except Exception:
                                victory_str = "unreadable"

                            def _sane_float(off):
                                try:
                                    v = pm.read_float(sample + off)
                                    return v if (v == v and 0.0 <= v <= 600.0) else None  # v==v excludes NaN
                                except Exception:
                                    return None

                            spawn_time = _sane_float(OFFSETS["mission_end_spawn_time"])
                            wait_duration = _sane_float(OFFSETS["mission_end_wait_duration"])

                            score = sum([
                                victory_str in ("True", "False"),
                                spawn_time is not None,
                                wait_duration is not None,
                            ])
                            results.append((score, cls, cnt, victory_str, spawn_time, wait_duration))

                        results.sort(key=lambda r: (-r[0], -r[2]))
                        for score, cls, cnt, victory_str, spawn_time, wait_duration in results[:40]:
                            st_str = f"{spawn_time:.2f}" if spawn_time is not None else "garbage"
                            wd_str = f"{wait_duration:.2f}" if wait_duration is not None else "garbage"
                            flag = "  <-- STRONG candidate (all 3 fields sane)" if score == 3 else (
                                   "  <-- possible candidate" if score == 2 else "")
                            print(f"  class_name_index={cls:<8} instances_created={cnt:<3} "
                                  f"victory={victory_str:<8} spawn_time={st_str:<8} "
                                  f"wait_duration={wd_str:<8}{flag}")
                        if len(results) > 40:
                            print(f"  ... and {len(results) - 40} more (not shown)")

                        # Secondary ranking: closest wait_duration to the REAL measured
                        # in-game timer (8s per the actual observed countdown) - this can
                        # surface a low-instance-count candidate that the instance-count
                        # sort above buried past the 40-item cutoff.
                        target_wait = 8.0
                        if "--target-wait" in sys.argv:
                            idx = sys.argv.index("--target-wait")
                            if idx + 1 < len(sys.argv):
                                try:
                                    target_wait = float(sys.argv[idx + 1])
                                except ValueError:
                                    pass
                        by_wait = [r for r in results if r[5] is not None]
                        by_wait.sort(key=lambda r: abs(r[5] - target_wait))
                        print(f"\nClosest to a {target_wait:.1f}s wait_duration (regardless of "
                              f"instance count):")
                        for score, cls, cnt, victory_str, spawn_time, wait_duration in by_wait[:10]:
                            print(f"  class_name_index={cls:<8} instances_created={cnt:<3} "
                                  f"wait_duration={wait_duration:.2f}  (diff={abs(wait_duration - target_wait):.2f})")

                        print("\nPick a STRONG candidate first if one exists. If several tie, "
                              "rerun this for the opposite outcome (win vs fail) and compare -"
                              "the real one is the one whose victory flips between runs while "
                              "still passing all 3 checks.")
                        pick = input("\nclass_name_index for the mission-complete widget "
                                      "(blank to skip): ").strip()
                        if pick:
                            try:
                                cls_id = int(pick)
                                _save_mission_end_widget_class(cls_id)
                                print("Saved - permanent from now on.")
                            except ValueError:
                                print("Not a valid integer - nothing saved.")
    elif len(sys.argv) > 1 and sys.argv[1] == "identify_health_attribute_set":
        # python dungeons_reader.py identify_health_attribute_set
        # Lists every entry in the pawn's SpawnedAttributes array (usually
        # ~10-20 - one per attribute set type: health, movement, melee,
        # ranged, etc), reading Health/MaxHealth at UHealthAttributeSet's
        # known offsets from EACH one. Reading those offsets on the WRONG
        # attribute set just gives garbage floats (different class,
        # different layout) - so the real one should stand out as having
        # sane values (0 <= Health <= MaxHealth, MaxHealth in a plausible
        # range like 50-5000) while the others look like nonsense.
        # IMPORTANT: go take some damage FIRST so Health < MaxHealth when
        # you run this - if they're equal you can't fully rule out it
        # being the wrong entry that coincidentally looks sane.
        if HEALTH_ATTRIBUTE_SET_CLASS is not None:
            print(f"Already confirmed: class_name_index={HEALTH_ATTRIBUTE_SET_CLASS} in "
                  f"{HEALTH_ATTRIBUTE_SET_LOOKUP_FILE}. Delete that file first if you need to redo this.")
        else:
            pawn, error = get_pawn(pm, base)
            if not pawn:
                print(f"Could not reach Pawn: {error}")
            else:
                entries = get_spawned_attributes(pm, pawn)
                if not entries:
                    print("No SpawnedAttributes found - check OFFSETS['ability_system_component'] "
                          "and OFFSETS['spawned_attributes_array'].")
                else:
                    print(f"{len(entries)} attribute set(s) found:\n")
                    for attr_set, cls in entries:
                        try:
                            health = pm.read_float(attr_set + OFFSETS["health_attr_health"])
                            max_health = pm.read_float(attr_set + OFFSETS["health_attr_max_health"])
                            sane = (0.0 <= health <= max_health and 10.0 <= max_health <= 5000.0
                                    and health == health and max_health == max_health)  # NaN check
                        except Exception:
                            health, max_health, sane = None, None, False
                        flag = "  <-- STRONG candidate" if sane else ""
                        print(f"  class_name_index={cls!s:<8} Health={health}  MaxHealth={max_health}{flag}")

                    print("\nPick the flagged candidate. If several look sane, take more damage "
                          "and rerun to see which one's Health actually changed.")
                    pick = input("\nclass_name_index for the real HealthAttributeSet "
                                  "(blank to skip): ").strip()
                    if pick:
                        try:
                            cls_id = int(pick)
                            _save_health_attribute_set_class(cls_id)
                            print("Saved - permanent from now on.")
                        except ValueError:
                            print("Not a valid integer - nothing saved.")
    elif len(sys.argv) > 1 and sys.argv[1] == "watch_level_lock":
        # python dungeons_reader.py watch_level_lock --ap-host HOST --slot SLOT [--ap-port PORT] [--game NAME] [--password PASS]
        # Enforces Archipelago's mission-access items for real: connects,
        # receives every item ever sent to this slot (via
        # ap_client.poll_received_items, same mechanism as filler-reward
        # receiving), and maintains a persisted set of unlocked zones
        # (unlocked_zones.json) from any "<Mission> Access" items seen.
        #
        # The game's OWN mission-unlock system (UMissionProgressComponent)
        # computes unlock state from mission COMPLETION, not from AP items
        # received - those are two different things in a randomizer (you
        # can logically be "done" with Squid Coast in-game while the AP
        # item for Creeper Woods is still sitting on someone else's board).
        # We can't hook the game's own unlock check without code injection,
        # so this enforces reactively instead: every tick, if the current
        # zone is a tracked base-game mission AND its Access item hasn't
        # been received yet, it immediately calls kill_local_player() and
        # warns - same idea as watch_deathlink's enforcement, just gated
        # on inventory instead of an incoming Bounce.
        #
        # BASE GAME ONLY for now (see MISSION_ACCESS_ITEM_IDS) - DLC zones
        # and zones without a tracked Access item are never locked by this.
        def _get_flag(name, default=None):
            if name in sys.argv:
                idx = sys.argv.index(name)
                if idx + 1 < len(sys.argv):
                    return sys.argv[idx + 1]
            return default

        ap_host = _get_flag("--ap-host")
        ap_slot = _get_flag("--slot")
        if not ap_host or not ap_slot:
            print("Needs a real connection - pass --ap-host <host> --slot <name>.")
        else:
            ap_port = int(_get_flag("--ap-port", "38281"))
            ap_game = _get_flag("--game", "Minecraft Dungeons")
            ap_password = _get_flag("--password", "")

            from ap_client import ArchipelagoClient
            ap_client = ArchipelagoClient(ap_host, ap_port, ap_slot, ap_game, ap_password)
            try:
                ap_client.connect()
                print(f"Connected to Archipelago as '{ap_slot}' ({ap_game}) at {ap_host}:{ap_port}.")
            except Exception as e:
                print(f"Could not connect to Archipelago: {e}")
                ap_client = None

            if ap_client is not None:
                unlocked = _load_unlocked_zones()
                print(f"Unlocked so far: {sorted(unlocked)}\n")
                print("Watching for locked-mission entry and incoming items (Ctrl+C to stop)...\n")

                current_zone_index = "__unset__"
                last_warned_zone = None
                try:
                    while True:
                        time.sleep(0.5)

                        # --- pick up any newly received Access items ---
                        received = ap_client.poll_received_items()
                        newly_unlocked = set()
                        for item_id, _abs_index in received:
                            zone_name = ITEM_ID_TO_ZONE.get(item_id)
                            if zone_name and zone_name not in unlocked:
                                unlocked.add(zone_name)
                                newly_unlocked.add(zone_name)
                        if newly_unlocked:
                            _save_unlocked_zones(unlocked)
                            for z in sorted(newly_unlocked):
                                print(f"  [UNLOCKED] {z}")

                        # --- check current zone against the unlocked set ---
                        try:
                            world = pm.read_longlong(base + OFFSETS["gworld"])
                        except Exception:
                            continue
                        if not world:
                            continue
                        zone_index = get_zone_name_index(pm, world)
                        zone_name = ZONE_NAME_LOOKUP.get(zone_index, f"unknown_zone_{zone_index}") if zone_index is not None else "unknown_zone"

                        if zone_index != current_zone_index:
                            current_zone_index = zone_index
                            last_warned_zone = None
                            print(f"=== Entered zone: {zone_name} ===")

                        if zone_name in MISSION_ACCESS_ITEM_IDS and zone_name not in unlocked:
                            if last_warned_zone != zone_name:
                                # First tick detecting this locked zone since we last
                                # left it - punish exactly once, not every tick, or
                                # the player would lose a totem every 0.5s the whole
                                # time they're stuck in here (e.g. during their own
                                # death/respawn sequence, which can linger in the
                                # same zone for a few seconds).
                                print(f"  [LOCKED MISSION] '{zone_name}' - Access item not received yet.")
                                last_warned_zone = zone_name
                                success, kill_error, diag = kill_local_player(pm, base)
                                if success:
                                    print(f"    -> enforced (killed local player, chain={diag.get('chain')}).")
                                else:
                                    print(f"    -> enforcement failed: {kill_error}")
                except KeyboardInterrupt:
                    print("\nStopped watching.")
                finally:
                    ap_client.close()
    elif len(sys.argv) > 1 and sys.argv[1] == "set_ap_goal":
        # python dungeons_reader.py set_ap_goal <zone_internal_name>
        # python dungeons_reader.py set_ap_goal --list       (see available zones)
        # python dungeons_reader.py set_ap_goal --clear      (no goal mission)
        # Which single mission's completion declares the AP goal reached
        # (sends StatusUpdate/CLIENT_GOAL in addition to its own
        # LocationCheck). Base-game missions only for now - see
        # MISSION_LOCATION_IDS above.
        if len(sys.argv) < 3:
            print("Usage: set_ap_goal <zone_internal_name> | --list | --clear")
        elif sys.argv[2] == "--list":
            print("Available zones (base game only, for now):")
            for zone_name in sorted(MISSION_LOCATION_IDS):
                print(f"  {zone_name}")
        elif sys.argv[2] == "--clear":
            if os.path.exists(AP_GOAL_FILE):
                os.remove(AP_GOAL_FILE)
            print("Goal zone cleared - watch_mission_end will send checks but never "
                  "declare the AP goal complete.")
        else:
            zone_name = sys.argv[2]
            if zone_name not in MISSION_LOCATION_IDS:
                print(f"'{zone_name}' isn't a known base-game zone. Run with --list to see options.")
            else:
                _save_ap_goal_zone(zone_name)
                print(f"Goal zone set to '{zone_name}' - completing it will also declare "
                      f"the AP goal reached (in addition to its own LocationCheck).")
    elif len(sys.argv) > 1 and sys.argv[1] == "watch_mission_end":
        # python dungeons_reader.py watch_mission_end
        #   [--ap-host HOST] [--ap-port PORT] [--slot SLOT_NAME] [--game GAME_NAME] [--password PASS]
        # Without any --ap-* flags, this just prints [MISSION COMPLETE] events
        # locally (same as before). Add --ap-host to also connect to an
        # Archipelago server and send real LocationChecks for base-game
        # missions (see MISSION_LOCATION_IDS) - DLC missions aren't sent yet,
        # they're simply not in that table. If a goal zone is configured
        # (see `set_ap_goal`), completing it also sends CLIENT_GOAL.
        #
        # The real completion signal: watches GObjects for the confirmed
        # mission-end-widget class to appear ANYWHERE in the array (full
        # pointer-diff each tick, not just past the old NumElements) -
        # transient widgets very often reuse a freed slot rather than
        # growing the array, so a tail-only diff would miss most opens.
        # Same last_mission_zone tracking as watch_end_chest/door.
        if MISSION_END_WIDGET_CLASS is None:
            print(f"No confirmed class in {MISSION_END_WIDGET_LOOKUP_FILE} yet - run "
                  f"`identify_mission_end_widget` first.")
        else:
            def _get_flag(name, default=None):
                if name in sys.argv:
                    idx = sys.argv.index(name)
                    if idx + 1 < len(sys.argv):
                        return sys.argv[idx + 1]
                return default

            ap_host = _get_flag("--ap-host")
            ap_client = None
            if ap_host:
                ap_port = int(_get_flag("--ap-port", "38281"))
                ap_slot = _get_flag("--slot")
                ap_game = _get_flag("--game", "Minecraft Dungeons")
                ap_password = _get_flag("--password", "")
                if not ap_slot:
                    print("--ap-host given but --slot is missing - can't connect without a slot name.")
                else:
                    from ap_client import ArchipelagoClient
                    ap_client = ArchipelagoClient(ap_host, ap_port, ap_slot, ap_game, ap_password)
                    try:
                        ap_client.connect()
                        print(f"Connected to Archipelago as '{ap_slot}' ({ap_game}) at {ap_host}:{ap_port}.")
                    except Exception as e:
                        print(f"Could not connect to Archipelago: {e} - continuing in local-only mode.")
                        ap_client = None

            goal_zone = _load_ap_goal_zone()
            if goal_zone:
                print(f"Goal zone: '{goal_zone}' (completing it will send CLIENT_GOAL).")
            if ap_client is None:
                print("No Archipelago connection - running in local/print-only mode "
                      "(pass --ap-host <host> --slot <name> to send real checks).")

            print("Watching for the mission-end widget to appear (Ctrl+C to stop)...\n")
            current_zone_index = "__unset__"
            last_mission_zone = None
            last_snapshot = get_gobjects_pointer_snapshot(pm, base)
            try:
                while True:
                    time.sleep(0.5)  # faster than the 1.0s zone-watchers - transient widgets
                                      # can get created and cleaned up quickly
                    try:
                        world = pm.read_longlong(base + OFFSETS["gworld"])
                    except Exception:
                        continue
                    if not world:
                        continue

                    zone_index = get_zone_name_index(pm, world)
                    if zone_index != current_zone_index:
                        current_zone_index = zone_index
                        zone_name = ZONE_NAME_LOOKUP.get(zone_index, f"unknown_zone_{zone_index}") if zone_index is not None else "unknown_zone"
                        print(f"=== Entered zone: {zone_name} ===")
                        if zone_name in ZONE_ID_ORDER:
                            last_mission_zone = zone_name
                            print(f"  (tracking as last mission zone: {zone_name})")

                    new_snapshot = get_gobjects_pointer_snapshot(pm, base)
                    if not new_snapshot:
                        continue

                    all_indices = set(last_snapshot) | set(new_snapshot)
                    for idx in all_indices:
                        before_ptr = last_snapshot.get(idx, 0)
                        after_ptr = new_snapshot.get(idx, 0)
                        if not after_ptr or after_ptr == before_ptr:
                            continue
                        obj = after_ptr
                        cls = get_uobject_class_name_index(pm, obj)
                        if cls == MISSION_END_WIDGET_CLASS:
                            attributed_to = last_mission_zone or "unknown mission"
                            # NOTE: the Victory field at this offset read False across every
                            # real win tested during identify_mission_end_widget, so it's not
                            # a reliable gate for THIS class (likely reads before the widget's
                            # own BP logic sets it, or this offset isn't quite Victory for this
                            # particular class). The class's appearance itself already proved
                            # reliable - absent on every tested failure, present on every
                            # tested win - so that's what actually gates the check here.
                            try:
                                victory = bool(pm.read_uchar(obj + OFFSETS["mission_end_victory"]))
                            except Exception:
                                victory = None
                            print(f"  [MISSION COMPLETE] {attributed_to}  (victory_field={victory}, informational only)")

                            location_id = MISSION_LOCATION_IDS.get(attributed_to)
                            if ap_client is not None:
                                if location_id is not None:
                                    try:
                                        ap_client.send_location_checks([location_id])
                                        print(f"    -> sent LocationCheck {location_id} to Archipelago.")
                                    except Exception as e:
                                        print(f"    -> failed to send LocationCheck: {e}")
                                else:
                                    print(f"    -> '{attributed_to}' has no known location_id yet "
                                          f"(DLC zone, or last_mission_zone wasn't tracked) - not sent.")
                                if goal_zone and attributed_to == goal_zone:
                                    try:
                                        ap_client.send_goal_complete()
                                        print(f"    -> goal zone reached, sent CLIENT_GOAL.")
                                    except Exception as e:
                                        print(f"    -> failed to send goal completion: {e}")
                    last_snapshot = new_snapshot
            except KeyboardInterrupt:
                print("\nStopped watching.")
            finally:
                if ap_client is not None:
                    ap_client.close()
    elif len(sys.argv) > 1 and sys.argv[1] == "watch_item_rewards":
        # python dungeons_reader.py watch_item_rewards --ap-host HOST --slot SLOT [--ap-port PORT] [--game NAME] [--password PASS] [--loading-buffer SECONDS]
        #
        # Receives every item ever sent to this slot and applies the ones
        # that are real equipment rewards (Random Melee Weapon / Random
        # Ranged Weapon / Random Armor / Random Item - see
        # apply_item_reward.py) via give_item.py's give_random_item(),
        # which already has its own safety machinery built in: inventory-
        # capacity checking (check_inventory_room), the PowerDropDetected
        # tripwire, and zone-transition read retries
        # (_wait_for_zone_transition).
        #
        # NOT handled yet: emerald filler items (100/300/500 Emeralds).
        # There's no confirmed way to WRITE the player's currency total,
        # only read it (see get_currency_pointer/read_currency_values) -
        # those get logged as "not yet appliable" and left unmarked so a
        # future version of this mode can pick them up once a currency
        # writer exists, without needing a save-format migration. Mission/
        # Secret/Ancient-Hunt Access items are also skipped here - that's
        # watch_level_lock's job, not this mode's.
        #
        # Resolving a received item_id back to its name requires the AP
        # world's own ITEM_TABLE (item IDs are allocated dynamically at
        # generation time via Items.py's _alloc_id() counter - NOT stable
        # to hardcode here). This imports worlds.mcdungeons.Items directly,
        # which only works if the Archipelago server checkout (with this
        # world's folder) is importable from wherever this script runs -
        # if it's not, this mode refuses to start rather than silently
        # doing nothing useful.
        #
        # Transition safety follows the same proven pattern as
        # stress_test_give_item.py: polls item_stash's address once a
        # second, and after any detected change waits --loading-buffer
        # seconds (default 10, matching observed real loading-screen
        # duration) before resuming. Rewards that arrive mid-transition
        # are queued and applied once stable, never dropped - each queued
        # reward's absolute index is only marked "applied" (and persisted
        # to APPLIED_REWARDS_FILE) after apply_item_reward actually
        # succeeds, so a failure (e.g. inventory still full) leaves it
        # queued for the next tick instead of losing it.
        def _get_flag(name, default=None):
            if name in sys.argv:
                idx = sys.argv.index(name)
                if idx + 1 < len(sys.argv):
                    return sys.argv[idx + 1]
            return default

        ap_host = _get_flag("--ap-host")
        ap_slot = _get_flag("--slot")
        if not ap_host or not ap_slot:
            print("Needs a real connection - pass --ap-host <host> --slot <name>.")
        else:
            ap_port = int(_get_flag("--ap-port", "38281"))
            ap_game = _get_flag("--game", "Minecraft Dungeons")
            ap_password = _get_flag("--password", "")
            loading_buffer = float(_get_flag("--loading-buffer", "10"))

            try:
                from worlds.mcdungeons.Items import ITEM_TABLE as _AP_ITEM_TABLE
            except ImportError:
                _AP_ITEM_TABLE = None

            if _AP_ITEM_TABLE is None:
                print("Could not import worlds.mcdungeons.Items - this mode needs the AP "
                      "world package importable (run from an environment that has the "
                      "Archipelago server checkout on its path) to resolve received item "
                      "IDs back to names.")
            else:
                item_id_to_name = {info.code: name for name, info in _AP_ITEM_TABLE.items()}

                from ap_client import ArchipelagoClient
                from apply_item_reward import is_item_reward, apply_item_reward
                import win32file
                # (pipe name computed via this file's own _pipe_name_for(pm) below -
                # identical formula to give_item.py's copy, no need to import it)

                applied_indices = load_applied_reward_indices()
                print(f"{len(applied_indices)} rewards already applied in a previous session.")

                ap_client = ArchipelagoClient(ap_host, ap_port, ap_slot, ap_game, ap_password)
                try:
                    ap_client.connect()
                    print(f"Connected to Archipelago as '{ap_slot}' ({ap_game}) at {ap_host}:{ap_port}.")
                except Exception as e:
                    print(f"Could not connect to Archipelago: {e}")
                    ap_client = None

                if ap_client is not None:
                    pipe = win32file.CreateFile(
                        _pipe_name_for(pm), win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                        0, None, win32file.OPEN_EXISTING, 0, None
                    )

                    pending = []  # [(item_name, absolute_index), ...] not yet applied
                    last_address = None

                    print("Watching for item rewards (Ctrl+C to stop)...\n")
                    try:
                        while True:
                            time.sleep(1.0)

                            received = ap_client.poll_received_items()
                            dirty = False
                            for item_id, absolute_index in received:
                                if absolute_index in applied_indices:
                                    continue
                                item_name = item_id_to_name.get(item_id)
                                if item_name is None:
                                    print(f"  [?] Unknown item_id {item_id} (index {absolute_index}) - "
                                          f"skipping, can't resolve to a name.")
                                    applied_indices.add(absolute_index)
                                    dirty = True
                                    continue
                                if is_item_reward(item_name):
                                    pending.append((item_name, absolute_index))
                                else:
                                    # Emerald filler or a progression item - not this mode's job.
                                    # Left unmarked on purpose for emerald fillers (see banner
                                    # comment above) so a future currency-writer version can
                                    # still pick these up; progression items need no action ever.
                                    if item_name not in ("100 Emeralds", "300 Emeralds", "500 Emeralds"):
                                        applied_indices.add(absolute_index)
                                        dirty = True
                            if dirty:
                                save_applied_reward_indices(applied_indices)

                            if not pending:
                                continue

                            item_stash, error = get_item_stash_component(pm, base)
                            current_address = item_stash if item_stash else None

                            if last_address is not None and current_address != last_address:
                                print(f"  Transition detected (ItemStashComponent address changed) - "
                                      f"waiting {loading_buffer}s before resuming...")
                                time.sleep(loading_buffer)
                                last_address = None
                                continue
                            last_address = current_address

                            if not current_address:
                                continue

                            item_stash_class = pm.read_longlong(item_stash + 0x10)
                            still_pending = []
                            for item_name, absolute_index in pending:
                                try:
                                    item_name_index, granted_name, power = apply_item_reward(
                                        pm, pipe, item_stash, item_stash_class, item_name
                                    )
                                    print(f"  [REWARD] {item_name} -> granted {granted_name} (power={power:.1f})")
                                    applied_indices.add(absolute_index)
                                except Exception as e:
                                    print(f"  [REWARD] {item_name} (index {absolute_index}) failed: {e} - will retry.")
                                    still_pending.append((item_name, absolute_index))
                            pending = still_pending
                            save_applied_reward_indices(applied_indices)
                    except KeyboardInterrupt:
                        print("\nStopped.")
                    finally:
                        win32file.CloseHandle(pipe)
                        ap_client.close()
    elif len(sys.argv) > 1 and sys.argv[1] == "watch_deathlink":
        # python dungeons_reader.py watch_deathlink --ap-host HOST --slot SLOT [--ap-port PORT] [--game NAME] [--password PASS] [--force]
        # Sends a DeathLink Bounce whenever the local player's real GAS
        # Health drops to (near) 0 (see identify_health_attribute_set) -
        # far more reliable than the old totem-lost-widget detection,
        # since Health is read straight from a stable, persistent object
        # every tick rather than diffing GObjects for a transient widget
        # class that turned out prone to false positives (slot reuse,
        # racy reads during loading screens, etc).
        #
        # On an incoming DeathLink Bounce from another player, calls
        # kill_local_player (the real Kill() call, or the Health-write
        # fallback if that's not confirmed).
        #
        # Respects the yaml's Death Link option: after connecting, this
        # reads slot_data.death_link and refuses to proceed if it's off,
        # unless --force is passed - so Death Link is genuinely "optional,
        # toggled in the yaml" rather than something this script always
        # does regardless of the generated seed's settings.
        if True:
            # NOTE: outgoing death detection now reads UHealthComponent
            # directly (OFFSETS["player_health_component"] /
            # "health_component_health" - fixed, native offsets confirmed
            # via the Dumper-7 dump), so a confirmed HEALTH_ATTRIBUTE_SET_CLASS
            # is no longer required to start watch_deathlink. It's only used
            # as a secondary fallback if the HealthComponent chain fails.
            def _get_flag(name, default=None):
                if name in sys.argv:
                    idx = sys.argv.index(name)
                    if idx + 1 < len(sys.argv):
                        return sys.argv[idx + 1]
                return default

            ap_host = _get_flag("--ap-host")
            ap_slot = _get_flag("--slot")
            if not ap_host or not ap_slot:
                print("DeathLink needs a real connection - pass --ap-host <host> --slot <name>.")
            else:
                ap_port = int(_get_flag("--ap-port", "38281"))
                ap_game = _get_flag("--game", "Minecraft Dungeons")
                ap_password = _get_flag("--password", "")

                from ap_client import ArchipelagoClient
                ap_client = ArchipelagoClient(ap_host, ap_port, ap_slot, ap_game, ap_password,
                                               tags=["DeathLink"])
                try:
                    ap_client.connect()
                    print(f"Connected to Archipelago as '{ap_slot}' ({ap_game}) at {ap_host}:{ap_port}.")
                except Exception as e:
                    print(f"Could not connect to Archipelago: {e}")
                    ap_client = None

                if ap_client is not None:
                    # Respect the yaml's death_link option by default - if slot_data
                    # has it explicitly off, don't proceed unless --force overrides
                    # it (e.g. for testing outside a real generated seed). Missing/
                    # absent slot_data (older server, or field just not present)
                    # defaults to allowed, so this doesn't break anything that
                    # predates the death_link yaml option existing.
                    death_link_enabled = ap_client.slot_data.get("death_link", True)
                    force = "--force" in sys.argv
                    if not death_link_enabled and not force:
                        print("This slot's yaml has Death Link OFF (death_link=false in slot_data) - "
                              "not sending/receiving DeathLink. Pass --force to override.")
                        ap_client.close()
                        ap_client = None
                    else:
                        if not death_link_enabled and force:
                            print("Warning: yaml has Death Link off, but --force was passed - "
                                  "proceeding anyway.")
                        print("Tagged for DeathLink.")

                if ap_client is not None:
                    debug = "--debug" in sys.argv
                    print("Watching for totem loss / incoming DeathLink (Ctrl+C to stop)...\n")
                    try:
                        _pawn_check, _pawn_err = get_pawn(pm, base)
                        _hc_check = pm.read_longlong(_pawn_check + OFFSETS["player_health_component"]) if _pawn_check else None
                    except Exception as _e:
                        _pawn_err = str(_e)
                        _hc_check = None
                    if not _hc_check and HEALTH_ATTRIBUTE_SET_CLASS is None:
                        print("Warning: could not read UHealthComponent AND no confirmed "
                              "HEALTH_ATTRIBUTE_SET_CLASS - outgoing death detection won't "
                              "work at all until you're in a level with a spawned pawn, or "
                              "run `identify_health_attribute_set`.\n")
                        if debug:
                            print(f"  [debug] get_pawn error: {_pawn_err}\n")
                    last_health = None
                    last_max_health = None
                    last_death_link_sent = 0.0
                    last_debug_print = 0.0
                    DEATH_LINK_COOLDOWN = 10.0  # seconds - a real death can't repeat faster
                                                 # than this; guards against any residual noise
                    try:
                        while True:
                            time.sleep(0.15)  # fast enough to have a real shot at sampling the
                                               # brief near-zero dip before a totem auto-revive -
                                               # 0.5s turned out too slow and missed all 3 deaths

                            # --- outgoing: did WE die? ---
                            # Watches the real GAS Health value directly (same chain
                            # kill_local_player/watch_health use) instead of diffing
                            # GObjects for a transient widget class - HealthAttributeSet
                            # is a STABLE, persistent object (found via the pawn's own
                            # SpawnedAttributes array each tick, not by guessing which
                            # GObjects slot got reused), so there's no slot-reuse false
                            # positive risk like the widget-class approach had.
                            pawn, pawn_err = get_pawn(pm, base)
                            health, max_health = None, None
                            _dbg_hc_err = None
                            _dbg_gas_err = None
                            _dbg_health_component = None
                            if pawn:
                                # Primary: UHealthComponent via a fixed, native
                                # offset chain (pawn -> HealthComponent pointer
                                # -> Health/MaxHealth floats). Confirmed against
                                # the Dumper-7 dump of ABP_PlayerCharacter_C /
                                # UHealthComponent - these are compiled-in
                                # offsets on native/BP classes, not a
                                # class_name_index, so unlike the GAS path below
                                # they DON'T go stale across a game restart.
                                try:
                                    health_component = pm.read_longlong(pawn + OFFSETS["player_health_component"])
                                    _dbg_health_component = health_component
                                    if health_component:
                                        health = pm.read_float(health_component + OFFSETS["health_component_health"])
                                        max_health = pm.read_float(health_component + OFFSETS["health_component_max_health"])
                                    else:
                                        _dbg_hc_err = "health_component pointer read as 0/NULL"
                                except Exception as _e:
                                    health = None
                                    _dbg_hc_err = str(_e)

                                # Fallback: old GAS/SpawnedAttributes path, only
                                # if the HealthComponent chain above failed AND
                                # a HEALTH_ATTRIBUTE_SET_CLASS was confirmed
                                # this session via identify_health_attribute_set.
                                if health is None and HEALTH_ATTRIBUTE_SET_CLASS is not None:
                                    entries = get_spawned_attributes(pm, pawn)
                                    attr_set = next((p for p, c in entries if c == HEALTH_ATTRIBUTE_SET_CLASS), None)
                                    if attr_set:
                                        try:
                                            health = pm.read_float(attr_set + OFFSETS["health_attr_health"])
                                            max_health = pm.read_float(attr_set + OFFSETS["health_attr_max_health"])
                                        except Exception as _e:
                                            health = None
                                            _dbg_gas_err = str(_e)
                                    else:
                                        _dbg_gas_err = "no matching attr_set found in SpawnedAttributes"

                            if debug:
                                now_dbg = time.time()
                                if now_dbg - last_debug_print > 1.0:  # throttle to 1/sec, not every 0.15s poll
                                    last_debug_print = now_dbg
                                    print(f"  [debug] pawn={hex(pawn) if pawn else None} "
                                          f"({pawn_err or 'ok'}) health_component={hex(_dbg_health_component) if _dbg_health_component else _dbg_health_component} "
                                          f"health={health} max_health={max_health} "
                                          f"hc_err={_dbg_hc_err} gas_err={_dbg_gas_err} "
                                          f"HEALTH_ATTRIBUTE_SET_CLASS={HEALTH_ATTRIBUTE_SET_CLASS}")

                            died = False
                            reason = ""
                            if health is not None and last_health is not None and max_health:
                                near_zero = max(1.0, max_health * 0.02)  # relative, not a fixed
                                                                          # 1.0 - max health scales
                                                                          # per-character (armor
                                                                          # etc), so an absolute
                                                                          # threshold undershoots
                                if last_health > near_zero and 0.0 <= health <= near_zero:
                                    died = True
                                    reason = f"Health {last_health:.1f} -> {health:.1f} (near zero)"
                                elif (last_max_health and last_health < last_max_health * 0.10
                                      and health > max_health * 0.60
                                      and (health - last_health) > max_health * 0.40):
                                    # Backup signal: even at 0.15s polling, a totem's instant
                                    # full-heal can still land between two samples and skip the
                                    # near-zero one entirely - but a jump from "was low" straight
                                    # to "mostly/fully healed" in one tick is exactly what a
                                    # totem revive looks like, so treat it as a death too.
                                    died = True
                                    reason = f"Health {last_health:.1f} -> {health:.1f} (sudden revive-sized jump)"

                            if died:
                                now = time.time()
                                if now - last_death_link_sent < DEATH_LINK_COOLDOWN:
                                    print(f"  [DIED] detected ({reason}), but still in cooldown "
                                          f"({DEATH_LINK_COOLDOWN - (now - last_death_link_sent):.1f}s left) "
                                          f"- not re-sending.")
                                else:
                                    print(f"  [DIED] {reason} - sending DeathLink...")
                                    try:
                                        ap_client.send_death_link()
                                        print(f"    -> DeathLink sent.")
                                        last_death_link_sent = now
                                    except Exception as e:
                                            print(f"    -> failed to send DeathLink: {e}")
                            if health is not None:
                                last_health = health
                                last_max_health = max_health

                            # --- incoming: did anyone ELSE die? ---
                            # poll_received_items() is the ONLY method draining the
                            # socket in the new ap_client.py (it also delivers received
                            # items) - it populates last_deathlinks as a side effect
                            # (self-echoed Bounces already filtered out there), so call
                            # it even though we don't care about the items list itself
                            # here. Calling a second, separate poll would race with
                            # this one and could steal packets meant for it.
                            try:
                                ap_client.poll_received_items()
                                for data in ap_client.last_deathlinks:
                                    source = data.get("source", "someone")
                                    cause = data.get("cause", "")
                                    print(f"  [DEATHLINK RECEIVED] {source}: {cause}")
                                    success, kill_error, diag = kill_local_player(pm, base)
                                    if success:
                                        if diag.get("chain") == "Kill()":
                                            print(f"    -> called confirmed Kill() - character should be dead.")
                                        else:
                                            print(f"    -> wrote Health=0 (before={diag.get('health_before')}, "
                                                  f"after={diag.get('health_after')}) via {diag.get('chain')}. "
                                                  f"Run `find_kill_function` to confirm the real Kill() call "
                                                  f"instead of this fallback, if it isn't reliably working.")
                                    else:
                                        print(f"    -> could not kill local player: {kill_error}")
                            except Exception:
                                pass
                    except KeyboardInterrupt:
                        print("\nStopped watching.")
                    finally:
                        ap_client.close()
    elif len(sys.argv) > 1 and sys.argv[1] == "find_kill_function":
        # python dungeons_reader.py find_kill_function
        # Structurally locates zero-parameter Native+Final+Public UFunctions
        # anywhere in the local pawn's class hierarchy (see
        # find_zero_param_native_final_functions) - candidates for
        # ABaseCharacter::Kill(), found WITHOUT name resolution since we
        # don't have GNames, just by matching its documented shape
        # (Final, Native, Public, no params). Lets you test each one live,
        # watching GAS Health before/after so you see immediately whether
        # it actually killed the character.
        #
        # DANGER: this actually calls whatever function you pick, for
        # real, inside the game. A wrong pick could do something
        # unexpected (though the Size==0/flags filter should keep the
        # candidate list small and low-risk - these are all simple
        # zero-argument native calls, not arbitrary functions).
        pawn, error = get_pawn(pm, base)
        if not pawn:
            print(f"Could not reach Pawn: {error}")
        else:
            pawn_class = pm.read_longlong(pawn + OFFSETS["uobject_class"])
            if not pawn_class:
                print("Could not read pawn's class pointer.")
            else:
                candidates = find_zero_param_native_final_functions(pm, pawn_class)
                if not candidates:
                    print("No matching candidates found - check the reflection offsets "
                          "(uobject_class/ustruct_super/ustruct_children/ufield_next) "
                          "are still right for this build.")
                else:
                    print(f"{len(candidates)} candidate(s) found (Size=0, has Final|Native|Public|Const):\n")
                    for i, (func_addr, level_class, depth) in enumerate(candidates):
                        print(f"  [{i}] function={hex(func_addr)}  owning_class={hex(level_class)}  "
                              f"super_chain_depth={depth}")

                    pick = input("\nIndex to test (blank to skip): ").strip()
                    if pick:
                        try:
                            idx = int(pick)
                            func_addr, level_class, depth = candidates[idx]
                        except (ValueError, IndexError):
                            print("Invalid index.")
                            func_addr = None

                        if func_addr:
                            # Prefer the stable UHealthComponent chain; fall back
                            # to GAS/SpawnedAttributes only if it's unavailable.
                            attr_set = None
                            health_component = None
                            try:
                                health_component = pm.read_longlong(pawn + OFFSETS["player_health_component"])
                            except Exception:
                                health_component = None
                            entries = get_spawned_attributes(pm, pawn)

                            def _read_health():
                                if health_component:
                                    try:
                                        return pm.read_float(health_component + OFFSETS["health_component_health"])
                                    except Exception:
                                        pass
                                if HEALTH_ATTRIBUTE_SET_CLASS is not None:
                                    a = next((p for p, c in entries if c == HEALTH_ATTRIBUTE_SET_CLASS), None)
                                    if a:
                                        try:
                                            return pm.read_float(a + OFFSETS["health_attr_health"])
                                        except Exception:
                                            pass
                                return None

                            health_before = _read_health()
                            print(f"Health before: {health_before}")

                            confirm = input(f"About to call candidate [{idx}] for real - type 'yes' to confirm: ").strip()
                            if confirm.lower() == "yes":
                                success, call_error = call_ufunction_no_params(pm, pawn, func_addr)
                                if not success:
                                    print(f"Call failed: {call_error}")
                                else:
                                    time.sleep(0.3)
                                    health_after = _read_health()
                                    print(f"Call succeeded. Health after: {health_after}")
                                    print("Check in-game whether your character actually died.")
                                    died = input("Did the character actually die in-game? (y/n): ").strip().lower()
                                    if died == "y":
                                        _save_kill_function_depth(depth)
                                        print(f"Saved super_chain_depth={depth} to {KILL_FUNCTION_DEPTH_FILE} - "
                                              f"kill_local_player() will use this Kill() call from now on "
                                              f"instead of the Health-write fallback.")
                            else:
                                print("Cancelled.")
    elif len(sys.argv) > 1 and sys.argv[1] == "scan_identity":
        # python dungeons_reader.py scan_identity
        # Grouped memory diff - looks for whatever offset actually drives
        # the in-game icon/name, since we've confirmed SerializedId isn't it.
        item_stash, error = get_item_stash_component(pm, base)
        if not item_stash:
            print(f"Could not reach ItemStashComponent: {error}")
        else:
            scan_identity_field(pm, item_stash)
    elif len(sys.argv) > 1 and sys.argv[1] == "watch_deaths":
        # python dungeons_reader.py watch_deaths
        #
        # Live, unfiltered print of every character death (dungeons_bridge.dll
        # required) with its resolved class - run this WHILE fighting a
        # boss, the true boss class is whichever line prints right before
        # the victory screen. No guessing, driven by the game's own real
        # death events.
        watch_deaths(pm)

    elif len(sys.argv) > 1 and sys.argv[1] == "dump_actors":
        # python dungeons_reader.py dump_actors
        #
        # One-shot, no interaction: prints every plausible class_name_index
        # in the CURRENT scan with its count, lone-instance (count=1, most
        # likely to include a boss) first, and flags whether each is
        # already known (chest/enemy/boss) or unlabeled. Meant to be run
        # while actually looking at the boss on screen, then the raw
        # output pasted back for analysis - much less friction than the
        # interactive prompts in a fast-moving fight.
        world = pm.read_longlong(base + OFFSETS["gworld"])
        if not world:
            print("No UWorld - are you in a level?")
        else:
            snapshot = scan_full_zone(pm, world)
            counts = {}
            for cls in snapshot.values():
                if isinstance(cls, int) and 0 <= cls <= 10_000_000:
                    counts[cls] = counts.get(cls, 0) + 1
            print(f"{len(counts)} distinct plausible class(es), {len(snapshot)} actors total.\n")
            for cls, n in sorted(counts.items(), key=lambda kv: kv[1]):
                if cls in BOSS_CLASS_LOOKUP:
                    tag = f"BOSS:{BOSS_CLASS_LOOKUP[cls]}"
                elif cls in ENEMY_CLASS_LOOKUP:
                    tag = f"enemy:{ENEMY_CLASS_LOOKUP[cls]}"
                else:
                    tag = "unlabeled"
                print(f"class_name_index={cls:<10} count={n:<4} {tag}")

    elif len(sys.argv) > 1 and sys.argv[1] == "document_bosses":
        # python dungeons_reader.py document_bosses
        #
        # Labeling-only workflow for BOSS_CLASS_LOOKUP - no AP connection,
        # sends no checks. Run this in a mission, play normally, and
        # you'll only be prompted when a genuinely new actor class shows
        # up (a lone-instance one is flagged as a likely boss candidate).
        # Progress (X/12 confirmed) prints after every label. Ctrl+C any
        # time - everything saves as you go. Repeat across missions until
        # all 12 names in ap_world/Locations.py's BOSS_NAMES are covered.
        document_bosses(pm, base)

    elif len(sys.argv) > 1 and sys.argv[1] == "watch_boss_kills":
        # python dungeons_reader.py watch_boss_kills <host> <port> <slot_name> <game_name> [password]
        # e.g. python dungeons_reader.py watch_boss_kills localhost 38281 MyName "Minecraft Dungeons"
        #
        # Per-mission watcher: sends a "<Boss> - First Kill" Archipelago
        # check the first time a boss labeled in BOSS_CLASS_LOOKUP (see
        # document_bosses above) disappears from the zone. Run
        # document_bosses first if BOSS_CLASS_LOOKUP is still empty -
        # nothing gets detected for an unlabeled boss class. Claimed
        # kills persist to boss_kills_claimed.json, so nothing's resent
        # across restarts.
        #
        # pip install websocket-client   (needed for ap_client.py)
        if len(sys.argv) < 5:
            print("Usage: python dungeons_reader.py watch_boss_kills <host> <port> "
                  "<slot_name> <game_name> [password]")
        else:
            host = sys.argv[2]
            port = int(sys.argv[3])
            slot_name = sys.argv[4]
            game_name = sys.argv[5]
            password = sys.argv[6] if len(sys.argv) > 6 else ""
            watch_boss_kills(pm, base, host, port, slot_name, game_name, password)

    elif len(sys.argv) > 1 and sys.argv[1] == "reset_mission_progress_baseline":
        # python dungeons_reader.py reset_mission_progress_baseline
        # python dungeons_reader.py reset_mission_progress_baseline --clear
        # Run this ONCE right before starting a brand new Archipelago seed
        # with an existing, already-progressed hero: it snapshots which
        # base-game missions currently read as completed and excludes them
        # from ever auto-firing a check for watch_mission_end - only
        # missions that flip to completed AFTER this point (i.e. actually
        # played during THIS seed) will send a check. Doesn't touch the
        # actual game save - purely a local bookkeeping file.
        # --clear removes the baseline entirely, going back to the default
        # "auto-grant anything already completed" behavior (the right
        # choice for a genuinely fresh hero, where there's nothing to
        # exclude anyway).
        if "--clear" in sys.argv:
            if os.path.exists(MISSION_PROGRESS_BASELINE_FILE):
                os.remove(MISSION_PROGRESS_BASELINE_FILE)
            print("Baseline cleared - watch_mission_end will auto-grant checks for any "
                  "already-completed mission again (default behavior).")
        elif IS_MISSION_COMPLETED_INDEX is None:
            print(f"No confirmed index in {IS_MISSION_COMPLETED_INDEX_FILE} yet - run "
                  f"`test_is_mission_completed` then `confirm_is_mission_completed` first.")
        else:
            baseline = set()
            for zone_name in MISSION_LOCATION_IDS:
                if get_elevelname_value(zone_name) is None:
                    continue
                completed = call_is_mission_completed(pm, base, zone_name)
                if completed is True:
                    baseline.add(zone_name)
            _save_mission_progress_baseline(baseline)
            print(f"Baseline saved: {len(baseline)} already-completed mission(s) will be "
                  f"excluded from auto-granting in watch_mission_end from now on:")
            for z in sorted(baseline):
                print(f"  {z}")
            if not baseline:
                print("  (none - this hero has no completed base-game missions yet)")
    elif len(sys.argv) > 1 and sys.argv[1] == "confirm_is_mission_completed":
        # python dungeons_reader.py confirm_is_mission_completed <index>
        # Saves the candidate index (from test_is_mission_completed's
        # batch listing) as the confirmed real IsMissionCompleted, so
        # watch_mission_end (and anything else) can call it going forward.
        if len(sys.argv) < 3:
            print("Usage: confirm_is_mission_completed <index>  (e.g. 1)")
        else:
            try:
                idx = int(sys.argv[2])
                _save_is_mission_completed_index(idx)
                print(f"Saved index={idx} to {IS_MISSION_COMPLETED_INDEX_FILE} - "
                      f"permanent from now on.")
            except ValueError:
                print(f"'{sys.argv[2]}' isn't a valid integer.")
    elif len(sys.argv) > 1 and sys.argv[1] == "test_is_mission_completed":
        # python dungeons_reader.py test_is_mission_completed <zone_name> [index]
        # Calls UMissionProgressComponent::IsMissionCompleted(ELevelNames)
        # for real, for the given zone, and prints the raw result bytes.
        # Without an index, tests EVERY candidate whose param_struct_size==2
        # (the expected size: 1-byte levelName input + 1-byte bool
        # ReturnValue) in one batch, instead of manually re-running per
        # index - much faster than testing 13+ candidates one at a time.
        # Test with a zone you KNOW you've completed (should read True-ish
        # at offset 1) and one you know you HAVEN'T (should read False-ish)
        # to confirm both the right candidate function and that offset 1
        # really is ReturnValue - this is all empirical since we don't
        # have the exact params struct dumped, only the function signature.
        if len(sys.argv) < 3:
            print("Usage: test_is_mission_completed <zone_name> [candidate_index]")
        else:
            zone_name = sys.argv[2]
            elevelname_value = get_elevelname_value(zone_name)
            if elevelname_value is None:
                print(f"'{zone_name}' has no known ELevelNames mapping - check ELEVELNAMES/"
                      f"ZONE_NAME_TO_ELEVELNAME_KEY.")
            else:
                pawn, error = get_pawn(pm, base)
                if not pawn:
                    print(f"Could not reach Pawn: {error}")
                else:
                    component = pm.read_longlong(pawn + OFFSETS["mission_progress_component"])
                    if not component:
                        print("Pawn's MissionProgressComponent pointer is null.")
                    else:
                        component_class = pm.read_longlong(component + OFFSETS["uobject_class"])
                        candidates = find_functions_on_class(pm, component_class, min_size=1, max_size=16)
                        if not candidates:
                            print("No matching candidate functions found on MissionProgressComponent's "
                                  "class - check the reflection offsets are still valid.")
                        else:
                            explicit_idx = None
                            if len(sys.argv) >= 4:
                                try:
                                    explicit_idx = int(sys.argv[3])
                                except ValueError:
                                    print(f"'{sys.argv[3]}' isn't a valid index - ignoring, batch-testing instead.")

                            if explicit_idx is not None:
                                to_test = [(explicit_idx, candidates[explicit_idx])]
                            else:
                                to_test = [(i, c) for i, c in enumerate(candidates) if c[1] == 2]
                                print(f"Batch-testing all {len(to_test)} candidate(s) with "
                                      f"param_struct_size==2 for '{zone_name}' (ELevelNames="
                                      f"{elevelname_value})...\n")

                            for idx, (func_addr, size) in to_test:
                                params_bytes = bytes([elevelname_value]) + bytes(max(size, 8) - 1)
                                success, call_error, params_after = call_process_event(
                                    pm, component, func_addr, params_bytes)
                                if not success:
                                    print(f"  [{idx}] {hex(func_addr)}  call failed: {call_error}")
                                else:
                                    b1 = params_after[1] if len(params_after) > 1 else None
                                    flag = "  <-- ReturnValue looks like a clean bool" if b1 in (0, 1) else ""
                                    print(f"  [{idx}] {hex(func_addr)}  raw={params_after.hex()}  "
                                          f"offset1={b1}{flag}")

                            print("\nRe-run with the same zone (known completed) vs. a zone you "
                                  "HAVEN'T completed, same candidate index via the 3rd argument "
                                  "(e.g. `test_is_mission_completed obsidianpinnacle 3`) - the "
                                  "candidate whose offset1 flips 1->0 between the two is the real "
                                  "IsMissionCompleted.")
    else:
        item_stash, error = get_item_stash_component(pm, base)
        if not item_stash:
            print(f"Could not reach ItemStashComponent: {error}")
        else:
            items = read_inventory(pm, item_stash)
            print(f"{len(items)} item(s) in inventory:\n")
            for item in items:
                print(f"[{item['slot']}] {item['name']:20s} power={item['power']:.1f}  {item['rarity']}")

            unknown_indices = {item["name_index"] for item in items if item["name_index"] not in NAME_LOOKUP}
            if unknown_indices:
                print(f"\n{len(unknown_indices)} item index(es) not in item_lookup.py's ITEM_TABLE: "
                      f"{sorted(unknown_indices)}. Add them to all_items.csv if confirmed.")