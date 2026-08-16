---
name: workspace
description: 记录和回顾日记，按需读写 Markdown 文件
type: tool
locked: true
---

# Workspace Skill

日记读写 + data 目录 markdown 文件编辑。

## Tools

### write_diary (resident)
记录今天值得留下的经历或感受；餐食、习惯和待办使用各自工具，不重复抄进日记。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| entry | string | yes | 日记内容 |

### read_diary (on_demand)
读取今天或指定日期的日记归档，为回顾当天经历提供原始记录。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| date | string | no | YYYY-MM-DD 格式。不填 = 今天 |

### edit_file (on_demand)
按用户需要读取或覆盖 Markdown 文件。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| action | string (enum: read, write) | yes | read = 读取内容, write = 覆盖写入 |
| path | string | yes | 相对于 data/ 的文件路径（如 draft.md） |
| content | string | no | write 时的新内容。action=write 时必填 |
