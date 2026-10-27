import asyncio
import hashlib
import json
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
from unittest.mock import Mock
import httpx
from campaign.budget import BudgetExceeded
from mechanisms.budget import DevelopmentBudget
from mechanisms.developer import MechanismDeveloper
from mechanisms.program import Program,ProgramError,canonical
from mechanisms.store import SkillStore,read_candidate
from mechanisms.validation import validate_candidate
from combat.planner import EmptyModelResponse
from tests.test_mechanism_program import fixture
from tests.test_mechanism_weapon import CALIBRATION


class TestDevelopmentBudget(unittest.TestCase):
    def test_explicit_single_extension_preserves_every_attempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            b=DevelopmentBudget(Path(tmp)/"budget.sqlite")
            for _ in range(3):b.reserve([],8192)
            b.authorize_one_extra("fixture human approval");b.authorize_one_extra("duplicate cannot add another")
            ticket=DevelopmentBudget(b.path).reserve([],8192)
            self.assertEqual(ticket["output"],4096)
            with self.assertRaises(BudgetExceeded):b.reserve([])
            self.assertEqual(b.report()["requests"],4)
            self.assertEqual(b.report()["unknown_usage_attempts"],4)

    def test_restarts_and_unknown_usage_do_not_refund_attempts(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"budget.sqlite"
            for _ in range(3):DevelopmentBudget(path).reserve([])
            with self.assertRaises(BudgetExceeded):DevelopmentBudget(path).reserve([])
            r=DevelopmentBudget(path).report()
            self.assertEqual(r["requests"],3);self.assertEqual(r["unknown_usage_attempts"],3)
            self.assertIsNone(r["cost"])

    def test_duplicate_settle_and_changed_limits_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            b=DevelopmentBudget(Path(tmp)/"budget.sqlite");ticket=b.reserve([])
            b.settle(ticket,{"prompt_tokens":20,"completion_tokens":40})
            with self.assertRaises(ValueError):b.settle(ticket,{})
            with self.assertRaises(ValueError):DevelopmentBudget(b.path,max_requests=2)
            self.assertEqual(b.report()["input_actual"],20)

    def test_concurrent_connections_share_global_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"budget.sqlite";a,b=DevelopmentBudget(path),DevelopmentBudget(path)
            a.reserve([]);b.reserve([]);a.reserve([])
            with self.assertRaises(BudgetExceeded):b.reserve([])


class FakeClient:
    model="test-fixture"
    def __init__(self,budget,raw):self.budget,self.raw=budget,raw;self.calls=0;self.messages=[]
    async def complete(self,messages):
        ticket=self.budget.reserve(messages)
        self.calls+=1;self.messages.append(messages)
        self.budget.settle(ticket,{"prompt_tokens":100,"completion_tokens":100})
        return self.raw,{"prompt_tokens":100,"completion_tokens":100,"response_model":"test-fixture","response_id":"test-response"}


def request():
    return {"task":{"task_id":"test-task"},"sources":{"local":{"source_id":"engineering-language-fixture","calibration":CALIBRATION},"eid":{"results":[]}}}


class TestMechanismDevelopment(unittest.IsolatedAsyncioTestCase):
    def test_session_skill_loading_is_explicit_and_not_environment_dependent(self):
        from combat.runtime import CombatSession
        with patch("mechanisms.integration.load_session_skills") as load:
            CombatSession(Mock(),None,None,Mock())
            load.assert_not_called()
            CombatSession(Mock(),None,None,Mock(),skill_store=Path("fixture-skills"))
            self.assertEqual(load.call_args.args[1],Path("fixture-skills"))
    async def test_empty_final_content_stops_without_blind_repair(self):
        with tempfile.TemporaryDirectory() as tmp:
            b=DevelopmentBudget(Path(tmp)/"budget.sqlite")
            class Empty(FakeClient):
                async def complete(self,messages):
                    self.budget.reserve(messages);self.calls+=1
                    raise EmptyModelResponse({"finish_reason":"length","response_id":"fixture"})
            client=Empty(b,"")
            r=await MechanismDeveloper(client,b,tmp,author="test_fixture").develop(request())
            self.assertEqual(client.calls,1)
            self.assertEqual(r["stop_reason"],"EMPTY_MODEL_CONTENT_REQUIRES_TRANSPORT_REVIEW")
    async def test_authentication_error_stops_without_repair_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            b=DevelopmentBudget(Path(tmp)/"budget.sqlite")
            class AuthFailure(FakeClient):
                async def complete(self,messages):
                    self.budget.reserve(messages);self.calls+=1
                    response=httpx.Response(401,request=httpx.Request("POST","https://api.example.invalid/chat/completions"))
                    raise httpx.HTTPStatusError("fixture",request=response.request,response=response)
            client=AuthFailure(b,"")
            r=await MechanismDeveloper(client,b,tmp,author="test_fixture").develop(request())
            self.assertEqual(client.calls,1);self.assertEqual(r["stop_reason"],"DEVELOPMENT_HTTP_401")

    async def test_failed_candidate_repairs_are_bounded_and_survive_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            budget=DevelopmentBudget(Path(tmp)/"budget.sqlite")
            client=FakeClient(budget,json.dumps(fixture()))
            result=await MechanismDeveloper(client,budget,tmp,author="test_fixture").develop(request())
            self.assertEqual(result["status"],"rejected");self.assertEqual(client.calls,3)
            self.assertTrue(client.messages[1][-2]["role"]=="assistant")
            other=FakeClient(DevelopmentBudget(budget.path),"{}")
            again=await MechanismDeveloper(other,other.budget,tmp,author="test_fixture").develop(request())
            self.assertEqual(other.calls,0);self.assertIn("BUDGET",again["stop_reason"])

    async def test_explicit_primitive_gap_stops_after_one_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            b=DevelopmentBudget(Path(tmp)/"budget.sqlite")
            client=FakeClient(b,'{"requires_host_primitive":"fixture missing observation"}')
            result=await MechanismDeveloper(client,b,tmp,author="test_fixture").develop(request())
            self.assertEqual(result["status"],"requires_host_primitive");self.assertEqual(client.calls,1)

    async def test_json_normalization_preserves_original_response_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            b=DevelopmentBudget(Path(tmp)/"budget.sqlite");raw=json.dumps(fixture(),indent=2)
            client=FakeClient(b,raw)
            # IO seam only: this is not evidence of a working mechanism.
            fake={"passed":True,"checks":["test_fixture"],"errors":[]}
            with patch("mechanisms.developer.validate_candidate",return_value=fake):
                result=await MechanismDeveloper(client,b,tmp,author="test_fixture").develop(request())
            candidate=Path(result["candidate"])
            prov=json.loads((candidate/"provenance.json").read_text())
            self.assertEqual(prov["response_sha256"],hashlib.sha256(raw.encode()).hexdigest())
            with self.assertRaisesRegex(ProgramError,"INDEPENDENT"):read_candidate(candidate)
            # Semantic rewrites cannot retain the old model response identity.
            doc=fixture();doc["decide"]["mode"]=2
            (candidate/"program.json").write_bytes(canonical(doc))
            prov["program_sha256"]=Program(doc).sha256
            (candidate/"provenance.json").write_bytes(canonical(prov))
            with self.assertRaisesRegex(ProgramError,"PROVENANCE"):read_candidate(candidate)

    def test_calibration_cannot_expand_host_work_or_scope(self):
        c=dict(CALIBRATION);c["max_charge_frames"]=1_000_000
        result=validate_candidate(Program(fixture()),c)
        self.assertFalse(result["passed"]);self.assertEqual(result["errors"][0]["check"],"calibration")

    def test_generic_compiler_does_not_grant_support_for_a_new_item(self):
        d=fixture();d["target_item"]=48
        program=Program(d)
        result=validate_candidate(program,CALIBRATION)
        self.assertFalse(result["passed"]);self.assertEqual(result["errors"][0]["check"],"support_scope")

    def test_skill_index_cannot_read_outside_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/"skills";root.mkdir()
            (root/"index.json").write_text(json.dumps({"schema_version":1,"active":{"69":{"path":"../elsewhere","manifest_sha256":"fake"}}}))
            with self.assertRaisesRegex(ProgramError,"OUTSIDE"):SkillStore(root).active()

    async def test_mocked_publication_io_and_tamper_detection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);b=DevelopmentBudget(root/"budget.sqlite")
            raw=json.dumps(fixture(),indent=2);client=FakeClient(b,raw)
            fake={"passed":True,"checks":["mocked_io_only"],"errors":[]}
            with patch("mechanisms.developer.validate_candidate",return_value=fake):
                result=await MechanismDeveloper(client,b,root/"development",author="test_fixture").develop(request())
            candidate=Path(result["candidate"])
            trial=root/"trial";trial.mkdir()
            p=Program(raw)
            (trial/"mechanism-trial.json").write_text(json.dumps({"program_sha256":p.sha256,"normal_combat":True,"released_actions":2,"adopted_actions":4,
                "applied_releases":2,"new_tears_observed":2,"game_build":"1.7.9b.J835","calibration_id":CALIBRATION["calibration_id"],"mechanism_failures":[],"passed":True}))
            (trial/"report.json").write_text('{"success":true,"result":"CLEARED"}')
            store=SkillStore(root/"skills")
            with patch("mechanisms.store.validate_candidate",return_value=fake):
                with self.assertRaisesRegex(ProgramError,"AUTHORSHIP"):store.publish(candidate,trial)
                # Simulated provider metadata tests IO; never leaves tempdir and
                # never counts as real model or game acceptance evidence.
                prov=json.loads((candidate/"provenance.json").read_text());prov["author"]="deepseek"
                (candidate/"provenance.json").write_bytes(canonical(prov))
                package=store.publish(candidate,trial)
                self.assertEqual(store.active()[0].sha256,p.sha256)
                store.quarantine(p.sha256,"fixture quarantine",trial)
                self.assertIsNone(store.active())
                (package/"model-response.json").write_text('{}')
                with self.assertRaises(ProgramError):store.load(package)
