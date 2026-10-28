"""Health watch - everything that goes wrong WITHOUT a crash: freezes, a stuck call of ours, a black screen, a frozen
picture, an endless level load, a blocking feature that keeps blocking. Crashes are crash_report.py's job.

Three sources, merged into one list of events (newest last), shown by /health and added to every crash bundle:
  * the DLL's watchdog (dungeons_bridge.cpp "Health watch"): the game thread stopped running ProcessEvent (STALL, with a
    HANG REPORT - every thread's top frame - in dungeons_bridge_crash.log), one of OUR game calls is stuck or slow
    (OWNCALL_STUCK / SLOW_CALL), a level transition never ends (LONG_TRANSITION), plus counters of what the blocking
    features swallowed (a count that keeps climbing is a false positive);
  * the game window: Windows' own "not responding" test (UI thread not pumping messages);
  * the screen: the game window grabbed at low resolution every 2 s - all black while the game logic runs (BLACK_SCREEN),
    or not one pixel changing for 20 s (FROZEN_PICTURE, low confidence). Needs Pillow; skipped silently without it.
Every alert says what the DLL was doing at the same moment (state=...), so a loading screen is not mistaken for a fault,
and the first serious alert saves a crash bundle while the game is still frozen (rate limited).
"""
import asyncio
import ctypes
import logging
import sys
import time

import crash_diag

game_logger = logging.getLogger("MCDungeonsGame")

TICK_S = 2.0                 # how often the monitor runs
BUNDLE_COOLDOWN_S = 180.0    # at most one automatic bundle per this long
BLACK_MEAN, BLACK_MAX = 5.0, 30
BLACK_SECONDS = 8.0          # black picture this long while the game logic runs = alert
FROZEN_SECONDS = 20.0        # identical picture this long while the game logic runs = alert (low confidence)
UI_HUNG_SECONDS = 6.0        # window not answering this long = alert (30 s while a level is loading)
EVENT_RING = 60


# ------------------------------------------------------------------ the DLL
def _pipe_request(pm, text):
    import win32file
    import miniboss as mb
    pipe = mb._R()._connect_bridge_pipe(pm)
    try:
        win32file.WriteFile(pipe, text.encode())
        _, data = win32file.ReadFile(pipe, 65536)
    finally:
        win32file.CloseHandle(pipe)
    return data.decode()


def get_health(pm):
    """{'peIdleMs': int, 'silent': int, ...} from the DLL, or None (old DLL / pipe down)."""
    try:
        answer = _pipe_request(pm, "get_health")
    except Exception:
        return None
    if not answer.startswith("HEALTH:"):
        return None
    out = {}
    for part in answer[len("HEALTH:"):].split("|"):
        key, _, value = part.partition("=")
        try:
            out[key] = int(value)
        except ValueError:
            continue
    return out


def get_health_events(pm):
    """[(age_ms, kind, text, value)] drained from the DLL, or None."""
    try:
        answer = _pipe_request(pm, "get_health_events")
    except Exception:
        return None
    if not answer.startswith("EVENTS:"):
        return None
    _count, _, rest = answer[len("EVENTS:"):].partition("|")
    events = []
    for entry in rest.split("|") if rest else []:
        parts = entry.split(",", 3)
        if len(parts) == 4:
            try:
                events.append((int(parts[0]), parts[1], parts[2], int(parts[3])))
            except ValueError:
                continue
    return events


# ------------------------------------------------------------------ the window
def _user32():
    return ctypes.WinDLL("user32", use_last_error=True)


def find_game_window(pid):
    """Handle of the biggest visible top-level window of the process, or None."""
    if sys.platform != "win32" or not pid:
        return None
    from ctypes import wintypes
    user32 = _user32()
    found = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def callback(hwnd, _lparam):
        owner = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd):
            rect = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            found.append(((rect.right - rect.left) * (rect.bottom - rect.top), hwnd))
        return True
    user32.EnumWindows(callback_type(callback), 0)
    return max(found)[1] if found else None


