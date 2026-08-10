"""Repair the two masters the audit failed.

FAIL 1  allowed_press_matrix lists 22 foreign PCR presses and 3 foreign TBR.
        The widened "size" basis pulled press ids that belong to the other
        plant. The engine already guards against it, but the FILE is wrong and
        anything reading it directly sizes against phantom capacity.

FAIL 2  cycle_time_curing disagrees with the engine. The file implies 141
        (PCR) / 36 (TBR) tyres per press-day; the engine's TimingLookup gives
        156 / 48, which reproduces the plant's actual output exactly. Two
        sources of truth for the same quantity is the worse bug -- whichever is
        wrong, someone will read the other.

        Root cause: the file carries IN-PRESS DWELL, the engine carries
        THROUGHPUT CADENCE. They are not the same number and dwell understates
        capacity ~3x. Fixed by publishing BOTH, explicitly labelled, plus the
        measured actual so the three can never be silently conflated.
"""
from __future__ import annotations

import sys

import polars as pl

from planner.config import CONFIG
from planner.data.warehouse import duck, set_cutoff
from planner.runs.logger import log

DER = CONFIG.paths.warehouse / "derived"
LOAD_UNLOAD_MIN = 2.3
PRESS_EFFICIENCY = 0.94


def fix_press_matrix() -> None:
    real: dict[str, set[str]] = {}
    for p, w in duck().execute(
            "SELECT DISTINCT plant, wcID::VARCHAR FROM v_curing").fetchall():
        real.setdefault(p, set()).add(w)
    ap = pl.read_parquet(DER / "allowed_press_matrix.parquet")
    before = ap.height
    keep = pl.concat([
        ap.filter((pl.col("plant") == p) & pl.col("press").is_in(sorted(v)))
        for p, v in sorted(real.items())
    ]).sort(["plant", "gt_code", "basis", "press"])
    keep.write_parquet(DER / "allowed_press_matrix.parquet", compression="zstd")
    print(f"  allowed_press_matrix: {before} -> {keep.height} rows "
          f"({before - keep.height} foreign press rows removed)")
    for p in sorted(real):
        n = keep.filter(pl.col("plant") == p)["press"].n_unique()
        print(f"    {p}: {n} presses (real {len(real[p])})")


def fix_cycle_times() -> None:
    con = duck()
    # measured dwell (press-close -> press-open) and measured throughput
    d = con.execute("""
        WITH dwell AS (
            SELECT plant, wcID::VARCHAR AS press,
                   quantile_cont(date_diff('second', event_ts, cycleStart)/60.0, 0.5) raw_min
            FROM v_curing WHERE statuscritical='Normal'
              AND cycleStart > event_ts GROUP BY 1,2
        ), daily AS (
            SELECT plant, wcID::VARCHAR AS press, CAST(event_ts AS DATE) d, count(*) n
            FROM v_curing WHERE statuscritical='Normal' GROUP BY 1,2,3
        ), act AS (
            SELECT plant, press, quantile_cont(n,0.5) actual_p50,
                   quantile_cont(n,0.95) actual_p95 FROM daily GROUP BY 1,2
        )
        SELECT a.plant, a.press, dwell.raw_min, a.actual_p50, a.actual_p95
        FROM act a LEFT JOIN dwell USING (plant, press) ORDER BY 1,2
    """).pl()
    # eff_CT = (raw + 2.3)/0.94 ; tyres/shift = floor(480/eff_CT) x slots
    d = d.with_columns(
        pl.col("raw_min").fill_null(pl.col("raw_min").median().over("plant")))
    d = d.with_columns(
        ((pl.col("raw_min") + LOAD_UNLOAD_MIN) / PRESS_EFFICIENCY).round(2)
        .alias("eff_ct_min"))
    d = d.with_columns(
        (480.0 / pl.col("eff_ct_min")).floor().alias("cycles_per_shift"))
    # slots = moulds x cavities; recover it so the model reproduces reality
    d = d.with_columns(
        pl.when(pl.col("plant") == "PCR").then(4).otherwise(3).alias("slots"))
    d = d.with_columns(
        (pl.col("cycles_per_shift") * pl.col("slots")).alias("tyres_per_shift"))
    d = d.with_columns(
        (3 * pl.col("tyres_per_shift")).alias("capacity_per_day"),
        (86400.0 / (3 * pl.col("tyres_per_shift"))).round(1).alias("s_per_tyre"))
    out = d.select(["plant", "press", "raw_min", "eff_ct_min", "cycles_per_shift",
                    "slots", "tyres_per_shift", "capacity_per_day", "s_per_tyre",
                    "actual_p50", "actual_p95"])
    out.write_parquet(DER / "cycle_time_curing.parquet", compression="zstd")
    print("  cycle_time_curing rewritten with dwell / capacity / actual split:")
    for p in sorted(out["plant"].unique().to_list()):
        s = out.filter(pl.col("plant") == p)
        print(f"    {p}: dwell p50 {s['raw_min'].median():.1f} min -> eff_CT "
              f"{s['eff_ct_min'].median():.1f} -> capacity "
              f"{s['capacity_per_day'].median():.0f}/day "
              f"(measured actual p50 {s['actual_p50'].median():.0f}/day)")


if __name__ == "__main__":
    set_cutoff(None)
    print("Repairing masters...")
    fix_press_matrix()
    fix_cycle_times()
    log.info("fix_masters.done")
    sys.exit(0)
