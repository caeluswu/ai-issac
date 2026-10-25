# 第一阶段控制器：实现与运行手册

## 运行条件

运行对象为 Windows 上的忏悔原版 v1.7.9b.J835、Isaac、普通难度、第一层初始房间。全解锁存档由运行者准备。仅开启项目的 SocketBridge 模组；游戏以 `--luadebug` 参数启动，使用窗口模式，并设 `PauseOnFocusLost=0`（见任务契约 §3.1）。游戏窗口可以留在后台被其他窗口遮挡，程序可在普通终端里运行；控制器只在入房暂停时把游戏窗口短暂置前发送 Escape，随后把前台还给原窗口。暂停那一刻窗口会抢走前台约 1 秒，此时不要在终端里输入命令。运行期间不要最小化游戏窗口——最小化会中断渲染回调，暂停心跳与暂停确认都会失败。F3 仍会终止自动控制，通信丢失时游戏侧独立释放输入。

控制器从初始房间寻找当前可见、开放、未访问的普通房间门，通过正常移动穿门。进入第一个战斗房后，使用 Escape 打开游戏原生暂停菜单；确认更新计数冻结且暂停心跳持续，再执行 DeepSeek 分析与配置。恢复后只发送移动、四方向射击及显式的资源按键释放状态。

全解锁存档槽 1 由项目方确认，基线元数据见 `configs/task/save-baseline.slot-1.json`。程序校验副本哈希、恢复基线并检查角色、难度、楼层和起始房间；这些检查不等同于自动核验全部成就。武器及库存变化会终止首轮运行，避免把其他构筑的结果混入普通泪弹实验。

## 安装和启动

在项目根目录安装依赖：

```powershell
uv pip install --python .venv\Scripts\python.exe -r SocketBridge\python\requirements-dev.txt
```

`.env.local` 是本地凭据文件，Git 已忽略；格式参考根目录 `.env.example`。必须设置 `DEEPSEEK_API_KEY` 和账户可用的 `DEEPSEEK_MODEL`。默认 API 地址是 `https://api.deepseek.com`。密钥不会写入 manifest、规划请求日志或 Git。

在项目根目录一键启动完整任务：

```powershell
.\run-first-room.ps1
```

等价的 Python 入口不依赖当前目录，也无需调整 PowerShell 执行策略：

```powershell
.\.venv\Scripts\python.exe run_first_room.py
```

运行器先绑定本机 9527 端口，检查凭据、指定版本的可执行文件哈希和其他启用的模组。若项目游戏正在运行，会请求正常关闭，等待退出；多个实例或关闭超时直接报错，不强杀进程。随后备份存档目录内的 `.dat`、原始选项、已安装模组和旧游戏日志，设置窗口模式、后台运行和调试控制台，同步模组，并以游戏目录作为工作目录启动 `isaac-ng.exe --luadebug`。

启动器跳过片头并进入指定存档，使用本次游戏日志确认存档槽。连接建立后，在计时任务前执行有记录的 `PREPARE_RUN`：清除特殊种子效果、明确设置自定义种子或空种子，再执行 `restart 0`。空种子会重新随机，不沿用之前的固定回放。取得新运行标识后，检查 Isaac、普通难度、第一层初始房间，再开始出门。菜单流程针对固定的原版构建；异常菜单、其他难度或不兼容状态会中止。默认存档为 1，可用 `--slot 2` 或 `--slot 3` 指定，并为相应槽准备基线。

仅验证自动启动、实时观测、原生暂停与恢复：

```powershell
.\run-first-room.ps1 --startup-only
```

返回 `STARTUP_READY` 和退出码 0 表示启动探针通过，报告中的清房 `success` 仍为 false。完整任务默认使用随机新局；`--seed "XXXX XXXX"` 用于故障复现，不计为随机泛化样本。`--local-only` 使用本地组件诊断，不调用 DeepSeek。参数均可与 Python 入口搭配使用。

完整 LLM 入口要求存在对应槽的受保护基线。制作基线时，从游戏停止后保存的备份目录冻结副本；该命令记录运行者对全解锁状态的声明，不解析成就：

```powershell
.\.venv\Scripts\python.exe scripts/prepare_save_baseline.py --source-dir 'artifacts/runs/<已结束运行>/backup' --slot 1
```

基线文件只创建一次，已有基线不覆盖。每次启动先备份当前存档，检查受保护副本哈希，再复制该槽的持久存档及可选继续存档；源副本不参与游戏写入。运行结束时释放输入并恢复 `MANUAL`，保留游戏窗口供检查。初始化选项与模组保持已安装状态，不在游戏仍运行时回写旧选项。备份位于本次运行目录的 `backup/`；如需恢复，先正常关闭游戏，再复制相应文件。启动过程会替换该槽当前对局，备份包含原对局数据。

