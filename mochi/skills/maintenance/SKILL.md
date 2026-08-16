---
name: maintenance
description: 夜间整理、审计和清理过期数据
type: automation
triggers: [cron]
locked: true
---

## Triggers
- type: cron
  schedule: 0 3 * * *
