"""
apply_item_reward.py - applies a received Archipelago filler item that
represents an in-game equipment reward, by dispatching to give_item.py's
give_random_item(). Lives alongside give_item.py and dungeons_reader.py
(not inside the AP world package) - same reasoning as ap_client.py: this
acts against a live game process, not at generation time.

ITEM_REWARD_CATEGORIES mirrors the AP world's Items.py of the same name.
Keep the two in sync by hand if you edit one - this file intentionally
doesn't import the world package, since the client-side script and the
world generation code may not always be run from the same checkout/
environment. If your setup DOES have the world package importable
alongside this script, prefer `from worlds.mcdungeons.Items import
ITEM_REWARD_CATEGORIES` instead of this local copy, to avoid drift:

    "Random Melee Weapon"  -> Melee weapon, random enchants
    "Random Ranged Weapon" -> bow/crossbow, random enchants
    "Random Armor"         -> armor set, random enchants
    "Random Artifact"      -> artifact/trinket, no enchants (none exist for these)
    "Random Item"          -> any of the three EQUIPMENT types above, picked at random (not Artifact)

Usage (called from dungeons_reader.py's ReceivedItems handling loop -
see the module docstring in ap_client.py for the index-tracking pattern
this expects: only call apply_item_reward for a given absolute_index
once, and only persist that index as "applied" AFTER this returns
successfully - if it raises, the caller should retry the same index on
its next poll rather than skipping it, so a reward is never silently
dropped just because it arrived at an inconvenient moment, e.g. mid
capacity-full or mid zone-transition (both already handled inside
give_item.py itself - see its PowerDropDetected / check_inventory_room /
_wait_for_zone_transition)):

    from apply_item_reward import (is_item_reward, apply_item_reward,
        is_location_reward, apply_location_reward, count_enchant_slot_tier)

    # names_received_so_far accumulates every resolved item name across
    # polls (including PROGRESSIVE_ENCHANT_SLOT_ITEM copies) - the caller
    # already needs something like this to resolve item_id -> name in the
    # first place, so no new state beyond what's already being tracked.
    for item_id, absolute_index in ap_client.poll_received_items():
        if absolute_index in applied_indices:
            continue
        item_name = item_id_to_name(item_id)  # however your reward loop
                                                # already resolves this
        names_received_so_far.append(item_name)
        num_slots = count_enchant_slot_tier(names_received_so_far)

        if is_item_reward(item_name):
            apply_item_reward(pm, pipe, item_stash, item_stash_class, item_name, num_slots=num_slots)
            applied_indices.add(absolute_index)
            save_applied_indices(applied_indices)  # REWARDS_FILE, per ap_client.py's docstring
        elif is_location_reward(item_name):
            apply_location_reward(pm, pipe, item_stash, item_stash_class, item_name, num_slots=num_slots)
            applied_indices.add(absolute_index)
            save_applied_indices(applied_indices)
        elif item_name in FILLER_ITEM_EMERALD_AMOUNTS:
            ...  # existing emerald-add handling
        # else: a progression/access item - no in-game action needed
"""

from give_item import give_random_item, give_location_reward

ITEM_REWARD_CATEGORIES = {
    "Random Melee Weapon": "Melee",
    "Random Ranged Weapon": "Ranged",
    "Random Armor": "Armor",
    "Random Artifact": "Artifact",
    "Random Item": None,
}

