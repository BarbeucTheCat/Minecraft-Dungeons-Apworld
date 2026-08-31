"""
The mission logic for Minecraft Dungeons.

Each ZoneInfo entry:
    internal_name:  the internal name of the mission, matching
                       the game's own memory and zone_name_lookup.json
    display_name:   the name you actually see in game
    category:       "base" or "dlc"
    dlc_pack:       which Options controls this zone
    requires:       which mission(s) you need first. More than one
                       means you need ALL of them. Empty means no
                       predecessor.
    secret:         Make secret mission differant than a normal mission
                       allow to make there access item in the option
    min_difficulty: "I" through "V" Not used, doesn't
                       affect logic here
"""

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass(frozen=True)
class ZoneInfo:
    internal_name: str
    display_name: str
    category: str  # "base" | "dlc"
    requires: List[str] = field(default_factory=list)
    secret: bool = False
    min_difficulty: str = "I"
    dlc_pack: Optional[str] = None  # matches the Options.py name


ZONES: List[ZoneInfo] = [
    #Mainland (base game)
    ZoneInfo("squidcoast", "Squid Coast", "base", [], False, "I"),
    ZoneInfo("creeperwoods", "Creeper Woods", "base", ["squidcoast"], False, "I"),
    ZoneInfo("creepycrypt", "Creepy Crypt", "base", ["creeperwoods"], True, "II"),
    ZoneInfo("pumpkinpastures", "Pumpkin Pastures", "base", ["creeperwoods"], False, "I"),
    ZoneInfo("archhaven", "Arch Haven", "base", ["pumpkinpastures"], True, "II"),
    ZoneInfo("soggyswamp", "Soggy Swamp", "base", ["creeperwoods"], False, "I"),
    ZoneInfo("soggycave", "Soggy Cave", "base", ["soggyswamp"], True, "II"),
    ZoneInfo("redstonemines", "Redstone Mines", "base", ["creeperwoods"], False, "II"),
    ZoneInfo("fieryforge", "Fiery Forge", "base", ["redstonemines"], False, "III"),
    ZoneInfo("cacticanyon", "Cacti Canyon", "base", ["creeperwoods"], False, "II"),
    ZoneInfo("deserttemple", "Desert Temple", "base", ["cacticanyon"], False, "III"),
    ZoneInfo("lowertemple", "Lower Temple", "base", ["deserttemple"], True, "IV"),
    ZoneInfo("highblockhalls", "Highblock Halls", "base", ["deserttemple", "fieryforge"], False, "IV"),
    ZoneInfo("underhalls", "Underhalls", "base", ["highblockhalls"], True, "V"),
    ZoneInfo("obsidianpinnacle", "Obsidian Pinnacle", "base", ["highblockhalls"], False, "IV"),
    ZoneInfo(
        "mooshroomisland", "??? (Mooshroom Island)", "base",
        [
            "squidcoast", "creeperwoods", "creepycrypt", "pumpkinpastures", "archhaven",
            "soggyswamp", "soggycave", "redstonemines", "fieryforge", "cacticanyon",
            "deserttemple", "lowertemple", "highblockhalls", "underhalls", "obsidianpinnacle",
        ],
        False, "IV",
    ),

    #Echoing Void (Stonghold is a Mainland Mission)
    ZoneInfo("thestronghold", "The Stronghold", "base", ["obsidianpinnacle"], False, "III"),
    ZoneInfo("enderwilds", "End Wilds", "dlc", ["thestronghold"], False, "II", dlc_pack="echoing_void_dlc"),
    ZoneInfo("endcitadel_blightedcitadel", "Broken Citadel", "dlc", ["enderwilds"], False, "IV", dlc_pack="echoing_void_dlc"),

    #Jungle Awakens
    ZoneInfo("dingyjungle", "Dingy Jungle", "dlc", ["squidcoast"], False, "II", dlc_pack="jungle_awakens_dlc"),
    ZoneInfo("overgrowntemple", "Overgrown Temple", "dlc", ["dingyjungle"], False, "IV", dlc_pack="jungle_awakens_dlc"),
    ZoneInfo("bamboobluff", "Panda Plateau", "dlc", ["dingyjungle"], True, "V", dlc_pack="jungle_awakens_dlc"),
    ZoneInfo("treetoptangle", "Treetop Tangle", "dlc", ["creeperwoods"], False, "III", dlc_pack="jungle_awakens_dlc"),

    #Creeping Winter
    ZoneInfo("frozenfjord", "Frosted Fjord", "dlc", ["squidcoast"], False, "II", dlc_pack="creeping_winter_dlc"),
    ZoneInfo("lonefortress", "Lone Fortress", "dlc", ["frozenfjord"], False, "IV", dlc_pack="creeping_winter_dlc"),
    ZoneInfo("lostsettlement", "Lost Settlement", "dlc", ["frozenfjord"], True, "V", dlc_pack="creeping_winter_dlc"),

    #Howling Peaks
    ZoneInfo("windsweptpeaks", "Windswept Peaks", "dlc", ["squidcoast"], False, "II", dlc_pack="howling_peaks_dlc"),
    ZoneInfo("gauntletofgales", "Gauntlet of Gales", "dlc", ["creeperwoods"], False, "III", dlc_pack="howling_peaks_dlc"),
    ZoneInfo("galesanctum", "Gale Sanctum", "dlc", ["windsweptpeaks"], False, "IV", dlc_pack="howling_peaks_dlc"),
    ZoneInfo("endlessrampart", "Colossal Rampart", "dlc", ["windsweptpeaks"], True, "V", dlc_pack="howling_peaks_dlc"),

    #Hidden Depths
    ZoneInfo("coralrise", "Coral Rise", "dlc", ["squidcoast"], False, "II", dlc_pack="hidden_depths_dlc"),
    ZoneInfo("abyssalmonument", "Abyssal Monument", "dlc", ["coralrise"], False, "IV", dlc_pack="hidden_depths_dlc"),
    ZoneInfo("radiantravine", "Radiant Ravine", "dlc", ["coralrise"], True, "V", dlc_pack="hidden_depths_dlc"),

    #Flames of the Nether
    ZoneInfo("netherwastes", "Nether Wastes", "dlc", ["squidcoast"], False, "I", dlc_pack="flames_of_the_nether_dlc"),
    ZoneInfo("warpedforest", "Warped Forest", "dlc", ["netherwastes"], False, "I", dlc_pack="flames_of_the_nether_dlc"),
    ZoneInfo("crimsonforest", "Crimson Forest", "dlc", ["warpedforest"], True, "I", dlc_pack="flames_of_the_nether_dlc"),
    ZoneInfo("soulsandvalley", "Soul Sand Valley", "dlc", ["crimsonforest"], True, "I", dlc_pack="flames_of_the_nether_dlc"),
    ZoneInfo("basaltdeltas", "Basalt Deltas", "dlc", ["netherwastes"], False, "I", dlc_pack="flames_of_the_nether_dlc"),
    ZoneInfo("netherfortress", "Nether Fortress", "dlc", ["basaltdeltas"], True, "I", dlc_pack="flames_of_the_nether_dlc"),
]

