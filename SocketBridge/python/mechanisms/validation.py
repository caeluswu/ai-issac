"""Independent behavioral oracles; no reference charge strategy is generated."""
import time
import json
from pathlib import Path
from .program import Fuel, ProgramError, OBSERVE_INPUTS, DECIDE_INPUTS


def validate_calibration(calibration):
    expected=json.loads((Path(__file__).resolve().parents[3]/"configs/mechanisms/chocolate-milk-j835.json").read_text(encoding="utf-8"))
    if calibration!=expected:raise ProgramError("CALIBRATION_NOT_HOST_APPROVED")


def observed(holding=False,valid=True,new_tear=False):
    return {"feedback_valid":valid,"holding":holding,"shoot_x":1 if holding else 0,"shoot_y":0,"delta_frames":1,"new_tear":new_tear}


def decision(calibration,*,hit=1.,has_target=True,target_hp=10.,last=(0,0),direction=(1,0)):
    return {"has_target":has_target,"hit":hit,"target_hp":target_hp,"shoot_x":direction[0],"shoot_y":direction[1],
        "last_shoot_x":last[0],"last_shoot_y":last[1],"min_charge_frames":calibration["min_charge_frames"],"max_charge_frames":calibration["max_charge_frames"]}


def validate_candidate(program,calibration):
    errors=[];checks=[];max_ms=0.;max_fuel=0
    if program.document["target_build"]!="1.7.9b.J835" or program.document["target_item"]!=69:
        return {"passed":False,"checks":[],"errors":[{"check":"support_scope","error":"NO_REGISTERED_MECHANISM_VALIDATOR"}],"program_sha256":program.sha256,"validator_version":1}
    try:validate_calibration(calibration)
    except ProgramError as exc:return {"passed":False,"checks":[],"errors":[{"check":"calibration","error":str(exc)}],"program_sha256":program.sha256,"validator_version":1}
    def check(name,fn):
        try:fn();checks.append(name)
        except (ProgramError,AssertionError,ValueError,KeyError) as exc:errors.append({"check":name,"error":str(exc)[:1000]})

    def nontrivial():
        assert program.declarations and program.nodes>=20,"Need a new stateful program, not constant output or existing component selection"
        def ops(node):
            if isinstance(node,list):
                if node and isinstance(node[0],str):yield node[0]
                for value in node:yield from ops(value)
            elif isinstance(node,dict):
                for value in node.values():yield from ops(value)
        operators=set(ops(program.document))
        assert {"if","state","set","input"}.issubset(operators),"Need input-dependent conditional state transitions"
    check("nontrivial_stateful_program",nontrivial)

    for hp in (1.,10.):
        def cycle(hp=hp):
            nonlocal max_ms,max_fuel
            state=program.observe(observed(),program.initial_state(),Fuel())
            d=program.decide(decision(calibration,target_hp=hp),state,Fuel())
            assert d["mode"]==1,"A valid target at zero charge must start a hold"
            released=None
            for frame in range(1,calibration["max_charge_frames"]+1):
                started=time.perf_counter();fuel=Fuel()
                state=program.observe(observed(True),state,fuel)
                before=dict(state)
                outcomes=[program.decide(decision(calibration,target_hp=hp,last=(1,0)),state,fuel) for _ in range(9)]
                assert state==before and all(x==outcomes[0] for x in outcomes),"Speculative decisions must be pure"
                max_ms=max(max_ms,(time.perf_counter()-started)*1000);max_fuel=max(max_fuel,fuel.used)
                if outcomes[0]["mode"] in (0,2):released=frame;break
            assert released is not None,"No release within calibrated maximum with perfect target alignment"
            assert released>=calibration["min_charge_frames"],"Release before measured minimum charge"
            state=program.observe(observed(False,new_tear=True),state,Fuel())
            assert program.decide(decision(calibration,target_hp=hp),state,Fuel())["mode"]==1,"Must begin another firing cycle after release"
        check("hold_release_restart_hp_"+str(hp),cycle)

    def bounded_long_feedback():
        state=program.initial_state()
        for n in range(250):state=program.observe(observed(n%57!=0),state,Fuel())
        state=program.observe(observed(valid=False),state,Fuel())
        state=program.observe(observed(),state,Fuel())
        program.decide(decision(calibration,has_target=False,hit=0.,target_hp=0),state,Fuel())
    check("bounded_state_and_interruption",bounded_long_feedback)

    def directional():
        for direction in ((1,0),(-1,0),(0,1),(0,-1)):
            state=program.observe(observed(),program.initial_state(),Fuel())
            d=program.decide(decision(calibration,direction=direction),state,Fuel())
            assert (d["shoot_x"],d["shoot_y"])==direction,"Hold must respect each offered cardinal direction"
    check("all_directions",directional)
    return {"passed":not errors,"checks":checks,"errors":errors,"max_nine_decisions_ms":max_ms,"max_frame_fuel":max_fuel,
            "program_sha256":program.sha256,"validator_version":1,"scope":"offline_semantics_not_combat_outcome"}
