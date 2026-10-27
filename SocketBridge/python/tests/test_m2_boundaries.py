import importlib.util
from pathlib import Path
import tempfile
import unittest
from combat.agent_store import AgentStore


class TestM2Boundaries(unittest.IsolatedAsyncioTestCase):
    async def test_review_scenarios_are_closed(self):
        root = Path(__file__).resolve().parents[3]
        spec = importlib.util.spec_from_file_location("readiness_diagnostics", root/"scripts/audit_m2_preparation.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        result = await module.audit()
        for finding in result["findings"]:
            with self.subTest(finding=finding["id"]):
                self.assertFalse(finding["issue_present"], finding)

    async def test_samples_deduplicate_after_reopen_and_equal_value_keeps_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"state.sqlite"
            samples = [{"sample_id":str(i),"value":10.} for i in range(3)]
            store = AgentStore(path)
            ref, value = store.passive_measurement("sig",samples,"normal_play")
            store.accept_claim("sig","launch_speed",value,[ref])
            store = AgentStore(path)
            self.assertIsNone(store.passive_measurement("sig",samples,"normal_play"))
            next_samples = [{"sample_id":str(i),"value":10.} for i in range(3,6)]
            ref, value = store.passive_measurement("sig",next_samples,"normal_play")
            store.accept_claim("sig","launch_speed",value,[ref])
            evidence = store.retrieve("sig")
            self.assertEqual(evidence["experience_revision"],1)
            self.assertEqual(len(evidence["local_claims"]),1)
            self.assertEqual(len(evidence["local_claims"][0]["evidence_refs"]),2)
