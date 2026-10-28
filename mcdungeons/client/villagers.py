"""Villager checks (seed option villager_checks) - everything the CLIENT does for them, in one place.

Freeing a captured villager in a mission unlocks that merchant in the camp. With the option on:
  1. the DLL reports every merchant unlock the GAME starts (get_merchant_events) and swallows the ones of the five
     captured camp merchants (set_block_merchants 1 <fragments>) - see the "Merchant unlock" section of
     dungeons_bridge.cpp. villager_tick() turns each report into the matching location check (Locations.VILLAGER_TYPES);
  2. every merchant ITEM received from the multiworld unlocks that merchant: grant_pending_villagers() ->
     unlock_merchant() calls UMerchantsUtil::UnlockMerchantForAllLocalPlayers(WorldContext, TSubclassOf<UMerchantDef>)
     through the bridge's CALLDATA (game thread, access violations caught, 5 s timeout, parameter size checked first).
     The DLL lets our own calls through, so the unlock really happens.
The apworld side lives in Items.py / Locations.py / Options.py / Regions.py / __init__.py.

What is NOT confirmed yet (first test in game): which UFunction the captured villager really calls, and the class name of
each merchant definition (it is logged: \"MERCHANT UNLOCK seen\" in dungeons_bridge_debug.log, and printed by /villager
events). A merchant the client cannot match is never blocked, so nothing can get stuck locked by accident.

Devtool (typed in the client):
    /villager                       this help
    /villager list                  the five captured merchants: definition class found, unlocked in the camp or not
    /villager events                the merchant unlocks the client saw this session (and what it did with them)
    /villager unlock <name>         unlock one merchant right now (village, mystery, luxury, giftwrapper, blacksmith)
    /villager block on|off          switch the DLL's blocking by hand (the client puts it back to the seed's value)
"""
import asyncio
import logging
import struct
import time

from _apworld_data import Locations as _apw, Items as _apw_items
import miniboss as mb

game_logger = logging.getLogger("MCDungeonsGame")

VILLAGER_TYPES = list(_apw.VILLAGER_TYPES)                 # [(key, fragment, display name, zone)]
VILLAGER_ITEM_NAMES = set(_apw_items.VILLAGER_ITEM_NAMES)  # item names that unlock a merchant
_KEY_BY_ITEM = {display: key for key, _frag, display, _zone in VILLAGER_TYPES}
_DISPLAY_BY_KEY = {key: display for key, _frag, display, _zone in VILLAGER_TYPES}
_FRAGMENT_BY_KEY = {key: frag for key, frag, _display, _zone in VILLAGER_TYPES}

UNLOCK_FUNCTION = "UnlockMerchantForAllLocalPlayers"      # static, UMerchantsUtil: WorldContext @0, Def @8
UNLOCK_SIZES_OK = (0x10,)
IS_UNLOCKED_FUNCTION = "IsMerchantUnlocked"                # static: WorldContext @0, Def @8, bool ReturnValue @0x10
IS_UNLOCKED_SIZES_OK = (0x18,)
POLL_INTERVAL = 1.5


# ------------------------------------------------------------------ bridge pipe
def _pipe_request(pm, text):
    """One request to dungeons_bridge.dll's pipe. Returns the answer text. Raises on connection failure."""
    import win32file
    R = mb._R()
    pipe = R._connect_bridge_pipe(pm)
    try:
        win32file.WriteFile(pipe, text.encode())
        _, data = win32file.ReadFile(pipe, 65536)
    finally:
        win32file.CloseHandle(pipe)
    return data.decode()


_ctx_ref = None    # set by villager_tick(): the context, to read the seed options


def set_villager_block(pm, enabled):
    """Switches the DLL's merchant unlock blocking on/off (set_block_merchants). On = only the five captured
    merchants (by class-name fragment) are swallowed. Returns (ok, error)."""
    try:
        if enabled:
            # the Piglin Merchant is unlocked by an Ancient Hunt: only swallow it when the seed has hunts, otherwise
            # nothing would ever give it back
            hunts_on = bool((getattr(_ctx_ref, "slot_data", None) or {}).get("ancient_hunts")) if _ctx_ref else True
            fragments = ",".join(_FRAGMENT_BY_KEY[key] for key, *_ in VILLAGER_TYPES
                                 if key != "piglin" or hunts_on)
            answer = _pipe_request(pm, f"set_block_merchants 1 {fragments}")
        else:
            answer = _pipe_request(pm, "set_block_merchants 0")
    except Exception as e:
        return False, f"couldn't talk to dungeons_bridge.dll: {e}"
    if answer != "OK":
        return False, f"unexpected response (DLL not rebuilt?): {answer}"
    return True, None


