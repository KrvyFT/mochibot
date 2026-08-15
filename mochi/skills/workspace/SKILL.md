---
name: workspace
description: "日记读写、markdown 文件编辑 — 写日记、查日记、编辑 data 目录下的 md 文件"
type: tool
locked: true
---

# Workspace Skill

日记读写 + data 目录 markdown 文件编辑。

## Tools

### write_diary (resident)
向今天的日记追加一段经历或感受，例如心情起伏、争执和状态变化。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| entry | string | yes | 日记内容 |

### read_diary (on_demand)
读取今天或指定日期的日记归档，为回顾当天经历提供原始记录。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| date | string | no | YYYY-MM-DD 格式。不填 = 今天 |

### edit_file (on_demand)
读写 data/ 目录下的 markdown 文件。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| action | string (enum: read, write) | yes | read = 读取内容, write = 覆盖写入 |
| path | string | yes | 相对于 data/ 的文件路径（如 draft.md） |
| content | string | no | write 时的新内容。action=write 时必填 |

## Capability Context

- `write_diary` 追加今日日记；habit、todo 和 meal 的结构化状态由各自技能维护，重复写入日记会留下两份事实。
- `read_diary` 不带日期时读取今天，带 `YYYY-MM-DD` 时读取对应归档。
- `edit_file` 直接读取或覆盖 `data/` 内的 Markdown 文件。路径逃逸和非 `.md` 文件会被执行层拒绝。
