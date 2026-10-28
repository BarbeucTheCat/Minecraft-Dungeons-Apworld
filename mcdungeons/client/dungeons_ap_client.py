"""
dungeons_ap_client.py - real Archipelago client for Minecraft Dungeons,
built on Archipelago's own CommonClient/kvui framework (the same one
every emulator client - SNIClient, OoTClient, Zelda1Client, etc. - is
built on), instead of the hand-rolled ap_client.py websocket client the
earlier version of this file used.

WHY THE REWRITE (previous version was a plain console script that just
print()ed everything in a `while True: time.sleep(0.2)` loop - no
window, no connection bar, no item log, nothing beyond text scrolling
by in a console THAT MIGHT NOT EVEN EXIST when launched from the
Launcher - see the HAS_CONSOLE saga in this file's git history):

    - CommonContext gives a real connection bar, slot-name entry,
      auto-reconnect, and - for free, no code needed here - a scrolling
      log of every item sent/received, with the same coloring and
      formatting every other Archipelago client uses.
    - kvui.GameManager gives the actual window. It runs fine with NO
      console attached (unlike the old input()-based prompts), because
      it's not a console program at all.
    - DeathLink, hint costs, /commands (/received, /missing, etc.) all
      come from CommonContext for free instead of being hand-rolled.

WHAT'S UNCHANGED: every bit of Minecraft Dungeons-specific logic -
memory offsets, zone detection, chest/boss event draining, mission
completion polling, emerald milestones, level-lock enforcement - still
lives in dungeons_reader.py exactly as before. This file's job is only
to glue that to CommonContext instead of to ap_client.py's raw
websocket client. ap_client.py itself is untouched and still usable
standalone if anyone wants the old headless script.

Usage (same either way):
    - Click "Minecraft Dungeons Client" in the Archipelago Launcher, or
    - `python dungeons_ap_client.py` from a terminal (--connect/--password
      optional - CommonClient's own UI/console will ask for anything
      not given, including the slot name, which used to be a --slot
      flag here and now isn't - see "no --slot flag" note below).

Requires dungeons_bridge.dll injected (see auto_inject.py) for boss
kills and chest events - there is no fallback for either, same as
before.
"""

import os
import re
import json
import sys
import time
import random
import asyncio
import logging
import tempfile
import traceback
import win32file
from pathlib import Path

# Running directly (`python dungeons_ap_client.py`) auto-adds this file's
# own directory to sys.path[0], so the plain (non-relative) imports below
# just work. Importing this module DOTTED instead - e.g.
# `from .client.dungeons_ap_client import launch`, which is how the
# Archipelago Launcher's registered Component reaches it (see
# mcdungeons/__init__.py) - does NOT get that for free, so we add it
# explicitly here, unconditionally, before anything below needs it.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# --- persistent, writable data directory ------------------------------------
# dungeons_reader.py's *_LOOKUP/*_FILE constants (ZONE_NAME_LOOKUP,
# CHEST_CLASS_LOOKUP, unlocked_zones.json, etc.) are all bare relative
# filenames, read/written via plain open()/os.path.exists() - which
# resolve against the process's CURRENT WORKING DIRECTORY, not this
# script's own folder. Launched from the Archipelago Launcher, cwd is
# wherever the Launcher itself started from (e.g. C:\ProgramData\
# Archipelago\), not client/ - so every one of those lookups silently
# started fresh empty every single launch, which is why zones showed as
# "unknown_zone_N" even after being confirmed in an earlier session:
# ZONE_NAME_LOOKUP = load_lookup("zone_name_lookup.json") ran against
# the wrong directory and just seeded a new empty file there instead of
# finding the real one.
#
# Fix: chdir to one fixed, writable, PERSISTENT-ACROSS-LAUNCHES folder
# before dungeons_reader.py is imported at all (its lookups load at
# import time) - this makes every one of those bare relative paths
# resolve consistently regardless of how this client is launched.
#
# IMPORTANT - this does NOT recover data from a previous ad-hoc working
# directory. If you have existing zone_name_lookup.json / *_class_lookup
# .json / etc. files from earlier dev/testing sessions (anywhere else on
# disk), copy them into DATA_DIR (path logged below at startup) so
# confirmed zone names etc. aren't lost/re-required.
DATA_DIR = Path(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()) / "MCDungeonsAP"
DATA_DIR.mkdir(parents=True, exist_ok=True)
os.chdir(DATA_DIR)

# Confirmed lookup/state data recovered from earlier dev/testing sessions
# (zone_name_lookup.json, zone_id_order.json, etc.) ships bundled in
# client/data_seeds/ and gets copied into DATA_DIR here, ONE TIME, before
# dungeons_reader.py's module-level load_lookup() calls run - so a fresh
# DATA_DIR starts already-confirmed instead of empty.
#
# "Already there, don't touch it" is NOT just "the file exists" - any
# client run from before this seeding step existed already created an
# EMPTY zone_name_lookup.json etc. in DATA_DIR (load_lookup's own
# "doesn't exist yet" fallback, with no defaults given, seeds {} and
# saves it) - a plain existence check treats that leftover empty file as
# "already seeded" and skips it forever, which is exactly why bundling
# the seed data alone didn't fix "unknown_zone_1" - the stale empty file
# from an earlier run was still there, shadowing the bundled data every
# time. So: only skip a seed if the existing file actually has content
# ({} / [] / whitespace-only all count as "not real data yet").
def _is_empty_json_file(path):
    try:
        content = path.read_text(encoding="utf-8").strip()
    except OSError:
        return True
    return content in ("", "{}", "[]")


_SEEDS_DIR = Path(__file__).resolve().parent / "data_seeds"
for _seed_name in ("is_mission_completed_index.json", "kill_function_depth.json",
                    "mission_end_widget_class.json", "totem_lost_widget_class.json",
                    "zone_id_order.json"):
    _dest = DATA_DIR / _seed_name
    if not _dest.exists() or _is_empty_json_file(_dest):
        try:
            _dest.write_bytes(__loader__.get_data(str(_SEEDS_DIR / _seed_name)))
        except OSError:
            pass  # seed not bundled this build, or unreadable - load_lookup's own
                   # "doesn't exist yet" fallback still applies, just starts empty

# zone_name_lookup.json gets its own merge instead of the copy-if-empty
# loop above: it's a GROWING dict (new zone index->name pairs get added to
# data_seeds/zone_name_lookup.json as more zones get confirmed over time),
# not a fixed one-shot value like the seeds above. The plain "only seed if
# the destination file is still empty" check means once a player's
# DATA_DIR copy has ANY confirmed zones in it (which happens almost
# immediately), it's no longer "empty" - so newer zone entries added to
# the bundled seed after that point would never reach existing installs,
# permanently stuck showing "unknown zone (index N)" for zones the seed
# actually already knows the name of. Merge the two dicts instead, only
# filling in keys the local file doesn't already have - any zone the
# player has confirmed/relabeled locally always wins over the bundled
# seed for that same key.
_zone_lookup_dest = DATA_DIR / "zone_name_lookup.json"
try:
    _bundled_zone_lookup = json.loads(__loader__.get_data(str(_SEEDS_DIR / "zone_name_lookup.json")))
except (OSError, json.JSONDecodeError):
    _bundled_zone_lookup = None
if _bundled_zone_lookup:
    try:
        _local_zone_lookup = json.loads(_zone_lookup_dest.read_text(encoding="utf-8")) if _zone_lookup_dest.exists() else {}
    except (OSError, json.JSONDecodeError):
        _local_zone_lookup = {}
    _merged_zone_lookup = dict(_bundled_zone_lookup)
    _merged_zone_lookup.update(_local_zone_lookup)  # local confirmations/overrides win
    if _merged_zone_lookup != _local_zone_lookup:
        _zone_lookup_dest.write_text(json.dumps(_merged_zone_lookup, indent=2, sort_keys=True), encoding="utf-8")
# -----------------------------------------------------------------------

# CommonClient does `ModuleUpdate.update()` and touches logging as a side
# effect of being imported - importing it before anything else of ours
# matters is the convention every AP client follows (see e.g. OoTClient.py's
# "CommonClient import first to trigger ModuleUpdater" comment).
from CommonClient import CommonContext, server_loop, gui_enabled, ClientCommandProcessor, logger, get_base_parser
from Utils import async_start

from dungeons_reader import (
    attach,
    OFFSETS,
    STORE_OFFSETS,
    GAME_STORE,
    save_game_store,
    apply_game_store,
    load_seed_game_store,
    save_seed_game_store,
    get_zone_name_index,
    ZONE_NAME_LOOKUP,
    ZONE_ID_ORDER,
    MISSION_LOCATION_IDS,
    MISSION_ACCESS_ITEM_IDS,
    ITEM_ID_TO_ZONE,
    MISSION_REQUIRES,
    missing_zone_items,
    secret_access_marker,
    set_secret_access_required,
    is_zone_truly_unlocked,
    get_pawn,
    get_spawned_attributes,
    HEALTH_ATTRIBUTE_SET_CLASS,
    kill_local_player,
    bridge_void_mode,
    call_is_mission_completed,
    IS_MISSION_COMPLETED_INDEX,
    IS_MISSION_COMPLETED_INDEX_FILE,
    # mission completion, primary path: MISSION_END_WIDGET_CLASS actually
    # appearing in GObjects. Proven reliable during standalone testing
    # (dungeons_reader.py's watch_mission_end tool, before this client was
    # unified) - absent on every tested failure, present on every tested
    # win - and unlike call_is_mission_completed above, doesn't depend on
    # OFFSETS["mission_progress_component"] (never actually found/set).
    MISSION_END_WIDGET_CLASS,
    get_gobjects_pointer_snapshot,
    get_uobject_class_name_index,
    _load_unlocked_zones,
    _save_unlocked_zones,
    UNLOCKED_ZONES_FILE,
    MISSION_PROGRESS_BASELINE_FILE,
    # mission completion (dungeons_bridge.dll MulticastMissionFinished /
    # OnShowMissionVictory / MulticastGameOver trigger - wakes up the
    # single IsMissionCompleted() confirm call below instead of polling it)
    get_mission_outcome_events,
    # boss kills
    get_death_events,
    PowerfulMobTracker,
    MapCountAnnouncer,
    get_secret_events,
    set_secret_block,
    resolve_boss_for_actor,
    BOSS_KILLS_FILE,
    # zone chests
    get_chest_open_events,
    classify_interactable_class,
    load_claimed_zone_chests,
    save_claimed_zone_chests,
    ZONE_CHESTS_FILE,
    # emeralds
    milestones_up_to,
    MILESTONES_FILE,
    # cumulative-earned tracking - milestones are checked against this,
    # NOT the live balance, so spending emeralds never costs a milestone
    # already earned (see update_emerald_earned_total's docstring)
    load_emerald_earned_state,
    save_emerald_earned_state,
    update_emerald_earned_total,
    EMERALD_EARNED_STATE_FILE,
    ITEM_ID_TO_EMERALD_AMOUNT,
    apply_emerald_reward,
    read_current_emeralds,
    test_unlock_any_secret_zone,
    apply_secret_zone_unlock,
    get_elevelname_value,
    _pick_any_secret_zone_name,
    pick_random_secret_zone_name,
    _save_unlock_secret_zone_index,
    UNLOCK_SECRET_ZONE_INDEX,
    load_applied_reward_indices,
    save_applied_reward_indices,
    APPLIED_REWARDS_FILE,
    load_enchant_slot_tier,
    save_enchant_slot_tier,
    load_pickup_tier,
    save_pickup_tier,
    ENCHANT_SLOT_TIER_FILE,
    PICKUP_TIER_FILE,
    # bridge pipe / item stash - needed here (not just inside give_item.py)
    # to run equipment/location reward grants off the main event-loop
    # thread; see _apply_next_pending_item_grant
    _connect_bridge_pipe,
    get_item_stash_component,
    # progressive pickups
    set_pickup_tier,
)
import dungeons_reader as dungeons_reader_module   # for the module-level LAST_OWN_UNLOCK_NOTIFY timestamp
import crash_diag   # mutation journal, process stats, safety gate, crash bundle (crash_diag.py)
# Feature modules (one file per subject, see each module's docstring):
from eye_of_ender import (EYE_OF_ENDER_ITEM_NAME, eye_of_ender_tick, push_eye_block,
                         grant_pending_eyes)
from mini_boss_checks import mini_boss_kill_tick, mini_boss_checks_active, mini_boss_zone_forced
from chest_tools import run_chests
import miniboss as miniboss_module   # /miniboss devtool (summon a powerful mob)
import effects as effects_module     # /effect devtool (freeze, slowness, potions... on the player)
import villagers as villagers_module # villager checks + /villager devtool (captured villager -> check, merchant item -> unlock)
import dll_health                    # DLL build check at attach + /crash devtool (crash_report.py reads the DLL's crash log)
import health_watch                  # freezes / black screen / stuck calls watchdog + /health devtool (not crashes)
import check_audit                   # every check sent, with its cause and flags for false positives + /audit devtool
import deathimg as deathimg_module   # custom picture instead of the skull on a DeathLink death screen
from _apworld_data import Locations as _apw, Items as _apw_items, ZoneData as _apw_zonedata
from apply_item_reward import (
    is_item_reward,
    apply_item_reward,
    is_location_reward,
    apply_location_reward,
    PROGRESSIVE_ENCHANT_SLOT_ITEM,
    MAX_ENCHANT_SLOT_TIER,
    PROGRESSIVE_PICKUP_ITEM,
    PICKUP_MAX_TIER,
)
from give_item import wait_for_stable_item_stash, get_best_displayed_power, PowerDropDetected, give_item
from item_lookup import items_by_category

# item_id -> item name, resolved from the AP world's own ITEM_TABLE (item
# IDs are allocated dynamically at generation time via Items.py's
# _alloc_id() counter - never hardcode these). Same resolution
# dungeons_reader.py's watch_item_rewards CLI mode already did, just
# sourced through _apworld_data's zip-safe loader instead of requiring a
# full `worlds.mcdungeons` package import.
ITEM_ID_TO_NAME = {info.code: name for name, info in _apw_items.ITEM_TABLE.items()}
ANCIENT_HUNT_ACCESS_ITEM_NAME = "Ancient Hunt Access"  # = Items.ANCIENT_HUNT_ACCESS_ITEM

# The mission every fresh save physically starts inside, before receiving
# any items - see the level-lock-enforcement exemption in game_watcher.
# Fixed by the base game itself, not something that varies per seed.
STARTING_ZONE = "squidcoast"

ITEM_GRANT_RETRY_COOLDOWN = 5.0  # seconds - see _apply_next_pending_item_grant

# Zones confirmed (via a real captured log - see the EndgameClicky handling
# below) to end via interacting with their own "EndgameClicky"-named actor
# rather than the normal MulticastMissionFinished/IsMissionCompleted
# confirmation flow ever actually resolving True. Deliberately an explicit,
# manually-confirmed allowlist rather than "any zone the interact resolves
# to" - the class name alone doesn't rule out the same Blueprint being
# reused elsewhere as an unrelated decorative prop, which would otherwise
# risk firing some OTHER zone's Mission Complete early. Add a zone here
# only once its own EndgameClicky interact has actually been observed in a
# real log, same as obsidianpinnacle was.
ENDGAME_CLICKY_ZONES = {"obsidianpinnacle"}

# --- dungeons_bridge.dll auto-injection --------------------------------
# Previously a fully separate manual step (run auto_inject.py yourself,
# against your own on-disk copy of dungeons_bridge.dll, before starting
# the client) - boss kills, chest events, AND the currency pointer (see
# get_currency_pointer's PIPE_NAME connection) all silently do nothing
# without it, which looked like three unrelated bugs (no chest checks,
# no boss checks, emeralds stuck at 0) but was really one missing step.
# Folded in here so the client does it itself.
# Returned (instead of a Win32 error) when the target PID is no longer a running
# Dungeons.exe - see _game_process_present.
GAME_GONE_MESSAGE = ("The game process is no longer running (it closed or crashed) - start the game "
                     "again; the client attaches and injects the DLL by itself.")


def _game_process_name():
    """The executable name of the CURRENT store's game process. It differs per
    store - Steam is "Dungeons-Win64-Shipping.exe", Microsoft Store and the
    Minecraft Launcher are "Dungeons.exe" (dungeons_reader.STORE_PROCESS_NAMES) -
    and /set_store can change it during a session, so it is read at call time
    from dungeons_reader, never hardcoded here or taken from auto_inject's
    own PROCESS_NAME (a fixed "Dungeons.exe" used only by its standalone CLI)."""
    import dungeons_reader
    return dungeons_reader.PROCESS_NAME


def _game_process_present(pid):
    """True only if `pid` is still a live game process (whatever the store's
    executable is called - see _game_process_name). Guards two ways the
    client can end up holding a stale PID: the game crashed/closed while the
    client stayed up (the tick loop only notices on its next liveness check),
    or Windows reused that PID for an unrelated process. Injecting into either
    fails with "Access denied during VirtualAllocEx (Win32 error 5)", whose
    generic advice (run as admin, antivirus...) is misleading in that case."""
    import auto_inject
    try:
        return (auto_inject.is_process_alive(pid)
                and pid in auto_inject.find_all_process_ids(_game_process_name()))
    except Exception:
        return True   # can't tell - don't hide a real injection attempt


def _ensure_bridge_dll_injected(pid):
    """Extracts dungeons_bridge.dll to DATA_DIR (it's bundled inside the
    .apworld's client/ folder, which - same issue as _apworld_data.py's
    zipimport fix - isn't a real on-disk file when running straight out
    of custom_worlds/mcdungeons.apworld; LoadLibraryA needs a real path,
    so extraction is required, not optional, whenever the client is a
    zip) then injects it if it isn't loaded in the target process yet.
    Safe to call every attach - both extraction and injection are no-ops
    if already done. Returns None on success, an error string otherwise
    (this is best-effort - the game watcher still runs without it, just
    with boss/chest/emerald tracking non-functional, same as before)."""
    import auto_inject

    dest_path = DATA_DIR / auto_inject.DLL_NAME

    # Check BEFORE writing, not after: Windows memory-maps a DLL into the
    # target process on LoadLibrary and holds a lock on that exact on-disk
    # file for as long as it stays loaded - which it does for the game's
    # whole lifetime, independent of whether OUR client process is still
    # running. Writing to dest_path unconditionally (as this used to)
    # meant that every time the client was reopened against a game that
    # was already injected from an earlier session - the normal case for
    # "close client, keep game running, relaunch client" - this raised an
    # unhandled sharing-violation OSError right here, which silently
    # killed the whole game_watcher task with no further ticks ever
    # running. Everything downstream (attach succeeding, the log line
    # printing) looked fine right up until this point, then nothing else
    # ever happened - exactly what "already injected -> nothing works"
    # looks like from the outside. Only touch the file at all when we're
    # actually about to inject it.
    if auto_inject.is_dll_loaded(pid, auto_inject.DLL_NAME):
        return None

    if not _game_process_present(pid):
        return GAME_GONE_MESSAGE

    src_path = str(Path(__file__).resolve().parent / auto_inject.DLL_NAME)
    dll_bytes = __loader__.get_data(src_path)
    dest_path.write_bytes(dll_bytes)

    try:
        auto_inject.inject_dll(pid, str(dest_path))
        return None
    except Exception as e:
        # The game may have died between the check above and the attempt (a
        # crash leaves the process half-alive for a moment - VirtualAllocEx
        # then reports "access denied"). Say what actually happened.
        if not _game_process_present(pid):
            return GAME_GONE_MESSAGE
        return str(e)


GAME_POLL_INTERVAL = 0.2       # seconds - matches the old client's tick rate
# Devtool mapping powerful mobs, passively while you play: logs which powerful mobs (mini bosses)
# are present / killed in each zone into dungeons_bridge_debug.log and powerful_mobs_seen.json.
# Purely observational - it never sends a check. Set to False to switch it off.
POWERFUL_MOB_LOGGING = True
# Zone mini boss: when the player opens the LAST chest check of a zone (ZONE_CHEST_COUNTS) and no powerful
# mob has been beaten in that zone this run, summon the zone's own mini boss (miniboss.ZONE_MINIBOSSES)
# above that chest. Needs POWERFUL_MOB_LOGGING (that is what tells whether a mini boss was beaten).
# Set to False to switch it off.
ZONE_MINIBOSS_ENABLED = True
# Devtool mapping chest count, at the start of every mission zone: logs the known chest count of the
# zone, then the chest/secret total read off the in-game map (dungeons_bridge_debug.log + client log).
# Purely observational. Set to False to switch it off.
ZONE_START_CHEST_COUNT_LOGGING = True
PAWN_ANNOUNCE_DEBOUNCE = 2      # consecutive identical get_pawn readings needed before the
                               # client announces a change in character presence (filters out
                               # a single transient bad read so the log doesn't flicker)
EMERALD_POLL_INTERVAL = 2.0    # currency reads are cheap but no need every tick
ATTACH_RETRY_INTERVAL = 3.0    # how often to retry finding Dungeons.exe if not attached yet
PROCESS_STARTUP_GRACE_PERIOD = 0.0   # (was 12.0 - REMOVED, see call site)  # seconds to wait after first finding a NEW Dungeons.exe
                                     # process before injecting - see the call site's own
                                     # comment for why ("client before game" crash history).
                                     # Raised from 5.0 -> 12.0 after a real crash report
                                     # (client already running/connected before the game was
                                     # launched) STILL crashed within ~7-8s of injection with
                                     # the 5.0 value.
                                     #
                                     # UPDATE: the actual fix for that crash history turned out
                                     # to be PAWN_STABILITY_* below, not this constant - a
                                     # confirmed-more-stable earlier version of this client had
                                     # NO grace period here at all (injects immediately) and
                                     # didn't have this crash, because what was actually unsafe
                                     # wasn't the injection itself but the startup mission-
                                     # completion sweep making real remote calls into the game
                                     # before a Pawn existed. Left at 12.0 anyway as a harmless
                                     # secondary margin, not because it's confirmed necessary -
                                     # if it turns out to just be pure added delay with no
                                     # stability benefit, lowering it back down is reasonable.
