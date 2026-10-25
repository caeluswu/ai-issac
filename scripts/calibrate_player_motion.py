"""Measure displacement/API velocity by entity class from consecutive snapshots."""
import argparse
import hashlib
import json
from pathlib import Path
import statistics


def measure(path):
    previous = None
    samples = {kind: [] for kind in ("player", "enemy", "enemy_projectile", "player_tear")}
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for line in stream:
            digest.update(line)
            row = json.loads(line)
            s = row.get("message", {}).get("payload", {}).get("COMBAT_FRAME")
            if not s: continue
            if previous and s["room_visit_id"] == previous["room_visit_id"] and s["frame"] == previous["frame"]+1 and not s["paused"]:
                for kind, current, old in (
                    ("player", [s["player"]], [previous["player"]]),
                    ("enemy", s["enemies"], previous["enemies"]),
                    ("enemy_projectile", s["projectiles"]["enemy_projectiles"], previous["projectiles"]["enemy_projectiles"]),
                    ("player_tear", s["projectiles"]["player_tears"], previous["projectiles"]["player_tears"])):
                    lookup = {str(e.get("instance_id", e.get("id", "player"))): e for e in old}
                    for entity in current:
                        prior = lookup.get(str(entity.get("instance_id", entity.get("id", "player"))))
                        if not prior: continue
                        vx, vy = entity["vel"]["x"], entity["vel"]["y"]
                        speed = (vx*vx+vy*vy)**.5
                        change = abs(vx-prior["vel"]["x"])+abs(vy-prior["vel"]["y"])
                        if speed < 1 or change > .05: continue
                        dx, dy = entity["pos"]["x"]-prior["pos"]["x"], entity["pos"]["y"]-prior["pos"]["y"]
                        distance = (dx*dx+dy*dy)**.5
                        if distance > 1: samples[kind].append(distance/speed)
            previous = s
    return {"trace": str(path), "trace_sha256": digest.hexdigest(), "steady_velocity_change_limit": .05,
            "measurements": {kind: {"samples": len(values), "median": statistics.median(values) if values else None}
                             for kind, values in samples.items()}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("traces", type=Path, nargs="+")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = {"scope": "Isaac Normal on pinned original Repentance build; empirical model, not engine specification",
              "player_displacement_scale": 2, "other_entities_scale": 1,
              "native_velocity_limit_at_speed_one": 4.4, "native_velocity_retention_per_update": .75,
              "sources": [measure(path) for path in args.traces]}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__": main()
