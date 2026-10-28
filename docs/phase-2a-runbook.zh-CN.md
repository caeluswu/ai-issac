# M2-A 首层运行手册

入口为项目根目录的 `run_floor.py`。环境为原版 Repentance `1.7.9b.J835`、全解锁槽 1、Isaac 普通难度、非 XL 第一层、普通泪弹。支持范围与独立成功条件见 [M2-A 计划](phase-2a-plan.zh-CN.md)，实际覆盖见 [验收证据](evidence/phase-2a-validation.zh-CN.md)。

## 准备与运行

沿用现有 `.venv`、游戏目录和受保护存档基线。自动启动器会正常关闭项目中的单个旧游戏实例、备份并恢复基线、同步模组、进入存档，再有记录地初始化正常新局。游戏窗口可以被遮挡，不能最小化；菜单与原生暂停会短暂激活窗口。F3 切回 MANUAL 会中止自动控制。

首次准备 EID 包：

```powershell
uv pip install --python .venv/Scripts/python.exe -e knowledge_project
.\.venv\Scripts\python.exe scripts/prepare_eid_knowledge.py
```

从根目录运行一次正常随机首层：

```powershell
# 本地规则控制，不请求模型
.\.venv\Scripts\python.exe run_floor.py --local-only

# DeepSeek 战术配置；凭据在 Git 忽略的 .env.local
.\.venv\Scripts\python.exe run_floor.py
```

`.env.local` 的读取和密钥更新规则见 [README](../README.md)。默认实际 HTTP 请求总上限为 6，常规最多 4、诊断最多 2；可用 `--llm-requests 1` 降低总上限。首层使用直接输出计划的单轮协议，不开放工具往返；修复和网络重试仍占额度。llm_request 日志包含被预算阻止的尝试，实际 HTTP 次数以请求预算总账为准。输入/输出总预算为 60,000/12,000 token，缺少 usage 时保守保留预占额。模型接入失败或额度不足时允许兼容本地回退，日志明确来源；模型模式退出码为 0 还要求实际采用过经过验证的模型计划。

固定种子只用于复现，显式输出目录必须不存在：

```powershell
.\.venv\Scripts\python.exe run_floor.py --local-only --seed "XXXX XXXX" --output-dir artifacts/runs/my-debug-run
```

不提供计时中的控制台或改血/改伤害选项。药丸、卡牌、炸弹使用、主动道具、未知收藏品/饰品与复杂武器不自动启用；首层与主线现可在商店进行受限金币购买，钱 ≥15 时优先考虑进入，血量与选择组策略见[运营验证](evidence/phase-3c-economy-validation.zh-CN.md)。普通资源及有限红/魂/黑心须满足子型、容量、代价和消费后证明，拾取不可达可以跳过。P7 主动实验关闭。

## 连续多层入口

`run_mainline.py` 复用同一 run、连接、构筑和健康资源账本；目标层 2–5 需正常离层，6 需击败 Mom。原有 `run_two_floors.py` 为两层兼容入口。

```powershell
# 默认规则控制、正常随机种子，不调用 DeepSeek
.\.venv\Scripts\python.exe run_mainline.py --target-stage 6 --llm-requests 0

# 固定种子仅用于开发复现；目录必须不存在
.\.venv\Scripts\python.exe run_mainline.py --target-stage 4 --seed "3BYH A82R" --llm-requests 0 --output-dir artifacts/runs/my-four-floor-debug
```

多层总墙钟默认 3600 秒，可用 `--wall-limit` 修改；达到期限就是失败。主线不会自动付费生成新道具代码，使用现有支持表和已发布技能。`run-report.json` 包含各层结果，`trace.jsonl` 保留动作/观测；固定种子为 `debug_replay`，正常随机为 `normal_random`，均不冒充封存正式评测。支持配置到第六层不等于已通过 Mom；当前结果与阻塞见[进度](progress.zh-CN.md)。

Host 等待开盖与周期地形等待都有证据和截止帧。无怪环境单阶段最多 600 更新，总环境/时序出门最多 1800 更新；普通门导航仍为 750 更新。只有实际进展可刷新相应监督依据，机关自身伸缩不计任务进展。不要靠增加总超时掩盖振荡。

## 成功与失败

`FLOOR_COMPLETED` 必须有同一局的 Boss 战、Boss 完成、正常出口、实际下一层和下一层受控移动证据，并成功释放输入/恢复 MANUAL。击败 Boss 后卡在出口、死亡、断线或清理失败均不成功。

正常房最多 120 游戏秒，Boss 最多 180 游戏秒；门导航、拾取和停滞有更早期限。整层最多 900 游戏秒/1,800 秒墙钟。Boss 入场和下楼画面只在对应目标授权后允许最长 15 秒的暂停心跳等待；不自动跳过未知暂停。原版加载会短暂停止渲染心跳，只有已经观察到相应暂停证据的有效过渡租约可等待到该硬期限；跨层心跳携带 run/floor 身份以区分自动 MANUAL 复位。墙钟包括启动、模型等待和游戏暂停，不能以暂停缩短墙钟成绩。

静态远处火焰、血池和不锁门机关可以留在房内，由运动与危险求解器避开。房间 clear 不等于当前站位安全，未知危险不会被当成无害。正常出口的通行授权只开放当前已确认的一个 trapdoor 网格，其他出口和未决定物品仍参与避让。

## 调试包与排查

每次运行输出 `artifacts/runs/floor-时间戳/`，包含：

| 文件 | 用途 |
|---|---|
| manifest.json | 游戏/源码哈希、提交及 dirty 状态、存档基线、知识包、种子来源、预算 |
| startup.json、game-log.txt、backup/ | 启动、构建核验、原选项与存档保护证据 |
| trace.jsonl | 原始观测、回执、动作、状态转换、计划及异常 |
| decisions.jsonl | 门/出口目标、物品结果、计划来源与失败摘要 |
| room-results.jsonl | 各次访问的战斗/清房后终态、损失和版本；失败访问也保留 |
| floor-report.json | 最终结果、Boss/离层/可行动证据、总损失、资源、时间和 API 用量 |

优先检查 `floor-report.json.error` 和最后一个 `floor_state`，再定位同 visit 的动作与快照。`UNSUPPORTED_*` 表示能力范围缺口，`STALE_OBSERVATION` 表示没有在允许期限内取得有效状态，`STALL_UNRECOVERED` 表示有界恢复失败；这些都不能通过延长总时限改成成功。

一次完成只证明对应构筑、路线和 Boss 的可行性。全部尝试保留；稳定胜率、模型收益、竞速与消融遵循 [README 的费用和研究政策](../README.md)，不默认追加付费批次。
