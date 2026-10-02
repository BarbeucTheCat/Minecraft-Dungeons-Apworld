// dungeons_bridge.cpp - v3
//
// FIXES THE THREADING BUG: ProcessEvent crashed (access violation) when
// called directly from our pipe-server thread, with BOTH the vtable-slot
// guess (64) and the direct RVA from the real Dumper-7 Basic.hpp
// (0x01244C30) - both identical crashes. That rules out "wrong address"
// and points at thread affinity: ProcessEvent drives full Blueprint VM
// execution, which (unlike a simple utility like AppendString, which DID
// work fine off-thread) has real thread-safety requirements tied to the
// game's own thread.
//
// FIX: hook IDXGISwapChain::Present (via MinHook) - guaranteed to run on
// the correct thread every frame. The pipe thread queues a call request
// and blocks (with a timeout) on a condition variable; the Present hook
// drains the queue and does the actual ProcessEvent call safely, then
// signals the waiting pipe thread with the result.
//
// REQUIRES MinHook (https://github.com/TsudaKageyu/minhook):
//   Option A (vcpkg, easiest):
//     vcpkg install minhook:x64-windows-static
//     cl /LD /EHsc dungeons_bridge.cpp /I <vcpkg>\installed\x64-windows-static\include ^
//        /link d3d11.lib dxgi.lib <vcpkg>\installed\x64-windows-static\lib\libMinHook.lib ^
//        /OUT:dungeons_bridge.dll
//   Option B (build from source):
//     Clone https://github.com/TsudaKageyu/minhook, build libMinHook.x64.lib
//     with its own project/CMake, then:
//     cl /LD /EHsc dungeons_bridge.cpp /I <minhook>\include ^
//        /link d3d11.lib dxgi.lib <minhook>\build\libMinHook.x64.lib ^
//        /OUT:dungeons_bridge.dll
//
// Both from "x64 Native Tools Command Prompt for VS 2022".

#include <windows.h>
#include <d3d11.h>
#include <dxgi.h>
#include <string>
#include <thread>
#include <mutex>
#include <condition_variable>
#include <queue>
#include <vector>
#include <set>
#include <unordered_set>
#include <unordered_map>
#include <algorithm>
#include <cctype>
#include <memory>
#include <cstdint>
#include <sstream>
#include <functional>
#include <atomic>

#include "MinHook.h"

#pragma comment(lib, "d3d11.lib")
#pragma comment(lib, "dxgi.lib")

// A fixed pipe name only works for a SINGLE injected game process at a
// time: CreateNamedPipeW below is called with nMaxInstances=1, so a
// second Dungeons.exe (two clients open at once, e.g. two players on the
// same machine) gets its own DLL instance trying to create a pipe of the
// SAME name, which fails outright (the first process's instance already
// holds the only slot) and just retries forever, never establishing a
// channel Python can talk to - looks exactly like "doesn't work" for
// whichever process lost the race. Making the name unique per process
// (its own PID, resolved once here at load time) means each game
// process's DLL gets its own independent pipe, and Python's side (see
// dungeons_reader.py's _pipe_name_for) computes the matching name from
// pm.process_id when connecting, so it always reaches the right one.
static std::wstring g_pipeName;

static const wchar_t* GetPipeName()
{
    if (g_pipeName.empty())
    {
        g_pipeName = L"\\\\.\\pipe\\dungeons_bridge_" + std::to_wstring(GetCurrentProcessId());
    }
    return g_pipeName.c_str();
}

// ---- RVAs of the engine globals/functions this DLL uses ----
// These are NOT the same across game builds: the Steam build
// (Dungeons-Win64-Shipping.exe) was updated and its engine globals moved
// (Steam gworld 0x4795230 vs 0x47540B0 on the Microsoft Store / Minecraft
// Launcher build, see STORE_OFFSETS in dungeons_reader.py). This DLL used to
// hard-code the Microsoft Store values, so on Steam it called AppendString
// at an address that is not AppendString there - the crash dumps from Steam
// show the faulting instruction at exactly image base + 0x10BF7C8, i.e.
// the Microsoft Store AppendString RVA (0x10BF7C0) + 8. The values are now
// filled in once by InitOffsetsForHost() from the host exe's name. 0 means
// "unknown for this build - do not use": every getter below returns null for
// a 0 offset and every caller already handles that.
namespace Offsets
{
    static int32_t GObjects = 0;
    static int32_t AppendString = 0;
    static int32_t ProcessEvent = 0;
    static int32_t GWorld = 0;
}

struct FUObjectItem
{
    void* Object;          // 0x0000
    uint8_t Pad_8[0x10];   // 0x0008
};

class TUObjectArray
{
public:
    static constexpr int32_t ElementsPerChunk = 0x10000;

    FUObjectItem** Objects;
    uint8_t Pad_8[0x8];
    int32_t MaxElements;
    int32_t NumElements;
    int32_t MaxChunks;
    int32_t NumChunks;

    int32_t Num() const { return NumElements; }

    void* GetByIndex(int32_t Index) const
    {
        const int32_t ChunkIndex = Index / ElementsPerChunk;
        const int32_t InChunkIdx = Index % ElementsPerChunk;

        if (Index < 0 || ChunkIndex >= NumChunks || Index >= NumElements)
            return nullptr;

        FUObjectItem* ChunkPtr = Objects[ChunkIndex];
        if (!ChunkPtr)
            return nullptr;

        return ChunkPtr[InChunkIdx].Object;
    }
};

struct SimpleFName
{
    int32_t ComparisonIndex;
    int32_t Number;
};

struct SimpleFString
{
    wchar_t* Data;
    int32_t Num;
    int32_t Max;
};

typedef void(*AppendStringFn)(const SimpleFName*, SimpleFString&);
typedef void(*ProcessEventFn)(void* self, void* function, void* params);

// ---- Debug logging - file-based since the pipe isn't always connected
// when something goes wrong at startup, and this needs to capture info
// from very early in the DLL's life. Written next to the DLL itself,
// via its own module handle - a bare relative filename would resolve
// against the HOST PROCESS's working directory (the game's own install
// folder), not wherever this DLL physically sits, since an injected DLL
// inherits the host's cwd rather than having one of its own.

static std::mutex g_logMutex;
static HMODULE g_ownModule = nullptr;

static std::string GetLogPath()
{
    char path[MAX_PATH] = {};
    if (g_ownModule && GetModuleFileNameA(g_ownModule, path, MAX_PATH))
    {
        std::string s(path);
        size_t slash = s.find_last_of("\\/");
        if (slash != std::string::npos)
            return s.substr(0, slash + 1) + "dungeons_bridge_debug.log";
    }
    return "dungeons_bridge_debug.log";  // fallback - better than nothing
}

static void LogLine(const std::string& line)
{
    std::lock_guard<std::mutex> lock(g_logMutex);
    FILE* f = nullptr;
    fopen_s(&f, GetLogPath().c_str(), "a");
    if (f)
    {
        fprintf(f, "%s\n", line.c_str());
        fclose(f);
    }
}

static uintptr_t GetImageBase()
{
    return reinterpret_cast<uintptr_t>(GetModuleHandle(0));
}

struct StoreOffsets
{
    const char* label;
    int32_t gobjects, appendString, processEvent, gworld;
};

// Microsoft Store + Minecraft Launcher (Dungeons.exe): all four values copied
// from a real Dumper-7 dump of the MS Store build's Basic.hpp.
static const StoreOffsets kOffsetsMsStore = {
    "microsoft_store / minecraft_launcher (Dungeons.exe)",
    0x046556C8, 0x010BF7C0, 0x01244C30, 0x047540B0 };

// Steam (Dungeons-Win64-Shipping.exe). gobjects/gworld: STORE_OFFSETS["steam"]
// in dungeons_reader.py (from Dumper-7's OffsetsInfo.json). processEvent
// 0x01246E10: derived from an older Steam session's log ("ProcessEvent target
// address 0x7ff7276a6e10") and the crash dump of that same process (image
// base ...26460000) - cross-checked, but not read from a Steam Dumper-7 dump.
// appendString 0x010C19A0: Offsets::AppendString from the Steam Dumper-7
// Basic.hpp. That same dump also confirms gobjects 0x04696848, gworld
// 0x04795230 and processEvent 0x01246E10 (so the ProcessEvent RVA is no
// longer just "derived from a log"). If AppendString were ever 0 again, name
// resolution would be disabled (nothing that needs a name can run) instead
// of calling a wrong address.
static const StoreOffsets kOffsetsSteam = {
    "steam (Dungeons-Win64-Shipping.exe)",
    0x04696848, 0x010C19A0, 0x01246E10, 0x04795230 };

static std::once_flag g_offsetsOnce;

static void InitOffsetsForHost()
{
    std::call_once(g_offsetsOnce, []()
    {
        char path[MAX_PATH] = { 0 };
        DWORD n = GetModuleFileNameA(nullptr, path, MAX_PATH);
        std::string exe = (n > 0 && n < MAX_PATH) ? std::string(path) : std::string();
        size_t slash = exe.find_last_of("\\/");
        if (slash != std::string::npos)
            exe = exe.substr(slash + 1);
        for (auto& c : exe)
            c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));

        const StoreOffsets* chosen = nullptr;
        if (exe == "dungeons-win64-shipping.exe")
            chosen = &kOffsetsSteam;
        else if (exe == "dungeons.exe")
            chosen = &kOffsetsMsStore;

        if (!chosen)
        {
            LogLine("Offsets: host exe '" + exe + "' is not a known build - ALL offsets left at 0, "
                    "this DLL will stay passive (no hooks, no engine calls).");
            return;
        }
        Offsets::GObjects = chosen->gobjects;
        Offsets::AppendString = chosen->appendString;
        Offsets::ProcessEvent = chosen->processEvent;
        Offsets::GWorld = chosen->gworld;
        LogLine(std::string("Offsets: using the ") + chosen->label + " set (host exe '" + exe + "').");
        if (!Offsets::AppendString)
            LogLine("Offsets: AppendString RVA is unknown for this build - name resolution disabled, "
                    "pickup blocking and chest/boss/secret detection are inert until it is filled in.");
    });
}

static uint64_t GetCurrentWorldPtr()
{
    // Read directly - this code runs INSIDE the target process (injected),
    // so no cross-process ReadProcessMemory is needed, unlike pymem's
    // out-of-process reads of this exact same global on the Python side.
    InitOffsetsForHost();
    if (!Offsets::GWorld)
        return 0;
    return *reinterpret_cast<uint64_t*>(GetImageBase() + Offsets::GWorld);
}

// ---- Autonomous hook silence / re-arm -------------------------------------
// Replaces the old "HOOK SILENT" scheme (hook created but NOT enabled, waiting
// for a reenable_hook pipe command). That design could never wake itself: with
// the hook disabled, no code of ours ran inside the game, so re-arming
// depended entirely on an external pipe command that never came.
//
// "Silent" is an atomic flag checked at the very top of hkProcessEvent: while
// it is set, the hook does nothing but forward to the original ProcessEvent
// (no queue draining, no classification, no name resolution, no blocking), and
// the pipe handlers that would touch the game (CALL/CALLDATA/resolve/find)
// answer "ERROR:..." immediately instead of queueing work.
//
// What live testing showed (Microsoft Store build):
//   * A hook that merely FORWARDS while silent ("soft" void) was not enough:
//     the game still crashed on the very first hub -> mission transition.
//   * Physically UNPATCHING ProcessEvent for the whole silent window ("hard"
//     void, see VoidControllerThread) carried nine transitions in a row,
//     mission victory and locked-mission kills included, and /void hard no
//     longer crashes the game. The MinHook thread-freeze that an earlier
//     version of this comment feared mid level-load is therefore NOT the
//     problem when it is done from the controller thread; leaving our patch
//     in the engine's code during the transition is.
//   * The hook must not be patched in while the engine is still booting
//     either (see WaitForEngineBoot): early injection is fine, early patching
//     is not.
//
// Because the check lives in the hook itself, re-arming needs nothing from
// outside. Three independent ways back to ACTIVE, any one is enough:
//   1. world-stable : GWorld non-null and unchanged for SILENCE_SETTLE_MS
//   2. deadman cap  : silent for SILENCE_MAX_MS no matter what (cannot get stuck)
//   3. hook_wake    : optional pipe command, convenience only - never required
// Silence starts (a) at hook install, (b) whenever GWorld changes or goes
// null (level transition), (c) on the optional hook_silence <ms> command
// (kept for tooling; the client no longer calls it - a client-side "silence
// at mission end" switch, /endsilence, was removed: the world-change silence
// in hard void mode already covered those transitions).
static const ULONGLONG SILENCE_SETTLE_MS = 4000;    // world must sit still this long
static const ULONGLONG SILENCE_MAX_MS    = 30000;   // absolute cap per silent window (world non-null)
static const ULONGLONG SILENCE_FORCE_MAX = 30000;   // cap on hook_silence <ms>
// GWorld == 0 means the engine is still booting (or a world is being torn
// down). Cold start of the game can take well over SILENCE_MAX_MS, and going
// ACTIVE mid engine-init is exactly the startup risk this silence exists to
// avoid, so a NULL world gets a much longer cap. It still fails open in the
// end (e.g. a stale GWorld offset after a game update must not leave the
// hook silent forever) and then stays open until a non-null world is seen.
static const ULONGLONG SILENCE_MAX_NULLWORLD_MS = 180000;
// Queued CALL/CALLDATA/generic work older than this, or enqueued under a
// different GWorld than the current one, is cancelled instead of executed
// (its object pointers belong to a world that no longer exists).
static const ULONGLONG PENDING_WORK_MAX_AGE_MS = 5000;

static std::atomic<bool>      g_hookSilent{ true };
static std::atomic<ULONGLONG> g_silentSince{ 0 };
static std::atomic<ULONGLONG> g_lastWorldChange{ 0 };
static std::atomic<uint64_t>  g_lastSeenWorld{ 0 };
static std::atomic<ULONGLONG> g_forceSilentUntil{ 0 };
static std::atomic<bool>      g_nullWorldGaveUp{ false };

// Supply Station report dedupe (by actor address). File scope so a world
// change can clear it: UObject addresses are reused across levels, and a
// stale entry would make a NEW station at the same address never report.
static std::mutex g_reportedSupplyActorsMutex;

// Built lazily on first use (function-local static), exactly like the original
// function-local set inside hkProcessEvent_Impl: nothing is allocated at DLL
// load time, so DLL_PROCESS_ATTACH stays identical to the pre-patch build.
static std::unordered_set<uint64_t>& ReportedSupplyActors()
{
    static std::unordered_set<uint64_t> s;
    return s;
}

static void ClearSupplyDedupe()
{
    std::lock_guard<std::mutex> lock(g_reportedSupplyActorsMutex);
    ReportedSupplyActors().clear();
}

// Cancels everything queued for the game thread (defined after the queues).
static void PurgePendingWork(const char* why);

