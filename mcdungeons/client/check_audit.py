"""Check audit - every check the client sends, with the evidence, so a FALSE POSITIVE (a check that fired when you did
not do the thing) can be traced to its cause in seconds.

MCDungeonsContext.check_locations() calls record() before sending. Each record keeps: when, which locations (names),
which code asked (module.function:line - the first frame inside the client folder), the zone, and flags for what looks
off. Flags are heuristics, not verdicts:
    BURST        4+ locations within 3 s (a bulk send: a load-time sweep, an event replay) - the usual false-positive shape
    JUST_LOADED  within 15 s of a level change (state still settling)
    NO_PAWN      no player character in the world when it fired
    DURING_LOAD  the DLL reported the hook silent (level loading) at that moment
    REPEAT       the same location was sent less than 30 s ago
    KNOWN        the server already has it as checked
    NOT_IN_SEED  the location id is not in this seed's location list
Flagged checks are logged as warnings, every check goes to check_audit.log, the flagged ones go into every crash
bundle's SUMMARY.txt, and /audit shows them.
"""
import logging
import sys
import time
from pathlib import Path

import crash_diag
from _apworld_data import Locations as _apw

game_logger = logging.getLogger("MCDungeonsGame")

CLIENT_DIR = Path(__file__).resolve().parent
AUDIT_LOG = CLIENT_DIR / "check_audit.log"
AUDIT_LOG_MAX = 256 * 1024
RING = 120
BURST_COUNT, BURST_WINDOW_S = 4, 3.0
JUST_LOADED_S = 15.0
REPEAT_S = 30.0

_NAME_BY_ID = {location_id: name for name, location_id in _apw.LOCATION_TABLE.items()}


def _caller():
    """'module.function:line' of the first frame inside the client folder that is not this module or the context's
    check_locations wrapper - i.e. the feature that asked for the check."""
    frame = sys._getframe(2)
    while frame:
        code = frame.f_code
        try:
            inside = Path(code.co_filename).resolve().parent == CLIENT_DIR
        except OSError:
            inside = False
        if inside and Path(code.co_filename).name != "check_audit.py" and code.co_name != "check_locations":
            return f"{Path(code.co_filename).stem}.{code.co_name}:{frame.f_lineno}"
        frame = frame.f_back
    return "unknown"


def _append_log(line):
    try:
        if AUDIT_LOG.exists() and AUDIT_LOG.stat().st_size > AUDIT_LOG_MAX:
            AUDIT_LOG.replace(AUDIT_LOG.with_suffix(".old"))
        with open(AUDIT_LOG, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass


def record(ctx, locations):
    """Call right before sending `locations`. Never raises."""
    try:
        now = time.time()
        ids = list(locations)
        if not ids:
            return
        ring = ctx.__dict__.setdefault("check_audit_ring", [])
        recent_sent = ctx.__dict__.setdefault("check_audit_recent", {})
        # burst: how many locations were sent in the last BURST_WINDOW_S, this one included
        window = [t for t, _loc in ring if now - t <= BURST_WINDOW_S]
        burst = len(window) + len(ids) >= BURST_COUNT
        flags = []
        if burst:
            flags.append("BURST")
        if now - crash_diag._level_changed_at < JUST_LOADED_S:
            flags.append("JUST_LOADED")
        if getattr(ctx, "pawn_present", True) is False:
            flags.append("NO_PAWN")
        monitor = getattr(ctx, "health_monitor", None)
        if monitor and monitor.last_metrics and monitor.last_metrics.get("silent"):
            flags.append("DURING_LOAD")
        checked = getattr(ctx, "checked_locations", set())
        server = getattr(ctx, "server_locations", None)
        names = []
        for location_id in ids:
            names.append(_NAME_BY_ID.get(location_id, f"#{location_id}"))
            if now - recent_sent.get(location_id, 0.0) < REPEAT_S and "REPEAT" not in flags:
                flags.append("REPEAT")
            if location_id in checked and "KNOWN" not in flags:
                flags.append("KNOWN")
            if server and location_id not in server and "NOT_IN_SEED" not in flags:
                flags.append("NOT_IN_SEED")
            recent_sent[location_id] = now
            ring.append((now, location_id))
        del ring[:-RING]
        entry = {"t": now, "names": names, "caller": _caller(), "zone": (getattr(ctx, "game_state", {}) or {}).get("zone"),
                 "flags": flags}
        history = ctx.__dict__.setdefault("check_audit_history", [])
        history.append(entry)
        del history[:-RING]
        line = (f"{time.strftime('%H:%M:%S', time.localtime(now))} zone={entry['zone']} by {entry['caller']}: "
                f"{', '.join(names)}" + (f"  [{' '.join(flags)}]" if flags else ""))
        _append_log(line)
        if flags and any(f in flags for f in ("BURST", "NO_PAWN", "DURING_LOAD", "NOT_IN_SEED", "JUST_LOADED")):
            game_logger.warning(f"CHECK AUDIT - suspicious check: {line}")
            crash_diag.journal("AUDIT", line)
    except Exception:
        pass


def suspects(ctx, count=10):
    history = getattr(ctx, "check_audit_history", [])
    flagged = [e for e in history if e["flags"]]
    return [_format(e) for e in flagged[-count:]]


def _format(entry):
    return (f"{time.strftime('%H:%M:%S', time.localtime(entry['t']))} zone={entry['zone']} by {entry['caller']}: "
            f"{', '.join(entry['names'])}" + (f"  [{' '.join(entry['flags'])}]" if entry["flags"] else ""))


def audit_lines(ctx, only_flagged=False, count=15):
    history = getattr(ctx, "check_audit_history", [])
    rows = [e for e in history if e["flags"]] if only_flagged else history
    if not rows:
        return ["No checks sent this session" + (" with a flag." if only_flagged else " yet.")]
    return [f"Last {min(count, len(rows))} check(s) sent{' (flagged only)' if only_flagged else ''}:"] + \
           [f"  {_format(e)}" for e in rows[-count:]]
