import json
from pathlib import Path
import tempfile
import unittest
from isaac_knowledge import KnowledgePackage, LookupRequest
from isaac_knowledge.importer import build_package, literal_rows
from combat.agent_store import AgentStore
from combat.builds import BuildResolver
from combat.geometry import GeometryService
from combat.item_knowledge import KnowledgeGateway
from combat.models import PickupObservation, Vec
from tests.combat_fixtures import snapshot


def package_fixture(root):
    for code in ("en_us","zh_cn"):
        for layer in ("ab+","rep"):
            lines = []
            for table in ("collectibles","trinkets","cards","pills"):
                head = f"EID.descriptions[languageCode].{table}" if layer=="ab+" else "local rep"+table[0].upper()+table[1:]
                description = "Base {{Tears}} boost" if layer=="ab+" else "Rep {{Tears}} boost#Conditional coverage unknown"
                lines.append(head+'={{"1","Fixture","'+description+'"},{"14","Roid Rage","{{Speed}} +0.3 Speed#{{Range}} +2.5 Range"}}')
            path = Path(root)/"source/descriptions"/layer/(code+".lua")
            path.parent.mkdir(parents=True,exist_ok=True); path.write_text("\n".join(lines),encoding="utf-8")
    destination = Path(root)/"package"
    build_package(Path(root)/"source",destination)
    return KnowledgePackage(destination)


def pickup(item_id=1, **changes):
    return PickupObservation.model_validate({"id":1,"instance_id":"test:pickup:1","variant":100,"sub_type":item_id,
        "pos":{"x":360,"y":160},"vel":{"x":0,"y":0},"collision_radius":10,"price":0,"shop_item_id":0,"wait":0,
        "options_pickup_index":0,"identity_visibility":"visible","identity_reason":"fixture",**changes})


class TestEIDPackage(unittest.TestCase):
    def test_literal_parser_does_not_execute_code_and_marks_expressions(self):
        text='local repCollectibles={ [1]={"1","Name","static"..dangerous()}, [2]={"2","Other","{{Warning}} text#next"} }; dangerous()'
        rows = list(literal_rows(text,r"local\s+repCollectibles"))
        self.assertFalse(rows[0][3]); self.assertTrue(rows[1][3])
        self.assertEqual(rows[1][2],"{{Warning}} text#next")
        with self.assertRaises(ValueError): list(literal_rows('local repCollectibles={{"1"',r"local\s+repCollectibles"))

    def test_overlay_language_markup_missing_and_readonly_contract(self):
        with tempfile.TemporaryDirectory() as root:
            package = package_fixture(root)
            request = LookupRequest(request_id="test",dataset_id=package.dataset_id,subjects=[{"kind":"collectible","id":1}])
            response = package.lookup(request)
            record = response["results"][0]["records"][0]
            self.assertEqual(response["status"],"partial")
            self.assertIn("Rep {{Tears}}",record["summary"]["text"])
            self.assertIn("\n",record["summary"]["text"])
            self.assertEqual(record["summary"]["language"],"zh-CN")
            request.subjects[0].id=999
            self.assertEqual(package.lookup(request)["status"],"unknown")
            request.target.build="different"
            self.assertEqual(package.lookup(request)["errors"][0]["code"],"DATASET_INCOMPATIBLE")
            self.assertIn("lookup",package.describe()["operations"])

    def test_ai_and_unreviewed_text_restrictions_and_cursor_binding(self):
        with tempfile.TemporaryDirectory() as root:
            package=package_fixture(root)
            request=LookupRequest(request_id="test",dataset_id=package.dataset_id,subjects=[{"kind":"collectible","id":1},{"kind":"card","id":1}],page={"limit":1})
            result=package.lookup(request)
            cursor=result["page"]["next_cursor"]
            self.assertTrue(cursor)
            request.page.cursor=cursor
            self.assertEqual(package.lookup(request)["results"][0]["subject"]["kind"],"card")
            request.language="en-US"
            self.assertEqual(package.lookup(request)["errors"][0]["code"],"CURSOR_MISMATCH")
            request.page.cursor=None; request.purpose="planning"
            record=package.lookup(request)["results"][0]["records"][0]
            self.assertEqual(record["summary"]["text"],"")
            self.assertEqual(record["claims"],[])
            package.database.write_bytes(b"corrupted")
            with self.assertRaisesRegex(ValueError,"DATASET_CORRUPT"): KnowledgePackage(package.directory)

    def test_source_and_target_capabilities_are_explicit(self):
        with tempfile.TemporaryDirectory() as root:
            package=package_fixture(root)
            base={"request_id":"q","dataset_id":package.dataset_id}
            evidence=package.get_evidence({**base,"evidence_ids":["eid:descriptions/rep/en_us.lua"]})
            self.assertEqual(evidence["evidence"][0]["text_state"],"metadata_only")
            interactions=package.lookup_interactions({**base,"participants":[{"subject":{"kind":"collectible","id":1},"count":1}]})
            self.assertFalse(interactions["coverage"]["exhaustive"])
            self.assertIn("INTERACTIONS_NOT_IMPORTED",interactions["warnings"])


