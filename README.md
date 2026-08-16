<div align="center">

# 🍡 MochiBot

**住在你自己设备里的轻量 AI 小机**

有长期记忆，会主动关心你，也能帮你管理习惯、提醒和日常琐事。

轻量自托管 · Telegram / 微信 · OpenAI / DeepSeek / Claude / Gemini

</div>

<img width="1440" alt="MochiBot 管理后台设置首页" src="docs/assets/admin-home.png" />

## MochiBot 简介

一款适合长期相处、属于你自己的小机。

会记住你，不会时间长了就失忆。记忆和人格随着你们聊天而增长，不需要预设。

能照顾你的日常生活：习惯打卡、待办、精确提醒、饮食记录、天气、联网搜索和 Telegram 表情包都已内置。

有自己的心跳，会主动找你，也会好奇你的一切。

有配置后台，使用简单，小白也可以上手。

支持微信和 Telegram。

## 快速开始

你需要：

- Python 3.11+
- 一个受支持的模型服务 API Key
- Telegram Bot Token，或一个可扫码登录的微信账号

```bash
git clone https://github.com/shikidmsh-rgb/mochibot.git
cd mochibot
```

- **Windows**：双击 `setup.bat`
- **macOS / Linux**：运行 `bash setup.sh`

脚本会创建独立环境、安装依赖并打开管理后台。接下来只需：

1. 添加模型并测试连接。
2. 将模型分配给 **Main** 和 **Lite**；两者可以使用同一个模型。
3. 配置 Telegram 或微信。
4. 编辑 Core，写下 Mochi 的性格和你们的关系。
5. 启动 MochiBot，然后由你发送第一条消息成为唯一的主人（Owner）。

> Bot 对外可见时，请务必先由自己发送第一条消息，避免其他人抢先成为 Owner。

## 模型支持

| 提供商 | 接入方式 | 说明 |
| --- | --- | --- |
| OpenAI / GPT | OpenAI API | 聊天；图片取决于模型能力 |
| DeepSeek | 官方兼容接口 | 聊天与图片，取决于模型能力 |
| Anthropic / Claude | Anthropic API | 聊天；图片取决于模型能力 |
| Google Gemini | 官方兼容接口 | 仅支持聊天模型 |

**Main** 是和你聊天的 Mochi，负责性格、判断和回复；**Lite** 在后台整理对话和记忆。两者可以选同一个模型。语义向量记忆是可选功能，默认不配置也能正常使用记忆。

## 主要能力

| 能力 | 能做什么 |
| --- | --- |
| 长期记忆 | 记住重要经历、关系和对话，并定期整理 |
| 主动陪伴 | 不必总等你先开口，也会跟随你的作息 |
| 习惯 | 自然语言创建、打卡、暂停和进度追踪 |
| 待办与提醒 | 一次性待办、到点提醒和循环提醒 |
| 饮食 | 记录餐食、估算营养并查询历史 |
| 搜索与天气 | 查询最新信息和当地天气 |
| 表情包 | 学习并发送 Telegram Sticker |
| 图片 | Telegram 单图理解，取决于 Main 模型能力 |
| 聊天搬家 | 从 ChatGPT 导出记录生成可预览的 Core 和记忆草稿 |
| 自助更新 | 主人提出更新时，安装 GitHub 官方正式版并自动重启 |

这些能力可以在管理后台单独开关。Mochi 会根据当前对话判断该用什么，不需要你记住固定说法。

## 聊天命令

Telegram 和微信均支持以下命令：

| 命令 | 说明 |
| --- | --- |
| `/help` | 查看命令帮助 |
| `/heartbeat` | 查看主动陪伴运行状态 |
| `/cost` | 查看今日、本月 Token 总量及各模型明细 |
| `/core` | 查看 Core |
| `/diary` | 查看今日日记 |
| `/skilloff` | 暂时进入轻量闲聊模式 |
| `/skillon` | 恢复完整能力 |
| `/reset` | 清空后续对话可见的短期上下文，保留数据库和长期记忆 |
| `/restart` | 重启 MochiBot |

除 `/help` 外，命令仅对 Owner 生效。

## 数据放在哪里

- `data/`：数据库、Core、Diary 和运行数据；备份这个目录即可保留 Mochi 的记忆。
- `.env`：Bot Token、管理后台和少量基础配置，请勿提交或分享。
- 模型 API Key：为管理后台设置访问 Token 后，会加密存入数据库。

聊天内容、图片和搜索词会发送给你选择的模型或搜索服务，但不会先经过 MochiBot 的官方服务器。

## 更新

如果是官方 Git 仓库的本地安装，可以直接在聊天里让 Mochi 更新，例如「你更新一下」。Mochi 只会在收到这类请求时检查 GitHub，不会每天轮询，也不会把版本信息放进 `look_around`。更新只安装官方正式 Release；Docker、其他 remote、开发分支或存在本地代码改动时会明确拒绝。

> 想让服务器上的 Mochi 也能按主人请求自助更新，请使用官方 Git 仓库安装，并让进程管理器启动 `scripts/start.py`。不要使用 Docker，也不要直接运行 `python -m mochi.main`。

也可以先关闭 MochiBot，再手动更新：

- **Windows**：双击 `update.bat`
- **macOS / Linux**：

```bash
git pull
source .venv/bin/activate
pip install -r requirements.txt
bash setup.sh
```

`.env` 和 `data/` 不会被代码更新覆盖，数据库变更会在启动时自动迁移。

## Docker

Docker 适合由宿主机统一维护镜像的环境，**不支持 Mochi 在聊天中自助更新**。如果无人负责服务器更新，请使用上面的官方 Git 本地安装方式。

```bash
git clone https://github.com/shikidmsh-rgb/mochibot.git
cd mochibot
cp .env.example .env
docker compose up -d
```

管理后台默认位于 `http://127.0.0.1:8080`。远程部署时请使用 SSH 隧道或带 HTTPS 的反向代理，不要直接暴露未保护的后台。

## 个性化

- 在管理后台编辑 **Core**，调整 Mochi 的性格和关系上下文。
- 每个版本改了什么：[更新记录](CHANGELOG.md)

## 许可证

[MIT](LICENSE)
