# Isaac 分层控制项目

面向《以撒的结合：忏悔》原版 `1.7.9b.J835`，使用 SocketBridge 读取结构化状态，由可替换的底层控制组件执行移动与射击，DeepSeek 负责选择组件、填写参数和必要的异常诊断。

当前可运行能力是：自动启动游戏，从初始房间正常出门，经 LLM 配置组件后清理一个实际战斗房，并保存调试信息。允许入房暂停；战斗中本地控制持续执行。下一目标 M2-A 是正常完成第一层；整层控制尚未实现。

道具扩展采用固定版本 EID 说明与本地经验。P0–P6 已实现构筑变化、健康资源账本、房间执行器、EID 查询、有限拾取和被动经验持久化；历史实机验证了简单属性道具拾取后出门清房。当前审查复现了资源子型、拾取授权、清房后终态和样本重复发布四个边界缺陷，须先完成 [M2-A 计划的 A0](docs/phase-2a-plan.zh-CN.md)，再接整层调度。主动实验保持关闭，DeepSeek 自主编写机制程序属于 M2-B。

## 验证与付费 API 原则

**现阶段以证明目标的端到端可行性为验收目标，避免为过度严谨的实验消耗 DeepSeek 付费 API。**

- 优先使用本地单元测试、模拟 TCP、已有轨迹离线重放和规则控制器排查问题；根据改动影响选择验证，复用仍适用的已有证据。
- 付费调用仅用于必要的接入检查、少量完整流程和确有需要的异常诊断。一次真实完成目标可作为初步可行性证据；只有新问题或相关代码改动才追加针对性验证。
- 启动器、日志、文档等改动不默认触发 DeepSeek 批量回归。重复运行相同种子不会自动训练模型，也不能增加独立样本量。
- 不默认执行大样本 LLM 回归、LLM/规则配对实验或消融。此类实验先记录，后续单独确定研究问题、样本量和 API 调用预算再开展。
- 可行性通过不等于稳定胜率、LLM 优于规则、竞速水平或 SOTA 已获证明。失败、失血、超时、降级与未完成批次均保留，不能只展示成功案例。
- 需要停止批次时，使用 `--stop-file`，在当前单场收尾并保存报告后停止后续样本。

## 后续实验清单（暂缓执行）

| 实验 | 要回答的问题 |
|---|---|
| 规则选组件 / LLM 选组件 | LLM 对清房、失血和完整耗时是否有实际收益？ |
| 固定参数 / LLM 调参 | 收益来自组件选择还是参数配置？ |
| 仅入房规划 / 加入战中诊断 | 额外调用是否改善恢复，是否值得其费用和延迟？ |
| 直接瞄准 / 预测瞄准与移动继承 | 对命中和击杀速度的贡献有多大？ |
| 简单站位 / 射击通道导航与机会射击 | 对遮挡、无效输出和清房速度的贡献有多大？ |
| 简单避让 / 时空预测、退路与死亡后果模型 | 各安全组件分别减少哪些伤害和失败？ |
| 不复用经验 / 技能与房间经验复用 | 能否减少调用次数、暂停时间和调试成本？ |

统一关注清房结果、非主动失血、资源消耗、游戏时间、完整墙钟时间和 API 调用量。研究性消融不能取消输入释放、断线保护等工程安全机制。

## 运行

在项目根目录使用现有 uv 虚拟环境；全解锁槽 1 的基线由 `configs/task/save-baseline.slot-1.json` 定位。

```powershell
# 本地规则流程，不调用 DeepSeek
.\.venv\Scripts\python.exe run_first_room.py --local-only

# 只验证启动、连接与原生暂停
.\.venv\Scripts\python.exe run_first_room.py --startup-only

# 本地测试
.\.venv\Scripts\python.exe -m unittest discover -s SocketBridge/python/tests -t SocketBridge/python -q
```

首次启用道具功能，先安装独立 SDK 并准备固定 EID 包。准备脚本只下载锁定提交的四个说明文件、校验哈希并生成本地 SQLite；运行中的查询不联网。

```powershell
uv pip install --python .venv/Scripts/python.exe -e knowledge_project
.\.venv\Scripts\python.exe scripts/prepare_eid_knowledge.py
.\.venv\Scripts\python.exe run_first_room.py --local-only --with-items
```

`--with-items` 启用实验性的本地说明、拾取、避让和被动记录。当前收藏品支持映射为 1/4/12 的简单属性、20 的飞行以及不使用主动效果的 105；未知机制、饰品、复杂武器和健康交易明确拒绝。资源子型与交互边界仍有 [已知缺陷](docs/phase-2a-preparation.zh-CN.md)，不能视为全道具安全策略。EID 来源包与智能体经验分存于 `artifacts/knowledge/` 和 `artifacts/agent-state/agent.sqlite`，均不进入 Git；遭遇、决策前态、拾取确认、房间结果、测量、结论和反例可追溯。

开发用实机核验入口为 `scripts/validate_m2_preparation_live.py`：会自动重开固定场景，并在记录的初始化环节生成道具，再正常拾取和战斗。其结果仅验证机制，不是正常随机对局胜率；不作为日常启动命令。

确需验证 LLM 完整流程时，在不入库的 `.env.local` 配置 `DEEPSEEK_API_KEY` 和 `DEEPSEEK_MODEL`，再单次执行 `run_first_room.py`。原始运行数据保存在 `artifacts/runs/`；凭据、游戏文件和存档二进制不进入 Git。

项目运行入口优先采用 `.env.local` 中显式填写的 DeepSeek 配置，覆盖继承的同名进程环境值；未填写的配置仍可来自环境。更换 key 后重新启动脚本即可，新建客户端读取新值；已存在的客户端不会自动换 key。无需为更新配置发送付费请求，实际 API 授权是否有效需在下一次必要调用中确认。

批次入口 `run_phase1_batch.py` 未加 `--local-only` 时会调用付费模型，当前不将其列为常规验收步骤。

## 文档导航

完整目录及文档职责见 [docs 文档索引](docs/README.md)。当前执行入口是审查结论与 M2-A 计划；历史验收只证明对应版本及场景，不覆盖新发现的缺陷。

| 入口 | 解决的问题 |
|---|---|
| [任务契约](docs/task-contract.zh-CN.md) / [总体架构](docs/architecture-plan.zh-CN.md) | 做什么，哪些辅助允许，模块如何分工 |
| [P0–P6 实现审查](docs/phase-2a-preparation.zh-CN.md) | 当前能做什么，四个已复现问题及准入状态 |
| [M2-A 详细实施计划](docs/phase-2a-plan.zh-CN.md) | 从 A0 修复到地图、调度、Boss 与正常离层的具体工作和验收 |
| [第二、三阶段路线](docs/phase-2-3-roadmap.zh-CN.md) | M2-B 模型开发组件与 M3 连续推进至 Mom 的边界 |
| [运行手册](docs/phase-1-runbook.zh-CN.md) | 已实现入口的配置、启动和排错 |
| [EID 与本地经验](docs/eid-local-learning-plan.zh-CN.md) / [知识 SDK](knowledge_project/README.md) | 说明、观测和学习证据如何分开，当前接口如何使用 |
| [协作约定](AGENTS.md) | 开发、验证、主动讨论设计与文档交付原则 |

`docs/evidence/` 保存版本化验收证据，`docs/archive/` 保存早期阶段方案，`docs/reference/` 保存源码参考；当前任务不以归档文档的旧清单为执行依据。
