"""'Map' tab for the Minecraft Dungeons client.

Shows the Mainland, Island Realms and Other Dimensions maps with one pin per zone (plus one for the Bonus Chests
and one for the Emerald milestones). Each pin displays checked/total and is
coloured by progress; clicking it lists that zone's checks.

Read-only: it only looks at ctx.checked_locations / ctx.missing_locations,
so it never touches the game process or the DLL.
"""
import logging
from io import BytesIO
from pathlib import Path

from kivy.clock import Clock
from kivy.core.image import Image as CoreImage
from kivy.graphics import Color, Rectangle
from kivy.metrics import dp
from kivy.graphics.transformation import Matrix
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.checkbox import CheckBox
from kivy.uix.scatter import Scatter
from kivy.uix.spinner import Spinner, SpinnerOption
from kivy.uix.stencilview import StencilView
from kivy.uix.widget import Widget
from kivy.uix.floatlayout import FloatLayout
from kivy.uix.gridlayout import GridLayout
from kivy.uix.label import Label
from kivy.uix.popup import Popup
from kivy.uix.scrollview import ScrollView

from _apworld_data import Locations as _apw, ZoneData as _zd, Items as _items

log = logging.getLogger("Client")

# ---------------------------------------------------------------------------
# MAP PAGES. One entry per map; the dropdown at the top switches between them.
# To add a map: drop its image in client/map/ and add an entry here.
#   pins: key -> (x, y) in image pixels. key is a zone internal_name (see
#         ZoneData.py) or one of the extra groups ("Bonus Chests",
#         "Emerald Milestones").
# ---------------------------------------------------------------------------
MAPS = [
    {
        "name": "Mainland Island",
        "image": "map/mainland.jpg",
        "size": (3073, 1530),
        "pins": {
            # main missions (hexagon icons)
            "squidcoast": (693, 813), "creeperwoods": (858, 637), "pumpkinpastures": (1412, 1051), "soggyswamp": (882, 1111),
            "redstonemines": (1371, 506), "cacticanyon": (1315, 836), "deserttemple": (1822, 672), "highblockhalls": (2282, 637),
            "fieryforge": (1888, 321), "obsidianpinnacle": (2606, 420), "thestronghold": (2376, 1072),
            # secret missions (orange shield icons)
            "creepycrypt": (591, 592), "soggycave": (864, 941), "archhaven": (1761, 1060), "lowertemple": (2060, 859),
            "underhalls": (2610, 711), "mooshroomisland": (815, 365),
            # The Tower: no check yet, its pin stays gray
            "The Tower": (511, 695),
            # icons drawn into the image (bottom right); pins sit just under them
            "Villagers": (1896, 1237), "Bonus Chests": (2063, 1237), "Eyes of Ender": (2683, 1237), "Emerald Milestones": (2876, 1237),
        },
    },
    {
        "name": "Island Realms",
        "image": "map/islandrealms.jpg",
        "size": (3077, 2676),
        "pins": {
            # Jungle Awakens (top-left island)
            "overgrowntemple": (1069, 691), "treetoptangle": (1293, 869),
            "dingyjungle": (631, 925), "bamboobluff": (1144, 998),            # bamboobluff = Panda Plateau (secret)
            # Howling Peaks (top-right island)
            "galesanctum": (2152, 298), "gauntletofgales": (2212, 486),
            "windsweptpeaks": (2093, 823), "endlessrampart": (2013, 1223),    # endlessrampart = Colossal Rampart (secret)
            # Creeping Winter (bottom-right island)
            "lostsettlement": (2321, 1670), "lonefortress": (1960, 1766),     # lostsettlement = secret
            "frozenfjord": (1980, 2026),
            # Hidden Depths (bottom-left island)
            "coralrise": (653, 1667), "abyssalmonument": (407, 1871),
            "radiantravine": (1007, 1877),                                     # radiantravine = secret
        },
    },
    {
        "name": "Other Dimensions",
        "image": "map/otherdimensions.jpg",
        "size": (3242, 1954),
        "pins": {
            # Flames of the Nether (left island)
            "netherwastes": (1300, 320), "warpedforest": (743, 364),
            "netherfortress": (1096, 469), "crimsonforest": (642, 583),       # netherfortress, crimsonforest, soulsandvalley = secret
            "soulsandvalley": (1483, 635), "basaltdeltas": (1172, 740),
            # Echoing Void (right island)
            "endcitadel_blightedcitadel": (2460, 761),                        # Broken Citadel
            "enderwilds": (2186, 1373),
        },
    },
]

