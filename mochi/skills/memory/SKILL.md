---
name: memory
description: 维护长期关系认知，按需查找已保存的具体记忆
type: tool
locked: true
---

## Tools

### update_core (resident)
修订每轮都能看到的长期关系与扮演认知。Core 是一份简短的自由文本，不是日记；提交整理后的完整文档即可。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| content | string | yes | 修订后的完整 Core 文本 |

### recall_memory (on_demand)
搜索已保存的核心记忆。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| query | string | no | 搜索关键词 |
| tag | string | no | 按标签筛选：事实 / 情感 / 偏好 / 事件 / 习惯 / 关系 |

### list_memories (on_demand, adaptive)
列出已保存的核心记忆。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| limit | integer | no | 最大返回条数（默认 30） |
| offset | integer | no | 从第几条开始（默认 0）；结果中的 next_offset 可用于继续读取 |
| tag | string | no | 按标签筛选：事实 / 情感 / 偏好 / 事件 / 习惯 / 关系 |

### delete_memory (on_demand)
按 ID 删除一条核心记忆（移入回收站，30 天内可恢复）。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| memory_id | integer | yes | 要删除的记忆 ID |

### memory_stats (on_demand, adaptive)
显示记忆系统统计（总数、重要记忆和回收站大小）。

无需参数。

### view_core_memory (on_demand, adaptive)
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
- 具体核心记忆 / 临时记忆由后台 Lite 按聊天批次整理；Main 不直接创建条目
- 临时记忆只在今天有效，系统会自动注入；日记用 `write_diary` 记当日叙事
- Core 是持续修订的文档，不是事件流水账；新事实优先合并到已有表达，不得重复整段用户画像或创建同名 H1 区块
- 若 Core 含「以上设置不要删除覆写」，该行及以上是身份钉住段：必须原样保留，不得压缩、改写或删除；只整理该行之后的相处立场与跨日长期事实
- 禁止写入 `# 今日近况`、带时刻的当日流水，或只在今天成立的琐事；当天经历留给 Diary / 临时记忆
- 工具实现、临时计划和运行流程属于功能状态，不写入 Core；标记之后只保留稳定的用户事实、偏好与关系认识
- 写入时仍提交完整文档（钉住段 + 整理后的后半）；保持重要认识，删掉过时或重复表达
- 每个用户回合只需成功写入一次
- 若系统提示 Core 已变化，重新读取后再提交当前版本
- 管理类操作（recall / list / delete / stats / view_core / trash）按需通过 `request_tools(skills=["memory"])` 申请
