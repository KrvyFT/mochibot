"""Unauthenticated debug endpoints for local relationship scoring.

Intentionally open: no token dependency. Only expose non-destructive scoring
helpers used by the admin Debug page.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)


class DebugScoreBody(BaseModel):
    """Manual dimension scores for a debug assessment write."""

    dimensions: dict[str, float] = Field(default_factory=dict)
    acs: float | None = None
    llmi: float | None = None
    note: str = "debug"
    refresh_voice: bool = True


def _serialize_result(result, momentum=None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "rqi": result.score,
        "tier": result.tier,
        "tier_summary": result.tier_summary,
        "coverage": result.coverage,
        "acs": result.acs,
        "acs_modifier": result.acs_modifier,
        "llmi": result.llmi,
        "strongest": result.strongest,
        "weakest": result.weakest,
        "dimensions": [
            {
                "key": d.key,
                "label": d.label,
                "score": d.score,
                "weight": d.weight,
                "contribution": d.contribution,
            }
            for d in result.dimensions
        ],
        "missing": list(result.missing),
    }
    if momentum is not None:
        payload["momentum"] = {
            "trajectory": momentum.trajectory,
            "delta": momentum.delta,
            "description": momentum.description,
        }
    return payload


def register_debug_routes(app: FastAPI) -> None:
    """Mount open debug routes (no auth dependency)."""

    @app.get("/api/debug/relationship")
    async def debug_relationship_get():
        from mochi.config import OWNER_USER_ID
        from mochi.relationship_model import (
            DIMENSION_LABELS,
            RQI_WEIGHTS,
            _RQI_TIERS,
            compute_rqi,
        )
        from mochi.skills.relationship_health.queries import (
            DEFAULT_SUBJECT,
            get_assessments,
            get_latest_assessment,
        )

        if not OWNER_USER_ID:
            raise HTTPException(400, "OWNER_USER_ID 未设置")

        latest = get_latest_assessment(OWNER_USER_ID, DEFAULT_SUBJECT)
        preview = None
        if latest:
            try:
                scores = json.loads(latest["dimensions_json"] or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                scores = {}
            if isinstance(scores, dict) and scores:
                try:
                    preview = _serialize_result(
                        compute_rqi(
                            scores,
                            acs=latest.get("acs"),
                            llmi=latest.get("llmi"),
                        )
                    )
                except ValueError:
                    preview = None

        return {
            "subject": DEFAULT_SUBJECT,
            "user_id": OWNER_USER_ID,
            "labels": dict(DIMENSION_LABELS),
            "weights": dict(RQI_WEIGHTS),
            "tiers": [
                {"min": thr, "tier": name, "summary": summary}
                for thr, name, summary in _RQI_TIERS
            ],
            "latest": (
                {
                    "id": latest["id"],
                    "rqi": latest["rqi"],
                    "tier": latest["tier"],
                    "coverage": latest["coverage"],
                    "acs": latest.get("acs"),
                    "llmi": latest.get("llmi"),
                    "dimensions": (
                        json.loads(latest["dimensions_json"])
                        if latest.get("dimensions_json")
                        else {}
                    ),
                    "note": latest.get("note") or "",
                    "created_at": latest.get("created_at"),
                }
                if latest
                else None
            ),
            "preview": preview,
            "history_count": len(
                get_assessments(OWNER_USER_ID, DEFAULT_SUBJECT, limit=200)
            ),
        }

    @app.post("/api/debug/relationship")
    async def debug_relationship_post(body: DebugScoreBody):
        from mochi.config import OWNER_USER_ID
        from mochi.relationship_model import RQI_WEIGHTS, compute_momentum, compute_rqi
        from mochi.skills.relationship_health.handler import _commit_assessment
        from mochi.skills.relationship_health.queries import (
            DEFAULT_SUBJECT,
            get_assessments,
        )

        if not OWNER_USER_ID:
            raise HTTPException(400, "OWNER_USER_ID 未设置")

        scores: dict[str, float] = {}
        for key, raw in (body.dimensions or {}).items():
            if key not in RQI_WEIGHTS:
                raise HTTPException(400, f"未知维度: {key}")
            try:
                value = float(raw)
            except (TypeError, ValueError) as exc:
                raise HTTPException(400, f"维度 {key} 分数无效") from exc
            if not 0.0 <= value <= 10.0:
                raise HTTPException(400, f"维度 {key} 需在 0–10")
            scores[key] = value

        if not scores:
            raise HTTPException(400, "至少提交一个维度分数")

        acs = body.acs
        llmi = body.llmi
        if acs is not None and not (0.0 <= float(acs) <= 1.0):
            raise HTTPException(400, "ACS 需在 0–1")
        if llmi is not None and not (0.0 <= float(llmi) <= 1.0):
            raise HTTPException(400, "LLMI 需在 0–1")

        try:
            result = compute_rqi(scores, acs=acs, llmi=llmi)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

        note = (body.note or "debug").strip()[:500] or "debug"
        if not note.startswith("debug"):
            note = f"debug：{note}"

        if body.refresh_voice:
            assessment_id, _history, momentum = _commit_assessment(
                OWNER_USER_ID,
                DEFAULT_SUBJECT,
                result,
                acs=acs,
                llmi=llmi,
                note=note,
            )
        else:
            from mochi.skills.relationship_health.queries import record_assessment

            stored = {d.key: d.score for d in result.dimensions}
            assessment_id = record_assessment(
                OWNER_USER_ID,
                DEFAULT_SUBJECT,
                rqi=result.score,
                tier=result.tier,
                coverage=result.coverage,
                acs=acs,
                llmi=llmi,
                dimensions=stored,
                note=note,
            )
            history = get_assessments(OWNER_USER_ID, DEFAULT_SUBJECT)
            momentum = compute_momentum([row["rqi"] for row in history])

        log.info(
            "Debug relationship score written: RQI=%s tier=%s id=%s",
            result.score,
            result.tier,
            assessment_id,
        )
        return {
            "ok": True,
            "assessment_id": assessment_id,
            "voice_refreshed": bool(body.refresh_voice and result.tiered),
            **_serialize_result(result, momentum),
        }

    @app.post("/api/debug/relationship/preview")
    async def debug_relationship_preview(body: DebugScoreBody):
        """Compute RQI without writing to the database."""
        from mochi.relationship_model import RQI_WEIGHTS, compute_rqi

        scores: dict[str, float] = {}
        for key, raw in (body.dimensions or {}).items():
            if key not in RQI_WEIGHTS:
                raise HTTPException(400, f"未知维度: {key}")
            try:
                value = float(raw)
            except (TypeError, ValueError) as exc:
                raise HTTPException(400, f"维度 {key} 分数无效") from exc
            if not 0.0 <= value <= 10.0:
                raise HTTPException(400, f"维度 {key} 需在 0–10")
            scores[key] = value
        if not scores:
            raise HTTPException(400, "至少提交一个维度分数")
        try:
            result = compute_rqi(scores, acs=body.acs, llmi=body.llmi)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return _serialize_result(result)
