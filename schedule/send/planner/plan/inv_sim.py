"""Inventory simulator — GT ledger + real BOM-driven component ledger.

Reads the build_schedule + BOM, explodes each build lot to raw components,
writes a component_events ledger, and reports negative-balance events as soft
violations.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import polars as pl

from planner.plan.component_ledger import (
    ComponentLedger,
    explode_bom,
    opening_component_inventory,
)
from planner.plan.ledger import GreenTireLedger
from planner.runs.logger import log


def simulate(build_schedule: pl.DataFrame,
             cure_schedule: pl.DataFrame | None,
             ledger: GreenTireLedger,
             out_dir: Path,
             *,
             cutoff: datetime | None = None) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)

    # -------- GT side --------
    gt_bal = ledger.balances()
    gt_bal.write_parquet(out_dir / "gt_final_balances.parquet", compression="zstd")

    gt_starve = ledger.starvations()
    if gt_starve.height:
        gt_starve.write_parquet(out_dir / "gt_starvation.parquet", compression="zstd")
        log.warning("inv_sim.gt_starvation", rows=gt_starve.height)

    daily_delta = ledger.con.execute("""
        SELECT plant, gt_code, CAST(ts AS DATE) AS date, sum(qty_delta) AS delta
        FROM gt_events GROUP BY 1,2,3 ORDER BY 1,2,3
    """).pl()
    daily_delta.write_parquet(out_dir / "gt_daily_delta.parquet", compression="zstd")

    # Persist full ledger event stream so the independent verifier can reason
    # about opening supply + build supply + cure consumption jointly.
    ledger.dump(out_dir / "gt_events.parquet")

    # -------- Component side (real BOM explosion) --------
    comp_ledger = ComponentLedger()
    events = explode_bom(build_schedule)
    comp_ledger.add_df(events)

    # Opening balances
    if cutoff is None and build_schedule.height:
        cutoff = build_schedule["start_ts"].min()
    if cutoff is not None:
        openings = opening_component_inventory(cutoff)
        if openings.height:
            open_events = openings.select([
                pl.lit(cutoff).alias("ts"),
                pl.col("plant"),
                pl.col("component"),
                pl.col("opening_qty").alias("qty_delta"),
                pl.lit("opening").alias("source"),
                pl.lit("opening").alias("lot_id"),
            ])
            comp_ledger.add_df(open_events)

    comp_bal = comp_ledger.balances()
    comp_bal.write_parquet(out_dir / "component_final_balances.parquet", compression="zstd")

    comp_neg = comp_ledger.negatives()
    if comp_neg.height:
        comp_neg.write_parquet(out_dir / "component_starvation.parquet", compression="zstd")
        log.warning("inv_sim.component_starvation", rows=comp_neg.height)

    comp_ledger.dump(out_dir / "component_events.parquet")

    n_gt_starve = gt_starve.height
    n_comp_neg = comp_neg.height
    log.info("inv_sim.done",
             gt_balances=gt_bal.height,
             gt_starvations=n_gt_starve,
             component_events=events.height,
             component_starvations=n_comp_neg)
    return {
        "gt_balances": gt_bal.height,
        "gt_starvations": n_gt_starve,
        "component_events": events.height,
        "component_starvations": n_comp_neg,
    }
