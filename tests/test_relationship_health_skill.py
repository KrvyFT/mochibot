"""Relationship health skill: persistence, momentum and argument handling."""

import pytest

from mochi.skills.base import SkillContext
from mochi.skills.relationship_health.handler import RelationshipHealthSkill
from mochi.skills.relationship_health.queries import DEFAULT_SUBJECT


def _context(tool_name: str, user_id: int = 1, **args) -> SkillContext:
    return SkillContext(
        trigger="tool_call",
        user_id=user_id,
        channel_id=user_id,
        transport="telegram",
        actor="main",
        source="chat",
        turn_id="turn-relationship",
        tool_name=tool_name,
        args=args,
    )


def _dimensions(**scores) -> list[dict]:
    return [
        {"dimension": key, "score": value} for key, value in scores.items()
    ]


async def _assess(skill, **kwargs):
    return await skill.execute(_context("assess_relationship_health", **kwargs))


@pytest.mark.asyncio
async def test_skill_is_discovered_with_on_demand_tools():
    import mochi.skills as skill_registry

    on_demand = {
        tool["function"]["name"]
        for tool in skill_registry.get_tools_by_load("on_demand")
    }
    assert "assess_relationship_health" in on_demand
    assert "relationship_health_history" in on_demand

    resident = {
        tool["function"]["name"]
        for tool in skill_registry.get_tools_by_load("resident")
    }
    assert "assess_relationship_health" not in resident


@pytest.mark.asyncio
async def test_full_assessment_reports_indices_and_stores_a_snapshot():
    skill = RelationshipHealthSkill()
    result = await _assess(
        skill,
        subject="我和小雨",
        dimensions=_dimensions(
            communication_quality=8.0,
            emotional_intimacy=7.5,
            conflict_resolution_capacity=6.0,
            mutual_support_index=8.0,
            shared_values_alignment=7.0,
            autonomy_togetherness_balance=7.5,
            physical_intimacy=7.0,
        ),
        attachment_self="安全型",
        attachment_other="焦虑型",
        love_language_self="quality_time",
        love_language_other="acts_of_service",
        note="连续三周每晚复盘",
    )

    assert result.success
    assert result.state_changed
    assert result.entity_refs[0].startswith("relationship_assessment:")
    assert "RQI" in result.output
    assert "ACS 0.75" in result.output
    assert "LLMI 0.40" in result.output
    # love_language_alignment was derived from the LLMI, completing coverage.
    assert "八项齐全" in result.output
    assert "分析材料" in result.output
    assert "我和小雨" in result.summary


@pytest.mark.asyncio
async def test_unknown_attachment_leaves_rqi_unmodified_and_says_so():
    skill = RelationshipHealthSkill()
    result = await _assess(
        skill,
        dimensions=_dimensions(**{
            "communication_quality": 7.0,
            "emotional_intimacy": 7.0,
            "conflict_resolution_capacity": 7.0,
            "love_language_alignment": 7.0,
            "mutual_support_index": 7.0,
            "shared_values_alignment": 7.0,
            "autonomy_togetherness_balance": 7.0,
            "physical_intimacy": 7.0,
        }),
        attachment_self="secure",
        attachment_other="说不清",
    )
    assert result.success
    assert "ACS 未知" in result.output
    assert "RQI 7.0/10" in result.output
    assert "无法识别的依恋类型" in result.output


@pytest.mark.asyncio
async def test_partial_coverage_is_flagged_and_left_untiered():
    skill = RelationshipHealthSkill()
    result = await _assess(
        skill,
        dimensions=_dimensions(communication_quality=9.0),
    )
    assert result.success
    assert "覆盖率 20%" in result.output
    assert "未分档" in result.output
    assert "untiered" in result.summary
    # A lone dimension is simultaneously strongest and weakest; naming it
    # three times reads as a bug, so the block is withheld.
    assert "最强" not in result.output
    assert "最弱" not in result.output


@pytest.mark.asyncio
async def test_leverage_line_appears_only_when_it_differs_from_weakest():
    skill = RelationshipHealthSkill()
    # Weakest is the 5% dimension, but the 20% one has more headroom.
    diverging = await _assess(
        skill,
        dimensions=_dimensions(emotional_intimacy=6.0, physical_intimacy=1.0,
                               communication_quality=9.0,
                               conflict_resolution_capacity=9.0),
    )
    assert "最弱：身体亲密" in diverging.output
    assert "改善收益最大：情感亲密" in diverging.output

    # Here the weakest dimension is also the highest-leverage one.
    agreeing = await _assess(
        skill,
        dimensions=_dimensions(communication_quality=3.0,
                               emotional_intimacy=9.0,
                               conflict_resolution_capacity=9.0,
                               love_language_alignment=9.0),
    )
    assert "最弱：沟通质量" in agreeing.output
    assert "改善收益最大" not in agreeing.output