def get_merchant_events(pm):
    """Drains the DLL's merchant unlock queue: [(def_address, def_class_name, function_name)], or None when the DLL
    does not know the command (not rebuilt) or the pipe is not reachable."""
    try:
        answer = _pipe_request(pm, "get_merchant_events")
    except Exception:
        return None
    if not answer.startswith("EVENTS:"):
        return None
    count, _, rest = answer[len("EVENTS:"):].partition("|")
    if count == "0" or not rest:
        return []
    events = []
    for entry in rest.split("|"):
        parts = entry.split(",")
        if len(parts) >= 2 and parts[0]:
            try:
                events.append((int(parts[0], 16), parts[1], parts[2] if len(parts) > 2 else ""))
            except ValueError:
                continue
    return events


# ------------------------------------------------------------------ which merchant is it
def villager_key_for_def_name(def_name):
    """'village' | 'mystery' | ... for a merchant definition class name, or None (not a captured camp merchant of the
    seed option: Tower merchants...). Same rule as the DLL: name fragment, never a Tower merchant."""
    lower = (def_name or "").lower()
    if "tower" in lower:
        return None
    for key, fragment, _display, _zone in VILLAGER_TYPES:
        if fragment in lower:
            return key
    return None


def _super_chain_names(pm, class_addr):
    R = mb._R()
    names = []
    for cls in R.walk_super_chain(pm, class_addr):
        try:
            names.append(R.resolve_fname(pm, pm.read_int(cls + R.OFFSETS["uobject_name"])) or "?")
        except Exception:
            names.append("?")
    return names


NATIVE_DEF_CLASS = {          # native UMerchantDef subclasses of the Dumper-7 SDK (a Blueprint subclass may exist on top)
    "village": "VillageMerchantDef", "mystery": "MysteryMerchantDef", "luxury": "LuxuryMerchantDef",
    "giftwrapper": "GiftWrapperMerchantDef", "blacksmith": "BlacksmithMerchantDef",
    "enchantsmith": "EnchanterMerchantDef", "piglin": "PiglinMerchantDef",   # last two: guessed names (not found = ignored)
}


def resolve_def_class(pm, base, key, say=None):
    """(class name, class address) of the merchant definition the game uses for `key`, or raises MinibossError.
    Candidates: the loaded classes whose name has the merchant's fragment and that derive from MerchantDef, plus the
    native class itself. When there are several (native class + Blueprint subclass) the most derived one wins - that
    is the one the camp villager actor holds, and the unlocked-merchants map is keyed by the exact class."""
    say = say or (lambda line: None)
    mb._state.update(base=base, say=say)
    fragment = _FRAGMENT_BY_KEY[key]
    candidates = {}
    for name, addr in mb.get_class_index(pm, base, say).items():
        lower = name.lower()
        if fragment in lower and "tower" not in lower:
            candidates[name] = addr
    native_name = NATIVE_DEF_CLASS[key]
    native = mb.find_class(pm, native_name)
    if native:
        candidates.setdefault(native_name, native)
    found = []
    for name, addr in candidates.items():
        try:
            chain = _super_chain_names(pm, addr)
        except Exception:
            continue
        if "MerchantDef" in chain[1:] or name.endswith("MerchantDef"):
            found.append((len(chain), name, addr))
    if not found:
        raise mb.MinibossError(f"no loaded merchant definition class matches '{fragment}' (is a level loaded? "
                               f"try /villager list in the camp)")
    found.sort(reverse=True)
    _depth, name, addr = found[0]
    return name, addr


# ------------------------------------------------------------------ the game calls
def _world_context(pm, base):
    R = mb._R()
    pawn, err = R.get_pawn(pm, base)
    if not pawn:
        raise mb.MinibossError(f"no player character ({err})")
    return pawn


