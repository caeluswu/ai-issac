# 游戏机制程序库

此目录保存经宿主验证、实机试用后发布的游戏组件程序，不是 Codex 的技能目录。`index.json` 指向活动版本并绑定 manifest 哈希；会话启动时显式加载，运行中不热替换。

当前版本为 `weapon.chocolate_milk/2f146c3dee844abedbb646268c64def479625c64e55c8e35ca744d8fdb784612`：基础 Isaac 的巧克力牛奶时序，适用构筑由 calibration.json 限定。program.json 是验收后的类型化 AST；model-response.json 是供应商原文，保留原始格式错误，不应直接当作合法程序解析。

manifest.json 记录来源、模型响应、规范化、独立检查和首次试用。算法作者为 DeepSeek，宿主只做有界语法恢复；重载会重放恢复并校验程序和校准哈希。原始响应禁用 Git 换行转换，按字节核验。原包保持不可变，停用记录写在独立 quarantine.json 中；没有兼容包时不将该道具冒充普通泪弹。

开发操作见 [项目 README](../README.md)，完整范围、API 失败及跨进程证据见 [M2-B 验收](../docs/evidence/phase-2b-validation.zh-CN.md)。
