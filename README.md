# Isaac 分层控制项目

面向《以撒的结合：忏悔》原版 `1.7.9b.J835`，使用 SocketBridge 读取结构化状态，由可替换的底层控制组件执行移动与射击。DeepSeek 负责选择组件、填写参数和必要的异常诊断，也可通过独立开发流程编写有界机制程序。

当前运行入口包括单房控制 `run_first_room.py` 和首层控制 `run_floor.py`。首层流程复用同一连接，维护已观察地图，执行普通战斗、有限拾取、Boss 和正常离层，并独立确认第二层可行动。原生入房暂停与战中异步诊断均有预算。

P0–P6 的四项审查缺陷已修复并加入回归。道具采用固定 EID 说明与有样本身份的本地经验，支持范围仍有限；主动实验关闭。M2-B 已完成巧克力牛奶的模型时序程序、实机清房和另一个进程复用，采用显式语法恢复，范围与全部失败见 [M2-B 验收](docs/evidence/phase-2b-validation.zh-CN.md)。首层覆盖见 [M2-A 证据](docs/evidence/phase-2a-validation.zh-CN.md)，操作见 [首层运行手册](docs/phase-2a-runbook.zh-CN.md)。

## 验证与付费 API 原则

**现阶段以证明目标的端到端可行性为验收目标，避免为过度严谨的实验消耗 DeepSeek 付费 API。**

- 优先使用本地单元测试、模拟 TCP、已有轨迹离线重放和规则控制器排查问题；根据改动影响选择验证，复用仍适用的已有证据。
- 付费调用仅用于必要的接入检查、少量完整流程和确有需要的异常诊断。一次真实完成目标可作为初步可行性证据；只有新问题或相关代码改动才追加针对性验证。
- 启动器、日志、文档等改动不默认触发 DeepSeek 批量回归。重复运行相同种子不会自动训练模型，也不能增加独立样本量。
- 不默认执行大样本 LLM 回归、LLM/规则配对实验或消融。此类实验先记录，后续单独确定研究问题、样本量和 API 调用预算再开展。
- 可行性通过不等于稳定胜率、LLM 优于规则、竞速水平或 SOTA 已获证明。失败、失血、超时、降级与未完成批次均保留，不能只展示成功案例。
- 需要停止批次时，使用 `--stop-file`，在当前单场收尾并保存报告后停止后续样本。
- DeepSeek 客户端默认显式关闭思考，避免继承供应商默认设置后只消耗输出额度而没有最终 JSON。机制开发按任务持久记账，默认最多 3 次实际请求；原语不足、空最终内容或认证错误先停止，不盲目修复。新增额度需要明确授权，不能靠重启或创建重复任务重置。

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
# 首层规则流程，不调用 DeepSeek
.\.venv\Scripts\python.exe run_floor.py --local-only

# 首层 DeepSeek 配置；将付费请求上限降到 1
.\.venv\Scripts\python.exe run_floor.py --llm-requests 1

# 第一阶段单房诊断
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

查询单个道具后，会将该道具的说明、源版本和解析状态存入 `artifacts/knowledge/queried-items.sqlite`。后续查询及重启脚本优先复用，规划和组件开发均可取得正文；查询本身不调用 DeepSeek。项目用途配置位于 `configs/knowledge/eid-package.json`，与上游来源权利元数据分开保存，不修改原始来源包。

```powershell
# 收藏品 69；首次从固定本地 EID 包读取并入库，之后命中单道具知识库
.\.venv\Scripts\python.exe query_item.py 69 --purpose component_development

# 查看当前数据集已入库条目（最多 100 条）
.\.venv\Scripts\python.exe query_item.py --list

# 其他类别：--kind trinket / card / pill_effect；默认中文，缺失则回退英文
# --refresh 仅重新读取固定本地来源并更新该条，不联网更新 EID
```

道具按 ID 查询；动态/运行时说明不保证覆盖，缺资料或不完整解析会明确返回状态。`partial` 表示已有说明而条件/协同未穷尽，并不等于查询失败；未知 ID 不制造知识条目。SQLite 写入失败返回错误，不宣称入库成功。安装包缺失时先执行准备脚本。排错与复现见 [EID 单道具验证](docs/evidence/eid-item-knowledge-validation.zh-CN.md)。

`--with-items` 启用实验性的本地说明、拾取、避让和被动记录。原有收藏品映射为 1/4/12 的简单属性、20 的飞行以及不使用主动效果的 105；启动时加载已发布的 [机制库](skills/README.md) 后，可在校准匹配的基础 Isaac 构筑下接纳 69。69 与其他属性道具/饰品/临时效果的组合仍拒绝；不能把已发布程序等同于全道具支持。未知机制、复杂武器和健康交易明确拒绝。普通资源按已验证子型处理，首层另支持有限红/魂心；这仍是有限策略。边界及历史审查见 [准备状态](docs/phase-2a-preparation.zh-CN.md)。EID 来源包与智能体经验分存于 `artifacts/knowledge/` 和 `artifacts/agent-state/agent.sqlite`，均不进入 Git；遭遇、决策前态、拾取确认、房间结果、测量、结论和反例可追溯。

