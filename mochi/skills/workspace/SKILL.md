---
name: workspace
description: 记录和回顾日记，按需读写 Markdown 文件
type: tool
locked: true
multi_turn: true
---

# Workspace Skill

日记读写 + data 目录 markdown 文件编辑。

## Tools

### write_diary (resident)
自由修订今天正文；完整提交“今日日記”；日期页头和状态区由系统管理；正文结构、分段、时间由 Main 决定。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| content | string | yes | 修订后的完整“今日日記”正文，不包含当天日期页头 |

### read_diary (on_demand)
读取今天或指定日期的日记归档，为回顾当天经历提供原始记录。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| date | string | no | YYYY-MM-DD 格式。不填 = 今天 |

### list_files (routed)
列出 data/ 内可读写的 Markdown 文件；结果有系统数量与目录深度边界。

无需参数。

### edit_file (routed)
按用户需要读取或覆盖 Markdown 文件。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| action | string (enum: read, write) | yes | read = 读取内容, write = 覆盖写入 |
| path | string | yes | 相对于 data/ 的文件路径（如 draft.md） |
| content | string | no | write 时的新内容。action=write 时必填 |
