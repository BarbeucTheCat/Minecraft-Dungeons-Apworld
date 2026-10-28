# Minecraft Dungeons AP client - bundled inside the apworld

This `client/` folder lives INSIDE `mcdungeons.apworld` (which is just a
zip). Everything needed to play - generator world code AND client - now
ships as one file.

## Setup

1. Extract `mcdungeons.apworld` somewhere (it's a normal zip - rename to
   `.zip` if your unarchiver doesn't recognize `.apworld` directly, or
   use `unzip mcdungeons.apworld`), OR drop the `.apworld` unmodified
   into Archipelago's `custom_worlds/` folder for generation. Either
   way, this `client/` folder needs to end up on disk somewhere you can
   run Python scripts from.
2. `pip install pymem websocket-client pywin32`
3. Run `auto_inject.py` while `Dungeons.exe` is running (past the main
   menu / in a level) - injects `dungeons_bridge.dll`.
4. Run:
   ```
   python dungeons_ap_client.py --ap-host <host> --slot <n> [--ap-port 38281] [--game "Minecraft Dungeons"] [--password ""]
   ```

Re-run step 3 any time you restart `Dungeons.exe` - the DLL isn't
persistent, but injecting it twice is a safe no-op (checks first).

## Why the client can sit next to the generator code without needing a full Archipelago install

`dungeons_ap_client.py` and `dungeons_reader.py` both need a few tables
from `Locations.py`/`ZoneData.py` (location IDs, boss names, chest
counts...) - but they must NOT trigger a real import of the `mcdungeons`
package's `__init__.py`, since that pulls in `BaseClasses` /
`worlds.AutoWorld`, which only exist inside a full Archipelago install
(not something a client-only machine needs).

`_apworld_data.py` (in this folder) solves that: it loads
`../Locations.py` and `../ZoneData.py` directly by file path - the exact
same two files the generator uses, not a copy - while skipping the
package `__init__.py` entirely. Both client scripts import from it:

```python
from _apworld_data import Locations, ZoneData
```

There is exactly ONE copy of `Locations.py`/`ZoneData.py` in this
project (the one next to `__init__.py`, one directory up from here) -
nothing here duplicates or mirrors it, so it can't drift out of sync.

`Items.py` (unlike Locations.py/ZoneData.py) DOES need `BaseClasses`, so
it still can't be loaded this way - `dungeons_reader.py`'s
`MISSION_ACCESS_ITEM_IDS` mirrors its ID-allocation *formula* instead
(documented at its definition, with the fragility called out explicitly:
it'll need updating by hand if Items.py's allocation order ever
changes).

## The client is now a real GUI window

`dungeons_ap_client.py` is built on Archipelago's own `CommonClient`/
`kvui` framework - the same one SNIClient, OoTClient, and every other
built-in emulator client use. Launching it (Launcher button or
`python dungeons_ap_client.py`) opens a real window: a connection bar,
a scrolling log of every item sent/received (free, from CommonContext),
a "Game" tab showing live zone/health/attach status, and the usual
`/received`, `/missing`, `/status`, `/unlocked` etc. commands. There is
no `--slot` flag anymore - type the slot name into the client window
itself, same as any other Archipelago client.

If Dungeons.exe isn't running yet when the client starts, it retries
attaching every few seconds (logged in the "Game" tab) instead of
crashing - open the client before or after the game either way.

`ap_client.py` (the earlier hand-rolled websocket client) is no longer
used by `dungeons_ap_client.py`, but is untouched and still used
internally by `dungeons_reader.py`'s standalone `watch_*` debug
commands, and still works standalone if you want the old headless
script.

## Files in this folder

- `dungeons_ap_client.py` - the real client, see above (mission
  completion, boss kills, chests, emeralds, DeathLink, level-lock
  enforcement, all in one CommonClient-based window)
- `dungeons_reader.py` - the memory-reading core (offsets, `attach()`,
  event draining, currency reads, individual `watch_*` debug commands)
