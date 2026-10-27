"""Request one budgeted, trace-grounded Mom's Knife trial review without launching the game."""
import argparse
import asyncio
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "SocketBridge/python"))
from combat.exploration_budget import ExplorationBudget
from mechanisms.item_adaptation import ItemAdaptation


async def run(trial_path: Path, authorization: str | None) -> int:
    path = trial_path.resolve()
    if not path.is_relative_to((ROOT / "artifacts/runs").resolve()):
        raise ValueError("TRIAL_OUTSIDE_RUN_ARTIFACTS")
    trial = json.loads(path.read_text(encoding="utf-8"))
    if not trial.get("passed") or trial.get("confirmed_thrown_hits", 0) < 1:
        raise ValueError("KNIFE_TRIAL_REQUIRES_CONFIRMED_THROWN_HIT")
    adaptation = ItemAdaptation(ROOT, None, allow_generate=True)
    try:
        if not adaptation.package or trial.get("source_sha256") != adaptation.package[1]["source_sha256"]:
            raise ValueError("TRIAL_SOURCE_DIFFERS_FROM_PUBLISHED_COMPONENT")
        budget = ExplorationBudget(ROOT / "artifacts/damage-attribution/budget.sqlite",
                                   phase="item_adaptation", gap="moms_knife")
        if authorization:
            budget.grant_gap(authorization)
        if budget.report()["gap_requests"] >= budget.report()["gap_request_limit"]:
            raise RuntimeError("KNIFE_FEEDBACK_REQUEST_LIMIT_REACHED")
        output = path.with_name("knife-model-feedback.json")
        if output.exists():
            raise FileExistsError("KNIFE_FEEDBACK_ALREADY_RECORDED")
        events = []
        result = await adaptation.review_trial(trial, lambda kind, **fields: events.append({"kind": kind, **fields}))
        output.write_text(json.dumps({"trial": str(path), "result": result, "events": events},
                                     ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"decision": result["decision"], "artifact": result["artifact"],
                          "budget": result["budget"]["gap_requests"]}, ensure_ascii=False))
        return 0 if result["decision"] in ("keep", "defer", "revise") else 1
    finally:
        await adaptation.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trial", type=Path)
    parser.add_argument("--grant-authorization", help="record the user's authorization for one additional request")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(run(args.trial, args.grant_authorization)))
