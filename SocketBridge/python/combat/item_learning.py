"""Boundary-only knowledge writes and passive, scoped calibration memory."""
import asyncio
import math
from .builds import digest, observed_build
from .item_interaction import PickupExecutor


class ItemLearning:
    def __init__(self, gateway, *, source_mode="normal_play"):
        self.gateway = gateway
        self.active_pickup = None
        self.decisions = {}
        self.scope = None
        self.pickups = PickupExecutor(self)
        self.active_experiments = False
        self.source_mode = source_mode

    def mask(self, s):
        allowed = None
        if self.active_pickup and self.active_pickup[:2] == (s.run_id,s.room_visit_id): allowed = self.active_pickup[2:]
        s.pickup_exclusions = [
            {"id":p.instance_id,"x":p.pos.x,"y":p.pos.y,"radius":p.collision_radius}
            for p in s.pickups or [] if p.is_empty is not True and allowed != (p.instance_id,p.sub_type,p.price)]

    async def prepare_room(self, s, build):
        scope = (s.run_id,s.room_visit_id)
        if self.scope != scope: self.scope, self.decisions = scope, {}
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
        if current.blocking_combat or current.hostiles or current.bombs or current.hazards: return current
        # Learning may inspect many candidates, but only a bounded number are
        # approached in one clear-room boundary. No active item actions exist.
        for _ in range(4):
            build = session.build_resolver.update(current)
            await self.prepare_room(current,build)
            choice = next(((p,self.decisions[(p.instance_id,p.sub_type,p.price)]) for p in current.pickups or []
                          if self.decisions[(p.instance_id,p.sub_type,p.price)].decision=="take"),None)
            if choice is None: break
            pickup, decision = choice
            result = await self.pickups.execute(session,current,build,pickup,decision)
            current = await session.next_snapshot(after=current.snapshot_id)
            if result["result"] != "confirmed": break
        return current

    async def record_room(self, s, build, result):
        identity = digest([s.run_id,s.floor.instance_id if s.floor else None,s.room_visit_id])
        await asyncio.to_thread(self.gateway.store.record,"room",identity,{"context":{"run_id":s.run_id,"room_visit_id":s.room_visit_id},
            "result":result,"build_signature":build.signature,"calibration_key":build.calibration_key})
        if not build.calibrated or build.calibration_source != "observed" or build.calibration_samples < 3: return
        measurement = {"value":build.tear_speed,"unit":"game_pixel/game_update","sample_count":build.calibration_samples,
                       "snapshot_id":s.snapshot_id,"source_mode":self.source_mode,"extractor_version":1}
        ref = await asyncio.to_thread(self.gateway.store.measurement,build.calibration_key,"launch_speed",measurement)
        previous = await asyncio.to_thread(self.gateway.store.retrieve,build.calibration_key)
        modes = self.evidence_modes()
        if any(c.get("source_mode") in modes for c in previous["counterexamples"]): return
        for claim in previous["local_claims"]:
            if claim.get("source_mode") in modes and claim["metric"] == "launch_speed" and abs(claim["value"]-build.tear_speed)>max(1.,abs(claim["value"])*.35):
                await asyncio.to_thread(self.gateway.store.contradict,claim["claim_id"],{"measurement_id":ref,"reason":"prediction_residual","snapshot_id":s.snapshot_id})
                return
        await asyncio.to_thread(self.gateway.store.accept_claim,build.calibration_key,"launch_speed",build.tear_speed,[ref])

    def evidence_modes(self):
        return {"simulated_game"} if self.source_mode == "simulated_game" else {"normal_play","development_initialization"}

    def restore(self, build):
        evidence = self.gateway.store.retrieve(build.calibration_key)
        modes = self.evidence_modes()
        usable = [c for c in evidence["local_claims"] if c["metric"]=="launch_speed" and c.get("source_mode") in modes
                  and type(c["value"]) in (float,int) and math.isfinite(c["value"]) and .1<=c["value"]<=1000]
        return {"experience_revision":evidence["experience_revision"],"calibration":usable[:1],"counterexamples":evidence["counterexamples"]}

    async def apply(self, s, resolver, build):
        evidence = await asyncio.to_thread(self.restore,build)
        adapter = resolver.adapters.get(s.stats["weapon_type"])
        if hasattr(adapter,"experience_revision"): adapter.experience_revision = evidence["experience_revision"]
        if hasattr(adapter,"clear_restored_calibration"): adapter.clear_restored_calibration()
        if evidence["calibration"] and hasattr(adapter,"restore_calibration"):
            claim = evidence["calibration"][0]
            adapter.restore_calibration(build.calibration_key,claim["value"],evidence["experience_revision"])
        return resolver.update(s)
