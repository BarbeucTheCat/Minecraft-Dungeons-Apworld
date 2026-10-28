"""deathimg.py - show a custom picture (the Archipelago logo) instead of the skull on the death screen
when the death comes from a DeathLink.

How it works (all names/offsets come from the Dumper-7 SDK of the game):
  * The death screen is the widget UMG_YouDiedHUD_C (UUserWidget). Its skull is the member
        UDungeonsImage* skull   at +0x2C8        (UDungeonsImage : UWidget, Brush at +0x110)
  * UDungeonsImage has SetBrushFromTexture(UTexture2D*, bool bMatchSize).
  * The PNG is loaded from disk with UKismetRenderingLibrary::ImportFileAsTexture2D(WorldContext, Filename)
    (a stock UE4 function), called through the bridge's CALLDATA like /miniboss does, so everything runs on
    the game thread, inside the bridge's SEH / world-change protection.
  * The texture is kept alive by the image's own Brush (a UPROPERTY), so it is not garbage collected while
    the widget exists. A new level builds a new widget: the picture is simply applied again after the next
    DeathLink.

Every game change goes through crash_diag.journaled (mutation_journal.log) and is refused while
crash_diag.mutations_allowed() says the game is in a fragile state (level just changed, crash-guard storm...).
"""
import os
import struct
import time

import crash_diag
import miniboss
from miniboss import MinibossError, calldata, encode_fstring, _find_static_function

HUD_CLASS = "UMG_YouDiedHUD_C"
HUD_PLAYERCHARACTER = 0x310         # UUMG_YouDiedHUD_C.PlayerCharacter
HUD_GAMEOVER = 0x320                # UUMG_YouDiedHUD_C.gameover (bool)
HUD_SKULL = 0x2C8                   # UUMG_YouDiedHUD_C.skull (UDungeonsImage*)

IMPORT_CLASS = "KismetRenderingLibrary"
IMPORT_FUNCTION = "ImportFileAsTexture2D"
IMPORT_SIZES_OK = (0x20,)           # WorldContextObject 8 + FString 16 + return 8
IMPORT_RETURN_OFFSET = 0x18
SET_BRUSH_FUNCTION = "SetBrushFromTexture"
SET_BRUSH_SIZES_OK = (0x9, 0x10)    # UTexture2D* 8 + bool 1 (the size UE reports is 9 or 16)

IMAGE_NAMES = ("archipelago_death.png", "deathlink.png")
_here = os.path.dirname(os.path.abspath(__file__))

_state = {"hud": None, "world": None, "class": None, "last": None}
_fstring_cache = {}


def _R():
    import dungeons_reader
    return dungeons_reader


# ---------------------------------------------------------------------------------------- file lookup
def find_image(custom=None, names=None):
    """Path of the picture to show: an explicit path, else archipelago_death.png next to the client,
    in its map/ folder or in the current folder."""
    candidates = []
    if custom:
        candidates.append(custom)
    for name in (names or IMAGE_NAMES):
        candidates += [os.path.join(_here, name), os.path.join(_here, "map", name),
                       os.path.join(os.getcwd(), name)]
    for c in candidates:
        if c and os.path.isfile(c):
            return os.path.abspath(c)
    return None


# ------------------------------------------------------------------------------------------ the HUD
def _is_default_object(pm, obj):
    try:
        name = _R().resolve_fname(pm, pm.read_int(obj + 0x18)) or ""
    except Exception:
        return True
    return name.startswith("Default__")


