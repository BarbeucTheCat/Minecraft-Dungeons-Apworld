"""Eye of Ender checks (seed option eye_of_ender_checks) - everything the CLIENT does for them, in one place.

The six Eyes of Ender the mini bosses drop are turned into checks (Locations.EYE_OF_ENDER_TABLE):
  1. scan_eye_of_ender_drops() / eye_of_ender_tick(): finds the Eye objects in the level and sends the check
     of every type seen (rides on the powerful-mob tracker's level scan when that is running);
  2. push_eye_block() -> set_eye_block(): tells dungeons_bridge.dll to swallow the pickup of the real Eye
     (set_block_eyes 1/0 - the matching C++ is the "Eye of Ender pickup blocking" section of dungeons_bridge.cpp);
  3. grant_pending_eyes() -> apply_eye_of_ender_reward(): every "Eye of Ender" ITEM received from the
     multiworld becomes one real Eye in the wallet (WalletComponent::ClientAdd, like emeralds).
The apworld side lives in Items.py / Locations.py / Options.py / Regions.py / Rules.py / __init__.py.
"""
import asyncio
import logging
import time

from _apworld_data import Locations as _apw, Items as _apw_items
from crash_diag import journaled
from dungeons_reader import (
    OFFSETS, resolve_fname, walk_super_chain, scan_full_zone_shared,
    _connect_bridge_pipe, apply_currency_reward, EYES_OF_ENDER_CURRENCY_ITEM_ID,
)

game_logger = logging.getLogger("MCDungeonsGame")

EYE_OF_ENDER_ITEM_NAME = _apw_items.EYE_OF_ENDER_ITEM  # "Eye of Ender"


# ------------------------------------------------------------------ detection (reads the game)
# AEyeOfEnderInstance (Dumper-7 SDK, derives AItemInstance): eyeOfEnderType (EEyeOfEnderType, uint8) @0x4E8.
# 1 DEEPWOOD_BROOK, 2 OLD_TOWN_HALL, 3 SUNKEN_WATCHTOWER, 4 THE_UNDERCROFT, 5 THE_TRIAL_OF_THE_NAMELESS,
# 6 HIGHBLOCK_HIDEAWAY (0 = NOT_SET). The mini bosses drop these; see Locations.EYE_OF_ENDER_TYPES.
_EYE_OF_ENDER_TYPE_OFFSET = 0x4E8
_eye_class_cache = {}   # class name index -> True/False: "is an AEyeOfEnderInstance subclass"


_eye_logged_actors = set()   # actor addresses already described in the log (one diagnostic line per Eye object)


def eye_type_for_zone(zone):
    """EEyeOfEnderType (1..6) whose check lives in this zone (Locations.EYE_OF_ENDER_ZONE), or None."""
    for eye_type, eye_zone in _apw.EYE_OF_ENDER_ZONE.items():
        if eye_zone == zone:
            return eye_type
    return None


def scan_eye_of_ender_drops(pm, base, zone=None):
    """EEyeOfEnderType values (1..6) of the Eye of Ender objects present in the level right now (read-only).
    An object counts when its inheritance chain really contains EyeOfEnderInstance, or when its class is
    literally BP_EyeOfEnderInstance* (safety net if the chain cannot be read). If the type byte is not 1..6
    the Eye of the CURRENT zone is assumed (each zone's mini boss drops its own Eye, see
    Locations.EYE_OF_ENDER_TYPES). Every Eye object is described once in the log, so a failure is visible.
    Returns a set; empty in the hub / when nothing is there."""
    world = pm.read_longlong(base + OFFSETS["gworld"])
    if not world:
        return set()
    snapshot = scan_full_zone_shared(pm, world, max_age=2.0)   # usually the tracker's snapshot of a moment ago
    found = set()
    for actor, class_idx in (snapshot or {}).items():
        name = resolve_fname(pm, class_idx) or ""
        if not name or name.startswith("ERROR"):
            continue                          # unresolved right now: try again next scan
        by_name = name.startswith("BP_EyeOfEnderInstance")
        is_eye = _eye_class_cache.get(class_idx)
        if is_eye is None:
            is_eye = False
            cls = pm.read_longlong(actor + OFFSETS["uobject_class"])   # decided by the inheritance chain first
            for c in walk_super_chain(pm, cls):
                if resolve_fname(pm, pm.read_int(c + OFFSETS["uobject_name"])) == "EyeOfEnderInstance":
                    is_eye = True
                    break
            if is_eye or by_name:
                _eye_class_cache[class_idx] = True
            else:
                _eye_class_cache[class_idx] = False
        if not (is_eye or by_name):
            continue
        try:
            eye_type = pm.read_bytes(actor + _EYE_OF_ENDER_TYPE_OFFSET, 1)[0]
        except Exception:
            eye_type = 0
        used = eye_type if 1 <= eye_type <= 6 else eye_type_for_zone(zone)
        if actor not in _eye_logged_actors:
            _eye_logged_actors.add(actor)
            game_logger.info(f"Eye of Ender object seen: class={name} actor=0x{actor:X} zone={zone} "
                             f"type byte={eye_type} -> using type {used}")
        if used:
            found.add(used)
    return found


