"""Devtool behind the /miniboss command - try to summon a mini boss ("powerful mob").

Why it exists: to test the "Powerful Mob" checks you have to be able to meet a powerful mob on
demand instead of hoping the game rolls one. This spawns one next to the local player and then
asks the game (IsEventMob) whether the result really counts as a powerful mob - the same question
the powerful-mob tracker in dungeons_reader.py asks.

NOT based on UUU (Universal Unreal Engine 4 Unlocker): UUU injects its own hooks next to the
bridge's and crashes the game, because it lacks the bridge's protections. Everything here goes
through the bridge's own CALLDATA command, which gives the protections for free: the call runs on
the game thread, is cancelled if the world changes or the hook goes silent, access violations are
caught and returned as an error instead of crashing, and it times out after 5 s. This module also
refuses to start while the hook is silent (loading screens).

How it spawns: GameplayStatics::BeginDeferredActorSpawnFromClass + FinishSpawningActor on the mob's
Blueprint class - the two stock UE4 spawn functions. Parameter layouts are hard-coded for UE 4.22;
before each call the real parameter-struct size is read from the UFunction and compared, and a
mismatch REFUSES the call instead of risking memory corruption.

Finding the class: without UUU's "summon" there is nobody to tell us the Blueprint class names, so
they are found three ways, in this order: (0) the class of a mob of that EntityType that is alive in the current zone (tried right after the
guesses, needs no names at all), (1) classes remembered by /miniboss learn or by an
earlier success, (2) the usual guesses (BP_<Name>_C, the convention seen in the bridge log:
BP_EndermanDeath, BP_EndersentDeath...), (3) a scan of every Blueprint class loaded in the game,
keeping the names that contain the mob's name AND that really inherit from AMobCharacter. The scan
is slow the first time (reads the whole object table, ~10-30 s) and is cached afterwards.

Game lag, and what was done about it: the bridge's find_by_name runs on the game's render thread and
converts the name of EVERY object (~600,000) to text, so each call froze the game for the whole scan,
and one create chained a dozen of them. They are now answered from an index of the object table that is
read from Python (memory reads only, at a lowered thread priority; first time per level, 10-30 s) and
that checks itself against the game (a default object must carry the name the game gives it); when the
index cannot answer, the old scan is used once and its result cached. Every create also prints what it
cost the game ("game thread: N bridge call(s), X ms ... no full object-table scan"), so a hitch can be
traced to the call that caused it. What is left is genuine game work: the blocking asset load
(LoadClassAsset_Blocking, see /miniboss preload) and the spawn itself.

Commands (typed in the client as /miniboss ...):

    /miniboss                       list the mini boss types and the class each one resolves to
    /miniboss <type> [ClassName]    spawn one next to you (type = evoker, ghast, redstonegolem...)
    /miniboss class <ClassName>     spawn any class by exact name (raw devtool)
    /miniboss find <text>           list the loaded Blueprint classes whose name contains <text>
    /miniboss learn                 read the mobs alive in the current zone and remember their real
                                    class names in miniboss_classes.json
    /miniboss create random [load] [dry]
                                    draw a mini boss at random (the six Endersent variants are not in the
                                    draw). By default only types whose class is ALREADY in memory are
                                    drawn, so nothing has to be loaded and nothing freezes; "load" also
                                    draws types that must be loaded from a saved asset path (one short
                                    freeze).
    /miniboss preload [type|all]    load the saved-path classes into memory NOW, at a moment you pick
                                    (the blocking load freezes the game briefly; the command says for
                                    how long). Loaded classes can be unloaded again by the game's garbage
                                    collector and by a level change, so create soon after.
    /miniboss create <type|Class> [dry]
                                    build a mini boss from the game's OWN classes only, with no mob
                                    alive nearby and no real powerful mob to copy: the class is picked
                                    from the game's class table by what its default object says (AI
                                    controller named / auto-possess), the EntityType is read from the
                                    game's EEntityType enum, and the powerful bit is found by asking the
                                    game's IsEventMob on the new, not-yet-finished actor. "dry" only
                                    shows these decisions and spawns nothing.
    /miniboss load <AssetPath>      load a mob class that no level has loaded yet, by its asset path
                                    (e.g. /Game/.../BP_X.BP_X_C), with the game's own blocking loader;
                                    the path is saved, so create / <type> then work in ANY level
    /miniboss paths [type|text]     list the saved asset paths; with a type, save the ones of the loaded
                                    classes that match it (paths are also saved on every spawn/learn)
    /miniboss reflect <type|Class|last> [word]
                                    list the properties/functions the game declares on a mob class
                                    (full list in miniboss_reflect.txt) - what to send when a step of
                                    "create" does not behave
    /miniboss diag [text]           what the command sees in this zone (actors, classes recognised as
                                    mobs, parent chain of classes matching <text>) - for when a mob
                                    that is clearly there is not found
    /miniboss probe                 list the mobs alive here with EntityType, flags byte and IsEventMob,
                                    and learn which flag bit makes a mob "powerful" (run it next to a
                                    real powerful mob)
    /miniboss promote [bit]         mark the last spawned mob as powerful (known bit, or try them all)
    /miniboss functions             list functions/fields on the last spawned mob that look related
                                    to "powerful/event/boss" (to find how the game promotes a mob)
    /miniboss console <type> [Cls]  EXPERIMENTAL: send "summon <class>" to the Unreal console through
                                    the bridge. Only works if a cheat manager exists, which in a
                                    shipping build normally only UUU provides - so expect nothing.

Known limits:
  * A spawned mob may be an ordinary mob, not a "powerful" one: the game may set that flag when a
    mission rolls its mini boss, not in the class. The command reports IsEventMob for the result.
  * Several EntityTypes can share one class (e.g. the Endersent variants); the spawn then gives
    the class default and the report shows which EntityType came out.
  * Only classes currently loaded in memory can be found; a mob that never loaded in this session
    may need /miniboss learn in a zone that has it.

Not thread-safe and blocking: call run_miniboss() from a worker thread while holding the client's
bridge pipe lock (see _run_miniboss in dungeons_ap_client.py). Never keeps a pipe open between
requests, because the reader helpers it calls open their own connection to the single-instance pipe.
"""
import difflib
import random
from array import array
from collections import Counter
import json
import os
import re
import struct
import time
import zlib

LEARNED_FILE = "miniboss_classes.json"
PATHS_KEY = "asset_paths"               # in miniboss_classes.json: {class name: "/Game/.../BP_X.BP_X_C"}
STRING_LIBRARY = "KismetStringLibrary"
STRING_TO_NAME = "Conv_StringToName"     # engine: FString -> FName (this is how a path becomes an FName)
LOAD_LIBRARY = "WidgetHelper"
LOAD_FUNCTION = "LoadClassAsset_Blocking"   # game: TSoftClassPtr<UClass> -> UClass*, loads the asset now
SOFT_CLASS_PTR_SIZE = 0x28               # FWeakObjectPtr 8 + tag 4 + pad 4 + FSoftObjectPath (FName 8 + FString 16)
ANY_PARAM_SIZES = tuple(range(8, 0x100, 8))   # real layout is read from the function itself, see function_layout

# alias -> (display name, EntityType enum name, EntityType ids). Ids come from the reader's
# ENTITY_TYPE_NAMES table (Dumper-7 dump). The in-game "Wildfire" is the class/entity the game calls
# HoveringInferno (EntityType 2810, seen as a powerful mob in Basalt Deltas), so "wildfire" and
# "hoveringinferno" both name that one entry.
MINIBOSS_TYPES = {
    "evoker":                ("Evoker", "Evoker", (2619,)),
    "redstonegolem":         ("Redstone Golem", "RedstoneGolem", (2588,)),
    "skeletonhorseman":      ("Skeleton Horseman", "SkeletonHorseman", (68177,)),
    "enderman":              ("Enderman", "Enderman", (2652,)),
    "illusioner":            ("Illusioner", "Illusioner", (224,)),
    "ghast":                 ("Ghast", "Ghast", (2601,)),
    "drownednecromancer":    ("Drowned Necromancer", "DrownedNecromancer", (1116983,)),
    "elderguardian":         ("Elder Guardian", "ElderGuardian", (2610,)),
    "pillagerraidcaptain":   ("Pillager Raid Captain", "PillagerRaidCaptain", (2874,)),
    "vindicatorraidcaptain": ("Vindicator Raid Captain", "VindicatorRaidCaptain", (2873,)),
    "wildfire":              ("Wildfire", "HoveringInferno", (2810,)),
    "endersent":             ("Endersent", "Endersent", (836,)),
    "endersentthornblight":  ("Endersent Thornblight", "EndersentThornblight", (837,)),
    "endersentvoidstrike":   ("Endersent Voidstrike", "EndersentVoidstrike", (838,)),
    "endersentwritherot":    ("Endersent Writherot", "EndersentWritherot", (839,)),
    "endersenteverfire":     ("Endersent Everfire", "EndersentEverfire", (840,)),
    "endersentsurgefiend":   ("Endersent Surgefiend", "EndersentSurgefiend", (841,)),
    "endersentdeadeye":      ("Endersent Deadeye", "EndersentDeadeye", (842,)),
}

# ---- spawn call layout (UE 4.22, x64) -------------------------------------------------------
# BeginDeferredActorSpawnFromClass(UObject* WorldContext, TSubclassOf<AActor> ActorClass,
#     const FTransform& SpawnTransform, ESpawnActorCollisionHandlingMethod Collision, AActor* Owner)
#     -> AActor*
#   0x00 WorldContext  0x08 ActorClass  0x10 SpawnTransform (48 bytes, 16-aligned)
#   0x40 Collision (uint8)  0x48 Owner  0x50 ReturnValue  -> 0x58 (0x60 once rounded to 16)
# FinishSpawningActor(AActor* Actor, const FTransform& SpawnTransform) -> AActor*
#   0x00 Actor  0x10 SpawnTransform  0x40 ReturnValue  -> 0x48 (0x50 rounded)
BEGIN_FUNCTION = "BeginDeferredActorSpawnFromClass"
FINISH_FUNCTION = "FinishSpawningActor"
BEGIN_SIZES_OK = (0x58, 0x60)
FINISH_SIZES_OK = (0x48, 0x50)
BEGIN_BUFFER = 0x60
FINISH_BUFFER = 0x50
COLLISION_ADJUST_ALWAYS_SPAWN = 2      # ESpawnActorCollisionHandlingMethod::AdjustIfPossibleButAlwaysSpawn
SPAWN_DISTANCE = 450.0                 # Unreal units (cm) in front along +X of the player
SPAWN_HEIGHT = 120.0
SPAWN_COOLDOWN = 2.0
CHEST_SPAWN_HEIGHT = 150.0             # anchored spawns drop the mob from this far above the anchor

# ExecuteConsoleCommand(UObject* WorldContext, const FString& Command, APlayerController* SpecificPlayer)
#   0x00 WorldContext  0x08 Command (FString: data ptr, Num, Max)  0x18 SpecificPlayer  -> 0x20
EXEC_FUNCTION = "ExecuteConsoleCommand"
EXEC_SIZES_OK = (0x20,)
SUMMON_WAIT = 0.45                     # seconds to let the spawn show up before looking at the level
MAX_SUMMON_TRIES = 5

# names containing these are Blueprint helpers around a mob (death effect, projectile...), never the mob
NOT_MOB_TOKENS = ("death", "fang", "projectile", "fireball", "spawner", "summon", "anim", "widget",
                  "fx", "effect", "hud", "icon", "ability", "spell", "clone", "baby", "statue",
                  "corpse", "trigger", "encounter", "spawn", "minion", "arrow", "debris")
CLASS_SCAN_BUDGET = 150.0              # seconds the object-table scan may take at most
CLASS_SCAN_HEADER = 0x20               # UObject header bytes read per object (class ptr @0x10, name @0x18)

FUNCTION_KEYWORDS = ("event", "boss", "powerful", "elite", "champion", "enchant", "ancient",
                     "promote", "empower", "mini")

_last_spawn = {"time": 0.0, "actor": 0, "class": 0}


class MinibossError(RuntimeError):
    """A normal, explainable failure - the message is shown to the player as is."""


