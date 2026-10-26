"""Drop one prestart Enter deliberately; never include this run in combat scores."""
import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'SocketBridge/python'))
from apps.auto_first_room import main as run
from combat.launcher import GameLauncher


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path)
    parser.add_argument('--phase', choices=('save', 'intro'), default='save')
    options = parser.parse_args()
    destination = options.output_dir or ROOT/'artifacts/runs'/('launcher-miss-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    state = {'scope': 'prestart_fault_injection_not_combat_evaluation', 'dropped': False, 'fault_phase': options.phase,
             'probe_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    method = 'confirm_save_slot' if options.phase == 'save' else 'skip_intro'
    original = getattr(GameLauncher, method)

    async def inject(launcher, *args):
        native = launcher.pause.key
        async def key(vk):
            if vk == 0x0D and not state['dropped']:
                state['dropped'] = True
                launcher.record('test_fault_injected', fault='drop_one_'+options.phase+'_enter', **state)
                return
            return await native(vk)
        launcher.pause.key = key
        try: return await original(launcher, *args)
        finally: launcher.pause.key = native

    args = argparse.Namespace(output_dir=destination, local_only=True, startup_only=True,
        slot=1, ready_timeout=75, seed=None, prepare_seeds=None, seed_count=1)
    with patch.object(GameLauncher, method, inject):
        code = asyncio.run(run(args))
    state.update(exit_code=code, passed=code == 0 and state['dropped'])
    (destination/'fault-injection.json').write_text(json.dumps(state, indent=2), encoding='utf8')
    print(json.dumps(state, indent=2))
    return 0 if state['passed'] else 1


if __name__ == '__main__': raise SystemExit(main())
