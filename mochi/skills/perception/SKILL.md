---
name: perception
description: 通过快照信息查看最近的各种情况
type: tool
locked: true
triggers: [tool_call]
---

## Tools

### look_around (resident)
通过快照信息查看最近的各种情况。默认返回全部来源概览；可读取全部或指定来源的详情，结果始终受真实上下文容量限制。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| sources | array (items: string) | no | 概览返回的 source 名；传入时读取这些来源的详情，不可重复 |
| detail | boolean | no | 是否读取详情（默认 false）；为 true 且未传 sources 时读取全部来源详情 |
