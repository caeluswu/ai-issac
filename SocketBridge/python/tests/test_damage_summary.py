import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from combat.agent_store import AgentStore
from combat.damage_summary import summarize,validate_summary,saved_for_review
from combat.exploration_budget import ExplorationBudget
from campaign.budget import BudgetExceeded


def analysis():
    return {"episode_id":"e","analysis_id":"a","interval":[1,2],"loss":{"damage":1},
        "direct_source":{"kind":"unknown"},"facts":[],"hypotheses":[],"missing_evidence":["source"],
        "evidence":{"e:1":{"code":"confirmed_loss","frame":2}},"build":{},"components":{},"constraints":[]}


def response():
    claim={"statement":"缺少来源实体，无法确定原因","evidence_level":"unknown","evidence_refs":["e:1"]}
    return {"episodes":[{"episode_id":"e","summary":"观察到损失，来源待确认。","source":{**claim,"kind":"unknown"},
        "hypotheses":[],"counterevidence":[],"missing_evidence":["source"],
        "recommended_action":{"kind":"add_observation","target":"source_entity","reason":"需要来源身份","evidence_refs":["e:1"]},
        "verification_test":"补采一次已知来源的伤害事件。"}]}


class FakeClient:
    model="fixture"
    def __init__(self,budget,raw=None,cancel=False):self.budget=budget;self.calls=0;self.raw=raw;self.cancel=cancel
    async def complete(self,messages):
        self.calls+=1;ticket=self.budget.reserve(messages,4096)
        if self.cancel:raise asyncio.CancelledError()
        self.budget.settle(ticket,{"prompt_tokens":100,"completion_tokens":100})
        return self.raw if self.raw is not None else json.dumps(response()),{"response_id":"fixture","response_model":"fixture"}


class TestDamageSummary(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.store=AgentStore(self.root/"agent.sqlite");self.budget=ExplorationBudget(self.root/"budget.sqlite")

    async def test_valid_summary_is_persisted_and_cached_without_second_request(self):
        client=FakeClient(self.budget)
        first=await summarize([analysis()],client,self.root/"summaries",self.store)
        second=await summarize([analysis()],client,self.root/"summaries",self.store)
        self.assertEqual(client.calls,1);self.assertTrue(second["cached"])
        self.assertEqual(first["status"],"summarized")
        self.assertEqual(self.store.get("damage_summary",first["summary_id"])["response"],response())

    async def test_bad_json_is_saved_and_not_blindly_retried(self):
        client=FakeClient(self.budget,raw="{invalid")
        first=await summarize([analysis()],client,self.root/"summaries",self.store)
        second=await summarize([analysis()],client,self.root/"summaries",self.store)
        self.assertEqual(first["status"],"failed");self.assertEqual(client.calls,1)
        self.assertEqual((Path(first["artifact"])/"response.txt").read_text(),"{invalid")
        self.assertTrue(second["cached"])

    async def test_unknown_evidence_is_rejected_and_raw_response_remains(self):
        doc=response();doc["episodes"][0]["source"]["evidence_refs"]=["fabricated"]
        result=await summarize([analysis()],FakeClient(self.budget,raw=json.dumps(doc)),self.root,self.store)
        self.assertEqual(result["status"],"failed")
        self.assertIn("SUMMARY_UNKNOWN_EVIDENCE_REFERENCE",result["validation_error"])

    async def test_cancellation_preserves_unknown_usage_and_artifacts(self):
        with self.assertRaises(asyncio.CancelledError):
            await summarize([analysis()],FakeClient(self.budget,cancel=True),self.root/"summaries",self.store)
        result=json.loads(next((self.root/"summaries").glob("*/result.json")).read_text())
        self.assertEqual(result["status"],"interrupted")
        self.assertEqual(self.budget.report()["unknown_usage_attempts"],1)

    async def test_episode_set_and_claim_references_are_validated(self):
        doc=response();doc["episodes"][0]["episode_id"]="wrong"
        with self.assertRaisesRegex(ValueError,"EPISODE_SET"):validate_summary(json.dumps(doc),[analysis()])
        doc=response();doc["episodes"][0]["source"].update(evidence_level="observed",evidence_refs=[])
        with self.assertRaisesRegex(ValueError,"UNREFERENCED"):validate_summary(json.dumps(doc),[analysis()])

    async def test_text_counterevidence_keeps_original_claim_and_validates_known_refs(self):
        doc=response();doc["episodes"][0]["counterevidence"]=["e:1 记录不完整，无法证明可避免。"]
        self.assertEqual(validate_summary(json.dumps(doc),[analysis()])["episodes"][0]["counterevidence"],doc["episodes"][0]["counterevidence"])
        doc["episodes"][0]["counterevidence"]=["f"*64+":99 fabricated"]
        with self.assertRaisesRegex(ValueError,"UNKNOWN_EVIDENCE"):validate_summary(json.dumps(doc),[analysis()])

    async def test_budget_survives_restart_and_does_not_allow_new_phase(self):
        for _ in range(3):self.budget.reserve([],256)
        other=ExplorationBudget(self.root/"budget.sqlite")
        with self.assertRaises(BudgetExceeded):other.reserve([],256)
        self.assertEqual(other.report()["requests"],3)
        with self.assertRaises(ValueError):ExplorationBudget(self.root/"budget.sqlite",phase="reset")

    async def test_summary_failure_does_not_modify_health_or_components(self):
        original=analysis();copy=json.loads(json.dumps(original))
        result=await summarize([original],FakeClient(self.budget,raw="{}"),self.root,self.store)
        self.assertEqual(result["status"],"failed");self.assertEqual(original,copy)

    async def test_invalid_reference_output_can_be_reviewed_without_rewriting_raw(self):
        import hashlib
        doc=response();doc["episodes"][0]["counterevidence"]=["f"*64+":2 another incident"]
        self.store.record("damage_analysis","a",analysis())
        directory=self.root/"summaries"
        result=await summarize([analysis()],FakeClient(self.budget,raw=json.dumps(doc)),directory,self.store)
        path=Path(result["artifact"])/"response.txt";before=hashlib.sha256(path.read_bytes()).hexdigest()
        original_result=(directory/result["summary_id"]/"result.json").read_bytes()
        reviewed=saved_for_review(result["summary_id"],directory,self.store)
        self.assertEqual(reviewed["status"],"needs_review")
        self.assertFalse(reviewed["automatic_use_allowed"])
        self.assertEqual(reviewed["protocol_checks"][0]["status"],"invalid")
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),before)
        self.assertEqual((directory/result["summary_id"]/"result.json").read_bytes(),original_result)
