# isaac-knowledge

独立安装的 EID 导入器和只读 Python SDK，接口为 Isaac Knowledge Contract 1.0。包不导入游戏控制器，不执行 Lua，不直接写智能体经验数据库。

在父项目根目录安装并准备固定包：

```powershell
uv pip install --python .venv/Scripts/python.exe -e knowledge_project
.\.venv\Scripts\python.exe scripts/prepare_eid_knowledge.py
```

也可独立运行 `python -m isaac_knowledge SOURCE_ROOT NEW_DESTINATION`。输入为固定 EID 提交中的 `descriptions/ab+/{en_us,zh_cn}.lua` 与 `descriptions/rep/{en_us,zh_cn}.lua`。父项目准备脚本额外核对已锁定的源文件哈希；导入器只处理静态字面量行，Repentance 覆盖基础说明，保留标记和未解析状态。

发布目录包含 `manifest.json`、只读消费的 `knowledge.sqlite` 和查询 JSON Schema；构建拒绝覆盖现有目录。dataset_id 绑定来源哈希、解析器、目标版本和内容使用策略，SQLite 校验值在打开 SDK 时验证。SDK 使用只读短连接，查询不联网。

```python
from isaac_knowledge import KnowledgePackage, LookupRequest
package = KnowledgePackage("path/to/package")
result = package.lookup(LookupRequest(
    request_id="example", dataset_id=package.dataset_id,
    subjects=[{"kind": "collectible", "id": 1}], purpose="display"))
```

实现 `describe/lookup/lookup_interactions/get_evidence`。协同查询明确返回未知和非穷尽覆盖；条件与运行时描述没有执行或展开，静态文本不能作为已审核的可执行机制。未实现搜索、爬虫和服务端写接口，describe 会声明缺失。

原始第三方文本和全量 SQLite 包留在父项目忽略目录。来源使用策略为 unknown 时，规划/组件开发查询不输出正文和未审核文本声明；display 可在本地查看。取得合适使用依据后，策略变更应发布新的不可变数据包，不能直接修改运行中的 manifest。

合同与项目接入说明见父项目的 [知识库接口计划](../docs/knowledge-project-interface-plan.zh-CN.md) 和 [EID 学习计划](../docs/eid-local-learning-plan.zh-CN.md)。