PIN_SIZE, PIN_BORDER = 76, 6      # in image pixels (scale with zoom)

# Pin colours, in priority order:
COL_DONE = (0.45, 0.45, 0.45, 1)   # gray  : finished (every check done) / no checks in this slot
COL_OUT = (0.80, 0.22, 0.22, 1)    # red   : not intended to be picked up yet (access items still missing)
COL_HARD = (0.95, 0.62, 0.10, 1)   # orange: reachable, but holds no progression item (optional DLC pack, grind groups)
COL_LOGIC = (0.20, 0.72, 0.25, 1)  # green : doable now

# Groups the apworld excludes from the fill logic (long grinds / post-game):
# they never hold a progression item -> orange instead of green. Edit freely.
HARD_GROUPS = {"Bonus Chests", "Emerald Milestones", "??? (Mooshroom Island)"}

_SKIP = _zd.skip_level_requires_map()


def zone_items_needed(zone, secret_opt):
    """Items the apworld's make_zone_rule() requires for this zone: ONE per zone (the zone's own plus its
    skip-level predecessors). That item is the zone's Access, or its Secret Access for a secret zone when the
    Secret Mission Access option is on (same as Items.zone_access_item_name)."""
    def one(z):
        zi = _zd.ZONES_BY_NAME[z]
        return f"{zi.display_name} Secret Access" if (zi.secret and secret_opt) else f"{zi.display_name} Access"
    return [one(zone)] + [one(req) for req in _SKIP.get(zone, [])]


def _read_bytes(path: Path) -> bytes:
    try:
        return __loader__.get_data(str(path))  # zip-aware, like the DLL/seed loading
    except Exception:
        return path.read_bytes()


def _title(key):
    z = _zd.ZONES_BY_NAME.get(key)
    return z.display_name if z else key


