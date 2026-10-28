"""Devtool behind the /effect command - put a gameplay effect (freeze, slowness, a potion...) on the
local player, or take it off again. Base of the future "trap" items.

How it works: every character owns a UDungeonsAbilitySystemComponent (ABaseCharacter::AbilitySystem,
+0x08B0). The stock UE4 function AbilitySystemComponent::BP_ApplyGameplayEffectToSelf(
TSubclassOf<UGameplayEffect> EffectClass, float Level, FGameplayEffectContextHandle Context) puts an
effect on it; an empty context is fine, the engine makes one itself. Same safety as miniboss.py: the
call goes through the bridge's CALLDATA (game thread, access violations caught, 5 s timeout), the
parameter struct size is read from the UFunction and compared to the layout hard-coded here (UE 4.22),
and a mismatch REFUSES the call.

Layouts (from the Dumper-7 dump, GameplayAbilities_classes.hpp):
  BP_ApplyGameplayEffectToSelf   0x00 EffectClass  0x08 Level (float)  0x10 EffectContext (TSharedPtr,
                                 16 bytes)  0x20 ReturnValue FActiveGameplayEffectHandle (int32 Handle,
                                 bool bPassedFiltering)  -> 0x28
  RemoveActiveGameplayEffectBySourceEffect
                                 0x00 EffectClass  0x08 InstigatorASC (null = any)  0x10 StacksToRemove
                                 (int32, -1 = all)  -> 0x18

IMPORTANT: the effect's duration / modifiers / gameplay cues live in the Blueprint asset, not in the
native class. Applying the native class (FrozenSolidGameplayEffect) alone may do nothing (instant,
no modifiers). Prefer the Blueprint class: /effect find frozen lists the loaded ones.

Commands:
    /effect                         this list
    /effect find <text>             loaded classes whose name contains <text> (Blueprint ones end in _C)
    /effect <name|alias> [level]    apply it to yourself. alias = freeze, chill, slow, weaken, heavy,
                                    bubble, web, blind, poison, swift, strength, defense...
    /effect remove <name|alias>     take it off again
"""
import struct

import miniboss as mb

APPLY_FUNCTION = "BP_ApplyGameplayEffectToSelf"
APPLY_SIZES_OK = (0x28,)
REMOVE_FUNCTION = "RemoveActiveGameplayEffectBySourceEffect"
REMOVE_SIZES_OK = (0x18, 0x20)
ASC_OFFSET = 0x08B0                       # ABaseCharacter::AbilitySystem

# alias -> text searched in the class names (the real Blueprint names are not known, so we search)
ALIASES = {
    "freeze": "FrozenSolid", "frozen": "FrozenSolid",
    "chill": "Chilling", "chilling": "Chilling",
    "slow": "MoveSpeedReduction", "slowness": "MoveSpeedReduction",
    "weaken": "Weaken", "weakness": "Weaken",
    "heavy": "Heavyweight", "heavyweight": "Heavyweight",
    "bubble": "Bubbled", "bubbled": "Bubbled",
    "web": "ShockWeb", "shockweb": "ShockWeb",
    "blind": "Blind", "blindness": "Blind",
    "poison": "PoisonTouched",
    "swift": "SwiftnessPotion", "swiftness": "SwiftnessPotion",
    "strength": "StrengthPotion", "defense": "DefensePotion", "defence": "DefensePotion",
    "ice": "IcePotion", "burning": "BurningBrewPotion", "dense": "DenseBrewPotion",
    "waterbreathing": "WaterBreathingPotion",
}

_func_cache = {}                          # (asc class, function) -> (function address, size, name index)


def _asc_of_player(pm, base):
    R = mb._R()
    pawn, err = R.get_pawn(pm, base)
    if not pawn:
        raise mb.MinibossError(f"no player character ({err})")
    asc = pm.read_longlong(pawn + ASC_OFFSET)
    if not asc:
        raise mb.MinibossError("the player has no AbilitySystem component (null at +0x8B0)")
    cls = pm.read_longlong(asc + 0x10)
    name = R.resolve_fname(pm, pm.read_int(cls + 0x18)) if cls else None
    if not name or "AbilitySystem" not in name:
        raise mb.MinibossError(f"+0x8B0 of the player is a '{name}', not an AbilitySystemComponent - "
                               f"refusing (offset changed in this build?)")
    return asc, cls


def _function(pm, asc_class, fname, sizes_ok):
    R = mb._R()
    hit = _func_cache.get((asc_class, fname))
    if hit:
        try:
            if hit[1] in sizes_ok and pm.read_int(hit[0] + 0x18) == hit[2]:
                return hit[0], hit[1]
        except Exception:
            pass
    func = R._find_function_on_class(pm, asc_class, fname)
    if not func:
        raise mb.MinibossError(f"{fname} not found on the AbilitySystemComponent")
    size = pm.read_int(func + R.OFFSETS["ustruct_size"])
    if size not in sizes_ok:
        raise mb.MinibossError(
            f"{fname} parameter struct is 0x{size:X} bytes, expected one of "
            f"{[hex(s) for s in sizes_ok]} - refusing to call it (wrong layout could corrupt memory). "
            f"Send me this number so the layout can be adjusted.")
    _func_cache[(asc_class, fname)] = (func, size, pm.read_int(func + 0x18))
    return func, size