// Log lines produced by the silence state machine are only QUEUED here and
// written later (FlushSilenceLog) from an already-active hook call or from
// the pipe thread - no file I/O on the game thread in the middle of a level
// transition.
static std::mutex g_silenceLogMutex;
static std::vector<std::string> g_silenceLogQueue;

static void SilenceLog(const char* what, uint64_t world, ULONGLONG ms)
{
    std::ostringstream o;
    o << "HOOK " << what << " (world=0x" << std::hex << world << std::dec << ", " << ms
      << " ms, tick=" << GetTickCount64() << ")";
    std::lock_guard<std::mutex> lock(g_silenceLogMutex);
    if (g_silenceLogQueue.size() < 64)
        g_silenceLogQueue.push_back(o.str());
}

static void FlushSilenceLog()
{
    std::vector<std::string> local;
    {
        std::lock_guard<std::mutex> lock(g_silenceLogMutex);
        if (g_silenceLogQueue.empty())
            return;
        local.swap(g_silenceLogQueue);
    }
    for (const auto& line : local)
        LogLine(line);
}

// Returns true while the hook must stay silent. Safe from any thread; only the
// hook thread normally drives it, pipe handlers call it read-mostly.
static bool EvaluateSilencePatched()
{
    const ULONGLONG now = GetTickCount64();
    const uint64_t world = GetCurrentWorldPtr();
    const uint64_t prevWorld = g_lastSeenWorld.exchange(world, std::memory_order_relaxed);
    const bool nullWorld = (world == 0);
    if (!nullWorld)
        g_nullWorldGaveUp.store(false, std::memory_order_relaxed);
    bool silent = g_hookSilent.load(std::memory_order_relaxed);

    // Null -> non-null: the engine just created its first world. The silent
    // time spent so far (possibly minutes of engine boot) must not count
    // against the normal cap, or the deadman below would fire immediately and
    // skip the settle period this silence exists for.
    if (silent && prevWorld == 0 && world != 0)
        g_silentSince.store(now, std::memory_order_relaxed);

    // Deadman first: a silent window can never outlive its cap, even if the
    // world pointer keeps changing the whole time.
    const ULONGLONG cap = nullWorld ? SILENCE_MAX_NULLWORLD_MS : SILENCE_MAX_MS;
    if (silent && now - g_silentSince.load(std::memory_order_relaxed) >= cap)
    {
        g_hookSilent.store(false, std::memory_order_relaxed);
        g_lastWorldChange.store(now, std::memory_order_relaxed);
        if (nullWorld)
            g_nullWorldGaveUp.store(true, std::memory_order_relaxed);
        SilenceLog(nullWorld ? "ACTIVE: deadman cap reached (GWorld still null)" : "ACTIVE: deadman cap reached",
                   world, now - g_silentSince.load(std::memory_order_relaxed));
        return false;
    }

    if (world != prevWorld)                       // level transition begins / continues
    {
        g_lastWorldChange.store(now, std::memory_order_relaxed);
        ClearSupplyDedupe();
        PurgePendingWork("world changed");
        if (!silent)
        {
            g_silentSince.store(now, std::memory_order_relaxed);
            g_hookSilent.store(true, std::memory_order_relaxed);
            SilenceLog("SILENT: world changed", world, 0);
        }
        return true;
    }

    if (now < g_forceSilentUntil.load(std::memory_order_relaxed))
    {
        if (!silent)
        {
            g_silentSince.store(now, std::memory_order_relaxed);
            g_hookSilent.store(true, std::memory_order_relaxed);
            PurgePendingWork("hook_silence");
            SilenceLog("SILENT: forced by hook_silence", world, 0);
        }
        return true;
    }

    // NULL world (engine booting / world torn down): stay silent, up to the
    // long null-world cap handled above.
    if (nullWorld && !g_nullWorldGaveUp.load(std::memory_order_relaxed))
    {
        if (!silent)
        {
            g_silentSince.store(now, std::memory_order_relaxed);
            g_hookSilent.store(true, std::memory_order_relaxed);
            PurgePendingWork("GWorld null");
            SilenceLog("SILENT: GWorld null", world, 0);
        }
        return true;
    }

    if (!silent)
        return false;

    const ULONGLONG since = g_silentSince.load(std::memory_order_relaxed);
    const ULONGLONG lastChange = g_lastWorldChange.load(std::memory_order_relaxed);
    if (world != 0 && now - lastChange >= SILENCE_SETTLE_MS)
    {
        g_hookSilent.store(false, std::memory_order_relaxed);
        SilenceLog("ACTIVE: world stable", world, now - since);
        return false;
    }
    return true;
}

// ---- Launch-parity guard --------------------------------------------------
// Until the hook has gone ACTIVE ONCE in this session (i.e. the whole game
// start-up window), EvaluateSilence() runs the ORIGINAL pre-patch state
// machine below, byte-for-byte in behaviour (30 s deadman, direct logging, no
// purge / dedupe-clear side effects). That is the build that is known not to
// crash at launch. Everything the safety patch adds (null-world cap, purge of
// stale queued work on world change, deferred log queue, ...) only takes over
// AFTER the first activation, i.e. for level transitions during play.
static std::atomic<bool> g_everActive{ false };
// Set when SetupHookThread has FINISHED (hook enabled, or deliberately not
// installed: no RVA for this host / MinHook init failed). Until then
// hook_state reports "silent": the DLL is loaded but the engine has not been
// patched yet, and callers (the Python safety gate) must treat that exactly
// like the silent window. Once finished it never blocks anything by itself.
static std::atomic<bool> g_hookInstalled{ false };

// ---- "Void" state during level transitions -------------------------------
// While silent, NOTHING of ours may run inside the game's threads. Two levels:
//   soft (DLL start-up default, NOT the recommended mode - see below): the
//        detour stays patched in but its silent path is a
//        single relaxed atomic load followed by the original call. All state
//        evaluation (settle timer, deadman, GWorld watch, log flush) is done
//        by VoidControllerThread, not by game threads.
//   hard: on top of soft, the controller physically UNPATCHES ProcessEvent
//        (MH_DisableHook) for the whole silent window and re-patches it
//        (MH_EnableHook) when the world is stable again - during the window
//        the engine runs byte-for-byte as if this DLL had no hook at all.
//        Costs two MinHook thread-freezes per transition (done from the
//        controller thread, never from a game thread).
// Switchable live: pipe "set_void_mode soft|hard" (client: /void), or
// DUNGEONS_BRIDGE_VOID_MODE=hard in the environment for the next launch.
// The DLL itself starts in soft (its mode is per process); the client pushes
// its saved choice right after injection, and that choice defaults to HARD:
// in live tests soft crashed on the first hub -> mission transition while hard
// went through nine in a row (see the notes at "Autonomous hook silence").
enum VoidMode { VOID_SOFT = 0, VOID_HARD = 1 };
static std::atomic<int>       g_voidMode{ VOID_SOFT };
static std::atomic<bool>      g_hookPatched{ false };    // detour currently patched into ProcessEvent
static std::atomic<uintptr_t> g_peTarget{ 0 };
static std::atomic<uint32_t>  g_voidCalls{ 0 };          // safety-net counter for the silent path

// Crash guard state (the guard itself is defined further down, see "Crash guard").
static const int GUARD_MAX_SITES = 16;
static std::atomic<uintptr_t> g_guardSites[GUARD_MAX_SITES];
static std::atomic<int>       g_guardSiteCount{ 0 };
static std::atomic<bool>      g_guardEnabled{ true };
static std::atomic<uint32_t>  g_guardHits{ 0 };
static std::atomic<uintptr_t> g_guardLastRip{ 0 };
static std::atomic<uintptr_t> g_guardBase{ 0 };

enum VoidAction { VOID_NONE = 0, VOID_UNPATCH = 1, VOID_REPATCH = 2 };

// Pure decision, kept separate so it can be unit-tested.
static VoidAction DecideVoidAction(bool silent, int mode, bool patched)
{
    if (mode == VOID_HARD)
    {
        if (silent && patched)  return VOID_UNPATCH;
        if (!silent && !patched) return VOID_REPATCH;
        return VOID_NONE;
    }
    // soft: never leave the hook unpatched (covers a live switch hard -> soft)
    if (!patched) return VOID_REPATCH;
    return VOID_NONE;
}

static void SilenceLogDirect(const char* what, uint64_t world, ULONGLONG ms)
{
    std::ostringstream o;
    o << "HOOK " << what << " (world=0x" << std::hex << world << std::dec << ", " << ms << " ms)";
    LogLine(o.str());
}

static bool EvaluateSilenceLegacy()
{
    const ULONGLONG now = GetTickCount64();
    const uint64_t world = GetCurrentWorldPtr();
    const uint64_t prevWorld = g_lastSeenWorld.exchange(world, std::memory_order_relaxed);
    bool silent = g_hookSilent.load(std::memory_order_relaxed);

    if (silent && now - g_silentSince.load(std::memory_order_relaxed) >= SILENCE_MAX_MS)
    {
        g_hookSilent.store(false, std::memory_order_relaxed);
        g_lastWorldChange.store(now, std::memory_order_relaxed);
        SilenceLogDirect("ACTIVE: deadman cap reached", world, now - g_silentSince.load(std::memory_order_relaxed));
        return false;
    }

    if (world != prevWorld)
    {
        g_lastWorldChange.store(now, std::memory_order_relaxed);
        if (!silent)
        {
            g_silentSince.store(now, std::memory_order_relaxed);
            g_hookSilent.store(true, std::memory_order_relaxed);
            SilenceLogDirect("SILENT: world changed", world, 0);
        }
        return true;
    }

    if (now < g_forceSilentUntil.load(std::memory_order_relaxed))
    {
        if (!silent)
        {
            g_silentSince.store(now, std::memory_order_relaxed);
            g_hookSilent.store(true, std::memory_order_relaxed);
            SilenceLogDirect("SILENT: forced by hook_silence", world, 0);
        }
        return true;
    }

    if (!silent)
        return false;

    const ULONGLONG since = g_silentSince.load(std::memory_order_relaxed);
    const ULONGLONG lastChange = g_lastWorldChange.load(std::memory_order_relaxed);
    if (world != 0 && now - lastChange >= SILENCE_SETTLE_MS)
    {
        g_hookSilent.store(false, std::memory_order_relaxed);
        SilenceLogDirect("ACTIVE: world stable", world, now - since);
        return false;
    }
    return true;
}

static bool EvaluateSilence()
{
    if (!g_everActive.load(std::memory_order_relaxed))
    {
        const bool silent = EvaluateSilenceLegacy();
        if (!silent)
            g_everActive.store(true, std::memory_order_relaxed);   // from now on: patched logic
        return silent;
    }
    return EvaluateSilencePatched();
}

static void InitSilenceAtInstall()
{
    const ULONGLONG now = GetTickCount64();
    g_lastSeenWorld.store(GetCurrentWorldPtr(), std::memory_order_relaxed);
    g_lastWorldChange.store(now, std::memory_order_relaxed);
    g_silentSince.store(now, std::memory_order_relaxed);
    g_forceSilentUntil.store(0, std::memory_order_relaxed);
    g_nullWorldGaveUp.store(false, std::memory_order_relaxed);
    g_hookSilent.store(true, std::memory_order_relaxed);
}

static TUObjectArray* GetGObjects()
{
    InitOffsetsForHost();
    if (!Offsets::GObjects)
        return nullptr;
    return reinterpret_cast<TUObjectArray*>(GetImageBase() + Offsets::GObjects);
}

static AppendStringFn GetAppendStringFn()
{
    InitOffsetsForHost();
    if (!Offsets::AppendString)
        return nullptr;
    return reinterpret_cast<AppendStringFn>(GetImageBase() + Offsets::AppendString);
}

static ProcessEventFn GetProcessEventFn()
{
    InitOffsetsForHost();
    if (!Offsets::ProcessEvent)
        return nullptr;
    return reinterpret_cast<ProcessEventFn>(GetImageBase() + Offsets::ProcessEvent);
}

// ---- name resolution (unchanged from v2 - AppendString works fine off-thread) ----

static bool TryResolveName(int32_t comparisonIndex, wchar_t* outBuffer, int32_t bufferSize, int32_t& outLen)
{
    __try
    {
        AppendStringFn appendString = GetAppendStringFn();
        if (!appendString)
            return false;   // AppendString RVA unknown for this build - never call a guessed address
        SimpleFName name{ comparisonIndex, 0 };
        SimpleFString str{ outBuffer, 0, bufferSize };
        appendString(&name, str);
        outLen = str.Num;
        return true;
    }
    __except (EXCEPTION_EXECUTE_HANDLER)
    {
        return false;
    }
}

static bool TryReadOuterAndCount(void* objPtr, uintptr_t& outOuter, int32_t& outCount)
{
    __try
    {
        uintptr_t obj = reinterpret_cast<uintptr_t>(objPtr);
        outOuter = *reinterpret_cast<uintptr_t*>(obj + 0x20);
        outCount = *reinterpret_cast<int32_t*>(obj + 0x1FC);
        return true;
    }
    __except (EXCEPTION_EXECUTE_HANDLER)
    {
        return false;
    }
}

// Added after a beta tester's crash during a full-GObjects scan
// (FindObjectsWithOuter, used by find_function_on_class's class-hierarchy
// walk) - the per-object dereference inside the scan loop
// (TryReadOuterAndCount above) was already protected, but the array-walk
// machinery itself (GetGObjects()->Num(), ->GetByIndex()) was not. During a
// zone transition, this engine tears down and recreates a large chunk of
// its object graph in one burst (see the earlier discussion of loading-
// screen instability) - the chunk array TUObjectArray::Objects can itself
// be mid-reallocation/resize at exactly the moment a full scan is walking
// it, and unlike an individual object's fields, that's not something a
// per-element __try alone protects against.
static bool TryGetGObjectsCount(TUObjectArray* arr, int32_t& outNum)
{
    __try
    {
        outNum = arr->Num();
        return true;
    }
    __except (EXCEPTION_EXECUTE_HANDLER)
    {
        return false;
    }
}

static bool TryGetByIndexSafe(TUObjectArray* arr, int32_t index, void*& outPtr)
{
    __try
    {
        outPtr = arr->GetByIndex(index);
        return true;
    }
    __except (EXCEPTION_EXECUTE_HANDLER)
    {
        return false;
    }
}

static bool TryReadComparisonIndex(void* objPtr, int32_t& outIndex)
{
    __try
    {
        uintptr_t obj = reinterpret_cast<uintptr_t>(objPtr);
        outIndex = *reinterpret_cast<int32_t*>(obj + 0x18);
        return true;
    }
    __except (EXCEPTION_EXECUTE_HANDLER)
    {
        return false;
    }
}

