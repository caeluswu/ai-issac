# 第一阶段控制器：实现与运行手册

本文维护单房入口；多层日常操作见[首层与多层手册](phase-2a-runbook.zh-CN.md)，阶段状态见[当前进度](progress.zh-CN.md)。

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

## 架构与当前状态

模块职责见 [总体架构](architecture-plan.zh-CN.md)，组件接口见 [组件合同](component-contract.zh-CN.md)，当前限制见 [准备审查](phase-2a-preparation.zh-CN.md)。现有求解、预测窗口与监督阈值见 [控制实现参考](reference/combat-controller-implementation.zh-CN.md)。含道具入口与安装步骤见 [README](../README.md)；M2-A 运行器仍为待实现计划，不能用首房入口代替整层验收。

## 模型与知识边界

空房与机关房不构成战斗成功。`EnvironmentController` 只生成目标，由同一输入执行器执行：先避开未知引信炸弹及可达的 TNT 连锁，观察爆炸转换残留消退，再以普通移动接触登记的常规压力板，必须看到实际清房标志或敌人生成才结束环境阶段。只开放当前压力板的通行权限，尖刺和其他机关不随之解禁。TNT 未被触发时仍是普通障碍，不是永久爆炸区。未知环境两秒无进展异步请求诊断，八秒未恢复或二十游戏秒环境总时限均失败；诊断不授予模型任意机关控制权限。A* 起始格中心不强迫角色回走，避免相邻格分界处振荡。

失血报告默认覆盖从正常起点到清理结束的整个任务，回血不抵消损失。`combat_damage_half_hearts` 单列目标战斗房，`task_ticks` 包含导航、环境、战斗与收尾的游戏帧，`environment_ticks` 单列机关阶段；暂停另外计墙钟。跨过机关房之后不能清空此前失血。

DeepSeek 输入包含当前房间事实、参数目录、当前构筑、派生几何与反馈。模型可以查询已索引敌人机制、玩家机制、组件目录、实体、网格，以及预览配置动作。入房分析与配置每阶段最多两轮工具调用、每轮三项，最多两次输出修复；战中直接配置且最多一次修复。预览仅运行当前预测模型，不代表实际游戏试跑或胜率。

Wiki 索引位于 `configs/knowledge/index.json`，缓存位于同目录的 `cache/`。模型不能指定任意网络地址。下载文本带来源、抓取时间、内容哈希及版本提示，视为未经实机验证的资料；网络不可用或没有索引时返回未知，不生成伪造事实。页面可能包含忏悔之后的改动，需要人工核对后才能建立可信机制档案。

目前敌人轨迹采用短期速度预测及固定误差余量；这不是逐怪精确仿真。泪弹基础速度通过近发射点且玩家静止的实测样本校准，候选移动继承仍是以原版接口为锚点的近似。`PlayerMotion` 独立处理玩家位移与 API 速度的关系：固定构建 Isaac 的稳态位移/API 速度约为 2，敌人和泪弹约为 1。玩家 API 速度保持原样供射击继承使用；`BuildProfile.move_speed` 表示每桥接更新帧的稳态位移，基础 speed=1 时使用 8.8 像素，原生速度上限约 4.4，逐更新速度保留系数约 0.75。证据与复算脚本为 `player-motion-calibration.json` 和 `scripts/calibrate_player_motion.py`。这是当前角色与构建的经验适配，不是通用引擎规则；有效射程、转向、击退和特殊地形仍有模型误差。炸弹原版接口没有公开剩余引信时间，按随时可能爆炸保守处理，并显式标记半径估计。

## 记录与结果

每次运行写入 `artifacts/runs/<UTC时间>/`：

