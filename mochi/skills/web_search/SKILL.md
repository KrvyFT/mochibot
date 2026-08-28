---
name: web_search
description: 联网搜索外部信息，或读取公开 HTTPS 网页正文
type: tool
config:
  BAIDU_API_KEY:
    type: str
    secret: true
    default: ""
    description: "Optional Baidu Qianfan AI Search API key"
---

# Web Search Skill

## Tools

### web_search (routed)
在互联网上查找当前对话需要的外部信息，如新闻、价格、知识或教程。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| query | string | yes | 搜索关键词。使用最可能获得好结果的语言。 |
| max_results | integer | no | 最大返回结果数（1-10，默认 5） |
| recency | string | no | 需要近期信息时可选 week、month、semiyear 或 year；由你根据问题判断 |

### read_web_page (routed)
读取 HTTPS 网页的可读正文。适合在搜索后打开结果页面，了解摘要之外的内容。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| url | string | yes | 要读取的公开 HTTPS 页面；不支持 localhost、私网或带凭据的 URL |