# ---- mini boss per zone ------------------------------------------------------------------------
# zone internal name (ZoneData.py) -> the mini boss of that zone (aliases of MINIBOSS_TYPES). This is the
# list checked in the game, one pinned entry per zone; an empty list means the zone has no mini boss and
# no mini boss check. (zone_miniboss still accepts several entries and then picks one per seed and zone,
# the same one every time, should you ever want a pool again.)
ZONE_MINIBOSSES = {
    # Mainland
    "squidcoast":                [],
    "creeperwoods":              ["enderman"],
    "creepycrypt":               ["skeletonhorseman"],
    "pumpkinpastures":           ["evoker"],
    "archhaven":                 [],
    "soggyswamp":                ["enderman"],
    "soggycave":                 ["skeletonhorseman"],
    "redstonemines":             ["redstonegolem"],
    "fieryforge":                ["redstonegolem"],
    "cacticanyon":               ["skeletonhorseman"],
    "deserttemple":              ["enderman"],
    "lowertemple":               ["skeletonhorseman"],
    "highblockhalls":            ["evoker"],
    "underhalls":                [],
    "obsidianpinnacle":          ["redstonegolem"],
    # Mooshroom Island and The Stronghold are not in the list: no mini boss
    # Jungle Awakens
    "dingyjungle":               ["enderman"],
    "overgrowntemple":           ["enderman"],
    "bamboobluff":               ["skeletonhorseman"],      # Panda Plateau
    "treetoptangle":             ["enderman"],
    # Creeping Winter
    "frozenfjord":               ["illusioner"],            # Frosted Fjord
    "lonefortress":              ["illusioner"],
    "lostsettlement":            ["illusioner"],
    # Howling Peaks
    "windsweptpeaks":            ["evoker"],
    "gauntletofgales":           ["illusioner"],
    "galesanctum":               ["evoker"],
    "endlessrampart":            ["illusioner"],            # Colossal Rampart
    # Hidden Depths
    "coralrise":                 ["drownednecromancer"],
    "abyssalmonument":           ["elderguardian"],
    "radiantravine":             ["drownednecromancer"],
    # Flames of the Nether
    "netherwastes":              [],
    "warpedforest":              ["enderman"],
    "crimsonforest":             ["wildfire"],
    "soulsandvalley":            ["wildfire"],
    "basaltdeltas":              ["ghast"],
    "netherfortress":            ["wildfire"],
    # Echoing Void
    "enderwilds":                ["endersent"],             # End Wilds
    "endcitadel_blightedcitadel": ["endersent"],            # Broken Citadel
}


def zone_miniboss(zone, seed_key=""):
    """The mini boss alias chosen for `zone` in this seed, or None when the zone has no entry. Several
    candidates -> one is picked by a stable hash of seed + zone (same seed, same choice, every time)."""
    pool = [a for a in ZONE_MINIBOSSES.get(zone, []) if a in MINIBOSS_TYPES]
    if not pool:
        return None
    return pool[zlib.crc32(f"{seed_key}|{zone}".encode("utf-8")) % len(pool)]


# ---- small pure helpers (unit-tested without the game) --------------------------------------
def normalize(text):
    return re.sub(r"[^a-z0-9]", "", str(text).lower())


ALIAS_SYNONYMS = {"hoveringinferno": "wildfire", "inferno": "wildfire"}


def resolve_alias(text):
    """Exact alias, or a unique prefix of one ('skeleton' -> skeletonhorseman). Returns
    (alias, None) or (None, error_message)."""
    key = ALIAS_SYNONYMS.get(normalize(text), normalize(text))
    if key in MINIBOSS_TYPES:
        return key, None
    matches = [a for a in MINIBOSS_TYPES if key and a.startswith(key)]
    if len(matches) == 1:
        return matches[0], None
    if len(matches) > 1:
        return None, f"'{text}' is ambiguous: {', '.join(sorted(matches))}"
    close = difflib.get_close_matches(key, list(MINIBOSS_TYPES), n=1, cutoff=0.8) if key else []
    if close:   # a typo such as 'eldergardian' -> elderguardian
        return close[0], None
    return None, f"Unknown mini boss '{text}'. Try /miniboss to see the list."


def guess_class_names(entity_name):
    n = entity_name
    return [f"BP_{n}_C", f"BP_{n}Character_C", f"{n}Character_C", f"{n}_C", f"BP_{n}Mob_C",
            f"BP_Mob_{n}_C", f"{n}Mob_C", f"Mob_{n}_C"]


def build_transform(location):
    x, y, z = location
    # FQuat (identity) | FVector + pad | FVector scale + pad  == 48 bytes
    return struct.pack("<12f", 0.0, 0.0, 0.0, 1.0, x, y, z, 0.0, 1.0, 1.0, 1.0, 0.0)


def build_begin_params(world_context, class_addr, location, collision=COLLISION_ADJUST_ALWAYS_SPAWN,
                       owner=0, size=BEGIN_BUFFER):
    buf = bytearray(max(size, BEGIN_BUFFER))
    struct.pack_into("<Q", buf, 0x00, world_context)
    struct.pack_into("<Q", buf, 0x08, class_addr)
    buf[0x10:0x40] = build_transform(location)
    struct.pack_into("<B", buf, 0x40, collision)
    struct.pack_into("<Q", buf, 0x48, owner)
    return bytes(buf)


def build_finish_params(actor, location, size=FINISH_BUFFER):
    buf = bytearray(max(size, FINISH_BUFFER))
    struct.pack_into("<Q", buf, 0x00, actor)
    buf[0x10:0x40] = build_transform(location)
    return bytes(buf)


def encode_fstring(text):
    """UTF-16LE bytes of text including the terminating NUL, and the element count (Num/Max)."""
    raw = text.encode("utf-16-le") + b"\x00\x00"
    return raw, len(raw) // 2


def build_exec_params(world_context, string_addr, count, player=0):
    buf = bytearray(0x20)
    struct.pack_into("<Q", buf, 0x00, world_context)
    struct.pack_into("<Q", buf, 0x08, string_addr)
    struct.pack_into("<ii", buf, 0x10, count, count)
    struct.pack_into("<Q", buf, 0x18, player)
    return bytes(buf)


def read_return_pointer(params_after, offset):
    return struct.unpack_from("<Q", params_after, offset)[0]


def _load_learned():
    try:
        with open(LEARNED_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data.get("classes"), dict):
            return data
    except (OSError, ValueError, AttributeError):
        pass
    return {"classes": {}}


def _save_learned(data):
    with open(LEARNED_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)


def candidate_class_names(alias, learned=None):
    """[(class_name, 'learned'|'guessed')] - learned classes (seen with that EntityType) first."""
    learned = learned if learned is not None else _load_learned()
    _, entity_name, ids = MINIBOSS_TYPES[alias]
    out, seen = [], set()
    for cname, rec in learned.get("classes", {}).items():
        if any(str(i) in rec.get("entity_types", {}) for i in ids) and cname not in seen:
            seen.add(cname)
            out.append((cname, "learned"))
    for cname in guess_class_names(entity_name):
        if cname not in seen:
            seen.add(cname)
            out.append((cname, "guessed"))
    return out


# ---- game access ----------------------------------------------------------------------------
def _R():
    import dungeons_reader
    return dungeons_reader


def _pipe_request(pm, text, read_size=65536):
    """One request on a fresh pipe connection, closed again before returning."""
    import win32file
    pipe = _R()._connect_bridge_pipe(pm)
    try:
        win32file.WriteFile(pipe, text.encode())
        _, data = win32file.ReadFile(pipe, read_size)
    finally:
        win32file.CloseHandle(pipe)
    return data.decode("utf-8", errors="replace").rstrip("\x00")


# ---- what costs the game time --------------------------------------------------------------
# The bridge's find_by_name runs on the game's render thread and converts the name of EVERY object
# (~600,000) to text before comparing: the game is frozen for the whole scan, and a single /miniboss
# create used to chain a dozen of them. Calls through CALLDATA also run on the game thread (the spawn,
# the blocking asset load...). Both are timed here so the command can say what it cost the game.
_perf = {"calls": []}          # (label, milliseconds, kind) kind = "game" (CALLDATA) | "scan" (find_by_name)
_state = {"base": None, "say": None}   # set by run_miniboss: lets name lookups use the object index
_slow_cache = {}               # (world, name) -> addresses, so one name costs at most one game-thread scan


def perf_reset():
    _perf["calls"].clear()


def perf_add(label, started, kind):
    _perf["calls"].append((label, (time.time() - started) * 1000.0, kind))


def perf_lines():
    """What this command cost the game: time spent in calls that run on the game thread, and any
    full object-table scan (the real freeze)."""
    game = [c for c in _perf["calls"] if c[2] == "game"]
    scans = [c for c in _perf["calls"] if c[2] == "scan"]
    lines = []
    if game:
        slowest = max(game, key=lambda c: c[1])
        lines.append(f"   game thread: {len(game)} bridge call(s), {sum(c[1] for c in game):.0f} ms in total "
                     f"(slowest: {slowest[0]} {slowest[1]:.0f} ms)")
    if scans:
        lines.append(f"   WARNING: {len(scans)} full object-table scan(s) ran on the game thread "
                     f"({sum(c[1] for c in scans):.0f} ms frozen): {', '.join(c[0] for c in scans[:6])}")
    elif game:
        lines.append("   no full object-table scan ran on the game thread")
    return lines


def _bridge_find_objects_by_name(pm, name):
    """The bridge's own find_by_name: exact, but it freezes the game for a full object-table scan."""
    R = _R()
    world = _state["base"] and pm.read_longlong(_state["base"] + R.OFFSETS["gworld"])
    if (world, name) in _slow_cache:
        return list(_slow_cache[(world, name)])
    started = time.time()
    resp = _pipe_request(pm, f"find_by_name:{name}")
    perf_add(name, started, "scan")
    if not resp.startswith("FOUND:"):
        raise MinibossError(f"bridge could not search objects ({resp[:120]})")
    header, _, rest = resp.partition("|")
    found = [] if int(header.split(":")[1]) == 0 else [int(a, 16) for a in rest.split(",") if a]
    _slow_cache[(world, name)] = list(found)       # a miss is cached too: asking again would freeze the game again
    return found


def find_objects_by_name(pm, name):
    """Objects called exactly <name>. Answered from the object index (read from Python, the game is
    not involved) whenever the index knows the name - classes, their default objects (Default__X)
    and enums; anything else falls back to the bridge's game-thread scan."""
    fast = _index_find(pm, name)
    if fast is not None:
        return fast
    return _bridge_find_objects_by_name(pm, name)


def find_class(pm, class_name):
    """Address of the UClass called exactly class_name, or None."""
    R = _R()
    for addr in find_objects_by_name(pm, class_name):
        try:
            meta = pm.read_longlong(addr + 0x10)            # the object's own class
            meta_name = R.resolve_fname(pm, pm.read_int(meta + 0x18)) if meta else None
        except Exception:
            continue
        if meta_name and meta_name.endswith("Class"):       # Class / BlueprintGeneratedClass
            return addr
    return None


def calldata(pm, object_addr, function_addr, params):
    started = time.time()
    resp = _pipe_request(pm, f"CALLDATA {object_addr:X} {function_addr:X} {params.hex().upper()}", 8192)
    elapsed = (time.time() - started) * 1000.0
    try:
        label = _R().get_ufunction_name(pm, function_addr) or hex(function_addr)
    except Exception:
        label = hex(function_addr)
    _perf["calls"].append((label, elapsed, "game"))
    if not resp.startswith("OK "):
        raise MinibossError(f"the bridge refused the call: {resp[:160]}")
    return bytes.fromhex(resp[3:])


_static_cache = {}      # (class, function) -> (cdo, function address, parameter size, function name index)


def _find_static_function(pm, class_name_of_cdo, function_name, sizes_ok):
    """(cdo_addr, function_addr, parms_size) for a static function on Default__<class>. Engine
    library functions never move, so the answer is kept for the session (and re-checked by name)."""
    R = _R()
    key = (class_name_of_cdo, function_name)
    hit = _static_cache.get(key)
    if hit:
        try:
            if hit[2] in sizes_ok and pm.read_int(hit[1] + 0x18) == hit[3]:
                return hit[0], hit[1], hit[2]
        except Exception:
            pass
    cdos = find_objects_by_name(pm, f"Default__{class_name_of_cdo}")
    if not cdos:
        raise MinibossError(f"Default__{class_name_of_cdo} not found - is a level loaded?")
    cdo = cdos[0]
    cls = pm.read_longlong(cdo + 0x10)
    func = R._find_function_on_class(pm, cls, function_name)
    if not func:
        raise MinibossError(f"{class_name_of_cdo}::{function_name} not found in this build")
    size = pm.read_int(func + R.OFFSETS["ustruct_size"])
    if size not in sizes_ok:
        raise MinibossError(
            f"{function_name} parameter struct is 0x{size:X} bytes, expected one of "
            f"{[hex(s) for s in sizes_ok]} - refusing to call it (wrong layout could corrupt "
            f"memory). Send me this number so the layout can be adjusted.")
    try:
        _static_cache[key] = (cdo, func, size, pm.read_int(func + 0x18))
    except Exception:
        pass
    return cdo, func, size