def unlock_merchant(pm, base, key, say=None):
    """Unlocks the camp merchant `key` for the local players through the game's own function. Returns (ok, error)."""
    R = mb._R()
    try:
        if R.bridge_hook_state(pm) == "silent":
            return False, "the game is loading or changing level"
        name, cls = resolve_def_class(pm, base, key, say)
        context = _world_context(pm, base)
        cdo, func, size = mb._find_static_function(pm, "MerchantsUtil", UNLOCK_FUNCTION, UNLOCK_SIZES_OK)
        params = bytearray(size)
        struct.pack_into("<Q", params, 0x00, context)
        struct.pack_into("<Q", params, 0x08, cls)
        mb.calldata(pm, cdo, func, bytes(params))
    except mb.MinibossError as e:
        return False, str(e)
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    game_logger.info(f"Unlocked camp merchant '{_DISPLAY_BY_KEY[key]}' ({name}) via {UNLOCK_FUNCTION}.")
    return True, None


def merchant_is_unlocked(pm, base, key, say=None):
    """True / False from the game's IsMerchantUnlocked, or raises MinibossError."""
    name, cls = resolve_def_class(pm, base, key, say)
    context = _world_context(pm, base)
    cdo, func, size = mb._find_static_function(pm, "MerchantsUtil", IS_UNLOCKED_FUNCTION, IS_UNLOCKED_SIZES_OK)
    params = bytearray(size)
    struct.pack_into("<Q", params, 0x00, context)
    struct.pack_into("<Q", params, 0x08, cls)
    out = mb.calldata(pm, cdo, func, bytes(params))
    return bool(out[0x10]) if len(out) > 0x10 else False


# ------------------------------------------------------------------ client loop glue
async def push_villager_block(ctx, pm):
    """Keeps the DLL's merchant blocking in line with the seed (best effort: an OLD dungeons_bridge.dll can't block
    nor report - the villager checks then never fire, the log says so once)."""
    global _ctx_ref
    _ctx_ref = ctx
    want = bool(ctx.slot_data.get("villager_checks", False))
    applied = getattr(ctx, "villager_block_applied", None)
    if applied == want or (applied is None and not want):
        return                      # nothing to change (and no needless pipe traffic for seeds without the option)
    async with ctx.bridge_pipe_lock:
        ok, error = await asyncio.to_thread(set_villager_block, pm, want)
    if ok:
        ctx.villager_block_applied = want
        game_logger.info("Villager blocking " + ("ON (freed villagers are checks now)." if want else "OFF."))
    elif not getattr(ctx, "villager_block_warned", False):
        ctx.villager_block_warned = True
        game_logger.info(f"Could not switch villager blocking ({error}) - rebuild dungeons_bridge.dll from the "
                         f"current .cpp. Until then the villager checks cannot be detected.")


def _note_event(ctx, text):
    log = ctx.__dict__.setdefault("villager_event_log", [])
    log.append(f"{time.strftime('%H:%M:%S')} {text}")
    del log[:-40]


async def villager_tick(ctx, pm):
    """Every ~1.5 s: drains the DLL's merchant unlock queue and sends the check of each captured camp merchant the
    game tried to unlock. Events of merchants the option does not cover are only logged."""
    global _ctx_ref
    _ctx_ref = ctx
    now = time.time()
    if now - getattr(ctx, "villager_polled_at", 0.0) < POLL_INTERVAL:
        return
    ctx.villager_polled_at = now
    async with ctx.bridge_pipe_lock:
        events = await asyncio.to_thread(get_merchant_events, pm)
    if events is None:
        if not getattr(ctx, "villager_events_warned", False):
            ctx.villager_events_warned = True
            game_logger.info("Villager checks: the DLL does not answer get_merchant_events - rebuild "
                             "dungeons_bridge.dll from the current .cpp.")
        return
    for _addr, def_name, func_name in events:
        key = villager_key_for_def_name(def_name)
        if key is None:
            game_logger.info(f"Merchant unlock seen ({func_name}): {def_name} - not a captured camp merchant of this "
                             f"option, left alone.")
            _note_event(ctx, f"{def_name} via {func_name}: ignored")
            continue
        location_id = _apw.get_villager_location_id(key)
        _note_event(ctx, f"{def_name} via {func_name}: check '{_apw.villager_location_name(key)}'")
        if location_id in ctx.checked_locations:
            continue
        server_locations = getattr(ctx, "server_locations", None)
        if server_locations and location_id not in server_locations:
            continue
        sent = ctx.__dict__.setdefault("villager_sent_at", {})
        if time.time() - sent.get(location_id, 0.0) < 10.0:
            continue
        sent[location_id] = time.time()
        game_logger.info(f"Villager freed: {_apw.villager_location_name(key)} (zone {ctx.game_state.get('zone')}).")
        await ctx.check_locations([location_id])