def window_responds(hwnd, timeout_ms=1000):
    """False when the window's thread does not answer within timeout_ms (what Windows calls 'not responding')."""
    from ctypes import wintypes
    result = wintypes.DWORD(0)
    ok = _user32().SendMessageTimeoutW(hwnd, 0, 0, 0, 0x0002, timeout_ms, ctypes.byref(result))   # WM_NULL, SMTO_ABORTIFHUNG
    return bool(ok)


def grab_picture(hwnd):
    """(mean, max, signature) of the game window's client area at 64x36 grey, or None (minimised, not in front,
    no Pillow, or the grab failed). Only the game window's own pixels are read, nothing is stored."""
    try:
        from ctypes import wintypes
        from PIL import ImageGrab
        user32 = _user32()
        if user32.IsIconic(hwnd) or user32.GetForegroundWindow() != hwnd:
            return None
        rect = wintypes.RECT()
        user32.GetClientRect(hwnd, ctypes.byref(rect))
        origin = wintypes.POINT(0, 0)
        user32.ClientToScreen(hwnd, ctypes.byref(origin))
        width, height = rect.right - rect.left, rect.bottom - rect.top
        if width < 200 or height < 120:
            return None
        image = ImageGrab.grab(bbox=(origin.x, origin.y, origin.x + width, origin.y + height))
        small = image.resize((64, 36)).convert("L")
        data = bytes(small.getdata())
        return sum(data) / len(data), max(data), hash(data)
    except Exception:
        return None


# ------------------------------------------------------------------ the monitor
class Monitor:
    def __init__(self):
        self.events = []                  # [(time, source, kind, text)]
        self.last_metrics = None
        self.last_tick = 0.0
        self.hung_since = None
        self.hung_reported = False
        self.black_since = None
        self.black_reported = False
        self.same_since = None
        self.last_signature = None
        self.frozen_reported = False
        self.last_bundle = 0.0
        self.warned_no_pillow = False
        self.last_blocked = {}
        self.blocked_reported = {}

    def add(self, source, kind, text):
        self.events.append((time.time(), source, kind, text))
        del self.events[:-EVENT_RING]


def monitor_of(ctx):
    return ctx.__dict__.setdefault("health_monitor", Monitor())


def recent_events(ctx, count=20):
    mon = getattr(ctx, "health_monitor", None)
    if not mon:
        return []
    return [f"{time.strftime('%H:%M:%S', time.localtime(t))} [{src}] {kind}: {text}" for t, src, kind, text in mon.events[-count:]]


def _alert(ctx, mon, source, kind, text, bundle=False):
    mon.add(source, kind, text)
    game_logger.warning(f"HEALTH {kind}: {text}")
    crash_diag.journal("HEALTH", f"{kind}: {text}")
    if bundle and time.time() - mon.last_bundle >= BUNDLE_COOLDOWN_S:
        mon.last_bundle = time.time()
        crash_diag.bundle_in_background(f"health alert {kind}", delay_s=2.0)


def _sample_window(pid, mon, loading, game_running):
    """Blocking part (window calls + screen grab) - run in a worker thread. Returns a list of (kind, text, bundle)."""
    alerts = []
    hwnd = find_game_window(pid)
    if not hwnd:
        return alerts
    now = time.time()
    # 1. the window answers messages?
    if window_responds(hwnd):
        mon.hung_since, mon.hung_reported = None, False
    else:
        mon.hung_since = mon.hung_since or now
        limit = 30.0 if loading else UI_HUNG_SECONDS
        if now - mon.hung_since >= limit and not mon.hung_reported:
            mon.hung_reported = True
            alerts.append(("UI_NOT_RESPONDING",
                           f"the game window has not answered for {now - mon.hung_since:.0f} s "
                           f"({'while a level is loading' if loading else 'game logic active - a real freeze'})", True))
    # 2. black / frozen picture - only meaningful while the game logic runs
    picture = grab_picture(hwnd) if game_running else None
    if picture is None:
        mon.black_since = mon.same_since = None
        mon.black_reported = mon.frozen_reported = False
        return alerts
    mean, peak, signature = picture
    if mean < BLACK_MEAN and peak < BLACK_MAX:
        mon.black_since = mon.black_since or now
        if now - mon.black_since >= BLACK_SECONDS and not mon.black_reported:
            mon.black_reported = True
            alerts.append(("BLACK_SCREEN", f"the picture has been black for {now - mon.black_since:.0f} s while the "
                           f"game logic runs (pawn present, hook active) - not a loading screen", True))
    else:
        mon.black_since, mon.black_reported = None, False
    if signature == mon.last_signature:
        mon.same_since = mon.same_since or now
        if now - mon.same_since >= FROZEN_SECONDS and not mon.frozen_reported:
            mon.frozen_reported = True
            alerts.append(("FROZEN_PICTURE", f"not one pixel changed for {now - mon.same_since:.0f} s while the game "
                           f"logic runs (low confidence: a still menu looks the same)", False))
    else:
        mon.same_since, mon.frozen_reported = None, False
    mon.last_signature = signature
    return alerts


