"""Crash-safe task budget. Reserve each actual HTTP request before sending."""
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
from campaign.budget import BudgetExceeded


class DevelopmentBudget:
    def __init__(self,path,*,max_requests=3,max_input=60000,max_output=24576):
        if any(type(v) is not int or v<1 for v in (max_requests,max_input,max_output)) or max_requests>3 or max_input>60000 or max_output>24576:
            raise ValueError("INVALID_DEVELOPMENT_BUDGET")
        self.path=Path(path);self.path.parent.mkdir(parents=True,exist_ok=True)
        self.limits={"max_requests":max_requests,"max_input":max_input,"max_output":max_output}
        with self.connect() as db:
            db.executescript("""CREATE TABLE IF NOT EXISTS settings(id INTEGER PRIMARY KEY, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS attempts(id INTEGER PRIMARY KEY, reserved_input INTEGER NOT NULL,
                reserved_output INTEGER NOT NULL, actual_input INTEGER, actual_output INTEGER, settled INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS extensions(id INTEGER PRIMARY KEY CHECK(id=1), authorization TEXT NOT NULL);""")
            db.execute("INSERT OR IGNORE INTO settings VALUES(1,?)",(json.dumps(self.limits,sort_keys=True),))
            if json.loads(db.execute("SELECT body FROM settings WHERE id=1").fetchone()[0])!=self.limits:
                raise ValueError("DEVELOPMENT_BUDGET_LIMITS_CHANGED")

    @contextmanager
    def connect(self):
        db=sqlite3.connect(self.path,timeout=3)
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db;db.commit()
        except BaseException:db.rollback();raise
        finally:db.close()

    def reserve(self,messages,max_output=8192):
        estimate=len(json.dumps(messages,ensure_ascii=False).encode())+32*len(messages)+64
        with self.connect() as db:
            extended=bool(db.execute("SELECT 1 FROM extensions WHERE id=1").fetchone())
            requests,inp,out=db.execute("SELECT count(*),coalesce(sum(coalesce(actual_input,reserved_input)),0),coalesce(sum(coalesce(actual_output,reserved_output)),0) FROM attempts").fetchone()
            if requests>=self.limits["max_requests"]+int(extended):raise BudgetExceeded("DEVELOPMENT_REQUEST_BUDGET_EXHAUSTED")
            if inp+estimate>self.limits["max_input"]:raise BudgetExceeded("DEVELOPMENT_INPUT_BUDGET_EXHAUSTED")
            output=min(max_output,self.limits["max_output"]+4096*int(extended)-out)
            if output<256:raise BudgetExceeded("DEVELOPMENT_OUTPUT_BUDGET_EXHAUSTED")
            cursor=db.execute("INSERT INTO attempts(reserved_input,reserved_output) VALUES(?,?)",(estimate,output))
            return {"attempt_id":cursor.lastrowid,"input":estimate,"output":output}

    def settle(self,ticket,usage):
        exceeded=False
        with self.connect() as db:
            row=db.execute("SELECT settled FROM attempts WHERE id=?",(ticket["attempt_id"],)).fetchone()
            if row is None or row[0]:raise ValueError("REQUEST_ALREADY_SETTLED_OR_UNKNOWN")
            inp,out=usage.get("prompt_tokens"),usage.get("completion_tokens")
            known=type(inp) is int and type(out) is int and inp>=0 and out>=0
            db.execute("UPDATE attempts SET settled=1,actual_input=?,actual_output=? WHERE id=?",(inp if known else None,out if known else None,ticket["attempt_id"]))
            if known:exceeded=inp>ticket["input"] or out>ticket["output"]
        if exceeded:raise BudgetExceeded("DEVELOPMENT_USAGE_EXCEEDED_RESERVATION")

    def report(self):
        with self.connect() as db:
            rows=db.execute("SELECT id,reserved_input,reserved_output,actual_input,actual_output,settled FROM attempts ORDER BY id").fetchall()
            extension=db.execute("SELECT authorization FROM extensions WHERE id=1").fetchone()
        return {**self.limits,"max_requests":self.limits["max_requests"]+bool(extension),"max_output":self.limits["max_output"]+4096*bool(extension),
            "base_limits":self.limits,"extension_authorization":extension[0] if extension else None,
            "requests":len(rows),"usage_known":sum(r[3] is not None for r in rows),
            "unknown_usage_attempts":sum(r[3] is None for r in rows),"input_actual":sum(r[3] or 0 for r in rows),
            "output_actual":sum(r[4] or 0 for r in rows),"cost":None,"attempts":[dict(zip(("id","reserved_input","reserved_output","actual_input","actual_output","settled"),r)) for r in rows]}

    def authorize_one_extra(self,authorization):
        if not isinstance(authorization,str) or not 0<len(authorization)<=500:raise ValueError("MISSING_HUMAN_AUTHORIZATION_REFERENCE")
        with self.connect() as db:db.execute("INSERT OR IGNORE INTO extensions VALUES(1,?)",(authorization,))
