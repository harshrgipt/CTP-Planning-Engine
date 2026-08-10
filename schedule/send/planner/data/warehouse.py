"""DuckDB singleton + view registration over the Parquet warehouse.

Supports a global as-of `cutoff`: when set, every event-stream view
(v_curing / v_build / v_consume) is re-registered with `WHERE date < cutoff`,
so no consumer -- miner, planner, or ledger -- can see data at or after the
cutoff. This is what makes walk-forward evaluation leak-free without having to
thread a date parameter through all ten miners. Static reference views (BOM,
construction, balance) carry no event date and are never filtered.
"""
from __future__ import annotations

from datetime import date as _date

import duckdb

from planner.config import CONFIG

_CON: duckdb.DuckDBPyConnection | None = None
_CUTOFF: _date | None = None


def set_cutoff(cutoff: _date | None) -> None:
    """Set (or clear, with None) the global as-of cutoff and re-register views."""
    global _CUTOFF
    _CUTOFF = cutoff
    if _CON is not None:
        _register_views(_CON)


def get_cutoff() -> _date | None:
    return _CUTOFF


def duck() -> duckdb.DuckDBPyConnection:
    global _CON
    if _CON is not None:
        return _CON
    con = duckdb.connect()
    try:
        con.execute("SET memory_limit='6GB'")
        con.execute("PRAGMA threads=4")
    except Exception:
        pass
    _register_views(con)
    _CON = con
    return con


def _reg(con: duckdb.DuckDBPyConnection, view: str, glob: str, *, dated: bool = False) -> None:
    path = CONFIG.paths.warehouse / glob
    # If nothing has been ingested yet, register an empty view so callers don't crash.
    parquets = list((CONFIG.paths.warehouse).glob(glob))
    if not parquets:
        return
    # Filter on `date` (the Hive partition key) so DuckDB prunes whole partitions
    # rather than scanning and discarding rows.
    where = ""
    if dated and _CUTOFF is not None:
        where = f" WHERE date < DATE '{_CUTOFF.isoformat()}'"
    con.execute(
        f"CREATE OR REPLACE VIEW {view} AS "
        f"SELECT * FROM read_parquet('{path.as_posix()}', hive_partitioning=1){where}"
    )


def _register_views(con: duckdb.DuckDBPyConnection) -> None:
    _reg(con, "v_curing",       "curing/**/*.parquet",          dated=True)
    _reg(con, "v_build",        "building_output/**/*.parquet",  dated=True)
    _reg(con, "v_consume",      "consumption/**/*.parquet",      dated=True)
    _reg(con, "v_bom",          "bom/bom_edges.parquet")
    _reg(con, "v_bom_gt",       "bom/bom_gt_map.parquet")
    _reg(con, "v_construction_pcr", "construction/construction_pcr.parquet")
    _reg(con, "v_construction_tbr", "construction/construction_tbr.parquet")
    _reg(con, "v_gt_template",  "construction/construction_gt_template_tbr.parquet")
    _reg(con, "v_test_skus",    "construction/test_skus_tbr.parquet")
    _reg(con, "v_balance",      "balance/**/*.parquet")
    _reg(con, "v_changeover_build", "masters/changeover_building.parquet")
    _reg(con, "v_mould_sku",    "masters/mould_sku.parquet")
    _reg(con, "v_ctp_mould_change", "masters/ctp_mould_change.parquet")
    _reg(con, "v_press_xwalk",  "masters/press_xwalk.parquet")


def refresh_views() -> None:
    global _CON
    if _CON is not None:
        _register_views(_CON)
