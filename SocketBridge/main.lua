--[[
    SocketBridge v3.0 — Sensor-based modular data collection framework

    Key improvements over v2.x:
    1. Sensors, not Collectors — each Sensor knows HOW to find entities
       (EntityPartition, FindInRadius, FindByType) instead of brute-force
       full-room traversal via Isaac.GetRoomEntities().
    2. Dynamic throttle — combat vs idle collection rates.
    3. Callback-driven triggers — MC_POST_NEW_ROOM, MC_POST_NPC_DEATH, etc.
    4. Subscription negotiation — Python tells Lua what data it needs.
    5. Runtime reconfiguration — CONFIGURE_SENSOR command.
    6. Dual frame counters (from EID) for accurate pause handling.

    Architecture:
    SensorRegistry → Protocol v3.0 → Network → Python
    Python → Network → CommandExecutor → Input injection / Sensor config
]]

local mod = RegisterMod("SocketBridge", 1)
local json = require("json")

-- ============================================================================
-- Configuration
-- ============================================================================
local Config = {
    HOST = "127.0.0.1",
    PORT = 9527,
    PROTOCOL_VERSION = "3.0",
    DEBUG = true,               -- Enable debug logging for data flow
    DEBUG_INTERVAL = 150,       -- Debug output every N frames (~5 sec)
    INPUT_TTL_UPDATES = 3,      -- Release stale input after three game updates
}

-- ============================================================================
-- Global State
-- ============================================================================
local State = {
    connected = false,
    socket = nil,

    -- Dual counters (from EID pattern)
    updateCount = 0,       -- MC_POST_UPDATE (30 tps, respects pause)
    renderCount = 0,       -- MC_POST_RENDER (60 tps, ignores pause)

    -- Message sequencing
    messageSeq = 0,
    prevFrameSent = 0,

    -- Room tracking
    currentRoom = -1,
    currentRoomIdentity = nil,
    roomEntryGeneration = 0,
    roomEntered = false,
    roomVisit = 0,
    runId = "boot",
    floorGeneration = 0,
    recentDeaths = {},

    -- Subscription
    subscribedSensors = {},  -- {["ENEMIES"] = true, ...}

    -- Control mode
    controlMode = "AUTO",    -- "MANUAL" | "AUTO" | "FORCE_AI"
    lastEnemyCount = 0,
    wasInCombat = false,
    aiActive = false,
    toggleCooldown = 0,
    showModeMessage = false,
    modeMessageTimer = 0,
}

-- ============================================================================
-- Helpers
-- ============================================================================
local Helpers = {}

function Helpers.vectorToTable(vec)
    if vec then return { x = vec.X, y = vec.Y } end
    return { x = 0, y = 0 }
end

function Helpers.getPlayers()
    local game = Game()
    local players = {}
    for i = 0, game:GetNumPlayers() - 1 do
        local player = Isaac.GetPlayer(i)
        if player then table.insert(players, player) end
    end
    return players
end

function Helpers.simpleHash(data)
    if type(data) ~= "table" then return tostring(data) end
    local str = ""
    for k, v in pairs(data) do
        if type(v) == "table" then str = str .. k .. Helpers.simpleHash(v)
        else str = str .. k .. tostring(v) end
    end
    return str
end

-- ============================================================================
-- Network Layer (unchanged from v2.x — proven stable)
-- ============================================================================
local InputExecutor  -- Network.disconnect also releases held game input
local Network = {
    retryInterval = 60,
    lastRetryFrame = 0,
}

function Network.connect()
    if State.connected then return true end
    -- Rendering continues in the pause menu while game updates stop.
    -- Use render time for retries so a paused/disconnected client can recover.
    if State.renderCount - Network.lastRetryFrame < Network.retryInterval then
        return false
    end
    Network.lastRetryFrame = State.renderCount

    local requireOk, socketMod = pcall(function()
        return require("socket.core")
    end)
    if not requireOk then
        if State.updateCount <= 60 then
            print("[SocketBridge] ERROR: require('socket.core') failed: " .. tostring(socketMod))
            print("[SocketBridge] Ensure --luadebug launch option is enabled in Steam")
        end
        return false
    end

    local success, tcp, connected, connectErr = pcall(function()
        local s = socketMod
        local t = s.tcp()
        t:settimeout(0.01)
        local ok, err = t:connect(Config.HOST, Config.PORT)
        return t, ok, err
    end)

    if success and tcp and connected then
        State.socket = tcp
        State.connected = true
        print("[SocketBridge] Connected to " .. Config.HOST .. ":" .. Config.PORT)
        return true
    end

    if success and tcp then pcall(function() tcp:close() end) end

    if State.updateCount <= 60 then
        if not success then
            print("[SocketBridge] Connection attempt failed: " .. tostring(tcp))
        else
            print("[SocketBridge] Connection failed: " .. tostring(connectErr))
        end
    end
    return false
end

function Network.disconnect()
    if State.socket then
        pcall(function() State.socket:close() end)
        State.socket = nil
    end
    State.connected = false
    State.receivePartial = nil
    if InputExecutor then InputExecutor.reset() end
    State.controlMode = "MANUAL"
end

function Network.send(data)
    if not State.connected then return false end
    local success, sent, err, expected = pcall(function()
        local payload = json.encode(data) .. "\n"
        local first = 1
        local attempts = 0
        local deadline = Isaac.GetTime() + 20
        while first <= #payload do
            attempts = attempts + 1
            if attempts > 8 or Isaac.GetTime() >= deadline then
                return nil, "send budget exceeded", #payload
            end
            -- LuaSocket returns the last payload index written, including on
            -- partial timeout. Continue from that index so a large snapshot
            -- is never turned into a truncated JSON line followed by EOF.
            local last, sendErr, partial = State.socket:send(payload, first)
            if sendErr and sendErr ~= "timeout" then
                return nil, sendErr, #payload
            end
            local advanced = last or partial
            if advanced and advanced >= first then
                first = advanced + 1
            elseif sendErr == "timeout" then
                -- Retry only within the total time and attempt budget.
            else
                return nil, sendErr or "send made no progress", #payload
            end
        end
        return #payload, nil, #payload
    end)
    if not success or not sent or sent ~= expected then
        if Config.DEBUG then print("[SocketBridge] Send failed: " .. tostring(err)) end
        Network.disconnect()
        return false
    end
    return true
end

function Network.receive()
    if not State.connected then return nil end
    local success, line, err, partial = pcall(function()
        return State.socket:receive("*l", State.receivePartial or "")
    end)
    if success and line then
        State.receivePartial = nil
        local ok, data = pcall(json.decode, line)
        if ok then return data end
    elseif success and err == "timeout" then
        State.receivePartial = partial
    elseif not success or (err and err ~= "timeout") then
        Network.disconnect()
    end
    return nil
end

-- ============================================================================
-- Protocol v3.0
-- ============================================================================
local Protocol = {
    VERSION = Config.PROTOCOL_VERSION,
    MessageType = {
        DATA = "DATA",
        FULL = "FULL",
        EVENT = "EVENT",
        COMMAND = "CMD",
        SUBSCRIBE_ACK = "SUBSCRIBE_ACK",
    }
}

function Protocol.createDataMessage(payload, sensorNames, sensorMeta)
    State.messageSeq = State.messageSeq + 1

    local msg = {
        version = Protocol.VERSION,
        type = Protocol.MessageType.DATA,
        timestamp = Isaac.GetTime(),
        frame = State.updateCount,
        room_index = State.currentRoom,
        seq = State.messageSeq,
        game_time = Isaac.GetTime(),
        prev_frame = State.prevFrameSent,
        sensors = sensorMeta or {},
        payload = payload,
        channels = sensorNames,
    }

    State.prevFrameSent = State.updateCount
    return msg
end

function Protocol.createFullStateMessage(payload, sensorNames, sensorMeta)
    State.messageSeq = State.messageSeq + 1

    local msg = {
        version = Protocol.VERSION,
        type = Protocol.MessageType.FULL,
        room_index = State.currentRoom,
        timestamp = Isaac.GetTime(),
        frame = State.updateCount,
        seq = State.messageSeq,
        game_time = Isaac.GetTime(),
        prev_frame = State.prevFrameSent,
        sensors = sensorMeta or {},
        payload = payload,
        channels = sensorNames,
    }

    State.prevFrameSent = State.updateCount
    return msg
end

function Protocol.createEventMessage(eventType, eventData)
    return {
        version = Protocol.VERSION,
        type = Protocol.MessageType.EVENT,
        timestamp = Isaac.GetTime(),
        frame = State.updateCount,
        event = eventType,
        data = eventData,
    }
end

-- ============================================================================
-- Sensor Registry (NEW — replaces CollectorRegistry)
-- ============================================================================
--
-- Sensor definition:
-- {
--     name = "ENEMIES",
--     search = {
--         strategy = "partition" | "callback" | "hybrid",
--         partitions = EntityPartition.ENEMY,   -- bitmask
--         type_filter = nil,                     -- optional EntityType
--         radius = nil,                          -- nil = full room
--         sort_by_distance = true,
--     },
--     throttle = {
--         base_interval = 1,       -- Frames between non-dynamic collection
--         dynamic = true,           -- Switch intervals based on combat
--         combat_interval = 1,
--         idle_interval = 15,
--     },
--     extract = function(entity, player) ... end,  -- Per-entity data
--     cache = {
--         strategy = "hash" | "none" | "snapshot",
--     },
--     triggers = { "MC_POST_NPC_DEATH", ... },  -- Callback-driven triggers
-- }
--
-- ============================================================================
-- Sensor Triggers (callback-driven forced collection)
-- ============================================================================
-- Sensor Triggers (callback-driven forced collection)
-- ============================================================================
local SensorTriggers = {
    triggers = {},  -- {triggerName: [{sensorName, onTriggerFn}]}
}

function SensorTriggers:register(triggerName, sensorName, onTriggerFn)
    if not self.triggers[triggerName] then
        self.triggers[triggerName] = {}
    end
    table.insert(self.triggers[triggerName], {
        sensorName = sensorName,
        onTrigger = onTriggerFn,
    })
end

function SensorTriggers:fire(triggerName, entity, forceCollectFn)
    local entries = self.triggers[triggerName]
    if not entries then return end
    for _, entry in ipairs(entries) do
        if entry.onTrigger then
            entry.onTrigger(entity)
        end
        -- Delegate to callback to avoid circular dependency with SensorRegistry
        if forceCollectFn then
            forceCollectFn(entry.sensorName)
        end
    end
end

-- ============================================================================
-- Sensor Registry (NEW — replaces CollectorRegistry)
-- ============================================================================
local SensorRegistry = {
    sensors = {},
    cache = {},
    changeHashes = {},
    lastCollect = {},
    lastCollectFrame = {},
    frameCounters = {},    -- per-sensor frame counters for throttle
    forcePending = {},      -- sensors force-collected this frame (always sent)
}

function SensorRegistry:register(name, def)
    self.sensors[name] = {
        name = name or def.name,
        enabled = def.enabled ~= false,
        search = def.search or { strategy = "partition" },
        throttle = def.throttle or {
            base_interval = 1, dynamic = false,
            combat_interval = 1, idle_interval = 15,
        },
        extract = def.extract or function(e, p) return {} end,
        cache = def.cache or {},
        triggers = def.triggers or {},
    }
    self.cache[name] = nil
    self.changeHashes[name] = nil
    self.lastCollect[name] = nil
    self.lastCollectFrame[name] = 0

    -- Register callback triggers
    if def.triggers then
        for _, triggerName in ipairs(def.triggers) do
            SensorTriggers:register(triggerName, name, def.onTrigger)
        end
    end
end

-- ── Search strategies ────────────────────────────────────────────────

