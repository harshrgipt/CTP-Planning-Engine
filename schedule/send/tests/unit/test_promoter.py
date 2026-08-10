from planner.kb.promoter import classify, wilson_ci
from planner.kb.rule_types import Rule, RuleType


def test_wilson_zero():
    lo, hi = wilson_ci(0, 0)
    assert lo == 0.0 and hi == 1.0


def test_wilson_typical():
    lo, hi = wilson_ci(80, 100)
    assert 0.6 < lo < 0.8 < hi < 0.9


def _rule(**kw):
    d = dict(rule_id="r", scope="s", statement={}, support=0, confidence=0.0)
    d.update(kw)
    return Rule(**d)


def test_classify_hard():
    r = _rule(confidence=0.999, support=1000, exception_rate=0.0,
             p_value=0.0, sample_size=1000, ci_low=0.99, ci_high=1.0)
    assert classify(r) == RuleType.HARD


def test_classify_soft():
    r = _rule(confidence=0.85, support=200, exception_rate=0.15,
             p_value=0.01, sample_size=200, ci_low=0.75, ci_high=0.9)
    assert classify(r) == RuleType.SOFT


def test_classify_stat_when_low_support():
    r = _rule(confidence=0.999, support=10, exception_rate=0.0,
             p_value=0.0, sample_size=10, ci_low=0.9, ci_high=1.0)
    assert classify(r) == RuleType.STAT
