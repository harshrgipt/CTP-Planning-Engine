"""L5 <-> L6 -- the correction loop.  Curing reseats until building can feed it.

    python -m planner.cmbc.l56_loop --month 2026-08 --run aug_loop --iters 4

WHAT WAS WRONG
  The old flow was one-way:

      L5 seats every cure campaign  ->  L6 reports which ones building cannot
      feed  ->  L7 tries to repair downstream, slice by slice

  L6's verdict went nowhere -- L7 never read `l6_infeasible.parquet`. So a cure
  seat that building could never reach stayed exactly where L5 put it, and the
  shortfall surfaced as `no feasible release` at the end: 33,161 tyres on August
  PCR, 18,900 on July. Building was made to chase a time it did not choose.

WHAT THIS DOES
      L5 (seat)  ->  L7 (probe: which campaigns went unfed, and by how much)
                 ->  delay hint per GT  ->  L5 (reseat later)  ->  repeat

  L7 IS THE FEASIBILITY ORACLE, not a separate model. It is the only component
  that knows whether a machine slot exists at the hour a seat needs one, so
  asking it is strictly better than re-deriving the answer with an approximation
  that could disagree. Each iteration is a full L5 -> L7 pass (~9 s).

THE DELAY IS BOUNDED AND MONOTONE
  A GT that went unfed is pushed later by `--step` hours, cumulatively, capped at
  `--max-delay`. Monotone because the hint only ever grows: an oscillating hint
  would let the loop cycle forever between two equally infeasible seatings.

  Delaying a cure seat is the RIGHT direction: it gives building more of the
  horizon to reach it, and R5 protects the tyre (72 h of shelf life) whereas a
  starved press-hour is gone for good. It is bounded because a seat pushed past
  the horizon stops being this month's fulfilment at all.

ACCEPTANCE -- KEEP THE BEST, NOT THE LAST
  Every iteration is scored on in-month fed volume and the best is kept. A loop
  that returns its final iteration can end worse than it started; this one
  cannot. The chosen iteration's artefacts are what remain in the run directory.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from planner import paths                                          # noqa: E402

PY = sys.executable


def _run(mod: str, month: str, run: str, flag: str) -> str:
    cp = subprocess.run(
        [PY, "-m", f"planner.cmbc.{mod}", "--month", month, flag, run],
        cwd=ROOT, env={**os.environ, "PYTHONIOENCODING": "utf-8",
                       "PYTHONPATH": str(ROOT)},
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    if cp.returncode != 0:
        sys.stderr.write(cp.stdout[-4000:] + "\n" + cp.stderr[-3000:] + "\n")
        raise SystemExit(f"!! {mod} failed ({cp.returncode})")
    return cp.stdout


def _score(run: str) -> tuple[float, pl.DataFrame]:
    cc = pl.read_parquet(paths.RUNS / run / "cure_campaigns_reconciled.parquet")
    return float(cc["qty_fed_in_month"].sum()), cc


def main() -> None:
    ap = argparse.ArgumentParser(description="L5<->L6 cure reseat loop")
    ap.add_argument("--month", default="2026-08")
    ap.add_argument("--run", required=True)
    ap.add_argument("--iters", type=int, default=4)
    ap.add_argument("--mode", choices=["split", "delay"], default="split",
                    help="split (default) divides unfed campaigns; delay pushes "
                         "them later and was MEASURED net-negative (-16,548 tyres)")
    ap.add_argument("--step", type=float, default=12.0,
                    help="delay mode only: hours to push an unfed GT each iter")
    ap.add_argument("--max-delay", type=float, default=96.0)
    ap.add_argument("--max-factor", type=int, default=4,
                    help="split mode: largest divisor tried per GT")
    a = ap.parse_args()

    dfile = paths.wh_derived(f"l56_delay_{a.month}.parquet")
    sfile = paths.wh_derived(f"l56_split_{a.month}.parquet")
    dfile.unlink(missing_ok=True)              # always start from no hint
    sfile.unlink(missing_ok=True)
    delay: dict[tuple, float] = {}
    split: dict[tuple, int] = {}
    best = (-1.0, -1, None)

    print(f"\n  L5<->L6 LOOP  {a.month}  run={a.run}  "
          f"step={a.step:.0f} h  cap={a.max_delay:.0f} h")
    print(f"  {'-' * 74}")
    print(f"  {'iter':>4}{'delayed GTs':>13}{'fed in-month':>15}"
          f"{'unfed':>11}{'campaigns unfed':>17}")

    for it in range(a.iters + 1):
        _run("l5_cure_master", a.month, a.run, "--out")
        _run("l7_pull_release", a.month, a.run, "--run")
        fed, cc = _score(a.run)
        unfed = cc.filter(pl.col("qty_unfed") > 0)
        hint = len(split) if a.mode == "split" else len(delay)
        print(f"  {it:>4}{hint:>13}{fed:>15,.0f}"
              f"{cc['qty_unfed'].sum():>11,.0f}{unfed.height:>17}")

        if fed > best[0]:
            snap = paths.RUNS / f"{a.run}__best"
            if snap.exists():
                shutil.rmtree(snap)
            shutil.copytree(paths.RUNS / a.run, snap)
            best = (fed, it, dict(split) if a.mode == "split" else dict(delay))

        if it == a.iters or unfed.height == 0:
            break

        # ---- L6's request ------------------------------------------------
        grew = 0
        rows = (unfed.group_by(["plant", "gt_code"])
                     .agg(pl.col("qty_unfed").sum()).iter_rows(named=True))
        if a.mode == "split":
            # Divide unfed campaigns so each piece needs a SHORTER contiguous
            # build window. L5 clamps the factor per GT so no piece drops below
            # the cure-lot floor -- the B12 cap is never traded for fulfilment.
            for r in rows:
                k = (p_g := (r["plant"], r["gt_code"]))
                nf = min(split.get(k, 1) + 1, a.max_factor)
                if nf > split.get(k, 1):
                    split[k] = nf
                    grew += 1
                _ = p_g
            if not grew:
                print("       every unfed GT is already at the split cap -- "
                      "the remainder is not fixable by splitting")
                break
            pl.DataFrame([{"plant": p, "gt_code": g, "factor": f}
                          for (p, g), f in split.items()]).write_parquet(sfile)
        else:
            for r in rows:
                k = (r["plant"], r["gt_code"])
                nd = min(delay.get(k, 0.0) + a.step, a.max_delay)
                if nd > delay.get(k, 0.0):
                    delay[k] = nd
                    grew += 1
            if not grew:
                print("       every unfed GT is already at the delay cap -- "
                      "the remainder is explicitly infeasible, not mis-seated")
                break
            pl.DataFrame([{"plant": p, "gt_code": g, "delay_h": h}
                          for (p, g), h in delay.items()]).write_parquet(dfile)

    # ---- keep the BEST iteration, not the last -------------------------
    snap = paths.RUNS / f"{a.run}__best"
    if snap.exists():
        tgt = paths.RUNS / a.run
        shutil.rmtree(tgt)
        shutil.move(str(snap), str(tgt))
    dfile.unlink(missing_ok=True)
    sfile.unlink(missing_ok=True)
    print(f"  {'-' * 74}")
    print(f"  kept iteration {best[1]} · fed in-month {best[0]:,.0f} · "
          f"{len(best[2] or {})} GTs hinted ({a.mode})")
    print(f"  NOTE L10/L11 must be re-run over runs/{a.run} to score it.\n")


if __name__ == "__main__":
    main()
