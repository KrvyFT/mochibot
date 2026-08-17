---
name: memory
description: 维护长期关系认知，按需查找已保存的具体记忆
type: tool
locked: true
---

## Tools

### update_core (resident)
修订每轮都能看到的长期关系认知。Core 是一份简短的自由文本，不是日记；提交整理后的完整文档即可。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| content | string | yes | 修订后的完整 Core 文本 |

### recall_memory (on_demand)
搜索已保存的用户记忆。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| query | string | no | 搜索关键词 |

### list_memories (on_demand)
列出已保存的记忆。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| limit | integer | no | 最大返回条数（默认 30） |
| offset | integer | no | 从第几条开始（默认 0）；结果中的 next_offset 可用于继续读取 |

### delete_memory (on_demand)
按 ID 删除一条记忆（移入回收站，30 天内可恢复）。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| memory_id | integer | yes | 要删除的记忆 ID |

### memory_stats (on_demand)
显示记忆系统统计（总数、重要记忆和回收站大小）。

无需参数。

### view_core_memory (on_demand)
读取完整 Core，在需要确认当前认知时使用。

无需参数。

### memory_trash_bin (on_demand)
查看或恢复回收站中已删除的记忆。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| action | string (enum: list, restore) | no | list（默认）或 restore |
| trash_id | integer | no | restore 时必填——要恢复的回收站条目 ID |
| limit | integer | no | list 时最大返回条数（默认 20） |
| offset | integer | no | list 时从第几条开始（默认 0）；结果中的 next_offset 可用于继续读取 |

## Usage Rules
- **update_core 每轮都可用**：Main 只维护每轮常驻的稳定 Core
- 具体 Memory Items 由后台 Lite 按聊天批次整理；Main 不直接创建条目
- Core 是持续修订的文档，不是事件流水账；新事实优先合并到已有表达，不得重复整段用户画像或创建同名 H1 区块
- 写入时提交整理后的完整文档；保持重要认识，删掉过时或重复表达
- 每个用户回合只需成功写入一次
- 若系统提示 Core 已变化，重新读取后再提交当前版本
- 管理类操作（recall / list / delete / stats / view_core / trash）按需通过 `request_tools(skills=["memory"])` 申请
