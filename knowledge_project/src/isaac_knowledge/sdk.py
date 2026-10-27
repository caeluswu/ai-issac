"""Contract 1.0 adapter over immutable sources, with optional local item storage.

Queries never access the network or execute source code.
"""
import base64
import hashlib
import json
from pathlib import Path
import sqlite3
from contextlib import closing
from .contracts import GameTarget, LookupRequest, LocalUsagePolicy
from .importer import dataset_identity
from .item_store import QueriedItemStore, ItemStoreError, InvalidCachedItem


class KnowledgePackage:
    def __init__(self, directory, *, usage_policy=None, item_store=None):
        self.directory = Path(directory).resolve()
        self.manifest = json.loads((self.directory/"manifest.json").read_text(encoding="utf-8"))
        self.database = self.directory/"knowledge.sqlite"
        if self.manifest.get("api_versions") != ["1.0"]: raise ValueError("UNSUPPORTED_API")
        if hashlib.sha256(self.database.read_bytes()).hexdigest() != self.manifest["database_sha256"]:
            raise ValueError("DATASET_CORRUPT")
        self.dataset_id = self.manifest["dataset_id"]
        expected = dataset_identity(self.manifest["commit"],self.manifest["parser_version"],self.manifest["source_hashes"],
                                    self.manifest["game_target"],self.manifest["rights"])
        if self.dataset_id != expected: raise ValueError("DATASET_CORRUPT: manifest semantic identity")
        self.usage_policy=LocalUsagePolicy.model_validate(usage_policy) if usage_policy is not None else None
        if self.usage_policy and (self.usage_policy.dataset_id!=self.dataset_id or self.usage_policy.provider_id!=self.manifest["provider_id"]):
            raise ValueError("LOCAL_USAGE_POLICY_SCOPE_MISMATCH")
        if item_store is not None:
            if not isinstance(item_store,QueriedItemStore):raise ValueError("Invalid item knowledge store")
            if item_store.path.is_relative_to(self.directory):raise ValueError("ITEM_STORE_INSIDE_IMMUTABLE_PACKAGE")
            if not self.usage_policy or not self.usage_policy.allow_local_storage or self.manifest["rights"].get("store_text") not in ("allowed","unknown"):
                raise ValueError("LOCAL_ITEM_STORAGE_NOT_AUTHORIZED")
        self.item_store=item_store

    def usage_metadata(self):
        if self.usage_policy is None:return None
        policy=self.usage_policy.model_dump(mode="json")
        return {"policy_id":policy["policy_id"],"policy_sha256":hashlib.sha256(json.dumps(policy,sort_keys=True).encode()).hexdigest(),
                "allow_local_storage":policy["allow_local_storage"],"allow_ai_input":policy["allow_ai_input"],
                "purposes":policy["purposes"],
                "authority":"project_configuration","source_rights_unchanged":True}

    def describe(self):
        return {"api_versions":["1.0"],"dataset_ids":[self.dataset_id],"operations":["describe","lookup","lookup_interactions","get_evidence"],
                "missing_operations":["search","submit_enrichment","get_job","submit_feedback"],
                "ontology_versions":[self.manifest["ontology_version"]],"targets":[self.manifest["game_target"]],
                "languages":self.manifest["languages"],"limits":{"subjects":32,"response_bytes":262144},
                "local_usage_policy":self.usage_metadata(),"query_populated_item_store":self.item_store is not None}

    def envelope(self, req, status="unknown"):
        return {"api_version":"1.0","request_id":req.request_id,"dataset_id":self.dataset_id,"status":status,
                "results":[],"warnings":[],"errors":[],"page":{"next_cursor":None,"truncated":False},
                "local_usage_policy":self.usage_metadata()}

    def _source_item(self,kind,item_id,language):
        with closing(sqlite3.connect(self.database.as_uri()+"?mode=ro&immutable=1",uri=True)) as db:
            row=db.execute("SELECT record FROM records WHERE kind=? AND id=? AND language=?",(kind,item_id,language)).fetchone()
        return json.loads(row[0]) if row else None

    def _item(self,subject,language,stats,refresh=False):
        key=(self.manifest["provider_id"],self.dataset_id,subject.kind,subject.id,language)
        if self.item_store is not None and not refresh:
            try:
                cached=self.item_store.get(key)
                if cached:
                    item,source_sha=cached
                    required={"kind","id","language","name","normalized_text","source_path","source_line","literal_complete"}
                    if (not isinstance(item,dict) or not required.issubset(item) or item["kind"]!=subject.kind or item["id"]!=subject.id or
                            any(not isinstance(item[k],str) for k in ("kind","language","name","normalized_text","source_path")) or
                            type(item["id"]) is not int or type(item["source_line"]) is not int or type(item["literal_complete"]) is not bool or
                            item["language"] not in (language,"en-US") or self.manifest["source_hashes"].get(item["source_path"])!=source_sha):
                        raise InvalidCachedItem("ITEM_KNOWLEDGE_SCOPE_MISMATCH")
                    stats["cache_hits"]+=1
                    return item
            except InvalidCachedItem:stats["cache_repairs"]+=1
        stats["source_reads"]+=1
        item=self._source_item(subject.kind,subject.id,language)
        if item is None and language!="en-US":item=self._source_item(subject.kind,subject.id,"en-US")
        if item is not None and self.item_store is not None:
            self.item_store.save(key,item,self.manifest["source_hashes"][item["source_path"]])
            stats["saved_records"]+=1
        return item

    def lookup(self, request, *, refresh=False):
        req = request if isinstance(request, LookupRequest) else LookupRequest.model_validate(request)
        out = self.envelope(req)
        stats={"cache_hits":0,"source_reads":0,"saved_records":0,"cache_repairs":0}
        if self.item_store is not None:out["local_knowledge"]={"storage":"sqlite","schema_version":1,**stats}
        if req.dataset_id != self.dataset_id or req.target.model_dump() != self.manifest["game_target"]:
            out.update(status="error",errors=[{"code":"DATASET_INCOMPATIBLE","message":"Pinned dataset or target mismatch","subject":None,"retryable":False}])
            return out
        key = req.model_dump(); key.pop("request_id"); key["page"]["cursor"] = None
        query = hashlib.sha256(json.dumps(key,sort_keys=True).encode()).hexdigest()
        offset = 0
        if req.page.cursor:
            try:
                cursor = json.loads(base64.urlsafe_b64decode(req.page.cursor))
                if cursor["query"] != query or type(cursor["offset"]) is not int or not 0 <= cursor["offset"] < len(req.subjects): raise ValueError()
                offset = cursor["offset"]
            except Exception:
                out.update(status="error",errors=[{"code":"CURSOR_MISMATCH","message":"Cursor does not match query","subject":None,"retryable":False}])
                return out
        try:
            for subject in req.subjects[offset:offset+req.page.limit]:
                item=self._item(subject,req.language,stats,refresh)
                language=item["language"] if item else req.language
                result = {"subject":subject.model_dump(exclude_none=True),"status":"unknown","records":[],"missing_facets":list(req.facets),"reason_codes":[]}
                if item:
                    record = self.record(item, req)
                    result.update(status="partial", records=[record], missing_facets=[f for f in req.facets if f != "identity"],
                                  reason_codes=["CONDITIONAL_AND_INTERACTION_COVERAGE_INCOMPLETE"])
                    if language != req.language: result["reason_codes"].append("LANGUAGE_FALLBACK")
                    if not record["summary"]["text"]: result["reason_codes"].append("UNREVIEWED_OR_AI_INPUT_POLICY_RESTRICTED")
                else: result["reason_codes"] = ["NO_APPLICABLE_RECORD"]
                if out["results"] and len(json.dumps(out,ensure_ascii=False).encode())+len(json.dumps(result,ensure_ascii=False).encode()) > 240000: break
                out["results"].append(result)
        except ItemStoreError as exc:
            if self.item_store is not None:out["local_knowledge"].update(stats)
            out.update(status="error",errors=[{"code":"LOCAL_KNOWLEDGE_UNAVAILABLE","message":str(exc),"subject":None,"retryable":True}])
            return out
        except (sqlite3.Error, OSError, ValueError) as exc:
            out.update(status="error",errors=[{"code":"DATASET_CORRUPT","message":type(exc).__name__,"subject":None,"retryable":False}])
            return out
        if self.item_store is not None:out["local_knowledge"].update(stats)
        next_offset = offset+len(out["results"])
        if next_offset < len(req.subjects):
            out["page"] = {"next_cursor":base64.urlsafe_b64encode(json.dumps({"query":query,"offset":next_offset}).encode()).decode(),"truncated":True}
        out["status"] = "partial" if any(r["records"] for r in out["results"]) else "unknown"
        return out

    def evidence(self, item):
        relative = item["source_path"]
        return {"source_id":"eid:"+relative,"provider_id":"eid_snapshot","locator":"https://github.com/wofsauge/External-Item-Descriptions/blob/"+self.manifest["commit"]+"/"+relative+"#L"+str(item["source_line"]),
                "revision":self.manifest["commit"],"retrieved_at":self.manifest["created_at"],"content_sha256":self.manifest["source_hashes"][relative],
                "parser_version":self.manifest["parser_version"],"declared_game_scope":self.manifest["game_target"],
                "rights":dict(self.manifest["rights"]),"verification_refs":[]}

    def record(self, item, req):
        identity = f"{self.dataset_id}:{item['kind']}:{item['id']}:{item['language']}"
        source = self.evidence(item)
        source_ai=self.manifest["rights"]["ai_input"]
        project_ai=(self.usage_policy is not None and self.usage_policy.allow_ai_input and req.purpose in self.usage_policy.purposes)
        allow_text = req.purpose == "display" or (req.purpose != "build_compilation" and
                     (source_ai == "allowed" or source_ai == "unknown" and project_ai))
        allow_text &= req.policy.include_unreviewed
        text = item["normalized_text"][:8000] if allow_text else ""
        claim_id = identity+":text"
        claims = []
        if text:
            claims = [{"claim_id":claim_id,"subject":{"kind":item["kind"],"id":item["id"]},"predicate":"item.effect",
                "value":{"type":"text","value":text},"scope":"unknown","conditions":{"all":[]},"unknown_conditions":True,
                "applicability":{**req.target.model_dump(),"match":"exact"},"evidence":{"kind":"source_statement","review_state":"unreviewed","verification_refs":[]},
                "source_refs":[source["source_id"]]}]
        return {"record_id":identity,"subject_key":f"{item['kind']}:{item['id']}","subject":{"kind":item["kind"],"id":item["id"]},
                "labels":{item["language"]:item["name"]},"knowledge_state":"partial", "summary":{"language":item["language"],"text":text,"claim_refs":[claim_id] if claims else []},
                "claims":claims,"coverage":{f:("covered" if f == "identity" else "partial" if f == "effects" and text else "unknown") for f in req.facets},
                "conflict_groups":[],"source_evidence":[source],"parser_complete":item["literal_complete"],"truncated":len(item["normalized_text"])>8000}

    def lookup_interactions(self, request):
        # No local synergy evaluator exists; explicitly report the missing facet.
        req = LookupRequest.model_validate({k:v for k,v in request.items() if k != "participants"} |
             {"subjects":[p["subject"] for p in request["participants"]]})
        out = self.lookup(req)
        out.pop("results", None)
        out.update(status="unknown" if not out["errors"] else "error", interactions=[],
                   coverage={"exhaustive":False,"searched_participants":request["participants"],"unknown_conditions":True})
        out["warnings"].append("INTERACTIONS_NOT_IMPORTED")
        return out

    def get_evidence(self, request):
        ids = request.get("evidence_ids", [])
        if not 0 < len(ids) <= 32: raise ValueError("Invalid evidence request")
        req = LookupRequest.model_validate({k:v for k,v in request.items() if k not in ("evidence_ids","include_text")} |
                                          {"subjects":[{"kind":"collectible","id":0}]})
        out = self.envelope(req); out.pop("results")
        out["evidence"] = []
        if req.dataset_id != self.dataset_id or req.target.model_dump() != self.manifest["game_target"]:
            out.update(status="error",errors=[{"code":"DATASET_INCOMPATIBLE","message":"Target mismatch","subject":None,"retryable":False}]); return out
        for source_id in ids:
            relative = source_id.removeprefix("eid:")
            if source_id.startswith("eid:") and relative in self.manifest["source_hashes"]:
                value = self.evidence({"source_path":relative,"source_line":1})
                out["evidence"].append({**value,"excerpt":None,"text_state":"metadata_only"})
        out["status"] = "ok" if len(out["evidence"]) == len(ids) else "partial" if out["evidence"] else "unknown"
        return out
