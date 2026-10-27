"""Generic language checks; fixture is not a Chocolate Milk strategy."""
from copy import deepcopy
import json
import unittest
from mechanisms.program import Program, ProgramError, Fuel, OBSERVE_INPUTS, DECIDE_INPUTS, parse
from mechanisms.runtime import TimingRuntime
from tests.combat_fixtures import snapshot
from combat.builds import BuildResolver


def fixture():
    return {"schema_version":1,"component_id":"weapon.fixture_counter","target_build":"1.7.9b.J835","target_item":69,
        "state":{"count":{"type":"number","initial":0,"min":0,"max":100}},
        "observe":[["set","count",["add",["state","count"],1]]],
        "decide":{"mode":0,"shoot_x":0,"shoot_y":0},"source_refs":["engineering-language-fixture"],"assumptions":[]}


def inputs(types):return {k:False if v=="bool" else 0 for k,v in types.items()}


class TestMechanismProgram(unittest.TestCase):
    def test_generic_state_and_pure_decision(self):
        p=Program(fixture());state=p.initial_state();fuel=Fuel()
        next_state=p.observe(inputs(OBSERVE_INPUTS),state,fuel)
        self.assertEqual(state,{"count":0});self.assertEqual(next_state,{"count":1})
        for _ in range(9):p.decide(inputs(DECIDE_INPUTS),next_state,fuel)
        self.assertEqual(next_state,{"count":1})

    def test_shared_fuel_cannot_reset_per_candidate(self):
        p=Program(fixture());fuel=Fuel(5)
        p.decide(inputs(DECIDE_INPUTS),p.initial_state(),fuel)
        with self.assertRaisesRegex(ProgramError,"FUEL"):p.decide(inputs(DECIDE_INPUTS),p.initial_state(),fuel)

    def test_rejects_dynamic_code_unknown_fields_and_state_assignment_in_decide(self):
        for node in (["eval","print(1)"],["input","__dict__"],["set","count",1],["while",True,[]]):
            d=fixture();d["decide"]["mode"]=node
            with self.assertRaises(ProgramError):Program(d)
        d=fixture();d["published"]=True
        with self.assertRaises(ProgramError):Program(d)

    def test_type_checker_is_strict_about_booleans(self):
        d=fixture();d["decide"]["mode"]=["add",True,1]
        with self.assertRaisesRegex(ProgramError,"NUMBER_TYPE"):Program(d)
        d=fixture();d["observe"]=[["set","count",False]]
        with self.assertRaisesRegex(ProgramError,"ASSIGN_TYPE"):Program(d)

    def test_state_bounds_transactional(self):
        p=Program(fixture());state={"count":100}
        with self.assertRaisesRegex(ProgramError,"STATE_VALUE"):p.observe(inputs(OBSERVE_INPUTS),state,Fuel())
        self.assertEqual(state,{"count":100})

    def test_runtime_division_invalid_output_and_nan(self):
        for expr in (["div",1,0],3,["mul",1000000,1000000]):
            d=fixture();d["decide"]["mode"]=expr;p=Program(d)
            with self.assertRaises(ProgramError):p.decide(inputs(DECIDE_INPUTS),p.initial_state(),Fuel())
        for raw in ('{"x":NaN}','{"x":1,"x":2}','['*100+']'*100):
            with self.assertRaises(ProgramError):parse(raw)

    def test_lazy_branch_does_not_evaluate_division_by_zero(self):
        d=fixture();d["decide"]["mode"]=["if",True,0,["div",1,0]]
        p=Program(d);self.assertEqual(p.decide(inputs(DECIDE_INPUTS),p.initial_state(),Fuel())["mode"],0)

    def test_ast_size_and_depth_limits(self):
        d=fixture();node=0
        for _ in range(30):node=["add",node,1]
        d["decide"]["mode"]=node
        with self.assertRaises(ProgramError):Program(d)
        with self.assertRaises(ProgramError):parse(' '*65537)

    def test_wrong_scope_sources_and_missing_inputs_fail_closed(self):
        for key,value in (("target_item",True),("target_build","repentance+"),("source_refs",["x"*501])):
            d=fixture();d[key]=value
            with self.assertRaises(ProgramError):Program(d)
        p=Program(fixture())
        with self.assertRaises(ProgramError):p.decide({},p.initial_state(),Fuel())

    def test_hash_is_canonical_and_source_not_mutable_by_caller(self):
        d=fixture();p=Program(d);sha=p.sha256;d["decide"]["mode"]=1
        self.assertEqual(Program(json.dumps(fixture(),indent=2)).sha256,sha)
        self.assertEqual(p.document["decide"]["mode"],0)


def feedback(frame,direction=(0,0),**overrides):
    x,y=direction
    return {"frame":frame,"room_visit_id":"test:room1","action_id":1,"consistent":True,
        "queried":dict.fromkeys(("left","right","up","down"),True),
        "values":{"left":x==-1,"right":x==1,"up":y==-1,"down":y==1},**overrides}


class TestTimingRuntime(unittest.TestCase):
    def setup_runtime(self):
        s=snapshot();s.room_visit_id="test:room1";s.frame=10
        s.weapon_feedback={"schema_version":1,"provenance":"engine_input_callback","history":[feedback(9)]}
        build=BuildResolver().update(s)
        runtime=TimingRuntime(Program(fixture()));runtime.begin_frame(s,build)
        return s,build,runtime

    def test_same_frame_candidates_and_observation_do_not_commit_twice(self):
        s,b,r=self.setup_runtime()
        self.assertEqual(r.state["count"],1)
        for _ in range(9):
            r.begin_frame(s,b);r.decide(inputs(DECIDE_INPUTS))
        self.assertEqual(r.state["count"],1)
        self.assertGreater(r.fuel.used,9)

    def test_feedback_gap_invalidates_until_neutral(self):
        s,b,r=self.setup_runtime();s.frame=13;s.weapon_feedback["history"]=[feedback(12,(1,0))]
        r.begin_frame(s,b)
        with self.assertRaisesRegex(ProgramError,"UNCONFIRMED"):r.decide(inputs(DECIDE_INPUTS))
        s.frame=14;s.weapon_feedback["history"].append(feedback(13))
        r.begin_frame(s,b);self.assertTrue(r.known);self.assertEqual(r.held_frames,0)

    def test_only_engine_applied_feedback_increments_host_charge(self):
        s,b,r=self.setup_runtime();s.frame=11
        s.weapon_feedback["history"].append(feedback(10,(1,0)))
        r.begin_frame(s,b);self.assertEqual(r.held_frames,1)
        r.decide(inputs(DECIDE_INPUTS));self.assertEqual(r.held_frames,1)
        s.frame=12;s.weapon_feedback["history"].append(feedback(11,(1,0),consistent=False))
        r.begin_frame(s,b);self.assertFalse(r.known)

    def test_new_context_never_reuses_unknown_charge_or_old_room(self):
        s,b,r=self.setup_runtime();s.room_visit_id="other";s.frame=20
        r.begin_frame(s,b)
        self.assertFalse(r.known);self.assertEqual(r.state["count"],0)
        with self.assertRaises(ProgramError):r.decide(inputs(DECIDE_INPUTS))

    def test_missing_query_or_multiple_axes_is_not_neutral(self):
        row=feedback(1);row["queried"].pop("left")
        self.assertIsNone(TimingRuntime._direction(row))
        row=feedback(1);row["values"].update(left=True,right=True)
        self.assertIsNone(TimingRuntime._direction(row))