static bool TryReadClassPtr(void* objPtr, void*& outClassPtr)
{
    __try
    {
        uintptr_t obj = reinterpret_cast<uintptr_t>(objPtr);
        outClassPtr = *reinterpret_cast<void**>(obj + 0x10);   // UObject::ClassPrivate
        return true;
    }
    __except (EXCEPTION_EXECUTE_HANDLER)
    {
        return false;
    }
}

static bool TryReadOuterPtr(void* objPtr, void*& outOuterPtr)
{
    __try
    {
        uintptr_t obj = reinterpret_cast<uintptr_t>(objPtr);
        outOuterPtr = *reinterpret_cast<void**>(obj + 0x20);   // UObject::OuterPrivate
        return true;
    }
    __except (EXCEPTION_EXECUTE_HANDLER)
    {
        return false;
    }
}

static bool TryGetObjectName(void* objPtr, std::string& outName)
{
    int32_t comparisonIndex = 0;
    if (!TryReadComparisonIndex(objPtr, comparisonIndex))
        return false;

    wchar_t buffer[1024];
    int32_t len = 0;
    if (!TryResolveName(comparisonIndex, buffer, 1024, len))
        return false;

    if (len <= 0)
        return false;

    int utf8Len = WideCharToMultiByte(CP_UTF8, 0, buffer, len, nullptr, 0, nullptr, nullptr);
    if (utf8Len <= 0)
        return false;

    std::string result(utf8Len, '\0');
    WideCharToMultiByte(CP_UTF8, 0, buffer, len, &result[0], utf8Len, nullptr, nullptr);
    // strip a trailing null terminator if AppendString's Num included it
    // (same reason the Python side does .rstrip("\x00") on resolve_name results)
    while (!result.empty() && result.back() == '\0')
        result.pop_back();
    outName = result;
    return true;
}

static bool TryGetClassName(void* objPtr, std::string& outName)
{
    void* classPtr = nullptr;
    if (!TryReadClassPtr(objPtr, classPtr) || !classPtr)
        return false;
    return TryGetObjectName(classPtr, outName);
}

std::string HandleResolveName(const std::string& hexIndex)
{
    int32_t comparisonIndex = 0;
    try
    {
        comparisonIndex = static_cast<int32_t>(std::stoul(hexIndex, nullptr, 16));
    }
    catch (...)
    {
        return "ERROR:could not parse index";
    }

    wchar_t buffer[1024];
    int32_t len = 0;
    if (!TryResolveName(comparisonIndex, buffer, 1024, len))
        return "ERROR:AppendString call faulted";

    if (len <= 0)
        return "ERROR:empty result (invalid index?)";

    int utf8Len = WideCharToMultiByte(CP_UTF8, 0, buffer, len, nullptr, 0, nullptr, nullptr);
    if (utf8Len <= 0)
        return "ERROR:UTF8 conversion failed";

    std::string result(utf8Len, '\0');
    WideCharToMultiByte(CP_UTF8, 0, buffer, len, &result[0], utf8Len, nullptr, nullptr);

    return "NAME:" + result;
}

// ---- generic thread-hopped work (for anything besides ProcessEvent that
// also needs to run on the game's own thread - the GObjects walk/AppendString
// loop turned out to need this too, same root cause as ProcessEvent did) ----

static std::mutex g_genericQueueMutex;
struct GenericJob
{
    std::function<void()> run;
    std::function<void()> cancel;   // wakes the waiting pipe thread without running the work
    ULONGLONG enqueuedAt = 0;
    uint64_t world = 0;             // GWorld when enqueued
};
static std::queue<GenericJob> g_pendingGenericWork;

static void QueueAndWaitForRenderThread(const std::function<void()>& work)
{
    auto localMutex = std::make_shared<std::mutex>();
    auto localCv = std::make_shared<std::condition_variable>();
    auto done = std::make_shared<bool>(false);

    std::function<void()> wrapped = [work, localMutex, localCv, done]()
    {
        work();
        {
            std::lock_guard<std::mutex> lock(*localMutex);
            *done = true;
        }
        localCv->notify_all();
    };

    std::function<void()> cancelFn = [localMutex, localCv, done]()
    {
        {
            std::lock_guard<std::mutex> lock(*localMutex);
            *done = true;
        }
        localCv->notify_all();
    };

    {
        std::lock_guard<std::mutex> lock(g_genericQueueMutex);
        GenericJob job;
        job.run = wrapped;
        job.cancel = cancelFn;
        job.enqueuedAt = GetTickCount64();
        job.world = GetCurrentWorldPtr();
        g_pendingGenericWork.push(job);
    }

    std::unique_lock<std::mutex> lock(*localMutex);
    localCv->wait_for(lock, std::chrono::seconds(10), [&] { return *done; });
}

static void DrainGenericWork()
{
    std::queue<GenericJob> localQueue;
    {
        std::lock_guard<std::mutex> lock(g_genericQueueMutex);
        std::swap(localQueue, g_pendingGenericWork);
    }
    while (!localQueue.empty())
    {
        GenericJob job = localQueue.front();
        localQueue.pop();
        // Stale work (older than the caller's own wait, or queued under a
        // different world) is cancelled, never executed against dead pointers.
        const bool stale = (GetTickCount64() - job.enqueuedAt > PENDING_WORK_MAX_AGE_MS) ||
                           (job.world != GetCurrentWorldPtr());
        if (stale)
        {
            if (job.cancel) job.cancel();
        }
        else if (job.run)
        {
            job.run();
        }
    }
}

std::string HandleFindByName(const std::string& targetName)
{
    auto result = std::make_shared<std::string>("ERROR:did not run");

    QueueAndWaitForRenderThread([targetName, result]()
    {
        TUObjectArray* gObjects = GetGObjects();
        if (!gObjects)
        {
            *result = "ERROR:could not resolve GObjects";
            return;
        }

        std::ostringstream matches;
        int matchCount = 0;
        int32_t total = 0;
        if (!TryGetGObjectsCount(gObjects, total))
        {
            *result = "ERROR:GObjects count read faulted (likely mid zone-transition) - retry later";
            return;
        }

        for (int32_t i = 0; i < total; ++i)
        {
            void* objPtr = nullptr;
            if (!TryGetByIndexSafe(gObjects, i, objPtr))
                continue;  // this slot's chunk faulted - skip it, don't abort the whole scan
            if (!objPtr)
                continue;

            std::string name;
            if (!TryGetObjectName(objPtr, name))
                continue;

            if (name == targetName)
            {
                if (matchCount > 0)
                    matches << ",";
                matches << std::hex << reinterpret_cast<uintptr_t>(objPtr);
                matchCount++;
            }
        }

        std::ostringstream out;
        out << "FOUND:" << matchCount;
        if (matchCount > 0)
            out << "|" << matches.str();
        *result = out.str();
    });

    return *result;
}

std::string FindObjectsWithOuter(uintptr_t targetOuter)
{
    auto result = std::make_shared<std::string>("ERROR:did not run");

    QueueAndWaitForRenderThread([targetOuter, result]()
    {
        TUObjectArray* gObjects = GetGObjects();
        if (!gObjects)
        {
            *result = "ERROR:could not resolve GObjects";
            return;
        }

        std::ostringstream matches;
        int matchCount = 0;
        int32_t total = 0;
        if (!TryGetGObjectsCount(gObjects, total))
        {
            *result = "ERROR:GObjects count read faulted (likely mid zone-transition) - retry later";
            return;
        }

        for (int32_t i = 0; i < total; ++i)
        {
            void* objPtr = nullptr;
            if (!TryGetByIndexSafe(gObjects, i, objPtr))
                continue;  // this slot's chunk faulted - skip it, don't abort the whole scan
            if (!objPtr)
                continue;

            uintptr_t outer = 0;
            int32_t countAsItemSlot = 0;
            if (!TryReadOuterAndCount(objPtr, outer, countAsItemSlot))
                continue;

            if (outer == targetOuter)
            {
                if (matchCount > 0)
                    matches << ",";
                matches << std::hex << reinterpret_cast<uintptr_t>(objPtr) << ":" << std::dec << countAsItemSlot;
                matchCount++;
            }
        }

        std::ostringstream out;
        out << "FOUND:" << matchCount;
        if (matchCount > 0)
            out << "|" << matches.str();
        *result = out.str();
    });

    return *result;
}

// ---- NEW: thread-hopped ProcessEvent calling ----

struct CallRequest
{
    uintptr_t objectAddr;
    uintptr_t functionAddr;
    size_t parmsSize;
    std::vector<uint8_t> parms;   // in/out - filled by the caller, overwritten in place by ProcessEvent
    bool done = false;
    bool faulted = false;
    DWORD exceptionCode = 0;
    bool cancelled = false;         // pipe thread gave up waiting (timeout) - never execute it afterwards
    ULONGLONG enqueuedAt = 0;
    uint64_t world = 0;             // GWorld when enqueued
};

static const DWORD kCallCancelledCode = 0xE0000002;   // private: cancelled / stale, never executed

static std::mutex g_queueMutex;
static std::queue<std::shared_ptr<CallRequest>> g_pendingCalls;

static std::mutex g_resultMutex;
static std::condition_variable g_resultCv;

// Isolated POD-only helper - no C++ objects with destructors in scope,
// same MSVC restriction as everywhere else __try is used in this file.
// Uses the comma-operator trick in the filter expression to actually
// capture the exception code (GetExceptionCode() is only valid inside
// the filter expression itself, not the __except body).
static bool TryProcessEventCall(uintptr_t objectAddr, uintptr_t functionAddr, uint8_t* parms, DWORD& outExceptionCode)
{
    DWORD code = 0;
    __try
    {
        ProcessEventFn pe = GetProcessEventFn();
        if (!pe)
        {
            outExceptionCode = 0xE0000001;   // private code: no ProcessEvent RVA for this build
            return false;
        }
        pe(reinterpret_cast<void*>(objectAddr), reinterpret_cast<void*>(functionAddr), parms);
        return true;
    }
    __except ((code = GetExceptionCode()), EXCEPTION_EXECUTE_HANDLER)
    {
        outExceptionCode = code;
        return false;
    }
}

// Runs INSIDE the Present hook - i.e. on the correct thread. Drains
// whatever's queued and services each one synchronously before letting
// the frame continue.
static void DrainPendingCalls()
{
    std::queue<std::shared_ptr<CallRequest>> localQueue;
    {
        std::lock_guard<std::mutex> lock(g_queueMutex);
        std::swap(localQueue, g_pendingCalls);
    }

    while (!localQueue.empty())
    {
        auto req = localQueue.front();
        localQueue.pop();

        // Never execute a request whose caller already gave up, that sat too
        // long, or that was queued under a previous world: its object
        // pointers may be dead or reused.
        bool stale;
        {
            std::lock_guard<std::mutex> lock(g_resultMutex);
            stale = req->cancelled || req->done;
        }
        if (!stale)
            stale = (GetTickCount64() - req->enqueuedAt > PENDING_WORK_MAX_AGE_MS) ||
                    (req->world != GetCurrentWorldPtr());
        if (stale)
        {
            {
                std::lock_guard<std::mutex> lock(g_resultMutex);
                if (!req->done)
                {
                    req->done = true;
                    req->faulted = true;
                    req->exceptionCode = kCallCancelledCode;
                }
            }
            g_resultCv.notify_all();
            continue;
        }

        DWORD exceptionCode = 0;
        bool ok = TryProcessEventCall(req->objectAddr, req->functionAddr, req->parms.data(), exceptionCode);

        {
            std::lock_guard<std::mutex> lock(g_resultMutex);
            req->done = true;
            req->faulted = !ok;
            req->exceptionCode = exceptionCode;
        }
        g_resultCv.notify_all();
    }
}

static void PurgePendingWork(const char* /*why*/)
{
    std::queue<std::shared_ptr<CallRequest>> calls;
    {
        std::lock_guard<std::mutex> lock(g_queueMutex);
        std::swap(calls, g_pendingCalls);
    }
    if (!calls.empty())
    {
        {
            std::lock_guard<std::mutex> lock(g_resultMutex);
            while (!calls.empty())
            {
                auto r = calls.front();
                calls.pop();
                if (!r->done)
                {
                    r->done = true;
                    r->faulted = true;
                    r->exceptionCode = kCallCancelledCode;
                }
            }
        }
        g_resultCv.notify_all();
    }

    std::queue<GenericJob> jobs;
    {
        std::lock_guard<std::mutex> lock(g_genericQueueMutex);
        std::swap(jobs, g_pendingGenericWork);
    }
    while (!jobs.empty())
    {
        if (jobs.front().cancel)
            jobs.front().cancel();
        jobs.pop();
    }
}

// Called from the pipe thread - queues the request, then blocks (with a
// timeout, in case the game is paused/minimized and Present stops
// firing) until the render thread services it.
std::string HandleCall(const std::string& args)
{
    std::istringstream iss(args);
    std::string objHex, funcHex, sizeHex;
    if (!(iss >> objHex >> funcHex >> sizeHex))
        return "ERROR bad request format (expected: CALL <obj_hex> <func_hex> <size_hex>)";

    uintptr_t objectAddr, functionAddr;
    size_t parmsSize;
    try
    {
        objectAddr = static_cast<uintptr_t>(std::stoull(objHex, nullptr, 16));
        functionAddr = static_cast<uintptr_t>(std::stoull(funcHex, nullptr, 16));
        parmsSize = static_cast<size_t>(std::stoull(sizeHex, nullptr, 16));
    }
    catch (...)
    {
        return "ERROR could not parse request";
    }

    if (parmsSize == 0 || parmsSize > 0x10000)
        return "ERROR parms_size out of sane range";

    auto req = std::make_shared<CallRequest>();
    req->objectAddr = objectAddr;
    req->functionAddr = functionAddr;
    req->parmsSize = parmsSize;
    req->parms.resize(parmsSize, 0);
    req->enqueuedAt = GetTickCount64();
    req->world = GetCurrentWorldPtr();

    {
        std::lock_guard<std::mutex> lock(g_queueMutex);
        g_pendingCalls.push(req);
    }

    std::unique_lock<std::mutex> lock(g_resultMutex);
    bool serviced = g_resultCv.wait_for(lock, std::chrono::seconds(5), [&] { return req->done; });

    if (!serviced)
    {
        req->cancelled = true;   // g_resultMutex is held here (wait_for returned) - Drain will skip it
        return "ERROR timed out waiting for render thread (is the game minimized/frozen?)";
    }

    if (req->faulted && req->exceptionCode == kCallCancelledCode)
        return "ERROR:cancelled (hook silent / world changed) - retry shortly";

    if (req->faulted)
    {
        std::ostringstream err;
        err << "ERROR exception during call (code 0x" << std::hex << req->exceptionCode << ")";
        return err.str();
    }

    std::ostringstream out;
    out << "OK ";
    for (uint8_t b : req->parms)
    {
        char buf[3];
        sprintf_s(buf, "%02X", b);
        out << buf;
    }
    return out.str();
}

