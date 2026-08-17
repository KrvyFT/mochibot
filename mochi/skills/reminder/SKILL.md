---
name: reminder
description: 在明确时间联系用户，或给未来的自己留一个提醒
type: tool
multi_turn: true
diary_status_order: 30
sense:
  interval: 5
---

## Tools

### manage_reminder (routed)
管理一次性定时联系：notify 到点直接通知用户；self 到点让未来的自己结合当时情况重新判断。它不追踪事情后来是否完成。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| action | string (enum: create, list, update, delete) | yes | 操作类型 |
| kind | string (enum: notify, self) | no | create 时使用；默认 notify |
| message | string | no | notify 的提醒内容；create 必填，update 可修改 |
| intent | string | no | self 的私有回望方向，不是预写给用户的话；create 必填，update 可修改 |
| remind_at | string | no | ISO 8601 格式；create 必填，update 可修改 |
| reminder_id | integer | no | list 返回的提醒 ID（update/delete 必填） |