def find_hud(pm, base, refresh=False):
    """Address of the live UMG_YouDiedHUD_C of this level (the widget exists even before anyone dies),
    or None. Pure Python-side pass over the object table (the game is not involved and nothing is
    scanned on the game thread): objects are grouped by class, each class name is resolved once.
    Cached for the level."""
    R = _R()
    world = pm.read_longlong(base + R.OFFSETS["gworld"])
    cached = _state["hud"]
    if cached and not refresh and _state["world"] == world:
        try:
            if pm.read_longlong(cached + 0x10) == _state["class"]:
                return cached
        except Exception:
            pass
    by_class = {}
    for obj in R.get_gobjects_pointer_snapshot(pm, base).values():
        if not obj:
            continue
        try:
            cls = pm.read_longlong(obj + 0x10)
        except Exception:
            continue
        if cls:
            by_class.setdefault(cls, []).append(obj)
    hits, hud_class = [], None
    for cls, objs in by_class.items():
        try:
            if R.resolve_fname(pm, pm.read_int(cls + 0x18)) != HUD_CLASS:
                continue
        except Exception:
            continue
        hud_class = cls
        hits = [o for o in objs if not _is_default_object(pm, o)]
        break
    if not hits:
        return None
    pawn, _err = R.get_pawn(pm, base)
    chosen = hits[-1]
    for obj in hits:                       # the one that belongs to our character, if several
        try:
            if pawn and pm.read_longlong(obj + HUD_PLAYERCHARACTER) == pawn:
                chosen = obj
        except Exception:
            pass
    _state.update(hud=chosen, world=world)
    _state["class"] = hud_class
    return chosen


def read_skull(pm, hud):
    skull = pm.read_longlong(hud + HUD_SKULL)
    if not skull or skull < 0x10000:
        raise MinibossError("the death screen has no skull image (pointer is null)")
    return skull


# --------------------------------------------------------------------------------------- game calls
@crash_diag.journaled
def import_texture(pm, base, path, context=None):
    """Loads a PNG/JPG file into a UTexture2D inside the game; returns its address.
    context = any live UObject, used as WorldContextObject (the player's pawn when None; in the main
    menu there is no pawn, a widget does)."""
    R = _R()
    pawn = context
    if not pawn:
        pawn, err = R.get_pawn(pm, base)
        if not pawn:
            raise MinibossError(f"no player character ({err})")
    cdo, func, _size = _find_static_function(pm, IMPORT_CLASS, IMPORT_FUNCTION, IMPORT_SIZES_OK)
    if path not in _fstring_cache:
        raw, count = encode_fstring(path)
        addr = pm.allocate(len(raw))
        pm.write_bytes(addr, raw, len(raw))
        _fstring_cache[path] = (addr, count)
    addr, count = _fstring_cache[path]
    params = bytearray(0x20)
    struct.pack_into("<Q", params, 0x00, pawn)
    struct.pack_into("<Q", params, 0x08, addr)
    struct.pack_into("<ii", params, 0x10, count, count)
    after = calldata(pm, cdo, func, bytes(params))
    texture = struct.unpack_from("<Q", after, IMPORT_RETURN_OFFSET)[0]
    if not texture:
        raise MinibossError(f"the game could not load {path} (not a valid PNG/JPG, or unreadable)")
    return texture


@crash_diag.journaled
def set_skull_texture(pm, skull, texture, match_size=False):
    """UDungeonsImage::SetBrushFromTexture(texture, match_size) on the skull image."""
    R = _R()
    cls = pm.read_longlong(skull + 0x10)
    func = R._find_function_on_class(pm, cls, SET_BRUSH_FUNCTION)
    if not func:
        raise MinibossError(f"{SET_BRUSH_FUNCTION} not found on the skull image class")
    size = pm.read_int(func + R.OFFSETS["ustruct_size"])
    if size not in SET_BRUSH_SIZES_OK:
        raise MinibossError(f"{SET_BRUSH_FUNCTION} parameter struct is 0x{size:X} bytes, expected "
                            f"{[hex(s) for s in SET_BRUSH_SIZES_OK]} - refusing to call it. Send me this number.")
    params = bytearray(size)
    struct.pack_into("<Q", params, 0x00, texture)
    params[0x08] = 1 if match_size else 0
    calldata(pm, skull, func, bytes(params))


