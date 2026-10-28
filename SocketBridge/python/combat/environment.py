"""Pure environmental objectives; execution and diagnostics remain in runtime."""
from dataclasses import dataclass
from .geometry import GeometryService
from .models import Vec


@dataclass(frozen=True)
class EnvironmentObjective:
    kind: str
    goal: Vec | None = None
    allowed_devices: tuple = ()


class EnvironmentController:
    """Allowlisted ordinary pressure plate; no invented state/console actions.

    Bombs and observed TNT transitions take priority. Activation is confirmed
    only by actual enemy creation, room clear, or a newly open exit.
    """
    def objective(self, snapshot):
        if snapshot.bombs or getattr(snapshot, "environment_residuals", []):
            return EnvironmentObjective("explosion_withdrawal")
        grid = snapshot.layout["grid"] or {}
        plates = [(int(i), c) for i, c in grid.items() if c["type"] == 20 and c["variant"] == 0 and c["state"] != 3]
        choices = []
        for index, plate in plates:
            point = Vec(x=plate["x"], y=plate["y"])
            geometry = GeometryService(snapshot, (index,))
            path = geometry.path(snapshot.player.pos, point)
            if path: choices.append((sum(a.distance(b) for a, b in zip([snapshot.player.pos]+path, path)), index, point))
        if choices:
            _, index, goal = min(choices, key=lambda item: (item[0], item[1]))
            return EnvironmentObjective("activate_pressure_plate", goal, (index,))
        # A plate can be reachable through timed spikes even when no static
        # path exists. The puzzle controller handles timing; this only checks
        # topology and never authorizes crossing a permanently raised spike.
        timed = tuple(int(i) for i,c in grid.items() if c["type"] == 9)
        for index, plate in plates:
            point = Vec(x=plate["x"], y=plate["y"])
            path = GeometryService(snapshot,(index,),(),timed).path(snapshot.player.pos,point)
            if path:
                distance = sum(a.distance(b) for a,b in zip([snapshot.player.pos]+path,path))
                choices.append((distance,index,point))
        if not choices: return EnvironmentObjective("unknown_environment")
        _, index, goal = min(choices, key=lambda item:(item[0],item[1]))
        return EnvironmentObjective("timed_pressure_plate",goal,(index,))

    @staticmethod
    def signature(snapshot):
        return (len(snapshot.bombs), tuple(sorted((i, c["state"], c["collision"]) for i, c in
                (snapshot.layout["grid"] or {}).items() if c["type"] in (12, 20))),
                snapshot.room["is_clear"], snapshot.room["enemy_count"])
