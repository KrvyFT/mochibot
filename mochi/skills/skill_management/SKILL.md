---
name: skill_management
description: "技能管理 — 列出所有技能及状态、开关技能、查看与修改技能配置"
type: tool
locked: true
---

# Skill Management

## Capability Context

- `list_skills` 和 `get_skill_config` 只读取当前安装状态；`toggle_skill` 与 `set_skill_config` 会立即改变后续轮次可用的能力。
- 启停或改配置属于用户授权边界：只有用户对具体技能和改动的明确意图才授权写操作。核心技能在执行层无法关闭。
- 写操作的工具回执包含实际新值与生效状态，失败不会伪装成成功。

## Tools

### list_skills (on_demand)
列出所有已注册技能及其状态。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|

### toggle_skill (on_demand)
启用或禁用一个技能。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| skill_name | string | yes | 技能名称 |
| enabled | boolean | yes | true=启用, false=禁用 |

### get_skill_config (on_demand)
查看某个技能的配置项及当前值。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| skill_name | string | yes | 技能名称 |

### set_skill_config (on_demand)
修改某个技能的配置值（写入数据库，立即生效）。传空 value 可清除自定义值、恢复默认。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| skill_name | string | yes | 技能名称 |
| key | string | yes | 配置项名称 |
| value | string | yes | 新值（空字符串=清除自定义值） |
