"""Crash diagnostics for the Minecraft Dungeons client.

  * mutation journal  - every call that CHANGES something in the game (item/currency grants, kills, DLL switches)
                        is recorded with the state of the game at that moment (zone, loading, pawn). The last
                        entries are dumped to the log when a guard storm starts, when a player types "bug" in the
                        chat, and when the game process disappears. Everything is also appended to
                        mutation_journal.log next to this file (rotated at ~512 KB).
  * process stats     - working set / private bytes / threads / handles of Dungeons.exe plus the DLL guard hit
                        counter, sampled every ~15 s, to spot a leak or a thread build-up BEFORE a black screen.
  * safety gate       - safe_to_mutate() is False for a few seconds after a level change, during a guard storm and
                        while the circuit breaker is open (3 failed game changes within 30 s). Callers simply keep
                        their work queued and try again later.
  * crash bundle      - one zip (crash_bundles/) with the journal, the stats history, the tail of the DLL log, the
                        tail of the Archipelago client log and the newest Unreal crash report, written ~15 s after
                        the game process disappears (the crash reporter needs a few seconds to write its files).

Nothing in here ever raises into the caller: diagnostics must never be the thing that breaks the client.
"""
import collections
import ctypes
import functools
import glob
import logging
import os
import sys
import threading
import time
import zipfile
from pathlib import Path

game_logger = logging.getLogger("MCDungeonsGame")

CLIENT_DIR = Path(__file__).resolve().parent
JOURNAL_FILE = CLIENT_DIR / "mutation_journal.log"
DLL_LOG_FILE = CLIENT_DIR / "dungeons_bridge_debug.log"   # the DLL writes its log next to itself
BUNDLE_DIR = CLIENT_DIR / "crash_bundles"

JOURNAL_MAX_BYTES = 512 * 1024
POST_LOAD_GRACE_S = 5.0        # no game changes this long after a level change
BREAKER_FAILURES = 3           # failed game changes ...
BREAKER_WINDOW_S = 30.0        # ... within this window ...
BREAKER_OPEN_S = 60.0          # ... pause all game changes this long
STORM_HITS_PER_SAMPLE = 50     # guard hits between two samples that count as a storm
STORM_PAUSE_S = 30.0
STATS_LOG_EVERY = 4            # log one PROCESS STATS line every N samples (always logged during a storm)
KEEP_BUNDLES = 10

_lock = threading.RLock()
_ring = collections.deque(maxlen=300)
_stats_history = collections.deque(maxlen=240)
_state_provider = None
_level_changed_at = 0.0
_failures = collections.deque()
_breaker_open_until = 0.0
_storm_until = 0.0
_storm_dumped = False
_last_guard_hits = None
_sample_count = 0
_last_block_log = 0.0


# ------------------------------------------------------------------------------------------ journal
def set_state_provider(fn):
    """fn() -> small dict describing the game state right now (zone, loading, pawn ...)."""
    global _state_provider
    _state_provider = fn


def _state():
    try:
        return dict(_state_provider()) if _state_provider else {}
    except Exception:
        return {}


def _write_file_line(line):
    try:
        if JOURNAL_FILE.exists() and JOURNAL_FILE.stat().st_size > JOURNAL_MAX_BYTES:
            old = JOURNAL_FILE.with_name(JOURNAL_FILE.name + ".1")
            try:
                old.unlink()
            except OSError:
                pass
            JOURNAL_FILE.replace(old)
        with open(JOURNAL_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def journal(kind, detail=""):
    """Appends one line to the ring buffer and to mutation_journal.log."""
    try:
        t = time.time()
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)) + f".{int(t % 1 * 1000):03d}"
        state = " ".join(f"{k}={v}" for k, v in _state().items())
        line = f"{stamp} [{kind}] {str(detail)[:300]} | {state}"
        with _lock:
            _ring.append(line)
            _write_file_line(line)
    except Exception:
        pass


def _short(value):
    try:
        text = repr(value)
    except Exception:
        text = "<unrepr>"
    return text if len(text) <= 60 else text[:57] + "..."


