"""Auto-injects dungeons_bridge.dll into the game process (replaces the manual injector step).
    OpenProcess: get a handle to the game process.
    VirtualAllocEx: allocate memory inside the game.
    WriteProcessMemory: write the DLL path into that memory.
    CreateRemoteThread(LoadLibraryA): ask the game to load the DLL from that address.
    LoadLibraryA's address is safe to grab from our own process: kernel32.dll loads at the
    same base address across all processes for a given boot session.
    Defender may flag it (same primitive as malware); add a folder exclusion if it does.
    Requires: pip install pywin32"""

import ctypes
from ctypes import wintypes
import os
import time

PROCESS_NAME = "Dungeons.exe"  # CLI fallback only. The real process name is store-dependent and lives in dungeons_reader
                               # (STORE_PROCESS_NAMES / PROCESS_NAME); the CLI asks dungeons_reader first (_cli_process_name).


def _cli_process_name():
    try:
        import dungeons_reader
        return dungeons_reader.PROCESS_NAME
    except Exception:
        return PROCESS_NAME
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

# ctypes defaults args to 32-bit int; on 64-bit Windows that silently truncates handles/pointers/sizes
# (caused WriteProcessMemory to fail). Declaring real argtypes below fixes it.
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

kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)
]
kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL

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

# ERROR_ACCESS_DENIED gets its own exception type (InjectionAccessDeniedError) because it's a
# permissions/isolation problem, not a bug in this script. All other failures are generic RuntimeError.
ERROR_ACCESS_DENIED = 5

# PROCESS_ALL_ACCESS can be denied without any special rights missing; PROCESS_QUERY_LIMITED_INFORMATION
# is almost never refused, so it's a cheap diagnostic probe (exe path) after a real injection failure —
# never used in the main path.
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_DIAGNOSTIC_QUERY_ACCESS = PROCESS_QUERY_LIMITED_INFORMATION


def _is_invalid_handle(h):
    return not h or h == INVALID_HANDLE_VALUE


class InjectionAccessDeniedError(RuntimeError):
    """Raised when Windows refuses a handle/memory operation with ERROR_ACCESS_DENIED (5) at any step of inject_dll.
    Separate type so callers can show a permissions-specific message (run as admin, check AV, Store install)
    instead of the generic "injection failed" one.
    step: which call was refused ("OpenProcess", "WriteProcessMemory", ...).
    likely_protected_path: target exe path if retrievable (None if not) — e.g. to detect "\\WindowsApps\\"."""
    def __init__(self, step: str, pid: int, likely_protected_path: "str | None" = None):
        self.step = step
        self.pid = pid
        self.likely_protected_path = likely_protected_path

        path_note = (
            f"\n  Target executable: {likely_protected_path}"
            if likely_protected_path else
            "\n  (couldn't even determine the target's exe path with reduced rights - "
            "the process is likely running under extra isolation, e.g. an AppContainer.)" 
        )
        store_hint = ""
        if likely_protected_path and "windowsapps" in likely_protected_path.lower():
            store_hint = (
            "\n This looks like a Microsoft Store install (path under WindowsApps) — those run sandboxed (AppContainer)\n"
            "with locked-down ACLs; even Administrators may not be able to open the process for memory access.\n"
            "If injection fails here, running as admin may not fix it."
            )

        super().__init__(
            f"Access denied during {step} (PID {pid}, Win32 error {ERROR_ACCESS_DENIED}) - "
            f"Windows refused this operation outright, this isn't a bug in the injector "
            f"itself. Things to try, roughly in order:\n"
            f"  1. Run the client (and/or the whole Archipelago Launcher) as Administrator.\n"
            f"  2. Check whether antivirus/Defender is silently blocking the handle/memory "
            f"operation rather than the process itself - a real block usually shows up in "
            f"its own logs/quarantine, not just here.\n"
            f"  3. Confirm the game process itself isn't running elevated while this client "
            f"is not (or vice versa) - a mismatched integrity level between the two commonly "
            f"causes exactly this failure.{path_note}{store_hint}"
        )


def _try_get_process_image_path(pid) -> "str | None":
    """Best-effort: retrieves the target exe path after a real injection failure, to enrich the error message.
    Uses PROCESS_QUERY_LIMITED_INFORMATION (lightest right that can read a path).
    Returns None on any failure — never raises."""
    handle = kernel32.OpenProcess(_DIAGNOSTIC_QUERY_ACCESS, False, pid)
    if _is_invalid_handle(handle):
        return None
    try:
        buf_len = wintypes.DWORD(260)
        buf = ctypes.create_unicode_buffer(buf_len.value)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(buf_len)):
            return None
        return buf.value or None
    except Exception:
        return None
    finally:
        kernel32.CloseHandle(handle)


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
    """Returns ALL PIDs matching the process name (find_process_id only returns the first).
    Needed because with two game instances running, the second silently never gets a client attached.
    Returns a list in enumeration order (empty if none found)."""
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


STILL_ACTIVE = 259

kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
kernel32.GetExitCodeProcess.restype = wintypes.BOOL


def is_process_alive(pid):
    """True only if the PID exists AND is still running (GetExitCodeProcess distinguishes "alive" from "exited, PID reused").
    Used by the client to detect a game crash: the watcher loop swallows all read errors silently,
    so without this check a crash would just stop checks being sent forever with no visible error."""
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


def is_dll_loaded(pid, dll_filename, retries=3, retry_delay=0.2):
    """Checks if the DLL is already loaded in the target process (avoids double-injection).
    Preventive: retries on transient snapshot failures (ERROR_PARTIAL_COPY) before returning False —
    a single false "not loaded" would trigger an unnecessary re-injection that breaks the session
    (MH_ERROR_ALREADY_CREATED, no checks sent for the rest of the run). Not confirmed by a real repro."""   
    last_error = None
    for attempt in range(retries):
        snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, pid)
        if _is_invalid_handle(snapshot):
            last_error = kernel32.GetLastError()
            if attempt < retries - 1:
                time.sleep(retry_delay)
                continue
            print(f"Warning: CreateToolhelp32Snapshot failed after {retries} attempts "
                  f"(last error {last_error}) - treating as 'not loaded', but this may be "
                  f"a false negative rather than a genuine absence.")
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
    return False


def inject_dll(pid, dll_path):
    dll_path_bytes = dll_path.encode("ascii") + b"\x00"

    h_process = kernel32.OpenProcess(PROCESS_ALL_ACCESS, False, pid)
    if not h_process:
        # Capture GetLastError() before anything else (even the diagnostic call below) makes its own kernel32 calls and clobbers it.
        error = kernel32.GetLastError()
        if error == ERROR_ACCESS_DENIED:
            raise InjectionAccessDeniedError("OpenProcess", pid, _try_get_process_image_path(pid))
        raise RuntimeError(
            f"Could not open process (PID {pid}, error {error}) - "
            f"try running this script as administrator?"
        )

    try:
        remote_mem = kernel32.VirtualAllocEx(
            h_process, None, len(dll_path_bytes), MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE
        )
        if not remote_mem:
            error = kernel32.GetLastError()
            if error == ERROR_ACCESS_DENIED:
                raise InjectionAccessDeniedError("VirtualAllocEx", pid, _try_get_process_image_path(pid))
            raise RuntimeError(f"VirtualAllocEx failed (error {error})")

        written = ctypes.c_size_t(0)
        if not kernel32.WriteProcessMemory(
            h_process, remote_mem, dll_path_bytes, len(dll_path_bytes), ctypes.byref(written)
        ):
            error = kernel32.GetLastError()
            if error == ERROR_ACCESS_DENIED:
                raise InjectionAccessDeniedError("WriteProcessMemory", pid, _try_get_process_image_path(pid))
            raise RuntimeError(f"WriteProcessMemory failed (error {error})")

        h_kernel32 = kernel32.GetModuleHandleA(b"kernel32.dll")
        load_library_addr = kernel32.GetProcAddress(h_kernel32, b"LoadLibraryA")
        if not load_library_addr:
            raise RuntimeError("Could not resolve LoadLibraryA")

        thread_id = wintypes.DWORD(0)
        h_thread = kernel32.CreateRemoteThread(
            h_process, None, 0, load_library_addr, remote_mem, 0, ctypes.byref(thread_id)
        )
        if not h_thread:
            error = kernel32.GetLastError()
            if error == ERROR_ACCESS_DENIED:
                raise InjectionAccessDeniedError("CreateRemoteThread", pid, _try_get_process_image_path(pid))
            raise RuntimeError(f"CreateRemoteThread failed (error {error})")

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
    """Picks which Dungeons.exe to attach to; the one place that notices multiple instances.
    One instance: use it. Multiple: pick the first without dungeons_bridge.dll already loaded.
    All claimed: fall back to the first PID with a warning.
    warn: callable for the warning text (defaults to print). Returns the picked PID or None."""
    pids = find_all_process_ids(process_name)
    # A crashed process stays in the snapshot while any handle is open (including our own), so it looks "found"
    # but injection into it silently fails — the relaunched game never gets the DLL. Only live PIDs are candidates.
    pids = [p for p in pids if is_process_alive(p)]
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

    process_name = _cli_process_name()
    print(f"Looking for {process_name}...")
    pid = pick_target_pid(process_name, DLL_NAME)
    if not pid:
        print("Process not found - is the game running? (and past the main menu / in a level)")
        raise SystemExit
    print(f"Found PID {pid}")

    if is_dll_loaded(pid, DLL_NAME):
        print(f"{DLL_NAME} is already injected - nothing to do.")
        raise SystemExit

    print(f"Injecting {dll_path}...")
    try:
        handle = inject_dll(pid, dll_path)
    except InjectionAccessDeniedError as e:
        print(f"\n{e}\n")
        raise SystemExit
    print(f"Success - loaded at handle 0x{handle:X}")
    print("Give it a moment to start its pipe server, then connect as normal.")
