"""Normal trapdoors must open before approaching them; no game mutation."""
from combat.geometry import GeometryService


class ExitNavigator:
    def __init__(self,exit):
        self.identity=exit.instance_id
        self.goal=None
        self.wait_started=None
        self.phase=None

    def step(self,s):
        exit=next((e for e in s.exit_observations or [] if e.instance_id==self.identity and e.type==17 and e.variant==0),None)
        if exit is None:raise RuntimeError('EXIT_TARGET_CHANGED')
        animation=exit.animation
        opened=animation=='Opened' if animation is not None else exit.state==1
        if opened:
            self.phase='approach'
            return exit.pos
        if self.phase=='approach':raise RuntimeError('EXIT_CLOSED_DURING_APPROACH')
        # J835 avoids opening under a nearby player. A 90px destination leaves
        # margin around the observed/emulated 60px opening boundary. The real
        # state/animation, rather than distance alone, authorizes approach.
        if self.goal is None:
            if s.player.pos.distance(exit.pos)>=80:
                self.goal=s.player.pos.model_copy()
            else:
                g=GeometryService(s,allowed_exits=(exit.grid_index,),allowed_portals=())
                choices=[]
                for i in g.walkable:
                    point=g.positions[i]
                    if point.distance(exit.pos)<90 or not g.clear(point):continue
                    path=g.path(s.player.pos,point)
                    if path:
                        cost=sum(a.distance(b) for a,b in zip([s.player.pos]+path,path))
                        choices.append((cost,i,point))
                if not choices:raise RuntimeError('EXIT_OPENING_RETREAT_UNREACHABLE')
                self.goal=min(choices,key=lambda x:x[:2])[2]
        if s.player.pos.distance(exit.pos)>=80:
            self.phase='wait_open'
            if self.wait_started is None:self.wait_started=s.frame
            if s.frame-self.wait_started>60:raise TimeoutError('EXIT_DID_NOT_OPEN')
        else:self.phase='withdraw'
        return self.goal