def build_groups():
    """{group title: [(check label, location id), ...]} - order: chests,
    supply chests, boss first kill, mini boss kill, mission complete."""
    A = _apw
    groups = {}
    boss_by_zone = {}
    for boss, zone in A.BOSS_ZONE.items():
        boss_by_zone.setdefault(zone, []).append(boss)
    for m in MAPS:
        for key in m["pins"]:
            if key in _zd.ZONES_BY_NAME:
                zone = key
                ch, sup = A.ZONE_CHEST_COUNTS.get(zone, (0, 0))
                rows = [(f"Chest {n}", A.get_zone_chest_location_id(zone, n)) for n in range(1, ch + 1)]
                rows += [(f"Supply Chest {n}", A.get_zone_supply_chest_location_id(zone, n)) for n in range(1, sup + 1)]
                rows += [(f"{b} - First Kill", A.get_boss_kill_location_id(b)) for b in boss_by_zone.get(zone, [])]
                for type_key in A.MINIBOSS_TYPE_ORDER:    # per-type "Mini Boss - <Type>": shown on the type's first zone
                    if A.MINIBOSS_TYPE_ZONES[type_key][0] == zone:
                        rows.append((f"Mini Boss - {A.MINIBOSS_TYPE_DISPLAY[type_key]}", A.get_mini_boss_type_location_id(type_key)))
                if zone in A.MINIBOSS_ZONES:    # "Mini Boss Kill" check (filtered out by _stats when the seed lacks it)
                    rows.append(("Mini Boss Kill", A.get_mini_boss_kill_location_id(zone)))
                # "Secret Zone Found" check (Secret Mission Access option): the secret
                # trigger sits in the secret zone's PARENT mission, so the row
                # lives on the parent's pin. Filtered out automatically by
                # _stats when the slot doesn't have the check (option off).
                secret_zone = A.SECRET_PARENT_TO_ZONE.get(zone)
                if secret_zone:
                    sec_name = A.SECRET_ZONE_FOUND_BY_ZONE[secret_zone]
                    rows.append((f"Secret Zone Found ({_title(secret_zone)})", A.LOCATION_TABLE[sec_name]))
                rows.append(("Mission Complete", A.LOCATION_TABLE[A.LOCATIONS_BY_ZONE[zone]]))
            elif key == "The Tower":
                rows = []                      # no check there yet: the pin shows "-" in gray
            elif key == "Villagers":
                rows = [(A.villager_location_name(k), A.get_villager_location_id(k)) for k, *_ in A.VILLAGER_TYPES]
            elif key == "Eyes of Ender":
                rows = [(A.eye_of_ender_location_name(t), A.get_eye_of_ender_location_id(t))
                        for t in A.EYE_OF_ENDER_ZONE]
            elif key == "Bonus Chests":
                rows = [(f"Bonus Chest {n}", A.get_bonus_chest_location_id(n)) for n in range(1, A.MAX_BONUS_CHESTS + 1)]
            elif key == "Emerald Milestones":
                rows = [(f"{a} emeralds", A.get_emerald_milestone_id(a)) for a in range(500, 50001, 500)]
            else:
                continue
            groups[_title(key)] = rows
    return groups


class Pin(Widget):
    """Square pin (black border + coloured fill + count), drawn in image pixels."""

    def __init__(self, title, cx, cy, **kw):
        super().__init__(size_hint=(None, None), size=(PIN_SIZE, PIN_SIZE), **kw)
        self.title = title
        self.center = (cx, cy)
        with self.canvas:
            Color(0, 0, 0, 1)
            self._border = Rectangle(pos=(self.x - PIN_BORDER, self.y - PIN_BORDER),
                                     size=(PIN_SIZE + 2 * PIN_BORDER, PIN_SIZE + 2 * PIN_BORDER))
            self._col = Color(*COL_LOGIC)
            self._fill = Rectangle(pos=self.pos, size=self.size)
        self.label = Label(text="", font_size=PIN_SIZE * 0.42, bold=True, size_hint=(None, None),
                           size=self.size, pos=self.pos, outline_width=3, outline_color=(0, 0, 0))
        self.add_widget(self.label)

    def set_state(self, text, color):
        self.label.text = text
        self._col.rgba = color


class MapScatter(Scatter):
    """Pan (drag) + zoom (wheel / pinch); a click without dragging opens the zone."""

    def __init__(self, on_tap, **kw):
        super().__init__(do_rotation=False, auto_bring_to_front=False, **kw)
        self.on_tap = on_tap

    WHEEL_STEP = 1.15   # zoom factor per notch of the mouse wheel
    blocked = None      # callable set by MapTab: True while the map dropdown is open (the map must not react then)

    def on_touch_down(self, touch):
        if self.blocked and self.blocked():
            return False
        if self.collide_point(*touch.pos) and getattr(self, "on_user", None):
            self.on_user()
        # Mouse wheel zoom, centred on the pointer (Kivy's Scatter has no wheel support of its own).
        # Kivy names the buttons the other way round: wheel up (away from you) = "scrolldown" = zoom in.
        if getattr(touch, "is_mouse_scrolling", False) and self.collide_point(*touch.pos):
            factor = self.WHEEL_STEP if touch.button == "scrolldown" else (
                1.0 / self.WHEEL_STEP if touch.button == "scrollup" else 1.0)
            target = min(max(self.scale * factor, self.scale_min), self.scale_max)
            factor = target / self.scale if self.scale else 1.0
            if abs(factor - 1.0) > 1e-6:
                self.apply_transform(Matrix().scale(factor, factor, factor), post_multiply=True,
                                     anchor=self.to_local(*touch.pos))
            return True
        touch.ud["mp_start"] = touch.pos
        return super().on_touch_down(touch)

    def on_touch_up(self, touch):
        start = touch.ud.get("mp_start")
        res = super().on_touch_up(touch)
        if self.blocked and self.blocked():
            return res
        if (start and not touch.is_mouse_scrolling and abs(touch.x - start[0]) < dp(8)
                and abs(touch.y - start[1]) < dp(8) and self.collide_point(*touch.pos)):
            self.on_tap(*self.to_local(*touch.pos))
        return res


