"""Continue a clear Boss-room exit, optionally measuring the next floor.

Uses observed pickups and normal inputs only; never formal whole-run evidence.
"""
import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'SocketBridge/python'))
from facade import SocketBridge
from campaign.director import FloorDirector
from campaign.judge import clear_of_threats
from combat.catalog import default_plan
from combat.controller import JointActionSolver
from combat.item_knowledge import KnowledgeGateway
from combat.item_learning import ItemLearning
from combat.launcher import GameLauncher
from combat.runtime import CombatSession, NativePause, RunLog
from mechanisms.item_adaptation import ItemAdaptation


class ResumeLauncher(GameLauncher):
    """Back up current saves without replacing the run with a baseline."""
    def prepare(self):
        self.baseline=None
        super().prepare()
        self.record('resume_existing_save',baseline_restored=False)


async def main(args):
    directory = ROOT/'artifacts/runs'/datetime.now(timezone.utc).strftime('floor-exit-probe-%Y%m%dT%H%M%S%fZ')
    log = RunLog(directory);log.manifest(ROOT,None)
    gateway = KnowledgeGateway.from_config(ROOT);gateway.purchase_budget=None
    learning = ItemLearning(gateway,source_mode='diagnostic_continuation')
    session = CombatSession(SocketBridge(),None,
        NativePause(game_executable=ROOT/'The Binding of Isaac Rebirth Repentance/isaac-ng.exe'),
        log,ready_timeout=30,learning=learning,skill_store=ROOT/'skills')
    session.floor_mode=True
    session.item_adaptation=ItemAdaptation(ROOT,gateway,allow_generate=False)
    report={'source_mode':'diagnostic_continuation','formal_run_proof':False,'commands':[], 'result':'INCOMPLETE'}
    print('Exit probe artifacts: '+str(directory),flush=True)
    try:
        launcher=ResumeLauncher(ROOT,log,session.pause,timeout=75) if args.resume_game else None
        s=await session.open(bootstrap=launcher)
        if s.floor.stage!=args.stage or s.room['room_type']!=5 or not clear_of_threats(s) or s.paused:
            raise ValueError('CURRENT_CLEAR_BOSS_ROOM_MISMATCH')
        source_run,source_floor=s.run_id,s.floor.instance_id
        report['initial']=s.model_dump(mode='json')
        s=await learning.collect(session,s)
        report['after_pickups']={'inventory':s.inventory,'health':s.health,'pos':s.player.pos.model_dump()}
        found=min((e for e in s.exit_observations or [] if e.type==17 and e.variant==0),
                  key=lambda e:s.player.pos.distance(e.pos))
        learning.authorized_exit=found.instance_id;learning.mask(s)
        await session.set_mode('FORCE_AI')
        s=await session.next_snapshot(after=session.observation.latest.snapshot_id)
        began,wall=s.frame,time.monotonic();solver=JointActionSolver()
        while s.floor.instance_id==source_floor:
            if s.run_id!=source_run or not clear_of_threats(s) or s.mode!='FORCE_AI':
                raise RuntimeError('EXIT_CONTEXT_OR_SAFETY_CHANGED')
            if s.frame-began>300 or time.monotonic()-wall>15:raise TimeoutError('FLOOR_EXIT_TIMEOUT')
            if not any(e.instance_id==found.instance_id for e in s.exit_observations or []):
                raise RuntimeError('EXIT_TARGET_CHANGED')
            build=session.build_resolver.update(s)
            action=await asyncio.to_thread(solver.solve,s,default_plan(session.context_ref(s,build),family=build.family),
                build,True,found.pos,(),(found.grid_index,),goal_tolerance=1.)
            if s.player.pos.distance(found.pos)<45 and session.transition_wait is None:
                session.transition_wait={'kind':'floor_transition','visit':s.room_visit_id,'run_id':s.run_id,
                    'floor_instance_id':source_floor,'expires':time.monotonic()+15}
            await session.send_action(s,action.move,(0,0),action.model_dump())
            s=await session.next_snapshot(after=s.snapshot_id)
        await session.release()
        wall=time.monotonic()
        while not (clear_of_threats(s) and s.floor_progress.controls_enabled is True
                   and s.floor_progress.extra_animation_finished is True):
            if time.monotonic()-wall>15:raise TimeoutError('NEXT_FLOOR_ANIMATION_TIMEOUT')
            s=await session.next_snapshot(after=s.snapshot_id)
        if s.run_id!=source_run or s.floor.stage!=args.stage+1 or not s.room['is_starting_room']:
            raise RuntimeError('WRONG_FLOOR_EXIT')
        report['arrival']=s.model_dump(mode='json');report['result']='EXITED'
        if args.next_floor:
            director=FloorDirector(session)
            floor=await director.run(initial=s,close_session=False)
            report['next_floor']=floor.model_dump(mode='json')
            report['result']='NEXT_FLOOR_COMPLETED' if floor.success else 'NEXT_FLOOR_FAILED'
    except Exception as exc:
        report.update(result='FAILED',error=type(exc).__name__+': '+str(exc))
    finally:
        report['final']=session.observation.latest.model_dump(mode='json') if session.observation.latest else None
        await session.close();report['cleanup_errors']=session.cleanup_errors
        if session.cleanup_errors:report['result']='CLEANUP_FAILED'
        (directory/'exit-probe.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
        log.file.close()
        print(json.dumps({k:v for k,v in report.items() if k not in ('initial','arrival','final')},ensure_ascii=False),flush=True)
    return 0 if report['result'] in ('EXITED','NEXT_FLOOR_COMPLETED') else 1


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage',type=int,choices=range(1,6),required=True)
    parser.add_argument('--next-floor',action='store_true')
    parser.add_argument('--resume-game',action='store_true',help='Boot and continue current save without restoring a baseline')
    raise SystemExit(asyncio.run(main(parser.parse_args())))
