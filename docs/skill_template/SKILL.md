---
name: my_skill
description: "What this skill does — keep it concise, the pre-router reads this"
type: tool
---

## Tools

### my_tool (routed)
Describe what this tool does.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| action | string | yes | add / list / delete |
| content | string | | Item content (action=add) |
| item_id | integer | | Item ID (action=delete) |

## Capability Context

- `add` creates a persistent item and returns its ID.
- `list` is read-only.
- `delete` removes the selected item.
