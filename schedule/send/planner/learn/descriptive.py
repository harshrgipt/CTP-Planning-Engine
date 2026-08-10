"""Phase 1a: descriptive baseline stats via DuckDB group-bys, streamed to Parquet."""
from __future__ import annotations

from pathlib import Path

from planner.data.warehouse import duck
from planner.runs.logger import log


def _write(con, sql: str, out: Path) -> int:
    out.parent.mkdir(parents=True, exist_ok=True)
    con.execute(f"COPY ({sql}) TO '{out.as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD, OVERWRITE_OR_IGNORE);")
    n = con.execute(f"SELECT count(*) FROM read_parquet('{out.as_posix()}')").fetchone()[0]
    return n


def compute_baselines(out_dir: Path) -> dict[str, int]:
    con = duck()
    counts: dict[str, int] = {}

    # 1. Per-SKU daily throughput (stage 2 = finished green tyre)
    counts["sku_daily_throughput"] = _write(
        con,
        """
        SELECT itemCode AS sku, plant, date, count(*) AS lots, sum(quantity) AS qty
        FROM v_build
        WHERE stage = 2 AND QualityStatus = '1'
        GROUP BY 1,2,3
        """,
        out_dir / "sku_daily_throughput.parquet",
    )

    # 2. Per-machine daily utilization proxy: rows per day
    counts["machine_daily_load"] = _write(
        con,
        """
        SELECT machineCode AS machine, plant, stage, date, count(*) AS events,
               count(DISTINCT itemCode) AS distinct_skus
        FROM v_build
        GROUP BY 1,2,3,4
        """,
        out_dir / "machine_daily_load.parquet",
    )

    # 3. Changeover count per (machine, day) = distinct-item transitions
    counts["machine_daily_changeovers"] = _write(
        con,
        """
        WITH ordered AS (
            SELECT machineCode AS machine, plant, stage, date, event_ts, itemCode,
                   lag(itemCode) OVER (PARTITION BY machineCode, date ORDER BY event_ts) AS prev_item
            FROM v_build
        )
        SELECT machine, plant, stage, date,
               sum(CASE WHEN prev_item IS NOT NULL AND prev_item <> itemCode THEN 1 ELSE 0 END) AS changeovers
        FROM ordered
        GROUP BY 1,2,3,4
        """,
        out_dir / "machine_daily_changeovers.parquet",
    )

    # 4. Curing daily throughput per press
    counts["curing_daily"] = _write(
        con,
        """
        SELECT wcID AS press, plant, date, count(*) AS cycles,
               count(DISTINCT recipeID) AS distinct_recipes
        FROM v_curing
        WHERE statuscritical = 'Normal'
        GROUP BY 1,2,3
        """,
        out_dir / "curing_daily.parquet",
    )

    # 5. Curing wait per green tyre (build.itemCode is the GT code; join on gtbarCode)
    counts["curing_wait"] = _write(
        con,
        """
        WITH b AS (
            SELECT productionID AS gtbar, itemCode AS gt_code, event_ts AS built_ts, plant
            FROM v_build WHERE stage = 2
        )
        SELECT b.plant, b.gt_code,
               count(*) AS n,
               avg(date_diff('minute', b.built_ts, c.event_ts)) AS mean_wait_min,
               quantile_cont(date_diff('minute', b.built_ts, c.event_ts), 0.5) AS p50_wait_min,
               quantile_cont(date_diff('minute', b.built_ts, c.event_ts), 0.95) AS p95_wait_min
        FROM b JOIN v_curing c ON b.gtbar = c.gtbarCode
        WHERE c.event_ts >= b.built_ts
        GROUP BY 1,2
        """,
        out_dir / "curing_wait.parquet",
    )

    log.info("learn.descriptive.done", counts=counts)
    return counts
