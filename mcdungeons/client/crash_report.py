"""Reads what dungeons_bridge.dll's crash reporter wrote (dungeons_bridge_crash.log) and says WHO is the likely culprit,
in plain text, in a few lines - so a crash can be understood in seconds instead of by reading logs.

What it does, in order:
  1. finds the last CRASH REPORT in dungeons_bridge_crash.log (see the "Crash reporter" sections of dungeons_bridge.cpp);
  2. checks that the DLL file on disk is the build that wrote it (the DBBUILD: marker compiled into the DLL) - a report
     from another build cannot be resolved and is the first thing to suspect;
  3. turns every "dungeons_bridge.dll+0xRVA" into a function name using the build's .map file
     (dungeons_bridge_<build>.map, or dungeons_bridge.map) - see finish_build.py, which saves it at build time;
  4. applies a few rules to name the culprit: our own protected call (and WHICH game function it was), our own code,
     the game's code right after one of our calls, a level change, ...

Pure Python, no game needed: python crash_report.py [client folder]   prints the summary of the newest report.
"""
import bisect
import re
import sys
from pathlib import Path

CLIENT_DIR = Path(__file__).resolve().parent
CRASH_LOG_NAME = "dungeons_bridge_crash.log"
DLL_NAME = "dungeons_bridge.dll"
BUILD_MARKER = b"DBBUILD:"
REQUIRED_FEATURES = {"merchants", "crashreport", "owncall", "crumbs", "health"}   # what THIS client expects of the DLL


# ------------------------------------------------------------------ build identity
def read_dll_build(dll_path):
    """The build id compiled into the DLL file (e.g. 'Oct 16 2026 07:24:10'), or None."""
    try:
        data = Path(dll_path).read_bytes()
    except OSError:
        return None
    i = data.find(BUILD_MARKER)
    if i < 0:
        return None
    end = data.find(b"\0", i)
    return data[i + len(BUILD_MARKER):end].decode("ascii", errors="replace")


def build_slug(build_id):
    """'Oct 16 2026 07:24:10' -> 'Oct_16_2026_07_24_10' (file-name safe; used for the .map file name)."""
    return re.sub(r"[^A-Za-z0-9]+", "_", (build_id or "").strip()).strip("_")


# ------------------------------------------------------------------ .map files
class MapFile:
    """Symbols of an MSVC linker .map file (/MAP): resolves an RVA to the function that contains it."""
    _LINE = re.compile(r"^\s*[0-9A-Fa-f]{4}:[0-9A-Fa-f]{8}\s+(\S+)\s+([0-9A-Fa-f]{16})\s+(f\s+)?")
    _BASE = re.compile(r"Preferred load address is\s+([0-9A-Fa-f]+)")

    def __init__(self, path):
        self.path = Path(path)
        text = self.path.read_text(encoding="utf-8", errors="replace")
        match = self._BASE.search(text)
        self.base = int(match.group(1), 16) if match else 0x180000000
        symbols = []
        for line in text.splitlines():
            m = self._LINE.match(line)
            if m:
                address = int(m.group(2), 16)
                if address >= self.base:
                    symbols.append((address - self.base, m.group(1), bool(m.group(3))))
        symbols.sort()
        self.rvas = [s[0] for s in symbols]
        self.symbols = symbols

    @staticmethod
    def demangle(name):
        """Light demangling of MSVC names: '?Method@Class@@YAXXZ' -> 'Class::Method'. Anything else is returned as is."""
        if not name.startswith("?"):
            return name
        head = name[1:].split("@@", 1)[0]
        parts = [p for p in head.split("@") if p]
        return "::".join(reversed(parts)) if parts else name

    def resolve(self, rva):
        """'FunctionName+0x1f' for the symbol at or before `rva`, or None."""
        i = bisect.bisect_right(self.rvas, rva) - 1
        if i < 0:
            return None
        start, name, _is_function = self.symbols[i]
        if rva - start > 0x4000:       # too far from any symbol: the map does not match this build
            return None
        return f"{self.demangle(name)}+0x{rva - start:x}"


def find_map(client_dir, build_id):
    """(MapFile or None, note). Prefers dungeons_bridge_<build>.map, falls back to dungeons_bridge.map."""
    client_dir = Path(client_dir)
    if build_id:
        exact = client_dir / f"dungeons_bridge_{build_slug(build_id)}.map"
        if exact.exists():
            return MapFile(exact), f"map for build {build_id}"
    generic = client_dir / "dungeons_bridge.map"
    if generic.exists():
        return MapFile(generic), "dungeons_bridge.map (NOT verified to belong to this build)"
    return None, ("no .map file: build the DLL with the linker option /MAP and run finish_build.py, "
                  "then frames of dungeons_bridge.dll show as function names")


