"""Tests for the RQI / ACS / LLMI scoring model.

Beyond the happy paths, these pin down the three places where the port
deliberately diverges from the source implementation, since each of those was
a case of absent evidence becoming a confident number.
"""

import itertools

import pytest

from mochi.relationship_model import (
    ATTACHMENT_STYLES,
    LOVE_LANGUAGES,
    MIN_TIER_COVERAGE,
    RQI_WEIGHTS,
    DIMENSION_LABELS,
    acs_modifier,
    classify_rqi,
    compute_acs,
    compute_llmi,
    compute_momentum,
    compute_rqi,
    love_language_alignment_score,
    normalize_attachment_style,
    normalize_love_language,
)

FULL_SCORES = {key: 7.0 for key in RQI_WEIGHTS}


# ── Table integrity ───────────────────────────────────────────────────────

def test_rqi_weights_sum_to_one():
    assert sum(RQI_WEIGHTS.values()) == pytest.approx(1.0)


def test_every_dimension_has_a_label():
    assert set(DIMENSION_LABELS) == set(RQI_WEIGHTS)


def test_acs_defined_and_symmetric_for_every_style_pair():
    for one, other in itertools.product(ATTACHMENT_STYLES, repeat=2):
        forward = compute_acs(one, other)
        assert forward is not None, f"missing ACS for {one}/{other}"
        assert 0.0 <= forward <= 1.0
        assert forward == compute_acs(other, one)


def test_llmi_defined_and_symmetric_for_every_language_pair():
    for one, other in itertools.product(LOVE_LANGUAGES, repeat=2):
        forward = compute_llmi(one, other)
        assert forward is not None, f"missing LLMI for {one}/{other}"
        assert 0.0 <= forward <= 1.0
        assert forward == compute_llmi(other, one)


def test_matching_love_languages_have_no_mismatch():
    for language in LOVE_LANGUAGES:
        assert compute_llmi(language, language) == 0.0


def test_secure_pairing_beats_every_other_combination():
    secure = compute_acs("secure", "secure")
    others = [
        compute_acs(one, other)
        for one, other in itertools.product(ATTACHMENT_STYLES, repeat=2)
        if not (one == other == "secure")
    ]
    assert secure > max(others)


# ── Normalisation: unknown must stay unknown ──────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("secure", "secure"),
    ("Secure", "secure"),
    ("安全型", "secure"),
    ("Anxious-Preoccupied", "anxious"),
    ("anxious preoccupied", "anxious"),
    ("anxious_preoccupied", "anxious"),
    ("  回避型 ", "avoidant"),
    ("FEARFUL-AVOIDANT", "fearful"),
])
def test_attachment_aliases_fold_to_canonical_keys(raw, expected):
    assert normalize_attachment_style(raw) == expected


@pytest.mark.parametrize("raw", ["", None, "不知道", "熊猫型", "unsure"])
def test_unknown_attachment_style_is_none_not_secure(raw):
    """The source defaulted to ``secure``, silently inflating every RQI."""
    assert normalize_attachment_style(raw) is None


@pytest.mark.parametrize("raw", ["", None, "不清楚", "sarcasm"])
def test_unknown_love_language_is_none_not_quality_time(raw):
    assert normalize_love_language(raw) is None


def test_unknown_inputs_yield_no_pairwise_index():
    assert compute_acs("secure", "熊猫型") is None
    assert compute_acs(None, "secure") is None
    assert compute_llmi("quality_time", "sarcasm") is None


def test_unknown_acs_applies_no_modifier():
    """Not knowing must not become a penalty or a bonus."""
    assert acs_modifier(None) == 1.0
    assert compute_rqi(FULL_SCORES, acs=None).score == pytest.approx(7.0)


def test_acs_modifier_spans_expected_band():
    assert acs_modifier(0.0) == pytest.approx(0.85)
    assert acs_modifier(1.0) == pytest.approx(1.05)
    assert acs_modifier(0.95) > acs_modifier(0.30)


def test_acs_modifier_matches_the_documented_anchors():
    """relationship_health.md states these two values explicitly."""
    assert acs_modifier(compute_acs("secure", "secure")) == pytest.approx(1.04)
    assert acs_modifier(
        compute_acs("anxious", "avoidant")
    ) == pytest.approx(0.91)


# ── RQI composition ───────────────────────────────────────────────────────

def test_uniform_scores_give_that_score_back():
    for value in (0.0, 3.5, 7.0, 10.0):
        result = compute_rqi({key: value for key in RQI_WEIGHTS})
        assert result.score == pytest.approx(value)
        assert result.coverage == pytest.approx(1.0)
        assert result.missing == ()


