"""Observed knife build and host wrapper; the external model supplies its policy."""
import json
from pathlib import Path
from combat.builds import NormalTearsBuild,observed_build,digest,collectibles
from combat.item_capabilities import resolve_items,attack_identity
from combat.models import Vec
from combat.components import NormalTears,PredictiveAim
from combat.component_contracts import CapabilitySet,AttackProposal

FAMILY="knife"


class KnifeBuild:
    def __init__(self,cache_directory=None):
        self.base=NormalTearsBuild();self.calibration=None;self.experience_revision=0
        self.cache_directory=Path(cache_directory) if cache_directory else None;self.from_cache=False

    def adopt(self,value):
        self.calibration=value;self.from_cache=False
        if self.cache_directory:
            self.cache_directory.mkdir(parents=True,exist_ok=True)
            (self.cache_directory/(value["scope_key"]+".json")).write_text(json.dumps(value,allow_nan=False),encoding="utf-8")

    def update(self,s):
        resolve_items(s,knife=True)
        neutral=s.model_copy(deep=True);neutral.stats={**s.stats,"weapon_type":1}
        neutral.inventory["collectibles"]={k:v for k,v in collectibles(s).items() if int(k)!=114}
        p=self.base.update(neutral);p.family=FAMILY;p.modifiers=[]
        key=digest([FAMILY,attack_identity(s)])
        if (not self.calibration or self.calibration["scope_key"]!=key) and self.cache_directory and (self.cache_directory/(key+".json")).exists():
            self.calibration=json.loads((self.cache_directory/(key+".json")).read_text(encoding="utf-8"));self.from_cache=True
        calibration=self.calibration if self.calibration and self.calibration["scope_key"]==key else None
        p.signature=digest([observed_build(s),calibration["calibration_id"] if calibration else "knife-prior"])
        p.calibration_key=key;p.calibration_observations=[]
        p.calibrated=calibration is not None;p.calibration_state="ready" if calibration else "needs_probe"
        p.calibration_source="experience" if calibration and self.from_cache else "observed" if calibration else "prior"
        p.calibration_samples=len(calibration["cycles"]) if calibration else 0
        p.effective_range=calibration["max_observed_distance"] if calibration else 180.
        p.tear_speed=calibration["observed_speed"] if calibration else 10.
        p.experience_revision=self.experience_revision
        return p


class KnifeWeapon(NormalTears):
    capabilities=CapabilitySet(FAMILY,moving_attack=True,break_plain_poop=False)
    def propose(self,s,build,profile,target,velocity,geometry,aim=None):
        result=getattr(self,"attack_model_results",{})
        outputs=[]
        knife=next(iter(s.projectiles.get("player_knives") or []),None)
        held=(0,0)
        for feedback in reversed((s.weapon_feedback or {}).get("history") or []):
            if not feedback.get("consistent"):continue
            values=feedback.get("values") or {}
            held=(int(values.get("right",False))-int(values.get("left",False)),int(values.get("down",False))-int(values.get("up",False)))
            break
        for (candidate_id,_),row in result.items():
            if candidate_id!=getattr(self,"attack_candidate_id",-1):continue
            direction=tuple(row["shoot"])
            # A zero input can launch a held knife. Cover the observed blade
            # direction rather than assuming a zero input has no consequence.
            physical=Vec(x=direction[0],y=direction[1])
            if physical.length()==0 and knife:
                physical=Vec(**knife["vel"])
                if physical.length()<.1:physical=Vec(**knife["pos"])-s.player.pos
            if physical.length()==0:
                history=(s.weapon_feedback or {}).get("history") or []
                for feedback in reversed(history):
                    values=feedback.get("values") or {}
                    physical=Vec(x=int(values.get("right",False))-int(values.get("left",False)),y=int(values.get("down",False))-int(values.get("up",False)))
                    if physical.length()>0:break
            end=s.player.pos+physical.unit()*build.effective_range
            outputs.append(AttackProposal(direction,row["hit_score"],row["expected_damage"],"model_knife_"+row.get("mode","idle"),
                target_id=row["target_ids"][0] if row["target_ids"] else None,
                footprint=(s.player.pos.x,s.player.pos.y,end.x,end.y,24.),prediction_horizon_frames=30,
                position_bias=row.get("position_score",0.),withdrawal_shoot=held))
        return outputs or [AttackProposal((0,0),0,0,"knife_model_unavailable")]


def register_knife():
    from combat.catalog import CATALOG,FAMILY_DEFAULTS,ComponentSpec
    from combat.components import EXECUTABLES
    fire="fire.model_knife";aim="aim.knife"
    if fire not in EXECUTABLES.factories:
        EXECUTABLES.register(fire,KnifeWeapon)
        CATALOG[fire]=ComponentSpec(fire,{},"model knife hold/release and attack geometry",("knife_feedback","target","geometry"),weapons=(FAMILY,),state_scope="room_and_build")
        EXECUTABLES.register(aim,PredictiveAim)
        CATALOG[aim]=ComponentSpec(aim,CATALOG["aim.predictive"].parameters,"knife prediction",("target","build"),weapons=(FAMILY,))
    FAMILY_DEFAULTS[FAMILY]=(aim,fire)