# Mirrors the AP world's Items.py LOCATION_REWARD_ITEM_ZONE_CATEGORY -
# keep in sync by hand (same reasoning as ITEM_REWARD_CATEGORIES above:
# this script may run without the world package importable). The pool
# CONTENTS for each (zone, category) pair live in
# location_reward_pools.py, not here - this dict only says which item
# NAMES map to which (zone, category) lookup key.
#
# Important: applying one of these does NOT check the player's current
# zone or unlock progress at all - the pool is entirely determined by
# which item this is, decided once at generation time. A "Squid Coast
# Artifact Reward" item grants from Squid Coast's pool no matter where
# the player currently is or whether they've even reached Squid Coast.
LOCATION_REWARD_ITEM_ZONE_CATEGORY = {
    "Squid Coast Artifact Reward": ("squidcoast", "Artifact"),
    "Creeper Woods Melee Reward": ("creeperwoods", "Melee"),
    "Creeper Woods Ranged Reward": ("creeperwoods", "Ranged"),
    "Creeper Woods Armor Reward": ("creeperwoods", "Armor"),
    "Creeper Woods Artifact Reward": ("creeperwoods", "Artifact"),
    "Soggy Swamp Melee Reward": ("soggyswamp", "Melee"),
    "Soggy Swamp Ranged Reward": ("soggyswamp", "Ranged"),
    "Soggy Swamp Armor Reward": ("soggyswamp", "Armor"),
    "Soggy Swamp Artifact Reward": ("soggyswamp", "Artifact"),
    "Pumpkin Pastures Melee Reward": ("pumpkinpastures", "Melee"),
    "Pumpkin Pastures Ranged Reward": ("pumpkinpastures", "Ranged"),
    "Pumpkin Pastures Armor Reward": ("pumpkinpastures", "Armor"),
    "Pumpkin Pastures Artifact Reward": ("pumpkinpastures", "Artifact"),
    "Cacti Canyon Melee Reward": ("cacticanyon", "Melee"),
    "Cacti Canyon Ranged Reward": ("cacticanyon", "Ranged"),
    "Cacti Canyon Armor Reward": ("cacticanyon", "Armor"),
    "Cacti Canyon Artifact Reward": ("cacticanyon", "Artifact"),
    "Redstone Mines Melee Reward": ("redstonemines", "Melee"),
    "Redstone Mines Ranged Reward": ("redstonemines", "Ranged"),
    "Redstone Mines Armor Reward": ("redstonemines", "Armor"),
    "Redstone Mines Artifact Reward": ("redstonemines", "Artifact"),
    "Desert Temple Melee Reward": ("deserttemple", "Melee"),
    "Desert Temple Ranged Reward": ("deserttemple", "Ranged"),
    "Desert Temple Armor Reward": ("deserttemple", "Armor"),
    "Desert Temple Artifact Reward": ("deserttemple", "Artifact"),
    "Fiery Forge Melee Reward": ("fieryforge", "Melee"),
    "Fiery Forge Ranged Reward": ("fieryforge", "Ranged"),
    "Fiery Forge Armor Reward": ("fieryforge", "Armor"),
    "Fiery Forge Artifact Reward": ("fieryforge", "Artifact"),
    "Highblock Halls Melee Reward": ("highblockhalls", "Melee"),
    "Highblock Halls Ranged Reward": ("highblockhalls", "Ranged"),
    "Highblock Halls Armor Reward": ("highblockhalls", "Armor"),
    "Highblock Halls Artifact Reward": ("highblockhalls", "Artifact"),
    "Obsidian Pinnacle Melee Reward": ("obsidianpinnacle", "Melee"),
    "Obsidian Pinnacle Ranged Reward": ("obsidianpinnacle", "Ranged"),
    "Obsidian Pinnacle Armor Reward": ("obsidianpinnacle", "Armor"),
    "Obsidian Pinnacle Artifact Reward": ("obsidianpinnacle", "Artifact"),
}


PROGRESSIVE_ENCHANT_SLOT_ITEM = "Progressive Enchant Slot"
MAX_ENCHANT_SLOT_TIER = 3

# Must match Items.py's PROGRESSIVE_PICKUP_ITEM name and its 3-copy count
# (tier 0 start -> 1 -> 2 -> 3 final) exactly - same plain-string-literal
# cross-package convention as PROGRESSIVE_ENCHANT_SLOT_ITEM above, since
# this file lives outside the mcdungeons world package proper.
PROGRESSIVE_PICKUP_ITEM = "Progressive Pickup"
PICKUP_MAX_TIER = 3


def count_enchant_slot_tier(received_item_names):
    """received_item_names: an iterable of item names already received
    this game (e.g. built by the caller from ap_client.poll_received_items'
    resolved names, or a running list it maintains across polls). Returns
    how many "Progressive Enchant Slot" copies have been received so far,
    capped at 3 - pass this straight through as num_slots to
    apply_item_reward/apply_location_reward for every reward applied from
    here on. Doesn't do any polling or state persistence itself - the
    caller owns tracking which items it's already seen, same as the rest
    of this module's index-based dedup pattern."""
    count = sum(1 for name in received_item_names if name == PROGRESSIVE_ENCHANT_SLOT_ITEM)
    return min(count, MAX_ENCHANT_SLOT_TIER)