def spawn_class(pm, base, class_addr, before_finish=None, anchor=None, offset=(0.0, 0.0)):
    """Spawns class_addr next to the local player. Returns (actor_addr, location).
    before_finish(actor), when given, runs between BeginDeferredActorSpawnFromClass and
    FinishSpawningActor - the window where a spawner sets a mob's properties so that BeginPlay (AI,
    behaviour tree, boss bar) already sees them."""
    R = _R()
    pawn, err = R.get_pawn(pm, base)
    if not pawn:
        raise MinibossError(f"no player character ({err})")
    if anchor:
        # a position known to be floor (a chest the player just opened, or the player himself): spawn right
        # above it and let the mob drop, instead of a spot in front of the player that may be inside a
        # wall or over a void
        where = (anchor[0] + offset[0], anchor[1] + offset[1], anchor[2] + CHEST_SPAWN_HEIGHT)
    else:
        loc = R.get_actor_location(pm, pawn)
        if loc is None:
            raise MinibossError("could not read the player's position")
        where = (loc[0] + SPAWN_DISTANCE + offset[0], loc[1] + offset[1], loc[2] + SPAWN_HEIGHT)

    cdo, f_begin, size_b = _find_static_function(pm, "GameplayStatics", BEGIN_FUNCTION, BEGIN_SIZES_OK)
    _, f_fin, size_f = _find_static_function(pm, "GameplayStatics", FINISH_FUNCTION, FINISH_SIZES_OK)

    out = calldata(pm, cdo, f_begin, build_begin_params(pawn, class_addr, where, size=size_b))
    actor = read_return_pointer(out, 0x50)
    if not actor:
        raise MinibossError("the game returned no actor (class not spawnable here, or blocked)")
    if before_finish:
        try:
            before_finish(actor)
        except Exception:
            pass        # never let a failed pre-setup stop the spawn from being finished
    calldata(pm, cdo, f_fin, build_finish_params(actor, where, size=size_f))
    return actor, where


# Some mini bosses never come alone: the Skeleton Horsemen are a group of four. Every spawn of such a type
# (/miniboss <type>, create, atchest, the zone mini boss) makes the whole group, spread around the spawn point.
SKELETON_HORSEMAN_ID = 68177
GROUP_SIZE_BY_ENTITY_TYPE = {SKELETON_HORSEMAN_ID: 4}
GROUP_OFFSETS = [(0.0, 0.0), (250.0, 0.0), (-250.0, 0.0), (0.0, 250.0), (0.0, -250.0)]   # Unreal units (cm)
GROUP_SPAWN_GAP = 0.15                  # seconds between two members, so the game is never hit by 4 spawns at once


def group_size(ids):
    return max([GROUP_SIZE_BY_ENTITY_TYPE.get(i, 1) for i in ids] or [1])


def spawn_group(pm, base, class_addr, ids, make_before_finish, anchor=None):
    """Spawns one mob, or the whole group for a type that comes in groups. make_before_finish(i) builds the
    pre-finish setup of member i (0 = the first one). Returns [(actor, where), ...]; the first member is the
    one the rest of the command reports on. A member that fails after the first is skipped, not fatal."""
    group = []
    for i in range(group_size(ids)):
        try:
            group.append(spawn_class(pm, base, class_addr, before_finish=make_before_finish(i), anchor=anchor,
                                     offset=GROUP_OFFSETS[i % len(GROUP_OFFSETS)]))
        except MinibossError:
            if not group:
                raise
            break
        if i + 1 < group_size(ids):
            time.sleep(GROUP_SPAWN_GAP)
    return group


def group_extra_lines(pm, group, ids, display):
    """Gives the other members of a group the same post-spawn setup as the first; returns a report line."""
    for actor, _ in group[1:]:
        try:
            post_spawn_setup(pm, actor, ids)
        except Exception:
            pass
    return [f"   group: {len(group)} x {display} spawned around that spot."] if len(group) > 1 else []


# ---- console route (what UUU unlocks) -------------------------------------------------------
_fstring_cache = {}     # command text -> (address in the game's memory, element count); tiny, kept on purpose


def console_command(pm, base, command):
    """Runs one console command for the local player via KismetSystemLibrary::ExecuteConsoleCommand
    (the same thing the Unreal console does). The text lives in memory allocated inside the game."""
    R = _R()
    pawn, err = R.get_pawn(pm, base)
    if not pawn:
        raise MinibossError(f"no player character ({err})")
    cdo, func, size = _find_static_function(pm, "KismetSystemLibrary", EXEC_FUNCTION, EXEC_SIZES_OK)
    if command not in _fstring_cache:
        raw, count = encode_fstring(command)
        addr = pm.allocate(len(raw))
        pm.write_bytes(addr, raw, len(raw))
        _fstring_cache[command] = (addr, count)
    addr, count = _fstring_cache[command]
    calldata(pm, cdo, func, build_exec_params(pawn, addr, count))


def _level_actors(pm, base):
    R = _R()
    world = pm.read_longlong(base + R.OFFSETS["gworld"])
    if not world:
        raise MinibossError("no level loaded")
    return R.scan_full_zone(pm, world)


def _zone_name(pm, base):
    R = _R()
    world = pm.read_longlong(base + R.OFFSETS["gworld"])
    index = R.get_zone_name_index(pm, world) if world else None
    return R.ZONE_NAME_LOOKUP.get(index, "hub") if index else "unknown"


def mobs_that_appeared(pm, base, action):
    """Snapshots the level, runs action(), waits a moment, and returns the mobs that are new:
    [(actor, class_name, class_ptr, info)] with info = EntityType/flags/IsEventMob."""
    R = _R()
    before = set(_level_actors(pm, base))
    action()
    time.sleep(SUMMON_WAIT)
    found, is_mob = [], {}
    for addr, class_idx in _level_actors(pm, base).items():
        if addr in before:
            continue
        try:
            class_ptr = pm.read_longlong(addr + R.OFFSETS["uobject_class"])
        except Exception:
            continue
        if not class_ptr:
            continue
        if class_idx not in is_mob:
            is_mob[class_idx] = R._is_mob_class(pm, class_ptr)
        if not is_mob[class_idx]:
            continue
        info = R._evaluate_mob(pm, addr, class_ptr, {})
        if info:
            found.append((addr, R.resolve_fname(pm, class_idx) or f"class{class_idx}", class_ptr, info))
    return found


def _record_mob(learned, class_name, info, zone):
    """Remembers class -> EntityType (and whether the game called it powerful). Returns new facts."""
    R = _R()
    rec = learned["classes"].setdefault(class_name, {"entity_types": {}, "event_types": [], "zones": []})
    fresh = []
    key = str(info["etype"])
    if key not in rec["entity_types"]:
        rec["entity_types"][key] = R.entity_type_name(info["etype"])
        fresh.append(f"{class_name}: {rec['entity_types'][key]}")
    if info["event"] and info["etype"] not in rec["event_types"]:
        rec["event_types"].append(info["etype"])
        fresh.append(f"   powerful in {zone}: {R.entity_type_name(info['etype'])} ({class_name})")
    if zone not in rec["zones"]:
        rec["zones"].append(zone)
    return fresh


def _info_lines(info, expected_ids):
    R = _R()
    name = R.entity_type_name(info["etype"])
    verdict = "matches" if info["etype"] in expected_ids else "DOES NOT match the requested type"
    lines = [f"   EntityType {info['etype']} = {name} ({verdict})"]
    if info["event"] is None:
        lines.append("   IsEventMob: could not be asked")
    elif info["event"]:
        lines.append("   IsEventMob = True -> the game treats it as a powerful mob")
    else:
        lines.append("   IsEventMob = False -> an ordinary mob, it would not count as a powerful kill. "
                     "Try /miniboss functions to look for how the game promotes one.")
    return lines


def describe_actor(pm, actor, expected_ids):
    """Report lines about a freshly spawned mob: EntityType and what the game says about IsEventMob."""
    R = _R()
    try:
        class_ptr = pm.read_longlong(actor + R.OFFSETS["uobject_class"])
        info = R._evaluate_mob(pm, actor, class_ptr, {}) if class_ptr else None
    except Exception:
        info = None
    if not info:
        return [f"   spawned actor at {hex(actor)} but it does not read like a mob (no EntityType)."]
    return _info_lines(info, expected_ids)


def _summon(pm, base, class_name):
    """'summon <class_name>' through the console; returns the mobs that appeared."""
    return mobs_that_appeared(pm, base, lambda: console_command(pm, base, f"summon {class_name}"))


def _remember_success(pm, base, hit):
    """Saves the class of what was just summoned and remembers it for /miniboss functions."""
    addr, class_name, class_ptr, info = hit
    learned = _load_learned()
    _record_mob(learned, class_name, info, _zone_name(pm, base))
    _save_learned(learned)
    _last_spawn.update(time=time.time(), actor=addr, **{"class": class_ptr})


def _summon_alias(pm, base, alias, forced_class):
    display, _, ids = MINIBOSS_TYPES[alias]
    names = [(forced_class, "forced")] if forced_class else candidate_class_names(alias)
    tried, stray = [], []
    for cname, source in names[:MAX_SUMMON_TRIES]:
        tried.append(cname)
        found = _summon(pm, base, cname)
        hits = [f for f in found if f[1] == cname.split(".")[-1]]
        if hits:
            _remember_success(pm, base, hits[0])
            return ([f"Summoned {display} with 'summon {cname}' ({source}); class saved for next time."]
                    + _info_lines(hits[0][3], ids))
        stray += [f[1] for f in found]
    if stray:
        raise MinibossError(f"a mob appeared ({', '.join(sorted(set(stray)))}) but not the class asked "
                            f"for ({', '.join(tried)}).")
    raise MinibossError(
        f"nothing spawned for {display} (tried: {', '.join(tried)}). Is UUU injected, so the console "
        f"and cheats are unlocked? If it is, those class names are wrong: run /miniboss learn in a "
        f"zone where a {display} exists, or pass the real class name: /miniboss {alias} <ClassName>. "
        f"/miniboss direct {alias} tries the direct spawn instead.")


# ---- finding the Blueprint class of a mob ---------------------------------------------------
_class_index = {"built": 0.0, "classes": {}, "world": 0, "complete": True,
                "all": None, "cdo": {}, "enums": {}, "disabled": False}
# classes  : Blueprint class name -> UClass address (this level)
# all      : EVERY class name (engine and Blueprint) -> (address, name index), or None until built
# cdo      : class address -> (default object address, its name index)
# enums    : enum name -> (address, name index)
RF_CLASS_DEFAULT_OBJECT = 0x10         # EObjectFlags::RF_ClassDefaultObject (UE 4.22)
META_CLASS_NAMES = ("Class", "BlueprintGeneratedClass", "Enum")
_name_cache = {}                       # FName comparison index -> text (never changes for a live object)
_cdo_verified = set()
_cdo_slot = {"offset": None}           # where UClass keeps its default object, learnt from the first probe


def _lower_priority():
    """Lowers the calling thread's priority (Windows) so a long memory scan never competes with the
    game for CPU. Returns the function that puts it back; a no-op where that is not possible."""
    try:
        import ctypes
        kernel = ctypes.windll.kernel32
        handle = kernel.GetCurrentThread()
        before = kernel.GetThreadPriority(handle)
        kernel.SetThreadPriority(handle, -1)          # THREAD_PRIORITY_BELOW_NORMAL
        return lambda: kernel.SetThreadPriority(handle, before)
    except Exception:
        return lambda: None


def _name_of(pm, name_index):
    if name_index not in _name_cache:
        _name_cache[name_index] = _R().resolve_fname(pm, name_index)
    return _name_cache[name_index]


def _blueprint_class_meta(pm):
    """Address of the UClass named BlueprintGeneratedClass (the class every Blueprint class is an
    instance of), or None. Only a fallback: it goes through the bridge's game-thread name scan."""
    R = _R()
    for addr in _bridge_find_objects_by_name(pm, "BlueprintGeneratedClass"):
        try:
            meta = pm.read_longlong(addr + 0x10)
            if meta and R.resolve_fname(pm, pm.read_int(meta + 0x18)) == "Class":
                return addr
        except Exception:
            continue
    return None


