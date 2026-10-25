"""Player displacement adapter, independent of weapon and hostile motion."""
from .models import Vec


class PlayerMotion:
    """Keep API velocity for tear inheritance; convert only displacement.

    Captured steady Isaac motion has displacement/API-velocity near 2 per
    bridge update. NPCs and tears have ratio near 1. Applying one global
    conversion would corrupt hostile prediction and aiming.
    """
    @staticmethod
    def desired(move, build):
        return Vec(x=move[0], y=move[1]).unit()*(build.move_speed/build.player_displacement_scale)

    @staticmethod
    def step(position, api_velocity, desired_api_velocity, geometry, build):
        velocity = api_velocity*.75+desired_api_velocity*.25
        point, displacement, blocked, unsafe = geometry.sweep(position, velocity*build.player_displacement_scale)
        return point, displacement*(1/build.player_displacement_scale), blocked, unsafe
