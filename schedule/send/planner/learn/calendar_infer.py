"""Infer shift boundaries + non-working windows from MES timestamp density."""
from __future__ import annotations

from pathlib import Path

import polars as pl

from planner.data.warehouse import duck
from planner.runs.logger import log


def infer_calendar(out_dir: Path, idle_gap_min: int = 60) -> Path:
    con = duck()
    # Per (plant, machine, date) event-hour distribution → derive "active hours".
    df = con.execute(f"""
        WITH gaps AS (
            SELECT plant, machineCode AS machine, date, event_ts,
                   date_diff('minute',
                       lag(event_ts) OVER (PARTITION BY machineCode ORDER BY event_ts),
                       event_ts) AS gap_min
            FROM v_build
        ),
        idle AS (
            SELECT plant, machine, date, count(*) AS n_gaps
            FROM gaps
            WHERE gap_min >= {idle_gap_min}
            GROUP BY 1,2,3
        ),
        act AS (
            SELECT plant, machineCode AS machine, date,
                   min(event_ts) AS first_ts, max(event_ts) AS last_ts,
                   count(*) AS events
            FROM v_build
            GROUP BY 1,2,3
        )
        SELECT a.plant, a.machine, a.date,
               a.first_ts, a.last_ts, a.events,
               COALESCE(i.n_gaps, 0) AS idle_windows
        FROM act a LEFT JOIN idle i USING (plant, machine, date)
    """).pl()

    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "calendar_inferred.parquet"
    df.write_parquet(out, compression="zstd")
    log.info("calendar.inferred", rows=df.height, path=str(out))
    return out
