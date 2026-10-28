"""Only current doors and completed transitions reveal graph information."""
from collections import deque
from dataclasses import dataclass, field
from .models import RoomKey, DoorObservation


@dataclass
class Node:
    key: RoomKey
    grid_index: int
    room_type: int
    doors: dict = field(default_factory=dict)
    links: dict = field(default_factory=dict)
    healing: bool = False


class TreasureFirstRoutePolicy:
    """Rank reachable frontiers; replace this policy for later floor economy."""

    priorities = {4: 0, 1: 1, 5: 2}

    def rank(self, door, snapshot, depth):
        if door.target_room_type not in self.priorities:
            return None
        if door.is_locked:
            if door.target_room_type != 4 or (snapshot.inventory.get("keys") or 0) < 1:
                return None
        elif not door.is_open:
            return None
        return self.priorities[door.target_room_type], depth

    def can_unlock(self, door, snapshot):
        return door.target_room_type == 4 and (snapshot.inventory.get("keys") or 0) >= 1


class EconomyRoutePolicy(TreasureFirstRoutePolicy):
    """Known shop at >=15 coins, while reserving the treasure-room key."""
    def __init__(self): self.treasure_resolved = False

    def can_unlock(self, door, snapshot):
        keys = snapshot.inventory.get("keys") or 0
        if door.target_room_type == 4: return keys >= 1
        return (door.target_room_type == 2 and (snapshot.inventory.get("coins") or 0) >= 15
                and keys >= (1 if self.treasure_resolved else 2))

    def rank(self, door, snapshot, depth):
        if door.target_room_type != 2: return super().rank(door,snapshot,depth)
        if (snapshot.inventory.get("coins") or 0) < 15: return None
        if door.is_locked and not self.can_unlock(door,snapshot): return None
        if not door.is_locked and not door.is_open: return None
        return -1, depth


class FloorGraph:
    def __init__(self, route_policy=None):
        self.nodes = {}
        self.grid_aliases = {}
        self.failures = {}
        self.floor = None
        self.route_policy = route_policy or EconomyRoutePolicy()

    def observe(self, s):
        key = RoomKey.from_snapshot(s)
        if self.floor is not None and self.floor != (key.floor,key.dimension):
            raise ValueError("GRAPH_FLOOR_CHANGED")
        self.floor = key.floor,key.dimension
        if len(self.nodes) >= 1024 and key not in self.nodes: raise ValueError("GRAPH_NODE_LIMIT")
        node = self.nodes.setdefault(key,Node(key,s.room["room_idx"],s.room["room_type"]))
        self.grid_aliases[s.room["room_idx"]] = key
        node.doors = {int(slot):DoorObservation(slot=int(slot),**{k:v for k,v in d.items() if k in DoorObservation.model_fields and k != "slot"})
                      for slot,d in (s.layout["doors"] or {}).items()}
        node.healing = any(p.variant == 10 and p.sub_type in (1,2,3) and p.price == 0 for p in s.pickups or [])
        return node

    def destination(self, door):
        if door.target_room in self.grid_aliases: return self.grid_aliases[door.target_room]
        if door.target_list_index is not None:
            return RoomKey(*self.floor,door.target_list_index)
        return None

    def confirm(self, source, slot, s):
        target = self.observe(s).key
        self.nodes[source].links[slot] = target
        return target

    def failed(self, source, slot):
        key = source,slot
        self.failures[key] = self.failures.get(key,0)+1

    def available(self, node, snapshot, depth):
        for slot,door in node.doors.items():
            if self.failures.get((node.key,slot),0) >= 2 or door.target_room < 0: continue
            rank = self.route_policy.rank(door,snapshot,depth)
            if rank is not None: yield slot,door,rank

    def choose(self, s):
        """BFS through observed directed links, then nearest available frontier."""
        current = self.observe(s)
        if isinstance(self.route_policy,EconomyRoutePolicy):
            self.route_policy.treasure_resolved = any(n.room_type == 4 for n in self.nodes.values())
        queue, seen, candidates = deque([(current.key,None,0)]),{current.key},[]
        while queue:
            key,first,depth = queue.popleft()
            node = self.nodes[key]
            if depth and s.hp <= 2 and node.healing:
                candidates.append((-1,depth,first))
            for slot,door,rank in self.available(node,s,depth):
                target = node.links.get(slot) or self.destination(door)
                step = slot if first is None else first
                if target not in self.nodes:
                    # Explore ordinary rooms before a known boss when the
                    # treasure room has not yet been found.
                    candidates.append((*rank,step))
                elif target not in seen:
                    # A visible open return door can be attempted; only confirm() records its actual edge.
                    seen.add(target); queue.append((target,step,depth+1))
        if not candidates: return None
        slot = min(candidates)[2]
        return slot,current.doors[slot]
