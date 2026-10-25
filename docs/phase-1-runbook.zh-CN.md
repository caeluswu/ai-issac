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

本地诊断成功只证明本地控制链路，不代表 LLM 规划验收。生产入口遇到 API 故障会记录降级，并用本地默认组件尝试完成当前房间。退出码为 0 的完整条件是清房成功且入房 LLM 规划成功；本地诊断入口允许无 LLM 的清房成功。游戏控制结束后，在途 HTTP 工作线程的回收可能使 Python 进程稍后才退出；`wall_seconds` 记录控制会话，未包含这段进程尾部等待。

## 实现分层

| 文件 | 职责 |
|---|---|
| `SocketBridge/main.lua` | `COMBAT_FRAME` 原子观测、房间访问标识、完整输入、两帧观测租约校验、动作接收回执、输入回调读取证据、三更新帧失效释放、暂停心跳 |
| `combat/models.py` | 严格计划结构、世界快照、构筑、动作、回执和房间报告 |
| `combat/observation.py` | 快照去重、缺失字段拒绝、独立失血与清房判定 |
| `combat/catalog.py` | 七个组件槽位、合法名称、参数默认值和上下界、互斥及上下文校验 |
| `combat/geometry.py` | 原版碰撞网格、房间外形、玩家与泪弹通行分离、A*、线段碰撞及逃生区域 |
| `combat/controller.py` | 目标迟滞、站位、运动预测、甩弹与提前瞄准、联合动作搜索、脱困 |
| `combat/planner.py` | DeepSeek JSON 请求、分析与配置两阶段、只读工具、参数修复、Wiki 缓存 |
| `combat/runtime.py` | 唯一动作发送者、原生暂停协调、异步重规划、运行日志和清理 |
| `combat/launcher.py` | 版本检查、选项与存档备份、模组同步、进程管理、菜单引导及启动证据 |
| `apps/auto_first_room.py`、根目录启动脚本 | 自动启动与首房任务的统一入口 |
| `apps/first_room.py` | 配置加载与运行入口 |

动作求解枚举九种移动和五种射击；先计算 12 更新帧的运动与危险，再执行一个动作。参数校验后才允许装载计划。规划上下文使用运行、房间访问、构筑、组件目录及计划版本；旧房间结果被拒绝，单纯游戏帧前进不会使有效计划过期。

战中请求由已确认失血、缺少输出进展、新敌人实例或重复脱困触发，至少相隔 90 更新帧，最多一个请求在途。本地控制继续运行，返回结果在下一动作边界校验后替换。新敌人实例是保守重规划信号，不等于已经识别出一种新机制。

## 模型与知识边界

DeepSeek 输入包含当前房间事实、参数目录、当前构筑、派生几何与反馈。模型可以查询已索引敌人机制、玩家机制、组件目录、实体、网格，以及预览配置动作。每阶段最多两轮工具调用、每轮三项，最多两次输出修复。预览仅运行当前预测模型，不代表实际游戏试跑或胜率。

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

## 回归测试

从 `SocketBridge/python` 执行：

```powershell
..\..\.venv\Scripts\python.exe -m unittest discover -s tests -q
```

测试包含原有桥接回归、真实 Lua 函数桩测试、几何与瞄准、计划编译、API 异常及 TCP 首房流程。Windows 异步运行时与模拟客户端需要本机回环网络权限。模拟游戏只验证协议、时序及降级，不能证明实际战斗能力；实机结果以独立验收记录为准。

参考：[DeepSeek JSON Output](https://api-docs.deepseek.com/guides/json_mode/)、[原版玩家 API](https://wofsauge.github.io/IsaacDocs/rep/EntityPlayer.html)、[网格碰撞枚举](https://wofsauge.github.io/IsaacDocs/rep/enums/GridCollisionClass.html)、[原版炸弹 API](https://wofsauge.github.io/IsaacDocs/rep/EntityBomb.html)。

实机结果、同种子对照与证据索引见 [第一阶段实机验收](phase-1-live-validation.zh-CN.md)。
