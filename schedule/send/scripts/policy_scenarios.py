"""WHAT EACH PLANT RULING IS WORTH -- measurement only, changes no default.

    python -m scripts.policy_scenarios --month 2026-07 --run jul_ship

Three of the largest remaining losses are not scheduling failures; they are rules
we enforce more strictly than the plant does. This prices each one from an
EXISTING run so the plant can decide with a number rather than an argument.

IT DOES NOT REPLAN AND IT DOES NOT RELAX ANY CAP.
  Every figure below is the same physical schedule, re-counted under a different
  rule. Nothing is rebuilt, no floor is lowered, no tyre is invented. A ruling
  that is accepted must then be applied in config and the month REPLANNED -- the
  scenario number is an upper bound on what replanning would show, not a result.

WHY IT IS SEPARATED FROM THE ENGINE
  Reporting a higher number because the rule changed, while presenting it as a
  planning improvement, is the failure mode this project's ledger exists to
  prevent. Keeping the scenarios in a report that cannot alter a default makes
  the distinction structural rather than a matter of discipline.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from planner import paths                                          # noqa: E402
from planner.config import GT_SHELF_LIFE_H                         # noqa: E402


def run(month: str, runid: str) -> None:
    req = pl.read_parquet(paths.wh_derived(f"net_requirement_{month}.parquet"))
    ccr = pl.read_parquet(paths.RUNS / runid / "cure_campaigns_reconciled.parquet")
    co = pl.read_parquet(paths.RUNS / runid / "carry_out.parquet")
    st = pl.read_parquet(paths.RUNS / runid / "build_starved.parquet")
    cc = pl.read_parquet(paths.RUNS / runid / "cure_campaigns.parquet")
    u = ccr.filter(pl.col("qty_unfed") > 0)

    print(f"\n  POLICY SCENARIOS  {month}  (from runs/{runid} -- NOT replanned)")
    print(f"  {'=' * 74}")
    for p in ("PCR", "TBR"):
        R = req.filter(pl.col("plant") == p)
        plan = R.filter(~pl.col("residual"))["demand"].sum()
        resid = R.filter(pl.col("residual"))["demand"].sum()
        total = plan + resid
        fed = ccr.filter(pl.col("plant") == p)["qty_fed_in_month"].sum()
        tail = co.filter(pl.col("plant") == p)["qty"].sum()

        # sub-floor tails: same-GT remainders whose seats fit ONE R5 window can be
        # pooled into a run that CLEARS the floor -- no cap breached.
        ml = st.filter((pl.col("plant") == p)
                       & pl.col("reason").str.contains("min_lot"))
        pool = 0.0
        for gt, g in ml.group_by("gt_code"):
            gt = gt[0] if isinstance(gt, tuple) else gt
            seats = u.filter((pl.col("plant") == p) & (pl.col("gt_code") == gt))
            if not seats.height:
                continue
            span = (seats["start_ts"].max()
                    - seats["start_ts"].min()).total_seconds() / 3600
            if span <= GT_SHELF_LIFE_H:
                pool += float(g["qty"].sum())
        blocked = float(ml["qty"].sum()) - pool

        base = 100 * fed / total
        print(f"\n  {p}   base {fed:>9,.0f} / {total:,.0f} = {base:5.2f} %")
        print(f"  {'-' * 72}")
        rows = [
            ("A  count the carry-out tail", tail,
             "DEFINITION. Tyres already built AND cured, 1-2 days past the "
             "boundary. No cap moves."),
            ("B  plan the B12 residual", resid,
             "CAP CHANGE. Lowers min_demand_units (PCR 300 / TBR 150). The plant "
             "makes these GTs."),
            ("C  pool sub-floor tails (R5-safe)", pool,
             "NO CAP CHANGE. Same-GT remainders whose seats fit one 72 h window "
             "pool into a run that CLEARS the floor."),
            ("D  allow genuinely sub-floor runs", blocked,
             "CAP CHANGE. Seats >72 h apart cannot pool; only a lower floor "
             "reaches these. Plant runs 12.7 % / 30.8 % sub-floor."),
        ]
        cum = fed
        for name, q, note in rows:
            cum += q
            print(f"    {name:<36}{q:>8,.0f} tyres  {100*q/total:>5.2f} pt "
                  f"-> {100*cum/total:5.2f} %")
            print(f"        {note}")
        print(f"    {'ALL FOUR':<36}{cum-fed:>8,.0f} tyres  "
              f"{100*(cum-fed)/total:>5.2f} pt -> {100*cum/total:5.2f} %")
    print(f"\n  {'=' * 74}")
    print("  A and C require no cap change. B and D DO -- they are plant rulings,")
    print("  and a ruling must be applied in config and the month REPLANNED before")
    print("  any of these figures may be quoted as achieved.\n")
    _ = cc


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--month", required=True)
    ap.add_argument("--run", required=True)
    a = ap.parse_args()
    run(a.month, a.run)


if __name__ == "__main__":
    main()