@pytest.mark.asyncio
async def test_momentum_appears_once_a_second_assessment_lands():
    skill = RelationshipHealthSkill()
    scores = {
        "communication_quality": 5.0,
        "emotional_intimacy": 5.0,
        "conflict_resolution_capacity": 5.0,
        "love_language_alignment": 5.0,
        "mutual_support_index": 5.0,
        "shared_values_alignment": 5.0,
        "autonomy_togetherness_balance": 5.0,
        "physical_intimacy": 5.0,
    }
    first = await _assess(skill, subject="我和小雨",
                          dimensions=_dimensions(**scores))
    assert "第一次评估" in first.output

    improved = {key: value + 1.5 for key, value in scores.items()}
    second = await _assess(skill, subject="我和小雨",
                           dimensions=_dimensions(**improved))
    assert "趋势（2 次评估）" in second.output
    assert "improving" in second.summary
    assert "5.0 → 6.5" in second.output


@pytest.mark.asyncio
async def test_history_lists_subjects_then_details():
    skill = RelationshipHealthSkill()
    empty = await skill.execute(_context("relationship_health_history"))
    assert "还没有任何" in empty.output

    await _assess(skill, subject="我和小雨",
                  dimensions=_dimensions(communication_quality=6.0,
                                         emotional_intimacy=6.0,
                                         conflict_resolution_capacity=6.0,
                                         love_language_alignment=6.0))
    await _assess(skill, dimensions=_dimensions(communication_quality=8.0,
                                                emotional_intimacy=8.0,
                                                conflict_resolution_capacity=8.0,
                                                love_language_alignment=8.0))

    listed = await skill.execute(_context("relationship_health_history"))
    assert "我和小雨" in listed.output
    assert DEFAULT_SUBJECT in listed.output

    detail = await skill.execute(
        _context("relationship_health_history", subject="我和小雨")
    )
    assert "RQI 6.0" in detail.output

    missing = await skill.execute(
        _context("relationship_health_history", subject="不存在的关系")
    )
    assert not missing.success


@pytest.mark.asyncio
async def test_assessments_are_scoped_per_user():
    skill = RelationshipHealthSkill()
    dims = _dimensions(communication_quality=9.0, emotional_intimacy=9.0,
                       conflict_resolution_capacity=9.0,
                       love_language_alignment=9.0)
    await _assess(skill, user_id=1, subject="共用名字", dimensions=dims)

    other = await skill.execute(_context(
        "relationship_health_history", user_id=2, subject="共用名字",
    ))
    assert not other.success


@pytest.mark.asyncio
async def test_recurring_weak_dimension_is_surfaced():
    skill = RelationshipHealthSkill()
    for _ in range(3):
        await _assess(
            skill,
            subject="我和小雨",
            dimensions=_dimensions(communication_quality=3.0,
                                   emotional_intimacy=8.0,
                                   conflict_resolution_capacity=8.0,
                                   love_language_alignment=8.0),
        )
    detail = await skill.execute(
        _context("relationship_health_history", subject="我和小雨")
    )
    assert "反复垫底的维度：沟通质量（3/3 次）" in detail.output


@pytest.mark.asyncio
@pytest.mark.parametrize("dimensions,expected", [
    ([], "非空数组"),
    ("communication_quality", "非空数组"),
    ([{"dimension": "vibes", "score": 8.0}], "未知的关系维度"),
    ([{"dimension": "", "score": 8.0}], "未知的关系维度"),
    ([{"dimension": "communication_quality", "score": 8.0},
      {"dimension": "communication_quality", "score": 4.0}], "重复出现"),
    ([{"dimension": "communication_quality", "score": 42}], "0-10"),
    ([{"dimension": "communication_quality", "score": "很好"}], "不是数字"),
    (["communication_quality"], "都应是对象"),
])
async def test_bad_dimension_arguments_fail_with_guidance(dimensions, expected):
    skill = RelationshipHealthSkill()
    result = await _assess(skill, dimensions=dimensions)
    assert not result.success
    assert expected in result.output


@pytest.mark.asyncio
async def test_blank_subject_falls_back_to_the_default_label():
    skill = RelationshipHealthSkill()
    result = await _assess(
        skill,
        subject="   ",
        dimensions=_dimensions(communication_quality=7.0,
                               emotional_intimacy=7.0,
                               conflict_resolution_capacity=7.0,
                               love_language_alignment=7.0),
    )
    assert DEFAULT_SUBJECT in result.output
