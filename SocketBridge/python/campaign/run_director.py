"""Sequential floors on one game run, bridge connection, build and health ledger."""
import asyncio
import time
from .director import FloorDirector
from .models import RunReport


class RunDirector:
    def __init__(self, session, budget=None, *, target_stage=2, wall_limit=3600):
        if type(target_stage) is not int or not 2 <= target_stage <= 5:
            raise ValueError("UNSUPPORTED_RUN_TARGET_STAGE")
        self.session = session
        self.budget = budget
        self.target_stage = target_stage
        self.wall_limit = wall_limit

    async def run(self, bootstrap=None):
        began = time.monotonic()
        floors, identities, cleanup = [], [], []
        run_id = seed = error = None
        result = "FAILED"
        director = FloorDirector(self.session, self.budget)
        try:
            current = await self.session.open(bootstrap=bootstrap)
            run_id, seed = current.run_id, current.seed
            if not current.floor or current.floor.stage != 1 or not current.room.get("is_starting_room"):
                raise ValueError("RUN_MUST_START_ON_FIRST_FLOOR")
            for stage in range(1, self.target_stage + 1):
                remaining = self.wall_limit - (time.monotonic() - began)
                if remaining <= 0:
                    raise TimeoutError("RUN_WALL_TIMEOUT")
                director.wall_limit = min(1800, remaining)
                if (current.run_id != run_id or current.seed != seed or not current.floor
                        or current.floor.stage != stage or not current.room.get("is_starting_room")
                        or current.floor.instance_id in identities):
                    raise RuntimeError("RUN_CONTINUITY_FAILED")
                identities.append(current.floor.instance_id)
                self.session.log("run_floor_started",stage=stage,floor_instance_id=current.floor.instance_id,
                                 snapshot_id=current.snapshot_id)
                report = await director.run(initial=current,close_session=False)
                floors.append(report)
                self.session.log("run_floor_finished",stage=stage,result=report.result,
                                 damage_half_hearts=report.damage_half_hearts,
                                 floor_instance_id=current.floor.instance_id)
                if not report.success:
                    raise RuntimeError("RUN_FLOOR_FAILED: "+(report.error or report.result))
                current = self.session.observation.latest
                if (current is None or current.run_id != run_id or current.seed != seed
                        or current.floor is None or current.floor.stage != stage+1
                        or current.floor.instance_id in identities or not current.alive):
                    raise RuntimeError("RUN_EXIT_CONTINUITY_FAILED")
            result = "TARGET_FLOORS_COMPLETED"
        except (Exception, asyncio.CancelledError) as exc:
            error = type(exc).__name__+": "+str(exc)
            result = "CANCELLED" if isinstance(exc, asyncio.CancelledError) else "FAILED"
            self.session.log("run_failed",error=error,completed_floors=len(floors))
        finally:
            try:
                await self.session.close()
                cleanup.extend(self.session.cleanup_errors)
            except Exception as exc:
                cleanup.append(type(exc).__name__+": "+str(exc))
        if cleanup:
            result = "CLEANUP_FAILED"
        return RunReport(result=result,success=result=="TARGET_FLOORS_COMPLETED",run_id=run_id,seed=seed,
                         error=error,floors=floors,floor_instance_ids=identities,
                         damage_half_hearts=self.session.task_judge.health.damage,
                         bones_lost=self.session.task_judge.health.bones_lost,
                         resources_spent=dict(self.session.resources.spent),wall_seconds=time.monotonic()-began,
                         request_budget=self.budget.report() if self.budget else {"requests":0,"cost":0},
                         cleanup_errors=cleanup)
