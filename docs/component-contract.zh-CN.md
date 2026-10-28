# 战斗组件合同 v1

当前接缝包括可信 Python 组件、M2-B 有界 AST 时序和 M2-C 常驻 QuickJS AttackModel。受伤采集及审核独立于动作执行；菜刀 114 的短测、边界加载与复用已有证据，模型修订后的局内优化仍未验收。当前完成状态见[进度](progress.zh-CN.md)，长期扩展见[能力设计](item-knowledge-and-skill-evolution.zh-CN.md)。

适用：M2-A 的可信本地组件与 M2-B 的受限时序接缝。代码定义位于 `SocketBridge/python/combat/component_contracts.py`；七槽位与参数仍由 catalog 管理。普通可信组件不提供任意代码隔离；模型程序通过下述 AST 或 QuickJS 接缝运行，均无真实输入或系统 IO 权限。

## 能力与输入

能力声明明确 family、能否移动攻击、能否破坏普通障碍；未声明的可选能力默认不具备。组件只产生计算结果，不直接进行 IO、发按键、消费资源或改写传入快照。武器路由和逐帧观测为独立可选接口。

参数由目录边界检查，实际构筑与所需能力必须匹配。上下文包含 run、floor instance、room visit、build revision、catalog revision；V1 历史记录的楼层身份允许为空。版本不匹配的结果不可执行。

## 输出与预算

`AttackProposal` 保留旧入口，增加可选 `ProposalContext`：观测帧、生效截止帧和版本。可信同步旧模块省略上下文时由宿主绑定当前调用；显式上下文必须匹配，异步模块不能靠省略身份绕过验证。

宿主限制每次攻击候选 1–16 个、路径最多 256 点、坐标绝对值不超过 100000 像素、攻击足迹宽度不超过 4096 像素、等待最多 90 游戏更新。分数与损害必须有限且有界；射击为中立或合法四向整数。目录可进一步收紧等待期限，不能放大宿主上限。

`hit_score` 是 0–1 启发式质量。`expected_damage` 是 `prediction_horizon_frames` 个 game_update 内的累计伤害估计，不是每帧伤害，也不是经统计验证的期望值。普通泪弹默认窗口 30，估计为命中质量×实际单发伤害×窗口/发射间隔；宿主约束窗口为 1–120，并把该累计量接入效用排序。攻击足迹 v1 只表示线段及宽度，复杂形状暂不宣称支持。

## 生命周期与失败

逐帧观测钩子每个上下文/更新只推进一次；`propose` 在多个移动候选下重复调用，必须无动作提交副作用。实际执行通过动作回执与观测确认，构筑变化经过版本事务；不能把预测动作当已执行。

模块状态至少绑定 run、floor instance、room visit、build、catalog、experience revision 与 profile。上下文变化重建实例，过期等待和旧提案无效。经验在安全边界采用，计划与提案必须匹配已采用的经验版本；缓存校准不计为新观测样本。低血量可调整风险偏好，但必须保留兼容武器家族。

异常或非法结果记录组件、方法和原因并隔离该实例；普通泪弹可回退受信任普通泪弹实现。没有兼容发射器时明确停火、保留独立移动/避险并交给有界进展监督，不调用错误家族。输入释放及断线 TTL 始终由宿主和 Lua 执行；释放型武器可能因此攻击，不能宣称无风险。

该边界只约束已返回结果；可信 Python 的死循环仍可能阻塞。M2-B 未可信代码不得直接注册到控制线程。

## 楼层复用与攻击后果

RoomExecutor 兼容原有字符串结果，同时在 results 暴露战斗结果、清房后结果、终态快照、alive/actionable 与错误；FloorDirector 校验 RoomResult 后消费。清房后收集结束重新核对存活、上下文和局部危险，释放/模式恢复或持久化失败不得形成可继续的结果。FloorJudge 独立按观测记录逐 visit 损失，包含跨门进入时的损伤。

GeometryService/JointActionSolver 的 allowed_exits 是宿主专用参数，只允许已授权索引中 type=17、variant=0 的网格通行。FloorJudge 确认当前 Boss 完成并授权具体出口后，ExitExecutor 才传入该参数；LLM 的组件参数不能设置它。授权参与几何缓存身份，不解除其他机关或物品避让。

弹体后果使用独立 ProjectileEffects 注册表，按 owner_type/variant 选择预测模型，输出 ThreatForecast，交统一危险场与运动求解器处理。首个模型依据 J835 捕获轨迹预测无头骑士血弹落地血池，使用标明未精确标定的保守范围；改变这类机制无需修改目标、瞄准、地图或独立判定器。

