"""'Map' tab for the Minecraft Dungeons client.

Shows the Mainland map with one pin per zone (plus one for the Bonus Chests
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
from kivy.uix.spinner import Spinner
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
        "size": (3073, 1566),
        "pins": {
            "squidcoast": (543, 930), "creeperwoods": (854, 678), "creepycrypt": (611, 581),
            "pumpkinpastures": (1409, 1098), "archhaven": (1798, 1112), "soggyswamp": (885, 1136),
            "soggycave": (883, 954), "redstonemines": (1369, 521), "fieryforge": (1913, 354),
            "cacticanyon": (1328, 871), "deserttemple": (1936, 740), "lowertemple": (2003, 889),
            "highblockhalls": (2315, 680), "underhalls": (2618, 759), "obsidianpinnacle": (2646, 432),
            "mooshroomisland": (805, 361),
            # icons drawn into the image; pins sit just under them
            "Bonus Chests": (2620, 1225), "Emerald Milestones": (2860, 1225),
        },
    },
]

PIN_SIZE, PIN_BORDER = 76, 6      # in image pixels (scale with zoom)

COL_DONE = (0.45, 0.45, 0.45, 1)   # gray  : zone finished / no checks in this slot
COL_LOGIC = (0.20, 0.72, 0.25, 1)  # green : in logic, doable now
COL_HARD = (0.95, 0.62, 0.10, 1)   # orange: hard but feasible (grind / excluded from logic)
COL_OUT = (0.80, 0.22, 0.22, 1)    # red   : out of logic (needs a bug/skip to do)

# Groups the apworld excludes from the fill logic (long grinds / post-game):
# they are reachable, but hard -> orange instead of green. Edit freely.
HARD_GROUPS = {"Bonus Chests", "Emerald Milestones", "??? (Mooshroom Island)"}

_SKIP = _zd.skip_level_requires_map()


def zone_items_needed(zone, secret_opt):
    """Access items the apworld's make_zone_rule() requires for this zone."""
    names = [f"{_zd.ZONES_BY_NAME[zone].display_name} Access"]
    for req in _SKIP.get(zone, []):
        rz = _zd.ZONES_BY_NAME[req]
        names.append(f"{rz.display_name} Access")
        if rz.secret and secret_opt:
            names.append(f"{rz.display_name} Secret Access")
    return names


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
    supply chests, boss first kill, mission complete."""
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
                rows.append(("Mission Complete", A.LOCATION_TABLE[A.LOCATIONS_BY_ZONE[zone]]))
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

    def on_touch_down(self, touch):
        if self.collide_point(*touch.pos) and getattr(self, "on_user", None):
            self.on_user()
        touch.ud["mp_start"] = touch.pos
        return super().on_touch_down(touch)

    def on_touch_up(self, touch):
        start = touch.ud.get("mp_start")
        res = super().on_touch_up(touch)
        if (start and not touch.is_mouse_scrolling and abs(touch.x - start[0]) < dp(8)
                and abs(touch.y - start[1]) < dp(8) and self.collide_point(*touch.pos)):
            self.on_tap(*self.to_local(*touch.pos))
        return res


class Viewport(StencilView, FloatLayout):
    pass


class MapTab(BoxLayout):
    PREFIX = "Current Map: "

    def __init__(self, ctx, **kwargs):
        super().__init__(orientation="vertical", **kwargs)
        self.ctx = ctx
        self.groups = build_groups()
        self.title_zone = {_title(z): z for m in MAPS for z in m["pins"] if z in _zd.ZONES_BY_NAME}
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
                               size_hint_x=None, width=dp(240))
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

    def _in_logic(self, title):
        zone = self.title_zone.get(title)
        if zone is None:  # Bonus Chests / Emerald Milestones: no access rule
            return True
        ctx = self.ctx
        got = {ni.item for ni in (getattr(ctx, "items_received", None) or [])}
        secret_opt = bool((getattr(ctx, "slot_data", None) or {}).get("secret_missions_require_secret"))
        for name in zone_items_needed(zone, secret_opt):
            info = _items.ITEM_TABLE.get(name)
            if info is not None and info.code not in got:
                return False
        return True

    def _color(self, title, total, done):
        if total == 0 or done == total:
            return COL_DONE
        if not self._in_logic(title):
            return COL_OUT
        return COL_HARD if title in HARD_GROUPS else COL_LOGIC

    def refresh(self, *_):
        allids, checked = self._slot_ids()
        for p in self.pins:
            rows, done = self._stats(p.title, allids, checked)
            total = len(rows)
            p.set_state("-" if total == 0 else f"{done}/{total}", self._color(p.title, total, done))
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
