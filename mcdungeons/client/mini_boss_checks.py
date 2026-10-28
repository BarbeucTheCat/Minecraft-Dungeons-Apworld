"""Mini boss kill checks (options mini_boss_kill_checks / dlc_mini_boss_kill_checks) - client side, in one place.

The options are Choices (slot_data values): 0 none, 1 per_type, 2 per_zone, 3 both - one value for the base-game
zones, one for the DLC zones.
  per_zone (2, 3): "<Zone> - Mini Boss Kill", sent when a powerful mob is beaten in the zone, and the client FORCES
      a mini boss to appear in every zone that has one (zone_miniboss_on_chest in dungeons_ap_client.py asks
      mini_boss_zone_forced() before summoning, the summon itself is miniboss.py).
  per_type (1, 3): "Mini Boss - <Type>", sent the first time a mini boss of that kind is beaten anywhere.
The signal is PowerfulMobTracker.run["killed_types"] (reader): the entity-type names of the powerful mobs beaten in
this run, the same one the zone mini boss rule uses. The apworld side is Locations.py / Options.py / Regions.py /
__init__.py.
"""
import logging
import time

import miniboss
from _apworld_data import Locations as _apw, ZoneData as _apw_zonedata

game_logger = logging.getLogger("MCDungeonsGame")

# entity-type name the tracker reports (killed_types) -> per-type key, e.g. "HoveringInferno" -> "wildfire"
_TYPE_BY_ENTITY = {miniboss.MINIBOSS_TYPES[key][1]: key for key in _apw.MINIBOSS_TYPE_ORDER}


def _mode(ctx, zone_name):
    """The seed's mini boss mode (0..3) for this zone's group: base-game or DLC."""
    key = ("mini_boss_kill_checks" if _apw_zonedata.ZONES_BY_NAME[zone_name].category == "base"
           else "dlc_mini_boss_kill_checks")
    return int(ctx.slot_data.get(key, 0) or 0)


def mini_boss_checks_active(ctx):
    """True when the seed has any mini boss check (watching powerful mob deaths is then needed)."""
    return bool(int(ctx.slot_data.get("mini_boss_kill_checks", 0) or 0)
                or int(ctx.slot_data.get("dlc_mini_boss_kill_checks", 0) or 0))


def mini_boss_zone_forced(ctx, zone_name):
    """True when a mini boss must be forced to appear in this zone: it has one and its group is per_zone / both."""
    return zone_name in _apw.MINIBOSS_ZONES and _apw.mini_boss_mode_wants_zone(_mode(ctx, zone_name))


async def _send(ctx, location_id, label):
    """Sends one check once (not if already checked, not if this seed has no such location, not twice within 10 s
    while the server has not acknowledged it yet)."""
    if location_id in ctx.checked_locations:
        return
    server_locations = getattr(ctx, "server_locations", None)
    if server_locations and location_id not in server_locations:
        return                                  # this seed does not have that location
    pending = ctx.__dict__.setdefault("mini_boss_sent_at", {})
    if time.time() - pending.get(location_id, 0.0) < 10.0:
        return
    pending[location_id] = time.time()
    game_logger.info(f"Mini boss kill: {label}")
    await ctx.check_locations([location_id])


async def mini_boss_kill_tick(ctx):
    """Sends the per-zone and per-type checks for the powerful mobs beaten so far this run."""
    tracker = getattr(ctx, "powerful_tracker", None)
    run = getattr(tracker, "run", None) if tracker is not None else None
    if not run or not run.get("killed_types"):
        return
    zone_name = run.get("zone")
    killed = sorted(run["killed_types"])

    # per zone: any powerful mob beaten in a zone that has a mini boss check
    if zone_name and mini_boss_zone_forced(ctx, zone_name):
        await _send(ctx, _apw.get_mini_boss_kill_location_id(zone_name), f"{zone_name} ({', '.join(killed)})")

    # per type: each kind of mini boss beaten, in ANY zone (a kind that exists in the base game and in a DLC counts
    # wherever you beat it; _send drops the kinds this seed has no location for)
    if (_apw.mini_boss_mode_wants_type(int(ctx.slot_data.get("mini_boss_kill_checks", 0) or 0))
            or _apw.mini_boss_mode_wants_type(int(ctx.slot_data.get("dlc_mini_boss_kill_checks", 0) or 0))):
        for entity_name in killed:
            type_key = _TYPE_BY_ENTITY.get(entity_name)
            if type_key is not None:
                await _send(ctx, _apw.get_mini_boss_type_location_id(type_key),
                            f"type {_apw.MINIBOSS_TYPE_DISPLAY[type_key]}")
