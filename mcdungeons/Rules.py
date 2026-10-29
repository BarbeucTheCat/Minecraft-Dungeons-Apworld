from typing import TYPE_CHECKING, Callable, List

from worlds.generic.Rules import CollectionRule

from .ZoneData import ZoneInfo, ZONES_BY_NAME
from .Items import EYE_OF_ENDER_ITEM, EYE_OF_ENDER_COPIES, zone_access_item_name

if TYPE_CHECKING:
    from . import MCDungeonsWorld


def make_zone_rule(world: "MCDungeonsWorld", zone: ZoneInfo, valid_requires: List[str]) -> CollectionRule:
    """Returns the access rule for a zone: its own Access item (Secret Access for secret zones with the option on)
    plus every item in valid_requires (skip-level predecessors, see skip_level_requires_map)."""
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
