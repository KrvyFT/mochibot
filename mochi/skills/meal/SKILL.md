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
| items | array (items: object {name:string, calories:integer, protein_g:number, carbs_g:number, fat_g:number}) | yes | 已吃食物及逐项营养估算；总热量和宏量营养素由代码相加 |
| source | string | | `text` / `photo` / `voice`，默认 `text` |
| date | string | | YYYY-MM-DD，默认今天 |

### query_meals (routed)
读取近期餐食、记录 ID、热量摄入和营养趋势，可按日期或回看天数汇总。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| days | integer | | 回看天数，默认 1（今天）。查一周用 7。 |
| date | string | | 指定日期 YYYY-MM-DD，会覆盖 days。 |

### delete_meal (on_demand)
按查询结果中的记录 ID 删除一条明确饮食记录。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| meal_id | integer | yes | `query_meals` 返回的记录 ID |
