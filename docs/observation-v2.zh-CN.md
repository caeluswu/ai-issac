# COMBAT_FRAME V2 观测合同

适用构建：忏悔原版 `1.7.9b.J835`。Lua 原子采集后输出 `schema_version=2`，Python `WorldSnapshot` 同时支持 V1/V2。SocketBridge 外层传输仍为 3.0，本合同版本与传输版本分开。

## 字段与未知语义

V2 保留 V1 的 room、layout、player、stats、health、inventory、敌人及弹体等字段，并要求下列扩展。旧记录缺少扩展时返回 `None`，不会补成已知空集合；Lua 的空 table 在明确的列表字段规范化为 `[]`。

| 扩展 | 内容 | 限制 |
|---|---|---|
| `pickups` | 局内实体身份、当前 subtype、位置/速度/半径、价格、商店/选择组、等待、`is_empty` 与可见性依据 | 空底座与隐藏身份分开；同一实体轮换时 ID 稳定而 subtype 可变；不是实际拾取确认 |
| `weapons` | HasWeaponType 返回的有效类型集合，known/partial/unknown 与缺口 | 多类型不能压成普通泪弹；不等于已经有对应控制组件 |
| `effects` | 临时效果 kind、item_id、count、cooldown；获取状态和缺口 | 不代表所有随机效果均可穷举；非空效果已做 Lua 合同测试，实机仅核验当前场景 |
| `inventory_coverage` | 运行时静态目录边界、扫描状态、可读的动态主动 ID、不可读范围 | 原版动态物品完整库存/效果保留 unknown，不扩大固定扫描上限冒充完整支持 |
| `health_observation` | 存活标志、红/魂/骨心、各容器与特殊心、额外生命 | 黑心字段是 API 位掩码；健康账本处理空骨心和有界复活等待 |
| `floor` | run 内地图生成实例、stage/type、当前房间 ListIndex、维度及缺口 | 原版通过当前房间描述符指针比较识别维度；不查询未探索房间敌人或奖励 |

所有扩展由同一次同步 Lua 采集产生，不拼接不同传输帧的传感器缓存。V2 缺组、楼层进度不一致、健康副本不一致或武器兼容标志不一致会拒收并记录错误。

静态库存按 `GetCollectibles().Size-1` 读取目录，再用 `GetCollectibleNum(id, true)` 获取真实持有数；不扫描无定义的额外 ID，也不把临时效果计入真实持有数。主动槽保留负 ID，找不到配置时 max_charge 为空。饰品、卡牌/药丸、充能和资源继续由 inventory 提供。

## 可见性

普通物理掉落物保留变体；药丸仅保留外观颜色，不由此揭示实际效果。收藏品身份默认未知，仅对 Isaac、stage 1–6、stage_type 0–2、普通维度 0、无盲目诅咒且正 ID 的当前普通底座按明确范围开放；盲目诅咒为 hidden，其他未验证情形为 unknown，两者均不导出 subtype。旧 PICKUPS 通道复用同一过滤器。

这不是任意角色、替代路线或视觉机制的完整身份识别器。遇到额外遮蔽机制应收紧范围或新增经验证的适配器，不能由“读到了底层 ID”推断允许使用。当前合同不启用隐藏身份辅助开关。

## 上下文与生命周期

`floor.instance_id` 由 run 和地图生成计数构成，`MC_POST_NEW_LEVEL` 更新地图实例并独立释放输入、回到 MANUAL。房间变化结合 MC_POST_NEW_ROOM 引擎事件代数、地图生成序号、当前房间 ListIndex 与网格索引，避免特殊/调试房复用描述符和索引时沿用旧 visit。输入立即清零，原生过渡期间保留源模式，目标布局成功后切 MANUAL。

`ContextRef` 包含可空 floor_instance_id，目录修订可为正整数或非空版本字符串。运行器、规划器与求解器均绑定楼层身份、构筑和经验修订；显式过期提案或缺少当前 V2 楼层身份的计划不可执行。BuildResolver 根据实际观测发布构筑修订，不依赖描述文字加算属性。