# Ancient Hunts: no predecessor, gated by an item instead (see Rules.py).
ANCIENT_HUNT_ZONES: List[ZoneInfo] = [
    ZoneInfo("hm_woodlandmansion", "Woodland Mansion", "base", [], False, "I"),
    ZoneInfo("hm_woodlandprison", "Woodland Prison", "base", [], False, "I"),
    ZoneInfo("hm_spidercave", "Spider Cave", "base", [], False, "I"),
]

SECRET_MISSION_NAMES = {z.internal_name for z in ZONES if z.secret}

ZONES_BY_NAME = {z.internal_name: z for z in ZONES}


def skip_level_requires_map(enabled_names=None):
    """The logic for mission unlock (look at the parents mission and it's grands parrent)"""
    names = enabled_names if enabled_names is not None else set(ZONES_BY_NAME)
    result = {}
    for name, zone in ZONES_BY_NAME.items():
        if name not in names:
            continue
        direct = [r for r in zone.requires if r in names]
        if not direct or len(zone.requires) > 2:
            result[name] = direct
            continue
        effective = set(direct)  # keep the direct predecessor(s)
        for parent_name in direct:
            parent = ZONES_BY_NAME.get(parent_name)
            parent_direct = [r for r in parent.requires if r in names] if parent else []
            effective.update(parent_direct)  # and add their predecessor(s) too
        result[name] = sorted(effective)
    return result


def full_predecessor_chain(zone_name, enabled_names=None):
    """Maps each zone to its closest ancestors internal_names (parent +
    grandparent tier and add it together)
    """
    names = enabled_names if enabled_names is not None else set(ZONES_BY_NAME)
    seen: set = set()
    order: List[str] = []

    def visit(name: str) -> None:
        zone = ZONES_BY_NAME.get(name)
        if not zone:
            return
        for req in zone.requires:
            if req not in names or req in seen:
                continue
            seen.add(req)
            order.append(req)
            visit(req)

    visit(zone_name)
    return order
