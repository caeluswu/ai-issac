"""Execute actual Lua collectors and validate their output with Python V2 DTOs."""
import copy
import unittest
from lupa.lua53 import lua_type
from combat.catalog import compile_plan, default_plan
from combat.controller import BuildResolver, JointActionSolver
from combat.models import BuildProfile, ContextRef, WorldSnapshot
from combat.observation import SnapshotAssembler
from tests import test_combat_lua as harness
from tests.combat_fixtures import context, snapshot


def lua_value(value):
    if lua_type(value) != "table": return value
    pairs = dict(value.items())
    if pairs and set(pairs) == set(range(1, len(pairs)+1)):
        return [lua_value(pairs[i]) for i in range(1, len(pairs)+1)]
    return {k: lua_value(v) for k, v in pairs.items()}


class TestObservationV2(unittest.TestCase):
    def setUp(self):
        setup = harness.TestLuaInput(); setup.setUp()
        self.lua, self.api = setup.lua, setup.api
        base = snapshot(enemies=False).model_dump()
        base["room"]["stage_type"] = 0
        self.lua.globals().roomInfo = self.lua.table_from(base["room"], recursive=True)
        self.lua.globals().roomLayout = self.lua.table_from(base["layout"], recursive=True)
        self.lua.execute('''
            function Vector(x,y) return {X=x,Y=y,Distance=function(self,b) return math.sqrt((self.X-b.X)^2+(self.Y-b.Y)^2) end} end
            WeaponType={WEAPON_TEARS=1,WEAPON_BRIMSTONE=2,WEAPON_LASER=3,NUM_WEAPON_TYPES=16}
            LevelCurse={CURSE_OF_BLIND=64}
            stage=1; stageType=0; curses=0; effectCount=1; effectCooldown=20
            catalogMax=800; itemCounts={[105]=1,[799]=2}; weaponTypes={[1]=true}
            activeItem=-7
            desc={ListIndex=84,SafeGridIndex=84,pointer="main"}
            level={GetStage=function() return stage end, GetStageType=function() return stageType end,
                GetCurrentRoomDesc=function() return desc end,GetCurrentRoomIndex=function() return 84 end,
                GetRoomByIdx=function(self,index,d) if d==0 then return desc else return {pointer=tostring(d)} end end,
                GetCurses=function() return curses end}
            GetPtrHash=function(value) return value.pointer end
            local item={ID=2,IsCollectible=function() return true end}
            effectList={Size=1,Get=function(self,index) return {Item=item,Count=effectCount,Cooldown=effectCooldown} end}
            player=setmetatable({Damage=3.5,MoveSpeed=1,MaxFireDelay=10,TearRange=260,ShotSpeed=1,Luck=0,
                TearHeight=-20,TearFallingSpeed=0,CanFly=false,Size=10,SpriteScale={X=1},
                Position=Vector(280,160),Velocity=Vector(0,0),
                GetPlayerType=function() return 0 end,IsDead=function() return false end,
                GetAimDirection=function() return {X=1,Y=0} end,
                GetTearMovementInheritance=function() return {X=0,Y=0} end,
                HasWeaponType=function(self,id) return weaponTypes[id] or false end,
                GetCollectibleNum=function(self,id,trueOnly) assert(trueOnly); return itemCounts[id] or 0 end,
                GetCollectibleCount=function() return 3 end,
                GetActiveItem=function(self,slot) if slot==2 then return activeItem else return 0 end end,
                GetHearts=function() return 6 end,GetMaxHearts=function() return 6 end,
                GetEffects=function() return {GetEffectsList=function() return effectList end} end},
                {__index=function(t,k) return function() return 0 end end})
            Game=function() return {GetNumPlayers=function() return 1 end,GetLevel=function() return level end,
                IsPaused=function() return false end,GetSeeds=function() return {
                    GetStartSeedString=function() return "TEST SEED" end} end} end
            Isaac.GetPlayer=function() return player end
            Isaac.GetItemConfig=function() return {GetCollectibles=function() return {Size=catalogMax+1} end,
                GetCollectible=function(self,id) if id>0 then return {MaxCharges=6} end end} end
            entities={}
            Isaac.GetRoomEntities=function() return entities end
            api.Custom.ROOM_INFO=function() roomInfo.stage=stage;roomInfo.stage_type=stageType;return roomInfo end
            api.Custom.ROOM_LAYOUT=function() return roomLayout end
            api.State.runId="test";api.State.roomVisit=1;api.State.updateCount=10
        ''')

    def raw(self): return lua_value(self.api.Custom.COMBAT_FRAME())
    def observe(self): return WorldSnapshot.model_validate(self.raw())

    def test_real_lua_v2_roundtrip_and_normal_solver(self):
        s = self.observe()
        self.assertEqual(s.schema_version, 2)
        self.assertEqual(s.pickups, [])
        self.assertEqual(s.weapons.types, [1])
        self.assertEqual(s.effects.items[0].item_id, 2)
        self.assertEqual(s.inventory["collectibles"]["799"], 2)
        self.assertEqual(s.inventory_coverage.catalog_max_id, 800)
        self.assertEqual(s.inventory_coverage.dynamic_status, "unknown")
        self.assertEqual(s.inventory_coverage.observed_dynamic_ids, [-7])
        self.assertEqual(s.floor.dimension, 0)
        self.assertEqual(s.health_observation.max_hearts, 6)
        self.assertFalse(s.health_observation.dead)
        # Synthetic ID 799 tests observation coverage, not item compatibility.
        with self.assertRaisesRegex(ValueError, "UNSUPPORTED_BUILD"): BuildResolver().update(s)
        action = JointActionSolver().solve(s, default_plan(context(s)), BuildProfile(), True)
        self.assertEqual(action.shoot, (0, 0))

    def test_compound_weapon_does_not_masquerade_as_normal_tears(self):
        self.lua.execute("weaponTypes[3]=true")
        s = self.observe()
        self.assertEqual(s.weapons.types, [1, 3])
        self.assertEqual(s.stats["weapon_type"], -1)
        with self.assertRaisesRegex(ValueError, "UNSUPPORTED_BUILD"): BuildResolver().update(s)

    def test_optional_api_absence_is_unknown_not_empty_fact(self):
        self.lua.execute("Isaac.GetItemConfig=function() return {} end;player.GetEffects=function() error('unavailable') end;GetPtrHash=nil")
        s = self.observe()
        self.assertEqual(s.effects.status, "unknown")
        self.assertEqual(s.inventory_coverage.static_status, "unknown")
        self.assertIsNone(s.inventory_coverage.catalog_max_id)
        self.assertIsNone(s.floor.dimension)
        self.assertTrue(s.effects.unknowns)

    def test_temporary_effect_change_without_inventory_change_is_observed(self):
        before = self.observe()
        self.lua.execute("effectCount=2;effectCooldown=10")
        after = self.observe()
        self.assertEqual(before.inventory, after.inventory)
        self.assertNotEqual(before.effects, after.effects)

    def pickup(self):
        self.lua.execute('''
            pickupEntity={Type=EntityType.ENTITY_PICKUP,Index=8,InitSeed=42,Variant=100,SubType=2,
                Position=Vector(360,200),Velocity=Vector(0,0),Size=10,
                ToNPC=function() return nil end,ToPickup=function() return {Price=-1,ShopItemId=2,Wait=5,OptionsPickupIndex=3} end}
            entities={pickupEntity}
        ''')

    def test_pickup_instance_price_and_visibility_do_not_leak_blind_id(self):
        self.pickup()
        visible = self.observe().pickups[0]
        self.assertEqual(visible.sub_type, 2)
        self.assertEqual(visible.price, -1)
        self.assertEqual(visible.options_pickup_index, 3)
        self.lua.execute("curses=64")
        hidden = self.observe().pickups[0]
        self.assertEqual(hidden.instance_id, visible.instance_id)
        self.assertEqual(hidden.identity_visibility, "hidden")
        self.assertIsNone(hidden.sub_type)
        # The old PICKUPS channel uses the same visibility filter.
        legacy = lua_value(self.lua.eval('api.Sensors.sensors.PICKUPS.extract(pickupEntity,player)'))
        self.assertIsNone(legacy.get("sub_type"))
        self.lua.execute("stage=2;curses=0")
        later = self.observe().pickups[0]
        self.assertEqual(later.identity_visibility, "visible")
        self.assertEqual(later.sub_type, 2)
        self.lua.execute("curses=64")
        self.assertIsNone(self.observe().pickups[0].sub_type)
        self.lua.execute("curses=0;level.GetRoomByIdx=function(self,index,d) if d==1 then return desc else return {pointer=tostring(d)} end end")
        self.assertEqual(self.observe().pickups[0].identity_visibility,"unknown")
        self.lua.execute("curses=0;stage=7")
        self.assertEqual(self.observe().pickups[0].identity_visibility, "unknown")

    def test_reroll_identity_changes_but_entity_instance_is_stable(self):
        self.pickup(); before = self.observe().pickups[0]
        self.lua.execute("pickupEntity.SubType=3")
        after = self.observe().pickups[0]
        self.assertEqual(before.instance_id, after.instance_id)
        self.assertNotEqual(before.sub_type, after.sub_type)

    def test_floor_callback_releases_and_invalidates_same_room_index(self):
        before = self.observe()
        self.lua.execute("api.Input.applyCommand({move={x=1,y=0}});callbacks[ModCallbacks.MC_POST_NEW_LEVEL]()")
        after = self.observe()
        self.assertNotEqual(before.floor.instance_id, after.floor.instance_id)
        self.assertEqual(self.api.Input.moveDirection.x, 0)
        self.assertEqual(self.api.State.controlMode, "MANUAL")
        old_plan = default_plan(context(before))
        with self.assertRaisesRegex(ValueError, "STALE_PLAN"):
            JointActionSolver().solve(after, old_plan, BuildProfile())

    def test_v1_missing_groups_stay_unknown_and_v2_requires_them(self):
        old = snapshot()
        self.assertIsNone(old.pickups)
        self.assertIsNone(old.effects)
        for name in ("pickups", "weapons", "effects", "inventory_coverage", "floor", "health_observation"):
            raw = self.raw(); del raw[name]
            with self.subTest(name=name), self.assertRaises(ValueError): WorldSnapshot.model_validate(raw)

    def test_inconsistent_or_hidden_payload_is_rejected(self):
        self.pickup()
        for group, key, value in (("health_observation", "red_hearts", 5), ("floor", "stage", 3)):
            raw = self.raw(); raw[group][key] = value
            with self.assertRaises(ValueError): WorldSnapshot.model_validate(raw)
        raw = self.raw(); raw["pickups"][0]["identity_visibility"] = "hidden"
        with self.assertRaises(ValueError): WorldSnapshot.model_validate(raw)

    def test_floor_and_dynamic_catalog_context_migration(self):
        s = self.observe(); ref = context(s); ref.catalog_revision = "catalog:sha256:example"
        plan = default_plan(ref)
        self.assertEqual(plan.context_ref.catalog_revision, ref.catalog_revision)
        changed = ref.model_copy(update={"floor_instance_id": "test:floor:99"})
        with self.assertRaisesRegex(ValueError, "stale"): compile_plan(plan, changed)

    def test_snapshot_assembler_accepts_v2_and_retains_error_evidence(self):
        assembler = SnapshotAssembler()
        raw = self.raw()
        self.assertIsNotNone(assembler.consume({"payload": {"COMBAT_FRAME": raw}}))
        raw = copy.deepcopy(raw); del raw["effects"]
        self.assertIsNone(assembler.consume({"payload": {"COMBAT_FRAME": raw}}))
        self.assertIn("Missing V2", assembler.error)