PAWN_STABILITY_ATTEMPTS = 15       # max get_pawn polls before giving up on the startup sweep
PAWN_STABILITY_MATCHES = 3         # consecutive identical non-null pawn reads required
PAWN_STABILITY_POLL_INTERVAL = 0.5 # seconds between polls (~7.5s worst case before giving up)
DEATH_LINK_COOLDOWN = 10.0
MISSION_POPUP_SPACING = 4.0   # seconds between native 'mission unlocked' popups

# Separate logger/tab for "what's happening in the game right now" (zone,
# health, attach status) so it doesn't drown out the normal Archipelago
# item/connection log. Appended to logging_pairs in run_gui() below - this
# is the same officially-supported extension point CommonContext's own
# make_gui() docstring describes for adding a tab ("logging_pairs.append(
# ("Foo", "Bar")) will add a Bar tab which follows logger 'Foo'").
game_logger = logging.getLogger("MCDungeonsGame")


GIVE_SAFE_MAX_RETRIES = 12  # * ITEM_GRANT_RETRY_COOLDOWN (5s) = 1 minute total -
                             # long enough to ride out a loading screen or a
                             # menu detour, short enough that a forgotten
                             # /give_safe from an ended session doesn't retry
                             # forever in the background.


def _give_safe_prepare(ctx):
    """Phase 1 of /give_safe - everything that does NOT need the bridge
    pipe (loading check, pawn, waiting for a stable item_stash - the
    latter can take up to LOADING_MAX_WAIT seconds). Kept separate so it
    can run WITHOUT holding ctx.bridge_pipe_lock: holding the pipe lock
    through a long memory-only wait would stall every other pipe user
    (chest polling, real item grants) for nothing.

    Returns (failure, item_stash, item_stash_class) where failure is
    either None (proceed) or a ready-made (success, retryable, message)
    tuple."""
    pm, base = ctx.pm, ctx.base

    if ctx.currently_loading:
        return (False, True, ("Refusing to grant: a zone transition/loading screen is in progress "
                               "right now. This is exactly the window where a write can silently "
                               "corrupt the inventory - will retry automatically in a few seconds.")), None, None
    _grant_ok, _grant_why = crash_diag.mutations_allowed()
    if not _grant_ok:
        return (False, True, (f"Holding the grant: {_grant_why}. It stays queued and is retried "
                               f"automatically in a few seconds.")), None, None

    _, pawn_error = get_pawn(pm, base)
    if pawn_error:
        return (False, True, (f"Refusing to grant: player isn't in a level right now ({pawn_error}). "
                               f"Will retry automatically in a few seconds.")), None, None

    try:
        item_stash, item_stash_class = wait_for_stable_item_stash(pm, base)
    except RuntimeError as e:
        return (False, True, f"Refusing to grant: {e} Will retry automatically in a few seconds."), None, None

    return None, item_stash, item_stash_class


def _give_safe_grant(ctx, item_stash, item_stash_class):
    """Phase 2 of /give_safe - the part that actually uses the bridge
    pipe. MUST be called with ctx.bridge_pipe_lock held (see
    _attempt_give_safe_async). Returns (success, retryable, message)."""
    pm = ctx.pm

    import win32file
    pipe = _connect_bridge_pipe(pm)
    try:
        try:
            get_best_displayed_power(pm, pipe, item_stash, item_stash_class, check_for_drop=True)
        except PowerDropDetected as e:
            return False, True, f"ABORTED - {e} Will retry automatically in a few seconds."

        category = random.choice(["Melee", "Ranged"])
        # exclude seasonal (DLC-exclusive) items - a random grant
        # shouldn't be able to hand out something the player might
        # not even own the season pass for.
        candidates = items_by_category(category, include_seasonal=False)
        if not candidates:
            return False, False, f"No {category} items available in item_lookup.py's ITEM_TABLE."
        name_index, item_name = random.choice(candidates)

        granted_power = give_item(pm, pipe, item_stash, item_stash_class, name_index)
        return True, False, f"Granted random weapon: {item_name} ({category}) at power={granted_power:.1f}"
    finally:
        win32file.CloseHandle(pipe)


def _attempt_give_safe(ctx):
    """One attempt at /give_safe's grant, fully synchronous (both phases
    back to back, no locking) - kept for any direct/CLI caller. The
    client's own command uses _attempt_give_safe_async below instead.
    Returns (success, retryable, message) - retryable=True for the
    specific TEMPORARY conditions (loading screen, no Pawn, item_stash
    not yet stable, a power drop that might just be transient
    loading-screen instability) that are worth trying again shortly;
    False for anything else (no items in the category, capacity full,
    etc - a real problem retrying won't fix)."""
    failure, item_stash, item_stash_class = _give_safe_prepare(ctx)
    if failure:
        return failure
    return _give_safe_grant(ctx, item_stash, item_stash_class)


async def _attempt_give_safe_async(ctx):
    """What /give_safe actually runs. Both phases go through
    asyncio.to_thread so the event loop (chest polling, the AP websocket
    connection itself) is NEVER blocked - /give_safe used to run
    synchronously right on the event loop, where a slow
    wait_for_stable_item_stash (up to LOADING_MAX_WAIT = 120s) froze the
    whole client. Only phase 2 holds ctx.bridge_pipe_lock, so it can't
    overlap the per-tick pipe polling or a real item grant (the pipe is
    single-instance - see ctx.bridge_pipe_lock). Any pipe/OS error is
    turned into a normal retryable failure message instead of an
    unhandled exception (which used to surface only as asyncio's
    "Task exception was never retrieved")."""
    try:
        failure, item_stash, item_stash_class = await asyncio.to_thread(_give_safe_prepare, ctx)
        if failure:
            return failure
        async with ctx.bridge_pipe_lock:
            return await asyncio.to_thread(_give_safe_grant, ctx, item_stash, item_stash_class)
    except Exception as e:
        return False, True, (f"give_safe hit an error talking to the game ({type(e).__name__}: {e}) - "
                              f"will retry automatically in a few seconds.")


async def _drain_mission_outcome_events_locked(ctx):
    """Fire-and-forget helper for the "discard stale outcome events on a
    fresh mission attempt" drain in game_watcher - the result is
    discarded either way, but the pipe call itself still needs
    ctx.bridge_pipe_lock like every other call site (single-instance
    pipe), which a bare asyncio.create_task(asyncio.to_thread(...))
    can't provide on its own."""
    try:
        async with ctx.bridge_pipe_lock:
            await asyncio.to_thread(get_mission_outcome_events, ctx.pm)
    except Exception:
        pass


async def _retry_give_safe_until_done(ctx, output):
    """Background retry loop for a /give_safe call that failed for a
    retryable (temporary) reason - keeps trying every
    ITEM_GRANT_RETRY_COOLDOWN seconds, up to GIVE_SAFE_MAX_RETRIES times,
    without blocking anything else the client is doing (chest/boss/
    emerald polling, real AP reward grants, etc all keep running
    normally in between). Stops as soon as a grant succeeds, a
    non-retryable failure happens, or the retry cap is hit."""
    for attempt in range(1, GIVE_SAFE_MAX_RETRIES + 1):
        await asyncio.sleep(ITEM_GRANT_RETRY_COOLDOWN)
        if not ctx.pm or not ctx.base:
            output("give_safe retry stopped: no longer attached to Dungeons.exe.")
            return
        success, retryable, message = await _attempt_give_safe_async(ctx)
        output(f"[give_safe retry {attempt}/{GIVE_SAFE_MAX_RETRIES}] {message}")
        if success or not retryable:
            return
    output(f"give_safe: gave up after {GIVE_SAFE_MAX_RETRIES} retries - try the command again by hand.")


async def _run_give_safe(ctx, output):
    """Body of the /give_safe command (runs as its own task - the command
    handler itself is synchronous and must return immediately)."""
    success, retryable, message = await _attempt_give_safe_async(ctx)
    output(message)
    if not success and retryable:
        await _retry_give_safe_until_done(ctx, output)


def _zone_run_key(ctx):
    """Identity of the current zone visit: the powerful-mob tracker starts a new run object on every zone change
    (hub included), so entering the same zone twice gives two different keys."""
    tracker = getattr(ctx, "powerful_tracker", None)
    run = getattr(tracker, "run", None) if tracker is not None else None
    return id(run) if run is not None else None


def _zone_mb_state(ctx, zone):
    run_key = _zone_run_key(ctx)
    st = getattr(ctx, "zone_mb", None)
    if st is None or st["zone"] != zone or st["run_key"] != run_key:
        st = ctx.zone_mb = {"zone": zone, "run_key": run_key, "chests": set(), "spawned": False, "noted": False}
    return st


def _zone_seed_key(ctx):
    return f"{getattr(ctx, '_room_seed_name', None)}:{getattr(ctx, 'auth', None)}"


async def zone_miniboss_on_chest(ctx, zone, event):
    """Called for every opened chest (kind 'chest' or 'supply'). Remembers it as the last chest of this run (for
    /miniboss atchest) and, when it is the last CHEST CHECK of the zone, applies the zone mini boss rule:
      a mini boss already beaten in this zone this run?  yes -> nothing.
      no -> summon the zone's chosen mini boss above this chest."""
    tracker = getattr(ctx, "powerful_tracker", None)
    run_key = _zone_run_key(ctx)
    ctx.last_chest_event = dict(event, zone=zone, run_key=run_key)
    if not ZONE_MINIBOSS_ENABLED or tracker is None or getattr(tracker, "run", None) is None:
        return
    if not mini_boss_zone_forced(ctx, zone):
        return                              # only seeds with per_zone / both force a mini boss into the zone
    if classify_interactable_class(event["class_name"]) != "chest":
        return                              # supply stations are not "chest checks"
    st = _zone_mb_state(ctx, zone)
    if st["spawned"] or event["actor_addr"] in st["chests"]:
        return
    st["chests"].add(event["actor_addr"])
    total = _apw.ZONE_CHEST_COUNTS.get(zone, (0, 0))[0]
    alias = miniboss_module.zone_miniboss(zone, _zone_seed_key(ctx))
    if total <= 0 or alias is None:
        if not st["noted"]:
            st["noted"] = True
            why = "no chest checks are set up for this zone" if total <= 0 else "no mini boss is listed for this zone"
            game_logger.info(f"Zone mini boss [{zone}]: {why} - nothing will be summoned here.")
        return
    game_logger.info(f"Zone mini boss [{zone}]: chest {len(st['chests'])}/{total}")
    if len(st["chests"]) < total:
        return
    st["spawned"] = True                    # decided once per run, whatever happens next
    killed = sorted(tracker.run.get("killed_types", ()))
    if killed:
        game_logger.info(f"Zone mini boss [{zone}]: last chest reached, but a mini boss was already beaten here "
                         f"this run ({', '.join(killed)}) - nothing summoned.")
        return
    game_logger.info(f"Zone mini boss [{zone}]: last chest reached and no mini boss beaten yet - "
                     f"summoning {miniboss_module.MINIBOSS_TYPES[alias][0]}.")
    await _summon_zone_miniboss(ctx, alias, ctx.last_chest_event)


async def _summon_zone_miniboss(ctx, alias, chest_event):
    if not ctx.pm or not ctx.base:
        game_logger.info("Zone mini boss: not attached to Dungeons.exe.")
        return
    chest_event = dict(chest_event or {}, fallback_player=True)
    try:
        async with ctx.bridge_pipe_lock:
            lines = await asyncio.to_thread(miniboss_module.run_miniboss, ctx.pm, ctx.base, "atchest", alias, "",
                                            game_logger.info, chest_event)
    except Exception as e:
        game_logger.info(f"Zone mini boss: error talking to the game ({type(e).__name__}: {e}).")
        return
    for line in lines:
        game_logger.info(line)


async def _zone_miniboss_command(ctx, output, option):
    """/miniboss zone [now]: the zone's mini boss and where this run stands; 'now' summons it right away above
    the last chest (test the placement without opening every chest)."""
    zone = ctx.game_state.get("zone")
    if not zone or zone == "hub":
        output("Zone mini boss: you are not in a mission zone.")
        return
    seed_key = _zone_seed_key(ctx)
    pool = miniboss_module.ZONE_MINIBOSSES.get(zone, [])
    alias = miniboss_module.zone_miniboss(zone, seed_key)
    total = _apw.ZONE_CHEST_COUNTS.get(zone, (0, 0))[0]
    st = _zone_mb_state(ctx, zone)
    tracker = getattr(ctx, "powerful_tracker", None)
    killed = sorted(tracker.run.get("killed_types", ())) if tracker is not None and tracker.run else []
    output(f"Zone mini boss [{zone}]: chosen = {alias or 'none listed'} (pool: {', '.join(pool) or 'empty'}); "
           f"chest checks known = {total or 'none'}; opened this run = {len(st['chests'])}/{total}; "
           f"beaten this run = {', '.join(killed) or 'no'}; summoned this run = {'yes' if st['spawned'] else 'no'}; "
           f"trigger {'ON' if ZONE_MINIBOSS_ENABLED else 'OFF'}.")
    if miniboss_module.normalize(option) == "now":
        if not alias:
            output("Zone mini boss: nothing is listed for this zone.")
            return
        st["spawned"] = True
        await _summon_zone_miniboss(ctx, alias, getattr(ctx, "last_chest_event", None))


async def _run_miniboss(ctx, output, name, extra, extra2):
    """Body of the /miniboss devtool (runs as its own task - the command handler itself is
    synchronous). Holds ctx.bridge_pipe_lock for the whole run because the bridge pipe is
    single-instance and miniboss.py makes several round trips; the work itself goes through
    asyncio.to_thread so the event loop (AP connection, chest polling) is never blocked."""
    if not ctx.pm or not ctx.base:
        output("Not attached to Dungeons.exe yet.")
        return
    if miniboss_module.normalize(name) == "zone":
        await _zone_miniboss_command(ctx, output, extra)
        return
    chest_event = getattr(ctx, "last_chest_event", None)
    if chest_event and chest_event.get("run_key") != _zone_run_key(ctx):
        chest_event = None                  # recorded in an earlier zone visit: its address means nothing now
    try:
        async with ctx.bridge_pipe_lock:
            lines = await asyncio.to_thread(miniboss_module.run_miniboss, ctx.pm, ctx.base, name, extra, extra2, output,
                                            chest_event)
    except Exception as e:
        output(f"miniboss hit an error talking to the game ({type(e).__name__}: {e}).")
        return
    for line in lines:
        output(line)


async def _run_effect(ctx, output, name, extra, extra2):
    """Body of the /effect devtool: same pattern as _run_miniboss (own task, bridge pipe lock held,
    the blocking work goes through asyncio.to_thread so the event loop is never blocked)."""
    if not ctx.pm or not ctx.base:
        output("Not attached to Dungeons.exe yet.")
        return
    try:
        async with ctx.bridge_pipe_lock:
            lines = await asyncio.to_thread(effects_module.run_effect, ctx.pm, ctx.base, name, extra, extra2, output)
    except Exception as e:
        output(f"effect hit an error talking to the game ({type(e).__name__}: {e}).")
        return
    for line in lines:
        output(line)


async def _run_crash(ctx, output, sub):
    """Body of the /crash devtool. Reading the crash log needs no game; the live parts go through the bridge pipe."""
    try:
        if ctx.pm and sub.strip().lower() in ("crumbs", "build", "test"):
            async with ctx.bridge_pipe_lock:
                lines = await asyncio.to_thread(dll_health.run_crash, ctx.pm, sub)
        elif sub.strip().lower() in ("", "help", "?"):
            lines = await asyncio.to_thread(dll_health.run_crash, ctx.pm, sub)
        else:
            lines = ["Unknown /crash command - type /crash help."]
        if not ctx.pm and sub.strip().lower() in ("crumbs", "build", "test"):
            lines = ["Not attached to Dungeons.exe yet."]
    except Exception as e:
        lines = [f"crash hit an error ({type(e).__name__}: {e})."]
    for line in lines:
        output(line)


async def _run_villager(ctx, output, name, extra):
    """Body of the /villager devtool: own task, bridge pipe lock held, blocking work in a worker thread."""
    if not ctx.pm or not ctx.base:
        output("Not attached to Dungeons.exe yet.")
        return
    try:
        async with ctx.bridge_pipe_lock:
            lines = await asyncio.to_thread(villagers_module.run_villager, ctx, ctx.pm, ctx.base, name, extra, output)
    except Exception as e:
        output(f"villager hit an error talking to the game ({type(e).__name__}: {e}).")
        return
    for line in lines:
        output(line)


async def _run_death_image(ctx, output=None, custom_path=None, attempts=6):
    """Puts the custom picture on the death screen's skull. Runs as its own task: the pipe lock is
    held only for the game calls, the work itself goes through asyncio.to_thread. Retries a few times
    (the widget may not exist yet right after a level change); never raises."""
    say = output or game_logger.info
    last_error = None
    for attempt in range(attempts):
        if not ctx.pm or not ctx.base:
            return
        try:
            async with ctx.bridge_pipe_lock:
                line = await asyncio.to_thread(deathimg_module.apply, ctx.pm, ctx.base, custom_path)
            say(f"Death screen: {line}")
            return
        except Exception as e:
            last_error = e
            await asyncio.sleep(1.0)
    say(f"Death screen picture not applied ({type(last_error).__name__}: {last_error}).")


async def _run_deathimg_info(ctx, output):
    if not ctx.pm or not ctx.base:
        output("Not attached to Dungeons.exe yet.")
        return
    try:
        async with ctx.bridge_pipe_lock:
            lines = await asyncio.to_thread(deathimg_module.info, ctx.pm, ctx.base)
    except Exception as e:
        output(f"deathimg info failed ({type(e).__name__}: {e}).")
        return
    for line in lines:
        output(line)


async def _run_chest_icon(ctx, output, custom_path=None):
    if not ctx.pm or not ctx.base:
        output("Not attached to Dungeons.exe yet.")
        return
    try:
        async with ctx.bridge_pipe_lock:
            line = await asyncio.to_thread(deathimg_module.apply_map_chest_icon, ctx.pm, ctx.base, custom_path)
        output(f"Map: {line}")
    except Exception as e:
        output(f"Map chest icon not applied ({type(e).__name__}: {e}).")


async def _run_menu_logo(ctx, output, custom_path=None):
    if not ctx.pm or not ctx.base:
        output("Not attached to Dungeons.exe yet.")
        return
    try:
        async with ctx.bridge_pipe_lock:
            line = await asyncio.to_thread(deathimg_module.apply_menu_logo, ctx.pm, ctx.base, custom_path)
        output(f"Menu: {line}")
    except Exception as e:
        output(f"Menu logo not applied ({type(e).__name__}: {e}).")


