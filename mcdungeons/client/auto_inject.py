"""
Auto-injector for dungeons_bridge.dll - replaces the manual "use an
external injector tool" step from earlier in this project.

Uses the classic, well-known technique (the same one UUU and Cheat Engine
use internally): OpenProcess -> VirtualAllocEx -> WriteProcessMemory ->
CreateRemoteThread(LoadLibraryA). LoadLibraryA's address is safe to grab
from our own process, since kernel32.dll loads at the same base address
across all processes for a given boot session.

Heads up: some antivirus/Defender configurations flag CreateRemoteThread
injection heuristically, since it's the same primitive used by malware -
this is a common false-positive source for legitimate tools too (game
trainers, overlays, mod loaders all use this). If Defender blocks it,
you may need an exclusion for this folder.

pip install pywin32
"""

import ctypes
from ctypes import wintypes
import os

PROCESS_NAME = "Dungeons.exe"  # match dungeons_reader.py - adjust if needed
DLL_NAME = "dungeons_bridge.dll"

PROCESS_ALL_ACCESS = 0x1F0FFF
MEM_COMMIT = 0x1000
MEM_RESERVE = 0x2000
MEM_RELEASE = 0x8000
PAGE_READWRITE = 0x04
TH32CS_SNAPPROCESS = 0x00000002
TH32CS_SNAPMODULE = 0x00000008
TH32CS_SNAPMODULE32 = 0x00000010

kernel32 = ctypes.windll.kernel32

# ctypes defaults every argument to a 32-bit int unless told otherwise -
# on 64-bit Windows that SILENTLY TRUNCATES handles/pointers/sizes, which
# is exactly what caused WriteProcessMemory to fail even though
# OpenProcess and VirtualAllocEx both "succeeded" first. Declaring the
# real types below is what fixes it.
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE

kernel32.VirtualAllocEx.argtypes = [
    wintypes.HANDLE, wintypes.LPVOID, ctypes.c_size_t, wintypes.DWORD, wintypes.DWORD
]
kernel32.VirtualAllocEx.restype = wintypes.LPVOID

kernel32.WriteProcessMemory.argtypes = [
    wintypes.HANDLE, wintypes.LPVOID, wintypes.LPCVOID, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)
]
kernel32.WriteProcessMemory.restype = wintypes.BOOL

kernel32.VirtualFreeEx.argtypes = [wintypes.HANDLE, wintypes.LPVOID, ctypes.c_size_t, wintypes.DWORD]
kernel32.VirtualFreeEx.restype = wintypes.BOOL

kernel32.CreateRemoteThread.argtypes = [
    wintypes.HANDLE, wintypes.LPVOID, ctypes.c_size_t, wintypes.LPVOID,
    wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)
]
kernel32.CreateRemoteThread.restype = wintypes.HANDLE

kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
kernel32.WaitForSingleObject.restype = wintypes.DWORD

kernel32.GetExitCodeThread.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
kernel32.GetExitCodeThread.restype = wintypes.BOOL

kernel32.GetModuleHandleA.argtypes = [ctypes.c_char_p]
kernel32.GetModuleHandleA.restype = wintypes.HMODULE

kernel32.GetProcAddress.argtypes = [wintypes.HMODULE, ctypes.c_char_p]
kernel32.GetProcAddress.restype = wintypes.LPVOID

kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL

kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE

kernel32.Process32First.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
kernel32.Process32First.restype = wintypes.BOOL
kernel32.Process32Next.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
kernel32.Process32Next.restype = wintypes.BOOL

kernel32.Module32First.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
kernel32.Module32First.restype = wintypes.BOOL
kernel32.Module32Next.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
kernel32.Module32Next.restype = wintypes.BOOL


class PROCESSENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_char * 260),
    ]


class MODULEENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("th32ModuleID", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("GlblcntUsage", wintypes.DWORD),
        ("ProccntUsage", wintypes.DWORD),
        ("modBaseAddr", ctypes.POINTER(ctypes.c_byte)),
        ("modBaseSize", wintypes.DWORD),
        ("hModule", wintypes.HMODULE),
        ("szModule", ctypes.c_char * 256),
        ("szExePath", ctypes.c_char * 260),
    ]


INVALID_HANDLE_VALUE = 0xFFFFFFFFFFFFFFFF


