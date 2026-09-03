---
name: sticker
description: 用贴纸为当前表达增加情绪
type: tool
---

## Tools

### send_sticker (resident)
从已学会的库里选一张贴纸，作为独立的 Telegram 消息发出。不要在正文里假装发过贴纸。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| mood | string | yes | 简短中文情绪或语境，如开心、得意、委屈、困倦 |

### delete_last_sticker (on_demand)
删除本聊天最近发送的一张贴纸。

无需参数。

## Capability Context
库里的贴纸是真人表情。调用 `send_sticker` 才会真的发出去；文字里写「发了贴纸」或描述贴纸内容不会发出任何东西。

偶尔用：得意、撒娇、委屈、调侃、晚安等情绪对得上时发一张。不要每轮都发，也不要连着多张。`mood` 用短中文（开心、委屈、困倦），系统按标签匹配。

调用成功后不要在正文里提这张贴纸。用户刚发贴纸、你在学新图时不要抢着回贴。
