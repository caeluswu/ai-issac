"""Agent-owned SQLite state. Call writes at boundaries, outside frame solving."""
import json
from pathlib import Path
import sqlite3
import math
import statistics
from contextlib import contextmanager
from .builds import digest


class AgentStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT);
                CREATE TABLE IF NOT EXISTS encounters(id TEXT PRIMARY KEY,body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS decisions(encounter_id TEXT PRIMARY KEY,body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS pickup_results(intent_id TEXT PRIMARY KEY,body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS room_results(id TEXT PRIMARY KEY,body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS measurements(id TEXT PRIMARY KEY,signature TEXT,metric TEXT,body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS evidence_samples(signature TEXT,mode TEXT,sample_id TEXT,measurement_id TEXT,PRIMARY KEY(signature,mode,sample_id));
                CREATE TABLE IF NOT EXISTS claims(id TEXT PRIMARY KEY,signature TEXT,metric TEXT,body TEXT NOT NULL,active INTEGER);
                CREATE TABLE IF NOT EXISTS counterexamples(id TEXT PRIMARY KEY,claim_id TEXT,body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS learning_tasks(id TEXT PRIMARY KEY,body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS damage_episodes(id TEXT PRIMARY KEY,body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS damage_analyses(id TEXT PRIMARY KEY,body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS damage_summaries(id TEXT PRIMARY KEY,body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS damage_reviews(id TEXT PRIMARY KEY,body TEXT NOT NULL);
                INSERT OR IGNORE INTO metadata VALUES('schema_version','1');
                INSERT OR IGNORE INTO metadata VALUES('experience_revision','0');
            """)
            if db.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()[0] != "1":
                raise ValueError("AGENT_STORE_SCHEMA_MISMATCH")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=3)
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally: db.close()

    def record(self, kind, identity, body):
        tables = {"encounter":("encounters","id"),"decision":("decisions","encounter_id"),
                    "pickup":("pickup_results","intent_id"),"room":("room_results","id"),"task":("learning_tasks","id"),
                    "damage_episode":("damage_episodes","id"),"damage_analysis":("damage_analyses","id"),
                    "damage_summary":("damage_summaries","id"),"damage_review":("damage_reviews","id")}
        table, column = tables[kind]
        encoded = json.dumps(body,ensure_ascii=False,allow_nan=False,sort_keys=True)
        with self.connect() as db:
            db.execute(f"INSERT OR IGNORE INTO {table}({column},body) VALUES(?,?)", (identity,encoded))

    def get(self, kind, identity):
        table, column = {"encounter":("encounters","id"),"decision":("decisions","encounter_id"),
                         "pickup":("pickup_results","intent_id"),"room":("room_results","id"),"task":("learning_tasks","id"),
                         "damage_episode":("damage_episodes","id"),"damage_analysis":("damage_analyses","id"),
                         "damage_summary":("damage_summaries","id"),"damage_review":("damage_reviews","id")}[kind]
        with self.connect() as db: row = db.execute(f"SELECT body FROM {table} WHERE {column}=?", (identity,)).fetchone()
        return json.loads(row[0]) if row else None

    def measurement(self, signature, metric, body):
        identity = digest([signature,metric,body])
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO measurements VALUES(?,?,?,?)",(identity,signature,metric,json.dumps(body,allow_nan=False)))
        return identity

    def resolve_task(self,identity,state,artifact):
        if state not in ("validated_offline","supported","rejected","requires_host_primitive","quarantined"):raise ValueError("INVALID_TASK_STATE")
        with self.connect() as db:
            row=db.execute("SELECT body FROM learning_tasks WHERE id=?",(identity,)).fetchone()
            if not row:raise ValueError("UNKNOWN_TASK")
            old=json.loads(row[0])
            if old.get("state")=="supported" and state not in ("supported","quarantined"):raise ValueError("TASK_STATE_DOWNGRADE")
            body={**old,"state":state,"artifact":str(artifact)}
            db.execute("UPDATE learning_tasks SET body=? WHERE id=?",(json.dumps(body,ensure_ascii=False,allow_nan=False),identity))

    def passive_measurement(self, signature, samples, mode):
        """Atomically consume new physical samples; a cache is never evidence."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            new = {}
            for sample in samples:
                key, value = sample["sample_id"], sample["value"]
                if not isinstance(key,str) or not key or type(value) not in (int,float) or not math.isfinite(value) or not .1 <= value <= 1000:
                    raise ValueError("INVALID_PASSIVE_SAMPLE")
                if not db.execute("SELECT 1 FROM evidence_samples WHERE signature=? AND mode=? AND sample_id=?",(signature,mode,key)).fetchone():
                    new[key] = value
            if len(new) < 3: return None
            body = {"value":statistics.median(new.values()),"unit":"game_pixel/game_update",
                    "sample_count":len(new),"sample_ids":sorted(new),"source_mode":mode,"extractor_version":2}
            identity = digest([signature,"launch_speed",body])
            db.execute("INSERT OR IGNORE INTO measurements VALUES(?,?,?,?)",(identity,signature,"launch_speed",json.dumps(body,allow_nan=False)))
            db.executemany("INSERT INTO evidence_samples VALUES(?,?,?,?)",[(signature,mode,key,identity) for key in new])
            return identity, body["value"]

    def accept_claim(self, signature, metric, value, evidence_refs):
        if not evidence_refs: raise ValueError("Claim requires measurements")
        identity = digest([signature,metric,value,sorted(evidence_refs)])
        body = {"claim_id":identity,"signature":signature,"metric":metric,"value":value,"evidence_refs":evidence_refs,
                "state":"observed_in_scope","support_limits":["exact_calibration_signature_only"]}
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            modes = set()
            verified = True
            for ref in evidence_refs:
                evidence = db.execute("SELECT signature,metric,body FROM measurements WHERE id=?",(ref,)).fetchone()
                if evidence is None or evidence[:2] != (signature,metric): raise ValueError("Missing or mismatched measurement")
                observed = json.loads(evidence[2])
                if observed.get("value") != value: raise ValueError("Claim value differs from measurement")
                modes.add(observed.get("source_mode","unknown"))
                ids=observed.get("sample_ids",[])
                verified = verified and observed.get("extractor_version")==2 and len(ids)>=3 and len(set(ids))==len(ids)
            if len(modes) != 1: raise ValueError("Mixed evidence modes")
            body["source_mode"] = modes.pop()
            body["sample_identity_verified"] = verified
            rows = db.execute("SELECT id,body FROM claims WHERE signature=? AND metric=? AND active=1",(signature,metric)).fetchall()
            for old_id, encoded in rows:
                old = json.loads(encoded)
                if old.get("source_mode") == body["source_mode"] and old["value"] == value and old.get("sample_identity_verified",False) == verified:
                    old["evidence_refs"] = sorted(set(old["evidence_refs"]+evidence_refs))
                    db.execute("UPDATE claims SET body=? WHERE id=?",(json.dumps(old,allow_nan=False),old_id))
                    return old_id
            inserted = db.execute("INSERT OR IGNORE INTO claims VALUES(?,?,?,?,1)",(identity,signature,metric,json.dumps(body,allow_nan=False))).rowcount
            if inserted: db.execute("UPDATE metadata SET value=CAST(value AS INTEGER)+1 WHERE key='experience_revision'")
        return identity

    def contradict(self, claim_id, evidence):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM claims WHERE id=?",(claim_id,)).fetchone() is None: raise ValueError("Unknown claim")
            identity = digest([claim_id,evidence])
            added = db.execute("INSERT OR IGNORE INTO counterexamples VALUES(?,?,?)",(identity,claim_id,json.dumps(evidence,allow_nan=False))).rowcount
            if added:
                db.execute("UPDATE claims SET active=0 WHERE id=?",(claim_id,))
                db.execute("UPDATE metadata SET value=CAST(value AS INTEGER)+1 WHERE key='experience_revision'")

    def retrieve(self, signature):
        with self.connect() as db:
            revision = int(db.execute("SELECT value FROM metadata WHERE key='experience_revision'").fetchone()[0])
            rows = db.execute("SELECT id,body,active FROM claims WHERE signature=? ORDER BY rowid DESC LIMIT 32",(signature,)).fetchall()
            counters = []
            for claim_id, claim_body, _ in rows:
                mode = json.loads(claim_body).get("source_mode","unknown")
                counters.extend({**json.loads(body),"claim_id":claim_id,"source_mode":mode,"sample_identity_verified":json.loads(claim_body).get("sample_identity_verified",False)} for (body,) in db.execute("SELECT body FROM counterexamples WHERE claim_id=? ORDER BY rowid DESC LIMIT 8",(claim_id,)))
        return {"experience_revision":revision,"local_claims":[json.loads(body) for _,body,active in rows if active],
                "counterexamples":counters,"hypotheses":[],"support_limits":["exact_signature_match","not_global_item_facts"]}