# ------------------------------------------------------------------ blocking the real pickup (talks to the DLL)
@journaled
def set_eye_block(pm, enabled):
    """Switches the DLL's Eye of Ender pickup blocking on/off (set_block_eyes 1/0): with the
    eye_of_ender_checks option the real Eye is not picked up any more, its check is sent instead.
    Needs the dungeons_bridge.dll rebuilt from the matching dungeons_bridge.cpp (older DLLs answer
    with an error). Returns (ok, error)."""
    try:
        import win32file
    except ImportError:
        return False, "pywin32 not installed - pip install pywin32"
    try:
        pipe = _connect_bridge_pipe(pm)
    except Exception as e:
        return False, f"couldn't connect to dungeons_bridge.dll: {e}"
    try:
        win32file.WriteFile(pipe, f"set_block_eyes {1 if enabled else 0}".encode())
        _, data = win32file.ReadFile(pipe, 65536)
    finally:
        win32file.CloseHandle(pipe)
    if data.decode() != "OK":
        return False, f"unexpected response (DLL not rebuilt?): {data.decode()}"
    return True, None


# ------------------------------------------------------------------ giving the Eyes received as items
def apply_eye_of_ender_reward(pm, base, amount=1):
    """Adds `amount` Eyes of Ender to the player's wallet through the game's own ClientAdd, exactly like
    an emerald grant but for the Eyes of Ender currency (the "Eye of Ender" items of the
    eye_of_ender_checks option). Returns (success, error)."""
    return apply_currency_reward(pm, base, EYES_OF_ENDER_CURRENCY_ITEM_ID, amount)


# ------------------------------------------------------------------ client loop glue
async def _send_eye_check(ctx, eye_type, how):
    """Sends the check of one Eye type once (10 s anti-repeat, skipped if already checked / not in this seed)."""
    location_id = _apw.get_eye_of_ender_location_id(eye_type)
    if location_id in ctx.checked_locations:
        return
    server_locations = getattr(ctx, "server_locations", None)
    if server_locations and location_id not in server_locations:
        return
    pending = ctx.__dict__.setdefault("eye_sent_at", {})
    if time.time() - pending.get(location_id, 0.0) < 10.0:
        return
    pending[location_id] = time.time()
    game_logger.info(f"Eye of Ender dropped: {_apw.eye_of_ender_location_name(eye_type)} "
                     f"(type {eye_type}, zone {ctx.game_state.get('zone')}, via {how})")
    await ctx.check_locations([location_id])


