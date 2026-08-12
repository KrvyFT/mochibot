---
name: meal
description: "饮食记录 — 记录饮食、查询历史、删除记录"
type: tool
multi_turn: true
diary_status_order: 40
writes:
  diary: [diary]
  db: [health_log]
---

# Meal Skill

Tool-only mode: `log_meal` (record meals with nutrition estimation) + `query_meals` (query meal history with daily summaries) + `delete_meal` (remove incorrect records).

## Tools

### log_meal (routed)
记录一餐的食物、估算热量和宏量营养素，适用于文字描述或食物照片。结果保留总量与逐项明细，作为后续交流的真实依据。

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

## Capability Context

- `log_meal` 写入总热量和逐项营养估算；工具回执中的明细是用户可见价值，也明确了哪些数字只是估算。
- 没有原地编辑餐食的操作。更正一条记录会产生一次删除和一次新记录，两次操作各自返回回执。
- `delete_meal` 是扩展能力，按日期和餐型删除整条记录。
