---
name: voice
description: 用已复刻的音色合成一句短语音，作为 Telegram 语音气泡发出
type: tool
config:
  VOICE_PROVIDER:
    type: str
    default: ""
    description: "dashscope 或留空表示未配置"
  VOICE_API_KEY:
    type: str
    default: ""
    secret: yes
    description: "阿里云百炼 API Key"
  VOICE_MODEL:
    type: str
    default: "cosyvoice-v3.5-plus"
    description: "语音合成模型"
  VOICE_BASE_URL:
    type: str
    default: ""
    description: "业务空间地址，支持 compatible-mode/v1 或 wss 推理地址"
  VOICE_ID:
    type: str
    default: ""
    description: "已复刻音色的 voice_id"
---

## Tools

### send_voice (routed)
把一句短话合成语音，作为独立 Telegram 语音气泡发出。调用前后都不要说话：不要说「我录一下」「等我」「录好了你听」，也不要在正文里复述或描写这段音频。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| text | string | yes | 要说出口的那句话，短、像口语，不要长段说明 |

## Capability Context
想用声音说一句短话时调用 `send_voice`。语音气泡就是那句话。适合短、亲、不想打字的时候，不适合长说明或清单。写「发了语音」不会真的发出，必须调用这个工具。调用期间保持安静，成功后也不要再打字预告或旁白。
