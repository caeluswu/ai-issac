"""Replaceable projectile consequences, independent of aim and task policies."""
import math


class HorsemanBloodLanding:
    """J835 owner 82/variant 0: observed landing creates a 60px red puddle.

    A conservative descent estimate is an exclusion region, not a calibrated
    hit probability. It starts early so retreat need not await puddle creation.
    """
    def forecast(self,p,s):
        from .threats import ThreatForecast
        height,speed = p.get("height"),p.get("falling_speed")
        if type(height) not in (int,float) or type(speed) not in (int,float) or not math.isfinite(height+speed) or speed <= 0:
            return []
        eta = min(45,max(0,-height/max(1,speed)))
        vx,vy=p["vel"]["x"],p["vel"]["y"]
        uncertainty=min(40,max(12,math.hypot(vx,vy)*6))
        return [ThreatForecast("landing:"+str(p["id"]),p["pos"]["x"]+vx*eta,p["pos"]["y"]+vy*eta,
            0,0,60,start=0,end=45,uncertainty=uncertainty,source="j835_horseman_blood_landing_bound")]


class ProjectileEffects:
    def __init__(self): self.adapters={(82,0):HorsemanBloodLanding()}
    def register(self,owner,variant,adapter): self.adapters[owner,variant]=adapter
    def forecast(self,p,s):
        adapter=self.adapters.get((p.get("owner_type"),p.get("variant")))
        return adapter.forecast(p,s) if adapter else []


PROJECTILE_EFFECTS=ProjectileEffects()
