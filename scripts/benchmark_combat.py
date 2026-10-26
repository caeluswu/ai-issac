"""Deterministic density benchmark; synthetic geometry is not gameplay evidence."""
import argparse
import json
from pathlib import Path
import platform
import statistics
import sys
import hashlib
import subprocess
import importlib.metadata

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/"SocketBridge/python"))
from tests.combat_fixtures import snapshot, context
from combat.controller import JointActionSolver, BuildResolver
from combat.catalog import default_plan
from combat.models import WorldSnapshot, BattlePlan


def measure(s, repeats, step=None, plan=None):
    build, solver = BuildResolver().update(s), JointActionSolver()
    plan = plan or default_plan(context(s))
    timings, exhausted = [], 0
    for _ in range(repeats):
        s.frame += 1
        action = step(s, plan, build) if step else solver.solve(s, plan, build)
        timings.append(action.decision_ms)
        exhausted += action.diagnostics["budget_exhausted"]
    ordered = sorted(timings)
    return {"enemies": len(s.enemies), "projectiles": len(s.projectiles["enemy_projectiles"]),
            "median_ms": statistics.median(timings), "p95_ms": ordered[min(len(ordered)-1, int(.95*len(ordered)))],
            "p99_ms": ordered[min(len(ordered)-1, int(.99*len(ordered)))], "budget_exhausted": exhausted}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=100)
    args = parser.parse_args()
    result = {"platform": platform.platform(), "python": platform.python_version(), "repeats": args.repeats, "cases": []}
    result["implementation_commit"] = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    result["source_hashes"] = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                                for p in (ROOT/"SocketBridge/python/combat").glob("*.py")}
    result["dependencies"] = {name: importlib.metadata.version(name) for name in ("numpy", "pydantic", "httpx")}
    for enemy_count, n in ((1, 0), (1, 50), (1, 200), (1, 500), (32, 200), (64, 500)):
        s = snapshot()
        base = s.enemies[0]
        if enemy_count > 1:
            from combat.models import Vec
            s.enemies = [base.model_copy(deep=True) for _ in range(enemy_count)]
            for index, enemy in enumerate(s.enemies):
                enemy.instance_id = "benchmark:"+str(index)
                enemy.pos = Vec(x=80+(index*53)%480, y=80+(index*37)%220)
                enemy.vel = Vec(x=(index%5-2)*.7, y=(index%3-1)*.7)
            s.room["enemy_count"] = enemy_count
        s.projectiles["enemy_projectiles"] = [{"pos": {"x": (i*37)%520+40, "y": (i*53)%240+40},
            "vel": {"x": (i%5-2)*2, "y": (i%7-3)*2}, "collision_radius": 5} for i in range(n)]
        result["cases"].append({"case": "synthetic_density", **measure(s, args.repeats)})
    for raw in json.loads((ROOT/"SocketBridge/python/tests/fixtures/large_wall.json").read_text(encoding="utf8")):
        result["cases"].append({"case": "captured_large_wall", "source_frame": raw["frame"],
                                **measure(WorldSnapshot.model_validate(raw), args.repeats)})
    args.output.parent.mkdir(parents=True, exist_ok=True)
    from combat.navigation import DoorNavigator
    from combat.models import Vec
    s = WorldSnapshot.model_validate(json.loads((ROOT/"SocketBridge/python/tests/fixtures/holdout_failure_44.json").read_text(encoding="utf8")))
    s.player.dead = False; s.health["red_hearts"] = 6
    s.player.pos = Vec(x=320, y=400); s.player.vel = Vec(x=0, y=0)
    navigator = DoorNavigator(1, s.layout["doors"]["1"])
    result["cases"].append({"case": "captured_fire_navigation", **measure(s, args.repeats, navigator.step)})
    raw = json.loads((ROOT/"SocketBridge/python/tests/fixtures/latency_joint.json").read_text(encoding="utf8"))
    result["cases"].append({"case": "captured_llm_joint_tail", **measure(WorldSnapshot.model_validate(raw["snapshot"]),
                            args.repeats, plan=BattlePlan.model_validate(raw["plan"]))})
    for raw in json.loads((ROOT/"SocketBridge/python/tests/fixtures/latency_room_fields.json").read_text(encoding="utf8")):
        for cold in (False,True):
            step=(lambda s,p,b: JointActionSolver().solve(s,p,b)) if cold else None
            result["cases"].append({"case":"captured_terrain_route_cold" if cold else "captured_terrain_route_warm",
                "source_sample":raw["sample"],"source_frame":raw["snapshot"]["frame"],
                **measure(WorldSnapshot.model_validate(raw["snapshot"]),args.repeats,step)})
    args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))
    return int(any(c["p95_ms"] > 20 or c["p99_ms"] > 30 for c in result["cases"]))


if __name__ == "__main__": raise SystemExit(main())
