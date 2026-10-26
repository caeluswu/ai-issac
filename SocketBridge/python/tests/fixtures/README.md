# Test Fixtures

此目录包含测试所需的固定数据文件。

## 录制数据

### session_20260202_234038/

用于实体状态管理测试的录制会话数据。

- **录制时间**: 2026-02-02 23:40:38
- **时长**: 约 3 分钟 (187.79 秒)
- **总帧数**: 4989
- **协议版本**: 2.1

该数据用于测试：
- `TestEntityStateWithReplayData.test_replay_with_entity_state`
- `TestEntityStateWithReplayData.test_entity_expiry_during_replay`
- `TestEntityStateWithReplayData.test_facade_process_message_with_replay`

## 其他文件

- `sample_messages.json` - 示例消息数据

## 战斗控制回归

以下快照用于确定性机制、几何和性能回归，不计作随机留出样本，也不证明对应房间已完成实机验收。

| 文件 | 验证内容 |
|---|---|
| `rock_stall.json` | 岩石遮挡下取得射击通道和有效进展 |
| `crowd_wall.json`、`large_wall.json` | 贴墙、围堵及大型房间的碰撞与决策开销 |
| `environment_tnt.json` | 压力板、炸弹与 TNT 连锁危险 |
| `airborne_spider.json`、`mulliboom_death.json` | 腾空落点、接触爆炸与攻击后果 |
| `corner_overlap.json` | 已有碰撞半径重叠时的退出路线 |
| `holdout_failure_03.json` | 无接触伤害实体不形成虚假扣血危险区 |
| `holdout_failure_16.json` | 无合法射击位置时，通过换位引出遮挡后的敌人 |
| `holdout_failure_44.json` | 出门导航避开火堆 |
| `nav_portal_stall.json` | 路线转弯、前进奖励和门口通过条件 |
| `empty_branch_navigation.json` | 两条实际空支路的已观测房间序列；返回已知探索前沿，不读取隐藏房间 |
| `latency_joint.json` | 实机慢帧的完整快照、实际生效计划与动作；精确查询优化不得缩减候选评估 |
| `latency_room_fields.json` | 多障碍房间的四个慢帧；冷、热缓存开销与全量地形目标选择等价性 |
| `portal_bomb_recovery.json` | 清房后门口躲炸弹；允许从已观测的开放门缝向室内退让 |

三个 `holdout_failure_*` 来自已暴露的实验失败，属于开发回归。`latency_joint.json` 来自 `artifacts/runs/batch-20261026T005936569707Z/sample-08/trace.jsonl` 的帧 402；计划取该帧之前已安装的 `explosive_clear`，不能用稍后安装的重规划覆盖它。原始轨迹保留在本地运行目录。
