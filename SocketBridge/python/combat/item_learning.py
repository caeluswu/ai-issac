"""Boundary-only knowledge writes and passive, scoped calibration memory."""
import asyncio
import math
from .builds import digest, observed_build
from .item_interaction import PickupExecutor
from .boundary_safety import boundary_danger


class ItemLearning:
    def __init__(self, gateway, *, source_mode="normal_play"):
        self.gateway = gateway
        self.active_pickup = None
        self.authorized_exit = None
        self.decisions = {}
        self.attempted = set()
        self.scope = None
        self.pickups = PickupExecutor(self)
        self.active_experiments = False
        self.source_mode = source_mode
        self.gateway.evidence_modes = self.evidence_modes()

    def mask(self, s):
        allowed = None
        if self.active_pickup and self.active_pickup[:2] == (s.run_id,s.room_visit_id): allowed = self.active_pickup[2:]
        s.pickup_exclusions = [
            {"id":p.instance_id,"x":p.pos.x,"y":p.pos.y,"radius":p.collision_radius}
            for p in s.pickups or [] if p.is_empty is not True and allowed != (p.instance_id,p.sub_type,p.price)]
        s.pickup_exclusions += [{"id":e.instance_id,"x":e.pos.x,"y":e.pos.y,"radius":18}
            for e in s.exit_observations or [] if e.instance_id != self.authorized_exit]

    async def prepare_room(self, s, build):
        scope = (s.run_id,s.room_visit_id)
        if self.scope != scope: self.scope, self.decisions, self.attempted = scope, {}, set()
        self.mask(s)
        for pickup in s.pickups or []:
            decision = await asyncio.to_thread(self.gateway.decide,s,build,pickup)
            self.decisions[(pickup.instance_id,pickup.sub_type,pickup.price)] = decision
        subjects = [{"kind":"collectible","id":p.sub_type} for p in s.pickups or [] if p.variant==100 and p.identity_visibility=="visible" and p.sub_type is not None and p.sub_type>=0]
        bundle = await self.gateway.prepare_bundle(s,build,subjects)
        s.knowledge_bundle = bundle
        return bundle

    async def collect(self, session, snapshot):
        current = snapshot
        if current.blocking_combat or current.hostiles or boundary_danger(current): return current
        # Learning may inspect many candidates, but only a bounded number are
        # approached in one clear-room boundary. No active item actions exist.
        for _ in range(4):
            build = session.build_resolver.update(current)
            build = await self.apply(current,session.build_resolver,build)
            await self.prepare_room(current,build)
            candidates = [(p,self.decisions[(p.instance_id,p.sub_type,p.price)]) for p in current.pickups or []
                if (p.instance_id,p.sub_type,p.price) not in self.attempted
                and self.decisions[(p.instance_id,p.sub_type,p.price)].decision == "take"]
            if not candidates: break
            pickup,decision = min(candidates,key=lambda pair:(pair[0].variant != 10,current.player.pos.distance(pair[0].pos)))
            self.attempted.add((pickup.instance_id,pickup.sub_type,pickup.price))
            result = await self.pickups.execute(session,current,build,pickup,decision)
            current = await session.next_snapshot(after=current.snapshot_id)
            if result["result"] != "confirmed" and (current.blocking_combat or not current.alive or boundary_danger(current)): break
        return current

    async def record_room(self, s, build, result):
        identity = digest([s.run_id,s.floor.instance_id if s.floor else None,s.room_visit_id])
        await asyncio.to_thread(self.gateway.store.record,"room",identity,{"context":{"run_id":s.run_id,"room_visit_id":s.room_visit_id},
            "result":result,"build_signature":build.signature,"calibration_key":build.calibration_key})
        if not build.calibrated or build.calibration_source != "observed" or build.calibration_samples < 3: return
        observed = await asyncio.to_thread(self.gateway.store.passive_measurement,
            build.calibration_key,build.calibration_observations,self.source_mode)
        if observed is None: return
        ref, measured_value = observed
        previous = await asyncio.to_thread(self.gateway.store.retrieve,build.calibration_key)
        modes = self.evidence_modes()
        if any(c.get("source_mode") in modes and c.get("sample_identity_verified") is True for c in previous["counterexamples"]): return
        for claim in previous["local_claims"]:
            if claim.get("sample_identity_verified") is True and claim.get("source_mode") in modes and claim["metric"] == "launch_speed" and abs(claim["value"]-measured_value)>max(1.,abs(claim["value"])*.35):
                await asyncio.to_thread(self.gateway.store.contradict,claim["claim_id"],{"measurement_id":ref,"reason":"prediction_residual","snapshot_id":s.snapshot_id})
                return
        await asyncio.to_thread(self.gateway.store.accept_claim,build.calibration_key,"launch_speed",measured_value,[ref])

    def evidence_modes(self):
        return {"simulated_game"} if self.source_mode == "simulated_game" else {"normal_play","development_initialization"}

    def restore(self, build):
        evidence = self.gateway.store.retrieve(build.calibration_key)
        modes = self.evidence_modes()
        usable = [c for c in evidence["local_claims"] if c["metric"]=="launch_speed" and c.get("sample_identity_verified") is True and c.get("source_mode") in modes
                  and type(c["value"]) in (float,int) and math.isfinite(c["value"]) and .1<=c["value"]<=1000]
        return {"experience_revision":evidence["experience_revision"],"calibration":usable[:1],"counterexamples":evidence["counterexamples"]}

    async def apply(self, s, resolver, build):
        evidence = await asyncio.to_thread(self.restore,build)
        adapter = resolver.adapter_for(s) if hasattr(resolver,"adapter_for") else resolver.adapters.get(s.stats["weapon_type"])
        if hasattr(adapter,"experience_revision"): adapter.experience_revision = evidence["experience_revision"]
        if hasattr(adapter,"clear_restored_calibration"): adapter.clear_restored_calibration()
        if evidence["calibration"] and hasattr(adapter,"restore_calibration"):
            claim = evidence["calibration"][0]
            adapter.restore_calibration(build.calibration_key,claim["value"],evidence["experience_revision"])
        return resolver.update(s)