class MCDungeonsCommandProcessor(ClientCommandProcessor):
    # Commands here still work exactly normally when typed (dispatch is by
    # name via getattr(self, f"_cmd_{name}"), untouched by this) - they're
    # just left out of the /help listing below, so an ordinary player
    # doesn't see them and doesn't get the idea to poke at manual item
    # grants during normal play. Still there and fully functional for
    # troubleshooting a specific problem (e.g. "run /give_safe once as a
    # quick capacity/power sanity check") without exposing that as a
    # normal, everyday interaction.
    HIDDEN_FROM_HELP = {"give_safe"}

    def get_help_message(self):
        text = super().get_help_message()
        for name in self.HIDDEN_FROM_HELP:
            # Matches "/name ...\n    <docstring lines>\n" up to (but not
            # including) the next command's "/something" line, or the end
            # of the text - same block shape get_help_message itself
            # builds per command, confirmed against a real /help capture.
            text = re.sub(rf"/{re.escape(name)} .*?(?=\n/\w|\Z)", "", text, flags=re.DOTALL)
        return text

    def _cmd_status(self):
        """Show current game state (zone, health, attach status)."""
        if isinstance(self.ctx, MCDungeonsContext):
            gs = self.ctx.game_state
            self.output(f"Game attached: {gs['attached']}")
            self.output(f"Zone: {gs['zone']}")
            if self.ctx.pawn_present is None:
                self.output("Character: not checked yet")
            elif self.ctx.pawn_present:
                self.output(f"Character: present (for {time.time() - self.ctx.pawn_state_since:.0f}s)")
            else:
                self.output(f"Character: ABSENT - {self.ctx.pawn_last_reason} "
                             f"(for {time.time() - self.ctx.pawn_state_since:.0f}s)")
            if gs["health"] is not None:
                self.output(f"Health: {gs['health']:.0f}/{gs['max_health']:.0f}")
            self.output(f"Boss kills claimed: {len(self.ctx.boss_kills_claimed)}")
            self.output(f"Emeralds: {gs['emeralds']}")

    def _cmd_unlocked(self):
        """List zones unlocked so far this run."""
        if isinstance(self.ctx, MCDungeonsContext):
            self.output(f"Unlocked zones: {sorted(self.ctx.unlocked_zones)}")

    def _cmd_debug_interacts(self):
        """Devtool to get information on what the player interacted with"""
        if isinstance(self.ctx, MCDungeonsContext):
            self.ctx.debug_interact_logging = not self.ctx.debug_interact_logging
            self.output(f"Interact debug logging: {'ON' if self.ctx.debug_interact_logging else 'OFF'}")

    def _cmd_void(self, mode: str = ""):
        """Devtool - DO NOT TOUCH, changing it can crash the game"""
        if not isinstance(self.ctx, MCDungeonsContext) or getattr(self.ctx, "pm", None) is None:
            self.output("Not attached to the game yet.")
            return
        mode = mode.strip().lower()
        if mode not in ("", "soft", "hard"):
            self.output("WARNING: devtool - do not touch, changing the void mode can crash the game.")
            self.output("Usage: /void [soft|hard]   (no argument = show current state)")
            self.output("  soft: hook stays patched in, does nothing during transitions")
            self.output("  hard (default): ProcessEvent is physically unpatched for the whole transition window")
            return
        ok, resp = bridge_void_mode(self.ctx.pm, mode or None)
        if ok and mode:
            save_void_mode(mode)   # remembered for the next launches
            self.output("WARNING: devtool - changing the void mode can crash the game. "
                        "Set it back to 'hard' (the default) if you are not sure.")
        self.output(f"Void mode: {resp}" if ok else f"Void mode request failed: {resp}")

    def _cmd_guard(self, mode: str = ""):
        """Devtool - show / switch the DLL crash guard (on|off). Off = the game really crashes at the first fault."""
        if not isinstance(self.ctx, MCDungeonsContext) or getattr(self.ctx, "pm", None) is None:
            self.output("Not attached to the game yet.")
            return
        mode = mode.strip().lower()
        if mode not in ("", "on", "off"):
            self.output("Usage: /guard [on|off]   (no argument = show the current state)")
            return
        if mode:
            ok, resp = dungeons_reader_module.bridge_set_crash_guard(self.ctx.pm, mode)
            if not ok:
                self.output(f"Crash guard request failed: {resp}")
                return
            if mode == "off":
                self.output("Crash guard OFF (diagnostic): the game will now really crash at the first bad read, which "
                            "gives a full Unreal crash report with the exact call stack. Turn it back on with /guard on.")
        stats = dungeons_reader_module.bridge_crash_guard_stats(self.ctx.pm)
        if stats is None:
            self.output("Crash guard state unavailable (old DLL?).")
        else:
            self.output(f"Crash guard: {'ON' if stats['enabled'] else 'OFF'}, {stats['sites']} patched site(s), "
                        f"{stats['hits']} null read(s) recovered so far.")

    def _cmd_set_store(self, channel: str = ""):
        """set the game store for THIS seed (steam/minecraft_launcher/microsoft_store)"""
        seed_dir = getattr(self.ctx, "seed_dir", None)
        if not channel:
            self.output(f"Usage: /set_store <{'|'.join(STORE_OFFSETS)}>  (saved per seed)")
            self.output(f"Currently in use: '{dungeons_reader_module.GAME_STORE}'")
            if seed_dir:
                saved = load_seed_game_store(seed_dir)
                self.output(f"Saved for this seed with /set_store: {saved or 'nothing (the YAML option is used)'}")
            return
        if channel not in STORE_OFFSETS:
            self.output(f"Unknown store '{channel}' - must be one of {list(STORE_OFFSETS)}")
            return
        if not seed_dir:
            self.output("Connect to your slot first: the store is saved per seed.")
            return
        save_seed_game_store(seed_dir, channel)
        if getattr(self.ctx, "pm", None):
            if channel != dungeons_reader_module.GAME_STORE:
                self.output(f"Store set to '{channel}' for this seed. The game is already attached with "
                             f"'{dungeons_reader_module.GAME_STORE}': restart the client (and the game/DLL) "
                             "for it to take effect.")
            else:
                self.output(f"Store set to '{channel}' for this seed.")
        else:
            apply_game_store(channel)
            self.output(f"Store set to '{channel}' for this seed and applied now.")
        if channel != "steam":
            self.output("NOTE: this channel's offsets are still UNCONFIRMED placeholders "
                         "copied from steam - a fresh Dumper-7 dump for this build is "
                         "needed to actually calibrate them.")

    def _cmd_reset_progress(self):
        """Safety feature if a chest is skipped - shouldn't happen normally"""
        if isinstance(self.ctx, MCDungeonsContext):
            self.ctx._wipe_current_seed_progress()
            self.output("Local progress tracking cleared (unlocked zones, chests, "
                         "bosses, milestones, rewards, emerald total).")

    def _cmd_give_safe(self):
        """Only use it if the game clears your inventory - it shouldn't
        happen, but again, it's for safety
        """
        if not isinstance(self.ctx, MCDungeonsContext):
            return
        ctx = self.ctx
        if not ctx.pm or not ctx.base:
            self.output("Not attached to Dungeons.exe yet.")
            return

        # Runs as a background task: the command handler itself is
        # synchronous, and the grant (memory waits + pipe round trips)
        # must never run on - and freeze - the event loop.
        self.output("give_safe: working on it...")
        async_start(_run_give_safe(ctx, self.output), name="GiveSafe")

    def _cmd_img(self, kind: str = "", *rest: str):
        """Devtool reload image and make it so you can change the path for the img"""
        if not isinstance(self.ctx, MCDungeonsContext):
            return
        ctx = self.ctx
        kind = (kind or "").strip().lower()
        arg = " ".join(rest).strip().strip('"')          # a path may contain spaces
        low = arg.lower()
        if kind in ("death", "deathimg") and low in ("on", "off", "info"):
            if low == "info":
                async_start(_run_deathimg_info(ctx, self.output), name="DeathImgInfo")
            else:
                ctx.deathimg_enabled = (low == "on")
                self.output("Death screen picture: ON (applied after each received DeathLink)." if low == "on" else
                            "Death screen picture: OFF (a death screen already changed stays until the level changes).")
        elif kind in ("death", "deathimg"):
            if arg:
                ctx.deathimg_path = arg
                self.output(f"Death screen picture file: {arg}")
            async_start(_run_death_image(ctx, self.output, ctx.deathimg_path, attempts=1), name="DeathImgReload")
        elif kind in ("chest", "chesticon", "icon"):
            async_start(_run_chest_icon(ctx, self.output, arg or None), name="ChestIcon")
        elif kind in ("logo", "logoimg"):
            async_start(_run_menu_logo(ctx, self.output, arg or None), name="MenuLogo")
        elif kind == "all":
            async_start(_run_death_image(ctx, self.output, ctx.deathimg_path, attempts=1), name="DeathImgReload")
            async_start(_run_chest_icon(ctx, self.output, None), name="ChestIcon")
            async_start(_run_menu_logo(ctx, self.output, None), name="MenuLogo")
        else:
            state = "ON" if ctx.deathimg_enabled else "OFF"
            self.output("/img death [file]  reload the death screen picture (optionally from another file); "
                        f"currently {state}, also: /img death on | off | info")
            self.output("/img chest [file]  reload the chest icon of the in-game map (open the map in a mission first)")
            self.output("/img logo [file]   reload the game logo of the main menu (run it while the main menu is on screen)")
            self.output("/img all           reload the three with their default files")

    def _cmd_miniboss(self, name: str = "", extra: str = "", extra2: str = ""):
        """Devtool try to summon a mini boss"""
        if not isinstance(self.ctx, MCDungeonsContext):
            return
        async_start(_run_miniboss(self.ctx, self.output, name, extra, extra2), name="Miniboss")

    def _cmd_health(self):
        """Detect freeze, black screen or if the game is stuck (this command only shows the process since it's always active)"""
        if not isinstance(self.ctx, MCDungeonsContext):
            return
        for line in health_watch.status_lines(self.ctx):
            self.output(line)

    def _cmd_audit(self, what: str = ""):
        """List the last 15 checks and the reason why they were sent"""
        if not isinstance(self.ctx, MCDungeonsContext):
            return
        for line in check_audit.audit_lines(self.ctx, only_flagged=what.strip().lower() in ("flagged", "suspects", "bad")):
            self.output(line)

    def _cmd_crash(self, sub: str = ""):
        """Give the latest calls before the crash to find a culprit"""
        if not isinstance(self.ctx, MCDungeonsContext):
            return
        async_start(_run_crash(self.ctx, self.output, sub), name="Crash")

    def _cmd_effect(self, name: str = "", extra: str = "", extra2: str = ""):
        """Devtool not implemented yet wait for v0.0.4"""
        if not isinstance(self.ctx, MCDungeonsContext):
            return
        self.output("/effect is not implemented yet, wait for v0.0.4.")


SWEEP_PROBE_VALUE = 0xFF                 # not a real ELevelNames value: the real IsMissionCompleted says False
SWEEP_NEVER_FIRE_ZONES = {"obsidianpinnacle"}   # the goal mission is only ever sent from a live completion

UNCONFIRMED_CHESTS_FILE = "dlc_chest_counts_seen.json"   # in the seed's progress folder


def _note_unconfirmed_chest(ctx, zone_name, kind, class_name):
    """A chest/supply chest was opened in a zone that has no chest checks set up (not in
    ZONE_CHEST_COUNTS - typically a DLC zone whose chests are still to be measured). It is NOT sent
    as a check; instead keep a tally to measure the mission: the count of this run, and the highest
    count ever seen per zone (dlc_chest_counts_seen.json, chests reset on every run of a mission).
    After clearing a whole mission, the logged numbers go into Locations.DLC_ZONE_CHEST_COUNTS."""
    if not zone_name or zone_name == "hub":
        return
    run = getattr(ctx, "unconfirmed_chest_run", None)
    if not run or run.get("zone") != zone_name:
        run = ctx.unconfirmed_chest_run = {"zone": zone_name, "chest": 0, "supply": 0}
    run["supply" if kind == "supply" else "chest"] += 1
    seen = {}
    try:
        with open(UNCONFIRMED_CHESTS_FILE, encoding="utf-8") as f:
            seen = json.load(f)
    except (OSError, ValueError):
        pass
    best = seen.setdefault(zone_name, {"chest": 0, "supply": 0})
    best["chest"] = max(best.get("chest", 0), run["chest"])
    best["supply"] = max(best.get("supply", 0), run["supply"])
    try:
        with open(UNCONFIRMED_CHESTS_FILE, "w", encoding="utf-8") as f:
            json.dump(seen, f, indent=2, sort_keys=True)
    except OSError as e:
        game_logger.info(f"Could not save {UNCONFIRMED_CHESTS_FILE}: {e}")
    game_logger.info(
        f"Chest tally for '{zone_name}' (no chest checks set up yet, nothing sent): this run "
        f"{run['chest']} chest(s) + {run['supply']} supply chest(s); most seen {best['chest']} + "
        f"{best['supply']}. After clearing the whole mission, add \"{zone_name}\": "
        f"({best['chest']}, {best['supply']}) to DLC_ZONE_CHEST_COUNTS in Locations.py "
        f"[{class_name}].")


def _own_world_version() -> str:
    """Version of the apworld this client is running from (archipelago.json at the
    apworld root; read zip-aware like map_tab does). "unknown" if unreadable."""
    try:
        import json as _json
        from pathlib import Path as _Path
        manifest_path = _Path(__file__).resolve().parents[1] / "archipelago.json"
        try:
            raw = __loader__.get_data(str(manifest_path))   # inside a .apworld
        except Exception:
            raw = manifest_path.read_bytes()                # unpacked folder
        return str(_json.loads(raw)["world_version"])
    except Exception:
        return "unknown"


