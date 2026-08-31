from typing import TYPE_CHECKING, Callable, List

from worlds.generic.Rules import CollectionRule

from .ZoneData import ZoneInfo, ZONES_BY_NAME

if TYPE_CHECKING:
    from . import MCDungeonsWorld


def _access_item_name(zone_internal_name: str) -> str:
    zone = ZONES_BY_NAME[zone_internal_name]
    return f"{zone.display_name} Access"


def _secret_item_name(zone_internal_name: str) -> str:
    zone = ZONES_BY_NAME[zone_internal_name]
    return f"{zone.display_name} Secret Access"


def make_zone_rule(world: "MCDungeonsWorld", zone: ZoneInfo, valid_requires: List[str]) -> CollectionRule:
    """You need every zone valid_requires' Access item, and the SecretMissionsAccess option make secret mission count,
    and create Secret Access item too. valid_requires is usually the zone's
    skip-level predecessors (see ZoneData.skip_level_requires_map), not
    its raw direct requires[]"""
    player = world.player
    require_secret_option = bool(world.options.secret_missions_require_secret)

    predecessor_items: List[str] = []
    for req_name in valid_requires:
        predecessor_items.append(_access_item_name(req_name))
        req_zone = ZONES_BY_NAME[req_name]
        if req_zone.secret and require_secret_option:
            predecessor_items.append(_secret_item_name(req_name))

    # It also require the zone's own access item, rather than relying on region reachability alone
    this_zone_item = _access_item_name(zone.internal_name)

    def rule(state) -> bool:
        return (
            state.has(this_zone_item, player)
            and all(state.has(item, player) for item in predecessor_items)
        )

    return rule


def make_ancient_hunt_rule(world: "MCDungeonsWorld") -> CollectionRule:
    player = world.player

    def rule(state) -> bool:
        return state.has("Ancient Hunt Access", player)

    return rule