class TestPickupDecisions(unittest.TestCase):
    def test_decisions_preserve_before_state_support_and_resource_boundaries(self):
        with tempfile.TemporaryDirectory() as root:
            gateway=KnowledgeGateway(package_fixture(root),AgentStore(Path(root)/"agent.sqlite"))
            s=snapshot();s.inventory["coins"]=2;build=BuildResolver().update(s)
            p=pickup();take=gateway.decide(s,build,p)
            self.assertEqual(take.decision,"take")
            self.assertTrue(take.expected_benefits)
            stored=gateway.store.get("encounter",take.encounter_id)
            self.assertNotIn("1",stored["inventory_before"]["collectibles"])
            expensive=gateway.decide(s,build,pickup(price=3))
            self.assertEqual(expensive.decision,"skip")
            self.assertIn("INSUFFICIENT_COINS_OR_RESERVE",expensive.reason_codes)
            self.assertEqual(gateway.decide(s,build,pickup(price=-1)).decision,"skip")
            hidden=gateway.decide(s,build,pickup(sub_type=None,identity_visibility="hidden"))
            self.assertEqual(hidden.decision,"skip")
            self.assertEqual(gateway.decide(s,build,pickup(wait=10)).decision,"defer")
            self.assertEqual(gateway.decide(s,build,pickup(item_id=999)).decision,"skip")

    def test_boss_stat_reward_is_taken_from_pinned_description_and_capability(self):
        with tempfile.TemporaryDirectory() as root:
            gateway=KnowledgeGateway(package_fixture(root),AgentStore(Path(root)/"agent.sqlite"))
            resolver=BuildResolver();gateway.build_resolver=resolver
            s=snapshot();s.room.update(room_type=5,stage=2);s.inventory["coins"]=0
            reward=pickup(14)
            result=gateway.decide(s,resolver.update(s),reward)
            self.assertEqual(result.decision,"take")
            self.assertIn("SUPPORTED_FREE_ITEM",result.reason_codes)
            self.assertTrue(result.evidence_refs)

    def test_skipped_pickup_blocks_path_but_allows_withdrawal(self):
        s=snapshot();s.pickup_exclusions=[{"id":"item","x":360,"y":160,"radius":10}]
        geometry=GeometryService(s)
        self.assertFalse(geometry.motion_clear(Vec(x=330,y=160),Vec(x=350,y=160)))
        self.assertFalse(geometry.line_clear(Vec(x=280,y=160),Vec(x=400,y=160)))
        self.assertTrue(geometry.line_clear(Vec(x=280,y=160),Vec(x=400,y=160),shot=True))
        self.assertTrue(geometry.pickup_path_clear(Vec(x=345,y=160),Vec(x=340,y=160)))
        self.assertTrue(geometry.motion_clear(Vec(x=345,y=160),Vec(x=340,y=160)))
        self.assertFalse(geometry.pickup_path_clear(Vec(x=345,y=160),Vec(x=350,y=160)))
