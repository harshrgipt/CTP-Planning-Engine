"""DERIVE THE END-OF-PREVIOUS-MONTH MACHINE STATE from opening GT timestamps.

    python -m scripts.derive_machine_warm --month 2026-07

WHAT THIS RECOVERS, AND WHY IT IS NOT AN ASSUMPTION
  The opening-GT file carries `built_ts` per tyre. The tyres built in the LAST
  HOUR before t0 must have come off machines that were running at that moment.
  Measured on July 2026:

      PCR  tyres built within 1 h of t0 -> 566 tyres on exactly 11 distinct GTs
      TBR  tyres built within 1 h of t0 -> 132 tyres on exactly 10 distinct GTs
           (PCR has 11 building machines, TBR has 9)

  One GT per machine. That is the 30-June machine state, and it was in the data
  all along -- it did not need to be requested from the plant.

WHY IT MATTERS
  At t0 the engine treats every machine as idle and unconfigured, so building can
  only reach ~9 GTs in the first two hours while 30 GTs' presses are seated. Which
  9 it picks is arbitrary. The plant has no such ramp because its machines were
  already threaded on these GTs.

  Marking them warm means their first slice releases at t0 + tau_min instead of
  waiting for a setup plus a full legal run. It claims NO pre-month production:
  those tyres already exist and are already counted in the opening stock. It only
  stops asserting that a machine which was running at 06:59 is cold at 07:00.

MACHINE ASSIGNMENT IS INFERRED, NOT OBSERVED
  The file gives GT and time, not machine. Each warm GT is mapped to its home /
  highest-share allowable machine, one GT per machine, largest first. The plant
  should confirm the mapping before this is trusted for execution -- the GT SET
  is measured, the GT->machine pairing is derived.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from planner import paths                                          # noqa: E402
from planner.cmbc import allowable                                 # noqa: E402

N_MACHINES = {"PCR": 11, "TBR": 9}


def run(month: str, window_h: float = 1.0, *, write: bool = True) -> pl.DataFrame:
    og = pl.read_parquet(paths.opening_gt(month))
    cm = allowable.restrict(
        pl.read_parquet(paths.wh_derived(f"cap_machine_{month}.parquet")), quiet=True)
    cm = allowable.restrict_rimlock(cm, quiet=True)
    share = {}
    f = paths.input_derived("machine_gt_share.parquet")
    if f.exists():
        for r in pl.read_parquet(f).iter_rows(named=True):
            share[(r["plant"], r["gt_code"], r["machine"])] = float(r["share_pct"])

    rows = []
    for plant, nmc in N_MACHINES.items():
        z = og.filter((pl.col("plant") == plant) & (pl.col("age_h") <= window_h))
        if not z.height:
            continue
        g = (z.group_by("gt_code").agg(pl.len().alias("tyres"))
              .sort("tyres", descending=True))
        elig = {}
        for r in cm.filter(pl.col("plant") == plant).iter_rows(named=True):
            elig.setdefault(r["gt_code"], []).append(r["machine"])
        taken: set = set()
        for r in g.iter_rows(named=True):
            gt = r["gt_code"]
            cands = [m for m in elig.get(gt, []) if m not in taken]
            if not cands:
                continue                      # every allowable machine already warm
            cands.sort(key=lambda m: (-share.get((plant, gt, m), 0.0), m))
            mc = cands[0]
            taken.add(mc)
            rows.append({"plant": plant, "machine": mc, "gt_code": gt,
                         "tyres_last_h": r["tyres"], "month": month})
            if len(taken) >= nmc:
                break
    df = pl.DataFrame(rows)
    print(f"\n  MACHINE WARM STATE  {month}  (window {window_h:.0f} h before t0)")
    print(f"  {'-' * 66}")
    for p, gp in df.group_by("plant"):
        print(f"    {p[0]}: {gp.height} of {N_MACHINES[p[0]]} machines warm")
        for r in gp.sort("tyres_last_h", descending=True).iter_rows(named=True):
            print(f"       {r['machine']:<16} {r['gt_code'][:32]:<34}"
                  f"{r['tyres_last_h']:>5} tyres in the last hour")
    if write:
        out = paths.wh_derived(f"machine_warm_{month}.parquet")
        df.write_parquet(out)
        print(f"  -> {out.name}")
    return df


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--month", required=True)
    ap.add_argument("--window-h", type=float, default=1.0)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    run(a.month, a.window_h, write=not a.dry_run)


if __name__ == "__main__":
    main()
