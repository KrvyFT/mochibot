---
name: system_update
description: "系统更新 — 按主人的请求检查并安装 MochiBot 官方 Release"
type: tool
locked: true
---

## Tools

### check_system_update (on_demand)
检查 GitHub 上最新的 MochiBot 官方 Release 和当前本地版本，不修改代码。

无需参数。

### install_system_update (on_demand)
检查并安装最新的 MochiBot 官方 Release。只接受普通主人对话中的明确更新请求；回复送达后 Mochi 会离线片刻，由外层启动器更新依赖并重新启动。

无需参数。

## Capability Context

- 系统更新只在主人当下提出检查或更新时访问 GitHub，不会每日轮询，也不会出现在 look_around Observer 中。
- `check_system_update` 只读；`install_system_update` 会安装最新的正式 Release，并在当前回复送达后重启 Mochi。
- 自动安装仅支持官方 Git 仓库的干净 `main` 分支本地安装。Docker、其他 remote、开发分支或存在本地代码改动时会明确拒绝，不会覆盖用户代码。
