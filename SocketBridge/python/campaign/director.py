"""A bounded first-floor task; CombatSession remains the only input writer."""
import asyncio
import time
from combat.builds import digest
from combat.catalog import default_plan, compile_plan
from combat.controller import JointActionSolver
from combat.models import Vec
from combat.navigation import DoorNavigator
from combat.supervision import ProgressSupervisor
from .graph import FloorGraph
from .judge import FloorJudge, clear_of_threats
from .models import FloorContext, FloorReport, FloorObjective, RoomKey, TransitionReceipt, RoomResult


class FloorDirector:
    def __init__(self,session,budget=None,wall_limit=1800,game_limit=900*30,route_policy=None):
        self.session,self.budget = session,budget
        self.wall_limit,self.game_limit = wall_limit,game_limit
        self.route_policy = route_policy
        self.graph,self.judge = FloorGraph(route_policy),FloorJudge()
        self.context = None
        self.start = None
        self.state = "WAIT_READY"
        self.transitions = []
        self.rooms = []
        self.exit_executing = False
        self.displacement_errors = 0
        self.diagnostic_task = None
        self.diagnostic_plan = None
        self.diagnosed = set()
        self.began = time.monotonic()
        session.floor_mode = True
        session.bridge.on_message(self.judge.on_raw)
        session.action_auditors.append(self.judge.action)

    def reset_for_floor(self, stage):
        """Retain the one session and registered judge callback across floors."""
        self.graph = FloorGraph(self.route_policy)
        self.judge.__init__(expected_stage=stage)
        self.context = self.start = None
        self.state = "WAIT_READY"
        self.transitions = []
        self.rooms = []
        self.exit_executing = False
        self.displacement_errors = 0
        self.diagnostic_task = self.diagnostic_plan = None
        self.diagnosed = set()
        self.began = time.monotonic()
        self.session.transition_wait = None

    def enter_state(self,state,s,**details):
        self.state = state
        self.session.log("floor_state",state=state,snapshot_id=s.snapshot_id,**details)

    def check(self,s):
        if self.judge.failure: raise RuntimeError(self.judge.failure)
        if not self.session.bridge.connected: raise ConnectionError("DISCONNECTED")
        if time.monotonic()-self.session.observation.received_at > 2:
            heartbeat = self.session.heartbeat
            if not (self.session.transition_paused() or (self.session.pause_owned and heartbeat and heartbeat.get("paused") and time.monotonic()-self.session.heartbeat_at < 1)):
                raise TimeoutError("STALE_OBSERVATION")
        if s.run_id != self.start.run_id: raise RuntimeError("RUN_CHANGED")
        if s.frame-self.start.frame >= self.game_limit: raise TimeoutError("FLOOR_GAME_TIMEOUT")
        if time.monotonic()-self.began >= self.wall_limit: raise TimeoutError("FLOOR_WALL_TIMEOUT")
        if s.floor.instance_id != self.start.floor.instance_id and not self.exit_executing:
            raise RuntimeError("UNAUTHORIZED_FLOOR_CHANGE")
        if self.session.death_observed: raise RuntimeError("DEAD")

    def diagnose(self,s,build,watcher,reason=None):
        if self.diagnostic_task and self.diagnostic_task.done():
            try:
                self.diagnostic_plan=compile_plan(self.diagnostic_task.result(),self.session.context_ref(s,build))
                self.session.log("floor_diagnosis_installed",state=self.state,plan=self.diagnostic_plan.model_dump())
            except Exception as exc: self.session.log("floor_diagnosis_rejected",error=str(exc))
            self.diagnostic_task=None
        reasons=[e.kind for e in watcher.events if e.age_ticks>=60]
        if reason: reasons.append(reason)
        key=s.room_visit_id,self.state
        if reasons and self.session.planner and not self.diagnostic_task and key not in self.diagnosed:
            self.diagnosed.add(key)
            self.diagnostic_task=asyncio.create_task(asyncio.wait_for(self.session.planner.plan(s,build,self.session.revision,
                {"scope":self.state,"reasons":reasons,"progress":watcher.feedback()}),20))
            self.session.log("floor_diagnosis_started",state=self.state,reasons=reasons)
        if self.diagnostic_plan:
            try: return compile_plan(self.diagnostic_plan,self.session.context_ref(s,build))
            except ValueError: self.diagnostic_plan=None
        return default_plan(self.session.context_ref(s,build),family=build.family)

    async def next(self,s):
        fresh = await self.session.next_snapshot(after=s.snapshot_id)
        self.check(fresh)
        return fresh

    async def acquire(self,s):
        await self.session.set_mode("FORCE_AI")
        stamp = self.session.observation.latest
        fresh = await self.next(stamp)
        if fresh.mode != "FORCE_AI": raise RuntimeError("CONTROL_MODE_NOT_CONFIRMED")
        return fresh

    async def settle(self,s,ticks=3):
        began,stable = s.frame,None
        solver = JointActionSolver()
        s = await self.acquire(s)
        while s.frame-began <= max(120,ticks+30):
            if s.mode == "MANUAL": raise RuntimeError("MANUAL_TAKEOVER")
            if s.blocking_combat: return s
            if clear_of_threats(s):
                if stable is None: stable = s.frame
                if s.frame-stable >= ticks:
                    await self.session.release()
                    return s
            else:
                stable = None
                build = self.session.build_resolver.update(s)
                action = await asyncio.to_thread(solver.solve,s,default_plan(self.session.context_ref(s,build),family=build.family),build,True)
                await self.send(s,action.move,action.model_dump())
            s = await self.next(s)
        raise TimeoutError("POST_CLEAR_UNSAFE")

    async def send(self,s,move,metadata=None):
        try: await self.session.send_action(s,move,(0,0),metadata)
        except TimeoutError as exc:
            if "STALE_ACTION" not in str(exc): raise
            self.session.log("floor_stale_action",state=self.state,frame=s.frame)

    def objective(self,s,kind,target,ttl=750,cost=0):
        build = self.session.build_resolver.update(s)
        value = FloorObjective(objective_id=digest([s.snapshot_id,kind,target]),
            context_ref=self.session.context_ref(s,build),kind=kind,target=target,
            deadline_frame=s.frame+ttl,cost_keys=cost)
        self.session.log("floor_objective",objective=value.model_dump())
        return value

    async def door(self,s,slot,door):
        source,prior = RoomKey.from_snapshot(s),s
        cost = int(door.is_locked)
        intent = self.objective(s,"enter_room",door.model_dump(),cost=cost)
        before = s.inventory.get("keys")
        if cost and (before is None or before < cost or door.target_room_type != 4):
            raise ValueError("DOOR_COST_NOT_AUTHORIZED")
        s = await self.acquire(s)
        if not cost:
            s,prior = await asyncio.wait_for(self.session.navigate_door(s,slot,door.model_dump(),s.run_id),30)
        else:
            navigator,watcher,previous = DoorNavigator(slot,door.model_dump()),ProgressSupervisor(),None
            while s.room_visit_id == prior.room_visit_id:
                if s.mode == "MANUAL": raise RuntimeError("MANUAL_TAKEOVER")
                if s.frame > intent.deadline_frame: raise TimeoutError("DOOR_NAVIGATION_TIMEOUT")
                if s.blocking_combat: return s
                if s.inventory.get("keys") not in (before,before-cost): raise RuntimeError("DOOR_BUDGET_CHANGED")
                failure = watcher.observe(s,previous,require_progress=True)
                if failure: raise TimeoutError("DOOR_"+failure)
                build = self.session.build_resolver.update(s)
                model = s.model_copy(deep=True)
                selected = (model.layout["doors"] or {}).get(str(slot))
                if selected is None or selected["target_room"] != door.target_room: raise RuntimeError("DOOR_TARGET_CHANGED")
                selected.update(is_open=True,is_locked=False)  # only the explicitly authorized key door
                action = await asyncio.to_thread(navigator.step,model,default_plan(self.session.context_ref(s,build),family=build.family),build)
                await self.send(s,action.move,action.model_dump())
                previous = action
                s = await self.next(s)
        await self.session.release()
        if s.room_visit_id == prior.room_visit_id: return s
        self.check(s)
        target = self.graph.confirm(source,slot,s)
        matched = s.room["room_idx"] == door.target_room or (door.target_list_index is not None and target.index == door.target_list_index)
        spent = before-s.inventory["keys"] if before is not None and s.inventory.get("keys") is not None else 0
        if spent != cost: raise RuntimeError("DOOR_COST_NOT_CONFIRMED")
        if not matched:
            self.displacement_errors += 1
            if self.displacement_errors > 2: raise RuntimeError("REPEATED_UNEXPECTED_DOOR_DESTINATION")
        receipt = TransitionReceipt(intent_id=intent.objective_id,source=source.text(),target=target.text(),
            slot=slot,from_visit=prior.room_visit_id,to_visit=s.room_visit_id,keys_spent=spent,
            applied_action_id=prior.applied_action_id,matched_expected=matched,completed_frame=s.frame)
        self.transitions.append(receipt)
        self.session.log("floor_transition",**receipt.model_dump())
        return s

    async def exit(self,s):
        choices = [e for e in s.exit_observations or [] if e.type == 17 and e.variant == 0]
        if not choices: raise RuntimeError("NORMAL_EXIT_NOT_OBSERVED")
        found = min(choices,key=lambda e:s.player.pos.distance(e.pos))
        intent = self.objective(s,"leave_floor",found.model_dump(),ttl=300)
        self.judge.arm_exit(found.instance_id)
        if self.session.learning:
            self.session.learning.authorized_exit = found.instance_id
            self.session.learning.mask(s)
        self.exit_executing = True
        s = await self.acquire(s)
        began,wall = s.frame,time.monotonic()
        solver,watcher,previous = JointActionSolver(),ProgressSupervisor(),None
        while s.floor.instance_id == self.start.floor.instance_id:
            if s.mode == "MANUAL": raise RuntimeError("MANUAL_TAKEOVER")
            if s.frame-began > 300 or time.monotonic()-wall > 15: raise TimeoutError("FLOOR_EXIT_TIMEOUT")
            if not clear_of_threats(s): raise RuntimeError("EXIT_NEW_THREAT")
            if not any(e.instance_id == found.instance_id and e.type == 17 and e.variant == 0 for e in s.exit_observations or []):
                raise RuntimeError("EXIT_TARGET_CHANGED")
            failure = watcher.observe(s,previous,require_progress=True)
            if failure: raise TimeoutError("EXIT_"+failure)
            build = self.session.build_resolver.update(s)
            if build.revision != intent.context_ref.build_revision: raise RuntimeError("EXIT_BUILD_CHANGED")
            solver.recovery_level=watcher.recovery_level
            plan=self.diagnose(s,build,watcher)
            action = await asyncio.to_thread(solver.solve,s,plan,build,True,found.pos,(),(found.grid_index,))
            if s.player.pos.distance(found.pos)<45 and self.session.transition_wait is None:
                self.session.transition_wait={"kind":"floor_transition","visit":s.room_visit_id,"run_id":s.run_id,"floor_instance_id":s.floor.instance_id,"expires":time.monotonic()+15}
                self.session.log("transition_wait_authorized",kind_of_wait="floor_transition",room_visit_id=s.room_visit_id,wall_limit=15)
            await self.send(s,action.move,action.model_dump())
            previous = action
            s = await self.next(s)
        if not self.judge.departure: raise RuntimeError("NORMAL_EXIT_NOT_PROVEN")
        self.session.log("normal_exit_observed",intent_id=intent.objective_id,**self.judge.departure)
        self.enter_state("VERIFY_ACTIONABLE",s)
        await self.session.release()
        began,wall = s.frame,time.monotonic()
        while not (s.floor_progress.controls_enabled is True and s.floor_progress.extra_animation_finished is True and clear_of_threats(s)):
            if s.frame-began > 300 or time.monotonic()-wall > 15: raise TimeoutError("NEXT_FLOOR_ANIMATION_TIMEOUT")
            s = await self.next(s)
        s = await self.acquire(s)
        solver = JointActionSolver()
        origin = s.player.pos.model_copy()
        goal = origin+Vec(x=40,y=0)
        while not self.judge.success:
            if s.mode == "MANUAL": raise RuntimeError("MANUAL_TAKEOVER")
            if s.frame-began > 300 or time.monotonic()-wall > 15: raise TimeoutError("NEXT_FLOOR_NOT_ACTIONABLE")
            if not clear_of_threats(s): raise RuntimeError("NEXT_FLOOR_NOT_SAFE")
            build = self.session.build_resolver.update(s)
            action = await asyncio.to_thread(solver.solve,s,default_plan(self.session.context_ref(s,build),family=build.family),build,True,goal)
            await self.send(s,action.move,action.model_dump())
            s = await self.next(s)
        return s

    async def execute(self,s):
        self.start = s
        self.judge.begin(s)
        self.context = FloorContext(run_id=s.run_id,floor_instance_id=s.floor.instance_id,
            stage=s.floor.stage,stage_type=s.floor.stage_type,dimension=s.floor.dimension,
            curses=s.floor_progress.curses,initial_snapshot_id=s.snapshot_id,
            support_profile=("j835-isaac-normal-tears-first-floor-v1" if s.floor.stage==1 else
                             f"j835-isaac-normal-tears-stage{s.floor.stage}-experimental-v1"))
        while True:
            self.check(s)
            damage_before = self.session.task_judge.health.damage
            self.enter_state("CLASSIFY",s)
            if len(self.rooms) >= 128: raise RuntimeError("FLOOR_VISIT_LIMIT")
            for _ in range(3): s = await self.next(s)
            self.graph.observe(s)
            if s.blocking_combat:
                self.enter_state("RESOLVE_ROOM",s)
                result = await self.session.execute_room(s)
                row = {"room_key":RoomKey.from_snapshot(s).text(),**self.session.room_executor.results[-1]}
                row.update({k:self.judge.visits.get(s.room_visit_id,{}).get(k,row[k]) for k in ("damage","bones_lost")})
                contract = RoomResult.model_validate({k:row[k] for k in RoomResult.model_fields if k in row})
                self.rooms.append(row)
                if result != "CLEARED" or not contract.actionable or not contract.alive: raise RuntimeError(result)
                s = self.session.observation.latest
            elif not s.room["is_clear"]:
                self.enter_state("RESOLVE_ENVIRONMENT",s)
                s = await self.session.resolve_environment(s)
                if s.blocking_combat: continue
            self.enter_state("POST_CLEAR",s)
            s = await self.settle(s,45 if s.room["room_type"] == 5 else 3)
            if s.blocking_combat: continue
            if self.session.learning:
                s = await self.session.learning.collect(self.session,s)
                self.check(s)
                if not clear_of_threats(s): s = await self.settle(s)
                if not clear_of_threats(s): raise RuntimeError("POST_CLEAR_INTERACTION_UNSAFE")
            self.graph.observe(s)
            if not self.rooms or self.rooms[-1]["room_visit_id"] != s.room_visit_id:
                build = self.session.build_resolver.update(s)
                row = {"room_key":RoomKey.from_snapshot(s).text(),"room_visit_id":s.room_visit_id,
                    "result":"CLEARED","combat_result":"NOT_REQUIRED","post_clear_result":"SAFE",
                    "terminal_snapshot_id":s.snapshot_id,"alive":s.alive,"actionable":True,
                    "damage":self.judge.visits.get(s.room_visit_id,{}).get("damage",0),
                    "damage_since_classification":self.session.task_judge.health.damage-damage_before,
                    "build_revision":build.revision,"experience_revision":build.experience_revision}
                self.rooms.append(row)
                if self.session.learning: await self.session.learning.record_room(s,build,row)
                self.session.log("empty_room_finished",**row)
            self.enter_state("SELECT_OBJECTIVE",s)
            if self.judge.completed_boss and self.judge.completed_boss["room_key"] == RoomKey.from_snapshot(s).text():
                self.enter_state("EXECUTE_EXIT",s)
                s = await self.exit(s)
                self.enter_state("COMPLETE",s)
                return
            choice = self.graph.choose(s)
            if choice is None: raise RuntimeError("FLOOR_ROUTE_EXHAUSTED")
            slot,door = choice
            self.enter_state("EXECUTE_DOOR",s,slot=slot)
            try: s = await self.door(s,slot,door)
            except TimeoutError as exc:
                if "STALL" in str(exc) or "STALE" in str(exc): raise
                self.graph.failed(RoomKey.from_snapshot(s),slot)
                self.session.log("door_attempt_failed",slot=slot,error=str(exc))
                await self.session.release()
                s = await self.next(self.session.observation.latest)

    async def supervised(self,s):
        task = asyncio.create_task(self.execute(s))
        try:
            while not task.done():
                await asyncio.sleep(.05)
                if self.start is not None: self.check(self.session.observation.latest)
            return await task
        finally:
            if not task.done(): task.cancel()
            await asyncio.gather(task,return_exceptions=True)

    async def run(self,bootstrap=None,*,initial=None,close_session=True):
        if initial is not None and bootstrap is not None:
            raise ValueError("EXISTING_SESSION_CANNOT_BOOTSTRAP")
        if initial is not None:
            self.reset_for_floor(initial.floor.stage)
        before_damage=self.session.task_judge.health.damage
        before_bones=self.session.task_judge.health.bones_lost
        before_resources=dict(self.session.resources.spent)
        before_pause=self.session.pause_seconds
        result,error,cleanup = "FAILED",None,[]
        try:
            s = initial if initial is not None else await self.session.open(bootstrap=bootstrap)
            await self.supervised(s)
            result = "FLOOR_COMPLETED" if self.judge.success else "FLOOR_NOT_PROVEN"
        except (Exception,asyncio.CancelledError) as exc:
            error = type(exc).__name__+": "+str(exc)
            result = "CANCELLED" if isinstance(exc,asyncio.CancelledError) else "FAILED"
            self.session.log("floor_failed",state=self.state,error=error)
        finally:
            if self.diagnostic_task:
                self.diagnostic_task.cancel()
                await asyncio.gather(self.diagnostic_task,return_exceptions=True)
            try:
                if close_session:
                    await self.session.close()
                    cleanup.extend(self.session.cleanup_errors)
                elif self.session.bridge.connected:
                    await self.session.release()
                    await self.session.set_mode("MANUAL")
            except Exception as exc: cleanup.append(str(exc))
        if result == "FLOOR_COMPLETED" and not self.judge.success:
            result="FLOOR_TERMINAL_NOT_PROVEN"
            error=self.judge.failure or "Terminal proof invalidated during cleanup"
        if cleanup: result = "CLEANUP_FAILED"
        recorded={row["room_visit_id"] for row in self.rooms}
        combat={row["room_visit_id"]:row for row in self.session.room_executor.results}
        for visit,observed in self.judge.visits.items():
            if visit not in recorded:
                self.rooms.append({**combat.get(visit,{"result":result,"combat_result":"NOT_FINISHED","post_clear_result":"NOT_FINISHED"}),
                    **observed,"actionable":False})
        last = self.session.observation.latest
        return FloorReport(result=result,success=result == "FLOOR_COMPLETED",error=error,
            seed=self.start.seed if self.start else None,context=self.context,rooms=self.rooms,
            transitions=self.transitions,evidence=self.judge.evidence(),
            damage_half_hearts=self.session.task_judge.health.damage-before_damage,
            bones_lost=self.session.task_judge.health.bones_lost-before_bones,
            resources_spent={k:v-before_resources.get(k,0) for k,v in self.session.resources.spent.items()},
            game_ticks=last.frame-self.start.frame if last and self.start else 0,
            wall_seconds=time.monotonic()-self.began,pause_seconds=self.session.pause_seconds-before_pause,
            llm_configured=self.session.llm_configured,fallback_count=self.session.fallbacks,
            request_budget=self.budget.report() if self.budget else {"requests":0,"cost":0},
            evaluation_mode="debug_replay" if getattr(self.session,"seed",None) else "normal_random",
            cleanup_errors=cleanup)
