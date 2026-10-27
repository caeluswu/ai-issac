import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from isaac_knowledge import KnowledgePackage, LookupRequest, QueriedItemStore
from isaac_knowledge.item_store import ItemStoreError
from tests.test_item_knowledge import package_fixture


def policy(package, **changes):
    return {"policy_id":"test-project-use", "dataset_id":package.dataset_id,
            "allow_local_storage":True, "allow_ai_input":True,
            "authorization_basis":"test fixture", **changes}


def request(package, **changes):
    return LookupRequest(request_id="query", dataset_id=package.dataset_id,
                         subjects=[{"kind":"collectible","id":1}], **changes)


def text(response):
    return response["results"][0]["records"][0]["summary"]["text"]


class TestQueriedItemKnowledge(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.source=package_fixture(self.root)
        self.cache=QueriedItemStore(self.root/"queried.sqlite")
        self.package=KnowledgePackage(self.source.directory,usage_policy=policy(self.source),item_store=self.cache)

    def test_project_policy_enables_ai_text_without_relabeling_source_rights(self):
        for purpose in ("display","planning","component_development"):
            answer=self.package.lookup(request(self.package,purpose=purpose))
            self.assertTrue(text(answer))
            record=answer["results"][0]["records"][0]
            self.assertEqual(record["source_evidence"][0]["rights"]["ai_input"],"unknown")
            self.assertEqual(record["claims"][0]["evidence"]["review_state"],"unreviewed")
        self.assertFalse(text(self.source.lookup(request(self.source,purpose="planning"))))
        with self.assertRaises(ValueError):
            LookupRequest.model_validate({**request(self.package).model_dump(),"allow_ai_input":True})

    def test_first_query_persists_one_item_and_new_instance_reuses_it(self):
        before=hashlib.sha256(self.source.database.read_bytes()).hexdigest()
        first=self.package.lookup(request(self.package,purpose="planning"))
        self.assertEqual(first["local_knowledge"]["saved_records"],1)
        reopened=KnowledgePackage(self.source.directory,usage_policy=policy(self.source),
                                  item_store=QueriedItemStore(self.cache.path))
        with patch.object(reopened,"_source_item",side_effect=AssertionError("must use stored item")):
            second=reopened.lookup(request(reopened,purpose="component_development"))
        self.assertEqual(text(first),text(second))
        self.assertEqual(second["local_knowledge"]["cache_hits"],1)
        rows=self.cache.list_items(provider_id="eid_snapshot",dataset_id=self.package.dataset_id)
        self.assertEqual([(r["id"],r["requested_language"]) for r in rows],[(1,"zh-CN")])
        self.assertEqual(hashlib.sha256(self.source.database.read_bytes()).hexdigest(),before)

    def test_cached_text_is_rendered_using_current_policy(self):
        self.assertTrue(text(self.package.lookup(request(self.package))))
        strict=KnowledgePackage(self.source.directory,usage_policy=policy(self.source,allow_ai_input=False),item_store=self.cache)
        self.assertFalse(text(strict.lookup(request(strict,purpose="planning"))))
        self.assertFalse(text(self.package.lookup(request(self.package,purpose="build_compilation",
                                                        policy={"include_unreviewed":False}))))
        self.assertFalse(text(self.package.lookup(request(self.package,policy={"include_unreviewed":False}))))
        # An explicit upstream prohibition cannot be overridden by host policy.
        self.package.manifest["rights"]["ai_input"]="forbidden"
        self.assertFalse(text(self.package.lookup(request(self.package,purpose="component_development"))))

    def test_scope_and_storage_authorization_checked_before_any_write(self):
        with self.assertRaisesRegex(ValueError,"SCOPE_MISMATCH"):
            KnowledgePackage(self.source.directory,usage_policy=policy(self.source,dataset_id="wrong"))
        with self.assertRaisesRegex(ValueError,"NOT_AUTHORIZED"):
            KnowledgePackage(self.source.directory,item_store=self.cache)
        bad_path=self.source.directory/"should-not-exist.sqlite"
        with self.assertRaisesRegex(ValueError,"INSIDE_IMMUTABLE"):
            KnowledgePackage(self.source.directory,usage_policy=policy(self.source),item_store=QueriedItemStore(bad_path))
        self.assertFalse(bad_path.exists())

    def test_missing_item_does_not_create_fake_knowledge(self):
        req=request(self.package);req.subjects[0].id=999
        answer=self.package.lookup(req)
        self.assertEqual(answer["status"],"unknown")
        self.assertEqual(answer["local_knowledge"]["saved_records"],0)
        self.assertEqual(self.cache.list_items(provider_id="eid_snapshot",dataset_id=self.package.dataset_id),[])

    def test_english_fallback_is_preserved_on_cache_hit(self):
        original=self.package._source_item
        with patch.object(self.package,"_source_item",side_effect=lambda k,i,l:None if l=="zh-CN" else original(k,i,l)):
            first=self.package.lookup(request(self.package))
        with patch.object(self.package,"_source_item",side_effect=AssertionError("cache required")):
            second=self.package.lookup(request(self.package))
        for answer in (first,second):
            self.assertIn("LANGUAGE_FALLBACK",answer["results"][0]["reason_codes"])
            self.assertEqual(answer["results"][0]["records"][0]["summary"]["language"],"en-US")

    def test_cache_corruption_is_repaired_from_source(self):
        expected=text(self.package.lookup(request(self.package)))
        for broken in ("tampered",b"unexpected blob"):
            with self.cache.connect() as db:db.execute("UPDATE item_knowledge SET record_json=?",(broken,))
            with self.assertRaisesRegex(ItemStoreError,"requery collectible:1"):
                self.cache.list_items(provider_id="eid_snapshot",dataset_id=self.package.dataset_id)
            answer=self.package.lookup(request(self.package))
            self.assertEqual(text(answer),expected)
            self.assertEqual(answer["local_knowledge"]["cache_repairs"],1)
            self.assertEqual(answer["local_knowledge"]["saved_records"],1)
        with self.cache.connect() as db:self.assertEqual(db.execute("SELECT COUNT(*) FROM item_knowledge").fetchone()[0],1)

    def test_refresh_upserts_and_preserves_first_query_time(self):
        self.package.lookup(request(self.package))
        first=self.cache.list_items(provider_id="eid_snapshot",dataset_id=self.package.dataset_id)[0]
        answer=self.package.lookup(request(self.package),refresh=True)
        rows=self.cache.list_items(provider_id="eid_snapshot",dataset_id=self.package.dataset_id)
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]["first_queried_at"],first["first_queried_at"])
        self.assertEqual(answer["local_knowledge"]["saved_records"],1)
        self.assertEqual(answer["local_knowledge"]["cache_hits"],0)

    def test_dataset_versions_and_requested_languages_have_separate_keys(self):
        self.package.lookup(request(self.package))
        self.package.lookup(request(self.package,language="en-US"))
        with self.cache.connect() as db:
            db.execute("UPDATE item_knowledge SET dataset_id='old-version'")
        answer=self.package.lookup(request(self.package))
        self.assertEqual(answer["local_knowledge"]["cache_hits"],0)
        with self.cache.connect() as db:self.assertEqual(db.execute("SELECT COUNT(*) FROM item_knowledge").fetchone()[0],3)

    def test_failed_save_is_reported_as_failure_not_success(self):
        with patch.object(self.cache,"save",side_effect=ItemStoreError("disk unavailable")):
            answer=self.package.lookup(request(self.package))
        self.assertEqual(answer["status"],"error")
        self.assertEqual(answer["errors"][0]["code"],"LOCAL_KNOWLEDGE_UNAVAILABLE")
        self.assertEqual(answer["results"],[])

    def test_development_context_ignores_hit_miss_counters(self):
        from mechanisms.developer import skill_request
        from combat.item_knowledge import KnowledgeGateway
        root=Path(__file__).resolve().parents[3]
        calibration=json.loads((root/"configs/mechanisms/chocolate-milk-j835.json").read_text(encoding="utf-8"))
        scope=calibration["scope"]
        task={"task_id":"fixture","dataset_id":self.package.dataset_id,"subject":{"variant":100,"id":69},
              "observed_stats":{k:scope[k] for k in ("player_type","weapon_type","damage","max_fire_delay","shot_speed")},
              "observed_inventory":{"collectibles":{},"trinkets":[]}}
        # Fixture item 1 stands in for 69; exercise actual SDK policy/storage.
        gateway=KnowledgeGateway(self.package,None)
        actual=gateway.lookup
        with patch.object(KnowledgeGateway,"from_config",return_value=gateway),patch.object(gateway,"lookup",side_effect=lambda k,i,**kw:actual(k,1,**kw)):
            first=skill_request(root,task)
            second=skill_request(root,task)
        for unavailable in ({"status":"error","results":[]},{"status":"unknown","results":[]}):
            with patch.object(KnowledgeGateway,"from_config",return_value=gateway),patch.object(gateway,"lookup",return_value=unavailable):
                with self.assertRaisesRegex(ValueError,"DEVELOPMENT_EID_TEXT_UNAVAILABLE"):
                    skill_request(root,task)
        self.assertEqual(first,second)
        self.assertTrue(text(first["sources"]["eid"]))
        self.assertNotIn("local_knowledge",first["sources"]["eid"])
