---
name: todo
description: "一次性待办 — 追踪到完成后即结束的事项（如买猫粮、约牙医、查资料），也承载不属于活跃习惯的一次性完成回报。"
type: tool
diary_status_order: 20
sense:
  interval: 20
---

# Todo Skill

## Capability Context

- todo 持续保留一次性事项的未完成状态，直到完成或删除；完成后不再重复出现。
- `nudge_date` 是软截止日期，到期会让 heartbeat 看见这个事项。它不是精确到点通知。
- 今日状态中的待办带有 `[todo_id=X]`，这个 ID 可直接用于完成、更新或删除，操作结果会返回真实回执。

## Tools

### manage_todo (routed)
创建、查看、完成、更新或删除需要持续追踪的一次性事项，例如买猫粮、交报告或已经完成的 PR。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| action | string | yes | add / list / complete / delete / update |
| task | string | | 任务描述（add 必填，update 时为新内容） |
| todo_id | integer | | 待办 ID（complete/delete/update 必填） |
| nudge_date | string | | 软提醒日期（YYYY-MM-DD），设置后系统会在该日期主动提醒（add/update 可用） |
| include_done | boolean | | 是否包含已完成项（list 用），默认 false |