async def health_tick(ctx, pm):
    """Called from the client loop; does real work every TICK_S seconds."""
    mon = monitor_of(ctx)
    now = time.time()
    if now - mon.last_tick < TICK_S:
        return
    mon.last_tick = now

    async with ctx.bridge_pipe_lock:
        events = await asyncio.to_thread(get_health_events, pm)
        metrics = await asyncio.to_thread(get_health, pm)
    if metrics is not None:
        mon.last_metrics = metrics
    for age_ms, kind, text, value in events or []:
        _alert(ctx, mon, "DLL", kind, text, bundle=kind in ("STALL", "OWNCALL_STUCK"))

    # false-positive watch: a blocking feature whose counter keeps climbing
    if metrics:
        for key in ("blockedPickup", "blockedEye", "blockedSecret", "blockedMerchant"):
            count = metrics.get(key, 0)
            if count - mon.blocked_reported.get(key, 0) >= 200:     # 200 more swallowed calls since the last warning
                mon.blocked_reported[key] = count
                _alert(ctx, mon, "DLL", "BLOCK_COUNT",
                       f"{key} has swallowed {count} game calls so far - if you cannot do something you should be able "
                       f"to, this feature is blocking it by mistake")

    loading = bool(metrics and metrics.get("silent"))
    stalled = bool(metrics and metrics.get("stalled"))
    game_running = bool(metrics) and not loading and not stalled and bool(getattr(ctx, "pawn_present", False))
    alerts = await asyncio.to_thread(_sample_window, pm.process_id, mon, loading, game_running)
    for kind, text, bundle in alerts:
        state = (f" [state: silent={metrics.get('silent')} stalled={metrics.get('stalled')} peIdleMs={metrics.get('peIdleMs')}]"
                 if metrics else " [state: DLL not answering]")
        _alert(ctx, mon, "client", kind, text + state, bundle=bundle)


def status_lines(ctx):
    mon = monitor_of(ctx)
    m = mon.last_metrics
    lines = ["Health watch:"]
    if m is None:
        lines.append("  the DLL does not answer get_health - rebuild dungeons_bridge.dll from the current .cpp")
    else:
        lines.append(f"  game thread: last ProcessEvent activity {m.get('peIdleMs', 0) / 1000:.1f} s ago, "
                     f"{'LOADING (hook silent)' if m.get('silent') else 'hook active'}"
                     f"{', STALLED' if m.get('stalled') else ''}")
        lines.append(f"  our game calls: slowest {m.get('maxCallMs', 0)} ms, {m.get('slowCalls', 0)} slow (>=150 ms)"
                     f"{', one running for ' + str(m['ownCallMs']) + ' ms' if m.get('ownCallMs') else ''}")
        lines.append(f"  swallowed by blocking features: pickups {m.get('blockedPickup', 0)}, eyes {m.get('blockedEye', 0)}, "
                     f"secrets {m.get('blockedSecret', 0)}, merchants {m.get('blockedMerchant', 0)}; "
                     f"crash-guard patches {m.get('guardHits', 0)}; in this level for {m.get('inLevelMs', 0) / 60000:.1f} min")
    events = recent_events(ctx, 12)
    lines.append("  recent events:" if events else "  no health event so far")
    lines.extend(f"    {e}" for e in events)
    return lines
