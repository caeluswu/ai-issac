# 第一阶段优化证据

当前工程可行性结论见 [验收文档](../../docs/phase-1-optimization-validation.zh-CN.md)；机器可读结论为 [feasibility-audit.json](qualification-final-20261026T041644Z/feasibility-audit.json)。该结论核对五十次规则运行和十二次实际完成的 DeepSeek 运行，不宣称完成五十次 LLM 留出或消融。

`qualification-final-20261026T041644Z/batches/` 保存当前实现的逐次文本证据；索引记录本地原始轨迹路径与 SHA-256。测试、性能、物理校准、故障注入和其他 qualification 文件保留各自的实现提交、失败与停止状态，不能跨实现合并为同一轮通过结果。`experiment-register.json` 提供这些记录的入口。

完整逐帧轨迹和存档备份保留在本地 `artifacts/runs/`。此目录不包含 API 凭据、游戏二进制或存档二进制。大样本付费评测与消融处于暂缓状态。