- `manifest.json`：代码哈希、Git 提交、Python/系统版本、模型标识及实验约束。
- `trace.jsonl`：按序号排列的原始消息、规划输入输出、工具结果、校验错误、计划装载、最终动作、接收回执、暂停与故障。
- `report.json`：独立清房结果、真实失血、剩余生命、时间、动作计数、降级及恢复次数、决策延迟。
- `startup.json`、`game-log.txt`、`saves-after.json`、`backup/`：启动步骤及失败位置、本次游戏日志、存档哈希与修改前备份；只在自动启动入口生成。

完整候选集使用 90 帧环形缓存；受伤、脱困相关事件和重规划故障用于定位候选选择。逐帧最终动作始终保存。日志记录发送、接收和输入回调读取的区别；回调读取不是命中或位移证明，实际效果要结合后续快照。

清房需连续三份更新快照同时满足游戏清房标志、游戏敌人数为零、没有存活且能封门的敌对实体。原版 `EntityNPC.CanShutDoors` 用于区分封门敌人与清房后仍存在的环境危险；字段缺失时保守视为封门实体。暂时无敌敌人和环境危险都保留在观测与避险模型中，不能用“不可受伤”直接判清房。伤害回调记录的是尝试和扣血前生命；报告累加后续快照的生命下降，回血不抵消历史损失。接口依据：[EntityNPC.CanShutDoors](https://wofsauge.github.io/IsaacDocs/rep/EntityNPC.html#canshutdoors)。

清房基础判据成立后，再进行至少 15 更新帧的安全收尾；90 帧仍不能满足安全条件判 `CLEAR_UNSAFE`。收尾伤害计入房间损失，清理期间观察到死亡也不能报告成功。普通 Boom Fly 的模型爆炸半径为 100 像素；Ticking Spider 使用 70 像素，再额外计入玩家体积、撤离余量和残余危险误差。攻击这些目标时持续保留爆炸距离，不能等最后一发泪弹在途后才撤退。

Ticking Spider 即使空中碰撞类为零，也保留落地预测。`type=16, variant=2` 登记独立的接近自爆适配器：原始记录包含满血个体在未开火时自爆，以及同帧爆炸伤害与死亡事件。它在路径选择前就提供可能的自爆范围，并参与开火前与死亡后检查；不把保护推迟到击杀之后。该变体暂用 100 像素工程包络，未宣称精确半径；普通 Mulligan 不继承这一爆炸机制。观测证据见 `artifacts/phase-1-optimization-20261026/mulliboom-observations.json`。

Ticking Spider 参数来自固定构建的已有轨迹：无遮挡、无近期受伤的条件下，约 54/60 像素记录到爆炸伤害，约 72 像素保持无伤。回调与快照存在时序误差，因此 70 只是附加余量前的工程模型值，不是游戏精确半径。证据位于 `artifacts/phase-1-optimization-20261026/ticking-spider-calibration.json`，可用 `scripts/analyze_death_radius.py` 从原始轨迹复算。其他变体和冠军机制没有逐一校准。

追逐、突进和 Hopper 落地采用多个运动假设及额外误差；这些是未校准的保守预测，不把未知内部状态编号解释成已知攻击阶段。适配器只覆盖所登记的普通变体，不宣称覆盖全部怪物或冠军机制。桥接保存带实例 ID 的近期死亡证据，单纯实体消失不会算作击杀。地面效果观测区分敌方与玩家来源；未知或减速地面采用保守危险模型。静态尖刺与机关区域保持避让，尚不解释所有机关状态数值。类型编号依据固定安装包 `resources/scripts/enums.lua`；API 依据 [EntityEffect](https://wofsauge.github.io/IsaacDocs/rep/EntityEffect.html)、[EntityNPC](https://wofsauge.github.io/IsaacDocs/rep/EntityNPC.html)，机制资料索引见 [Boom Fly](https://bindingofisaacrebirth.wiki.gg/wiki/Boom_Fly)。

报告新增进展指标、强制边界次数、战中请求数、决策 p99 与安全收尾帧数。候选日志包含停火理由、逃生余量、路线目标、剩余路径成本和预算是否耗尽。密度基准可执行 `python scripts/benchmark_combat.py --output artifacts/tmp-benchmark.json`；批次门槛由 `scripts/evaluate_phase1_optimization.py` 独立审计，混用实现版本、种子不符、缺少收尾证据或遗漏失败都不能通过。

`ActionFrame.decision_ms` 是求解器内部耗时，`diagnostics.decision_wall_ms` 与房间报告延迟包含线程调度和等待，使用 perf_counter 测量。`scripts/qualify_phase1.py` 是历史大样本研究工具，当前不属于常规验收；执行会产生多批模型调用，应依 README 的研究与费用原则另行安排。

## 导航与遮挡

导航与遮挡处理使用同一组安全服务。`DoorNavigator` 通过原子观测跟随路径、避让火堆和敌方弹幕；预测包含后续转向及终点制动，以整条路线的折扣进展评分。角色静止、目标尚远且存在已验证安全的本帧前进动作时，前进契约拒绝无理由零动作。接近已确认开放、对齐的目标门时，检查下一次位移后穿门，并从新的实际房间快照重新分类。进入迟到生成敌人的房间后立即交接战斗。

导航在房间已清空时仍需要进展：一秒触发本地恢复、两秒无进展请求异步诊断，八秒未恢复或二十五游戏秒导航总时限均失败。每个导航房间最多两次请求，同时最多一个在途。`navigation_metrics`、`navigation_replan_requests` 和导航决策 p95/p99 单列保存，并纳入延迟门槛。刚完成的已授权压力板在当前房间实例内保留离开所需的通行权限。

`combat_scope=first_observed_blocking_room` 表示战斗时间和战斗损失仅归属于首次实际观察到封门敌人的房间；任务总损失覆盖导航、环境、战斗和收尾。导航阶段死亡时战斗时间为零，死亡仍计任务失败与完整资源损失。独立审计从原始快照重新计算这两个口径。

观测中的零接触伤害用于区分身体阻挡和伤害威胁：Stoney 仍保留在实体观测中，其无伤接触不会生成伤害排斥场。接近自爆与已登记落地机制继续独立预测。没有有效射击站位时，先检查普通可破坏障碍，再由位置组件选择可达换位目标，引导会跟随的敌人离开遮挡；目标在到达或被拒绝前保持稳定，所有换位受相同进展时限约束。

已暴露的失败集位于 `configs/task/phase1-adversarial-seeds.json`。完整验证可附加 `--adversarial-seeds configs/task/phase1-adversarial-seeds.json`，在新留出评测前分别检查规则与 LLM 两组的失败回归。开发批次也检查原始轨迹、实际战斗、手动控制恢复、版本和导航延迟。所有已用于诊断的种子归入开发数据；新代码的留出评测使用另行预提交的随机种子。

## 回归测试

从 `SocketBridge/python` 执行：

```powershell
..\..\.venv\Scripts\python.exe -m unittest discover -s tests -q
```

测试包含原有桥接回归、真实 Lua 函数桩测试、几何与瞄准、计划编译、API 异常及 TCP 首房流程。Windows 异步运行时与模拟客户端需要本机回环网络权限。模拟游戏只验证协议、时序及降级，不能证明实际战斗能力；实机结果以独立验收记录为准。

参考：[DeepSeek JSON Output](https://api-docs.deepseek.com/guides/json_mode/)、[原版玩家 API](https://wofsauge.github.io/IsaacDocs/rep/EntityPlayer.html)、[网格碰撞枚举](https://wofsauge.github.io/IsaacDocs/rep/enums/GridCollisionClass.html)、[原版炸弹 API](https://wofsauge.github.io/IsaacDocs/rep/EntityBomb.html)。

实机结果、同种子对照与证据索引见 [第一阶段实机验收](evidence/phase-1-live-validation.zh-CN.md)。
