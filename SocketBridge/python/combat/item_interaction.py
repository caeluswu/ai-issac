"""Bounded movement-based pickup transaction through the sole input writer."""
import asyncio
from dataclasses import asdict
import json
from .builds import collectibles, digest
from .catalog import default_plan
from .controller import JointActionSolver
from .item_knowledge import pickup_authority
from .boundary_safety import boundary_danger
from .geometry import GeometryService
from .supervision import ProgressSupervisor


def pickup_approach_allowed(snapshot, pickup):
    """Require a safe standing point at the pickup before authorizing movement."""
    probe = snapshot.model_copy(update={"player": snapshot.player.model_copy(update={"pos": pickup.pos}),
                                        "pickup_exclusions": []})
    if boundary_danger(probe):
        return False
    geometry = GeometryService(probe,allowed_portals=())
    return bool(geometry.walkable and geometry.clear(pickup.pos)
                and geometry.path(snapshot.player.pos, pickup.pos))


class PickupExecutor:
    def __init__(self, learning):
        self.learning = learning

    async def execute(self, session, start, build, pickup, decision):
        store = self.learning.gateway.store
        intent_id = digest([start.run_id,start.floor.instance_id if start.floor else None,start.room_visit_id,pickup.instance_id,pickup.variant,pickup.sub_type,pickup.price])
        previous = await asyncio.to_thread(store.get,"pickup",intent_id)
        if previous: return previous
        result = {"intent_id":intent_id,"encounter_id":decision.encounter_id,"decision_id":decision.decision_id,"result":"aborted","reason":"not_started"}
        solver = JointActionSolver()
        before = dict(collectibles(start))
        resources = {k:start.inventory.get(k) for k in ("coins","keys","bombs")}
        current = start
        empty_since = None
        watcher, previous_action = ProgressSupervisor(), None
        self.learning.active_pickup = (start.run_id,start.room_visit_id,pickup.instance_id,pickup.sub_type,pickup.price)
        try:
            if decision.decision != "take" or decision.build_revision != build.revision: raise ValueError("PICKUP_NOT_AUTHORIZED")
            stored = await asyncio.to_thread(store.get,"decision",decision.decision_id)
            if stored != json.loads(json.dumps(asdict(decision))): raise ValueError("PICKUP_DECISION_NOT_RECORDED")
            if decision.target_signature != pickup_authority(start,build,pickup): raise ValueError("PICKUP_TARGET_NOT_AUTHORIZED")
            if start.frame > decision.deadline_frame: raise ValueError("PICKUP_DECISION_EXPIRED")
            if not pickup_approach_allowed(start,pickup): raise ValueError("PICKUP_TARGET_UNSAFE")
            await session.release()
            await session.set_mode("FORCE_AI")
            stamp = session.observation.latest.snapshot_id
            current = await session.next_snapshot(after=stamp)
            # Approach and an observed item-grant animation have separate
            # budgets. The queue proves pending delivery, never ownership.
            for _ in range(180):
                if (current.run_id,current.room_visit_id) != (start.run_id,start.room_visit_id): raise RuntimeError("PICKUP_ROOM_CHANGED")
                queued = pickup.variant == 100 and current.inventory.get("queued_collectible_id") == pickup.sub_type
                deadline = decision.deadline_frame + (90 if empty_since is not None else 0)
                if current.frame > deadline: raise TimeoutError("PICKUP_DEADLINE")
                if current.blocking_combat or not current.room['is_clear'] or not current.alive: raise RuntimeError("PICKUP_NEW_THREAT_OR_DEATH")
                if boundary_danger(current):
                    raise RuntimeError("PICKUP_NEW_HAZARD")
                instance = next((p for p in current.pickups or [] if p.instance_id == pickup.instance_id),None)
                acquired = False
                if pickup.variant == 100:
                    acquired = collectibles(current).get(str(pickup.sub_type),0) > before.get(str(pickup.sub_type),0)
                elif pickup.variant == 10:
                    metric = "soul_hearts" if pickup.sub_type in (3,6) else "red_hearts"
                    acquired = current.health.get(metric,0) > start.health.get(metric,0)
                else:
                    resource = {20:"coins",30:"keys",40:"bombs"}.get(pickup.variant)
                    acquired = resource is not None and resources[resource] is not None and current.inventory.get(resource,0)>resources[resource]
                emptied = instance is None or instance.is_empty is True
                if acquired and emptied:
                    if pickup.price > 0:
                        after_coins = current.inventory.get("coins")
                        if resources["coins"] is None or after_coins is None:
                            raise RuntimeError("PICKUP_COST_NOT_OBSERVED")
                        net = resources["coins"]-after_coins
                        if net > pickup.price: raise RuntimeError("PICKUP_COST_EXCEEDS_AUTHORIZATION")
                        result.update(quoted_price=pickup.price,coins_before=resources["coins"],coins_after=after_coins,
                            observed_net_spend=net,cost_evidence="matching_net" if net == pickup.price else "simultaneous_income_unresolved")
                    updated = session.build_resolver.update(current)
                    result.update(result="confirmed",reason="pickup_consumed_and_inventory_changed",build_revision=updated.revision,
                                  inventory_after=current.inventory,health_before=start.health,
                                  health_after=current.health,source_mode="normal_pickup")
                    return result
                if current.hp < start.hp or current.health["bone_hearts"] < start.health["bone_hearts"]:
                    raise RuntimeError("PICKUP_UNEXPECTED_HEALTH_LOSS")
                if emptied:
                    if empty_since is None:
                        empty_since = current.frame
                        session.log("pickup_waiting_for_inventory",intent_id=intent_id,frame=current.frame)
                    limit = 90 if queued else 30
                    if current.frame-empty_since >= limit: raise RuntimeError("PICKUP_CONSUMED_WITHOUT_OWNERSHIP_PROOF")
                    await session.release()
                    current = await session.next_snapshot(after=current.snapshot_id)
                    continue
                if (instance.variant,instance.sub_type,instance.price) != (pickup.variant,pickup.sub_type,pickup.price): raise RuntimeError("PICKUP_CHANGED")
                if not pickup_approach_allowed(current,instance): raise RuntimeError("PICKUP_TARGET_UNSAFE")
                if pickup.price and (current.inventory.get("coins") is None or current.inventory["coins"]-pickup.price < decision.reserve_coins):
                    raise RuntimeError("PICKUP_RESOURCE_BUDGET_CHANGED")
                updated = session.build_resolver.update(current)
                if updated.revision != build.revision: raise RuntimeError("PICKUP_BUILD_CHANGED")
                if decision.target_signature != pickup_authority(current,updated,instance): raise RuntimeError("PICKUP_TARGET_NOT_AUTHORIZED")
                failure = watcher.observe(current,previous_action,require_progress=True)
                if failure or any(e.age_ticks >= 30 and e.kind in ("idle","blocked","stall") for e in watcher.events):
                    session.log("pickup_boundary",reason="PICKUP_NO_PROGRESS",frame=current.frame,progress=watcher.feedback())
                    raise RuntimeError("PICKUP_NO_PROGRESS")
                self.learning.mask(current)
                action = await asyncio.to_thread(solver.solve,current,default_plan(session.context_ref(current,build),family=build.family),build,True,instance.pos)
                try: await session.send_action(current,action.move,(0,0),action.model_dump())
                except TimeoutError as exc:
                    if "STALE_ACTION" not in str(exc): raise
                previous_action = action
                current = await session.next_snapshot(after=current.snapshot_id)
            raise TimeoutError("PICKUP_UPDATE_LIMIT")
        except (ValueError,RuntimeError,TimeoutError,ConnectionError) as exc:
            result.update(reason=str(exc),observed_inventory=current.inventory)
            return result
        finally:
            self.learning.active_pickup = None
            try:
                await session.release()
                if session.bridge.connected: await session.set_mode("MANUAL")
            except Exception as exc:
                result.update(result="aborted", reason="PICKUP_CLEANUP_FAILED", cleanup_error=str(exc))
            await asyncio.to_thread(store.record,"pickup",intent_id,result)
            session.log("pickup_transaction",**result)