开发用实机核验入口为 `scripts/validate_m2_preparation_live.py`：会自动重开固定场景，并在记录的初始化环节生成道具，再正常拾取和战斗。其结果仅验证机制，不是正常随机对局胜率；不作为日常启动命令。

确需验证 LLM 完整流程时，在不入库的 `.env.local` 配置 `DEEPSEEK_API_KEY` 和 `DEEPSEEK_MODEL`，再单次执行 `run_first_room.py`。原始运行数据保存在 `artifacts/runs/`；凭据、游戏文件和存档二进制不进入 Git。

项目运行入口优先采用 `.env.local` 中显式填写的 DeepSeek 配置，覆盖继承的同名进程环境值；未填写的配置仍可来自环境。更换 key 后重新启动脚本即可，新建客户端读取新值；已存在的客户端不会自动换 key。无需为更新配置发送付费请求，实际 API 授权是否有效需在下一次必要调用中确认。

批次入口 `run_phase1_batch.py` 未加 `--local-only` 时会调用付费模型，当前不将其列为常规验收步骤。

## 机制程序开发（M2-B）

启用道具功能的正常流程遇到未支持且已识别的道具时，记录带构筑和观测身份的缺口任务。开发进程消费任务；程序经过有界解释器、独立检查和实机试用后才能发布，后续会话加载，战斗中不热替换。

```powershell
# 本地列出当前首例支持的 69 开发任务，不调用 API
.\.venv\Scripts\python.exe develop_mechanism.py list

# 仅对尚未完成、范围匹配且预算未耗尽的任务发起开发；会调用付费 API
.\.venv\Scripts\python.exe develop_mechanism.py develop <task_id>

# 已启动并安装当前模组的游戏中验证已发布程序的跨进程复用；不调用 API
# 开发初始化会重开固定场景并给予 69，然后正常出门和战斗
.\.venv\Scripts\python.exe scripts/validate_mechanism_live.py --reuse
```

已完成任务返回历史产物，不重新生成；当前激活版本另由技能索引和隔离记录决定。任务账本、请求/原文/恢复/检查位于 `artifacts/skill-development/<task_id>/`；不可变发布程序位于 `skills/`。模型编写蓄力条件与状态，宿主维护权限、预算、几何、安全和 Judge。首例原始 JSON 多了一个闭括号，通过唯一类型化语法恢复接纳，未人工改写逻辑；不是无错误输出的证明。

当前 EID 正文可进入规划和组件开发上下文，并在首次查询后保存到单道具知识库；说明始终标为未审核，不能自动放行未知机制。历史 M2-B 实机验收使用的是本地测量，EID 当时只提供身份/来源引用；该证据不证明模型读过 EID。自然生成底座的整条实机拾取链未由 M2-B 单独验证。固定场景的可行性不代表高效打法或稳定胜率；详见验收中的停滞、恢复和费用记录。

## 文档导航

完整目录及文档职责见 [docs 文档索引](docs/README.md)。M2-B 的实现边界从计划与验收进入，后续 M3-A 见阶段路线；当前首层能力见运行手册和验收证据。历史验收只证明对应版本及场景，不覆盖新发现的缺陷。

| 入口 | 解决的问题 |
|---|---|
| [任务契约](docs/task-contract.zh-CN.md) / [总体架构](docs/architecture-plan.zh-CN.md) | 做什么，哪些辅助允许，模块如何分工 |
| [P0–P6 实现审查](docs/phase-2a-preparation.zh-CN.md) | 当前能做什么，四个已复现问题及准入状态 |
| [M2-A 详细实施计划](docs/phase-2a-plan.zh-CN.md) | A0–A7 的模块、预算与验收合同 |
| [M2-B 详细计划](docs/phase-2b-plan.zh-CN.md) / [验收证据](docs/evidence/phase-2b-validation.zh-CN.md) | 蓄力时序程序的合同、发布复用、全部失败、语法恢复和 API 总账 |
| [首层运行手册](docs/phase-2a-runbook.zh-CN.md) / [验收证据](docs/evidence/phase-2a-validation.zh-CN.md) | 实际入口、完整首层证明及覆盖限制 |
| [第二、三阶段路线](docs/phase-2-3-roadmap.zh-CN.md) | M2-B 模型开发组件与 M3 连续推进至 Mom 的边界 |
| [运行手册](docs/phase-1-runbook.zh-CN.md) | 已实现入口的配置、启动和排错 |
| [EID 与本地经验](docs/eid-local-learning-plan.zh-CN.md) / [知识 SDK](knowledge_project/README.md) | 说明、观测和学习证据如何分开，当前接口如何使用 |
| [协作约定](AGENTS.md) | 开发、验证、主动讨论设计与文档交付原则 |

`docs/evidence/` 保存版本化验收证据，`docs/archive/` 保存早期阶段方案，`docs/reference/` 保存源码参考；当前任务不以归档文档的旧清单为执行依据。