async def grant_pending_villagers(ctx, pm, base, save_applied_reward_indices):
    """Unlocks the camp merchants received as items (same rules as the eye grants: a confirmed character, marked
    applied only once the game call succeeded, otherwise retried next tick)."""
    still_pending = []
    for absolute_index, item_name in ctx.pending_villager_grants:
        key = _KEY_BY_ITEM.get(item_name)
        if key is None:
            continue
        async with ctx.bridge_pipe_lock:
            ok, err = await asyncio.to_thread(unlock_merchant, pm, base, key)
        if ok:
            ctx.applied_reward_indices.add(absolute_index)
            save_applied_reward_indices(ctx.applied_reward_indices)
            game_logger.info(f"Granted merchant '{item_name}' (reward #{absolute_index}).")
        else:
            if time.time() - getattr(ctx, "villager_grant_log_at", 0.0) >= 15.0:
                ctx.villager_grant_log_at = time.time()
                game_logger.info(f"Merchant '{item_name}' (reward #{absolute_index}) not unlocked this tick: "
                                 f"{err} - will retry.")
            still_pending.append((absolute_index, item_name))
    ctx.pending_villager_grants = still_pending


# ------------------------------------------------------------------ /villager devtool
HELP_LINES = [
    "/villager list                  the five captured merchants: definition class found, unlocked in the camp or not",
    "/villager events                the merchant unlocks the client saw this session (and what it did with them)",
    "/villager unlock <name>         unlock one merchant now (village, mystery, luxury, giftwrapper, blacksmith)",
    "/villager block on|off          switch the DLL's blocking by hand (the client restores the seed's value)",
]


def run_villager(ctx, pm, base, name="", extra="", say=None):
    """Entry point of /villager (blocking, run through asyncio.to_thread with the bridge pipe lock held)."""
    R = mb._R()
    key = mb.normalize(name)
    if key in ("", "help"):
        return HELP_LINES
    if key == "events":
        log = getattr(ctx, "villager_event_log", [])
        return ["Merchant unlocks seen this session:"] + (log or ["  (none yet - free a captured villager or "
                                                                   "receive a merchant item)"])
    if key == "block":
        on = mb.normalize(extra) in ("on", "1", "true")
        ok, err = set_villager_block(pm, on)
        return [f"Villager blocking {'ON' if on else 'OFF'}." if ok else f"villager: {err}"]
    if R.bridge_hook_state(pm) == "silent":
        return ["The game is loading or changing level - try again in a few seconds."]
    say = say or (lambda line: None)
    try:
        if key == "list":
            lines = []
            for vkey, _frag, display, zone in VILLAGER_TYPES:
                try:
                    cname, _addr = resolve_def_class(pm, base, vkey, say)
                    state = "unlocked" if merchant_is_unlocked(pm, base, vkey, say) else "locked"
                    lines.append(f"  {display} ({zone}): {cname} - {state}")
                except mb.MinibossError as e:
                    lines.append(f"  {display} ({zone}): {e}")
            return ["Captured camp merchants:"] + lines
        if key == "unlock":
            target = mb.normalize(extra)
            for vkey, _frag, display, _zone in VILLAGER_TYPES:
                if target in (vkey, mb.normalize(display)):
                    ok, err = unlock_merchant(pm, base, vkey, say)
                    return [f"Unlocked {display}." if ok else f"villager: {err}"]
            return [f"Unknown merchant '{extra}'. One of: " + ", ".join(v[0] for v in VILLAGER_TYPES)]
    except mb.MinibossError as e:
        return [f"villager: {e}"]
    return ["Unknown /villager command - type /villager for the list."]
