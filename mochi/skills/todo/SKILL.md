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
| action | string | yes | add / list / complete / delete / update |
| task | string | | 任务描述（add 必填，update 时为新内容） |
| todo_id | integer | | 待办 ID（complete/delete/update 必填） |
| nudge_date | string | | 希望再次关注的日期（YYYY-MM-DD），不是精确到点通知 |
| include_done | boolean | | 是否包含已完成项（list 用），默认 false |