// CALLDATA <obj_hex> <func_hex> <parms_hex> - same as CALL, but the parms
// buffer starts as the ACTUAL BYTES given instead of zeros. Needed for
// functions that take real input (like ClientAddItem's FInventoryItemData
// parameter), as opposed to GetDisplayNameText-style calls that only need
// a zeroed buffer for an out-parameter.
std::string HandleCallData(const std::string& args)
{
    std::istringstream iss(args);
    std::string objHex, funcHex, parmsHex;
    if (!(iss >> objHex >> funcHex >> parmsHex))
        return "ERROR bad request format (expected: CALLDATA <obj_hex> <func_hex> <parms_hex>)";

    uintptr_t objectAddr, functionAddr;
    try
    {
        objectAddr = static_cast<uintptr_t>(std::stoull(objHex, nullptr, 16));
        functionAddr = static_cast<uintptr_t>(std::stoull(funcHex, nullptr, 16));
    }
    catch (...)
    {
        return "ERROR could not parse request";
    }

    if (parmsHex.size() % 2 != 0)
        return "ERROR parms_hex must have an even number of hex digits";

    size_t parmsSize = parmsHex.size() / 2;
    if (parmsSize == 0 || parmsSize > 0x10000)
        return "ERROR parms_size out of sane range";

    auto req = std::make_shared<CallRequest>();
    req->objectAddr = objectAddr;
    req->functionAddr = functionAddr;
    req->parmsSize = parmsSize;
    req->parms.resize(parmsSize, 0);

    for (size_t i = 0; i < parmsSize; ++i)
    {
        std::string byteStr = parmsHex.substr(i * 2, 2);
        req->parms[i] = static_cast<uint8_t>(std::stoul(byteStr, nullptr, 16));
    }
    req->enqueuedAt = GetTickCount64();
    req->world = GetCurrentWorldPtr();

    {
        std::lock_guard<std::mutex> lock(g_queueMutex);
        g_pendingCalls.push(req);
    }

    std::unique_lock<std::mutex> lock(g_resultMutex);
    bool serviced = g_resultCv.wait_for(lock, std::chrono::seconds(5), [&] { return req->done; });

    if (!serviced)
    {
        req->cancelled = true;   // g_resultMutex is held here (wait_for returned) - Drain will skip it
        return "ERROR timed out waiting for render thread (is the game minimized/frozen?)";
    }

    if (req->faulted && req->exceptionCode == kCallCancelledCode)
        return "ERROR:cancelled (hook silent / world changed) - retry shortly";

    if (req->faulted)
    {
        std::ostringstream err;
        err << "ERROR exception during call (code 0x" << std::hex << req->exceptionCode << ")";
        return err.str();
    }

    std::ostringstream out;
    out << "OK ";
    for (uint8_t b : req->parms)
    {
        char buf[3];
        sprintf_s(buf, "%02X", b);
        out << buf;
    }
    return out.str();
}

// ---- Chest-open detection via a global ProcessEvent hook ----
// Confirmed via live logging (dungeons_bridge_debug.log): OnOpenLootChest
// never actually fires - it's a reflected UFunction that exists in the
// SDK but isn't wired to any real code path in this build. What DOES
// fire, confirmed against real opens of a Fancy chest and a Supply
// station: an "OnInteracted"-suffixed bound-event function (there are
// several distinct ones per interactable-component type - "Clicky",
// "InteractableComp", "ClickyComponent" all seen), with `self` resolving
// directly to the real actor class - BP_FancyChest_C, BP_SupplyStation_C,
// confirmed via the SAME name-resolution machinery already proven
// reliable all session. No memory-offset guessing, no polling, no
// heuristic byte-shape matching - just the game's own class identity,
// straight from GNames.
//
// Since there are MULTIPLE distinct "OnInteracted" function pointers
// (one per component type), a single cached pointer (like the old
// OnOpenLootChest attempt) doesn't work - this classifies and caches
// PER function pointer instead, both positive and negative results, so
// after the first time any given function is seen, every future call
// through it is an O(1) hash lookup instead of a name resolve.
//
// Filtering WHICH actor classes count as "a chest" happens on the
// PYTHON side, not here - class names differ per class (Chest vs
// SupplyStation) and will differ further for Wooden/Deluxe once
// confirmed, and keeping that list in Python means updating it never
// requires recompiling/reinjecting this DLL again.

struct InteractEvent
{
    uint64_t actorAddr;
    std::string className;
    uint64_t worldPtr;  // GWorld at the exact moment of this interact - the Python side
                         // resolves this into a zone name via get_zone_name_index(pm, worldPtr),
                         // instead of relying on its own separately-polled "current zone"
                         // tracking, which could race a zone transition happening between
                         // this event firing and the event queue being drained.
};

static std::mutex g_interactEventsMutex;
static std::vector<InteractEvent> g_interactEvents;   // drained by the pipe thread via get_chest_events

// Matches the WHOLE interact family, including "OnInteract" (no "ed")
// and "OnInteracted". Confirmed live this matters for pickup BLOCKING: a Food pickup's healing effect still applied even
// though its "OnInteracted" call was correctly blocked, because the
// actual effect gets applied during the EARLIER "OnInteract" call,
// which the narrow match never saw at all. Used specifically for the
// pickup-tier blocking check - NOT for chest-open reporting, which uses
// the even narrower IsChestReportFunction below instead.
// A function whose NAME could not be resolved must not be permanently
// negative-cached on the first failure (GNames/AppendString can fail
// transiently while objects are mid-construction): that would kill its
// detection for the whole session. Only cache a negative once the name
// resolved, or after NAME_FAIL_LIMIT failed attempts on the same pointer
// (so a truly unresolvable one still stops costing a resolve per call).
static std::mutex g_nameFailMutex;
static std::unordered_map<void*, int>& NameFailCounts()   // lazy: nothing allocated at DLL load
{
    static std::unordered_map<void*, int> m;
    return m;
}
static const int NAME_FAIL_LIMIT = 8;

static bool ShouldCacheNegative(void* function, bool nameResolved)
{
    if (nameResolved)
        return true;
    std::lock_guard<std::mutex> lock(g_nameFailMutex);
    return ++NameFailCounts()[function] >= NAME_FAIL_LIMIT;
}

static std::mutex g_interactFamilyClassifyMutex;
static std::unordered_set<void*> g_interactFamilyFunctionPtrs;
static std::unordered_set<void*> g_nonInteractFamilyFunctionPtrs;

static bool IsInteractFamilyFunction(void* function)
{
    {
        std::lock_guard<std::mutex> lock(g_interactFamilyClassifyMutex);
        if (g_interactFamilyFunctionPtrs.count(function))
            return true;
        if (g_nonInteractFamilyFunctionPtrs.count(function))
            return false;
    }

    std::string name;
    const bool resolved = TryGetObjectName(function, name);
    bool isMatch = resolved &&
                   name.find("Interact") != std::string::npos;

    std::lock_guard<std::mutex> lock(g_interactFamilyClassifyMutex);
    if (isMatch)
    {
        g_interactFamilyFunctionPtrs.insert(function);
        LogLine("Classified new Interact-family function (blocking-eligible): " + name);
    }
    else
    {
        if (ShouldCacheNegative(function, resolved))
            g_nonInteractFamilyFunctionPtrs.insert(function);
    }
    return isMatch;
}

// Narrow match for CHEST-OPEN EVENT REPORTING specifically - "OnInteract"
// but explicitly NOT "OnInteracted". Chests can fire "OnInteracted"
// multiple times per single physical open (confirmed live - e.g. once
// per item that spawns from the chest), which would double/triple-
// report one open if used for counting. "OnInteract" (the earlier
// trigger, no "ed") fires exactly once per genuine interaction attempt,
// which is what chest-open counting actually needs.
static std::mutex g_chestReportClassifyMutex;
static std::unordered_set<void*> g_chestReportFunctionPtrs;
static std::unordered_set<void*> g_nonChestReportFunctionPtrs;

static bool IsChestReportFunction(void* function)
{
    {
        std::lock_guard<std::mutex> lock(g_chestReportClassifyMutex);
        if (g_chestReportFunctionPtrs.count(function))
            return true;
        if (g_nonChestReportFunctionPtrs.count(function))
            return false;
    }

    std::string name;
    const bool resolved = TryGetObjectName(function, name);
    bool isMatch = resolved &&
                   name.find("OnInteract") != std::string::npos &&
                   name.find("OnInteracted") == std::string::npos;

    std::lock_guard<std::mutex> lock(g_chestReportClassifyMutex);
    if (isMatch)
    {
        g_chestReportFunctionPtrs.insert(function);
        LogLine("Classified new OnInteract-only (chest-reporting) function: " + name);
    }
    else
    {
        if (ShouldCacheNegative(function, resolved))
            g_nonChestReportFunctionPtrs.insert(function);
    }
    return isMatch;
}

