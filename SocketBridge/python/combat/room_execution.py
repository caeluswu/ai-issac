"""Reusable room task; the connection and whole-run accounting stay alive."""
import asyncio
from collections import deque
from dataclasses import dataclass, field
import time
from .catalog import compile_plan, default_plan
from .controller import JointActionSolver
from .observation import CombatJudge
from .supervision import ProgressSupervisor


@dataclass
class RoomState:
    judge: CombatJudge = field(default_factory=CombatJudge)
    judge_result: str | None = None
    target_room: str | None = None
    solver: JointActionSolver = field(default_factory=JointActionSolver)
    supervisor: ProgressSupervisor = field(default_factory=ProgressSupervisor)
    previous_action: object = None
    clear_started: int | None = None
    clear_safe_since: int | None = None
    last_damage_frame: int = -1000
    plan: object = None
    pending: object = None
    pending_started_frame: int | None = None
    pending_reasons: set = field(default_factory=set)
    last_replan: int = -1000
    event_reasons: set = field(default_factory=set)


class RoomExecutor:
    def __init__(self, session):
        self.session = session
        self.state = RoomState()
        self.context = None
        self.floor = None
        self.results = deque(maxlen=128)
        self.cleared = set()

    @staticmethod
    def identity(s):
        floor = s.floor.instance_id if s.floor else (s.room["stage"], s.room.get("stage_type"))
        return s.run_id, floor, s.room_visit_id

    async def enter(self, s):
        identity = self.identity(s)
        if identity == self.context: return
        if self.state.pending:
            self.state.pending.cancel()
            await asyncio.gather(self.state.pending, return_exceptions=True)
        await self.session.release()
        old = self.state
        # Preserve entry damage collected during the navigation grace period.
        judge = old.judge if old.target_room == s.room_visit_id else CombatJudge()
        solver = old.solver if self.context is None else JointActionSolver()
        judge.timeout_ticks = (180 if s.room.get("room_type") == 5 else 120)*30
        self.state = RoomState(judge=judge, solver=solver, target_room=s.room_visit_id)
        self.state.judge_result = judge.observe(s)
        if self.floor != identity[:2]:
            self.cleared.clear()
            if self.floor is not None:
                self.session.visited.clear()
                self.session.navigation_devices.clear()
            self.floor = identity[:2]
        self.context = identity
        self.session.log("room_started", context=list(map(str, identity)))

    async def execute(self, s):
        # Every visit checks current hazards; an old clear row is never authority.
        await self.enter(s)
        result, combat_result, post_result = "ABORTED", "ABORTED", "NOT_RUN"
        started = s.frame
        errors = []
        current = s
        try:
            result = combat_result = await self._engage(s)
        finally:
            if self.state.pending:
                self.state.pending.cancel()
                await asyncio.gather(self.state.pending, return_exceptions=True)
                self.state.pending = None
            current = self.session.observation.latest or current
            try:
                if result == "CLEARED" and self.session.learning:
                    current = await self.session.learning.collect(self.session,current)
                current = self.session.observation.latest or current
                if self.identity(current) != self.identity(s): result = "POST_CLEAR_CONTEXT_CHANGED"
                elif self.session.death_observed or not current.alive: result = "DEAD"
                elif self.session.death_event_frame is not None: result = "REVIVAL_UNCONFIRMED"
                elif not self.session.bridge.connected: result = "DISCONNECTED"
                elif result == "CLEARED" and (current.blocking_combat or current.hostiles or not current.room["is_clear"]): result = "POST_CLEAR_NEW_THREAT"
                elif result == "CLEARED" and (current.bombs or current.hazards or current.projectiles.get("enemy_projectiles") or any(x.get("is_enemy",True) for x in current.projectiles.get("lasers") or [])): result = "POST_CLEAR_UNSAFE"
                post_result = "SAFE" if result == "CLEARED" else result
            except Exception as exc:
                result = post_result = "ITEM_BOUNDARY_FAILED"
                errors.append(type(exc).__name__+": "+str(exc))
            try:
                await self.session.release()
                if self.session.bridge.connected: await self.session.set_mode("MANUAL")
            except Exception as exc:
                errors.append("cleanup: "+str(exc))
                result = "CLEANUP_FAILED"
            room_key = (s.floor.dimension, s.floor.room_list_index) if s.floor else s.room["room_idx"]
            known_room = not s.floor or s.floor.room_list_index is not None
            first = known_room and result == "CLEARED" and room_key not in self.cleared
            row = {"room_visit_id":s.room_visit_id,"result":result,"combat_result":combat_result,
                   "post_clear_result":post_result,"first_clear":first,"ticks":max(0,current.frame-started),
                   "damage":self.state.judge.damage,"bones_lost":self.state.judge.bones_lost,
                   "terminal_snapshot_id":current.snapshot_id,"alive":current.alive,
                   "actionable":result == "CLEARED","errors":errors}
            if self.session.learning:
                try:
                    build = self.session.build_resolver.update(current)
                    row.update(build_revision=build.revision,experience_revision=build.experience_revision)
                    await self.session.learning.record_room(current,build,row)
                except Exception as exc:
                    result = row["result"] = "PERSISTENCE_FAILED"
                    row["actionable"] = row["first_clear"] = False
                    errors.append("record: "+str(exc))
            if row["first_clear"]:
                if len(self.cleared) >= 1024:
                    result = row["result"] = "FLOOR_ROOM_IDENTITY_LIMIT"
                    row["actionable"] = row["first_clear"] = False
                else: self.cleared.add(room_key)
            self.results.append(row)
            self.session.log("room_finished",**row)
        return result

    async def _engage(self, s):
        session = self.session
        session.target_room = s.room_visit_id
        s = await session.configure_room(s)
        while True:
            if s.room_visit_id != session.target_room: raise RuntimeError("UNEXPECTED_ROOM_CHANGE")
            if s.mode == "MANUAL": raise RuntimeError("MANUAL_TAKEOVER")
            if session.rejected:
                session.log("rejected_action", receipt=session.rejected); session.rejected = None
                await session.release()
            if session.judge_result and session.judge_result != "CLEARED": return session.judge_result
            if session.task_judge.health.state == "reviving":
                await session.release(); s = await session.next_snapshot(after=s.snapshot_id); continue
            build = session.build_resolver.update(s)
            if session.plan.context_ref.build_revision != build.revision:
                await session.release()
                if session.pending:
                    session.pending.cancel(); await asyncio.gather(session.pending, return_exceptions=True)
                    session.pending = None
                session.revision += 1
                session.plan = default_plan(session.context_ref(s, build))
                session.previous_action = None
                session.log("build_transaction", revision=build.revision, signature=build.signature,
                         changed_domains=build.changed_domains, plan=session.plan.model_dump())
            failure = session.supervisor.observe(s, session.previous_action)
            for event in session.supervisor.events:
                session.log("boundary", boundary=event.__dict__, evidence=session.supervisor.evidence)
                if event.kind in ("no_safe_candidate", "escape_margin"):
                    session.solver.emergency_until = max(session.solver.emergency_until, s.frame+60)
                if event.age_ticks >= 60 or event.kind in ("no_safe_candidate", "escape_margin"):
                    session.event_reasons.add(event.kind)
            if failure: return failure
            session.solver.recovery_level = session.supervisor.recovery_level
            if s.frame-session.last_damage_frame <= 60:
                session.solver.emergency_until = max(session.solver.emergency_until, session.last_damage_frame+60)
            if session.supervisor.recovery_level >= 2:
                session.solver.emergency_until = max(session.solver.emergency_until, s.frame+30)
            if session.pending and session.pending.done():
                try:
                    value = session.pending.result()
                    if session.pending_reasons <= {"idle", "blocked", "stall", "no_progress", "oscillation"} and session.supervisor.last_progress > session.pending_started_frame:
                        raise ValueError("REPLAN_EVENT_RESOLVED: local recovery already made progress")
                    session.plan = compile_plan(value, session.context_ref(s, build))
                    session.revision += 1
                    session.solver.profile_name = None
                    session.log("plan_installed", source="deepseek_replan", plan=session.plan.model_dump())
                except Exception as exc:
                    session.log("replan_rejected", error=type(exc).__name__+": "+str(exc))
                session.pending = None
            recent = [f for f in session.solver.recoveries if s.frame-f <= 150]
            if len(recent) >= 2: session.event_reasons.add("repeated_recovery")
            # Mandatory supervision is not policy-controlled by replan_on.
            reasons = sorted(session.event_reasons)
            severe = bool(set(reasons) & {"idle", "blocked", "stall", "no_safe_candidate", "escape_margin"})
            budget = 6 if severe else 4
            if reasons and session.planner and not session.pending and session.replan_requests < budget and (severe or s.frame-session.last_replan >= 90):
                session.last_replan = s.frame; session.event_reasons.clear()
                session.replan_requests += 1
                session.pending_started_frame, session.pending_reasons = s.frame, set(reasons)
                feedback = {"reasons": reasons, "damage_half_hearts": session.judge.damage, "current_plan": session.plan.model_dump(),
                            "progress": session.supervisor.feedback(), "failed_stances": list(session.solver.failed_goals),
                            "last_action": session.previous_action.model_dump() if session.previous_action else None}
                session.pending = asyncio.create_task(asyncio.wait_for(session.planner.plan(s, build.model_copy(deep=True), session.revision, feedback), 20))
                session.log("replan_started", frame=s.frame, reasons=reasons)
            solve_started = time.perf_counter()
            action = await asyncio.to_thread(session.solver.solve, s, session.plan, build.model_copy(deep=True))
            decision_wall_ms = (time.perf_counter()-solve_started)*1000
            action.diagnostics["decision_wall_ms"] = decision_wall_ms
            if session.judge_result == "CLEARED":
                if session.clear_started is None: session.clear_started = s.frame
                if not action.diagnostics["no_safe_candidate"] and action.score["escape_slack"] >= 6:
                    if session.clear_safe_since is None: session.clear_safe_since = s.frame
                    if s.frame-session.clear_safe_since >= 15:
                        session.log("safe_clear_confirmed", frame=s.frame, started=session.clear_started, safe_since=session.clear_safe_since)
                        return "CLEARED"
                else: session.clear_safe_since = None
                if s.frame-session.clear_started >= 90: return "CLEAR_UNSAFE"
            else:
                session.clear_started = session.clear_safe_since = None
            if action.diagnostics.get("component_failures"):
                session.fallbacks += len(action.diagnostics["component_failures"])
                session.event_reasons.add("invalid_profile")
                session.log("component_fallback", frame=s.frame, failures=action.diagnostics["component_failures"])
            session.timings.append(decision_wall_ms)
            session.log.candidates(s.frame, session.solver.last_candidates)
            try:
                await session.send_action(s, action.move, action.shoot, action.model_dump())
            except TimeoutError as exc:
                if "STALE_ACTION" not in str(exc): raise
                session.deadline_misses.append(s.frame)
                session.log("stale_decision_discarded", frame=s.frame)
                if sum(s.frame-f <= 30 for f in session.deadline_misses) >= 3:
                    raise RuntimeError("CONTROL_DEADLINE_MISSES") from exc
                s = await session.next_snapshot(after=s.snapshot_id)
                continue
            if action.profile == "contract_emergency" and (session.previous_action is None or session.previous_action.profile != action.profile):
                session.contract_overrides += 1
                session.log("contract_override", frame=s.frame, last_damage_frame=session.last_damage_frame,
                         recovery_level=session.supervisor.recovery_level, until=session.solver.emergency_until,
                         profile=session.solver.emergency_profile.model_dump())
            session.previous_action = action
            s = await session.next_snapshot(after=s.snapshot_id)

