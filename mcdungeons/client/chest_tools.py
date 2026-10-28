"""/chests - the number of chests in a zone, in one place.

  /chests [zone|all]: known chest/supply-chest counts (Locations.ZONE_CHEST_COUNTS) + checks already sent; for
  the zone you are in also the level's own counters (MapStatActor, read_current_map_counts) and a scan of the
  chest actors really present (count_chest_actors - sees the hidden chests the counter misses).
The zone-start announcement (MapCountAnnouncer) and the map counter reader live in dungeons_reader.py.
"""
import asyncio
import json
import re

from _apworld_data import Locations as _apw, ZoneData as _apw_zonedata
from dungeons_reader import (
    OFFSETS, resolve_fname, scan_full_zone_shared, classify_interactable_class,
    find_map_stat_actor, read_map_stat_counts, MAP_COUNTS_SEEN_FILE,
)


# ------------------------------------------------------------------ reading the game
def read_current_map_counts(pm, base):
    """Live chest/secret counters of the level the player is in right now (same source as the
    zone-start announcement), or None (hub, loading screen, counter not filled yet)."""
    world = pm.read_longlong(base + OFFSETS["gworld"])
    if not world:
        return None
    actor = find_map_stat_actor(pm, world)
    return read_map_stat_counts(pm, actor) if actor else None


# AChestActor (Dumper-7 SDK): mChestType (EChestType, uint8) @0x330, bOpened @0x331, bDiscovered @0x332.
_CHEST_ACTOR_TYPE_OFFSET = 0x330


def count_chest_actors(pm, base):
    """Chest actors actually present in the level, by class name (BP_WoodenChest_C, BP_FancyChest_C,
    BP_WoodenChest_Hidden_C, BP_SupplyStation_C...), or None when there is no level. Unlike the map's
    counter this also sees the 'Hidden' chest variants. Returns {"chest": n, "hidden": h (included in
    chest), "supply": m, "opened": o, "discovered": d, "types": {EChestType value: count},
    "classes": {name: count}}. opened/discovered/types come from the AChestActor fields."""
    world = pm.read_longlong(base + OFFSETS["gworld"])
    if not world:
        return None
    snapshot = scan_full_zone_shared(pm, world, max_age=2.0)
    if not snapshot:
        return None
    names = {}
    result = {"chest": 0, "hidden": 0, "supply": 0, "opened": 0, "discovered": 0, "types": {}, "classes": {}}
    for class_idx in set(snapshot.values()):
        names[class_idx] = resolve_fname(pm, class_idx) or ""
    for actor, class_idx in snapshot.items():
        name = names[class_idx]
        kind = classify_interactable_class(name) if name else None
        if kind is None:
            continue
        result["classes"][name] = result["classes"].get(name, 0) + 1
        if kind == "supply":
            result["supply"] += 1
            continue
        result["chest"] += 1
        if "hidden" in name.lower():
            result["hidden"] += 1
        try:
            chest_type, opened, discovered = pm.read_bytes(actor + _CHEST_ACTOR_TYPE_OFFSET, 3)
        except Exception:
            continue
        if opened > 1 or discovered > 1:   # not an AChestActor layout: ignore these fields
            continue
        result["types"][chest_type] = result["types"].get(chest_type, 0) + 1
        result["opened"] += opened
        result["discovered"] += discovered
    return result


def seen_map_chest_total(zone_name):
    """Largest chest total ever read off the map for a zone (map_counts_seen.json), or None."""
    try:
        with open(MAP_COUNTS_SEEN_FILE, encoding="utf-8") as f:
            totals = json.load(f).get(zone_name, {}).get("chests_total") or []
    except (OSError, ValueError):
        return None
    return max(totals) if totals else None


# ------------------------------------------------------------------ the command
def _find_zone(text):
    """ZoneInfo from an internal name or display name (case/space/punctuation-insensitive), or a
    unique prefix of one. Returns (zone, candidates)."""
    norm = lambda t: re.sub(r"[^a-z0-9]", "", t.lower())
    wanted = norm(text)
    if not wanted:
        return None, []
    for z in _apw_zonedata.ZONES:
        if wanted in (norm(z.internal_name), norm(z.display_name)):
            return z, []
    cands = [z for z in _apw_zonedata.ZONES
             if norm(z.internal_name).startswith(wanted) or norm(z.display_name).startswith(wanted)]
    return (cands[0], []) if len(cands) == 1 else (None, cands)


