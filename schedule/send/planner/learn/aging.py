"""Learn aging rules — observed cure-lag distribution per (plant, gt_code)."""
from __future__ import annotations

from pathlib import Path

import polars as pl

from planner.data.warehouse import duck
from planner.kb.rule_types import Rule, RuleType
from planner.runs.logger import log


def compute_aging(out_dir: Path) -> tuple[Path, list[Rule]]:
    con = duck()
    df = con.execute("""
        WITH pairs AS (
            SELECT b.plant, b.itemCode AS gt_code,
                   date_diff('second', b.event_ts, c.event_ts) AS wait_s
            FROM v_build b JOIN v_curing c ON b.productionID = c.gtbarCode
            WHERE b.stage = 2
              AND c.statuscritical = 'Normal'
              AND c.event_ts >= b.event_ts
        )
        SELECT plant, gt_code,
               count(*) AS n,
               quantile_cont(wait_s, 0.05) AS min_wait_s,
               quantile_cont(wait_s, 0.5)  AS p50_wait_s,
               quantile_cont(wait_s, 0.95) AS p95_wait_s
        FROM pairs
        GROUP BY 1,2
        HAVING count(*) >= 20
    """).pl()

    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "aging.parquet"
    df.write_parquet(out, compression="zstd")

    rules: list[Rule] = []
    for row in df.iter_rows(named=True):
        rid = f"aging.{row['plant']}.{row['gt_code']}"
        rules.append(Rule(
            rule_id=rid, scope="aging",
            statement={"predicate": "cure_lag_dist", "params": {
                "plant": row["plant"], "gt_code": row["gt_code"],
                "min_wait_s": float(row["min_wait_s"]),
                "p50_wait_s": float(row["p50_wait_s"]),
                "p95_wait_s": float(row["p95_wait_s"]),
            }},
            support=int(row["n"]),
            sample_size=int(row["n"]),
            confidence=1.0, ci_low=1.0, ci_high=1.0, p_value=0.0,
            type=RuleType.STAT,
            provenance={"miner": "aging"},
        ))
    log.info("aging.rules", n=len(rules))
    return out, rules
