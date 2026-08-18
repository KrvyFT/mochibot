---
name: habit
description: 追踪每天或每周反复完成的目标与次数；同一请求还要求定时提醒时也需要 habit
type: tool
multi_turn: true
diary_status_order: 10
---

# Habit Skill

追踪需要长期坚持的习惯（如运动、喝水、学习）。用户通过聊天打卡，日记状态面板实时反映进度。

## Tools

### query_habit (routed)
读取习惯的今日进度或历史统计，例如当天打卡和月度跑步次数。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| action | string (enum: list, stats) | yes | list = 今日进度；stats = 历史统计 |
| habit_id | integer | no | 习惯 ID（仅 stats 需要） |

### checkin_habit (routed)
记录或撤销一次已经完成的习惯；“打算做”或“晚点做”不算完成。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| action | string (enum: checkin, undo_checkin) | yes | 操作类型 |
| habit_id | integer | no | 习惯 ID；与 habit_name 二选一 |
| habit_name | string | no | 唯一、精确的习惯名称；与 habit_id 二选一 |
| note | string | no | 备注 |
| count | integer | no | 打卡次数（默认 1） |

### edit_habit (routed)
创建或调整需要反复追踪的长期习惯，包括频率、重要性、暂停和恢复。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| action | string (enum: add, remove, pause, resume, update) | yes | 操作类型 |
| habit_id | integer | no | 习惯 ID（remove/pause/resume/update 与 habit_name 二选一） |
| habit_name | string | no | 唯一、精确的现有习惯名称（remove/pause/resume/update 可替代 habit_id） |
| name | string | no | 习惯名称（add 必填；update 可选） |
| cycle | string (enum: daily, weekly) | no | 统计周期（add 必填；update 可选） |
| target | integer | no | 每个周期的目标次数，默认 1 |
| weekdays | array | no | 仅 weekly 使用；可选 mon、tue、wed、thu、fri、sat、sun，省略表示不限星期 |
| category | string | no | 分类标签（如 health、pet、study） |
| importance | string | no | important 或 normal（默认 normal） |
| context | string | no | 时间安排备注 |
| until | string | no | 暂停截止日期（ISO 格式），默认 7 天 |
