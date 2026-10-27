"""Clear-boundary model development, installation and finite trial feedback."""
import asyncio
import hashlib
import json
from pathlib import Path
from uuid import uuid4
from combat.boundary_safety import boundary_danger
from combat.damage import encoded
from combat.exploration_budget import ExplorationBudget
from combat.planner import DeepSeekClient,load_env
from .javascript import AttackModelRuntime
from .knife_developer import AttackStore,generate,check_source,recover_valid_response
from .knife import KnifeBuild,register_knife
from .knife_calibration import probe_knife,charge_rate


class ItemAdaptation:
    def __init__(self,root,gateway,*,allow_generate=False):
        self.root=Path(root);self.gateway=gateway;self.allow_generate=allow_generate
        self.store=AttackStore(self.root/"skills");self.runtime=None;self.package=None;self.client=None
        self.adapter=KnifeBuild(self.root/"artifacts/item-adaptation/knife-calibrations")
        active=self.store.active()
        if active:self.package=active;self.runtime=AttackModelRuntime(active[0])

    def model_client(self):
        if self.client is None:
            load_env(self.root/".env.local",override=True)
            budget=ExplorationBudget(self.root/"artifacts/damage-attribution/budget.sqlite",phase="item_adaptation",gap="moms_knife")
            self.client=DeepSeekClient(timeout=120,budget=budget,max_tokens=4096,retries=1,thinking="disabled")
        return self.client

    async def prepare(self,session,s):
        # Install before pickup preview/confirmation changes legacy type to -1.
        register_knife();session.build_resolver.adapters[-1]=self.adapter
        session.build_resolver.knife_enabled=bool(self.runtime or self.allow_generate)
        session.build_resolver.attack_model=self.runtime
        owned_item=(s.inventory.get("collectibles") or {}).get("114")
        if not owned_item:return s
        if not self.runtime and not self.allow_generate:raise RuntimeError("KNIFE_COMPONENT_UNAVAILABLE: use --learn-items or a published component")
        if s.hostiles or s.blocking_combat or boundary_danger(s) or s.paused:
            if self.runtime:return s
            raise RuntimeError("KNIFE_DEVELOPMENT_REQUIRES_CLEAR_BOUNDARY")
        self.gateway.lookup("collectible",114,purpose="component_development")
        build=session.build_resolver.update(s)
        if build.calibration_state=="needs_probe":
            calibration,s=await probe_knife(session,s)
            if calibration:self.adapter.adopt(calibration)
            else:raise RuntimeError("KNIFE_PROBE_NO_OBSERVATIONS")
        if self.runtime:
            self.bind_calibration()
            return s
        await session.release()
        frozen=s;owned=False
        try:
            if session.pause is not None:
                frozen,_=await session.pause_for_plan(s);owned=True
            directory=self.root/"artifacts/item-adaptation/development/moms-knife"
            package=await asyncio.to_thread(recover_valid_response,directory)
            if package:
                session.log("knife_candidate_revalidated",source_sha256=package[1]["source_sha256"],request_artifact=package[1]["request_artifact"])
            else:
                package=await generate(self.gateway,frozen,self.model_client(),directory,session.log,self.adapter.calibration)
            runtime=await asyncio.to_thread(AttackModelRuntime,package[0])
            self.package=package;self.runtime=runtime;session.build_resolver.attack_model=runtime
            self.bind_calibration()
            session.log("attack_model_installed",origin="generated_in_run",source_sha256=package[1]["source_sha256"],
                identity=runtime.identity,room_visit_id=s.room_visit_id,worker_pid=runtime.worker.process.pid)
        finally:
            if owned:
                await session.resume_owned_pause(frozen);session.pause.release_focus()
        return await session.next_snapshot(after=frozen.snapshot_id)

    def bind_calibration(self):
        value=self.adapter.calibration
        if self.runtime and value:
            self.runtime.calibration_summary={k:v for k,v in value.items() if k!="cycles"}
            self.runtime.calibration_summary["cycles"]=[{k:v for k,v in row.items() if k!="observations"} for row in value["cycles"]]
            self.runtime.calibration_summary["charge_per_held_update_estimate"]=charge_rate(value)

    async def review_trial(self,trial,log):
        if not self.package:raise RuntimeError("NO_ATTACK_MODEL_TRIAL")
        source,manifest,candidate=self.package
        client=self.model_client();directory=self.root/"artifacts/item-adaptation/feedback"/uuid4().hex;directory.mkdir(parents=True)
        messages=[{"role":"system","content":"Review a model-authored Mom's Knife (114) game component. Decide keep/revise/defer from the actual trial. Distinguish held contact, thrown hits and return; do not invent causal certainty. Return JSON {decision,reason,source}; source is null for keep/defer, or a complete JS replacement with the same init/observe/proposeBatch SDK for revise. Consider clear result, health loss in HALF HEARTS, actual throws/hits/misses, output, time, failures and runtime cost. Do not revise just to show a change."},
            {"role":"user","content":encoded({"source":source,"trial":trial,"limits":{"remaining_total_requests":24-client.budget.report()["requests"],"gap_requests_max":3}})}]
        (directory/"request.json").write_text(encoded({"model":client.model,"messages":messages})+"\n",encoding="utf-8")
        try:
            raw,usage=await client.complete(messages)
            (directory/"response.json").write_bytes(raw.encode());(directory/"usage.json").write_text(encoded(usage)+"\n",encoding="utf-8")
            decision=json.loads(raw)
            if decision.get("decision") not in ("keep","revise","defer") or not isinstance(decision.get("reason"),str):raise ValueError("INVALID_TRIAL_FEEDBACK")
            if decision["decision"]=="revise":
                replacement=decision["source"];validation=await asyncio.to_thread(check_source,replacement)
                sha=hashlib.sha256(replacement.encode()).hexdigest();path=directory/"candidate";path.mkdir()
                new_manifest={**manifest,"source_sha256":sha,"response_sha256":hashlib.sha256(raw.encode()).hexdigest(),
                    "response_id":usage.get("response_id"),"response_model":usage.get("response_model"),"status":"trial",
                    "request_artifact":str(directory),"validation":validation,"parent_source_sha256":manifest["source_sha256"]}
                (path/"source.js").write_bytes(replacement.encode());(path/"model-response.json").write_bytes(raw.encode())
                (path/"manifest.json").write_text(encoded(new_manifest)+"\n",encoding="utf-8")
                new_runtime=AttackModelRuntime(replacement)
                self.runtime.close();self.runtime=new_runtime;self.package=(replacement,new_manifest,path)
            elif decision["decision"]=="keep" and trial["result"]=="CLEARED":
                path=await asyncio.to_thread(self.store.publish,candidate,trial)
                decision["published"]=str(path)
            result={**decision,"artifact":str(directory),"budget":client.budget.report()}
            (directory/"result.json").write_text(encoded(result)+"\n",encoding="utf-8")
            log("attack_model_feedback",**result)
            return result
        except Exception as exc:
            result={"decision":"failed","error_type":type(exc).__name__,"artifact":str(directory),"budget":client.budget.report()}
            (directory/"result.json").write_text(encoded(result)+"\n",encoding="utf-8");log("attack_model_feedback_failed",**result)
            return result

    async def close(self):
        if self.runtime:await asyncio.to_thread(self.runtime.close)
        if self.client:
            await self.client.aclose()
            self.client=None
