"""Game-facing knowledge gateway; visibility, support and costs remain local."""
import asyncio
from dataclasses import dataclass, asdict
import json
from copy import deepcopy
from functools import lru_cache
from pathlib import Path
from isaac_knowledge import KnowledgePackage, LookupRequest
from .agent_store import AgentStore
from .builds import ITEM_SUPPORT, digest


@dataclass(frozen=True)
class PickupDecision:
    encounter_id: str
    decision: str
    reason_codes: tuple[str, ...]
    build_revision: int
    evidence_refs: tuple[str, ...] = ()
    expected_benefits: tuple[str, ...] = ()
    cost_coins: int = 0
    deadline_frame: int = 0
    decision_id: str = ""
    reserve_coins: int = 0
    target_signature: str = ""


def pickup_authority(s, build, pickup):
    return digest([s.run_id, s.floor.instance_id if s.floor else None, s.room_visit_id,
                   pickup.instance_id, pickup.variant, pickup.sub_type, pickup.price,
                   build.revision, build.signature])


class KnowledgeGateway:
    def __init__(self, package, store):
        self.package, self.store = package, store

    @classmethod
    def from_config(cls, root):
        config = json.loads((Path(root)/"configs/knowledge/eid-package.json").read_text(encoding="utf-8"))
        package = KnowledgePackage(Path(root)/config["path"])
        if package.dataset_id != config["dataset_id"]: raise ValueError("KNOWLEDGE_DATASET_MISMATCH")
        return cls(package, AgentStore(Path(root)/"artifacts/agent-state/agent.sqlite"))

    def lookup(self, kind, item_id, purpose="display"):
        return deepcopy(self._lookup(kind,item_id,purpose))

    @lru_cache(maxsize=512)
    def _lookup(self, kind, item_id, purpose):
        request = LookupRequest(request_id=f"local:{kind}:{item_id}",dataset_id=self.package.dataset_id,
            subjects=[{"kind":kind,"id":item_id}],purpose=purpose)
        return self.package.lookup(request)

    def decide(self, s, build, pickup, *, reserve_coins=0):
        encounter = digest([s.run_id, s.room_visit_id, pickup.instance_id, pickup.sub_type])
        decision_id = digest([encounter,s.snapshot_id,build.revision,pickup.price,pickup.wait,s.inventory.get("coins"),s.health,reserve_coins])
        context = {"run_id":s.run_id,"floor_instance_id":s.floor.instance_id if s.floor else None,
                   "room_visit_id":s.room_visit_id,"frame":s.frame,"build_revision":build.revision,"build_signature":build.signature}
        self.store.record("encounter",encounter,{"context":context,"pickup":pickup.model_dump(),"inventory_before":s.inventory,"health_before":s.health})
        records, reasons, decision, cost = [], [], "skip", 0
        benefits = []
        if pickup.is_empty is True: reasons.append("EMPTY_PEDESTAL")
        elif pickup.identity_visibility != "visible" or pickup.sub_type is None:
            reasons.append("IDENTITY_NOT_AVAILABLE")
        elif pickup.wait is None or pickup.price is None: reasons.append("INTERACTION_FACTS_UNKNOWN")
        elif pickup.options_pickup_index not in (None,0): reasons.append("CHOICE_GROUP_NOT_SUPPORTED")
        elif pickup.price > 0 and getattr(self,"purchase_budget",None) is not None and pickup.price > self.purchase_budget:
            reasons.append("PURCHASE_BUDGET_EXCEEDED")
        elif pickup.wait > 0:
            decision="defer"; reasons.append("PICKUP_NOT_READY")
        elif pickup.variant == 100:
            answer = self.lookup("collectible",pickup.sub_type)
            if answer["status"] == "error": reasons.append("KNOWLEDGE_UNAVAILABLE")
            else: records = answer["results"][0]["records"]
            if not reasons:
                if not records or not records[0]["parser_complete"]: reasons.append("DESCRIPTION_PARTIAL_OR_MISSING")
                elif pickup.sub_type not in ITEM_SUPPORT: reasons.append("CAPABILITY_NOT_IMPLEMENTED")
                elif ITEM_SUPPORT[pickup.sub_type] == "inactive_active": reasons.append("ACTIVE_STRATEGY_NOT_ENABLED")
                elif pickup.price < 0: reasons.append("HEALTH_PAYMENT_NOT_AUTHORIZED")
                elif s.inventory.get("coins") is None or s.inventory["coins"]-pickup.price < reserve_coins:
                    if pickup.price == 0: decision="take"; reasons.append("SUPPORTED_FREE_ITEM")
                    else: reasons.append("INSUFFICIENT_COINS_OR_RESERVE")
                else: decision="take"; cost=pickup.price; reasons.append("SUPPORTED_ITEM_WITH_DESCRIPTION")
                benefits = [r["summary"]["text"] for r in records if r["summary"]["text"]]
        elif pickup.variant == 20 and pickup.sub_type in (1, 2, 3, 5) and pickup.price == 0 and (s.inventory.get("coins") or 0) < 99:
            decision="take"; reasons.append("FREE_COIN")
        elif pickup.variant == 30 and pickup.sub_type in (1, 3) and pickup.price == 0 and (s.inventory.get("keys") or 0) < 99:
            decision="take"; reasons.append("FREE_KEY")
        elif pickup.variant == 40 and pickup.sub_type in (1, 2) and pickup.price == 0 and (s.inventory.get("bombs") or 0) < 99:
            decision="take"; reasons.append("FREE_BOMB_PICKUP")
        elif pickup.variant == 10 and pickup.sub_type in (1,2,3) and pickup.price == 0:
            eligible = s.floor_progress and (s.floor_progress.can_pick_soul if pickup.sub_type == 3 else s.floor_progress.can_pick_red)
            if eligible is True: decision="take"; reasons.append("SUPPORTED_FREE_HEALTH")
            else: reasons.append("HEALTH_CAPACITY_FULL_OR_UNKNOWN")
        else: reasons.append("PICKUP_STRATEGY_NOT_IMPLEMENTED")
        value = PickupDecision(encounter,decision,tuple(reasons),build.revision,
            tuple(r["record_id"] for r in records),tuple(benefits),cost,s.frame+90,decision_id,reserve_coins,pickup_authority(s,build,pickup))
        self.store.record("decision",decision_id,asdict(value))
        if decision != "take" and ("CAPABILITY_NOT_IMPLEMENTED" in reasons or "DESCRIPTION_PARTIAL_OR_MISSING" in reasons):
            self.store.record("task",digest([self.package.dataset_id,pickup.variant,pickup.sub_type,reasons]),{"dataset_id":self.package.dataset_id,"subject":{"variant":pickup.variant,"id":pickup.sub_type},"reasons":reasons,"state":"pending"})
        return value

    async def prepare_bundle(self, s, build, subjects, purpose="planning"):
        records, unknowns = [], []
        for subject in subjects[:32]:
            response = await asyncio.to_thread(self.lookup,subject["kind"],subject["id"],purpose)
            if response["status"] == "error": unknowns.extend(response["errors"])
            else:
                for result in response["results"]:
                    records.extend(result["records"])
                    if result["missing_facets"]: unknowns.append({"subject":result["subject"],"facets":result["missing_facets"]})
        experience = await asyncio.to_thread(self.store.retrieve, build.calibration_key)
        if experience["experience_revision"] != build.experience_revision: raise ValueError("EXPERIENCE_SNAPSHOT_CHANGED")
        modes = getattr(self, "evidence_modes", {"normal_play", "development_initialization"})
        for key in ("local_claims", "counterexamples"):
            experience[key] = [c for c in experience[key] if c.get("source_mode") in modes]
        return {"bundle_schema_version":2,"bundle_id":digest([self.package.dataset_id,experience["experience_revision"],s.snapshot_id,subjects,purpose]),
            "dataset_id":self.package.dataset_id,"knowledge_revision":1,
            "context_ref":{"run_id":s.run_id,"floor_instance_id":s.floor.instance_id if s.floor else None,"room_visit_id":s.room_visit_id,
                           "snapshot_id":s.snapshot_id,"build_revision":build.revision,"catalog_revision":2,"experience_revision":build.experience_revision},
            "purpose":purpose,"resolved_subjects":subjects[:32],"records":records,"interactions":[],"unknowns":unknowns,
            "evidence_refs":[source["source_id"] for record in records for source in record["source_evidence"]],
            "source_request_ids":[],"visibility_policy_id":"identified_current_and_observed_inventory","truncated":len(subjects)>32,
            "usable_for_build_compilation":False,**experience}
