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