# ---------------------------------------------------------------------------------------- entry points
def apply(pm, base, custom_path=None, match_size=False):
    """Puts the picture on the skull of the current death screen. Returns a one-line report.
    Raises MinibossError with a readable reason on failure. Caller holds ctx.bridge_pipe_lock."""
    ok, why = crash_diag.mutations_allowed()
    if not ok:
        raise MinibossError(f"game changes are on hold: {why}")
    miniboss._state["base"] = base          # lets name lookups use the object index, not a game-thread scan
    path = find_image(custom_path)
    if not path:
        raise MinibossError("picture not found: put archipelago_death.png (PNG, ideally a power of two "
                            "like 256x256) next to dungeons_ap_client.py, or use /img death <file>")
    hud = find_hud(pm, base)
    if not hud:
        raise MinibossError("death screen widget (UMG_YouDiedHUD_C) not found in this level")
    skull = read_skull(pm, hud)
    texture = import_texture(pm, base, path)
    set_skull_texture(pm, skull, texture, match_size)
    _state["last"] = (time.time(), hud, skull, texture)
    return f"death screen skull {skull:X} -> {os.path.basename(path)} (texture {texture:X})"


def info(pm, base):
    """Diagnostic lines: what is found, nothing is changed."""
    R = _R()
    miniboss._state["base"] = base
    lines = []
    path = find_image()
    lines.append(f"picture: {path or 'NOT FOUND (archipelago_death.png next to the client)'}")
    hud = find_hud(pm, base, refresh=True)
    if not hud:
        lines.append(f"{HUD_CLASS}: not found in this level")
        return lines
    lines.append(f"{HUD_CLASS} at {hud:X}, gameover={pm.read_bytes(hud + HUD_GAMEOVER, 1)[0]}")
    try:
        skull = read_skull(pm, hud)
        cname = R.resolve_fname(pm, pm.read_int(pm.read_longlong(skull + 0x10) + 0x18))
        lines.append(f"skull at {skull:X} (class {cname})")
        func = R._find_function_on_class(pm, pm.read_longlong(skull + 0x10), SET_BRUSH_FUNCTION)
        lines.append(f"{SET_BRUSH_FUNCTION}: " + (f"found, params 0x{pm.read_int(func + R.OFFSETS['ustruct_size']):X}"
                                                  if func else "NOT FOUND"))
    except Exception as e:
        lines.append(f"skull: {e}")
    try:
        cdo, func, size = _find_static_function(pm, IMPORT_CLASS, IMPORT_FUNCTION, IMPORT_SIZES_OK)
        lines.append(f"{IMPORT_CLASS}::{IMPORT_FUNCTION}: found, params 0x{size:X}")
    except MinibossError as e:
        lines.append(f"{IMPORT_FUNCTION}: {e}")
    return lines


# ------------------------------------------------------------------------------------ map chest icon
# Dumper-7: UMG_MapWidget_C +0x268 -> UMG_MapStatTrackerList_C (+0x230 Chests, +0x238 Secrets)
#           UMG_MapStatTracker_C: UDungeonsImage* Icon at +0x248 (IconTexture, UTexture*, at +0x278)
# Only the Chests tracker is touched: the Secrets tracker is the same widget class with its own icon.
MAP_WIDGET_STAT_LIST = 0x268
MAP_STAT_LIST_CHESTS = 0x230
MAP_TRACKER_ICON = 0x248
CHEST_ICON_NAMES = ("chest_icon.png", "archipelago_chest.png")


def find_map_chest_icons(pm, base):
    """[(widget, tracker, icon_image)] for every live map widget that has a chest tracker."""
    R = _R()
    found = []
    for widget in R.find_map_widget_instances(pm, base):
        try:
            stat_list = R._read_valid_ptr(pm, widget + MAP_WIDGET_STAT_LIST)
            tracker = R._read_valid_ptr(pm, stat_list + MAP_STAT_LIST_CHESTS) if stat_list else 0
            icon = R._read_valid_ptr(pm, tracker + MAP_TRACKER_ICON) if tracker else 0
        except Exception:
            continue
        if icon:
            found.append((widget, tracker, icon))
    return found


