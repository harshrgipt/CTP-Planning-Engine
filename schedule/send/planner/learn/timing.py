"""Phase 1e: cycle-time and setup-time distributions.

- Curing cycle time: `cycle_end - cycle_start` per (recipe, press). Trim 1-99%.
- Setup time (building): inter-lot gap on same machine when itemCode changes.
- Cross-validate PCR construction spec `cycle_time_sec` against MES-observed.
"""
from __future__ import annotations

from pathlib import Path

import polars as pl

from planner.data.warehouse import duck
from planner.kb.rule_types import Rule, RuleType
from planner.runs.logger import log


def compute_timing(out_dir: Path) -> tuple[list[Path], list[Rule]]:
    con = duck()
    out_dir.mkdir(parents=True, exist_ok=True)
    outs: list[Path] = []
    rules: list[Rule] = []

    # 1) Curing cycle time per (recipe, press).
    #    event_ts (dtandTime) = press close / cycle start
    #    cycleStart column = press open / cycle end (misnamed in source)
    #    Duration = cycleStart - event_ts. Median observed ~1955 s (32 min).
    ct_path = out_dir / "curing_cycle_time.parquet"
    con.execute(f"""
        COPY (
            WITH d AS (
                SELECT plant, recipeID AS recipe, wcID AS press,
                       date_diff('second', event_ts, TRY_CAST(cycleStart AS TIMESTAMP)) AS dur_s
                FROM v_curing
                WHERE statuscritical = 'Normal' AND TRY_CAST(cycleStart AS TIMESTAMP) IS NOT NULL
            ),
            trim AS (
                SELECT plant, recipe, press, dur_s FROM d
                WHERE dur_s BETWEEN 300 AND 5400
            )
            SELECT plant, recipe, press,
                   count(*) AS n,
                   avg(dur_s)::DOUBLE AS mean_s,
                   stddev_samp(dur_s)::DOUBLE AS std_s,
                   quantile_cont(dur_s, 0.5)::DOUBLE AS p50_s,
                   quantile_cont(dur_s, 0.95)::DOUBLE AS p95_s
            FROM trim GROUP BY 1,2,3
        ) TO '{ct_path.as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD, OVERWRITE_OR_IGNORE);
    """)
    outs.append(ct_path)

    # 2) Setup time (building): gap between consecutive events on same machine when itemCode changed.
    st_path = out_dir / "building_setup_time.parquet"
    con.execute(f"""
        COPY (
            WITH ordered AS (
                SELECT plant, stage, machineCode AS machine, itemCode, event_ts,
                       lag(itemCode) OVER (PARTITION BY machineCode ORDER BY event_ts) AS prev_item,
                       lag(event_ts)  OVER (PARTITION BY machineCode ORDER BY event_ts) AS prev_ts
                FROM v_build
            ),
            gaps AS (
                SELECT plant, stage, machine, prev_item AS from_sku, itemCode AS to_sku,
                       date_diff('second', prev_ts, event_ts) AS gap_s
                FROM ordered
                WHERE prev_item IS NOT NULL AND prev_item <> itemCode
            )
            SELECT plant, stage, machine, from_sku, to_sku,
                   count(*) AS n,
                   avg(gap_s)::DOUBLE AS mean_s,
                   stddev_samp(gap_s)::DOUBLE AS std_s,
                   quantile_cont(gap_s, 0.5)::DOUBLE AS p50_s,
                   quantile_cont(gap_s, 0.95)::DOUBLE AS p95_s
            FROM gaps
            WHERE gap_s BETWEEN 30 AND 14400  -- 30s to 4h; longer = shift-break, not setup
            GROUP BY 1,2,3,4,5
        ) TO '{st_path.as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD, OVERWRITE_OR_IGNORE);
    """)
    outs.append(st_path)

    # 3) PCR construction-spec vs MES-observed cycle time reconciliation.
    if Path("warehouse/construction/construction_pcr.parquet").exists():
        recon_path = out_dir / "pcr_cycle_reconciliation.parquet"
        con.execute(f"""
            COPY (
                WITH spec AS (
                    SELECT sku, gt_code, cycle_time_sec AS spec_s
                    FROM v_construction_pcr
                ),
                gaps AS (
                    SELECT itemCode AS gt_code,
                           date_diff('second',
                               lag(event_ts) OVER (PARTITION BY machineCode ORDER BY event_ts),
                               event_ts) AS gap_s
                    FROM v_build
                    WHERE plant = 'PCR' AND stage = 2 AND QualityStatus = '1'
                ),
                obs AS (
                    SELECT gt_code, avg(gap_s)::DOUBLE AS obs_s, count(*) AS n
                    FROM gaps
                    WHERE gap_s BETWEEN 5 AND 1800
                    GROUP BY 1
                )
                SELECT s.sku, s.gt_code, s.spec_s, o.obs_s, o.n,
                       (o.obs_s / NULLIF(s.spec_s,0))::DOUBLE AS ratio
                FROM spec s LEFT JOIN obs o USING (gt_code)
            ) TO '{recon_path.as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD, OVERWRITE_OR_IGNORE);
        """)
        outs.append(recon_path)

    # Emit stat rules per curing (plant, recipe, press)
    ct = pl.read_parquet(ct_path)
    for row in ct.iter_rows(named=True):
        rid = f"cycle.{row['plant']}.recipe{row['recipe']}.press{row['press']}"
        rules.append(Rule(
            rule_id=rid, scope="cycle_time",
            statement={"predicate": "cure_cycle_dist", "params": {
                "plant": row["plant"], "recipe": row["recipe"], "press": row["press"],
                "mean_s": row["mean_s"], "std_s": row["std_s"],
                "p50_s": row["p50_s"], "p95_s": row["p95_s"]}},
            support=int(row["n"]), sample_size=int(row["n"]),
            confidence=1.0, ci_low=1.0, ci_high=1.0, p_value=0.0,
            type=RuleType.STAT,
            provenance={"miner": "timing"},
        ))
    log.info("timing.rules", n=len(rules))
    return outs, rules
