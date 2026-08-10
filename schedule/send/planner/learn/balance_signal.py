"""Learn quality signal from TBR uniformity balance data.

For each (plant, press, gt_code) compute rate of `total_rank in {C,E}`; if the
rate exceeds threshold, emit a **soft** rule that demotes that machine-SKU
pairing (halve its MPM weight).
"""
from __future__ import annotations

from planner.data.warehouse import duck
from planner.kb.rule_types import Rule, RuleType
from planner.runs.logger import log


def compute_balance_signal(defect_rate_threshold: float = 0.05) -> list[Rule]:
    con = duck()
    # Join balance to curing via barcode to attach gt_code via building's productionID.
    df = con.execute("""
        WITH matched AS (
            SELECT c.plant, c.wcID::VARCHAR AS press,
                   b_.itemCode AS gt_code,
                   bal.total_rank
            FROM v_balance bal
            JOIN v_curing c ON bal.barcode = c.gtbarCode
            JOIN v_build b_ ON c.gtbarCode = b_.productionID AND b_.stage = 2
        ),
        agg AS (
            SELECT plant, press, gt_code,
                   count(*) AS n,
                   sum(CASE WHEN total_rank IN ('C','E') THEN 1 ELSE 0 END) AS defects
            FROM matched
            GROUP BY 1,2,3
            HAVING count(*) >= 20
        )
        SELECT plant, press, gt_code, n, defects,
               (defects::DOUBLE / n) AS defect_rate
        FROM agg
    """).pl()

    rules: list[Rule] = []
    for row in df.iter_rows(named=True):
        if row["defect_rate"] < defect_rate_threshold:
            continue
        rid = f"quality.{row['plant']}.{row['press']}.{row['gt_code']}"
        rules.append(Rule(
            rule_id=rid, scope="sku_machine_quality",
            statement={"predicate": "quality_demote", "params": {
                "plant": row["plant"], "press": row["press"],
                "gt_code": row["gt_code"], "defect_rate": float(row["defect_rate"]),
                "action": "halve_mpm_weight"}},
            support=int(row["defects"]),
            sample_size=int(row["n"]),
            confidence=float(row["defect_rate"]),
            exception_rate=1.0 - float(row["defect_rate"]),
            ci_low=float(row["defect_rate"]),
            ci_high=float(row["defect_rate"]),
            p_value=0.0,
            type=RuleType.SOFT,
            weight=0.5,
            provenance={"miner": "balance_signal"},
        ))
    log.info("balance.rules", n=len(rules))
    return rules
