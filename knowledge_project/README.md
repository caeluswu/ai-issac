# isaac-knowledge

独立安装的 EID 导入器和 Python SDK，接口为 Isaac Knowledge Contract 1.0。来源包只读，可选择启用独立的单道具查询知识库。包不导入游戏控制器，不执行 Lua，不直接写智能体经验数据库。

在父项目根目录安装并准备固定包：

```powershell
uv pip install --python .venv/Scripts/python.exe -e knowledge_project
.\.venv\Scripts\python.exe scripts/prepare_eid_knowledge.py
```

也可独立运行 `python -m isaac_knowledge SOURCE_ROOT NEW_DESTINATION`。输入为固定 EID 提交中的 `descriptions/ab+/{en_us,zh_cn}.lua` 与 `descriptions/rep/{en_us,zh_cn}.lua`。父项目准备脚本额外核对已锁定的源文件哈希；导入器只处理静态字面量行，Repentance 覆盖基础说明，保留标记和未解析状态。

发布目录包含 `manifest.json`、只读消费的 `knowledge.sqlite` 和查询 JSON Schema；构建拒绝覆盖现有目录。dataset_id 绑定来源哈希、解析器、目标版本和来源使用策略，SQLite 校验值在打开 SDK 时验证。SDK 对来源使用只读短连接，查询不联网；可写的单道具库另存于发布目录之外。

```python
from isaac_knowledge import KnowledgePackage, LookupRequest
package = KnowledgePackage("path/to/package")
result = package.lookup(LookupRequest(
    request_id="example", dataset_id=package.dataset_id,
    subjects=[{"kind": "collectible", "id": 1}], purpose="display"))
```

实现 `describe/lookup/lookup_interactions/get_evidence`。协同查询明确返回未知和非穷尽覆盖；条件与运行时描述没有执行或展开，静态文本不能作为已审核的可执行机制。未实现搜索、爬虫和服务端写接口，describe 会声明缺失。

原始第三方文本、全量来源包和单道具查询库留在父项目忽略目录。默认构造的 SDK 在来源 `ai_input=unknown` 时仍不向规划/组件开发输出正文。父项目通过受信任的宿主配置明确允许本项目保存和使用 EID 说明，权限不来自查询请求，也不将来源权利元数据改成 allowed。显式禁止的来源仍不会被项目配置放行；未审核说明始终不能用于 build_compilation。

```python
from isaac_knowledge import KnowledgePackage, LookupRequest, QueriedItemStore
import json
from pathlib import Path

root = Path("path/to/issac")
config = json.loads((root / "configs/knowledge/eid-package.json").read_text(encoding="utf-8"))
package = KnowledgePackage(
    root / config["path"], usage_policy=config["usage_policy"],
    item_store=QueriedItemStore(root / config["local_knowledge_database"]))
result = package.lookup(LookupRequest(
    request_id="develop-69", dataset_id=package.dataset_id,
    subjects=[{"kind":"collectible", "id":69}], purpose="component_development"))
```

首次查询从来源包取出一个条目，写入独立 SQLite 后才返回成功记录；重复查询优先读单道具库，重新启动也能复用。键为 provider/dataset/kind/id/请求语言，跨用途复用原始条目，每次响应重新执行用途过滤；中文回退英文会保留标记。保存源文件哈希、原始解析记录、记录哈希和首次/最近抓取时间。来源数据集更新使用不同键，不覆盖旧版本。

`lookup(..., refresh=True)` 从固定本地来源重取并 upsert，不联网。记录损坏时自动从来源修复；写入不可用返回 `LOCAL_KNOWLEDGE_UNAVAILABLE`。不可变包目录不能作为查询库位置。`local_knowledge` 返回 cache_hits/source_reads/saved_records/cache_repairs，source_reads 计按 subject 回源次数，不是 SQL 条数。`local_usage_policy` 标明项目策略身份和哈希。项目授权策略变化不改变来源 dataset_id；来源权利或内容变化才需发布新包，不能原地编辑 manifest。

父项目提供 `query_item.py` 单条查询和 `--list` 入口，命令见 [项目 README](../README.md)。来源说明是未审核线索；条件、协同及动态说明仍可能不完整，不构成道具控制能力验证。

合同与项目接入说明见父项目的 [知识库接口计划](../docs/knowledge-project-interface-plan.zh-CN.md) 和 [EID 学习计划](../docs/eid-local-learning-plan.zh-CN.md)。
