"""L4 -- NET CURE REQUIREMENT.  Phase A of the CMBC flow.

    python -m planner.cmbc.l4_net_requirement --month 2026-07

Plant steps 1-2 · rules R1, R5, R15, B12.

    net_cure     = demand - FG stock - usable GT stock          (R1)
    gross_build  = net_cure / cure_yield                        (R15)
    plannable    = gross_build where demand >= min_demand       (B12)

WHAT "USABLE" MEANS (R5)
  Opening GT is only stock if it can still be cured inside the shelf life.
  Tyres are consumed FEFO -- oldest first -- so the oldest usable tyre is
  allocated before any fresh build is scheduled. A tyre already past
  GT_SHELF_LIFE_H at the planning epoch is scrap, not stock, and is reported
  separately rather than silently dropped.

PARAMETERS COME FROM L0, NOT FROM A LOCAL QUERY
  An earlier cut recomputed cure yield inline. Two places deriving the same
  quantity drift, and the one downstream is the one nobody checks. Yield is an
  L0 parameter; this layer reads it and names the file it came from.

TWO INPUT REALITIES, STATED RATHER THAN ASSUMED
  * FINISHED-GOODS STOCK IS NOT NETTED. R1 asks for demand - FG - GT, but our
    `demand` is derived from MES production history (`scripts/make_demand.py`),
    i.e. it is what the plant actually produced, so FG netting is already
    implicit. The `fg_stock` column exists and defaults to zero: when a real
    order book arrives, subtract FG there and nothing else changes.
  * BUILD-SIDE YIELD IS NOT MEASURABLE. `v_build.QualityStatus` is '1' for all
    3.75 M rows -- no variation, so no scrap signal exists on the build side.
    We gross up by the CURE loss only and report build yield as unknown rather
    than assuming 1.0 and presenting it as measured.

BELOW-MINIMUM DEMAND IS ROUTED, NOT DROPPED (B12)
  A GT under min_demand is not worth a machine setup, but silently discarding it
  loses demand the plant still owes. It is flagged `residual` with its quantity
  intact so L4.5 can consolidate it into a later campaign, over-produce to
  stock, or surface it as a priced exception.
"""
from __future__ import annotations

import argparse
import calendar as _cal
import json
import os
from pathlib import Path

import polars as pl

from planner.cmbc import _stamp
from planner.config import CONFIG, GT_SHELF_LIFE_H

ROOT = Path(__file__).resolve().parent.parent.parent

# ---- OPENING GT SOURCE OVERRIDE ---------------------------------------
# The next month's opening stock is THIS month's carry-forward. L7 emits it as
# `masters/opening_gt/carryforward_gt_<next>.parquet` under its own name so a
# planner output can never overwrite the MES-derived `opening_gt_<month>`
# master. Point a run at it with PLANNER_OPENING_GT -- a bare filename resolves
# inside masters/opening_gt, an absolute path is taken as given.
def _opening_gt_path(root, month):
    """Thin shim over `paths.opening_gt`. There were SIX character-identical
    re-implementations of this resolver (L4, L5, L7, plus inline reads in
    export_btp_format and export_shift_schedule, plus paths.py itself), against
    CLAUDE.md's "all input lookups go through planner/paths.py". They agreed;
    the doctrine is that more than one site is the defect, because the day they
    stop agreeing nothing detects it. `root` is accepted and ignored so the call
    sites did not have to change."""
    from planner import paths as _paths
    return _paths.opening_gt(month)

D = ROOT / "warehouse" / "derived"
PARAMS = ROOT / "warehouse" / "params"