批量验证会先用系统随机数生成器抽取不重复的非零 uint32，通过游戏 `Seeds.Seed2String` 格式化，在第一场战斗前写出完整清单与哈希，然后依序执行所有样本：

```powershell
.\.venv\Scripts\python.exe run_phase1_batch.py --count 10
.\.venv\Scripts\python.exe run_phase1_batch.py --seeds 'artifacts/runs/batch-<时间>/seeds.json' --local-only
```

第二条在同一清单上运行规则上层。`--local-only` 不要求每次都出现 LLM 计划；报告仍如实标记本地配置。批量目录包含 `summary.json`、种子清单、各次完整调试包和子进程控制台输出。模型失败后本地清房与完整 LLM 清房分别计数，不能混算。

需要自行准备游戏、只附加控制器时，从 `SocketBridge/python` 运行：

```powershell
..\..\.venv\Scripts\python.exe -m apps.first_room
```

先启动程序，再在游戏选择 Isaac 普通难度开局；或者让游戏停在尚未离开的初始房间后启动程序。只保留一个游戏实例和一个控制服务。默认等待连接 120 秒，可用 `--ready-timeout` 调整。`--port` 同时需要与 Lua 模组端口一致。

不调用 LLM 的诊断入口：

```powershell
..\..\.venv\Scripts\python.exe -m apps.first_room --local-only
```

本地诊断成功只证明本地控制链路，不代表 LLM 规划验收。生产入口遇到 API 故障会记录降级，并用本地默认组件尝试完成当前房间。退出码为 0 的完整条件是清房成功且入房 LLM 规划成功；本地诊断入口允许无 LLM 的清房成功。DeepSeek 使用可取消的异步 HTTP 客户端，进入控制前初始化 TLS，整次运行复用连接池，退出时关闭。`wall_seconds` 记录控制会话，批量报告的 `process_wall_seconds` 记录完整子进程耗时。

## 实现分层

| 文件 | 职责 |
|---|---|
| `SocketBridge/main.lua` | `COMBAT_FRAME` 原子观测、房间访问标识、完整输入、两帧观测租约校验、动作接收回执、输入回调读取证据、三更新帧失效释放、暂停心跳 |
| `combat/models.py` | 严格计划结构、世界快照、构筑、动作、回执和房间报告 |
| `combat/observation.py` | 快照去重、缺失字段拒绝、独立失血与清房判定 |
| `combat/catalog.py` | 七个组件槽位、合法名称、参数默认值和上下界、互斥及上下文校验 |
| `combat/geometry.py` | 原版碰撞网格、房间外形、玩家与泪弹通行分离、A*、线段碰撞及逃生区域 |
| `combat/components.py` | 可替换目标、站位、瞄准、武器、躲避、逃生与恢复实现，攻击及等待证据契约 |
| `combat/threats.py` | 可注入敌人机制、时间区间危险、时空索引、逃生路径与死亡后果检查 |
| `combat/supervision.py` | 强制进展监督、边界计时、恢复级别与未恢复停滞判定 |
| `combat/controller.py` | 注册组件调度、目标迟滞、联合动作搜索、路径进展与计算预算 |
| `combat/planner.py` | DeepSeek JSON 请求、分析与配置两阶段、只读工具、参数修复、Wiki 缓存 |
| `combat/runtime.py` | 唯一动作发送者、原生暂停协调、异步重规划、运行日志和清理 |
| `combat/launcher.py` | 版本检查、选项与存档备份、模组同步、进程管理、菜单引导及启动证据 |
| `apps/auto_first_room.py`、根目录启动脚本 | 自动启动与首房任务的统一入口 |
| `apps/first_room.py` | 配置加载与运行入口 |

动作求解枚举九种移动；武器模块提出攻击候选，普通泪弹最多四方向，有效攻击不存在时才提出停火。先计算 12 更新帧的运动与危险，并检查 45 帧内逃生路径及至少六帧余量，再执行一个动作。组件的 `begin_frame` 只在每次求解开始执行，候选生成不得推进武器状态；组件应自行对相同游戏帧去重。v1 计划经显式转换为 v2。参数校验后才允许装载计划。规划上下文使用运行、房间访问、构筑、组件目录及计划版本；旧房间结果被拒绝，单纯游戏帧前进不会使有效计划过期。

