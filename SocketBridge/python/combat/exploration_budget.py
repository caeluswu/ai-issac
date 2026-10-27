"""One persistent M2-C ledger: 24 total requests, 3 for attribution first round."""
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
from campaign.budget import BudgetExceeded


class ExplorationBudget:
    LIMITS={"requests":24,"input":240000,"output":98304,"damage_review_requests":3,"damage_review_output":12288}
    def __init__(self,path,*,phase="damage_review",gap="default"):
        if phase not in ("damage_review","item_adaptation"):raise ValueError("UNIMPLEMENTED_EXPLORATION_PHASE")
        if not isinstance(gap,str) or not 1<=len(gap)<=80:raise ValueError("INVALID_CAPABILITY_GAP")
        self.path=Path(path);self.path.parent.mkdir(parents=True,exist_ok=True);self.phase=phase
        self.gap=gap
        with self.connect() as db:
            db.executescript("""CREATE TABLE IF NOT EXISTS m2c_settings(id INTEGER PRIMARY KEY,body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS m2c_attempts(id INTEGER PRIMARY KEY,phase TEXT NOT NULL,
                  reserved_input INTEGER NOT NULL,reserved_output INTEGER NOT NULL,
                  actual_input INTEGER,actual_output INTEGER,settled INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS m2c_grants(id INTEGER PRIMARY KEY,phase TEXT NOT NULL,gap TEXT NOT NULL,
                  extra_requests INTEGER NOT NULL,authorization TEXT NOT NULL UNIQUE);""")
            db.execute("INSERT OR IGNORE INTO m2c_settings VALUES(1,?)",(json.dumps(self.LIMITS,sort_keys=True),))
            if json.loads(db.execute("SELECT body FROM m2c_settings WHERE id=1").fetchone()[0])!=self.LIMITS:
                raise ValueError("M2C_BUDGET_SETTINGS_CHANGED")
            if "gap" not in {r[1] for r in db.execute("PRAGMA table_info(m2c_attempts)")}:
                db.execute("ALTER TABLE m2c_attempts ADD COLUMN gap TEXT NOT NULL DEFAULT 'default'")

    @contextmanager
    def connect(self):
        db=sqlite3.connect(self.path,timeout=3)
        try:
            db.execute("BEGIN IMMEDIATE");yield db;db.commit()
        except BaseException:db.rollback();raise
        finally:db.close()

    def reserve(self,messages,max_output=4096):
        estimate=len(json.dumps(messages,ensure_ascii=False).encode())+32*len(messages)+64
        with self.connect() as db:
            rows=db.execute("SELECT phase,coalesce(actual_input,reserved_input),coalesce(actual_output,reserved_output),gap FROM m2c_attempts").fetchall()
            local=[r for r in rows if r[0]==self.phase]
            phase_limit=3 if self.phase=="damage_review" else 6
            grants=db.execute("SELECT coalesce(sum(extra_requests),0) FROM m2c_grants WHERE phase=? AND gap=?",(self.phase,self.gap)).fetchone()[0]
            if len(rows)>=24 or len(local)>=phase_limit or self.phase=="item_adaptation" and sum(r[3]==self.gap for r in local)>=3+grants:
                raise BudgetExceeded("M2C_REQUEST_BUDGET_EXHAUSTED")
            if sum(r[1] for r in rows)+estimate>240000:raise BudgetExceeded("M2C_INPUT_BUDGET_EXHAUSTED")
            output=min(max_output,98304-sum(r[2] for r in rows),(12288 if self.phase=="damage_review" else 24576)-sum(r[2] for r in local))
            if output<256:raise BudgetExceeded("M2C_OUTPUT_BUDGET_EXHAUSTED")
            cursor=db.execute("INSERT INTO m2c_attempts(phase,reserved_input,reserved_output,gap) VALUES(?,?,?,?)",(self.phase,estimate,output,self.gap))
            return {"attempt_id":cursor.lastrowid,"input":estimate,"output":output}

    def grant_gap(self,authorization):
        """Record an explicit one-request user grant; existing attempts stay put."""
        if self.phase!="item_adaptation" or self.gap!="moms_knife" or not isinstance(authorization,str) or len(authorization)<12:
            raise ValueError("M2C_GRANT_REQUIRES_RECORDED_AUTHORIZATION")
        with self.connect() as db:
            if db.execute("SELECT 1 FROM m2c_grants WHERE phase=? AND gap=?",(self.phase,self.gap)).fetchone():
                raise ValueError("M2C_GRANT_ALREADY_USED")
            db.execute("INSERT INTO m2c_grants(phase,gap,extra_requests,authorization) VALUES(?,?,1,?)",(self.phase,self.gap,authorization))

    def settle(self,ticket,usage):
        with self.connect() as db:
            row=db.execute("SELECT settled FROM m2c_attempts WHERE id=?",(ticket["attempt_id"],)).fetchone()
            if row is None or row[0]:raise ValueError("INVALID_OR_SETTLED_M2C_ATTEMPT")
            inp,out=usage.get("prompt_tokens"),usage.get("completion_tokens")
            known=type(inp) is int and type(out) is int and inp>=0 and out>=0
            db.execute("UPDATE m2c_attempts SET actual_input=?,actual_output=?,settled=1 WHERE id=?",(inp if known else None,out if known else None,ticket["attempt_id"]))

    def report(self):
        with self.connect() as db:
            rows=db.execute("SELECT id,phase,reserved_input,reserved_output,actual_input,actual_output,settled,gap FROM m2c_attempts ORDER BY id").fetchall()
            grants=db.execute("SELECT coalesce(sum(extra_requests),0) FROM m2c_grants WHERE phase=? AND gap=?",(self.phase,self.gap)).fetchone()[0]
        return {"limits":{**self.LIMITS,"item_adaptation_requests":6,"requests_per_gap":3},"requests":len(rows),"phase":self.phase,
            "phase_requests":sum(r[1]==self.phase for r in rows),
            "gap_requests":sum(r[1]==self.phase and r[7]==self.gap for r in rows),"gap_request_limit":3+grants if self.phase=="item_adaptation" else 3,
            "input_actual":sum(r[4] or 0 for r in rows),
            "output_actual":sum(r[5] or 0 for r in rows),"unknown_usage_attempts":sum(r[4] is None for r in rows),"cost":None,
            "attempts":[dict(zip(("id","phase","reserved_input","reserved_output","actual_input","actual_output","settled","gap"),r)) for r in rows]}
