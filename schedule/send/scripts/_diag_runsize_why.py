"""Why does one machine run 132 lots of 160 and its neighbour 41 of 755?

    python scripts/_diag_runsize_why.py <run-dir> <YYYY-MM> [PCR]

Rebuilds L7's own run-size target for every GT -- target = max(floor, r_g x T),
capped by r_g x span_cap -- from the SAME cure demand L7 saw, and shows which
term binds.  A GT whose target is pinned at the floor is rebuilt every
floor/r_g hours no matter how long the month is; that, not the machine, is what
sets the changeover count.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import polars as pl
from planner import paths

ROOT = Path(__file__).resolve().parent.parent
INP = paths.INPUT_DERIVED
FLOOR = {"PCR": 150.0, "TBR": 70.0}
TAU = {"PCR": 4.32, "TBR": 4.81}
T = 16.0
pl.Config.set_tbl_rows(60)
pl.Config.set_tbl_width_chars(200)


def main(run: Path, month: str, plant: str) -> None:
    lock = pl.read_parquet(INP / "machine_rim_lock.parquet") \
        .filter(pl.col("plant") == plant)
    co = {r["machine"]: r for r in lock.iter_rows(named=True)}
    b = (pl.read_parquet(run / "build_schedule.parquet")
         .filter((pl.col("plant") == plant)
                 & (pl.col("machine") != "OPENING_STOCK")))
    span_cap = 72.0 - TAU[plant] - 2.0

    # r_g exactly as L7 derives it: qty / (last cure - first cure), per GT
    g = (b.group_by("gt_code").agg(
        pl.col("qty").sum().alias("q"),
        pl.col("cure_ts").min().alias("c0"), pl.col("cure_ts").max().alias("c1"),
        pl.col("run_id").n_unique().alias("runs"),
        pl.col("machine").n_unique().alias("machs"))
        .with_columns(((pl.col("c1") - pl.col("c0")).dt.total_seconds()
                       / 3600.0).alias("span_h")))
    g = g.with_columns(
        pl.when(pl.col("span_h") > 1e-6)
        .then(pl.col("q") / pl.col("span_h")).otherwise(pl.col("q")).alias("r_g"))
    g = g.with_columns(
        (pl.col("r_g") * T).alias("rT"),
        pl.max_horizontal(pl.lit(FLOOR[plant]), pl.col("r_g") * T).alias("tgt_raw"))
    g = g.with_columns(
        pl.min_horizontal(pl.col("tgt_raw"), pl.col("r_g") * span_cap)
        .alias("target"),
        (pl.col("rT") < FLOOR[plant]).alias("floor_binds"))

    # observed run sizes
    rs = (b.group_by("run_id").agg(pl.col("gt_code").first(),
                                   pl.col("machine").first(),
                                   pl.col("qty").sum().alias("rq")))
    obs = rs.group_by("gt_code").agg(pl.col("rq").median().alias("obs_p50"))
    g = g.join(obs, on="gt_code", how="left")

    print("=" * 110)
    print(f"{plant} {month}: run-size target per GT  (floor {FLOOR[plant]:.0f}, "
          f"T {T:.0f} h, span_cap {span_cap:.1f} h)")
    print("=" * 110)
    print(g.select("gt_code", "q", "span_h", "r_g", "rT", "target", "obs_p50",
                   "runs", "machs", "floor_binds")
          .sort("runs", descending=True))
    fb = g.filter(pl.col("floor_binds"))
    print(f"\n  GTs where the FLOOR sets the target (r_g x T < floor): "
          f"{fb.height} of {g.height}, "
          f"{fb['q'].sum():,.0f} of {g['q'].sum():,.0f} tyres "
          f"({fb['q'].sum()/g['q'].sum()*100:.1f} %), "
          f"{fb['runs'].sum():,} of {g['runs'].sum():,} runs "
          f"({fb['runs'].sum()/g['runs'].sum()*100:.1f} %)")

    # ---- per machine: runs attributable to floor-bound GTs -----------------
    rr = rs.join(g.select("gt_code", "floor_binds", "r_g", "target"),
                 on="gt_code", how="left")
    per = (rr.group_by("machine").agg(
        pl.len().alias("runs"),
        pl.col("floor_binds").sum().alias("runs_floor_bound"),
        pl.col("rq").median().alias("p50"),
        pl.col("rq").sum().alias("q"),
        pl.col("gt_code").n_unique().alias("gts")))
    per = per.with_columns(
        (pl.col("runs_floor_bound") / pl.col("runs") * 100).round(1).alias("pct_fb"))
    per = per.with_columns(
        pl.col("machine").map_elements(
            lambda m: float(28.0 if int(''.join(c for c in m if c.isdigit())[:2]
                                        .rstrip('2') or 0) <= 5 and plant == "PCR"
                            else (22.0 if plant == "PCR" else 10.0)),
            return_dtype=pl.Float64).alias("same_min"))
    print("\n-- per machine: how many runs belong to floor-bound GTs")
    print(per.join(lock.select("machine", "locked_rim"), on="machine", how="left")
          .select("machine", "locked_rim", "gts", "runs", "runs_floor_bound",
                  "pct_fb", "p50", "q")
          .sort("runs", descending=True))

    # ---- changeover minutes actually spent, per machine --------------------
    rim = {r["gt_code"]: r["rim"] for r in
           pl.read_parquet(INP / "gt_size.parquet")
           .filter(pl.col("plant") == plant).unique(["gt_code"])
           .iter_rows(named=True)}
    print("\n-- changeover minutes actually on the calendar")
    tot = []
    for m in sorted(b["machine"].unique()):
        rows = b.filter(pl.col("machine") == m).sort("start_ts").to_dicts()
        n = int(''.join(c for c in m if c.isdigit())[:-1] or 0)  # strip Stage2 "2"
        s_min, d_min = ((28.0, 60.0) if (plant == "PCR" and n <= 5)
                        else (22.0, 42.0) if plant == "PCR" else (10.0, 24.0))
        co_n = same_n = 0
        mins = 0.0
        for a, c in zip(rows, rows[1:]):
            gap = (c["start_ts"] - a["end_ts"]).total_seconds() / 3600.0
            if c["gt_code"] == a["gt_code"] and gap <= 1.0:
                continue
            co_n += 1
            if rim.get(a["gt_code"]) == rim.get(c["gt_code"]):
                same_n += 1
                mins += s_min
            else:
                mins += d_min
        tot.append((m, co_n, same_n, co_n - same_n, mins, mins / 60.0))
    for m, cn, sn, dn, mins, hrs in sorted(tot, key=lambda x: -x[4]):
        print(f"   {m.replace('Stage2',''):<12} CO {cn:>4}  same {sn:>4}  "
              f"diff {dn:>3}  {mins:>7,.0f} min  {hrs:>6.1f} h")
    print(f"   TOTAL {sum(x[4] for x in tot)/60:,.1f} h")


if __name__ == "__main__":
    main(Path(sys.argv[1]), sys.argv[2],
         sys.argv[3] if len(sys.argv) > 3 else "PCR")
