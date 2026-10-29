// dungeons_bridge.cpp: injected DLL, named-pipe server the Python client uses to call into the game.
// ProcessEvent must run on the game thread, so pipe requests are queued and run by the hooked ProcessEvent (MinHook).
// Build (x64 Native Tools prompt): cl /LD /EHsc dungeons_bridge.cpp /I <minhook>\include /link d3d11.lib dxgi.lib
//   <libMinHook.x64.lib> /MAP:dungeons_bridge.map /OUT:dungeons_bridge.dll   (MinHook: github.com/TsudaKageyu/minhook)
// Then run `python finish_build.py`: the .map lets /crash turn dungeons_bridge.dll+0x... into function names.

#include <windows.h>
#include <tlhelp32.h>
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
#include <cstdarg>
#include <cstring>

#include "MinHook.h"

#pragma comment(lib, "d3d11.lib")
#pragma comment(lib, "dxgi.lib")

// One pipe per game process (named after its PID): the pipe is created with nMaxInstances=1, so a shared name
// would make a second game process fail forever. Python computes the same name (dungeons_reader.py, _pipe_name_for).
static std::wstring g_pipeName;

static const wchar_t* GetPipeName()
{
    if (g_pipeName.empty())
    {
        g_pipeName = L"\\\\.\\pipe\\dungeons_bridge_" + std::to_wstring(GetCurrentProcessId());
    }
    return g_pipeName.c_str();
}

// RVAs of the engine globals/functions
// They differ between game builds (Steam vs Microsoft Store / Minecraft Launcher, see STORE_OFFSETS in
// dungeons_reader.py) and are filled in once by InitOffsetsForHost() from the host exe name.
// 0 = unknown for this build: every getter returns null and every caller handles it.
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

// Debug logging
// File-based because the pipe may not be connected yet. Written next to the DLL (via its module handle): an
// injected DLL's relative paths would resolve against the game's working directory.

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

// Build identity + breadcrumbs (crash reporting, early part)
// kBuildMarker is searched for in the DLL FILE by the tools (tools/finish_build.py, crash_diag.py) to tell which build
// a crash report or a .map file belongs to. kFeatures is what the client checks at attach time (get_build_info), so
// an old DLL next to a newer client is reported immediately instead of failing in a confusing way.
static const char kBuildMarker[] = "DBBUILD:" __DATE__ " " __TIME__;
static const char* const kBuildId = kBuildMarker + 8;
static const char kFeatures[] = "merchants,crashreport,owncall,crumbs,health";

// Breadcrumbs: a small lock-free ring of "what the DLL itself was doing" (pipe commands, our own game calls,
// merchant events, hook state changes...). Allocation-free so it can be written - and read by the crash reporter -
// from any thread, even inside an exception handler. Torn entries are possible and harmless.
struct Crumb { ULONGLONG tick; DWORD tid; char kind[12]; char text[76]; uint64_t a; uint64_t b; };
static const uint32_t CRUMB_SIZE = 256;   // power of two
static Crumb g_crumbs[CRUMB_SIZE];
static std::atomic<uint32_t> g_crumbHead{ 0 };

static void CrumbAdd(const char* kind, const char* text, uint64_t a = 0, uint64_t b = 0)
{
    Crumb& c = g_crumbs[g_crumbHead.fetch_add(1, std::memory_order_relaxed) & (CRUMB_SIZE - 1)];
    c.tick = GetTickCount64();
    c.tid = GetCurrentThreadId();
    strncpy_s(c.kind, sizeof(c.kind), kind ? kind : "", _TRUNCATE);
    strncpy_s(c.text, sizeof(c.text), text ? text : "", _TRUNCATE);
    c.a = a;
    c.b = b;
}

static std::atomic<DWORD> g_hookThreadId{ 0 };          // thread that runs hkProcessEvent (the game thread)
static std::atomic<uint64_t> g_curCallObj{ 0 };         // our own CALLDATA call in progress (0 = none)
static std::atomic<uint64_t> g_curCallFunc{ 0 };
static std::atomic<uint64_t> g_lastFaultAddr{ 0 };      // address of the last fault caught by one of OUR __try blocks
static void DescribeAddress(uint64_t addr, char* out, size_t n);   // "module+0xRVA", defined with the crash reporter

// Health watch (freezes, slow calls, false positives): shared state
// Everything that is not a crash but still ruins a session. The watchdog (HealthSample, run by the VoidController
// thread) turns it into events the client drains with get_health_events; live numbers come from get_health.
struct HealthEvent { ULONGLONG tick; char kind[16]; char text[140]; uint64_t value; };
static std::mutex g_healthMutex;
static std::vector<HealthEvent> g_healthEvents;
static void HealthEventAdd(const char* kind, const char* text, uint64_t value = 0)   // normal context only (locks, may allocate)
{
    CrumbAdd("HEALTH", kind, value);
    HealthEvent e;
    memset(&e, 0, sizeof(e));
    e.tick = GetTickCount64();
    strncpy_s(e.kind, sizeof(e.kind), kind ? kind : "", _TRUNCATE);
    strncpy_s(e.text, sizeof(e.text), text ? text : "", _TRUNCATE);
    e.value = value;
    std::lock_guard<std::mutex> lock(g_healthMutex);
    if (g_healthEvents.size() < 64)
        g_healthEvents.push_back(e);
}
// How many game calls each blocking feature swallowed (a false positive shows as a count that keeps climbing).
static std::atomic<uint32_t> g_blockedPickup{ 0 };
static std::atomic<uint32_t> g_blockedEye{ 0 };
static std::atomic<uint32_t> g_blockedSecret{ 0 };
static std::atomic<uint32_t> g_blockedMerchant{ 0 };
// Our own game calls: how long the current one has been running, how many were slow.
static std::atomic<ULONGLONG> g_curCallStartMs{ 0 };
static char g_curCallName[72];
static std::atomic<uint32_t> g_slowCalls{ 0 };
static std::atomic<uint32_t> g_maxCallMs{ 0 };
static std::atomic<bool> g_stalled{ false };
static std::atomic<ULONGLONG> g_lastPeChangeMs{ 0 };
static void WriteHangSample(DWORD gameTid, ULONGLONG idleMs, bool foreground, int sampleNo);   // defined with the freeze watchdog
static bool GameWindowIsForeground();

// Own function because MSVC refuses __try in a function that has C++ objects needing unwinding (HandleRequest has).
static void CrashSelfTestFault()
{
    __try
    {
        *reinterpret_cast<volatile int*>(0x10) = 1;
    }
    __except (EXCEPTION_EXECUTE_HANDLER)
    {
    }
}