新增武器时，向 `BuildResolver` 注册对应游戏武器类型的构筑适配器，并注册匹配的武器、瞄准组件及能力声明。武器的 `firing_route` 决定攻击几何，`propose` 返回 `AttackProposal`；站位组件包装成 `MotionProposal`，调度器不假设所有武器都使用四方向弹道。状态只在 `begin_frame` 推进，候选评估必须只读。首阶段生产注册表只允许普通泪弹；模拟蓄力族验证完整注册、编译、站位和攻击替换。未注册的真实武器直接停止。

敌人通过 `EnemyAdapters` 按类型/变体注册预测模块，输出带有效时间和误差的 `ThreatForecast`。固定安全服务生成 `SafetyAssessment`，`ProgressSupervisor` 输出 `ProgressEvidence` 与 `BoundaryEvent`；这些契约不是自由文本指令。组件抛出异常时通过 `ResilientComponent` 切换到预置替代实现，并记录故障、计入降级次数和请求诊断。

导航使用带余量的半径，物理滚动预测使用真实碰撞半径。普通墙体碰撞会阻挡对应轴，允许沿墙滑动；尖刺等危险区域继续禁止进入。所有候选均不安全时，先降低最近三帧的风险，保留实际逃生路径，再比较整段风险与进攻收益。开火使用下一更新帧预测速度，包含墙体阻挡后的变化，不以期望方向代替真实移动继承。

稀疏危险使用按需建立的时空索引；至少 128 个预测危险时使用 NumPy 批量计算整条运动轨迹。两种后端都保留全部危险实体，密集后端与逐实体计算有数值等价回归。逃生结果按连续终点分别检查，不能把同一网格内不同站位视为相同安全状态。

战中请求由独立监督器与已确认失血、新敌人实例等事件触发。一般请求至少相隔 90 更新帧，严重边界不受普通冷却限制；每房最多六次请求，普通事件最多使用前四次，同时最多一个请求在途。战中直接重配，最多一次修复，总预算 20 墙钟秒。已恢复的停滞事件返回旧配置时拒绝装载。本地控制继续运行，返回结果在下一动作边界校验后替换。新敌人实例是保守重规划信号，不等于已经识别出一种新机制。

无理由零动作或移动受阻一秒触发本地恢复，两秒无有效进展触发强制诊断，四秒切换替代站位，八秒仍无进展判 `STALL_UNRECOVERED`。LLM 不能关闭边界，切换计划不能重置计时。停滞、120 游戏秒总超时及进程超时均计失败。原生恢复必须看到未暂停的新更新帧；仅在确认仍持有暂停时有限重试，不以发出 Escape 作为恢复成功证据。

入房动画可能忽略首次暂停键；暂停协调器在三秒内有限重试，等待期间用当前状态避险移动，正式配置前不射击。过期求解结果先释放输入，再从新快照重算；一秒内三次计算期限失误判 `CONTROL_DEADLINE_MISSES`。不会发送过期动作，也不会用反复丢弃掩盖持续控制失效。

## 模型与知识边界

DeepSeek 输入包含当前房间事实、参数目录、当前构筑、派生几何与反馈。模型可以查询已索引敌人机制、玩家机制、组件目录、实体、网格，以及预览配置动作。入房分析与配置每阶段最多两轮工具调用、每轮三项，最多两次输出修复；战中直接配置且最多一次修复。预览仅运行当前预测模型，不代表实际游戏试跑或胜率。

Wiki 索引位于 `configs/knowledge/index.json`，缓存位于同目录的 `cache/`。模型不能指定任意网络地址。下载文本带来源、抓取时间、内容哈希及版本提示，视为未经实机验证的资料；网络不可用或没有索引时返回未知，不生成伪造事实。页面可能包含忏悔之后的改动，需要人工核对后才能建立可信机制档案。

目前敌人轨迹采用短期速度预测及固定误差余量；这不是逐怪精确仿真。泪弹基础速度通过近发射点且玩家静止的实测样本校准，候选移动继承仍是以原版接口为锚点的近似。有效射程和移动加速度使用工程初值，需用实机轨迹进一步校准。炸弹原版接口没有公开剩余引信时间，按随时可能爆炸保守处理，并显式标记半径估计。

## 记录与结果

每次运行写入 `artifacts/runs/<UTC时间>/`：

- `manifest.json`：代码哈希、Git 提交、Python/系统版本、模型标识及实验约束。
- `trace.jsonl`：按序号排列的原始消息、规划输入输出、工具结果、校验错误、计划装载、最终动作、接收回执、暂停与故障。
- `report.json`：独立清房结果、真实失血、剩余生命、时间、动作计数、降级及恢复次数、决策延迟。
- `startup.json`、`game-log.txt`、`saves-after.json`、`backup/`：启动步骤及失败位置、本次游戏日志、存档哈希与修改前备份；只在自动启动入口生成。

