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

// ---- Copied directly from the real Dumper-7 Basic.hpp - not guessed ----
namespace Offsets
{
    constexpr int32_t GObjects = 0x046556C8;
    constexpr int32_t AppendString = 0x010BF7C0;
    constexpr int32_t ProcessEvent = 0x01244C30; // direct RVA, confirmed from Basic.hpp - the
                                                  // vtable-slot-64 guess was wrong, this is the
                                                  // real deal, threading was the actual problem
    constexpr int32_t GWorld = 0x047540B0;       // MUST match dungeons_reader.py's OFFSETS["gworld"]
                                                  // exactly - both read the same global pointer, just
                                                  // from inside (this DLL) vs outside (pymem) the process.
                                                  // Update both together if it ever changes.
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

static uint64_t GetCurrentWorldPtr()
{
    // Read directly - this code runs INSIDE the target process (injected),
    // so no cross-process ReadProcessMemory is needed, unlike pymem's
    // out-of-process reads of this exact same global on the Python side.
    return *reinterpret_cast<uint64_t*>(GetImageBase() + Offsets::GWorld);
}

static TUObjectArray* GetGObjects()
{
    return reinterpret_cast<TUObjectArray*>(GetImageBase() + Offsets::GObjects);
}

static AppendStringFn GetAppendStringFn()
{
    return reinterpret_cast<AppendStringFn>(GetImageBase() + Offsets::AppendString);
}

static ProcessEventFn GetProcessEventFn()
{
    return reinterpret_cast<ProcessEventFn>(GetImageBase() + Offsets::ProcessEvent);
}

// ---- name resolution (unchanged from v2 - AppendString works fine off-thread) ----

static bool TryResolveName(int32_t comparisonIndex, wchar_t* outBuffer, int32_t bufferSize, int32_t& outLen)
{
    __try
    {
        SimpleFName name{ comparisonIndex, 0 };
        SimpleFString str{ outBuffer, 0, bufferSize };
        GetAppendStringFn()(&name, str);
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
static std::queue<std::function<void()>> g_pendingGenericWork;

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

    {
        std::lock_guard<std::mutex> lock(g_genericQueueMutex);
        g_pendingGenericWork.push(wrapped);
    }

    std::unique_lock<std::mutex> lock(*localMutex);
    localCv->wait_for(lock, std::chrono::seconds(10), [&] { return *done; });
}

static void DrainGenericWork()
{
    std::queue<std::function<void()>> localQueue;
    {
        std::lock_guard<std::mutex> lock(g_genericQueueMutex);
        std::swap(localQueue, g_pendingGenericWork);
    }
    while (!localQueue.empty())
    {
        localQueue.front()();
        localQueue.pop();
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
        int32_t total = gObjects->Num();

        for (int32_t i = 0; i < total; ++i)
        {
            void* objPtr = gObjects->GetByIndex(i);
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
        int32_t total = gObjects->Num();

        for (int32_t i = 0; i < total; ++i)
        {
            void* objPtr = gObjects->GetByIndex(i);
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
};

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
        GetProcessEventFn()(reinterpret_cast<void*>(objectAddr), reinterpret_cast<void*>(functionAddr), parms);
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

    {
        std::lock_guard<std::mutex> lock(g_queueMutex);
        g_pendingCalls.push(req);
    }

    std::unique_lock<std::mutex> lock(g_resultMutex);
    bool serviced = g_resultCv.wait_for(lock, std::chrono::seconds(5), [&] { return req->done; });

    if (!serviced)
        return "ERROR timed out waiting for render thread (is the game minimized/frozen?)";

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

    {
        std::lock_guard<std::mutex> lock(g_queueMutex);
        g_pendingCalls.push(req);
    }

    std::unique_lock<std::mutex> lock(g_resultMutex);
    bool serviced = g_resultCv.wait_for(lock, std::chrono::seconds(5), [&] { return req->done; });

    if (!serviced)
        return "ERROR timed out waiting for render thread (is the game minimized/frozen?)";

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
    bool isMatch = TryGetObjectName(function, name) &&
                   name.find("Interact") != std::string::npos;

    std::lock_guard<std::mutex> lock(g_interactFamilyClassifyMutex);
    if (isMatch)
    {
        g_interactFamilyFunctionPtrs.insert(function);
        LogLine("Classified new Interact-family function (blocking-eligible): " + name);
    }
    else
    {
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
    bool isMatch = TryGetObjectName(function, name) &&
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
    bool isMatch = TryGetObjectName(function, name) && name == "OnValueChanged";

    std::lock_guard<std::mutex> lock(g_currencyValueClassifyMutex);
    if (isMatch)
    {
        g_currencyValueFunctionPtrs.insert(function);
        LogLine("Classified new OnValueChanged function (currency-widget candidate): " + name);
    }
    else
    {
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
    bool isMatch = TryGetObjectName(function, name) && name == "OnCurrencyTypeChanged";

    std::lock_guard<std::mutex> lock(g_currencyTypeClassifyMutex);
    if (isMatch)
    {
        g_currencyTypeFunctionPtrs.insert(function);
        LogLine("Classified new OnCurrencyTypeChanged function (currency-widget candidate): " + name);
    }
    else
    {
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
        g_nonMissionOutcomeFunctionPtrs.insert(function);
    }
    return isExactMatch;
}

static void CaptureMissionOutcomeEvent(void* self, const std::string& triggerName)
{
    std::lock_guard<std::mutex> lock(g_missionOutcomeEventsMutex);
    g_missionOutcomeEvents.push_back({ reinterpret_cast<uint64_t>(self), triggerName });
}

void hkProcessEvent(void* self, void* function, void* params)
{
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
    static std::mutex g_reportedSupplyActorsMutex;
    static std::unordered_set<uint64_t> g_reportedSupplyActors;

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
                    alreadyReported = !g_reportedSupplyActors.insert(addr).second;
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
        oProcessEvent(self, function, params);
    // else: deliberately NOT calling the original - this is what actually
    // prevents the pickup. The interaction visually/functionally just does
    // nothing (no inventory add, no destroy, no sound - whatever the
    // Blueprint graph would have done never runs).
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

static void* g_currencyPtr = nullptr;

typedef int(*CurrencyGetterFn)(void* thisPtr);
static CurrencyGetterFn oCurrencyGetter = nullptr;

int hkCurrencyGetter(void* thisPtr)
{
    // Always track the LATEST thisPtr the game itself calls this getter
    // with - NOT just the first one ever seen. This getter is called
    // constantly by the game's own UI/logic, always with whatever the
    // CURRENT live currency object is. A "capture once, keep forever"
    // guard (the previous `if (!g_currencyPtr)` here) means that if the
    // underlying currency component is ever destroyed and recreated -
    // a hero swap, a save reload, the same class of zone-transition
    // churn already documented around apply_emerald_reward's own
    // staleness handling on the Python side - g_currencyPtr stays
    // permanently pinned to the OLD, now-stale/freed pointer, and every
    // later call (which DOES have the correct new pointer) gets
    // silently ignored. Confirmed happening live: a long-running
    // Dungeons.exe process (same PID across multiple client sessions)
    // ended up handing back a wildly garbage read (-1558079228) through
    // the stale pointer, and every subsequent write via that same
    // pointer "verified" successfully against ITSELF (read-back matched
    // what was just written) while never touching the real, live
    // currency object at all - so the in-game HUD never moved despite
    // every grant reporting success. Unconditionally re-assigning here
    // keeps g_currencyPtr self-healing: it converges back onto the real
    // live object within one native getter call after the game itself
    // next needs to display/use the currency (essentially immediately -
    // this getter is called far more often than dungeons_reader.py
    // polls), rather than requiring the whole DLL (and therefore the
    // whole game process) to restart to recover.
    g_currencyPtr = thisPtr;
    return oCurrencyGetter(thisPtr);
}

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
        std::ostringstream out;
        out << "PTR:" << std::hex << reinterpret_cast<uintptr_t>(g_currencyPtr);
        return out.str();
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

// ---- Present hook setup ----

typedef HRESULT(__stdcall* PresentFn)(IDXGISwapChain*, UINT, UINT);
static PresentFn oPresent = nullptr;

HRESULT __stdcall hkPresent(IDXGISwapChain* pSwapChain, UINT SyncInterval, UINT Flags)
{
    DrainPendingCalls();
    DrainGenericWork();
    return oPresent(pSwapChain, SyncInterval, Flags);
}

// Classic pattern: create a throwaway D3D11 device+swapchain just to read
// Present's real address off its vtable (slot 8) - the function pointer
// is identical no matter which swapchain instance you get it from, since
// it's the same code in the same loaded D3D11/DXGI DLL.
static uintptr_t GetPresentAddress()
{
    D3D_FEATURE_LEVEL featureLevel = D3D_FEATURE_LEVEL_11_0;
    DXGI_SWAP_CHAIN_DESC scd = {};
    scd.BufferCount = 1;
    scd.BufferDesc.Width = 100;
    scd.BufferDesc.Height = 100;
    scd.BufferDesc.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
    scd.BufferDesc.RefreshRate.Numerator = 60;
    scd.BufferDesc.RefreshRate.Denominator = 1;
    scd.BufferUsage = DXGI_USAGE_RENDER_TARGET_OUTPUT;
    scd.OutputWindow = GetDesktopWindow();
    scd.SampleDesc.Count = 1;
    scd.Windowed = TRUE;
    scd.SwapEffect = DXGI_SWAP_EFFECT_DISCARD;

    IDXGISwapChain* pSwapChain = nullptr;
    ID3D11Device* pDevice = nullptr;
    ID3D11DeviceContext* pContext = nullptr;

    HRESULT hr = D3D11CreateDeviceAndSwapChain(
        nullptr, D3D_DRIVER_TYPE_HARDWARE, nullptr, 0,
        &featureLevel, 1, D3D11_SDK_VERSION,
        &scd, &pSwapChain, &pDevice, nullptr, &pContext);

    if (FAILED(hr) || !pSwapChain)
        return 0;

    void** vtable = *reinterpret_cast<void***>(pSwapChain);
    uintptr_t presentAddr = reinterpret_cast<uintptr_t>(vtable[8]); // IDXGISwapChain::Present

    if (pContext) pContext->Release();
    if (pDevice) pDevice->Release();
    pSwapChain->Release();

    return presentAddr;
}

void SetupHookThread()
{
    LogLine("=== SetupHookThread starting (new session) ===");
    // Small delay so we're not racing the game's own D3D init too early
    Sleep(3000);

    MH_STATUS initStatus = MH_Initialize();
    LogLine("MH_Initialize status: " + std::to_string(static_cast<int>(initStatus)));
    if (initStatus != MH_OK)
        return;

    uintptr_t presentAddr = GetPresentAddress();
    if (presentAddr)
    {
        MH_STATUS presentStatus = MH_CreateHook(reinterpret_cast<void*>(presentAddr), &hkPresent,
                      reinterpret_cast<void**>(&oPresent));
        LogLine("Present hook MH_CreateHook status: " + std::to_string(static_cast<int>(presentStatus)));
        // don't bail out here if this fails - still try the currency hook below
    }
    else
    {
        LogLine("Present address not found (D3D11CreateDeviceAndSwapChain failed)");
    }

    const uint8_t currencyPattern[] = { 0xF8, 0x8B, 0x41, 0x08, 0xC3, 0xCC };
    // A live cross-check (real HUD: Emeralds=258, Gold=0, Eyes of Ender=801
    // vs the currency object this hook was capturing: only Eyes of Ender's
    // value found ANYWHERE within 0x200 bytes of it) proved this 6-byte
    // pattern is too generic to trust FindPattern's old first-match-only
    // behavior for - `mov eax,[rcx+8]; ret` is the compiled shape of any
    // trivial int32 getter, not something unique to a shared multi-
    // currency wallet object. Logging every match (not just the first) so
    // the next debugging round has hard data instead of another guess:
    // if there are several matches, the real wallet getter - if the game
    // even has ONE getter shared across all 3 currencies rather than a
    // separate object/getter per currency type - is presumably among
    // them, and each one's captured pointer can be checked with
    // dungeons_reader.py's scan_currency the same way this one just was.
    std::vector<uintptr_t> currencyMatches = FindAllPatterns(currencyPattern, sizeof(currencyPattern));
    LogLine("Currency pattern occurrences found in image: " + std::to_string(currencyMatches.size()));
    for (size_t i = 0; i < currencyMatches.size() && i < 16; ++i)
    {
        uintptr_t rva = currencyMatches[i] - GetImageBase();
        LogLine("  candidate[" + std::to_string(i) + "] @ image+0x" +
                [&]{ std::ostringstream oss; oss << std::hex << rva; return oss.str(); }());
    }
    uintptr_t found = currencyMatches.empty() ? 0 : currencyMatches[0];
    if (found)
    {
        uintptr_t hookTarget = found + 1; // skip the leading 0xF8, hook at the actual
                                           // `mov eax,[rcx+8]` body
        MH_STATUS currencyStatus = MH_CreateHook(reinterpret_cast<void*>(hookTarget), &hkCurrencyGetter,
                      reinterpret_cast<void**>(&oCurrencyGetter));
        LogLine("Currency hook MH_CreateHook status: " + std::to_string(static_cast<int>(currencyStatus)) +
                " (hooked candidate[0] - see the count above; if scan_currency still can't find "
                "Emeralds/Gold nearby, the real getter is probably one of the OTHER candidates "
                "logged above, not this one)");
    }
    else
    {
        LogLine("Currency pattern not found in image");
    }

    // ProcessEvent hook for chest-open detection. Same RVA GetProcessEventFn()
    // already uses for the CALL/CALLDATA path - after this hook is installed,
    // that path naturally starts going through hkProcessEvent too (which
    // just forwards to oProcessEvent when it's not an OnOpenLootChest call),
    // so nothing about the existing CALL functionality changes.
    uintptr_t peAddr = GetImageBase() + Offsets::ProcessEvent;
    LogLine("ProcessEvent target address: 0x" + [&]{ std::ostringstream o; o << std::hex << peAddr; return o.str(); }());
    MH_STATUS peStatus = MH_CreateHook(reinterpret_cast<void*>(peAddr),
                  &hkProcessEvent, reinterpret_cast<void**>(&oProcessEvent));
    LogLine("ProcessEvent hook MH_CreateHook status: " + std::to_string(static_cast<int>(peStatus)));

    MH_STATUS enableStatus = MH_EnableHook(MH_ALL_HOOKS);
    LogLine("MH_EnableHook(MH_ALL_HOOKS) status: " + std::to_string(static_cast<int>(enableStatus)));
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
