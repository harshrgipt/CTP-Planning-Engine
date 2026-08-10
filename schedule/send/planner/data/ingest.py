"""CSV ingest: raw MES CSVs -> Hive-partitioned Parquet via DuckDB streaming.

Each source CSV is registered as a DuckDB view, then written to
`warehouse/<table>/plant=<P>/stage=<S>/date=<YYYY-MM-DD>/*.parquet` in a single
`COPY ... TO ... (PARTITION_BY, OVERWRITE_OR_IGNORE)` — no full frame is
materialized in Python. Idempotent: rerunning overwrites the target partitions.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import duckdb

from planner.config import CONFIG
from planner.runs.logger import log


@dataclass
class CsvSpec:
    src: Path
    table: str            # 'curing' | 'building_output' | 'consumption'
    plant: str            # 'PCR' | 'TBR'
    stage: int | None     # 1 | 2 | None for curing
    ts_col: str           # 'dtandTime'


def _mes_specs() -> list[CsvSpec]:
    p = CONFIG.paths
    return [
        CsvSpec(p.raw_curing / "curingpcr.csv",             "curing",          "PCR", None, "dtandTime"),
        CsvSpec(p.raw_curing / "curingtbr.csv",             "curing",          "TBR", None, "dtandTime"),
        CsvSpec(p.raw_o_production / "tbmpcrstage1.csv",    "building_output", "PCR", 1,    "dtandTime"),
        CsvSpec(p.raw_o_production / "tbmpcrstage2.csv",    "building_output", "PCR", 2,    "dtandTime"),
        CsvSpec(p.raw_o_production / "tbmtbrstage1.csv",    "building_output", "TBR", 1,    "dtandTime"),
        CsvSpec(p.raw_o_production / "tbmtbrstage2.csv",    "building_output", "TBR", 2,    "dtandTime"),
        CsvSpec(p.raw_consumption  / "tbmpcrstage1.csv",    "consumption",     "PCR", 1,    "dtandTime"),
        CsvSpec(p.raw_consumption  / "tbmpcrstage2.csv",    "consumption",     "PCR", 2,    "dtandTime"),
        CsvSpec(p.raw_consumption  / "tbmtbrstage1.csv",    "consumption",     "TBR", 1,    "dtandTime"),
        CsvSpec(p.raw_consumption  / "tbmtbrstage2.csv",    "consumption",     "TBR", 2,    "dtandTime"),
    ]


def _target_dir(spec: CsvSpec) -> Path:
    root = CONFIG.paths.warehouse / spec.table / f"plant={spec.plant}"
    if spec.stage is not None:
        root = root / f"stage={spec.stage}"
    return root


def ingest_one(spec: CsvSpec, con: duckdb.DuckDBPyConnection, *, limit_rows: int | None = None) -> int:
    if not spec.src.exists():
        log.warning("ingest.skip_missing", src=str(spec.src))
        return 0

    target = _target_dir(spec)
    target.mkdir(parents=True, exist_ok=True)

    # Build a SELECT that adds partition columns and forces types on the timestamp.
    # DuckDB COPY with PARTITION_BY writes date=<value>/*.parquet under target dir.
    limit_clause = f"LIMIT {limit_rows}" if limit_rows else ""
    stage_expr = f"{spec.stage} AS stage," if spec.stage is not None else ""
    stage_partition = ", stage" if spec.stage is not None else ""

    # Force VARCHAR on identifier columns to avoid schema drift across daily partitions
    # (e.g. productionID sometimes parses as BIGINT, sometimes VARCHAR).
    varchar_forces = {
        "curing":          ["gtbarCode", "pressbarCode", "serialNo", "mouldNo"],
        "building_output": ["productionID", "itemCode", "machineCode", "QualityStatus", "ConsumeStatus"],
        "consumption":     ["productionID", "consumptionItemCode", "machineCode", "syncStatus"],
    }.get(spec.table, [])
    force_types = ", ".join(f"'{c}': 'VARCHAR'" for c in varchar_forces)
    force_clause = f", types={{{force_types}}}" if force_types else ""

    sql = f"""
    COPY (
        SELECT
            *,
            '{spec.plant}' AS plant,
            {stage_expr}
            TRY_CAST("{spec.ts_col}" AS TIMESTAMP) AS event_ts,
            CAST(TRY_CAST("{spec.ts_col}" AS TIMESTAMP) AS DATE) AS date
        FROM read_csv_auto('{spec.src.as_posix()}', HEADER=true, IGNORE_ERRORS=true{force_clause})
        {limit_clause}
    ) TO '{target.as_posix()}'
      (FORMAT PARQUET, PARTITION_BY (date{stage_partition}, plant), OVERWRITE_OR_IGNORE, COMPRESSION 'ZSTD');
    """
    log.info("ingest.start", src=str(spec.src), target=str(target))
    con.execute(sql)
    # Count what we wrote for a sanity number
    n = con.execute(
        f"SELECT count(*) FROM read_parquet('{target.as_posix()}/**/*.parquet', hive_partitioning=1)"
    ).fetchone()[0]
    log.info("ingest.done", table=spec.table, plant=spec.plant, stage=spec.stage, rows=n)
    return n


def ingest_all(*, limit_rows: int | None = None) -> dict[str, int]:
    con = duckdb.connect()
    try:
        con.execute("SET memory_limit='6GB'")
        con.execute("PRAGMA threads=4")
    except Exception:
        pass
    counts: dict[str, int] = {}
    for spec in _mes_specs():
        key = f"{spec.table}/{spec.plant}" + (f"/{spec.stage}" if spec.stage is not None else "")
        counts[key] = ingest_one(spec, con, limit_rows=limit_rows)
    return counts
