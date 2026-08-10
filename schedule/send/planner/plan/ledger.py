"""Green-Tire Ledger — shared inventory of GT codes across building & curing.

Backed by an in-memory DuckDB table `gt_events(ts, plant, gt_code, qty_delta,
source, lot_id)`. Build lots write positive deltas; cure lots write negative.
Callers can query `available(gt_code, ts)` at any planning timestamp.

Derives OPENING balance from MES history using the productionID↔gtbarCode
lineage (builds not yet cured before the cutoff).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

import duckdb
import polars as pl

from planner.data.warehouse import duck
from planner.runs.logger import log


@dataclass
class GTEvent:
    ts: datetime
    plant: str
    gt_code: str
    qty_delta: float
    source: str        # 'build' | 'cure' | 'opening'
    lot_id: str


class GreenTireLedger:
    """Per-run ledger. Not shared across runs — pass one instance through the
    building/curing/sync loop."""

    _DDL = """
    CREATE TABLE IF NOT EXISTS gt_events (
        ts       TIMESTAMP,
        plant    VARCHAR,
        gt_code  VARCHAR,
        qty_delta DOUBLE,
        source   VARCHAR,
        lot_id   VARCHAR
    );
    """

    def __init__(self, path: Path | None = None):
        # DuckDB in-memory for hot path; optionally persisted at end via `dump()`.
        self.con = duckdb.connect(":memory:")
        self.con.execute(self._DDL)
        self._path = path

    # -------------------- writes --------------------
    def add(self, events: Iterable[GTEvent]) -> int:
        rows = [(e.ts, e.plant, e.gt_code, e.qty_delta, e.source, e.lot_id) for e in events]
        if not rows:
            return 0
        self.con.executemany(
            "INSERT INTO gt_events VALUES (?, ?, ?, ?, ?, ?)", rows,
        )
        return len(rows)

    # -------------------- reads --------------------
    def available(self, plant: str, gt_code: str, at: datetime) -> float:
        r = self.con.execute(
            "SELECT COALESCE(sum(qty_delta), 0) FROM gt_events "
            "WHERE plant = ? AND gt_code = ? AND ts <= ?",
            [plant, gt_code, at],
        ).fetchone()
        return float(r[0])

    def earliest_available(self, plant: str, gt_code: str, min_qty: float) -> datetime | None:
        # Cumulative sum over event stream; return first ts where running total >= min_qty.
        rows = self.con.execute(
            "SELECT ts, qty_delta FROM gt_events "
            "WHERE plant = ? AND gt_code = ? ORDER BY ts",
            [plant, gt_code],
        ).fetchall()
        run = 0.0
        for ts, delta in rows:
            run += delta
            if run >= min_qty:
                return ts
        return None

    def balances(self, at: datetime | None = None) -> pl.DataFrame:
        if at is None:
            sql = "SELECT plant, gt_code, sum(qty_delta) AS qty FROM gt_events GROUP BY 1,2 ORDER BY 1,2"
            return self.con.execute(sql).pl()
        return self.con.execute(
            "SELECT plant, gt_code, sum(qty_delta) AS qty FROM gt_events "
            "WHERE ts <= ? GROUP BY 1,2 ORDER BY 1,2",
            [at],
        ).pl()

    def starvations(self) -> pl.DataFrame:
        """Rows where cumulative balance goes negative — cure ran without GT."""
        return self.con.execute("""
            WITH ordered AS (
                SELECT plant, gt_code, ts, qty_delta, source, lot_id,
                       sum(qty_delta) OVER (PARTITION BY plant, gt_code ORDER BY ts, source
                                            ROWS UNBOUNDED PRECEDING) AS run_bal
                FROM gt_events
            )
            SELECT * FROM ordered WHERE run_bal < 0
        """).pl()

    # -------------------- opening from MES history --------------------
    def load_opening_from_mes(self, cutoff: datetime, max_age_h: float | None = None) -> int:
        """GT built before cutoff but not yet cured = opening GT WIP.

        productionID (build stage2) joins to gtbarCode (curing): a build with no
        curing row before the cutoff is still sitting as a green tyre.

        Two things this must get right, both of which were wrong before:

        1. `built AND NOT cured-before-cutoff` also matches tyres that were never
           cured *at all* -- scrap and quality rejects -- which are not stock. We
           can't see the future to tell them apart, so we bound by age: a tyre
           older than the observed build->cure p99 lag is not waiting, it is
           gone. The lag is measured from pre-cutoff data only, so this stays
           leak-free.
        2. Emit ONE ROW PER TYRE, not one row per GT carrying a qty. Build events
           are per-tyre, and sync/kpi rank supply *by row* -- so an aggregated
           opening row counted as a single tyre and silently dropped the rest,
           which then resurfaced as phantom demand shortfall.
        """
        con = duck()
        if max_age_h is None:
            r = con.execute(
                """
                SELECT quantile_cont(
                           date_diff('second', b.event_ts, c.event_ts) / 3600.0, 0.99)
                FROM v_build b JOIN v_curing c ON b.productionID = c.gtbarCode
                WHERE b.stage = 2 AND b.QualityStatus = '1'
                  AND c.statuscritical = 'Normal' AND c.event_ts >= b.event_ts
                """
            ).fetchone()
            max_age_h = float(r[0]) if r and r[0] else GT_SHELF_LIFE_H
        # A tyre older than the shelf life is scrap, never opening stock. The
        # p99 lag above (and the old 168 h fallback) could exceed it, admitting
        # already-dead tyres as usable WIP.
        max_age_h = min(max_age_h, GT_SHELF_LIFE_H)
        floor_ts = cutoff - timedelta(hours=max_age_h)

        df = con.execute(
            """
            WITH built AS (
                SELECT plant, itemCode AS gt_code, productionID AS gtbar, event_ts
                FROM v_build
                WHERE stage = 2 AND QualityStatus = '1'
                  AND event_ts < ? AND event_ts >= ?
            ),
            cured AS (
                SELECT gtbarCode AS gtbar
                FROM v_curing
                WHERE event_ts < ? AND statuscritical = 'Normal'
            )
            SELECT b.plant, b.gt_code, b.event_ts AS ts
            FROM built b
            LEFT JOIN cured c ON b.gtbar = c.gtbar
            WHERE c.gtbar IS NULL
            """,
            [cutoff, floor_ts, cutoff],
        ).pl()

        if df.height == 0:
            log.info("ledger.opening_loaded", cutoff=str(cutoff), tyres=0)
            return 0

        # Bulk path -- never per-row insert at this volume.
        opening = df.with_columns(
            pl.lit(1.0).alias("qty_delta"),
            pl.lit("opening").alias("source"),
            (pl.lit("opening_") + pl.col("gt_code")).alias("lot_id"),
        ).select(["ts", "plant", "gt_code", "qty_delta", "source", "lot_id"])
        self.con.register("_open_evt", opening.to_arrow())
        self.con.execute("INSERT INTO gt_events SELECT * FROM _open_evt")
        self.con.unregister("_open_evt")

        log.info("ledger.opening_loaded", cutoff=str(cutoff), tyres=opening.height,
                 gt_codes=df["gt_code"].n_unique(), max_age_h=round(max_age_h, 1))
        return opening.height

    # -------------------- persistence --------------------
    def dump(self, out: Path) -> Path:
        out.parent.mkdir(parents=True, exist_ok=True)
        self.con.execute(
            f"COPY (SELECT * FROM gt_events ORDER BY ts) TO '{out.as_posix()}' "
            "(FORMAT PARQUET, COMPRESSION ZSTD);"
        )
        log.info("ledger.dump", path=str(out))
        return out
