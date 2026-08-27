"""Deterministic Nightly archive, audit, and retention pipeline."""

import logging

from mochi.skills.base import Skill, SkillContext, SkillResult
from mochi.config import (
    CORE_MAX_TOKENS,
    OWNER_USER_ID,
)

log = logging.getLogger(__name__)


async def run_maintenance(user_id: int = 0) -> dict:
    """Execute deterministic Nightly work without any model calls."""
    uid = user_id or OWNER_USER_ID
    from mochi.config import (
        TRASH_PURGE_DAYS,
        logical_today,
    )
    from mochi.db import (
        cleanup_heartbeat_log,
        cleanup_old_trash,
        cleanup_proactive_log,
    )
    from mochi.diary import diary
    from mochi.knowledge_graph import cleanup_expired_triples
    from mochi.core_store import get_core_stats

    results: dict = {
        "diary": diary.rollover(logical_today()),
    }
    core_stats = get_core_stats()
    estimated_tokens = core_stats["tokens"]
    results["core_audit"] = (
        f"WARNING ({estimated_tokens}/{CORE_MAX_TOKENS})"
        if estimated_tokens > CORE_MAX_TOKENS
        else f"OK ({estimated_tokens}/{CORE_MAX_TOKENS})"
    )
    results["trash_purge"] = cleanup_old_trash(TRASH_PURGE_DAYS)
    results["proactive_log"] = cleanup_proactive_log(30)
    results["heartbeat_log"] = cleanup_heartbeat_log(30)
    results["kg_cleanup"] = cleanup_expired_triples(days=90)
    log.info("Deterministic Nightly complete: %s", results)
    return results


class MaintenanceSkill(Skill):
    """Deterministic Nightly automation."""

    async def execute(self, context: SkillContext) -> SkillResult:
        results = await run_maintenance(context.user_id)
        output = "\n".join(f"{k}: {v}" for k, v in results.items())
        return SkillResult(output=output)