# These legitimately answer (False, ...) all the time (no pawn yet, retry loops, DLL not rebuilt): journaled, but a
# soft failure never counts towards the circuit breaker (an exception still does).
_NO_SOFT_FAIL = {"kill_local_player", "set_level_gate", "bridge_set_crash_guard"}


def journaled(fn):
    """Decorator for every helper that changes something in the game. Records the call, its result, its
    duration and any exception (re-raised untouched), and feeds the circuit breaker."""
    name = fn.__name__

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        shown = [_short(a) for a in args[1:]]            # args[0] is the pymem handle in every such helper
        shown += [f"{k}={_short(v)}" for k, v in kwargs.items()]
        call = f"{name}({', '.join(shown)})"
        t0 = time.perf_counter()
        try:
            result = fn(*args, **kwargs)
        except Exception as e:
            _record_failure()
            journal("FAIL", f"{call} raised {type(e).__name__}: {e} after {1000 * (time.perf_counter() - t0):.0f} ms")
            raise
        journal("MUT", f"{call} -> {_short(result)} in {1000 * (time.perf_counter() - t0):.0f} ms")
        if name not in _NO_SOFT_FAIL and isinstance(result, tuple) and result and result[0] is False:
            _record_failure()   # a normal (False, reason) answer counts as a failed change
        return result

    return wrapper


def dump_journal(reason, n=40):
    """Writes the last n journal lines to the client log."""
    try:
        with _lock:
            tail = list(_ring)[-n:]
        game_logger.info(f"MUTATION JOURNAL ({reason}) - last {len(tail)} game change(s):")
        for line in tail:
            game_logger.info(f"  {line}")
    except Exception:
        pass


# ------------------------------------------------------------------------------------------ safety gate
def _record_failure():
    global _breaker_open_until
    now = time.time()
    with _lock:
        _failures.append(now)
        while _failures and now - _failures[0] > BREAKER_WINDOW_S:
            _failures.popleft()
        if len(_failures) >= BREAKER_FAILURES and now >= _breaker_open_until:
            _breaker_open_until = now + BREAKER_OPEN_S
            journal("BREAKER", f"{len(_failures)} failed game changes within {BREAKER_WINDOW_S:.0f}s - "
                               f"pausing grants for {BREAKER_OPEN_S:.0f}s")


EXTRA_SUMMARY = []   # callables returning lines for SUMMARY.txt (health events, suspicious checks) - set by the client


def note_level_change(zone=None):
    """Call when the zone/level changed: opens the post-load grace period."""
    global _level_changed_at, _storm_dumped
    _level_changed_at = time.time()
    _storm_dumped = False
    journal("LEVEL", f"zone -> {zone}")


def mutations_allowed():
    """(ok, reason). Pure check, no side effects."""
    now = time.time()
    if now < _breaker_open_until:
        return False, f"circuit breaker open for {_breaker_open_until - now:.0f}s more (repeated failed game changes)"
    if now < _storm_until:
        return False, "a crash-guard storm is in progress (the game is in a bad state)"
    if now - _level_changed_at < POST_LOAD_GRACE_S:
        return False, "the level just changed"
    return True, ""


def safe_to_mutate():
    """Boolean form of mutations_allowed() that also logs (at most every 15 s) why changes are on hold."""
    global _last_block_log
    ok, why = mutations_allowed()
    if not ok:
        now = time.time()
        if now - _last_block_log > 15.0:
            _last_block_log = now
            game_logger.info(f"Game changes on hold: {why}. They stay queued and are applied afterwards.")
    return ok


