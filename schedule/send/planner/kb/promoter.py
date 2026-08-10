"""Promote raw rule candidates to hard/soft/stat based on CI + support + p-value."""
from __future__ import annotations

import math

from scipy.stats import chi2_contingency  # type: ignore

from planner.config import CONFIG
from planner.kb.rule_types import Rule, RuleType


def wilson_ci(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n <= 0:
        return 0.0, 1.0
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def chi2_p(observed: list[list[int]]) -> float:
    try:
        _, p, _, _ = chi2_contingency(observed)
        return float(p)
    except Exception:
        return 1.0


def classify(r: Rule) -> RuleType:
    th = CONFIG.thresholds
    if (r.confidence >= th.hard_confidence
        and r.support >= th.hard_support_min
        and r.exception_rate <= th.hard_exception_max
        and r.p_value <= th.hard_p_value_max):
        return RuleType.HARD
    if (r.confidence >= th.soft_confidence
        and r.support >= th.soft_support_min
        and r.ci_low >= th.soft_ci_low):
        return RuleType.SOFT
    return RuleType.STAT


def promote(rules: list[Rule]) -> list[Rule]:
    for r in rules:
        r.type = classify(r)
        if r.type == RuleType.SOFT:
            # weight scales with confidence — a rule at 0.8 -> weight 0.5, at 1.0 -> 1.0
            r.weight = max(0.1, (r.confidence - 0.5) * 2)
        elif r.type == RuleType.HARD:
            r.weight = 1.0
        else:
            r.weight = 0.0
    return rules
