"""Build ↔ Cure synchronization — FIFO pairing.

For each (plant, gt_code), pair the kth cure event with the kth supply event
(build or opening) by ascending ts. The cure's earliest legal start_ts is
max(its current start, the kth supply's ts). Vectorized in DuckDB — no per-row
Python loop.

Guarantees:
- After one pass, no cure_ts precedes the corresponding supply_ts → no
  starvation for pairs where enough supply exists.
- Where supply is insufficient (fewer builds than cures for that GT), the
  unmatched cures are dropped from the schedule and reported as
  `demand_shortfall` soft violations.
"""
from __future__ import annotations

from pathlib import Path

import polars as pl

from planner.plan.ledger import GreenTireLedger
from planner.runs.logger import log


def sync(build_df: pl.DataFrame,
         cure_df: pl.DataFrame,
         ledger: GreenTireLedger,
         out_dir: Path,
         max_iters: int = 5) -> dict:
    """FIFO-pair cure events with their supply events per (plant, gt_code).

    max_iters kept for API compat; only 1 pass is required for correctness.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    stats = {"iterations": 0, "starvation_fixed": 0, "starvation_remaining": 0,
             "demand_shortfall": 0}

    # 1) Pull supply + cure events out of the ledger.
    supply = ledger.con.execute("""
        SELECT plant, gt_code, ts, lot_id, source
        FROM gt_events
        WHERE source IN ('build','opening') AND qty_delta > 0
    """).pl().sort(["plant", "gt_code", "ts", "lot_id"])

    cures = ledger.con.execute("""
        SELECT plant, gt_code, ts AS cure_ts, lot_id
        FROM gt_events
        WHERE source = 'cure'
    """).pl().sort(["plant", "gt_code", "cure_ts", "lot_id"])

    if supply.height == 0 or cures.height == 0:
        log.warning("sync.empty", supply=supply.height, cures=cures.height)
        return stats

    # 2) For each partition (plant, gt_code) attach a rank; join by rank.
    supply = supply.with_columns(
        pl.col("ts").rank("ordinal").over(["plant", "gt_code"]).alias("rk")
    ).select(["plant", "gt_code", "rk", pl.col("ts").alias("supply_ts"),
              pl.col("lot_id").alias("supply_lot_id")])
    cures = cures.with_columns(
        pl.col("cure_ts").rank("ordinal").over(["plant", "gt_code"]).alias("rk")
    )

    paired = cures.join(supply, on=["plant", "gt_code", "rk"], how="left")
    # supply_ts NULL → this cure has no corresponding supply → demand shortfall
    shortfall = paired.filter(pl.col("supply_ts").is_null())
    stats["demand_shortfall"] = shortfall.height
    valid = paired.filter(pl.col("supply_ts").is_not_null())

    # 3) New cure_ts = max(old cure_ts, supply_ts). Only writes changes when supply is later.
    updates = valid.with_columns(
        pl.max_horizontal("cure_ts", "supply_ts").alias("new_ts")
    ).filter(pl.col("new_ts") != pl.col("cure_ts")).select([
        "lot_id", "new_ts",
    ])
    stats["starvation_fixed"] = updates.height
    stats["iterations"] = 1

    # 4) Apply updates via DuckDB register + join-update (bulk).
    if updates.height:
        ledger.con.register("_upd", updates.to_arrow())
        ledger.con.execute("""
            UPDATE gt_events e
            SET ts = u.new_ts
            FROM _upd u
            WHERE e.lot_id = u.lot_id AND e.source = 'cure'
        """)
        ledger.con.unregister("_upd")

    # 5) Drop unmatched cures from ledger — they're soft violations.
    if shortfall.height:
        ids = shortfall["lot_id"].to_list()
        ledger.con.execute(
            "DELETE FROM gt_events WHERE source = 'cure' AND lot_id IN "
            f"({','.join(['?']*len(ids))})",
            ids,
        )
        shortfall.write_parquet(out_dir / "demand_shortfall.parquet", compression="zstd")

    # 6) Rewrite cure_df with new ts values for downstream reporting.
    if cure_df.height and (updates.height or shortfall.height):
        new_ts_map = dict(zip(updates["lot_id"].to_list(), updates["new_ts"].to_list()))
        drop_ids = set(shortfall["lot_id"].to_list())
        cure_df = cure_df.filter(~pl.col("lot_id").is_in(list(drop_ids)))
        if new_ts_map:
            # Replace start_ts where lot_id in map; end_ts += shift.
            m_df = pl.DataFrame({"lot_id": list(new_ts_map.keys()),
                                 "new_start": list(new_ts_map.values())})
            cure_df = (cure_df.join(m_df, on="lot_id", how="left")
                              .with_columns([
                                  pl.when(pl.col("new_start").is_not_null())
                                    .then(pl.col("new_start"))
                                    .otherwise(pl.col("start_ts"))
                                    .alias("start_ts"),
                              ])
                              .with_columns(
                                  (pl.col("start_ts") + pl.duration(seconds=pl.col("cycle_s")))
                                  .alias("end_ts")
                              )
                              .drop("new_start"))
        cure_df.write_parquet(out_dir / "cure_schedule.parquet", compression="zstd")

    # 7) Post-sync starvation should be zero given correct FIFO pairing.
    remaining = ledger.starvations()
    stats["starvation_remaining"] = remaining.height
    if remaining.height:
        remaining.write_parquet(out_dir / "starvation_unresolved.parquet", compression="zstd")
    log.info("sync.done", **stats)
    return stats
