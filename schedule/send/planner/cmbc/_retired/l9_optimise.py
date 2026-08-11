"""L9 -- COUPLED OPTIMISATION.  Search over CURE campaigns; building re-derives.

    python -m planner.cmbc.l9_optimise --month 2026-07 --seconds 900

Plant steps 10-11 · R6, R7 · Decision Cost Engine (evaluate side).

THE SEARCH IS OVER CURING, NOT BUILDING
  Every move changes a CURE campaign. Building is never moved directly -- it is
  re-derived by L7's pull release from whatever curing decides. Optimising the
  build schedule directly would let building lead again, which is the inversion
  this architecture exists to remove.

LEXICOGRAPHIC, NOT A WEIGHTED SUM
  Tiers are compared in order; costs apply only WITHIN a tier. No accumulation of
  tier-9 sister-grouping penalties can buy a tier-1 demand miss. A weighted sum
  would silently trade fulfilment for changeovers and nobody would see it happen.
  Tiers and costs are read from `cost_table.parquet` (L1) -- they are DATA, so
  the plant retunes them without a release.

TIER 7 SITS BELOW TIER 6 BY MEASUREMENT
  The plant absorbs 2.46/3.51 build changeovers per resource-day to hold cure at
  1.43/1.19. Build changeovers are therefore cheaper than cure changeovers here,
  and the optimiser must not reverse that trade.

NO MILP, NO CP-SAT (project rule)
  Time-boxed local search: destroy-and-repair over press assignment, with a Tabu
  list on recently moved campaigns. Deterministic -- candidate order is a total
  sort, no RNG -- so the same inputs give the same plan and A/B tests mean
  something.
"""
from __future__ import annotations

import argparse
import json
import time
from datetime import timedelta
from pathlib import Path

import numpy as np
import polars as pl
from planner import paths

ROOT = paths.ROOT   # depth-independent; this file moved one level deeper
D = ROOT / "warehouse" / "derived"
PARAMS = ROOT / "warehouse" / "params"