完整候选集使用 90 帧环形缓存；受伤、脱困相关事件和重规划故障用于定位候选选择。逐帧最终动作始终保存。日志记录发送、接收和输入回调读取的区别；回调读取不是命中或位移证明，实际效果要结合后续快照。

清房需连续三份更新快照同时满足游戏清房标志、游戏敌人数为零、没有存活且能封门的敌对实体。原版 `EntityNPC.CanShutDoors` 用于区分封门敌人与清房后仍存在的环境危险；字段缺失时保守视为封门实体。暂时无敌敌人和环境危险都保留在观测与避险模型中，不能用“不可受伤”直接判清房。伤害回调记录的是尝试和扣血前生命；报告累加后续快照的生命下降，回血不抵消历史损失。接口依据：[EntityNPC.CanShutDoors](https://wofsauge.github.io/IsaacDocs/rep/EntityNPC.html#canshutdoors)。

清房基础判据成立后，再进行至少 15 更新帧的安全收尾；90 帧仍不能满足安全条件判 `CLEAR_UNSAFE`。收尾伤害计入房间损失。普通 Boom Fly、Ticking Spider 的爆炸死亡后果有独立适配器，100 像素为待校准的保守模型半径；Ticking Spider 的爆炸性质有本地死亡与伤害标志证据。攻击这些目标时持续保留爆炸距离，不能等最后一发泪弹在途后才撤退。

追逐、突进和 Hopper 落地采用多个运动假设及额外误差；这些是未校准的保守预测，不把未知内部状态编号解释成已知攻击阶段。适配器只覆盖所登记的普通变体，不宣称覆盖全部怪物或冠军机制。桥接保存带实例 ID 的近期死亡证据，单纯实体消失不会算作击杀。地面效果观测区分敌方与玩家来源；未知或减速地面采用保守危险模型。静态尖刺与机关区域保持避让，尚不解释所有机关状态数值。类型编号依据固定安装包 `resources/scripts/enums.lua`；API 依据 [EntityEffect](https://wofsauge.github.io/IsaacDocs/rep/EntityEffect.html)、[EntityNPC](https://wofsauge.github.io/IsaacDocs/rep/EntityNPC.html)，机制资料索引见 [Boom Fly](https://bindingofisaacrebirth.wiki.gg/wiki/Boom_Fly)。

报告新增进展指标、强制边界次数、战中请求数、决策 p99 与安全收尾帧数。候选日志包含停火理由、逃生余量、路线目标、剩余路径成本和预算是否耗尽。密度基准可执行 `python scripts/benchmark_combat.py --output artifacts/tmp-benchmark.json`；批次门槛由 `scripts/evaluate_phase1_optimization.py` 独立审计，混用实现版本、种子不符、缺少收尾证据或遗漏失败都不能通过。

`ActionFrame.decision_ms` 是求解器内部耗时，`diagnostics.decision_wall_ms` 与房间报告延迟包含线程调度和等待，使用高分辨率 `perf_counter` 测量。正式回归、开发与留出链可执行 `.\.venv\Scripts\python.exe scripts/qualify_phase1.py`：先要求规则与 LLM 各十例通过，再执行三十例开发，最后冻结五十个新种子配对评测。任一前置门槛未通过时停止并写 `qualification.json`；不会消耗留出样本掩盖开发失败。`--through regression` 或 `--through development` 可限定一次验证的终点。

## 回归测试

从 `SocketBridge/python` 执行：

```powershell
..\..\.venv\Scripts\python.exe -m unittest discover -s tests -q
```

测试包含原有桥接回归、真实 Lua 函数桩测试、几何与瞄准、计划编译、API 异常及 TCP 首房流程。Windows 异步运行时与模拟客户端需要本机回环网络权限。模拟游戏只验证协议、时序及降级，不能证明实际战斗能力；实机结果以独立验收记录为准。

参考：[DeepSeek JSON Output](https://api-docs.deepseek.com/guides/json_mode/)、[原版玩家 API](https://wofsauge.github.io/IsaacDocs/rep/EntityPlayer.html)、[网格碰撞枚举](https://wofsauge.github.io/IsaacDocs/rep/enums/GridCollisionClass.html)、[原版炸弹 API](https://wofsauge.github.io/IsaacDocs/rep/EntityBomb.html)。

实机结果、同种子对照与证据索引见 [第一阶段实机验收](phase-1-live-validation.zh-CN.md)。