def _is_invalid_handle(h):
    return not h or h == INVALID_HANDLE_VALUE


def find_process_id(process_name):
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if _is_invalid_handle(snapshot):
        return None

    entry = PROCESSENTRY32()
    entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
    pid = None

    if kernel32.Process32First(snapshot, ctypes.byref(entry)):
        while True:
            if entry.szExeFile.decode(errors="ignore").lower() == process_name.lower():
                pid = entry.th32ProcessID
                break
            if not kernel32.Process32Next(snapshot, ctypes.byref(entry)):
                break

    kernel32.CloseHandle(snapshot)
    return pid


def find_all_process_ids(process_name):
    """Same enumeration as find_process_id, but collects EVERY matching
    PID instead of stopping at the first - needed to even notice that
    more than one Dungeons.exe is running at once. find_process_id (and
    a bare pymem.Pymem(process_name)) both silently return just the
    first match in Process32First/Next order, which is fine with one
    game running but means, with two, every client process launched
    keeps attaching to that SAME first process - the second game
    instance never gets a client (or the DLL) at all, no error, just
    quietly nothing happening for player 2. Returns a list of PIDs, in
    enumeration order (empty if none found)."""
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if _is_invalid_handle(snapshot):
        return []

    entry = PROCESSENTRY32()
    entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
    pids = []

    if kernel32.Process32First(snapshot, ctypes.byref(entry)):
        while True:
            if entry.szExeFile.decode(errors="ignore").lower() == process_name.lower():
                pids.append(entry.th32ProcessID)
            if not kernel32.Process32Next(snapshot, ctypes.byref(entry)):
                break

    kernel32.CloseHandle(snapshot)
    return pids


PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
STILL_ACTIVE = 259

kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
kernel32.GetExitCodeProcess.restype = wintypes.BOOL


def is_process_alive(pid):
    """True only if a process with this PID both exists AND is still
    running (not just "some process happens to have reused this PID" -
    GetExitCodeProcess on a handle actually opened for THIS pid is what
    makes that distinction, a plain "does OpenProcess succeed" check
    alone can't). Used by the client to notice a game crash/relaunch -
    Dungeons.exe closing invalidates the pymem handle the game_watcher
    tick loop was reading through, but every read this loop does is
    already wrapped in a broad try/except that just `continue`s, so
    nothing else would ever surface that as an error - checks would
    simply stop being sent forever until the client itself was
    restarted. Returns False for a PID that no longer exists (OpenProcess
    fails) or one that exists but has exited (GetExitCodeProcess reports
    anything other than STILL_ACTIVE)."""
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if _is_invalid_handle(handle):
        return False
    try:
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return False
        return exit_code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def is_dll_loaded(pid, dll_filename):
    """Checks if a DLL with this filename is already loaded in the target
    process - avoids double-injecting if the client (or a previous run)
    already did it."""
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, pid)
    if _is_invalid_handle(snapshot):
        return False

    entry = MODULEENTRY32()
    entry.dwSize = ctypes.sizeof(MODULEENTRY32)
    found = False

    if kernel32.Module32First(snapshot, ctypes.byref(entry)):
        while True:
            if entry.szModule.decode(errors="ignore").lower() == dll_filename.lower():
                found = True
                break
            if not kernel32.Module32Next(snapshot, ctypes.byref(entry)):
                break

    kernel32.CloseHandle(snapshot)
    return found