async def _eye_of_ender_scan(ctx, pm, base):
    """Background part of the Eye of Ender checks. Two independent detectors, both feed _send_eye_check:
      1. the level scan (the Eye object appears when the mini boss dies);
      2. the DLL's blockedEye counter (get_health): every time the player tries to pick an Eye up and the DLL
         swallows it, the Eye of the current zone is sent - works even if the scan never sees the object."""
    try:
        zone = ctx.game_state.get("zone")
        async with ctx.bridge_pipe_lock:
            seen = await asyncio.to_thread(scan_eye_of_ender_drops, pm, base, zone)
        for eye_type in sorted(seen):
            await _send_eye_check(ctx, eye_type, "level scan")

        try:
            from health_watch import get_health
            async with ctx.bridge_pipe_lock:
                metrics = await asyncio.to_thread(get_health, pm)
        except Exception:
            metrics = None
        if metrics and "blockedEye" in metrics:
            blocked = metrics["blockedEye"]
            previous = getattr(ctx, "eye_blocked_count", None)
            ctx.eye_blocked_count = blocked
            if previous is not None and blocked > previous:
                eye_type = eye_type_for_zone(zone)
                if eye_type is None:
                    game_logger.info(f"Eye of Ender pickup blocked in zone {zone}, but no Eye check belongs to it.")
                else:
                    await _send_eye_check(ctx, eye_type, "blocked pickup")
    except Exception as e:
        if time.time() - getattr(ctx, "last_eye_error_log", 0.0) >= 30.0:
            ctx.last_eye_error_log = time.time()
            game_logger.info(f"Eye of Ender scan failed: {e}")
    finally:
        ctx.eye_scan_busy = False


def eye_of_ender_tick(ctx, pm, base):
    """Cheap per-tick part: every ~3 s, and only one at a time, launches the background Eye scan."""
    if getattr(ctx, "eye_scan_busy", False):
        return
    now = time.time()
    tracker = getattr(ctx, "powerful_tracker", None)
    tracker_scan_at = getattr(tracker, "last_scan", 0.0) if tracker is not None else 0.0
    if now - tracker_scan_at < 6.0:
        # The powerful-mob tracker is running (mini boss checks / logging): ride on ITS scan - one walk of
        # the level per ~3 s for both, the Eye scan reads the snapshot the tracker just took.
        if tracker_scan_at <= getattr(ctx, "eye_scanned_tracker_at", 0.0):
            return
        ctx.eye_scanned_tracker_at = tracker_scan_at
    elif now - getattr(ctx, "eye_scan_at", 0.0) < 3.0:
        return                                  # tracker not running: own 3 s timer
    ctx.eye_scan_busy = True
    ctx.eye_scan_at = now
    asyncio.create_task(_eye_of_ender_scan(ctx, pm, base), name="EyeOfEnderScan")


async def push_eye_block(ctx, pm):
    """Keeps the DLL's Eye pickup blocking in line with the seed (best effort: an OLD dungeons_bridge.dll can't
    block, but the checks are sent either way - the Eye object is detected by scanning the level)."""
    want_eye_block = bool(ctx.slot_data.get("eye_of_ender_checks", False))
    if getattr(ctx, "eye_block_applied", False) == want_eye_block:
        return
    async with ctx.bridge_pipe_lock:
        ok, eye_block_error = await asyncio.to_thread(set_eye_block, pm, want_eye_block)
    if ok:
        ctx.eye_block_applied = want_eye_block
        game_logger.info("Eye of Ender pickup blocking " + ("ON (the Eyes are checks now)." if want_eye_block else "OFF."))
    elif not getattr(ctx, "eye_block_warned", False):
        ctx.eye_block_warned = True
        game_logger.info(f"Could not switch Eye of Ender blocking ({eye_block_error}) - rebuild "
                         "dungeons_bridge.dll from the current .cpp. The Eye checks are still "
                         "sent, but you will also pick up the real Eyes.")


async def grant_pending_eyes(ctx, pm, base, save_applied_reward_indices):
    """Gives the queued "Eye of Ender" items (same rules as the emerald grants: a confirmed character, one
    ClientAdd call per item, marked applied only once it succeeded, otherwise retried next tick)."""
    still_pending = []
    for absolute_index, amount in ctx.pending_eye_grants:
        success, err = await asyncio.to_thread(apply_eye_of_ender_reward, pm, base, amount)
        if success:
            ctx.applied_reward_indices.add(absolute_index)
            save_applied_reward_indices(ctx.applied_reward_indices)
            game_logger.info(f"Granted {amount} Eye of Ender (reward #{absolute_index}) via WalletComponent::ClientAdd.")
        else:
            game_logger.info(f"Eye of Ender grant #{absolute_index} not applied this tick: {err} - will retry.")
            still_pending.append((absolute_index, amount))
    ctx.pending_eye_grants = still_pending
