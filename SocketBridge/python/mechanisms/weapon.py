"""Host geometry/safety wrapper for a model-authored WeaponTiming program."""
import math
import json
from pathlib import Path
from combat.builds import NormalTearsBuild, collectibles, observed_build, digest
from combat.component_contracts import AttackProposal, CapabilitySet, WaitEvidence
from combat.components import NormalTears, PredictiveAim
from combat.models import Vec
from .program import ProgramError
from .runtime import TimingRuntime


FAMILY="charged_tears"


class ChargedBuild:
    def __init__(self,program,calibration):
        from .validation import validate_calibration
        validate_calibration(calibration)
        self.program,self.calibration=program,calibration
        self.base=NormalTearsBuild()
        self.experience_revision=0

    def supports_pickup(self,s,pickup):
        if pickup.variant!=100 or pickup.sub_type!=69:return False
        future=s.model_copy(deep=True)
        items=dict(collectibles(s));items["69"]=items.get("69",0)+1
        future.inventory["collectibles"]=items
        try:ChargedBuild(self.program,self.calibration).update(future)
        except ValueError:return False
        return True

    def update(self,s):
        scope=self.calibration["scope"]
        items={int(k):v for k,v in collectibles(s).items() if v}
        if items.get(69)!=1 or any(k not in (69,105) or v!=1 for k,v in items.items()):raise ValueError("UNSUPPORTED_BUILD: unverified charged combination")
        for field in ("player_type","weapon_type","damage","max_fire_delay","shot_speed"):
            if type(s.stats.get(field)) not in (int,float) or not math.isclose(s.stats[field],scope[field],abs_tol=1e-6):
                raise ValueError("UNSUPPORTED_BUILD: charge calibration mismatch "+field)
        if s.effects and any(e.count for e in s.effects.items):raise ValueError("UNSUPPORTED_BUILD: charged temporary effects")
        neutral=s.model_copy(deep=True)
        neutral.inventory["collectibles"]={k:v for k,v in collectibles(s).items() if int(k)!=69}
        p=self.base.update(neutral)
        p.family=FAMILY
        p.signature=digest([observed_build(s),self.program.sha256,self.calibration["calibration_id"]])
        p.calibration_key=digest([FAMILY,self.program.sha256,self.calibration])
        p.calibrated=True;p.calibration_source="observed"
        p.calibration_samples=len(self.calibration["damage_samples"])
        p.calibration_observations=[]
        p.tear_speed=self.calibration["tear_speed"]
        p.experience_revision=self.experience_revision
        return p


class ProgramWeapon(NormalTears):
    capabilities=CapabilitySet(FAMILY,moving_attack=True,break_plain_poop=False)

    def __init__(self,params,program,calibration):
        from .calibration import validate_runtime_calibration
        validate_runtime_calibration(calibration)
        super().__init__({"min_hit_score":0})
        self.program,self.calibration=program,calibration
        self.runtime=TimingRuntime(program)
        self.params=params

    def begin_frame(self,s,build):self.runtime.begin_frame(s,build)

    def pending_damage(self,s,target):
        # Attribute each existing tear using its observed damage, not the current
        # charge state; preserve the existing bounded contact heuristic.
        total=0.
        for tear in (s.projectiles.get("player_tears") or [])[:256]:
            value=tear.get("collision_damage")
            if type(value) not in (int,float) or not math.isfinite(value) or value<0:continue
            position,velocity=Vec(**tear["pos"]),Vec(**tear["vel"])
            delta=target.pos-position
            ticks=delta.dot(velocity)/max(1e-9,velocity.dot(velocity))
            if 0<=ticks<=20 and (position+velocity*ticks).distance(target.pos+target.vel*ticks)<=target.collision_radius+tear.get("collision_radius",5):
                total+=value*.6
        return total

    def damage_for_hold(self):
        samples=self.calibration["damage_samples"]
        return max((damage for frames,damage in samples if frames<=self.runtime.held_frames),default=0.)

    def diagnostics(self):return self.runtime.report()

    def propose(self,s,build,profile,target,velocity,geometry,aim=None):
        aim=aim or PredictiveAim(profile.aim.params)
        r=self.runtime
        if not r.known:
            # Priming is permitted only before a charge has been observed. The
            # room executor aborts component failures instead of false fallback.
            if r.last_feedback is None:return [AttackProposal((0,0),0,0,"awaiting_neutral_feedback")]
            raise ProgramError("CHARGE_STATE_UNCONFIRMED")
        pool=([target] if target else [])+[e for e in s.hostiles if e.vulnerable and (not target or e.instance_id!=target.instance_id)][:7]
        proposals=[]
        for direction in self.directions:
            options=[(self.hit_score(s,build,aim,e,direction,velocity,geometry),e) for e in pool]
            hit,enemy=max(options,key=lambda pair:(pair[0],pair[1].instance_id)) if options else (0.,None)
            decision=r.decide({"has_target":bool(enemy),"hit":hit,"target_hp":enemy.hp if enemy else 0.,
                "shoot_x":direction[0],"shoot_y":direction[1],"last_shoot_x":r.last_shoot[0],"last_shoot_y":r.last_shoot[1],
                "min_charge_frames":self.calibration["min_charge_frames"],"max_charge_frames":self.calibration["max_charge_frames"]})
            mode=decision["mode"]
            requested=(decision["shoot_x"],decision["shoot_y"])
            if mode==2 and direction!=r.last_shoot:continue
            # Idle while charged is physically a release. It gets identical
            # consequence checks; a model cannot bypass them by saying idle.
            release=mode==2 or (mode==0 and r.held_frames>0)
            if release and direction!=r.last_shoot:continue
            shoot=(0,0) if mode!=1 else requested
            actual=r.last_shoot if release else requested
            if mode==1 and requested!=direction:continue
            end=s.player.pos+self.launch_velocity(s,build,aim,actual,velocity).unit()*build.effective_range if actual!=(0,0) else s.player.pos
            footprint=(s.player.pos.x,s.player.pos.y,end.x,end.y,24.) if release else None
            damage=self.damage_for_hold()
            wait=None
            if mode==1 and 0<r.held_frames<self.calibration["max_charge_frames"]:
                wait=WaitEvidence("charge:"+str(r.last_feedback),"weapon_charge",s.frame+self.calibration["max_charge_frames"]-r.held_frames,
                                  "verified_engine_hold:"+str(r.held_frames))
            # Charging has bounded future opportunity, not an immediate hit.
            reward=hit*damage if release else (hit*min(3.5,damage+.25) if mode==1 else 0.)
            proposals.append(AttackProposal(shoot,hit if release else 0.,reward,
                "program_release" if release else "program_hold" if mode==1 else "program_idle",wait=wait,
                target_id=enemy.instance_id if enemy else None,footprint=footprint,
                prediction_horizon_frames=build.prediction_horizon_frames,withdrawal_shoot=r.last_shoot))
        if not proposals:raise ProgramError("NO_TIMING_PROPOSAL")
        return proposals