# ------------------------------------------------------------------ the crash log
_REPORT_SPLIT = re.compile(r"^==== CRASH REPORT #(\d+)", re.M)
_HANG_SPLIT = re.compile(r"^==== HANG REPORT sample (\d+)", re.M)
_MODULE_OFFSET = re.compile(r"(dungeons_bridge\.dll)\+0x([0-9a-fA-F]+)")


def parse_reports(text):
    """[dict] one per CRASH REPORT block, oldest first: number, build, exception, at, thread, own_call, our_call,
    stack (list of 'module+0x..'), crumbs (list of str), raw."""
    reports = []
    starts = [m.start() for m in _REPORT_SPLIT.finditer(text)]
    for k, start in enumerate(starts):
        end_marker = text.find("==== END ====", start)
        end = end_marker if end_marker >= 0 else (starts[k + 1] if k + 1 < len(starts) else len(text))
        raw = text[start:end]
        report = {"raw": raw, "stack": [], "crumbs": []}
        head = _REPORT_SPLIT.match(raw)
        report["number"] = int(head.group(1)) if head else 0
        build = re.search(r"build=(.+?)\s{2}pid=", raw)
        report["build"] = build.group(1).strip() if build else None
        for key, pattern in (("exception", r"^exception (.+)$"), ("access", r"^access: (.+)$"),
                             ("at", r"^at: (.+)$"), ("thread", r"^thread: (.+)$"), ("our_call", r"^our call: (.+)$")):
            m = re.search(pattern, raw, re.M)
            report[key] = m.group(1).strip() if m else None
        report["own_call"] = bool(report["thread"] and "OUR protected game call" in report["thread"])
        in_stack = in_crumbs = False
        for line in raw.splitlines():
            if line.startswith("stack ("):
                in_stack, in_crumbs = True, False
            elif line.startswith("breadcrumbs"):
                in_stack, in_crumbs = False, True
            elif line.startswith("===="):
                in_stack = in_crumbs = False
            elif in_stack and line.strip().startswith("#"):
                report["stack"].append(line.strip().split(None, 1)[1] if " " in line.strip() else line.strip())
            elif in_crumbs and line.strip().startswith("+"):
                report["crumbs"].append(line.strip())
        reports.append(report)
    return reports


def parse_hangs(text):
    """[dict] one per HANG REPORT block (the DLL's freeze watchdog), oldest first: sample, build, idle_ms, foreground,
    stuck_at, stack, our_state, threads (list of 'tid=.. "name" module+0x..'), crumbs."""
    hangs = []
    starts = [m.start() for m in _HANG_SPLIT.finditer(text)]
    for k, start in enumerate(starts):
        end = text.find("==== END ====", start)
        raw = text[start:end if end >= 0 else len(text)]
        hang = {"raw": raw, "stack": [], "threads": [], "crumbs": []}
        head = re.match(r"==== HANG REPORT sample (\d+)\s+build=(.+?)\s{2}pid=\d+\s+idle=(\d+)ms\s+foreground=(\d)", raw)
        hang["sample"] = int(head.group(1)) if head else 0
        hang["build"] = head.group(2).strip() if head else None
        hang["idle_ms"] = int(head.group(3)) if head else 0
        hang["foreground"] = bool(head and head.group(4) == "1")
        m = re.search(r"^game thread \d+ stuck at: (.+)$", raw, re.M)
        hang["stuck_at"] = m.group(1).strip() if m else None
        m = re.search(r"^our state: (.+)$", raw, re.M)
        hang["our_state"] = m.group(1).strip() if m else None
        section = None
        for line in raw.splitlines():
            if line.startswith("stack ("):
                section = "stack"
            elif line.startswith("threads ("):
                section = "threads"
            elif line.startswith("breadcrumbs"):
                section = "crumbs"
            elif section == "stack" and line.strip().startswith("#"):
                hang["stack"].append(line.strip().split(None, 1)[1] if " " in line.strip() else line.strip())
            elif section == "threads" and line.strip().startswith("tid="):
                hang["threads"].append(line.strip())
            elif section == "crumbs" and line.strip().startswith("+"):
                hang["crumbs"].append(line.strip())
        hangs.append(hang)
    return hangs


