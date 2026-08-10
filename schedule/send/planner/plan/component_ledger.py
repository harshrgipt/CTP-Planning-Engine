"""Component ledger — mirrors GreenTireLedger but for BOM component codes.

Bulk-populated from build-lot events. Opening balance derived from MES
consumption cumulative (positive incoming inferred as build_stage2 rate ×
child_qty − outgoing consumption).
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import duckdb
import polars as pl

from planner.data.warehouse import duck
from planner.runs.logger import log


class ComponentLedger:
    _DDL = """
    CREATE TABLE IF NOT EXISTS component_events (
        ts        TIMESTAMP,
        plant     VARCHAR,
        component VARCHAR,
        qty_delta DOUBLE,
        source    VARCHAR,    -- 'build' | 'opening' | 'external_receipt'
        lot_id    VARCHAR
    );
    """

    def __init__(self):
        self.con = duckdb.connect(":memory:")
        self.con.execute(self._DDL)

    def add_df(self, df: pl.DataFrame) -> int:
        if df.height == 0:
            return 0
        self.con.register("_ce", df.to_arrow())
        self.con.execute("INSERT INTO component_events SELECT * FROM _ce")
        self.con.unregister("_ce")
        return df.height

    def balances(self, at: datetime | None = None) -> pl.DataFrame:
        if at is None:
            return self.con.execute(
                "SELECT plant, component, sum(qty_delta) AS qty "
                "FROM component_events GROUP BY 1,2 ORDER BY 1,2"
            ).pl()
        return self.con.execute(
            "SELECT plant, component, sum(qty_delta) AS qty "
            "FROM component_events WHERE ts <= ? GROUP BY 1,2 ORDER BY 1,2",
            [at],
        ).pl()

    def negatives(self) -> pl.DataFrame:
        """Rows where cumulative balance goes negative — build ran without component."""
        return self.con.execute("""
            WITH ordered AS (
                SELECT plant, component, ts, qty_delta, source, lot_id,
                       sum(qty_delta) OVER (PARTITION BY plant, component
                                             ORDER BY ts, source
                                             ROWS UNBOUNDED PRECEDING) AS run_bal
                FROM component_events
            )
            SELECT * FROM ordered WHERE run_bal < 0
        """).pl()

    def dump(self, out: Path) -> Path:
        out.parent.mkdir(parents=True, exist_ok=True)
        self.con.execute(
            f"COPY (SELECT * FROM component_events ORDER BY ts) TO '{out.as_posix()}' "
            "(FORMAT PARQUET, COMPRESSION ZSTD);"
        )
        return out


# ---------------------------- BOM explosion -------------------------------

# Precomputed once per process: (plant, gt_root) -> {leaf_component: qty_per_unit_gt}.
_GT_LEAVES_CACHE: dict | None = None


def _precompute_gt_leaves() -> dict[tuple[str, str], dict[str, float]]:
    """Walk v_bom once in Python. For each (plant, root_parent) accumulate the
    total qty of each leaf reached (leaf = never appears as parent).
    Cycles are prevented by a visited-set per walk."""
    con = duck()
    edges = con.execute("""
        SELECT plant, parent, child, coalesce(child_qty, 1.0) AS qty
        FROM v_bom
    """).pl()
    parents = con.execute("SELECT DISTINCT plant, parent FROM v_bom").pl()
    is_parent = {(r["plant"], r["parent"]) for r in parents.iter_rows(named=True)}

    # Adjacency: (plant, parent) -> [(child, qty), ...]
    adj: dict[tuple[str, str], list[tuple[str, float]]] = {}
    for row in edges.iter_rows(named=True):
        adj.setdefault((row["plant"], row["parent"]), []).append(
            (row["child"], float(row["qty"]))
        )

    # BFS from every distinct parent, accumulate leaf multipliers.
    out: dict[tuple[str, str], dict[str, float]] = {}
    for (plant, root) in list(adj.keys()):
        leaves: dict[str, float] = {}
        stack = [(root, 1.0)]
        visited: set[str] = set()
        while stack:
            node, mult = stack.pop()
            if node in visited:
                continue
            visited.add(node)
            children = adj.get((plant, node))
            if not children:
                leaves[node] = leaves.get(node, 0.0) + mult
                continue
            for c, q in children:
                if (plant, c) in is_parent:
                    stack.append((c, mult * q))
                else:
                    leaves[c] = leaves.get(c, 0.0) + mult * q
        if leaves:
            out[(plant, root)] = leaves

    log.info("bom.leaves.precomputed", roots=len(out))
    return out


def explode_bom(build_df: pl.DataFrame) -> pl.DataFrame:
    """Emit one negative component_event per (lot, leaf) at lot start_ts.

    Uses a memoised per-GT leaf multiplier table so no recursion runs per call.
    """
    global _GT_LEAVES_CACHE
    if build_df.height == 0:
        return pl.DataFrame()
    if _GT_LEAVES_CACHE is None:
        _GT_LEAVES_CACHE = _precompute_gt_leaves()
    if not _GT_LEAVES_CACHE:
        return pl.DataFrame()

    rows: list[dict] = []
    hit = 0
    miss = 0
    for r in build_df.iter_rows(named=True):
        leaves = _GT_LEAVES_CACHE.get((r["plant"], r["gt_code"]))
        if not leaves:
            miss += 1
            continue
        hit += 1
        lot_qty = float(r["qty"])
        for comp, per_unit in leaves.items():
            rows.append({
                "ts": r["start_ts"],
                "plant": r["plant"],
                "component": comp,
                "qty_delta": -1.0 * lot_qty * per_unit,
                "source": "build",
                "lot_id": r["lot_id"],
            })
    df = pl.DataFrame(rows) if rows else pl.DataFrame()
    log.info("bom.explode", n_events=df.height, n_lots=build_df.height,
             hit=hit, miss=miss)
    return df


# ---------------------------- Opening inventory ---------------------------

def opening_component_inventory(cutoff: datetime) -> pl.DataFrame:
    """Derive per-component starting balance at cutoff from MES consumption.

    Initialise each component to `max(0, mean_daily_usage × 3d_buffer)`.
    Uses `date` partition pruning (the Hive key) so DuckDB skips 14.9M-row
    scans on unrelated days.
    """
    from datetime import date as _date
    cutoff_date = cutoff.date() if hasattr(cutoff, "date") else cutoff
    con = duck()
    df = con.execute(
        """
        WITH used AS (
            SELECT plant, consumptionItemCode AS component,
                   sum(consumedQuantity)::DOUBLE AS total_used,
                   date_diff('day', min(date), ?::DATE) AS days_seen
            FROM v_consume
            WHERE date < ?::DATE
            GROUP BY 1,2
        )
        SELECT plant, component,
               greatest(0.0, (total_used / greatest(days_seen, 1)) * 3.0) AS opening_qty
        FROM used
        WHERE total_used > 0
        """,
        [cutoff_date, cutoff_date],
    ).pl()
    log.info("bom.opening_inventory", cutoff=str(cutoff_date), rows=df.height)
    return df