class MapOption(SpinnerOption):
    """Entry of the map dropdown: flat, so the entries touch each other (the default textured button leaves a gap)."""
    NORMAL = (0.30, 0.30, 0.30, 1)
    PRESSED = (0.42, 0.42, 0.42, 1)

    def __init__(self, **kw):
        super().__init__(**kw)
        self.background_normal = ""
        self.background_down = ""
        self.background_color = self.NORMAL
        self.bind(state=lambda w, st: setattr(w, "background_color", w.PRESSED if st == "down" else w.NORMAL))
        self.bind(on_press=self._choose)

    def _choose(self, *_):
        """Select this map on press: the entries sit on top of the map, so don't wait for the release."""
        widget = self.parent
        while widget is not None and not hasattr(widget, "select"):
            widget = widget.parent
        if widget is not None:
            widget.select(self.text)


class Viewport(StencilView, FloatLayout):
    """StencilView only clips the DRAWING: a zoomed map still received the clicks aimed at the bar above it (map
    name button, Recenter...). A press that starts outside the view now never reaches the map."""

    def on_touch_down(self, touch):
        if not self.collide_point(*touch.pos):
            return False
        return super().on_touch_down(touch)


class MapTab(BoxLayout):
    PREFIX = "Current Map: "

    def __init__(self, ctx, **kwargs):
        super().__init__(orientation="vertical", **kwargs)
        self.ctx = ctx
        self.groups = build_groups()
        self.title_zone = {_title(z): z for m in MAPS for z in m["pins"] if z in _zd.ZONES_BY_NAME}
        # Villagers / Eyes of Ender: location id -> the zone it lives in, so the pin can follow logic
        self.group_row_zone = {
            "Villagers": {_apw.get_villager_location_id(k): zone for k, _f, _d, zone in _apw.VILLAGER_TYPES},
            "Eyes of Ender": {_apw.get_eye_of_ender_location_id(t): zone for t, zone in _apw.EYE_OF_ENDER_ZONE.items()},
        }
        self.zone_map = {z: i for i, m in enumerate(MAPS) for z in m["pins"] if z in _zd.ZONES_BY_NAME}
        self.cur = 0
        self.pins = []
        self._textures = {}
        self._last_zone = None
        self._need_center = True

        # ---- top bar: Auto Tab | Current Map | Recenter --------------
        bar = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(10), padding=(dp(8), dp(4)))
        bar.add_widget(Widget())
        self.auto = CheckBox(active=True, size_hint_x=None, width=dp(32))
        bar.add_widget(self.auto)
        bar.add_widget(Label(text="Auto Tab", size_hint_x=None, width=dp(70)))
        self.spinner = Spinner(text=self.PREFIX + MAPS[0]["name"], values=[m["name"] for m in MAPS],
                               size_hint_x=None, width=dp(240), option_cls=MapOption)
        try:
            self.spinner._dropdown.container.spacing = 0     # no hole between the map names
        except Exception:
            pass
        self.spinner.bind(text=self._on_spinner)
        bar.add_widget(self.spinner)
        bar.add_widget(Button(text="Recenter", size_hint_x=None, width=dp(110),
                              on_release=lambda *_: self._recenter_btn()))
        bar.add_widget(Widget())
        self.add_widget(bar)

        # ---- map area --------------------------------------------------
        self.view = Viewport()
        self.scatter = MapScatter(self._tap, size_hint=(None, None))
        self.scatter.on_user = lambda: setattr(self, "_need_center", False)
        self.scatter.blocked = lambda: self.spinner.is_open
        self.view.add_widget(self.scatter)
        self.add_widget(self.view)
        self.view.bind(size=self._on_view_size, pos=self._on_view_size)

        self.load_map(0)
        Clock.schedule_interval(self.refresh, 1.0)

    # -- maps ------------------------------------------------------------
    def _texture(self, i):
        if i not in self._textures:
            path = Path(__file__).resolve().parent / MAPS[i]["image"]
            self._textures[i] = CoreImage(BytesIO(_read_bytes(path)), ext="jpg").texture
        return self._textures[i]

    def load_map(self, i):
        m = MAPS[i]
        self.cur = i
        sc = self.scatter
        sc.clear_widgets()
        iw, ih = m["size"]
        sc.size = (iw, ih)
        content = Widget(size_hint=(None, None), size=(iw, ih))
        with content.canvas.before:
            Color(1, 1, 1, 1)  # neutral tint, otherwise the map inherits a dark colour
            Rectangle(texture=self._texture(i), pos=(0, 0), size=(iw, ih))
        self.pins = []
        for key, (x, y) in m["pins"].items():
            if _title(key) not in self.groups:
                continue
            p = Pin(_title(key), x, ih - y)
            content.add_widget(p)
            self.pins.append(p)
        sc.add_widget(content)
        self.refresh()
        self._need_center = True
        self._on_view_size()

    def _on_spinner(self, spinner, text):
        if text.startswith(self.PREFIX):
            return
        for i, m in enumerate(MAPS):
            if m["name"] == text:
                spinner.text = self.PREFIX + text
                if i != self.cur:
                    self.load_map(i)
                return

    # -- geometry --------------------------------------------------------
    def _on_view_size(self, *_):
        # keep the map fitted while the window is being laid out / resized,
        # until the user starts panning or zooming
        if self._need_center and self.view.width > 1 and self.view.height > 1:
            self.recenter()

    def _recenter_btn(self):
        self._need_center = True
        self.recenter()

    def recenter(self):
        iw, ih = MAPS[self.cur]["size"]
        vw, vh = self.view.size
        if vw <= 1 or vh <= 1:
            return
        s = min(vw / iw, vh / ih) * 0.97
        sc = self.scatter
        sc.scale_min, sc.scale_max = 0.01, 100.0  # lift limits while resetting
        sc.transform = Matrix()
        sc.pos = self.view.pos
        sc.apply_transform(Matrix().scale(s, s, s), anchor=(0, 0))
        sc.apply_transform(Matrix().translate((vw - iw * s) / 2, (vh - ih * s) / 2, 0))
        sc.scale_min, sc.scale_max = s * 0.4, s * 12

    def _tap(self, lx, ly):
        for p in self.pins:
            half = PIN_SIZE / 2 + PIN_BORDER
            if abs(lx - p.center_x) <= half and abs(ly - p.center_y) <= half:
                self.open_zone(p.title)
                return

    # -- state -----------------------------------------------------------
    def _slot_ids(self):
        ctx = self.ctx
        checked = set(getattr(ctx, "checked_locations", ()) or ())
        allids = set(getattr(ctx, "server_locations", ()) or ())
        allids |= checked | set(getattr(ctx, "missing_locations", ()) or ())
        return allids, checked

    def _stats(self, title, allids, checked):
        rows = [(n, i) for n, i in self.groups[title] if i in allids]
        done = sum(1 for _, i in rows if i in checked)
        return rows, done

    def _zone_accessible(self, zone):
        """Does the player hold the items the apworld's rule asks for to play `zone`?"""
        ctx = self.ctx
        got = {ni.item for ni in (getattr(ctx, "items_received", None) or [])}
        if zone in {z.internal_name for z in _zd.ANCIENT_HUNT_ZONES}:    # the three hunts share one item
            info = _items.ITEM_TABLE.get(_items.ANCIENT_HUNT_ACCESS_ITEM)
            return info is None or info.code in got
        secret_opt = bool((getattr(ctx, "slot_data", None) or {}).get("secret_missions_require_secret"))
        for name in zone_items_needed(zone, secret_opt):
            info = _items.ITEM_TABLE.get(name)
            if info is not None and info.code not in got:
                return False
        return True

    def _in_logic(self, title, rows=None, checked=None):
        zone = self.title_zone.get(title)
        if zone is None:
            row_zone = self.group_row_zone.get(title)
            if row_zone is None or rows is None:  # Bonus Chests / Emerald Milestones: no access rule
                return True
            # Villagers / Eyes of Ender: in logic as long as ONE check still to do lives in a zone we can enter
            remaining = [i for _n, i in rows if i not in (checked or ())]
            return any(self._zone_accessible(row_zone[i]) for i in remaining if i in row_zone) or \
                not any(i in row_zone for i in remaining)
        return self._zone_accessible(zone)

    def _is_optional_dlc(self, title):
        """True for a DLC mission whose pack is not selected in the seed (slot_data["optional_dlc_packs"])."""
        zone = self.title_zone.get(title)
        info = _zd.ZONES_BY_NAME.get(zone) if zone else None
        if info is None or info.category != "dlc":
            return False
        optional = (getattr(self.ctx, "slot_data", None) or {}).get("optional_dlc_packs") or ()
        return info.dlc_pack in optional

    def _color(self, title, total, done, rows=None, checked=None):
        if total == 0 or done == total:
            return COL_DONE
        if not self._in_logic(title, rows, checked):      # red wins over orange: an optional DLC mission stays red until unlocked
            return COL_OUT
        if self._is_optional_dlc(title) or title in HARD_GROUPS:
            return COL_HARD                # unlocked, but no progression item can be there
        return COL_LOGIC

    def refresh(self, *_):
        allids, checked = self._slot_ids()
        for p in self.pins:
            rows, done = self._stats(p.title, allids, checked)
            total = len(rows)
            p.set_state("-" if total == 0 else f"{done}/{total}", self._color(p.title, total, done, rows, checked))
        # Auto Tab: follow the zone the player is currently in
        zone = (getattr(self.ctx, "game_state", None) or {}).get("zone")
        if zone != self._last_zone:
            self._last_zone = zone
            i = self.zone_map.get(zone)
            if self.auto.active and i is not None and i != self.cur:
                self.spinner.text = MAPS[i]["name"]  # triggers load_map

    # -- popup -----------------------------------------------------------
    def open_zone(self, title):
        allids, checked = self._slot_ids()
        rows, done = self._stats(title, allids, checked)
        grid = GridLayout(cols=1, size_hint_y=None, spacing=dp(2), padding=dp(6))
        grid.bind(minimum_height=grid.setter("height"))
        if not rows:
            rows_text = ["[color=aaaaaa]No checks from this group in your slot.[/color]"]
        else:
            rows_text = [("[color=44dd44][X][/color] " if i in checked else "[color=ff6666][  ][/color] ") + n
                         for n, i in rows]
        for t in rows_text:
            lbl = Label(text=t, markup=True, size_hint_y=None, height=dp(24), halign="left", valign="middle")
            lbl.bind(size=lambda w, sz: setattr(w, "text_size", (sz[0], None)))
            grid.add_widget(lbl)
        sv = ScrollView()
        sv.add_widget(grid)
        Popup(title=f"{title}  ({done}/{len(rows)})", content=sv, size_hint=(0.5, 0.7)).open()
