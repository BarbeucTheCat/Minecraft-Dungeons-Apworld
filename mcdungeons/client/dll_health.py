"""Is the DLL the one this client expects, and what is it doing? (the /crash devtool and the attach-time check)

    /crash            summary of the newest crash report the DLL wrote (culprit, resolved stack, breadcrumbs)
    /crash crumbs     what the DLL is doing right now (live breadcrumbs: pipe commands, our game calls, merchants...)
    /crash build      DLL build id + whether it has every feature this client needs
    /crash test       makes the DLL write a harmless test report (a deliberate, caught fault) to prove the reporter works
"""
import logging

import crash_report

game_logger = logging.getLogger("MCDungeonsGame")

HELP_LINES = [
    "/crash            summary of the newest crash report: likely culprit, resolved stack, last actions of the DLL",
    "/crash crumbs     what the DLL is doing right now (its last pipe commands, game calls, merchant events...)",
    "/crash build      DLL build id and whether it has every feature this client needs",
    "/crash test       writes a harmless test crash report AND a test freeze snapshot to prove both reporters work",
]


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


def get_build_info(pm):
    """(build id, features text) from the running DLL, or (None, None) when it does not know the command (old DLL)."""
    try:
        answer = _pipe_request(pm, "get_build_info")
    except Exception:
        return None, None
    if not answer.startswith("BUILD:"):
        return None, None
    build, _, features = answer[len("BUILD:"):].partition("|FEATURES:")
    return build, features


def announce(pm):
    """Lines to log once per attach: which DLL is loaded and whether it is current. Blocking (pipe)."""
    build, features = get_build_info(pm)
    if build is None:
        return ["DLL check: dungeons_bridge.dll does not answer get_build_info - it is OLDER than this client "
                "(no crash reporter, no villager checks). Rebuild it from the dungeons_bridge.cpp that came with this "
                "client and restart the game."]
    ok, message = crash_report.check_features(features)
    return [f"DLL check: build {build}; {message}."]


def run_crash(pm, sub=""):
    """Entry point of /crash (blocking, run through asyncio.to_thread with the bridge pipe lock held)."""
    sub = (sub or "").strip().lower()
    if sub in ("help", "?"):
        return HELP_LINES
    if sub == "crumbs":
        try:
            answer = _pipe_request(pm, "get_crumbs")
        except Exception as e:
            return [f"crash: could not talk to the DLL ({e})"]
        if not answer.startswith("CRUMBS:"):
            return ["crash: the DLL does not know get_crumbs - rebuild it from the current dungeons_bridge.cpp."]
        lines = [line for line in answer[len("CRUMBS:"):].splitlines() if line.strip()]
        return ["Last actions of the DLL (oldest first):"] + [f"  {line}" for line in lines[-40:]]
    if sub == "build":
        return announce(pm)
    if sub == "test":
        try:
            answer = _pipe_request(pm, "crash_selftest")
            hang_answer = _pipe_request(pm, "health_selftest")
        except Exception as e:
            return [f"crash: could not talk to the DLL ({e})"]
        if answer != "OK" or hang_answer != "OK":
            return ["crash: the DLL does not know crash_selftest / health_selftest - rebuild it from the current "
                    "dungeons_bridge.cpp."]
        return ["A test crash report and a test freeze snapshot were written. This is what the reporter produced:", ""] + \
            crash_report.summarize().splitlines()
    return crash_report.summarize().splitlines()
