"""Agent-owned SQLite state. Call writes at boundaries, outside frame solving."""
import json
from pathlib import Path
import sqlite3
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
                CREATE TABLE IF NOT EXISTS claims(id TEXT PRIMARY KEY,signature TEXT,metric TEXT,body TEXT NOT NULL,active INTEGER);
                CREATE TABLE IF NOT EXISTS counterexamples(id TEXT PRIMARY KEY,claim_id TEXT,body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS learning_tasks(id TEXT PRIMARY KEY,body TEXT NOT NULL);
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
                  "pickup":("pickup_results","intent_id"),"room":("room_results","id"),"task":("learning_tasks","id")}
        table, column = tables[kind]
        encoded = json.dumps(body,ensure_ascii=False,allow_nan=False,sort_keys=True)
        with self.connect() as db:
            db.execute(f"INSERT OR IGNORE INTO {table}({column},body) VALUES(?,?)", (identity,encoded))

    def get(self, kind, identity):
        table, column = {"encounter":("encounters","id"),"decision":("decisions","encounter_id"),
                         "pickup":("pickup_results","intent_id"),"room":("room_results","id")}[kind]
        with self.connect() as db: row = db.execute(f"SELECT body FROM {table} WHERE {column}=?", (identity,)).fetchone()
        return json.loads(row[0]) if row else None

    def measurement(self, signature, metric, body):
        identity = digest([signature,metric,body])
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO measurements VALUES(?,?,?,?)",(identity,signature,metric,json.dumps(body,allow_nan=False)))
        return identity

    def accept_claim(self, signature, metric, value, evidence_refs):
        if not evidence_refs: raise ValueError("Claim requires measurements")
        identity = digest([signature,metric,value,sorted(evidence_refs)])
        body = {"claim_id":identity,"signature":signature,"metric":metric,"value":value,"evidence_refs":evidence_refs,
                "state":"observed_in_scope","support_limits":["exact_calibration_signature_only"]}
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            modes = set()
            for ref in evidence_refs:
                evidence = db.execute("SELECT signature,metric,body FROM measurements WHERE id=?",(ref,)).fetchone()
                if evidence is None or evidence[:2] != (signature,metric): raise ValueError("Missing or mismatched measurement")
                observed = json.loads(evidence[2])
                if observed.get("value") != value: raise ValueError("Claim value differs from measurement")
                modes.add(observed.get("source_mode","unknown"))
            if len(modes) != 1: raise ValueError("Mixed evidence modes")
            body["source_mode"] = modes.pop()
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
                counters.extend({**json.loads(body),"claim_id":claim_id,"source_mode":mode} for (body,) in db.execute("SELECT body FROM counterexamples WHERE claim_id=? ORDER BY rowid DESC LIMIT 8",(claim_id,)))
        return {"experience_revision":revision,"local_claims":[json.loads(body) for _,body,active in rows if active],
                "counterexamples":counters,"hypotheses":[],"support_limits":["exact_signature_match","not_global_item_facts"]}