def is_item_reward(item_name):
    """True if item_name is one of ITEM_REWARD_CATEGORIES' keys - use
    this to distinguish an equipment reward from an emerald filler or a
    progression/access item in the caller's ReceivedItems loop."""
    return item_name in ITEM_REWARD_CATEGORIES


def is_location_reward(item_name):
    """True if item_name is one of LOCATION_REWARD_ITEM_ZONE_CATEGORY's
    keys - a reward whose pool is tied to a specific zone+category,
    decided at generation time rather than picked fully at random."""
    return item_name in LOCATION_REWARD_ITEM_ZONE_CATEGORY


def apply_item_reward(pm, pipe, item_stash, item_stash_class, item_name, num_slots=3):
    """Grants the actual in-game item for a received reward. item_name
    must be one of ITEM_REWARD_CATEGORIES' keys - call is_item_reward()
    first if the caller also handles other item types.

    num_slots: how many enchant slots the granted item actually gets
    (0-3, default 3). The caller is responsible for tracking how many
    "Progressive Enchant Slot" items have been received so far (see
    PROGRESSIVE_ENCHANT_SLOT_ITEM in the AP world's Items.py) and passing
    that count here - this function does no tracking of its own, since it
    has no visibility into the player's overall received-items history,
    only the one item currently being applied.

    Returns (item_name_index, granted_item_name, displayed_power) from
    give_random_item(). Can raise the same way give_random_item can:
    RuntimeError if inventory+storage is full (check_inventory_room) or
    if item_stash never stabilizes, PowerDropDetected if a power drop is
    observed (see give_item.py) - the caller should NOT mark the reward
    as applied when this raises; retry on a later poll instead, so
    nothing gets silently dropped just because it arrived at a bad
    moment. include_uniques/include_dlc use give_random_item's defaults
    (uniques allowed, DLC excluded) - pass through kwargs if a specific
    reward type should behave differently."""
    if item_name not in ITEM_REWARD_CATEGORIES:
        raise ValueError(f"{item_name!r} isn't a known item reward - check is_item_reward() first")

    category = ITEM_REWARD_CATEGORIES[item_name]
    return give_random_item(pm, pipe, item_stash, item_stash_class, category=category, num_slots=num_slots)


def apply_location_reward(pm, pipe, item_stash, item_stash_class, item_name, fallback_to_full_pool=True,
                            num_slots=3):
    """Grants the actual in-game item for a received location-specific
    reward. item_name must be one of LOCATION_REWARD_ITEM_ZONE_CATEGORY's
    keys - call is_location_reward() first if the caller also handles
    other item types.

    Does NOT check the player's current zone or unlock progress - the
    pool is entirely determined by which item this is (see
    LOCATION_REWARD_ITEM_ZONE_CATEGORY above).

    fallback_to_full_pool: True (default) falls back to
    give_random_item's normal full-category pool if location_reward_
    pools.py doesn't (yet) have an entry for this zone+category - useful
    since LOCATION_REWARD_ITEM_ZONE_CATEGORY and location_reward_pools.py
    are meant to be filled in together but could drift out of sync. Set
    False to raise instead if you'd rather catch that mismatch loudly.
    num_slots: how many enchant slots the granted item actually gets
    (0-3, default 3) - see apply_item_reward's docstring; same tracking
    responsibility applies here.

    Returns (item_name_index, granted_item_name, displayed_power) from
    give_location_reward(). Same failure-handling expectation as
    apply_item_reward: don't mark the reward applied if this raises."""
    if item_name not in LOCATION_REWARD_ITEM_ZONE_CATEGORY:
        raise ValueError(f"{item_name!r} isn't a known location reward - check is_location_reward() first")

    zone_internal_name, category = LOCATION_REWARD_ITEM_ZONE_CATEGORY[item_name]
    return give_location_reward(
        pm, pipe, item_stash, item_stash_class, zone_internal_name, category,
        fallback_to_full_pool=fallback_to_full_pool, num_slots=num_slots,
    )
