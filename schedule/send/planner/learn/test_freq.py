"""Extract test-SKU cadence rules from TBR Sheet1 (28 SKUs)."""
from __future__ import annotations

from planner.data.warehouse import duck
from planner.kb.rule_types import Rule, RuleType
from planner.runs.logger import log


_FREQ_TO_DAYS = {
    "every month": 30,
    "every week": 7,
    "every quarter": 90,
    "every fortnight": 14,
    "every 2 months": 60,
}


def compute_test_frequency() -> list[Rule]:
    con = duck()
    try:
        df = con.execute("SELECT sku, category, test_type, frequency FROM v_test_skus").pl()
    except Exception as e:  # noqa: BLE001
        log.warning("test_freq.no_view", err=str(e))
        return []

    rules: list[Rule] = []
    for row in df.iter_rows(named=True):
        freq = str(row["frequency"] or "").strip().lower()
        days = _FREQ_TO_DAYS.get(freq, 30)
        rid = f"test_freq.{row['sku']}"
        rules.append(Rule(
            rule_id=rid, scope="test_sku",
            statement={"predicate": "test_cadence", "params": {
                "sku": row["sku"], "category": row["category"],
                "test_type": row["test_type"], "frequency_days": days,
                "source_frequency": row["frequency"],
            }},
            support=1, sample_size=1,
            confidence=1.0, ci_low=1.0, ci_high=1.0, p_value=0.0,
            type=RuleType.SOFT, weight=1.0,
            provenance={"miner": "test_freq"},
        ))
    log.info("test_freq.rules", n=len(rules))
    return rules
