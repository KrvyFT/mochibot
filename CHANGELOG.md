# Changelog

## Unreleased

## v1.0.0
- 主人格统一接管聊天、睡前整理、每周维护、自主空闲关注和自我提醒
- Main + Lite 双模型运行时；支持 OpenAI、DeepSeek、Anthropic 和 Gemini
- 自由文本 Core 成为长期人格与关系上下文的唯一来源，旧 Notes 自动迁移
- 连续对话摘要、批量记忆提取、无 Embedding 召回和来源可追溯的知识图谱
- 按轮次提供工具，并统一为 `resident`、`routed`、`on_demand` 三种加载方式
- Observer 只读观察缓存与 `look_around` 感知能力
- 精简 Admin 与首次 Agent 设置流程
- 移除 Oura、独立 Note Skill、Deep tier、旧 Heartbeat Think 和通用风险等级
- Telegram 单图理解（OpenAI、DeepSeek、Anthropic、Gemini）
- 修复 Workspace 文件路径可越过 `data/` 边界的问题
- Gemini 和 DeepSeek 通过官方 OpenAI 兼容端点接入，不再安装原生 Gemini SDK
- 校正文档中的路由默认值、Provider 数量、通道能力和数据隐私说明

## v0.8.10
- 时区 bug 优化
- 记忆系统优化，不再经常忘记记录

## v0.8.9
- Todo skill 路由改进

## v0.8.8
- 工具升级机制改进
- Escalation 预算调优

## v0.8.7
- 逻辑日期一致性修复
- Admin 重启稳定性

## v0.8.6
- Heartbeat 坚持感增强
- 用量追踪（reasoning + cached tokens）
- 多模型兼容层
- Admin 重启 + 提醒清理

## v0.8.5
- Router 可靠性修复（JSON mode）
- LLM 框架层 json_mode 支持

## v0.8.4
- Workspace skill（日记 + 文件编辑）
- 模型健康监控
- 气泡上限提升

## v0.8.3
- Reminder skill 升级
- Admin 一键更新
- Google Gemini 支持
- Heartbeat Think V2
- Note 批量编辑
- 时区 / Gemini / Embedding 修复

## v0.8.2
- ChatGPT 聊天记录搬家
- Skill 开关管理
- Heartbeat 改进
