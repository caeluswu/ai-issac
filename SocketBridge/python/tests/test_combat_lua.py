"""Run actual mod functions in Lua with minimal game stubs, not text assertions."""
from pathlib import Path
import unittest
from lupa.lua53 import LuaRuntime


class TestLuaInput(unittest.TestCase):
    def setUp(self):
        self.lua = LuaRuntime(unpack_returned_tuples=True)
        self.lua.execute('''
            package.preload.json = function() return {encode=function() return "{}" end, decode=function() return {} end} end
            function enum() local n=0; return setmetatable({}, {__index=function(t,k) n=n+1; rawset(t,k,n); return n end}) end
            ModCallbacks=enum(); EntityPartition=enum(); EntityType=enum(); InputHook=enum(); ButtonAction=enum(); Keyboard=enum()
            EntityFlag=enum(); WeaponType=enum()
            callbacks={}; fakeMod={AddCallback=function(self, key, fn) callbacks[key]=fn end}
            function RegisterMod() return fakeMod end
            paused=false
            function Game() return {IsPaused=function() return paused end} end
            Input={IsButtonPressed=function() return false end}
            Isaac={GetPlayer=function() return nil end, RenderText=function() end, GetScreenWidth=function() return 800 end, GetTime=function() return 0 end}
            print=function() end
        ''')
        source = (Path(__file__).parents[2]/"main.lua").read_text(encoding="utf-8")
        self.api = self.lua.execute(source+"\nreturn {State=State, Network=Network, Input=InputExecutor, Commands=CommandHandler, Custom=CustomCollectors, Sensors=SensorRegistry, Helpers=Helpers}")
        self.lua.globals().api = self.api
        self.lua.globals().realSend = self.api.Network.send
        self.lua.execute("api.Network.send=function(value) lastSent=value; return true end; api.State.controlMode='FORCE_AI'")

    def test_partial_send_resumes_at_last_index(self):
        self.lua.execute('''
            writes={}; api.State.connected=true
            api.State.socket={send=function(self,payload,first)
                table.insert(writes,first)
                if first==1 then return nil,'timeout',1 end
                return #payload
            end}
            sendResult=realSend({})
        ''')
        self.assertTrue(self.lua.globals().sendResult)
        self.assertEqual(list(self.lua.globals().writes.values()), [1,2])

    def test_send_progress_does_not_bypass_budget_or_closed_error(self):
        for mode in ('timeout', 'closed'):
            self.lua.globals().sendMode = mode
            self.lua.execute('''
                calls=0; clock=0; api.State.connected=true
                Isaac.GetTime=function() clock=clock+5; return clock end
                api.State.socket={close=function() end,send=function(self,payload,first)
                    calls=calls+1; return nil,sendMode,first-1
                end}
                sendResult=realSend({})
            ''')
            self.assertFalse(self.lua.globals().sendResult)
            self.assertFalse(self.api.State.connected)
            self.assertEqual(self.api.State.controlMode, 'MANUAL')
            self.assertLessEqual(self.lua.globals().calls, 3)
            if mode == 'closed': self.assertEqual(self.lua.globals().calls, 1)

    def test_large_snapshot_partial_writes_keep_the_complete_line(self):
        self.lua.execute('''
            package.loaded.json.encode=function() return string.rep('x',20000) end
            chunks={}; api.State.connected=true
            api.State.socket={send=function(self,payload,first)
                local last=math.min(first+8191,#payload)
                table.insert(chunks,string.sub(payload,first,last))
                if last < #payload then return nil,'timeout',last end
                return last
            end}
            sendResult=realSend({}); delivered=table.concat(chunks)
        ''')
        self.assertTrue(self.lua.globals().sendResult)
        self.assertEqual(self.lua.globals().delivered, 'x'*20000+'\n')

    def test_diagnostic_fixture_requires_explicit_confirmation_and_room(self):
        self.lua.execute("""
            function Game() return {
                GetRoom=function() return {IsClear=function() return false end} end,
                GetLevel=function() return {GetCurrentRoomDesc=function() return {Data={Variant=1}} end} end
            } end
            hostRejected=api.Commands.handlers.PREPARE_HOST_PROBE({confirm='diagnostic_initialization'})
            terrainRejected=api.Commands.handlers.PREPARE_TERRAIN_PROBE({})
        """)
        self.assertFalse(self.lua.globals().hostRejected.success)
        self.assertFalse(self.lua.globals().terrainRejected.success)

    def test_watchdog_releases_after_three_updates(self):
        self.lua.execute("api.Input.applyCommand({move={x=1,y=0},shoot={x=0,y=-1}})")
        for _ in range(3): self.lua.execute("callbacks[ModCallbacks.MC_POST_UPDATE]()")
        self.assertEqual(self.api.Input.moveDirection.x, 1)
        self.lua.execute("callbacks[ModCallbacks.MC_POST_UPDATE]()")
        self.assertEqual(self.api.Input.moveDirection.x, 0)
        self.assertEqual(self.api.Input.shootDirection.y, 0)

    def test_hostile_shockwave_exports_movement_and_owner(self):
        self.lua.execute('''
            wave={Type=EntityType.ENTITY_EFFECT,Variant=61,SpawnerType=276,Size=0,
                Index=20,InitSeed=31,FrameCount=5,Position={X=200,Y=220},Velocity={X=3,Y=0},
                ToEffect=function() return {Timeout=20,MaxRadius=120,MinRadius=10} end}
            hazard=api.Sensors.sensors.FIRE_HAZARDS.extract(wave,nil)
        ''')
        hazard=self.lua.globals().hazard
        self.assertEqual(hazard.mechanism,'shockwave')
        self.assertEqual(hazard.owner_type,276)
        self.assertEqual(hazard.vel.x,3)
        self.assertEqual(hazard.max_radius,120)

    def test_burnt_j835_fire_does_not_seal_a_corridor(self):
        self.lua.execute('''
            fire={Type=33,Variant=1,SubType=0,Index=7,HitPoints=1,MaxHitPoints=5,
                Size=13,EntityCollisionClass=0,SpriteScale={X=1},Position={X=360,Y=200},
                ToNPC=function() return {State=3} end,
                GetSprite=function() return {GetAnimation=function() return 'Dissapear3' end} end}
        ''')
        hazard=self.api.Sensors.sensors.FIRE_HAZARDS.extract(self.lua.globals().fire,None)
        self.assertTrue(hazard.is_extinguished)
        # Neither HP 1 nor State 3 alone may discard an active collision.
        self.lua.execute('fire.EntityCollisionClass=4')
        self.assertFalse(self.api.Sensors.sensors.FIRE_HAZARDS.extract(self.lua.globals().fire,None).is_extinguished)
        self.lua.execute("fire.EntityCollisionClass=0;fire.GetSprite=function() return {GetAnimation=function() return 'Flickering' end} end")
        self.assertFalse(self.api.Sensors.sensors.FIRE_HAZARDS.extract(self.lua.globals().fire,None).is_extinguished)
        self.lua.execute("fire.ToNPC=function() return {State=8} end;fire.HitPoints=5;fire.EntityCollisionClass=4")
        self.assertFalse(self.api.Sensors.sensors.FIRE_HAZARDS.extract(self.lua.globals().fire,None).is_shooting)
        self.lua.execute("fire.GetSprite=function() return {GetAnimation=function() return 'Shoot' end} end")
        self.assertTrue(self.api.Sensors.sensors.FIRE_HAZARDS.extract(self.lua.globals().fire,None).is_shooting)

    def test_knight_directional_damage_queries_are_read_only_and_optional(self):
        self.lua.execute('''
            Vector=function(x,y) return {X=x,Y=y} end
            calls=0
            npc={GetPlayerTarget=function() return nil end,IsChampion=function() return false end,
                 GetChampionColorIdx=function() return -1 end,
                 CanBeDamagedFromVelocity=function(self,v) calls=calls+1;return v.Y<0 end}
            knight={Type=41,Variant=0,SubType=0,Index=2,InitSeed=3,HitPoints=20,MaxHitPoints=20,
                Size=13,CollisionDamage=1,EntityCollisionClass=4,
                Position={X=200,Y=200},Velocity={X=0,Y=0},
                ToNPC=function() return npc end,IsActiveEnemy=function() return true end,
                IsVulnerableEnemy=function() return true end,HasEntityFlags=function() return false end,
                IsDead=function() return false end,IsBoss=function() return false end}
            row=api.Sensors.sensors.ENEMIES.extract(knight,nil)
        ''')
        row = self.lua.globals().row
        self.assertEqual(self.lua.globals().calls,8)
        self.assertTrue(row.damage_directions['0,-1'])
        self.assertFalse(row.damage_directions['0,1'])
        self.assertEqual(self.lua.globals().knight.HitPoints,20)
        self.lua.execute('npc.CanBeDamagedFromVelocity=function() error("unavailable") end;row=api.Sensors.sensors.ENEMIES.extract(knight,nil)')
        self.assertIsNone(self.lua.globals().row.damage_directions)

    def test_stale_room_action_rejected(self):
        self.lua.execute("api.Input.applyCommand({action_id=1,room_visit_id='wrong',observed_frame=0,move={x=1,y=0}})")
        self.assertFalse(self.lua.globals().lastSent.accepted)
        self.assertEqual(self.api.Input.moveDirection.x, 0)

    def test_lease_age_and_manual_rejected(self):
        for mode, frame in (("MANUAL", 0), ("FORCE_AI", -3)):
            self.api.State.controlMode = mode
            self.lua.globals().observed = frame
            self.lua.execute("api.Input.applyCommand({action_id=1,room_visit_id='boot:0',observed_frame=observed,move={x=1,y=0}})")
            self.assertFalse(self.lua.globals().lastSent.accepted)

    def test_action_edge_not_retriggered_while_held(self):
        self.lua.execute("api.Input.applyCommand({move={x=1,y=0}})")
        callback = self.lua.eval("function() return callbacks[ModCallbacks.MC_INPUT_ACTION](nil,{Type=EntityType.ENTITY_PLAYER},InputHook.IS_ACTION_TRIGGERED,ButtonAction.ACTION_RIGHT) end")
        self.assertTrue(callback()); self.assertTrue(callback())
        self.api.State.updateCount = 1
        self.assertFalse(callback())
        self.lua.execute("api.Input.applyCommand({move={x=0,y=0}})")
        self.assertFalse(callback())
        self.lua.execute("api.Input.applyCommand({move={x=1,y=0}})")
        self.assertTrue(callback())

    def test_disconnect_releases_and_restores_manual(self):
        self.lua.execute("api.Input.applyCommand({move={x=1,y=0}}); api.Network.disconnect()")
        self.assertEqual(self.api.State.controlMode, "MANUAL")
        self.assertEqual(self.api.Input.moveDirection.x, 0)

    def test_new_room_callback_releases_without_invalidating_intro_lease(self):
        self.lua.execute("api.Input.applyCommand({move={x=1,y=0}}); callbacks[ModCallbacks.MC_POST_NEW_ROOM]()")
        self.assertEqual(self.api.State.roomEntryGeneration,1)
        self.assertEqual(self.api.State.controlMode,'FORCE_AI')
        self.assertEqual(self.api.Input.moveDirection.x,0)

    def test_shoot_feedback_requires_engine_queries_and_survives_release(self):
        self.lua.execute('''
            api.Input.applyCommand({action_id=1,room_visit_id="boot:0",observed_frame=0,shoot={x=1,y=0}})
        ''')
        self.assertEqual(len(self.api.Input.feedback),0)
        self.lua.execute('''
            local callback=callbacks[ModCallbacks.MC_INPUT_ACTION]
            for _,action in ipairs({ButtonAction.ACTION_SHOOTLEFT,ButtonAction.ACTION_SHOOTRIGHT,
                                    ButtonAction.ACTION_SHOOTUP,ButtonAction.ACTION_SHOOTDOWN}) do
                callback(nil,{Type=EntityType.ENTITY_PLAYER},InputHook.GET_ACTION_VALUE,action)
            end
        ''')
        row=self.api.Input.feedback[1]
        self.assertTrue(row.consistent)
        self.assertEqual(row.action_id,1)
        self.assertTrue(row["values"].right)
        self.assertFalse(row["values"].left)
        self.lua.execute('api.Input.reset()')
        self.assertEqual(len(self.api.Input.feedback),1)
        self.api.State.updateCount=1
        self.lua.execute('callbacks[ModCallbacks.MC_INPUT_ACTION](nil,{Type=EntityType.ENTITY_PLAYER},InputHook.GET_ACTION_VALUE,ButtonAction.ACTION_SHOOTRIGHT)')
        self.assertFalse(self.api.Input.feedback[2]["values"].right)
        self.assertIsNone(self.api.Input.feedback[2].action_id)

    def test_shoot_feedback_marks_within_update_change_and_bounds_history(self):
        self.lua.execute('''
            api.Input.recordShootFeedback(ButtonAction.ACTION_SHOOTRIGHT,true)
            api.Input.recordShootFeedback(ButtonAction.ACTION_SHOOTRIGHT,false)
        ''')
        self.assertFalse(self.api.Input.feedback[1].consistent)
        for frame in range(1,50):
            self.api.State.updateCount=frame
            self.lua.execute('api.Input.recordShootFeedback(ButtonAction.ACTION_SHOOTRIGHT,false)')
        self.assertEqual(len(self.api.Input.feedback),32)

    def test_paused_render_pumps_rpc_and_heartbeat(self):
        self.lua.execute('''
            paused=true; api.State.connected=true
            local once=true
            api.Network.receive=function() if once then once=false; return {command="GET_CONTROL_MODE",request_id="a"} end end
            api.State.renderCount=1
            callbacks[ModCallbacks.MC_POST_RENDER]()
        ''')
        self.assertEqual(self.lua.globals().lastSent.request_id, "a")
        self.lua.execute("api.State.renderCount=5; callbacks[ModCallbacks.MC_POST_RENDER]()")
        self.assertEqual(self.lua.globals().lastSent.type, "HEARTBEAT")
        self.assertTrue(self.lua.globals().lastSent.paused)

    def test_damage_callback_records_pre_damage_not_confirmed_loss(self):
        self.lua.execute('''
            local p={GetHearts=function() return 6 end,GetSoulHearts=function() return 0 end}
            callbacks[ModCallbacks.MC_ENTITY_TAKE_DMG](nil,{Type=EntityType.ENTITY_PLAYER,ToPlayer=function() return p end},1,0,nil)
        ''')
        e = self.lua.globals().fakeMod.EventSystem.pendingEvents[1]
        self.assertEqual(e.type, "PLAYER_DAMAGE_ATTEMPT")
        self.assertEqual(e.data.hp_before, 6)
        self.assertIsNone(e.data.hp_after)

    def test_knife_observation_phase_and_optional_legacy_methods(self):
        self.lua.execute('''
            Isaac.GetPlayer=function() return {} end
            blade={Type=EntityType.ENTITY_KNIFE,Variant=0,Index=7,InitSeed=42,Size=8,FrameCount=2,
                SpawnerType=EntityType.ENTITY_PLAYER,Position={X=120,Y=50},Velocity={X=8,Y=0},
                ToKnife=function(self) return self end,
                IsFlying=function() return flying end,GetKnifeDistance=function() return distance end}
            Isaac.GetRoomEntities=function() return {blade} end
            flying=false;distance=0
            a=api.Custom.PROJECTILES().player_knives[1]
        ''')
        self.assertEqual(self.lua.globals().a.phase,'held')
        self.assertIsNone(self.lua.globals().a.charge)
        self.lua.execute('api.State.updateCount=1; flying=true; distance=80; b=api.Custom.PROJECTILES().player_knives[1]')
        self.assertEqual(self.lua.globals().b.phase,'outbound')
        self.lua.execute('api.State.updateCount=2;distance=60;c=api.Custom.PROJECTILES().player_knives[1]')
        self.assertEqual(self.lua.globals().c.phase,'returning')
        self.lua.execute('api.State.runId="other";d=api.Custom.PROJECTILES().player_knives[1]')
        self.assertEqual(self.lua.globals().d.phase,'unknown')

    def test_knife_damage_attempt_keeps_callback_identity_and_is_not_hp_loss(self):
        self.lua.execute('''
            api.State.updateCount=12
            local target={Type=10,InitSeed=99,Index=4,HitPoints=20,ToNPC=function(self) return self end}
            local source={Type=EntityType.ENTITY_KNIFE,Entity={InitSeed=42,Index=7}}
            callbacks[ModCallbacks.MC_ENTITY_TAKE_DMG](nil,target,14,0,source)
        ''')
        e=self.lua.globals().fakeMod.EventSystem.pendingEvents[1]
        self.assertEqual(e.type,'KNIFE_DAMAGE_ATTEMPT');self.assertEqual(e.data.callback_frame,12)
        self.assertEqual(e.data.source_knife_id,'42:7');self.assertEqual(e.data.target_id,'99:4')
        self.assertEqual(e.data.hp_before,20);self.assertIsNone(e.data.hp_after)

    def test_damage_source_identity_and_callback_frame_survive_flush(self):
        self.lua.execute('''
            api.State.updateCount=10
            local p={GetHearts=function() return 6 end,GetSoulHearts=function() return 0 end}
            local origin={Type=9,Variant=2,SubType=0,InitSeed=123,Index=7,Size=5,
                Position={X=100,Y=200},Velocity={X=1,Y=2},SpawnerType=10,
                SpawnerEntity={InitSeed=456,Index=8}}
            callbacks[ModCallbacks.MC_ENTITY_TAKE_DMG](nil,{Type=EntityType.ENTITY_PLAYER,ToPlayer=function() return p end},1,0,{Type=9,Entity=origin})
            api.State.updateCount=12
            fakeMod.EventSystem.flush()
        ''')
        message=self.lua.globals().lastSent
        self.assertEqual(message.frame,10)
        self.assertEqual(message.data.callback_frame,10)
        self.assertEqual(message.data.source_entity.instance_id,"123:7")
        self.assertEqual(message.data.source_entity.owner_instance_id,"456:8")
        self.assertEqual(message.data.room_visit_id,"boot:0")

    def test_reconnect_while_paused_uses_render_clock(self):
        self.lua.execute('''
            paused=true; attempts=0; api.State.connected=false
            api.State.updateCount=0; api.State.renderCount=0
            package.preload["socket.core"]=function() return {tcp=function()
                return {settimeout=function() end, connect=function() attempts=attempts+1; return nil,"refused" end,
                        close=function() end}
            end} end
            for i=1,121 do callbacks[ModCallbacks.MC_POST_RENDER]() end
        ''')
        self.assertEqual(self.lua.globals().attempts, 2)
        self.assertEqual(self.api.State.updateCount, 0)

    def test_seed_formatting_is_bounded_and_does_not_reset_run(self):
        self.lua.execute('Seeds={Seed2String=function(seed) return "formatted:"..tostring(seed) end}')
        result = self.lua.eval('api.Commands.process({command="FORMAT_SEEDS",params={values={1,4294967295}}})')
        self.assertTrue(result.success)
        self.assertEqual(result.seeds[2], "formatted:4294967295")
        for values in ("{}", "{0}", "{-1}", "{1.5}", "{4294967296}"):
            result = self.lua.eval('api.Commands.process({command="FORMAT_SEEDS",params={values='+values+'}})')
            self.assertFalse(result.success)

    def test_prepare_run_explicitly_clears_custom_seed_and_effects(self):
        self.lua.execute('''
            initialSeed="OLD1 SEED"; effects=true; executed=nil
            Seeds={IsStringValidSeed=function(s) return s=="ABCD EFGH" end}
            local seeds={ClearSeedEffects=function() effects=false end,
                         SetStartSeed=function(self,seed) initialSeed=seed end}
            Game=function() return {GetSeeds=function() return seeds end} end
            Isaac.ExecuteCommand=function(command) executed=command end
        ''')
        result = self.lua.eval('api.Commands.process({command="PREPARE_RUN",params={seed=""}})')
        self.assertTrue(result.success)
        self.assertEqual(self.lua.globals().initialSeed, "")
        self.assertFalse(self.lua.globals().effects)
        self.assertEqual(self.lua.globals().executed, "restart 0")
        self.assertEqual(self.api.State.controlMode, "MANUAL")
        result = self.lua.eval('api.Commands.process({command="PREPARE_RUN",params={seed="ABCD EFGH"}})')
        self.assertTrue(result.success)
        self.assertEqual(self.lua.globals().initialSeed, "ABCD EFGH")
        result = self.lua.eval('api.Commands.process({command="PREPARE_RUN",params={seed="invalid"}})')
        self.assertFalse(result.success)

    def test_floor_progress_preserves_unknowns_and_reads_vanilla_player_facts(self):
        self.lua.execute("function Game() return {GetLevel=function() return {GetCurses=function() return 0 end} end} end")
        result = self.lua.eval("api.Helpers.floorProgress({ControlsEnabled=true,CanPickRedHearts=function() return false end,CanPickSoulHearts=function() return true end,IsExtraAnimationFinished=function() return true end})")
        self.assertEqual(result.capability_version,1)
        self.assertEqual(result.curses,0)
        self.assertFalse(result.can_pick_red)
        self.assertTrue(result.can_pick_soul)
        unknown = self.lua.eval("api.Helpers.floorProgress({})")
        self.assertIsNone(unknown.can_pick_red)
        exits = self.lua.eval("api.Helpers.exitObservations({grid={a={grid_index=1,type=17,variant=0,state=1,collision=0,x=100,y=120},b={grid_index=2,type=2}}})")
        self.assertEqual(len(exits),1)
        self.assertEqual(exits[1].type,17)

    def test_exit_animation_is_read_without_changing_grid_state(self):
        self.lua.execute("""
            exitGrid={State=0,GetSprite=function() return {GetAnimation=function() return 'Closed' end} end}
            Game=function() return {GetRoom=function() return {GetGridEntity=function() return exitGrid end} end} end
        """)
        exits=self.lua.eval("api.Helpers.exitObservations({grid={a={grid_index=37,type=17,variant=0,state=0,collision=0,x=320,y=200}}})")
        self.assertEqual(exits[1].animation,'Closed')
        self.assertEqual(self.lua.globals().exitGrid.State,0)
