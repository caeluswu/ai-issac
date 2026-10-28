"""Validate navigation from the current clear room without restarting a run."""
import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'SocketBridge/python'))
from facade import SocketBridge
from combat.item_knowledge import KnowledgeGateway
from combat.item_learning import ItemLearning
from combat.runtime import CombatSession, NativePause, RunLog


async def main(args):
    directory = ROOT/'artifacts/runs'/datetime.now(timezone.utc).strftime('door-probe-%Y%m%dT%H%M%S%fZ')
    log = RunLog(directory); log.manifest(ROOT,None)
    session = CombatSession(SocketBridge(),None,
        NativePause(game_executable=ROOT/'The Binding of Isaac Rebirth Repentance/isaac-ng.exe'),
        log,ready_timeout=30,learning=ItemLearning(KnowledgeGateway.from_config(ROOT)),skill_store=ROOT/'skills')
    session.floor_mode = True
    result = {'source_mode':'diagnostic_continuation','formal_run_proof':False,'result':'INCOMPLETE'}
    print('Door probe artifacts: '+str(directory),flush=True)
    try:
        start = await session.open()
        if (not start.room['is_clear'] or start.hostiles or start.bombs or not start.alive
                or start.paused or start.room.get('room_variant') != args.room):
            raise ValueError('CURRENT_CLEAR_ROOM_MISMATCH')
        door = start.layout['doors'].get(str(args.slot))
        if not door or not door['is_open'] or door['is_locked']:
            raise ValueError('SELECTED_DOOR_NOT_OPEN')
        result['initial'] = {'snapshot_id':start.snapshot_id,'room':start.room,
            'pos':start.player.pos.model_dump(),'hp':start.hp,'slot':args.slot}
        await session.set_mode('FORCE_AI')
        current = await session.next_snapshot(after=start.snapshot_id)
        arrived,_ = await asyncio.wait_for(session.navigate_door(current,args.slot,door,start.run_id),120)
        result['final'] = {'snapshot_id':arrived.snapshot_id,'room':arrived.room,
            'pos':arrived.player.pos.model_dump(),'hp':arrived.hp}
        result['damage_half_hearts'] = session.task_judge.health.damage
        if (arrived.room_visit_id == start.room_visit_id or arrived.run_id != start.run_id
                or arrived.floor.instance_id != start.floor.instance_id or not arrived.alive):
            raise RuntimeError('DOOR_TRANSITION_NOT_PROVEN')
        result['result'] = 'EXITED' if not result['damage_half_hearts'] else 'EXITED_WITH_LOSS'
    except Exception as exc:
        result.update(result='FAILED',error=type(exc).__name__+': '+str(exc))
    finally:
        await session.close()
        result['cleanup_errors'] = session.cleanup_errors
        if session.cleanup_errors: result['result'] = 'CLEANUP_FAILED'
        (directory/'door-probe.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
        log.file.close()
        print(json.dumps(result,ensure_ascii=False),flush=True)
    return 0 if result['result'] in ('EXITED','EXITED_WITH_LOSS') else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--room',type=int,required=True,help='Expected current room variant')
    parser.add_argument('--slot',type=int,choices=range(8),required=True)
    raise SystemExit(asyncio.run(main(parser.parse_args())))
