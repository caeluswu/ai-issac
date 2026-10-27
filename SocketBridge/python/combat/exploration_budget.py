"""One persistent M2-C ledger: 24 total requests, 3 for attribution first round."""
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
from campaign.budget import BudgetExceeded


class ExplorationBudget:
    LIMITS={"requests":24,"input":240000,"output":98304,"damage_review_requests":3,"damage_review_output":12288}
    def __init__(self,path,*,phase="damage_review"):
        if phase!="damage_review":raise ValueError("UNIMPLEMENTED_EXPLORATION_PHASE")
        self.path=Path(path);self.path.parent.mkdir(parents=True,exist_ok=True);self.phase=phase
        with self.connect() as db:
            db.executescript("""CREATE TABLE IF NOT EXISTS m2c_settings(id INTEGER PRIMARY KEY,body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS m2c_attempts(id INTEGER PRIMARY KEY,phase TEXT NOT NULL,
                  reserved_input INTEGER NOT NULL,reserved_output INTEGER NOT NULL,
                  actual_input INTEGER,actual_output INTEGER,settled INTEGER NOT NULL DEFAULT 0);""")
            db.execute("INSERT OR IGNORE INTO m2c_settings VALUES(1,?)",(json.dumps(self.LIMITS,sort_keys=True),))
            if json.loads(db.execute("SELECT body FROM m2c_settings WHERE id=1").fetchone()[0])!=self.LIMITS:
                raise ValueError("M2C_BUDGET_SETTINGS_CHANGED")

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
            rows=db.execute("SELECT phase,coalesce(actual_input,reserved_input),coalesce(actual_output,reserved_output) FROM m2c_attempts").fetchall()
            local=[r for r in rows if r[0]==self.phase]
            if len(rows)>=24 or len(local)>=3:raise BudgetExceeded("M2C_REQUEST_BUDGET_EXHAUSTED")
            if sum(r[1] for r in rows)+estimate>240000:raise BudgetExceeded("M2C_INPUT_BUDGET_EXHAUSTED")
            output=min(max_output,98304-sum(r[2] for r in rows),12288-sum(r[2] for r in local))
            if output<256:raise BudgetExceeded("M2C_OUTPUT_BUDGET_EXHAUSTED")
            cursor=db.execute("INSERT INTO m2c_attempts(phase,reserved_input,reserved_output) VALUES(?,?,?)",(self.phase,estimate,output))
            return {"attempt_id":cursor.lastrowid,"input":estimate,"output":output}

    def settle(self,ticket,usage):
        with self.connect() as db:
            row=db.execute("SELECT settled FROM m2c_attempts WHERE id=?",(ticket["attempt_id"],)).fetchone()
            if row is None or row[0]:raise ValueError("INVALID_OR_SETTLED_M2C_ATTEMPT")
            inp,out=usage.get("prompt_tokens"),usage.get("completion_tokens")
            known=type(inp) is int and type(out) is int and inp>=0 and out>=0
            db.execute("UPDATE m2c_attempts SET actual_input=?,actual_output=?,settled=1 WHERE id=?",(inp if known else None,out if known else None,ticket["attempt_id"]))

    def report(self):
        with self.connect() as db:
            rows=db.execute("SELECT id,phase,reserved_input,reserved_output,actual_input,actual_output,settled FROM m2c_attempts ORDER BY id").fetchall()
        return {"limits":dict(self.LIMITS),"requests":len(rows),"phase":self.phase,
            "phase_requests":sum(r[1]==self.phase for r in rows),"input_actual":sum(r[4] or 0 for r in rows),
            "output_actual":sum(r[5] or 0 for r in rows),"unknown_usage_attempts":sum(r[4] is None for r in rows),"cost":None,
            "attempts":[dict(zip(("id","phase","reserved_input","reserved_output","actual_input","actual_output","settled"),r)) for r in rows]}
