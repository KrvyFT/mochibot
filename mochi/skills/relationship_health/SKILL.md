---
name: relationship_health
description: 用 RQI / ACS / LLMI 模型给一段关系的健康度打分，并跟踪历次评估的走向
type: tool
triggers: [tool_call, cron]
diary_status_order: 45
---

# Relationship Health

把八个维度的判断合成关系质量指数（RQI），按依恋类型查依恋兼容分（ACS），按爱的语言查错配指数（LLMI），并与历史快照比较趋势（RMM）。

模型移植自 partner-skill 项目的量化层。

## Tools

### assess_relationship_health (on_demand)
按你判断的维度分数计算 RQI / ACS / LLMI，存一次快照，并给出与历史评估的趋势对比。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| dimensions | array (items: object{dimension:string,score:number}) | yes | 有证据支撑的维度评分，score 为 0-10。dimension 取值：communication_quality 沟通质量、emotional_intimacy 情感亲密、conflict_resolution_capacity 冲突修复能力、love_language_alignment 爱的语言契合、mutual_support_index 相互支持、shared_values_alignment 价值观一致、autonomy_togetherness_balance 自主与共处平衡、physical_intimacy 身体亲密。没证据的维度不要填 |
| subject | string | no | 这段关系的称呼，用于区分多段关系和归集历史。不填 = 默认关系 |
| attachment_self | string | no | 一方的依恋类型：secure / anxious / avoidant / fearful，或中文安全型 / 焦虑型 / 回避型 / 恐惧回避型 |
| attachment_other | string | no | 另一方的依恋类型，与 attachment_self 同时给出才能算 ACS |
| love_language_self | string | no | 一方的主要爱的语言：words_of_affirmation / quality_time / acts_of_service / physical_touch / receiving_gifts，或对应中文 |
| love_language_other | string | no | 另一方的主要爱的语言，与 love_language_self 同时给出才能算 LLMI |
| note | string | no | 这次评估的依据摘要，用于日后回看当时为什么这么判 |

### relationship_health_history (on_demand)
查看某段关系历次评估的分数、分档和趋势；不指定 subject 时列出所有已评估的关系。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| subject | string | no | 关系称呼。不填 = 列出全部已评估关系的清单 |
| limit | integer | no | 最多返回多少次评估，默认 20 |

## Capability Context

- 维度分数由你依据证据判断后传入，工具只做加权合成、查表和与历史快照比对，本身不推断关系状况
- 分数是排序刻度而非测量值：权重与矩阵是基于文献的手工标定。RQI 6.8 意为「比 5.2 好、比 8.1 差」，不是「健康度 68%」，ACS 与 LLMI 同理
- 只传有证据的维度。工具按实际传入的维度重新归一化权重并报告覆盖率，覆盖率低于 50% 时不给健康分档
- 依恋类型或爱的语言无法识别时，对应指数返回空而非默认值，此时 RQI 不做依恋修正
- assess 每次都会写入一条持久快照；累计两次以上才能算出趋势
- 用户让你评估时调用 assess_relationship_health；每日早上系统也会静默评一次默认关系，不向用户发分数
- 默认关系的每次有效评估会改写相处文稿（行为准则、深层人格、关系互动）。那是行为指导，不是台词，也不要向用户报出数字。Core 只保留身份与长期事实