def test_weights_are_respected():
    """A 20% dimension must move the result four times a 5% one."""
    heavy = compute_rqi({**FULL_SCORES, "communication_quality": 10.0}).score
    light = compute_rqi({**FULL_SCORES, "physical_intimacy": 10.0}).score
    assert heavy - 7.0 == pytest.approx((light - 7.0) * 4, abs=0.02)


def test_contributions_sum_to_the_weighted_sum():
    result = compute_rqi({
        "communication_quality": 8.0,
        "emotional_intimacy": 6.0,
        "conflict_resolution_capacity": 4.0,
    })
    total = sum(d.contribution for d in result.dimensions)
    assert total == pytest.approx(result.weighted_sum, abs=0.01)
    assert sum(d.weight for d in result.dimensions) == pytest.approx(1.0, abs=0.01)


def test_partial_coverage_renormalises_instead_of_defaulting_to_five():
    """Two dimensions at 9.0 must read as 9.0, not be dragged toward 5.0."""
    result = compute_rqi({
        "communication_quality": 9.0,
        "emotional_intimacy": 9.0,
    })
    assert result.score == pytest.approx(9.0)
    assert result.coverage == pytest.approx(0.40)
    assert len(result.missing) == 6


def test_coverage_below_threshold_withholds_the_tier():
    result = compute_rqi({"communication_quality": 9.5})
    assert result.coverage == pytest.approx(0.20)
    assert result.tier == ""
    assert result.tiered is False
    assert "不足以" in result.tier_summary


def test_coverage_exactly_at_threshold_grants_a_tier():
    """0.20 + 0.15 + 0.15 lands on the boundary, which is inclusive."""
    result = compute_rqi({
        "communication_quality": 9.0,
        "conflict_resolution_capacity": 9.0,
        "love_language_alignment": 9.0,
    })
    assert result.coverage == pytest.approx(MIN_TIER_COVERAGE)
    assert result.tier == "Thriving"


def test_coverage_just_under_threshold_withholds_the_tier():
    result = compute_rqi({
        "communication_quality": 9.0,
        "conflict_resolution_capacity": 9.0,
        "mutual_support_index": 9.0,
    })
    assert result.coverage == pytest.approx(0.45)
    assert result.tier == ""


def test_llmi_derives_the_alignment_dimension():
    llmi = compute_llmi("words_of_affirmation", "receiving_gifts")
    assert llmi == pytest.approx(0.5)
    scores = {k: v for k, v in FULL_SCORES.items()
              if k != "love_language_alignment"}
    result = compute_rqi(scores, llmi=llmi)
    derived = next(d for d in result.dimensions
                   if d.key == "love_language_alignment")
    assert derived.score == pytest.approx(love_language_alignment_score(llmi))
    assert result.coverage == pytest.approx(1.0)


def test_explicit_alignment_score_wins_over_derived():
    scores = {**FULL_SCORES, "love_language_alignment": 2.0}
    result = compute_rqi(scores, llmi=0.0)
    derived = next(d for d in result.dimensions
                   if d.key == "love_language_alignment")
    assert derived.score == pytest.approx(2.0)


def test_score_is_capped_at_ten():
    result = compute_rqi({key: 10.0 for key in RQI_WEIGHTS}, acs=1.0)
    assert result.score == 10.0


@pytest.mark.parametrize("bad", [
    {"communication_quality": 11.0},
    {"communication_quality": -0.5},
    {"communication_quality": "很好"},
    {"communication_quality": None},
    {"communication_quality": float("nan")},
    {"communication_quality": float("inf")},
    {"vibes": 8.0},
    {},
])
def test_invalid_input_raises(bad):
    with pytest.raises(ValueError):
        compute_rqi(bad)


@pytest.mark.parametrize("value", [True, False])
def test_boolean_scores_are_rejected_not_coerced(value):
    """``bool`` subclasses ``int``, so True would otherwise score 1.0."""
    with pytest.raises(ValueError, match="布尔值"):
        compute_rqi({"communication_quality": value})


# ── Strength / growth identification ──────────────────────────────────────

def test_growth_area_is_not_just_the_lightest_dimension():
    """The source ranked by ``weight x score`` and so almost always named
    ``physical_intimacy`` regardless of how it actually scored."""
    scores = {**FULL_SCORES,
              "communication_quality": 2.0,
              "physical_intimacy": 9.5}
    result = compute_rqi(scores)
    assert result.weakest == "communication_quality"
    assert result.highest_leverage == "communication_quality"
    assert result.strongest == "physical_intimacy"


def test_highest_leverage_prefers_weighted_headroom():
    """A 20% dimension at 6.0 (headroom 0.80) outranks a 5% one at 1.0 (0.45)."""
    scores = {**FULL_SCORES,
              "emotional_intimacy": 6.0,
              "physical_intimacy": 1.0}
    result = compute_rqi(scores)
    assert result.weakest == "physical_intimacy"
    assert result.highest_leverage == "emotional_intimacy"