static std::string ToLowerCopy(const std::string& s)
{
    std::string out = s;
    std::transform(out.begin(), out.end(), out.begin(),
                    [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
    return out;
}

static void CaptureInteractEvent(uint64_t actorAddr, const std::string& className)
{
    InteractEvent evt;
    evt.actorAddr = actorAddr;
    evt.className = className;
    evt.worldPtr = GetCurrentWorldPtr();

    std::lock_guard<std::mutex> lock(g_interactEventsMutex);
    g_interactEvents.push_back(evt);
}

// ---- Progressive pickup gating ----
// Three tiers, unlocked in order as the player receives copies of a
// progressive AP item (client-side logic, not this DLL's concern - this
// DLL just enforces whatever tier Python currently says is unlocked):
//   tier 1 = Health items (food) pickable
//   tier 2 = + Potions pickable
//   tier 3 = + TNT pickable
// Weapons, armor, artifacts, tokens, eye of ender, arrows, and any
// non-pickup interactable (UI objects like BP_MapTable_C) are NEVER
// gated, regardless of tier - returned as tier -1, meaning "not
// gateable at all, never block". Tier 3 intentionally gates ONLY TNT,
// not the broad "everything else" it used to - weapon/armor/artifact
// names vary too much to enumerate or pattern-match reliably, so
// instead of guessing which of them to gate, none of them are.
//
// Classified by NAME PATTERN, not an enumerated class list - this
// covers every pickup class in the game automatically, including ones
// never directly observed in testing yet, same philosophy as
// classify_interactable_class on the Python side. Confirmed real
// examples from live testing: BP_Food1/2/3Storable_C (health),
// BP_StrengthPotionStorable_C / BP_SwiftnessPotionStorable_C /
// BP_BackstabbersBrewStorable_C (potion - "brew" matched too, since
// Backstabber's Brew doesn't literally contain "Potion"),
// BP_TNTBoxStorable_C (TNT).
static int ClassifyPickupTier(const std::string& className)
{
    std::string lower = ToLowerCopy(className);

    if (lower.find("storable_c") == std::string::npos)
        return -1;  // not a pickup at all
    if (lower.find("food") != std::string::npos)
        return 1;
    if (lower.find("potion") != std::string::npos || lower.find("brew") != std::string::npos)
        return 2;
    if (lower.find("tnt") != std::string::npos)
        return 3;
    return -1;  // weapons, armor, artifacts, tokens, eye of ender, arrows, etc - never gated
}

// Defaults LOCKED (tier 0 - nothing beyond arrows pickable) rather than
// unlocked, so the gate fails safe: if Python's watcher hasn't connected
// yet or crashes, pickups stay blocked instead of silently becoming
// unrestricted and defeating the whole progression rule.
static std::atomic<int> g_pickupUnlockTier{ 0 };

typedef void(*ProcessEventHookFn)(void*, void*, void*);
static ProcessEventHookFn oProcessEvent = nullptr;

static std::atomic<uint64_t> g_processEventCallCount{ 0 };

// ---- Character-death detection. Same classify-and-cache pattern as
// IsChestReportFunction above, checking for the exact reflected name
// "OnCharacterDeath" (Dumper-7 SDK, ABaseCharacter::OnCharacterDeath(),
// no params - `self` is the dying actor).
//
// IMPORTANT: OnOpenLootChest looked exactly this solid on paper too - a
// genuine reflected UFunction the SDK confirms exists - and turned out to
// never actually fire via ProcessEvent in this build. Don't repeat that
// mistake blind: alongside the exact-name check, this also logs the
// first-ever sighting of ANY function whose name contains "death"
// case-insensitively, matched or not, so if OnCharacterDeath is also
// SDK-only and something differently-named is what actually fires on a
// real kill, it shows up in dungeons_bridge_debug.log instead of another
// silent dead end.
struct CharacterDeathEvent
{
    uint64_t actorAddr;
};

static std::mutex g_deathEventsMutex;
static std::vector<CharacterDeathEvent> g_deathEvents;   // drained by the pipe thread via get_death_events

static std::mutex g_deathClassifyMutex;
static std::unordered_set<void*> g_deathFunctionPtrs;       // confirmed exact "OnCharacterDeath"
static std::unordered_set<void*> g_nonDeathFunctionPtrs;    // confirmed NOT - avoids re-resolving every call
static std::unordered_set<void*> g_loggedDeathLikeFunctionPtrs;  // already logged as a "death"-ish name, don't spam

// ---- Secret-mission-unlock detection. Same classify-and-cache pattern as
// IsDeathFunction above. Confirmed via the SDK dump (Dungeons_functions.cpp):
// USecretComponent::SecretFound(AActor* SecretFinder) - fires on the
// component attached to whatever physical trigger (lever, hidden wall,
// etc) reveals a secret mission. USecretComponent::ExecuteSecretFound is
// ALSO a real UFunction in the dump (Dumper-7's wrapper-call name for the
// same BlueprintImplementableEvent, or possibly a distinct entry point) -
// classify on EITHER exact name, since it's not certain from the dump
// alone which one actually fires via ProcessEvent at runtime. Same
// unmatched-but-"secret"-ish diagnostic logging fallback as the death
// classifier, in case neither name is what really fires.
struct SecretFoundEvent
{
    uint64_t actorAddr;
    std::string className;
};

static std::mutex g_secretEventsMutex;
static std::vector<SecretFoundEvent> g_secretEvents;   // drained by the pipe thread via get_secret_events

static std::mutex g_secretClassifyMutex;
static std::unordered_set<void*> g_secretFunctionPtrs;      // confirmed exact "SecretFound" or "ExecuteSecretFound"
static std::unordered_set<void*> g_nonSecretFunctionPtrs;   // confirmed NOT - avoids re-resolving every call
static std::unordered_set<void*> g_loggedSecretLikeFunctionPtrs;  // already logged as a "secret"-ish name, don't spam

static bool IsSecretFoundFunction(void* function)
{
    {
        std::lock_guard<std::mutex> lock(g_secretClassifyMutex);
        if (g_secretFunctionPtrs.count(function))
            return true;
        if (g_nonSecretFunctionPtrs.count(function))
            return false;
    }

    std::string name;
    bool resolved = TryGetObjectName(function, name);
    bool isExactMatch = resolved && (name == "SecretFound" || name == "ExecuteSecretFound");

    if (resolved && !isExactMatch && ToLowerCopy(name).find("secret") != std::string::npos)
    {
        std::lock_guard<std::mutex> lock(g_secretClassifyMutex);
        if (!g_loggedSecretLikeFunctionPtrs.count(function))
        {
            g_loggedSecretLikeFunctionPtrs.insert(function);
            LogLine("Secret-like (but not exact 'SecretFound'/'ExecuteSecretFound') function seen: " + name);
        }
    }

    std::lock_guard<std::mutex> lock(g_secretClassifyMutex);
    if (isExactMatch)
    {
        g_secretFunctionPtrs.insert(function);
        LogLine("Classified exact SecretFound-pattern function pointer: " + name);
    }
    else
    {
        if (ShouldCacheNegative(function, resolved))
            g_nonSecretFunctionPtrs.insert(function);
    }
    return isExactMatch;
}

static void CaptureSecretFoundEvent(void* self)
{
    std::string className;
    if (!TryGetClassName(self, className))
        return;

    SecretFoundEvent evt;
    evt.actorAddr = reinterpret_cast<uint64_t>(self);
    evt.className = className;

    std::lock_guard<std::mutex> lock(g_secretEventsMutex);
    g_secretEvents.push_back(evt);
}

// ---- Currency widget hook: OnValueChanged / OnCurrencyTypeChanged on
// UMG_CurrencyCounterBase_C (and its Blueprint children - UMG_EmeraldCounter_C,
// etc). Unlike the native p_currency getter defined further below (which
// needs hand-confirmed fixed offsets, and got Gold/Eyes of Ender swapped
// once already), these are real reflected UFUNCTIONs the game calls with
// the value it's ABOUT to display, directly as a parameter - no offset
// guessing at all. Useful as a cross-check against CURRENCY_OFFSETS, or
// as a fallback read path when the HUD widget is on-screen.
//
// Only fires while the relevant currency counter widget actually exists
// (HUD visible) - NOT a replacement for the always-available
// p_currency-based read/write path, just a second, offset-free source of
// truth to validate against.
//
// Defined here (before hkProcessEvent, which calls into these classifiers)
// rather than down by hkCurrencyGetter - these have nothing to do with the
// native p_currency pointer capture and don't need to sit next to it; they
// DO need to be declared before hkProcessEvent uses them below.

struct CurrencyValueEvent
{
    uint64_t widgetAddr;
    int32_t newValue;
    int32_t previousValue;
};

struct CurrencyTypeEvent
{
    uint64_t widgetAddr;
    uint32_t serializedIdIndex;  // FSerializableItemId::SerializedId's FName
                                  // ComparisonIndex - same name_index space
                                  // used everywhere else (items, zones, etc),
                                  // so the Python side can label it via the
                                  // usual name-lookup workflow.
};

static std::mutex g_currencyValueEventsMutex;
static std::vector<CurrencyValueEvent> g_currencyValueEvents;

static std::mutex g_currencyTypeEventsMutex;
static std::vector<CurrencyTypeEvent> g_currencyTypeEvents;

// Exact-name match (not a substring family match like IsInteractFamilyFunction) -
// "OnValueChanged" and "OnCurrencyTypeChanged" are specific, unambiguous event
// names, but ARE generic enough that some unrelated widget elsewhere in the
// game could coincidentally share the name "OnValueChanged" (a slider, a
// settings widget, etc). Gated additionally on the INSTANCE's class name
// containing "Counter" (case-insensitive) below, at the call site - covers
// UMG_CurrencyCounterBase_C itself plus every Blueprint child
// (UMG_EmeraldCounter_C, UMG_GoldCounter_C, UMG_EyeOfEnderCounter_C, ...)
// without needing to enumerate them by name.
static std::mutex g_currencyValueClassifyMutex;
static std::unordered_set<void*> g_currencyValueFunctionPtrs;
static std::unordered_set<void*> g_nonCurrencyValueFunctionPtrs;

static bool IsCurrencyValueChangedFunction(void* function)
{
    {
        std::lock_guard<std::mutex> lock(g_currencyValueClassifyMutex);
        if (g_currencyValueFunctionPtrs.count(function))
            return true;
        if (g_nonCurrencyValueFunctionPtrs.count(function))
            return false;
    }

    std::string name;
    const bool resolved = TryGetObjectName(function, name);
    bool isMatch = resolved && name == "OnValueChanged";

    std::lock_guard<std::mutex> lock(g_currencyValueClassifyMutex);
    if (isMatch)
    {
        g_currencyValueFunctionPtrs.insert(function);
        LogLine("Classified new OnValueChanged function (currency-widget candidate): " + name);
    }
    else
    {
        if (ShouldCacheNegative(function, resolved))
            g_nonCurrencyValueFunctionPtrs.insert(function);
    }
    return isMatch;
}

static std::mutex g_currencyTypeClassifyMutex;
static std::unordered_set<void*> g_currencyTypeFunctionPtrs;
static std::unordered_set<void*> g_nonCurrencyTypeFunctionPtrs;

static bool IsCurrencyTypeChangedFunction(void* function)
{
    {
        std::lock_guard<std::mutex> lock(g_currencyTypeClassifyMutex);
        if (g_currencyTypeFunctionPtrs.count(function))
            return true;
        if (g_nonCurrencyTypeFunctionPtrs.count(function))
            return false;
    }

    std::string name;
    const bool resolved = TryGetObjectName(function, name);
    bool isMatch = resolved && name == "OnCurrencyTypeChanged";

    std::lock_guard<std::mutex> lock(g_currencyTypeClassifyMutex);
    if (isMatch)
    {
        g_currencyTypeFunctionPtrs.insert(function);
        LogLine("Classified new OnCurrencyTypeChanged function (currency-widget candidate): " + name);
    }
    else
    {
        if (ShouldCacheNegative(function, resolved))
            g_nonCurrencyTypeFunctionPtrs.insert(function);
    }
    return isMatch;
}

static void CaptureCurrencyValueEvent(uint64_t widgetAddr, void* params)
{
    // Params layout: two plain (non-out) int32 parms in declared order -
    // int32 newValue @ +0x00, int32 previousValue @ +0x04. Standard UE
    // ProcessEvent params blob layout for two consecutive by-value int32s.
    CurrencyValueEvent evt;
    evt.widgetAddr = widgetAddr;
    evt.newValue = *reinterpret_cast<int32_t*>(reinterpret_cast<uint8_t*>(params) + 0x00);
    evt.previousValue = *reinterpret_cast<int32_t*>(reinterpret_cast<uint8_t*>(params) + 0x04);

    std::lock_guard<std::mutex> lock(g_currencyValueEventsMutex);
    g_currencyValueEvents.push_back(evt);
}

static void CaptureCurrencyTypeEvent(uint64_t widgetAddr, void* params)
{
    // Params layout: single FSerializableItemId (0x14 bytes) at offset 0.
    // Its SerializedId FName sits at +0x0C within that struct (see
    // Dungeons_structs.hpp's FSerializableItemId - Pad_0[0xC] then FName
    // SerializedId @ 0x0C). An FName's first 4 bytes are ComparisonIndex,
    // which is the same "name_index" numbering used for items/zones/etc
    // elsewhere in this DLL and in dungeons_reader.py.
    CurrencyTypeEvent evt;
    evt.widgetAddr = widgetAddr;
    evt.serializedIdIndex = *reinterpret_cast<uint32_t*>(reinterpret_cast<uint8_t*>(params) + 0x0C);

    std::lock_guard<std::mutex> lock(g_currencyTypeEventsMutex);
    g_currencyTypeEvents.push_back(evt);
}

static bool IsDeathFunction(void* function)
{
    {
        std::lock_guard<std::mutex> lock(g_deathClassifyMutex);
        if (g_deathFunctionPtrs.count(function))
            return true;
        if (g_nonDeathFunctionPtrs.count(function))
            return false;
    }

    std::string name;
    bool resolved = TryGetObjectName(function, name);
    bool isExactMatch = resolved && name == "OnCharacterDeath";

    if (resolved && !isExactMatch && ToLowerCopy(name).find("death") != std::string::npos)
    {
        std::lock_guard<std::mutex> lock(g_deathClassifyMutex);
        if (!g_loggedDeathLikeFunctionPtrs.count(function))
        {
            g_loggedDeathLikeFunctionPtrs.insert(function);
            LogLine("Death-like (but not exact 'OnCharacterDeath') function seen: " + name);
        }
    }

    std::lock_guard<std::mutex> lock(g_deathClassifyMutex);
    if (isExactMatch)
    {
        g_deathFunctionPtrs.insert(function);
        LogLine("Classified exact OnCharacterDeath function pointer.");
    }
    else
    {
        if (ShouldCacheNegative(function, resolved))
            g_nonDeathFunctionPtrs.insert(function);
    }
    return isExactMatch;
}

static void CaptureDeathEvent(void* self)
{
    std::lock_guard<std::mutex> lock(g_deathEventsMutex);
    g_deathEvents.push_back({ reinterpret_cast<uint64_t>(self) });
}

// ---- Mission-outcome trigger detection. Same classify-and-cache pattern
// as IsDeathFunction/IsSecretFoundFunction above, but this is NOT a
// source of truth by itself - it's just a wake-up signal telling the
// Python side "a mission run just concluded somehow, go make one
// authoritative IsMissionCompleted() call right now" instead of polling
// that call every tick forever. Classifies on ANY of three exact
// reflected names, since it isn't confirmed which one(s) actually fire
// via ProcessEvent in this build:
//   - MulticastMissionFinished / MulticastGameOver: both flagged
//     (Native, Event, NetMulticast) in the SDK dump - RPC dispatch is
//     structurally guaranteed to route through ProcessEvent, same reason
//     OnCharacterDeath works.
//   - OnShowMissionVictory: flagged (Event, BlueprintEvent) - same
//     ProcessEvent guarantee via a different mechanism (blueprint-event
//     thunk instead of RPC dispatch).
// All three are OR'd together rather than picked in advance, because
// OnOpenLootChest looked exactly this solid on paper too and never
// fired. Whichever name(s) actually show up in dungeons_bridge_debug.log
// is the real answer; the other two cost nothing to leave classified.
//
// Deliberately does NOT try to guess win/loss from the trigger name or
// parse any of its parameters (FMissionFinishedSummary, ELevelNames,
// etc.) - a totem-loss failure may fire the exact same trigger as a real
// win, and guessing wrong here is exactly how a false positive gets
// reported to the AP server. The trigger only ever causes ONE
// IsMissionCompleted() confirm call on the Python side; that call is the
// only thing allowed to decide "completed or not."
struct MissionOutcomeEvent
{
    uint64_t actorAddr;
    std::string triggerName;   // which of the three fired - debug log only
};

static std::mutex g_missionOutcomeEventsMutex;
static std::vector<MissionOutcomeEvent> g_missionOutcomeEvents;   // drained by the pipe thread via get_mission_outcome_events

static std::mutex g_missionOutcomeClassifyMutex;
static std::unordered_set<void*> g_missionOutcomeFunctionPtrs;
static std::unordered_set<void*> g_nonMissionOutcomeFunctionPtrs;
static std::unordered_set<void*> g_loggedMissionOutcomeLikeFunctionPtrs;

static bool IsMissionOutcomeFunction(void* function, std::string& outMatchedName)
{
    {
        std::lock_guard<std::mutex> lock(g_missionOutcomeClassifyMutex);
        if (g_missionOutcomeFunctionPtrs.count(function))
        {
            TryGetObjectName(function, outMatchedName);  // cheap - just for the event payload
            return true;
        }
        if (g_nonMissionOutcomeFunctionPtrs.count(function))
            return false;
    }

    std::string name;
    bool resolved = TryGetObjectName(function, name);
    bool isExactMatch = resolved && (name == "MulticastMissionFinished" ||
                                      name == "OnShowMissionVictory" ||
                                      name == "MulticastGameOver");

    if (resolved && !isExactMatch)
    {
        std::string lower = ToLowerCopy(name);
        bool missionish = lower.find("mission") != std::string::npos &&
            (lower.find("finish") != std::string::npos || lower.find("complet") != std::string::npos ||
             lower.find("victory") != std::string::npos || lower.find("gameover") != std::string::npos);
        if (missionish)
        {
            std::lock_guard<std::mutex> lock(g_missionOutcomeClassifyMutex);
            if (!g_loggedMissionOutcomeLikeFunctionPtrs.count(function))
            {
                g_loggedMissionOutcomeLikeFunctionPtrs.insert(function);
                LogLine("Mission-outcome-like (but not one of the three exact names) function seen: " + name);
            }
        }
    }

    std::lock_guard<std::mutex> lock(g_missionOutcomeClassifyMutex);
    if (isExactMatch)
    {
        g_missionOutcomeFunctionPtrs.insert(function);
        LogLine("Classified exact mission-outcome trigger function pointer: " + name);
        outMatchedName = name;
    }
    else
    {
        if (ShouldCacheNegative(function, resolved))
            g_nonMissionOutcomeFunctionPtrs.insert(function);
    }
    return isExactMatch;
}

static void CaptureMissionOutcomeEvent(void* self, const std::string& triggerName)
{
    std::lock_guard<std::mutex> lock(g_missionOutcomeEventsMutex);
    g_missionOutcomeEvents.push_back({ reinterpret_cast<uint64_t>(self), triggerName });
}

// hkProcessEvent itself is now a thin __try/__except wrapper (below) -
// all the actual classification/detection logic that used to live directly
// in hkProcessEvent was moved, unchanged, into this _Impl function. This is
// purely a crash-containment measure: MSVC's __try/__except can't wrap a
// scope that also does C++ object unwinding/destructors directly (same
// restriction noted elsewhere in this file, e.g. around TryProcessEventCall),
// so the real logic needed to move into its own function callable from
// inside a plain __try block, rather than annotating hkProcessEvent in place.
// blockedOut is an out-param since __except's filter can't see local
// variables declared inside the __try'd call in the caller's scope.
void hkProcessEvent_Impl(void* self, void* function, void* params, bool* blockedOut)
{
    // Services queued CALL/CALLDATA requests (see HandleCall/HandleCallData)
    // here instead of from a Present hook. Present hooking required creating
    // a second, throwaway D3D11 device via D3D11CreateDeviceAndSwapChain at
    // startup just to read Present's address off its vtable - concurrent
    // with the game's own real device/swapchain creation. That's the
    // strongest suspect for a crash seen consistently at game startup
    // ("during connection, on construction", 3 identical crash dumps, zero
    // dungeons_bridge frames on any of them - consistent with a GPU-driver-
    // level fault, not our own code). ProcessEvent is already confirmed
    // firing reliably and immediately once the game is running, whether or
    // not a real Present address was ever found, so it's a strictly safer
    // place to drain from - see SetupHookThread's own comment for why the
    // Present hook attempt itself was removed entirely, not just made
    // optional.
    FlushSilenceLog();   // deferred silence-transition log lines (active hook call = safe point)
    DrainPendingCalls();
    DrainGenericWork();

    uint64_t callNum = g_processEventCallCount.fetch_add(1, std::memory_order_relaxed);
    if (callNum == 0)
        LogLine("hkProcessEvent: first call received - hook is definitely firing.");

    bool blocked = false;

    // Blocking check: broad match (whole Interact family, not just
    // "OnInteracted") - the actual pickup effect can apply during the
    // EARLIER "OnInteract" call, so only blocking "OnInteracted" let it
    // through too late to matter. See IsInteractFamilyFunction's comment.
    if (IsInteractFamilyFunction(function))
    {
        std::string className;
        if (TryGetClassName(self, className))
        {
            int tier = ClassifyPickupTier(className);
            int unlocked = g_pickupUnlockTier.load(std::memory_order_relaxed);
            if (tier > 0 && tier > unlocked)
            {
                blocked = true;
                LogLine("BLOCKED pickup (tier " + std::to_string(tier) + " > unlocked " +
                         std::to_string(unlocked) + "): " + className);
            }
        }
    }

    // Per-actor dedupe for Supply Station reporting - see the comment at the
    // call site below for why this exists even though supply uses the same
    // trigger filter as regular chests.
    // (g_reportedSupplyActors / its mutex now live at file scope so a world
    // change can clear them - see ClearSupplyDedupe.)

    // Chest-open event reporting: both regular chests AND Supply Stations
    // use IsChestReportFunction - "OnInteract" present, "OnInteracted"
    // absent. OnInteract fires exactly once per interaction attempt;
    // OnInteracted fires once per spawned item, which would over-count if
    // used for reporting.
    //
    // g_reportedSupplyActors (dedup by actor address) is a defensive
    // safety net for Supply specifically - a Supply Station is destroyed
    // on open rather than flipping a bOpened-style flag the way
    // AChestActor gives regular chests, so there's no other state
    // available to double-check against if OnInteract were ever to fire
    // more than once for the same station. Regular chests don't need it.
    // Clicky/ClickyComponent-family bound events (confirmed via
    // dungeons_bridge_debug.log to be what Supply Station's interact
    // button routes through - see the log's "Classified new Interact-
    // family function" lines, which only ever show Clicky/ClickyComponent
    // as broad Interact-family, never as the narrow chest-reporting
    // match) apparently never produce a bare "OnInteract" ProcessEvent
    // call the way InteractableComp/ReplicatedInteractable do - only
    // "OnInteracted" ever fires for them. IsChestReportFunction's narrow
    // match can therefore never see them at all, meaning a Supply
    // Station's open was never reaching CaptureInteractEvent - not
    // misclassified, just structurally unreachable via that path. Falling
    // back to the broader Interact-family match for this, but ONLY when
    // the actor's own class name contains "supply" (never for arbitrary
    // Interact-family hits - that would badly over-report on doors, NPCs,
    // popups, etc., all of which are also Interact-family), and ONLY when
    // the narrow match didn't already claim it. The existing per-actor
    // dedup (originally added as a defensive safety net) is what makes
    // this safe to widen: OnInteracted can fire more than once per open,
    // but each actor address can only ever report once.
    bool isNarrowChestReport = IsChestReportFunction(function);
    bool isSupplyFallback = !isNarrowChestReport && IsInteractFamilyFunction(function);
    if (!blocked && (isNarrowChestReport || isSupplyFallback))
    {
        std::string className;
        if (TryGetClassName(self, className))
        {
            if (ToLowerCopy(className).find("supply") != std::string::npos)
            {
                uint64_t addr = reinterpret_cast<uint64_t>(self);
                bool alreadyReported;
                {
                    std::lock_guard<std::mutex> lock(g_reportedSupplyActorsMutex);
                    alreadyReported = !ReportedSupplyActors().insert(addr).second;
                }
                if (!alreadyReported)
                    CaptureInteractEvent(addr, className);
            }
            else if (isNarrowChestReport)
            {
                CaptureInteractEvent(reinterpret_cast<uint64_t>(self), className);
            }
        }
    }

    if (IsDeathFunction(function))
        CaptureDeathEvent(self);

    if (IsSecretFoundFunction(function))
        CaptureSecretFoundEvent(self);

    // Currency widget cross-check (see the block above g_currencyPtr's
    // section for why this exists). Gated on the INSTANCE's class name
    // containing "counter" - the function name alone ("OnValueChanged")
    // is too generic to trust by itself game-wide.
    if (IsCurrencyValueChangedFunction(function) || IsCurrencyTypeChangedFunction(function))
    {
        std::string className;
        if (TryGetClassName(self, className) &&
            ToLowerCopy(className).find("counter") != std::string::npos)
        {
            uint64_t widgetAddr = reinterpret_cast<uint64_t>(self);
            if (IsCurrencyValueChangedFunction(function))
                CaptureCurrencyValueEvent(widgetAddr, params);
            else
                CaptureCurrencyTypeEvent(widgetAddr, params);
        }
    }

    {
        std::string matchedTriggerName;
        if (IsMissionOutcomeFunction(function, matchedTriggerName))
            CaptureMissionOutcomeEvent(self, matchedTriggerName);
    }

    if (!blocked)
        *blockedOut = false;
    else
        *blockedOut = true;
    // Deliberately does NOT call oProcessEvent itself - see hkProcessEvent
    // below, which calls it exactly once, unconditionally, OUTSIDE this
    // function's __try wrapper. Keeping the real engine call out of the
    // SEH-guarded region is intentional: if an exception is ever caught
    // below, we must be certain oProcessEvent hasn't already run (and so
    // is still safe/necessary to call) rather than risk calling the
    // original engine function twice for the same event.
}

// Theoretical/preventive hardening, added after a beta tester's crash dump
// showed the fault happening directly inside this DLL's own frames (not the
// game's) - most likely our classification code (TryGetClassName, the various
// IsXFunction matchers, resolve_fname-style GObjects/FName walks) touching an
// object that's still mid-construction, whose vtable/class pointer isn't
// valid yet. Not confirmed against a repro - we don't have the exact source
// that produced the crashing .dll, so this can't be verified against that
// specific build. What this DOES guarantee: an access violation raised
// anywhere inside classification/detection no longer takes the whole game
// down - it's caught, logged once, and oProcessEvent (the actual engine
// call the player's action depends on) still runs exactly once regardless,
// unmodified, exactly as if detection had been skipped entirely for that call.
static std::atomic<uint64_t> g_hkProcessEventExceptionCount{0};

// Pulled out of hkProcessEvent on purpose: it builds std::string temporaries
// (std::to_string, concatenation), and a function containing a __try/__except
// can't also contain any C++ object requiring destructor/unwind handling
// under /EHsc (MSVC C2712) - including temporaries created inside the
// __except block itself, not just named locals. Keeping hkProcessEvent's own
// body free of any such object is what lets it use __try at all.
void LogProcessEventException()
{
    uint64_t n = g_hkProcessEventExceptionCount.fetch_add(1, std::memory_order_relaxed);
    if (n < 20)  // cap - a fast, repeating crash-and-catch loop shouldn't spam the log file unbounded
        LogLine("hkProcessEvent: caught an exception in classification/detection code "
                 "(#" + std::to_string(n + 1) + ") - object was likely mid-construction. "
                 "Treating this call as not-blocked and calling the original function unmodified.");
}

void hkProcessEvent(void* self, void* function, void* params)
{
    // Silent window (startup / level transition): pure pass-through, nothing
    // else of ours runs - one relaxed atomic load, then the original call.
    // VoidControllerThread owns the state machine and re-arms; as a safety
    // net only, one call in 1024 also evaluates it here, so a dead controller
    // can never leave the hook silent forever.
    if (g_hookSilent.load(std::memory_order_relaxed) &&
        (g_voidCalls.fetch_add(1, std::memory_order_relaxed) & 0x3FF) != 0)
    {
        oProcessEvent(self, function, params);
        return;
    }
    if (EvaluateSilence())
    {
        oProcessEvent(self, function, params);
        return;
    }

    bool blocked = false;
    __try
    {
        hkProcessEvent_Impl(self, function, params, &blocked);
    }
    __except (EXCEPTION_EXECUTE_HANDLER)
    {
        LogProcessEventException();
        blocked = false;  // classification never completed, so there's nothing
                           // to justify blocking this call - fail open, not closed.
    }
    // Called exactly once, always, regardless of whether classification
    // above succeeded or was caught - see the comment on hkProcessEvent_Impl
    // for why the real engine call had to move out here.
    if (!blocked)
        oProcessEvent(self, function, params);
}

// ---- Currency pointer capture, via the exact technique confirmed working
// in a real Cheat Engine table (Dungeons_Master_Table_v3_70.CT) ----
// The real Emeralds/Gold/Eyes of Ender getter is a tiny NATIVE (non-
// reflected) function: `mov eax,[rcx+8]; ret` - explaining why every
// GObjects/reflection-based technique tried before this failed to find
// it; it was never a UFUNCTION at all. The CT hooks this exact function
// and captures its "this" pointer (rcx) the first time anything calls
// it, into a symbol it calls p_currency. Confirmed offsets from that CT
// table cross-checked against real in-game HUD values (see
// dungeons_reader.py's CURRENCY_OFFSETS block for the full story):
//   Emeralds      = p_currency + 0x08
//   Eyes of Ender = p_currency + 0x14
//   Gold          = p_currency + 0x20  (still UNCONFIRMED - no nonzero
//                                        Gold reading tested against yet)
//
// We do the same via MinHook instead of a hand patch - safer, since
// MinHook handles instruction relocation properly. The AOB pattern
// (F8 8B 41 08 C3 CC) is scanned for in the main module; we hook at
// pattern+1 (the actual `mov eax,[rcx+8]; ret` body). Because x64's
// calling convention always passes the first pointer arg in RCX and
// returns int in EAX, a normal C function with this signature is ABI-
// compatible as a drop-in replacement - no hand-assembled bytes needed.

// ---- hkCurrencyGetter / g_currencyPtr: PERMANENTLY REMOVED ----
// AVOID this hook (it was "branch 0" in the branch-by-branch crash
// isolation test) - it is the CONFIRMED cause of the recurring
// Dungeons.exe crash (PCallStackHash A3D4B2C4B9A67CD3F78D1BBF39B8F686BCA73B30,
// ~70s into a session, 100% engine frames, zero dungeons_bridge frames).
// Isolation test results:
//   branch 0 (currency hook ALONE)                 -> CRASH
//   branch 7 (nothing hooked at all)                -> no crash
//   branch 1 (ProcessEvent hooked, no classification) -> no crash
//   branch 8 (everything EXCEPT the currency hook)   -> no crash
// Branch 0 crashing alone + its exact inverse not crashing = necessary and
// sufficient. Do NOT re-add this hook or its AOB pattern scan.
//
// Nothing in the live client needs it: apply_emerald_reward grants via the
// real UWalletComponent::ClientAdd UFunction and read_current_emeralds is a
// plain memory read - neither ever touched g_currencyPtr. Only dev-only CLI
// tooling in dungeons_reader.py (scan_currency/dump_currency and friends)
// used get_currency_ptr; it now returns a "REMOVED:..." sentinel below.
// For currency tracking use get_currency_value_events /
// get_currency_type_events (the currency-widget ProcessEvent cross-check).

// Simple AOB scan across the main module's mapped image (reads size from
// its own PE header - no extra library dependency needed). Returns EVERY
// match, not just the first - see the currency hook install site below
// for why this matters: a hook built on FindPattern's old first-match-
// only behavior turned out to be silently hooking the wrong instance of
// a too-generic 6-byte pattern, and there was no way to tell without
// this.
// Isolated POD-only helper - no C++ objects with destructors in scope,
// same MSVC restriction as TryProcessEventCall above (C2712: __try can't
// coexist with a function that also has an object requiring unwinding -
// FindAllPatterns/FindPattern's own std::vector<uintptr_t>/local state
// triggered this the first time these two were written directly with
// __try inline). Pure pointer/size arguments only, so nothing here ever
// needs unwinding - safe to __try around.
static bool TryMatchPatternAt(const uint8_t* data, size_t offset, const uint8_t* pattern, size_t patternLen)
{
    __try
    {
        for (size_t j = 0; j < patternLen; ++j)
        {
            if (data[offset + j] != pattern[j])
                return false;
        }
        return true;
    }
    __except (EXCEPTION_EXECUTE_HANDLER)
    {
        return false; // unmapped/guard page mid-scan - treat as no match
    }
}

static std::vector<uintptr_t> FindAllPatterns(const uint8_t* pattern, size_t patternLen, size_t maxMatches = 64)
{
    std::vector<uintptr_t> matches;
    uintptr_t base = GetImageBase();
    auto dosHeader = reinterpret_cast<PIMAGE_DOS_HEADER>(base);
    if (dosHeader->e_magic != IMAGE_DOS_SIGNATURE)
        return matches;

    auto ntHeaders = reinterpret_cast<PIMAGE_NT_HEADERS>(base + dosHeader->e_lfanew);
    if (ntHeaders->Signature != IMAGE_NT_SIGNATURE)
        return matches;

    size_t imageSize = ntHeaders->OptionalHeader.SizeOfImage;
    const uint8_t* data = reinterpret_cast<const uint8_t*>(base);

    for (size_t i = 0; i + patternLen <= imageSize && matches.size() < maxMatches; ++i)
    {
        if (TryMatchPatternAt(data, i, pattern, patternLen))
            matches.push_back(base + i);
    }
    return matches;
}

// Simple AOB scan across the main module's mapped image (reads size from
// its own PE header - no extra library dependency needed).
static uintptr_t FindPattern(const uint8_t* pattern, size_t patternLen)
{
    uintptr_t base = GetImageBase();
    auto dosHeader = reinterpret_cast<PIMAGE_DOS_HEADER>(base);
    if (dosHeader->e_magic != IMAGE_DOS_SIGNATURE)
        return 0;

    auto ntHeaders = reinterpret_cast<PIMAGE_NT_HEADERS>(base + dosHeader->e_lfanew);
    if (ntHeaders->Signature != IMAGE_NT_SIGNATURE)
        return 0;

    size_t imageSize = ntHeaders->OptionalHeader.SizeOfImage;
    const uint8_t* data = reinterpret_cast<const uint8_t*>(base);

    for (size_t i = 0; i + patternLen <= imageSize; ++i)
    {
        if (TryMatchPatternAt(data, i, pattern, patternLen))
            return base + i;
    }
    return 0;
}


std::string HandleRequest(const std::string& request)
{
    if (request == "get_currency_ptr")
    {
        // hkCurrencyGetter/g_currencyPtr permanently removed - confirmed
        // crash cause (see the AVOID note where hkCurrencyGetter used to be
        // defined). A clear sentinel instead of a stale/null "PTR:0" so any
        // Python-side caller fails loudly instead of reading garbage.
        return "REMOVED:native currency getter hook permanently removed - confirmed "
               "crash cause; use get_currency_value_events/get_currency_type_events instead";
    }

    if (request == "get_currency_value_events")
    {
        std::vector<CurrencyValueEvent> drained;
        {
            std::lock_guard<std::mutex> lock(g_currencyValueEventsMutex);
            std::swap(drained, g_currencyValueEvents);
        }
        std::ostringstream out;
        out << "EVENTS:" << std::dec << drained.size();
        for (const auto& e : drained)
        {
            out << "|" << std::hex << e.widgetAddr << std::dec
                << "," << e.newValue << "," << e.previousValue;
        }
        return out.str();
    }

    if (request == "get_currency_type_events")
    {
        std::vector<CurrencyTypeEvent> drained;
        {
            std::lock_guard<std::mutex> lock(g_currencyTypeEventsMutex);
            std::swap(drained, g_currencyTypeEvents);
        }
        std::ostringstream out;
        out << "EVENTS:" << std::dec << drained.size();
        for (const auto& e : drained)
        {
            out << "|" << std::hex << e.widgetAddr << std::dec
                << "," << e.serializedIdIndex;
        }
        return out.str();
    }

    if (request == "get_chest_events")
    {
        std::vector<InteractEvent> drained;
        {
            std::lock_guard<std::mutex> lock(g_interactEventsMutex);
            std::swap(drained, g_interactEvents);
        }
        std::ostringstream out;
        out << "EVENTS:" << std::dec << drained.size();
        for (const auto& e : drained)
        {
            // className can't contain '|' or ',' (UE identifiers are
            // alphanumeric/underscore only), so simple delimiting is safe.
            out << "|" << std::hex << e.actorAddr << std::dec << "," << e.className
                << "," << std::hex << e.worldPtr << std::dec;
        }
        return out.str();
    }

    if (request == "get_death_events")
    {
        std::vector<CharacterDeathEvent> drained;
        {
            std::lock_guard<std::mutex> lock(g_deathEventsMutex);
            std::swap(drained, g_deathEvents);
        }
        std::ostringstream out;
        out << "EVENTS:" << std::dec << drained.size();
        for (const auto& e : drained)
        {
            out << "|" << std::hex << e.actorAddr << std::dec;
        }
        return out.str();
    }

    if (request == "get_secret_events")
    {
        std::vector<SecretFoundEvent> drained;
        {
            std::lock_guard<std::mutex> lock(g_secretEventsMutex);
            std::swap(drained, g_secretEvents);
        }
        std::ostringstream out;
        out << "EVENTS:" << std::dec << drained.size();
        for (const auto& e : drained)
        {
            out << "|" << std::hex << e.actorAddr << std::dec << "," << e.className;
        }
        return out.str();
    }

    if (request == "get_mission_outcome_events")
    {
        std::vector<MissionOutcomeEvent> drained;
        {
            std::lock_guard<std::mutex> lock(g_missionOutcomeEventsMutex);
            std::swap(drained, g_missionOutcomeEvents);
        }
        std::ostringstream out;
        out << "EVENTS:" << std::dec << drained.size();
        for (const auto& e : drained)
        {
            out << "|" << std::hex << e.actorAddr << std::dec << "," << e.triggerName;
        }
        return out.str();
    }

    if (request.rfind("set_pickup_tier ", 0) == 0)
    {
        std::string body = request.substr(std::string("set_pickup_tier ").size());
        try
        {
            int tier = std::stoi(body);
            g_pickupUnlockTier.store(tier, std::memory_order_relaxed);
            LogLine("set_pickup_tier: now at tier " + std::to_string(tier));
            return "OK";
        }
        catch (...)
        {
            return "ERROR: invalid tier value";
        }
    }

    if (request == "get_pickup_tier")
    {
        std::ostringstream out;
        out << "TIER:" << g_pickupUnlockTier.load(std::memory_order_relaxed);
        return out.str();
    }

    // ---- hook silence control (all optional - the hook re-arms by itself) ----
    if (request == "hook_state")
    {
        EvaluateSilence();
        FlushSilenceLog();
        std::ostringstream out;
        const bool reportSilent = g_hookSilent.load() || !g_hookInstalled.load();
        out << "HOOK:" << (reportSilent ? "silent" : "active")
            << ",silent_ms=" << (g_hookSilent.load() ? (GetTickCount64() - g_silentSince.load()) : 0)
            << ",world=0x" << std::hex << g_lastSeenWorld.load() << std::dec
            << ",void=" << (g_voidMode.load() == VOID_HARD ? "hard" : "soft")
            << ",patched=" << (g_hookPatched.load() ? 1 : 0);
        return out.str();
    }
    if (request.rfind("set_void_mode ", 0) == 0)
    {
        std::string m = request.substr(std::string("set_void_mode ").size());
        while (!m.empty() && (m.back() == '\n' || m.back() == '\r' || m.back() == ' ')) m.pop_back();
        if (m == "soft")      g_voidMode.store(VOID_SOFT);
        else if (m == "hard") g_voidMode.store(VOID_HARD);
        else                  return "ERROR: void mode must be soft or hard";
        LogLine("VOID MODE (live override): " + m);
        return "OK";
    }
    if (request.rfind("set_crash_guard ", 0) == 0)
    {
        std::string m = request.substr(std::string("set_crash_guard ").size());
        while (!m.empty() && (m.back() == '\n' || m.back() == '\r' || m.back() == ' ')) m.pop_back();
        if (m == "on")       g_guardEnabled.store(true);
        else if (m == "off") g_guardEnabled.store(false);
        else                 return "ERROR: crash guard must be on or off";
        LogLine("CRASH GUARD (live override): " + m);
        return "OK";
    }
    if (request == "get_crash_guard")
    {
        std::ostringstream out;
        out << "GUARD:enabled=" << (g_guardEnabled.load() ? 1 : 0)
            << ",sites=" << g_guardSiteCount.load() << ",hits=" << g_guardHits.load();
        return out.str();
    }
    if (request == "get_void_mode")
    {
        std::ostringstream out;
        out << "VOID:" << (g_voidMode.load() == VOID_HARD ? "hard" : "soft")
            << ",patched=" << (g_hookPatched.load() ? 1 : 0)
            << ",silent=" << (g_hookSilent.load() ? 1 : 0);
        return out.str();
    }
    if (request == "hook_wake")
    {
        g_forceSilentUntil.store(0);
        g_hookSilent.store(false);
        g_nullWorldGaveUp.store(true);
        LogLine("HOOK ACTIVE: hook_wake command");
        FlushSilenceLog();
        return "OK";
    }
    if (request.rfind("hook_silence ", 0) == 0)
    {
        try
        {
            ULONGLONG ms = std::stoull(request.substr(13));
            if (ms > SILENCE_FORCE_MAX) ms = SILENCE_FORCE_MAX;
            g_forceSilentUntil.store(GetTickCount64() + ms);
            return "OK";
        }
        catch (...) { return "ERROR: invalid hook_silence value"; }
    }

    // While silent, anything that would touch the game (queued CALLs, name
    // resolution, GObjects walks) is refused immediately with a normal
    // ERROR: string - callers already treat those as "retry next tick".
    const bool touchesGame =
        request.rfind("CALL ", 0) == 0 || request.rfind("CALLDATA ", 0) == 0 ||
        request.rfind("resolve_name:", 0) == 0 || request.rfind("find_by_name:", 0) == 0 ||
        request.rfind("find_outer:", 0) == 0;
    if (touchesGame && EvaluateSilence())
        return "ERROR:hook silent (game loading/transition) - retry shortly";

    if (request.rfind("CALL ", 0) == 0)
        return HandleCall(request.substr(5));

    if (request.rfind("CALLDATA ", 0) == 0)
        return HandleCallData(request.substr(9));

    const std::string resolvePrefix = "resolve_name:";
    if (request.rfind(resolvePrefix, 0) == 0)
        return HandleResolveName(request.substr(resolvePrefix.size()));

    const std::string findByNamePrefix = "find_by_name:";
    if (request.rfind(findByNamePrefix, 0) == 0)
        return HandleFindByName(request.substr(findByNamePrefix.size()));

    const std::string findOuterPrefix = "find_outer:";
    if (request.rfind(findOuterPrefix, 0) == 0)
    {
        std::string hexAddr = request.substr(findOuterPrefix.size());
        uintptr_t target = 0;
        try
        {
            target = static_cast<uintptr_t>(std::stoull(hexAddr, nullptr, 16));
        }
        catch (...)
        {
            return "ERROR:could not parse address";
        }
        return FindObjectsWithOuter(target);
    }

    return "DLL received: " + request;
}

void PipeServerThread()
{
    while (true)
    {
        HANDLE pipe = CreateNamedPipeW(
            GetPipeName(),
            PIPE_ACCESS_DUPLEX,
            PIPE_TYPE_MESSAGE | PIPE_READMODE_MESSAGE | PIPE_WAIT,
            1,
            16384,
            16384,
            0,
            nullptr
        );

        if (pipe == INVALID_HANDLE_VALUE)
        {
            Sleep(1000);
            continue;
        }

        BOOL connected = ConnectNamedPipe(pipe, nullptr) ? TRUE : (GetLastError() == ERROR_PIPE_CONNECTED);

        if (connected)
        {
            char buffer[16384];
            DWORD bytesRead = 0;

            while (ReadFile(pipe, buffer, sizeof(buffer) - 1, &bytesRead, nullptr) && bytesRead > 0)
            {
                buffer[bytesRead] = '\0';
                std::string request(buffer);

                std::string response = HandleRequest(request);

                DWORD bytesWritten = 0;
                WriteFile(pipe, response.c_str(), (DWORD)response.size(), &bytesWritten, nullptr);
            }
        }

        DisconnectNamedPipe(pipe);
        CloseHandle(pipe);
    }
}

// (Present hook removed entirely - see the comment above SetupHookThread.)

// ---- Present hook: REMOVED ----
// This used to create a throwaway D3D11 device+swapchain via
// D3D11CreateDeviceAndSwapChain purely to read Present's address off its
// vtable, concurrently with the game's own real device/swapchain creation
// at startup (a Sleep(3000) in SetupHookThread tried to reduce the overlap,
// but couldn't guarantee it never happens). This is the strongest suspect
// for a crash a beta tester hit consistently at game startup ("during
// connection, on construction") - 3 crash dumps with byte-identical
// offsets, all purely inside Dungeons-Win64-Shipping/driver code with zero
// dungeons_bridge frames, and the tester confirmed it never happens with
// the mod NOT injected. A second D3D11 device fighting the game's own for
// the same GPU/driver resources during its init window is exactly the kind
// of thing that can fault at the driver level with no attributable stack.
//
// The only thing Present was actually used for was draining g_pendingCalls
// (the CALL/CALLDATA queue) once per frame - see DrainPendingCalls' comment,
// now called from hkProcessEvent_Impl instead, which needs no throwaway
// device and was already confirmed firing reliably (including in sessions
// where GetPresentAddress used to fail anyway, e.g. "Present address not
// found" in this tester's own log - Present was never reliably available
// to begin with, on top of being a suspected crash cause).

// Breadcrumb thread (compile out with -DBRIDGE_WORLD_WATCH=0): READS GWorld and
// the silence flag every 250 ms and logs each change from ITS OWN thread, so a
// crash timeline exists without any file I/O on the game thread. It never calls
// EvaluateSilence() and never writes any shared state.
#ifndef BRIDGE_WORLD_WATCH
#define BRIDGE_WORLD_WATCH 1
#endif
#if BRIDGE_WORLD_WATCH
static void WorldWatchThread()
{
    uint64_t lastWorld = ~0ull;
    int lastSilent = -1;
    int lines = 0;
    for (;;)
    {
        const uint64_t w = GetCurrentWorldPtr();
        const int sil = g_hookSilent.load(std::memory_order_relaxed) ? 1 : 0;
        if ((w != lastWorld || sil != lastSilent) && lines < 400)
        {
            std::ostringstream o;
            o << "WATCH tick=" << GetTickCount64() << " GWorld=0x" << std::hex << w << std::dec
              << " hook=" << (sil ? "silent" : "active");
            LogLine(o.str());
            ++lines;
            lastWorld = w;
            lastSilent = sil;
        }
        Sleep(250);
    }
}
#endif

// ---- Crash guard: null GetX(this,0)->member read in the game's tick -------
// Two separate live crashes (Microsoft Store build, different sessions, ~15 min
// after the last level load, no transition and no void window in progress, no
// dungeons_bridge / ProcessEvent frame anywhere in the stack) died at the very
// same instruction with the very same 16-frame stack:
//
//     33 D2                xor  edx,edx
//     48 8B CB             mov  rcx,rbx
//     E8 xx xx xx xx       call <getter>            ; returns NULL here
//     48 8B 98 C8 03 00 00 mov  rbx,[rax+3C8h]      ; <- access violation, read of 0x3C8
//     48 85 DB             test rbx,rbx
//     74 30                jz   <null path>         ; the game ALREADY handles a null rbx
//
// i.e. the game null-checks the loaded member but not the pointer it comes
// from (looks like GetPlayerController(...,0) coming back NULL). Treating
// "rax == NULL" as "member == NULL" takes exactly the branch the game itself
// wrote for a null member: this handler sets rbx = 0 and steps over the load.
// It only ever acts on a read of address 0x3C8 with rax == 0 at a site that
// matched the byte pattern at start-up - anything else is passed on untouched,
// so it cannot hide any other kind of crash. No allocation, no locks, no file
// I/O inside the handler (hits are only counted; the controller thread logs).
//
// Note: this removes the symptom (the crash), not the cause (why no player
// controller exists at that moment - the log lines around each guard hit,
// written by VoidControllerThread, are there to find out).

// -1 = wildcard
static const int16_t kGuardPattern[] = {
    0x33, 0xD2, 0x48, 0x8B, 0xCB, 0xE8, -1, -1, -1, -1,
    0x48, 0x8B, 0x98, 0xC8, 0x03, 0x00, 0x00, 0x48, 0x85, 0xDB, 0x74 };
static const size_t kGuardPatternLen = sizeof(kGuardPattern) / sizeof(kGuardPattern[0]);
static const size_t kGuardFaultOffset = 10;   // "mov rbx,[rax+3C8h]" inside the pattern
static const size_t kGuardInstrLen = 7;       // its length
static const uintptr_t kGuardFaultDisp = 0x3C8;

// Pure: finds the pattern in [mem, mem+size); returns absolute addresses of the
// faulting instruction (memBase + offset) - separate so it can be unit-tested.
static size_t FindGuardSites(const uint8_t* mem, size_t size, uintptr_t memBase, uintptr_t* out, size_t maxOut)
{
    size_t n = 0;
    if (size < kGuardPatternLen)
        return 0;
    for (size_t i = 0; i + kGuardPatternLen <= size && n < maxOut; ++i)
    {
        if (mem[i] != 0x33)
            continue;
        bool ok = true;
        for (size_t j = 1; j < kGuardPatternLen; ++j)
        {
            const int16_t want = kGuardPattern[j];
            if (want >= 0 && mem[i + j] != static_cast<uint8_t>(want)) { ok = false; break; }
        }
        if (ok)
            out[n++] = memBase + i + kGuardFaultOffset;
    }
    return n;
}

// Pure decision, kept separate so it can be unit-tested.
static bool GuardShouldRecover(bool enabled, DWORD code, uintptr_t faultRip, uint64_t readWrite,
                               uint64_t faultAddr, uint64_t rax,
                               const uintptr_t* sites, int siteCount)
{
    if (!enabled) return false;
    if (code != EXCEPTION_ACCESS_VIOLATION) return false;
    if (readWrite != 0 || faultAddr != kGuardFaultDisp || rax != 0) return false;
    for (int i = 0; i < siteCount; ++i)
        if (sites[i] == faultRip) return true;
    return false;
}

static LONG CALLBACK CrashGuardVeh(PEXCEPTION_POINTERS info)
{
    const EXCEPTION_RECORD* er = info->ExceptionRecord;
    if (er->ExceptionCode != EXCEPTION_ACCESS_VIOLATION || er->NumberParameters < 2)
        return EXCEPTION_CONTINUE_SEARCH;

    uintptr_t sites[GUARD_MAX_SITES];
    const int count = g_guardSiteCount.load(std::memory_order_acquire);
    for (int i = 0; i < count && i < GUARD_MAX_SITES; ++i)
        sites[i] = g_guardSites[i].load(std::memory_order_relaxed);

    CONTEXT* c = info->ContextRecord;
    const uintptr_t rip = reinterpret_cast<uintptr_t>(er->ExceptionAddress);
    if (!GuardShouldRecover(g_guardEnabled.load(std::memory_order_relaxed), er->ExceptionCode, rip,
                            er->ExceptionInformation[0], er->ExceptionInformation[1], c->Rax,
                            sites, count))
        return EXCEPTION_CONTINUE_SEARCH;

    c->Rbx = 0;                 // "member is null" - the game's own jz handles it
    c->Rip += kGuardInstrLen;   // step over "mov rbx,[rax+3C8h]"
    g_guardLastRip.store(rip, std::memory_order_relaxed);
    g_guardHits.fetch_add(1, std::memory_order_relaxed);
    return EXCEPTION_CONTINUE_EXECUTION;
}

// Reading a code section can't normally fault, but never let a scan take the
// game down: POD-only helper so __try is allowed.
static size_t ScanRegionSafe(uintptr_t start, size_t size, uintptr_t* out, size_t maxOut)
{
    __try
    {
        return FindGuardSites(reinterpret_cast<const uint8_t*>(start), size, start, out, maxOut);
    }
    __except (EXCEPTION_EXECUTE_HANDLER)
    {
        return 0;
    }
}

static void InstallCrashGuard()
{
    char envBuf[16] = { 0 };
    DWORD envLen = GetEnvironmentVariableA("DUNGEONS_BRIDGE_CRASH_GUARD", envBuf, sizeof(envBuf));
    if (envLen > 0 && envLen < sizeof(envBuf) && std::string(envBuf) == "off")
    {
        g_guardEnabled.store(false);
        LogLine("CRASH GUARD: disabled by DUNGEONS_BRIDGE_CRASH_GUARD=off (not armed).");
        return;
    }

    const uintptr_t base = GetImageBase();
    g_guardBase.store(base);
    auto dos = reinterpret_cast<PIMAGE_DOS_HEADER>(base);
    if (!dos || dos->e_magic != IMAGE_DOS_SIGNATURE)
        return;
    auto nt = reinterpret_cast<PIMAGE_NT_HEADERS>(base + dos->e_lfanew);
    if (nt->Signature != IMAGE_NT_SIGNATURE)
        return;

    uintptr_t found[GUARD_MAX_SITES];
    int total = 0;
    PIMAGE_SECTION_HEADER sec = IMAGE_FIRST_SECTION(nt);
    for (WORD i = 0; i < nt->FileHeader.NumberOfSections && total < GUARD_MAX_SITES; ++i, ++sec)
    {
        if (!(sec->Characteristics & IMAGE_SCN_MEM_EXECUTE))
            continue;
        total += static_cast<int>(ScanRegionSafe(base + sec->VirtualAddress, sec->Misc.VirtualSize,
                                                 found + total, GUARD_MAX_SITES - total));
    }

    if (total == 0)
    {
        LogLine("CRASH GUARD: pattern not found in this build - not armed (nothing is patched or hidden).");
        return;
    }
    for (int i = 0; i < total; ++i)
    {
        g_guardSites[i].store(found[i], std::memory_order_relaxed);
        std::ostringstream o;
        o << "CRASH GUARD: armed at Dungeons image + 0x" << std::hex << (found[i] - base);
        LogLine(o.str());
    }
    g_guardSiteCount.store(total, std::memory_order_release);
    PVOID handle = AddVectoredExceptionHandler(1, CrashGuardVeh);
    LogLine(handle ? "CRASH GUARD: exception handler installed."
                   : "CRASH GUARD: AddVectoredExceptionHandler FAILED - not armed.");
}

static std::atomic<ULONGLONG> g_dllStartTick{ 0 };

// Owns the void state. Runs on its own thread, never on a game thread.
static void VoidControllerThread()
{
    LogLine("VoidController: started (mode=" + std::string(g_voidMode.load() == VOID_HARD ? "hard" : "soft") + ").");
    ULONGLONG nextHeartbeat = GetTickCount64() + 60000;
    uint32_t guardLogged = 0;
    for (;;)
    {
        Sleep(20);

        // One line a minute: how long the game has been up / in the current
        // level. Two crashes happened ~15 min after a level load - this shows
        // whether that is a pattern.
        {
            const ULONGLONG nowHb = GetTickCount64();
            if (nowHb >= nextHeartbeat)
            {
                nextHeartbeat = nowHb + 60000;
                std::ostringstream o;
                o << "HEARTBEAT: up=" << (nowHb - g_dllStartTick.load()) / 60000.0 << " min, in-level="
                  << (nowHb - g_lastWorldChange.load()) / 60000.0 << " min, silent=" << (g_hookSilent.load() ? 1 : 0)
                  << ", patched=" << (g_hookPatched.load() ? 1 : 0) << ", guardHits=" << g_guardHits.load()
                  << ", world=0x" << std::hex << g_lastSeenWorld.load();
                LogLine(o.str());
            }
        }

        // The crash guard only counts in the handler; report from here.
        {
            const uint32_t hits = g_guardHits.load();
            if (hits != guardLogged)
            {
                const ULONGLONG nowG = GetTickCount64();
                std::ostringstream o;
                o << "CRASH GUARD: recovered " << (hits - guardLogged) << " null-pointer read(s) at image + 0x"
                  << std::hex << (g_guardLastRip.load() - g_guardBase.load()) << std::dec
                  << " - game kept running. total=" << hits << ", in-level="
                  << (nowG - g_lastWorldChange.load()) / 60000.0 << " min, up="
                  << (nowG - g_dllStartTick.load()) / 60000.0 << " min, world=0x" << std::hex << g_lastSeenWorld.load();
                LogLine(o.str());
                guardLogged = hits;
            }
        }

        const bool silent = EvaluateSilence();   // drives settle / deadman / GWorld watch
        FlushSilenceLog();

        const VoidAction act = DecideVoidAction(silent, g_voidMode.load(), g_hookPatched.load());
        if (act == VOID_UNPATCH)
        {
            MH_STATUS st = MH_DisableHook(reinterpret_cast<void*>(g_peTarget.load()));
            if (st == MH_OK)
                g_hookPatched.store(false);
            LogLine("VOID HARD: ProcessEvent UNPATCHED for the silent window, MH_DisableHook status=" +
                    std::to_string(static_cast<int>(st)));
        }
        else if (act == VOID_REPATCH)
        {
            MH_STATUS st = MH_EnableHook(reinterpret_cast<void*>(g_peTarget.load()));
            if (st == MH_OK)
                g_hookPatched.store(true);
            LogLine("VOID: ProcessEvent RE-PATCHED, MH_EnableHook status=" +
                    std::to_string(static_cast<int>(st)));
        }
    }
}

// The inline hook is only PATCHED into the game once the engine has finished
// booting, i.e. once its first UWorld exists and has stayed the same for this
// long. Evidence (Microsoft Store build, client started BEFORE the game): with
// the hook enabled ~3 s after an early injection the game died 10-16 s after
// process start, inside FEngineLoop init, with no dungeons_bridge / ProcessEvent
// frame in the crash stack and GWorld still 0 the whole time - and that also
// happened with the "launch-parity" state machine, so the trigger is the
// patching of the engine's code during its own init, not the silence logic.
// Injecting the DLL early is fine (client A/B test); pipe server, pickup tier
// and hook_state (= "silent") are all available while we wait.
static const ULONGLONG HOOK_INSTALL_WORLD_STABLE_MS = 6000;
// Fail-open cap: if GWorld never becomes valid (e.g. a stale offset after a
// game update) the hook is installed anyway, exactly as before this guard.
static const ULONGLONG HOOK_INSTALL_MAX_WAIT_MS = 180000;

static void WaitForEngineBoot()
{
    if (!Offsets::GWorld)
        return;   // no GWorld offset for this host: nothing to wait on

    const ULONGLONG waitStart = GetTickCount64();
    ULONGLONG nonNullSince = 0;
    uint64_t lastWorld = 0;
    ULONGLONG lastLog = 0;

    for (;;)
    {
        const ULONGLONG now = GetTickCount64();
        const uint64_t world = GetCurrentWorldPtr();

        if (world != 0)
        {
            if (nonNullSince == 0 || world != lastWorld)
                nonNullSince = now;
            if (now - nonNullSince >= HOOK_INSTALL_WORLD_STABLE_MS)
            {
                LogLine("Engine boot finished (GWorld stable) - installing the ProcessEvent hook now.");
                return;
            }
        }
        else
        {
            nonNullSince = 0;
        }
        lastWorld = world;

        if (now - waitStart >= HOOK_INSTALL_MAX_WAIT_MS)
        {
            LogLine("WARNING: GWorld never became valid within the wait cap - installing the hook anyway.");
            return;
        }

        if (now - lastLog >= 5000)
        {
            lastLog = now;
            std::ostringstream o;
            o << "WAIT engine boot: tick=" << now << " GWorld=0x" << std::hex << world
              << " (hook not installed yet, state=silent)";
            LogLine(o.str());
        }
        Sleep(250);
    }
}

void SetupHookThread()
{
    LogLine("=== SetupHookThread starting (new session) ===");
    LogLine("BUILD: silence + safety patch, launch-parity guard v1 + boot-wait v2 + void controller v3 (soft/hard void during transitions)");
    struct SetupDone { ~SetupDone() { g_hookInstalled.store(true); } } setupDone;   // every exit path, incl. early returns
    // Small delay so classification/hooking doesn't start before the game's
    // own early init has had a chance to settle.
    Sleep(3000);

    InitOffsetsForHost();
    if (!Offsets::ProcessEvent)
    {
        LogLine("SetupHookThread: no ProcessEvent RVA for this host - installing NO hooks (passive).");
        return;
    }

    // Breadcrumbs from here on (timeline of GWorld / silence during the wait).
#if BRIDGE_WORLD_WATCH
    std::thread(WorldWatchThread).detach();
#endif
    g_dllStartTick.store(GetTickCount64());
    WaitForEngineBoot();
    InstallCrashGuard();   // independent of the hook: works even if MinHook fails below

    MH_STATUS initStatus = MH_Initialize();
    LogLine("MH_Initialize status: " + std::to_string(static_cast<int>(initStatus)));
    if (initStatus != MH_OK)
        return;

    // AVOID: the native currency-getter hook (AOB pattern scan + MH_CreateHook
    // on hkCurrencyGetter) used to be installed right here. It has been
    // PERMANENTLY REMOVED - it is the confirmed crash cause (see the note
    // where hkCurrencyGetter used to be defined). Do not reinstall it.
    LogLine("Currency getter hook: permanently removed from this build (confirmed crash cause) - not installed.");

    // ProcessEvent hook for chest-open detection. Same RVA GetProcessEventFn()
    // already uses for the CALL/CALLDATA path - after this hook is installed,
    // that path naturally starts going through hkProcessEvent too (which
    // just forwards to oProcessEvent when it's not an OnOpenLootChest call),
    // so nothing about the existing CALL functionality changes.
    uintptr_t peAddr = GetImageBase() + Offsets::ProcessEvent;
    g_peTarget.store(peAddr);
    LogLine("ProcessEvent target address: 0x" + [&]{ std::ostringstream o; o << std::hex << peAddr; return o.str(); }());
    MH_STATUS peStatus = MH_CreateHook(reinterpret_cast<void*>(peAddr),
                  reinterpret_cast<void*>(&hkProcessEvent), reinterpret_cast<void**>(&oProcessEvent));
    LogLine("ProcessEvent hook MH_CreateHook status: " + std::to_string(static_cast<int>(peStatus)));

    // Start SILENT (flag only): the hook is enabled for good right below and
    // never toggled again; it wakes itself once the world has been stable.
    g_everActive.store(false);   // hook_state polls during the wait may have flipped it: the launch-parity guard applies from the real install
    InitSilenceAtInstall();
    LogLine("HOOK SILENT at install - will self-activate after the world is stable (or after the deadman cap).");

    MH_STATUS enableStatus = MH_EnableHook(MH_ALL_HOOKS);
    LogLine("MH_EnableHook(MH_ALL_HOOKS) status: " + std::to_string(static_cast<int>(enableStatus)));

    if (enableStatus == MH_OK && peStatus == MH_OK)
    {
        g_hookPatched.store(true);
        char modeBuf[16] = { 0 };
        DWORD n = GetEnvironmentVariableA("DUNGEONS_BRIDGE_VOID_MODE", modeBuf, sizeof(modeBuf));
        if (n > 0 && n < sizeof(modeBuf) && std::string(modeBuf) == "hard")
            g_voidMode.store(VOID_HARD);
        std::thread(VoidControllerThread).detach();
    }
}

BOOL APIENTRY DllMain(HMODULE hModule, DWORD reason, LPVOID)
{
    if (reason == DLL_PROCESS_ATTACH)
    {
        g_ownModule = hModule;
        DisableThreadLibraryCalls(hModule);
        std::thread(PipeServerThread).detach();
        std::thread(SetupHookThread).detach();
    }
    return TRUE;
}
