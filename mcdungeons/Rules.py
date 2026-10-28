from typing import TYPE_CHECKING, Callable, List

from worlds.generic.Rules import CollectionRule

from .ZoneData import ZoneInfo, ZONES_BY_NAME
from .Items import EYE_OF_ENDER_ITEM, EYE_OF_ENDER_COPIES, zone_access_item_name

if TYPE_CHECKING:
    from . import MCDungeonsWorld


def make_zone_rule(world: "MCDungeonsWorld", zone: ZoneInfo, valid_requires: List[str]) -> CollectionRule:
    """You need ONE item per zone: the zone's own, plus that of every zone in valid_requires. That item is the
    zone's Access item, except for a secret zone with the secret_missions_require_secret option on, whose single
    item is its Secret Access (see Items.zone_access_item_name; the client keeps a secret mission locked in-game
    until that item is received).
    valid_requires is usually the zone's skip-level predecessors (see
    ZoneData.skip_level_requires_map), not its raw direct requires[]."""
    player = world.player
    require_secret_option = bool(world.options.secret_missions_require_secret)

    predecessor_items: List[str] = [
        zone_access_item_name(ZONES_BY_NAME[req_name], require_secret_option) for req_name in valid_requires
    ]

    # It also requires the zone's own item, rather than relying on region reachability alone.
    own_items: List[str] = [zone_access_item_name(zone, require_secret_option)]
    required_items = own_items + predecessor_items
    # With eye_of_ender_checks, The Stronghold also needs all six Eye of Ender items.
    needs_eyes = zone.internal_name == "thestronghold" and bool(world.options.eye_of_ender_checks)

    def rule(state) -> bool:
        if needs_eyes and not state.has(EYE_OF_ENDER_ITEM, player, EYE_OF_ENDER_COPIES):
            return False
        return all(state.has(item, player) for item in required_items)

    return rule


def make_ancient_hunt_rule(world: "MCDungeonsWorld") -> CollectionRule:
    player = world.player

    def rule(state) -> bool:
        return state.has("Ancient Hunt Access", player)

    return rule