def build_class_index(pm, base, say):
    """One pass over the game's object table, read from Python (the game is not asked anything and
    never paused): the header of each object gives its class, its name and its flags. From that:
    every class (engine + Blueprint), each class's default object (the RF_ClassDefaultObject flag),
    and the enums - which is what the bridge's game-thread find_by_name used to be asked for.
    The three types needed to tell classes from other objects are found by NAME among the most
    common class pointers, so no scan is needed for that either. Cached; rebuilt when the level
    changes. Runs at a lowered thread priority."""
    R = _R()
    objects = R.get_gobjects_pointer_snapshot(pm, base)
    if not objects:
        raise MinibossError("could not read the game's object table")
    say(f"Reading {len(objects)} game objects to index the classes "
        f"(memory only - the game is not paused; first time per level, 10-30 seconds)...")
    started, complete = time.time(), True
    ptrs, flags, klass, names = array("Q"), array("I"), array("Q"), array("i")
    _state["building"] = True
    restore = _lower_priority()
    try:
        for n, ptr in enumerate(objects.values()):
            if not ptr:
                continue
            if n % 8192 == 0:
                if time.time() - started > CLASS_SCAN_BUDGET:
                    say("  (scan stopped early: time budget reached, the list may be incomplete)")
                    complete = False
                    break
                time.sleep(0)                      # let the game's threads go first
            try:
                raw = pm.read_bytes(ptr, CLASS_SCAN_HEADER)
            except Exception:
                continue
            flag, _, cls, name_index = struct.unpack_from("<IIQi", raw, 8)
            ptrs.append(ptr)
            flags.append(flag)
            klass.append(cls)
            names.append(name_index)

        meta = {}
        for cls, _ in Counter(klass).most_common(700):
            if not cls:
                continue
            try:
                label = _name_of(pm, pm.read_int(cls + 0x18))
            except Exception:
                continue
            if label in META_CLASS_NAMES and label not in meta:
                meta[label] = cls
            if len(meta) == len(META_CLASS_NAMES):
                break
        if "BlueprintGeneratedClass" not in meta:       # unexpected: use the (slow) bridge lookup once
            fallback = _blueprint_class_meta(pm)
            if not fallback:
                raise MinibossError("could not find the BlueprintGeneratedClass type - is a level loaded?")
            meta["BlueprintGeneratedClass"] = fallback

        blueprint, every, cdo, enums = {}, {}, {}, {}
        for ptr, flag, cls, name_index in zip(ptrs, flags, klass, names):
            if flag & RF_CLASS_DEFAULT_OBJECT and cls:
                cdo[cls] = (ptr, name_index)
            if cls == meta.get("BlueprintGeneratedClass"):
                label = _name_of(pm, name_index)
                if label:
                    blueprint[label] = ptr
                    every[label] = (ptr, name_index)
            elif cls == meta.get("Class"):
                label = _name_of(pm, name_index)
                if label:
                    every[label] = (ptr, name_index)
            elif cls == meta.get("Enum"):
                label = _name_of(pm, name_index)
                if label:
                    enums[label] = (ptr, name_index)
    finally:
        _state["building"] = False
        restore()
    ready = bool(meta.get("Class"))          # without the plain Class type the engine tables are not trusted
    _class_index.update(built=time.time(), classes=blueprint, world=_current_world(pm, base),
                        complete=complete, all=every if ready else None, cdo=cdo, enums=enums,
                        disabled=False)
    say(f"  indexed {len(blueprint)} Blueprint classes, {len(every)} classes in all, "
        f"{len(cdo)} default objects, {len(enums)} enums in {time.time() - started:.0f}s.")
    return blueprint


def _probe_cdo(pm, class_ptr):
    """(address, name index) of a class's default object found by looking inside the UClass itself for
    a pointer to an object of that class that carries RF_ClassDefaultObject. Used for classes that
    came into memory after the index was built (e.g. loaded by path)."""
    offsets = [_cdo_slot["offset"]] if _cdo_slot["offset"] else range(0x30, 0x300, 8)
    try:
        raw = pm.read_bytes(class_ptr, 0x300)
    except Exception:
        return None
    for off in offsets:
        pointer = struct.unpack_from("<Q", raw, off)[0]
        if pointer < 0x1000:                       # flags, indexes, counters: not an address
            continue
        try:
            header = pm.read_bytes(pointer, CLASS_SCAN_HEADER)
        except Exception:
            continue
        flag, _, cls, name_index = struct.unpack_from("<IIQi", header, 8)
        if cls == class_ptr and flag & RF_CLASS_DEFAULT_OBJECT:
            _cdo_slot["offset"] = off
            return pointer, name_index
    return None


def _index_find(pm, name):
    """Answer find_by_name from the object index, or None when the index cannot answer (and the
    slow bridge scan must be used). A class-like name that is simply not in a fresh, complete index
    answers [] - 'not loaded' - instead of freezing the game to find out."""
    base = _state.get("base")
    if not base or _state.get("building") or _class_index.get("disabled"):
        return None
    say = _state.get("say") or (lambda line: None)
    try:
        get_class_index(pm, base, say)
    except MinibossError:
        return None
    every = _class_index.get("all")
    if every is None:
        return None
    if name.startswith("Default__"):
        entry = every.get(name[len("Default__"):])
        if not entry:
            return [] if _class_index["complete"] else None
        found = _class_index["cdo"].get(entry[0]) or _probe_cdo(pm, entry[0])
        if not found:
            return None
        if name not in _cdo_verified:
            # the flag-based lookup is only trusted once the game itself names the object the same way
            if _name_of(pm, found[1]) != name:
                _class_index["disabled"] = True
                say("  (object index disagrees with the game about default objects - using the slow lookup)")
                return None
            _cdo_verified.add(name)
        _class_index["cdo"][entry[0]] = found
        return [found[0]]
    entry = _class_index["enums"].get(name) or every.get(name)
    if entry:
        try:
            if pm.read_int(entry[0] + 0x18) == entry[1]:
                return [entry[0]]
        except Exception:
            pass
        every.pop(name, None)                                      # freed or reused since: forget it
        _class_index["classes"].pop(name, None)
        return []
    return [] if _class_index["complete"] else None


def index_register(pm, class_name, class_ptr):
    """Adds a class that was just loaded (by path) to the index so it is found without a rebuild."""
    try:
        name_index = pm.read_int(class_ptr + 0x18)
    except Exception:
        return
    for key in [k for k in _slow_cache if k[1] == class_name]:
        del _slow_cache[key]                       # an earlier "not loaded" answer is no longer true
    if _class_index.get("all") is not None:
        _class_index["all"][class_name] = (class_ptr, name_index)
        _class_index["classes"][class_name] = class_ptr


def _current_world(pm, base):
    try:
        return pm.read_longlong(base + _R().OFFSETS["gworld"])
    except Exception:
        return 0


def get_class_index(pm, base, say, rescan=False):
    """The cached index of loaded Blueprint classes. Rebuilt when asked (rescan) AND whenever the
    level changed since it was built: which Blueprint classes are loaded depends on the level, so an
    index built in the hub or another mission never contains this mission's mobs."""
    stale = _class_index["classes"] and _class_index.get("world") != _current_world(pm, base)
    if rescan or stale or not _class_index["classes"]:
        return build_class_index(pm, base, say)
    return _class_index["classes"]


def discover_classes(pm, base, text, say, rescan=False, mobs_only=True):
    """[(class_name, address)] of loaded Blueprint classes whose name contains text, shortest name
    first. mobs_only drops helper classes (death effects, projectiles...) and anything that does
    not inherit from AMobCharacter."""
    R = _R()
    key = normalize(text)
    if not key:
        return []
    out = []
    for name, addr in get_class_index(pm, base, say, rescan).items():
        norm = normalize(name)
        if key not in norm:
            continue
        if mobs_only and (any(t in norm for t in NOT_MOB_TOKENS) or not R._is_mob_class(pm, addr)):
            continue
        out.append((name, addr))
    return sorted(out, key=lambda x: (len(x[0]), x[0]))


def class_from_live_mobs(pm, base, ids):
    """(class_addr, class_name) of a mob that is ALIVE in the current level and has one of the
    EntityTypes in ids, else None. Fast (only the level's actors) and needs no guessed names: the game
    itself says which Blueprint class the mob is. This is what makes /miniboss work in a zone where
    the mob you want is standing right there."""
    R = _R()
    is_mob = {}
    for addr, class_idx in _level_actors(pm, base).items():
        try:
            class_ptr = pm.read_longlong(addr + R.OFFSETS["uobject_class"])
        except Exception:
            continue
        if not class_ptr:
            continue
        if class_idx not in is_mob:
            is_mob[class_idx] = R._is_mob_class(pm, class_ptr)
        if not is_mob[class_idx]:
            continue
        try:
            etype = pm.read_uint(addr + R._MOB_ENTITY_TYPE_OFFSET)
        except Exception:
            continue
        if etype in ids:
            return class_ptr, R.resolve_fname(pm, class_idx) or f"class{class_idx}"
    return None


def class_from_live_mobs_by_name(pm, base, entity_name, ids):
    """Tolerant second pass for class_from_live_mobs, used when the strict one finds nothing: any
    live actor whose class name contains the mob's name (helper classes like death effects excluded)
    and whose EntityType, when readable, is one of ids. It does not depend on _is_mob_class, so it
    still works if that inheritance check is what fails in this build."""
    R = _R()
    key = normalize(entity_name)
    seen = {}
    for addr, class_idx in _level_actors(pm, base).items():
        if class_idx not in seen:
            name = R.resolve_fname(pm, class_idx) or ""
            norm = normalize(name)
            seen[class_idx] = name if key in norm and not any(t in norm for t in NOT_MOB_TOKENS) else None
        if not seen[class_idx]:
            continue
        try:
            class_ptr = pm.read_longlong(addr + R.OFFSETS["uobject_class"])
            etype = pm.read_uint(addr + R._MOB_ENTITY_TYPE_OFFSET)
        except Exception:
            continue
        if class_ptr and etype in ids:
            return class_ptr, seen[class_idx]
    return None


def _first_actor_of_class(actors, pm, class_ptr):
    R = _R()
    for addr in actors:
        try:
            if pm.read_longlong(addr + R.OFFSETS["uobject_class"]) == class_ptr:
                return addr
        except Exception:
            continue
    return 0


def _sample_report(pm, actor):
    """One line about a live mob: EntityType, flags byte and the class of its AI controller.
    Compare a mob the game spawned with one /miniboss spawned: a different controller class (or
    none) is the difference between a mob that fights and a statue."""
    if not actor:
        return "none found"
    R = _R()
    parts = []
    try:
        parts.append(f"EntityType={pm.read_uint(actor + R._MOB_ENTITY_TYPE_OFFSET)}")
        parts.append(f"flags=0x{_read_flags(pm, actor):02X}")
    except Exception as e:
        parts.append(f"(fields unreadable: {e})")
    try:
        out = _call_actor_function(pm, actor, "AIController")
        ai = struct.unpack_from("<Q", out, 0)[0]
        if ai:
            ai_class = pm.read_longlong(ai + R.OFFSETS["uobject_class"])
            name = R.resolve_fname(pm, pm.read_int(ai_class + R.OFFSETS["uobject_name"])) if ai_class else None
            parts.append(f"AI controller={name or hex(ai)}")
        else:
            parts.append("AI controller=none")
    except Exception as e:
        parts.append(f"AI controller=? ({e})")
    return ", ".join(parts)


def diag_lines(pm, base, text=""):
    """/miniboss diag [text]: what the command can see in the current zone - actor and class counts,
    how many classes pass the 'is a mob' check, and for classes matching <text> their parent chain."""
    R = _R()
    actors = _level_actors(pm, base)
    classes = {}
    for addr, class_idx in actors.items():
        try:
            class_ptr = pm.read_longlong(addr + R.OFFSETS["uobject_class"])
        except Exception:
            continue
        if class_ptr:
            rec = classes.setdefault(class_idx, [class_ptr, 0])
            rec[1] += 1
    mobs, others = [], []
    for class_idx, (class_ptr, n) in classes.items():
        name = R.resolve_fname(pm, class_idx) or f"class{class_idx}"
        (mobs if R._is_mob_class(pm, class_ptr) else others).append((name, n, class_ptr))
    lines = [f"diag [{_zone_name(pm, base)}]: {len(actors)} actors, {len(classes)} classes, "
             f"{len(mobs)} class(es) recognised as mobs."]
    lines.append("  mobs: " + (", ".join(f"{n}x{c}" for n, c, _ in sorted(mobs)[:25]) or "none"))
    key = normalize(text)
    if key:
        hits = [x for x in mobs + others if key in normalize(x[0])]
        if not hits:
            lines.append(f"  no live class contains '{text}'.")
        for name, n, class_ptr in hits[:8]:
            chain = []
            try:
                for cls in R.walk_super_chain(pm, class_ptr):
                    chain.append(R.resolve_fname(pm, pm.read_int(cls + R.OFFSETS["uobject_name"])) or "?")
            except Exception as e:
                chain.append(f"(chain read failed: {e})")
            lines.append(f"  {name} x{n} - recognised as mob: {any(name == m[0] for m in mobs)}; parents: "
                         + " > ".join(chain[:10]))
            lines.append("    sample actor: " + _sample_report(pm, _first_actor_of_class(actors, pm, class_ptr)))
    return lines