# ------------------------------------------------------------------------------------------ process stats
def sample_process_stats(pid):
    """{'ws_mb', 'private_mb', 'handles', 'threads'} for the process, or None (non-Windows / no access)."""
    if sys.platform != "win32" or not pid:
        return None
    try:
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)

        class PMC(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
                        ("PrivateUsage", ctypes.c_size_t)]

        class THREADENTRY32(ctypes.Structure):
            _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ThreadID", wintypes.DWORD),
                        ("th32OwnerProcessID", wintypes.DWORD), ("tpBasePri", wintypes.LONG),
                        ("tpDeltaPri", wintypes.LONG), ("dwFlags", wintypes.DWORD)]

        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        k32.GetProcessHandleCount.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(PMC), wintypes.DWORD]
        k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        k32.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(THREADENTRY32)]
        k32.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(THREADENTRY32)]

        handle = k32.OpenProcess(0x0410, False, int(pid))      # QUERY_INFORMATION | VM_READ
        if not handle:
            handle = k32.OpenProcess(0x1000, False, int(pid))  # QUERY_LIMITED_INFORMATION
        if not handle:
            return None
        try:
            pmc = PMC()
            pmc.cb = ctypes.sizeof(PMC)
            if not psapi.GetProcessMemoryInfo(handle, ctypes.byref(pmc), pmc.cb):
                return None
            handles = wintypes.DWORD(0)
            k32.GetProcessHandleCount(handle, ctypes.byref(handles))
        finally:
            k32.CloseHandle(handle)

        threads = 0
        snap = k32.CreateToolhelp32Snapshot(0x4, 0)            # TH32CS_SNAPTHREAD
        if snap and snap != ctypes.c_void_p(-1).value:
            try:
                entry = THREADENTRY32()
                entry.dwSize = ctypes.sizeof(THREADENTRY32)
                ok = k32.Thread32First(snap, ctypes.byref(entry))
                while ok:
                    if entry.th32OwnerProcessID == int(pid):
                        threads += 1
                    ok = k32.Thread32Next(snap, ctypes.byref(entry))
            finally:
                k32.CloseHandle(snap)
        return {"ws_mb": round(pmc.WorkingSetSize / 1048576, 1), "private_mb": round(pmc.PrivateUsage / 1048576, 1),
                "handles": int(handles.value), "threads": threads}
    except Exception:
        return None


def periodic(pid, guard_hits=None):
    """Called every ~15 s from the client loop (in a worker thread): samples the process, remembers the history,
    logs a PROCESS STATS line every few samples, and detects a crash-guard storm from the guard hit counter."""
    global _sample_count, _last_guard_hits, _storm_until, _storm_dumped
    try:
        stats = sample_process_stats(pid) or {}
        now = time.time()
        storm = False
        delta = 0
        if guard_hits is not None:
            if _last_guard_hits is not None and guard_hits >= _last_guard_hits:
                delta = guard_hits - _last_guard_hits
                storm = delta >= STORM_HITS_PER_SAMPLE
            _last_guard_hits = guard_hits
        state = _state()
        record = dict(stats)
        record.update({"t": now, "guard_hits": guard_hits, "guard_delta": delta, "zone": state.get("zone")})
        with _lock:
            _stats_history.append(record)
        _sample_count += 1
        if storm:
            _storm_until = now + STORM_PAUSE_S
            journal("STORM", f"{delta} crash-guard hits since the last sample (total {guard_hits}) - pausing game changes")
        if storm or _sample_count % STATS_LOG_EVERY == 1:
            parts = [f"{k}={v}" for k, v in stats.items()]
            game_logger.info("PROCESS STATS: " + " ".join(parts) +
                             (f" guard_hits={guard_hits} (+{delta})" if guard_hits is not None else "") +
                             (f" zone={state.get('zone')}" if state.get("zone") else ""))
        if storm and not _storm_dumped:
            _storm_dumped = True
            dump_journal("crash-guard storm started")
    except Exception:
        pass


# ------------------------------------------------------------------------------------------ crash bundle
def _tail_lines(path, n):
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            f.seek(max(0, size - 600_000))
            data = f.read()
        return b"\n".join(data.splitlines()[-n:]).decode("utf-8", errors="replace")
    except Exception as e:
        return f"<could not read {path}: {e}>"


def _local_app_roots():
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        return []
    base = Path(local)
    roots = [base / "Dungeons" / "Saved"]
    roots += [Path(p) / "LocalCache" / "Local" / "Dungeons" / "Saved"
              for p in glob.glob(str(base / "Packages" / "*"))]
    return [r for r in roots if r.exists()]


def _newest_files(pattern_dir, suffix, since):
    out = []
    try:
        for p in Path(pattern_dir).glob("*" + suffix):
            if p.is_file() and p.stat().st_mtime >= since:
                out.append(p)
    except Exception:
        pass
    return sorted(out, key=lambda p: p.stat().st_mtime)


