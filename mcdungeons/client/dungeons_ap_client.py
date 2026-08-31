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
    get_zone_name_index,
    ZONE_NAME_LOOKUP,
    ZONE_ID_ORDER,
    MISSION_LOCATION_IDS,
    MISSION_ACCESS_ITEM_IDS,
    ITEM_ID_TO_ZONE,
    MISSION_REQUIRES,
    is_zone_truly_unlocked,
    get_pawn,
    get_spawned_attributes,
    HEALTH_ATTRIBUTE_SET_CLASS,
    kill_local_player,
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
from _apworld_data import Locations as _apw, Items as _apw_items
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

    src_path = str(Path(__file__).resolve().parent / auto_inject.DLL_NAME)
    dll_bytes = __loader__.get_data(src_path)
    dest_path.write_bytes(dll_bytes)

    try:
        auto_inject.inject_dll(pid, str(dest_path))
        return None
    except Exception as e:
        return str(e)


GAME_POLL_INTERVAL = 0.2       # seconds - matches the old client's tick rate
EMERALD_POLL_INTERVAL = 2.0    # currency reads are cheap but no need every tick
ATTACH_RETRY_INTERVAL = 3.0    # how often to retry finding Dungeons.exe if not attached yet
DEATH_LINK_COOLDOWN = 10.0

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


def _attempt_give_safe(ctx):
    """One attempt at /give_safe's grant. Returns (success, retryable,
    message) - retryable=True for the specific TEMPORARY conditions
    (loading screen, no Pawn, item_stash not yet stable, a power drop
    that might just be transient loading-screen instability) that are
    worth trying again shortly; False for anything else (no items in
    the category, capacity full, etc - a real problem retrying won't
    fix). Shared by the command's first attempt and the background
    retry loop below so both follow identical logic."""
    pm, base = ctx.pm, ctx.base

    if ctx.currently_loading:
        return False, True, ("Refusing to grant: a zone transition/loading screen is in progress "
                              "right now. This is exactly the window where a write can silently "
                              "corrupt the inventory - will retry automatically in a few seconds.")

    _, pawn_error = get_pawn(pm, base)
    if pawn_error:
        return False, True, (f"Refusing to grant: player isn't in a level right now ({pawn_error}). "
                              f"Will retry automatically in a few seconds.")

    try:
        item_stash, item_stash_class = wait_for_stable_item_stash(pm, base)
    except RuntimeError as e:
        return False, True, f"Refusing to grant: {e} Will retry automatically in a few seconds."

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
        success, retryable, message = _attempt_give_safe(ctx)
        output(f"[give_safe retry {attempt}/{GIVE_SAFE_MAX_RETRIES}] {message}")
        if success or not retryable:
            return
    output(f"give_safe: gave up after {GIVE_SAFE_MAX_RETRIES} retries - try the command again by hand.")


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

        success, retryable, message = _attempt_give_safe(ctx)
        self.output(message)
        if not success and retryable:
            asyncio.create_task(
                _retry_give_safe_until_done(ctx, self.output),
                name="GiveSafeRetry",
            )


class MCDungeonsContext(CommonContext):
    game = "Minecraft Dungeons"
    items_handling = 0b111  # everything: other players' sends, our own echoed back, starting inventory
    command_processor = MCDungeonsCommandProcessor

    def __init__(self, server_address, password):
        super().__init__(server_address, password)

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
        self.progressive_pickups_unlocked = False  # set once per attach, see game_watcher
        self.pending_emerald_grants = []  # [(absolute_index, amount), ...] - not yet applied
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

    def _on_package(self, cmd: str, args: dict):
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
                try:
                    inject_error = _ensure_bridge_dll_injected(self.pm.process_id)
                except Exception as e:
                    inject_error = f"{type(e).__name__}: {e}"
                if inject_error:
                    game_logger.info(f"dungeons_bridge.dll injection on connect failed ({inject_error}) - "
                                      f"boss kills, chests, and emeralds may not work until this succeeds.")
                else:
                    game_logger.info("dungeons_bridge.dll confirmed injected (or already was) on connect.")

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
                zone_name = ITEM_ID_TO_ZONE.get(item.item)
                if zone_name and zone_name not in self.unlocked_zones:
                    self.unlocked_zones.add(zone_name)
                    _save_unlocked_zones(self.unlocked_zones)
                    game_logger.info(f"Unlocked: {zone_name}")

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

                if emerald_amount:
                    if absolute_index not in self.applied_reward_indices:
                        self.pending_emerald_grants.append((absolute_index, emerald_amount))
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
        self.pending_emerald_grants = []
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
            success, kill_error, diag = kill_local_player(self.pm, self.base)
            if success:
                game_logger.info(f"{source} died (chain={diag.get('chain')}).")
            else:
                game_logger.info(f"{source} died - could not kill local player: {kill_error}")

    def make_gui(self):
        # Documented extension point (see CommonContext.make_gui's own
        # docstring): return a GameManager subclass, base_title renames
        # the window, and appending to logging_pairs adds a tab that
        # follows a given logger - "Game" here follows game_logger above,
        # giving zone/health/attach status its own tab next to the
        # normal Archipelago connection/item log.
        from kvui import GameManager

        class MCDungeonsManager(GameManager):
            logging_pairs = [
                ("Client", "Archipelago"),
                ("MCDungeonsGame", "Game"),
            ]
            base_title = "Minecraft Dungeons Client"

        return MCDungeonsManager