def hang_verdict(hang, mapfile=None):
    """Plain-language lines for one freeze sample."""
    lines = []
    state = hang.get("our_state") or ""
    stack = hang.get("stack", [])
    if "OUR game call" in state:
        lines.append(f"CULPRIT: {state}. The freeze is inside a call WE made on the game thread - find that function "
                     f"in the breadcrumbs (CALL) and stop making it, or make it later / with other parameters.")
    elif any(f.startswith("dungeons_bridge.dll+") for f in stack):
        lines.append("CULPRIT (likely): dungeons_bridge.dll is on the game thread's stack - our code is what it is stuck in "
                     "or waiting for (the frames named below).")
    else:
        waiting = [t for t in hang.get("threads", []) if any(w in t.lower() for w in ("renderthread", "rhithread"))]
        lines.append("The game thread is stuck in the game itself, none of our code on its stack. "
                     + ("Render-side threads, for comparison: " + "; ".join(waiting[:2]) + ". " if waiting else "")
                     + "If a render thread and the game thread both sit in the same wait/driver module, that is a "
                       "GPU/driver stall, not our client.")
    if not hang.get("foreground"):
        lines.append("The game window was NOT in front: a paused/background game can idle on purpose - weigh this sample low.")
    return lines


def _resolve_text(text, mapfile):
    if not mapfile or not text:
        return text

    def repl(m):
        name = mapfile.resolve(int(m.group(2), 16))
        return f"{m.group(0)} [{name}]" if name else m.group(0)
    return _MODULE_OFFSET.sub(repl, text)


def _crumb_age_ms(crumb):
    m = re.match(r"\+(\d+)ms", crumb)
    return int(m.group(1)) if m else None


def _crumb_kind_text(crumb):
    m = re.match(r"\+\d+ms tid=\d+ (\S+) (.*?) a=0x[0-9a-f]+ b=0x[0-9a-f]+$", crumb)
    return (m.group(1), m.group(2)) if m else ("?", crumb)


def verdict(report, mapfile=None, dll_build=None):
    """Plain-language lines naming the likely culprit of one report."""
    lines = []
    if dll_build and report.get("build") and dll_build != report["build"]:
        lines.append(f"!! The DLL on disk is build '{dll_build}' but this report was written by build "
                     f"'{report['build']}': the game still runs the OLD DLL (restart it) or the files were mixed up.")
    crumbs = report.get("crumbs", [])
    last_call = None
    for crumb in reversed(crumbs):
        kind, text = _crumb_kind_text(crumb)
        if kind in ("CALL", "CALLFAULT"):
            last_call = (kind, text, _crumb_age_ms(crumb))
            break
    stack = report.get("stack", [])
    ours = [f for f in stack if f.startswith("dungeons_bridge.dll+")]
    at_ours = (report.get("at") or "").startswith("dungeons_bridge.dll+")

    if report.get("own_call"):
        name = last_call[1] if last_call else "an unknown function"
        lines.append(f"CULPRIT: our own game call '{name}' made the game fault. It was caught by our __try, so the "
                     f"game did NOT crash from this - but that call is wrong (bad object, bad parameter layout, or "
                     f"the wrong moment). Fix or stop calling it.")
    elif at_ours:
        lines.append("CULPRIT: our own code (dungeons_bridge.dll) faulted - see the stack below for the function.")
    elif ours:
        lines.append("CULPRIT (likely): the game faulted while running a call that came from our code (dungeons_bridge.dll "
                     "is in the stack) - the frame just below the game's frames is what handed it bad data.")
    elif last_call and last_call[2] is not None and last_call[2] < 3000:
        lines.append(f"SUSPECT: the game faulted {last_call[2]} ms after our call '{last_call[1]}' - a call whose "
                     f"effect is delayed (an object we created, a state we changed) is the usual cause.")
    else:
        recent_hook = next((c for c in reversed(crumbs) if _crumb_kind_text(c)[0] == "HOOK"
                            and (_crumb_age_ms(c) or 10 ** 9) < 8000), None)
        if recent_hook:
            lines.append("SUSPECT: a level change / hook state change just before the fault "
                         f"({_crumb_kind_text(recent_hook)[1]}): something touched an object from the old level.")
        else:
            lines.append("No sign of our code in the stack or the last seconds - probably the game itself "
                         "(or another mod). Compare with a run without the client attached.")
    last_pipe = next((c for c in reversed(crumbs) if _crumb_kind_text(c)[0] == "PIPE"), None)
    if last_pipe and (_crumb_age_ms(last_pipe) or 10 ** 9) < 3000:
        lines.append(f"Last pipe command: {_crumb_kind_text(last_pipe)[1]} ({_crumb_age_ms(last_pipe)} ms before).")
    return lines