function SensorRegistry:_searchEntities(sensor)
    local player = Isaac.GetPlayer(0)
    if not player then return {} end

    local search = sensor.search
    local strategy = search.strategy or "partition"
    local results = {}

    if strategy == "callback" then
        -- Purely callback-driven, no polled search
        return {}
    end

    if strategy == "partition" or strategy == "hybrid" then
        if search.partitions and search.radius and player then
            -- Use FindInRadius with EntityPartition mask (EID technique)
            local radius = search.radius * 40  -- grid units → pixels
            local entities = Isaac.FindInRadius(player.Position, radius, search.partitions)
            for i = 1, #entities do
                local e = entities[i]
                if not search.type_filter or e.Type == search.type_filter then
                    table.insert(results, e)
                end
            end
        elseif search.partitions and not search.radius then
            -- Full room but filtered by partition
            local entities = Isaac.GetRoomEntities()
            for _, e in ipairs(entities) do
                -- Check if entity type matches partition mask
                -- (Simplified: include all for broad partitions)
                if not search.type_filter or e.Type == search.type_filter then
                    table.insert(results, e)
                end
            end
        elseif search.type_filter then
            -- Use FindByType for specific entity type (EID technique)
            local entities = Isaac.FindByType(search.type_filter, -1, -1, true, false)
            for i = 1, #entities do
                table.insert(results, entities[i])
            end
        else
            -- Fallback: full room traversal
            local entities = Isaac.GetRoomEntities()
            for _, e in ipairs(entities) do
                table.insert(results, e)
            end
        end
    end

    -- Sort by distance if requested
    if search.sort_by_distance and player and #results > 0 then
        local playerPos = player.Position
        table.sort(results, function(a, b)
            return playerPos:Distance(a.Position) < playerPos:Distance(b.Position)
        end)
    end

    return results
end

-- ── Throttle ──────────────────────────────────────────────────────────

function SensorRegistry:_shouldCollect(sensor)
    if not sensor.enabled then return false end

    -- Check subscription
    if next(State.subscribedSensors) and not State.subscribedSensors[sensor.name] then
        return false
    end

    local throttle = sensor.throttle
    local interval = throttle.base_interval

    if throttle.dynamic then
        local room = Game():GetRoom()
        local inCombat = room and room:GetAliveEnemiesCount() > 0
        interval = inCombat and throttle.combat_interval or throttle.idle_interval
    end

    if interval <= 0 then
        return false  -- Disabled throttle (callback-only)
    end

    -- Per-sensor frame counter (resets on force-collect)
    local name = sensor.name
    if not self.frameCounters[name] then self.frameCounters[name] = 0 end
    self.frameCounters[name] = self.frameCounters[name] + 1
    if self.frameCounters[name] >= interval then
        self.frameCounters[name] = 0
        return true
    end
    return false
end

-- ── Collect ───────────────────────────────────────────────────────────

