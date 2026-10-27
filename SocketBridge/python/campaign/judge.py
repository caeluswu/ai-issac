"""Outcome proof from raw observations and applied input, independent of Director."""
from combat.observation import SnapshotAssembler
from combat.ledgers import HealthLedger
from .models import RoomKey


def clear_of_threats(s):
    return (s.alive and not s.blocking_combat and not s.hostiles and s.room["is_clear"]
            and not s.bombs and not s.hazards and not s.projectiles.get("enemy_projectiles")
            and not any(x.get("is_enemy",True) for x in s.projectiles.get("lasers") or [])
            and not getattr(s,"environment_residuals",[]))


class FloorJudge:
    def __init__(self):
        self.observation = SnapshotAssembler()
        self.start = None
        self.previous = None
        self.health = HealthLedger()
        self.actions = {}
        self.bosses = {}
        self.completed_boss = None
        self.clear_since = None
        self.armed = None
        self.departure = None
        self.new_floor_samples = 0
        self.actionable = None
        self.failure = None

    def begin(self,s):
        if (not s.floor or not s.floor_progress or s.floor_progress.curses is None
                or not s.room.get("is_starting_room") or s.room["stage"] != 1
                or s.room.get("stage_type") not in (0,1,2) or s.room["difficulty"] != 0
                or s.stats["player_type"] != 0 or not s.alive or s.hostiles):
            raise ValueError("UNSUPPORTED_FLOOR_START")
        if s.floor_progress.curses & 2: raise ValueError("UNSUPPORTED_XL_FLOOR")
        if s.floor.dimension != 0: raise ValueError("UNSUPPORTED_DIMENSION")
        self.start = s
        self.observe(s)

    def on_raw(self,raw):
        if raw.get("type") == "ACTION_RECEIPT" and not raw.get("accepted",False):
            self.actions.pop(raw.get("action_id"),None)
        s = self.observation.consume(raw)
        if s is not None and not s.paused and self.start is not None: self.observe(s)

    def action(self,s,move,shoot,action_id):
        self.actions[action_id] = {"visit":s.room_visit_id,"floor":s.floor.instance_id if s.floor else None,
            "frame":s.frame,"move":tuple(move),"shoot":tuple(shoot),"pos":s.player.pos.model_copy()}
        if len(self.actions)>256: self.actions.pop(next(iter(self.actions)))

    def observe(self,s):
        if s.run_id != self.start.run_id: self.failure = "RUN_CHANGED"; return
        if self.health.observe(s).state == "dead": self.failure = "DEAD"; return
        if self.previous and s.frame <= self.previous.frame: return
        key = RoomKey.from_snapshot(s)
        action = self.actions.get(s.applied_action_id)
        if s.floor.instance_id == self.start.floor.instance_id:
            if self.departure: self.failure = "UNEXPECTED_RETURN_TO_SOURCE_FLOOR"
            if s.room.get("room_type") == 5:
                bosses = [e for e in s.hostiles if e.is_boss is True]
                if s.hostiles or not s.room["is_clear"]: self.completed_boss = None
                if bosses:
                    row = self.bosses.setdefault(key.text(),{"visit":s.room_visit_id,"first_frame":s.frame,
                        "identities":[],"engaged":False,"hp":{}})
                    for enemy in bosses:
                        if [enemy.type,enemy.variant] not in row["identities"]: row["identities"].append([enemy.type,enemy.variant])
                        if enemy.instance_id in row["hp"] and enemy.hp < row["hp"][enemy.instance_id]: row["engaged"] = True
                        row["hp"][enemy.instance_id] = enemy.hp
                    if action and action["visit"] == s.room_visit_id and action["shoot"] != (0,0): row["engaged"] = True
                    self.clear_since = None
                if key.text() in self.bosses and self.bosses[key.text()]["engaged"] and clear_of_threats(s):
                    if self.clear_since is None: self.clear_since = s.frame
                    if s.frame-self.clear_since >= 45:
                        self.completed_boss = {"room_key":key.text(),"visit":s.room_visit_id,
                            "clear_frame":s.frame,**self.bosses[key.text()]}
                else: self.clear_since = None
        elif self.departure is None:
            armed = self.armed
            if not armed or s.frame-armed["frame"] > 300 or not self.previous:
                self.failure = "UNAUTHORIZED_FLOOR_CHANGE"
            elif (self.previous.room_visit_id != armed["visit"] or self.previous.player.pos.distance(armed["pos"])>50
                    or s.floor.stage != 2 or s.floor.stage_type not in (0,1,2) or s.floor.dimension != 0):
                self.failure = "WRONG_FLOOR_EXIT"
            elif not armed["movement_applied"]: self.failure = "EXIT_INPUT_NOT_PROVEN"
            else:
                self.departure = {"source_floor":self.start.floor.instance_id,"destination_floor":s.floor.instance_id,
                    "snapshot_id":s.snapshot_id,"exit_id":armed["exit_id"],"armed_frame":armed["frame"]}
        if self.armed and action and action["visit"] == self.armed["visit"] and action["move"] != (0,0):
            self.armed["movement_applied"] = True
        if self.departure and s.floor.instance_id == self.departure["destination_floor"]:
            ready = (clear_of_threats(s) and s.room.get("is_starting_room") and s.floor_progress
                and s.floor_progress.controls_enabled is True and s.floor_progress.extra_animation_finished is True)
            self.new_floor_samples = self.new_floor_samples+1 if ready else 0
            if ready and self.new_floor_samples >= 3 and action and action["floor"] == s.floor.instance_id and action["visit"] == s.room_visit_id and action["move"] != (0,0) and s.player.pos.distance(action["pos"])>=2:
                self.actionable = {"snapshot_id":s.snapshot_id,"action_id":s.applied_action_id,
                    "displacement":s.player.pos.distance(action["pos"]),"consecutive_ready_samples":self.new_floor_samples}
        self.previous = s

    def arm_exit(self,exit_id):
        s = self.observation.latest or self.previous
        if (not s or not self.completed_boss or self.completed_boss["room_key"] != RoomKey.from_snapshot(s).text()
                or not clear_of_threats(s) or s.floor.instance_id != self.start.floor.instance_id):
            raise ValueError("BOSS_EXIT_NOT_READY")
        found = next((e for e in s.exit_observations or [] if e.instance_id == exit_id),None)
        if found is None or found.type != 17 or found.variant != 0 or s.room["room_type"] != 5:
            raise ValueError("EXIT_NOT_NORMAL_MAINLINE")
        self.armed = {"exit_id":exit_id,"visit":s.room_visit_id,"pos":found.pos,
            "frame":s.frame,"movement_applied":False}
        return found

    @property
    def success(self): return bool(self.completed_boss and self.departure and self.actionable and not self.failure)

    def evidence(self):
        armed = {k:v for k,v in (self.armed or {}).items() if k != "pos"}
        return {"boss":self.completed_boss,"exit_authorization":armed,"departure":self.departure,
                "actionable":self.actionable,"failure":self.failure}
