"""Phase 1f: flag historical anomalies. Feeds exception counts back to promoter."""
from __future__ import annotations

from pathlib import Path

from planner.data.warehouse import duck
from planner.runs.logger import log


def scan_violations(out_dir: Path) -> dict[str, int]:
    con = duck()
    out_dir.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}

    # 1. Mould on two presses simultaneously (physically impossible — a hard-rule
    #    violation if it appears). Full self-join on (plant, mould), no time-partition
    #    shortcut so cross-midnight overlaps are still detected.
    mould_conflict = out_dir / "mould_double_booked.parquet"
    con.execute(f"""
        COPY (
            WITH cur AS (
                SELECT plant, wcID AS press, MouldCodeLH AS mould,
                       TRY_CAST(cycleStart AS TIMESTAMP) AS start_ts, event_ts AS end_ts
                FROM v_curing WHERE MouldCodeLH IS NOT NULL AND statuscritical = 'Normal'
                UNION ALL
                SELECT plant, wcID AS press, MouldCodeRH AS mould,
                       TRY_CAST(cycleStart AS TIMESTAMP) AS start_ts, event_ts AS end_ts
                FROM v_curing WHERE MouldCodeRH IS NOT NULL AND statuscritical = 'Normal'
            )
            SELECT a.plant, a.mould, a.press AS press_a, b.press AS press_b, a.start_ts, a.end_ts
            FROM cur a JOIN cur b USING (plant, mould)
            WHERE a.press < b.press
              AND a.start_ts IS NOT NULL AND b.start_ts IS NOT NULL
              AND a.start_ts < b.end_ts
              AND b.start_ts < a.end_ts
        ) TO '{mould_conflict.as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD, OVERWRITE_OR_IGNORE);
    """)
    counts["mould_double_booked"] = con.execute(
        f"SELECT count(*) FROM read_parquet('{mould_conflict.as_posix()}')"
    ).fetchone()[0]

    # 2. Curing status_critical != Normal
    bad_cure = out_dir / "curing_critical.parquet"
    con.execute(f"""
        COPY (
            SELECT plant, wcID AS press, recipeID, event_ts, statuscritical
            FROM v_curing WHERE statuscritical != 'Normal'
        ) TO '{bad_cure.as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD, OVERWRITE_OR_IGNORE);
    """)
    counts["curing_critical"] = con.execute(
        f"SELECT count(*) FROM read_parquet('{bad_cure.as_posix()}')"
    ).fetchone()[0]

    # 3. Building QualityStatus != '1'
    bad_build = out_dir / "building_bad_quality.parquet"
    con.execute(f"""
        COPY (
            SELECT plant, stage, machineCode, itemCode, event_ts, QualityStatus
            FROM v_build WHERE QualityStatus != '1'
        ) TO '{bad_build.as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD, OVERWRITE_OR_IGNORE);
    """)
    counts["building_bad_quality"] = con.execute(
        f"SELECT count(*) FROM read_parquet('{bad_build.as_posix()}')"
    ).fetchone()[0]

    # 4. TBR balance rank in {C,E}: quality defect signal
    try:
        bad_balance = out_dir / "balance_defects.parquet"
        con.execute(f"""
            COPY (
                SELECT * FROM v_balance
                WHERE total_rank IN ('C', 'E')
            ) TO '{bad_balance.as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD, OVERWRITE_OR_IGNORE);
        """)
        counts["balance_defects"] = con.execute(
            f"SELECT count(*) FROM read_parquet('{bad_balance.as_posix()}')"
        ).fetchone()[0]
    except Exception as e:
        log.warning("violation.balance_skip", err=str(e))

    log.info("violation.scan.done", counts=counts)
    return counts