function SensorRegistry:collect(name, forceCollect)
    local sensor = self.sensors[name]
    if not sensor then return nil, nil end

    if not forceCollect and not self:_shouldCollect(sensor) then
        return nil, nil
    end

    -- Run extract on searched entities (or custom collect function)
    local success, data = pcall(function()
        local player = Isaac.GetPlayer(0)
        local entities = self:_searchEntities(sensor)
        local results = {}
        for _, entity in ipairs(entities) do
            local entry = sensor.extract(entity, player)
            if entry then
                table.insert(results, entry)
            end
        end
        return results
    end)

    if not success then
        return nil, nil
    end

    if data == nil or (type(data) == "table" and #data == 0 and next(data) == nil) then
        return nil, nil
    end

    -- Hash-based change detection
    if not forceCollect and sensor.cache.strategy == "hash" then
        local newHash = Helpers.simpleHash(data)
        if self.changeHashes[name] == newHash then
            return nil, nil  -- Unchanged
        end
        self.changeHashes[name] = newHash
    end

    self.cache[name] = data
    self.lastCollectFrame[name] = State.updateCount

    local meta = {
        collect_frame = State.updateCount,
        collect_time = Isaac.GetTime(),
        interval = sensor.throttle.dynamic and "dynamic" or "fixed",
        stale_frames = 0,
        entity_count = #data,
        hash = self.changeHashes[name] or "",
    }
    SensorRegistry.lastCollect[name] = meta

    return data, meta
end

function SensorRegistry:collectAll()
    local results = {}
    local collectedNames = {}
    local collectedMeta = {}

    for name, _ in pairs(self.sensors) do
        local data, meta = self:collect(name, false)
        if data ~= nil then
            results[name] = data
            table.insert(collectedNames, name)
            collectedMeta[name] = meta
        end
    end

    -- Include force-pending sensors (force-collected this frame)
    -- These have cached data but throttle may have blocked them
    for name, _ in pairs(self.forcePending) do
        local cached = self.cache[name]
        if cached ~= nil and results[name] == nil then
            results[name] = cached
            table.insert(collectedNames, name)
            collectedMeta[name] = self.lastCollect[name] or {
                collect_frame = State.updateCount,
                collect_time = Isaac.GetTime(),
                interval = "forced",
                stale_frames = 0,
                entity_count = (type(cached) == "table" and #cached) or 0,
                hash = self.changeHashes[name] or "",
            }
        end
    end

    -- Clear force-pending for next frame
    self.forcePending = {}

    return results, collectedNames, collectedMeta
end

function SensorRegistry:forceCollectAll()
    local results = {}
    local names = {}
    local meta = {}
    for name, _ in pairs(self.sensors) do
        local data, m = self:collect(name, true)
        if data ~= nil then
            results[name] = data
            table.insert(names, name)
            meta[name] = m
        end
    end
    return results, names, meta
end

function SensorRegistry:getCached(name)
    return self.cache[name]
end

function SensorRegistry:getConfig(name)
    local sensor = self.sensors[name]
    if not sensor then return nil end
    return {
        name = sensor.name,
        enabled = sensor.enabled,
        throttle = sensor.throttle,
        search = sensor.search,
    }
end

function SensorRegistry:getAllConfigs()
    local configs = {}
    for name, _ in pairs(self.sensors) do
        configs[name] = self:getConfig(name)
    end
    return configs
end

function SensorRegistry:setEnabled(name, enabled)
    if self.sensors[name] then
        self.sensors[name].enabled = enabled
    end
end

function SensorRegistry:setThrottle(name, throttleConfig)
    local sensor = self.sensors[name]
    if sensor then
        for k, v in pairs(throttleConfig) do
            sensor.throttle[k] = v
        end
    end
end

-- ============================================================================
-- Sensor Definitions (all 12 sensors ported from v2.x + enhanced)
-- ============================================================================

-- 1. PLAYER_POSITION (HIGH frequency, single entity — no search needed)
SensorRegistry:register("PLAYER_POSITION", {
    name = "PLAYER_POSITION",
    search = { strategy = "callback" },  -- Direct API, no entity search
    throttle = { base_interval = 1, dynamic = false },
    cache = { strategy = "hash" },
    extract = function()  -- Override collect behavior per-sensor
        -- This sensor uses a custom collect function, defined below
    end,
})

-- 2. PLAYER_STATS (LOW frequency)
SensorRegistry:register("PLAYER_STATS", {
    name = "PLAYER_STATS",
    search = { strategy = "callback" },
    throttle = { base_interval = 30, dynamic = false },
    cache = { strategy = "hash" },
})

-- 3. PLAYER_HEALTH (LOW frequency)
SensorRegistry:register("PLAYER_HEALTH", {
    name = "PLAYER_HEALTH",
    search = { strategy = "callback" },
    throttle = { base_interval = 30, dynamic = false },
    cache = { strategy = "hash" },
})

-- 4. PLAYER_INVENTORY (RARE frequency)
SensorRegistry:register("PLAYER_INVENTORY", {
    name = "PLAYER_INVENTORY",
    search = { strategy = "callback" },
    throttle = { base_interval = 90, dynamic = false },
    cache = { strategy = "hash" },
})

-- 5. ENEMIES (HIGH in combat, LOW idle)
SensorRegistry:register("ENEMIES", {
    name = "ENEMIES",
    search = {
        strategy = "partition",
        partitions = EntityPartition.ENEMY,
        sort_by_distance = true,
    },
    throttle = {
        base_interval = 1, dynamic = true,
        combat_interval = 1, idle_interval = 15,
    },
    cache = { strategy = "hash" },
    triggers = { "MC_POST_NPC_DEATH", "MC_POST_NEW_ROOM" },
    extract = function(entity, player)
        if not entity:IsActiveEnemy(false) then
            return nil
        end
        local npc = entity:ToNPC()
        local targetPos = { x = 0, y = 0 }
        if npc then
            local target = npc:GetPlayerTarget()
            if target then targetPos = Helpers.vectorToTable(target.Position) end
        end
        local damageDirections = nil
        if npc and entity.Type == 41 and type(npc.CanBeDamagedFromVelocity) == "function" then
            damageDirections = {}
            for _, direction in ipairs({{-1,-1},{0,-1},{1,-1},{-1,0},{1,0},{-1,1},{0,1},{1,1}}) do
                local ok, allowed = pcall(npc.CanBeDamagedFromVelocity, npc, Vector(direction[1]*10,direction[2]*10))
                if ok and type(allowed) == "boolean" then
                    damageDirections[tostring(direction[1]) .. "," .. tostring(direction[2])] = allowed
                end
            end
            if next(damageDirections) == nil then damageDirections = nil end
        end
        return {
            id = entity.Index,
            instance_id = tostring(entity.InitSeed) .. ":" .. tostring(entity.Index),
            vulnerable = entity:IsVulnerableEnemy(),
            damage_directions = damageDirections,
            can_shut_doors = npc == nil or npc.CanShutDoors,
            collision_class = entity.EntityCollisionClass,
            friendly = entity:HasEntityFlags(EntityFlag.FLAG_FRIENDLY),
            dead = entity:IsDead(),
            type = entity.Type, variant = entity.Variant, subtype = entity.SubType,
            pos = Helpers.vectorToTable(entity.Position),
            vel = Helpers.vectorToTable(entity.Velocity),
            hp = entity.HitPoints, max_hp = entity.MaxHitPoints,
            is_boss = entity:IsBoss(),
            is_champion = npc and npc:IsChampion() or false,
            state = npc and npc.State or 0,
            state_frame = npc and npc.StateFrame or 0,
            projectile_cooldown = npc and npc.ProjectileCooldown or 0,
            projectile_delay = npc and npc.ProjectileDelay or 0,
            collision_radius = entity.Size,
            contact_damage = entity.CollisionDamage,
            champion_color = npc and npc:GetChampionColorIdx() or -1,
            distance = player and player.Position:Distance(entity.Position) or 0,
            target_pos = targetPos,
            v1 = npc and Helpers.vectorToTable(npc.V1) or { x = 0, y = 0 },
            v2 = npc and Helpers.vectorToTable(npc.V2) or { x = 0, y = 0 },
        }
    end,
})

-- 6. PROJECTILES (HIGH in combat, LOW idle)
SensorRegistry:register("PROJECTILES", {
    name = "PROJECTILES",
    search = {
        strategy = "partition",
        partitions = EntityPartition.BULLET + EntityPartition.EFFECT,
    },
    throttle = {
        base_interval = 1, dynamic = true,
        combat_interval = 1, idle_interval = 15,
    },
    cache = { strategy = "hash" },
    triggers = { "MC_POST_NEW_ROOM" },
    extract = function(entity, player)
        -- We override this with a custom collect — see custom sensor handlers below
    end,
})

-- 7. ROOM_INFO (on room change + LOW)
SensorRegistry:register("ROOM_INFO", {
    name = "ROOM_INFO",
    search = { strategy = "callback" },
    throttle = { base_interval = 15, dynamic = false },
    cache = { strategy = "hash" },
    triggers = { "MC_POST_NEW_ROOM" },
})

-- 8. ROOM_LAYOUT (on room change)
SensorRegistry:register("ROOM_LAYOUT", {
    name = "ROOM_LAYOUT",
    search = { strategy = "callback" },
    throttle = { base_interval = -1, dynamic = false },  -- Callback-only
    cache = { strategy = "snapshot" },
    triggers = { "MC_POST_NEW_ROOM" },
})

-- 9. BOMBS (LOW frequency)
SensorRegistry:register("BOMBS", {
    name = "BOMBS",
    search = {
        strategy = "partition",
        type_filter = EntityType.ENTITY_BOMB,
    },
    throttle = { base_interval = 15, dynamic = false },
    cache = { strategy = "hash" },
    extract = function(entity, player)
        if entity.Type ~= EntityType.ENTITY_BOMB then return nil end
        local bomb = entity:ToBomb()
        local dist = player and player.Position:Distance(entity.Position) or 0
        local BOMB_VARIANTS = {
            [0]="NORMAL",[1]="BIG",[2]="DECOY",[3]="TROLL",[4]="MEGA_TROLL",
            [5]="POISON",[6]="BIG_POISON",[7]="SAD",[8]="HOT",[9]="BUTT",
            [10]="MR_MEGA",[11]="BOBBY",[12]="GLITTER",[13]="THROWABLE",
            [14]="SMALL",[15]="BRIMSTONE",[16]="BLOODY_SAD",[17]="GIGA",
            [18]="GOLDEN_TROLL",[19]="ROCKET",[20]="GIGA_ROCKET",
        }
        return {
            id = entity.Index, type = entity.Type,
            instance_id = tostring(entity.InitSeed) .. ":" .. tostring(entity.Index),
            owner_type = entity.SpawnerType,
            owner_instance_id = entity.SpawnerEntity and (tostring(entity.SpawnerEntity.InitSeed) .. ":" .. tostring(entity.SpawnerEntity.Index)),
            variant = entity.Variant,
            variant_name = BOMB_VARIANTS[entity.Variant] or ("UNKNOWN_" .. entity.Variant),
            sub_type = entity.SubType,
            pos = Helpers.vectorToTable(entity.Position),
            vel = Helpers.vectorToTable(entity.Velocity),
            -- Vanilla Repentance exposes RadiusMultiplier, not ExplosionRadius
            -- or Timer. Treat an unknown fuse as imminent, never as harmless.
            explosion_radius = 100 * (bomb and bomb.RadiusMultiplier or 1),
            radius_estimated = true, timer = 0, timer_known = false,
            distance = dist,
        }
    end,
})

-- 10. INTERACTABLES (LOW frequency)
SensorRegistry:register("INTERACTABLES", {
    name = "INTERACTABLES",
    search = {
        strategy = "partition",
        type_filter = 6,  -- EntityType 6 = interactable entities
    },
    throttle = { base_interval = 15, dynamic = false },
    cache = { strategy = "hash" },
    extract = function(entity, player)
        if entity.Type ~= 6 then return nil end
        local npc = entity:ToNPC()
        local dist = player and player.Position:Distance(entity.Position) or 0
        local INTERACTABLE_VARIANTS = {
            [1]="SLOT_MACHINE",[2]="BLOOD_DONATION",[3]="FORTUNE_TELLING",
            [4]="BEGGAR",[5]="DEVIL_BEGGAR",[6]="SHELL_GAME",
            [7]="KEY_MASTER",[8]="DONATION_MACHINE",[9]="BOMB_BUM",
            [10]="RESTOCK_MACHINE",[11]="GREED_MACHINE",[12]="MOMS_DRESSING_TABLE",
            [13]="BATTERY_BUM",[14]="ISAAC_SECRET",[15]="HELL_GAME",
            [16]="CRANE_GAME",[17]="CONFESSIONAL",[18]="ROTTEN_BEGGAR",
            [19]="REVIVE_MACHINE",
        }
        local target = npc and npc:GetPlayerTarget()
        return {
            id = entity.Index, type = entity.Type,
            variant = entity.Variant,
            variant_name = INTERACTABLE_VARIANTS[entity.Variant] or ("UNKNOWN_" .. entity.Variant),
            sub_type = entity.SubType,
            pos = Helpers.vectorToTable(entity.Position),
            vel = Helpers.vectorToTable(entity.Velocity),
            state = npc and npc.State or 0,
            state_frame = npc and npc.StateFrame or 0,
            target_pos = target and Helpers.vectorToTable(target.Position) or { x = 0, y = 0 },
            distance = dist,
        }
    end,
})

-- 11. PICKUPS (LOW frequency + callback on pickup init)
SensorRegistry:register("PICKUPS", {
    name = "PICKUPS",
    search = {
        strategy = "partition",
        partitions = EntityPartition.PICKUP,
    },
    throttle = { base_interval = 15, dynamic = false },
    cache = { strategy = "hash" },
    triggers = { "MC_POST_PICKUP_INIT", "MC_POST_NEW_ROOM" },
    extract = function(entity, player)
        if entity.Type ~= EntityType.ENTITY_PICKUP then return nil end
        return Helpers.pickupObservation(entity, player)
    end,
})

-- 12. FIRE_HAZARDS (LOW frequency)
SensorRegistry:register("FIRE_HAZARDS", {
    name = "FIRE_HAZARDS",
    search = { strategy = "hybrid" },
    throttle = { base_interval = 15, dynamic = false },
    cache = { strategy = "hash" },
    extract = function(entity, player)
        local dist = player and player.Position:Distance(entity.Position) or 0
        -- Handle fire effects (bomb fire, candle flame)
        local DANGEROUS_FIRE = { [51] = true, [52] = true }
        local FIREPLACE_TYPES = {
            [0]="NORMAL",[1]="RED",[2]="BLUE",[3]="PURPLE",[4]="WHITE",
            [10]="MOVABLE",[11]="COAL",[12]="MOVABLE_BLUE",[13]="MOVABLE_PURPLE",
        }
        if entity.Type == EntityType.ENTITY_EFFECT then
            local WAVES = {[61]=true,[67]=true,[72]=true,[73]=true,[150]=true,[151]=true,[187]=true}
            if WAVES[entity.Variant] and entity.SpawnerType ~= EntityType.ENTITY_PLAYER
                and entity.SpawnerType ~= EntityType.ENTITY_FAMILIAR then
                local effect = entity:ToEffect()
                return {id=entity.Index,instance_id=tostring(entity.InitSeed)..":"..tostring(entity.Index),
                    type="EFFECT",variant=entity.Variant,owner_type=entity.SpawnerType,
                    pos=Helpers.vectorToTable(entity.Position),vel=Helpers.vectorToTable(entity.Velocity),
                    collision_radius=math.max(16,entity.Size),mechanism="shockwave",
                    entity_frame_count=entity.FrameCount,
                    max_radius=Helpers.optional(function() return effect.MaxRadius end),
                    min_radius=Helpers.optional(function() return effect.MinRadius end),
                    remaining_ticks=effect and effect.Timeout > 0 and effect.Timeout or 30}
            end
            local CREEP = {[22]=true,[23]=true,[24]=true,[25]=true,[26]=true,[56]=true}
            local hostileCreep = CREEP[entity.Variant] and entity.SpawnerType ~= EntityType.ENTITY_PLAYER
                and entity.SpawnerType ~= EntityType.ENTITY_FAMILIAR
            if DANGEROUS_FIRE[entity.Variant] or hostileCreep then
                local effect = entity:ToEffect()
                return {
                    id = entity.Index, type = "EFFECT", variant = entity.Variant,
                    pos = Helpers.vectorToTable(entity.Position),
                    collision_radius = entity.Size > 0 and entity.Size or 20,
                    distance = dist,
                    mechanism = hostileCreep and "creep_or_slowing_surface" or "fire",
                    remaining_ticks = effect and effect.Timeout > 0 and effect.Timeout or 45,
                }
            end
        elseif entity.Type == 33 then  -- ENTITY_FIREPLACE
            local variant = entity.Variant
            local npc = entity:ToNPC()
            local animation = entity:GetSprite():GetAnimation()
            -- J835 retains burnt fireplaces at HP 1 / State 3, collision 0,
            -- with a Dissapear* animation. They are not dead NPCs. Keeping
            -- these as live hazards seals corridors which the engine opened.
            local isExtinguished = npc and (npc.State == 1000 or
                (npc.State == 3 and entity.HitPoints <= 1 and
                 entity.EntityCollisionClass == 0 and animation:match("^Dissapear") ~= nil))
            local isShooting = false
            if npc and (variant == 1 or variant == 3) then
                -- State 8 also persists during ordinary Flickering*. The
                -- shipped red/purple fire sprites have a separate Shoot
                -- animation; state alone would forbid every clear boundary.
                isShooting = animation:match("^Shoot") ~= nil
            end
            return {
                id = entity.Index, type = "FIREPLACE",
                fireplace_type = FIREPLACE_TYPES[variant] or ("UNKNOWN_" .. variant),
                variant = variant, sub_variant = entity.SubType,
                pos = Helpers.vectorToTable(entity.Position),
                hp = entity.HitPoints, max_hp = entity.MaxHitPoints,
                state = npc and npc.State or 0, is_extinguished = isExtinguished,
                animation = animation, collision_class = entity.EntityCollisionClass,
                collision_radius = entity.Size > 0 and entity.Size or 25,
                distance = dist, is_shooting = isShooting,
                sprite_scale = entity.SpriteScale.X,
            }
        end
        return nil
    end,
})

-- ═══════════════════════════════════════════════════════════════════════
-- Custom sensor collect functions (override search-based collection)
-- These sensors don't use entity search — they query the API directly.
-- ═══════════════════════════════════════════════════════════════════════

local CustomCollectors = {}

-- Optional observations preserve absence instead of inventing a false/zero.
function Helpers.optional(fn)
    local ok, value = pcall(fn)
    if ok then return value end
    return nil
end

function Helpers.weaponObservation(player)
    local result = {status="known", types={}, unknowns={}}
    local seen, checked = {}, 0
    for name, id in pairs(WeaponType) do
        if type(name) == "string" and name:match("^WEAPON_") and type(id) == "number" and not seen[id] then
            seen[id] = true
            local value = Helpers.optional(function() return player:HasWeaponType(id) end)
            checked = checked+1
            if value == true then table.insert(result.types, id)
            elseif value ~= false then result.status="partial"; table.insert(result.unknowns, name) end
        end
    end
    if checked == 0 then result.status="unknown"; table.insert(result.unknowns, "weapon_enum_unavailable") end
    table.sort(result.types)
    return result
end

function Helpers.effectObservation(player)
    local result = {status="known", items={}, unknowns={}}
    local list = Helpers.optional(function() return player:GetEffects():GetEffectsList() end)
    local size = list and Helpers.optional(function() return list.Size end)
    if type(size) ~= "number" or size < 0 then
        result.status="unknown"; table.insert(result.unknowns, "effect_list_unavailable"); return result
    end
    if size > 256 then result.status="partial"; table.insert(result.unknowns, "effect_list_truncated") end
    for index=0, math.min(size, 256)-1 do
        local effect = Helpers.optional(function() return list:Get(index) end)
        local record = effect and Helpers.optional(function()
            local item, kind = effect.Item, "unknown"
            if item:IsCollectible() then kind="collectible"
            elseif item:IsTrinket() then kind="trinket"
            elseif item:IsNull() then kind="null" end
            return {kind=kind, item_id=item.ID, count=effect.Count, cooldown=effect.Cooldown}
        end)
        if record then table.insert(result.items, record)
        else result.status="partial"; table.insert(result.unknowns, "effect_entry_unavailable:"..index) end
    end
    return result
end

function Helpers.floorObservation()
    local level = Game():GetLevel()
    local dimension, listIndex
    local desc = Helpers.optional(function() return level:GetCurrentRoomDesc() end)
    if desc then
        listIndex = desc.ListIndex
        for d=0,2 do
            local equal = Helpers.optional(function()
                return GetPtrHash(desc) == GetPtrHash(level:GetRoomByIdx(desc.SafeGridIndex, d))
            end)
            if equal then dimension=d; break end
        end
    end
    local result = {instance_id=State.runId..":floor:"..State.floorGeneration,
        generation=State.floorGeneration, stage=level:GetStage(), stage_type=level:GetStageType(),
        dimension=dimension, room_list_index=listIndex, unknowns={}}
    if dimension == nil then table.insert(result.unknowns, "dimension_unavailable") end
    if listIndex == nil then table.insert(result.unknowns, "room_list_index_unavailable") end
    return result
end

-- Optional floor-control capability. Missing vanilla APIs stay unknown.
function Helpers.optionalBoolean(fn)
    local value = Helpers.optional(fn)
    if type(value) == "boolean" then return value end
    return nil
end
function Helpers.floorProgress(player)
    return {capability_version=1,
        curses=Helpers.optional(function() return Game():GetLevel():GetCurses() end),
        can_pick_red=Helpers.optionalBoolean(function() return player:CanPickRedHearts() end),
        can_pick_soul=Helpers.optionalBoolean(function() return player:CanPickSoulHearts() end),
        controls_enabled=Helpers.optionalBoolean(function() return player.ControlsEnabled end),
        extra_animation_finished=Helpers.optionalBoolean(function() return player:IsExtraAnimationFinished() end)}
end

function Helpers.exitObservations(layout)
    local result = {}
    for key, grid in pairs(layout.grid) do
        if grid.type == 17 or grid.type == 18 or grid.type == 23 then
            table.insert(result, {instance_id=State.runId..":"..State.roomVisit..":grid:"..key,
                grid_index=grid.grid_index, type=grid.type, variant=grid.variant, state=grid.state,
                collision=grid.collision, pos={x=grid.x,y=grid.y}})
        end
    end
    return result
end

function Helpers.pickupObservation(entity, player)
    local pickup = entity:ToPickup()
    local visibility, reason, subtype = "visible", "visible_variant", entity.SubType
    if entity.Variant == 100 then
        visibility, reason = "unknown", "appearance_not_verified"
        local visible = Helpers.optional(function()
            local level = Game():GetLevel()
            local stage = level:GetStage()
            return player:GetPlayerType() == 0 and stage >= 1 and stage <= 6
                and level:GetStageType() <= 2 and Helpers.floorObservation().dimension == 0
                and (level:GetCurses() & LevelCurse.CURSE_OF_BLIND) == 0 and entity.SubType > 0
        end)
        local blind = Helpers.optional(function() return (Game():GetLevel():GetCurses() & LevelCurse.CURSE_OF_BLIND) ~= 0 end)
        if blind then visibility, reason="hidden", "curse_of_blind"
        elseif visible then visibility, reason="visible", "isaac_normal_mainline_no_blind" end
        if visibility ~= "visible" then subtype=nil end
    elseif entity.Variant == 70 then
        visibility, reason="unknown", "pill_color_only"
    end
    return {id=entity.Index, instance_id=State.runId..":"..tostring(entity.InitSeed)..":"..entity.Index,
        variant=entity.Variant, sub_type=subtype, pos=Helpers.vectorToTable(entity.Position),
        is_empty=entity.Variant == 100 and entity.SubType == 0,
        vel=Helpers.vectorToTable(entity.Velocity), collision_radius=entity.Size,
        price=pickup and pickup.Price, shop_item_id=pickup and pickup.ShopItemId,
        wait=pickup and pickup.Wait, options_pickup_index=pickup and pickup.OptionsPickupIndex,
        identity_visibility=visibility, identity_reason=reason}
end

function CustomCollectors.PLAYER_POSITION()
    local players = Helpers.getPlayers()
    local data = {}
    for i, player in ipairs(players) do
        data[i] = {
            pos = Helpers.vectorToTable(player.Position),
            vel = Helpers.vectorToTable(player.Velocity),
            move_dir = player:GetMovementDirection(),
            fire_dir = player:GetFireDirection(),
            head_dir = player:GetHeadDirection(),
            aim_dir = Helpers.vectorToTable(player:GetAimDirection()),
            dead = player:IsDead(),
            collision_radius = player.Size,
        }
    end
    return data
end

function CustomCollectors.PLAYER_STATS()
    local players = Helpers.getPlayers()
    local data = {}
    for i, player in ipairs(players) do
        local weapons = Helpers.weaponObservation(player)
        data[i] = {
            player_type = player:GetPlayerType(),
            damage = player.Damage, speed = player.MoveSpeed,
            tears = player.MaxFireDelay,
            max_fire_delay = player.MaxFireDelay,
            weapon_type = weapons.status == "known" and #weapons.types == 1 and weapons.types[1] == WeaponType.WEAPON_TEARS and 1 or -1,
            weapons = weapons,
            range = player.TearRange, tear_range = player.TearRange,
            shot_speed = player.ShotSpeed, luck = player.Luck,
            tear_height = player.TearHeight,
            tear_falling_speed = player.TearFallingSpeed,
            can_fly = player.CanFly, size = player.Size,
            sprite_scale = player.SpriteScale.X,
        }
    end
    return data
end

function CustomCollectors.PLAYER_HEALTH()
    local players = Helpers.getPlayers()
    local data = {}
    for i, player in ipairs(players) do
        data[i] = {
            red_hearts = player:GetHearts(), max_hearts = player:GetMaxHearts(),
            soul_hearts = player:GetSoulHearts(), black_hearts = player:GetBlackHearts(),
            bone_hearts = player:GetBoneHearts(), golden_hearts = player:GetGoldenHearts(),
            eternal_hearts = player:GetEternalHearts(), rotten_hearts = player:GetRottenHearts(),
            broken_hearts = player:GetBrokenHearts(), extra_lives = player:GetExtraLives(),
        }
    end
    return data
end

function CustomCollectors.PLAYER_INVENTORY()
    local players = Helpers.getPlayers()
    local data = {}
    for i, player in ipairs(players) do
        local playerData = {
            coins = player:GetNumCoins(), bombs = player:GetNumBombs(),
            keys = player:GetNumKeys(),
            trinket_0 = player:GetTrinket(0), trinket_1 = player:GetTrinket(1),
            card_0 = player:GetCard(0), pill_0 = player:GetPill(0),
            collectible_count = player:GetCollectibleCount(),
        }
        -- The catalog is runtime-sized; generated negative IDs are not enumerable
        -- with the original API, so coverage must remain explicit.
        local items = {}
        local catalogMax = Helpers.optional(function() return Isaac.GetItemConfig():GetCollectibles().Size-1 end)
        local coverage = {static_status="known", catalog_max_id=catalogMax, dynamic_status="unknown",
            observed_dynamic_ids={}, unknowns={"dynamic_inventory_not_enumerable_in_original_api"}}
        if type(catalogMax) ~= "number" or catalogMax < 0 then
            coverage.static_status="unknown"; table.insert(coverage.unknowns, "catalog_size_unavailable")
        else
            if catalogMax > 10000 then coverage.static_status="partial"; table.insert(coverage.unknowns,"catalog_scan_truncated") end
            for itemId=1, math.min(catalogMax, 10000) do
                local count = Helpers.optional(function() return player:GetCollectibleNum(itemId, true) end)
                if type(count) ~= "number" then coverage.static_status="partial"
                elseif count > 0 then items[tostring(itemId)] = count end
            end
        end
        playerData.collectibles = items
        -- Ownership is granted after the pickup animation, not on contact.
        playerData.queued_collectible_id = Helpers.optional(function()
            local queued = player.QueuedItem.Item
            return queued and queued.ID or 0
        end)
        playerData.inventory_coverage = coverage
        -- Active items
        local activeSlots = {}
        for slot = 0, 3 do
            local activeItem = player:GetActiveItem(slot)
            if activeItem ~= 0 then
                local config = Helpers.optional(function() return Isaac.GetItemConfig():GetCollectible(activeItem) end)
                activeSlots[tostring(slot)] = {
                    item = activeItem,
                    charge = player:GetActiveCharge(slot),
                    max_charge = config and config.MaxCharges,
                    battery_charge = player:GetBatteryCharge(slot),
                }
                if activeItem < 0 then table.insert(coverage.observed_dynamic_ids, activeItem) end
            end
        end
        playerData.active_items = activeSlots
        data[i] = playerData
    end
    return data
end

function CustomCollectors.PROJECTILES()
    local player = Isaac.GetPlayer(0)
    if not player then return { enemy_projectiles = {}, player_tears = {}, lasers = {}, player_knives = {} } end
    local data = { enemy_projectiles = {}, player_tears = {}, lasers = {}, friendly_tears = {}, player_knives = {} }
    State.knifeObservations = State.knifeObservations or {}
    for _, entity in ipairs(Isaac.GetRoomEntities()) do
        if entity.Type == EntityType.ENTITY_PROJECTILE then
            local proj = entity:ToProjectile()
            table.insert(data.enemy_projectiles, {
                id = entity.Index,
                instance_id = tostring(entity.InitSeed) .. ":" .. tostring(entity.Index),
                type = entity.Type,
                pos = Helpers.vectorToTable(entity.Position),
                vel = Helpers.vectorToTable(entity.Velocity),
                variant = entity.Variant, collision_radius = entity.Size,
                owner_type = entity.SpawnerType,
                owner_seed = entity.SpawnerEntity and entity.SpawnerEntity.InitSeed,
                owner_instance_id = entity.SpawnerEntity and (tostring(entity.SpawnerEntity.InitSeed) .. ":" .. tostring(entity.SpawnerEntity.Index)),
                height = proj and proj.Height or 0,
                falling_speed = proj and proj.FallingSpeed or 0,
                falling_accel = proj and proj.FallingAccel or 0,
            })
        elseif entity.Type == EntityType.ENTITY_TEAR then
            local tear = entity:ToTear()
            local tearData = {
                id = entity.Index,
                instance_id = tostring(entity.InitSeed) .. ":" .. tostring(entity.Index),
                owner_type = entity.SpawnerType,
                owner_seed = entity.SpawnerEntity and entity.SpawnerEntity.InitSeed,
                pos = Helpers.vectorToTable(entity.Position),
                vel = Helpers.vectorToTable(entity.Velocity),
                variant = entity.Variant, collision_radius = entity.Size,
                height = tear and tear.Height or 0,
                scale = tear and tear.Scale or 1,
                collision_damage = Helpers.optional(function() return entity.CollisionDamage end),
                entity_frame_count = entity.FrameCount,
            }
            if entity.SpawnerType == EntityType.ENTITY_PLAYER then
                table.insert(data.player_tears, tearData)
            else
                table.insert(data.friendly_tears, tearData)
            end
        elseif entity.Type == EntityType.ENTITY_KNIFE then
            local knife = entity:ToKnife()
            if knife and (entity.SpawnerType == EntityType.ENTITY_PLAYER or (entity.Parent and entity.Parent.Type == EntityType.ENTITY_PLAYER)) then
                local id=tostring(entity.InitSeed)..":"..tostring(entity.Index)
                local distance=Helpers.optional(function() return knife:GetKnifeDistance() end)
                local flying=Helpers.optional(function() return knife:IsFlying() end)
                local previous=State.knifeObservations[id]
                local delta=nil
                if previous and previous.run_id==State.runId and previous.visit==State.roomVisit and distance and previous.distance then
                    delta=State.updateCount>previous.frame and distance-previous.distance or previous.delta
                end
                State.knifeObservations[id]={run_id=State.runId,visit=State.roomVisit,frame=State.updateCount,distance=distance,delta=delta}
                table.insert(data.player_knives,{id=entity.Index,instance_id=id,type=entity.Type,variant=entity.Variant,
                    owner_type=entity.SpawnerType,pos=Helpers.vectorToTable(entity.Position),vel=Helpers.vectorToTable(entity.Velocity),
                    collision_radius=entity.Size,collision_damage=Helpers.optional(function() return entity.CollisionDamage end),
                    entity_frame_count=entity.FrameCount,charge=Helpers.optional(function() return knife.Charge end),
                    is_flying=flying,distance=distance,distance_change=delta,
                    knife_velocity=Helpers.optional(function() return knife:GetKnifeVelocity() end),
                    max_distance=Helpers.optional(function() return knife.MaxDistance end),
                    rotation=Helpers.optional(function() return knife.Rotation end),
                    phase=flying==false and "held" or (flying==true and delta and (delta>0 and "outbound" or delta<0 and "returning" or "turning") or "unknown")})
            end
        elseif entity.Type == EntityType.ENTITY_LASER then
            local laser = entity:ToLaser()
            if laser then
                table.insert(data.lasers, {
                    id = entity.Index,
                    instance_id = tostring(entity.InitSeed) .. ":" .. tostring(entity.Index),
                    type = entity.Type, variant = entity.Variant,
                    owner_type = entity.SpawnerType,
                    owner_instance_id = entity.SpawnerEntity and (tostring(entity.SpawnerEntity.InitSeed) .. ":" .. tostring(entity.SpawnerEntity.Index)),
                    pos = Helpers.vectorToTable(entity.Position),
                    angle = laser.Angle, max_distance = laser.MaxDistance,
                    is_enemy = not (entity.SpawnerType == EntityType.ENTITY_PLAYER or
                        entity.SpawnerType == EntityType.ENTITY_FAMILIAR),
                    collision_radius = entity.Size,
                })
            end
        end
    end
    return data
end

function CustomCollectors.ROOM_INFO()
    local room = Game():GetRoom()
    local level = Game():GetLevel()
    if not room then return nil end
    local tl = room:GetTopLeftPos()
    local br = room:GetBottomRightPos()
    local roomDesc = level:GetCurrentRoomDesc()
    return {
        room_type = room:GetType(), room_shape = room:GetRoomShape(),
        room_idx = level:GetCurrentRoomIndex(),
        is_starting_room = level:GetCurrentRoomIndex() == level:GetStartingRoomIndex(),
        stage = level:GetStage(), stage_type = level:GetStageType(),
        difficulty = Game().Difficulty,
        is_clear = room:IsClear(), is_first_visit = room:IsFirstVisit(),
        grid_width = room:GetGridWidth(), grid_height = room:GetGridHeight(),
        top_left = Helpers.vectorToTable(tl), bottom_right = Helpers.vectorToTable(br),
        has_boss = room:GetBossID() > 0, enemy_count = room:GetAliveEnemiesCount(),
        room_variant = roomDesc and roomDesc.Data and roomDesc.Data.Variant or 0,
    }
end

function CustomCollectors.ROOM_LAYOUT()
    local room = Game():GetRoom()
    if not room then return nil end
    local grid, doors, cells = {}, {}, {}
    local width = room:GetGridWidth()
    for i = 0, room:GetGridSize() - 1 do
        local cellPos = room:GetGridPosition(i)
        cells[tostring(i)] = {x = cellPos.X, y = cellPos.Y,
            inside = room:IsPositionInRoom(cellPos, 0),
            collision = room:GetGridCollision(i)}
        local gridEntity = room:GetGridEntity(i)
        if gridEntity then
            local gridType = gridEntity:GetType()
            -- Keep all valid vanilla grid types so terrain visualization is complete.
            if gridType >= 0 and gridType <= 27 then
                local pos = room:GetGridPosition(i)
                grid[tostring(i)] = {
                    grid_index = i,
                    type = gridType, variant = gridEntity:GetVariant(),
                    state = gridEntity.State, collision = gridEntity.CollisionClass,
                    x = pos.X, y = pos.Y,
                }
            end
        end
    end
    for slot = 0, DoorSlot.NUM_DOOR_SLOTS - 1 do
        local door = room:GetDoor(slot)
        if door then
            local doorPos = door.Position
            doors[tostring(slot)] = {
                target_room = door.TargetRoomIndex, target_room_type = door.TargetRoomType,
                target_list_index = Helpers.optional(function()
                    local floor = Helpers.floorObservation()
                    if floor.dimension == nil then return nil end
                    return Game():GetLevel():GetRoomByIdx(door.TargetRoomIndex, floor.dimension).ListIndex
                end),
                is_open = door:IsOpen(), is_locked = door:IsLocked(),
                x = doorPos.X, y = doorPos.Y,
            }
        end
    end
    return {
        grid = grid, doors = doors, cells = cells,
        grid_size = room:GetGridSize(), width = width, height = room:GetGridHeight(),
    }
end

-- Override collect method for custom sensors
local origCollect = SensorRegistry.collect
function SensorRegistry:collect(name, forceCollect)
    local sensor = self.sensors[name]
    if not sensor then return nil, nil end
    if not forceCollect and not self:_shouldCollect(sensor) then
        return nil, nil
    end

    -- Check for custom collector
    local customFn = CustomCollectors[name]
    local success, data
    if customFn then
        success, data = pcall(customFn)
    elseif sensor.search.strategy ~= "callback" then
        success, data = pcall(function()
            local player = Isaac.GetPlayer(0)
            local entities = self:_searchEntities(sensor)
            if Config.DEBUG and (name == "PICKUPS" or name == "FIRE_HAZARDS") and State.updateCount % 30 == 1 then
                print("[SB SEARCH] " .. name .. " found=" .. #entities .. " frame=" .. State.updateCount)
            end
            local results = {}
            for _, entity in ipairs(entities) do
                local ok, entry = pcall(sensor.extract, entity, player)
                if ok and entry then table.insert(results, entry) end
            end
            return results
        end)
    else
        return nil, nil  -- Callback-only sensors without customFn
    end

    if not success then
        print("[SB ERR] collect(" .. name .. ") pcall FAILED: " .. tostring(data))
        Isaac.DebugString("[SB ERR] collect(" .. name .. "): " .. tostring(data))
        return nil, nil
    end
    if data == nil then
        if forceCollect then
            print("[SB ERR] collect(" .. name .. ") returned nil (forceCollect)")
        end
        return nil, nil
    end

    -- On force-collect: reset frame counter and mark for immediate send
    if forceCollect then
        self.frameCounters[name] = 0
        self.forcePending[name] = true
    end

    -- Hash-based change detection
    if not forceCollect and sensor.cache.strategy == "hash" then
        local newHash = Helpers.simpleHash(data)
        if self.changeHashes[name] == newHash then
            if Config.DEBUG and name == "PICKUPS" and State.updateCount % 60 == 1 then
                print("[SB HASH] PICKUPS skipped (unchanged) frame=" .. State.updateCount ..
                      " count=" .. #data .. " hash=" .. string.sub(newHash, 1, 20))
            end
            return nil, nil
        end
        if Config.DEBUG and name == "PICKUPS" then
            print("[SB HASH] PICKUPS changed frame=" .. State.updateCount ..
                  " count=" .. #data .. " newHash=" .. string.sub(newHash, 1, 20))
        end
        self.changeHashes[name] = newHash
    end

    self.cache[name] = data
    self.lastCollectFrame[name] = State.updateCount

    local meta = {
        collect_frame = State.updateCount,
        collect_time = Isaac.GetTime(),
        interval = sensor.throttle.dynamic and "dynamic" or "fixed",
        stale_frames = 0,
        entity_count = (type(data) == "table" and #data) or 0,
        hash = self.changeHashes[name] or "",
    }
    self.lastCollect[name] = meta
    return data, meta
end

-- ============================================================================
-- Input Executor (unchanged from v2.x)
-- ============================================================================
InputExecutor = {
    moveDirection = { x = 0, y = 0 },
    shootDirection = { x = 0, y = 0 },
    useItem = false, useBomb = false,
    useCard = false, usePill = false, drop = false,
    edgeFrames = {},
    -- Read-only engine-query evidence, retained across input resets so releases
    -- remain observable. An entry is complete only after all four shoot axes
    -- were queried in one update with the same command/context.
    feedback = {},
}

function InputExecutor.recordShootFeedback(action, active)
    local names = {
        [ButtonAction.ACTION_SHOOTLEFT]="left", [ButtonAction.ACTION_SHOOTRIGHT]="right",
        [ButtonAction.ACTION_SHOOTUP]="up", [ButtonAction.ACTION_SHOOTDOWN]="down",
    }
    local name = names[action]
    if not name then return end
    local rows = InputExecutor.feedback
    local row = rows[#rows]
    local visit = State.runId .. ":" .. tostring(State.roomVisit)
    if not row or row.frame ~= State.updateCount or row.room_visit_id ~= visit then
        row = {frame=State.updateCount, room_visit_id=visit, action_id=InputExecutor.actionId,
            queried={}, values={}, consistent=true}
        table.insert(rows,row)
        if #rows > 32 then table.remove(rows,1) end
    end
    if row.action_id ~= InputExecutor.actionId then row.consistent=false end
    if row.queried[name] and row.values[name] ~= active then row.consistent=false end
    row.queried[name]=true; row.values[name]=active
end

function InputExecutor.applyCommand(command)
    if not command then return end
    if command.move == nil and command.shoot == nil and
       command.use_item == nil and command.use_bomb == nil and
       command.use_card == nil and command.use_pill == nil and
       command.drop == nil then return end
    if command.action_id then
        local visit = State.runId .. ":" .. tostring(State.roomVisit)
        local valid = command.room_visit_id == visit and
            type(command.observed_frame) == "number" and
            State.updateCount - command.observed_frame <= 2 and
            command.observed_frame <= State.updateCount and
            State.controlMode == "FORCE_AI" and not Game():IsPaused()
        Network.send({type = "ACTION_RECEIPT", action_id = command.action_id,
            accepted = valid, frame = State.updateCount, room_visit_id = visit})
        if not valid then InputExecutor.reset(); return end
    end
    local hasInput = false
    if command.move then
        InputExecutor.moveDirection = command.move
        if command.move.x ~= 0 or command.move.y ~= 0 then hasInput = true end
    end
    if command.shoot then
        InputExecutor.shootDirection = command.shoot
        if command.shoot.x ~= 0 or command.shoot.y ~= 0 then hasInput = true end
    end
    if command.use_item ~= nil then InputExecutor.useItem = command.use_item; if command.use_item then hasInput = true end end
    if command.use_bomb ~= nil then InputExecutor.useBomb = command.use_bomb; if command.use_bomb then hasInput = true end end
    if command.use_card ~= nil then InputExecutor.useCard = command.use_card; if command.use_card then hasInput = true end end
    if command.use_pill ~= nil then InputExecutor.usePill = command.use_pill; if command.use_pill then hasInput = true end end
    if command.drop ~= nil then InputExecutor.drop = command.drop; if command.drop then hasInput = true end end
    State.aiActive = hasInput
    InputExecutor.lastInputFrame = State.updateCount
    InputExecutor.actionId = command.action_id
end

function InputExecutor.reset()
    InputExecutor.moveDirection = { x = 0, y = 0 }
    InputExecutor.shootDirection = { x = 0, y = 0 }
    InputExecutor.useItem = false; InputExecutor.useBomb = false
    InputExecutor.useCard = false; InputExecutor.usePill = false
    InputExecutor.drop = false
    InputExecutor.lastInputFrame = nil
    InputExecutor.actionId = nil
    InputExecutor.edgeFrames = {}
    State.aiActive = false
end

local function shouldAIControl()
    local mode = State.controlMode
    if mode == "MANUAL" then
        InputExecutor.reset()
        return false
    elseif mode == "FORCE_AI" then
        return true
    end
    -- AUTO mode
    local room = Game():GetRoom()
    local enemyCount = room and room:GetAliveEnemiesCount() or 0
    if State.aiActive then State.wasInCombat = true end
    if enemyCount > 0 and State.aiActive then return true end
    if enemyCount == 0 and State.wasInCombat then
        InputExecutor.reset()
        State.wasInCombat = false
    end
    return false
end

-- ============================================================================
-- Event System
-- ============================================================================
local EventSystem = { pendingEvents = {} }

function EventSystem.emit(eventType, eventData)
    table.insert(EventSystem.pendingEvents, {
        type = eventType, data = eventData or {},
        frame = State.updateCount,
    })
end

function EventSystem.flush()
    for _, event in ipairs(EventSystem.pendingEvents) do
        local message = Protocol.createEventMessage(event.type, event.data)
        message.frame = event.frame
        Network.send(message)
    end
    EventSystem.pendingEvents = {}
end

-- ============================================================================
-- Command Handler (extended for v3.0)
-- ============================================================================
local CommandHandler = { handlers = {} }

function CommandHandler.register(command, handler)
    CommandHandler.handlers[command] = handler
end

function CommandHandler.process(cmdMessage)
    if not cmdMessage then return nil end
    -- Input commands
    if cmdMessage.move or cmdMessage.shoot or cmdMessage.use_item or cmdMessage.use_bomb then
        return nil  -- Handled by InputExecutor
    end
    -- Named commands
    if cmdMessage.command then
        local handler = CommandHandler.handlers[cmdMessage.command]
        if handler then return handler(cmdMessage.params or {}) end
    end
    -- v3.0 subscription
    if cmdMessage.type == "SUBSCRIBE" then
        return CommandHandler._handleSubscribe(cmdMessage)
    end
    return nil
end

function CommandHandler._handleSubscribe(msg)
    local requested = msg.sensors or {}
    local accepted, rejected = {}, {}
    for _, name in ipairs(requested) do
        if SensorRegistry.sensors[name] then
            table.insert(accepted, name)
            State.subscribedSensors[name] = true
        else
            table.insert(rejected, name)
        end
    end
    -- Apply config overrides
    local overrides = msg.config_overrides or {}
    for name, config in pairs(overrides) do
        if config.throttle then
            SensorRegistry:setThrottle(name, config.throttle)
        end
        if config.enabled ~= nil then
            SensorRegistry:setEnabled(name, config.enabled)
        end
    end
    -- Send ACK
    Network.send({
        version = Protocol.VERSION,
        type = "SUBSCRIBE_ACK",
        accepted = accepted,
        rejected = rejected,
        sensor_configs = SensorRegistry:getAllConfigs(),
    })
    return { success = true, accepted = accepted, rejected = rejected }
end

-- ── Register system commands ──────────────────────────────────────────

CommandHandler.register("SET_CHANNEL", function(params)
    if params.channel and params.enabled ~= nil then
        SensorRegistry:setEnabled(params.channel, params.enabled)
        return { success = true, channel = params.channel, enabled = params.enabled }
    end
    return { success = false, error = "Invalid params" }
end)

CommandHandler.register("SET_INTERVAL", function(params)
    if params.channel and params.interval then
        SensorRegistry:setThrottle(params.channel, { base_interval = params.interval })
        return { success = true }
    end
    return { success = false, error = "Invalid params" }
end)

CommandHandler.register("CONFIGURE_SENSOR", function(params)
    local sensor = params.sensor
    if not sensor or not SensorRegistry.sensors[sensor] then
        return { success = false, error = "Unknown sensor: " .. (sensor or "nil") }
    end
    if params.enabled ~= nil then
        SensorRegistry:setEnabled(sensor, params.enabled)
    end
    if params.throttle then
        SensorRegistry:setThrottle(sensor, params.throttle)
    end
    return { success = true, config = SensorRegistry:getConfig(sensor) }
end)

CommandHandler.register("LIST_SENSORS", function()
    local names = {}
    for name, _ in pairs(SensorRegistry.sensors) do table.insert(names, name) end
    return { success = true, sensors = names }
end)

CommandHandler.register("GET_SENSOR_CONFIG", function(params)
    local config = SensorRegistry:getConfig(params.sensor)
    if config then return { success = true, config = config }
    else return { success = false, error = "Unknown sensor" } end
end)

CommandHandler.register("SUBSCRIBE_SENSORS", function(params)
    local sensors = params.sensors or {}
    for _, name in ipairs(sensors) do
        State.subscribedSensors[name] = true
    end
    return { success = true, subscribed = sensors }
end)

CommandHandler.register("UNSUBSCRIBE_SENSORS", function(params)
    local sensors = params.sensors or {}
    for _, name in ipairs(sensors) do
        State.subscribedSensors[name] = nil
    end
    return { success = true, unsubscribed = sensors }
end)

CommandHandler.register("GET_FULL_STATE", function()
    local fullState, channels, meta = SensorRegistry:forceCollectAll()
    Network.send(Protocol.createFullStateMessage(fullState, channels, meta))
    return { success = true }
end)

CommandHandler.register("GET_CONFIG", function()
    return { success = true, config = SensorRegistry:getAllConfigs() }
end)

CommandHandler.register("SET_MANUAL", function(params)
    if params.enabled ~= nil then
        State.controlMode = params.enabled and "MANUAL" or "AUTO"
        InputExecutor.reset()
        return { success = true, mode = State.controlMode }
    end
    return { success = false, error = "Invalid params" }
end)

CommandHandler.register("SET_FORCE_AI", function(params)
    if params.enabled ~= nil then
        State.controlMode = params.enabled and "FORCE_AI" or "AUTO"
        return { success = true, mode = State.controlMode }
    end
    return { success = false, error = "Invalid params" }
end)

CommandHandler.register("SET_CONTROL_MODE", function(params)
    local mode = params.mode
    if mode and (mode == "MANUAL" or mode == "AUTO" or mode == "FORCE_AI") then
        State.controlMode = mode
        InputExecutor.reset()
        return { success = true, mode = mode }
    end
    return { success = false, error = "Invalid mode. Use: MANUAL, AUTO, or FORCE_AI" }
end)

CommandHandler.register("GET_CONTROL_MODE", function()
    return { success = true, mode = State.controlMode }
end)

-- Pure formatting for precommitted evaluation seeds. No game RNG is advanced.
CommandHandler.register("FORMAT_SEEDS", function(params)
    if type(params.values) ~= "table" or #params.values < 1 or #params.values > 100 then
        return {success=false, error="Expected 1..100 uint32 seeds"}
    end
    local seeds = {}
    for _, value in ipairs(params.values) do
        if type(value) ~= "number" or value < 1 or value > 4294967295 or value ~= math.floor(value) then
            return {success=false, error="Expected nonzero uint32"}
        end
        table.insert(seeds, Seeds.Seed2String(value))
    end
    return {success=true, seeds=seeds}
end)

-- Explicit runner-only fixture. Never offered as a policy/planner action.
CommandHandler.register("PREPARE_ANGEL_TRIP_PROBE", function(params)
    if params.confirm ~= "diagnostic_initialization" then return {success=false,error="Diagnostic confirmation required"} end
    local game, player = Game(), Isaac.GetPlayer(0)
    local room, level = game:GetRoom(), game:GetLevel()
    if params.action == "enter_boss" then
        if not room:IsClear() or level:GetCurrentRoomIndex() ~= level:GetStartingRoomIndex() then
            return {success=false,error="Boss fixture requires clear floor start"}
        end
        local rooms = level:GetRooms()
        for i=0,rooms.Size-1 do
            local descriptor = rooms:Get(i)
            if descriptor.Data and descriptor.Data.Type == 5 then
                local index = descriptor.SafeGridIndex
                game:StartRoomTransition(index,Direction.NO_DIRECTION,RoomTransitionAnim.TELEPORT,player)
                return {success=true,source_mode="diagnostic_initialization",target_room=index}
            end
        end
        return {success=false,error="No normal-map boss room"}
    elseif params.action == "spawn_angel_door" then
        if not room:IsClear() or room:GetType() ~= 5 then return {success=false,error="Clear boss required"} end
        for slot=0,7 do
            local door=room:GetDoor(slot)
            if door and door.TargetRoomType == 15 then return {success=true,existing=true} end
            if door and door.TargetRoomType == 14 then room:RemoveDoor(slot) end
        end
        -- Grant only during initialization of the door; no unsupported item
        -- remains in the measured inventory or combat controller.
        player:AddCollectible(499,0,false)
        local spawned=room:TrySpawnDevilRoomDoor(true,true)
        player:RemoveCollectible(499)
        return {success=true,source_mode="diagnostic_initialization",spawned=spawned}
    end
    return {success=false,error="Unknown angel trip fixture"}
end)

CommandHandler.register("PREPARE_ECONOMY_PROBE", function(params)
    local room = Game():GetRoom()
    if params.confirm ~= "diagnostic_initialization" or not room:IsClear() or
       (room:GetType() ~= 2 and room:GetType() ~= 15) then
        return {success=false,error="Economy fixture requires clear shop/angel room"}
    end
    local mode = params.mode
    if mode ~= "shop_purchase" and mode ~= "shop_health" and mode ~= "angel_choice" then
        return {success=false,error="Unknown economy fixture"}
    end
    if (mode == "angel_choice") ~= (room:GetType() == 15) then
        return {success=false,error="Economy fixture room mismatch"}
    end
    for _, entity in ipairs(Isaac.GetRoomEntities()) do
        if entity.Type == EntityType.ENTITY_PICKUP then entity:Remove() end
    end
    local player = Isaac.GetPlayer(0)
    player:AddCoins(15-player:GetNumCoins())
    if mode == "shop_health" then player:AddHearts(2-player:GetHearts()) end
    local stock = mode == "shop_purchase" and {{100,227,15,0}} or
        mode == "shop_health" and {{10,3,5,0},{100,260,7,0}} or
        {{100,182,0,77},{100,185,0,77},{100,407,0,77}}
    for i, row in ipairs(stock) do
        local pickup = Isaac.Spawn(5,row[1],row[2],Vector(200+i*80,280),Vector(0,0),nil):ToPickup()
        pickup.Price=row[3]; pickup.AutoUpdatePrice=false; pickup.ShopItemId=i
        pickup.OptionsPickupIndex=row[4]
    end
    player.Position=Vector(320,360); player.Velocity=Vector(0,0)
    return {success=true,source_mode="diagnostic_initialization",mode=mode,count=#stock}
end)

CommandHandler.register("PREPARE_TERRAIN_PROBE", function(params)
    local room = Game():GetRoom()
    if params.confirm ~= "diagnostic_initialization" or not room:IsClear() or
       type(params.x) ~= "number" or type(params.y) ~= "number" then
        return {success=false, error="Terrain fixture requires an empty clear room"}
    end
    local point = Vector(params.x,params.y)
    if not room:IsPositionInRoom(point,20) or room:GetGridCollisionAtPos(point) ~= 0 then
        return {success=false,error="Invalid terrain fixture position"}
    end
    local player = Isaac.GetPlayer(0)
    player.Position, player.Velocity = point, Vector(0,0)
    return {success=true,source_mode="diagnostic_initialization",x=params.x,y=params.y}
end)

CommandHandler.register("PREPARE_HOST_PROBE", function(params)
    local room = Game():GetRoom()
    local descriptor = Game():GetLevel():GetCurrentRoomDesc()
    if params.confirm ~= "diagnostic_initialization" or not descriptor.Data or descriptor.Data.Variant ~= 114 then
        return {success=false, error="Host fixture requires diagnostic room 114"}
    end
    for _, entity in ipairs(Isaac.GetRoomEntities()) do
        if entity:ToNPC() or entity.Type == EntityType.ENTITY_PROJECTILE then entity:Remove() end
    end
    for _, point in ipairs({{120,160},{516,156},{120,400}}) do
        Isaac.Spawn(27,0,0,Vector(point[1],point[2]),Vector(0,0),nil)
    end
    local player = Isaac.GetPlayer(0)
    player.Position = Vector(467,209)
    player.Velocity = Vector(0,0)
    room:SetClear(false)
    return {success=true, source_mode="diagnostic_initialization", hosts=3}
end)

CommandHandler.register("EXEC_CONSOLE", function(params)
    if params.command then
        local success, result = pcall(function() return Isaac.ExecuteCommand(params.command) end)
        if success then
            return { success = true, command = params.command, result = result or "" }
        else
            return { success = false, error = result, command = params.command }
        end
    end
    return { success = false, error = "No command provided" }
end)

-- Runner-only initialization, never exposed as a planner tool. An ordinary
-- console restart preserves a previously entered custom seed; explicitly clear
-- it for random runs and discard Easter-egg seed effects before restarting.
CommandHandler.register("PREPARE_RUN", function(params)
    local seed = params.seed or ""
    if type(seed) ~= "string" or (seed ~= "" and not Seeds.IsStringValidSeed(seed)) then
        return {success=false, error="Invalid seed"}
    end
    InputExecutor.reset()
    State.controlMode = "MANUAL"
    local seeds = Game():GetSeeds()
    seeds:ClearSeedEffects()
    seeds:SetStartSeed(seed)
    Isaac.ExecuteCommand("restart 0")
    return {success=true, seed=seed, character=0}
end)

-- ============================================================================
-- Mod Callbacks
-- ============================================================================

-- Atomic, opt-in combat observation. No hash suppression: an unchanged value
-- is still a fresh observation. All fields share one game update counter.
SensorRegistry:register("COMBAT_FRAME", {
    name = "COMBAT_FRAME", search = {strategy = "callback"},
    enabled = false,
    throttle = {base_interval = 1, dynamic = false}, cache = {strategy = "snapshot"},
})

function CustomCollectors.COMBAT_FRAME()
    local player = Isaac.GetPlayer(0)
    if not player then return nil end
    local enemies, bombs, hazards, pickups = {}, {}, {}, {}
    for _, e in ipairs(Isaac.GetRoomEntities()) do
        if e.Type == EntityType.ENTITY_PICKUP then table.insert(pickups, Helpers.pickupObservation(e, player)) end
        if e:ToNPC() and e:IsActiveEnemy(false) then
            local value = SensorRegistry.sensors.ENEMIES.extract(e, player)
            if value then table.insert(enemies, value) end
        end
        if e.Type == EntityType.ENTITY_BOMB then
            table.insert(bombs, SensorRegistry.sensors.BOMBS.extract(e, player))
        end
        local hazard = SensorRegistry.sensors.FIRE_HAZARDS.extract(e, player)
        if hazard then table.insert(hazards, hazard) end
    end
    local inherited = {}
    for _, d in ipairs({Vector(1,0), Vector(-1,0), Vector(0,1), Vector(0,-1)}) do
        local ok, value = pcall(function() return player:GetTearMovementInheritance(d) end)
        if ok then inherited[tostring(d.X) .. "," .. tostring(d.Y)] = Helpers.vectorToTable(value) end
    end
    local deaths = {}
    for _, death in ipairs(State.recentDeaths) do
        if State.updateCount - death.frame <= 90 then table.insert(deaths, death) end
    end
    State.recentDeaths = deaths
    local position = CustomCollectors.PLAYER_POSITION()[1]
    local stats = CustomCollectors.PLAYER_STATS()[1]
    local health = CustomCollectors.PLAYER_HEALTH()[1]
    local inventory = CustomCollectors.PLAYER_INVENTORY()[1]
    local healthObservation = {dead=position.dead}
    for name, value in pairs(health) do healthObservation[name]=value end
    local layout = CustomCollectors.ROOM_LAYOUT()
    return {schema_version = 2, run_id = State.runId,
        room_visit_id = State.runId .. ":" .. tostring(State.roomVisit),
        frame = State.updateCount, paused = Game():IsPaused(), mode = State.controlMode,
        room = CustomCollectors.ROOM_INFO(), layout = layout,
        floor_progress=Helpers.floorProgress(player), exit_observations=Helpers.exitObservations(layout),
        player=position, stats=stats, health=health, inventory=inventory,
        weapons=stats.weapons, effects=Helpers.effectObservation(player), floor=Helpers.floorObservation(),
        pickups=pickups, inventory_coverage=inventory.inventory_coverage, health_observation=healthObservation,
        enemies = enemies, projectiles = CustomCollectors.PROJECTILES(), bombs = bombs, hazards = hazards,
        recent_deaths = deaths,
        movement_inheritance = inherited,
        applied_action_id = InputExecutor.appliedActionId,
        applied_frame = InputExecutor.appliedFrame,
        weapon_feedback = {schema_version=1, provenance="engine_input_callback",
            history=InputExecutor.feedback,
            fire_delay=Helpers.optional(function()
                local value=player.FireDelay
                if type(value)=="number" then return value end
            end),
            fire_direction=Helpers.optional(function() return player:GetFireDirection() end)},
        seed = Game():GetSeeds():GetStartSeedString()}
end

local function pumpCommands()
    for _ = 1, 16 do
        local command = Network.receive()
        if not command then break end
        local result = CommandHandler.process(command)
        if result then
            Network.send({type = "CMD", frame = State.updateCount,
                request_id = command.request_id, result = result})
        end
        InputExecutor.applyCommand(command)
    end
end

-- MC_POST_UPDATE — main game loop (30 tps, respects pause)
mod:AddCallback(ModCallbacks.MC_POST_UPDATE, function()
    State.updateCount = State.updateCount + 1
    if InputExecutor.lastInputFrame and
       State.updateCount - InputExecutor.lastInputFrame > Config.INPUT_TTL_UPDATES then
        InputExecutor.reset()
    end

    -- Cooldown timers
    if State.toggleCooldown > 0 then State.toggleCooldown = State.toggleCooldown - 1 end
    if State.modeMessageTimer > 0 then State.modeMessageTimer = State.modeMessageTimer - 1 end

    -- F3 toggle manual/AI mode
    if Input.IsButtonPressed(Keyboard.KEY_F3, 0) and State.toggleCooldown == 0 then
        if State.controlMode == "MANUAL" then
            State.controlMode = "AUTO"
        else
            State.controlMode = "MANUAL"
        end
        State.toggleCooldown = 20
        State.showModeMessage = true
        State.modeMessageTimer = 90
        if State.controlMode == "MANUAL" then InputExecutor.reset() end
    end

    -- ── Connection (always attempt, even without player) ────────────
    if not State.connected then Network.connect() end

    -- ── Debug: periodic data flow report ────────────────────────────
    if Config.DEBUG and State.updateCount % Config.DEBUG_INTERVAL == 1 then
        local sc = State.connected and "YES" or "NO"
        local sub = 0; for _ in pairs(State.subscribedSensors) do sub = sub + 1 end
        local cacheKeys = 0; for _ in pairs(SensorRegistry.cache) do cacheKeys = cacheKeys + 1 end
        print("[SB DEBUG] frame=" .. State.updateCount ..
              " connected=" .. sc ..
              " room=" .. State.currentRoom ..
              " subscribed=" .. sub ..
              " cached=" .. cacheKeys ..
              " sent=" .. State.messageSeq ..
              " prevSent=" .. State.prevFrameSent)
    end

    -- ── Data collection (only when player exists) ────────────────────
    local player = Isaac.GetPlayer(0)
    if not player then
        -- Still receive commands even without player (for console, etc.)
        if State.connected then
            local command = Network.receive()
            if command then
                local result = CommandHandler.process(command)
                if result then
                    Network.send({
                        version = Protocol.VERSION,
                        type = Protocol.MessageType.COMMAND,
                        frame = State.updateCount,
                        result = result,
                    })
                end
                InputExecutor.applyCommand(command)
            end
        end
        return
    end

    -- Room change detection
    local currentRoom = Game():GetLevel():GetCurrentRoomIndex()
    local listIndex = Helpers.optional(function() return Game():GetLevel():GetCurrentRoomDesc().ListIndex end)
    local roomIdentity = State.floorGeneration..":"..State.roomEntryGeneration..":"..tostring(listIndex)..":"..currentRoom
    if currentRoom ~= State.currentRoom or roomIdentity ~= State.currentRoomIdentity then
        local prevRoom = State.currentRoom

        -- Fire sensor triggers
        SensorTriggers:fire("MC_POST_NEW_ROOM", nil,
            function(name) SensorRegistry:collect(name, true) end)

        -- Force-collect room data
        SensorRegistry:collect("ROOM_INFO", true)
        local rlData, _ = SensorRegistry:collect("ROOM_LAYOUT", true)
        SensorRegistry:collect("PICKUPS", true)

        -- Only commit room change if ROOM_LAYOUT succeeded (GetRoom() may be nil during transition)
        if rlData ~= nil then
            InputExecutor.reset()
            State.controlMode = "MANUAL"
            State.currentRoom = currentRoom
            State.currentRoomIdentity = roomIdentity
            State.roomVisit = State.roomVisit + 1
            State.recentDeaths = {}
            State.roomEntered = true

            if Config.DEBUG then
                print("[SB ROOM] Room change: " .. tostring(prevRoom) ..
                      " -> " .. tostring(currentRoom) .. " frame=" .. State.updateCount)
            end

            EventSystem.emit("ROOM_ENTER", {
                room_index = currentRoom,
                room_info = SensorRegistry:getCached("ROOM_INFO"),
                room_layout = SensorRegistry:getCached("ROOM_LAYOUT"),
            })
        elseif Config.DEBUG then
            print("[SB ROOM] Room change DEFERRED: " .. tostring(prevRoom) ..
                  " -> " .. tostring(currentRoom) .. " (ROOM_LAYOUT collect failed, retrying next frame)")
        end
    end

    -- Collect and send
    if State.connected then
        local data, channels, meta = SensorRegistry:collectAll()
        if next(data) then
            local msg = Protocol.createDataMessage(data, channels, meta)

            -- Dump raw payload keys when PICKUPS/FIRE is present
            if Config.DEBUG and (data["PICKUPS"] or data["FIRE_HAZARDS"]) then
                local pKey = data["PICKUPS"] and "YES" or "NO"
                local fKey = data["FIRE_HAZARDS"] and "YES" or "NO"
                local pData = msg.payload["PICKUPS"]
                local fData = msg.payload["FIRE_HAZARDS"]
                local pType = type(pData)
                local pLen = (pType == "table" and #pData) or "nil"
                local fType = type(fData)
                local fLen = (fType == "table" and #fData) or "nil"
                print("[SB PAYLOAD] seq=" .. State.messageSeq + 1 ..
                      " PICKUPS_in_data=" .. pKey .. " PICKUPS_in_payload: type=" .. pType .. " len=" .. tostring(pLen) ..
                      " FIRE_in_data=" .. fKey .. " FIRE_in_payload: type=" .. fType .. " len=" .. tostring(fLen))
            end

            local ok = Network.send(msg)

            -- Targeted trace: log whenever PICKUPS, FIRE_HAZARDS, or ROOM_LAYOUT included
            if Config.DEBUG and (data["PICKUPS"] or data["FIRE_HAZARDS"] or data["ROOM_LAYOUT"]) then
                local pCount = (type(data["PICKUPS"]) == "table" and #data["PICKUPS"]) or 0
                local fCount = (type(data["FIRE_HAZARDS"]) == "table" and #data["FIRE_HAZARDS"]) or 0
                local rlPresent = data["ROOM_LAYOUT"] ~= nil
                print("[SB SEND] seq=" .. State.messageSeq + 1 .. " frame=" .. State.updateCount ..
                      " PICKUPS=" .. pCount .. " FIRE=" .. fCount .. " LAYOUT=" .. tostring(rlPresent) ..
                      " channels=[" .. table.concat(channels, ",") .. "]")
                -- Dump a sample pickup for comparison
                if pCount > 0 then
                    local sample = data["PICKUPS"][1]
                    print("[SB SEND]   sample pickup id=" .. tostring(sample.id) ..
                          " variant=" .. tostring(sample.variant) ..
                          " pos=(" .. tostring(sample.pos.x) .. "," .. tostring(sample.pos.y) .. ")")
                end
            end

            -- Periodic summary (only when NO pickups/fire to keep output clean)
            if Config.DEBUG and State.updateCount % Config.DEBUG_INTERVAL == 1
               and not data["PICKUPS"] and not data["FIRE_HAZARDS"] then
                local names = table.concat(channels, ",")
                print("[SB SEND] frame=" .. State.updateCount .. " channels=[" .. names .. "]")
            end
        elseif Config.DEBUG and State.updateCount % Config.DEBUG_INTERVAL == 1 then
            print("[SB SEND] frame=" .. State.updateCount .. " NO DATA")
        end

        -- Flush pending events
        EventSystem.flush()

        -- Receive commands
        pumpCommands()
    end
end)

-- MC_POST_RENDER — render loop (60 tps, ignores pause)
mod:AddCallback(ModCallbacks.MC_POST_RENDER, function()
    State.renderCount = State.renderCount + 1
    if not State.connected then Network.connect() end
    if State.connected and Game():IsPaused() then
        InputExecutor.reset()
        pumpCommands()
        if State.renderCount % 6 == 0 then
            Network.send({type = "HEARTBEAT", paused = true, frame = State.updateCount,
                run_id=State.runId, floor_instance_id=State.runId..":floor:"..State.floorGeneration,
                room_visit_id = State.runId .. ":" .. tostring(State.roomVisit),
                mode = State.controlMode})
        end
    end

    -- Mode indicator
    if State.showModeMessage and State.modeMessageTimer > 0 then
        local alpha = math.min(1.0, State.modeMessageTimer / 30)
        local modeText = State.controlMode .. " MODE (F3)"
        local r, g, b = 1.0, 1.0, 1.0
        if State.controlMode == "MANUAL" then
            r, g, b = 1.0, 1.0, 0.2
        elseif State.controlMode == "FORCE_AI" then
            r, g, b = 0.2, 1.0, 1.0
        end
        Isaac.RenderText(modeText, 50, 20, r, g, b, alpha)
    end

    -- Connection indicator
    local connTxt = State.connected and "●" or "○"
    local connR = State.connected and 0.2 or 0.8
    local connG = State.connected and 1.0 or 0.2
    Isaac.RenderText(connTxt, Isaac.GetScreenWidth() - 20, 5, connR, connG, 0.2, 0.8)
end)

-- MC_INPUT_ACTION — AI input injection
mod:AddCallback(ModCallbacks.MC_INPUT_ACTION, function(_, entity, hook, action)
    if not entity or entity.Type ~= EntityType.ENTITY_PLAYER then return nil end
    if not shouldAIControl() then return nil end

    local function ret(isActive)
        local edge = InputExecutor.edgeFrames[action]
        if isActive and not edge then
            edge = State.updateCount
            InputExecutor.edgeFrames[action] = edge
        elseif not isActive then InputExecutor.edgeFrames[action] = nil end
        if hook == InputHook.IS_ACTION_TRIGGERED then
            return isActive and edge == State.updateCount
        end
        InputExecutor.appliedActionId = InputExecutor.actionId
        InputExecutor.appliedFrame = State.updateCount
        if hook == InputHook.IS_ACTION_PRESSED or hook == InputHook.GET_ACTION_VALUE then
            InputExecutor.recordShootFeedback(action,isActive)
        end
        if hook == InputHook.IS_ACTION_PRESSED then
            return isActive
        end
        if hook == InputHook.GET_ACTION_VALUE then
            return isActive and 1.0 or 0.0
        end
        return nil
    end

    local moveDir = InputExecutor.moveDirection
    local shootDir = InputExecutor.shootDirection

    if action == ButtonAction.ACTION_LEFT then return ret(moveDir.x == -1)
    elseif action == ButtonAction.ACTION_RIGHT then return ret(moveDir.x == 1)
    elseif action == ButtonAction.ACTION_UP then return ret(moveDir.y == -1)
    elseif action == ButtonAction.ACTION_DOWN then return ret(moveDir.y == 1)
    elseif action == ButtonAction.ACTION_SHOOTLEFT then return ret(shootDir.x == -1)
    elseif action == ButtonAction.ACTION_SHOOTRIGHT then return ret(shootDir.x == 1)
    elseif action == ButtonAction.ACTION_SHOOTUP then return ret(shootDir.y == -1)
    elseif action == ButtonAction.ACTION_SHOOTDOWN then return ret(shootDir.y == 1)
    elseif action == ButtonAction.ACTION_ITEM then return ret(InputExecutor.useItem)
    elseif action == ButtonAction.ACTION_BOMB then return ret(InputExecutor.useBomb)
    elseif action == ButtonAction.ACTION_PILLCARD then return ret(InputExecutor.useCard or InputExecutor.usePill)
    elseif action == ButtonAction.ACTION_DROP then return ret(InputExecutor.drop)
    end

    return nil
end)

-- MC_PRE_SPAWN_CLEAN_AWARD — room clear
mod:AddCallback(ModCallbacks.MC_PRE_SPAWN_CLEAN_AWARD, function()
    InputExecutor.reset()
    EventSystem.emit("ROOM_CLEAR", { room_index = State.currentRoom })
end)

-- MC_ENTITY_TAKE_DMG — player damage
mod:AddCallback(ModCallbacks.MC_ENTITY_TAKE_DMG, function(_, entity, amount, flags, source)
    if entity.Type ~= EntityType.ENTITY_PLAYER then
        if source and source.Type == EntityType.ENTITY_KNIFE then
            local ok, origin=pcall(function() return source.Entity end)
            local npc=entity:ToNPC()
            if npc then EventSystem.emit("KNIFE_DAMAGE_ATTEMPT",{
                run_id=State.runId,room_visit_id=State.runId..":"..tostring(State.roomVisit),callback_frame=State.updateCount,
                target_id=tostring(entity.InitSeed)..":"..tostring(entity.Index),target_type=entity.Type,
                source_knife_id=ok and origin and (tostring(origin.InitSeed)..":"..tostring(origin.Index)) or nil,
                hp_before=entity.HitPoints,amount=amount,flags=flags}) end
        end
        return
    end
    local player = entity:ToPlayer()
    local function optional(fn) local ok,value=pcall(fn); if ok then return value end end
    local origin = optional(function() return source.Entity end)
    local sourceEntity = origin and optional(function()
        local owner = origin.SpawnerEntity
        return {instance_id=tostring(origin.InitSeed)..":"..tostring(origin.Index),
            type=origin.Type, variant=origin.Variant, subtype=origin.SubType,
            pos=Helpers.vectorToTable(origin.Position), vel=Helpers.vectorToTable(origin.Velocity),
            collision_radius=origin.Size, owner_type=origin.SpawnerType,
            owner_instance_id=owner and (tostring(owner.InitSeed)..":"..tostring(owner.Index))}
    end)
    State.damageEventSequence = (State.damageEventSequence or 0)+1
    EventSystem.emit("PLAYER_DAMAGE_ATTEMPT", {
        schema_version=2, event_id=State.runId..":damage:"..State.damageEventSequence,
        callback_frame=State.updateCount, run_id=State.runId,
        room_visit_id=State.runId..":"..tostring(State.roomVisit),
        amount = amount, flags = flags,
        source_type = source and source.Type or -1,
        source_entity=sourceEntity,
        source_entity_status=sourceEntity and "observed" or "unavailable",
        hp_before = player:GetHearts() + player:GetSoulHearts(),
    })
end)

-- MC_POST_NPC_DEATH — NPC killed
mod:AddCallback(ModCallbacks.MC_POST_NPC_DEATH, function(_, npc)
    table.insert(State.recentDeaths, {instance_id = tostring(npc.InitSeed) .. ":" .. tostring(npc.Index),
        frame = State.updateCount, type = npc.Type, variant = npc.Variant,
        pos = Helpers.vectorToTable(npc.Position)})
    SensorTriggers:fire("MC_POST_NPC_DEATH", npc,
        function(name) SensorRegistry:collect(name, true) end)
    EventSystem.emit("NPC_DEATH", {
        type = npc.Type, variant = npc.Variant, subtype = npc.SubType,
        pos = Helpers.vectorToTable(npc.Position), is_boss = npc:IsBoss(),
    })
end)

-- MC_POST_PLAYER_DEATH — player death
if ModCallbacks.MC_POST_PLAYER_DEATH then
    mod:AddCallback(ModCallbacks.MC_POST_PLAYER_DEATH, function(_, player)
        EventSystem.emit("PLAYER_DEATH", { player_idx = player:GetPlayerIndex() })
        Network.disconnect()
    end)
end

-- Generation identity must change even when the room index is reused.
-- Special/debug rooms can reuse both room index and pooled descriptor.
-- The engine callback is the authoritative visit boundary, not the index.
mod:AddCallback(ModCallbacks.MC_POST_NEW_ROOM, function()
    State.roomEntryGeneration = State.roomEntryGeneration+1
    InputExecutor.reset()
    -- Preserve the source mode during native boss/floor intro pauses. The
    -- first successful destination layout commits MANUAL with its new visit.
end)

mod:AddCallback(ModCallbacks.MC_POST_NEW_LEVEL, function()
    State.floorGeneration = State.floorGeneration+1
    State.currentRoom = -1
    State.recentDeaths = {}
    InputExecutor.reset()
    State.controlMode = "MANUAL"
end)

-- MC_POST_GAME_STARTED — game start
mod:AddCallback(ModCallbacks.MC_POST_GAME_STARTED, function(_, continued)
    State.updateCount = 0
    Network.lastRetryFrame = State.renderCount - Network.retryInterval
    State.currentRoom = -1
    State.currentRoomIdentity = nil
    State.roomVisit = 0
    State.runId = tostring(Game():GetSeeds():GetStartSeed()) .. ":" .. tostring(Isaac.GetTime())
    State.messageSeq = 0
    State.prevFrameSent = 0
    InputExecutor.reset()

    -- Reset sensor caches
    for name, _ in pairs(SensorRegistry.sensors) do
        SensorRegistry.cache[name] = nil
        SensorRegistry.changeHashes[name] = nil
    end

    EventSystem.emit("GAME_START", { continued = continued })

    if State.connected then
        local fullState, channels, meta = SensorRegistry:forceCollectAll()
        Network.send(Protocol.createFullStateMessage(fullState, channels, meta))
    end
end)

-- MC_PRE_GAME_EXIT — game exit
mod:AddCallback(ModCallbacks.MC_PRE_GAME_EXIT, function(_, shouldSave)
    EventSystem.emit("GAME_END", { reason = shouldSave and "exit_save" or "exit_nosave" })
    EventSystem.flush()
    Network.disconnect()
end)

-- MC_POST_ADD_COLLECTIBLE — item collected
if ModCallbacks.MC_POST_ADD_COLLECTIBLE then
    mod:AddCallback(ModCallbacks.MC_POST_ADD_COLLECTIBLE, function(_, itemId, charge, firstTime, slot, varData, player)
        EventSystem.emit("ITEM_COLLECTED", {
            item_id = itemId, first_time = firstTime, slot = slot,
            player_idx = player:GetPlayerIndex(),
        })
    end)
end

-- MC_POST_PICKUP_INIT — new pickup spawned (v3.0: callback-driven PICKUPS)
mod:AddCallback(ModCallbacks.MC_POST_PICKUP_INIT, function(_, pickup)
    SensorTriggers:fire("MC_POST_PICKUP_INIT", pickup,
        function(name) SensorRegistry:collect(name, true) end)
end)

-- ============================================================================
-- Debug command
-- ============================================================================
mod:AddCallback(ModCallbacks.MC_EXECUTE_CMD, function(_, cmd, params)
    if cmd == "sbdebug" then
        local player = Isaac.GetPlayer(0)
        if player then
            print("[SocketBridge Debug] Coins: " .. player:GetNumCoins() ..
                  ", Bombs: " .. player:GetNumBombs() .. ", Keys: " .. player:GetNumKeys())
            print("[SocketBridge Debug] Collectible Count: " .. player:GetCollectibleCount())
        end
        return true
    end
end)

-- ============================================================================
-- Public API
-- ============================================================================
mod.SensorRegistry = SensorRegistry
mod.EventSystem = EventSystem
mod.Protocol = Protocol
mod.Config = Config
mod.InputExecutor = InputExecutor
mod.CommandHandler = CommandHandler
mod.shouldAIControl = shouldAIControl

print("[SocketBridge] v3.0 loaded — Sensor-based Data Collection Framework")
print("[SocketBridge] Server: " .. Config.HOST .. ":" .. Config.PORT)
print("[SocketBridge] F3: Toggle Manual/AI Mode")
print("[SocketBridge] Console: 'sbdebug' to test inventory API")