# ── Tiers ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("score,tier", [
    (10.0, "Thriving"), (8.5, "Thriving"),
    (8.4, "Healthy"), (7.0, "Healthy"),
    (6.9, "Developing"), (5.5, "Developing"),
    (5.4, "Strained"), (4.0, "Strained"),
    (3.9, "At Risk"), (0.0, "At Risk"),
])
def test_tier_boundaries(score, tier):
    assert classify_rqi(score)[0] == tier


# ── Momentum ──────────────────────────────────────────────────────────────

def test_momentum_needs_two_samples():
    for history in ([], [7.0]):
        momentum = compute_momentum(history)
        assert momentum.trajectory == "insufficient_data"
        assert momentum.delta == 0.0


@pytest.mark.parametrize("history,trajectory", [
    ([6.0, 7.0], "improving"),
    ([7.0, 6.0], "declining"),
    ([7.0, 7.3], "stable"),
    ([7.0, 6.5], "stable"),
    ([6.0, 6.5, 7.5], "improving"),
])
def test_momentum_trajectories(history, trajectory):
    assert compute_momentum(history).trajectory == trajectory


def test_momentum_reports_per_step_rate_and_sample_count():
    momentum = compute_momentum([6.0, 6.5, 7.0, 7.5])
    assert momentum.delta == pytest.approx(1.5)
    assert momentum.per_step == pytest.approx(0.5)
    assert momentum.samples == 4


def test_momentum_reads_endpoints_only():
    """Documented limitation: a full recovery is indistinguishable from calm."""
    assert compute_momentum([7.0, 3.0, 7.0]).trajectory == "stable"


# ── Stance ────────────────────────────────────────────────────────────────

def test_stance_is_withheld_when_untiered_or_healthy_stable():
    from mochi.relationship_model import derive_stance

    thin = compute_rqi({"communication_quality": 9.0})
    assert derive_stance(thin, compute_momentum([thin.score])) == ()

    healthy = compute_rqi(FULL_SCORES)
    assert healthy.tier == "Healthy"
    assert derive_stance(healthy, compute_momentum([7.0, 7.2])) == ()


def test_stance_never_mentions_scores_or_tier_names():
    from mochi.relationship_model import derive_stance

    strained = compute_rqi({key: 4.2 for key in RQI_WEIGHTS})
    declining = compute_momentum([6.0, 4.2])
    text = "\n".join(derive_stance(strained, declining))
    assert text
    for forbidden in ("RQI", "Healthy", "Strained", "At Risk", "4.2", "20%", "ACS"):
        assert forbidden not in text


def test_declining_adds_distance_and_weakest_bias():
    from mochi.relationship_model import derive_stance

    scores = {**FULL_SCORES, "conflict_resolution_capacity": 2.0}
    result = compute_rqi(scores)
    lines = derive_stance(result, compute_momentum([8.0, result.score]))
    joined = "".join(lines)
    assert "沉默" in joined or "拨开" in joined
    assert "分歧" in joined


def test_voice_follows_tier_and_never_names_the_score():
    from mochi.relationship_voice import compose_voice, starting_voice

    start = starting_voice()
    assert "# 行为准则" in start
    assert "路过" in start or "软软地待在旁边" in start
    assert "RQI" not in start
    assert "病娇" not in start
    assert "重度" not in start

    thriving = compose_voice(
        compute_rqi({key: 9.0 for key in RQI_WEIGHTS}),
        compute_momentum([8.0, 9.0]),
    )
    healthy = compose_voice(
        compute_rqi({key: 7.5 for key in RQI_WEIGHTS}),
        compute_momentum([7.3, 7.5]),
    )
    strained = compose_voice(
        compute_rqi({key: 4.2 for key in RQI_WEIGHTS}),
        compute_momentum([6.0, 4.2]),
    )
    assert thriving and healthy and strained
    assert thriving != healthy
    assert "Elma 想你了" in thriving or "再待一下下" in thriving
    assert "轻轻关心" in healthy or "有点想你了" in healthy or "明天有空" in healthy
    assert "不疾不徐" in healthy
    assert "沉默" in strained or "裂痕" in strained or "真讨厌" in strained
    for text in (thriving, healthy, strained):
        assert "RQI" not in text
        assert "Thriving" not in text
        assert "Strained" not in text


def test_voice_weakest_dimension_is_a_short_scene_not_one_slogan():
    from mochi.relationship_voice import compose_voice

    scores = {**FULL_SCORES, "emotional_intimacy": 3.0}
    text = compose_voice(compute_rqi(scores), compute_momentum([7.0, 6.5]))
    assert text and "短句陪着" in text
    assert text.count("这一阵更少主动交心") == 1
    intimacy_block = text.split("# 这一阵", 1)[1]
    assert intimacy_block.count("。") >= 3
