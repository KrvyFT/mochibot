"""Relationship health scoring exposed as MochiBot tools.

The arithmetic lives in :mod:`mochi.relationship_model`; this handler only
validates arguments, persists snapshots and renders results as material for
Main to read rather than as a report to forward.
"""

import json
import logging

from mochi.relationship_model import (
    DIMENSION_LABELS,
    RQI_WEIGHTS,
    Momentum,
    RqiResult,
    compute_acs,
    compute_llmi,
    compute_momentum,
    compute_rqi,
    describe_llmi,
    normalize_attachment_style,
    normalize_love_language,
)
from mochi.skills.base import Skill, SkillContext, SkillResult
from mochi.skills.relationship_health.queries import (
    get_assessments,
    list_subjects,
    normalize_subject,
    record_assessment,
)

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS relationship_assessments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    subject TEXT NOT NULL,
    rqi REAL NOT NULL,
    tier TEXT NOT NULL DEFAULT '',
    coverage REAL NOT NULL DEFAULT 0,
    acs REAL,
    llmi REAL,
    dimensions_json TEXT NOT NULL DEFAULT '{}',
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_relationship_assessments_subject
    ON relationship_assessments(user_id, subject, created_at);
"""


def _parse_dimensions(raw: object) -> dict[str, float]:
    """Turn the ``{dimension, score}`` list from the tool call into a mapping.

    # Errors

    Raises :class:`ValueError` with the valid keys listed, because the tool
    schema cannot express an enum inside array items and the model has to
    learn the vocabulary from the failure.
    """
    if not isinstance(raw, list) or not raw:
        raise ValueError(
            "dimensions 需要是非空数组，每项形如 "
            '{"dimension": "communication_quality", "score": 7.5}'
        )
    parsed: dict[str, float] = {}
    for entry in raw:
        if not isinstance(entry, dict):
            raise ValueError(f"dimensions 的每一项都应是对象，收到 {entry!r}")
        key = str(entry.get("dimension") or "").strip()
        if key not in RQI_WEIGHTS:
            raise ValueError(
                f"未知的关系维度：{key or '(空)'}。可用维度："
                + "、".join(f"{name}（{DIMENSION_LABELS[name]}）"
                            for name in RQI_WEIGHTS)
            )
        if key in parsed:
            raise ValueError(f"维度 {key} 重复出现，请只给一个分数。")
        parsed[key] = entry.get("score")  # type: ignore[assignment]
    return parsed


def _render_assessment(
    subject: str,
    result: RqiResult,
    momentum: Momentum,
    history: list[dict],
) -> str:
    # The reading guidance rides on the output rather than the always-on
    # capability context: it only matters at the moment these numbers are read,
    # and this tool is on-demand, so prompt budget spent on it is mostly idle.
    lines = [
        "关系健康度评估（供你参考的分析材料，不是回复模板；"
        "下面的数字、英文分档名和表格不要原样转述，用你自己的话讲结论）",
        f"对象：{subject}",
        "",
        f"RQI {result.score}/10"
        + (f"  分档 {result.tier} — {result.tier_summary}"
           if result.tiered else f"  未分档 — {result.tier_summary}"),
        f"维度覆盖率 {result.coverage:.0%}"
        + (f"（缺：{'、'.join(DIMENSION_LABELS[k] for k in result.missing)}）"
           if result.missing else "（八项齐全）"),
    ]

    if result.acs is None:
        lines.append("ACS 未知，本次 RQI 未做依恋修正")
    else:
        lines.append(
            f"ACS {result.acs:.2f} → RQI 修正系数 {result.acs_modifier}"
        )
    if result.llmi is None:
        lines.append("LLMI 未知")
    else:
        lines.append(f"LLMI {result.llmi:.2f} — {describe_llmi(result.llmi)}")

    lines.extend(["", "各维度（权重已按实际覆盖归一化）："])
    for dimension in result.dimensions:
        lines.append(
            f"  {dimension.label} {dimension.score:.1f}"
            f"  权重 {dimension.weight:.0%}"
            f"  贡献 {dimension.contribution:.2f}"
        )

    # With a single dimension all three labels name it, which reads as a bug.
    # The leverage line is only worth printing when it disagrees with the
    # weakest dimension, since that disagreement is the whole point of it.
    if len(result.dimensions) > 1:
        lines.extend([
            "",
            f"最强：{DIMENSION_LABELS[result.strongest]}",
            f"最弱：{DIMENSION_LABELS[result.weakest]}",
        ])
        if result.highest_leverage != result.weakest:
            lines.append(
                f"改善收益最大：{DIMENSION_LABELS[result.highest_leverage]}"
                "（权重 x 距满分的差距，比最弱项更值得先动）"
            )

    if momentum.samples >= 2:
        lines.extend([
            "",
            f"趋势（{momentum.samples} 次评估）：{momentum.description}"
            f" 每次平均 {momentum.per_step:+.2f}",
            "历次 RQI：" + " → ".join(
                f"{row['rqi']:.1f}" for row in history
            ),
        ])
    else:
        lines.extend(["", "这是第一次评估，还看不出趋势。"])

    return "\n".join(lines)


class RelationshipHealthSkill(Skill):
    """RQI / ACS / LLMI scoring over caller-supplied dimension judgements."""

    def init_schema(self, conn) -> None:
        conn.executescript(_SCHEMA)

    async def execute(self, context: SkillContext) -> SkillResult:
        handlers = {
            "assess_relationship_health": self._assess,
            "relationship_health_history": self._history,
        }
        handler = handlers.get(context.tool_name)
        if handler is None:
            return SkillResult(
                output=f"Unknown tool: {context.tool_name}", success=False,
            )
        try:
            return handler(context)
        except ValueError as exc:
            return SkillResult(output=f"参数有问题：{exc}", success=False)

    def _assess(self, context: SkillContext) -> SkillResult:
        args = context.args
        subject = normalize_subject(args.get("subject"))
        scores = _parse_dimensions(args.get("dimensions"))

        acs = compute_acs(
            args.get("attachment_self"), args.get("attachment_other"),
        )
        llmi = compute_llmi(
            args.get("love_language_self"), args.get("love_language_other"),
        )
        result = compute_rqi(scores, acs=acs, llmi=llmi)

        # Unrecognised inputs are reported rather than swallowed: the model
        # supplied something it believed was a style, and silence would let it
        # assume the modifier was applied.
        notices = []
        for label, raw in (
            ("attachment_self", args.get("attachment_self")),
            ("attachment_other", args.get("attachment_other")),
        ):
            if raw and normalize_attachment_style(raw) is None:
                notices.append(f"无法识别的依恋类型 {label}={raw!r}")
        for label, raw in (
            ("love_language_self", args.get("love_language_self")),
            ("love_language_other", args.get("love_language_other")),
        ):
            if raw and normalize_love_language(raw) is None:
                notices.append(f"无法识别的爱的语言 {label}={raw!r}")

        stored = {
            dimension.key: dimension.score for dimension in result.dimensions
        }
        assessment_id = record_assessment(
            context.user_id,
            subject,
            rqi=result.score,
            tier=result.tier,
            coverage=result.coverage,
            acs=acs,
            llmi=llmi,
            dimensions=stored,
            note=str(args.get("note") or ""),
        )
        history = get_assessments(context.user_id, subject)
        momentum = compute_momentum([row["rqi"] for row in history])

        output = _render_assessment(subject, result, momentum, history)
        if notices:
            output += "\n\n注意：" + "；".join(notices)

        return SkillResult(
            output=output,
            summary=(
                f"Assessed {subject}: RQI {result.score}"
                + (f" ({result.tier})" if result.tiered else " (untiered)")
                + f", coverage {result.coverage:.0%}, "
                f"{momentum.trajectory}."
            ),
            entity_refs=[f"relationship_assessment:{assessment_id}"],
            state_changed=True,
        )

    def _history(self, context: SkillContext) -> SkillResult:
        raw_subject = context.args.get("subject")
        if not (raw_subject or "").strip():
            subjects = list_subjects(context.user_id)
            if not subjects:
                return SkillResult(
                    output="还没有任何关系健康度评估记录。"
                )
            body = "\n".join(
                f"  {row['subject']} — {row['runs']} 次，最近 "
                f"{str(row['latest_at'])[:16]}"
                for row in subjects
            )
            return SkillResult(
                output=f"已评估的关系（{len(subjects)} 个）：\n{body}",
                summary=(
                    "Listed relationship subjects: "
                    + ", ".join(row["subject"] for row in subjects) + "."
                ),
            )

        subject = normalize_subject(raw_subject)
        limit = context.args.get("limit")
        history = get_assessments(
            context.user_id, subject, int(limit) if limit else 20,
        )
        if not history:
            return SkillResult(
                output=f"没有 {subject} 的评估记录。", success=False,
            )

        momentum = compute_momentum([row["rqi"] for row in history])
        lines = [
            f"{subject} 的历次评估（分析材料，不是回复模板）",
            f"趋势：{momentum.description}",
            "",
        ]
        for row in history:
            tier = row["tier"] or "未分档"
            entry = (
                f"  {str(row['created_at'])[:16]}  RQI {row['rqi']:.1f}"
                f"  {tier}  覆盖 {row['coverage']:.0%}"
            )
            if row["note"]:
                entry += f"\n      依据：{row['note']}"
            lines.append(entry)

        weakest = _weakest_recurring(history)
        if weakest:
            lines.extend([
                "",
                f"反复垫底的维度：{weakest}",
            ])

        return SkillResult(
            output="\n".join(lines),
            summary=(
                f"Relationship history for {subject}: {len(history)} "
                f"assessments, {momentum.trajectory}, latest "
                f"RQI {history[-1]['rqi']:.1f}."
            ),
        )


def _weakest_recurring(history: list[dict]) -> str:
    """Name the dimension that scored lowest most often across ``history``."""
    tally: dict[str, int] = {}
    for row in history:
        try:
            scores = json.loads(row["dimensions_json"])
        except (TypeError, ValueError):
            continue
        if not isinstance(scores, dict) or not scores:
            continue
        known = {
            key: value for key, value in scores.items() if key in RQI_WEIGHTS
        }
        if not known:
            continue
        low = min(known, key=lambda key: (known[key], -RQI_WEIGHTS[key]))
        tally[low] = tally.get(low, 0) + 1
    if not tally:
        return ""
    key = max(tally, key=lambda name: (tally[name], RQI_WEIGHTS[name]))
    if tally[key] < 2:
        return ""
    return f"{DIMENSION_LABELS[key]}（{tally[key]}/{len(history)} 次）"