def usable_opening(month: str) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Opening GT split into usable (age <= shelf life) and expired."""
    f = _opening_gt_path(ROOT, month)
    empty = pl.DataFrame(schema={"plant": pl.Utf8, "gt_code": pl.Utf8,
                                 "usable": pl.Int64, "age_p50_h": pl.Float64})
    if not f.exists():
        return empty, empty
    o = pl.read_parquet(f).with_columns(pl.col("age_h").cast(pl.Float64))
    live = o.filter(pl.col("age_h") <= GT_SHELF_LIFE_H)
    dead = o.filter(pl.col("age_h") > GT_SHELF_LIFE_H)
    agg = (live.group_by(["plant", "gt_code"])
           .agg(pl.len().alias("usable"),
                pl.col("age_h").median().alias("age_p50_h"),
                pl.col("age_h").max().alias("age_max_h"))
           .sort(["plant", "gt_code"]))
    exp = (dead.group_by(["plant", "gt_code"])
           .agg(pl.len().alias("expired"),
                pl.col("age_h").max().alias("age_max_h")))
    return agg, exp


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--month", default="2026-07")
    # ---- ROLLING HORIZON: NEXT-MONTH DEMAND LOOKAHEAD ----------------------
    # Measured cause, PARTITION §4b: `l45_lots_<M>.parquet` holds only month M's
    # cure demand, so NOTHING PULLS BUILD on the last days of M. July 2026, v32:
    # day-31 build is 25 % of the interior mean on PCR and 7 % on TBR, and cure
    # collapses WITH it (32 % / 13 %) because the campaigns simply end. WIP is
    # cum(build) - cum(cure), so closing stock falls to ~0 by construction -- PCR
    # 486 against a 4,500 band floor, TBR 195 against 1,200. That is the G8
    # last-day failure, and it is a DEMAND-HORIZON defect, not a pacing one. The
    # plant builds on July 31 for early-August cures.
    #
    # This is the ONLY correct fix for that failure. Do NOT instead force a
    # closing-stock floor: that means building tyres with no cure to consume them
    # inside the horizon, which destroys the audited "built == fed exactly on
    # both plants" invariant (MEMORY §10d).
    #
    # BLOCKED ON DATA FOR JULY 2026, stated plainly rather than worked around:
    # `masters/demand/` ends at 2026-07 because demand is derived from what was
    # CURED in MES (scripts/make_demand.py) and MES ends 2026-07-31. There is no
    # August demand and there cannot be one. The flag therefore ships at 0 and
    # DEGRADES TO A CLEAN NO-OP when the next month's file is absent -- it must
    # never error, or every July run breaks.
    #
    # To measure it, move the reference month to one with a successor on disk
    # (June 2026 has July). That is a planning decision, not a code one.
    # ENV FALLBACK so the flag is reachable from `scripts/run_arm.py`, which
    # passes only `--month` to the month-scoped layers. Default 0 = inert; the
    # CLI arg still wins when given. Without this the ONE mechanism designed to
    # fix the month-end demand-horizon dip cannot be A/B'd at all.
    ap.add_argument("--lookahead-days", type=int,
                    default=int(os.environ.get("PLANNER_LOOKAHEAD_DAYS", "0") or 0),
                    help="append the first N days of month M+1 demand so building "
                         "has something to pull at the end of M; rows are tagged "
                         "`lookahead` and must be excluded when scoring month M")
    a = ap.parse_args()

    pj = sorted(PARAMS.glob("params_*.json"))
    if not pj:
        raise SystemExit("no L0 parameter file -- run l0_learn first")
    P = json.loads(pj[-1].read_text())
    yld = {p: float(v["cure_yield"]) for p, v in P["yields"].items()}
    min_dem = CONFIG.thresholds.min_demand_units

    dem = pl.read_parquet(ROOT / "masters" / "demand" / f"demand_{a.month}.parquet")
    la_qty = 0
    if a.lookahead_days > 0:
        _y, _m = int(a.month[:4]), int(a.month[5:7])
        nxt = f"{_y + (_m == 12)}-{(_m % 12) + 1:02d}"
        nf = ROOT / "masters" / "demand" / f"demand_{nxt}.parquet"
        if nf.exists():
            nd = pl.read_parquet(nf)
            # UNPHASED ORDER BOOK -> PRO-RATE, AND SAY SO.
            # `demand_2026-08.parquet` puts ALL 528,165 tyres on day 31 -- it is
            # an order book, not a phased schedule. The `day <= N` filter then
            # returns ZERO rows and the success message below prints
            # "+3 d (0 tyres) appended", which is a no-op reported as a win.
            # That is why this flag has never done anything.
            # If the file carries a single distinct day, treat it as a monthly
            # total and take N days' worth pro-rata. Never silent: the print
            # names which branch ran.
            _prorata = False
            if "day" in nd.columns:
                if nd["day"].n_unique() <= 1:
                    _ny, _nm = int(nxt[:4]), int(nxt[5:7])
                    _nd_days = _cal.monthrange(_ny, _nm)[1]
                    if os.environ.get("PLANNER_LOOKAHEAD_PRORATA", "0") == "1":
                        _k = a.lookahead_days / float(_nd_days)
                        nd = nd.with_columns(
                            (pl.col("qty") * _k).round(0).alias("qty")).filter(
                            pl.col("qty") > 0)
                        _prorata = True
                    else:
                        raise SystemExit(
                            f"!! {nf.name} is an UNPHASED order book (all "
                            f"{int(nd['qty'].sum()):,} tyres on one day). The "
                            f"lookahead filter would return 0 rows and report "
                            f"success. Either phase it by day, or set "
                            f"PLANNER_LOOKAHEAD_PRORATA=1 to take "
                            f"{a.lookahead_days}/{_nd_days} of the month.")
                else:
                    nd = nd.filter(pl.col("day") <= a.lookahead_days)
            # Align dtypes before the concat: the two months' parquets carry
            # `day`/`qty` at different widths (Int8 vs Int64) and diagonal concat
            # will not coerce them.
            for _c, _t in (("day", pl.Int64), ("qty", pl.Float64)):
                if _c in nd.columns:
                    nd = nd.with_columns(pl.col(_c).cast(_t))
                if _c in dem.columns:
                    dem = dem.with_columns(pl.col(_c).cast(_t))
            la_qty = int(nd["qty"].sum())
            if _prorata:
                print(f"  lookahead: {nf.name} is unphased -- PRO-RATED "
                      f"{a.lookahead_days}/{_nd_days} of the month")
            dem = pl.concat([dem.with_columns(pl.lit(False).alias("_la")),
                             nd.with_columns(pl.lit(True).alias("_la"))],
                            how="diagonal")
            print(f"  lookahead: +{a.lookahead_days} d of {nxt} "
                  f"({la_qty:,} tyres) appended to pull end-of-month build")
        else:
            # CLEAN NO-OP, never an error -- see the flag's docstring.
            print(f"  lookahead: {nf.name} does not exist "
                  f"(demand ends with MES) -- running without it")
    if "_la" not in dem.columns:
        dem = dem.with_columns(pl.lit(False).alias("_la"))
    d = (dem.group_by(["plant", "gt_code"])
         .agg(pl.col("qty").sum().alias("demand"),
              pl.col("sku").n_unique().alias("skus"),
              # A GT is lookahead-only if NONE of its rows belong to month M --
              # those must be excluded when scoring M.
              (~pl.col("_la")).sum().alias("_in_month"),
              # LOOKAHEAD IS A QUANTITY, NOT A GT PROPERTY.
              # Tagging whole GTs (`_in_month == 0`) only catches GTs demanded
              # SOLELY next month. A GT demanded in BOTH months keeps its
              # next-month quantity inside `demand`, which silently inflates
              # THIS month's denominator -- measured 2026-08-14: arming a 3-day
              # lookahead read July PCR 96.2 % -> 89.8 % with 9,456 MORE tyres
              # cured. The percentage fell because the denominator grew, not
              # because the plan got worse.
              pl.col("qty").filter(pl.col("_la")).sum().alias("demand_la"))
         .with_columns((pl.col("_in_month") == 0).alias("lookahead"),
                       pl.col("demand_la").fill_null(0.0))
         .drop("_in_month")
         .sort(["plant", "gt_code"]))
    use, exp = usable_opening(a.month)

    r = (d.join(use, on=["plant", "gt_code"], how="left")
         .with_columns(pl.col("usable").fill_null(0),
                       pl.lit(0.0).alias("fg_stock")))       # R1 hook
    # OPENING GT NETS OFF THE BUILD, NEVER OFF THE CURE.
    # An opening green tyre is a tyre AWAITING CURE -- it is upstream of the
    # press, not downstream of it. Subtracting it from `net_cure` said the press
    # need not run for those tyres, which is the opposite of what the inventory
    # is. The plan then double-counted it: L4 removed 6,117 tyres from the cure
    # requirement, and L7 read the SAME opening_gt file and fed 5,256 of them to
    # presses as supply -- once as a demand reduction, once as a delivery. Only
    # finished-goods stock nets off the cure; green stock nets off the build.
    r = r.with_columns(pl.min_horizontal("usable", "demand").alias("from_stock"))
    r = r.with_columns(
        (pl.col("demand") - pl.col("fg_stock"))
        .clip(lower_bound=0).alias("net_cure"))
    r = r.with_columns(pl.col("plant").replace_strict(yld).alias("cure_yield"))
    # TWO QUANTITIES, NOT ONE. THEY ARE NOT INTERCHANGEABLE.
    #
    #   cure_requirement  green tyres the PRESSES must consume        = ceil(net_cure / yield)
    #   gross_build       green tyres BUILDING must make      = cure_requirement - from_stock
    #
    # Opening green stock changes WHO SUPPLIES a press cycle. It cannot change
    # HOW MANY press cycles exist: to deliver `net_cure` good tyres the presses
    # run `net_cure / yield` cycles whether the green tyre was built this month
    # or was already on the floor. So it is subtracted from the BUILD quantity
    # and from nothing else -- which is exactly what MEMORY.md records as the
    # invariant from the last time this was got wrong: "It reduces what must be
    # BUILT, never what must be CURED."
    #
    # `cure_requirement` DID NOT EXIST until 2026-08-18, so every cure consumer
    # had to reach for `gross_build`, and L4.5 sized the CURE LOT from it
    # (l45_lotsize.py `need = x["gross_build"]`). The fix above was therefore
    # in place while its own invariant was violated one layer down. Measured
    # cost -- cure cycles never planned, equal to `from_stock` to the tyre in
    # all four cells:
    #
    #        cure_requirement   gross_build   never planned   from_stock
    #   Jul PCR    398,459        393,639         4,820          4,820
    #   Jul TBR     99,242         97,991         1,251          1,251
    #   Aug PCR    427,949        423,473         4,476          4,476
    #   Aug TBR    100,567         99,541         1,026          1,026
    #
    # That capped fulfilment below 100 % before any scheduling decision was
    # taken: PCR 98.78 % (Jul) / 96.49 % (Aug), TBR 98.26 % / 98.08 %.
    #
    # RULE FOR CONSUMERS: if the quantity is fed to a press, compared against
    # something fed to a press, or divided into press cycles, it is
    # `cure_requirement`. If it is what a building machine must produce, it is
    # `gross_build`. `qty_fed` counts opening stock, so anything divided by it
    # is the former.
    r = r.with_columns(
        (pl.col("net_cure") / pl.col("cure_yield")).ceil().cast(pl.Int64)
        .alias("cure_requirement"))
    r = r.with_columns(
        (pl.col("cure_requirement") - pl.col("from_stock"))
        .clip(lower_bound=0).cast(pl.Int64).alias("gross_build"))
    # Lookahead share of each, so L11 can SUBTRACT it from whichever denominator
    # it is using rather than dropping whole GT rows.
    _la_frac = pl.col("demand_la") / pl.col("demand").clip(lower_bound=1e-9)
    r = r.with_columns(
        (pl.col("gross_build") * _la_frac).alias("gross_build_la"),
        (pl.col("cure_requirement") * _la_frac).alias("cure_requirement_la"))
    # B12: below the floor it is not worth a setup -- route, never drop
    r = r.with_columns(
        (pl.col("demand") < pl.col("plant").replace_strict(min_dem))
        .alias("residual"))

    # F10: the two demand generators disagree on dtype (`make_demand` writes qty
    # Float64, `ingest_orderbook_demand` Int64), which propagates here and makes
    # `pl.concat` of two months raise. Pin the numeric columns so the artefact has
    # one schema whatever produced its input.
    r = r.with_columns([pl.col(c).cast(pl.Float64) for c in
                        ("demand", "demand_la", "usable", "fg_stock",
                         "from_stock", "net_cure", "cure_yield",
                         "gross_build_la", "cure_requirement_la")
                        if c in r.columns]
                       + [pl.col(c).cast(pl.Int64) for c in
                          ("gross_build", "cure_requirement") if c in r.columns])

    out = D / f"net_requirement_{a.month}.parquet"
    r.write_parquet(out)
    # `lookahead_days` is a CLI ARG, not an env flag, so preflight cannot
    # reconstruct it -- hashing it would make the stamp permanently mismatch.
    # It is recorded for the reader but excluded from the hash; the env flag
    # PLANNER_LOOKAHEAD_PRORATA (which any real lookahead run must set) is what
    # the gate actually keys on. A lookahead run WITHOUT that flag raises in the
    # unphased-order-book branch above, so the hole is closed on that side.
    _stamp.write(out)

    print("=" * 92)
    print(f"L4  NET CURE REQUIREMENT  --  {a.month}   (R1, R5, R15, B12)")
    print("=" * 92)
    print(f"  shelf life {GT_SHELF_LIFE_H:.0f} h (hardcoded) · yields from "
          f"{pj[-1].name} · FG not netted (demand is production-derived)\n")
    print(f"  {'plant':<6}{'GTs':>5}{'demand':>10}{'from stock':>12}"
          f"{'net cure':>10}{'yield':>8}{'gross build':>13}{'plannable':>12}")
    for p in ["PCR", "TBR"]:
        s = r.filter(pl.col("plant") == p)
        if not s.height:
            continue
        pl_ = s.filter(~pl.col("residual"))
        print(f"  {p:<6}{s.height:>5}{int(s['demand'].sum()):>10,}"
              f"{int(s['from_stock'].sum()):>12,}{int(s['net_cure'].sum()):>10,}"
              f"{yld.get(p, 1.0):>8.4f}{int(s['gross_build'].sum()):>13,}"
              f"{int(pl_['gross_build'].sum()):>12,}")
    tot_d, tot_n = int(r["demand"].sum()), int(r["net_cure"].sum())
    print(f"  {'TOTAL':<6}{r.height:>5}{tot_d:>10,}"
          f"{int(r['from_stock'].sum()):>12,}{tot_n:>10,}{'':>8}"
          f"{int(r['gross_build'].sum()):>13,}"
          f"{int(r.filter(~pl.col('residual'))['gross_build'].sum()):>12,}")

    # Green stock offsets BUILD, so measure it against build -- against demand it
    # now reads 0.0% and looks like the stock vanished.
    _gb = int(r["gross_build"].sum())
    _fs = int(r["from_stock"].sum())
    print(f"\n  opening GT covers {100*_fs/max(_gb+_fs,1):.1f}% of the build "
          f"({_fs:,} of {_gb+_fs:,}); it is cured like any other tyre")
    res = r.filter(pl.col("residual"))
    if res.height:
        print(f"  B12 residual (below min_demand): {res.height} GTs, "
              f"{int(res['demand'].sum()):,} tyres -> L4.5 residual policy")
        for p in ["PCR", "TBR"]:
            s = res.filter(pl.col("plant") == p)
            if s.height:
                print(f"      {p}: {s.height} GTs under {min_dem[p]} "
                      f"({int(s['demand'].sum()):,} tyres)")
    print(f"  expired at epoch (>{GT_SHELF_LIFE_H:.0f} h): "
          f"{int(exp['expired'].sum()) if exp.height else 0} tyres")
    # SURPLUS IS TWO DIFFERENT THINGS AND ONLY ONE OF THEM WAS COUNTED.
    # `r` is a LEFT JOIN FROM DEMAND, so a GT with stock and no demand at all has
    # no row in it and could never appear in a line explicitly labelled
    # "(no demand)". August 2026: the line printed 27 tyres across 1 GT while
    # 672 tyres across 9 GTs (PCR 432 / TBR 240) sat on the floor for GTs nobody
    # ordered -- physical stock invisible to this layer, and L7 will find no
    # campaign for it. July printed 0 and 0, which is why it never showed.
    surplus = r.filter(pl.col("usable") > pl.col("from_stock"))
    if surplus.height:
        print(f"  demanded GTs with stock above demand: "
              f"{int((surplus['usable'] - surplus['from_stock']).sum()):,} tyres "
              f"across {surplus.height} GTs")
    _demanded = set(zip(r["plant"].to_list(), r["gt_code"].to_list()))
    _nod = use.filter(~pl.struct(["plant", "gt_code"]).map_elements(
        lambda x: (x["plant"], x["gt_code"]) in _demanded,
        return_dtype=pl.Boolean)) if use.height else use
    if _nod.height:
        _by = _nod.group_by("plant").agg(pl.col("usable").sum())
        print(f"  opening stock with NO DEMAND AT ALL: "
              f"{int(_nod['usable'].sum()):,} tyres across {_nod.height} GTs ("
              + ", ".join(f"{x['plant']} {int(x['usable']):,}"
                          for x in _by.iter_rows(named=True)) + ")")

    # ---- reconcile against the L3 ceiling -------------------------------
    cf = D / f"l3_ceiling_{a.month}.parquet"
    if cf.exists():
        c = pl.read_parquet(cf)
        # THE CEILING ITSELF IS OVERSTATED, and this layer cannot fix it.
        # `_offline/l3_ceiling.py` sums each press's OWN p90 day. The chance of
        # all 86 PCR presses hitting their 90th-percentile day at once is
        # essentially nil -- a fleet p90 is far tighter than the sum of
        # per-press p90s (per-press p90/p50 = 1.176). Rebuilding it needs the
        # raw MES. Read `load` as an OPTIMISTIC bound: at 98 % the month is
        # probably already over capacity.
        print("\n  vs L3 ceiling   (ceiling = SUM of per-press p90 -- optimistic)")
        print(f"  {'plant':<6}{'plannable':>12}{'capacity/mo':>14}{'load':>8}")
        for x in c.iter_rows(named=True):
            # A PRESS ceiling takes the PRESS quantity. `gross_build` is what
            # building must make; the presses must run `cure_requirement` cycles
            # whether the green tyre is new or came off the floor. Same F1 class
            # as the L4.5 lot sizing, one report down.
            _sub = r.filter((pl.col("plant") == x["plant"]) & ~pl.col("residual"))
            need = float(_sub[("cure_requirement" if "cure_requirement"
                               in _sub.columns else "gross_build")].sum())
            # F3: 744 was hardcoded, so a 30-day month read as 31 and a 28-day
            # month as 31 -- June PCR capacity was overstated by 14,073 tyres
            # (3.3 %), February would be 10.7 % out. `l5_cure_master.py` derives
            # the same conversion correctly from the calendar: one constant, two
            # derivations, one of them month-blind. Fourth site of this class.
            cap = x["max_feasible"] * (_cal.monthrange(
                int(a.month[:4]), int(a.month[5:7]))[1] * 24) / 168.0
            print(f"  {x['plant']:<6}{need:>12,.0f}{cap:>14,.0f}"
                  f"{100*need/max(cap,1):>7.1f}%")
    print(f"\n  -> {out.name}")


if __name__ == "__main__":
    main()
