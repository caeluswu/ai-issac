import json
import unittest
from mechanisms.program import Program,ProgramError
from mechanisms.normalization import model_program
from tests.test_mechanism_program import fixture


class TestModelSyntaxRecovery(unittest.TestCase):
    def test_only_one_excess_closer_and_raw_source_remains_invalid(self):
        d=fixture();raw=json.dumps(d).replace('"mode": 0,','"mode": 0],')
        with self.assertRaises(ProgramError):Program(raw)
        p,transform=model_program(raw,allow_recovery=True)
        self.assertEqual(p.sha256,Program(d).sha256)
        self.assertEqual(transform["deleted"],"]")
        self.assertEqual(transform["well_typed_results"],1)
        self.assertEqual(json.loads(raw[:transform["offset"]]+raw[transform["offset"]+1:]),d)

    def test_valid_json_with_bad_semantics_is_never_rewritten(self):
        d=fixture();d["decide"]["mode"]=["add",True,1]
        with self.assertRaisesRegex(ProgramError,"NUMBER_TYPE"):model_program(json.dumps(d),allow_recovery=True)

    def test_identity_and_missing_or_multiple_tokens(self):
        p,transform=model_program(json.dumps(fixture()),allow_recovery=True)
        self.assertEqual(transform,{"kind":"identity"})
        for raw in ('{"schema_version":1',json.dumps(fixture()).replace('"mode": 0,','"mode": 0]],')):
            with self.assertRaises(ProgramError):model_program(raw,allow_recovery=True)
