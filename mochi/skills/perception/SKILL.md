---
name: perception
description: 通过快照信息查看最近的各种情况
type: tool
locked: true
triggers: [tool_call]
---

## Tools

### look_around (resident)
通过快照信息查看最近的各种情况。默认返回全部来源概览；需要了解某项详情时，从概览返回的 source 中选择 1–3 项。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| sources | array (items: string) | no | 概览返回的 source 名，最多 3 个且不可重复；空数组表示全部来源概览 |
