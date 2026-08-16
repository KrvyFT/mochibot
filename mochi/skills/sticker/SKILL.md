---
name: sticker
description: 用贴纸为当前表达增加情绪
type: tool
exclude_transports: [wechat]
---

## Tools

### send_sticker (resident)
为当前回复选择一张符合情绪或语境的贴纸。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| mood | string | yes | 简短中文情绪或语境，如开心、得意、委屈、困倦 |

### delete_last_sticker (on_demand)
删除本聊天最近发送的一张贴纸。

无需参数。
