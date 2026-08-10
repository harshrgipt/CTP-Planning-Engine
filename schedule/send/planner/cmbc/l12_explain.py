"""L12 -- EXPLAINABILITY · KPIs · GAP-TO-CEILING.

    python -m planner.cmbc.l12_explain --month 2026-07

Plant step 14.

EVERY PLAN REPORTS achieved / MAX_FEASIBLE WITH THE GAP ATTRIBUTED
  A fulfilment percentage on its own is not explainable -- it says how much was
  missed, not why, and a planner who cannot see why will override. Every override
  then corrupts L0's parameter estimates, because L0 learns from what the plant
  did, not from what was planned. So the gap is decomposed into NAMED causes and
  the residual is reported as residual rather than absorbed into a cause that
  happens to be nearby.

THE ATTRIBUTION MUST SUM
  Each press-hour between the ceiling and the achieved plan belongs to exactly
  one cause. If the named causes do not reconcile to the total, the unexplained
  remainder is printed as "unattributed" -- never silently distributed across the
  others, which is how a decomposition becomes a story.

CAUSES ARE NOT ALL OURS TO FIX
  A mould change is real work; the cold-start floor is a consequence of planning
  a month with no carry-in; residual demand below the economic lot is a business
  decision routed by B12. Separating those from packing loss is the point --
  they need different responses and only one of them is a scheduling problem.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import polars as pl

ROOT = Path(__file__).resolve().parent.parent.parent
D = ROOT / "warehouse" / "derived"
PARAMS = ROOT / "warehouse" / "params"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--month", default="2026-07")
    ap.add_argument("--run", default=None)
    a = ap.parse_args()
    run = ROOT / "runs" / (a.run or f"cmbc_{a.month}")

    P = json.loads(sorted(PARAMS.glob("params_*.json"))[-1].read_text())
    camp = pl.read_parquet(run / "cure_campaigns.parquet")
    bs = pl.read_parquet(run / "build_schedule.parquet")
    bsm = bs.filter(pl.col("machine") != "OPENING_STOCK")
    rec = pl.read_parquet(run / "cure_campaigns_reconciled.parquet")
    req = pl.read_parquet(D / f"net_requirement_{a.month}.parquet")
    ceil = pl.read_parquet(D / f"l3_ceiling_{a.month}.parquet")
    mc = pl.read_parquet(run / "mould_changes.parquet")
    unp = pl.read_parquet(run / "cure_unplaced.parquet")
    cav = pl.read_parquet(D / "l3_cavities.parquet")

    y, m = int(a.month[:4]), int(a.month[5:7])
    ndays = (datetime(y + (m == 12), (m % 12) + 1, 1) - datetime(y, m, 1)).days
    rate = {p: float(cav.filter(pl.col("plant") == p)["cavities"].median())
            * 3600.0 / float(cav.filter(pl.col("plant") == p)["cycle_s"].median())
            for p in ["PCR", "TBR"]}
    tau = {p: float(P["tau"][p]["tau_star_h"]) for p in ["PCR", "TBR"]}
    bband = {p: float(P["campaign_bands"][p]["build"]["hours_p50"])
             for p in ["PCR", "TBR"]}

    print("=" * 92)
    print(f"L12  EXPLAINABILITY · KPIs · GAP-TO-CEILING  --  {a.month}")
    print("=" * 92)

    # ---- KPIs -----------------------------------------------------------
    print("\n  KPIs")
    print(f"  {'':<28}{'PCR':>14}{'TBR':>14}{'plant PCR':>12}{'plant TBR':>12}")
    ref = {"head": (4.4, 4.8), "inv": (4772, 1743)}
    rows = []
    for p in ["PCR", "TBR"]:
        w = np.array(bs.filter(pl.col("plant") == p)["wait_h"], float)
        e = (pl.read_parquet(run / "gt_events.parquet")
             .filter(pl.col("plant") == p).sort("ts")
             .with_columns(pl.col("d").cum_sum().alias("bal")))
        rows.append({"p": p, "head": float(np.median(w)),
                     "inv": float(e["bal"].mean()),
                     "fed": float(rec.filter(pl.col("plant") == p)["qty_fed"].sum()),
                     "need": float(req.filter((pl.col("plant") == p)
                                              & ~pl.col("residual"))["gross_build"].sum())})
    k = {r["p"]: r for r in rows}
    print(f"  {'GT head p50 (h)':<28}{k['PCR']['head']:>14.2f}{k['TBR']['head']:>14.2f}"
          f"{ref['head'][0]:>12.1f}{ref['head'][1]:>12.1f}")
    print(f"  {'GT inventory (mean)':<28}{k['PCR']['inv']:>14,.0f}{k['TBR']['inv']:>14,.0f}"
          f"{ref['inv'][0]:>12,}{ref['inv'][1]:>12,}")
    print(f"  {'fulfilment':<28}"
          f"{100*k['PCR']['fed']/k['PCR']['need']:>13.1f}%"
          f"{100*k['TBR']['fed']/k['TBR']['need']:>13.1f}%")

    # ---- gap to ceiling, attributed -------------------------------------
    print("\n  GAP TO CEILING  (press-hours, each hour attributed to one cause)")
    out = []
    for p in ["PCR", "TBR"]:
        c = ceil.filter(pl.col("plant") == p).row(0, named=True)
        npress = camp.filter(pl.col("plant") == p)["press"].n_unique()
        total_h = npress * 24.0 * ndays
        used_h = float(camp.filter(pl.col("plant") == p)["hours"].sum())
        mould_h = float(mc.filter(pl.col("plant") == p)["minutes"].sum()) / 60.0
        # cold-start floor: no press may cure before t0 + tau* + build band
        cold_h = npress * (tau[p] + bband[p])
        # end-of-horizon tail: the last campaign on each press rarely ends at 744h
        g = camp.filter(pl.col("plant") == p).group_by("press").agg(
            pl.col("end_ts").max().alias("last"))
        t_end = datetime(y, m, 1, 7, 0) + timedelta(days=ndays)
        tail_h = float(sum((t_end - x).total_seconds() / 3600.0
                           for x in g["last"].to_list()))
        idle_h = total_h - used_h - mould_h - cold_h - tail_h
        out.append({"plant": p, "capacity_h": total_h, "productive_h": used_h,
                    "mould_change_h": mould_h, "cold_start_h": cold_h,
                    "horizon_tail_h": tail_h, "unattributed_h": idle_h})
    gp = pl.DataFrame(out)
    gp.write_parquet(run / "l12_gap.parquet")
    print(f"  {'plant':<6}{'capacity':>11}{'productive':>12}{'mould chg':>11}"
          f"{'cold start':>12}{'horizon tail':>14}{'unattributed':>14}")
    for r in gp.iter_rows(named=True):
        print(f"  {r['plant']:<6}{r['capacity_h']:>11,.0f}{r['productive_h']:>12,.0f}"
              f"{r['mould_change_h']:>11,.0f}{r['cold_start_h']:>12,.0f}"
              f"{r['horizon_tail_h']:>14,.0f}{r['unattributed_h']:>14,.0f}")
    for r in gp.iter_rows(named=True):
        t = r["capacity_h"]
        print(f"    {r['plant']}: productive {100*r['productive_h']/t:.1f}%  "
              f"mould {100*r['mould_change_h']/t:.1f}%  "
              f"cold start {100*r['cold_start_h']/t:.1f}%  "
              f"tail {100*r['horizon_tail_h']/t:.1f}%  "
              f"unattributed {100*r['unattributed_h']/t:.1f}%")

    # ---- tyres missed, attributed ---------------------------------------
    print("\n  DEMAND NOT MET  (tyres, by named cause)")
    resid = req.filter(pl.col("residual"))
    unfed = float(rec["qty_unfed"].sum())
    unpl = float(unp["qty"].sum()) if unp.height else 0.0
    tot_need = float(req.filter(~pl.col("residual"))["gross_build"].sum())
    tot_fed = float(rec["qty_fed"].sum())
    miss = tot_need - tot_fed
    print(f"    {'plannable requirement':<44}{tot_need:>12,.0f}")
    print(f"    {'delivered':<44}{tot_fed:>12,.0f}")
    print(f"    {'MISSED':<44}{miss:>12,.0f}   ({100*miss/tot_need:.1f}%)")
    print(f"      {'cure placed but not fed by building':<42}{unfed:>12,.0f}")
    print(f"      {'cure that would not fit the horizon':<42}{unpl:>12,.0f}")
    print(f"      {'unattributed':<42}{miss-unfed-unpl:>12,.0f}")
    print(f"    {'below min_demand -> B12 residual policy':<44}"
          f"{float(resid['demand'].sum()):>12,.0f}   (excluded from requirement)")

    # ---- decisions carry reasons ----------------------------------------
    lots = pl.read_parquet(D / f"l45_lots_{a.month}.parquet")
    cm = pl.read_parquet(D / f"cap_machine_{a.month}.parquet")
    print("\n  DECISION PROVENANCE  (why each choice was made)")
    print("    lot sizing:")
    for r in (lots.group_by("policy").agg(pl.len().alias("n"))
              .sort("n", descending=True).head(4).iter_rows(named=True)):
        print(f"      {str(r['policy'])[:56]:<56}{r['n']:>6} GTs")
    print("    machine eligibility basis:")
    for r in (cm.group_by(["plant", "basis"]).agg(pl.len().alias("n"))
              .sort("n", descending=True).head(6).iter_rows(named=True)):
        print(f"      {r['plant']} {r['basis']:<52}{r['n']:>6} pairs")
    print(f"\n  -> {run.name}/l12_gap.parquet")


if __name__ == "__main__":
    main()