def resolve_mob_class(pm, base, alias, forced_class, say):
    """(class_addr, class_name, source, other_candidates). Raises MinibossError when nothing fits."""
    R = _R()
    display, entity_name, _ = MINIBOSS_TYPES[alias]
    if forced_class:
        addr = find_class(pm, forced_class)
        if not addr:
            raise MinibossError(f"no class named '{forced_class}' found (try /miniboss find <part of the name>)")
        return addr, forced_class, "forced", []
    _, _, ids = MINIBOSS_TYPES[alias]
    # A mob the GAME spawned and that is alive right now tells the real Blueprint class (the one
    # with the behaviour tree). Learned/guessed names can point at a legacy class that spawns fine
    # but never runs any AI, so the live mob comes first.
    live = class_from_live_mobs(pm, base, ids)
    if live:
        return live[0], live[1], "live mob", []
    for cname, source in candidate_class_names(alias):
        addr = find_class(pm, cname)
        if addr and R._is_mob_class(pm, addr):
            return addr, cname, source, []
    live = class_from_live_mobs_by_name(pm, base, entity_name, ids)
    if live:
        return live[0], live[1], "live mob", []
    found = discover_classes(pm, base, entity_name, say)
    if found:
        remember_paths(pm, found[:3])
        return found[0][1], found[0][0], "scan", [n for n, _ in found[1:6]]
    load_errors = []
    saved = class_from_saved_path(pm, base, entity_name, say, load_errors)
    if saved:
        return saved[0][1], saved[0][0], "loaded from saved path", [n for n, _ in saved[1:6]]
    if load_errors:
        raise MinibossError(f"a path is saved for {display} but loading it failed - {load_errors[0]}")
    raise MinibossError(
        f"no mob class found for {display}: none is alive in this zone, no loaded Blueprint class "
        f"matches the name, and no asset path is saved for it. Go to a zone where one appears and run "
        f"/miniboss {alias} there (its path is saved), or give the path yourself with "
        f"/miniboss load <AssetPath>, or look around with /miniboss find <part of the name>.")


# ---- making a spawned mob behave like one the game spawned (AI, EntityType, powerful flag) ----
# A mob created with BeginDeferredActorSpawnFromClass + FinishSpawningActor is only the bare class:
# no AI controller (so it stands still), EntityType left at "Undefined" and no powerful/event flag
# (no boss bar). The game's own spawner does these steps; we redo them here after the spawn.
EVENT_MASK_KEY = "event_flag_mask"      # in miniboss_classes.json: which bit(s) of mMobReplicatedFlags IsEventMob reads


def _actor_class(pm, actor):
    return pm.read_longlong(actor + _R().OFFSETS["uobject_class"])


