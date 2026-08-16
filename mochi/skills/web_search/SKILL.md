---
name: web_search
description: 联网查找最新或外部信息
type: tool
---

# Web Search Skill

## Tools

### web_search (routed)
在互联网上查找当前对话需要的外部信息，如新闻、价格、知识或教程。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| query | string | yes | 搜索关键词。使用最可能获得好结果的语言。 |
| max_results | integer | no | 最大返回结果数（1-10，默认 5） |
