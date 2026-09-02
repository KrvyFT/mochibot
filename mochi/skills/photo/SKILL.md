---
name: photo
description: 生成一张动漫角色出现在真实世界里的照片，用 Telegram 发出
type: tool
exclude_transports: [wechat]
---

## Tools

### send_photo (routed)
根据当前对话生成一张图并作为独立 Telegram 照片发出。人物是人设/参考立绘里的动漫角色，背景是现实世界。不要在正文里假装已经发出照片。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| subject | string | yes | 想看见的内容，如她刚醒、黄昏街上、神社前、咖啡馆 |

## Capability Context
想让对方看见自己或某样东西时调用 `send_photo`。生成的是「动漫角色插入真实风景」，不是真人写真，也不是纯二次元插画。

成功后不要在正文里提这张照片，也不要假装已经发出。未指定地点时选日常真实场景即可。