`inventory.queued_collectible_id` 为原版 `player.QueuedItem.Item.ID` 的可选读数：0 表示无队列，缺失表示 API 不可用。收藏品接触后底座可能先清空，动画结束才入库；队列只证明待交付，不证明拥有。拾取执行器默认接近预算 90 更新，消费后确认预算 30 更新；明确观察到对应待入库项时确认预算为 90 更新，总循环不超过 180 次。等待期间释放移动射击，仍检查新威胁、失血、换房、断线及事务期限；成功必须有实际库存增量。依据见 [EntityPlayer 队列说明](https://wofsauge.github.io/IsaacDocs/rep/EntityPlayer.html#queueditem)，J835 验证见 [P3–P6 报告](evidence/phase-2a-preparation-validation.zh-CN.md)。

## 验证与复现

本地运行：

```powershell
Set-Location SocketBridge/python
..\..\.venv\Scripts\python.exe -m unittest tests.test_observation_v2 tests.test_combat_lua -q
```

已有模组与源码一致、游戏位于可观察房间时，从项目根目录运行只读探针：

```powershell
.\.venv\Scripts\python.exe scripts/probe_m2_observations.py
```

探针监听本机桥接，将模式恢复 MANUAL，采集快照后断开，输出本地 trace 与 summary；不启动/重开游戏、不控制移动射击、不调用模型。失败和已采样内容保留，不能将超时改为通过。原始产物位于忽略 Git 的 `artifacts/runs/`。

API 依据：[ItemConfig](https://wofsauge.github.io/IsaacDocs/rep/ItemConfig.html)、[WeaponType](https://wofsauge.github.io/IsaacDocs/rep/enums/WeaponType.html)、[TemporaryEffects](https://wofsauge.github.io/IsaacDocs/rep/TemporaryEffects.html)、[RoomDescriptor](https://wofsauge.github.io/IsaacDocs/rep/RoomDescriptor.html)。资料接口与目标 J835 的实际可读范围分别记录，不能将文档说明等同于全机制实测。

## M2-A 可选楼层能力

V2 增加可选 `floor_progress` 与 `exit_observations`。旧 V2 轨迹可继续读取；首层入口必须确认 `floor_progress.capability_version=1`、已知 curses、当前 dimension 和 room_list_index，不把缺失当零。

`floor_progress` 包含 curses、can_pick_red/can_pick_soul、controls_enabled、extra_animation_finished；原版 API 缺失或类型错误保留 null。`exit_observations` 保存当前房间的网格实例身份、grid_index、type/variant/state/collision 和位置；17/18/23 只是基础网格事实，不直接声明合法出口。门新增可选 target_list_index，用当前可见门的目标描述符确认大房归并身份，不读取目标内部实体、物品或 RNG。NPC 的 is_boss 为可空兼容字段；缺失不能用于证明 Boss 已出现。

Boss 入场或下楼可能只收到 paused 心跳。它们只有在门口/已授权出口附近生成对应过渡等待后，才能延长取帧等待至最多 15 秒；动作租约仍按真实观测帧检查，不能靠等待凭证发送旧输入。出处：[EntityPlayer API](https://wofsauge.github.io/IsaacDocs/rep/EntityPlayer.html)、[GridEntity API](https://wofsauge.github.io/IsaacDocs/rep/GridEntity.html)。

## M2-B 可选武器反馈

M2-C 增加可选的伤害来源事件字段和弹体/激光/炸弹实体身份，不提高 COMBAT_FRAME 的必填版本。PLAYER_DAMAGE_ATTEMPT 的 schema_version=2 保存 event_id、callback_frame、run/room 和可空 source_entity，来源实体不可读时明确为 unavailable。外层 frame 保留回调/入队帧，timestamp 仍是发送时刻。伤害尝试不代表实际扣血；归因使用健康账本。新字段有 Lua 桩测试及后续实机轨迹证据；Host 与随机 Roundy 房的实际来源记录见[当前证据](evidence/phase-3c-host-terrain-generalization.zh-CN.md)。记录到来源不等于已确定决策原因；接口与缺口见 [M2-C 计划](phase-2c-plan.zh-CN.md)。

V2 增加可选 `weapon_feedback`，其内部 `schema_version=1`、`provenance=engine_input_callback`。`history` 最多保存 32 个有射击输入查询的更新帧；每条包含 frame、room_visit_id、可空 action_id、queried 与 values 的 left/right/up/down 布尔值，以及 consistent。同帧动作身份或方向值变化时 consistent=false。只有四向查询齐全、值一致、房间身份匹配且帧早于当前快照的条目，才可用于已应用输入反馈；没有查询不能补成零输入。重置保留历史，消费端按房间和帧去重。

反馈证明输入回调提供的值，不能直接证明内部蓄力进度。可选 fire_delay 为原版玩家 FireDelay 数值，fire_direction 为可读时的方向；均不能替代已校准的时序。己方泪弹额外导出 collision_damage 与 entity_frame_count；出生归属仍需实例、owner 与轨迹交叉核对，不直接把实体年龄当成与桥接计数完全等价的时间轴。真实可读性和暂停释放后果见 [机制探针](evidence/phase-2b-mechanism-probe.zh-CN.md)。缺少反馈的旧轨迹仍可读取，但不能用于声称蓄力已确认。


## M2-C 刀实体与地形观测

`projectiles.player_knives` 保存刀实例身份、owner_type、位置/速度、半径、可空 collision_damage/charge、entity_frame_count、is_flying、distance/distance_change，以及可读时的 knife_velocity/max_distance/rotation。`phase` 为 held/outbound/returning/turning/unknown：除 held 外依据实际飞行标记及跨帧距离变化推断，不是引擎内部阶段证明。原版 charge 字段不能单独证明充能进度，运行器可用已确认输入历史和校准估计，保留来源。

周期尖刺直接读取网格 type/variant/state/collision 和游戏更新帧；单帧收回状态、room.is_clear 或踏板触发都不证明未来安全。当前规划器逐实体学习周期，离房后不保留旧相位；未知机制和观测空档按证据不足处理。字段消费及范围见[Host/地形证据](evidence/phase-3c-host-terrain-generalization.zh-CN.md)。

## 方向护甲观测

Knight 家族（41）可选输出 `damage_directions`：八个 `dx,dy` 字符串键对应布尔值，取自只读 `EntityNPC:CanBeDamagedFromVelocity(Vector(dx*10,dy*10))`。查询失败或旧轨迹缺字段时保留未知；`vulnerable=true` 不再等同任意方向都可击伤。此处是当前快照的方向许可，未预测下一帧转身，不是实弹命中的证明。接口依据见 [EntityNPC 文档](https://wofsauge.github.io/IsaacDocs/rep/EntityNPC.html#canbedamagedfromvelocity)。

## 熄灭火焰

`FIREPLACE` 增加可选 `animation` 与 `collision_class`。J835 实机观察到已熄灭红火仍为 HP 1、State 3、碰撞类 0，动画为 `Dissapear*`，实体不标记死亡。采集器用这些联合条件或原有 State 1000 设置 `is_extinguished`；不因 HP 1 或状态 3 单独忽略危险。几何和危险场只在确认熄灭后移除禁区，旧轨迹不会被自动改写。来源及限制见[运营验证](evidence/phase-3c-economy-validation.zh-CN.md)。

红/紫火 `is_shooting` 根据 `Shoot*` 动画判断；State 8 可伴随持续 `Flickering*`，不能单独代表射击。此标记用于短暂安全边界等待，不保证接下来的帧不会发射；实际敌方弹体和火焰近身范围仍由危险场处理。