class MCDungeonsContext(CommonContext):
    game = "Minecraft Dungeons"
    items_handling = 0b111  # everything: other players' sends, our own echoed back, starting inventory
    command_processor = MCDungeonsCommandProcessor

    async def check_locations(self, locations):
        """Every check goes through here: the audit records who asked for it and flags what looks off (check_audit.py)."""
        check_audit.record(self, locations)
        sent = await super().check_locations(locations)
        # CommonContext.send_msgs() silently drops everything while the server connection is down,
        # and a boss kill / chest event only fires once in game - so a check made during a server
        # outage was lost for good (the player had to redo the mission). Remember what we sent in
        # ctx.locations_checked: the base client re-sends that whole set on every (re)connect, and
        # the server ignores duplicates.
        if sent:
            self.locations_checked.update(sent)
        return sent

    def __init__(self, server_address, password):
        super().__init__(server_address, password)
        crash_diag.EXTRA_SUMMARY[:] = [lambda: ["HEALTH EVENTS (newest last):"] + (health_watch.recent_events(self, 25) or ["  none"]),
                                       lambda: ["SUSPICIOUS CHECKS:"] + (check_audit.suspects(self) or ["  none"])]

        # memory attach state - filled in by game_watcher once Dungeons.exe is found
        self.pm = None
        self.base = None

        # Off by default - toggled via /debug_interacts. Logs every
        # interact event that ISN'T a chest/supply chest (unrecognized
        # actor classes, or a chest resolved to a zone missing from
        # ZONE_CHEST_COUNTS) as "Interacted with 'ClassName_C' (not
        # tracked)". Genuinely useful when diagnosing a missing check,
        # but pure noise for normal play (a raid boss's clicky props,
        # every door, every lever, etc all fire this), hence gated
        # and off by default rather than always-on.
        self.debug_interact_logging = False

        # mirrors the old client's game_state prints, but as live state a
        # GUI (or /status) can read instead of scrolling console text
        self.game_state = {
            "attached": False,
            "zone": "unknown",
            "health": None,
            "max_health": None,
            "emeralds": 0,
        }

        # persisted-to-disk progress trackers - same files/format as before
        self.unlocked_zones = _load_unlocked_zones()
        # boss_kills_claimed / emerald_milestones_claimed / known_mission_completed
        # used to be their own local files (boss_kills_claimed.json,
        # emerald_milestones_claimed.json, mission_progress_baseline.json).
        # All three map to real, fixed location IDs the server already
        # tracks in ctx.checked_locations (server-authoritative, synced on
        # every Connected packet) - so they're derived fresh from that
        # instead now (see _reset_progress_if_new_seed, where
        # ctx.checked_locations is actually populated by then). Starting
        # empty here is fine - nothing reads these before the Connected
        # packet arrives and reseeds them properly.
        self.known_mission_completed = set()
        self.boss_kills_claimed = set()
        self.zone_chests_claimed = load_claimed_zone_chests()
        self.emerald_milestones_claimed = set()
        # cumulative emeralds ever earned, not the live spendable balance -
        # see update_emerald_earned_total's docstring
        self.emerald_earned_state = load_emerald_earned_state()
        self.applied_reward_indices = load_applied_reward_indices()
        # how many "Progressive Enchant Slot" items received so far (0-3) -
        # persisted like applied_reward_indices, but this is the derived
        # COUNT that num_slots actually needs, not the index set itself
        self.progressive_enchant_slot_tier = load_enchant_slot_tier()
        self.progressive_pickup_tier = load_pickup_tier()
        self.progressive_pickup_tier_applied = None  # last tier actually
                                                        # pushed into the
                                                        # game - lets the
                                                        # push-loop below
                                                        # retry only on
                                                        # mismatch, not
                                                        # every tick

        # per-run tracking, not persisted
        self.last_mission_zone = None
        self.last_locked_zone_warned = None
        # A level-lock kill (or an incoming DeathLink kill) that was refused
        # because the bridge hook was silent / a loading screen was active is
        # retried instead of being dropped - see the tick loop.
        self.pending_lock_kill_zone = None
        self.pending_lock_kill_until = 0.0
        # The player has several lives (totems), so ONE kill only costs a life:
        # the lock keeps killing - with a respawn wait in between - until the
        # player has actually left the zone (or the caps below are hit).
        self.lock_kill_count = 0
        self.lock_kill_next_at = 0.0
        self.deathlink_kill_retry_until = 0.0
        self.deathimg_enabled = True              # custom picture on the death screen of a DeathLink death
        self.deathimg_path = None                 # None = archipelago_death.png next to the client
        # set when a mission-outcome trigger fires and we're waiting on the
        # one-shot IsMissionCompleted() confirm call to resolve it; cleared
        # on a definitive True/False, and reset to None the moment a NEW
        # mission attempt starts so a stale trigger/result from the
        # PREVIOUS attempt can never be attributed to this one
        self.pending_mission_outcome_zone = None
        self.pending_mission_outcome_triggers = set()
        self.gameover_trigger_pending = False
        # True once a DeathLink has been sent for the current "life"
        # (current mission attempt), cleared on the next fresh attempt
        # (see the "New mission attempt starting" reset above) or on a
        # zone/hub return after a confirmed death - so the slow HUD/
        # health-poll fallback can't send a second DeathLink for a death
        # the fast MulticastGameOver trigger already reported, even if
        # it eventually catches up outside the 10s cooldown window.
        self.death_already_sent_this_life = False
        self.warned_mission_completion_unavailable = False
        self.mission_end_scan_snapshot = None
        # Whether we've already sent the server a "goal reached"
        # StatusUpdate this game - only ever sent once (the server
        # ignores it after the first time anyway, but no reason to keep
        # trying every tick).
        self.sent_goal = False
        # See the big comment where this gets checked (startup catch-up
        # sweep) - shown at most once per connection, not every tick.
        self.emerald_reset_hint_shown = False
        self.last_health = None
        self.last_max_health = None
        self.last_death_link_sent = 0.0
        self.last_emerald_poll = 0.0
        self.last_chest_error_log = 0.0
        self.current_zone_index = "__unset__"
        # True while zone_index read as 0 this tick (the transient
        # loading-screen state, not a real zone - see the big comment
        # where zone_index is read in game_watcher). Gates item grants:
        # wait_for_stable_item_stash's address-stability check narrows
        # the "wrote right as the game tore the stash down" race but
        # doesn't close it outright, since stability is only confirmed
        # for a window in the past, not guaranteed going forward -
        # confirmed via user report that a grant still landed (and got
        # silently wiped) mid-transition despite that check passing.
        # This is a second, independent gate using the same "are we
        # loading right now" signal the game itself already exposes,
        # rather than trying to further tighten a race that can't
        # actually be closed by narrowing timing windows alone.
        self.currently_loading = False
        # Character (Pawn) presence tracking - see _track_pawn_presence.
        # pawn_present is None until the first debounced reading after an
        # attach, then True/False. Purely informational: nothing gates on
        # these fields, the real guards still call get_pawn themselves.
        self.pawn_present = None
        self.pawn_state_since = None      # time.time() the current state was first announced
        self.pawn_last_reason = ""        # get_pawn's error string while absent
        self.pawn_candidate = None        # last raw reading (True/False), not yet announced
        self.pawn_candidate_count = 0
        self.progressive_pickups_unlocked = False  # set once per attach, see game_watcher
        self.pending_emerald_grants = []  # [(absolute_index, amount), ...] - not yet applied
        self.pending_eye_grants = []      # [(absolute_index, 1), ...] "Eye of Ender" items not yet given in-game
        self.pending_villager_grants = [] # [(absolute_index, item name), ...] camp merchant items not yet unlocked in-game
        self.pending_mission_popups = []  # [zone_name, ...] - native "zone unlocked" popups not shown yet
        self.last_mission_popup_time = 0.0
                                           # in-game (not attached yet, or the write failed);
                                           # retried every tick in game_watcher until it lands.
        self.pending_item_grants = []  # [(absolute_index, item_name), ...] - same idea, for
                                        # equipment/location rewards; see
                                        # _apply_next_pending_item_grant. Processed ONE at a
                                        # time (not drained all at once like emerald grants) -
                                        # a single give_item.py call can legitimately take a
                                        # while (its own zone-transition retry logic goes up
                                        # to 2 minutes), so bundling several into one tick
                                        # would compound that unpredictably.
        self.next_item_grant_attempt_time = 0  # epoch seconds - see ITEM_GRANT_RETRY_COOLDOWN

        # dungeons_bridge.dll's pipe server only serves one client
        # connection at a time. _apply_next_pending_item_grant runs a
        # grant on a background thread and can hold its own pipe
        # connection open for a long time (its own retry logic goes up
        # to 2 minutes) - if the main loop's own periodic pipe calls
        # (chests, boss kills, mission outcomes, currency) tried to
        # connect at the same time, every single one of them would
        # fail for that whole 2 minutes. This flag lets the main loop
        # just skip those calls for a tick instead - nothing is lost,
        # since they're all polled repeatedly anyway and pick back up
        # normally the moment the grant finishes.
        self.item_grant_pipe_busy = False

        # THE real mutual exclusion for dungeons_bridge.dll's named pipe
        # (nMaxInstances=1 in dungeons_bridge.cpp: only ONE client
        # connection can exist at a time, any other attempt gets
        # ERROR_PIPE_BUSY). Every coroutine that touches the pipe through
        # asyncio.to_thread - the per-tick chest/death/outcome/pickup-tier
        # polling, real AP item grants, and /give_safe - takes this lock
        # around the call, so two of them can never overlap.
        # item_grant_pipe_busy above stays as a cheap "skip this tick"
        # hint only; this lock is what actually guarantees exclusion.
        self.bridge_pipe_lock = asyncio.Lock()

        # Set right after we kill the local player OURSELVES
        # (level-lock enforcement, or a DeathLink we just received) -
        # checked by the death-detection code below before sending a
        # NEW DeathLink out, so a death we caused programmatically
        # never gets mistaken for a real one and re-broadcast. Without
        # this, every DeathLink you receive would echo straight back
        # out to the whole multiworld.
        #
        # A TIMESTAMP (0.0 = not suppressed), not a bool that waits to be
        # cleared by the health-poll "died" heuristic actually firing.
        # kill_local_player() kills through a direct engine hook
        # (ABaseCharacter::Kill()), not organic combat damage - it isn't
        # guaranteed to pass through the same "health drained to near-
        # zero" pattern the poll watches for, especially within one
        # 0.2s tick. A bool left waiting for that confirmation could
        # (and did, in practice) get stuck True forever the moment one
        # programmatic kill wasn't caught by the heuristic - silently
        # swallowing every REAL death from then on, with no way to
        # notice. A short expiring window fixes that: suppression always
        # lapses on its own, whether or not the heuristic ever "saw" the
        # kill it was meant to cover.
        self.suppress_death_link_until = 0.0

        self.game_watcher_task = None

    async def server_auth(self, password_requested: bool = False):
        if password_requested and not self.password:
            await super().server_auth(password_requested)
        await self.get_username()
        await self.send_connect()

    def on_package(self, cmd: str, args: dict):
        # CommonContext's own base on_package does real bookkeeping we
        # still want (data package prep, permission logging, etc.) - it
        # is NOT where the seed name comes from, though (see _on_package's
        # RoomInfo handling below for that - CommonContext.seed_name is
        # only ever READ by the base class, never assigned from the
        # incoming packet, confirmed directly against the real
        # CommonClient.py source). Still called unconditionally, before
        # our own handling below, so base bookkeeping always happens even
        # if our own logic then errors - and wrapped on its own, so if
        # base on_package itself somehow raises for some cmd, that
        # doesn't skip OUR handling of it either (each half runs
        # independently of whether the other succeeded).
        try:
            super().on_package(cmd, args)
        except Exception as e:
            game_logger.info(f"super().on_package error handling {cmd} (continuing): {e}")

        # Called synchronously from CommonContext's own server message
        # loop - an uncaught exception here (see the "'NetworkItem' object
        # has no attribute 'get'" incident this wraps around) doesn't just
        # fail quietly, it kills that loop and forces a full disconnect.
        # Wrapping the whole method means a bug in OUR handling of one
        # cmd can never take the connection down with it again.
        try:
            self._on_package(cmd, args)
        except Exception as e:
            game_logger.info(f"on_package error handling {cmd} (continuing): {e}")

    async def _inject_on_connect(self):
        """Backgrounded body for the on-connect (re-)injection - see the
        call site's comment in _on_package for why this can't run
        synchronously there. self.pm is re-read here (not passed in)
        since some time may pass between scheduling this and it actually
        running; if it went from attached to None in that window, there's
        nothing to inject into anymore and this just quietly returns,
        same as the original inline check did."""
        if self.pm is None:
            return
        try:
            inject_error = await asyncio.to_thread(_ensure_bridge_dll_injected, self.pm.process_id)
        except Exception as e:
            inject_error = f"{type(e).__name__}: {e}"
        if inject_error == GAME_GONE_MESSAGE:
            game_logger.info(GAME_GONE_MESSAGE)
        elif inject_error:
            game_logger.info(f"dungeons_bridge.dll injection on connect failed ({inject_error}) - "
                              f"boss kills, chests, and emeralds may not work until this succeeds.")
        else:
            game_logger.info("dungeons_bridge.dll confirmed injected (or already was) on connect.")

    def _maybe_mark_bug(self, args: dict):
        """A player typing exactly "bug" in the Archipelago chat marks a black screen / glitch while it happens:
        log it with the client timestamp and ask the DLL to dump its flight recorder (the last ProcessEvent
        calls) so the moment can be matched with dungeons_bridge_debug.log. Only this slot's own chat counts."""
        if args.get("type") != "Chat":
            return
        if args.get("slot") != getattr(self, "slot", None):
            return
        if str(args.get("message", "")).strip().lower() != "bug":
            return
        game_logger.info("BUG MARKER: chat message 'bug' received - asking the DLL to dump its flight recorder.")
        async_start(self._bug_marker_dump())

    async def _bug_marker_dump(self):
        pm = self.pm
        if pm is None:
            game_logger.info("BUG MARKER: game not attached - nothing to dump.")
            return
        try:
            ok, resp = await asyncio.to_thread(dungeons_reader_module.bridge_dump_recorder, pm)
        except Exception as e:
            game_logger.info(f"BUG MARKER: recorder dump request failed: {e}")
            return
        game_logger.info(f"BUG MARKER: recorder dump {'done' if ok else 'FAILED'} ({resp}). "
                         f"See the FLIGHT RECORDER lines in dungeons_bridge_debug.log.")

    def _on_package(self, cmd: str, args: dict):
        if cmd == "PrintJSON":
            try:
                self._maybe_mark_bug(args)
            except Exception as e:
                game_logger.info(f"bug marker error (continuing): {e}")
        if cmd == "RoomInfo":
            # CommonContext.seed_name is ONLY ever READ by the base class
            # (process_server_cmd's RoomInfo handler uses it purely to
            # warn on a mismatch against something the CLIENT would have
            # to set beforehand) - it is NEVER assigned from the
            # incoming packet by anything in CommonContext. Confirmed by
            # reading the actual current CommonClient.py source directly
            # (an earlier fix here assumed a "server_seed_name" attribute
            # existed based on a misleading search snippet - it doesn't
            # exist anywhere in the real source). So this has to be
            # captured ourselves, from the one packet that actually
            # carries it - RoomInfo, always sent before Connected.
            self._room_seed_name = args.get("seed_name")

        elif cmd == "Connected":
            # CommonContext doesn't store slot_data itself - every
            # subclass that needs it does this. Everything downstream
            # (death_link, boss/emerald/pickup toggles) reads from here.
            self.slot_data = args.get("slot_data", {}) or {}

            # With secret_missions_require_secret ON, the level lock also needs each secret zone's
            # Secret Access item (a missing one kills the player on entry, like any locked zone).
            set_secret_access_required(self.slot_data.get("secret_missions_require_secret", False))

            # Warn when the seed was generated with another apworld version than this client
            # (a mismatch can mean different logic / options / item tables).
            _seed_version = self.slot_data.get("world_version")
            _client_version = _own_world_version()
            if _seed_version and _client_version != "unknown" and str(_seed_version) != _client_version:
                _mismatch_msg = (f"WARNING: this seed was generated with Minecraft Dungeons apworld "
                                 f"v{_seed_version} but this client is v{_client_version}. Install the "
                                 f"matching .apworld to avoid logic or item mismatches.")
                logging.getLogger("Client").warning(_mismatch_msg)
                game_logger.info(_mismatch_msg)

            # Per-run progress (unlocked_zones, claimed boss kills/chests
            # /milestones, applied rewards, mission-complete baseline,
            # cumulative emerald-earned total) persists in DATA_DIR
            # indefinitely, with nothing scoping it to a particular seed
            # - starting a DIFFERENT playthrough previously just
            # inherited whatever the last one left behind. Most visibly
            # this can make mission completion look like it "doesn't
            # trigger" at all: a zone finished in an OLD playthrough
            # stays in known_mission_completed forever, so the real
            # completion on a NEW seed is silently skipped (already
            # believed done). Detect a seed change here (the only point
            # where we actually learn which seed this is) and wipe just
            # the per-run files - NOT the stable reference/lookup data
            # (zone_name_lookup.json etc), which has nothing to do with
            # any particular seed.
            seed_key = f"{getattr(self, '_room_seed_name', None)}:{self.auth}"
            self._reset_progress_if_new_seed(seed_key)

            # Game store, PER SEED. Priority: this seed's /set_store
            # (seed_dir/game_store.json) > the YAML's game_store option > the global
            # default setting. Applied to the session only if the game isn't attached yet.
            _seed_store = load_seed_game_store(self.seed_dir)
            _yaml_store = self.slot_data.get("game_store")
            if _seed_store:
                _wanted, _source = _seed_store, "/set_store (this seed)"
            elif _yaml_store in STORE_OFFSETS:
                _wanted, _source = _yaml_store, "YAML option game_store"
            else:
                _wanted, _source = None, None
            if _wanted:
                if getattr(self, "pm", None):
                    if _wanted != dungeons_reader_module.GAME_STORE:
                        game_logger.info(f"Game store '{_wanted}' ({_source}) differs from the store already "
                                         f"in use ('{dungeons_reader_module.GAME_STORE}') and the game is "
                                         "attached: restart the client and the game to apply it.")
                else:
                    try:
                        if apply_game_store(_wanted):
                            game_logger.info(f"Game store set to '{_wanted}' from {_source}.")
                    except ValueError as _e:
                        game_logger.info(f"Ignoring game store '{_wanted}': {_e}")

            # STARTING_ZONE (Squid Coast) is a precollected/starting item
            # at generation time now (see __init__.py's generate_early) -
            # not something placed at a location. Confirmed NOT reliably
            # showing up as an "Unlocked: squidcoast" ReceivedItems event
            # client-side (log evidence: never once logged across a full
            # session, despite the player legitimately starting with it) -
            # rather than depend on exactly when/whether that
            # notification arrives, just guarantee it directly. This
            # matters far beyond Squid Coast itself: EVERY other zone's
            # predecessor chain eventually requires it, so without this,
            # is_zone_truly_unlocked would transitively reject every
            # single zone in the game, not just this one - confirmed
            # happening (missing: ['creeperwoods', 'squidcoast'] even
            # after Creeper Woods Access was legitimately received).
            if STARTING_ZONE not in self.unlocked_zones:
                self.unlocked_zones.add(STARTING_ZONE)
                _save_unlocked_zones(self.unlocked_zones)

            game_logger.info(
                f"Connected. Boss kill checks: base={'on' if self.slot_data.get('boss_kill_checks') else 'off'}, "
                f"dlc={'on' if self.slot_data.get('dlc_boss_kill_checks') else 'off'}. "
                f"Eye of Ender checks: {'on' if self.slot_data.get('eye_of_ender_checks') else 'off'}. "
                f"Villager checks: {'on' if self.slot_data.get('villager_checks') else 'off'}. "
                f"Mini boss kill checks (0 none, 1 per_type, 2 per_zone, 3 both): base={self.slot_data.get('mini_boss_kill_checks', 0)}, "
                f"dlc={self.slot_data.get('dlc_mini_boss_kill_checks', 0)}. "
                f"Emerald goal: {self.slot_data.get('emerald_goal', 'none')}."
            )

            # Also (re-)inject dungeons_bridge.dll right here, on connect -
            # not just relying on game_watcher's own attach-loop injection
            # (which only fires when it notices Dungeons.exe's PID has
            # changed, i.e. on the NEXT tick after a crash+relaunch it
            # happens to catch). Reported bug: after a game crash, boss/
            # chest/emerald tracking stayed dead until the whole CLIENT was
            # restarted - even though the game itself came back up fine.
            # _ensure_bridge_dll_injected is a documented no-op if the DLL
            # is already loaded, so calling it again here on every /connect
            # is always safe, never double-injects, and closes the gap for
            # whatever edge case let a stale/unloaded DLL slip past
            # game_watcher's own check (e.g. connecting to the AP server
            # again without game_watcher's loop having cycled since the
            # crash, or is_process_alive not yet having noticed the PID
            # change at the exact moment this packet arrives). Best-effort,
            # same as everywhere else this is called - self.pm may not be
            # attached yet at all (game_watcher hasn't found Dungeons.exe
            # for the first time yet), in which case there's nothing to
            # inject INTO yet and game_watcher's own attach-loop injection
            # will cover it once attach succeeds.
            if self.pm is not None:
                # Backgrounded via async_start + asyncio.to_thread - NOT
                # called directly here. _on_package is a SYNCHRONOUS method
                # (called from the AP client library's packet dispatch), and
                # _ensure_bridge_dll_injected does blocking Windows API calls
                # (OpenProcess/inject) with no guaranteed-short runtime.
                # Calling it directly here blocks whatever thread dispatches
                # packets - confirmed via a real traced session: a "Lost
                # connection to the multiworld server... keepalive ping
                # timeout" that lined up exactly with this call, not a
                # coincidence. Same class of bug as the other pipe/injection
                # calls fixed elsewhere in this file - unthreaded blocking
                # calls on a path that also needs to keep the AP connection
                # alive.
                async_start(self._inject_on_connect())

            async_start(self.update_death_link(bool(self.slot_data.get("death_link", True))),
                        name="set DeathLink tag")
            if self.game_watcher_task is None:
                self.game_watcher_task = asyncio.create_task(game_watcher(self), name="MCDungeonsGameWatcher")

        elif cmd == "ReceivedItems":
            # Mirrors the previous client's poll_received_items handling:
            # a zone-access item unlocks that zone the moment it arrives,
            # regardless of when game_watcher's level-lock check next
            # runs - unlocked_zones is what is_zone_truly_unlocked (used
            # by game_watcher's lock enforcement) reads.
            #
            # args["items"] entries are real NetworkItem objects here
            # (attribute access: .item/.location/.player/.flags), NOT
            # plain dicts - item.get("item") raised
            # "'NetworkItem' object has no attribute 'get'" on every
            # single item received, which crashed out of on_package
            # (called synchronously from CommonContext's own server
            # message loop) hard enough to force a full disconnect -
            # "sending a check" only ever looked like the trigger because
            # a check earning something back (or another player's send
            # arriving) is what causes a ReceivedItems packet at all.
            for offset, item in enumerate(args.get("items", [])):
                # A Secret Access item is recorded in the persisted unlocked set (like an Access
                # item) so the level lock can require it when the option is on.
                _sa_name = ITEM_ID_TO_NAME.get(item.item)
                if _sa_name and _sa_name.endswith(" Secret Access"):
                    _sa_zone = next((z.internal_name for z in _apw_zonedata.ZONES
                                     if z.secret and z.display_name == _sa_name[:-len(" Secret Access")]), None)
                    if _sa_zone and secret_access_marker(_sa_zone) not in self.unlocked_zones:
                        self.unlocked_zones.add(secret_access_marker(_sa_zone))
                        # With the option on, the Secret Access is the zone's ONLY unlock item: it
                        # also stands for the zone's Access (children, /unlocked, ...).
                        if (getattr(self, "slot_data", None) or {}).get("secret_missions_require_secret"):
                            self.unlocked_zones.add(_sa_zone)
                        _save_unlocked_zones(self.unlocked_zones)
                zone_name = ITEM_ID_TO_ZONE.get(item.item)
                newly_unlocked_zone = False
                if zone_name and zone_name not in self.unlocked_zones:
                    self.unlocked_zones.add(zone_name)
                    _save_unlocked_zones(self.unlocked_zones)
                    game_logger.info(f"Unlocked: {zone_name}")
                    newly_unlocked_zone = True

                # Emerald filler items ("100/300/500 Emeralds") grant
                # straight into the player's currency total instead of
                # being a zone unlock - queued here rather than applied
                # immediately since pm/base might not be attached yet;
                # game_watcher drains this every tick and retries until
                # apply_emerald_reward actually succeeds. Deduped by this
                # item's ABSOLUTE ReceivedItems index (start_index +
                # offset in this packet), persisted to
                # applied_item_rewards.json, because a reconnect resends
                # the ENTIRE items backlog from index 0 every time - item
                # identity alone isn't enough to tell "already granted"
                # apart from "granted again on reconnect".
                emerald_amount = ITEM_ID_TO_EMERALD_AMOUNT.get(item.item)
                item_name = ITEM_ID_TO_NAME.get(item.item)
                absolute_index = args.get("index", 0) + offset

                # Notification in the client's main tab for every mission
                # item received (normal zone access, Secret Access, Ancient
                # Hunt access). Normal zones: only when the zone was newly
                # unlocked just above, so a reconnect (which resends the
                # whole backlog from index 0) or a restart never repeats it.
                # Secret / Ancient Hunt items have no unlocked_zones entry,
                # so they're deduped by absolute index in
                # applied_reward_indices instead (persisted, per-seed).
                mission_label = None
                if newly_unlocked_zone:
                    zinfo = _apw_zonedata.ZONES_BY_NAME.get(zone_name)
                    mission_label = f"Mission unlocked: {zinfo.display_name if zinfo else zone_name}"
                elif item_name and absolute_index not in self.applied_reward_indices:
                    if item_name.endswith(" Secret Access"):
                        # Option OFF = old system: secret missions are items in the
                        # table but never handed out, and if one somehow arrives
                        # anyway (console/start inventory) nothing happens at all.
                        if not getattr(self, "slot_data", {}).get("secret_missions_require_secret"):
                            continue
                        mission_label = (f"Secret mission unlocked: "
                                         f"{item_name[:-len(' Secret Access')]}")
                    elif item_name == ANCIENT_HUNT_ACCESS_ITEM_NAME:
                        mission_label = "Ancient Hunts unlocked"
                    if mission_label:
                        self.applied_reward_indices.add(absolute_index)
                        save_applied_reward_indices(self.applied_reward_indices)
                if mission_label:
                    logging.getLogger("Client").info(mission_label)
                    game_logger.info(mission_label)
                    # Also queue the game's own native "mission unlocked"
                    # popup (ClientUnlockBonusMission - image + sound, popup
                    # only, no state change). Shown from game_watcher once a
                    # character is on screen, one at a time.
                    popup_zone = zone_name if newly_unlocked_zone else None
                    is_secret_item = bool(item_name and item_name.endswith(" Secret Access"))
                    if popup_zone is None and is_secret_item:
                        base_display = item_name[:-len(" Secret Access")]
                        popup_zone = next((z.internal_name for z in _apw_zonedata.ZONES
                                           if z.secret and z.display_name == base_display), None)
                    if popup_zone and get_elevelname_value(popup_zone) is not None:
                        # "Secret Access" items only exist when the world option
                        # secret_missions_require_secret is ON. In that mode the
                        # item REALLY unlocks the secret mission in-game
                        # (UnlockBonusMission) + shows the native popup. With the
                        # option off, no such item exists and everything else keeps
                        # the old behaviour (popup only, no state change).
                        full_unlock = (is_secret_item and bool(
                            getattr(self, "slot_data", {}).get("secret_missions_require_secret")))
                        self.pending_mission_popups.append((popup_zone, full_unlock))
                    elif popup_zone or item_name == ANCIENT_HUNT_ACCESS_ITEM_NAME:
                        game_logger.info(f"No native popup for {popup_zone or item_name}: "
                                         f"no ELevelNames value known (text message only).")

                if emerald_amount:
                    if absolute_index not in self.applied_reward_indices:
                        self.pending_emerald_grants.append((absolute_index, emerald_amount))
                elif item_name == EYE_OF_ENDER_ITEM_NAME:
                    # An "Eye of Ender" received from the multiworld = one real Eye in the wallet (the real
                    # Eyes the mini bosses drop are checks now and not picked up, see the eye_of_ender_checks
                    # option). Same queue + dedup-by-absolute-index scheme as the emerald grants above.
                    if absolute_index not in self.applied_reward_indices:
                        self.pending_eye_grants.append((absolute_index, 1))
                elif item_name in villagers_module.VILLAGER_ITEM_NAMES:
                    # A camp merchant received from the multiworld = that merchant unlocked in the camp (the villager
                    # itself is a check now, see the villager_checks option and villagers.py). Same queue +
                    # dedup-by-absolute-index scheme as the Eye of Ender grants above.
                    if absolute_index not in self.applied_reward_indices:
                        self.pending_villager_grants.append((absolute_index, item_name))
                elif item_name == PROGRESSIVE_ENCHANT_SLOT_ITEM:
                    # Counting IS the entire action for this one - nothing
                    # to grant in-game, just tracked so later equipment
                    # grants know how many enchant slots to actually use
                    # (give_random_item's num_slots). Applied immediately
                    # (marked in applied_reward_indices right here) rather
                    # than queued into pending_item_grants, since there's
                    # nothing that could fail/need retrying about it.
                    if absolute_index not in self.applied_reward_indices:
                        self.applied_reward_indices.add(absolute_index)
                        save_applied_reward_indices(self.applied_reward_indices)
                        if self.progressive_enchant_slot_tier < MAX_ENCHANT_SLOT_TIER:
                            self.progressive_enchant_slot_tier += 1
                            save_enchant_slot_tier(self.progressive_enchant_slot_tier)
                            game_logger.info(f"Enchant slot tier now {self.progressive_enchant_slot_tier}.")
                elif item_name == PROGRESSIVE_PICKUP_ITEM:
                    # Same pattern as PROGRESSIVE_ENCHANT_SLOT_ITEM above -
                    # counting is the whole action here too. The actual
                    # push into the game (set_pickup_tier) happens in
                    # game_watcher's tick loop, not here, since it needs
                    # pm/base attached and this handler can fire before
                    # that's ready.
                    if absolute_index not in self.applied_reward_indices:
                        self.applied_reward_indices.add(absolute_index)
                        save_applied_reward_indices(self.applied_reward_indices)
                        if self.progressive_pickup_tier < PICKUP_MAX_TIER:
                            self.progressive_pickup_tier += 1
                            save_pickup_tier(self.progressive_pickup_tier)
                            game_logger.info(f"Progressive pickup tier now {self.progressive_pickup_tier}.")
                elif item_name and (is_item_reward(item_name) or is_location_reward(item_name)):
                    # Random equipment / location-specific reward - queued
                    # the same way emerald grants are (pm/base might not
                    # be attached yet). Unlike emerald grants, give_item.py
                    # itself is what handles the actual retry-on-failure
                    # (capacity, zone transitions) - see
                    # _apply_next_pending_item_grant.
                    if absolute_index not in self.applied_reward_indices:
                        self.pending_item_grants.append((absolute_index, item_name))
                # else: a progression/access item, or an item_id that
                # didn't resolve to a name at all - no in-game action needed.

    def _wipe_current_seed_progress(self) -> None:
        """Needed to fix an old bug shouldn't be a problem now if
        you skip an chest you can use this"""
        for path in (Path.cwd() / UNLOCKED_ZONES_FILE, Path.cwd() / MISSION_PROGRESS_BASELINE_FILE,
                     Path.cwd() / BOSS_KILLS_FILE, Path.cwd() / ZONE_CHESTS_FILE,
                     Path.cwd() / MILESTONES_FILE, Path.cwd() / APPLIED_REWARDS_FILE,
                     Path.cwd() / EMERALD_EARNED_STATE_FILE, Path.cwd() / ENCHANT_SLOT_TIER_FILE,
                     Path.cwd() / PICKUP_TIER_FILE):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

        self.unlocked_zones = set()
        self.zone_chests_claimed = {}
        self.emerald_earned_state = {"total_earned": 0, "last_seen_balance": None}
        self.applied_reward_indices = set()
        self.progressive_enchant_slot_tier = 0
        self.progressive_pickup_tier = 0
        self.progressive_pickup_tier_applied = None

        self.known_mission_completed = {zone for zone, loc_id in MISSION_LOCATION_IDS.items()
                                         if loc_id in self.checked_locations}
        self.boss_kills_claimed = {boss for boss in _apw.BOSS_NAMES
                                    if _apw.get_boss_kill_location_id(boss) in self.checked_locations}
        emerald_goal = self.slot_data.get("emerald_goal") or 0 if hasattr(self, "slot_data") else 0
        self.emerald_milestones_claimed = {m for m in range(500, emerald_goal + 1, 500)
                                            if _apw.get_emerald_milestone_id(m) in self.checked_locations}

        if STARTING_ZONE not in self.unlocked_zones:
            self.unlocked_zones.add(STARTING_ZONE)
            _save_unlocked_zones(self.unlocked_zones)

    def _reset_progress_if_new_seed(self, seed_key: str) -> None:
        """keep important information in folder for every seed"""
        safe_key = re.sub(r"[^A-Za-z0-9_.-]+", "_", seed_key or "unknown").strip("_") or "unknown"
        seed_dir = DATA_DIR / "seeds" / safe_key
        seed_dir.mkdir(parents=True, exist_ok=True)
        self.seed_dir = seed_dir   # used by /set_store (per-seed setting)

        is_new_seed_dir = not any(seed_dir.iterdir())
        game_logger.info(f"Seed check: current={seed_key!r} -> switching to "
                          f"{'FRESH' if is_new_seed_dir else 'EXISTING'} progress folder {seed_dir}")

        os.chdir(seed_dir)

        if is_new_seed_dir:
            # Never played this exact seed_key before - start empty,
            # same defaults _reset_progress_if_new_seed always used.
            self.unlocked_zones = set()
            self.zone_chests_claimed = {}
            self.emerald_earned_state = {"total_earned": 0, "last_seen_balance": None}
            self.applied_reward_indices = set()
            self.progressive_enchant_slot_tier = 0
            self.progressive_pickup_tier = 0
        else:
            # Played this seed_key before (even if not most recently) -
            # reload its own saved progress from its own subfolder,
            # rather than treating "not the LAST seed played" as "never
            # played".
            self.unlocked_zones = _load_unlocked_zones()
            self.zone_chests_claimed = load_claimed_zone_chests()
            self.emerald_earned_state = load_emerald_earned_state()
            self.applied_reward_indices = load_applied_reward_indices()
            self.progressive_enchant_slot_tier = load_enchant_slot_tier()
            self.progressive_pickup_tier = load_pickup_tier()

        # Boss kills / mission completion / emerald milestones: derived
        # from ctx.checked_locations (already populated at this point -
        # super().on_package's own Connected handling, called just before
        # this method, is what fills it in) rather than any local file.
        # Correct regardless of which seed_dir branch ran above, since
        # this is server-room state, not local-file state.
        self.known_mission_completed = {zone for zone, loc_id in MISSION_LOCATION_IDS.items()
                                         if loc_id in self.checked_locations}
        self.boss_kills_claimed = {boss for boss in _apw.BOSS_NAMES
                                    if _apw.get_boss_kill_location_id(boss) in self.checked_locations}
        # Scanned up to THIS player's own emerald_goal (available in
        # slot_data, set just above in the Connected handler) rather than
        # the option's full static ceiling (50000) - milestones beyond a
        # player's own goal were never added as real locations for their
        # slot in the first place (see Regions.py), so scanning further
        # than that would just waste cycles checking IDs that can never
        # be in checked_locations for this slot anyway.
        emerald_goal = self.slot_data.get("emerald_goal") or 0
        self.emerald_milestones_claimed = {m for m in range(500, emerald_goal + 1, 500)
                                            if _apw.get_emerald_milestone_id(m) in self.checked_locations}

        # Always-fresh, per-connection state - never persisted, so
        # nothing to load either way above.
        self.pending_mission_popups = []
        self.pending_emerald_grants = []
        self.pending_eye_grants = []
        self.pending_villager_grants = []
        self.pending_item_grants = []
        self.next_item_grant_attempt_time = 0
        self.progressive_pickup_tier_applied = None
        self.pending_mission_outcome_zone = None
        self.pending_mission_outcome_triggers = set()
        self.gameover_trigger_pending = False
        self.death_already_sent_this_life = False
        self.sent_goal = False
        self.emerald_reset_hint_shown = False
        self.mission_end_scan_snapshot = None
        self.progressive_pickups_unlocked = False

    SUPPRESS_DEATH_LINK_WINDOW = 5.0  # seconds - long enough to cover the
                                        # poll cadence + the kill actually
                                        # landing, short enough that it can
                                        # never swallow a genuinely later,
                                        # unrelated real death

    def on_deathlink(self, data: dict) -> None:
        super().on_deathlink(data)
        if self.pm and self.base:
            self.suppress_death_link_until = time.time() + self.SUPPRESS_DEATH_LINK_WINDOW
            source = data.get("source") or "Someone"
            if self.currently_loading:
                # No engine call during a loading screen - retried from the tick loop.
                self.deathlink_kill_retry_until = time.time() + 20.0
                game_logger.info(f"{source} died - loading screen in progress, will apply the kill right after.")
                return
            success, kill_error, diag = kill_local_player(self.pm, self.base)
            if success:
                game_logger.info(f"{source} died (chain={diag.get('chain')}).")
                if self.deathimg_enabled:
                    async_start(_run_death_image(self, None, self.deathimg_path), name="DeathImage")
            else:
                self.deathlink_kill_retry_until = time.time() + 20.0
                game_logger.info(f"{source} died - could not kill local player yet: {kill_error} (will retry).")

    def make_gui(self):
        # Documented extension point (see CommonContext.make_gui's own
        # docstring): return a GameManager subclass, base_title renames
        # the window, and appending to logging_pairs adds a tab that
        # follows a given logger - "Game" here follows game_logger above,
        # giving zone/health/attach status its own tab next to the
        # normal Archipelago connection/item log.
        from kvui import GameManager

        class MCDungeonsManager(GameManager):
            # The old "Game" log tab (MCDungeonsGame) was replaced by the
            # "Map" tab below. game_logger still logs, it just isn't shown
            # in the window any more - to bring it back, add
            # ("MCDungeonsGame", "Game") to this list.
            logging_pairs = [
                ("Client", "Archipelago"),
            ]
            base_title = "Minecraft Dungeons Client"

            def build(self):
                container = super().build()
                try:
                    from map_tab import MapTab
                    tab = MapTab(self.ctx)
                    if hasattr(self, "add_client_tab"):
                        self.add_client_tab("Map", tab)
                    else:  # older Archipelago builds without add_client_tab
                        from kivy.uix.tabbedpanel import TabbedPanelItem
                        item = TabbedPanelItem(text="Map")
                        item.add_widget(tab)
                        self.tabs.add_widget(item)
                except Exception:
                    logging.getLogger("Client").exception("Map tab failed to load")
                return container

        return MCDungeonsManager