// The last `maxEntries` breadcrumbs, oldest first, one per line: "+<ms ago> tid=<n> KIND text a=0x.. b=0x..".
static std::string CrumbsToString(uint32_t maxEntries)
{
    const uint32_t head = g_crumbHead.load(std::memory_order_relaxed);
    const uint32_t count = std::min<uint32_t>(std::min<uint32_t>(head, CRUMB_SIZE), maxEntries);
    const ULONGLONG now = GetTickCount64();
    std::ostringstream out;
    for (uint32_t k = 0; k < count; ++k)
    {
        const Crumb& c = g_crumbs[(head - count + k) & (CRUMB_SIZE - 1)];
        out << "+" << (now >= c.tick ? now - c.tick : 0) << "ms tid=" << c.tid << " " << c.kind << " " << c.text
            << " a=0x" << std::hex << c.a << " b=0x" << c.b << std::dec << "\n";
    }
    return out.str();
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

// Steam (Dungeons-Win64-Shipping.exe) values: from the Steam Dumper-7 dump (Basic.hpp / OffsetsInfo.json), see
// STORE_OFFSETS["steam"] in dungeons_reader.py. An AppendString of 0 disables name resolution instead of calling
// a wrong address.
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

// Hook silence: an atomic flag checked at the top of hkProcessEvent. While set, the hook only forwards to the original
// ProcessEvent and CALL/CALLDATA/resolve/find answer "ERROR:..." at once. It starts at hook install and on every GWorld change,
// and ends when GWorld is non-null and unchanged for SILENCE_SETTLE_MS, or at the deadman cap SILENCE_MAX_MS (hook_wake and
// hook_silence <ms> are optional). Forwarding alone ("soft") did not survive a level transition, unpatching ("hard") did.
// Never patch while the engine is still booting (see WaitForEngineBoot).
static const ULONGLONG SILENCE_SETTLE_MS = 4000;    // world must sit still this long
static const ULONGLONG SILENCE_MAX_MS    = 30000;   // absolute cap per silent window (world non-null)
static const ULONGLONG SILENCE_FORCE_MAX = 30000;   // cap on hook_silence <ms>
// GWorld == 0 means the engine is still booting or a world is being torn down. A cold start can exceed
// SILENCE_MAX_MS, so a null world gets a much longer cap. It still fails open eventually (a stale GWorld offset
// after a game update must not leave the hook silent forever).
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

// Launch-parity guard
// Until the hook has gone ACTIVE once in this session (the game start-up window), EvaluateSilence() runs the
// original simple state machine (30 s deadman, direct logging, no purge / dedupe-clear), which is known not to
// crash at launch. The safety extras (null-world cap, purge of stale queued work, deferred log queue...) only
// apply after the first activation.
static std::atomic<bool> g_everActive{ false };
// Set when SetupHookThread has finished (hook enabled, or deliberately not installed: no RVA for this host /
// MinHook init failed). Until then hook_state reports "silent", and callers (the Python safety gate) must treat
// it like the silent window.
static std::atomic<bool> g_hookInstalled{ false };

// Void state (level transitions): nothing of ours may run in game threads while silent. soft: the detour stays patched and
// only forwards (one atomic load). hard: ProcessEvent is unpatched (MH_DisableHook) for the whole window and re-patched when
// the world is stable, from VoidControllerThread. Switch with "set_void_mode soft|hard" (client /void) or
// DUNGEONS_BRIDGE_VOID_MODE=hard. The DLL starts in soft and the client pushes its saved choice after injection
// (default hard: soft crashed on the first hub -> mission transition).
enum VoidMode { VOID_SOFT = 0, VOID_HARD = 1 };
static std::atomic<int>       g_voidMode{ VOID_SOFT };
static std::atomic<bool>      g_hookPatched{ false };    // detour currently patched into ProcessEvent
static std::atomic<uintptr_t> g_peTarget{ 0 };
static std::atomic<uint32_t>  g_voidCalls{ 0 };          // safety-net counter for the silent path

// Crash guard state (the guard itself is defined further down, see "Crash guard").
static const int GUARD_MAX_SITES = 16;
static std::atomic<uintptr_t> g_guardSites[GUARD_MAX_SITES];
static std::atomic<int>       g_guardSiteRule[GUARD_MAX_SITES];   // index into kGuardRules for each armed site
static std::atomic<int>       g_guardLastRule{ -1 };
static std::atomic<int>       g_guardSiteCount{ 0 };
static std::atomic<bool>      g_guardEnabled{ true };
static std::atomic<uint32_t>  g_guardHits{ 0 };
static std::atomic<uintptr_t> g_guardLastRip{ 0 };
static std::atomic<uintptr_t> g_guardBase{ 0 };

// Storm breaker: a few hits are a harmless glitch, but a sustained burst (seen live: ~9 hits/s for 40 s, then a
// permanent black screen) means the game is stuck. Past the limit the guard stops recovering and lets the real
// crash happen, so the client's "waiting for relaunch" path takes over instead of a zombie black screen.
// The flight recorder is frozen at the FIRST hit of an episode, so its dump is already logged when the breaker trips.
static const uint64_t GUARD_STORM_WINDOW_MS = 3000;   // hits are counted inside this window
static const uint32_t GUARD_STORM_MAX_HITS  = 15;     // more than this inside one window = storm
static std::atomic<uint64_t>  g_guardWindowStart{ 0 };
static std::atomic<uint32_t>  g_guardWindowHits{ 0 };
static std::atomic<bool>      g_guardStormTripped{ false };

// Flight recorder: the game can lose its local player ~12-16 min into a level (black screen, NULL player controller read ~10x/s,
// logged as "CRASH GUARD: recovered"). It keeps the last FR_SIZE ProcessEvent calls (raw pointers, no locks) and freezes them at
// the first guard hit; the controller thread then logs the resolved names plus any call that can remove a local player
// (RemovePlayer, HandleLocalPlayerLeave, OnUserSignedOut...). Max one dump per minute, "dump_recorder" dumps the live ring.
// The VEH handler only sets atomics (no allocation, lock or I/O).
static const uint32_t FR_SIZE = 16384;               // power of two
struct FrEntry { void* fn; void* self; uint32_t tick; };
static FrEntry                g_frRing[FR_SIZE];     // static storage = zero-initialised
static std::atomic<uint32_t>  g_frHead{ 0 };         // total calls recorded (index = head & (FR_SIZE - 1))
static std::atomic<bool>      g_frFrozen{ false };
static std::atomic<uint32_t>  g_frFrozenHead{ 0 };
static std::atomic<ULONGLONG> g_frFrozenTick{ 0 };
static std::atomic<ULONGLONG> g_frLastDumpMs{ 0 };

static inline void FlightRecord(void* self, void* function)
{
    if (g_frFrozen.load(std::memory_order_relaxed))
        return;
    const uint32_t i = g_frHead.fetch_add(1, std::memory_order_relaxed);
    FrEntry& e = g_frRing[i & (FR_SIZE - 1)];
    e.fn = function;
    e.self = self;
    e.tick = GetTickCount();
}

// Recent pipe commands from the client (everything except the polled "get_*" reads). The flight recorder dump
// prints them with their age, so a black screen / guard storm can be matched with what the client had just asked
// the game to do (grants, void mode, level gate ...). Diagnostic only: a torn read of a slot is harmless.
struct CmdNote { ULONGLONG ms; char text[80]; };
static CmdNote                g_cmdRing[32];         // static storage = zero-initialised
static std::atomic<uint32_t>  g_cmdHead{ 0 };
static void NoteCommand(const std::string& request)
{
    if (request.compare(0, 4, "get_") == 0 || request == "hook_state")
        return;
    CrumbAdd("PIPE", request.substr(0, 70).c_str());
    const uint32_t i = g_cmdHead.fetch_add(1, std::memory_order_relaxed) & 31;
    CmdNote& n = g_cmdRing[i];
    n.ms = GetTickCount64();
    size_t len = request.size() < sizeof(n.text) - 1 ? request.size() : sizeof(n.text) - 1;
    for (size_t k = 0; k < len; ++k)
        n.text[k] = (request[k] == '\n' || request[k] == '\r') ? ' ' : request[k];
    n.text[len] = 0;
}

static void DumpFlightRecorder();   // defined with the crash guard, below

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
    CrumbAdd("HOOK", what, world, ms);
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

// name resolution (unchanged from v2 - AppendString works fine off-thread)

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

// Guards the GObjects array walk itself (Num(), GetByIndex()), not just the per-object read. During a zone
// transition the engine tears down and recreates many objects, so the chunk array can be mid-reallocation while a
// full scan (FindObjectsWithOuter, used by find_function_on_class) walks it.
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

// Generic thread-hopped work: anything besides ProcessEvent that must run on the game's own thread (e.g. the GObjects walk).

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

// NEW: thread-hopped ProcessEvent calling

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

// Thread id currently inside one of OUR OWN CALLDATA calls (0 = none). Our call re-enters hkProcessEvent on this thread, and
// features that block a game function (merchant unlock blocking) compare against it to let our own calls through.
static std::atomic<DWORD> g_ownCallThreadId{ 0 };

// POD-only helper (no C++ objects with destructors: MSVC __try restriction). The comma operator in the filter captures the
// exception code, as GetExceptionCode() is only valid inside the filter expression.
static LONG CaptureOwnFault(EXCEPTION_POINTERS* ep, DWORD* code)
{
    *code = ep->ExceptionRecord->ExceptionCode;
    g_lastFaultAddr.store(reinterpret_cast<uint64_t>(ep->ExceptionRecord->ExceptionAddress), std::memory_order_relaxed);
    return EXCEPTION_EXECUTE_HANDLER;
}

static bool TryProcessEventCall(uintptr_t objectAddr, uintptr_t functionAddr, uint8_t* parms, DWORD& outExceptionCode)
{
    DWORD code = 0;
    const DWORD previousOwner = g_ownCallThreadId.exchange(GetCurrentThreadId(), std::memory_order_relaxed);
    g_curCallObj.store(objectAddr, std::memory_order_relaxed);
    g_curCallFunc.store(functionAddr, std::memory_order_relaxed);
    g_curCallStartMs.store(GetTickCount64(), std::memory_order_relaxed);
    __try
    {
        ProcessEventFn pe = GetProcessEventFn();
        if (!pe)
        {
            outExceptionCode = 0xE0000001;   // private code: no ProcessEvent RVA for this build
            g_ownCallThreadId.store(previousOwner, std::memory_order_relaxed);
            g_curCallObj.store(0, std::memory_order_relaxed);
            g_curCallStartMs.store(0, std::memory_order_relaxed);
            return false;
        }
        pe(reinterpret_cast<void*>(objectAddr), reinterpret_cast<void*>(functionAddr), parms);
        g_ownCallThreadId.store(previousOwner, std::memory_order_relaxed);
        g_curCallObj.store(0, std::memory_order_relaxed);
        g_curCallStartMs.store(0, std::memory_order_relaxed);
        return true;
    }
    __except (CaptureOwnFault(GetExceptionInformation(), &code))
    {
        g_ownCallThreadId.store(previousOwner, std::memory_order_relaxed);
        g_curCallObj.store(0, std::memory_order_relaxed);
        g_curCallStartMs.store(0, std::memory_order_relaxed);
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
        std::string callName = "?";
        TryGetObjectName(reinterpret_cast<void*>(req->functionAddr), callName);
        CrumbAdd("CALL", callName.c_str(), req->objectAddr, req->functionAddr);
        strncpy_s(g_curCallName, sizeof(g_curCallName), callName.c_str(), _TRUNCATE);
        const ULONGLONG callStartMs = GetTickCount64();
        bool ok = TryProcessEventCall(req->objectAddr, req->functionAddr, req->parms.data(), exceptionCode);
        {
            const uint32_t tookMs = static_cast<uint32_t>(GetTickCount64() - callStartMs);
            if (tookMs > g_maxCallMs.load(std::memory_order_relaxed))
                g_maxCallMs.store(tookMs, std::memory_order_relaxed);
            if (tookMs >= 150)
            {
                g_slowCalls.fetch_add(1, std::memory_order_relaxed);
                HealthEventAdd("SLOW_CALL", (callName + " took " + std::to_string(tookMs) + " ms on the game thread").c_str(), tookMs);
            }
        }
        if (!ok)
        {
            char where[160];
            DescribeAddress(g_lastFaultAddr.load(std::memory_order_relaxed), where, sizeof(where));
            CrumbAdd("CALLFAULT", callName.c_str(), exceptionCode, g_lastFaultAddr.load(std::memory_order_relaxed));
            std::ostringstream fault;
            fault << "CALLDATA FAULT: " << callName << " code=0x" << std::hex << exceptionCode << std::dec
                  << " at " << where << " (object 0x" << std::hex << req->objectAddr << std::dec
                  << ", build " << kBuildId << ") - the game call was aborted, the game keeps running";
            LogLine(fault.str());
        }

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

// CALLDATA <obj_hex> <func_hex> <parms_hex>: like CALL, but the parms buffer starts as the given bytes instead of
// zeros. For functions that take real input (e.g. ClientAddItem's FInventoryItemData).
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

// Chest-open detection: OnOpenLootChest never fires in this build; a real open fires an "OnInteracted"-suffixed bound event
// (Clicky, InteractableComp, ClickyComponent) whose `self` is the actor (BP_FancyChest_C, BP_SupplyStation_C), resolved via
// GNames. Results are cached per function pointer (several exist). Which classes count as a chest is decided in Python,
// so that list can change without recompiling this DLL.

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

// Whole interact family ("OnInteract" and "OnInteracted"), used for pickup BLOCKING: a pickup's effect (e.g. food healing) is
// applied in the earlier "OnInteract" call. Not for chest reporting (see IsChestReportFunction). A function whose name failed
// to resolve is not negative-cached (GNames can fail transiently): only once the name resolved or after NAME_FAIL_LIMIT tries.
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

// Narrow match for chest-open REPORTING: "OnInteract" but NOT "OnInteracted". "OnInteracted" can fire several
// times per physical open (once per spawned item), which would over-count; "OnInteract" fires once per
// interaction attempt.
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

// Progressive pickup gating: the client picks the unlocked tier, this DLL only enforces it (1 = Health items, 2 = + Potions,
// 3 = + TNT). Weapons, armor, artifacts, tokens, eyes, arrows and non-pickups (e.g. BP_MapTable_C) are never gated (tier -1).
// Classified by NAME PATTERN, not a class list, so unseen pickup classes are covered: BP_Food1/2/3Storable_C (health),
// BP_StrengthPotionStorable_C, BP_BackstabbersBrewStorable_C ("brew" matches too: potion), BP_TNTBoxStorable_C (TNT).
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

// Eye of Ender pickup blocking (option eye_of_ender_checks): the client turns mini boss Eyes into checks, so the real Eye must
// not also be picked up. "set_block_eyes 1" enables it (DEFAULT OFF, nothing runs otherwise). Swallowed: any "PickUpEye"
// function, and every Interact-family call on an object whose class is or derives from EyeOfEnderInstance (SuperStruct walk,
// so the Blueprint subclass name does not matter; whole family because the effect can apply in OnInteract).
// The Eye stays on the ground; the check was already sent.
static std::atomic<bool> g_blockEyePickup{ false };

static const uintptr_t kUStructSuperOffset = 0x40;   // UStruct::SuperStruct - same value as OFFSETS["ustruct_super"] in dungeons_reader.py

static bool TryReadSuperStruct(void* structPtr, void*& outSuper)
{
    __try
    {
        outSuper = *reinterpret_cast<void**>(reinterpret_cast<uintptr_t>(structPtr) + kUStructSuperOffset);
        return true;
    }
    __except (EXCEPTION_EXECUTE_HANDLER)
    {
        return false;
    }
}

static std::mutex g_eyeClassMutex;
static std::unordered_map<void*, bool>& EyeClassCache()   // lazy: nothing allocated at DLL load
{
    static std::unordered_map<void*, bool> m;
    return m;
}

// True if the object's class is, or derives from, the native class EyeOfEnderInstance. Cached per class pointer;
// a negative is only cached when the WHOLE chain could be read (a transient name-resolve failure must not make a
// real Eye class look like "not an Eye" for the rest of the session).
static bool IsEyeOfEnderObject(void* self)
{
    void* classPtr = nullptr;
    if (!TryReadClassPtr(self, classPtr) || !classPtr)
        return false;
    {
        std::lock_guard<std::mutex> lock(g_eyeClassMutex);
        auto it = EyeClassCache().find(classPtr);
        if (it != EyeClassCache().end())
            return it->second;
    }

    bool isEye = false;
    bool chainComplete = true;
    void* current = classPtr;
    for (int depth = 0; depth < 16 && current; ++depth)
    {
        std::string name;
        if (!TryGetObjectName(current, name))
        {
            chainComplete = false;
            break;
        }
        if (name == "EyeOfEnderInstance")
        {
            isEye = true;
            break;
        }
        void* next = nullptr;
        if (!TryReadSuperStruct(current, next))
        {
            chainComplete = false;
            break;
        }
        current = next;
    }

    if (isEye || chainComplete)
    {
        std::lock_guard<std::mutex> lock(g_eyeClassMutex);
        EyeClassCache()[classPtr] = isEye;
    }
    return isEye;
}

static std::mutex g_eyePickupFnMutex;
static std::unordered_set<void*> g_eyePickupFunctionPtrs;
static std::unordered_set<void*> g_nonEyePickupFunctionPtrs;

// UFunction named like "PickUpEye" (AEyeOfEnderInstance::PickUpEye). Cached like the other function classifiers.
static bool IsEyePickupFunction(void* function)
{
    {
        std::lock_guard<std::mutex> lock(g_eyePickupFnMutex);
        if (g_eyePickupFunctionPtrs.count(function))
            return true;
        if (g_nonEyePickupFunctionPtrs.count(function))
            return false;
    }

    std::string name;
    const bool resolved = TryGetObjectName(function, name);
    const bool isMatch = resolved && name.find("PickUpEye") != std::string::npos;

    std::lock_guard<std::mutex> lock(g_eyePickupFnMutex);
    if (isMatch)
    {
        g_eyePickupFunctionPtrs.insert(function);
        LogLine("Classified Eye of Ender pickup function (blocking-eligible): " + name);
    }
    else if (ShouldCacheNegative(function, resolved))
    {
        g_nonEyePickupFunctionPtrs.insert(function);
    }
    return isMatch;
}

typedef void(*ProcessEventHookFn)(void*, void*, void*);
static ProcessEventHookFn oProcessEvent = nullptr;

static std::atomic<uint64_t> g_processEventCallCount{ 0 };

// Character-death detection (classify-and-cache, like IsChestReportFunction)
// Matches the exact name "OnCharacterDeath" (ABaseCharacter::OnCharacterDeath(), no params, `self` = dying actor).
// A reflected UFunction in the SDK is no proof it fires through ProcessEvent (OnOpenLootChest did not), so the
// first sighting of ANY function whose name contains "death" (case-insensitive) is also logged to
// dungeons_bridge_debug.log, in case something else is what really fires.
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

// Secret-mission-unlock detection (classify-and-cache, like IsDeathFunction)
// USecretComponent::SecretFound(AActor* SecretFinder) fires on the component of whatever trigger (lever, hidden
// wall...) reveals a secret mission. USecretComponent::ExecuteSecretFound also exists; it is not certain which one
// fires at runtime, so either exact name matches. Unmatched "secret"-ish names are logged like the death
// classifier does.
struct SecretFoundEvent
{
    uint64_t actorAddr;
    std::string className;
    std::string funcName;   // "SecretFound" (any secret in a level, hidden chests included)
                            // or "ShowSecretMissionUnlocked" (the game announcing that a
                            // secret MISSION was unlocked - the one the client turns into
                            // the "Secret Zone Found" check)
};

// When set, hkProcessEvent swallows the secret unlock after capturing it (get_secret_events), so the game never reveals the
// mission and the client sends an Archipelago check instead. "set_secret_block <level>", enabled only with Secret Mission Access.
// 0 = off, 1 = swallow SecretFound / ExecuteSecretFound, 2 = also the Blueprint handler containing "OnSecretFoundRecieved"
// (the game's spelling), since it is unknown which one performs the unlock (see SECRET TRACE in the debug log).
static std::atomic<int> g_blockSecretFound{ 0 };

// Secret trace (diagnostic, always on, near-zero cost)
// Every ProcessEvent call stores its function pointer in a ring buffer (one relaxed fetch_add + one store). When
// SecretFound fires, the ring is dumped to the debug log (names filtered to unlock/mission/secret-ish ones) and the
// next 3 s of calls are logged the same way, showing which game function really performs the unlock.
static const size_t SECRET_RING_SIZE = 16384;   // power of two
static void* g_secretRing[SECRET_RING_SIZE];
static std::atomic<uint32_t> g_secretRingIdx{ 0 };
static std::atomic<ULONGLONG> g_secretTraceUntil{ 0 };
static std::mutex g_secretTraceMutex;
static std::unordered_set<void*> g_secretTraceSeen;

static bool SecretTraceInteresting(const std::string& name)
{
    std::string n = ToLowerCopy(name);
    return n.find("secret") != std::string::npos || n.find("unlock") != std::string::npos
        || n.find("bonus") != std::string::npos || n.find("mission") != std::string::npos
        || n.find("reveal") != std::string::npos || n.find("discover") != std::string::npos;
}

static void SecretTraceDumpRing()
{
    std::unordered_set<void*> distinct;
    for (size_t i = 0; i < SECRET_RING_SIZE; ++i)
        if (g_secretRing[i]) distinct.insert(g_secretRing[i]);
    int logged = 0;
    LogLine("SECRET TRACE: functions called shortly BEFORE this event (" + std::to_string(distinct.size()) + " distinct, filtered):");
    for (void* f : distinct)
    {
        std::string name;
        if (TryGetObjectName(f, name) && SecretTraceInteresting(name) && logged < 80)
        {
            LogLine("SECRET TRACE (before): " + name);
            ++logged;
        }
    }
    std::lock_guard<std::mutex> lock(g_secretTraceMutex);
    g_secretTraceSeen.clear();
    g_secretTraceUntil.store(GetTickCount64() + 3000, std::memory_order_relaxed);
}

// Merchant (captured villager) unlock (option villager_checks): UMerchantsUtil::UnlockMerchantForAllLocalPlayers (WorldContext
// @0x00, Def @0x08) and APlayerCharacter::ClientUnlockMerchant (Def @0x00); SDK names, not confirmed at runtime. Calls not made by
// us (g_ownCallThreadId) are queued for get_merchant_events and become an AP check. "set_block_merchants 1 <tokens>" also swallows
// them for matching merchant defs (never Tower ones), so the merchant unlocks when the AP item arrives.
// DEFAULT OFF: the call then runs normally and is only reported.
struct MerchantUnlockEvent
{
    uint64_t defAddr;
    std::string defName;    // name of the UClass passed as the merchant definition
    std::string funcName;   // which UFunction fired
};

static std::atomic<bool> g_blockMerchantUnlock{ false };
static std::mutex g_merchantTokensMutex;
static std::vector<std::string> g_merchantBlockTokens;      // lowercase fragments of the merchant definition class names to block
static std::mutex g_merchantEventsMutex;
static std::vector<MerchantUnlockEvent> g_merchantEvents;   // drained by the pipe thread via get_merchant_events

static std::mutex g_merchantFnMutex;
static std::unordered_map<void*, int> g_merchantFnKind;     // function -> 0 not a merchant unlock, 1 ForAllLocalPlayers, 2 ClientUnlockMerchant

static int ClassifyMerchantUnlockFunction(void* function)
{
    {
        std::lock_guard<std::mutex> lock(g_merchantFnMutex);
        auto it = g_merchantFnKind.find(function);
        if (it != g_merchantFnKind.end())
            return it->second;
    }
    std::string name;
    const bool resolved = TryGetObjectName(function, name);
    int kind = 0;
    if (resolved)
    {
        if (name == "UnlockMerchantForAllLocalPlayers")
            kind = 1;
        else if (name == "ClientUnlockMerchant")
            kind = 2;
    }
    if (kind != 0 || ShouldCacheNegative(function, resolved))
    {
        std::lock_guard<std::mutex> lock(g_merchantFnMutex);
        g_merchantFnKind[function] = kind;
        if (kind != 0)
            LogLine("Classified merchant unlock function: " + name);
    }
    return kind;
}

static bool MerchantDefIsBlockable(const std::string& defName)
{
    const std::string lower = ToLowerCopy(defName);
    if (lower.find("tower") != std::string::npos)
        return false;
    std::lock_guard<std::mutex> lock(g_merchantTokensMutex);
    for (const std::string& token : g_merchantBlockTokens)
        if (!token.empty() && lower.find(token) != std::string::npos)
            return true;
    return false;
}

// Queues the event and returns the merchant definition's class name through outDefName.
static void CaptureMerchantUnlockEvent(void* params, int kind, std::string& outDefName)
{
    if (!params)
        return;
    const uint8_t* base = reinterpret_cast<const uint8_t*>(params);
    const uint64_t defAddr = *reinterpret_cast<const uint64_t*>(base + (kind == 1 ? 0x08 : 0x00));
    MerchantUnlockEvent evt;
    evt.defAddr = defAddr;
    evt.funcName = (kind == 1) ? "UnlockMerchantForAllLocalPlayers" : "ClientUnlockMerchant";
    evt.defName = "?";
    if (defAddr)
        TryGetObjectName(reinterpret_cast<void*>(defAddr), evt.defName);
    LogLine("MERCHANT UNLOCK seen: " + evt.funcName + " def=" + evt.defName);
    CrumbAdd("MERCHANT", evt.defName.c_str(), defAddr, kind);
    outDefName = evt.defName;
    std::lock_guard<std::mutex> lock(g_merchantEventsMutex);
    if (g_merchantEvents.size() < 256)
        g_merchantEvents.push_back(std::move(evt));
}

// Diagnostic only (no behaviour): which functions the game runs on a captured villager / camp villager actor. The
// interact-family hook already computed the class name, so this costs nothing for other actors. Rate limited.
static void LogVillagerInteract(void* function, const std::string& className)
{
    if (className.find("CapturedVillager") == std::string::npos && className.find("LobbyVillager") == std::string::npos)
        return;
    static std::atomic<ULONGLONG> lastMs{ 0 };
    const ULONGLONG nowMs = GetTickCount64();
    if (nowMs - lastMs.load(std::memory_order_relaxed) < 1000)
        return;
    lastMs.store(nowMs, std::memory_order_relaxed);
    std::string fname = "?";
    TryGetObjectName(function, fname);
    LogLine("VILLAGER interact: " + fname + " on " + className);
}

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

static std::mutex g_secretHandlerMutex;
static std::unordered_set<void*> g_secretHandlerPtrs;
static std::unordered_set<void*> g_nonSecretHandlerPtrs;

// The Blueprint-side handler bound to the Secret component's delegate
// (BndEvt__Secret_..._OnSecretFoundRecieved__DelegateSignature).
static bool IsSecretHandlerFunction(void* function)
{
    {
        std::lock_guard<std::mutex> lock(g_secretHandlerMutex);
        if (g_secretHandlerPtrs.count(function)) return true;
        if (g_nonSecretHandlerPtrs.count(function)) return false;
    }
    std::string name;
    bool resolved = TryGetObjectName(function, name);
    bool match = resolved && name.find("OnSecretFoundRecieved") != std::string::npos;
    std::lock_guard<std::mutex> lock(g_secretHandlerMutex);
    if (match) g_secretHandlerPtrs.insert(function);
    else if (ShouldCacheNegative(function, resolved)) g_nonSecretHandlerPtrs.insert(function);
    return match;
}

// ShowSecretMissionUnlocked: the UI-side function the game runs when a secret
// mission gets unlocked (it also runs for OUR ClientUnlockBonusMission popups -
// the Python client discards events that fall right after its own call).
// Never blocked: blocking it would kill our own popup too.
static std::mutex g_secretPopupMutex;
static std::unordered_set<void*> g_secretPopupPtrs;
static std::unordered_set<void*> g_nonSecretPopupPtrs;

static bool IsSecretUnlockedPopupFunction(void* function)
{
    {
        std::lock_guard<std::mutex> lock(g_secretPopupMutex);
        if (g_secretPopupPtrs.count(function)) return true;
        if (g_nonSecretPopupPtrs.count(function)) return false;
    }
    std::string name;
    bool resolved = TryGetObjectName(function, name);
    bool match = resolved && name == "ShowSecretMissionUnlocked";
    std::lock_guard<std::mutex> lock(g_secretPopupMutex);
    if (match) g_secretPopupPtrs.insert(function);
    else if (ShouldCacheNegative(function, resolved)) g_nonSecretPopupPtrs.insert(function);
    return match;
}

static void CaptureSecretFoundEvent(void* self, const char* funcName)
{
    std::string className;
    if (!TryGetClassName(self, className))
        return;

    SecretFoundEvent evt;
    evt.actorAddr = reinterpret_cast<uint64_t>(self);
    evt.className = className;
    evt.funcName = funcName;

    std::lock_guard<std::mutex> lock(g_secretEventsMutex);
    g_secretEvents.push_back(evt);
}

// Currency widget hook: OnValueChanged / OnCurrencyTypeChanged on UMG_CurrencyCounterBase_C (and children)
// Real reflected UFUNCTIONs that receive the value about to be displayed, so no offset guessing. Use as a
// cross-check against CURRENCY_OFFSETS or a fallback read path. They only fire while the counter widget exists
// (HUD visible), so they do not replace the always-available memory read.
// Defined before hkProcessEvent because it calls these classifiers.

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

// Exact-name match (not a substring family match). The names are generic enough that an unrelated widget (slider,
// settings...) could share them, so the call site also requires the instance's class name to contain "Counter"
// (case-insensitive), which covers UMG_CurrencyCounterBase_C and all its Blueprint children without listing them.
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
    // Params: a single FSerializableItemId (0x14 bytes) at offset 0. Its SerializedId FName is at +0x0C; an FName's
    // first 4 bytes are ComparisonIndex, the same "name_index" numbering used for items/zones elsewhere in this DLL
    // and in dungeons_reader.py.
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

// Mission-outcome trigger: only a wake-up telling Python to make ONE authoritative IsMissionCompleted() call, never a source of
// truth. Matches MulticastMissionFinished, MulticastGameOver (NetMulticast RPCs) or OnShowMissionVictory (BlueprintEvent), as it is
// unknown which fire in this build. It never guesses win/loss from the name or parameters (a totem loss may fire the same
// trigger), which would report a false positive to the AP server.
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

// Level gate (refuse a locked mission at the camp): UMissionSelectorComponent::TryStartMissionSelection (player @0x00,
// FMissionSelection @0x08 with levelName at +0x08, ReturnValue @0x10) and TryContinueMission (levelName @0x08, ReturnValue @0x09):
// swallow the call with ReturnValue = false. ServerRequestLevel/ServerAccept/ServerReject/TravelToMission/ClientLevelRequested
// are trace only (no ELevelNames). Needs the game to call these through ProcessEvent (see the "LEVEL GATE" debug lines).
// Mode 0 = trace only, 1 = deny the levels sent by "set_level_gate <mode> <id,id,...>".
static std::atomic<int> g_levelGateMode{ 0 };
static std::atomic<uint8_t> g_lockedLevel[256];
static std::atomic<uint64_t> g_levelGateDenied{ 0 };
static std::mutex g_gateFnMutex;
static std::unordered_map<void*, int>& GateFnCache()   // lazy: nothing allocated at DLL load
{
    static std::unordered_map<void*, int> m;
    return m;
}
enum GateFn { GF_NONE = 0, GF_TRY_START = 1, GF_TRY_CONTINUE = 2, GF_REQUEST = 3, GF_ACCEPT = 4,
              GF_REJECT = 5, GF_TRAVEL = 6, GF_CLIENT_REQUESTED = 7 };
static const char* const kGateFnNames[] = { "", "TryStartMissionSelection", "TryContinueMission", "ServerRequestLevel",
                                            "ServerAccept", "ServerReject", "TravelToMission", "ClientLevelRequested" };

static int ClassifyGateFunction(void* function)
{
    {
        std::lock_guard<std::mutex> lock(g_gateFnMutex);
        auto it = GateFnCache().find(function);
        if (it != GateFnCache().end())
            return it->second;
    }
    std::string name;
    if (!TryGetObjectName(function, name))
        return GF_NONE;   // transient failure: do not cache
    int kind = GF_NONE;
    for (int k = 1; k <= GF_CLIENT_REQUESTED; ++k)
        if (name == kGateFnNames[k]) { kind = k; break; }
    std::lock_guard<std::mutex> lock(g_gateFnMutex);
    GateFnCache()[function] = kind;
    return kind;
}

// POD-only helpers (a __try block cannot live in a function that has C++ objects needing unwinding).
static bool GateReadByte(uintptr_t addr, uint8_t& out)
{
    __try { out = *reinterpret_cast<const uint8_t*>(addr); return true; }
    __except (EXCEPTION_EXECUTE_HANDLER) { return false; }
}
static bool GateWriteByte(uintptr_t addr, uint8_t v)
{
    __try { *reinterpret_cast<uint8_t*>(addr) = v; return true; }
    __except (EXCEPTION_EXECUTE_HANDLER) { return false; }
}
static bool GateReadPtr(uintptr_t addr, uintptr_t& out)
{
    __try { out = *reinterpret_cast<const uintptr_t*>(addr); return true; }
    __except (EXCEPTION_EXECUTE_HANDLER) { return false; }
}
// FString = { wchar_t* Data; int32 Num; int32 Max } at addr.
static bool GateReadWideFString(uintptr_t addr, wchar_t* buf, int cap, int& len)
{
    __try
    {
        const wchar_t* data = *reinterpret_cast<const wchar_t* const*>(addr);
        int32_t num = *reinterpret_cast<const int32_t*>(addr + 8);
        if (!data || num <= 0 || num >= cap) return false;
        for (int i = 0; i < num; ++i) buf[i] = data[i];
        buf[num] = 0;
        len = num;
        return true;
    }
    __except (EXCEPTION_EXECUTE_HANDLER) { return false; }
}
static std::string GateFString(uintptr_t addr)
{
    wchar_t buf[512];
    int len = 0;
    if (!GateReadWideFString(addr, buf, 512, len))
        return "<unreadable>";
    int n = WideCharToMultiByte(CP_UTF8, 0, buf, len, nullptr, 0, nullptr, nullptr);
    if (n <= 0) return "<unreadable>";
    std::string r(n, '\0');
    WideCharToMultiByte(CP_UTF8, 0, buf, len, &r[0], n, nullptr, nullptr);
    while (!r.empty() && r.back() == '\0') r.pop_back();
    return r;
}

static void GateHandle(void* self, void* params, int kind, bool* blocked)
{
    std::string cls;
    if (!TryGetClassName(self, cls) || cls.find("MissionSelectorComponent") == std::string::npos)
        return;
    const int mode = g_levelGateMode.load(std::memory_order_relaxed);
    const std::string fname = kGateFnNames[kind];

    if (kind == GF_TRY_START || kind == GF_TRY_CONTINUE)
    {
        uint8_t id = 0;
        if (!params || !GateReadByte(reinterpret_cast<uintptr_t>(params) + 0x08, id))
            return;
        const bool locked = mode >= 1 && g_lockedLevel[id].load(std::memory_order_relaxed) != 0;
        LogLine("LEVEL GATE: " + fname + " level id " + std::to_string(id) +
                (locked ? " -> DENIED" : " -> allowed") + " (gate " + (mode >= 1 ? "on" : "off") + ")");
        if (locked)
        {
            GateWriteByte(reinterpret_cast<uintptr_t>(params) + (kind == GF_TRY_START ? 0x10 : 0x09), 0);   // ReturnValue = false
            *blocked = true;
            g_levelGateDenied.fetch_add(1, std::memory_order_relaxed);
        }
        return;
    }

    // Trace only: what the lobby actor says the pending request is (names to build the next, denying hook).
    std::string line = "LEVEL GATE TRACE: " + fname;
    uintptr_t lobby = 0;
    if (GateReadPtr(reinterpret_cast<uintptr_t>(self) + 0x128, lobby) && lobby)   // mLobbyActor
    {
        // ALobbyActor::mRequest at 0x370; FLevelRequest::LevelSettings at +0x18; unrealMapName +0x1A0, levelFilename +0x1B0
        line += " | unrealMapName=" + GateFString(lobby + 0x370 + 0x18 + 0x1A0) +
                " | levelFilename=" + GateFString(lobby + 0x370 + 0x18 + 0x1B0);
        uint8_t reqLevel = 0;
        (void)reqLevel;
    }
    LogLine(line);
}

// hkProcessEvent is a thin __try/__except wrapper around this function (all detection logic): MSVC cannot mix __try with
// objects needing unwinding. blockedOut is an out-param because the __except filter cannot see the callee's locals.
void hkProcessEvent_Impl(void* self, void* function, void* params, bool* blockedOut)
{
    // Services queued CALL/CALLDATA requests (see HandleCall/HandleCallData) from here instead of a Present hook:
    // hooking Present needed a throwaway D3D11 device created at startup alongside the game's own, the strongest
    // suspect for a startup crash. ProcessEvent fires reliably once the game runs, so draining from it is safer.
    FlushSilenceLog();   // deferred silence-transition log lines (active hook call = safe point)
    DrainPendingCalls();
    DrainGenericWork();

    uint64_t callNum = g_processEventCallCount.fetch_add(1, std::memory_order_relaxed);
    if (callNum == 0)
    {
        g_hookThreadId.store(GetCurrentThreadId(), std::memory_order_relaxed);
        LogLine("hkProcessEvent: first call received - hook is definitely firing.");
    }

    bool blocked = false;

    // Blocking check: whole Interact family, because the pickup effect can apply in the earlier "OnInteract"
    // call (see IsInteractFamilyFunction).
    const bool isInteractFn = IsInteractFamilyFunction(function);   // classified once, reused by the eye block below
    if (isInteractFn)
    {
        std::string className;
        if (TryGetClassName(self, className))
        {
            LogVillagerInteract(function, className);
            int tier = ClassifyPickupTier(className);
            int unlocked = g_pickupUnlockTier.load(std::memory_order_relaxed);
            if (tier > 0 && tier > unlocked)
            {
                blocked = true;
                g_blockedPickup.fetch_add(1, std::memory_order_relaxed);
                LogLine("BLOCKED pickup (tier " + std::to_string(tier) + " > unlocked " +
                         std::to_string(unlocked) + "): " + className);
            }
        }
    }

    // Eye of Ender pickup blocking (see the g_blockEyePickup comment). Off unless Python enabled it.
    if (!blocked && g_blockEyePickup.load(std::memory_order_relaxed))
    {
        bool eyeCall = IsEyePickupFunction(function);
        if (!eyeCall && isInteractFn)
            eyeCall = IsEyeOfEnderObject(self);
        if (eyeCall)
        {
            blocked = true;
            g_blockedEye.fetch_add(1, std::memory_order_relaxed);
            // A held interact key calls this every frame: log at most once per second.
            static std::atomic<ULONGLONG> lastEyeBlockLogMs{ 0 };
            const ULONGLONG nowMs = GetTickCount64();
            if (nowMs - lastEyeBlockLogMs.load(std::memory_order_relaxed) >= 1000)
            {
                lastEyeBlockLogMs.store(nowMs, std::memory_order_relaxed);
                std::string eyeClassName = "?";
                TryGetClassName(self, eyeClassName);
                LogLine("BLOCKED Eye of Ender pickup (the Eye is a check now): " + eyeClassName);
            }
        }
    }

    // Merchant unlock (captured villager freed): report it, and swallow it when Python enabled blocking.
    // Our own CALLDATA calls pass through here too - they are never reported and never blocked.
    {
        const int merchantKind = ClassifyMerchantUnlockFunction(function);
        if (merchantKind != 0 && g_ownCallThreadId.load(std::memory_order_relaxed) != GetCurrentThreadId())
        {
            std::string merchantDefName;
            CaptureMerchantUnlockEvent(params, merchantKind, merchantDefName);
            if (g_blockMerchantUnlock.load(std::memory_order_relaxed) && MerchantDefIsBlockable(merchantDefName))
            {
                blocked = true;
                g_blockedMerchant.fetch_add(1, std::memory_order_relaxed);
                LogLine("BLOCKED merchant unlock (the villager is a check now): " + merchantDefName);
                CrumbAdd("BLOCK", merchantDefName.c_str());
            }
        }
    }

    // Per-actor dedupe for Supply Station reporting (see the call site below). The set and its mutex are file-scope so
    // a world change can clear them (ClearSupplyDedupe).

    // Chest-open reporting: regular chests use IsChestReportFunction (OnInteract fires once per attempt, OnInteracted once per item).
    // Supply Stations only fire "OnInteracted", so they use the broader Interact-family match, only when the class name contains
    // "supply" and the narrow match did not claim it. Per-actor dedupe (g_reportedSupplyActors) keeps it to one report per station,
    // which is destroyed on open (no bOpened-style flag to check instead).
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

    // ring buffer for the secret trace (see g_secretRing)
    g_secretRing[g_secretRingIdx.fetch_add(1, std::memory_order_relaxed) & (SECRET_RING_SIZE - 1)] = function;

    {
        int blockLevel = g_blockSecretFound.load(std::memory_order_relaxed);
        if (IsSecretFoundFunction(function))
        {
            CaptureSecretFoundEvent(self, "SecretFound");
            SecretTraceDumpRing();
            if (blockLevel >= 1)
            {
                blocked = true;
                g_blockedSecret.fetch_add(1, std::memory_order_relaxed);
                LogLine("BLOCKED SecretFound/ExecuteSecretFound (sent to client as a check instead of unlocking the secret mission)");
            }
        }
        else if (blockLevel >= 2 && IsSecretHandlerFunction(function))
        {
            blocked = true;
            g_blockedSecret.fetch_add(1, std::memory_order_relaxed);
            LogLine("BLOCKED OnSecretFoundRecieved handler (level 2)");
        }
        else if (IsSecretUnlockedPopupFunction(function))
        {
            CaptureSecretFoundEvent(self, "ShowSecretMissionUnlocked");
            LogLine("ShowSecretMissionUnlocked fired (secret mission unlock announced by the game)");
        }

        // the 3 s after a secret event: log every distinct interesting function once
        ULONGLONG traceUntil = g_secretTraceUntil.load(std::memory_order_relaxed);
        if (traceUntil != 0)
        {
            if (GetTickCount64() > traceUntil)
                g_secretTraceUntil.store(0, std::memory_order_relaxed);
            else
            {
                bool first;
                {
                    std::lock_guard<std::mutex> lock(g_secretTraceMutex);
                    first = g_secretTraceSeen.insert(function).second;
                }
                std::string fname;
                if (first && TryGetObjectName(function, fname) && SecretTraceInteresting(fname))
                    LogLine("SECRET TRACE (after): " + fname);
            }
        }
    }

    {
        const int gateKind = ClassifyGateFunction(function);
        if (gateKind != GF_NONE)
            GateHandle(self, params, gateKind, &blocked);
    }

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
    // Deliberately does NOT call oProcessEvent: hkProcessEvent calls it exactly once, outside this function's __try,
    // so if an exception is caught here the original engine call is still guaranteed to run once, never twice.
}

// Preventive hardening after a beta tester's crash dump with the fault inside this DLL's own frames (probably
// classification code - TryGetClassName, the IsXFunction matchers, GObjects/FName walks - touching an object still
// under construction); not reproduced. An access violation inside classification/detection is now caught and
// logged once, and oProcessEvent still runs exactly once, as if detection had been skipped for that call.
static std::atomic<uint64_t> g_hkProcessEventExceptionCount{0};

// Separate from hkProcessEvent on purpose: this builds std::string temporaries, and a function with __try/__except
// cannot contain objects needing unwinding under /EHsc (MSVC C2712), including temporaries in the __except block.
// Keeping hkProcessEvent's body free of them is what lets it use __try.
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
    // Silent window (startup / level transition): pure pass-through, one relaxed atomic load then the original call.
    // VoidControllerThread owns the state machine; as a safety net, one call in 1024 also evaluates it here so a dead
    // controller cannot leave the hook silent forever.
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

    FlightRecord(self, function);   // black-screen investigation, see "Flight recorder"

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

// Currency pointer hook (hkCurrencyGetter / g_currencyPtr): removed, do NOT re-add. Confirmed cause of a recurring Dungeons.exe
// crash (PCallStackHash A3D4B2C4B9A67CD3F78D1BBF39B8F686BCA73B30, ~70 s in, engine frames only): it crashed alone, nothing else did.
// Nothing needs it (apply_emerald_reward uses UWalletComponent::ClientAdd, read_current_emeralds is a memory read);
// get_currency_ptr returns a "REMOVED:..." sentinel. For currency tracking use get_currency_value_events / get_currency_type_events.

// AOB scan across the main module's mapped image (size read from its PE header). Returns EVERY match, not just the
// first, so a too-generic pattern matching several places can be noticed.
// TryMatchPatternAt is a POD-only helper (no C++ objects with destructors in scope): __try cannot share a function
// with objects needing unwinding (MSVC C2712).
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
    NoteCommand(request);
    if (request == "get_currency_ptr")
    {
        // The currency getter hook is removed (crash cause, see the note above). Return a clear sentinel instead of
        // "PTR:0" so Python callers fail loudly instead of reading garbage.
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
            out << "|" << std::hex << e.actorAddr << std::dec << "," << e.className << "," << e.funcName;
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

    if (request.rfind("set_level_gate ", 0) == 0)
    {
        // "set_level_gate <mode> <id,id,...>": mode 0 = trace only, 1 = deny; ids are ELevelNames values (1..255).
        std::string body = request.substr(std::string("set_level_gate ").size());
        int mode = body.empty() ? 0 : (body[0] - '0');
        if (mode < 0 || mode > 1) mode = 0;
        for (int i = 0; i < 256; ++i) g_lockedLevel[i].store(0, std::memory_order_relaxed);
        int count = 0;
        size_t sp = body.find(' ');
        if (sp != std::string::npos)
        {
            std::string csv = body.substr(sp + 1);
            size_t pos = 0;
            while (pos < csv.size())
            {
                size_t comma = csv.find(',', pos);
                std::string tok = csv.substr(pos, comma == std::string::npos ? std::string::npos : comma - pos);
                int id = atoi(tok.c_str());
                if (id >= 1 && id <= 255) { g_lockedLevel[id].store(1, std::memory_order_relaxed); ++count; }
                if (comma == std::string::npos) break;
                pos = comma + 1;
            }
        }
        g_levelGateMode.store(mode, std::memory_order_relaxed);
        LogLine("set_level_gate: mode " + std::to_string(mode) + ", " + std::to_string(count) + " level id(s) refused");
        return "OK";
    }

    if (request == "get_level_gate")
        return std::to_string(g_levelGateMode.load()) + "," + std::to_string(g_levelGateDenied.load());

    if (request.rfind("set_secret_block ", 0) == 0)
    {
        std::string body = request.substr(std::string("set_secret_block ").size());
        int level = body.empty() ? 0 : (body[0] - '0');
        if (level < 0 || level > 2) level = 0;
        g_blockSecretFound.store(level, std::memory_order_relaxed);
        LogLine("set_secret_block: level " + std::to_string(level));
        return "OK";
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

    if (request.rfind("set_block_merchants ", 0) == 0)
    {
        // "set_block_merchants 1 villagemerchant,mysterymerchant" / "set_block_merchants 0"
        const std::string body = request.substr(std::string("set_block_merchants ").size());
        const bool enable = !body.empty() && body[0] == '1';
        std::vector<std::string> tokens;
        if (enable && body.size() > 2)
        {
            std::string current;
            for (size_t i = 2; i <= body.size(); ++i)
            {
                if (i == body.size() || body[i] == ',')
                {
                    if (!current.empty())
                        tokens.push_back(ToLowerCopy(current));
                    current.clear();
                }
                else if (body[i] != ' ')
                    current.push_back(body[i]);
            }
        }
        {
            std::lock_guard<std::mutex> lock(g_merchantTokensMutex);
            g_merchantBlockTokens = tokens;
        }
        g_blockMerchantUnlock.store(enable, std::memory_order_relaxed);
        LogLine(std::string("set_block_merchants: merchant unlock blocking ") + (enable ? "ON" : "OFF") +
                " (" + std::to_string(tokens.size()) + " name fragment(s))");
        return "OK";
    }

    if (request == "get_block_merchants")
    {
        return g_blockMerchantUnlock.load(std::memory_order_relaxed) ? "BLOCK_MERCHANTS:1" : "BLOCK_MERCHANTS:0";
    }

    if (request == "get_merchant_events")
    {
        std::vector<MerchantUnlockEvent> drained;
        {
            std::lock_guard<std::mutex> lock(g_merchantEventsMutex);
            std::swap(drained, g_merchantEvents);
        }
        std::ostringstream out;
        out << "EVENTS:" << std::dec << drained.size();
        for (const auto& e : drained)
            out << "|" << std::hex << e.defAddr << std::dec << "," << e.defName << "," << e.funcName;
        return out.str();
    }

    if (request.rfind("set_block_eyes ", 0) == 0)
    {
        const std::string body = request.substr(std::string("set_block_eyes ").size());
        const bool enable = !body.empty() && body[0] == '1';
        g_blockEyePickup.store(enable, std::memory_order_relaxed);
        LogLine(std::string("set_block_eyes: Eye of Ender pickup blocking ") + (enable ? "ON" : "OFF"));
        return "OK";
    }

    if (request == "get_health")
    {
        const ULONGLONG now = GetTickCount64();
        const ULONGLONG changed = g_lastPeChangeMs.load(std::memory_order_relaxed);
        const ULONGLONG callStart = g_curCallStartMs.load(std::memory_order_relaxed);
        std::ostringstream out;
        out << "HEALTH:peIdleMs=" << (changed && now >= changed ? now - changed : 0)
            << "|silent=" << (g_hookSilent.load(std::memory_order_relaxed) ? 1 : 0)
            << "|stalled=" << (g_stalled.load(std::memory_order_relaxed) ? 1 : 0)
            << "|ownCallMs=" << (callStart && now >= callStart ? now - callStart : 0)
            << "|slowCalls=" << g_slowCalls.load(std::memory_order_relaxed)
            << "|maxCallMs=" << g_maxCallMs.load(std::memory_order_relaxed)
            << "|guardHits=" << g_guardHits.load(std::memory_order_relaxed)
            << "|blockedPickup=" << g_blockedPickup.load(std::memory_order_relaxed)
            << "|blockedEye=" << g_blockedEye.load(std::memory_order_relaxed)
            << "|blockedSecret=" << g_blockedSecret.load(std::memory_order_relaxed)
            << "|blockedMerchant=" << g_blockedMerchant.load(std::memory_order_relaxed)
            << "|inLevelMs=" << (now - g_lastWorldChange.load(std::memory_order_relaxed));
        return out.str();
    }

    if (request == "get_health_events")
    {
        std::vector<HealthEvent> drained;
        {
            std::lock_guard<std::mutex> lock(g_healthMutex);
            std::swap(drained, g_healthEvents);
        }
        const ULONGLONG now = GetTickCount64();
        std::ostringstream out;
        out << "EVENTS:" << drained.size();
        for (const HealthEvent& e : drained)
        {
            std::string text = e.text;
            for (char& ch : text)
                if (ch == ',' || ch == '|' || ch == '\n')
                    ch = ';';
            out << "|" << (now >= e.tick ? now - e.tick : 0) << "," << e.kind << "," << text << "," << e.value;
        }
        return out.str();
    }

    if (request == "health_selftest")
    {
        // Proves the freeze snapshot works WITHOUT a freeze: takes one hang sample of the game thread right now.
        CrumbAdd("SELFTEST", "hang sample requested");
        WriteHangSample(g_hookThreadId.load(std::memory_order_relaxed), 0, GameWindowIsForeground(), 0);
        return "OK";
    }

    if (request == "get_build_info")
    {
        return std::string("BUILD:") + kBuildId + "|FEATURES:" + kFeatures;
    }

    if (request == "get_crumbs")
    {
        return "CRUMBS:\n" + CrumbsToString(60);
    }

    if (request == "crash_selftest")
    {
        // Proves the crash reporter works WITHOUT crashing anything: a deliberate, caught access violation on this
        // (pipe) thread. A "CRASH REPORT" must show up in dungeons_bridge_crash.log; the game never notices.
        CrumbAdd("SELFTEST", "deliberate caught access violation");
        CrashSelfTestFault();
        return "OK";
    }

    if (request == "dump_recorder")
    {
        // On-demand flight recorder dump (e.g. right after a black screen with no guard hit): freezes the ring
        // now, logs it, resumes recording. The result is in the DLL log ("FLIGHT RECORDER: ...").
        g_frFrozenHead.store(g_frHead.load(std::memory_order_relaxed), std::memory_order_relaxed);
        g_frFrozenTick.store(GetTickCount64(), std::memory_order_relaxed);
        g_frFrozen.store(true, std::memory_order_release);
        DumpFlightRecorder();
        return "OK";
    }

    if (request == "get_block_eyes")
    {
        return g_blockEyePickup.load(std::memory_order_relaxed) ? "BLOCK_EYES:1" : "BLOCK_EYES:0";
    }

    if (request == "get_pickup_tier")
    {
        std::ostringstream out;
        out << "TIER:" << g_pickupUnlockTier.load(std::memory_order_relaxed);
        return out.str();
    }

    // hook silence control (all optional - the hook re-arms by itself)
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
        if (m == "on")
        {
            g_guardWindowHits.store(0);
            g_guardStormTripped.store(false);
            g_guardEnabled.store(true);
        }
        else if (m == "off") g_guardEnabled.store(false);
        else                 return "ERROR: crash guard must be on or off";
        LogLine("CRASH GUARD (live override): " + m);
        return "OK";
    }
    if (request == "get_crash_guard")
    {
        std::ostringstream out;
        out << "GUARD:enabled=" << (g_guardEnabled.load() ? 1 : 0)
            << ",sites=" << g_guardSiteCount.load() << ",hits=" << g_guardHits.load()
            << ",storm=" << (g_guardStormTripped.load() ? 1 : 0);
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

// Pipe server: one client at a time (nMaxInstances=1); the pipe name contains the game's PID (see GetPipeName).
// Security attributes are nullptr (default Windows ACL); remote clients are rejected (PIPE_REJECT_REMOTE_CLIENTS).
// Every message received goes to HandleRequest and its reply is written back: there is no authentication.
// When the client disconnects the pipe is closed and recreated.
void PipeServerThread()
{
    while (true)
    {
        HANDLE pipe = CreateNamedPipeW(
            GetPipeName(),
            PIPE_ACCESS_DUPLEX,
            PIPE_TYPE_MESSAGE | PIPE_READMODE_MESSAGE | PIPE_WAIT | PIPE_REJECT_REMOTE_CLIENTS,
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

// Present hook: removed, do NOT re-add
// It created a throwaway D3D11 device+swapchain at startup, alongside the game's own, just to read Present's
// address. That is the prime suspect for a startup crash (3 dumps, identical offsets, driver/game frames only, no
// dungeons_bridge frame, never happens without the mod). Present only drained g_pendingCalls (the CALL/CALLDATA
// queue); that now happens in hkProcessEvent_Impl (see DrainPendingCalls), which needs no extra device.

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

// Crash guard rule 0: two live crashes (Microsoft Store build, ~15 min after a level load, no dungeons_bridge frame) at `call <getter>`
// (returns NULL, likely GetPlayerController(...,0)) then `48 8B 98 C8 03 00 00 mov rbx,[rax+3C8h]` and `test rbx,rbx; jz`: the game
// null-checks the member but not its source pointer. The handler sets rbx = 0 and skips the load, only for a read of 0x3C8 with
// rax == 0 at a site matched at start-up; no allocation, lock or I/O (hits are counted, the controller thread logs).
// It hides the symptom, not why no player controller exists.

// Rule 1 (UE4Minidump, 96 min in, RIP in Dungeons.exe): a small method (this, bool, bool) doing `48 8B 49 30 mov rcx,[rcx+30h]` with
// this == NULL, then `test rcx,rcx; je <epilogue>` (the game already handles a null member). Same fix as rule 0: rcx = 0 and skip
// the 4-byte load, only for a read of 0x30 with rcx == 0 at a site matched at start-up. Pattern: 48 89 6C 24 10 / 48 89 74 24 18 /
// 57 / 48 83 EC 20, 48 8B F9, 41 0F B6 F0, 48 8B 49 30, 0F B6 EA, 48 85 C9, 74 70.

enum GuardReg { GR_RAX = 0, GR_RCX, GR_RDX, GR_RBX, GR_RSI, GR_RDI, GR_COUNT };

struct GuardRule
{
    const char*    name;
    const int16_t* pattern;      // -1 = wildcard; pattern[0] must be a real byte
    size_t         patternLen;
    size_t         faultOffset;  // the faulting instruction inside the pattern
    size_t         instrLen;     // its length
    uintptr_t      faultDisp;    // the address it must fault on (the member offset, base register being NULL)
    GuardReg       baseReg;      // register that holds the NULL pointer
    GuardReg       destReg;      // register the skipped load would have filled: set to 0
};

static const int16_t kGuardPatternTick[] = {
    0x33, 0xD2, 0x48, 0x8B, 0xCB, 0xE8, -1, -1, -1, -1,
    0x48, 0x8B, 0x98, 0xC8, 0x03, 0x00, 0x00, 0x48, 0x85, 0xDB, 0x74 };

static const int16_t kGuardPatternNullThis[] = {
    0x48, 0x89, 0x6C, 0x24, 0x10, 0x48, 0x89, 0x74, 0x24, 0x18, 0x57, 0x48, 0x83, 0xEC, 0x20,
    0x48, 0x8B, 0xF9, 0x41, 0x0F, 0xB6, 0xF0, 0x48, 0x8B, 0x49, 0x30, 0x0F, 0xB6, 0xEA,
    0x48, 0x85, 0xC9, 0x74, 0x70, 0x41, 0x0F, 0xB6, 0xD0, 0x48, 0x89, 0x5C, 0x24, 0x30,
    0xE8, -1, -1, -1, -1, 0x48, 0x8B, 0xD8, 0x40, 0x84, 0xED, 0x74, 0x40,
    0x83, 0xBF, 0x90, 0x00, 0x00, 0x00, 0x00, 0x7E, 0x37 };

static const GuardRule kGuardRules[] = {
    { "tick: GetPlayerController(..,0)->member @0x3C8", kGuardPatternTick,
      sizeof(kGuardPatternTick) / sizeof(kGuardPatternTick[0]), 10, 7, 0x3C8, GR_RAX, GR_RBX },
    { "null this: this->member @0x30", kGuardPatternNullThis,
      sizeof(kGuardPatternNullThis) / sizeof(kGuardPatternNullThis[0]), 22, 4, 0x30, GR_RCX, GR_RCX },
};
static const int kGuardRuleCount = static_cast<int>(sizeof(kGuardRules) / sizeof(kGuardRules[0]));

// Pure: finds one rule's pattern in [mem, mem+size); returns the absolute addresses of the faulting instruction
// (memBase + offset) - separate so it can be unit-tested.
static size_t FindGuardSites(const GuardRule& rule, const uint8_t* mem, size_t size, uintptr_t memBase,
                             uintptr_t* out, size_t maxOut)
{
    size_t n = 0;
    if (size < rule.patternLen)
        return 0;
    const uint8_t first = static_cast<uint8_t>(rule.pattern[0]);
    for (size_t i = 0; i + rule.patternLen <= size && n < maxOut; ++i)
    {
        if (mem[i] != first)
            continue;
        bool ok = true;
        for (size_t j = 1; j < rule.patternLen; ++j)
        {
            const int16_t want = rule.pattern[j];
            if (want >= 0 && mem[i + j] != static_cast<uint8_t>(want)) { ok = false; break; }
        }
        if (ok)
            out[n++] = memBase + i + rule.faultOffset;
    }
    return n;
}

// Pure decision, kept separate so it can be unit-tested: the index of the rule to apply, or -1 to pass the
// exception on untouched. regs is indexed by GuardReg.
static int GuardMatch(bool enabled, DWORD code, uintptr_t faultRip, uint64_t readWrite, uint64_t faultAddr,
                      const uint64_t* regs, const uintptr_t* sites, const int* siteRules, int siteCount)
{
    if (!enabled) return -1;
    if (code != EXCEPTION_ACCESS_VIOLATION) return -1;
    if (readWrite != 0) return -1;
    for (int i = 0; i < siteCount; ++i)
    {
        if (sites[i] != faultRip)
            continue;
        const int ruleIdx = siteRules[i];
        if (ruleIdx < 0 || ruleIdx >= kGuardRuleCount)
            return -1;
        const GuardRule& r = kGuardRules[ruleIdx];
        if (faultAddr != r.faultDisp || regs[r.baseReg] != 0)
            return -1;
        return ruleIdx;
    }
    return -1;
}

// Pure: registers one hit at time `now` (ms). Starts a new window when the
// current one has expired, otherwise counts inside it. Returns true when the
// hits inside one window exceed `maxHits` (= a storm). Kept separate so it can
// be unit-tested.
static bool GuardStormUpdate(uint64_t now, uint64_t windowMs, uint32_t maxHits,
                             uint64_t& windowStart, uint32_t& windowHits)
{
    if (windowHits == 0 || now < windowStart || now - windowStart > windowMs)
    {
        windowStart = now;
        windowHits = 0;
    }
    ++windowHits;
    return windowHits > maxHits;
}

static LONG CALLBACK CrashGuardVeh(PEXCEPTION_POINTERS info)
{
    const EXCEPTION_RECORD* er = info->ExceptionRecord;
    if (er->ExceptionCode != EXCEPTION_ACCESS_VIOLATION || er->NumberParameters < 2)
        return EXCEPTION_CONTINUE_SEARCH;

    uintptr_t sites[GUARD_MAX_SITES];
    int siteRules[GUARD_MAX_SITES];
    const int count = g_guardSiteCount.load(std::memory_order_acquire);
    for (int i = 0; i < count && i < GUARD_MAX_SITES; ++i)
    {
        sites[i] = g_guardSites[i].load(std::memory_order_relaxed);
        siteRules[i] = g_guardSiteRule[i].load(std::memory_order_relaxed);
    }

    CONTEXT* c = info->ContextRecord;
    const uintptr_t rip = reinterpret_cast<uintptr_t>(er->ExceptionAddress);
    const uint64_t regs[GR_COUNT] = { c->Rax, c->Rcx, c->Rdx, c->Rbx, c->Rsi, c->Rdi };
    const int rule = GuardMatch(g_guardEnabled.load(std::memory_order_relaxed), er->ExceptionCode, rip,
                                er->ExceptionInformation[0], er->ExceptionInformation[1], regs,
                                sites, siteRules, count);
    if (rule < 0)
        return EXCEPTION_CONTINUE_SEARCH;

    // Storm check (atomics + GetTickCount64 only: no allocation, no locks, no file
    // I/O). On a storm: disarm and pass the exception on untouched, so the game's
    // own crash path (and our crash reporter, registered after us) takes over.
    {
        uint64_t ws = g_guardWindowStart.load(std::memory_order_relaxed);
        uint32_t wh = g_guardWindowHits.load(std::memory_order_relaxed);
        const bool storm = GuardStormUpdate(GetTickCount64(), GUARD_STORM_WINDOW_MS, GUARD_STORM_MAX_HITS, ws, wh);
        g_guardWindowStart.store(ws, std::memory_order_relaxed);
        g_guardWindowHits.store(wh, std::memory_order_relaxed);
        if (storm)
        {
            g_guardStormTripped.store(true, std::memory_order_release);
            g_guardEnabled.store(false, std::memory_order_relaxed);
            g_guardLastRip.store(rip, std::memory_order_relaxed);
            g_guardLastRule.store(rule, std::memory_order_relaxed);
            CrumbAdd("GUARD_STORM", "storm breaker tripped - guard disarmed", rip, static_cast<uint64_t>(rule));
            return EXCEPTION_CONTINUE_SEARCH;
        }
    }

    const GuardRule& r = kGuardRules[rule];
    switch (r.destReg)          // "member is null" - the game's own jz/je handles it
    {
    case GR_RAX: c->Rax = 0; break;
    case GR_RCX: c->Rcx = 0; break;
    case GR_RDX: c->Rdx = 0; break;
    case GR_RBX: c->Rbx = 0; break;
    case GR_RSI: c->Rsi = 0; break;
    case GR_RDI: c->Rdi = 0; break;
    default: return EXCEPTION_CONTINUE_SEARCH;
    }
    c->Rip += r.instrLen;       // step over the faulting load
    g_guardLastRip.store(rip, std::memory_order_relaxed);
    g_guardLastRule.store(rule, std::memory_order_relaxed);
    g_guardHits.fetch_add(1, std::memory_order_relaxed);
    CrumbAdd("GUARD", kGuardRules[rule].name, rip, static_cast<uint64_t>(rule));

    // First hit of an episode (nothing dumped for a minute): freeze the flight recorder so the calls that led up
    // to the lost player are kept. Atomics only - this runs inside the exception handler.
    {
        const ULONGLONG nowMs = GetTickCount64();
        bool expected = false;
        if (nowMs - g_frLastDumpMs.load(std::memory_order_relaxed) > 60000 &&
            g_frFrozen.compare_exchange_strong(expected, true))
        {
            g_frFrozenHead.store(g_frHead.load(std::memory_order_relaxed), std::memory_order_relaxed);
            g_frFrozenTick.store(nowMs, std::memory_order_relaxed);
        }
    }
    return EXCEPTION_CONTINUE_EXECUTION;
}

// Reading a code section can't normally fault, but never let a scan take the
// game down: POD-only helper so __try is allowed.
static size_t ScanRegionSafe(int ruleIdx, uintptr_t start, size_t size, uintptr_t* out, size_t maxOut)
{
    __try
    {
        return FindGuardSites(kGuardRules[ruleIdx], reinterpret_cast<const uint8_t*>(start), size, start, out, maxOut);
    }
    __except (EXCEPTION_EXECUTE_HANDLER)
    {
        return 0;
    }
}

// Names (substrings) of functions that can take a local player away from the game.
static const char* const kLocalPlayerLossNeedles[] = {
    "RemovePlayer", "RemoveLocalPlayer", "HandleControllerConnectionChange", "HandleLocalPlayerLeave",
    "OnUserSignedOut", "OnControllerConnectionChanged", "GetControllerDisconnectedMessage",
    "ControllerDisconnect", "LocalPlayerLeave", "SignedOut",
};

static bool IsLocalPlayerLossName(const std::string& name)
{
    for (const char* needle : kLocalPlayerLossNeedles)
        if (name.find(needle) != std::string::npos)
            return true;
    return false;
}

// Resolves and logs the frozen ring: how many calls / distinct functions, every suspect call, the busiest
// functions, and the last 80 calls (oldest first, "-ms" = time before the freeze). Unfreezes at the end.
static void DumpFlightRecorder()
{
    const uint32_t head = g_frFrozenHead.load(std::memory_order_relaxed);
    const uint32_t count = head < FR_SIZE ? head : FR_SIZE;
    const uint32_t frozenTick32 = static_cast<uint32_t>(g_frFrozenTick.load(std::memory_order_relaxed));

    std::unordered_map<void*, std::string> names;
    std::unordered_map<void*, uint32_t> counts;
    auto nameOf = [&](void* fn) -> const std::string& {
        auto it = names.find(fn);
        if (it != names.end())
            return it->second;
        std::string n;
        if (!TryGetObjectName(fn, n))
            n = "?";
        return names.emplace(fn, n).first->second;
    };

    for (uint32_t k = 0; k < count; ++k)
    {
        const FrEntry e = g_frRing[(head - count + k) & (FR_SIZE - 1)];
        if (e.fn)
            ++counts[e.fn];
    }

    {
        std::ostringstream o;
        o << "FLIGHT RECORDER: first guard hit of an episode - last " << count
          << " ProcessEvent calls before it, " << counts.size() << " distinct functions.";
        LogLine(o.str());
    }

    int suspectsLogged = 0;
    for (uint32_t k = 0; k < count && suspectsLogged < 40; ++k)
    {
        const FrEntry e = g_frRing[(head - count + k) & (FR_SIZE - 1)];
        if (!e.fn)
            continue;
        const std::string& fname = nameOf(e.fn);
        if (!IsLocalPlayerLossName(fname))
            continue;
        std::string cls = "?";
        TryGetClassName(e.self, cls);
        std::ostringstream o;
        o << "FLIGHT RECORDER:   SUSPECT -" << static_cast<uint32_t>(frozenTick32 - e.tick) << " ms ("
          << (count - k) << " calls before the freeze): " << fname << " on " << cls;
        LogLine(o.str());
        ++suspectsLogged;
    }
    if (suspectsLogged == 0)
        LogLine("FLIGHT RECORDER:   no local-player-removal function (RemovePlayer, HandleControllerConnectionChange, "
                "HandleLocalPlayerLeave, OnUserSignedOut...) went through ProcessEvent in this window.");

    // busiest functions in the window
    std::vector<std::pair<uint32_t, void*>> busiest;
    busiest.reserve(counts.size());
    for (const auto& kv : counts)
        busiest.emplace_back(kv.second, kv.first);
    std::sort(busiest.begin(), busiest.end(),
              [](const std::pair<uint32_t, void*>& a, const std::pair<uint32_t, void*>& b) { return a.first > b.first; });
    for (size_t i = 0; i < busiest.size() && i < 12; ++i)
    {
        std::ostringstream o;
        o << "FLIGHT RECORDER:   busiest #" << (i + 1) << ": " << busiest[i].first << " x " << nameOf(busiest[i].second);
        LogLine(o.str());
    }

    // the last 80 calls, oldest first
    const uint32_t tail = count < 80 ? count : 80;
    for (uint32_t k = count - tail; k < count; ++k)
    {
        const FrEntry e = g_frRing[(head - count + k) & (FR_SIZE - 1)];
        if (!e.fn)
            continue;
        std::string cls = "?";
        TryGetClassName(e.self, cls);
        std::ostringstream o;
        o << "FLIGHT RECORDER:   -" << static_cast<uint32_t>(frozenTick32 - e.tick) << " ms  " << nameOf(e.fn)
          << "  <- " << cls;
        LogLine(o.str());
    }

    // what the client had asked for just before (oldest first), aged against the moment the recorder froze
    {
        const ULONGLONG frozenMs = g_frFrozenTick.load(std::memory_order_relaxed);
        const uint32_t h = g_cmdHead.load(std::memory_order_relaxed);
        const uint32_t cnt = h < 32 ? h : 32;
        LogLine("FLIGHT RECORDER: last " + std::to_string(cnt) + " client command(s) (no get_* polls), oldest first:");
        for (uint32_t k = 0; k < cnt; ++k)
        {
            const CmdNote n = g_cmdRing[(h - cnt + k) & 31];
            if (!n.ms)
                continue;
            const long long age = static_cast<long long>(frozenMs) - static_cast<long long>(n.ms);
            LogLine("FLIGHT RECORDER:   cmd " + (age >= 0 ? "-" + std::to_string(age) : "+" + std::to_string(-age)) +
                    " ms  " + std::string(n.text));
        }
    }

    g_frLastDumpMs.store(GetTickCount64(), std::memory_order_relaxed);
    g_frFrozen.store(false, std::memory_order_release);   // recording resumes
}

// Crash reporter: a vectored exception handler registered LAST (the crash guard gets first pick). For every serious first-chance
// exception nobody patched it writes dungeons_bridge_crash.log: code, faulting module+offset, registers, stack walk, whether it was
// inside one of our protected game calls, and the breadcrumbs. It never changes the outcome (CONTINUE_SEARCH): the LAST report
// before the game dies is the culprit, earlier ones were caught by a __try. Allocation- and lock-free (static buffers, raw WriteFile).
static HANDLE g_crashFile = INVALID_HANDLE_VALUE;
static std::atomic<uint32_t> g_crashReports{ 0 };
static std::atomic<long> g_inCrashReporter{ 0 };

static void CrashPrintf(const char* fmt, ...)
{
    if (g_crashFile == INVALID_HANDLE_VALUE)
        return;
    char buf[640];
    va_list ap;
    va_start(ap, fmt);
    int n = _vsnprintf_s(buf, sizeof(buf), _TRUNCATE, fmt, ap);
    va_end(ap);
    if (n < 0)
        n = static_cast<int>(strlen(buf));
    DWORD written = 0;
    WriteFile(g_crashFile, buf, static_cast<DWORD>(n), &written, nullptr);
}

static void DescribeAddress(uint64_t addr, char* out, size_t n)
{
    MEMORY_BASIC_INFORMATION mbi;
    if (addr == 0 || !VirtualQuery(reinterpret_cast<LPCVOID>(addr), &mbi, sizeof(mbi)) || !mbi.AllocationBase)
    {
        _snprintf_s(out, n, _TRUNCATE, "0x%llx (unmapped)", static_cast<unsigned long long>(addr));
        return;
    }
    const char* name = "?";
    char path[MAX_PATH];
    if (mbi.AllocationBase == g_ownModule)
        name = "dungeons_bridge.dll";
    else if (mbi.Type == MEM_IMAGE && GetModuleFileNameA(reinterpret_cast<HMODULE>(mbi.AllocationBase), path, MAX_PATH))
    {
        const char* slash = strrchr(path, '\\');
        name = slash ? slash + 1 : path;
    }
    else
        name = (mbi.Type == MEM_PRIVATE) ? "(heap/private memory)" : "(mapped memory)";
    _snprintf_s(out, n, _TRUNCATE, "%s+0x%llx", name,
                static_cast<unsigned long long>(addr - reinterpret_cast<uint64_t>(mbi.AllocationBase)));
}

static const char* ExceptionName(DWORD code)
{
    switch (code)
    {
    case EXCEPTION_ACCESS_VIOLATION:      return "ACCESS_VIOLATION";
    case EXCEPTION_IN_PAGE_ERROR:         return "IN_PAGE_ERROR";
    case EXCEPTION_STACK_OVERFLOW:        return "STACK_OVERFLOW";
    case EXCEPTION_ILLEGAL_INSTRUCTION:   return "ILLEGAL_INSTRUCTION";
    case EXCEPTION_PRIV_INSTRUCTION:      return "PRIV_INSTRUCTION";
    case EXCEPTION_INT_DIVIDE_BY_ZERO:    return "INT_DIVIDE_BY_ZERO";
    case EXCEPTION_ARRAY_BOUNDS_EXCEEDED: return "ARRAY_BOUNDS_EXCEEDED";
    case 0xC0000374:                      return "HEAP_CORRUPTION";
    case 0xC0000409:                      return "FAIL_FAST (stack buffer overrun / abort)";
    default:                              return "?";
    }
}

static int WalkStack(CONTEXT ctx, uint64_t* frames, int maxFrames)
{
    int n = 0;
    for (; n < maxFrames; ++n)
    {
        const DWORD64 pc = ctx.Rip;
        if (!pc)
            break;
        frames[n] = pc;
        DWORD64 imageBase = 0;
        PRUNTIME_FUNCTION fe = RtlLookupFunctionEntry(pc, &imageBase, nullptr);
        if (!fe)
        {
            ctx.Rip = *reinterpret_cast<DWORD64*>(ctx.Rsp);   // leaf function: the return address is on top of the stack
            ctx.Rsp += 8;
        }
        else
        {
            PVOID handlerData = nullptr;
            DWORD64 establisher = 0;
            RtlVirtualUnwind(UNW_FLAG_NHANDLER, imageBase, pc, fe, &ctx, &handlerData, &establisher, nullptr);
        }
        if (!ctx.Rsp)
            break;
    }
    return n;
}

static void WriteCrashReport(PEXCEPTION_POINTERS info, uint32_t number)
{
    const EXCEPTION_RECORD* er = info->ExceptionRecord;
    const CONTEXT* c = info->ContextRecord;
    const DWORD tid = GetCurrentThreadId();
    const bool ownCall = g_ownCallThreadId.load(std::memory_order_relaxed) == tid;
    const bool stackOverflow = er->ExceptionCode == EXCEPTION_STACK_OVERFLOW;
    char where[200];

    CrashPrintf("\r\n==== CRASH REPORT #%u  build=%s  pid=%lu  uptime=%llus ====\r\n", number, kBuildId,
                GetCurrentProcessId(), static_cast<unsigned long long>(GetTickCount64() / 1000));
    CrashPrintf("exception 0x%08lX %s (first-chance: harmless if the game keeps running)\r\n",
                er->ExceptionCode, ExceptionName(er->ExceptionCode));
    if ((er->ExceptionCode == EXCEPTION_ACCESS_VIOLATION || er->ExceptionCode == EXCEPTION_IN_PAGE_ERROR) &&
        er->NumberParameters >= 2)
    {
        const ULONG_PTR kind = er->ExceptionInformation[0];
        CrashPrintf("access: %s of address 0x%llx\r\n", kind == 0 ? "READ" : kind == 1 ? "WRITE" : "EXECUTE (DEP)",
                    static_cast<unsigned long long>(er->ExceptionInformation[1]));
    }
    DescribeAddress(reinterpret_cast<uint64_t>(er->ExceptionAddress), where, sizeof(where));
    CrashPrintf("at: %s\r\n", where);
    CrashPrintf("thread: %lu (%s)%s\r\n", tid,
                tid == g_hookThreadId.load(std::memory_order_relaxed) ? "game/hook thread" : "other thread",
                ownCall ? "  ** inside OUR protected game call - caught by our __try, the game continues **" : "");
    if (ownCall && g_curCallObj.load(std::memory_order_relaxed))
        CrashPrintf("our call: object=0x%llx function=0x%llx (see the CALL breadcrumb below for its name)\r\n",
                    static_cast<unsigned long long>(g_curCallObj.load(std::memory_order_relaxed)),
                    static_cast<unsigned long long>(g_curCallFunc.load(std::memory_order_relaxed)));
    CrashPrintf("hook: silent=%d  guard hits=%llu\r\n", g_hookSilent.load(std::memory_order_relaxed) ? 1 : 0,
                static_cast<unsigned long long>(g_guardHits.load(std::memory_order_relaxed)));
    CrashPrintf("regs: RAX=%llx RBX=%llx RCX=%llx RDX=%llx RSI=%llx RDI=%llx RBP=%llx RSP=%llx\r\n",
                c->Rax, c->Rbx, c->Rcx, c->Rdx, c->Rsi, c->Rdi, c->Rbp, c->Rsp);
    CrashPrintf("      R8=%llx R9=%llx R10=%llx R11=%llx R12=%llx R13=%llx R14=%llx R15=%llx RIP=%llx\r\n",
                c->R8, c->R9, c->R10, c->R11, c->R12, c->R13, c->R14, c->R15, c->Rip);

    if (!stackOverflow)   // a stack walk needs stack, and an overflow has none left
    {
        uint64_t frames[24];
        const int count = WalkStack(*c, frames, 24);
        CrashPrintf("stack (%d frames):\r\n", count);
        for (int i = 0; i < count; ++i)
        {
            DescribeAddress(frames[i], where, sizeof(where));
            CrashPrintf("  #%02d %s\r\n", i, where);
        }
    }

    const uint32_t head = g_crumbHead.load(std::memory_order_relaxed);
    const uint32_t count = head < 40 ? head : 40;
    const ULONGLONG now = GetTickCount64();
    CrashPrintf("breadcrumbs (last %u, oldest first):\r\n", count);
    for (uint32_t k = 0; k < count; ++k)
    {
        const Crumb& cr = g_crumbs[(head - count + k) & (CRUMB_SIZE - 1)];
        CrashPrintf("  +%llums tid=%lu %s %s a=0x%llx b=0x%llx\r\n",
                    static_cast<unsigned long long>(now >= cr.tick ? now - cr.tick : 0), cr.tid, cr.kind, cr.text,
                    static_cast<unsigned long long>(cr.a), static_cast<unsigned long long>(cr.b));
    }
    CrashPrintf("==== END ====\r\n");
    FlushFileBuffers(g_crashFile);
}

static LONG CALLBACK CrashReporterVeh(PEXCEPTION_POINTERS info)
{
    const DWORD code = info->ExceptionRecord->ExceptionCode;
    switch (code)
    {
    case EXCEPTION_ACCESS_VIOLATION: case EXCEPTION_IN_PAGE_ERROR: case EXCEPTION_STACK_OVERFLOW:
    case EXCEPTION_ILLEGAL_INSTRUCTION: case EXCEPTION_PRIV_INSTRUCTION: case EXCEPTION_INT_DIVIDE_BY_ZERO:
    case EXCEPTION_ARRAY_BOUNDS_EXCEEDED: case 0xC0000374: case 0xC0000409:
        break;
    default:
        return EXCEPTION_CONTINUE_SEARCH;
    }
    if (g_inCrashReporter.exchange(1) != 0)       // an exception inside the reporter itself: do nothing more
        return EXCEPTION_CONTINUE_SEARCH;

    // the same fault address twice within a second (a loop that keeps faulting and being caught) is written once
    static std::atomic<uint64_t> lastAddr{ 0 };
    static std::atomic<ULONGLONG> lastMs{ 0 };
    const uint64_t addr = reinterpret_cast<uint64_t>(info->ExceptionRecord->ExceptionAddress);
    const ULONGLONG nowMs = GetTickCount64();
    const bool repeat = lastAddr.load(std::memory_order_relaxed) == addr && nowMs - lastMs.load(std::memory_order_relaxed) < 1000;
    lastAddr.store(addr, std::memory_order_relaxed);
    lastMs.store(nowMs, std::memory_order_relaxed);

    if (!repeat && g_crashReports.load(std::memory_order_relaxed) < 80)
    {
        const uint32_t number = g_crashReports.fetch_add(1, std::memory_order_relaxed) + 1;
        __try
        {
            WriteCrashReport(info, number);
        }
        __except (EXCEPTION_EXECUTE_HANDLER)
        {
        }
    }
    g_inCrashReporter.store(0);
    return EXCEPTION_CONTINUE_SEARCH;
}

static void InstallCrashReporter()
{
    std::string path = GetLogPath();
    const size_t slash = path.find_last_of("\\/");
    path = (slash == std::string::npos ? std::string() : path.substr(0, slash + 1)) + "dungeons_bridge_crash.log";
    g_crashFile = CreateFileA(path.c_str(), FILE_APPEND_DATA, FILE_SHARE_READ | FILE_SHARE_WRITE, nullptr,
                              OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (g_crashFile != INVALID_HANDLE_VALUE)
        CrashPrintf("\r\n---- session start  build=%s  features=%s  pid=%lu ----\r\n", kBuildId, kFeatures,
                    GetCurrentProcessId());
    PVOID handle = AddVectoredExceptionHandler(0, CrashReporterVeh);   // 0 = AFTER the crash guard's handler
    LogLine(std::string("CRASH REPORTER: build ") + kBuildId + ", features " + kFeatures +
            (handle && g_crashFile != INVALID_HANDLE_VALUE ? ", armed (dungeons_bridge_crash.log)."
                                                           : ", NOT fully armed (handler or log file failed)."));
}

// Freeze watchdog: HealthSample() runs every 250 ms on the VoidController thread. If hkProcessEvent's call count is stuck for 2.5 s
// while the hook is active, it logs a HANG REPORT (all threads' top frames, game thread stack) at +0, +8 and +25 s. Our own calls:
// >1.5 s running = OWNCALL_STUCK, >=150 ms afterwards = SLOW_CALL; a silent hook >20 s = LONG_TRANSITION. A RenderThread in a GPU/driver
// wait while the GameThread waits on it means a driver-caused black screen. Threads are suspended only for a GetThreadContext.
struct ThreadSnap { DWORD tid; uint64_t rip; uint64_t rsp; bool ok; };

static int WalkStackGuarded(const CONTEXT& ctx, uint64_t* frames, int maxFrames)
{
    int n = 0;
    __try
    {
        n = WalkStack(ctx, frames, maxFrames);
    }
    __except (EXCEPTION_EXECUTE_HANDLER)
    {
    }
    return n;
}

static bool SnapshotThread(DWORD tid, CONTEXT* out)
{
    HANDLE h = OpenThread(THREAD_SUSPEND_RESUME | THREAD_GET_CONTEXT | THREAD_QUERY_INFORMATION, FALSE, tid);
    if (!h)
        return false;
    bool ok = false;
    if (SuspendThread(h) != static_cast<DWORD>(-1))
    {
        ZeroMemory(out, sizeof(*out));
        out->ContextFlags = CONTEXT_CONTROL | CONTEXT_INTEGER;
        ok = GetThreadContext(h, out) != FALSE;
        ResumeThread(h);
    }
    CloseHandle(h);
    return ok;
}

typedef HRESULT(WINAPI* GetThreadDescriptionFn)(HANDLE, PWSTR*);

static void ThreadName(DWORD tid, char* out, size_t n)
{
    out[0] = 0;
    static GetThreadDescriptionFn fn = reinterpret_cast<GetThreadDescriptionFn>(
        GetProcAddress(GetModuleHandleA("kernelbase.dll"), "GetThreadDescription"));
    if (!fn)
        return;
    HANDLE h = OpenThread(THREAD_QUERY_LIMITED_INFORMATION, FALSE, tid);
    if (!h)
        return;
    PWSTR wide = nullptr;
    if (SUCCEEDED(fn(h, &wide)) && wide)
    {
        WideCharToMultiByte(CP_UTF8, 0, wide, -1, out, static_cast<int>(n), nullptr, nullptr);
        out[n - 1] = 0;
        LocalFree(wide);
    }
    CloseHandle(h);
}

static void WriteHangSample(DWORD gameTid, ULONGLONG idleMs, bool foreground, int sampleNo)
{
    // 1. collect everything first (threads suspended one at a time, briefly, nothing printed meanwhile)
    static ThreadSnap snaps[64];
    int snapCount = 0;
    CONTEXT gameCtx;
    const bool haveGame = gameTid && SnapshotThread(gameTid, &gameCtx);
    HANDLE snap = CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0);
    if (snap != INVALID_HANDLE_VALUE)
    {
        THREADENTRY32 te;
        te.dwSize = sizeof(te);
        const DWORD pid = GetCurrentProcessId();
        const DWORD self = GetCurrentThreadId();
        for (BOOL more = Thread32First(snap, &te); more && snapCount < 64; more = Thread32Next(snap, &te))
        {
            if (te.th32OwnerProcessID != pid || te.th32ThreadID == self)
                continue;
            CONTEXT c;
            ThreadSnap& t = snaps[snapCount++];
            t.tid = te.th32ThreadID;
            t.ok = SnapshotThread(t.tid, &c);
            t.rip = t.ok ? c.Rip : 0;
            t.rsp = t.ok ? c.Rsp : 0;
        }
        CloseHandle(snap);
    }
    uint64_t frames[24];
    const int frameCount = haveGame ? WalkStackGuarded(gameCtx, frames, 24) : 0;

    // 2. now write it
    char where[200];
    CrashPrintf("\r\n==== HANG REPORT sample %d  build=%s  pid=%lu  idle=%llums  foreground=%d ====\r\n", sampleNo, kBuildId,
                GetCurrentProcessId(), static_cast<unsigned long long>(idleMs), foreground ? 1 : 0);
    if (haveGame)
    {
        DescribeAddress(gameCtx.Rip, where, sizeof(where));
        CrashPrintf("game thread %lu stuck at: %s\r\n", gameTid, where);
        CrashPrintf("stack (%d frames):\r\n", frameCount);
        for (int i = 0; i < frameCount; ++i)
        {
            DescribeAddress(frames[i], where, sizeof(where));
            CrashPrintf("  #%02d %s\r\n", i, where);
        }
    }
    else
        CrashPrintf("game thread %lu: could not be read\r\n", gameTid);
    const ULONGLONG callStart = g_curCallStartMs.load(std::memory_order_relaxed);
    if (callStart)
        CrashPrintf("our state: OUR game call '%s' has been running for %llu ms\r\n", g_curCallName,
                    static_cast<unsigned long long>(GetTickCount64() - callStart));
    else
        CrashPrintf("our state: no game call of ours in progress; hook silent=%d\r\n",
                    g_hookSilent.load(std::memory_order_relaxed) ? 1 : 0);
    CrashPrintf("threads (top frame of each):\r\n");
    for (int i = 0; i < snapCount; ++i)
    {
        char name[64];
        ThreadName(snaps[i].tid, name, sizeof(name));
        DescribeAddress(snaps[i].ok ? snaps[i].rip : 0, where, sizeof(where));
        CrashPrintf("  tid=%lu \"%s\" %s\r\n", snaps[i].tid, name, snaps[i].ok ? where : "(unreadable)");
    }
    const uint32_t head = g_crumbHead.load(std::memory_order_relaxed);
    const uint32_t count = head < 25 ? head : 25;
    const ULONGLONG now = GetTickCount64();
    CrashPrintf("breadcrumbs (last %u, oldest first):\r\n", count);
    for (uint32_t k = 0; k < count; ++k)
    {
        const Crumb& cr = g_crumbs[(head - count + k) & (CRUMB_SIZE - 1)];
        CrashPrintf("  +%llums tid=%lu %s %s a=0x%llx b=0x%llx\r\n",
                    static_cast<unsigned long long>(now >= cr.tick ? now - cr.tick : 0), cr.tid, cr.kind, cr.text,
                    static_cast<unsigned long long>(cr.a), static_cast<unsigned long long>(cr.b));
    }
    CrashPrintf("==== END ====\r\n");
    FlushFileBuffers(g_crashFile);
}

static bool GameWindowIsForeground()
{
    DWORD pid = 0;
    GetWindowThreadProcessId(GetForegroundWindow(), &pid);
    return pid == GetCurrentProcessId();
}

static void HealthSample()
{
    static ULONGLONG nextMs = 0, lastChangeMs = 0, stallStartMs = 0, silentReported = 0;
    static uint64_t lastCount = 0;
    static int stallSamples = 0;
    static bool ownCallReported = false;
    const ULONGLONG now = GetTickCount64();
    if (now < nextMs)
        return;
    nextMs = now + 250;

    const uint64_t count = g_processEventCallCount.load(std::memory_order_relaxed);
    const bool silent = g_hookSilent.load(std::memory_order_relaxed);
    if (count != lastCount || silent || lastChangeMs == 0)
    {
        if (g_stalled.load(std::memory_order_relaxed) && !silent)
        {
            g_stalled.store(false, std::memory_order_relaxed);
            HealthEventAdd("STALL_END", ("the game thread is running again after " +
                                         std::to_string((now - stallStartMs) / 1000) + " s").c_str(), now - stallStartMs);
        }
        lastCount = count;
        lastChangeMs = now;
        stallSamples = 0;
    }
    g_lastPeChangeMs.store(lastChangeMs, std::memory_order_relaxed);

    // 1. the game thread stopped
    const bool foreground = GameWindowIsForeground();
    const ULONGLONG idle = now - lastChangeMs;
    const ULONGLONG threshold = foreground ? 2500 : 15000;   // an alt-tabbed game may legitimately idle
    if (!silent && count > 0 && idle >= threshold)
    {
        if (!g_stalled.load(std::memory_order_relaxed))
        {
            g_stalled.store(true, std::memory_order_relaxed);
            stallStartMs = lastChangeMs;
            HealthEventAdd("STALL", ("the game thread stopped running ProcessEvent (" + std::string(foreground ? "window in front"
                                      : "window in background") + ") - see HANG REPORT in dungeons_bridge_crash.log").c_str(), idle);
        }
        if ((stallSamples == 0 && idle >= threshold) || (stallSamples == 1 && idle >= threshold + 8000) ||
            (stallSamples == 2 && idle >= threshold + 25000))
        {
            WriteHangSample(g_hookThreadId.load(std::memory_order_relaxed), idle, foreground, stallSamples + 1);
            ++stallSamples;
        }
    }

    // 2. one of OUR calls is taking too long right now
    const ULONGLONG callStart = g_curCallStartMs.load(std::memory_order_relaxed);
    if (callStart && now - callStart >= 1500)
    {
        if (!ownCallReported)
        {
            ownCallReported = true;
            HealthEventAdd("OWNCALL_STUCK", (std::string("our game call '") + g_curCallName + "' has been running for " +
                                             std::to_string((now - callStart) / 1000) + " s on the game thread").c_str(), now - callStart);
        }
    }
    else if (!callStart)
        ownCallReported = false;

    // 3. a level transition that never ends
    if (silent)
    {
        const ULONGLONG since = g_silentSince.load(std::memory_order_relaxed);
        if (since && now - since >= 20000 && silentReported != since)
        {
            silentReported = since;
            HealthEventAdd("LONG_TRANSITION", ("the hook has been silent (level loading) for " +
                                               std::to_string((now - since) / 1000) + " s").c_str(), now - since);
        }
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
    int foundRule[GUARD_MAX_SITES];
    int total = 0;
    for (int ruleIdx = 0; ruleIdx < kGuardRuleCount; ++ruleIdx)
    {
        PIMAGE_SECTION_HEADER sec = IMAGE_FIRST_SECTION(nt);
        for (WORD i = 0; i < nt->FileHeader.NumberOfSections && total < GUARD_MAX_SITES; ++i, ++sec)
        {
            if (!(sec->Characteristics & IMAGE_SCN_MEM_EXECUTE))
                continue;
            const int before = total;
            total += static_cast<int>(ScanRegionSafe(ruleIdx, base + sec->VirtualAddress, sec->Misc.VirtualSize,
                                                     found + total, GUARD_MAX_SITES - total));
            for (int k = before; k < total; ++k)
                foundRule[k] = ruleIdx;
        }
    }

    if (total == 0)
    {
        LogLine("CRASH GUARD: pattern not found in this build - not armed (nothing is patched or hidden).");
        return;
    }
    for (int i = 0; i < total; ++i)
    {
        g_guardSites[i].store(found[i], std::memory_order_relaxed);
        g_guardSiteRule[i].store(foundRule[i], std::memory_order_relaxed);
        std::ostringstream o;
        o << "CRASH GUARD: armed at Dungeons image + 0x" << std::hex << (found[i] - base) << std::dec
          << " (rule " << foundRule[i] << ": " << kGuardRules[foundRule[i]].name << ")";
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
    bool stormWarned = false;
    bool stormReported = false;
    for (;;)
    {
        Sleep(20);
        HealthSample();   // freeze / stuck-call / endless-transition watchdog (see "Health watch")

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
                  << " (rule " << g_guardLastRule.load() << ")"
                  << " - game kept running. total=" << hits << ", in-level="
                  << (nowG - g_lastWorldChange.load()) / 60000.0 << " min, up="
                  << (nowG - g_dllStartTick.load()) / 60000.0 << " min, world=0x" << std::hex << g_lastSeenWorld.load();
                LogLine(o.str());
                guardLogged = hits;
            }
            if (g_frFrozen.load(std::memory_order_acquire))
                DumpFlightRecorder();   // the VEH froze the flight recorder at the first hit of an episode
        }

        // Storm breaker reporting. The handler cannot log, so say it from here:
        // a warning when a burst is building, then the verdict. The verdict line
        // may be the last thing in the log - the game crashes right after.
        {
            const uint32_t wh = g_guardWindowHits.load();
            const bool inWindow = (GetTickCount64() - g_guardWindowStart.load()) <= GUARD_STORM_WINDOW_MS;
            if (!stormWarned && inWindow && wh >= GUARD_STORM_MAX_HITS / 2)
            {
                stormWarned = true;
                LogLine("CRASH GUARD: hit storm building (" + std::to_string(wh) + " hits in <" +
                        std::to_string(GUARD_STORM_WINDOW_MS) + " ms) - the game is probably stuck with no player controller.");
            }
            else if (stormWarned && !inWindow && !g_guardStormTripped.load())
                stormWarned = false;   // burst ended on its own

            if (g_guardStormTripped.load() && !stormReported)
            {
                stormReported = true;
                LogLine("CRASH GUARD: STORM BREAKER TRIPPED - more than " + std::to_string(GUARD_STORM_MAX_HITS) +
                        " hits in " + std::to_string(GUARD_STORM_WINDOW_MS) +
                        " ms. Guard disarmed: letting the real crash happen instead of a black screen.");
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

// The inline hook is only PATCHED in once the engine has finished booting: its first UWorld exists and has stayed
// the same for this long. Patching the engine's code during its own init killed the game 10-16 s after start
// (inside FEngineLoop init, no dungeons_bridge frame, GWorld still 0), even with the launch-parity state machine.
// Injecting early is fine: the pipe server, pickup tier and hook_state (= "silent") work while we wait.
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
    LogLine("BUILD: silence + safety patch, launch-parity guard v1 + boot-wait v2 + void controller v3 (soft/hard void during transitions) + crash guard rules (tick + null-this) + flight recorder v1 (black-screen investigation) + Eye of Ender pickup blocking + level gate v0 (trace + TryStart/TryContinue deny) + client command trace in the flight recorder + guard storm breaker (3 s / 15 hits)");
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
    InstallCrashReporter();   // before everything else that can fail: reports what the rest does wrong
    InstallCrashGuard();   // independent of the hook: works even if MinHook fails below

    MH_STATUS initStatus = MH_Initialize();
    LogLine("MH_Initialize status: " + std::to_string(static_cast<int>(initStatus)));
    if (initStatus != MH_OK)
        return;

    // The native currency-getter hook is deliberately not installed: confirmed crash cause (see the note above
    // get_currency_ptr's sentinel). Do not reinstall it.
    LogLine("Currency getter hook: permanently removed from this build (confirmed crash cause) - not installed.");

    // ProcessEvent hook (chest-open detection and the CALL/CALLDATA queue). Same RVA GetProcessEventFn() uses for
    // CALL/CALLDATA; hkProcessEvent forwards everything it does not handle to oProcessEvent.
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
