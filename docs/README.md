# 文档索引与维护边界

当前可运行范围为单房控制、有限道具与首层控制。P0–P6 的四个边界缺陷已由 A0 修复；首层的实机覆盖以验收证据为准。模型自主开发新机制程序与主动实验仍未交付。

建议阅读顺序：任务契约 → 总体架构 → M2-A 验收 → M2-B 计划 → 需要的专项合同。日常启动与付费验证政策从 [项目 README](../README.md) 进入。

## 当前文档及唯一职责

| 文档 | 负责维护的内容 |
|---|---|
| [任务契约](task-contract.zh-CN.md) | 环境、角色、辅助边界、阶段成功事件与必要记录 |
| [总体架构](architecture-plan.zh-CN.md) | 稳定模块职责、数据与控制流、身份和权限边界 |
| [P0–P6 实现审查](phase-2a-preparation.zh-CN.md) | 当前准备状态、源码缺口、四项离线复现与 A0 准入要求 |
| [M2-A 详细计划](phase-2a-plan.zh-CN.md) | 首层范围、状态机、模块接口、预算、A0–A7 提交与验收 |
| [M2-B 详细计划](phase-2b-plan.zh-CN.md) | 当前开发目标、首例蓄力组件、B0–B6 实施与提交、开发预算及验收 |
| [第二、三阶段路线](phase-2-3-roadmap.zh-CN.md) | M2-A、M2-B、M3 的里程碑和阶段衔接，不复制当前工作队列 |
| [组件合同](component-contract.zh-CN.md) | 组件 DTO、能力、生命周期、宿主校验与限额 |
| [V2 观测合同](observation-v2.zh-CN.md) | Lua/Python 结构化状态、字段语义、覆盖与兼容性 |
| [知识项目接口](knowledge-project-interface-plan.zh-CN.md) | 独立来源包与本项目的所有权、查询接口和 Gateway 边界 |
| [EID 与本地经验](eid-local-learning-plan.zh-CN.md) | 观测如何成为经验、证据去重、适用性和失效；主动实验的后续条件 |
| [DeepSeek 组件开发](item-knowledge-and-skill-evolution.zh-CN.md) | 机制分类、模型产物等级与长期验证发布原则；当前队列由 M2-B 计划维护 |
| [首层运行手册](phase-2a-runbook.zh-CN.md) | 自动首层、请求限额、日志与失败排查 |
| [第一阶段运行手册](phase-1-runbook.zh-CN.md) | 当前入口、配置、运行、排错与已有控制实现的使用限制 |
| [知识 SDK 使用说明](../knowledge_project/README.md) | 已实现包的安装、导入、查询及最小调用示例 |

任务契约约束阶段计划；已实现 DTO 的细节由组件/观测合同维护。M2-A 文档中标为待实现的接口是设计目标，实施时同步对应合同与代码。审查决定当前准入状态，历史测试通过不能覆盖后续发现的问题。若文档间存在冲突，应查代码和版本化证据并修正所属文档，不能凭较新的日期认定功能已实现。

## 版本化证据

这些记录描述特定提交、环境和场景；不随当前设计改写历史结果。

| 文档 | 证据范围 |
|---|---|
| [控制 MVP 实机记录](evidence/control-mvp-live-validation.zh-CN.md) | 初期状态读取、移动、射击、过门和断线释放 |
| [第一阶段实机验收](evidence/phase-1-live-validation.zh-CN.md) | 自动启动、出门、DeepSeek 配置与首个战斗房 |
| [第一阶段优化验收](evidence/phase-1-optimization-validation.zh-CN.md) | 清房、损失、耗时与保留失败的工程可行性 |
| [P0–P2 验证](evidence/phase-2a-p0-p2-validation.zh-CN.md) | 组件边界和只读 V2 观测 |
| [P3–P6 准入验证](evidence/phase-2a-preparation-validation.zh-CN.md) | 构筑、生命周期、EID 与被动学习的历史测试及开发场景实机证据 |
| [M2-A 验收](evidence/phase-2a-validation.zh-CN.md) | 正常随机首层与受预算限制的模型回放，包含全部失败 |
| [M2-B 机制探针](evidence/phase-2b-mechanism-probe.zh-CN.md) | 巧克力牛奶的输入/出弹、有限伤害校准与暂停/TTL 释放；不是模型程序战斗验收 |
| [当前准备审查诊断 JSON](../artifacts/validation/m2-readiness-audit.json) | 合成场景复现四项边界缺陷；不是游戏损失记录 |

## 归档方案与源码参考

| 文档 | 用途 |
|---|---|
| [控制 MVP 探针指南](archive/control-mvp-guide.zh-CN.md) | 早期控制链路探针及判据 |
| [第一阶段战斗组件方案](archive/phase-1-combat-controller.zh-CN.md) | 单房架构的阶段设计背景 |
| [第一阶段优化方案](archive/phase-1-optimization.zh-CN.md) | 第一阶段安全、进攻和进展监督设计 |
| [第一阶段扩展性审查](archive/phase-1-extensibility-review.zh-CN.md) | P0–P6 之前的代码问题与扩展依据 |
| [SocketBridge 源码导读](reference/socketbridge-code-overview.zh-CN.md) | 固定上游基线的模块结构；实际能力另查当前代码与证据 |
| [战斗控制实现参考](reference/combat-controller-implementation.zh-CN.md) | 当前基线的求解细节、预测窗口、替换机制和监督阈值 |
| [SocketBridge 上游文档](../SocketBridge/README.md) | 桥接项目原有安装与接口资料；不是本项目当前验收清单 |

## 维护规则

- 修改目标写任务契约；修改当前队列写对应阶段计划，当前为 M2-B；修复审查问题后更新审查状态，并链接对应提交与回归证据。
- 字段或组件接口只在所属合同维护完整定义，其他文档链接引用；学习语义与跨项目传输格式分开维护。
- 新能力明确区分设计、已实现、本地已验证、实机已验证；不以文档完成替代功能验收。
- 操作命令维护在 README/运行手册；付费 API 政策与暂缓研究实验集中于项目 README。
- 归档保留版本溯源及独有细节，不复制成另一份当前计划；移动文档时同步引用。
