"""Boundary-only knowledge writes and passive, scoped calibration memory."""
import asyncio
import math
from dataclasses import asdict, replace
from .economy import PickupChoicePolicy
from .builds import digest, observed_build
from .item_interaction import PickupExecutor, pickup_approach_allowed
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
        self.choice_policy = PickupChoicePolicy()
        self.angel_claimed = set()
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
            decision = await asyncio.to_thread(self.gateway.decide,s,build,pickup,allow_choice=True)
            self.decisions[(pickup.instance_id,pickup.sub_type,pickup.price)] = decision
        groups = {}
        angel_key = (s.run_id,s.floor.instance_id if s.floor else None,s.room.get("room_idx"))
        for pickup in s.pickups or []:
            group = pickup.options_pickup_index
            if s.room.get("room_type") == 15 and pickup.variant == 100:
                group = "angel"
            if group:
                groups.setdefault(group,[]).append((pickup,self.decisions[(pickup.instance_id,pickup.sub_type,pickup.price)]))
        for group,pairs in groups.items():
            winner = self.choice_policy.choose(s,[pair for pair in pairs if pair[1].decision == "take" and pickup_approach_allowed(s,pair[0])])
            for pickup,decision in pairs:
                if decision.decision != "take": continue
                if winner is None or pickup.instance_id != winner[0].instance_id or (group == "angel" and angel_key in self.angel_claimed):
                    rejected = replace(decision,decision="skip",reason_codes=("CHOICE_OTHER_SELECTED",),
                        decision_id=digest([decision.decision_id,"choice",group,winner[0].instance_id if winner else None]))
                    self.decisions[(pickup.instance_id,pickup.sub_type,pickup.price)] = rejected
                    await asyncio.to_thread(self.gateway.store.record,"decision",rejected.decision_id,asdict(rejected))
        subjects = [{"kind":"collectible","id":p.sub_type} for p in s.pickups or [] if p.variant==100 and p.identity_visibility=="visible" and p.sub_type is not None and p.sub_type>=0]
        bundle = await self.gateway.prepare_bundle(s,build,subjects)
        s.knowledge_bundle = bundle
        return bundle

    async def collect(self, session, snapshot):
        current = snapshot
        if current.blocking_combat or not current.room['is_clear'] or boundary_danger(current): return current
        # Fresh shop stock has a short engine pickup cooldown. Leaving before
        # it expires silently loses the entire shop visit.
        wait_began = current.frame
        while any(p.wait is not None and p.wait > 0 and p.is_empty is not True for p in current.pickups or []):
            if current.frame-wait_began >= 30: break
            await session.release()
            current = await session.next_snapshot(after=current.snapshot_id)
            if current.blocking_combat or not current.room['is_clear'] or boundary_danger(current) or not current.alive:
                return current
        if hasattr(session,"prepare_item_capabilities"):current=await session.prepare_item_capabilities(current)
        # Learning may inspect many candidates, but only a bounded number are
        # approached in one clear-room boundary. No active item actions exist.
        for _ in range(4):
            build = session.build_resolver.update(current)
            build = await self.apply(current,session.build_resolver,build)
            await self.prepare_room(current,build)
            candidates = [(p,self.decisions[(p.instance_id,p.sub_type,p.price)]) for p in current.pickups or []
                if (p.instance_id,p.sub_type,p.price) not in self.attempted
                and self.decisions[(p.instance_id,p.sub_type,p.price)].decision == "take"
                and pickup_approach_allowed(current,p)]
            if not candidates: break
            selected = self.choice_policy.choose(current,candidates)
            if selected is None: break
            pickup,decision = selected
            session.log("pickup_compared",selected=pickup.instance_id,candidates=[{
                "id":p.instance_id,"item":p.sub_type,"price":p.price,
                "score":self.choice_policy.score(current,p)} for p,_ in candidates])
            self.attempted.add((pickup.instance_id,pickup.sub_type,pickup.price))
            result = await self.pickups.execute(session,current,build,pickup,decision)
            current = await session.next_snapshot(after=current.snapshot_id)
            if result["result"]=="confirmed" and current.room.get("room_type") == 15 and pickup.variant == 100:
                self.angel_claimed.add((current.run_id,current.floor.instance_id if current.floor else None,current.room.get("room_idx")))
            if result["result"]=="confirmed" and hasattr(session,"prepare_item_capabilities"):
                current=await session.prepare_item_capabilities(current)
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
