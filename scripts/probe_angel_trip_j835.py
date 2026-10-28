"""Diagnostic real-map boss fight and angel door round trip; no run proof."""
import asyncio
from datetime import datetime,timezone
import json
from pathlib import Path
import sys
import time
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'SocketBridge/python'))
from facade import SocketBridge
from combat.launcher import GameLauncher
from combat.runtime import CombatSession,NativePause,RunLog
from combat.item_knowledge import KnowledgeGateway
from combat.item_learning import ItemLearning
from campaign.director import FloorDirector
from campaign.models import RoomKey


async def main():
    directory=ROOT/'artifacts/runs'/datetime.now(timezone.utc).strftime('angel-trip-%Y%m%dT%H%M%S%fZ')
    log=RunLog(directory);log.manifest(ROOT,None)
    pause=NativePause(game_executable=ROOT/'The Binding of Isaac Rebirth Repentance/isaac-ng.exe')
    launcher=GameLauncher(ROOT,log,pause,slot=1,timeout=75)
    gateway=KnowledgeGateway.from_config(ROOT);gateway.purchase_budget=None
    session=CombatSession(SocketBridge(),None,pause,log,ready_timeout=75,new_run=True,seed='3BYH A82R',
        learning=ItemLearning(gateway,source_mode='development_initialization'))
    report={'source_mode':'development_initialization','formal_run_proof':False,'passed':False}
    async def command(name,params):
        result=await session.rpc(name,params)
        log('initialization_command',command=name,params=params,result=result)
        if not result.get('success'):raise RuntimeError(str(result))
        return result
    try:
        s=await session.open(bootstrap=launcher)
        await command('EXEC_CONSOLE',{'command':'stage 2'})
        deadline=time.monotonic()+15
        while s.floor.stage!=2 or not s.room.get('is_starting_room'):
            if time.monotonic()>deadline:raise TimeoutError('STAGE_NOT_READY')
            s=await session.next_snapshot(after=s.snapshot_id,timeout=10)
        await command('EXEC_CONSOLE',{'command':'giveitem c4'})
        await command('EXEC_CONSOLE',{'command':'giveitem c499'})
        await command('PREPARE_ANGEL_TRIP_PROBE',{'confirm':'diagnostic_initialization','action':'enter_boss'})
        deadline=time.monotonic()+15
        while s.room['room_type']!=5:
            if time.monotonic()>deadline:raise TimeoutError('BOSS_NOT_READY')
            s=await session.next_snapshot(after=s.snapshot_id,timeout=10)
        for _ in range(3):s=await session.next_snapshot(after=s.snapshot_id)
        if not s.blocking_combat:raise RuntimeError('BOSS_FIXTURE_EMPTY')
        result=await asyncio.wait_for(session.execute_room(s),180)
        report['combat']=session.room_executor.results[-1]
        if result!='CLEARED':raise RuntimeError(result)
        s=session.observation.latest
        report['source_key']=RoomKey.from_snapshot(s).text()
        report['door_initialization']=await command('PREPARE_ANGEL_TRIP_PROBE',{
            'confirm':'diagnostic_initialization','action':'spawn_angel_door'})
        deadline=time.monotonic()+10
        while not any(d['target_room_type']==15 for d in s.layout['doors'].values()):
            if time.monotonic()>deadline:raise TimeoutError('ANGEL_DOOR_NOT_OBSERVED')
            s=await session.next_snapshot(after=s.snapshot_id)
        director=FloorDirector(session);director.start=s
        returned=await asyncio.wait_for(director.visit_angel(s),90)
        report.update(returned_key=RoomKey.from_snapshot(returned).text(),rooms=director.rooms,
            inventory=returned.inventory,alive=returned.alive)
        report['passed']=returned.alive and report['returned_key']==report['source_key'] and bool(director.rooms)
    except Exception as exc:report['error']=type(exc).__name__+': '+str(exc)
    finally:
        await session.close();launcher.collect_evidence();log.file.close()
        (directory/'angel-trip.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
        print('Angel trip artifacts: '+str(directory),flush=True)
        print(json.dumps(report,ensure_ascii=False),flush=True)
    return 0 if report['passed'] else 1

if __name__=='__main__':raise SystemExit(asyncio.run(main()))