# ---------------------------------------------------------------------------
# fire_* helpers: send one check, update the matching claimed-state tracker,
# and log it. Same responsibilities as the old client's fire_* closures, just
# using ctx.check_locations (async, CommonContext-native) instead of a raw
# ArchipelagoClient.send_location_checks call.
# ---------------------------------------------------------------------------

# --- void mode (how the DLL empties itself during level transitions) ---------
# Live-tested: with "soft" (hook stays patched in, does nothing) the game crashed
# on the very first hub -> mission transition; with "hard" (ProcessEvent is
# physically unpatched for the whole transition window) nine transitions in a
# row - including a mission victory and two locked-mission kills - went through,
# and /void hard no longer crashes the game. So "hard" is the default. The DLL
# always starts in "soft" (its mode is per process), so the client pushes the
# saved mode right after injection (void_mode.txt remembers /void's choice).
# Findings behind the current design:
#   * the hook must not be PATCHED into the game while the engine is still
#     booting (early injection is fine; the DLL waits for a stable first
#     GWorld before installing it - see WaitForEngineBoot in the DLL);
#   * during a transition the safest state is the one where nothing of ours is
#     in the game's code at all (hard void), not merely a hook that forwards;
#   * a client-side "force the hook silent at mission end" (/endsilence) was
#     tried and removed: hard mode's own world-change silence already carried
#     mission victories and locked-mission kills without it.
VOID_MODE_FILE = DATA_DIR / "void_mode.txt"
VOID_MODE_DEFAULT = "hard"


def load_void_mode() -> str:
    try:
        mode = VOID_MODE_FILE.read_text(encoding="utf-8").strip().lower()
        if mode in ("soft", "hard"):
            return mode
    except Exception:
        pass
    return VOID_MODE_DEFAULT


def save_void_mode(mode: str) -> None:
    try:
        VOID_MODE_FILE.write_text(mode, encoding="utf-8")
    except Exception as e:
        game_logger.info(f"Could not save the void mode preference: {e}")


async def apply_saved_void_mode(ctx):
    """Pushes the saved void mode to the DLL. Retries for up to 60 s because the
    DLL's pipe only exists once it has finished loading inside the game.

    Also the client's check that an injection really took: the DLL opens its
    pipe within a moment of being loaded, so a pipe that stays missing means
    the DLL is not (or no longer) in this process. After a few seconds of that
    the process is inspected and, if it is alive but the DLL is absent, the DLL
    is sent again (twice at most) - so a relaunched game never silently stays
    without the bridge."""
    import auto_inject
    mode = load_void_mode()
    started = time.monotonic()
    deadline = started + 60.0
    last_err = ""
    reinjections = 0
    next_check = started + 8.0
    while time.monotonic() < deadline:
        pm = getattr(ctx, "pm", None)
        if pm is None:
            return
        try:
            async with ctx.bridge_pipe_lock:
                ok, resp = await asyncio.to_thread(bridge_void_mode, pm, mode)
        except Exception as e:
            ok, resp = False, str(e)
        if ok:
            game_logger.info(f"Void mode set to '{mode}' (anti-crash transition safety).")
            return
        last_err = str(resp)

        if time.monotonic() >= next_check:
            next_check = time.monotonic() + 8.0
            pid = pm.process_id
            alive = auto_inject.is_process_alive(pid)
            loaded = alive and auto_inject.is_dll_loaded(pid, auto_inject.DLL_NAME)
            if not alive:
                return   # the watcher reports the dead game and re-attaches on its own
            if not loaded and reinjections < 2:
                reinjections += 1
                game_logger.info(f"dungeons_bridge.dll is not loaded in {_game_process_name()} (PID {pid}) - "
                                  f"sending it again (attempt {reinjections}/2).")
                try:
                    err = await asyncio.to_thread(_ensure_bridge_dll_injected, pid)
                except Exception as e:
                    err = f"{type(e).__name__}: {e}"
                if err:
                    game_logger.info(f"Re-injection failed: {err}")
                    if err == GAME_GONE_MESSAGE:
                        return
            elif loaded:
                game_logger.info(f"dungeons_bridge.dll is loaded in PID {pid} but its pipe is not "
                                  f"answering yet ({last_err}) - still waiting.")
        await asyncio.sleep(1.5)
    game_logger.info(f"Could not set the void mode to '{mode}' ({last_err}) - the DLL keeps its own default. "
                      f"Check dungeons_bridge_debug.log (next to the DLL): a fresh "
                      f"'SetupHookThread starting' line means the DLL did load in the new game process.")


# --- level lock: keep killing until the player is out of the locked zone -----
LOCK_KILL_WINDOW_S = 120.0        # total time the lock keeps enforcing (was 60 s, single kill)
LOCK_KILL_RESPAWN_WAIT_S = 4.0    # pause after a kill so the respawn can complete
LOCK_KILL_MAX_KILLS = 10          # hard cap - more than any lives count
# Level gate: the DLL refuses locked missions at the camp (TryStartMissionSelection / TryContinueMission) so the
# player never enters them. The kill-based enforcement below stays as a fallback for anything the gate misses.
LEVEL_GATE_ENABLED = True
PROCESS_STATS_INTERVAL = 15.0   # seconds between process/guard samples (crash_diag.periodic)
# Unmapped boss candidates (SDK EntityType ids): only LOGGED when they die, never claimed as a boss kill.
# Vengeful Heart of Ender still has no confirmed EntityType (see BOSS_ENTITY_TYPE_IDS: 10/12 bosses).
BOSS_CANDIDATE_ENTITY_TYPES = {2893: "PerfectFormHeart (Vengeful Heart of Ender?)"}

async def fire_mission_complete(ctx: MCDungeonsContext, zone_name: str):
    """Sends this zone's Mission Complete check - "already sent" is
    checked against ctx.checked_locations (server-authoritative) rather
    than a local set, same reasoning as fire_chest_open/fire_boss_kill."""
    location_id = MISSION_LOCATION_IDS.get(zone_name)
    if location_id is None or location_id in ctx.checked_locations:
        return
    game_logger.info(f"Mission complete: {zone_name}")
    await ctx.check_locations([location_id])


async def fire_secret_zone_found(ctx: MCDungeonsContext, zone_name):
    """A SecretFound event fired while the player was in `zone_name`. That
    zone is the secret zone's parent mission, so it maps to exactly one
    "Secret Zone Found" check (Locations.SECRET_PARENT_TO_ZONE). Several events
    for the same secret (SecretFound + ExecuteSecretFound) collapse into one
    check because "already sent" is read from ctx.checked_locations."""
    secret_zone = _apw.SECRET_PARENT_TO_ZONE.get(zone_name)
    if not secret_zone:
        game_logger.info(f"SecretFound event in '{zone_name}' - not a known secret parent zone, ignored.")
        return
    location_id = _apw.SECRET_ZONE_FOUND_TABLE[_apw.SECRET_ZONE_FOUND_BY_ZONE[secret_zone]]
    if location_id in ctx.checked_locations:
        game_logger.info(f"Secret zone found in {zone_name} (-> {secret_zone}) - check already sent.")
        return
    game_logger.info(f"Secret zone found in {zone_name} (-> {secret_zone}) - sending check.")
    await ctx.check_locations([location_id])


async def powerful_mob_tick(ctx, pm, base, death_events):
    """One step of the passive powerful-mob logging (see POWERFUL_MOB_LOGGING). Threaded and
    under the bridge pipe lock like the other pipe users; any failure is logged (throttled)
    and swallowed so this can never break the game watcher."""
    try:
        tracker = getattr(ctx, "powerful_tracker", None)
        if tracker is None:
            tracker = ctx.powerful_tracker = PowerfulMobTracker(say=game_logger.info)
        async with ctx.bridge_pipe_lock:
            await asyncio.to_thread(tracker.tick, pm, base, death_events)
    except Exception as e:
        if time.time() - getattr(ctx, "last_powerful_error_log", 0.0) >= 30.0:
            ctx.last_powerful_error_log = time.time()
            game_logger.info(f"powerful mob logging failed: {e}")


async def _map_count_attempt(ctx, announcer, pm, base):
    """Reading part of the zone-start chest announcement (the level's MapStatActor counters), run as a
    background task so the game loop never waits on it."""
    try:
        async with ctx.bridge_pipe_lock:        # resolving class names goes over the bridge pipe
            await asyncio.to_thread(announcer.attempt, pm, base)
    except Exception as e:
        if time.time() - getattr(ctx, "last_map_count_error_log", 0.0) >= 30.0:
            ctx.last_map_count_error_log = time.time()
            game_logger.info(f"zone-start chest count failed: {e}")
    finally:
        announcer.busy = False


def zone_start_chest_count_tick(ctx, pm, base):
    """Cheap per-tick part: detects the zone start (logs the known count) and, when a map read
    is due, launches it in the background (one at a time)."""
    try:
        announcer = getattr(ctx, "map_count_announcer", None)
        if announcer is None:
            announcer = ctx.map_count_announcer = MapCountAnnouncer(say=game_logger.info)
        if announcer.check_zone(pm, base) and not announcer.busy:
            announcer.busy = True
            asyncio.create_task(_map_count_attempt(ctx, announcer, pm, base), name="ZoneStartChestCount")
    except Exception as e:
        if time.time() - getattr(ctx, "last_map_count_error_log", 0.0) >= 30.0:
            ctx.last_map_count_error_log = time.time()
            game_logger.info(f"zone-start chest count failed: {e}")


async def fire_boss_kill(ctx: MCDungeonsContext, boss_name: str):
    """Checks for the First Kill check. "already sent" is checked
    against ctx.checked_locations (server-authoritative)"""
    location_id = _apw.get_boss_kill_location_id(boss_name)
    if location_id in ctx.checked_locations:
        return
    game_logger.info(f"Boss kill: {boss_name}")
    await ctx.check_locations([location_id])


async def fire_chest_open(ctx: MCDungeonsContext, zone_name: str, kind: str):
    """Sends the next unclaimed check for zone_name/kind - base count
    first, then extras beyond that zone/kind's confirmed baseline feed
    the single GLOBAL bonus-chest pool, capped by slot_data's
    bonus_chest_count.

    How many of this zone/kind's chests are "already claimed" is derived
    directly from ctx.checked_locations (server-authoritative, synced on
    connect and kept current) rather than from a local claimed-count
    file - every chest/supply/bonus check maps to a real, fixed location
    ID (see _apworld_data.py), so "already claimed" is just "is that ID
    in ctx.checked_locations", counted from #1 up. This can't drift from
    the server's own truth and self-heals on every reconnect, unlike a
    local count that could theoretically fall out of sync (a crash
    between check_locations() succeeding and the local save landing,
    etc). The one exception is "global:extra_count" below - a
    cosmetic-only counter for the log line ("extra #N globally") that
    was never itself sent as a location, so it has no server equivalent
    and still needs its own tiny bit of local state.
    """
    ZONE_CHEST_COUNTS = _apw.ZONE_CHEST_COUNTS
    total = ZONE_CHEST_COUNTS[zone_name][0 if kind == "chest" else 1]
    location_id_fn = (_apw.get_zone_chest_location_id if kind == "chest"
                       else _apw.get_zone_supply_chest_location_id)
    # IDs sent a moment ago that the server hasn't acknowledged yet: without
    # this, two opens processed before the ack would both target the same
    # "next" location. Entries drop as soon as the server confirms them, or
    # after 10 s (so a send lost to a disconnect can never block a chest).
    _now = time.time()
    _pend = getattr(ctx, "_pending_chest_ids", None)
    if _pend is None:
        _pend = ctx._pending_chest_ids = {}
    for _lid in [l for l, t in _pend.items() if l in ctx.checked_locations or _now - t > 10.0]:
        del _pend[_lid]

    def _claimed(lid):
        return lid in ctx.checked_locations or lid in _pend

    already = sum(1 for n in range(1, total + 1) if _claimed(location_id_fn(zone_name, n)))

    if already < total:
        next_num = already + 1
        location_id = location_id_fn(zone_name, next_num)
        location_name = (_apw.zone_chest_location_name(zone_name, next_num) if kind == "chest"
                          else _apw.zone_supply_chest_location_name(zone_name, next_num))
        _pend[location_id] = time.time()
        await ctx.check_locations([location_id])
        game_logger.info(f"Chest: {location_name}")
        return

    claimed = ctx.zone_chests_claimed  # only ever holds "global:extra_count" now - see docstring
    bonus_chest_count = ctx.slot_data.get("bonus_chest_count", 20) if hasattr(ctx, "slot_data") else 20
    limit = max(0, min(bonus_chest_count, _apw.MAX_BONUS_CHESTS))
    extra_count = claimed.get("global:extra_count", 0) + 1
    claimed["global:extra_count"] = extra_count
    save_claimed_zone_chests(claimed)

    bonus_already = sum(1 for n in range(1, limit + 1) if _claimed(_apw.get_bonus_chest_location_id(n)))
    if bonus_already >= limit:
        return
    bonus_num = bonus_already + 1
    _pend[_apw.get_bonus_chest_location_id(bonus_num)] = time.time()
    await ctx.check_locations([_apw.get_bonus_chest_location_id(bonus_num)])
    game_logger.info(f"Bonus chest: {_apw.bonus_chest_location_name(bonus_num)} "
                      f"(extra #{extra_count} globally, found in {zone_name})")


