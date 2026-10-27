"""Damage evidence capture and offline reconstruction. Never changes health scoring."""
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from .builds import digest
from .ledgers import HealthLedger
from .models import WorldSnapshot


def encoded(value):
    return json.dumps(value,ensure_ascii=False,allow_nan=False,sort_keys=True,separators=(",",":"))


@dataclass
class DamageEpisode:
    episode_id: str
    run_id: str
    room_visit_id: str
    start_frame: int
    end_frame: int
    health_before: dict
    health_after: dict
    loss: dict
    trace_sequence: int
    records: list
    closure: str = "pending"
    schema_version: int = 1


def compact_snapshot(s):
    # Full snapshots remain in trace.jsonl. No geometry copy on combat updates.
    groups={"enemies":[e.model_dump() for e in s.enemies[:64]],
            "projectiles":{k:(v or [])[:128] for k,v in s.projectiles.items()},
            "bombs":s.bombs[:32],"hazards":s.hazards[:32]}
    return {"snapshot_id":s.snapshot_id,"run_id":s.run_id,"room_visit_id":s.room_visit_id,"frame":s.frame,
        "player":s.player.model_dump(),"health":dict(s.health),"stats":dict(s.stats),"mode":s.mode,
        "weapons":s.weapons.model_dump() if s.weapons else None,
        "effects":s.effects.model_dump() if s.effects else None,
        "floor_instance_id":s.floor.instance_id if s.floor else None,
        "inventory":deepcopy(s.inventory),"applied_action_id":s.applied_action_id,
        "weapon_feedback":deepcopy(s.weapon_feedback),
        "room":dict(s.room),"entities":deepcopy(groups),
        "entity_capture_truncated":len(s.enemies)>64 or any(len(v or [])>128 for v in s.projectiles.values()) or len(s.bombs)>32 or len(s.hazards)>32}