def _chest_line(ctx, zone):
    """One line: chests known for the zone (+ checks already sent when the slot is connected)."""
    known = _apw.ZONE_CHEST_COUNTS.get(zone.internal_name)
    label = f"{zone.display_name}"
    if zone.category == "dlc" and not bool(getattr(ctx, "slot_data", {}).get(zone.dlc_pack, True)):
        label += " [DLC off in this seed]"
    if known:
        chests, supply = known
        line = f"{label}: {chests} chest(s) + {supply} supply chest(s)"
        done = getattr(ctx, "checked_locations", None)
        if done is not None:
            sent = sum(1 for n in range(1, chests + 1)
                       if _apw.get_zone_chest_location_id(zone.internal_name, n) in done)
            sent += sum(1 for n in range(1, supply + 1)
                        if _apw.get_zone_supply_chest_location_id(zone.internal_name, n) in done)
            line += f" - checks sent {sent}/{chests + supply}"
        return line
    seen = seen_map_chest_total(zone.internal_name)
    if seen is not None:
        return f"{label}: not confirmed yet - the map showed up to {seen} chest(s) (supply chests not split out)"
    return f"{label}: not measured yet (enter the mission once with the client running)"


async def run_chests(ctx, output, arg):
    """Body of /chests (own task: the live read goes through the pipe lock + a worker thread)."""
    arg = arg.strip()
    if arg.lower() == "all":
        for z in _apw_zonedata.ZONES:
            output(_chest_line(ctx, z))
        return
    live_zone = None
    if arg:
        zone, cands = _find_zone(arg)
        if zone is None:
            if cands:
                output("Several zones match: " + ", ".join(z.display_name for z in cands))
            else:
                output(f"Unknown zone '{arg}'. Use a zone name (e.g. /chests Creeper Woods), "
                       f"nothing for the current zone, or 'all'.")
            return
    else:
        current = ctx.game_state.get("zone")
        if not current or current == "hub":
            output("You are not in a mission right now - give a zone name, or 'all'.")
            return
        zone = _apw_zonedata.ZONES_BY_NAME.get(current)
        if zone is None:
            output(f"Current zone '{current}' is not a known zone yet.")
            return
        live_zone = zone
    output(_chest_line(ctx, zone))
    if live_zone is not None and ctx.pm and ctx.base and not ctx.currently_loading:
        try:
            async with ctx.bridge_pipe_lock:
                counts = await asyncio.to_thread(read_current_map_counts, ctx.pm, ctx.base)
        except Exception as e:
            counts = None
            output(f"Could not read the level's counter: {e}")
        if counts:
            output(f"Right now in this level: chests opened {counts['chests']['current']}/"
                   f"{counts['chests']['total']}, secrets {counts['secrets']['current']}/"
                   f"{counts['secrets']['total']}")
        elif counts is None:
            output("The level's counter is not readable yet (still loading?) - try again in a few seconds.")
        try:
            async with ctx.bridge_pipe_lock:
                actors = await asyncio.to_thread(count_chest_actors, ctx.pm, ctx.base)
        except Exception as e:
            actors = None
            output(f"Could not scan the level's chest actors: {e}")
        if actors:
            hidden = f" (of which {actors['hidden']} hidden)" if actors["hidden"] else ""
            output(f"Chest actors in the level: {actors['chest']}{hidden} + {actors['supply']} supply chest(s)"
                   f" - the map counter above does not count hidden chests.")
            output("  " + ", ".join(f"{n} x{c}" for n, c in sorted(actors["classes"].items())))
            if actors["types"]:
                output("  EChestType: " + ", ".join(f"{t} x{c}" for t, c in sorted(actors["types"].items()))
                       + f" - opened {actors['opened']}, discovered {actors['discovered']}")
