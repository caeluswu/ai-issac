import json
from pathlib import Path
import unittest
from combat.models import WorldSnapshot, Enemy, Vec
from combat.threats import ThreatField, EnemyAdapters
from combat.boundary_safety import boundary_danger
from combat.controller import JointActionSolver, BuildResolver
from combat.engagement import ClosedHost, EngagementPlanner, EngagementRoute
from combat.catalog import default_plan
from tests.combat_fixtures import context, snapshot


class TestRecordedEnemyEffects(unittest.TestCase):
    def load(self, name):
        return WorldSnapshot.model_validate(json.loads((Path(__file__).parent/"fixtures"/name).read_text(encoding="utf8")))

    def test_airborne_explosive_spider_retains_landing_forecast(self):
        s = self.load("airborne_spider.json")
        self.assertEqual(s.enemies[0].collision_class, 0)
        field = ThreatField(s)
        self.assertTrue(any(t.source == "uncalibrated_landing_envelope" for t in field.threats))
        solver = JointActionSolver()
        action = solver.solve(s, default_plan(context(s)), BuildResolver().update(s))
        # The captured choice ran northwest into an airborne spider and claimed
        # zero risk until actual landing contact. That cannot remain "safe".
        prior = [c for c in solver.last_candidates if c["move"] == (-1, -1)]
        self.assertTrue(prior)
        self.assertFalse(any(c["safe"] for c in prior))

    def test_mulliboom_variant_has_attack_and_residual_death_envelopes(self):
        s = self.load("mulliboom_death.json")
        target = next(e for e in s.hostiles if (e.type, e.variant) == (16, 2))
        field = ThreatField(s)
        self.assertTrue(any(t.source == "possible_contact_triggered_explosion" for t in field.threats))
        self.assertEqual(EnemyAdapters().resolve(target).death_radius(target), 100)
        self.assertFalse(field.attack_safe(target, BuildResolver().update(s), [target.pos]*13, 1))
        s.recent_deaths = [{"instance_id": target.instance_id, "type": 16, "variant": 2,
                            "pos": target.pos.model_dump(), "frame": s.frame}]
        self.assertTrue(any(t.identity == "death:"+target.instance_id for t in ThreatField(s).threats))
        target.variant = 0
        self.assertEqual(EnemyAdapters().resolve(target).death_radius(target), 0)

    def test_little_horn_moving_pitfall_is_forecast_despite_zero_hp(self):
        s=snapshot(enemies=False)
        s.player.pos=Vec(x=401,y=319)
        pit=Enemy(instance_id="pitfall:observed",type=291,variant=2,
                  pos=Vec(x=541,y=320),vel=Vec(x=-8.7,y=0),hp=0,max_hp=0,
                  collision_radius=30,vulnerable=False,friendly=False,dead=False,
                  collision_class=0,contact_damage=0,can_shut_doors=False)
        s.enemies=[pit]
        self.assertEqual(s.hostiles,[])
        field=ThreatField(s)
        self.assertTrue(any(t.source=="moving_pitfall_observed_motion" for t in field.threats))
        self.assertLess(field.gap((401,319),(401,319),15,10),0)
        pit.collision_class=3;pit.pos=Vec(x=407,y=319)
        self.assertTrue(boundary_danger(s))

    def test_mom_parts_warn_before_contact_becomes_collidable(self):
        s=snapshot(enemies=False)
        s.player.pos=Vec(x=320,y=370)
        body=Enemy(instance_id="mom_lower",type=45,variant=0,state=9,state_frame=0,
                   pos=Vec(x=320,y=425),vel=Vec(x=0,y=0),hp=793.35,max_hp=793.35,
                   collision_radius=47.25,vulnerable=False,friendly=False,dead=False,
                   collision_class=0,contact_damage=2)
        foot=body.model_copy(update={"instance_id":"mom_foot","variant":10,
                                    "state":7,"state_frame":1,"pos":Vec(x=320,y=280),
                                    "collision_radius":35})
        s.enemies=[body,foot]
        field=ThreatField(s)
        self.assertTrue(any(t.source=="mom_body_visible_windup" for t in field.threats))
        self.assertTrue(any(t.source=="mom_foot_visible_windup" for t in field.threats))
        self.assertLess(field.gap((320,370),(320,370),10,10),0)
        body.state=3
        foot.state=3
        self.assertFalse(any(t.source.startswith("mom_") for t in ThreatField(s).threats))

        # Keep the same observed separation inside the small fixture's
        # walkable grid. A pre-impact warning must cause retreat, not a stall.
        body.state=9
        body.pos=Vec(x=280,y=295)
        s.enemies=[body]
        s.player.pos=Vec(x=280,y=240)
        s.room.update(is_clear=False,enemy_count=1,room_type=5)
        action=JointActionSolver().solve(s,default_plan(context(s)),BuildResolver().update(s))
        self.assertLess(action.move[1],0)
        self.assertEqual(action.shoot,(0,0))

    def test_closed_hosts_reposition_without_firing_then_yield_to_open_target(self):
        # Frame 17400 from the fourth-floor room that previously stalled.
        s = self.load("closed_hosts_room109.json")
        self.assertEqual(len(s.hostiles), 3)
        self.assertFalse(any(enemy.vulnerable for enemy in s.hostiles))
        solver = JointActionSolver()
        build, plan = BuildResolver().update(s), default_plan(context(s))
        action = solver.solve(s, plan, build)
        self.assertNotEqual(action.move, (0, 0))
        self.assertEqual(action.shoot, (0, 0))
        self.assertIsNone(action.target)
        self.assertEqual(action.diagnostics["engagement_reason"], "closed_host_firing_stance")
        self.assertIsNotNone(action.diagnostics["route_goal"])
        old_goal = action.diagnostics["route_goal"]
        engaged = next(enemy for enemy in s.hostiles if enemy.instance_id == action.diagnostics["movement_target"])
        stances = solver.geometry.firing_stances(engaged.pos, build.effective_range, False)
        excluded = [(round(point.x), round(point.y)) for options in stances.values()
                    for point, _, _ in options]
        retry = ClosedHost().route(solver.geometry, s, engaged, build,
                                   solver.modules["position"], solver.modules["fire"], excluded)
        self.assertEqual(retry.reason, "closed_host_retry_firing_stance")

        s.frame += 15
        solver.recovery_level = 1
        action = solver.solve(s, plan, build)
        self.assertNotEqual(action.diagnostics["route_goal"], old_goal)
        self.assertEqual(action.shoot, (0, 0))

        solver.failed_goals.append(tuple(action.diagnostics["route_goal"]))
        engaged.hp -= 1
        s.frame += 1
        solver.solve(s, plan, build)
        self.assertFalse(solver.failed_goals)

        opened_id = action.diagnostics["movement_target"]
        next(enemy for enemy in s.enemies if enemy.instance_id == opened_id).vulnerable = True
        s.frame += 1
        action = solver.solve(s, plan, build)
        self.assertEqual(action.diagnostics["movement_target"], opened_id)
        self.assertIsNotNone(action.diagnostics["engagement_reason"])
        self.assertEqual(action.diagnostics["engagement_phase"], "attack")

    def test_closed_host_route_overrides_generic_kiting_at_rock_corner(self):
        # A later visit to the same room reached this corner with the firing
        # stance below the player. Generic kite scoring used to pull northwest
        # forever although the Host-specific route was valid.
        s = self.load("closed_hosts_room109.json")
        s.player.pos = Vec(x=466.92398071289,y=209.31443786621)
        s.player.vel = Vec(x=1.9143525362015,y=-1.9143525362015)
        target = next(enemy for enemy in s.enemies if enemy.pos.x > 500)
        target.pos = Vec(x=515.76177978516,y=155.98181152344)
        solver = JointActionSolver()
        action = solver.solve(s,default_plan(context(s)),BuildResolver().update(s))
        self.assertEqual(action.diagnostics["engagement_reason"],"closed_host_firing_stance")
        self.assertEqual(action.diagnostics["route_goal"],[516,280])
        self.assertGreater(action.move[1],0)
        self.assertEqual(action.shoot,(0,0))

    def test_open_host_retries_exhausted_firing_stances(self):
        s = self.load("closed_hosts_room109.json")
        s.player.pos = Vec(x=396.5993347168,y=224.6315612793)
        target = next(enemy for enemy in s.enemies if enemy.pos.x > 500)
        target.vulnerable = True
        solver = JointActionSolver()
        solver.failed_goals.extend(((520,280),(520,320)))
        action = solver.solve(s,default_plan(context(s)),BuildResolver().update(s))
        self.assertIn(action.diagnostics["route_goal"],([520,280],[520,320]))
        self.assertFalse(action.diagnostics["occlusion_reposition"])
        self.assertEqual(action.diagnostics["movement_target"],target.instance_id)

    def test_closed_host_engagement_adapter_can_be_replaced(self):
        class NoEngagement(ClosedHost):
            def route(self, *args): return None
        s = self.load("closed_hosts_room109.json")
        action = JointActionSolver(engagement_adapters={(27, 0): NoEngagement()}).solve(
            s, default_plan(context(s)), BuildResolver().update(s))
        self.assertIsNone(action.diagnostics["engagement_reason"])
        self.assertIsNone(action.diagnostics["movement_target"])

    def test_host_wait_expires_and_knockback_invalidates_stance_memory(self):
        class Stances:
            def route(self, geometry, snapshot, enemy, build, position, weapon, excluded):
                for point in (Vec(x=200,y=200),Vec(x=240,y=200)):
                    if (round(point.x),round(point.y)) not in excluded:
                        return EngagementRoute(enemy,(point,),"test_stance")
        s = self.load("closed_hosts_room109.json")
        s.enemies = [s.enemies[0]]
        enemy = s.enemies[0]
        s.player.pos = Vec(x=200,y=200)
        planner = EngagementPlanner({(27,0):Stances()})
        start = s.frame
        first = planner.choose(None,s,None,None,None)
        self.assertEqual(first.phase,"wait_open")
        s.frame = start+121
        second = planner.choose(None,s,None,None,None)
        self.assertEqual(second.waypoints[-1],Vec(x=240,y=200))
        self.assertIn((200,200),planner.memory[enemy.instance_id]["blocked"])
        enemy.pos = enemy.pos+Vec(x=20,y=0)
        s.frame += 1
        planner.choose(None,s,None,None,None)
        self.assertFalse(planner.memory[enemy.instance_id]["blocked"])

    def test_red_host_uses_route_control_without_waiting_for_a_shell(self):
        s = self.load("closed_hosts_room109.json")
        s.enemies = [s.enemies[0]]
        s.enemies[0].variant = 1
        s.enemies[0].vulnerable = True
        action = JointActionSolver().solve(s,default_plan(context(s)),BuildResolver().update(s))
        self.assertEqual(action.diagnostics["engagement_phase"],"attack")
        self.assertIsNotNone(action.diagnostics["route_goal"])

    def test_roundy_disappearance_does_not_treat_remote_wave_damage_as_contact(self):
        s=self.load("roundy_wave_frame572.json")
        field=ThreatField(s)
        reserves=[t for t in field.threats if t.source=="roundy_disappearance_wave_hypothesis"]
        self.assertTrue(reserves)
        point=(s.player.pos.x,s.player.pos.y)
        self.assertLess(min(t.gap(point,point,1,s.player.collision_radius) for t in reserves),0)
        for e in s.enemies:
            if e.type==276:e.state=8
        self.assertFalse(any(t.source=="roundy_disappearance_wave_hypothesis" for t in ThreatField(s).threats))
