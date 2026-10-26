import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('phase1_batch_under_test', Path(__file__).resolve().parents[3]/'run_phase1_batch.py')
batch = importlib.util.module_from_spec(spec); spec.loader.exec_module(batch)


class TestBatchStopping(unittest.TestCase):
    def run_batch(self, succeeds, administrative=False, budget=None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); seeds = ['AAAA AAA'+str(i) for i in range(4)]
            manifest = root/'seeds.json'
            manifest.write_text(json.dumps({'seeds':seeds,'seeds_sha256':hashlib.sha256('\n'.join(seeds).encode()).hexdigest()}))
            stop = root/'stop'; calls = []
            def run(command, **kwargs):
                calls.append(command)
                output = Path(command[command.index('--output-dir')+1]); output.mkdir()
                (output/'report.json').write_text(json.dumps({'success':succeeds,'result':'CLEARED' if succeeds else 'ABORTED','damage_half_hearts':0}))
                if administrative: stop.touch()
                return SimpleNamespace(returncode=0 if succeeds else 1)
            argv = ['batch','--seeds',str(manifest),'--local-only','--stop-file',str(stop)]
            if budget is not None: argv += ['--stop-after-failures',str(budget)]
            with patch.object(batch,'ROOT',root), patch.object(batch.sys,'argv',argv), patch.object(batch.subprocess,'run',side_effect=run):
                code = batch.main()
            summaries = list((root/'artifacts/runs').glob('batch-*/summary.json'))
            return code, json.loads(summaries[0].read_text()), len(calls)

    def test_failure_budget_stops_only_unstarted_samples_and_preserves_denominator(self):
        code, summary, calls = self.run_batch(False, budget=3)
        self.assertEqual((code,calls,summary['completed'],summary['planned'],summary['unstarted']), (1,3,3,4,1))
        self.assertEqual(summary['stop_reason'],'FAILURE_BUDGET_REACHED')
        self.assertTrue(all(not row['success'] for row in summary['reports']))

    def test_stop_file_waits_for_active_report_then_marks_partial(self):
        code, summary, calls = self.run_batch(True, administrative=True)
        self.assertEqual((code,calls,summary['completed'],summary['planned']), (1,1,1,4))
        self.assertEqual(summary['stop_reason'],'ADMINISTRATIVE_STOP_FILE')
        self.assertTrue(summary['reports'][0]['success'])

    def test_successful_batch_runs_every_precommitted_seed(self):
        code, summary, calls = self.run_batch(True,budget=3)
        self.assertEqual((code,calls,summary['completed'],summary['unstarted']), (0,4,4,0))
        self.assertEqual(summary['status'],'complete')