class DamageCapture:
    """Bounded frame window; disk/analysis jobs run off the control thread."""
    def __init__(self,directory,*,authoritative=True,background=True,store_path=None,external_snapshots=False,trace_path=None):
        self.directory=Path(directory);self.authoritative=authoritative;self.store_path=store_path
        self.history=deque(maxlen=2048);self.pending=[];self.index=[];self.errors=[]
        self.latest=None;self.previous_health=None;self.ledger=HealthLedger();self.plan=None
        self.external_snapshots=external_snapshots
        self.raw_extras={}
        self.trace_path=Path(trace_path) if trace_path else self.directory/"trace.jsonl"
        self.executor=ThreadPoolExecutor(max_workers=1,thread_name_prefix="damage-evidence") if background else None
        self.jobs=[];self.closed=False

    def _append(self,row):
        self.history.append(row)
        for episode in self.pending:
            if row.get("run_id")==episode.run_id and row.get("room_visit_id")==episode.room_visit_id and row["frame"]<=episode.end_frame+30:
                episode.records.append(row)

    def snapshot(self,s,sequence):
        if self.closed or s.paused:return
        if self.latest and (s.run_id,s.room_visit_id)==(self.latest["run_id"],self.latest["room_visit_id"]) and s.frame<=self.latest["frame"]:return
        for episode in list(self.pending):
            if (s.run_id,s.room_visit_id)!=(episode.run_id,episode.room_visit_id):self._complete(episode,"context_changed")
            elif s.frame>episode.end_frame+30:self._complete(episode,"post_window_collected")
        row={"kind":"snapshot","sequence":sequence,**compact_snapshot(s),**self.raw_extras}
        self._append(row)
        while self.history and (self.history[0].get("run_id")!=s.run_id or self.history[0]["frame"]<s.frame-90):self.history.popleft()
        old=self.latest;self.latest=row
        if not self.authoritative:
            delta=self.ledger.observe(s)
            if delta.damage or delta.bones_lost:self.loss(s,asdict(delta),sequence,old)

    def loss(self,s,delta,sequence,before=None):
        if not (delta.get("damage") or delta.get("bones_lost")):return
        before=before or self.previous_health or self.latest
        start=before["frame"] if before and before["run_id"]==s.run_id else s.frame
        identity=digest([s.run_id,s.room_visit_id,start,s.frame,delta,s.health])
        if any(e.episode_id==identity for e in self.pending) or any(e["episode_id"]==identity for e in self.index):return
        records=[r for r in self.history if r.get("run_id")==s.run_id and
                 (r.get("room_visit_id")==s.room_visit_id or r.get("kind")=="snapshot" and start<=r["frame"]<=s.frame) and r["frame"]>=s.frame-90]
        self.pending.append(DamageEpisode(identity,s.run_id,s.room_visit_id,start,s.frame,
            dict(before["health"]) if before else {},dict(s.health),dict(delta),sequence,list(records)))

    def health(self,s,delta,sequence):
        self.loss(s,delta,sequence)
        self.previous_health={"frame":s.frame,"health":dict(s.health),"run_id":s.run_id}

    def consume(self,row):
        if self.closed:return
        kind=row.get("kind");sequence=row.get("sequence",0)
        if kind=="damage_health_delta":
            if self.latest:
                # This event uses the same confirmed ledger as the task judge.
                s=SimpleNamespace(**row["health_point"])
                self.health(s,row["delta"],sequence)
            return
        if kind=="plan_installed":self.plan=deepcopy(row.get("plan"));return
        if kind=="game_message":
            message=row.get("message") or {};raw=(message.get("payload") or {}).get("COMBAT_FRAME")
            if raw is not None:
                self.raw_extras={"recent_deaths":deepcopy(raw.get("recent_deaths"))}
                if not self.external_snapshots:self.snapshot(WorldSnapshot.model_validate(raw),sequence)
                return
            if message.get("event")=="PLAYER_DAMAGE_ATTEMPT":
                data=message.get("data") or {};frame=data.get("callback_frame",message.get("frame"))
                self.ledger.damage_attempt(frame)
                if self.latest:
                    self._append({"kind":"damage_attempt","sequence":sequence,"frame":frame,
                        "run_id":data.get("run_id",self.latest["run_id"]),
                        "room_visit_id":data.get("room_visit_id",self.latest["room_visit_id"]),"data":deepcopy(data)})
                return
            if message.get("type")=="ACTION_RECEIPT" and self.latest:
                self._append({"kind":"receipt","sequence":sequence,"run_id":self.latest["run_id"],
                    "room_visit_id":message.get("room_visit_id"),"frame":message["frame"],"data":deepcopy(message)})
            return
        if not self.latest:return
        context={"run_id":self.latest["run_id"],"room_visit_id":row.get("room_visit_id",self.latest["room_visit_id"])}
        if kind=="action_sent":
            self._append({"kind":"action","sequence":sequence,**context,"frame":row["frame"],"data":deepcopy(row),"plan":self.plan})
        elif kind in ("candidate_window","debug_trigger"):
            if self.external_snapshots:return
            rows=row.get("preceding_candidates",[]) if kind=="debug_trigger" else [row]
            for entry in rows:
                if entry["frame"]>=self.latest["frame"]-90:
                    self._append({"kind":"candidates","sequence":sequence,**context,"frame":entry["frame"],"candidates":deepcopy(entry["candidates"])})
        elif kind in ("component_fallback","stale_decision_discarded","fault"):
            self._append({"kind":kind,"sequence":sequence,**context,"frame":row.get("frame",self.latest["frame"]),"data":deepcopy(row)})

    def candidates(self,frame,candidates,sequence):
        if self.latest:self._append({"kind":"candidates","sequence":sequence,"run_id":self.latest["run_id"],
            "room_visit_id":self.latest["room_visit_id"],"frame":frame,"candidates":deepcopy(candidates)})

    def _complete(self,episode,reason):
        self.pending.remove(episode);episode.closure=reason
        value=asdict(episode);value["plan"]=self.plan
        if self.executor:self.jobs.append(self.executor.submit(self._write,value))
        else:self._write(value)

    def _write(self,value):
        from .damage_analysis import analyze_episode
        directory=self.directory/"damage";directory.mkdir(parents=True,exist_ok=True)
        value["trace_ref"]=str(self.trace_path.resolve())
        source_manifest=self.trace_path.parent/"manifest.json"
        if source_manifest.exists():value["source_manifest"]=json.loads(source_manifest.read_text(encoding="utf-8"))
        report=self.trace_path.parent/"report.json"
        if report.exists():
            outcome=json.loads(report.read_text(encoding="utf-8"))
            value["run_outcome"]={k:outcome.get(k) for k in ("result","success","damage_half_hearts","combat_ticks","wall_seconds")}
        raw=encoded(value)+"\n";window_sha=hashlib.sha256(raw.encode()).hexdigest()
        path=directory/(value["episode_id"]+"."+window_sha[:16]+".json");path.write_bytes(raw.encode("utf-8"))
        analysis=analyze_episode(value)
        path.with_suffix(".analysis.json").write_text(encoded(analysis)+"\n",encoding="utf-8")
        index={k:value[k] for k in ("episode_id","run_id","room_visit_id","start_frame","end_frame","loss","closure")}
        index.update(artifact=str(path),sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        self.index.append(index)
        if self.store_path:
            from .agent_store import AgentStore
            store=AgentStore(self.store_path);store.record("damage_episode",digest([value["episode_id"],index["sha256"]]),index)
            store.record("damage_analysis",analysis["analysis_id"],analysis)

    def finish(self,reason="run_finished"):
        if self.closed:return
        for episode in list(self.pending):self._complete(episode,reason)
        self.closed=True
        if self.executor:self.executor.shutdown(wait=True)
        for job in self.jobs:
            try:job.result()
            except Exception as exc:self.errors.append(type(exc).__name__+": "+str(exc))
        if self.index or self.errors:
            try:(self.directory/"damage-index.json").write_text(encoded({"schema_version":1,"episodes":self.index,"errors":self.errors})+"\n",encoding="utf-8")
            except OSError as exc:self.errors.append(type(exc).__name__+": "+str(exc))


def extract_trace(trace,destination,*,store_path=None):
    trace=Path(trace);destination=Path(destination);destination.mkdir(parents=True,exist_ok=True)
    with trace.open(encoding="utf-8") as stream:
        first=json.loads(next(stream))
    capture=DamageCapture(destination,authoritative=first.get("kind")=="damage_capture_started",background=False,store_path=store_path,trace_path=trace)
    with trace.open(encoding="utf-8") as stream:
        for line in stream:capture.consume(json.loads(line))
    capture.finish("trace_ended")
    manifest={"trace":str(trace.resolve()),"trace_sha256":hashlib.sha256(trace.read_bytes()).hexdigest(),
              "ledger_mode":"recorded_task_delta" if capture.authoritative else "legacy_health_reconstruction",
              "episodes":capture.index,"errors":capture.errors}
    (destination/"extraction.json").write_text(encoded(manifest)+"\n",encoding="utf-8")
    return manifest
