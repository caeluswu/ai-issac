"""Small replaceable pickup valuation; scores are preferences, not measured EV."""
from .item_capabilities import ATTACK_ATTRIBUTES, SUPPLEMENTAL_PASSIVES


class PickupChoicePolicy:
    def score(self, snapshot, pickup):
        price = pickup.price or 0
        if pickup.variant == 20: return 120 if price == 0 else -1
        if pickup.variant == 10:
            value = (140 if snapshot.hp <= 2 else 85) if pickup.sub_type in (3,6) else (150 if snapshot.hp <= 2 else 65)
        elif pickup.variant in (30,40):
            count = snapshot.inventory.get("keys" if pickup.variant == 30 else "bombs") or 0
            value = 55 if count < 2 else 8
        elif pickup.variant == 100:
            item = pickup.sub_type
            stage = snapshot.floor.stage if snapshot.floor else snapshot.room.get("stage",1)
            owned = snapshot.inventory.get("collectibles") or {}
            if item in (108,313,535,260): value = 115
            elif item in (182,331,374): value = 110
            elif item in (20,185,184): value = 95 if not snapshot.stats.get("can_fly") else 25
            elif item in (64,203,199,249,414): value = 65 if stage <= 3 else 20
            elif item == 227: value = 38 if stage <= 3 else 15
            elif item in (247,248):
                value = 65 if any(int(i) in SUPPLEMENTAL_PASSIVES for i,n in owned.items() if n) else 5
            elif item in ATTACK_ATTRIBUTES: value = 80
            elif item in SUPPLEMENTAL_PASSIVES: value = 65
            else: value = 40
        else: return -1
        return value-price*2

    def choose(self, snapshot, candidates):
        usable = [pair for pair in candidates if self.score(snapshot,pair[0]) > 0]
        return min(usable,key=lambda pair:(-self.score(snapshot,pair[0]),
            snapshot.player.pos.distance(pair[0].pos),pair[0].instance_id)) if usable else None
