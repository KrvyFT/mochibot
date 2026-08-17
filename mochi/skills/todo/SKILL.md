---
name: todo
description: 追踪做完即结束的一次性事项，如买猫粮、约牙医或查资料
type: tool
multi_turn: true
diary_status_order: 20
sense:
  interval: 20
---

# Todo Skill

## Tools

### manage_todo (routed)
管理需要持续记到完成的一次性事项；完成或删除后不再追踪。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| action | string (enum: add, list, complete, reopen, update, delete) | yes | 要执行的操作 |
| task | string | | add 的任务描述；update 时为新的完整任务描述 |
| todo_id | integer | | complete/reopen/update 可用；delete 必须使用 ID |
| match | string | | complete/reopen/update 未给 ID 时，可提交待办原文；仅唯一规范化精确匹配才执行 |
| nudge_date | string | | add/update 的再次关注日期（YYYY-MM-DD），不是精确到点通知 |
| clear_nudge_date | boolean | | update 时设为 true，显式清除再次关注日期 |
| include_done | boolean | | 是否包含已完成项（list 用），默认 false |