def inject_dll(pid, dll_path):
    dll_path_bytes = dll_path.encode("ascii") + b"\x00"

    h_process = kernel32.OpenProcess(PROCESS_ALL_ACCESS, False, pid)
    if not h_process:
        raise RuntimeError(
            f"Could not open process (PID {pid}, error {kernel32.GetLastError()}) - "
            f"try running this script as administrator?"
        )

    try:
        remote_mem = kernel32.VirtualAllocEx(
            h_process, None, len(dll_path_bytes), MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE
        )
        if not remote_mem:
            raise RuntimeError("VirtualAllocEx failed")

        written = ctypes.c_size_t(0)
        if not kernel32.WriteProcessMemory(
            h_process, remote_mem, dll_path_bytes, len(dll_path_bytes), ctypes.byref(written)
        ):
            raise RuntimeError("WriteProcessMemory failed")

        h_kernel32 = kernel32.GetModuleHandleA(b"kernel32.dll")
        load_library_addr = kernel32.GetProcAddress(h_kernel32, b"LoadLibraryA")
        if not load_library_addr:
            raise RuntimeError("Could not resolve LoadLibraryA")

        thread_id = wintypes.DWORD(0)
        h_thread = kernel32.CreateRemoteThread(
            h_process, None, 0, load_library_addr, remote_mem, 0, ctypes.byref(thread_id)
        )
        if not h_thread:
            raise RuntimeError(f"CreateRemoteThread failed (error {kernel32.GetLastError()})")

        kernel32.WaitForSingleObject(h_thread, 10000)

        exit_code = wintypes.DWORD(0)
        kernel32.GetExitCodeThread(h_thread, ctypes.byref(exit_code))
        kernel32.CloseHandle(h_thread)
        kernel32.VirtualFreeEx(h_process, remote_mem, 0, MEM_RELEASE)

        if exit_code.value == 0:
            raise RuntimeError(
                "LoadLibraryA returned NULL inside the target process - the DLL "
                "failed to load (wrong path, missing dependency, or architecture mismatch)."
            )

        return exit_code.value  # this is the loaded module's HMODULE

    finally:
        kernel32.CloseHandle(h_process)


def pick_target_pid(process_name, dll_filename, warn=print):
    """Picks which Dungeons.exe to attach/inject THIS client into, and
    is the one place that actually notices (and warns about) more than
    one instance running - find_process_id/pymem.Pymem(name) alone
    would silently just grab the first one every time, so with two game
    instances open, every client launched kept attaching to the same
    first process and the second player's game was never reachable by
    anything, with no error anywhere to explain why.

    Strategy: if exactly one Dungeons.exe is running, use it (no
    warning - the common case). If more than one, pick the first PID
    that does NOT already have dungeons_bridge.dll loaded - a process
    with the DLL already loaded means SOME client already claimed and
    injected it, so this new client should take the next free one
    instead, letting each client naturally claim a different game
    instance one at a time as they're launched. If every detected
    instance already has the DLL loaded (all already claimed), falls
    back to the first PID found, but with an explicit warning so the
    person knows this client is sharing an already-injected process
    rather than getting a distinct one.

    `warn`: callable given the warning text (defaults to print; the
    client passes its own logger so this shows up in its log/console).
    Returns the picked pid, or None if no matching process was found."""
    pids = find_all_process_ids(process_name)
    if not pids:
        return None
    if len(pids) == 1:
        return pids[0]

    unclaimed = [p for p in pids if not is_dll_loaded(p, dll_filename)]
    if unclaimed:
        target = unclaimed[0]
        warn(f"{len(pids)} {process_name} instances detected (PIDs: {pids}) - only "
             f"one client should inject into each. This client is attaching to the "
             f"first one WITHOUT {dll_filename} already loaded (PID {target}). If "
             f"you're starting a second client for a second instance, wait for THIS "
             f"one to finish connecting/injecting before launching the next - "
             f"launching several at once can race and pick the same target.")
        return target

    target = pids[0]
    warn(f"{len(pids)} {process_name} instances detected (PIDs: {pids}), and ALL of "
         f"them already have {dll_filename} loaded - every instance already has a "
         f"client claiming it. This client is attaching to PID {target} anyway "
         f"(sharing it with whatever already injected it), which is probably not "
         f"what you want - close the extra {process_name} instance(s) you're not "
         f"using, or make sure each one gets its own client.")
    return target


if __name__ == "__main__":
    dll_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), DLL_NAME)

    if not os.path.exists(dll_path):
        print(f"Can't find {DLL_NAME} - make sure it's compiled and sitting next to this script.")
        raise SystemExit

    print(f"Looking for {PROCESS_NAME}...")
    pid = pick_target_pid(PROCESS_NAME, DLL_NAME)
    if not pid:
        print("Process not found - is the game running? (and past the main menu / in a level)")
        raise SystemExit
    print(f"Found PID {pid}")

    if is_dll_loaded(pid, DLL_NAME):
        print(f"{DLL_NAME} is already injected - nothing to do.")
        raise SystemExit

    print(f"Injecting {dll_path}...")
    handle = inject_dll(pid, dll_path)
    print(f"Success - loaded at handle 0x{handle:X}")
    print("Give it a moment to start its pipe server, then connect as normal.")
