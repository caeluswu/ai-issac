"""Reusable tactics with fresh authority; no old context or lease is reused."""
import asyncio
from collections import OrderedDict
from combat.builds import digest
from combat.catalog import compile_plan, default_plan, current_revision
from combat.models import ContextRef
from .budget import BudgetExceeded


class FloorPlanner:
    def __init__(self, planner, log):
        self.planner, self.log = planner,log
        self.templates = OrderedDict()
        self.last_source = None
        planner.compact_context = True
        planner.single_stage = True
        planner.allow_tools = False

    @staticmethod
    def key(s,build):
        return digest([build.signature,s.room.get("room_type"),s.room.get("room_shape"),
            sorted(set((e.type,e.variant,e.is_boss) for e in s.hostiles)),
            sorted((c["type"],c["state"]) for c in (s.layout["grid"] or {}).values())])

    async def plan(self,s,build,revision=0,feedback=None):
        ref = ContextRef(run_id=s.run_id,room_visit_id=s.room_visit_id,snapshot_id=s.snapshot_id,
            catalog_revision=current_revision(),
            floor_instance_id=s.floor.instance_id if s.floor else None,build_revision=build.revision,
            experience_revision=build.experience_revision,plan_revision=revision)
        key = self.key(s,build)
        if not feedback and key in self.templates:
            value = self.templates[key].model_dump()
            value["context_ref"] = ref.model_dump()
            self.last_source = "deepseek_template"
            self.log("plan_template_rebound",key=key,context_ref=ref.model_dump())
            return compile_plan(value,ref,capabilities=(build.family,))
        try:
            value = await asyncio.wait_for(self.planner.plan(s,build,revision,feedback),20 if feedback else 45)
            value = compile_plan(value,ref,capabilities=(build.family,))
            self.last_source = "deepseek"
            self.templates[key] = value.model_copy(deep=True)
            if len(self.templates)>32: self.templates.popitem(last=False)
            return value
        except BudgetExceeded as exc:
            self.last_source = "local_fallback"
            self.log("floor_request_budget_exhausted",error=str(exc),context_ref=ref.model_dump())
            return default_plan(ref,family=build.family)
