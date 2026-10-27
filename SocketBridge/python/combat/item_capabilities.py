"""One narrow, observed capability map shared by pickup and build resolution."""
from dataclasses import dataclass

ITEM_SUPPORT={105:"inactive_active",1:"attributes",4:"attributes",12:"attributes",
              20:"flight",27:"movement_attributes",32:"attributes",50:"attributes"}
MECHANISMS={69:"charge",114:"knife"}


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
    if s.effects and (s.effects.status!="known" or any(e.count and (e.kind!="collectible" or e.item_id not in ITEM_SUPPORT) for e in s.effects.items)):
        raise ValueError("UNSUPPORTED_BUILD: temporary effects unknown")
    return ItemCapabilities("knife" if is_knife else "charged_tears" if 69 in items else "normal_tears",
                            tuple(MECHANISMS[i] for i in sorted(items) if i==69),tuple(sorted(items)))


def attack_identity(s):
    """Movement-only items/health cannot invalidate a weapon measurement."""
    return {"stats":{k:s.stats.get(k) for k in ("player_type","weapon_type","damage","max_fire_delay","shot_speed","tear_range","tear_height","tear_falling_speed")},
        "mechanisms":sorted(int(k) for k,v in s.inventory.get("collectibles",{}).items() if v and int(k) in MECHANISMS),
        "effects":[(e.kind,e.item_id,e.count) for e in s.effects.items] if s.effects else None}
