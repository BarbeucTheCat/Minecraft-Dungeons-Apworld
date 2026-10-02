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
    category: str
    requires: List[str] = field(default_factory=list)
    secret: bool = False
    min_difficulty: str = "I"
    dlc_pack: Optional[str] = None

ZONES: List[ZoneInfo] = [
    #Mainland
    ZoneInfo("squidcoast", "Squid Coast", "base", []),
    ZoneInfo("creeperwoods", "Creeper Woods", "base", ["squidcoast"]),
    ZoneInfo("creepycrypt", "Creepy Crypt", "base", ["creeperwoods"], True, min_difficulty="II"),
    ZoneInfo("pumpkinpastures", "Pumpkin Pastures", "base", ["creeperwoods"], min_difficulty="I"),
    ZoneInfo("archhaven", "Arch Haven", "base", ["pumpkinpastures"], True, min_difficulty="II"),
    ZoneInfo("soggyswamp", "Soggy Swamp", "base", ["creeperwoods"], min_difficulty="I"),
    ZoneInfo("soggycave", "Soggy Cave", "base", ["soggyswamp"], True, min_difficulty="II"),
    ZoneInfo("redstonemines", "Redstone Mines", "base", ["creeperwoods"], min_difficulty="II"),
    ZoneInfo("fieryforge", "Fiery Forge", "base", ["redstonemines"], min_difficulty="III"),
    ZoneInfo("cacticanyon", "Cacti Canyon", "base", ["creeperwoods"], min_difficulty="II"),
    ZoneInfo("deserttemple", "Desert Temple", "base", ["cacticanyon"], min_difficulty="III"),
    ZoneInfo("lowertemple", "Lower Temple", "base", ["deserttemple"], True, min_difficulty="IV"),
    ZoneInfo("highblockhalls", "Highblock Halls", "base", ["deserttemple", "fieryforge"], min_difficulty="IV"),
    ZoneInfo("underhalls", "Underhalls", "base", ["highblockhalls"], True, min_difficulty="V"),
    ZoneInfo("obsidianpinnacle", "Obsidian Pinnacle", "base", ["highblockhalls"], min_difficulty="IV"),
    ZoneInfo("thestronghold", "The Stronghold", "base", ["obsidianpinnacle"], min_difficulty="III"),
    ZoneInfo("mooshroomisland", "??? (Mooshroom Island)", "base", ["squidcoast", "creeperwoods", "creepycrypt", "pumpkinpastures", "archhaven", "soggyswamp", "soggycave", "redstonemines", "fieryforge", "cacticanyon", "deserttemple", "lowertemple", "highblockhalls", "underhalls", "obsidianpinnacle"], min_difficulty="IV"),

    #Echoing Void
    ZoneInfo("enderwilds", "End Wilds", "dlc", ["thestronghold"], min_difficulty="II", dlc_pack="echoing_void_dlc"),
    ZoneInfo("endcitadel_blightedcitadel", "Broken Citadel", "dlc", ["enderwilds"], min_difficulty="IV", dlc_pack="echoing_void_dlc"),
    
    #Jungle Awakens
    ZoneInfo("dingyjungle", "Dingy Jungle", "dlc", ["squidcoast"], min_difficulty="II", dlc_pack="jungle_awakens_dlc"),
    ZoneInfo("overgrowntemple", "Overgrown Temple", "dlc", ["dingyjungle"], min_difficulty="IV", dlc_pack="jungle_awakens_dlc"),
    ZoneInfo("bamboobluff", "Panda Plateau", "dlc", ["dingyjungle"], True, min_difficulty="V", dlc_pack="jungle_awakens_dlc"),
    ZoneInfo("treetoptangle", "Treetop Tangle", "dlc", ["creeperwoods"], min_difficulty="III", dlc_pack="jungle_awakens_dlc"),

    #Creeping Winter
    ZoneInfo("frozenfjord", "Frosted Fjord", "dlc", ["squidcoast"], min_difficulty="II", dlc_pack="creeping_winter_dlc"),
    ZoneInfo("lonefortress", "Lone Fortress", "dlc", ["frozenfjord"], min_difficulty="IV", dlc_pack="creeping_winter_dlc"),
    ZoneInfo("lostsettlement", "Lost Settlement", "dlc", ["frozenfjord"], True, min_difficulty="V", dlc_pack="creeping_winter_dlc"),

    #Howling Peaks
    ZoneInfo("windsweptpeaks", "Windswept Peaks", "dlc", ["squidcoast"], min_difficulty="II", dlc_pack="howling_peaks_dlc"),
    ZoneInfo("gauntletofgales", "Gauntlet of Gales", "dlc", ["creeperwoods"], min_difficulty="III", dlc_pack="howling_peaks_dlc"),
    ZoneInfo("galesanctum", "Gale Sanctum", "dlc", ["windsweptpeaks"], min_difficulty="IV", dlc_pack="howling_peaks_dlc"),
    ZoneInfo("endlessrampart", "Colossal Rampart", "dlc", ["windsweptpeaks"], True, min_difficulty="V", dlc_pack="howling_peaks_dlc"),

    #Hidden Depths
    ZoneInfo("coralrise", "Coral Rise", "dlc", ["squidcoast"], min_difficulty="II", dlc_pack="hidden_depths_dlc"),
    ZoneInfo("abyssalmonument", "Abyssal Monument", "dlc", ["coralrise"], min_difficulty="IV", dlc_pack="hidden_depths_dlc"),
    ZoneInfo("radiantravine", "Radiant Ravine", "dlc", ["coralrise"], True, min_difficulty="V", dlc_pack="hidden_depths_dlc"),

    #Flames of the Nether
    ZoneInfo("netherwastes", "Nether Wastes", "dlc", ["squidcoast"], min_difficulty="I", dlc_pack="flames_of_the_nether_dlc"),
    ZoneInfo("warpedforest", "Warped Forest", "dlc", ["netherwastes"], min_difficulty="I", dlc_pack="flames_of_the_nether_dlc"),
    ZoneInfo("crimsonforest", "Crimson Forest", "dlc", ["warpedforest"], True, min_difficulty="I", dlc_pack="flames_of_the_nether_dlc"),
    ZoneInfo("soulsandvalley", "Soul Sand Valley", "dlc", ["crimsonforest"], True, min_difficulty="I", dlc_pack="flames_of_the_nether_dlc"),
    ZoneInfo("basaltdeltas", "Basalt Deltas", "dlc", ["netherwastes"], min_difficulty="I", dlc_pack="flames_of_the_nether_dlc"),
    ZoneInfo("netherfortress", "Nether Fortress", "dlc", ["basaltdeltas"], True, min_difficulty="I", dlc_pack="flames_of_the_nether_dlc"),
]
ANCIENT_HUNT_ZONES: List[ZoneInfo] = [
    ZoneInfo("hm_woodlandmansion", "Woodland Mansion", "base", [], min_difficulty="I"),
    ZoneInfo("hm_woodlandprison", "Woodland Prison", "base", [], min_difficulty="I"),
    ZoneInfo("hm_spidercave", "Spider Cave", "base", [], min_difficulty="I"),
]

SECRET_MISSION_NAMES = {z.internal_name for z in ZONES if z.secret}

ZONES_BY_NAME = {z.internal_name: z for z in ZONES}
