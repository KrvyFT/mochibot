---
name: internal_search
description: 在自己的聊天记录、Diary 和 Memory 中按关键词查找过去的信息
type: tool
---

## Tools

### search_personal_history (on_demand, adaptive)
在本地保存的聊天记录、Diary 和 Memory 中搜索关键词或短语。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| query | string | yes | 要查找的关键词或短语 |
| source | string (enum: all, conversation, diary, memory) | no | 搜索范围，默认 all |
| limit | integer | no | 每类最多返回条数，默认 5，范围 1-10 |

## Capability Context

- 搜索只读取 MochiBot 本地保存的资料，不会发起外部请求或修改记忆
- all 会分别返回聊天、Diary 和 Memory 片段；结果有固定数量与长度边界
- 聊天和 Diary 使用文字匹配，Memory 使用已有的文字召回能力
