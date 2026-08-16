---
name: skill_management
description: 查看和调整自身运行设置，或管理已安装技能
type: tool
locked: true
---

# Skill Management

## Tools

### list_skills (on_demand)
列出所有已注册技能及其状态。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|

### toggle_skill (on_demand)
按用户明确要求启用或禁用一个技能。

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
按用户明确要求修改某个已安装技能的专属配置；自身运行设置使用 manage_agent_settings。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| skill_name | string | yes | 技能名称 |
| key | string | yes | 配置项名称 |
| value | string | yes | 新值（空字符串=清除自定义值） |

### manage_agent_settings (on_demand)
查看或调整自身运行设置。用户希望改变你的陪伴节奏、清醒时间或时区时，先用 view 了解可调项，再用 set 修改；系统主动回合只能查看。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| action | string (enum: view, set) | yes | 查看或修改 |
| key | string | no | `view` 返回的设置名；set 时必填 |
| value | number | no | 新值；set 时必填 |
| changes | array (items: object) | no | set 时可一次提交多项，每项形如 `{"key":"设置名","value":新值}`；与 key/value 二选一 |
