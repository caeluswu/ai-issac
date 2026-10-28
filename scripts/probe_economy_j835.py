"""Diagnostic shop/angel pickup, with real movement, payment and inventory proof."""
import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'SocketBridge/python'))
from facade import SocketBridge
from combat.item_knowledge import KnowledgeGateway
from combat.item_learning import ItemLearning
from combat.launcher import GameLauncher
from combat.runtime import CombatSession, NativePause, RunLog


async def main(mode):
    directory=ROOT/'artifacts/runs'/datetime.now(timezone.utc).strftime(f'economy-{mode}-%Y%m%dT%H%M%S%fZ')
    log=RunLog(directory);log.manifest(ROOT,None)
    pause=NativePause(game_executable=ROOT/'The Binding of Isaac Rebirth Repentance/isaac-ng.exe')
    launcher=GameLauncher(ROOT,log,pause,slot=1,timeout=75)
    gateway=KnowledgeGateway.from_config(ROOT);gateway.purchase_budget=None
    session=CombatSession(SocketBridge(),None,pause,log,new_run=True,seed='3BYH A82R',
        learning=ItemLearning(gateway,source_mode='development_initialization'),ready_timeout=75)
    report={'source_mode':'development_initialization','formal_run_proof':False,'mode':mode,'passed':False}
    print('Economy artifacts: '+str(directory),flush=True)
    try:
        s=await session.open(bootstrap=launcher)
        room_type=15 if mode=='angel_choice' else 2
        command='goto s.angel.1' if room_type==15 else 'goto s.shop.1'
        await session.rpc('EXEC_CONSOLE',{'command':command});report['goto']=command
        deadline=time.monotonic()+15
        while s.room['room_type']!=room_type:
            if time.monotonic()>deadline:raise TimeoutError('ECONOMY_ROOM_NOT_OBSERVED')
            s=await session.next_snapshot(after=s.snapshot_id)
        initialized=await session.rpc('PREPARE_ECONOMY_PROBE',{'confirm':'diagnostic_initialization','mode':mode})
        if not initialized.get('success'):raise RuntimeError(str(initialized))
        log('initialization_command',command='PREPARE_ECONOMY_PROBE',result=initialized)
        for _ in range(5):s=await session.next_snapshot(after=s.snapshot_id)
        report['before']={'coins':s.inventory.get('coins'),'health':s.health,'items':s.inventory.get('collectibles'),
                          'pickups':[p.model_dump() for p in s.pickups or []]}
        s=await asyncio.wait_for(session.learning.collect(session,s),60)
        report['after']={'coins':s.inventory.get('coins'),'health':s.health,'items':s.inventory.get('collectibles'),
                         'pickups':[p.model_dump() for p in s.pickups or []]}
        owned=s.inventory.get('collectibles') or {}
        if mode=='shop_purchase':report['passed']=bool(owned.get('227') and s.inventory['coins'] <= 3)
        elif mode=='shop_health':report['passed']=bool(owned.get('260') and s.health.get('soul_hearts',0)>=2)
        else:report['passed']=bool(owned.get('182') and not owned.get('185') and not owned.get('407'))
        if mode=='angel_choice' and report['passed']:
            await session.rpc('EXEC_CONSOLE',{'command':'goto d.1'})
            original=s.room_visit_id
            while s.room_visit_id==original:s=await session.next_snapshot(after=s.snapshot_id)
            for _ in range(3):s=await session.next_snapshot(after=s.snapshot_id)
            if not s.blocking_combat:raise RuntimeError('ANGEL_COMBAT_FIXTURE_EMPTY')
            result=await asyncio.wait_for(session.execute_room(s),120)
            report['combat']=session.room_executor.results[-1]
            report['passed']=result=='CLEARED'
    except Exception as exc:report['error']=type(exc).__name__+': '+str(exc)
    finally:
        await session.close();launcher.collect_evidence();log.file.close()
        (directory/'economy-probe.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
        print(json.dumps(report,ensure_ascii=False),flush=True)
    return 0 if report['passed'] and not report.get('error') else 1


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode',choices=('shop_purchase','shop_health','angel_choice'),required=True)
    raise SystemExit(asyncio.run(main(parser.parse_args().mode)))
