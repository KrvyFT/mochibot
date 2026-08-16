"""Owner-requested official MochiBot updates."""

from __future__ import annotations

from mochi.skills.base import Skill, SkillContext, SkillResult
from mochi.update_service import (
    UpdateError,
    check_for_update,
    stage_update,
    validate_installation,
)


class SystemUpdateSkill(Skill):
    async def execute(self, context: SkillContext) -> SkillResult:
        if context.source != "chat":
            return SkillResult(
                output="系统更新只接受主人当前对话中的明确请求。",
                success=False,
            )
        if context.args:
            return SkillResult(output="系统更新工具不接受参数。", success=False)

        from mochi.config import OWNER_USER_ID

        if OWNER_USER_ID and context.user_id != OWNER_USER_ID:
            return SkillResult(output="只有主人可以更新 MochiBot。", success=False)

        try:
            release = await check_for_update()
        except UpdateError as exc:
            return SkillResult(output=str(exc), success=False)

        if context.tool_name == "check_system_update":
            if not release.available:
                return SkillResult(
                    output=f"当前已经是最新正式版 v{release.current_version}。"
                )
            notes = f"\n\n更新说明：\n{release.notes}" if release.notes else ""
            return SkillResult(
                output=(
                    f"发现官方正式版 v{release.version}，"
                    f"当前是 v{release.current_version}。{notes}"
                )
            )

        if context.tool_name != "install_system_update":
            return SkillResult(output=f"Unknown tool: {context.tool_name}", success=False)
        if not release.available:
            return SkillResult(
                output=f"当前已经是最新正式版 v{release.current_version}。"
            )
        try:
            validate_installation(require_clean=True)
            stage_update(
                release,
                user_id=context.user_id,
                channel_id=context.channel_id,
                transport=context.transport,
            )
        except UpdateError as exc:
            return SkillResult(output=str(exc), success=False)

        def _after_delivery() -> None:
            from mochi.shutdown import UPDATE_EXIT_CODE, request_process_exit

            request_process_exit(UPDATE_EXIT_CODE)

        return SkillResult(
            output=(
                f"已准备从 v{release.current_version} 更新到官方正式版 "
                f"v{release.version}。请先告诉主人即将短暂离线；"
                "当前回复送达后才会开始更新和重启。"
            ),
            summary=f"Scheduled MochiBot update to v{release.version}.",
            state_changed=True,
            after_delivery=_after_delivery,
        )
