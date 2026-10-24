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
        self.api = self.lua.execute(source+"\nreturn {State=State, Network=Network, Input=InputExecutor, Commands=CommandHandler, Custom=CustomCollectors}")
        self.lua.globals().api = self.api
        self.lua.execute("api.Network.send=function(value) lastSent=value; return true end; api.State.controlMode='FORCE_AI'")

    def test_watchdog_releases_after_three_updates(self):
        self.lua.execute("api.Input.applyCommand({move={x=1,y=0},shoot={x=0,y=-1}})")
        for _ in range(3): self.lua.execute("callbacks[ModCallbacks.MC_POST_UPDATE]()")
        self.assertEqual(self.api.Input.moveDirection.x, 1)
        self.lua.execute("callbacks[ModCallbacks.MC_POST_UPDATE]()")
        self.assertEqual(self.api.Input.moveDirection.x, 0)
        self.assertEqual(self.api.Input.shootDirection.y, 0)

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
