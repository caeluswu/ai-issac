# Isaac 分层控制项目

面向《以撒的结合：忏悔》原版 `1.7.9b.J835`，使用 SocketBridge 读取结构化状态，由可替换的底层控制组件执行移动与射击，DeepSeek 负责选择组件、填写参数和必要的异常诊断。

当前工程目标是：自动启动游戏，从初始房间正常出门，经 LLM 配置组件后清理一个实际战斗房，并保存调试信息。允许入房暂停；战斗中本地控制持续执行。整层和一至六层妈腿规划属于后续阶段。

道具扩展采用固定版本 EID 说明与本地经验：结合当前构筑决定拾取，在符合条件的安全节点进行有界测试，再更新校准、策略或组件。LLM 自身知识作为待验证假设；默认不接入 Wiki 爬虫。该学习链路处于计划阶段，见下方文档。

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

确需验证 LLM 完整流程时，在不入库的 `.env.local` 配置 `DEEPSEEK_API_KEY` 和 `DEEPSEEK_MODEL`，再单次执行 `run_first_room.py`。原始运行数据保存在 `artifacts/runs/`；凭据、游戏文件和存档二进制不进入 Git。

项目运行入口优先采用 `.env.local` 中显式填写的 DeepSeek 配置，覆盖继承的同名进程环境值；未填写的配置仍可来自环境。更换 key 后重新启动脚本即可，新建客户端读取新值；已存在的客户端不会自动换 key。无需为更新配置发送付费请求，实际 API 授权是否有效需在下一次必要调用中确认。

批次入口 `run_phase1_batch.py` 未加 `--local-only` 时会调用付费模型，当前不将其列为常规验收步骤。

## 文档导航

**当前工作入口：[M2-A 前置准备与含道具组件评估](docs/phase-2a-preparation.zh-CN.md)。** P0–P2 已完成合同、组件边界和 V2 观测接入，验证范围见 [P0–P2 验证报告](docs/phase-2a-p0-p2-validation.zh-CN.md)；下一项为 P3 构筑事务与健康账本。M2-A 指首层连续通关，M2-B 指 DeepSeek 实际开发新机制组件；EID 计划内 A–F 是学习功能步骤。

建议阅读顺序：任务契约 → 总体架构 → 二、三阶段路线 → M2-A 准备清单 → 对应专项设计。判断已实现能力时查验收与审查报告，计划文档不作为完成证据。

### 目标与总体设计

| 文档 | 职责 |
|---|---|
| [任务契约](docs/task-contract.zh-CN.md) | 游戏、存档、辅助边界、阶段目标、成功事件和记录要求 |
| [总体架构](docs/architecture-plan.zh-CN.md) | 分层控制、系统模块及长期设计边界 |
| [第二、三阶段路线](docs/phase-2-3-roadmap.zh-CN.md) | 首层、模型扩展与连续推进至 Mom 的里程碑和交付顺序 |
| [协作约定](AGENTS.md) | 开发、验证、主动讨论设计与文档交付原则 |

### 第二阶段准备与道具扩展

| 文档 | 职责 |
|---|---|
| [M2-A 前置准备与含道具组件评估](docs/phase-2a-preparation.zh-CN.md) | 当前执行队列 P0–P8、组件职责、支持矩阵、依赖和准入用例；统一准备顺序 |
| [战斗组件合同](docs/component-contract.zh-CN.md) | 可信组件 DTO、能力、宿主限额、上下文与生命周期边界 |
| [V2 观测合同](docs/observation-v2.zh-CN.md) | 拾取物、武器集合、临时效果、库存覆盖、健康与楼层身份；旧协议兼容 |
| [EID 与本地经验学习计划](docs/eid-local-learning-plan.zh-CN.md) | 来源接入、拾取决策、被动观测、有界实验、证据更新与经验复用 |
| [独立知识库接口计划](docs/knowledge-project-interface-plan.zh-CN.md) | 跨项目所有权、分层、查询合同、版本化数据包与本项目 Gateway |
| [道具知识与 DeepSeek 组件开发](docs/item-knowledge-and-skill-evolution.zh-CN.md) | 道具能力分类、程序生成物品、模型编程、验证发布与角色取舍 |

### 第一阶段设计与运行

| 文档 | 职责 |
|---|---|
| [战斗组件计划](docs/phase-1-combat-controller.zh-CN.md) | 单房间的观测、LLM 配置、组件接口与初始实现目标 |
| [第一阶段优化架构](docs/phase-1-optimization.zh-CN.md) | 安全与进攻、危险边界、进展监督和组件替换设计 |
| [运行手册](docs/phase-1-runbook.zh-CN.md) | 自动启动、连接、配置、单次运行与排错 |

### 验收证据与扩展审查

| 文档 | 职责与范围 |
|---|---|
| [M2-A P0–P2 验证](docs/phase-2a-p0-p2-validation.zh-CN.md) | 组件边界本地回归和 J835 只读 V2 实机证据；不代表拾取后控制已通过 |
| [第一阶段实机验收](docs/phase-1-live-validation.zh-CN.md) | 自动启动、出门、DeepSeek 配置和首个战斗房的闭环证据 |
| [第一阶段优化验收](docs/phase-1-optimization-validation.zh-CN.md) | 优化后的清房、损失、耗时与保留失败；不代表整层胜率 |
| [第一阶段扩展性审查](docs/phase-1-extensibility-review.zh-CN.md) | 当前源码的道具/长局扩展阻断及离线边界探针，作为准备工作的依据 |
| [控制 MVP 实机记录](docs/control-mvp-live-validation.zh-CN.md) | 状态读取、移动、射击、停发、过门与断线释放的早期实机证据 |

### SocketBridge 接口与工具

| 文档 | 职责与范围 |
|---|---|
| [SocketBridge 文档](SocketBridge/README.md) | 桥接模组与 Python 端的安装、接口和使用 |
| [SocketBridge 源码导读](docs/socketbridge-code-overview.zh-CN.md) | 固定上游基线的架构与源码说明；实机能力另查验收记录 |
| [控制 MVP 探针指南](docs/control-mvp-guide.zh-CN.md) | 底层控制探针的准备、运行与判据 |