def collect_crash_bundle(reason, since_s=1800):
    """Zips everything useful about the last minutes. Returns (path or None, found_unreal_crash_report)."""
    try:
        BUNDLE_DIR.mkdir(exist_ok=True)
        now = time.time()
        stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(now))
        path = BUNDLE_DIR / f"crash_{stamp}.zip"
        found_crash = False
        total = 0
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
            with _lock:
                ring_text = "\n".join(_ring)
                stats_text = "\n".join(
                    f"{time.strftime('%H:%M:%S', time.localtime(r['t']))} " +
                    " ".join(f"{k}={v}" for k, v in r.items() if k != "t") for r in _stats_history)
            z.writestr("README.txt", f"Minecraft Dungeons crash bundle\nreason: {reason}\ncreated: "
                                     f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now))}\n")
            z.writestr("mutation_journal_tail.txt", ring_text)
            z.writestr("process_stats_history.txt", stats_text)
            if DLL_LOG_FILE.exists():
                z.writestr("dungeons_bridge_debug_tail.log", _tail_lines(DLL_LOG_FILE, 1500))
            crash_log = CLIENT_DIR / "dungeons_bridge_crash.log"
            if crash_log.exists():
                z.writestr("dungeons_bridge_crash_tail.log", _tail_lines(crash_log, 600))
            try:
                import crash_report   # plain-language verdict + resolved stack - read this FIRST
                summary = crash_report.summarize(CLIENT_DIR, max_reports=3)
                for provider in EXTRA_SUMMARY:
                    try:
                        summary += "\n\n" + "\n".join(provider())
                    except Exception as e:
                        summary += f"\n\n(a summary section failed: {e})"
                z.writestr("SUMMARY.txt", summary)
            except Exception as e:
                z.writestr("SUMMARY.txt", f"could not build the summary: {e}")
            try:
                from Utils import user_path   # Archipelago's own helper
                logs = sorted(Path(user_path("logs")).glob("*.txt"), key=lambda p: p.stat().st_mtime)
                if logs:
                    z.writestr(f"client_log_tail_{logs[-1].name}", _tail_lines(logs[-1], 3000))
            except Exception:
                pass
            for root in _local_app_roots():
                crashes = root / "Crashes"
                if crashes.exists():
                    for folder in sorted(crashes.iterdir(), key=lambda p: p.stat().st_mtime)[-3:]:
                        if folder.is_dir() and folder.stat().st_mtime >= now - since_s:
                            for f in folder.iterdir():
                                if f.is_file() and f.stat().st_size <= 8_000_000 and total < 20_000_000:
                                    z.write(f, f"unreal_crash/{folder.name}/{f.name}")
                                    total += f.stat().st_size
                                    found_crash = True
                for lg in _newest_files(root / "Logs", ".log", now - since_s)[-1:]:
                    z.writestr(f"unreal_log_tail_{lg.name}", _tail_lines(lg, 2000))
        bundles = sorted(BUNDLE_DIR.glob("crash_*.zip"), key=lambda p: p.stat().st_mtime)
        for old in bundles[:-KEEP_BUNDLES]:
            try:
                old.unlink()
            except OSError:
                pass
        return path, found_crash
    except Exception as e:
        game_logger.info(f"CRASH BUNDLE failed: {e}")
        return None, False


def bundle_in_background(reason, delay_s=0.0):
    """Builds the bundle on a worker thread after `delay_s` and logs where it went."""
    def work():
        if delay_s:
            time.sleep(delay_s)
        path, found = collect_crash_bundle(reason)
        if path:
            game_logger.info(f"CRASH BUNDLE ({reason}): {path}" +
                             ("  [includes the Unreal crash report]" if found else
                              "  [no Unreal crash report found - the game probably closed normally]"))
    threading.Thread(target=work, daemon=True, name="CrashBundle").start()


def on_game_gone(pid=None, reason="game process exited"):
    """Call when the game process is no longer running."""
    journal("GONE", f"{reason} (pid {pid})")
    dump_journal(reason)
    bundle_in_background(reason, delay_s=15.0)
