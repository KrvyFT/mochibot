---
name: skill_management
description: "配置管理 — 查看与调整 Agent 自身运行设置，或管理已安装技能"
type: tool
locked: true
---

# Skill Management

## Capability Context

- `list_skills`、`get_skill_config` 和 `manage_agent_settings(action=view)` 只读取当前状态；其余写操作会改变后续运行。
- 启停或改配置属于用户授权边界：只有用户对具体技能和改动的明确意图才授权写操作。核心技能在执行层无法关闭。
- `manage_agent_settings(action=set)` 只接受用户当前对话中的明确调整意图；系统主动回合不能借此改变自己的运行节奏。
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
查看某个已安装技能的专属配置项及当前值；不用于 Agent 自身运行设置。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| skill_name | string | yes | 技能名称 |

### set_skill_config (on_demand)
修改某个已安装技能的专属配置值；不用于 Agent 自身运行设置。传空 value 可清除自定义值、恢复默认。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| skill_name | string | yes | 技能名称 |
| key | string | yes | 配置项名称 |
| value | string | yes | 新值（空字符串=清除自定义值） |

### manage_agent_settings (resident)
查看或调整 Agent 自身运行设置。用户觉得你主动联系太频繁或太少、希望改变陪伴节奏时，先用 `view` 了解当前可调项，再用 `set` 修改。系统主动回合只能查看，不能修改。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| action | string (enum: view, set) | yes | 查看或修改 |
| key | string | no | `view` 返回的设置名；set 时必填 |
| value | integer | no | 新值；set 时必填 |