def _call_actor_function(pm, actor, name, allowed_sizes=(0, 8)):
    """Calls a parameterless (or pointer-returning) UFunction on a mob through the protected bridge
    call. Refuses when the real parameter-struct size is not one of allowed_sizes."""
    R = _R()
    func = R._find_function_on_class(pm, _actor_class(pm, actor), name)
    if not func:
        raise MinibossError(f"{name} not found on this mob")
    size = pm.read_int(func + R.OFFSETS["ustruct_size"])
    if size not in allowed_sizes:
        raise MinibossError(f"{name} parameter struct is 0x{size:X} bytes, expected {[hex(x) for x in allowed_sizes]} "
                            f"- refusing to call it")
    return calldata(pm, actor, func, bytes(max(8, (size + 7) // 8 * 8)))


def _is_event(pm, actor):
    R = _R()
    func = R._find_function_on_class(pm, _actor_class(pm, actor), "IsEventMob")
    return R._call_bool_function(pm, actor, func) if func else None


def _read_flags(pm, actor):
    return pm.read_bytes(actor + _R()._MOB_REPLICATED_FLAGS_OFFSET, 1)[0]


def _write_flags(pm, actor, value):
    pm.write_bytes(actor + _R()._MOB_REPLICATED_FLAGS_OFFSET, bytes([value & 0xFF]), 1)


def apply_event_flag(pm, actor, mask):
    """Sets the powerful flag bit(s) on a mob and asks the game whether it now counts as one."""
    _write_flags(pm, actor, _read_flags(pm, actor) | mask)
    ev = _is_event(pm, actor)
    return [f"   powerful flag 0x{mask:02X} set -> IsEventMob = {ev}"
            + ("  (look for the boss bar)" if ev else "  (the game does not read that bit)")]


def post_spawn_setup(pm, actor, ids):
    """Steps after a spawn: EntityType, AI controller, powerful flag. Returns report lines."""
    R = _R()
    lines = []
    try:
        etype = pm.read_uint(actor + R._MOB_ENTITY_TYPE_OFFSET)
        if etype == 1 and ids:
            pm.write_uint(actor + R._MOB_ENTITY_TYPE_OFFSET, ids[0])
            lines.append(f"   EntityType was Undefined: set to {ids[0]} ({R.entity_type_name(ids[0])})")
    except Exception as e:
        lines.append(f"   could not set EntityType ({e})")
    try:
        _call_actor_function(pm, actor, "SpawnDefaultController")
        out = _call_actor_function(pm, actor, "AIController")
        ai = struct.unpack_from("<Q", out, 0)[0]
        lines.append(f"   AI controller: present ({hex(ai)})" if ai
                     else "   AI controller: still none - the mob will stand still")
    except MinibossError as e:
        lines.append(f"   AI setup skipped: {e}")
    mask = _load_learned().get(EVENT_MASK_KEY)
    if mask:
        try:
            lines += apply_event_flag(pm, actor, mask)
        except Exception as e:
            lines.append(f"   could not set the powerful flag ({e})")
    else:
        lines.append("   not marked as powerful yet: the flag bit is unknown - run /miniboss probe in a zone "
                     "with a real powerful mob (or /miniboss promote to try the bits one by one)")
    return lines


def promote_lines(pm, base, bit_text=""):
    """/miniboss promote [bit]: mark the last spawned mob as powerful. With no bit: use the known
    one, or try the 8 bits of mMobReplicatedFlags one at a time until IsEventMob turns True."""
    actor = _last_spawn["actor"]
    if not actor:
        return ["promote: spawn a mob first (/miniboss <type>), then run /miniboss promote."]
    try:
        original = _read_flags(pm, actor)
    except Exception:
        return ["promote: the last spawned mob is gone - spawn a new one first."]
    learned = _load_learned()
    bit_text = str(bit_text).strip()
    if bit_text:
        if not bit_text.isdigit() or int(bit_text) > 7:
            return ["Usage: /miniboss promote [bit 0-7]"]
        mask = 1 << int(bit_text)
        lines = apply_event_flag(pm, actor, mask)
        if _is_event(pm, actor):
            learned[EVENT_MASK_KEY] = mask
            _save_learned(learned)
            lines.append("   remembered: new spawns get this flag automatically.")
        else:
            _write_flags(pm, actor, original)
        return lines
    if learned.get(EVENT_MASK_KEY):
        return apply_event_flag(pm, actor, learned[EVENT_MASK_KEY])
    lines = [f"promote: flags byte is 0x{original:02X}; trying each bit..."]
    for bit in range(8):
        _write_flags(pm, actor, original | (1 << bit))
        if _is_event(pm, actor):
            learned[EVENT_MASK_KEY] = 1 << bit
            _save_learned(learned)
            lines.append(f"   bit {bit} (0x{1 << bit:02X}) makes IsEventMob True - remembered. Look for the boss bar.")
            return lines
        _write_flags(pm, actor, original)
    lines.append("   no single bit of mMobReplicatedFlags turns IsEventMob on: the game reads something "
                 "else (a tag?). Run /miniboss probe next to a real powerful mob and send me the output.")
    return lines


def probe_lines(pm, base):
    """/miniboss probe: for every mob alive in this zone print EntityType, the flags byte in binary
    and IsEventMob; then work out which bit(s) only powerful mobs have, and remember them."""
    R = _R()
    rows, is_mob = [], {}
    for addr, class_idx in _level_actors(pm, base).items():
        try:
            class_ptr = pm.read_longlong(addr + R.OFFSETS["uobject_class"])
        except Exception:
            continue
        if not class_ptr:
            continue
        if class_idx not in is_mob:
            is_mob[class_idx] = R._is_mob_class(pm, class_ptr)
        if not is_mob[class_idx]:
            continue
        info = R._evaluate_mob(pm, addr, class_ptr, {})
        if info:
            rows.append((R.resolve_fname(pm, class_idx) or f"class{class_idx}", info))
    lines = [f"probe [{_zone_name(pm, base)}]: {len(rows)} mob(s) alive."]
    for name, info in sorted(rows, key=lambda r: (not r[1]["event"], r[0]))[:40]:
        lines.append(f"  {name:<34} {R.entity_type_name(info['etype']):<22} flags={info['flags']:08b} "
                     f"IsEventMob={info['event']}")
    event = [i["flags"] for _, i in rows if i["event"]]
    normal = [i["flags"] for _, i in rows if i["event"] is False]
    if not event:
        lines.append("  no powerful mob alive here: go next to one (its boss bar is on screen) and run this again.")
        return lines
    common = 0xFF
    for f in event:
        common &= f
    seen_normal = 0
    for f in normal:
        seen_normal |= f
    candidate = common & ~seen_normal & 0xFF
    if candidate:
        learned = _load_learned()
        learned[EVENT_MASK_KEY] = candidate
        _save_learned(learned)
        lines.append(f"  bit(s) 0x{candidate:02X} are set on every powerful mob and on no ordinary one: "
                     f"remembered, new spawns get it automatically.")
    else:
        lines.append("  no flag bit separates powerful mobs from ordinary ones here: IsEventMob probably "
                     "reads something other than mMobReplicatedFlags. Send me this output.")
    return lines


# ---- creating a mini boss WITHOUT a reference: everything is read from the game's own classes ----
# /miniboss create <type|ClassName> [dry]
# No live mob, no learned file, no real powerful mob next to you. The game itself is asked:
#   * which Blueprint class: only classes found in the game's class table, ranked by what their own
#     default object says (does it name an AI controller class? does it auto-possess one?);
#   * which EntityType: the game's own EEntityType enum, checked against the reader's table;
#   * which flag bit makes a mob "powerful": the game's own IsEventMob is the judge - the bit is found
#     on the not-yet-finished actor by trying them (reflection hints first), before BeginPlay runs.
# Reflection offsets are for UE 4.22 and are CHECKED at run time against two values known from the
# Dumper-7 dump (EntityType = 0xBA0, mMobReplicatedFlags = 0xD37); when they disagree, reflection is
# not used and the report says so, instead of trusting wrong numbers.
UPROP_ELEMENT_SIZE = 0x34              # UProperty::ElementSize
UPROP_OFFSET = 0x44                    # UProperty::Offset_Internal
UBOOL_BYTES = 0x70                     # UBoolProperty: FieldSize, ByteOffset, ByteMask, FieldMask
UENUM_NAMES = 0x40                     # UEnum::Names (TArray<TPair<FName, int64>>, 16 bytes per entry)
ENUM_CANDIDATES = ("EEntityType", "EntityType")
ENTITY_PROPERTY = "EntityType"
FLAGS_PROPERTY = "mMobReplicatedFlags"
EVENT_FUNCTION = "IsEventMob"
EVENT_HINT_WORDS = ("event", "powerful")
REFLECT_FILE = "miniboss_reflect.txt"
AUTO_POSSESS_WILL_POSSESS = (2, 3)     # EAutoPossessAI::Spawned / PlacedInWorldOrSpawned


def _kind_of(pm, child, cache):
    """Name of the class of a UField: 'Function', 'BoolProperty', 'ObjectProperty', ..."""
    R = _R()
    meta = pm.read_longlong(child + R.OFFSETS["uobject_class"])
    if meta not in cache:
        cache[meta] = R.resolve_fname(pm, pm.read_int(meta + R.OFFSETS["uobject_name"])) or "?"
    return cache[meta]


def reflect_class(pm, class_ptr, stop_at=None):
    """Every member (properties and functions) along the class chain, as dicts:
    {class, name, kind, offset, size, bool=(byte_offset, byte_mask, field_mask), params}.
    stop_at(row) -> True ends the walk early (used by find_property)."""
    R = _R()
    rows, kinds = [], {}
    for cls in R.walk_super_chain(pm, class_ptr):
        cname = R.resolve_fname(pm, pm.read_int(cls + R.OFFSETS["uobject_name"])) or "?"
        for child in R.walk_children(pm, cls, max_count=4096):
            name = R.get_ufunction_name(pm, child)
            if not name:
                continue
            kind = _kind_of(pm, child, kinds)
            row = {"class": cname, "name": name, "kind": kind}
            if kind == "Function":
                row["params"] = pm.read_int(child + R.OFFSETS["ustruct_size"])
            elif kind.endswith("Property"):
                row["offset"] = pm.read_int(child + UPROP_OFFSET)
                row["size"] = pm.read_int(child + UPROP_ELEMENT_SIZE)
                if kind == "BoolProperty":
                    raw = pm.read_bytes(child + UBOOL_BYTES, 4)
                    row["bool"] = (raw[1], raw[2], raw[3])
            rows.append(row)
            if stop_at and stop_at(row):
                return rows
    return rows


def _name_matches(candidate, wanted):
    """Property names in this game sometimes carry a leading 'm' (mMobReplicatedFlags), so
    EntityType / mEntityType and mMobReplicatedFlags / MobReplicatedFlags are the same field."""
    c, w = normalize(candidate), normalize(wanted)
    return c == w or c == "m" + w or "m" + c == w


def find_property(pm, class_ptr, name):
    """The first property called name on the class chain, or None (stops reading as soon as found)."""
    hit = []

    def stop(row):
        if _name_matches(row["name"], name) and "offset" in row:
            hit.append(row)
            return True
        return False

    reflect_class(pm, class_ptr, stop_at=stop)
    return hit[0] if hit else None


def verify_reflection(pm, class_ptr):
    """(ok, message): reflection is trusted only if it reproduces two offsets known from the dump."""
    R = _R()
    entity, flags = find_property(pm, class_ptr, ENTITY_PROPERTY), find_property(pm, class_ptr, FLAGS_PROPERTY)
    want = (R._MOB_ENTITY_TYPE_OFFSET, R._MOB_REPLICATED_FLAGS_OFFSET)
    got = (entity["offset"] if entity else None, flags["offset"] if flags else None)
    if got == want:
        return True, f"reflection verified (EntityType @0x{want[0]:X}, flags @0x{want[1]:X} match the dump)"
    shown = tuple(hex(g) if g is not None else "missing" for g in got)
    return False, (f"reflection NOT trusted: found EntityType/flags at {shown[0]}/{shown[1]}, "
                   f"the dump says 0x{want[0]:X}/0x{want[1]:X} - using the built-in values instead")


def class_default_object(pm, class_name):
    found = find_objects_by_name(pm, "Default__" + class_name)
    return found[0] if found else 0


def class_evidence(pm, class_name, class_ptr, trusted):
    """What the class's own default object says about AI. A mob class that names an AI controller
    and auto-possesses it is a real, fighting mob; one that does neither spawns as a statue."""
    ev = {"cdo": 0, "ai_class": None, "auto_possess": None, "score": 0, "notes": []}
    if not trusted:
        ev["notes"].append("no reflection")
        return ev
    cdo = class_default_object(pm, class_name)
    ev["cdo"] = cdo
    if not cdo:
        ev["notes"].append("no default object loaded")
        return ev
    prop = find_property(pm, class_ptr, "AIControllerClass")
    if prop:
        ev["ai_class"] = pm.read_longlong(cdo + prop["offset"])
    prop = find_property(pm, class_ptr, "AutoPossessAI")
    if prop:
        ev["auto_possess"] = pm.read_bytes(cdo + prop["offset"], 1)[0]
    ev["score"] = (2 if ev["ai_class"] else 0) + (2 if ev["auto_possess"] in AUTO_POSSESS_WILL_POSSESS else 0)
    return ev


def _evidence_text(ev):
    if ev["notes"]:
        return ev["notes"][0]
    ai = "AI controller named" if ev["ai_class"] else "no AI controller class"
    ap = {0: "never possessed", 1: "possessed when placed", 2: "possessed when spawned",
          3: "possessed placed or spawned"}.get(ev["auto_possess"], "auto-possess unknown")
    return f"{ai}, {ap}"


def game_entity_ids(pm, entity_name):
    """EntityType value(s) named entity_name in the game's own EEntityType enum, or None when the
    enum cannot be read (or does not agree with the reader's table, which means a different layout)."""
    R = _R()
    key = normalize(entity_name)
    for enum_name in ENUM_CANDIDATES:
        for obj in find_objects_by_name(pm, enum_name):
            try:
                meta = pm.read_longlong(obj + R.OFFSETS["uobject_class"])
                if R.resolve_fname(pm, pm.read_int(meta + R.OFFSETS["uobject_name"])) != "Enum":
                    continue
                data = pm.read_longlong(obj + UENUM_NAMES)
                count = pm.read_int(obj + UENUM_NAMES + 8)
                if not data or not 0 < count < 20000:
                    continue
                raw = pm.read_bytes(data, count * 16)
            except Exception:
                continue
            values = {}
            for i in range(count):
                name_index, _, value = struct.unpack_from("<iiq", raw, i * 16)
                name = R.resolve_fname(pm, name_index)
                if name:
                    values[value] = name.split("::")[-1]
            known = getattr(R, "ENTITY_TYPE_NAMES", {})
            both = [v for v in values if v in known]
            agree = [v for v in both if normalize(values[v]) == normalize(known[v])]
            if len(both) < 20 or len(agree) < 0.9 * len(both):
                continue                      # not the entity enum, or a layout this code misreads
            return [v for v, n in values.items() if normalize(n) == key]
    return None


def _is_event_protected(pm, actor):
    """IsEventMob through the protected bridge call (usable on an actor that is not finished yet).
    True/False, or None when the function is missing or is not a plain bool getter."""
    R = _R()
    func = R._find_function_on_class(pm, _actor_class(pm, actor), EVENT_FUNCTION)
    if not func or pm.read_int(func + R.OFFSETS["ustruct_size"]) != 1:
        return None
    return bool(calldata(pm, actor, func, bytes(8))[0])


def _event_state(pm, actor):
    state = _is_event_protected(pm, actor)
    return state if state is not None else _is_event(pm, actor)


def event_mask_hints(pm, class_ptr):
    """Masks of bool properties on the flags byte whose name sounds like 'event'/'powerful'."""
    R = _R()
    hints = []
    for row in reflect_class(pm, class_ptr):
        if row.get("kind") != "BoolProperty" or not any(w in row["name"].lower() for w in EVENT_HINT_WORDS):
            continue
        byte_offset, _, field_mask = row["bool"]
        if row["offset"] + byte_offset == R._MOB_REPLICATED_FLAGS_OFFSET and field_mask and field_mask not in hints:
            hints.append(field_mask)
    return hints


def find_event_mask(pm, actor, hints=()):
    """Which bit of the flags byte makes the game's IsEventMob answer True, found on this very actor.
    Returns (mask, how) or (None, reason). Leaves the flags byte as it found it unless it succeeds."""
    original = _read_flags(pm, actor)
    if _event_state(pm, actor):
        return None, "the game already reports this mob as powerful"
    tries = list(hints) + [1 << b for b in range(8) if (1 << b) not in hints]
    for mask in tries:
        _write_flags(pm, actor, original | mask)
        if _event_state(pm, actor):
            return mask, "reflection hint" if mask in hints else "tried the bits against IsEventMob"
        _write_flags(pm, actor, original)
    return None, "no single bit of the flags byte makes IsEventMob true"


def _evaluate_protected(pm, actor):
    """EntityType, flags byte and IsEventMob of a mob, WITHOUT the foreign-thread call the reader's
    _evaluate_mob makes: the first two are plain memory reads, IsEventMob goes through the bridge."""
    R = _R()
    try:
        return {"etype": pm.read_uint(actor + R._MOB_ENTITY_TYPE_OFFSET),
                "flags": pm.read_bytes(actor + R._MOB_REPLICATED_FLAGS_OFFSET, 1)[0],
                "event": _event_state(pm, actor)}
    except Exception:
        return None


def _create_pre_finish(pm, ids, hints, notes):
    """before_finish callback for the reference-free path: EntityType, then the powerful bit, both on
    the unfinished actor so that BeginPlay builds the AI and the boss bar around them."""
    def run(actor):
        R = _R()
        try:
            if ids and pm.read_uint(actor + R._MOB_ENTITY_TYPE_OFFSET) == 1:
                pm.write_uint(actor + R._MOB_ENTITY_TYPE_OFFSET, ids[0])
                notes.append(f"EntityType set to {ids[0]} ({R.entity_type_name(ids[0])})")
        except Exception as e:
            notes.append(f"could not set EntityType ({e})")
        learned = _load_learned()
        mask = learned.get(EVENT_MASK_KEY)
        try:
            if mask:
                _write_flags(pm, actor, _read_flags(pm, actor) | mask)
                notes.append(f"powerful bit 0x{mask:02X} applied (found earlier by asking the game)")
                return
            mask, how = find_event_mask(pm, actor, hints)
            if mask:
                learned[EVENT_MASK_KEY] = mask
                _save_learned(learned)
                notes.append(f"powerful bit 0x{mask:02X} found on this mob ({how}) and remembered")
            else:
                notes.append(f"powerful bit not set: {how}")
        except Exception as e:
            notes.append(f"powerful bit step failed ({e})")
    return run


def class_asset_path(pm, class_ptr):
    """'/Game/Folder/BP_Mob.BP_Mob_C' for a loaded Blueprint class: its package name + its own name."""
    R = _R()
    outer = pm.read_longlong(class_ptr + R.OFFSETS["uobject_outer"])
    cname = R.resolve_fname(pm, pm.read_int(class_ptr + R.OFFSETS["uobject_name"]))
    pname = R.resolve_fname(pm, pm.read_int(outer + R.OFFSETS["uobject_name"])) if outer else None
    if not (cname and pname and pname.startswith("/")):
        return None
    return f"{pname}.{cname}"


def remember_paths(pm, pairs):
    """Saves the asset path of each (class_name, class_addr) so that class can be loaded by path in a
    later session, even in a level that never loads it. Never raises: this is a convenience."""
    try:
        learned = _load_learned()
        paths = learned.setdefault(PATHS_KEY, {})
        changed = False
        for name, addr in pairs:
            path = class_asset_path(pm, addr)
            if path and paths.get(name) != path:
                paths[name] = path
                changed = True
        if changed:
            _save_learned(learned)
    except Exception:
        pass


def function_layout(pm, func):
    """{parameter name: (offset, size)} read from the UFunction's own property list - the layout is
    taken from the game, not assumed."""
    R = _R()
    out, kinds = {}, {}
    for child in R.walk_children(pm, func, max_count=64):
        name = R.get_ufunction_name(pm, child)
        if name and _kind_of(pm, child, kinds).endswith("Property"):
            out[name] = (pm.read_int(child + UPROP_OFFSET), pm.read_int(child + UPROP_ELEMENT_SIZE))
    return out


def _game_string(pm, text):
    """(address, element count) of text as an FString buffer inside the game's memory (cached)."""
    if text not in _fstring_cache:
        raw, count = encode_fstring(text)
        addr = pm.allocate(len(raw))
        pm.write_bytes(addr, raw, len(raw))
        _fstring_cache[text] = (addr, count)
    return _fstring_cache[text]


def load_class_by_path(pm, base, path):
    """Loads a Blueprint class from its asset path ('/Game/.../BP_X.BP_X_C') with the game's own
    blocking loader and returns the UClass address. This is what makes a mob creatable from a level
    that never loaded it. Two protected calls: Conv_StringToName builds the FName for the path,
    LoadClassAsset_Blocking loads it. Both layouts come from the functions themselves and the call is
    refused if they are not what is expected."""
    R = _R()
    path = path.strip()
    if not path.startswith("/") or "." not in path.rsplit("/", 1)[-1]:
        raise MinibossError("an asset path looks like /Game/Folder/BP_Name.BP_Name_C (package, a dot, the class)")
    pawn, err = R.get_pawn(pm, base)
    if not pawn:
        raise MinibossError(f"no player character ({err}) - wait until the level has finished loading")

    cdo, f_name, size_n = _find_static_function(pm, STRING_LIBRARY, STRING_TO_NAME, ANY_PARAM_SIZES)
    raw_lay = function_layout(pm, f_name)
    lay = {k.lower(): v for k, v in raw_lay.items()}       # the game spells it 'inString'; compare without case
    if "instring" not in lay or "returnvalue" not in lay or lay["instring"][1] != 0x10 or lay["returnvalue"][1] != 0x8:
        raise MinibossError(f"{STRING_TO_NAME} has an unexpected layout {raw_lay} - refusing to call it")
    addr, count = _game_string(pm, path)
    buf = bytearray(size_n)
    struct.pack_into("<Q", buf, lay["instring"][0], addr)
    struct.pack_into("<ii", buf, lay["instring"][0] + 8, count, count)
    out = calldata(pm, cdo, f_name, bytes(buf))
    name_index, name_number = struct.unpack_from("<ii", out, lay["returnvalue"][0])
    if not name_index:
        raise MinibossError("the game returned no name for that path")

    cdo2, f_load, size_l = _find_static_function(pm, LOAD_LIBRARY, LOAD_FUNCTION, ANY_PARAM_SIZES)
    raw_lay2 = function_layout(pm, f_load)
    lay2 = {k.lower(): v for k, v in raw_lay2.items()}
    if "asset" not in lay2 or "returnvalue" not in lay2 or lay2["asset"][1] != SOFT_CLASS_PTR_SIZE:
        raise MinibossError(
            f"{LOAD_FUNCTION} takes {raw_lay2} - the soft class pointer is not the expected 0x{SOFT_CLASS_PTR_SIZE:X} "
            f"bytes, refusing to call it (wrong layout could corrupt memory). Send me this line.")
    buf = bytearray(size_l)
    a = lay2["asset"][0]
    struct.pack_into("<ii", buf, a, -1, 0)                     # FWeakObjectPtr: not resolved yet
    struct.pack_into("<i", buf, a + 8, 0)                      # TagAtLastTest
    struct.pack_into("<ii", buf, a + 0x10, name_index, name_number)   # FSoftObjectPath::AssetPathName
    # SubPathString (FString at +0x18) stays empty: all zero
    out = calldata(pm, cdo2, f_load, bytes(buf))
    class_ptr = read_return_pointer(out, lay2["returnvalue"][0])
    if not class_ptr:
        raise MinibossError(f"the game could not load '{path}' (wrong path, or no such asset in this build)")
    index_register(pm, path.rsplit(".", 1)[-1], class_ptr)
    return class_ptr


def class_from_saved_path(pm, base, entity_name, say, errors=None):
    """[(class_name, class_addr)] for the saved asset paths whose class name matches entity_name, loading
    the class when it is not in memory. Empty when nothing is saved for that mob."""
    R = _R()
    key = normalize(entity_name)
    paths = _load_learned().get(PATHS_KEY, {})
    out = []
    for cname, path in sorted(paths.items(), key=lambda kv: (len(kv[0]), kv[0])):
        norm = normalize(cname)
        if key not in norm or any(t in norm for t in NOT_MOB_TOKENS):
            continue
        addr = find_class(pm, cname)
        if not addr:
            say(f"{cname} is not loaded - loading it from its saved path {path} ...")
            try:
                addr = load_class_by_path(pm, base, path)
            except MinibossError as e:
                say(f"  {e}")
                if errors is not None:
                    errors.append(f"{cname}: {e}")
                continue
        if R._is_mob_class(pm, addr):
            out.append((cname, addr))
    return out


def load_lines(pm, base, path, say):
    """/miniboss load <AssetPath>: load a class by path, remember it. Spawns nothing."""
    if not path:
        return ["Usage: /miniboss load /Game/Folder/BP_Name.BP_Name_C   (loads the class into memory and saves "
                "the path; spawns nothing)"]
    R = _R()
    name = path.rsplit(".", 1)[-1]
    already = find_class(pm, name)
    class_ptr = already or load_class_by_path(pm, base, path)
    lines = [f"{name}: " + ("already loaded" if already else "loaded") + f" at {hex(class_ptr)}."]
    lines.append(f"  inherits from MobCharacter: {R._is_mob_class(pm, class_ptr)}")
    remember_paths(pm, [(name, class_ptr)])
    lines.append(f"  path saved in {LEARNED_FILE}; /miniboss create / <type> will now find it in any level.")
    return lines


def paths_lines(pm, base, text, say):
    """/miniboss paths [type|text]: saved asset paths; with text, also saves those of the loaded classes
    that match it (run it in a zone where the mob is loaded - that is how a path is learned)."""
    lines = []
    if text:
        alias, _ = resolve_alias(text)
        entity = MINIBOSS_TYPES[alias][1] if alias else text
        found = discover_classes(pm, base, entity, say, mobs_only=False)
        remember_paths(pm, found)
        lines.append(f"paths [{text}]: {len(found)} loaded class(es) matched; their paths were saved.")
    paths = _load_learned().get(PATHS_KEY, {})
    if not paths:
        return lines + ["No asset path saved yet. Spawn or /miniboss paths <type> in a zone where the mob is "
                        "loaded, or give one with /miniboss load <AssetPath>."]
    shown = [(c, p) for c, p in sorted(paths.items()) if not text or normalize(text) in normalize(c)
             or normalize(text) in normalize(p)]
    lines.append(f"{len(paths)} path(s) saved" + (f", {len(shown)} matching:" if text else ":"))
    lines += [f"  {c}: {p}" for c, p in shown[:40]]
    return lines


def _pick_class(pm, base, target, say):
    """(display, entity_name, ids, [(name, addr)] ranked-candidates-as-found, forced?) for a type
    alias or an exact class name. Only the game's own class table is used."""
    if "_" in target or target.endswith("_C"):
        # something like BP_Enderman_C is a class name, not a typo of a type: an exact class wins
        exact = find_class(pm, target)
        if exact:
            return target, None, [], [(target, exact)], True
    alias, err = resolve_alias(target)
    if alias:
        display, entity_name, ids = MINIBOSS_TYPES[alias]
        found = discover_classes(pm, base, entity_name, say)
        load_errors = []
        if not found:
            found = class_from_saved_path(pm, base, entity_name, say, load_errors)   # not loaded: load it by its saved path
        if not found and load_errors:
            raise MinibossError(f"a path is saved for {display} but loading it failed - {load_errors[0]}")
        if not found:
            raise MinibossError(
                f"no mob class named like {display} is loaded in this session, and no asset path is saved for "
                f"it. Either enter a level that uses it once (its path is then saved automatically), or load it "
                f"by path with /miniboss load <AssetPath>. Look around with /miniboss find <part of the name>.")
        remember_paths(pm, found[:3])
        return display, entity_name, list(ids), found, False
    addr = find_class(pm, target)
    if not addr:
        raise MinibossError(f"{err} (and no class is named exactly '{target}')")
    return target, None, [], [(target, addr)], True


# ---- /miniboss create random, /miniboss preload ---------------------------------------------
# The six enchanted Endersent variants are left out of the draw for now; every other type is in.
RANDOM_EXCLUDE = frozenset({"endersentthornblight", "endersentvoidstrike", "endersentwritherot",
                            "endersenteverfire", "endersentsurgefiend", "endersentdeadeye"})
_last_random = {"alias": None}


def random_pool():
    return [a for a in MINIBOSS_TYPES if a not in RANDOM_EXCLUDE]


def ready_types(pm, base, say):
    """Types of the pool whose mob class is in memory RIGHT NOW: creating one needs no asset load,
    so it cannot cause the loading hitch."""
    return [a for a in random_pool() if discover_classes(pm, base, MINIBOSS_TYPES[a][1], say)]


def loadable_types(pm):
    """Types of the pool that are not loaded but have an asset path saved (a blocking load is needed)."""
    paths = _load_learned().get(PATHS_KEY, {})
    out = []
    for alias in random_pool():
        key = normalize(MINIBOSS_TYPES[alias][1])
        if any(key in normalize(n) and not any(t in normalize(n) for t in NOT_MOB_TOKENS) for n in paths):
            out.append(alias)
    return out


def random_pick(pm, base, allow_load, say):
    """(alias, how_many_candidates, kind). By default only types that are already loaded - the draw can
    never trigger a load; with allow_load the types that need loading by saved path are in it too."""
    ready = ready_types(pm, base, say)
    candidates, kind = ready, "already loaded"
    if allow_load:
        loadable = [a for a in loadable_types(pm) if a not in ready]
        candidates, kind = ready + loadable, f"{len(ready)} loaded + {len(loadable)} to load"
    if not candidates:
        raise MinibossError(
            "no mini boss class is loaded right now, so a random draw would have to load one (and freeze the "
            "game while it does). Run /miniboss preload at a quiet moment (start of a mission), or draw with "
            "/miniboss create random load and accept one short freeze.")
    choices = [a for a in candidates if a != _last_random["alias"]] or candidates
    alias = random.choice(choices)
    _last_random["alias"] = alias
    return alias, len(candidates), kind


def preload_lines(pm, base, text, say):
    """/miniboss preload [type|all]: loads the mob classes that are saved by path but not in memory,
    NOW - the blocking load freezes the game for a moment, so do it when that does not matter."""
    word = normalize(text)
    if word in ("", "all", "random"):
        aliases = random_pool()
    else:
        alias, err = resolve_alias(text)
        if err:
            return [err]
        aliases = [alias]
    lines = ["preload: loading freezes the game for a moment per class; do it when that is harmless. "
             "Loaded classes are not protected from the game's garbage collector and a level change "
             "unloads them again, so create soon after."]
    for alias in aliases:
        display, entity_name, _ = MINIBOSS_TYPES[alias]
        if discover_classes(pm, base, entity_name, say):
            lines.append(f"  {display}: already loaded")
            continue
        before, errors = len(_perf["calls"]), []
        found = class_from_saved_path(pm, base, entity_name, say, errors)
        froze = sum(ms for _, ms, kind in _perf["calls"][before:] if kind == "game")
        if found:
            lines.append(f"  {display}: loaded {found[0][0]} (game thread busy {froze:.0f} ms)")
        elif errors:
            lines.append(f"  {display}: load failed - {errors[0]}")
        else:
            lines.append(f"  {display}: no saved path - enter a level that uses it once (its path is then saved)")
    return lines + perf_lines()


def anchor_location(pm, base, chest_event):
    """(x, y, z) of the chest recorded by the client, after checking it still reads like a chest/supply
    station (UE4 reuses addresses, so a stale one could now be anything) and has a sane position."""
    R = _R()
    if not chest_event:
        raise MinibossError("no chest opened yet this run - open a chest first")
    addr = chest_event.get("actor_addr")
    try:
        class_ptr = pm.read_longlong(addr + R.OFFSETS["uobject_class"])
        cname = R.resolve_fname(pm, pm.read_int(class_ptr + R.OFFSETS["uobject_name"])) or ""
    except Exception:
        cname = ""
    if not any(k in cname.lower() for k in ("chest", "supply")):
        raise MinibossError(f"the recorded chest actor no longer reads like a chest (class '{cname}')")
    loc = R.get_actor_location(pm, addr)
    if loc is None or any(v != v or abs(v) > 1.0e7 for v in loc) or not any(loc):
        raise MinibossError("could not read a sane position for that chest")
    return loc


def player_location(pm, base):
    R = _R()
    pawn, err = R.get_pawn(pm, base)
    if not pawn:
        raise MinibossError(f"no player character ({err})")
    loc = R.get_actor_location(pm, pawn)
    if loc is None:
        raise MinibossError("could not read the player's position")
    return loc


def create_lines(pm, base, target, option, say, anchor=None, anchor_note=""):
    """/miniboss create <type|ClassName> [dry]"""
    R = _R()
    if not target:
        return ["Usage: /miniboss create <type|ClassName|random> [dry]   (dry = only show what would be done)"]
    if normalize(target) == "random":
        words = [w.lower() for w in (option or "").split()]
        alias, count, kind = random_pick(pm, base, allow_load=any(w in ("load", "any", "all") for w in words), say=say)
        head = (f"random: drew {MINIBOSS_TYPES[alias][0]} among {count} type(s) ({kind}) "
                f"of {len(random_pool())}.")
        return [head] + create_lines(pm, base, alias, "dry" if "dry" in words else "", say, anchor, anchor_note)
    dry = normalize(option) == "dry"
    display, entity_name, ids, found, forced = _pick_class(pm, base, target, say)

    trusted, why = verify_reflection(pm, found[0][1])
    lines = [f"create {display}: {why}."]
    ranked = sorted(((class_evidence(pm, n, a, trusted), n, a) for n, a in found[:8]),
                    key=lambda t: (-t[0]["score"], len(t[1]), t[1]))
    for ev, name, _ in ranked[:6]:
        lines.append(f"  class {name}: {_evidence_text(ev)}")
    ev, class_name, class_addr = ranked[0]
    lines.append(f"  -> using {class_name}" + (" (named by you)" if forced else
                 f" (best of {len(ranked)} checked in the game's class table)"))

    if entity_name:
        game_ids = None
        try:
            game_ids = game_entity_ids(pm, entity_name)
        except Exception:
            game_ids = None
        if game_ids:
            if sorted(game_ids) != sorted(ids):
                lines.append(f"  EntityType: the game's enum says {game_ids}, the built-in table says {ids} - using the game's")
            else:
                lines.append(f"  EntityType {game_ids[0]}: confirmed by the game's own enum")
            ids = game_ids
        else:
            lines.append(f"  EntityType {ids[0] if ids else '?'}: from the built-in table (the game's enum could not be read)")
    else:
        lines.append("  EntityType: left to the class (you named a class, not a type)")

    hints = event_mask_hints(pm, class_addr) if trusted else []
    learned_mask = _load_learned().get(EVENT_MASK_KEY)
    if learned_mask:
        lines.append(f"  powerful bit: 0x{learned_mask:02X}, found earlier by asking the game's IsEventMob")
    else:
        lines.append("  powerful bit: will be found on the new mob by asking the game's IsEventMob"
                     + (f" (reflection hints: {', '.join(f'0x{h:02X}' for h in hints)})" if hints else ""))
    if dry:
        lines.append("  dry run: nothing was spawned.")
        return lines
    if time.time() - _last_spawn["time"] < SPAWN_COOLDOWN:
        return ["Slow down - one spawn every couple of seconds."]

    notes = []
    group = spawn_group(pm, base, class_addr, ids,
                        lambda i: _create_pre_finish(pm, ids, hints, notes if i == 0 else []), anchor)
    actor, where = group[0]
    _last_spawn.update(time=time.time(), actor=actor, **{"class": class_addr})
    if anchor_note:
        lines.append(f"  {anchor_note}")
    lines.append(f"Spawned {display} via {class_name} (no reference mob) at X={where[0]:.0f} Y={where[1]:.0f} Z={where[2]:.0f}.")
    lines += [f"   {n}" for n in notes]
    lines += post_spawn_setup(pm, actor, ids)
    lines += group_extra_lines(pm, group, ids, display)
    info = _evaluate_protected(pm, actor)
    lines += _info_lines(info, ids) if info else ["   spawned actor does not read like a mob (no EntityType)."]
    return lines + perf_lines()


def reflect_lines(pm, base, target, text, say):
    """/miniboss reflect <type|ClassName|last> [filter]: members of a class chain as the game declares
    them. The full list goes to miniboss_reflect.txt, the console shows the lines matching <filter>."""
    if not target:
        return ["Usage: /miniboss reflect <type|ClassName|last> [filter]   e.g. /miniboss reflect enderman event"]
    if normalize(target) == "last":
        if not _last_spawn["class"]:
            return ["reflect: nothing spawned yet."]
        class_ptr, label = _last_spawn["class"], "last spawned class"
    else:
        _, _, _, found, _ = _pick_class(pm, base, target, say)
        label, class_ptr = found[0]
    rows = reflect_class(pm, class_ptr)
    trusted, why = verify_reflection(pm, class_ptr)

    def fmt(r):
        if r["kind"] == "Function":
            return f"{r['class']}.{r['name']}()  params=0x{r['params']:X}"
        extra = f" bool(byte {r['bool'][0]}, mask 0x{r['bool'][2]:02X})" if "bool" in r else ""
        return f"{r['class']}.{r['name']}  {r['kind']} @0x{r['offset']:X} size={r['size']}{extra}" if "offset" in r \
            else f"{r['class']}.{r['name']}  {r['kind']}"
    with open(REFLECT_FILE, "w", encoding="utf-8") as f:
        f.write(f"# {label}: {len(rows)} members. {why}\n")
        f.write("\n".join(fmt(r) for r in rows) + "\n")
    key = (text or "").lower()
    shown = [fmt(r) for r in rows if key in r["name"].lower()] if key else []
    lines = [f"reflect {label}: {len(rows)} members, {why}.", f"  full list written to {REFLECT_FILE}"]
    if key:
        lines.append(f"  {len(shown)} match(es) for '{text}':")
        lines += [f"    {x}" for x in shown[:40]] + ([f"    ... {len(shown) - 40} more in the file"] if len(shown) > 40 else [])
    else:
        lines.append("  add a filter word to list matches here, e.g. /miniboss reflect <type> event")
    return lines


# ---- subcommands ----------------------------------------------------------------------------
def _list_lines():
    learned = _load_learned()
    lines = ["Mini boss types (use /miniboss <type>):"]
    for alias, (display, _, ids) in MINIBOSS_TYPES.items():
        known = [c for c, src in candidate_class_names(alias, learned) if src == "learned"]
        state = f"learned class: {known[0]}" if known else "class not learned yet (will guess, then scan)"
        lines.append(f"  {alias:<24} {display}  [{state}]")
    lines.append("From the game's classes alone (no mob needed nearby): /miniboss create <type|random> [dry] | reflect <type> [filter]")
    lines.append("Avoiding lag: /miniboss preload [type|all] loads classes at a moment you choose; create random only draws loaded ones (add 'load' to include the others)")
    lines.append("Placed on a known floor: /miniboss atchest <type|random> [dry] (above the last chest you opened) | zone [now] (the zone's own mini boss)")
    lines.append("Class not loaded in this level? /miniboss load <AssetPath> | paths [type]  (paths are saved whenever a mob's class is seen)")
    lines.append("Other: /miniboss find <text> | learn | diag | probe | promote | functions | class <ClassName> | console <type>")
    return lines


def learn_live_mobs(pm, base, max_mobs=80):
    R = _R()
    snapshot = _level_actors(pm, base)
    zone = _zone_name(pm, base)
    learned = _load_learned()
    mob_cache, func_cache, counted, fresh = {}, {}, 0, []
    for addr, class_idx in snapshot.items():
        if counted >= max_mobs:
            break
        try:
            class_ptr = pm.read_longlong(addr + R.OFFSETS["uobject_class"])
        except Exception:
            continue
        if not class_ptr:
            continue
        if class_idx not in mob_cache:
            mob_cache[class_idx] = R._is_mob_class(pm, class_ptr)
        if not mob_cache[class_idx]:
            continue
        info = R._evaluate_mob(pm, addr, class_ptr, func_cache)
        if not info:
            continue
        counted += 1
        cname = R.resolve_fname(pm, class_idx) or f"class{class_idx}"
        fresh += _record_mob(learned, cname, info, zone)
        try:
            path = class_asset_path(pm, class_ptr)
            if path:
                learned.setdefault(PATHS_KEY, {})[cname] = path
        except Exception:
            pass
    _save_learned(learned)
    lines = [f"learn [{zone}]: looked at {counted} mob(s); {len(learned['classes'])} class(es) known "
             f"(saved to {LEARNED_FILE})."]
    lines += [f"  new: {x}" for x in fresh[:25]] or ["  nothing new in this zone."]
    return lines


def find_lines(pm, base, text, say, rescan=False):
    if not text:
        return ["Usage: /miniboss find <part of a class name> [rescan]   e.g. /miniboss find evoker"]
    R = _R()
    every = discover_classes(pm, base, text, say, rescan=rescan, mobs_only=False)
    note = []
    if not every:
        alias, _ = resolve_alias(text)
        if alias and normalize(text) != alias:     # looks like a typo of a known mini boss name
            fixed = MINIBOSS_TYPES[alias][1]
            note = [f"'{text}' looks like a typo of '{fixed}' - searching that instead."]
            text = fixed
            every = discover_classes(pm, base, text, say, rescan=rescan, mobs_only=False)
    if not every:
        return note + [f"No loaded Blueprint class contains '{text}'. If the mob is not in this zone its class "
                       f"may simply not be loaded: go where it appears, or use /miniboss load <AssetPath>."]
    lines = note + [f"{len(every)} Blueprint class(es) containing '{text}' (M = a mob class):"]
    for name, addr in every[:40]:
        lines.append(f"  {'M' if R._is_mob_class(pm, addr) else ' '} {name}")
    if len(every) > 40:
        lines.append(f"  ... and {len(every) - 40} more - narrow the text.")
    return lines


def functions_lines(pm, base):
    R = _R()
    if not _last_spawn["class"]:
        raise MinibossError("spawn something first (/miniboss <type>), then run /miniboss functions")
    deadline = time.time() + 25.0
    lines, kinds = [], {}
    for cls in R.walk_super_chain(pm, _last_spawn["class"]):
        cname = R.resolve_fname(pm, pm.read_int(cls + R.OFFSETS["uobject_name"])) or "?"
        for child in R.walk_children(pm, cls):
            if time.time() > deadline:
                lines.append("  (stopped: time budget reached)")
                return lines
            fname = R.get_ufunction_name(pm, child)
            if not fname or not any(k in fname.lower() for k in FUNCTION_KEYWORDS):
                continue
            try:
                meta = pm.read_longlong(child + 0x10)
                kind = kinds.setdefault(meta, R.resolve_fname(pm, pm.read_int(meta + 0x18)) or "?")
            except Exception:
                kind = "?"
            lines.append(f"  [{kind}] {cname}.{fname}")
    return [f"Candidates on the last spawned mob's class chain ({len(lines)}):"] + (lines or ["  none found"])


def pre_finish_setup(pm, ids):
    """Returns a before_finish callback that writes EntityType (when still Undefined) and the learned
    powerful flag on the not-yet-finished actor. Done BEFORE FinishSpawningActor because that call
    runs BeginPlay: a captain finished with EntityType Undefined initialises its AI and boss bar
    for 'nothing', and writing the type afterwards is too late."""
    def run(actor):
        R = _R()
        if not R._is_mob_class(pm, _actor_class(pm, actor)):
            return
        if ids and pm.read_uint(actor + R._MOB_ENTITY_TYPE_OFFSET) == 1:
            pm.write_uint(actor + R._MOB_ENTITY_TYPE_OFFSET, ids[0])
        mask = _load_learned().get(EVENT_MASK_KEY)
        if mask:
            _write_flags(pm, actor, _read_flags(pm, actor) | mask)
    return run


def _spawn_alias(pm, base, alias, forced_class, say):
    """The default route: spawn the class through GameplayStatics, via the protected bridge call."""
    R = _R()
    display, _, ids = MINIBOSS_TYPES[alias]
    class_addr, chosen, source, others = resolve_mob_class(pm, base, alias, forced_class, say)
    remember_paths(pm, [(chosen, class_addr)])
    group = spawn_group(pm, base, class_addr, ids, lambda i: pre_finish_setup(pm, ids))
    actor, where = group[0]
    _last_spawn.update(time=time.time(), actor=actor, **{"class": class_addr})
    lines = [f"Spawned {display} via {chosen} ({source}) at X={where[0]:.0f} Y={where[1]:.0f} Z={where[2]:.0f}."]
    lines += post_spawn_setup(pm, actor, ids)
    lines += group_extra_lines(pm, group, ids, display)
    info = _evaluate_protected(pm, actor)
    if info:
        # NOT saved as "learned": post_spawn_setup wrote the EntityType itself, so this class would
        # be remembered as that mob without the game ever having said so (and then be trusted next
        # time). Only /miniboss learn, run next to a real mob, teaches classes.
        lines += _info_lines(info, ids)
    else:
        lines.append(f"   spawned actor at {hex(actor)} but it does not read like a mob (no EntityType).")
    if others:
        lines.append(f"   other candidate classes: {', '.join(others)} (force one with /miniboss {alias} <ClassName>)")
    return lines + perf_lines()


def _spawn_raw(pm, base, class_name):
    class_addr = find_class(pm, class_name)
    if not class_addr:
        raise MinibossError(f"no class named '{class_name}' found (try /miniboss find <part of the name>)")
    actor, where = spawn_class(pm, base, class_addr)
    _last_spawn.update(time=time.time(), actor=actor, **{"class": class_addr})
    return [f"Spawned {class_name} at X={where[0]:.0f} Y={where[1]:.0f} Z={where[2]:.0f}."] \
        + describe_actor(pm, actor, ())


def run_miniboss(pm, base, name="", extra="", extra2="", say=None, chest_event=None):
    """Entry point of /miniboss. Returns the lines to print (say, if given, receives progress
    lines while a slow step runs). Blocking."""
    R = _R()
    say = say or (lambda line: None)
    key = normalize(name)
    if key in ("", "list", "help"):
        return _list_lines()
    if R.bridge_hook_state(pm) == "silent":
        return ["The game is loading or changing level - try again in a few seconds."]
    perf_reset()
    _state.update(base=base, say=say)
    restore = _lower_priority()          # everything below is Python-side work: never let it compete with the game
    try:
        return _run_command(pm, base, key, name, extra, extra2, say, chest_event)
    finally:
        restore()


def _run_command(pm, base, key, name, extra, extra2, say, chest_event=None):
    try:
        if key == "preload":
            return preload_lines(pm, base, extra, say)
        if key == "learn":
            return learn_live_mobs(pm, base)
        if key == "functions":
            return functions_lines(pm, base)
        if key == "diag":
            return diag_lines(pm, base, extra)
        if key == "probe":
            return probe_lines(pm, base)
        if key == "promote":
            return promote_lines(pm, base, extra)
        if key == "find":
            return find_lines(pm, base, extra, say, rescan=normalize(extra2) == "rescan")
        if key == "reflect":
            return reflect_lines(pm, base, extra, extra2, say)
        if key == "create":
            return create_lines(pm, base, extra, extra2, say)
        if key == "atchest":
            # /miniboss atchest <type|random> [dry]: create it above the last chest opened this run.
            # The zone trigger calls this with chest_event["fallback_player"] = True: if the chest cannot be
            # read any more it falls back to the player's own position (he just stood at that chest).
            if not extra:
                return ["Usage: /miniboss atchest <type|random> [dry]   (creates it above the last chest you opened)"]
            try:
                anchor, note = anchor_location(pm, base, chest_event), "placed above the last opened chest"
            except MinibossError as e:
                if not (chest_event and chest_event.get("fallback_player")):
                    raise
                anchor, note = player_location(pm, base), f"{e} - placed above the player instead"
            return create_lines(pm, base, extra, extra2, say, anchor=anchor, anchor_note=note)
        if key == "load":
            return load_lines(pm, base, extra, say)
        if key == "paths":
            return paths_lines(pm, base, extra, say)
        # validate the arguments first, so a usage mistake is never hidden behind the cooldown
        alias = forced = None
        if key == "class":
            if not extra:
                return ["Usage: /miniboss class <ExactClassName>"]
        else:
            console = key == "console"
            type_text, forced = (extra, extra2) if console else (name, extra)
            if console and not type_text:
                return ["Usage: /miniboss console <type> [ClassName]"]
            alias, err = resolve_alias(type_text)
            if err:
                return [err]
            forced = forced.strip() or None
        if time.time() - _last_spawn["time"] < SPAWN_COOLDOWN:
            return ["Slow down - one spawn every couple of seconds."]
        if key == "class":
            return _spawn_raw(pm, base, extra)
        if key == "console":
            return _summon_alias(pm, base, alias, forced)
        return _spawn_alias(pm, base, alias, forced, say)
    except MinibossError as e:
        return [f"miniboss: {e}"]