def register_program(program,calibration):
    """Host-only publication seam. Caller validates trial/published authority."""
    from combat.catalog import CATALOG, FAMILY_DEFAULTS, ComponentSpec
    from combat.components import EXECUTABLES
    fire="fire.program_"+digest([program.sha256,calibration])[:16]
    aim="aim.charged_predictive"
    if fire not in EXECUTABLES.factories:
        EXECUTABLES.register(fire,lambda params:ProgramWeapon(params,program,calibration))
        CATALOG[fire]=ComponentSpec(fire,{},"bounded model WeaponTiming; release can attack",("weapon_feedback","target","geometry"),weapons=(FAMILY,),state_scope="room_and_build")
    if aim not in EXECUTABLES.factories:
        EXECUTABLES.register(aim,PredictiveAim)
        CATALOG[aim]=ComponentSpec(aim,CATALOG["aim.predictive"].parameters,"charged tear trajectory",("target","build"),weapons=(FAMILY,))
    FAMILY_DEFAULTS[FAMILY]=(aim,fire)
    return fire


class AdaptiveChargedBuild(ChargedBuild):
    """Keep the original program immutable; bind a current measured/prior profile."""
    def __init__(self,program,baseline,cache_directory=None):
        super().__init__(program,baseline)
        self.baseline=baseline;self.allow_piercing=False
        self.cache_directory=Path(cache_directory) if cache_directory else None
        self.measurements={};self.cached=set();self.current_calibration=baseline

    def supports_pickup(self,s,pickup):
        from combat.item_capabilities import resolve_items
        if pickup.variant!=100 or pickup.sub_type is None:return False
        future=s.model_copy(deep=True);items=dict(collectibles(s));items[str(pickup.sub_type)]=items.get(str(pickup.sub_type),0)+1
        future.inventory["collectibles"]=items
        try:resolve_items(future,charge=True,piercing=self.allow_piercing)
        except ValueError:return False
        return True

    def adopt(self,calibration):
        from .calibration import validate_runtime_calibration
        validate_runtime_calibration(calibration)
        key=calibration["scope_key"];self.measurements[key]=calibration;self.cached.discard(key)
        if self.cache_directory:
            self.cache_directory.mkdir(parents=True,exist_ok=True)
            path=self.cache_directory/(key+".json");temp=path.with_suffix(".tmp")
            temp.write_text(json.dumps(calibration,ensure_ascii=False,allow_nan=False),encoding="utf-8");temp.replace(path)

    def update(self,s):
        from combat.item_capabilities import resolve_items
        from .calibration import calibration_key,estimated_calibration,validate_runtime_calibration
        capabilities=resolve_items(s,charge=True,piercing=self.allow_piercing)
        if capabilities.family!=FAMILY:raise ValueError("UNSUPPORTED_BUILD: charge item missing")
        key=calibration_key(s)
        if key not in self.measurements and self.cache_directory and (self.cache_directory/(key+".json")).exists():
            value=json.loads((self.cache_directory/(key+".json")).read_text(encoding="utf-8"));validate_runtime_calibration(value)
            if value["scope_key"]!=key:raise ValueError("CHARGE_CALIBRATION_SCOPE_MISMATCH")
            self.measurements[key]=value;self.cached.add(key)
        calibration=self.measurements.get(key) or estimated_calibration(s,self.baseline)
        self.current_calibration=calibration
        neutral=s.model_copy(deep=True);neutral.inventory["collectibles"]={k:v for k,v in collectibles(s).items() if int(k) not in (69,48)}
        p=self.base.update(neutral);p.family=FAMILY;p.modifiers=list(capabilities.modifiers)
        p.signature=digest([observed_build(s),self.program.sha256,calibration["calibration_id"]]);p.calibration_key=key
        p.calibrated=calibration["source"]=="observed"
        p.calibration_source="experience" if key in self.cached else "observed" if p.calibrated else "prior"
        p.calibration_state="ready" if p.calibrated else "needs_probe"
        p.calibration_samples=len(calibration["observations"]);p.calibration_observations=[]
        p.tear_speed=calibration["tear_speed"];p.experience_revision=self.experience_revision
        return p
