"""Bounded batch RPC to a persistent JS worker; source never runs in Python."""
from concurrent.futures import Future,TimeoutError as FutureTimeout
import json
import math
import os
from pathlib import Path
from queue import Queue
import subprocess
import sys
from threading import Thread,Lock
import time
from combat.builds import digest


class JSWorker:
    def __init__(self,source):
        self.identity=digest(["attack-model-js-v1",source]);self.closed=False;self.lock=Lock();self.number=0
        env={k:v for k,v in os.environ.items() if k.upper() in ("SYSTEMROOT","WINDIR","TEMP","TMP","PATH")}
        self.process=subprocess.Popen([sys.executable,"-u","-m","mechanisms.js_worker"],cwd=Path(__file__).resolve().parents[1],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding="utf-8",env=env,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name=="nt" else 0)
        self.queue=Queue(maxsize=1);self.thread=Thread(target=self._io,daemon=True);self.thread.start()
        try:self.call({"type":"load","source":source},timeout=5)
        except BaseException:self.close();raise

    def _io(self):
        while True:
            item=self.queue.get()
            if item is None:return
            message,future=item
            try:
                self.process.stdin.write(json.dumps(message,ensure_ascii=False,allow_nan=False,separators=(",",":"))+"\n");self.process.stdin.flush()
                line=self.process.stdout.readline(65537)
                if not line or len(line.encode())>65536:raise RuntimeError("JS_WORKER_EXIT_OR_OUTPUT_SIZE")
                result=json.loads(line)
                if result.get("id")!=message["id"]:raise RuntimeError("JS_RPC_ID_MISMATCH")
                if not result.get("ok"):raise RuntimeError(result.get("error","JS_FAILURE"))
                future.set_result(result["value"])
            except Exception as exc:
                if not future.done():future.set_exception(exc)

    def call(self,message,timeout=.02):
        with self.lock:
            if self.closed:raise RuntimeError("JS_WORKER_CLOSED")
            self.number+=1;message={**message,"id":self.number};future=Future()
            if len(json.dumps(message,ensure_ascii=False,allow_nan=False).encode())>262144:raise ValueError("JS_INPUT_SIZE")
            self.queue.put_nowait((message,future))
            try:return future.result(timeout=timeout)
            except FutureTimeout:
                self.close();raise TimeoutError("JS_BATCH_DEADLINE")

    def close(self):
        if self.closed:return
        self.closed=True
        if self.process.poll() is None:
            self.process.terminate()
            try:self.process.wait(timeout=.5)
            except subprocess.TimeoutExpired:self.process.kill();self.process.wait(timeout=1)
        try:self.queue.put_nowait(None)
        except Exception:pass
        for stream in (self.process.stdin,self.process.stdout,self.process.stderr):
            try:stream.close()
            except OSError:pass


class AttackModelRuntime:
    def __init__(self,source):
        self.worker=JSWorker(source);self.identity=self.worker.identity;self.state=None;self.scope=None
        self.last_snapshot=None;self.result={};self.timings=[];self.calls=0;self.failure=None

    def evaluate(self,context,candidates,feedback=None):
        key=(context["run_id"],context["room_visit_id"],context["build_signature"])
        if self.last_snapshot==context["snapshot_id"]:return self.result
        if self.failure:return {}
        began=time.perf_counter()
        try:
            value=self.worker.call({"type":"tick","payload":{"context":context,"candidates":candidates,
                "feedback":feedback or {},"reset":key!=self.scope,"state":self.state}})
            rows=value["proposals"]
            if not isinstance(rows,list) or len(rows)>len(candidates)*4:raise ValueError("JS_PROPOSAL_COUNT")
            ids={c["id"] for c in candidates};enemies={e["id"] for e in context["enemies"]};result={}
            for row in rows:
                if not isinstance(row,dict):raise ValueError("JS_CANDIDATE_ID")
                candidate_id=row.get("candidate_id")
                # Numeric JSON IDs often round-trip through String(id) in
                # generated JS. Canonical decimal conversion preserves the
                # offered candidate identity without changing attack policy.
                if isinstance(candidate_id,str) and candidate_id.isascii() and candidate_id.isdecimal() and str(int(candidate_id))==candidate_id:
                    candidate_id=int(candidate_id)
                if type(candidate_id) is not int or candidate_id not in ids:raise ValueError("JS_CANDIDATE_ID")
                direction=row.get("shoot")
                if direction not in ([0,0],[1,0],[-1,0],[0,1],[0,-1]):raise ValueError("JS_SHOOT_DIRECTION")
                if row.get("mode") not in ("hold","release","idle"):raise ValueError("JS_KNIFE_MODE")
                if (row["mode"]=="hold")!=(direction!=[0,0]):raise ValueError("JS_MODE_INPUT_MISMATCH")
                bias=row.get("position_score",0.)
                if type(bias) not in (int,float) or not math.isfinite(bias) or not -10<=bias<=10:raise ValueError("JS_POSITION_SCORE")
                for field,high in (("hit_score",1),("expected_damage",1000000)):
                    if type(row.get(field)) not in (int,float) or not math.isfinite(row[field]) or not 0<=row[field]<=high:raise ValueError("JS_PROPOSAL_NUMERIC")
                targets=row.get("target_ids")
                if not isinstance(targets,list) or len(targets)>64 or not set(targets).issubset(enemies) or len(set(targets))!=len(targets):raise ValueError("JS_TARGET_IDS")
                pair=(candidate_id,tuple(direction))
                if pair in result:raise ValueError("JS_DUPLICATE_PROPOSAL")
                result[pair]=row
            self.state=value["state"];self.scope=key;self.last_snapshot=context["snapshot_id"];self.result=result;self.calls+=1
            return result
        except Exception as exc:
            self.failure=type(exc).__name__+": "+str(exc);self.worker.close();return {}
        finally:
            self.timings.append((time.perf_counter()-began)*1000);self.timings=self.timings[-2048:]

    def close(self):self.worker.close()