- `ap_client.py` - minimal standalone Archipelago websocket client
- `_apworld_data.py` - the loader described above
- `dungeons_bridge.cpp` / `dungeons_bridge.dll` - the injected hook DLL
  (`OnCharacterDeath` / `OnInteracted` events for boss kills and chests)
- `auto_inject.py` - injects `dungeons_bridge.dll` automatically

## What changed in `dungeons_ap_client.py` this session

Goal completion is now decided **server-side** by the generator (see
`Regions.py`'s `_place_victory_event`): two possible "Victory" event
items get locked onto real locations - reaching your Emerald Goal and/or
reaching your chosen Goal Mission, ANDed together when both are active.
Practically:

- The client no longer manually declares "goal reached" - the old
  `goal_zone` / `send_goal_complete()` codepath (driven by a local JSON
  file) is gone. The server completes your slot automatically once
  you've sent the right LocationChecks, which the client already sends
  for mission-complete and emerald milestones.
- Boss-kill watching turns on if **either** `boss_kill_checks` or the
  newer `dlc_boss_kill_checks` slot_data flag is on.
- Eye of Ender checks (`eye_of_ender_checks`): the client scans the level every ~3 s for Eye of Ender objects
  (`AEyeOfEnderInstance`, type @0x4E8 = EEyeOfEnderType 1..6) and sends the check of each type it sees.
- Crash diagnosis (`crash_report.py`, `dll_health.py`, `finish_build.py`): the DLL writes `dungeons_bridge_crash.log`
  (every serious exception nobody patched: faulting module+offset, registers, stack walk, whether it happened inside
  one of OUR protected game calls and which, and the last breadcrumbs of what the DLL was doing). `/crash` prints the
  newest report with the likely culprit and resolves `dungeons_bridge.dll+0x...` to function names when the build's
  `.map` file is next to the DLL: build with the linker option /MAP and run `python finish_build.py` after every build
  (it saves the map under the build id). `/crash crumbs` = what the DLL is doing now, `/crash build` = DLL build and
  features vs this client (also logged at every attach), `/crash test` = harmless proof that the reporter works. The
  crash bundle (`crash_bundles/`) now starts with `SUMMARY.txt`. Works offline too: `python crash_report.py`.
- Health watch, for problems that are NOT crashes (`health_watch.py`, `check_audit.py`, DLL section "Health watch"):
  the DLL's watchdog notices when the game thread stops running ProcessEvent (a freeze) and writes a HANG REPORT into
  `dungeons_bridge_crash.log` (the game thread's stack plus the top frame and name of EVERY thread, so a render/driver
  stall can be told from our code), reports one of our own game calls that is stuck (OWNCALL_STUCK) or slow (SLOW_CALL),
  and a level load that never ends. The client adds Windows' "not responding" test, a black-screen and a frozen-picture
  check on the game window (only while the game logic runs, never during a loading screen; needs Pillow), and warns when
  a blocking feature keeps swallowing calls. `/health` = live numbers + recent events; the first serious alert saves a
  crash bundle while the game is still frozen. `/audit` lists every check sent with who asked for it and flags for the
  false-positive shapes (burst, just after a level change, no player, during a load, repeat): `check_audit.log`.
  Both feed SUMMARY.txt in the crash bundles.
- Villager checks (`villager_checks`): freeing a captured villager sends the check "Rescue the <Merchant>" and the camp
  merchant arrives as an item instead (`villagers.py`). The DLL reports every merchant unlock the game starts
  (`get_merchant_events`) and swallows those of the five captured camp merchants (`set_block_merchants 1 <fragments>`);
  a received merchant item calls `UMerchantsUtil::UnlockMerchantForAllLocalPlayers` through CALLDATA, which the DLL lets
  through because it is one of our own calls. Needs the dungeons_bridge.dll rebuilt from the matching .cpp. Devtool:
  `/villager` (list / events / unlock <name> / block on|off).
- Mini boss checks: `mini_boss_kill_checks` (base-game zones) and `dlc_mini_boss_kill_checks` (DLC zones) are
  Choices, slot_data values 0 none / 1 per_type / 2 per_zone / 3 both (see `mini_boss_checks.py`).
  per_zone = one "<Zone> - Mini Boss Kill" per zone in `MINIBOSS_ZONES`, and a mini boss is FORCED into every such
  zone (the zone mini boss rule only runs in seeds with per_zone / both); per_type = one "Mini Boss - <Type>" per
  kind in `MINIBOSS_TYPE_ORDER` (EXCLUDED locations: filler only). Both are sent from the powerful-mob tracker's
  kills (the tracker is then run even if `POWERFUL_MOB_LOGGING` is off).
- Emerald milestone sending respects the `emerald_checks` /
  `emerald_is_goal` split: if `emerald_checks` is off, only the single
  top milestone is ever sent (and only if `emerald_is_goal` is on),
  instead of trying to send checks for locations that don't exist this
  generation.

## `ap_world_locations_ADDITION.py` is gone

An earlier session's hand-maintained mirror of the chest tables has been
replaced entirely by `_apworld_data.py` loading the real `Locations.py`
directly - if you still have a copy of the ADDITION file lying around,
delete it.

## Antivirus heads-up

`auto_inject.py` uses `CreateRemoteThread` + `LoadLibraryA`, the same
primitive game trainers, overlays, and mod loaders use. Some Defender
configurations flag this heuristically - add an exclusion for this
folder if it gets blocked.

## Code layout (one module per subject)

Newer features each live in ONE client module, imported by `dungeons_ap_client.py`; the apworld side of the same
feature stays in `Items.py` / `Locations.py` / `Options.py` / `Regions.py` / `Rules.py` / `__init__.py`.

| Subject | Client module | DLL part (`dungeons_bridge.cpp`) |
|---|---|---|
| Eye of Ender checks (detection, pickup blocking, giving the Eyes received as items) | `eye_of_ender.py` | section "Eye of Ender pickup blocking" |
| Mini boss kill checks (one per zone) | `mini_boss_checks.py` | - |
| `/chests` (counts, live counters, chest actors scan) | `chest_tools.py` | - |
| `/miniboss` Devtool and the zone mini boss rule | `miniboss.py` | - |
| Villager checks (captured villager -> check, merchant item -> unlock) and `/villager` | `villagers.py` | section "Merchant unlock" |
| `/effect` Devtool (freeze, slowness, potions on the player) | `effects.py` | - |

`dungeons_reader.py` keeps the shared game-reading primitives (memory, pipe, level scans, wallet) these modules import.
The level is scanned once and shared (`scan_full_zone_shared`) by the powerful-mob tracker, the Eye scan and `/chests`.

## Black screen / lost local player (flight recorder)

Some 12-16 minutes into a level the game can lose its local player: the client logs `Character ABSENT: No local
players`, the screen goes black and the DLL log fills with `CRASH GUARD: recovered ... at image + 0xb9ebbb` (the game
reads a NULL player controller ~10x per second; the guard keeps it from crashing). To find out WHY the player
disappears, `dungeons_bridge.dll` keeps the last 16384 ProcessEvent calls and, at the first guard hit of an episode,
writes `FLIGHT RECORDER:` lines to `dungeons_bridge_debug.log`: the number of calls, every call to a function that can
remove a local player (RemovePlayer, HandleControllerConnectionChange, HandleLocalPlayerLeave, OnUserSignedOut...), the
12 busiest functions and the last 80 calls with their time before the freeze. `dump_recorder` over the pipe dumps the
live ring on demand. Needs the DLL rebuilt from this `dungeons_bridge.cpp`.

The crash guard is rule based (`kGuardRules` in `dungeons_bridge.cpp`): each rule is a byte pattern for one known
"null object" crash site of the game, armed at start-up (`CRASH GUARD: armed at ... (rule N: ...)` in the DLL log) and
only acting on a READ of the exact member offset with the exact register at NULL. Rule 0: the tick reading
`GetPlayerController(..,0)->member @0x3C8`. Rule 1 (UE4Minidump of 2026-10-14, access violation reading 0x30 in
`Dungeons.exe+0x2362846`): a method called with `this == NULL` loading `this->member @0x30` before its own null check.