## M2-B 有界时序程序接缝

`mechanisms/program.py` 实现 `typed_scalar_ast` v1：数值/布尔常量、声明状态引用、固定输入引用、有限算术/比较/条件和 observe 中的 set/if；无模型循环、Python 执行或 IO。SDK 的可执行字段定义由 `sdk_contract()` 提供，模型程序不读取整个快照。AST 最多 512 节点、深度 16、64 KiB；状态最多 16 个有界标量，所有求值共用每游戏帧 12000 燃料。

`TimingRuntime.begin_frame` 从当前房间、完整且一致的引擎输入查询历史逐帧提交 observe，未知区间必须等已确认中性输入重新建立状态。`decide` 只读已提交状态，同帧多候选不重复提交、不重置燃料；决策仅有 mode（0 idle / 1 hold / 2 release）和四向输入。模型收益、权限及等待凭据不直接进入求解器。

`ProgramWeapon` 将时序结果转为已有 `AttackProposal`。DTO 新增 `withdrawal_shoot`，默认 `(0,0)`；蓄力释放被否决时可返回上一次实际保持方向。idle 在已经蓄力时同样按物理释放处理，必须提供实际攻击方向的 footprint；命中估计为零也不能跳过该 footprint 内的死亡后果检查。相同程序与参数、同房间/构筑/目录身份下，`room_and_build` 组件可跨战术 profile 保留状态。

原版字段与观测限度见 [武器观测合同](observation-v2.zh-CN.md)。技能开发在独立进程入口进行；持久化预算、原始模型响应哈希、独立语义检查和实机试用共同约束发布。M2-B AST 在会话启动加载受支持包；M2-C JS 另支持无威胁短测后的暂停边界加载，不能在逐帧候选计算中原地覆盖程序。首例及阶段完成标准见 [M2-B 计划](phase-2b-plan.zh-CN.md)；这些接口本身不证明模型战斗验收通过。

原始模型内容先经过严格解析；语法恢复只在离线开发入口尝试一个多余闭符，必须只有一种可类型化程序，不更改任何值、分支或状态逻辑。记录变换和原始错误，装载时复验；严格 Program 解析器和独立行为检查保持不变。控制进程的技能库路径通过 `CombatSession(skill_store=...)` 显式注入；产品入口传入项目 skills，单元测试和通用会话默认不读取外部库。


## M2-C JavaScript AttackModel

`mechanisms/javascript.py` 与 `js_worker.py` 提供常驻 QuickJS worker。接口为 `init(context) → state`、`observe(context, feedback, state) → state`、`proposeBatch(context, candidates, state) → proposals`；Python 统一准备移动候选和公共数据，每个快照至多一次批量求值，多次查询复用结果。上下文持续存在，显式状态按 run/visit/build 签名重置，攻击方案仍经宿主验证和统一危险检查。

源码最多 64 KiB、JS 内存 32 MiB、栈 512 KiB、单次 JS 时间限额 10 ms；初始化宿主等待最多 5 秒，运行批量等待最多 20 ms。显式状态最多 16 KiB，输入消息最多 256 KiB、输出消息最多 64 KiB。超时或非法结果记录故障并关闭 worker，不能视为完成攻击。当前实机算法是菜刀 114，不能由接缝存在推导所有武器均已适配。

## 持续交战与环境意图

`EngagementRoute` 输出敌人、路点、原因、优先级及可选 `phase/deadline`。Host 的 approach/wait_open/attack 连续管理移动目标，武器组件决定实际保持与释放；敌人开盖不重置交战路线。普通障碍破坏仍经原有武器/几何流程，不能用无效重定位掩盖缺少射击通道。

无怪踏板目标由 `EnvironmentController` 选择，周期尖刺交 `TimedTerrainPuzzle` 做全房时序搜索；解谜输出正常 `ActionFrame`，不增加输入写入者。房间清理不自动使动态地形安全，缓存周期不等于复用旧相位。

攻击提案的 90 更新等待上限保持不变。宿主监督器另接纳带证据及绝对截止帧的 `known_invulnerability`（最多 120 更新）、`terrain_observation/terrain_phase`（最多 600 更新）；它们不由模型自由扩大。没有实际进展时等待额度不能重领，且不能取消阶段/房间总期限。范围与实机边界见[Host/地形证据](evidence/phase-3c-host-terrain-generalization.zh-CN.md)。