def _reached_emerald_milestones(ctx: MCDungeonsContext, current_emeralds):
    """Only the milestones that actually EXIST as locations this
    generation - matches Regions.py's EmeraldChecks/EmeraldIsGoal wiring."""
    emerald_goal = ctx.slot_data.get("emerald_goal")
    if not emerald_goal:
        return []
    emerald_checks_enabled = bool(ctx.slot_data.get("emerald_checks", True))
    emerald_is_goal_enabled = bool(ctx.slot_data.get("emerald_is_goal", True))
    if emerald_checks_enabled:
        return milestones_up_to(current_emeralds, emerald_goal)
    if emerald_is_goal_enabled and current_emeralds >= emerald_goal:
        top = (emerald_goal // 500) * 500
        return [top] if top >= 1000 else []
    return []


async def _apply_next_pending_item_grant(ctx: MCDungeonsContext, pm, base):
    """Attempts to apply exactly ONE queued equipment/location reward
    (the oldest one) per call - not the whole queue at once, since a
    single give_item.py call can legitimately take a long time (its own
    zone-transition retry logic goes up to 2 minutes - see
    _wait_for_zone_transition in give_item.py). If the front one keeps
    failing (e.g. inventory genuinely full), later ones stay queued
    behind it rather than jumping the line - correct, since none of them
    could succeed either until the same underlying problem clears.

    Runs the actual grant via asyncio.to_thread so a slow/stuck attempt
    never blocks ctx.check_locations, DeathLink, or the websocket
    connection to the AP server itself - give_item.py's functions are
    all plain synchronous/blocking calls, not coroutines.

    Opens its own short-lived bridge pipe connection for just this one
    grant rather than holding one open across ticks -
    dungeons_bridge.dll's pipe server is single-instance (see
    _connect_bridge_pipe's docstring: it "recreates its named pipe
    instance fresh after every single client disconnects"), so a
    long-held connection here would starve every other periodic bridge
    request (mission outcomes, chest events, death events...) for as
    long as it's held.

    On failure, leaves the grant at the front of the queue so the SAME
    one retries next tick - matches apply_item_reward/
    apply_location_reward's documented contract (never mark an index
    applied unless the call actually succeeded).

    A failure also arms a short cooldown (ITEM_GRANT_RETRY_COOLDOWN)
    before the NEXT attempt of any kind, rather than retrying on the very
    next tick. Without this, a grant that fails for a reason that isn't
    going to resolve itself moment-to-moment (item_lookup.py's table
    failing to load, say) would otherwise retry at full poll frequency
    forever - opening and closing a bridge pipe connection every single
    tick indefinitely, which competes with every other periodic bridge
    request (mission outcomes, chest events, death events) far more
    aggressively than any of this was ever designed to tolerate."""
    if not ctx.pending_item_grants:
        return
    if time.time() < ctx.next_item_grant_attempt_time:
        return

    absolute_index, item_name = ctx.pending_item_grants[0]
    num_slots = ctx.progressive_enchant_slot_tier

    def _do_grant():
        ctx.item_grant_pipe_busy = True
        try:
            # Menu/character-select/loading-screen guard: the PlayerController
            # has no possessed Pawn at all in any of those states (confirmed -
            # get_pawn already returns "No Pawn - character not spawned yet?"
            # for exactly this case elsewhere in this file). item_stash's own
            # address-stability check below (wait_for_stable_item_stash)
            # only proves the SAME pointer stayed valid for ~1.5s - it does
            # NOT prove that pointer belongs to a real, currently-playable
            # save slot rather than some transient/default component that
            # exists momentarily while the menu UI is up. Checking for a
            # real Pawn first is a much stronger "is the player actually in
            # a level right now" signal, and costs nothing extra since
            # get_pawn is a handful of cheap pointer reads.
            #
            # Raising here (rather than silently returning) is deliberate -
            # it routes through the SAME retry/cooldown/logging path as any
            # other grant failure below, so the reward is never marked
            # applied and gets retried automatically once a Pawn exists
            # again, with ITEM_GRANT_RETRY_COOLDOWN already preventing this
            # from hammering the menu at full poll frequency.
            _, pawn_error = get_pawn(pm, base)
            if pawn_error:
                raise RuntimeError(f"Player not in a level yet ({pawn_error}) - deferring reward.")

            pipe = _connect_bridge_pipe(pm)
            try:
                # wait_for_stable_item_stash (not the raw
                # get_item_stash_component lookup) is what's needed here:
                # confirmed via real testing that a menu/loading-screen
                # grant could get a momentarily-readable-but-about-to-be-
                # torn-down item_stash pointer, and writing to it right
                # then wiped the inventory instead of adding to it. Being
                # readable once was never proof it was safe to write to.
                item_stash, item_stash_class = wait_for_stable_item_stash(pm, base)

                # get_best_displayed_power's own docstring: "the stability
                # check alone did not prevent item loss during a loading
                # screen" - confirmed via real testing, and it's exactly
                # the class of bug reported here (inventory wiped right
                # around a hub/zone entry, well past the address-stability
                # window above). This was already built for exactly this
                # case but was never actually wired into the live grant
                # path - it just sat unused in give_item.py. Calling it
                # here, with check_for_drop=True, means: if the inventory
                # was ALREADY wiped or corrupted (by this or anything
                # else) before we even attempt to write, we find out and
                # stop NOW rather than writing a new item into a
                # still-settling/already-damaged container and making it
                # worse. PowerDropDetected is a RuntimeError subclass, so
                # it flows through the exact same retry/cooldown/logging
                # path as the Pawn check above - the reward stays
                # unmarked and gets retried automatically.
                get_best_displayed_power(pm, pipe, item_stash, item_stash_class, check_for_drop=True)

                if is_item_reward(item_name):
                    return apply_item_reward(pm, pipe, item_stash, item_stash_class, item_name, num_slots=num_slots)
                return apply_location_reward(pm, pipe, item_stash, item_stash_class, item_name, num_slots=num_slots)
            finally:
                win32file.CloseHandle(pipe)
        finally:
            ctx.item_grant_pipe_busy = False

    try:
        async with ctx.bridge_pipe_lock:
            item_name_index, granted_name, power = await asyncio.to_thread(_do_grant)
        game_logger.info(f"Item reward: {item_name} -> granted {granted_name} "
                          f"(power={power:.1f}, reward #{absolute_index}).")
        ctx.applied_reward_indices.add(absolute_index)
        save_applied_reward_indices(ctx.applied_reward_indices)
        ctx.pending_item_grants.pop(0)
    except Exception as e:
        # {e} alone can be uselessly terse for some exception types (a bare
        # IndexError just says "list index out of range" with zero context
        # on WHERE) - type name + full traceback makes the actual failure
        # point visible in the log without needing to reproduce it again
        # with a debugger attached.
        game_logger.info(f"Item reward {item_name!r} not applied yet - will retry.\n"
                          f"{type(e).__name__}: {e}\n"
                          f"{''.join(traceback.format_exception(type(e), e, e.__traceback__))}")
        ctx.next_item_grant_attempt_time = time.time() + ITEM_GRANT_RETRY_COOLDOWN


async def _check_and_maybe_send_goal(ctx: MCDungeonsContext) -> None:
    """Tells the server we've won, if we actually have - mirrors
    Regions.py's completion_condition exactly, using the same slot_data
    fields the server used to build it in the first place:

      - if Emerald Mode includes the goal (slot_data["emerald_is_goal"]),
        the top emerald milestone needs to have actually been SENT as a
        check (not just "your balance is high enough" - what matters is
        whether the server has heard about it yet, same as the real
        completion_condition, which only counts an item once its
        location has actually been checked)
      - if a Goal Mission is set (slot_data["goal_mission"]), that
        mission's own completion needs to have actually been sent too
      - if BOTH are set, you need both, same AND as the real one

    Sending {"cmd": "StatusUpdate", "status": 30} is what
    ClientStatus.CLIENT_GOAL means - the server auto-releases your
    remaining items (per this room's Release permission) and marks you
    goaled for everyone else once it sees this."""
    if ctx.sent_goal or not hasattr(ctx, "slot_data"):
        return

    emerald_is_goal = bool(ctx.slot_data.get("emerald_is_goal"))
    goal_mission = ctx.slot_data.get("goal_mission")
    if not emerald_is_goal and not goal_mission:
        return  # no win condition configured - shouldn't happen (generate_early
                 # requires at least one), but nothing to check for either way

    if emerald_is_goal:
        emerald_goal = ctx.slot_data.get("emerald_goal") or 0
        top_milestone = (emerald_goal // 500) * 500
        if top_milestone < 500 or top_milestone not in ctx.emerald_milestones_claimed:
            return

    if goal_mission and goal_mission not in ctx.known_mission_completed:
        return

    ctx.sent_goal = True
    game_logger.info("Goal reached - telling the server.")
    await ctx.send_msgs([{"cmd": "StatusUpdate", "status": 30}])


def _reset_pawn_tracking(ctx):
    """Forget the last announced character state - called on every fresh
    attach so the first reading after a (re)attach is always announced."""
    ctx.pawn_present = None
    ctx.pawn_state_since = None
    ctx.pawn_last_reason = ""
    ctx.pawn_candidate = None
    ctx.pawn_candidate_count = 0


def _track_pawn_presence(ctx, pawn, error):
    """Logs when the player's character (Pawn) appears or disappears.
    `pawn`/`error` are get_pawn's own return values - error is its
    human-readable reason for the absence (\"No UWorld\", \"No Pawn -
    character not spawned yet?\", ...), which is logged as-is so the
    message says WHICH link of the chain is missing.

    A change is only announced after PAWN_ANNOUNCE_DEBOUNCE identical
    readings in a row (0.4s at the normal tick rate), so one transient
    bad read doesn't produce a detected/absent flicker. Informational
    only: nothing in the client gates on this - every real guard still
    calls get_pawn itself at the moment it acts."""
    present = bool(pawn) and not error
    if present == ctx.pawn_candidate:
        ctx.pawn_candidate_count += 1
    else:
        ctx.pawn_candidate = present
        ctx.pawn_candidate_count = 1
    if not present:
        ctx.pawn_last_reason = error or "unknown"

    if ctx.pawn_candidate_count < PAWN_ANNOUNCE_DEBOUNCE or present == ctx.pawn_present:
        return

    now = time.time()
    previous = ctx.pawn_present
    since = ctx.pawn_state_since
    ctx.pawn_present = present
    ctx.pawn_state_since = now
    if present:
        if previous is False and since is not None:
            game_logger.info(f"Character DETECTED (pawn 0x{pawn:X}) - was absent for {now - since:.1f}s.")
        else:
            game_logger.info(f"Character DETECTED (pawn 0x{pawn:X}).")
    else:
        if previous is True and since is not None:
            game_logger.info(f"Character ABSENT: {ctx.pawn_last_reason} - was present for {now - since:.1f}s.")
        else:
            game_logger.info(f"Character ABSENT: {ctx.pawn_last_reason}")


async def game_watcher(ctx: MCDungeonsContext):
    """The main per-tick loop - runs as its own asyncio task alongside
    CommonContext's server/UI tasks, replacing the old client's blocking
    `while True: time.sleep(0.2)`. Everything here is Minecraft
    Dungeons-specific; connection/reconnect/item-receiving is handled by
    CommonContext itself."""

    # Outer attach/reattach loop. Originally this whole function attached
    # ONCE at startup and never checked again - fine for "start the game,
    # then start the client," but if Dungeons.exe crashes or is closed
    # and relaunched WITHOUT also restarting this client, every tick's
    # memory read starts failing against the now-dead pymem handle. Every
    # one of those reads is wrapped in its own broad try/except that just
    # `continue`s (see e.g. the `world = pm.read_longlong(...)` read
    # below), specifically so one bad read can't kill the whole task -
    # but that same safety net meant a genuinely dead process never
    # surfaced as anything at all: no error, no log, just every check
    # silently stopping forever until a human noticed and manually
    # restarted the client. Wrapping attach+inject+the tick loop in this
    # outer `while` and having the tick loop itself check process
    # liveness (via is_process_alive, not just catching read exceptions -
    # a dead handle doesn't reliably raise) means a relaunch is instead
    # just... a second pass through the exact same attach logic that
    # handled "open the client before the game" in the first place.
    last_attached_pid = None
    while not ctx.exit_event.is_set():
        # Attach retry loop - the old client called attach() once at
        # startup and crashed hard if Dungeons.exe wasn't running yet.
        # Retrying here instead means "open the client before the game"
        # (and now also "the game crashed and hasn't relaunched yet")
        # just works.
        while ctx.pm is None and not ctx.exit_event.is_set():
            try:
                ctx.pm, ctx.base = attach()
                ctx.game_state["attached"] = True
                _reset_pawn_tracking(ctx)
                game_logger.info(f"Attached to {_game_process_name()}.")
            except Exception as e:
                game_logger.info(f"Waiting for {_game_process_name()}... ({e})")
                await asyncio.sleep(ATTACH_RETRY_INTERVAL)

        if ctx.exit_event.is_set():
            break

        # Startup grace period, for a genuinely NEW process only (pid
        # differs from the last one we attached to) - attach() (via
        # pick_target_pid) succeeds the instant Dungeons.exe shows up in
        # the process list, which can be well before the game itself has
        # finished its own startup (module fully loaded, D3D device
        # created, etc). Traced a real "client launched before the game -
        # crashes; game launched before the client - fine" report to
        # exactly this: injecting (VirtualAllocEx/CreateRemoteThread) into
        # a process that's still THAT early in its own initialization is
        # inherently more fragile than injecting into one that's already
        # been running a while, which is what "launch the game first"
        # effectively gives you for free. Giving the game the same head
        # start here removes the need to rely on launch order at all.
        if ctx.pm.process_id != last_attached_pid:
            last_attached_pid = ctx.pm.process_id
            # Grace period disabled (0.0): A/B testing showed injecting after a
            # delay crashed the "client before game" scenario reproducibly while
            # injecting immediately never did. The DLL now protects its own
            # startup window instead: it is loaded at once but does NOT patch
            # ProcessEvent until the engine's first GWorld has been stable for
            # a few seconds (WaitForEngineBoot), and from then on the void
            # controller handles every level transition - see dungeons_bridge.cpp.
            if PROCESS_STARTUP_GRACE_PERIOD > 0:
                game_logger.info("New Dungeons.exe process - giving it a few seconds to finish "
                                  "starting up before injecting.")
                await asyncio.sleep(PROCESS_STARTUP_GRACE_PERIOD)

        pm, base = ctx.pm, ctx.base

        # Reset per-attach state so a reattach after a crash re-applies
        # everything a fresh process needs (progressive pickup tier,
        # zone/mission tracking) instead of assuming the new process
        # already matches what the old one had going into its death.
        ctx.progressive_pickups_unlocked = False
        ctx.villager_block_applied = None      # a fresh game/DLL has blocking off again: push it again (villagers.py)
        ctx.progressive_pickup_tier_applied = None
        ctx.secret_block_applied = None   # fresh DLL/process: re-push the SecretFound block
        ctx.dll_build_announced = False   # fresh DLL/process: log which build it is once
        ctx.level_gate_applied = None     # fresh DLL/process: re-push the level gate
        ctx.level_gate_retry_at = 0.0
        crash_diag.set_state_provider(lambda: {"zone": ctx.game_state.get("zone"), "loading": ctx.currently_loading,
                                               "pawn": ctx.pawn_present})
        ctx.current_zone_index = "__unset__"
        ctx.currently_loading = False
        ctx.pending_mission_outcome_zone = None
        ctx.pending_mission_outcome_triggers = set()
        ctx.gameover_trigger_pending = False
        ctx.death_already_sent_this_life = False
        ctx.mission_end_scan_snapshot = None

        try:
            inject_error = _ensure_bridge_dll_injected(pm.process_id)
        except Exception as e:
            # This must never be allowed to propagate uncaught - it used to,
            # and an unhandled exception here silently killed the whole
            # game_watcher task with nothing logged anywhere the GUI shows
            # (asyncio only prints "Task exception was never retrieved" to
            # stderr). Continuing without injection is a real degraded state
            # (boss/chest/emerald tracking won't work), but it's a state the
            # player can actually see and act on, unlike a task that's just
            # silently dead.
            inject_error = f"{type(e).__name__}: {e}"
        if inject_error:
            game_logger.info(f"dungeons_bridge.dll injection failed ({inject_error}) - "
                              f"boss kills, chests, and emeralds will not work this session. "
                              f"Try running as administrator.")
        else:
            game_logger.info("dungeons_bridge.dll injected (or already was).")
            asyncio.create_task(apply_saved_void_mode(ctx), name="ApplySavedVoidMode")

        # Startup catch-up sweeps - grant anything already true on this hero
        # before this session started (e.g. missions finished, or emeralds
        # already past a milestone, while nothing was watching). Same as the
        # pre-rewrite client's identical sweep, just done once here instead
        # of before entering its while-loop.
        #
        # Gated on the player pawn actually existing/stable first.
        # call_is_mission_completed makes a real remote call into the game
        # for every zone - doing that immediately after injection, before a
        # World/GameInstance/PlayerController/Pawn chain is even up, is
        # exactly the kind of "touching the game too early in its own
        # startup" that's been linked to real crashes in this project - this
        # is the confirmed fix for the "client already running/connected
        # before the game was launched -> crash shortly after injection"
        # history (a fixed-delay grace period on injection timing alone was
        # NOT sufficient - the sweep below is what actually needed to wait).
        # Polls get_pawn a few times rather than trusting a single read,
        # since a transient miss right at startup (loading screen, hub not
        # fully spawned in yet) shouldn't be mistaken for "no character
        # exists at all" - requires PAWN_STABILITY_MATCHES consecutive
        # identical non-null reads before treating the pawn as real. Skips
        # the sweep entirely (not a crash, not a retry-forever) if it never
        # stabilizes in time - individual mission completions are still
        # caught live via the normal poll loop once a real pawn does show
        # up, so nothing is permanently missed, just not backfilled from
        # before this session started.
        pawn_stable = False
        last_pawn = None
        stable_count = 0
        for _ in range(PAWN_STABILITY_ATTEMPTS):
            pawn, _err = await asyncio.to_thread(get_pawn, pm, base)
            if pawn and pawn == last_pawn:
                stable_count += 1
                if stable_count >= PAWN_STABILITY_MATCHES:
                    pawn_stable = True
                    break
            else:
                stable_count = 0
            last_pawn = pawn
            await asyncio.sleep(PAWN_STABILITY_POLL_INTERVAL)

        if not pawn_stable:
            game_logger.info("Pawn never stabilized - skipping the mission-completion "
                              "catch-up sweep this session (individual completions will "
                              "still be caught live as they happen).")
        elif IS_MISSION_COMPLETED_INDEX is not None:
            game_logger.info("Checking existing mission completion state...")
            # The sweep is only trusted if the function behind IS_MISSION_COMPLETED_INDEX behaves like
            # IsMissionCompleted. That index points into an unordered list of candidate functions, so
            # it can land on another function that answers True for everything - which used to mark
            # every mission complete and even send the goal. Two checks, nothing is sent before both pass:
            #  1) an impossible level value must answer False;
            #  2) the answers must not be True for (nearly) every zone.
            probe = await asyncio.to_thread(call_is_mission_completed, pm, base, "creeperwoods",
                                            None, SWEEP_PROBE_VALUE)
            sweep = {}
            if probe is False:
                for zone_name in MISSION_LOCATION_IDS:
                    if zone_name in ctx.known_mission_completed or zone_name in SWEEP_NEVER_FIRE_ZONES:
                        continue
                    # Threaded - same missing-read-timeout risk as the other
                    # pipe calls fixed elsewhere in this file.
                    sweep[zone_name] = await asyncio.to_thread(call_is_mission_completed, pm, base, zone_name)
            answered = [z for z, v in sweep.items() if v is not None]
            done = [z for z in answered if sweep[z] is True]
            if probe is not False:
                game_logger.info(f"Mission catch-up sweep skipped: the sanity probe answered {probe!r} "
                                 f"(expected False), so IsMissionCompleted could not be checked reliably "
                                 f"right now. Completions are still caught live.")
            elif len(answered) >= 10 and len(done) >= 0.9 * len(answered):
                game_logger.info(f"Mission catch-up sweep DISCARDED: it reported {len(done)} of "
                                 f"{len(answered)} missions as complete, which looks like the wrong game "
                                 f"function answering. Nothing was sent. Completions are still caught live.")
            else:
                for zone_name in done:
                    ctx.known_mission_completed.add(zone_name)
                    await fire_mission_complete(ctx, zone_name)

        if getattr(ctx, "slot_data", {}).get("emerald_goal"):
            # Real remote call into the game (remote thread, blocks until it
            # returns) - off the event loop like every other such call.
            current_emeralds, _err = await asyncio.to_thread(read_current_emeralds, pm, base)
            if current_emeralds is not None:
                if update_emerald_earned_total(ctx.emerald_earned_state, current_emeralds):
                    save_emerald_earned_state(ctx.emerald_earned_state)
                reached = _reached_emerald_milestones(ctx, ctx.emerald_earned_state["total_earned"])
                new_ones = [m for m in reached if m not in ctx.emerald_milestones_claimed]
                if new_ones:
                    game_logger.info(f"Catching up on {len(new_ones)} emerald milestone(s): {new_ones}")
                    await ctx.check_locations([_apw.get_emerald_milestone_id(m) for m in new_ones])
                    ctx.emerald_milestones_claimed.update(new_ones)

                # Best available heuristic for "this looks like a different
                # save than the progress we have on file" - NOT a reliable
                # per-zone check, see the big comment on this same limitation
                # a few lines up. Emerald balance is the one piece of live
                # game state that's actually confirmed working right now, so
                # it's what this leans on: a real continuing save practically
                # never sits at a tiny live balance while our persisted
                # cumulative total claims meaningful progress - that specific
                # combination is a much stronger signal of a fresh save than
                # of a player who happened to spend everything. Only ever a
                # suggestion, never an automatic reset - a wrong automatic
                # reset would silently throw away real progress, which is
                # worse than asking once.
                if (not ctx.emerald_reset_hint_shown and ctx.emerald_earned_state["total_earned"] >= 500
                        and current_emeralds < 50):
                    game_logger.info(
                        f"Heads up: you're showing {current_emeralds} emeralds in-game, but this client has "
                        f"{ctx.emerald_earned_state['total_earned']} recorded as earned. If this is actually a "
                        f"fresh Minecraft Dungeons save (not just low on emeralds), run /reset_progress."
                    )
                    ctx.emerald_reset_hint_shown = True

        await _check_and_maybe_send_goal(ctx)

        while not ctx.exit_event.is_set():
            await asyncio.sleep(GAME_POLL_INTERVAL)
            if not hasattr(ctx, "slot_data"):
                continue  # not connected/authenticated yet - nothing to do
            if ctx.item_grant_pipe_busy:
                continue  # a background item grant currently has the bridge pipe -
                           # skip this tick rather than fight it for the connection

            # Detect a crashed/closed/relaunched Dungeons.exe. Every read
            # below is wrapped in its own try/except that just continues
            # on failure (by design - one bad read shouldn't kill this
            # whole task), which means a merely-dead handle never raises
            # anything this loop would notice on its own. Checking
            # liveness explicitly is what actually catches it: break back
            # out to the attach loop above, which finds Dungeons.exe
            # again under whatever NEW pid it relaunched with, re-injects
            # the bridge DLL into it, and resumes - all without needing
            # the client itself restarted.
            import auto_inject
            if not auto_inject.is_process_alive(pm.process_id):
                game_logger.info(f"{_game_process_name()} is no longer running - waiting for it to relaunch...")
                crash_diag.on_game_gone(pm.process_id)
                try:
                    pm.close_process()   # an open handle keeps the dead process listed
                except Exception:
                    pass
                ctx.pm = None
                ctx.base = None
                ctx.game_state["attached"] = False
                _reset_pawn_tracking(ctx)
                break

            # Character presence marker - one cheap get_pawn per tick, off
            # the event loop like every other memory-touching call here.
            # A raised read (dead handle, mid-teardown) counts as absent.
            try:
                _pawn_ptr, _pawn_err = await asyncio.to_thread(get_pawn, pm, base)
            except Exception as _pawn_exc:
                _pawn_ptr, _pawn_err = None, f"read failed ({type(_pawn_exc).__name__})"
            _track_pawn_presence(ctx, _pawn_ptr, _pawn_err)

            try:
                # progressive pickups: force-unlock once per attach if the option
                # is off this seed (see the old client's identical comment on why
                # this can't just be the DLL's own default).
                if not ctx.progressive_pickups_unlocked and not bool(ctx.slot_data.get("progressive_pickups", False)):
                    # Threaded - see get_chest_open_events below for why:
                    # win32file.ReadFile inside set_pickup_tier is a blocking
                    # named-pipe read with no timeout.
                    async with ctx.bridge_pipe_lock:
                        ok, tier_error = await asyncio.to_thread(set_pickup_tier, pm, 3)
                    if ok:
                        ctx.progressive_pickups_unlocked = True
                        game_logger.info("Progressive pickups off this seed - all pickups unlocked.")
                elif (bool(ctx.slot_data.get("progressive_pickups", False))
                        and ctx.progressive_pickup_tier_applied != ctx.progressive_pickup_tier):
                    # Option is ON - push the player's actual earned tier
                    # (from received "Progressive Pickup" items) into the game.
                    # Only retries while the applied/target tiers disagree, so
                    # this doesn't hit the bridge pipe every tick once caught up.
                    # Threaded for the same reason as above.
                    async with ctx.bridge_pipe_lock:
                        ok, tier_error = await asyncio.to_thread(set_pickup_tier, pm, ctx.progressive_pickup_tier)
                    if ok:
                        ctx.progressive_pickup_tier_applied = ctx.progressive_pickup_tier

                await push_eye_block(ctx, pm)   # Eye of Ender checks: the real Eye must not be picked up too (eye_of_ender.py)
                await villagers_module.push_villager_block(ctx, pm)   # Villager checks: freed villagers must not unlock the merchant too (villagers.py)
                if not getattr(ctx, "dll_build_announced", False):   # once per attach: which DLL build, and is it current (dll_health.py)
                    ctx.dll_build_announced = True
                    async with ctx.bridge_pipe_lock:
                        for line in await asyncio.to_thread(dll_health.announce, pm):
                            game_logger.info(line)

                # --- Secret Mission Access: "Secret Zone Found" check ---
                # With the option on, the in-game secret trigger (lever, hidden
                # wall...) must NOT reveal the secret mission any more: the DLL
                # swallows SecretFound and we send the zone's "Secret Zone Found"
                # check instead (the mission itself comes from its Secret
                # Access item). The trigger sits in the secret zone's PARENT
                # mission, so the current zone tells us which check it is.
                if bool(ctx.slot_data.get("secret_missions_require_secret", False)):
                    # Blocking is best-effort: an OLD dungeons_bridge.dll (not rebuilt
                    # from the current .cpp) can't block, but it DOES queue SecretFound
                    # events, so detection must never depend on blocking succeeding.
                    # Without blocking the game still unlocks the mission itself,
                    # but the "Secret Zone Found" check is sent either way.
                    want_level = getattr(ctx, "secret_block_level", 0)
                    if want_level == 0 and getattr(ctx, "secret_block_applied", None) is None:
                        pass   # default: no blocking at all - hidden chests etc. must keep working
                    elif getattr(ctx, "secret_block_applied", None) != want_level:
                        async with ctx.bridge_pipe_lock:
                            ok, block_error = await asyncio.to_thread(set_secret_block, pm, want_level)
                        if ok:
                            ctx.secret_block_applied = want_level
                            game_logger.info(f"SecretFound blocking level {want_level} ON (secret missions now come from "
                                              "items; finding the secret sends a check).")
                        elif not getattr(ctx, "secret_block_warned", False):
                            ctx.secret_block_warned = True
                            game_logger.info(f"Could not enable SecretFound blocking ({block_error}) - rebuild "
                                              "dungeons_bridge.dll from the current .cpp. Secret finds are still "
                                              "detected and sent as checks, but the game will also unlock the mission.")
                    async with ctx.bridge_pipe_lock:
                        secret_events = await asyncio.to_thread(get_secret_events, pm) or []
                    # Only ShowSecretMissionUnlocked counts: plain SecretFound also fires
                    # for hidden chests etc. And ignore the popup that OUR OWN
                    # ClientUnlockBonusMission (Secret Access item / test commands) causes.
                    own_recently = (time.time() - dungeons_reader_module.LAST_OWN_UNLOCK_NOTIFY) < 6.0
                    found_popup = [e for e in secret_events if e[2] == "ShowSecretMissionUnlocked"]
                    if found_popup and not own_recently:
                        zone_for_secret = ctx.game_state.get("zone")
                        if zone_for_secret not in _apw.SECRET_PARENT_TO_ZONE:
                            zone_for_secret = ctx.last_mission_zone   # popup shown back in the hub
                        await fire_secret_zone_found(ctx, zone_for_secret)
                    elif secret_events and ctx.debug_interact_logging:
                        game_logger.info(f"{len(secret_events)} secret event(s) ignored "
                                          f"({'own popup' if own_recently else 'not a mission unlock'}).")
                elif getattr(ctx, "secret_block_applied", None) not in (None, 0):
                    # Connected to a seed WITHOUT the option after one with it:
                    # the DLL is still blocking - switch the base-game behaviour back on.
                    async with ctx.bridge_pipe_lock:
                        ok, _block_error = await asyncio.to_thread(set_secret_block, pm, 0)
                    if ok:
                        ctx.secret_block_applied = None
                        game_logger.info("SecretFound blocking OFF (option disabled for this seed).")

                death_link_enabled = bool(ctx.slot_data.get("death_link", True))
                watch_boss_kills_enabled = bool(ctx.slot_data.get("boss_kill_checks", False)) or \
                    bool(ctx.slot_data.get("dlc_boss_kill_checks", False))
                watch_mini_boss_enabled = mini_boss_checks_active(ctx)   # option: none / per_type / per_zone / both

                # --- boss kills (dungeons_bridge.dll OnCharacterDeath events) ---
                death_events = []
                if watch_boss_kills_enabled or watch_mini_boss_enabled or POWERFUL_MOB_LOGGING:
                    # Threaded - same reasoning as get_chest_open_events below:
                    # runs every tick, unthreaded it's a live risk of freezing
                    # the whole client (and its connection to the AP server)
                    # on a stuck pipe read.
                    async with ctx.bridge_pipe_lock:
                        death_events = await asyncio.to_thread(get_death_events, pm) or []
                    for _cand_addr in death_events:
                        try:
                            _cand_type = dungeons_reader_module.get_actor_entity_type(pm, _cand_addr)
                        except Exception:
                            _cand_type = None
                        if _cand_type in BOSS_CANDIDATE_ENTITY_TYPES:
                            game_logger.info(f"BOSS CANDIDATE DIED: {BOSS_CANDIDATE_ENTITY_TYPES[_cand_type]} "
                                             f"(EntityType {_cand_type}) in zone {ctx.game_state.get('zone')}")
                    if watch_boss_kills_enabled:
                        for addr in death_events:
                            boss_name = resolve_boss_for_actor(pm, addr)
                            if boss_name:
                                await fire_boss_kill(ctx, boss_name)
                if POWERFUL_MOB_LOGGING or watch_mini_boss_enabled:
                    await powerful_mob_tick(ctx, pm, base, death_events)
                if watch_mini_boss_enabled:
                    await mini_boss_kill_tick(ctx)
                if ctx.slot_data.get("eye_of_ender_checks", False):
                    eye_of_ender_tick(ctx, pm, base)
                if ctx.slot_data.get("villager_checks", False):
                    await villagers_module.villager_tick(ctx, pm)
                if ZONE_START_CHEST_COUNT_LOGGING:
                    zone_start_chest_count_tick(ctx, pm, base)

                # zone chest events are drained here but attributed to a zone
                # below, once this tick's current zone is actually known.
                #
                # Threaded - NOT called directly. win32file.ReadFile inside
                # get_chest_open_events is a blocking named-pipe read with NO
                # TIMEOUT at all. This runs every tick; if the DLL ever fails
                # to answer for any reason (a transient race, a busy pipe),
                # an unthreaded call here hangs the entire client indefinitely
                # - including outgoing AP server keepalives, which is exactly
                # what produces a "Lost connection... keepalive ping timeout"
                # disconnect with no exception or error message anywhere to
                # explain why. Confirmed as a real bug via a traced session
                # (same class of issue as the injection-on-connect fix above).
                async with ctx.bridge_pipe_lock:
                    chest_events, chest_events_err = await asyncio.to_thread(get_chest_open_events, pm)
                chest_events = chest_events or []
                if chest_events_err and time.time() - ctx.last_chest_error_log >= 10.0:
                    # This error was silently discarded before (chest_events
                    # falling back to [] on failure, same as a legitimate
                    # "nothing happened" tick, with zero way to tell the two
                    # apart from the log) - confirmed as a real gap: a player
                    # reported opening 2 chests with nothing detected and NO
                    # error anywhere in the log to explain why. Throttled
                    # rather than logged every tick, since a real failure
                    # (e.g. the pipe momentarily busy) would otherwise spam
                    # at GAME_POLL_INTERVAL.
                    ctx.last_chest_error_log = time.time()
                    game_logger.info(f"get_chest_open_events failed: {chest_events_err}")

                # --- zone tracking ---
                try:
                    world = pm.read_longlong(base + OFFSETS["gworld"])
                except Exception:
                    continue
                if not world:
                    continue
                zone_index = get_zone_name_index(pm, world)
                if zone_index == 0:
                    # index 0 is the loading-screen transition state, not a
                    # real zone - not in ZONE_NAME_LOOKUP at all, so it used to
                    # fall through to "unknown_zone_0" and get logged/tracked
                    # like any other zone. Ignored entirely instead: don't
                    # update current_zone_index (so the REAL zone that follows
                    # once loading finishes still triggers this block normally
                    # and gets its own "Entered zone" line), and don't touch
                    # game_state/last_mission_zone/the outcome queue - all of
                    # that stays exactly as it was going into the loading
                    # screen.
                    ctx.currently_loading = True
                else:
                    # zone_index is nonzero (a real zone or the hub) - not
                    # currently loading, whether or not it's the SAME zone
                    # as last tick. That "same zone" case matters: quitting
                    # to camp/menu and resuming the same mission from a
                    # checkpoint is also a zone_index==0 loading screen in
                    # between, but comes back out to the SAME zone_index
                    # afterward - if this were only cleared in the
                    # "zone changed" branch below, currently_loading would
                    # get stuck True forever after a same-zone reload, and
                    # every item grant would stay blocked for the rest of
                    # the run.
                    ctx.currently_loading = False
                if zone_index not in (0, ctx.current_zone_index):
                    ctx.current_zone_index = zone_index
                    # zone_index is None outside any mission - not unresolved,
                    # just means "no mission-singleton actor to read a zone
                    # off of", which is the camp/hub.
                    zone_name = ZONE_NAME_LOOKUP.get(zone_index, f"unknown_zone_{zone_index}") if zone_index is not None else "hub"
                    ctx.game_state["zone"] = zone_name
                    crash_diag.note_level_change(zone_name)
                    game_logger.info(f"Entered zone: {zone_name}")
                    ctx.unconfirmed_chest_run = None   # a new run of a mission starts a new chest tally
                    if zone_name in ZONE_ID_ORDER or zone_name.startswith("unknown_zone_"):
                        # A mission whose zone index isn't in zone_name_lookup.json (an unnamed DLC
                        # zone) must NOT inherit the previous mission: its completion and rewards
                        # would be credited to whatever zone was played before (e.g. Creepy Crypt).
                        # Unknown zone -> no zone at all, so nothing is attributed until it is named.
                        ctx.last_mission_zone = zone_name if zone_name in ZONE_ID_ORDER else None
                        if ctx.last_mission_zone is None:
                            game_logger.info(
                                f"WARNING: {zone_name} is not in zone_name_lookup.json - this mission's "
                                f"completion and chests will not be credited to any zone. Add its index "
                                f"to client/data_seeds/zone_name_lookup.json.")
                        ctx.last_locked_zone_warned = None
                        ctx.pending_lock_kill_zone = None
                        # New mission attempt starting. Anything still queued in
                        # the DLL's outcome-trigger buffer belongs to whatever
                        # run just ended (a finish/fail that fired but hasn't
                        # been drained yet) - drain and discard it here rather
                        # than letting the next tick misattribute it to this
                        # fresh attempt. Combined with resetting
                        # pending_mission_outcome_zone, this run starts clean:
                        # nothing is considered completed until a genuine new
                        # trigger + confirm happens for THIS attempt. Fire-
                        # and-forget threaded - result is discarded either
                        # way, no need to await it, but still shouldn't block
                        # this tick on a potentially-stuck pipe read.
                        asyncio.create_task(
                            _drain_mission_outcome_events_locked(ctx),
                            name="DrainMissionOutcomeEvents",
                        )
                        ctx.pending_mission_outcome_zone = None
                        ctx.pending_mission_outcome_triggers = set()
                        ctx.gameover_trigger_pending = False
                        ctx.mission_end_scan_snapshot = None
                        # Re-arm DeathLink sending for this fresh attempt - see
                        # death_already_sent_this_life's own comment below for
                        # why this exists.
                        ctx.death_already_sent_this_life = False

                zone_name = ctx.game_state["zone"]

                # --- zone chests: resolve each event's OWN zone from the world_ptr
                # the DLL captured at the exact moment it fired, not from this
                # tick's polled zone - a transition between the interact and this
                # poll draining the queue could otherwise misattribute the chest.
                if chest_events:
                    ZONE_CHEST_COUNTS = _apw.ZONE_CHEST_COUNTS
                    for event in chest_events:
                        # Resolve the event's OWN zone from the world_ptr the DLL
                        # captured at the exact moment it fired (not this tick's
                        # polled zone) - a transition between the interact and
                        # this poll draining the queue could otherwise
                        # misattribute it. Shared by every branch below.
                        if event.get("world_ptr"):
                            event_zone_index = get_zone_name_index(pm, event["world_ptr"])
                            event_zone_name = (ZONE_NAME_LOOKUP.get(event_zone_index, f"unknown_zone_{event_zone_index}")
                                                if event_zone_index is not None else None)
                        else:
                            event_zone_name = zone_name  # older DLL without the worldPtr fix

                        kind = classify_interactable_class(event["class_name"])

                        if kind not in ("chest", "supply"):
                            # Obsidian Pinnacle (and possibly other raid-style
                            # missions) ends via interacting with its own
                            # unique "EndgameClicky" actor rather than the
                            # normal MulticastMissionFinished/IsMissionCompleted
                            # confirmation flow actually resolving True -
                            # confirmed via a real log: the interact fires,
                            # MulticastMissionFinished fires right after it, but
                            # the usual confirm-by-polling never lands on
                            # "complete" for this mission. Treat this specific
                            # interact AS the completion signal directly,
                            # bypassing that confirmation entirely for zones
                            # where it doesn't reliably resolve.
                            #
                            # Gated on ENDGAME_CLICKY_ZONES (not just the class
                            # name matching "endgameclicky") on purpose: the
                            # class-name check alone can't rule out the same
                            # Blueprint being reused elsewhere as a purely
                            # decorative/unrelated prop in some other zone -
                            # if it were, an interact with it there would
                            # falsely fire that OTHER zone's Mission Complete
                            # before the player actually finished it. Requiring
                            # BOTH the class-name match AND the event's own
                            # resolved zone being in this explicit, confirmed
                            # set closes that gap - only fires for zones we've
                            # actually observed this mechanism on. Add more
                            # zone names here only once actually confirmed via
                            # a real log, same as obsidianpinnacle was.
                            if ("endgameclicky" in event["class_name"].lower()
                                    and event_zone_name in ENDGAME_CLICKY_ZONES):
                                if event_zone_name not in ctx.known_mission_completed:
                                    game_logger.info(f"Mission complete (endgame interact): {event_zone_name}")
                                    ctx.known_mission_completed.add(event_zone_name)
                                    await fire_mission_complete(ctx, event_zone_name)
                                continue
                            if ctx.debug_interact_logging:
                                game_logger.info(f"Interacted with {event['class_name']!r} (not tracked)")
                            continue

                        if event_zone_name in ZONE_CHEST_COUNTS:
                            await fire_chest_open(ctx, event_zone_name, kind)
                        else:
                            _note_unconfirmed_chest(ctx, event_zone_name, kind, event["class_name"])
                        try:
                            await zone_miniboss_on_chest(ctx, event_zone_name, event)
                        except Exception as e:      # never let the mini boss rule break chest handling
                            game_logger.info(f"Zone mini boss: failed ({type(e).__name__}: {e})")

                # --- level lock enforcement (full predecessor chain) ---
                # Squid Coast is exempt: it's the game's actual mandatory
                # starting mission - every fresh save begins physically
                # inside it, before receiving a single item, including its
                # own "Squid Coast Access" (which is itself typically found
                # from a chest INSIDE Squid Coast). Enforcing the lock here
                # would kill a brand new player on their very first second of
                # play, before they could ever reach the chest that unlocks
                # it - confirmed happening in practice, not just theoretical.
                # Every other zone genuinely requires having progressed to
                # reach it physically, so the enforcement stays real for them
                # (including the Ancient Hunt zones, which also have no
                # ZoneData predecessor but - unlike Squid Coast - are never
                # the zone a player starts in; reaching one without its
                # access item is exactly the exploit case this is for).
                if (zone_name != STARTING_ZONE and zone_name in MISSION_ACCESS_ITEM_IDS
                        and not is_zone_truly_unlocked(zone_name, ctx.unlocked_zones)):
                    if ctx.last_locked_zone_warned != zone_name:
                        missing = (missing_zone_items(zone_name, ctx.unlocked_zones)
                                   or [z for z in [zone_name] + MISSION_REQUIRES.get(zone_name, [])
                                       if not is_zone_truly_unlocked(z, ctx.unlocked_zones)])
                        game_logger.info(f"Locked mission '{zone_name}' - missing: {missing}")
                        ctx.last_locked_zone_warned = zone_name
                        ctx.pending_lock_kill_zone = zone_name
                        ctx.pending_lock_kill_until = time.time() + LOCK_KILL_WINDOW_S
                        ctx.lock_kill_count = 0
                        ctx.lock_kill_next_at = 0.0
                    # The kill is attempted once loading is over and the bridge
                    # hook is active; if it is refused (silent hook / loading)
                    # it is retried for up to 60 s rather than skipped, so the
                    # level lock can't be bypassed by a transition-timing miss.
                    if (ctx.pending_lock_kill_zone == zone_name and not ctx.currently_loading
                            and time.time() < ctx.pending_lock_kill_until
                            and time.time() >= ctx.lock_kill_next_at):
                        ctx.suppress_death_link_until = time.time() + MCDungeonsContext.SUPPRESS_DEATH_LINK_WINDOW
                        lock_kill_ok, _lock_kill_err, _lock_diag = kill_local_player(pm, base)
                        if lock_kill_ok:
                            ctx.lock_kill_count += 1
                            # Wait for the respawn, then kill again if the player
                            # is still in this zone (extra lives). The zone check
                            # above stops it as soon as they are back in camp.
                            ctx.lock_kill_next_at = time.time() + LOCK_KILL_RESPAWN_WAIT_S
                            game_logger.info(f"Locked mission '{zone_name}': kill #{ctx.lock_kill_count} applied.")
                            if ctx.lock_kill_count >= LOCK_KILL_MAX_KILLS:
                                ctx.pending_lock_kill_zone = None
                                game_logger.info(f"Locked mission '{zone_name}': stopped after "
                                                  f"{LOCK_KILL_MAX_KILLS} kills.")

                # --- delayed incoming-DeathLink kill (was refused during a
                # loading screen / silent hook - see on_deathlink) ---
                if ctx.deathlink_kill_retry_until and not ctx.currently_loading:
                    if time.time() >= ctx.deathlink_kill_retry_until:
                        ctx.deathlink_kill_retry_until = 0.0
                    else:
                        ctx.suppress_death_link_until = time.time() + MCDungeonsContext.SUPPRESS_DEATH_LINK_WINDOW
                        dl_ok, _dl_err, _dl_diag = kill_local_player(pm, base)
                        if dl_ok:
                            ctx.deathlink_kill_retry_until = 0.0
                            game_logger.info("Delayed DeathLink kill applied.")
                            if ctx.deathimg_enabled:
                                async_start(_run_death_image(ctx, None, ctx.deathimg_path), name="DeathImage")

                # --- mission completion: event-driven, not polled -----------
                # The DLL trigger (MulticastMissionFinished / OnShowMissionVictory
                # / MulticastGameOver - see dungeons_bridge.cpp) only means "a
                # mission run just concluded, somehow." It is NEVER trusted by
                # itself: a totem-loss failure may well fire the same trigger as
                # a real win. All it does is arm pending_mission_outcome_zone,
                # which starts a confirmation check - only that decides
                # completed or not. This is also what makes a retry safe: the
                # trigger that discards on zone-entry (above) means a run that
                # ends in failure simply leaves the zone un-completed, ready to
                # be re-armed cleanly the next time that mission is entered.
                #
                # PRIMARY confirmation: MISSION_END_WIDGET_CLASS actually
                # appearing in GObjects. This was fully proven out standalone
                # (dungeons_reader.py's watch_mission_end tool, before this
                # client was unified) - absent on every tested failure, present
                # on every tested win - and unlike call_is_mission_completed
                # below, it doesn't need OFFSETS["mission_progress_component"]
                # at all, which was never actually found. That's why this is
                # checked first, not call_is_mission_completed.
                # Threaded - same reasoning as get_chest_open_events above:
                # this runs every tick, unthreaded it's a live risk of
                # freezing the whole client on a stuck pipe read.
                async with ctx.bridge_pipe_lock:
                    outcome_events, _outcome_err = await asyncio.to_thread(get_mission_outcome_events, pm)
                if outcome_events and ctx.last_mission_zone and ctx.last_mission_zone not in ctx.known_mission_completed:
                    for evt in outcome_events:
                        game_logger.info(f"Mission-outcome trigger ({evt.get('trigger_name', '?')}) for "
                                          f"{ctx.last_mission_zone} - confirming...")
                    if ctx.pending_mission_outcome_zone != ctx.last_mission_zone:
                        ctx.pending_mission_outcome_triggers = set()
                        # Fresh baseline the moment we start watching for it, so
                        # the first diff below only catches genuinely NEW objects
                        # from this point on, not GObjects churn accumulated
                        # earlier.
                        ctx.mission_end_scan_snapshot = get_gobjects_pointer_snapshot(pm, base)
                    ctx.pending_mission_outcome_zone = ctx.last_mission_zone
                    ctx.pending_mission_outcome_triggers.update(
                        evt.get("trigger_name") for evt in outcome_events if evt.get("trigger_name"))
                    # DeathLink, fast path: MulticastGameOver fires specifically
                    # on a totem-loss failure (never alongside OnShowMissionVictory
                    # - see the win-combo comment below) and is event-driven via
                    # the DLL's ProcessEvent hook, so it lands the instant the
                    # game itself signals game over. This is now the SOLE signal
                    # for outgoing DeathLink - an earlier widget-scan/health-poll
                    # approach was removed after producing false positives.
                    if any(evt.get("trigger_name") == "MulticastGameOver" for evt in outcome_events):
                        ctx.gameover_trigger_pending = True

                if (ctx.pending_mission_outcome_zone
                        and ctx.pending_mission_outcome_zone not in ctx.known_mission_completed):
                    zone_to_confirm = ctx.pending_mission_outcome_zone
                    widget_confirmed = False
                    if MISSION_END_WIDGET_CLASS is not None:
                        new_snapshot = get_gobjects_pointer_snapshot(pm, base)
                        if new_snapshot:
                            old_snapshot = ctx.mission_end_scan_snapshot or {}
                            for idx, after_ptr in new_snapshot.items():
                                before_ptr = old_snapshot.get(idx, 0)
                                if not after_ptr or after_ptr == before_ptr:
                                    continue
                                if get_uobject_class_name_index(pm, after_ptr) == MISSION_END_WIDGET_CLASS:
                                    widget_confirmed = True
                                    break
                            ctx.mission_end_scan_snapshot = new_snapshot

                    # THIRD path - trusted trigger combo, no memory read needed
                    # at all: MulticastMissionFinished AND OnShowMissionVictory
                    # BOTH firing together is specifically what a real win looks
                    # like (confirmed in-game across two separate runs) -
                    # MulticastGameOver, seen on an actual death, never joins
                    # them. This exists because MISSION_END_WIDGET_CLASS's
                    # class_name_index can go stale across game builds (it's an
                    # Unreal FName pool index, reassigned on engine/content
                    # updates) with no error, just silent non-confirmation
                    # forever - this catches that case without needing yet
                    # another memory offset.
                    trigger_confirmed = {"MulticastMissionFinished", "OnShowMissionVictory"} <= ctx.pending_mission_outcome_triggers

                    if widget_confirmed or trigger_confirmed:
                        ctx.known_mission_completed.add(zone_to_confirm)
                        await fire_mission_complete(ctx, zone_to_confirm)
                        ctx.pending_mission_outcome_zone = None
                        ctx.pending_mission_outcome_triggers = set()
                    elif "mission_progress_component" in OFFSETS and IS_MISSION_COMPLETED_INDEX is not None:
                        # Fallback path - only reachable once that offset is
                        # actually found and added (see find_mission_progress_component).
                        # Remote call into the game - must not run on the event loop.
                        result = await asyncio.to_thread(call_is_mission_completed, pm, base, zone_to_confirm)
                        if result is True:
                            ctx.known_mission_completed.add(zone_to_confirm)
                            await fire_mission_complete(ctx, zone_to_confirm)
                            ctx.pending_mission_outcome_zone = None
                            ctx.pending_mission_outcome_triggers = set()
                        elif result is False:
                            game_logger.info(f"Mission run ended without completing {zone_to_confirm} "
                                              f"(totem loss / quit) - not reporting.")
                            ctx.pending_mission_outcome_zone = None
                            ctx.pending_mission_outcome_triggers = set()
                        # else: errored (e.g. the transition/loading-screen memory
                        # read issue noted elsewhere) - leave pending set, retry
                        # next tick.
                    # else: neither confirmation has landed yet this tick - leave
                    # pending set and try again next tick. Cheap, no error, no
                    # spam. Naturally resolved either by a confirmation landing
                    # (win) or by the player leaving the zone (failure/quit -
                    # discarded by the zone-entry reset above).

                # --- outgoing deathlink (UHealthComponent, GAS fallback) ---
                if death_link_enabled:
                    pawn, _ = get_pawn(pm, base)
                    health, max_health = None, None
                    if pawn:
                        # Primary: UHealthComponent via the fixed, native
                        # offset chain (pawn -> HealthComponent pointer ->
                        # Health/MaxHealth floats) - same fix already applied
                        # to dungeons_reader.py's watch_deathlink. Doesn't
                        # depend on HEALTH_ATTRIBUTE_SET_CLASS ever getting
                        # confirmed this session, unlike the GAS path below.
                        try:
                            health_component = pm.read_longlong(pawn + OFFSETS["player_health_component"])
                            if health_component:
                                health = pm.read_float(health_component + OFFSETS["health_component_health"])
                                max_health = pm.read_float(health_component + OFFSETS["health_component_max_health"])
                            else:
                                game_logger.debug("DeathLink: health_component NULL (offset may need "
                                                   "reconfirming for this build)")
                        except Exception as _e:
                            health = None
                            game_logger.debug(f"DeathLink: UHealthComponent read failed: {_e}")

                        # Fallback: old GAS/SpawnedAttributes path.
                        if health is None and HEALTH_ATTRIBUTE_SET_CLASS is not None:
                            entries = get_spawned_attributes(pm, pawn)
                            attr_set = next((p for p, c in entries if c == HEALTH_ATTRIBUTE_SET_CLASS), None)
                            if attr_set:
                                try:
                                    health = pm.read_float(attr_set + OFFSETS["health_attr_health"])
                                    max_health = pm.read_float(attr_set + OFFSETS["health_attr_max_health"])
                                except Exception:
                                    pass

                    died = False
                    death_reason = ""

                    # SOLE signal: the MulticastGameOver trigger captured above,
                    # event-driven via the DLL's ProcessEvent hook - fires
                    # specifically on a totem-loss mission failure. The
                    # UUMG_YouDiedHUD_C widget-scan path and the health-poll
                    # heuristic were removed here: both were producing false
                    # positives, so DeathLink now only fires on a confirmed
                    # mission failure instead of guessing from HUD/health state.
                    if ctx.gameover_trigger_pending:
                        died = True
                        death_reason = "MulticastGameOver trigger (mission failure)"
                        ctx.gameover_trigger_pending = False

                    if died and ctx.death_already_sent_this_life:
                        # Already reported this life's death (almost always via
                        # the fast MulticastGameOver trigger) - the slower HUD/
                        # health-poll signals can still fire afterwards since
                        # they don't know that, so swallow them here instead of
                        # relying solely on DEATH_LINK_COOLDOWN, which a slow
                        # widget rediscovery can legitimately outlast (that's
                        # exactly what produced a duplicate send in practice -
                        # confirmed via user-provided Launcher log: two "You
                        # died." lines 15s apart, both for the same death).
                        died = False
                    elif died and time.time() < ctx.suppress_death_link_until:
                        # This death is one we caused ourselves - a locked-
                        # mission kill, or a DeathLink we just received -
                        # not a real one, so it doesn't get sent back out.
                        # Cleared immediately (not left to expire on its own)
                        # so a second, genuinely real death moments later
                        # isn't also swallowed by the same window.
                        game_logger.info("Died (locked mission or received DeathLink) - not sending DeathLink.")
                        ctx.suppress_death_link_until = 0.0
                    elif died and time.time() - ctx.last_death_link_sent >= DEATH_LINK_COOLDOWN:
                        game_logger.info(f"You died. ({death_reason})" if death_reason else "You died.")
                        # send_death()'s signature has changed across
                        # Archipelago versions - some accept a custom death
                        # message, some don't take any argument at all. Try
                        # the newer form first and fall back automatically,
                        # so this works either way instead of silently
                        # failing on whichever version happens to be
                        # installed (which is exactly what was happening
                        # before this fix - the TypeError was getting
                        # swallowed by this loop's own error handling below,
                        # so sending looked like it was just doing nothing).
                        #
                        # "{name} died" is what this text becomes for
                        # everyone ELSE in the multiworld - other MCDungeonsGame
                        # clients show it via on_deathlink's own "{source} died"
                        # line above, and any other game's client falls back to
                        # CommonContext's generic "DeathLink: {cause}" logging,
                        # which reads the same way. The player who actually died
                        # never sees this text themselves - they get the plain
                        # "You died." logged just above instead.
                        try:
                            await ctx.send_death(f"{ctx.auth} died")
                        except TypeError:
                            await ctx.send_death()
                        ctx.last_death_link_sent = time.time()
                        ctx.death_already_sent_this_life = True

                    if health is not None:
                        ctx.last_health = health
                        ctx.last_max_health = max_health
                        ctx.game_state["health"] = health
                        ctx.game_state["max_health"] = max_health

                # --- pending emerald filler grants (100/300/500 Emeralds
                # items received) - retried every tick until
                # apply_emerald_reward succeeds (requires the wallet
                # component to already be resolvable - see its own
                # docstring).
                #
                # Trusts a successful call immediately - no more
                # stability-check delay here. That delay used to exist
                # specifically to catch a RAW MEMORY WRITE getting
                # silently reverted by a zone-load reloading the
                # on-disk save state a few seconds later. apply_emerald_
                # reward no longer writes memory at all - it calls the
                # game's own UWalletComponent::ClientAdd UFUNCTION,
                # the exact same NetClient/NetReliable function the
                # game's own pickup code calls, so a successful call IS
                # the game accepting the pickup through its own real,
                # replicated flow - the same trust level as a real
                # emerald pile. The old post-grant stability-check pass
                # (re-reading the balance a few seconds later to catch a
                # zone-load reverting a raw memory write) is gone
                # entirely now - there's nothing left for it to catch.
                if ctx.pending_emerald_grants and not ctx.pawn_present and crash_diag.safe_to_mutate():
                    # Confirmed root cause of a permanent earned/actual gap: get_wallet_
                    # component()'s own get_pawn() call is a single unstabilized read. In
                    # the window before the player has actually picked their character
                    # (still on the select/preview screen), it can resolve a transient
                    # Pawn that already has a live WalletComponent - ClientAdd succeeds
                    # against it, the reward gets marked applied (never retried), but
                    # that transient Pawn/wallet doesn't survive into the real hub
                    # character, so the emeralds never reach the actual save. total_earned
                    # then tracks the real (now permanently short) balance faithfully,
                    # which is exactly what makes the gap look "fixed" - it's not drift,
                    # it's one grant that landed on a wallet that was thrown away.
                    #
                    # ctx.pawn_present is the SAME debounced signal every other grant
                    # path (_apply_next_pending_item_grant et al.) already waits to
                    # settle on - PAWN_ANNOUNCE_DEBOUNCE consecutive identical readings
                    # before flipping - unlike a raw one-shot get_pawn() call. Simply not
                    # attempting emerald grants until it reads True closes this window:
                    # nothing is lost, the grant just waits one tick.
                    game_logger.info(f"{len(ctx.pending_emerald_grants)} emerald grant(s) "
                                      f"held back - character not confirmed present yet.")
                elif ctx.pending_emerald_grants:
                    still_pending = []
                    for absolute_index, amount in ctx.pending_emerald_grants:
                        # Remote call into the game - must not run on the event loop.
                        success, err = await asyncio.to_thread(apply_emerald_reward, pm, base, amount)
                        if success:
                            ctx.applied_reward_indices.add(absolute_index)
                            save_applied_reward_indices(ctx.applied_reward_indices)
                            game_logger.info(f"Granted {amount} emeralds (reward #{absolute_index}) "
                                              f"via WalletComponent::ClientAdd.")
                        else:
                            game_logger.info(f"Emerald grant #{absolute_index} not applied this tick: "
                                              f"{err} - will retry.")
                            still_pending.append((absolute_index, amount))
                    ctx.pending_emerald_grants = still_pending

                if ctx.pending_eye_grants and ctx.pawn_present and crash_diag.safe_to_mutate():   # "Eye of Ender" items -> real Eyes (eye_of_ender.py)
                    await grant_pending_eyes(ctx, pm, base, save_applied_reward_indices)

                if ctx.pending_villager_grants and ctx.pawn_present and crash_diag.safe_to_mutate():   # camp merchant items -> merchant unlocked (villagers.py)
                    await villagers_module.grant_pending_villagers(ctx, pm, base, save_applied_reward_indices)

                # Native "mission unlocked" popups queued by ReceivedItems.
                # Same debounced pawn_present gate as the grants above, and
                # spaced out so several items arriving at once (backlog on
                # connect) don't stack their popups on top of each other.
                if (ctx.pending_mission_popups and ctx.pawn_present
                        and not getattr(ctx, "currently_loading", False)
                        and time.time() - ctx.last_mission_popup_time >= MISSION_POPUP_SPACING):
                    popup_zone, full_unlock = ctx.pending_mission_popups[0]
                    # full_unlock: UnlockBonusMission (state) + popup.
                    # otherwise:   popup only (notify_only=True).
                    state_ok, notify_ok = await asyncio.to_thread(
                        apply_secret_zone_unlock, pm, base, popup_zone,
                        True, not full_unlock, game_logger.info)
                    if full_unlock and not state_ok:
                        notify_ok = False   # state change failed -> retry the whole thing
                    if notify_ok:
                        ctx.pending_mission_popups.pop(0)
                        ctx.last_mission_popup_time = time.time()
                        game_logger.info(f"Native unlock popup shown for {popup_zone}"
                                         f"{' (zone really unlocked)' if full_unlock else ''}.")
                        ctx.mission_popup_fails = 0
                    else:
                        ctx.last_mission_popup_time = time.time()  # retry after the spacing delay
                        ctx.mission_popup_fails = getattr(ctx, "mission_popup_fails", 0) + 1
                        if ctx.mission_popup_fails >= 5:   # give up on this one, never block the queue
                            ctx.pending_mission_popups.pop(0)
                            ctx.mission_popup_fails = 0
                            game_logger.info(f"Native unlock for {popup_zone} failed 5 times - dropped.")

                # --- pending equipment/location item grants - one at a time,
                # off-thread (see _apply_next_pending_item_grant's docstring).
                # Also gated on currently_loading: wait_for_stable_item_stash's
                # address-stability check only confirms the stash was stable
                # for the ~1.5s window BEFORE the write, it can't guarantee
                # the game won't tear it down in the moment right after -
                # confirmed via user report of an item getting silently wiped
                # despite that check passing, because a loading screen began
                # right as the grant landed. Skipping grants entirely while
                # zone_index reads as the loading-transition state (0) avoids
                # ever attempting a write into that specific window at all,
                # rather than trying to further narrow a race that timing
                # alone can't fully close. The grant just waits for the next
                # tick once loading finishes - nothing is lost or skipped. ---
                if ctx.pending_item_grants and not ctx.currently_loading:
                    await _apply_next_pending_item_grant(ctx, pm, base)

                # --- process stats + crash-guard storm detection (every ~15 s, see crash_diag.py) ---
                if time.time() >= getattr(ctx, "diag_next_at", 0.0):
                    ctx.diag_next_at = time.time() + PROCESS_STATS_INTERVAL
                    try:
                        async with ctx.bridge_pipe_lock:
                            _guard_stats = await asyncio.to_thread(dungeons_reader_module.bridge_crash_guard_stats, pm)
                        await asyncio.to_thread(crash_diag.periodic, pm.process_id, (_guard_stats or {}).get("hits"))
                        await health_watch.health_tick(ctx, pm)   # freezes, stuck calls, black screen (not crashes)
                    except Exception as _diag_exc:
                        game_logger.info(f"diagnostics sample failed (continuing): {_diag_exc}")

                # --- level gate: tell the DLL which missions to refuse at the camp (only when the set changes) ---
                if LEVEL_GATE_ENABLED and time.time() >= getattr(ctx, "level_gate_retry_at", 0.0):
                    try:
                        _gate_ids = dungeons_reader_module.level_gate_locked_ids(ctx.unlocked_zones)
                        _gate_key = tuple(_gate_ids)
                        if getattr(ctx, "level_gate_applied", None) != _gate_key:
                            async with ctx.bridge_pipe_lock:
                                _gate_ok, _gate_err = await asyncio.to_thread(
                                    dungeons_reader_module.set_level_gate, pm, 1, _gate_ids)
                            if _gate_ok:
                                ctx.level_gate_applied = _gate_key
                                game_logger.info(f"Level gate: {len(_gate_key)} level id(s) refused at the camp.")
                            else:
                                ctx.level_gate_retry_at = time.time() + 30.0
                                if not getattr(ctx, "level_gate_warned", False):
                                    ctx.level_gate_warned = True
                                    game_logger.info(f"Level gate not applied ({_gate_err}) - rebuild dungeons_bridge.dll from "
                                                     f"the current .cpp. The kill fallback still enforces the locks.")
                    except Exception as _gate_exc:
                        ctx.level_gate_retry_at = time.time() + 30.0
                        game_logger.info(f"Level gate sync error (continuing): {_gate_exc}")

                # --- emerald milestones (throttled) ---
                if ctx.slot_data.get("emerald_goal") and time.time() - ctx.last_emerald_poll >= EMERALD_POLL_INTERVAL:
                    ctx.last_emerald_poll = time.time()
                    current_emeralds, _err = await asyncio.to_thread(read_current_emeralds, pm, base)

                    if current_emeralds is not None:
                        ctx.game_state["emeralds"] = current_emeralds
                        if update_emerald_earned_total(ctx.emerald_earned_state, current_emeralds):
                            save_emerald_earned_state(ctx.emerald_earned_state)
                        total_earned = ctx.emerald_earned_state["total_earned"]
                        reached = _reached_emerald_milestones(ctx, total_earned)
                        new_ones = [m for m in reached if m not in ctx.emerald_milestones_claimed]
                        if new_ones:
                            game_logger.info(f"Emeralds: {current_emeralds} (earned total: {total_earned}) "
                                              f"- new milestone(s): {new_ones}")
                            await ctx.check_locations([_apw.get_emerald_milestone_id(m) for m in new_ones])
                            ctx.emerald_milestones_claimed.update(new_ones)

                if not ctx.sent_goal:
                    await _check_and_maybe_send_goal(ctx)
            except Exception as tick_error:
                # One bad subsystem (a bad memory read, a malformed
                # event, whatever) must never kill the whole watcher
                # task silently - that looked indistinguishable from
                # 'the client disconnected' from the outside, since a
                # dead game_watcher stops sending anything further
                # while CommonContext's own connection keeps running.
                game_logger.info(f"game_watcher tick error (continuing): {tick_error}")


def launch(*args):
    """Entry point for the Archipelago Launcher's registered Component
    (see mcdungeons/__init__.py's launch_client). Follows the same
    async main()/asyncio.run() pattern every CommonClient-based AP
    client uses (see e.g. OoTClient.py, Zelda1Client.py).

    No --slot flag anymore: CommonContext's own UI (or console, if run
    from a real terminal) asks for the slot name interactively via
    get_username()/console_input() - that's the standard AP client flow.
    Note this does NOT parse an "archipelago://" URL arg (some clients
    add a --url flag plus CommonClient.handle_url_arg for that,
    covering webhost "Connect" links specifically) - clicking the
    client from the Launcher's own list, which is the primary way this
    gets launched, doesn't need it; --connect/--password cover the
    plain CLI case."""
    async def main(args):
        ctx = MCDungeonsContext(args.connect, args.password)
        ctx.server_task = asyncio.create_task(server_loop(ctx), name="ServerLoop")
        if gui_enabled:
            ctx.run_gui()
        ctx.run_cli()

        await ctx.exit_event.wait()
        ctx.server_address = None
        await ctx.shutdown()

    parser = get_base_parser()
    parsed_args, _rest = parser.parse_known_args(args=list(args))

    import colorama
    colorama.init()
    try:
        asyncio.run(main(parsed_args))
    except Exception:
        # No console is attached when launched via the Launcher (see this
        # file's git history for the "lost sys.stdin"/None-stderr saga) -
        # kvui's own window handles ordinary runtime errors fine via its
        # log tabs, but a crash severe enough to unwind out of
        # asyncio.run() has no window left to show it in. Log to a file
        # and pop a Tk messagebox so it's never just silently gone.
        import traceback
        tb = traceback.format_exc()
        try:
            log_path = Path(tempfile.gettempdir()) / "dungeons_ap_client_crash.log"
            log_path.write_text(tb, encoding="utf-8")
        except OSError:
            log_path = None
        try:
            import tkinter as tk
            from tkinter import messagebox
            root = tk.Tk()
            root.withdraw()
            suffix = f"\n\n(Full log: {log_path})" if log_path else ""
            messagebox.showerror("Minecraft Dungeons Client - crashed", tb[-1500:] + suffix, parent=root)
            root.destroy()
        except Exception:
            pass  # Tk itself unavailable - the log file (if it wrote) is all we've got
        raise
    finally:
        colorama.deinit()


if __name__ == "__main__":
    launch(*sys.argv[1:])
