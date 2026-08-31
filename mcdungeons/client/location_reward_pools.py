"""
location_reward_pools.py - per-location reward pools. Restricts
give_location_reward's random pool to a curated list of items that
actually make sense for a given zone/mission, instead of picking from
every item of that category in the whole game (e.g. Squid Coast's
"Artifact" reward should only ever be a Fireworks Arrow or Fishing Rod,
not any of the other 44 artifacts in the game).

Keyed by the zone's internal_name (matches ZoneData.py's internal_name /
dungeons_reader.py's ZONE_NAME_LOOKUP), NOT its display name.

This intentionally starts small - fill it in zone by zone as you decide
what's thematically appropriate, rather than guessing at all ~50 zones'
pools. Item names must match item_lookup.py's real names exactly (case-
insensitive - see all_items.csv for the full reference list); a typo
here raises a clear error at grant time rather than failing silently.

Structure: {zone_internal_name: {category: [item names]}}
category matches give_location_reward's category argument: "Melee",
"Ranged", "Armor", "Artifact". A category not listed for a given zone
means "no pool override for this zone+category" - see get_location_pool's
None return and give_location_reward's fallback_to_full_pool argument for
how the caller should handle that.
"""

LOCATION_REWARD_POOLS = {
    "squidcoast": {
        "Artifact": ["Fireworks Arrow", "Fishing Rod"],
    },
    # Everything below is sourced from IGN's per-mission reward lists,
    # cross-checked name by name against item_lookup.py's real item table
    # and confirmed to actually be in the claimed category before being
    # added here. A few IGN display names map to different internal
    # codenames - noted where non-obvious: "Hunter's Armor" -> ArchersStrappings,
    # "Thief Armor" -> AssassinArmor, "Soul Armour" -> SoulRobe, "Great
    # Hammer" -> Hammer, "Scale Armour" (Fiery Forge) -> ScaleMail.
    # IGN's Soggy Swamp list also included "Soul Eater", which doesn't
    # match any real item in item_lookup.py - left out rather than guessed.
    # Fiery Forge's IGN list had one unlabeled "?" gear entry - also
    # omitted, nothing to map it to.
    "creeperwoods": {
        "Melee": ["Sword", "Axe"],
        "Ranged": ["Bow"],
        "Armor": ["ArchersStrappings", "WolfArmor", "SoulRobe"],
        "Artifact": ["BootsOfSwiftness", "Death Cap Mushroom", "TastyBone", "TormentQuiver"],
    },
    "soggyswamp": {
        "Melee": ["Glaive", "Daggers", "SoulKnife"],
        "Ranged": ["Hunting Bow", "ScatterCrossbow"],
        "Armor": ["Evocation Robe", "MysteryArmor"],
        "Artifact": ["Harvester", "Fishing Rod", "TotemOfRegeneration", "LightningRod"],
    },
    "pumpkinpastures": {
        "Melee": ["Sword", "Sickles", "SoulScythe"],
        "Ranged": ["Longbow"],
        "Armor": ["ScaleMail", "ArchersStrappings"],
        "Artifact": ["LightFeather", "Wind Horn", "FlamingQuiver", "CorruptedBeacon"],
    },
    "cacticanyon": {
        "Melee": ["Cutlass", "Gauntlets"],
        "Ranged": ["Trickbow", "Longbow", "Crossbow", "HeavyCrossbow", "Shortbow"],
        "Armor": ["Mercenary Armor", "Speluncker Armor"],
        "Artifact": ["Wind Horn", "WonderfulWheat", "CorruptedBeacon", "TotemOfShielding"],
    },
    "redstonemines": {
        "Melee": ["Daggers", "Pickaxe"],
        "Ranged": ["RapidCrossbow"],
        "Armor": ["WolfArmor", "Speluncker Armor", "AssassinArmor", "PhantomArmor"],
        "Artifact": ["Fireworks Arrow", "Harvester", "CorruptedBeacon", "TastyBone"],
    },
    "deserttemple": {
        "Melee": ["Glaive", "SoulKnife", "Sickles"],
        "Ranged": ["Shortbow"],
        "Armor": ["AssassinArmor", "GrimArmour", "MysteryArmor"],
        "Artifact": ["BootsOfSwiftness", "ShockPowder", "TotemOfShielding", "TormentQuiver"],
    },
    "fieryforge": {
        "Melee": ["Cutlass", "Hammer"],
        "Ranged": ["PowerBow"],
        "Armor": ["ReinforcedMail", "Mercenary Armor", "ScaleMail", "FullPlateArmor"],
        "Artifact": ["IronHideAmulet", "SoulHealer", "TotemOfRegeneration", "FlamingQuiver"],
    },
    "highblockhalls": {
        "Melee": ["Mace", "Axe"],
        "Ranged": ["PowerBow", "ScatterCrossbow", "RapidCrossbow"],
        "Armor": ["SoulRobe"],
        "Artifact": ["Fireworks Arrow", "LightFeather", "Death Cap Mushroom", "LoveMedallion"],
    },
    "obsidianpinnacle": {
        "Melee": ["Claymore", "Hammer"],
        "Ranged": ["Crossbow", "HeavyCrossbow", "SoulCrossbow"],
        "Armor": ["Evocation Robe", "GrimArmour"],
        "Artifact": ["ShockPowder", "WonderfulWheat", "LightningRod"],
    },
}


def get_location_pool(zone_internal_name, category):
    """Returns a list of item names restricted to this zone+category, or
    None if no pool is defined - the caller decides the fallback (e.g.
    give_location_reward's fallback_to_full_pool)."""
    zone_pools = LOCATION_REWARD_POOLS.get(zone_internal_name)
    if not zone_pools:
        return None
    return zone_pools.get(category)