# ---------------------------------------------------------------------------
# fire_* helpers: send one check, update the matching claimed-state tracker,
# and log it. Same responsibilities as the old client's fire_* closures, just
# using ctx.check_locations (async, CommonContext-native) instead of a raw
# ArchipelagoClient.send_location_checks call.
# ---------------------------------------------------------------------------

async def fire_mission_complete(ctx: MCDungeonsContext, zone_name: str):
    """Sends this zone's Mission Complete check - "already sent" is
    checked against ctx.checked_locations (server-authoritative) rather
    than a local set, same reasoning as fire_chest_open/fire_boss_kill."""
    location_id = MISSION_LOCATION_IDS.get(zone_name)
    if location_id is None or location_id in ctx.checked_locations:
        return
    game_logger.info(f"Mission complete: {zone_name}")
    await ctx.check_locations([location_id])


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
    already = sum(1 for n in range(1, total + 1) if location_id_fn(zone_name, n) in ctx.checked_locations)

    if already < total:
        next_num = already + 1
        location_id = location_id_fn(zone_name, next_num)
        location_name = (_apw.zone_chest_location_name(zone_name, next_num) if kind == "chest"
                          else _apw.zone_supply_chest_location_name(zone_name, next_num))
        await ctx.check_locations([location_id])
        game_logger.info(f"Chest: {location_name}")
        return

    claimed = ctx.zone_chests_claimed  # only ever holds "global:extra_count" now - see docstring
    bonus_chest_count = ctx.slot_data.get("bonus_chest_count", 20) if hasattr(ctx, "slot_data") else 20
    limit = max(0, min(bonus_chest_count, _apw.MAX_BONUS_CHESTS))
    extra_count = claimed.get("global:extra_count", 0) + 1
    claimed["global:extra_count"] = extra_count
    save_claimed_zone_chests(claimed)

    bonus_already = sum(1 for n in range(1, limit + 1) if _apw.get_bonus_chest_location_id(n) in ctx.checked_locations)
    if bonus_already >= limit:
        return
    bonus_num = bonus_already + 1
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
                game_logger.info("Attached to Dungeons.exe.")
            except Exception as e:
                game_logger.info(f"Waiting for Dungeons.exe... ({e})")
                await asyncio.sleep(ATTACH_RETRY_INTERVAL)

        if ctx.exit_event.is_set():
            break

        pm, base = ctx.pm, ctx.base

        # Reset per-attach state so a reattach after a crash re-applies
        # everything a fresh process needs (progressive pickup tier,
        # zone/mission tracking) instead of assuming the new process
        # already matches what the old one had going into its death.
        ctx.progressive_pickups_unlocked = False
        ctx.progressive_pickup_tier_applied = None
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

        # Startup catch-up sweeps - grant anything already true on this hero
        # before this session started (e.g. missions finished, or emeralds
        # already past a milestone, while nothing was watching). Same as the
        # pre-rewrite client's identical sweep, just done once here instead
        # of before entering its while-loop.
        if IS_MISSION_COMPLETED_INDEX is not None:
            game_logger.info("Checking existing mission completion state...")
            for zone_name in MISSION_LOCATION_IDS:
                if zone_name in ctx.known_mission_completed:
                    continue
                if call_is_mission_completed(pm, base, zone_name) is True:
                    ctx.known_mission_completed.add(zone_name)
                    await fire_mission_complete(ctx, zone_name)

        if getattr(ctx, "slot_data", {}).get("emerald_goal"):
            current_emeralds, _err = read_current_emeralds(pm, base)
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
                game_logger.info("Dungeons.exe is no longer running - waiting for it to relaunch...")
                ctx.pm = None
                ctx.base = None
                ctx.game_state["attached"] = False
                break

            try:
                # progressive pickups: force-unlock once per attach if the option
                # is off this seed (see the old client's identical comment on why
                # this can't just be the DLL's own default).
                if not ctx.progressive_pickups_unlocked and not bool(ctx.slot_data.get("progressive_pickups", False)):
                    ok, tier_error = set_pickup_tier(pm, 3)
                    if ok:
                        ctx.progressive_pickups_unlocked = True
                        game_logger.info("Progressive pickups off this seed - all pickups unlocked.")
                elif (bool(ctx.slot_data.get("progressive_pickups", False))
                        and ctx.progressive_pickup_tier_applied != ctx.progressive_pickup_tier):
                    # Option is ON - push the player's actual earned tier
                    # (from received "Progressive Pickup" items) into the game.
                    # Only retries while the applied/target tiers disagree, so
                    # this doesn't hit the bridge pipe every tick once caught up.
                    ok, tier_error = set_pickup_tier(pm, ctx.progressive_pickup_tier)
                    if ok:
                        ctx.progressive_pickup_tier_applied = ctx.progressive_pickup_tier

                death_link_enabled = bool(ctx.slot_data.get("death_link", True))
                watch_boss_kills_enabled = bool(ctx.slot_data.get("boss_kill_checks", False)) or \
                    bool(ctx.slot_data.get("dlc_boss_kill_checks", False))

                # --- boss kills (dungeons_bridge.dll OnCharacterDeath events) ---
                if watch_boss_kills_enabled:
                    for addr in (get_death_events(pm) or []):
                        boss_name = resolve_boss_for_actor(pm, addr)
                        if boss_name:
                            await fire_boss_kill(ctx, boss_name)

                # zone chest events are drained here but attributed to a zone
                # below, once this tick's current zone is actually known.
                chest_events, chest_events_err = get_chest_open_events(pm)
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
                    game_logger.info(f"Entered zone: {zone_name}")
                    if zone_name in ZONE_ID_ORDER:
                        ctx.last_mission_zone = zone_name
                        ctx.last_locked_zone_warned = None
                        # New mission attempt starting. Anything still queued in
                        # the DLL's outcome-trigger buffer belongs to whatever
                        # run just ended (a finish/fail that fired but hasn't
                        # been drained yet) - drain and discard it here rather
                        # than letting the next tick misattribute it to this
                        # fresh attempt. Combined with resetting
                        # pending_mission_outcome_zone, this run starts clean:
                        # nothing is considered completed until a genuine new
                        # trigger + confirm happens for THIS attempt.
                        get_mission_outcome_events(pm)
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
                        elif ctx.debug_interact_logging:
                            game_logger.info(f"{kind.capitalize()} interact ({event['class_name']}) resolved to "
                                              f"zone {event_zone_name!r}, which isn't in ZONE_CHEST_COUNTS - "
                                              f"not counted as a check.")

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
                        missing = [z for z in [zone_name] + MISSION_REQUIRES.get(zone_name, [])
                                   if not is_zone_truly_unlocked(z, ctx.unlocked_zones)]
                        game_logger.info(f"Locked mission '{zone_name}' - missing: {missing}")
                        ctx.last_locked_zone_warned = zone_name
                        ctx.suppress_death_link_until = time.time() + MCDungeonsContext.SUPPRESS_DEATH_LINK_WINDOW
                        kill_local_player(pm, base)

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
                outcome_events, _outcome_err = get_mission_outcome_events(pm)
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
                        result = call_is_mission_completed(pm, base, zone_to_confirm, debug_log=game_logger.info)
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
                if ctx.pending_emerald_grants:
                    still_pending = []
                    for absolute_index, amount in ctx.pending_emerald_grants:
                        success, err = apply_emerald_reward(pm, base, amount)
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

                # --- emerald milestones (throttled) ---
                if ctx.slot_data.get("emerald_goal") and time.time() - ctx.last_emerald_poll >= EMERALD_POLL_INTERVAL:
                    ctx.last_emerald_poll = time.time()
                    current_emeralds, _err = read_current_emeralds(pm, base)

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