def summarize(client_dir=CLIENT_DIR, max_reports=1):
    """Text summary of the newest crash report(s) in client_dir, or a message saying there is none."""
    client_dir = Path(client_dir)
    log = client_dir / CRASH_LOG_NAME
    if not log.exists():
        return f"No {CRASH_LOG_NAME} next to the DLL yet - nothing has been reported (or the DLL is not rebuilt)."
    text = log.read_text(encoding="utf-8", errors="replace")
    reports = parse_reports(text)
    dll_build = read_dll_build(client_dir / DLL_NAME)
    out = [f"{CRASH_LOG_NAME}: {len(reports)} report(s). DLL on disk: build {dll_build or 'unknown'}."]
    if not reports and not parse_hangs(text):
        out.append("The reporter is armed but has not seen a serious exception or a freeze yet.")
        return "\n".join(out)
    for report in reports[-max_reports:]:
        mapfile, note = find_map(client_dir, report.get("build") or dll_build)
        out.append("")
        out.append(f"--- report #{report['number']}  build {report.get('build')}  ({note}) ---")
        out.append(f"exception: {report.get('exception')}")
        if report.get("access"):
            out.append(f"access:    {report['access']}")
        out.append(f"at:        {_resolve_text(report.get('at'), mapfile)}")
        out.append(f"thread:    {report.get('thread')}")
        if report.get("our_call"):
            out.append(f"our call:  {report['our_call']}")
        out.append("")
        out.extend(verdict(report, mapfile, dll_build))
        if report["stack"]:
            out.append("")
            out.append("stack:")
            out.extend(f"  #{i:02d} {_resolve_text(f, mapfile)}" for i, f in enumerate(report["stack"][:16]))
        if report["crumbs"]:
            out.append("")
            out.append("last breadcrumbs (what the DLL was doing):")
            out.extend(f"  {c}" for c in report["crumbs"][-14:])
    hangs = parse_hangs(text)
    if hangs:
        last_sample = hangs[-1]
        episode = [h for h in hangs[-3:] if h["build"] == last_sample["build"]]
        mapfile, note = find_map(client_dir, last_sample.get("build") or dll_build)
        out.append("")
        out.append(f"--- newest FREEZE report: {len(hangs)} sample(s) in the log, build {last_sample.get('build')} ({note}) ---")
        out.append(f"game thread idle {last_sample['idle_ms'] / 1000:.1f} s at the last sample; stuck at: "
                   f"{_resolve_text(last_sample.get('stuck_at'), mapfile)}")
        out.append("")
        out.extend(hang_verdict(last_sample, mapfile))
        if last_sample["stack"]:
            out.append("")
            out.append("game thread stack:")
            out.extend(f"  #{i:02d} {_resolve_text(f, mapfile)}" for i, f in enumerate(last_sample["stack"][:14]))
        if last_sample["threads"]:
            out.append("")
            out.append("every thread's top frame:")
            out.extend(f"  {_resolve_text(t, mapfile)}" for t in last_sample["threads"][:24])
        if len(episode) >= 2 and episode[0]["stuck_at"] != episode[-1]["stuck_at"]:
            out.append("")
            out.append("The game thread MOVED between samples: it is looping or very slow, not deadlocked.")
        elif len(episode) >= 2:
            out.append("")
            out.append("The game thread was at the SAME place in every sample: a real hang (wait/deadlock).")
    return "\n".join(out)


def check_features(features_text):
    """(ok, message) for the 'FEATURES:a,b,c' part of get_build_info against what this client needs."""
    have = {f for f in (features_text or "").split(",") if f}
    missing = sorted(REQUIRED_FEATURES - have)
    if missing:
        return False, ("the DLL is older than this client (missing: " + ", ".join(missing) +
                       ") - rebuild dungeons_bridge.dll from the dungeons_bridge.cpp that came with it")
    return True, "the DLL has every feature this client needs"


if __name__ == "__main__":
    print(summarize(sys.argv[1] if len(sys.argv) > 1 else CLIENT_DIR))
