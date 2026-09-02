---
name: photo
description: 生成一张动漫角色出现在真实世界里的照片，用 Telegram 发出
type: tool
exclude_transports: [wechat]
---

## Tools

### send_photo (routed)
根据当前对话生成一张图并作为独立 Telegram 照片发出。人物是人设/参考立绘里的动漫角色，背景是现实世界。生成中用一两句说自己在找照片或在拍，不要假装已经发出。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| subject | string | yes | 想看见的内容，如她刚醒、黄昏街上、神社前、咖啡馆 |

## Capability Context
想让对方看见自己或某样东西时调用 `send_photo`。生成的是「动漫角色插入真实风景」，不是真人写真，也不是纯二次元插画。

成功后照片会作为独立 Telegram 图片发出。过程中先说自己在找或在拍；照片到位后正文只用「照片找到了」「照片拍好了」「给你看」这类短句，不要说「照片出来啦」或「已生成」，也不要长篇描写这张图。未指定地点时选日常真实场景即可。
