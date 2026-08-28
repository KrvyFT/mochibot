---
name: activity_pattern
interval: 60
type: context
enabled: true
requires_config: []
---

Conversation activity facts from SQLite message history.
Zero LLM calls. No external API. The observer reports counts and trends without
deciding what they mean or whether they deserve a proactive message.

## Fields
| Field | Type | Description |
|-------|------|-------------|
| today_messages | number | User messages sent today |
| yesterday_messages | number | User messages sent yesterday |
| daily_avg_7d | number | Average messages/day over prior days in the 7-day window |
| change_from_yesterday | number | Today count minus yesterday count |
| consecutive_silent_days | number | Consecutive zero-message days through today |
| today_to_7d_average | number | Today count divided by the prior-day average |
| weekly_trend | list | Per-day counts [{date, count}] for last 7 days |

## Notes
- interval=60: patterns don't change minute-to-minute, hourly check is enough
- Main may inspect the raw trend through `look_around`
