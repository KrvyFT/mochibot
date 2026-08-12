---
name: maintenance
description: "Nightly 确定性维护 — 归档、审计与保留期清理"
type: automation
triggers: [cron]
locked: true
---

## Triggers
- type: cron
  schedule: 0 3 * * *
