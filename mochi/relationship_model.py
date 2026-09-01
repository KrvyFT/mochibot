"""Relationship quality scoring: RQI, ACS, LLMI and momentum.

A port of the quantitative layer from the partner-skill project
(https://github.com/NatalieCao323/partner-skill). Its weights and matrices are
hand-calibrated against the literature it cites — Gottman's stability
predictors, Bartholomew & Horowitz's attachment categories, Chapman's love
languages — rather than fitted to data. They rank and compare consistently,
which is what makes them useful, but they are not measurements: an RQI of 6.8
means "better than 5.2, worse than 8.1", not "68% healthy".

Everything here is pure — no I/O, no clock, no model calls. Dimension scores
are judgements the caller supplies; this module only combines them.

Three deliberate departures from the source implementation, each of which
turned absent information into a confident number:

* Unrecognised attachment styles and love languages return ``None`` instead of
  defaulting to ``secure`` and ``quality_time``. Defaulting paired an unknown
  partner with the most favourable attachment style and inflated every RQI.
* Missing dimensions renormalise the weights and are reported as reduced
  coverage, instead of being silently scored 5.0.
* ``weakest`` and ``highest_leverage`` replace "primary growth area", which
  ranked dimensions by ``weight x score`` and therefore almost always named
  one of the two 5%-weight dimensions no matter how they scored.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

ATTACHMENT_STYLES = ("secure", "anxious", "avoidant", "fearful")

LOVE_LANGUAGES = (
    "words_of_affirmation",
    "quality_time",
    "acts_of_service",
    "physical_touch",
    "receiving_gifts",
)

# Weights sum to 1.0 (asserted in the tests) and are ordered by weight so
# reports read top-down.
RQI_WEIGHTS: dict[str, float] = {
    "communication_quality": 0.20,
    "emotional_intimacy": 0.20,
    "conflict_resolution_capacity": 0.15,
    "love_language_alignment": 0.15,
    "mutual_support_index": 0.10,
    "shared_values_alignment": 0.10,
    "autonomy_togetherness_balance": 0.05,
    "physical_intimacy": 0.05,
}

DIMENSION_LABELS: dict[str, str] = {
    "communication_quality": "沟通质量",
    "emotional_intimacy": "情感亲密",
    "conflict_resolution_capacity": "冲突修复能力",
    "love_language_alignment": "爱的语言契合",
    "mutual_support_index": "相互支持",
    "shared_values_alignment": "价值观一致",
    "autonomy_togetherness_balance": "自主与共处平衡",
    "physical_intimacy": "身体亲密",
}

# Below this share of the total weight the composite is still returned but not
# placed in a tier: labelling a relationship "Thriving" off one dimension
# would read as a verdict the evidence cannot support.
MIN_TIER_COVERAGE = 0.5

# Attachment compatibility, stored as one triangle and looked up on the sorted
# pair so that symmetry is structural. The source kept both directions, where
# an edit to one could silently desynchronise it from its mirror.
_ACS_PAIRS: dict[tuple[str, str], float] = {
    ("secure", "secure"): 0.95,
    ("anxious", "secure"): 0.75,
    ("avoidant", "secure"): 0.70,
    ("fearful", "secure"): 0.60,
    ("avoidant", "avoidant"): 0.50,
    ("anxious", "anxious"): 0.45,
    ("anxious", "fearful"): 0.40,
    ("avoidant", "fearful"): 0.35,
    ("anxious", "avoidant"): 0.30,
    ("fearful", "fearful"): 0.30,
}

_LOVE_LANGUAGE_PAIRS: dict[tuple[str, str], float] = {
    ("acts_of_service", "acts_of_service"): 1.0,
    ("physical_touch", "physical_touch"): 1.0,
    ("quality_time", "quality_time"): 1.0,
    ("receiving_gifts", "receiving_gifts"): 1.0,
    ("words_of_affirmation", "words_of_affirmation"): 1.0,
    ("acts_of_service", "physical_touch"): 0.5,
    ("acts_of_service", "quality_time"): 0.6,
    ("acts_of_service", "receiving_gifts"): 0.6,
    ("acts_of_service", "words_of_affirmation"): 0.5,
    ("physical_touch", "quality_time"): 0.7,
    ("physical_touch", "receiving_gifts"): 0.4,
    ("physical_touch", "words_of_affirmation"): 0.6,
    ("quality_time", "receiving_gifts"): 0.5,
    ("quality_time", "words_of_affirmation"): 0.7,
    ("receiving_gifts", "words_of_affirmation"): 0.5,
}

# Ordered high to low; the first threshold a score reaches wins.
_RQI_TIERS: tuple[tuple[float, str, str], ...] = (
    (8.5, "Thriving", "极健康，这段关系本身就是成长和喜悦的来源"),
    (7.0, "Healthy", "基础牢固，有一两个维度值得针对性投入"),
    (5.5, "Developing", "连接真实存在，但短板明确，需要有意识地补"),
    (4.0, "Strained", "多个维度同时承压，可以考虑伴侣咨询"),
    (0.0, "At Risk", "明显的关系困境，强烈建议寻求专业支持"),
)

_ATTACHMENT_ALIASES: dict[str, str] = {
    "secure": "secure",
    "安全型": "secure",
    "安全": "secure",
    "anxious": "anxious",
    "anxious_preoccupied": "anxious",
    "preoccupied": "anxious",
    "焦虑型": "anxious",
    "焦虑": "anxious",
    "痴迷型": "anxious",
    "avoidant": "avoidant",
    "dismissive_avoidant": "avoidant",
    "dismissive": "avoidant",
    "回避型": "avoidant",
    "回避": "avoidant",
    "疏离型": "avoidant",
    "fearful": "fearful",
    "fearful_avoidant": "fearful",
    "disorganized": "fearful",
    "恐惧型": "fearful",
    "恐惧回避型": "fearful",
    "恐惧_回避型": "fearful",
    "混乱型": "fearful",
}

_LOVE_LANGUAGE_ALIASES: dict[str, str] = {
    "words_of_affirmation": "words_of_affirmation",
    "words": "words_of_affirmation",
    "affirmation": "words_of_affirmation",
    "肯定话语": "words_of_affirmation",
    "语言肯定": "words_of_affirmation",
    "言语肯定": "words_of_affirmation",
    "quality_time": "quality_time",
    "time": "quality_time",
    "精心时刻": "quality_time",
    "高质量陪伴": "quality_time",
    "陪伴": "quality_time",
    "acts_of_service": "acts_of_service",
    "service": "acts_of_service",
    "acts": "acts_of_service",
    "服务行为": "acts_of_service",
    "服务的行动": "acts_of_service",
    "physical_touch": "physical_touch",
    "touch": "physical_touch",
    "身体接触": "physical_touch",
    "肢体接触": "physical_touch",
    "receiving_gifts": "receiving_gifts",
    "gifts": "receiving_gifts",
    "接受礼物": "receiving_gifts",
    "礼物": "receiving_gifts",
}


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

def _canonical_key(raw: str) -> str:
    """Fold case and collapse separators so alias keys need only one spelling.

    Callers write "Anxious-Preoccupied", "anxious preoccupied" or
    "anxious_preoccupied" interchangeably, so all three must land on the same
    lookup key. Alias tables therefore use underscores throughout.
    """
    folded = raw.strip().casefold()
    for separator in (" ", "-", "\u2011", "\u2013"):
        folded = folded.replace(separator, "_")
    while "__" in folded:
        folded = folded.replace("__", "_")
    return folded.strip("_")


def normalize_attachment_style(raw: str | None) -> str | None:
    """Map an attachment style to its canonical key, or ``None`` if unknown.

    # Examples

    >>> normalize_attachment_style("Anxious-Preoccupied")
    'anxious'
    >>> normalize_attachment_style("回避型")
    'avoidant'
    >>> normalize_attachment_style("不知道") is None
    True
    """
    if not raw:
        return None
    return _ATTACHMENT_ALIASES.get(_canonical_key(raw))


def normalize_love_language(raw: str | None) -> str | None:
    """Map a love language to its canonical key, or ``None`` if unknown."""
    if not raw:
        return None
    return _LOVE_LANGUAGE_ALIASES.get(_canonical_key(raw))


# ---------------------------------------------------------------------------
# Pairwise indices
# ---------------------------------------------------------------------------

def compute_acs(one: str | None, other: str | None) -> float | None:
    """Attachment Compatibility Score for a pair of styles, 0.0-1.0.

    Returns ``None`` when either style is unrecognised, so that an unknown
    style cannot quietly borrow the favourable numbers of a secure pairing.
    """
    left = normalize_attachment_style(one)
    right = normalize_attachment_style(other)
    if left is None or right is None:
        return None
    key = tuple(sorted((left, right)))
    return _ACS_PAIRS[key]  # type: ignore[index]


def acs_modifier(acs: float | None) -> float:
    """Multiplier the ACS applies to the weighted dimension sum.

    Runs from 0.85 at no compatibility to 1.05 at full, so attachment dynamics
    move the achievable ceiling by roughly a tenth either way. An unknown ACS
    applies no adjustment, rather than the source's mid-scale value which
    imposed a small penalty on merely not knowing.
    """
    if acs is None:
        return 1.0
    return round(0.85 + acs * 0.20, 4)


def compute_llmi(one: str | None, other: str | None) -> float | None:
    """Love Language Mismatch Index, 0.0 (aligned) to 1.0 (opposed).

    A high LLMI is a systematic difference in how affection is expressed and
    received, not a shortage of it. Returns ``None`` if either language is
    unrecognised.
    """
    left = normalize_love_language(one)
    right = normalize_love_language(other)
    if left is None or right is None:
        return None
    key = tuple(sorted((left, right)))
    return round(1.0 - _LOVE_LANGUAGE_PAIRS[key], 3)  # type: ignore[index]


def love_language_alignment_score(llmi: float) -> float:
    """Convert an LLMI into the 0-10 score for its matching RQI dimension."""
    return round((1.0 - llmi) * 10, 1)


def describe_llmi(llmi: float) -> str:
    """One-line reading of an LLMI value."""
    if llmi < 0.3:
        return "错配很小，表达和接收的方式天然对得上"
    if llmi < 0.6:
        return "中度错配，需要刻意翻译对方的表达方式"
    return "错配明显，爱意在传递过程中大量损耗"


# ---------------------------------------------------------------------------
# Composite index
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DimensionScore:
    """One judged dimension and its share of the composite."""

    key: str
    label: str
    score: float
    weight: float
    contribution: float


@dataclass(frozen=True)
class RqiResult:
    """A Relationship Quality Index computation and its inputs."""

    score: float
    tier: str
    tier_summary: str
    weighted_sum: float
    coverage: float
    acs: float | None
    acs_modifier: float
    llmi: float | None
    dimensions: tuple[DimensionScore, ...]
    missing: tuple[str, ...]
    strongest: str
    weakest: str
    highest_leverage: str

    @property
    def tiered(self) -> bool:
        """Whether coverage was sufficient to assign a health tier."""
        return bool(self.tier)


def classify_rqi(score: float) -> tuple[str, str]:
    """Return the ``(tier, summary)`` an RQI score falls into."""
    for threshold, tier, summary in _RQI_TIERS:
        if score >= threshold:
            return tier, summary
    return _RQI_TIERS[-1][1], _RQI_TIERS[-1][2]


def compute_rqi(
    scores: Mapping[str, float],
    *,
    acs: float | None = None,
    llmi: float | None = None,
) -> RqiResult:
    """Combine judged dimension scores into an RQI.

    ``RQI = (sum of weight x score over judged dimensions / coverage) x acs_modifier``

    Weights are renormalised over the dimensions actually supplied, and
    ``coverage`` reports how much of the nominal weight that was. The reported
    per-dimension ``weight`` is the renormalised one, so contributions always
    sum to ``weighted_sum``; when every dimension is judged it equals the
    nominal weight.

    When ``llmi`` is given and ``love_language_alignment`` was not scored
    directly, that dimension is derived from the index rather than guessed.

    # Errors

    Raises :class:`ValueError` for unknown dimension keys, scores outside
    0-10, non-numeric scores, or an empty set of dimensions.
    """
    judged: dict[str, float] = {}
    for key, raw in scores.items():
        if key not in RQI_WEIGHTS:
            raise ValueError(
                f"未知的关系维度：{key}。可用维度："
                + "、".join(RQI_WEIGHTS)
            )
        # bool is an int subclass, so True would otherwise pass as a score of
        # 1.0 — a wrong answer rather than a rejected one.
        if isinstance(raw, bool):
            raise ValueError(f"维度 {key} 的分数不能是布尔值：{raw!r}")
        try:
            value = float(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"维度 {key} 的分数不是数字：{raw!r}") from exc
        # Rejects NaN as well, since every comparison against it is false.
        if not 0.0 <= value <= 10.0:
            raise ValueError(f"维度 {key} 的分数需在 0-10 之间，收到 {value}")
        judged[key] = value

    if llmi is not None and "love_language_alignment" not in judged:
        judged["love_language_alignment"] = love_language_alignment_score(llmi)

    if not judged:
        raise ValueError("至少需要一个维度的评分。")

    coverage = sum(RQI_WEIGHTS[key] for key in judged)
    weighted_sum = sum(
        RQI_WEIGHTS[key] * value for key, value in judged.items()
    ) / coverage
    modifier = acs_modifier(acs)
    score = round(min(10.0, weighted_sum * modifier), 2)

    dimensions = tuple(
        DimensionScore(
            key=key,
            label=DIMENSION_LABELS[key],
            score=round(judged[key], 1),
            weight=round(RQI_WEIGHTS[key] / coverage, 4),
            contribution=round(RQI_WEIGHTS[key] / coverage * judged[key], 3),
        )
        for key in RQI_WEIGHTS
        if key in judged
    )
    missing = tuple(key for key in RQI_WEIGHTS if key not in judged)

    # Ties break toward the heavier dimension, which is the one worth naming.
    strongest = max(judged, key=lambda k: (judged[k], RQI_WEIGHTS[k]))
    weakest = min(judged, key=lambda k: (judged[k], -RQI_WEIGHTS[k]))
    highest_leverage = max(
        judged,
        key=lambda k: (RQI_WEIGHTS[k] * (10.0 - judged[k]), RQI_WEIGHTS[k]),
    )

    if coverage + 1e-9 >= MIN_TIER_COVERAGE:
        tier, tier_summary = classify_rqi(score)
    else:
        tier, tier_summary = "", (
            f"只覆盖了 {coverage:.0%} 的权重，不足以归入健康分档；"
            "补齐缺失维度后再看分档。"
        )

    return RqiResult(
        score=score,
        tier=tier,
        tier_summary=tier_summary,
        weighted_sum=round(weighted_sum, 3),
        coverage=round(coverage, 4),
        acs=acs,
        acs_modifier=modifier,
        llmi=llmi,
        dimensions=dimensions,
        missing=missing,
        strongest=strongest,
        weakest=weakest,
        highest_leverage=highest_leverage,
    )


# ---------------------------------------------------------------------------
# Momentum
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Momentum:
    """Direction of travel across a series of RQI snapshots."""

    trajectory: str
    delta: float
    per_step: float
    samples: int
    description: str


def compute_momentum(history: Sequence[float]) -> Momentum:
    """Compare the newest RQI against the oldest in ``history`` (oldest first).

    This is an endpoint difference, not a fitted trend: the +/-0.5 thresholds
    it feeds are calibrated on that quantity in the source model, and swapping
    in a regression slope would silently recalibrate them. The consequence is
    that a dip followed by a full recovery reads as stable, which is why
    ``samples`` and ``per_step`` travel with the verdict.
    """
    values = [float(value) for value in history]
    if len(values) < 2:
        return Momentum(
            trajectory="insufficient_data",
            delta=0.0,
            per_step=0.0,
            samples=len(values),
            description="至少需要两次评估才能看出趋势。",
        )

    delta = round(values[-1] - values[0], 2)
    steps = len(values) - 1
    per_step = round(delta / steps, 3)

    if delta > 0.5:
        trajectory = "improving"
        description = f"RQI 上升 {delta:.2f}，投入正在见效。"
    elif delta < -0.5:
        trajectory = "declining"
        description = f"RQI 下降 {abs(delta):.2f}，需要有意识地修复。"
    else:
        trajectory = "stable"
        description = f"RQI 基本持平（{delta:+.2f}），当前的强项是稳定。"

    return Momentum(
        trajectory=trajectory,
        delta=delta,
        per_step=per_step,
        samples=len(values),
        description=description,
    )
