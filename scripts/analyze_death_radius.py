"""Extract death/damage distances from existing traces; never controls the game."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/"SocketBridge/python"))
from combat.models import WorldSnapshot, Vec
from combat.geometry import GeometryService


def analyze(path, kind, variant):
    states, deaths, damage = {}, [], []
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            row = json.loads(line); message = row.get("message", {})
            state = message.get("payload", {}).get("COMBAT_FRAME")
            if state and not state["paused"]: states[state["frame"]] = state
            if message.get("event") == "NPC_DEATH" and message.get("data", {}).get("type") == kind and message["data"].get("variant") == variant:
                deaths.append(message)
            if message.get("event") == "PLAYER_DAMAGE_ATTEMPT": damage.append(message)
    result = []
    for death in deaths:
        frame = death["frame"]; point = Vec(**death["data"]["pos"])
        around = [i for i in states if abs(i-frame) <= 3]
        if not around: continue
        nearest = min(around, key=lambda i: abs(i-frame)); state = states[nearest]
        player = Vec(**state["player"]["pos"])
        prior = [event for event in damage if frame-60 <= event["frame"] < frame]
        window = [{"frame": i, "distance": point.distance(Vec(**states[i]["player"]["pos"])),
                   "hp": states[i]["health"]["red_hearts"]+states[i]["health"]["soul_hearts"]}
                  for i in sorted(around)]
        result.append({"death_frame": frame, "type": kind, "variant": variant, "event": death,
                       "nearest_snapshot_frame": nearest, "distance": point.distance(player),
                       "line_of_sight": GeometryService(WorldSnapshot.model_validate(state)).line_clear(player, point, shot=True),
                       "prior_damage_within_60_updates": prior, "window": window,
                       "damage_attempts_window": [event for event in damage if frame <= event["frame"] <= frame+3],
                       "complete_post_window": max(around) >= frame+3})
    return {"trace": str(path), "trace_sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "deaths": result}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("traces", nargs="+", type=Path)
    parser.add_argument("--type", type=int, default=250)
    parser.add_argument("--variant", type=int, default=0)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = {"kind": "observational_calibration_not_exact_radius", "unit": "room pixels",
              "limitations": ["Snapshot and callback timing differ; preserve the surrounding position window.",
                              "No damage after earlier damage cannot bound radius because of invulnerability.",
                              "Line of sight, ordinary variant and the pinned game build must be checked."],
              "runs": [analyze(path, args.type, args.variant) for path in args.traces]}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(args.output), "runs": len(result["runs"]),
                      "deaths": sum(len(r["deaths"]) for r in result["runs"])}, indent=2))


if __name__ == "__main__": main()
