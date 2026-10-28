"""Reviewed J835/EID passive classes shared by pickup and build resolution.

The panel, health and flight are read from the game after pickup. These IDs
were reviewed against the pinned EID English descriptions; this registry is
an implementation claim, not an item-value ranking or a stat formula.
"""
from dataclasses import dataclass

# Attack stats can change aim, damage or cadence, so their ID is part of the
# ordinary-tear calibration scope. Extra benign effects may be undervalued.
ATTACK_ATTRIBUTES=frozenset({
    1,4,6,7,12,14,29,30,32,50,51,62,70,72,79,
    80,90,101,109,120,121,138,143,154,165,169,176,183,189,193,
    194,196,197,213,216,228,240,254,255,259,301,304,307,309,310,
    339,341,342,345,355,359,370,381,411,415,438,442,443,455,465,
    493,513,547,564,567,573,600,601,621,644,659,695,708,730,731,
    732,
})
MOVEMENT_ATTRIBUTES=frozenset({20,27,28,71,82,232,340,571,590,632})
HEALTH_AND_UTILITY=frozenset({
    15,16,17,19,22,23,24,25,26,31,46,75,76,91,92,
    108,119,129,141,142,162,173,184,190,195,198,218,219,223,225,
    226,242,253,303,312,313,334,335,343,344,346,354,375,428,456,
    501,538,541,591,614,624,676,690,707,724,
})
# These add autonomous damage/defence. They do not replace Isaac's normal
# movement or tear input. Their contribution is observed, not promised DPS.
SUPPLEMENTAL_PASSIVES=frozenset({
    8,10,57,67,73,88,94,95,96,98,99,100,112,113,117,
    128,131,148,151,155,163,167,170,172,174,178,180,188,207,211,
    217,234,247,248,264,265,266,267,268,269,270,271,274,275,277,
    279,280,318,319,322,350,360,361,362,363,364,387,390,403,404,
    405,412,413,430,435,436,467,469,470,471,473,509,511,525,526,
    528,542,581,607,608,610,615,629,645,649,661,693,698,702,717,
})
assert not any(a & b for a,b in (
    (ATTACK_ATTRIBUTES,MOVEMENT_ATTRIBUTES),
    (ATTACK_ATTRIBUTES,HEALTH_AND_UTILITY),
    (ATTACK_ATTRIBUTES,SUPPLEMENTAL_PASSIVES),
    (MOVEMENT_ATTRIBUTES,HEALTH_AND_UTILITY),
    (MOVEMENT_ATTRIBUTES,SUPPLEMENTAL_PASSIVES),
    (HEALTH_AND_UTILITY,SUPPLEMENTAL_PASSIVES),
))
ITEM_SUPPORT={105:"inactive_active"}
for _items,_class in ((ATTACK_ATTRIBUTES,"attributes"),
                      (MOVEMENT_ATTRIBUTES,"movement_attributes"),
                      (HEALTH_AND_UTILITY,"health_utility"),
                      (SUPPLEMENTAL_PASSIVES,"supplemental_passive")):
    ITEM_SUPPORT.update({item:_class for item in _items})
# Observed one-shot room damage does not change movement/weapon input. This
# does not authorize picking up or activating the corresponding active item.
INSTANT_ROOM_DAMAGE_EFFECTS=frozenset({35})
MECHANISMS={69:"charge",114:"knife"}
PASSIVE_MODIFIERS={154:"left_eye_damage",169:"overkill_pierce",
                   213:"tear_projectile_interception",240:"random_stat_tradeoff",
                   254:"left_eye_damage",538:"trinket_generation",
                   600:"left_eye_fire_rate",731:"right_eye_damage"}


@dataclass(frozen=True)
class ItemCapabilities:
    family:str
    modifiers:tuple[str,...]
    items:tuple[int,...]


def resolve_items(s,*,charge=False,knife=False,preview=False):
    items={int(k):v for k,v in (s.inventory.get("collectibles") or {}).items() if v and (s.schema_version!=1 or int(k)<=732)}
    is_knife=114 in items
    knife_observed=s.weapons is not None and s.weapons.status=="known" and s.weapons.types==[4]
    if s.stats.get("player_type")!=0 or (not is_knife and s.stats.get("weapon_type")!=1) or (is_knife and not preview and not knife_observed):
        raise ValueError("UNSUPPORTED_BUILD: no compatible observed weapon/character")
    if is_knife and 69 in items:raise ValueError("UNSUPPORTED_BUILD: knife/charge special synergy is outside this trial")
    allowed=set(ITEM_SUPPORT)|({69} if charge else set())|({114} if knife else set())
    if set(items)-allowed or any(s.inventory.get(k,0) for k in ("trinket_0","trinket_1")):
        raise ValueError("UNSUPPORTED_BUILD: unmapped item/effect capabilities")
    if any(v.get("item") not in ITEM_SUPPORT for v in (s.inventory.get("active_items") or {}).values()):
        raise ValueError("UNSUPPORTED_BUILD: active inventory capability unknown")
    if s.inventory_coverage and s.inventory_coverage.static_status!="known":raise ValueError("UNSUPPORTED_BUILD: incomplete inventory observation")
    if s.effects and (s.effects.status!="known" or any(e.count and (e.kind!="collectible" or e.item_id not in (set(ITEM_SUPPORT)|INSTANT_ROOM_DAMAGE_EFFECTS)) for e in s.effects.items)):
        raise ValueError("UNSUPPORTED_BUILD: temporary effects unknown")
    return ItemCapabilities("knife" if is_knife else "charged_tears" if 69 in items else "normal_tears",
                            tuple((MECHANISMS.get(i) or PASSIVE_MODIFIERS[i]) for i in sorted(items)
                                  if i==69 or i in PASSIVE_MODIFIERS),tuple(sorted(items)))


def attack_identity(s):
    """Movement-only items/health cannot invalidate a weapon measurement."""
    return {"stats":{k:s.stats.get(k) for k in ("player_type","weapon_type","damage","max_fire_delay","shot_speed","tear_range","tear_height","tear_falling_speed")},
        "attack_items":sorted(int(k) for k,v in s.inventory.get("collectibles",{}).items() if v and int(k) in ATTACK_ATTRIBUTES),
        "mechanisms":sorted(int(k) for k,v in s.inventory.get("collectibles",{}).items() if v and int(k) in MECHANISMS),
        "effects":[(e.kind,e.item_id,e.count) for e in s.effects.items] if s.effects else None}
