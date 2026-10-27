"""Commit state only from observed engine input, never speculative decisions."""
import time
from .program import Fuel, ProgramError


class TimingRuntime:
    def __init__(self,program):
        self.program=program
        self.scope=None
        self.state=program.initial_state()
        self.last_feedback=None
        self.last_snapshot=None
        self.held_frames=0
        self.last_shoot=(0,0)
        self.known=False
        self.fuel=Fuel()
        self.max_ms=0.
        self.max_fuel=0
        self.transitions=[]
        self.seen_tears=set()

    def begin_frame(self,s,build):
        scope=(s.run_id,s.room_visit_id,s.floor.instance_id if s.floor else None,
               build.revision,build.signature,build.experience_revision,self.program.sha256)
        if scope!=self.scope:
            self.scope=scope
            self.state=self.program.initial_state()
            self.last_feedback=None;self.held_frames=0;self.known=False;self.last_shoot=(0,0)
            self.last_snapshot=None
            self.seen_tears=set()
        if self.last_snapshot==s.snapshot_id:return
        self.max_fuel=max(self.max_fuel,self.fuel.used)
        self.fuel=Fuel()
        started=time.perf_counter()
        feedback=s.weapon_feedback
        if not feedback or feedback.get("schema_version")!=1 or feedback.get("provenance")!="engine_input_callback":
            raise ProgramError("WEAPON_FEEDBACK_UNAVAILABLE")
        rows=feedback.get("history",[])
        if rows=={}:rows=[]
        if not isinstance(rows,list) or len(rows)>32:raise ProgramError("FEEDBACK_SIZE")
        rows=[r for r in rows if isinstance(r,dict) and r.get("room_visit_id")==s.room_visit_id
              and type(r.get("frame")) is int and r["frame"]<s.frame
              and (self.last_feedback is None or r["frame"]>self.last_feedback)]
        rows.sort(key=lambda r:r["frame"])
        tears=s.projectiles.get("player_tears") or []
        if len(tears)>256:raise ProgramError("TEAR_FEEDBACK_SIZE")
        newborn=any(t.get("owner_type")==1 and t.get("entity_frame_count")==0
                    and isinstance(t.get("instance_id"),str) and t["instance_id"] not in self.seen_tears for t in tears)
        self.seen_tears={t["instance_id"] for t in tears if isinstance(t.get("instance_id"),str)}
        # A new component may see pre-installation history. Prime from its most
        # recent neutral update; never invent the age of an ongoing charge.
        if self.last_feedback is None:
            neutral=[i for i,r in enumerate(rows) if self._direction(r)==(0,0)]
            rows=rows[neutral[-1]:] if neutral else rows[-1:]
        for row in rows:
            direction=self._direction(row)
            contiguous=self.last_feedback is None or row["frame"]==self.last_feedback+1
            valid=direction is not None and contiguous
            if direction==(0,0):self.known=True;valid=True
            elif not valid:self.known=False
            holding=valid and direction!=(0,0)
            self.held_frames=min(90,self.held_frames+1) if holding and self.known else 0
            self.last_shoot=direction or (0,0)
            value={"feedback_valid":bool(valid and self.known),"holding":bool(holding),
                   "shoot_x":self.last_shoot[0],"shoot_y":self.last_shoot[1],"delta_frames":1,
                   "new_tear":bool(newborn and row["frame"]==s.frame-1)}
            next_state=self.program.observe(value,self.state,self.fuel)
            if next_state!=self.state:
                self.transitions.append({"frame":row["frame"],"action_id":row.get("action_id"),"before":dict(self.state),"after":dict(next_state)})
                self.transitions=self.transitions[-32:]
            self.state=next_state
            self.last_feedback=row["frame"]
        if self.last_feedback is not None and s.frame-self.last_feedback>2:
            self.known=False
        self.last_snapshot=s.snapshot_id
        self.max_ms=max(self.max_ms,(time.perf_counter()-started)*1000)

    @staticmethod
    def _direction(row):
        keys=("left","right","up","down")
        q,v=row.get("queried") or {},row.get("values") or {}
        if row.get("consistent") is not True or any(q.get(k) is not True or type(v.get(k)) is not bool for k in keys):return None
        direction=(int(v["right"])-int(v["left"]),int(v["down"])-int(v["up"]))
        if sum(v.values())>1 or abs(direction[0])+abs(direction[1])>1:return None
        if direction!=(0,0) and (type(row.get("action_id")) is not int or row["action_id"]<=0):return None
        return direction

    def decide(self,inputs):
        if not self.known:raise ProgramError("CHARGE_STATE_UNCONFIRMED")
        began=time.perf_counter()
        result=self.program.decide(inputs,self.state,self.fuel)
        self.max_ms=max(self.max_ms,(time.perf_counter()-began)*1000)
        self.max_fuel=max(self.max_fuel,self.fuel.used)
        return result

    def report(self):
        return {"program_sha256":self.program.sha256,"state":dict(self.state),"known":self.known,
                "held_frames":self.held_frames,"last_shoot":self.last_shoot,"last_feedback":self.last_feedback,
                "fuel_used":self.fuel.used,"max_fuel":self.max_fuel,"max_call_ms":self.max_ms,
                "transitions":list(self.transitions)}
