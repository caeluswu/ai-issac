# Isaac 分层控制项目

面向《以撒的结合：忏悔》原版 `1.7.9b.J835`，使用 SocketBridge 读取结构化状态，由可替换的底层控制组件执行移动与射击，DeepSeek 负责选择组件、填写参数和必要的异常诊断。

当前工程目标是：自动启动游戏，从初始房间正常出门，经 LLM 配置组件后清理一个实际战斗房，并保存调试信息。允许入房暂停；战斗中本地控制持续执行。整层和一至六层妈腿规划属于后续阶段。

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

批次入口 `run_phase1_batch.py` 未加 `--local-only` 时会调用付费模型，当前不将其列为常规验收步骤。

## 文档

- [第一阶段扩展性审查](docs/phase-1-extensibility-review.zh-CN.md)
- [第二、三阶段实施计划](docs/phase-2-3-roadmap.zh-CN.md)
- [道具知识库与 DeepSeek 组件开发](docs/item-knowledge-and-skill-evolution.zh-CN.md)
- [第一阶段优化验收](docs/phase-1-optimization-validation.zh-CN.md)
- [第一阶段优化架构](docs/phase-1-optimization.zh-CN.md)
- [战斗组件计划](docs/phase-1-combat-controller.zh-CN.md)
- [总体架构](docs/architecture-plan.zh-CN.md)
- [运行手册](docs/phase-1-runbook.zh-CN.md)
- [SocketBridge 文档](SocketBridge/README.md)