def apply_map_chest_icon(pm, base, custom_path=None, match_size=False):
    """Puts the picture on the chest icon of the in-game map (the chest counter). The map widget only
    exists in a mission. Returns a one-line report; raises MinibossError with a readable reason."""
    ok, why = crash_diag.mutations_allowed()
    if not ok:
        raise MinibossError(f"game changes are on hold: {why}")
    miniboss._state["base"] = base
    path = find_image(custom_path, CHEST_ICON_NAMES)
    if not path:
        raise MinibossError("picture not found: put chest_icon.png (PNG, ideally a power of two) next to "
                            "dungeons_ap_client.py, or use /img chest <file>")
    targets = find_map_chest_icons(pm, base)
    if not targets:
        raise MinibossError("no map chest icon found (the map widget only exists inside a mission)")
    texture = import_texture(pm, base, path)
    for _widget, _tracker, icon in targets:
        set_skull_texture(pm, icon, texture, match_size)     # same UDungeonsImage::SetBrushFromTexture call
    return f"map chest icon x{len(targets)} -> {os.path.basename(path)} (texture {texture:X})"


# ------------------------------------------------------------------------------------ main menu logo
# Dumper-7: UMG_MenuDungeonsLogo_C (UUserWidget) has UDungeonsImage* LogoImage at +0x230
# (also held by UMG_Menu_C at +0x2C0). A RetainerBox wraps the image; it redraws on its own.
LOGO_CLASS = "UMG_MenuDungeonsLogo_C"
LOGO_IMAGE = 0x230
LOGO_NAMES = ("game_logo.png", "archipelago_logo.png")


def find_instances(pm, base, class_name):
    """Live objects (not the class default object) of the Blueprint class called class_name.
    Python-side pass over the object table, nothing runs on the game thread."""
    R = _R()
    by_class = {}
    for obj in R.get_gobjects_pointer_snapshot(pm, base).values():
        if not obj:
            continue
        try:
            cls = pm.read_longlong(obj + 0x10)
        except Exception:
            continue
        if cls:
            by_class.setdefault(cls, []).append(obj)
    for cls, objs in by_class.items():
        try:
            if R.resolve_fname(pm, pm.read_int(cls + 0x18)) == class_name:
                return [o for o in objs if not _is_default_object(pm, o)]
        except Exception:
            continue
    return []


def apply_menu_logo(pm, base, custom_path=None, match_size=False):
    """Replaces the Minecraft Dungeons logo of the main menu. Works in the menu (no pawn needed);
    the widget is recreated whenever the menu is rebuilt, so it may need applying again."""
    ok, why = crash_diag.mutations_allowed()
    if not ok:
        raise MinibossError(f"game changes are on hold: {why}")
    miniboss._state["base"] = base
    path = find_image(custom_path, LOGO_NAMES)
    if not path:
        raise MinibossError("picture not found: put game_logo.png (PNG, ideally a power of two) next to "
                            "dungeons_ap_client.py, or use /img logo <file>")
    widgets = find_instances(pm, base, LOGO_CLASS)
    targets = []
    for w in widgets:
        try:
            img = pm.read_longlong(w + LOGO_IMAGE)
        except Exception:
            continue
        if img and img > 0x10000:
            targets.append((w, img))
    if not targets:
        raise MinibossError("main menu logo widget (UMG_MenuDungeonsLogo_C) not found - is the main menu on screen?")
    texture = import_texture(pm, base, path, context=targets[0][0])
    for _w, img in targets:
        set_skull_texture(pm, img, texture, match_size)
    return f"menu logo x{len(targets)} -> {os.path.basename(path)} (texture {texture:X})"