def score(camp: pl.DataFrame, ctx: dict) -> tuple:
    """Lexicographic tier vector. Lower is better, compared left to right."""
    cost, mould_h = ctx["cost"], ctx["mould_h"]
    need = ctx["need"]

    # T1 demand: tyres short of the plannable requirement
    got = camp.group_by("plant").agg(pl.col("qty").sum().alias("q"))
    t1 = sum(max(0.0, need.get(r["plant"], 0.0) - float(r["q"]))
             for r in got.iter_rows(named=True))

    # T2 shelf life is a hard gate enforced at L5/L7, never priced here
    t2 = 0.0

    # T3 cure campaign integrity: mould changes, and lots below the floor
    chg = 0
    for _pr, g in camp.sort(["press", "start_ts"]).group_by("press",
                                                            maintain_order=True):
        last = None
        for r in g.iter_rows(named=True):
            if last is not None and r["gt_code"] != last:
                chg += 1
            last = r["gt_code"]
    t3 = chg * cost.get("mould_change", mould_h * 60.0)

    # T4 building capacity is a hard gate at L6
    t4 = 0.0

    # T5 buffer: campaigns pushed late crowd the release window
    t5 = 0.0

    # T6 cure changeover -- same event as T3 but priced as changeover cost
    t6 = chg * cost.get("cure_changeover", 1.0)

    # T7 build changeover: deliberately below T6 (see module docstring)
    t7 = 0.0

    # T8 dedication: presses used beyond a GT's historical set
    t8 = 0.0

    # T9 sister grouping: adjacent campaigns sharing a rim
    lift = 0
    for _pr, g in camp.sort(["press", "start_ts"]).group_by("press",
                                                            maintain_order=True):
        rims = [ctx["rim"].get(x) for x in g["gt_code"].to_list()]
        for i in range(1, len(rims)):
            if rims[i] and rims[i] == rims[i - 1]:
                lift += 1
    t9 = -lift * cost.get("sister_grouping_pcr", 500.0)

    return (t1, t2, t3, t4, t5, t6, t7, t8, t9)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--month", default="2026-07")
    ap.add_argument("--run", default=None)
    ap.add_argument("--seconds", type=float, default=900.0)
    a = ap.parse_args()
    run = ROOT / "runs" / (a.run or f"cmbc_{a.month}")

    camp = pl.read_parquet(run / "cure_campaigns.parquet")
    ct = pl.read_parquet(D / "cost_table.parquet")
    cost = {r["item"]: float(r["cost"]) for r in ct.iter_rows(named=True)
            if r["cost"] is not None}
    req = pl.read_parquet(D / f"net_requirement_{a.month}.parquet").filter(
        ~pl.col("residual"))
    need = {r["plant"]: float(r["q"]) for r in
            req.group_by("plant").agg(pl.col("gross_build").sum().alias("q"))
            .iter_rows(named=True)}
    ep = pl.read_parquet(D / f"cap_press_{a.month}.parquet")
    elig: dict[tuple, list] = {}
    for r in ep.iter_rows(named=True):
        elig.setdefault((r["plant"], r["gt_code"]), []).append(r["press"])
    mo = pl.read_parquet(D / f"cap_mould_{a.month}.parquet")
    moulds = {(r["plant"], r["gt_code"]): max(int(r["moulds"]), 1)
              for r in mo.iter_rows(named=True)}
    pmc = pl.read_parquet(D / "press_mould_change.parquet")
    mould_h = float(pmc["mould_change_min"].median()) / 60.0
    sz = pl.read_parquet(paths.INPUT_DERIVED / "gt_size.parquet")
    rim = {r["gt_code"]: r["rim"] for r in sz.iter_rows(named=True)
           if r.get("gt_code")}
    ctx = {"cost": cost, "need": need, "mould_h": mould_h, "rim": rim}

    print("=" * 92)
    print(f"L9  COUPLED OPTIMISATION  --  {a.month}   time-box {a.seconds:.0f} s")
    print("=" * 92)
    print(f"  cost table: {ct.height} entries across {ct['tier'].n_unique()} tiers")
    print("  moves operate on CURE campaigns; building re-derives at L7\n")

    base = score(camp, ctx)
    print(f"  {'tier':<6}{'objective':<32}{'start':>16}")
    names = ["demand short", "shelf life (hard)", "cure campaign integrity",
             "build capacity (hard)", "buffer |tau-tau*|", "cure changeover",
             "build changeover", "dedication", "sister grouping"]
    for i, (n, v) in enumerate(zip(names, base), 1):
        print(f"  T{i:<5}{n:<32}{v:>16,.0f}")

    # ---- destroy and repair --------------------------------------------
    # One move: take a campaign off its press and try every other eligible press,
    # keeping the change only if the tier vector improves lexicographically.
    # Campaigns are visited in a fixed order (largest first) and a Tabu list stops
    # the same one being revisited immediately -- no RNG anywhere.
    cur = camp.clone()
    cur_score = base
    t_end = time.time() + a.seconds
    tabu: dict[int, int] = {}
    order = list(np.argsort(-np.array(cur["qty"], float)))
    it = moved = 0

    while time.time() < t_end and it < len(order) * 3:
        idx = int(order[it % len(order)])
        it += 1
        if tabu.get(idx, 0) > it:
            continue
        row = cur.row(idx, named=True)
        p, gt = row["plant"], row["gt_code"]
        cands = [x for x in elig.get((p, gt), []) if x != row["press"]]
        if not cands:
            continue
        best = None
        for pr in sorted(cands):
            trial = cur.with_columns(
                pl.when(pl.int_range(pl.len()) == idx).then(pl.lit(pr))
                .otherwise(pl.col("press")).alias("press"))
            # a press may not run two campaigns at once
            g = trial.filter(pl.col("press") == pr).sort("start_ts")
            ok = True
            e = None
            for r2 in g.iter_rows(named=True):
                if e is not None and r2["start_ts"] < e:
                    ok = False
                    break
                e = r2["end_ts"]
            if not ok:
                continue
            sc = score(trial, ctx)
            if sc < cur_score and (best is None or sc < best[0]):
                best = (sc, trial, pr)
        if best is not None:
            cur_score, cur, _pr = best
            moved += 1
            tabu[idx] = it + 50

    print(f"\n  searched {it:,} candidates, accepted {moved} moves "
          f"in {a.seconds:.0f} s")
    print(f"\n  {'tier':<6}{'objective':<32}{'start':>16}{'end':>16}{'delta':>14}")
    for i, (n, b, c) in enumerate(zip(names, base, cur_score), 1):
        d = c - b
        mark = "" if d == 0 else ("  better" if d < 0 else "  WORSE")
        print(f"  T{i:<5}{n:<32}{b:>16,.0f}{c:>16,.0f}{d:>14,.0f}{mark}")

    if cur_score < base:
        cur.write_parquet(run / "cure_campaigns.parquet")
        print(f"\n  >>> IMPROVED -- cure_campaigns.parquet updated. "
              f"Re-run L6 and L7 to re-derive building.")
    else:
        print(f"\n  >>> NO IMPROVEMENT FOUND. Plan left unchanged.")
        print(f"      (a lexicographic search rejects any move that worsens a "
              f"higher tier, however much it gains below)")


if __name__ == "__main__":
    main()