def _candidates(pm, base, text, say):
    """[(name, address)] - classes whose name contains text; exact (case-insensitive) match first,
    Blueprint classes (_C) before native ones."""
    key = mb.normalize(text)
    found = [(n, a) for n, a in mb.get_class_index(pm, base, say).items() if key in mb.normalize(n)]
    native = mb.find_class(pm, text)
    if native and all(a != native for _, a in found):
        found.append((text, native))
    found.sort(key=lambda x: (mb.normalize(x[0]) != key, not x[0].endswith("_C"), len(x[0]), x[0]))
    return found


def _resolve(pm, base, name, say):
    """(class name, address) for an exact class name or an alias, or raises with the choices."""
    exact = mb.find_class(pm, name)
    if exact:
        return name, exact
    text = ALIASES.get(mb.normalize(name), name)
    found = _candidates(pm, base, text, say)
    # keep gameplay-effect-looking names when there are several
    effects = [c for c in found if "effect" in c[0].lower() or c[0].endswith("_C")] or found
    if not effects:
        raise mb.MinibossError(f"no loaded class matches '{name}' (searched '{text}'). It may not be "
                               f"loaded in this level - try /effect find {text}")
    if len(effects) > 1 and mb.normalize(effects[0][0]) != mb.normalize(text):
        lines = ", ".join(n for n, _ in effects[:8])
        raise mb.MinibossError(f"'{name}' matches several classes: {lines}. Use the exact name "
                               f"(/effect <ClassName>).")
    return effects[0]


def apply_lines(pm, base, name, level, say):
    cname, cls = _resolve(pm, base, name, say)
    asc, asc_class = _asc_of_player(pm, base)
    func, size = _function(pm, asc_class, APPLY_FUNCTION, APPLY_SIZES_OK)
    params = bytearray(size)
    struct.pack_into("<Q", params, 0x00, cls)
    struct.pack_into("<f", params, 0x08, float(level))
    out = mb.calldata(pm, asc, func, bytes(params))
    handle = struct.unpack_from("<i", out, 0x20)[0] if len(out) >= 0x24 else None
    passed = out[0x24] if len(out) > 0x24 else None
    lines = [f"Applied {cname} (level {level:g}) to the player."]
    if handle in (None, -1, 0):
        lines.append("  The game returned no effect handle: the effect was not kept (instant effect, "
                     "native class without data, or blocked by an immunity). Try the Blueprint class: "
                     f"/effect find {ALIASES.get(mb.normalize(name), name)}")
    else:
        lines.append(f"  effect handle {handle}, passed filtering: {bool(passed)}")
    return lines


def remove_lines(pm, base, name, say):
    cname, cls = _resolve(pm, base, name, say)
    asc, asc_class = _asc_of_player(pm, base)
    func, size = _function(pm, asc_class, REMOVE_FUNCTION, REMOVE_SIZES_OK)
    params = bytearray(size)
    struct.pack_into("<Q", params, 0x00, cls)
    struct.pack_into("<Q", params, 0x08, 0)          # any instigator
    struct.pack_into("<i", params, 0x10, -1)         # all stacks
    mb.calldata(pm, asc, func, bytes(params))
    return [f"Asked the game to remove {cname} from the player."]


def find_lines(pm, base, text, say):
    if not text:
        return ["Usage: /effect find <part of a class name>   e.g. /effect find frozen"]
    found = _candidates(pm, base, ALIASES.get(mb.normalize(text), text), say)
    if not found:
        return [f"No loaded class contains '{text}' (Blueprint classes only exist while their level/asset is loaded)."]
    lines = [f"{len(found)} class(es) containing '{text}':"]
    lines += [f"  {n}" for n, _ in found[:40]]
    if len(found) > 40:
        lines.append(f"  ... and {len(found) - 40} more - narrow the text.")
    return lines


def help_lines():
    return ["/effect <name|alias> [level]   put an effect on yourself (freeze, chill, slow, weaken, heavy, "
            "bubble, web, blind, poison, swift, strength, defense...)",
            "/effect remove <name|alias>     take it off again",
            "/effect find <text>             list loaded classes containing <text> (Blueprint ones end in _C)"]


def run_effect(pm, base, name="", extra="", extra2="", say=None):
    """Entry point of /effect. Blocking; returns the lines to print."""
    R = mb._R()
    say = say or (lambda line: None)
    key = mb.normalize(name)
    if key in ("", "list", "help"):
        return help_lines()
    if R.bridge_hook_state(pm) == "silent":
        return ["The game is loading or changing level - try again in a few seconds."]
    mb._state.update(base=base, say=say)
    restore = mb._lower_priority()
    try:
        if key == "find":
            return find_lines(pm, base, extra, say)
        if key == "remove":
            return remove_lines(pm, base, extra, say)
        try:
            level = float(extra) if extra else 1.0
        except ValueError:
            level = 1.0
        return apply_lines(pm, base, name, level, say)
    except mb.MinibossError as e:
        return [f"effect: {e}"]
    finally:
        restore()
