"""RCA: PCR occupancy inversion + changeover imbalance.  READ-ONLY diagnostic.

    python scripts/_diag_load_alloc.py <run-dir> <YYYY-MM>

Decomposes machine occupancy into
  * a RIM-STRUCTURAL component  -- what the rim lock forces, given each rim's
    demand and the machines locked to it, and
  * an ALLOCATION component     -- the spread that survives INSIDE a rim group,
    which is the only part any tie-break can move.
"""
from __future__ import annotations

import calendar
import sys
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parent.parent
INP = ROOT.parent.parent / "INPUT" / "derived"
DER = ROOT / "warehouse" / "derived"

pl.Config.set_tbl_rows(80)
pl.Config.set_tbl_width_chars(220)


def main(run: Path, month: str) -> None:
    y, m = (int(x) for x in month.split("-"))
    hours = calendar.monthrange(y, m)[1] * 24.0

    lock = pl.read_parquet(INP / "machine_rim_lock.parquet")
    cad = pl.read_parquet(INP / "cycle_time_building.parquet")
    gts = (pl.read_parquet(INP / "gt_size.parquet")
           .select("plant", "gt_code", "rim").unique())
    net = pl.read_parquet(DER / f"net_requirement_{month}.parquet")
    bs = pl.read_parquet(run / "build_schedule.parquet")
    st = pl.read_parquet(run / "build_starved.parquet")

    mach = (lock.join(cad, on=["plant", "machine"], how="left")
            .select("plant", "machine", "locked_rim", "tier", "s_per_tyre"))

    for plant in ["PCR", "TBR"]:
        M = mach.filter(pl.col("plant") == plant)
        print("=" * 100)
        print(f"{plant}   {month}   {hours:.0f} calendar hours/machine")
        print("=" * 100)

        # ---------- realised occupancy, re-derived from the plan ------------
        real = (bs.filter((pl.col("plant") == plant)
                          & (pl.col("machine") != "OPENING_STOCK"))
                .group_by("machine")
                .agg(pl.col("qty").sum().alias("tyres"),
                     ((pl.col("end_ts") - pl.col("start_ts"))
                      .dt.total_seconds().sum() / 3600.0).alias("busy_h"),
                     pl.col("run_id").n_unique().alias("run_ids"),
                     pl.col("gt_code").n_unique().alias("gts")))
        real = (M.join(real, on="machine", how="left").fill_null(0.0)
                .with_columns((pl.col("busy_h") / hours * 100).alias("occ")))

        # ---------- rim demand, independent of the plan ---------------------
        d = (net.filter(pl.col("plant") == plant)
             .join(gts.filter(pl.col("plant") == plant).unique(["gt_code"]),
                   on=["plant", "gt_code"], how="left")
             .with_columns(pl.col("rim").fill_null("?")))
        rim_q = d.group_by("rim").agg(pl.col("gross_build").sum().alias("dem_q"))

        # capacity of a rim group, in TYRES: sum over its machines of hours/cadence
        cap = (M.group_by("locked_rim")
               .agg((hours * 3600.0 / pl.col("s_per_tyre")).sum().alias("cap_q"),
                    pl.len().alias("n_mach"),
                    pl.col("s_per_tyre").mean().alias("cad_mean"),
                    pl.col("machine").sort().str.concat(" ").alias("machines"))
               .rename({"locked_rim": "rim"}))
        rim = (cap.join(rim_q, on="rim", how="full", coalesce=True)
               .fill_null(0.0)
               .with_columns((pl.col("dem_q") / pl.col("cap_q") * 100)
                             .alias("struct_occ_pct")))
        print("\n-- RIM STRUCTURE (demand vs the capacity its OWN locked machines hold)")
        print(rim.sort("struct_occ_pct", descending=True)
              .select("rim", "n_mach", "cad_mean", "dem_q", "cap_q",
                      "struct_occ_pct", "machines"))

        # ---------- per-machine: structural vs realised ---------------------
        j = (real.join(rim.select("rim", "struct_occ_pct", "n_mach"),
                       left_on="locked_rim", right_on="rim", how="left")
             .with_columns(
                 (pl.col("occ") - pl.col("struct_occ_pct")).alias("resid")))
        print("\n-- PER MACHINE: realised occ vs the rim's structural occ")
        print(j.select("machine", "tier", "locked_rim", "n_mach", "s_per_tyre",
                       "tyres", "busy_h", "occ", "struct_occ_pct", "resid",
                       "run_ids", "gts")
              .sort("occ", descending=True))

        # ---------- variance decomposition ----------------------------------
        occ = j["occ"].to_numpy()
        gmean = occ.mean()
        tot_ss = float(((occ - gmean) ** 2).sum())
        # between = each rim group's mean occ vs grand mean
        bet = 0.0
        wit = 0.0
        for r in j["locked_rim"].unique().to_list():
            sub = j.filter(pl.col("locked_rim") == r)["occ"].to_numpy()
            bet += len(sub) * (sub.mean() - gmean) ** 2
            wit += float(((sub - sub.mean()) ** 2).sum())
        print(f"\n-- OCCUPANCY VARIANCE DECOMPOSITION  (n={len(occ)} machines)")
        print(f"   grand mean {gmean:6.2f} %   min {occ.min():6.2f}   "
              f"max {occ.max():6.2f}   spread {occ.max()-occ.min():6.2f} pt   "
              f"CV {occ.std()/gmean:.4f}")
        print(f"   TOTAL SS {tot_ss:9.2f}")
        print(f"   BETWEEN rim groups {bet:9.2f}  ({bet/tot_ss*100:5.1f} %)"
              "   <- rim-structural, cannot move without breaking the lock")
        print(f"   WITHIN  rim groups {wit:9.2f}  ({wit/tot_ss*100:5.1f} %)"
              "   <- allocation, the only addressable part")
        for r in sorted(j["locked_rim"].unique().to_list()):
            sub = j.filter(pl.col("locked_rim") == r)
            o = sub["occ"].to_numpy()
            if len(o) > 1:
                print(f"      {r}: n={len(o)}  occ {o.min():.1f}-{o.max():.1f}"
                      f"  spread {o.max()-o.min():5.2f} pt"
                      f"  cadences {sorted(sub['s_per_tyre'].to_list())}")
            else:
                print(f"      {r}: n=1  occ {o[0]:.1f}  -- NO allocation freedom")

        # ---------- speed correlation ---------------------------------------
        import numpy as np
        cadv = j["s_per_tyre"].to_numpy()
        if len(cadv) > 2:
            print(f"\n   corr(cadence_s, occupancy) = "
                  f"{np.corrcoef(cadv, occ)[0,1]: .3f}"
                  "   (positive = slow machines busier)")
            resid = j["resid"].to_numpy()
            print(f"   corr(cadence_s, residual)  = "
                  f"{np.corrcoef(cadv, resid)[0,1]: .3f}"
                  "   (residual = realised - rim structural)")

        # ---------- run / changeover structure -------------------------------
        runs = (bs.filter((pl.col("plant") == plant)
                          & (pl.col("machine") != "OPENING_STOCK")
                          & pl.col("run_id").is_not_null())
                .group_by("run_id")
                .agg(pl.col("machine").first(), pl.col("gt_code").first(),
                     pl.col("qty").sum().alias("q"),
                     pl.len().alias("slices")))
        rs = (runs.group_by("machine")
              .agg(pl.len().alias("n_runs"),
                   pl.col("q").median().alias("p50"),
                   pl.col("q").quantile(0.25).alias("p25"),
                   pl.col("q").quantile(0.75).alias("p75"),
                   pl.col("q").sum().alias("q_tot"),
                   pl.col("gt_code").n_unique().alias("gts")))
        print("\n-- RUN STRUCTURE per machine (run_id groups)")
        print(M.join(rs, on="machine", how="left")
              .select("machine", "locked_rim", "s_per_tyre", "n_runs",
                      "p25", "p50", "p75", "q_tot", "gts")
              .sort("n_runs", descending=True))

        # per-GT run structure -- where the small runs come from
        gr = (runs.group_by(["machine", "gt_code"])
              .agg(pl.len().alias("n"), pl.col("q").median().alias("p50"),
                   pl.col("q").sum().alias("tot")))
        print("\n-- PER (machine, GT): the 18 most-fragmented")
        print(gr.sort("n", descending=True).head(18))

        sv = st.filter(pl.col("plant") == plant)
        print(f"\n-- starved {sv['qty'].sum():,.0f} tyres, "
              f"demand {d['gross_build'].sum():,.0f}")


if __name__ == "__main__":
    main(Path(sys.argv[1]), sys.argv[2])
