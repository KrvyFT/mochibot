---
name: meal
description: 记录和回顾饮食
type: tool
multi_turn: true
diary_status_order: 40
---

# Meal Skill

Tool-only mode: `log_meal` (record meals with nutrition estimation) + `query_meals` (query meal history with daily summaries) + `delete_meal` (remove incorrect records).

## Tools

### log_meal (routed)
记录用户已经吃过的一餐，并估算食物、热量和宏量营养素。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| meal_type | string | yes | `breakfast` / `lunch` / `dinner` / `snack` |
| items | string | yes | JSON 食物数组：`[{"name":"麻婆豆腐","calories":250,"protein_g":15,"carbs_g":8,"fat_g":18}]` |
| total_calories | integer | yes | 本餐估算总热量 |
| total_protein_g | number | | 总蛋白质克数 |
| total_carbs_g | number | | 总碳水克数 |
| total_fat_g | number | | 总脂肪克数 |
| source | string | | `text` / `photo` / `voice`，默认 `text` |
| date | string | | YYYY-MM-DD，默认今天 |

### query_meals (routed)
读取近期餐食、热量摄入和营养趋势，可按日期或回看天数汇总。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| days | integer | | 回看天数，默认 1（今天）。查一周用 7。 |
| date | string | | 指定日期 YYYY-MM-DD，会覆盖 days。 |

### delete_meal (on_demand)
按日期和餐型删除饮食记录。用于用户说记错了或想删掉的情况。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| meal_type | string | yes | `breakfast` / `lunch` / `dinner` / `snack` |
| date | string | | YYYY-MM-DD，默认今天 |
