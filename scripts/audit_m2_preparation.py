"""Offline readiness diagnostics; synthetic scenarios, no game or model calls.

Reports observed behavior rather than weakening production test assertions.
The JSON is review evidence, not a claim that these defects occurred in-game.
"""
import asyncio
import argparse
import subprocess
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "SocketBridge/python"))

from combat.agent_store import AgentStore
from combat.builds import BuildResolver
from combat.item_knowledge import KnowledgeGateway
from combat.item_learning import ItemLearning
from combat.room_execution import RoomExecutor
from tests.combat_fixtures import snapshot
from tests.test_item_knowledge import package_fixture, pickup


async def audit():
    findings = []
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        gateway = KnowledgeGateway(package_fixture(root), AgentStore(root / "agent.sqlite"))
        learning = ItemLearning(gateway, source_mode="simulated_game")
        s = snapshot(enemies=False)
        resolver = BuildResolver()
        build = resolver.update(s)

        decisions = {str(subtype): gateway.decide(s, build, pickup(subtype, variant=40)).decision
                     for subtype in (1, 3, 4, 5, 6, 7)}
        findings.append({"id": "R1", "scenario": "bomb_subtype_policy",
                         "observed": decisions,
                         "issue_present": any(decisions[str(i)] == "take" for i in (3, 4, 5, 6, 7))})

        # Keep exactly three original samples; advance only the room visit.
        for frame in range(11, 14):
            s.frame = frame
            s.projectiles["player_tears"] = [{"id": frame, "owner_type": 1,
                "pos": s.player.pos.model_dump(), "vel": {"x": 10, "y": 0}}]
            build = resolver.update(s)
        await learning.record_room(s, build, {"result": "CLEARED"})
        first = gateway.store.retrieve(build.calibration_key)
        s.frame += 1
        s.room_visit_id = "test:2"
        s.projectiles["player_tears"] = []
        build = resolver.update(s)
        await learning.record_room(s, build, {"result": "CLEARED"})
        second = gateway.store.retrieve(build.calibration_key)
        findings.append({"id": "R2", "scenario": "publish_without_new_samples",
            "observed": {"sample_count": build.calibration_samples,
                         "claims_before": len(first["local_claims"]), "claims_after": len(second["local_claims"]),
                         "revision_before": first["experience_revision"], "revision_after": second["experience_revision"]},
            "issue_present": len(second["local_claims"]) > len(first["local_claims"])})

        # A successful combat result must not hide death during post-clear work.
        start = snapshot(enemies=False)
        latest = start.model_copy(deep=True)
        session = SimpleNamespace(observation=SimpleNamespace(latest=latest),
            bridge=SimpleNamespace(connected=True), release=AsyncMock(), set_mode=AsyncMock(),
            death_observed=False, death_event_frame=None, visited=set(), navigation_devices={},
            build_resolver=BuildResolver(), log=lambda *args, **kwargs: None)

        async def collect_and_die(*args):
            latest.player.dead = True
            latest.health["red_hearts"] = 0
            session.death_observed = True
            return latest

        session.learning = SimpleNamespace(collect=collect_and_die, record_room=AsyncMock())
        executor = RoomExecutor(session)
        executor._engage = AsyncMock(return_value="CLEARED")
        result = await executor.execute(start)
        findings.append({"id": "R3", "scenario": "death_during_post_clear_collection",
            "observed": {"room_result": result, "recorded_result": executor.results[-1]["result"],
                         "dead_after_collection": session.death_observed},
            "issue_present": result == "CLEARED" and session.death_observed})

        # A valid decision for item A must not authorize movement toward item B.
        s = snapshot(enemies=False)
        build = BuildResolver().update(s)
        authorized = pickup(1, id=3, instance_id="test:pickup:authorized")
        other = pickup(149, id=2, instance_id="test:pickup:other")
        s.pickups = [authorized, other]
        decision = gateway.decide(s, build, authorized)
        action_log = []

        async def action(*args):
            action_log.append({"move": args[1], "target": other.sub_type})
            raise RuntimeError("AUDIT_STOP_BEFORE_ANY_GAME_IO")

        session = SimpleNamespace(observation=SimpleNamespace(latest=s), bridge=SimpleNamespace(connected=True),
            release=AsyncMock(), set_mode=AsyncMock(), next_snapshot=AsyncMock(return_value=s),
            build_resolver=BuildResolver(), send_action=action, log=lambda *a, **kw: None)
        from combat.models import ContextRef
        session.context_ref = lambda state, profile: ContextRef(run_id=state.run_id,
            room_visit_id=state.room_visit_id, snapshot_id=state.snapshot_id, build_revision=profile.revision)
        response = await learning.pickups.execute(session, s, build, other, decision)
        findings.append({"id": "R4", "scenario": "decision_target_binding",
            "observed": {"authorized_item": 1, "requested_item": 149, "input_attempts": len(action_log),
                         "result_reason": response["reason"]},
            "issue_present": bool(action_log)})

    return {"schema_version": 1, "source_mode": "synthetic_audit", "paid_api_calls": 0,
            "game_launched": False, "findings": findings}


if __name__ == "__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output",type=Path,default=ROOT/"artifacts/validation/m2-readiness-current.json")
    args=parser.parse_args()
    result = asyncio.run(audit())
    result["implementation_commit"]=subprocess.check_output(["git","rev-parse","HEAD"],cwd=ROOT,text=True).strip()
    output = args.output
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
